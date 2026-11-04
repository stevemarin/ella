"""Moves: remove copies of values that nothing else uses.

Copying is Ella's semantics (docs/memory-model.md §3): `let mut b = a`
gives b its own copy of a's list, so that changing b can't change a. The
lowering inserts a `copy` wherever a value might otherwise be shared:

    %1: [i64] = list %a, %b @0      # let a = [x, y]
    %2: [i64] = copy %1 @0          # let mut b = a
    set_index %2, %i, %v @0         # b[0] = 5

If nothing uses the original after the copy, nobody can tell the copy from
the original, so the copy can be dropped and the original *moved* into
its place. Here, once mem2reg has turned the variables into values, `%1`
has no other use, and `%2` can simply be `%1`.

This pass does that in the simplest safe case:

* the copy is the original's only use,
* it is in the same block after the source, or control flow proves that the
  source dominates the copy and the copy postdominates the source; cross-block
  cases that can cross a loop are excluded, and
* the original is made entirely of new objects, so no part of it is shared
  with anything else. A list of numbers or strings, a call's result, or an
  earlier copy qualifies; a list of lists built from variables doesn't,
  since its items may belong to those variables. Constants count as new:
  they are static, so a tuple of a number and a string constant can be
  made where its copy would be, and still share the constant.

The original is then made in the copy's region. The copy must still be its
only use, so this optimization does not need general liveness analysis.
"""

from ella.checker import ListTy
from ella.ir import (
    Block,
    Function,
    Instr,
    Module,
    Value,
    count_uses,
    is_array,
    is_boxed,
    predecessors,
    reachable,
    replace_values,
    successors,
)

# Operations whose results are entirely new objects.
NEW = frozenset({"copy", "call", "call_direct", "to_text", "range", "range_incl", "append", "index"})


def move_copies(function: Function, module: Module) -> bool:
    uses = count_uses(function)
    constants = {i.dest.id for b in function.blocks for i in b.instrs if i.op == "const" and i.dest is not None}
    blocks = reachable(function)
    dominators = block_dominators(function, blocks)
    postdominators = block_postdominators(blocks)
    in_cycle = cyclic_blocks(blocks)
    definitions: dict[int, tuple[Block, Instr]] = {
        instr.dest.id: (block, instr) for block in blocks for instr in block.instrs if instr.dest is not None
    }
    replacements: dict[int, Value] = {}
    for block in function.blocks:
        positions = {i.dest.id: index for index, i in enumerate(block.instrs) if i.dest is not None}
        kept = []
        for index, instr in enumerate(block.instrs):
            source_def = definitions.get(instr.args[0].id) if instr.op == "copy" else None
            source_block, source = source_def if source_def is not None else (None, None)
            same_block_before = source_block is block and positions[instr.args[0].id] < index
            crosses_blocks = (
                source_block is not None
                and source_block is not block
                and source_block not in in_cycle
                and not cyclic_on_path(function, blocks, source_block, block, in_cycle)
                and source_block in dominators.get(block, set())
                and block in postdominators.get(source_block, set())
                and block not in in_cycle
            )
            if (
                source is not None
                and (same_block_before or crosses_blocks)
                and uses[instr.args[0].id] == 1
                and movable(source, instr.region, constants)
            ):
                source.region = instr.region
                assert instr.dest is not None
                replacements[instr.dest.id] = instr.args[0]
            else:
                kept.append(instr)
        block.instrs = kept
    replace_values(function, replacements)
    return bool(replacements)


def block_dominators(function: Function, blocks: list[Block]) -> dict[Block, set[Block]]:
    """Blocks that execute on every path from entry to each block."""
    if not blocks:
        return {}
    entry = blocks[0]
    all_blocks = set(blocks)
    preds = predecessors(function)
    dom = {block: ({block} if block is entry else set(all_blocks)) for block in blocks}
    changed = True
    while changed:
        changed = False
        for block in blocks[1:]:
            incoming = [pred for pred in preds[block] if pred in all_blocks]
            shared = set.intersection(*(dom[pred] for pred in incoming)) if incoming else set()
            updated = {block} | shared
            if updated != dom[block]:
                dom[block] = updated
                changed = True
    return dom


def block_postdominators(blocks: list[Block]) -> dict[Block, set[Block]]:
    """Blocks that execute on every path from each block to a function exit."""
    all_blocks = set(blocks)
    exits = {block for block in blocks if not successors(block)}
    postdom = {block: ({block} if block in exits else set(all_blocks)) for block in blocks}
    changed = True
    while changed:
        changed = False
        for block in reversed(blocks):
            outgoing = [successor for successor in successors(block) if successor in all_blocks]
            if not outgoing:
                continue
            shared = set.intersection(*(postdom[successor] for successor in outgoing))
            updated = {block} | shared
            if updated != postdom[block]:
                postdom[block] = updated
                changed = True
    return postdom


def cyclic_blocks(blocks: list[Block]) -> set[Block]:
    """Blocks in a cycle; a copy inside one could run repeatedly for one source value."""
    all_blocks = set(blocks)
    result = set()
    for start in blocks:
        work = [successor for successor in successors(start) if successor in all_blocks]
        seen = set()
        while work:
            block = work.pop()
            if block is start:
                result.add(start)
                break
            if block in seen:
                continue
            seen.add(block)
            work.extend(successor for successor in successors(block) if successor in all_blocks)
    return result


def cyclic_on_path(function: Function, blocks: list[Block], source: Block, target: Block, cyclic: set[Block]) -> bool:
    """Whether a source-to-copy path can pass through a loop."""
    preds = predecessors(function)
    forward = set()
    work = [source]
    while work:
        block = work.pop()
        if block in forward:
            continue
        forward.add(block)
        work.extend(successor for successor in successors(block) if successor in blocks)
    backward = set()
    work = [target]
    while work:
        block = work.pop()
        if block in backward:
            continue
        backward.add(block)
        work.extend(pred for pred in preds[block] if pred in blocks)
    return bool(forward & backward & cyclic)


def movable(source: Instr, region: str, constants: set[int]) -> bool:
    """Whether a value made by `source` is all new objects, and can be made in `region` instead.

    An `append` can't move to another region: its list belongs to a variable,
    which lives where it lives.
    """
    if source.dest is None or not source.region:
        return False
    if source.op == "append":
        return source.region == region
    if source.op in NEW:
        return True
    t = source.dest.type
    if source.op in ("add", "sub", "mul", "div", "neg") and is_array(t):
        return True  # a new array of numbers
    if source.op == "add":  # joining strings, or lists of numbers, makes all new objects
        return not isinstance(t, ListTy) or not is_boxed(t.element)
    # A new list, tuple or variant of numbers and constants.
    parts = [arg for arg in source.args if is_boxed(arg.type) and arg.id not in constants]
    return source.op in ("list", "tuple", "variant") and not parts
