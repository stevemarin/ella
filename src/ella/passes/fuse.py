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

An operation takes in the one that made its operand when nothing else uses
that array, in the same block, with nothing but constants between them. So
the steps run in their order, at the last one's place, with nothing able to
see the difference: no other code runs in between, and the arrays they read
can't change. Native code reports an error by running the steps one by one
(ella.codegen), so even errors are the same as before.

The code generator keeps each step's array in a register, and needs a few
for the operands, so a group has at most MAX_STEPS steps, MAX_ARRAYS
arrays and MAX_NUMBERS numbers among its operands. An i64 `*` isn't fused:
AVX2 has no instruction for it.
"""

from ella.checker import I64
from ella.ir import Function, FusedStep, Instr, Module, Value, count_uses, is_array, item_type

ARITHMETIC = ("add", "sub", "mul", "div", "neg")
MAX_STEPS = 6
MAX_ARRAYS = 4
MAX_NUMBERS = 3


def fusable(instr: Instr) -> bool:
    """Arithmetic on arrays that native code can do in a fused loop."""
    if instr.op not in ARITHMETIC or instr.dest is None or not is_array(instr.dest.type):
        return False
    return not (instr.op == "mul" and item_type(instr.dest.type) == I64)


def fuse_arithmetic(function: Function, module: Module) -> bool:
    uses = count_uses(function)
    changed = False
    for block in function.blocks:
        position = {id(instr): i for i, instr in enumerate(block.instrs)}
        made = {instr.dest.id: instr for instr in block.instrs if instr.dest is not None and fusable(instr)}
        taken: set[int] = set()  # the instructions now part of a fused one
        replaced: dict[int, Instr] = {}
        for root in reversed(block.instrs):
            if id(root) in taken or not fusable(root):
                continue
            group = gather(root, made, uses, block.instrs, position, taken)
            if len(group) < 2:
                continue
            fused = make_fused(group, made)
            if fused is None:
                continue
            taken |= {id(instr) for instr in group}
            replaced[id(root)] = fused
        if replaced:
            block.instrs = [replaced.get(id(i), i) for i in block.instrs if id(i) not in taken or id(i) in replaced]
            changed = True
    return changed


def gather(
    root: Instr,
    made: dict[int, Instr],
    uses: dict[int, int],
    instrs: list[Instr],
    position: dict[int, int],
    taken: set[int],
) -> list[Instr]:
    """The operations to fuse with `root`, in the order they run: those that make its operands, and theirs, while
    nothing else uses their arrays and only constants are between them and root."""
    group = [root]
    members = {id(root)}
    grew = True
    while grew:  # (an operation may only fit once those between it and root have joined)
        grew = False
        for instr in list(group):
            for arg in instr.args:
                source = made.get(arg.id)
                if source is None or id(source) in taken or uses.get(arg.id, 0) != 1 or id(source) in members:
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
        return [root]
    return group


def make_fused(group: list[Instr], made: dict[int, Instr]) -> Instr | None:
    """One `fused` instruction doing the group's steps, or None if it needs too many registers."""
    root = group[-1]
    assert root.dest is not None
    step_of = {instr.dest.id: k for k, instr in enumerate(group) if instr.dest is not None}
    operands: list[Value] = []

    def ref(value: Value) -> int:
        if value.id in step_of:
            return -1 - step_of[value.id]
        if value not in operands:
            operands.append(value)
        return operands.index(value)

    steps = []
    for instr in group:
        left = ref(instr.args[0])
        right = ref(instr.args[1]) if len(instr.args) > 1 else None
        steps.append(FusedStep(instr.op, left, right, instr.span))
    arrays = sum(1 for value in operands if is_array(value.type))
    if arrays > MAX_ARRAYS or len(operands) - arrays > MAX_NUMBERS or arrays == 0:
        return None
    return Instr("fused", root.dest, operands, root.span, region=root.region, steps=tuple(steps))
