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
* it is in the same block, after it, so the copy is made once each time
  the original is (a copy inside a loop of something made before the loop
  makes a new copy on every iteration, and must stay), and
* the original is made entirely of new objects, so no part of it is shared
  with anything else. A list of numbers or strings, a call's result, or an
  earlier copy qualifies; a list of lists built from variables doesn't,
  since its items may belong to those variables. Constants count as new:
  they are static, so a tuple of a number and a string constant can be
  made where its copy would be, and still share the constant.

The original is then made where the copy would have been, in the copy's
region. Finding last uses in general, with liveness analysis, is left for
later.
"""

from ella.checker import ListTy
from ella.ir import Function, Instr, Module, Value, count_uses, is_boxed, replace_values

# Operations whose results are entirely new objects.
NEW = frozenset({"copy", "call", "call_direct", "to_text", "range", "range_incl", "append", "index"})


def move_copies(function: Function, module: Module) -> bool:
    uses = count_uses(function)
    constants = {i.dest.id for b in function.blocks for i in b.instrs if i.op == "const" and i.dest is not None}
    replacements: dict[int, Value] = {}
    for block in function.blocks:
        made = {i.dest.id: i for i in block.instrs if i.dest is not None}  # made earlier in this block
        kept = []
        for instr in block.instrs:
            source = made.get(instr.args[0].id) if instr.op == "copy" else None
            if source is not None and uses[instr.args[0].id] == 1 and movable(source, instr.region, constants):
                source.region = instr.region
                assert instr.dest is not None
                replacements[instr.dest.id] = instr.args[0]
            else:
                kept.append(instr)
        block.instrs = kept
    replace_values(function, replacements)
    return bool(replacements)


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
    if source.op == "add":  # joining strings, or lists of numbers, makes all new objects
        return not isinstance(t, ListTy) or not is_boxed(t.element)
    # A new list, tuple or variant of numbers and constants.
    parts = [arg for arg in source.args if is_boxed(arg.type) and arg.id not in constants]
    return source.op in ("list", "tuple", "variant") and not parts
