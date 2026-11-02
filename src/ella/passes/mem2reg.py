"""mem2reg: promote local slots to SSA values.

The lowering keeps every variable in a slot, so `x = x + 1` reads and writes
memory:

    %1: i64 = load x
    %2: i64 = add %1, %3
    store x, %2

After this pass, slots are gone. Each `load` is replaced by the value last
stored to the slot, and nothing else needs to change: `%2` simply *is* the
new value of x. That form, where every value is defined once and variables
are just names for a series of values, is SSA ("static single assignment").

The interesting part is where paths join. In

    let mut x = 0
    while x < 10
        x += 1

the loop header can be reached with x = 0 (from before the loop) or with
x = %2 (from the end of the body). The header gets a block parameter for x,
and each jump into it passes the value x has on that path:

    entry:      jump while(%zero)
    while(%x):  ... lt %x, %ten ...
    body:       %2 = add %x, %one
                jump while(%2)

To find a slot's value at the start of a block, look at its predecessors:
with one predecessor, it's the value at that predecessor's end; with
several, add a block parameter and ask each predecessor for its value. This
is the algorithm of Braun et al., "Simple and Efficient Construction of
Static Single Assignment Form" (2013), on a complete control-flow graph.
It can add parameters that turn out to receive the same value on every
path; `remove_trivial_params` takes those away again.
"""

from itertools import count

from ella.ir import (
    Block,
    Branch,
    Function,
    Jump,
    Module,
    Value,
    next_value_id,
    predecessors,
    remove_param,
    replace_values,
)


def promote_slots(function: Function, module: Module) -> bool:
    """Replace every local slot of the function by SSA values. Returns whether anything changed."""
    if not function.locals:
        return False
    split_critical_edges(function)
    preds = predecessors(function)
    ids = count(next_value_id(function))

    # The last value each block stores to each slot.
    stored: dict[Block, dict[str, Value]] = {}
    for block in function.blocks:
        stored[block] = {instr.name: instr.args[0] for instr in block.instrs if instr.op == "store"}

    at_start: dict[tuple[str, Block], Value] = {}  # remembered answers of value_at_start

    def value_at_start(slot: str, block: Block) -> Value:
        """The value a slot holds when `block` starts."""
        if (slot, block) in at_start:
            return at_start[(slot, block)]
        if block is function.blocks[0]:
            raise AssertionError(f"{function.name}: slot {slot} is read before it is ever written")
        if len(preds[block]) == 1:
            value = value_at_end(slot, preds[block][0])
        else:
            # Several ways in: the value arrives as a new block parameter.
            # Remember it before asking the predecessors, since in a loop one
            # of them leads back here.
            value = Value(next(ids), function.locals[slot])
            block.params.append(value)
            at_start[(slot, block)] = value
            for predecessor in preds[block]:
                jump = predecessor.terminator
                assert isinstance(jump, Jump) and jump.target is block
                jump.args.append(value_at_end(slot, predecessor))
        at_start[(slot, block)] = value
        return value

    def value_at_end(slot: str, block: Block) -> Value:
        """The value a slot holds when `block` ends."""
        if slot in stored[block]:
            return stored[block][slot]
        return value_at_start(slot, block)

    # Remove the loads and stores, remembering what each load's result really is.
    replacements: dict[int, Value] = {}
    for block in function.blocks:
        current: dict[str, Value] = {}  # values stored so far in this block
        kept = []
        for instr in block.instrs:
            if instr.op == "store":
                current[instr.name] = instr.args[0]
            elif instr.op == "load":
                assert instr.dest is not None
                value = current.get(instr.name)
                replacements[instr.dest.id] = value if value is not None else value_at_start(instr.name, block)
            else:
                kept.append(instr)
        block.instrs = kept
    replace_values(function, replacements)
    function.locals = {}
    remove_trivial_params(function)
    return True


def split_critical_edges(function: Function) -> None:
    """Give a new block to each branch that goes to a block with several predecessors.

    Block parameters are passed by jumps, and a branch can't pass any. So
    before parameters can be added to a block, every way into it must be a
    jump. The empty blocks this adds are removed later by the simplify pass
    where they aren't needed.
    """
    preds = predecessors(function)
    names = {block.name for block in function.blocks}
    for block in list(function.blocks):
        branch = block.terminator
        if not isinstance(branch, Branch):
            continue
        for side in ("then", "else_"):
            target: Block = getattr(branch, side)
            if len(preds[target]) > 1:
                name, n = f"{block.name}.{target.name}", 0
                while name in names:
                    n += 1
                    name = f"{block.name}.{target.name}.{n}"
                names.add(name)
                edge = Block(name, terminator=Jump(target, []))
                function.blocks.append(edge)
                setattr(branch, side, edge)


def remove_trivial_params(function: Function) -> bool:
    """Remove block parameters that always receive the same value (or themselves).

    For example, a variable that a loop never changes gets a parameter at
    the loop header, receiving x from before the loop and the parameter
    itself from the end of the body. It is just x.
    """
    changed_any = False
    changed = True
    while changed:
        changed = False
        preds = predecessors(function)
        for block in function.blocks[1:]:  # the entry's parameters are the function's
            for index, param in enumerate(block.params):
                incoming = set()
                for predecessor in preds[block]:
                    jump = predecessor.terminator
                    assert isinstance(jump, Jump)
                    if jump.args[index].id != param.id:
                        incoming.add(jump.args[index].id)
                if len(incoming) != 1:
                    continue
                [only] = incoming
                value = next(
                    j.args[index]
                    for p in preds[block]
                    if isinstance(j := p.terminator, Jump) and j.args[index].id == only
                )
                remove_param(function, block, index)
                replace_values(function, {param.id: value})
                changed = changed_any = True
                break
            if changed:
                break
    return changed_any
