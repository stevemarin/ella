"""Fusion: arithmetic on arrays in a row becomes one `fused` instruction.

`a * 2.0 + b - c` on arrays is three operations, each going over all the
items and making a new array:

    %4: Array[f64] = mul %1, %3 @1
    %5: Array[f64] = add %4, %2 @1
    %7: Array[f64] = sub %5, %6 @1

Two of those arrays are only there to be read by the next operation. As one
instruction, native code goes over the items once, reading a, b and c and
writing the result, with no arrays in between (ella.codegen): for big
arrays, which don't fit in the CPU's caches, that is most of the time saved.

    %7: Array[f64] = fused %1, %3, %2, %6 [mul(0, 1) add(s0, 2) sub(s1, 3)] @1

An operation takes in the one that made its operand in the same block,
with nothing but constants between them. If code outside the group uses
that array too, the step *keeps* it: the loop writes it as well, as an
extra result of the instruction (`let t = a * 2.0` used by `t + b` and by
later code is still read from a once). `total(...)` and (on f64s)
`dot(...)` can end a group: then the loop adds up the items as it goes, and
no array is made at all. So
the steps run in their order, at the last one's place, with nothing able to
see the difference: no other code runs in between, and the arrays they read
can't change. Native code reports an error by running the steps one by one
(ella.codegen), so even errors are the same as before.

The code generator keeps each step's array in a register, and needs a few
for the operands, so a group has at most MAX_STEPS steps, MAX_ARRAYS
arrays and MAX_NUMBERS numbers among its operands. An i64 `*` isn't fused:
AVX2 has no instruction for it. Nor is arithmetic on i32s, which the code
generator only does in plain loops.
"""

from collections import Counter
from collections.abc import Callable
from functools import partial

from ella.checker import F32, F64, I32, I64
from ella.ir import Function, FusedStep, Instr, Module, Value, count_uses, is_array, item_type

ARITHMETIC = ("add", "sub", "mul", "div", "neg")
MAX_STEPS = 6
MAX_ARRAYS = 4
MAX_NUMBERS = 3


def fusable(instr: Instr) -> bool:
    """Arithmetic on arrays that native code can do in a fused loop."""
    if instr.op not in ARITHMETIC or instr.dest is None or not is_array(instr.dest.type):
        return False
    item = item_type(instr.dest.type)
    return item != I32 and not (instr.op == "mul" and item == I64)


def reduction(instr: Instr) -> bool:
    """`total(a)`, or `dot(a, b)` of Array[f64]s, which can end a group."""
    if instr.op != "builtin" or instr.dest is None:
        return False
    return (instr.name == "total" and instr.dest.type != I32) or (
        instr.name == "dot" and instr.dest.type in (F64, F32)
    )


def fuse_arithmetic(function: Function, module: Module) -> bool:
    uses = count_uses(function)
    changed = False
    for block in function.blocks:
        position = {id(instr): i for i, instr in enumerate(block.instrs)}
        made = {instr.dest.id: instr for instr in block.instrs if instr.dest is not None and fusable(instr)}
        # First the groups whose arrays nothing else uses. Then each may take in operations left on their own,
        # keeping their arrays: so keeping never splits a group in two, which would make an array in between.
        plain = groups(block.instrs, made, uses, position, lambda source: False, {})
        alone = {id(i) for i in block.instrs} - {id(member) for group in plain for member in group}
        starts = {id(group[-1]): group for group in plain}
        kept = groups(block.instrs, made, uses, position, partial(is_alone, alone), starts)
        taken: set[int] = set()  # the instructions now part of a fused one
        replaced: dict[int, Instr] = {}
        for group in kept:
            fused = make_fused(group, made, uses)
            if fused is None:
                continue
            taken |= {id(instr) for instr in group}
            replaced[id(group[-1])] = fused
        if replaced:
            block.instrs = [replaced.get(id(i), i) for i in block.instrs if id(i) not in taken or id(i) in replaced]
            changed = True
    return changed


def is_alone(alone: set[int], source: Instr) -> bool:
    return id(source) in alone


def groups(
    instrs: list[Instr],
    made: dict[int, Instr],
    uses: dict[int, int],
    position: dict[int, int],
    may_keep: Callable[[Instr], bool],
    starts: dict[int, list[Instr]],
) -> list[list[Instr]]:
    """The groups of operations to fuse, from the last root up. An operation whose array code outside its group
    uses too joins only if `may_keep` says so (it will be kept). A root in `starts` starts with that group."""
    taken: set[int] = set()
    found = []
    for root in reversed(instrs):
        if id(root) in taken or not (fusable(root) or reduction(root)):
            continue
        group = gather(root, made, uses, instrs, position, taken, may_keep, starts.get(id(root), [root]))
        if len(group) < 2 or make_fused(group, made, uses) is None:
            continue
        taken |= {id(instr) for instr in group}
        found.append(group)
    return found


def gather(
    root: Instr,
    made: dict[int, Instr],
    uses: dict[int, int],
    instrs: list[Instr],
    position: dict[int, int],
    taken: set[int],
    may_keep: Callable[[Instr], bool],
    start: list[Instr],
) -> list[Instr]:
    """The operations to fuse with `root`, in the order they run: `start`, then those that make its operands, and
    theirs, with only constants between them and root."""
    group = list(start)
    members = {id(instr) for instr in group}
    grew = True
    while grew:  # (an operation may only fit once those between it and root have joined)
        grew = False
        for instr in list(group):
            for arg in instr.args:
                source = made.get(arg.id)
                if source is None or id(source) in taken or id(source) in members:
                    continue
                inside = sum(1 for member in group for other in member.args if other.id == arg.id)
                if uses.get(arg.id, 0) != inside and not may_keep(source):  # code outside the group needs it
                    continue
                between = instrs[position[id(source)] + 1 : position[id(root)]]
                if len(group) == MAX_STEPS or not all(i.op == "const" or id(i) in members for i in between):
                    continue
                group.append(source)
                members.add(id(source))
                grew = True
    group.sort(key=lambda instr: position[id(instr)])
    # Anything between the first and root that isn't in the group must be a constant.
    first = position[id(group[0])]
    if not all(i.op == "const" or id(i) in members for i in instrs[first : position[id(root)]]):
        return list(start)
    return group


def make_fused(group: list[Instr], made: dict[int, Instr], uses: dict[int, int]) -> Instr | None:
    """One `fused` instruction doing the group's steps, or None if it needs too many registers."""
    root = group[-1]
    assert root.dest is not None
    step_of = {instr.dest.id: k for k, instr in enumerate(group) if instr.dest is not None}
    inside = Counter(arg.id for instr in group for arg in instr.args)
    operands: list[Value] = []

    def ref(value: Value) -> int:
        if value.id in step_of:
            return -1 - step_of[value.id]
        if value not in operands:
            operands.append(value)
        return operands.index(value)

    steps, outs, out_regions = [], [], []
    for instr in group:
        assert instr.dest is not None
        left = ref(instr.args[0])
        right = ref(instr.args[1]) if len(instr.args) > 1 else None
        kept = instr is not root and uses.get(instr.dest.id, 0) != inside[instr.dest.id]  # used outside too
        if kept:
            outs.append(instr.dest)
            out_regions.append(instr.region)
        steps.append(FusedStep(instr.name if instr.op == "builtin" else instr.op, left, right, instr.span, kept))
    arrays = sum(1 for value in operands if is_array(value.type))
    if arrays + len(outs) > MAX_ARRAYS or len(operands) - arrays > MAX_NUMBERS or arrays == 0:
        return None  # (each array, and each kept one, needs a register to point to its items)
    # A group that ends in a sum makes no array, but if it has to do its steps one by one (natively, when the
    # arrays' shapes differ), they go where the last array would have.
    region = root.region or group[-2].region
    fused = Instr("fused", root.dest, operands, root.span, region=region, steps=tuple(steps))
    fused.outs, fused.out_regions = outs, out_regions
    return fused
