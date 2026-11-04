"""Register allocation: which IR values live in registers, for the code generator (ella.codegen)."""

from dataclasses import dataclass, field

from ella.checker import BOOL, F32, F64, I32, I64, RANGE, STR, UNIT, ListTy
from ella.ir import (
    Block,
    Branch,
    Function,
    Instr,
    Jump,
    Return,
    Trap,
    Yield,
    defined_by,
    is_array,
    region_level,
    successors,
)

CALLEE_SAVED = ["rbx", "r12", "r13", "r14", "r15"]
CALLER_SAVED = ["rsi", "rdi", "r8", "r9", "r10"]

# Operations whose code never calls a function (other than to report an error, which doesn't return).
NO_CALLS = frozenset(
    {
        "const", "load", "store", "global_get", "global_set", "div", "floordiv", "mod", "bitand", "bitor", "bitxor",
        "neg", "not", "bitnot", "tuple_get", "is_variant", "variant_field", "quote_begin", "enter", "leave", "assert",
        "array_offset",
    }
)  # fmt: skip
COMPARISONS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})


@dataclass(slots=True)
class Allocation:
    """Where a function's values are: the register of each value that has one (by id); the rest have stack slots."""

    registers: dict[int, str] = field(default_factory=dict)
    saved: list[str] = field(default_factory=list)  # the callee-saved registers it uses, which it must save


def calls(instr: Instr) -> bool:
    """Whether an instruction's code calls a function (the runtime, or Ella code), which may change caller-saved
    registers. This must match what ella.codegen generates."""
    op, args = instr.op, instr.args
    if op == "fused" or (
        op in ("add", "sub", "mul", "div", "neg") and instr.dest is not None and is_array(instr.dest.type)
    ):
        return True  # the runtime makes the result
    if op in ("index", "set_index", "len") and is_array(args[0].type):
        return False
    if op in ("floordiv", "mod") and args[0].type == I32:
        return True  # done by the runtime
    if op in NO_CALLS:
        return False
    if op in ("add", "sub", "mul"):
        return args[0].type not in (I64, I32, F64, F32)
    if op in COMPARISONS:
        return args[0].type not in (I64, I32, F64, F32, BOOL, UNIT)
    if op == "len":
        return args[0].type != STR and not isinstance(args[0].type, ListTy)
    if op == "index":
        return args[0].type != RANGE and not isinstance(args[0].type, ListTy)
    if op == "closure":
        return bool(args)
    if op == "set_field":
        return instr.region == "slot"
    return True


def calls_partway(instr: Instr) -> bool:
    """Whether an instruction uses its operands or results after calling a function in its own code."""
    op = instr.op
    if op in ("tuple", "list", "variant", "gen_new") or (op == "closure" and instr.args):
        return True
    if op == "fused" or (
        op in ("add", "sub", "mul", "div", "neg") and instr.dest is not None and is_array(instr.dest.type)
    ):
        return True
    if op == "builtin" and instr.name in ("total", "dot"):
        return True
    if op in ("set_index", "set_field"):
        return instr.region == "slot"
    # A mut argument in a pool's slot: the runtime finds its arena first, kept in the new value's slot.
    return op in ("call", "call_direct") and "slot" in instr.out_regions


def uses_levels(function: Function) -> bool:
    """Whether a function makes objects in its own scope levels, and so takes arenas for them when it starts."""
    return any(
        (region_level(instr.region) is not None and instr.op not in ("set_index", "set_field")) or instr.op == "swap"
        for block in function.blocks
        for instr in block.instrs
    )


def allocate(function: Function, limit: int | None = None) -> Allocation:
    """Choose registers for a function's values. `limit` uses at most that many registers of each kind (for tests)."""
    allocation = Allocation()
    if function.generator:
        return allocation
    callee_saved, caller_saved = CALLEE_SAVED[:limit], CALLER_SAVED[:limit]
    blocks = dominator_order(function)
    live_in, live_after = liveness(function, blocks)

    # The values that must keep their register across a call.
    crossing: set[int] = set()
    in_memory: set[int] = set()
    for block in blocks:
        for instr, after in zip(block.instrs, live_after[block]):
            defined = {v.id for v in defined_by(instr)}
            if calls(instr):
                # Caller-saved registers cannot hold values that survive a call.
                crossing |= after - defined
            if calls_partway(instr):
                crossing |= {a.id for a in instr.args} | defined
            if instr.op in ("call", "call_direct") and "slot" in instr.out_regions:
                in_memory |= {out.id for out in instr.outs}
        if isinstance(block.terminator, Return) and uses_levels(function):
            crossing |= {block.terminator.value.id, *(out.id for out in block.terminator.outs)}

    registers = allocation.registers

    def choose(value_id: int, taken: set[str]) -> None:
        if value_id in in_memory:
            return
        choices = callee_saved if value_id in crossing else caller_saved + callee_saved
        for register in choices:
            if register not in taken:
                registers[value_id] = register
                taken.add(register)
                return

    for block in blocks:
        params = {p.id for p in block.params}
        taken = {registers[v] for v in live_in[block] - params if v in registers}
        for param in block.params:
            if param.id in live_in[block]:
                choose(param.id, taken)
        for instr, after in zip(block.instrs, live_after[block]):
            for value in defined_by(instr):
                if value.id in after:
                    choose(value.id, taken)
            for arg in instr.args:
                if arg.id not in after and arg.id in registers:
                    taken.discard(registers[arg.id])
    allocation.saved = [r for r in CALLEE_SAVED if r in registers.values()]
    return allocation


def dominator_order(function: Function) -> list[Block]:
    """The blocks reachable from the entry, each before the blocks it dominates (a preorder of the dominator tree).

    Dominators by the iterative algorithm of Cooper, Harvey and Kennedy: each
    block's immediate dominator, found from its predecessors' in reverse
    postorder until nothing changes.
    """
    entry = function.blocks[0]
    postorder: list[Block] = []
    seen = {entry}
    stack = [(entry, iter(successors(entry)))]
    while stack:
        block, rest = stack[-1]
        for successor in rest:
            if successor not in seen:
                seen.add(successor)
                stack.append((successor, iter(successors(successor))))
                break
        else:
            stack.pop()
            postorder.append(block)
    number = {block: i for i, block in enumerate(postorder)}
    preds: dict[Block, list[Block]] = {block: [] for block in postorder}
    for block in postorder:
        for successor in successors(block):
            preds[successor].append(block)

    idom: dict[Block, Block] = {entry: entry}

    def intersect(a: Block, b: Block) -> Block:
        while a is not b:
            while number[a] < number[b]:
                a = idom[a]
            while number[b] < number[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for block in reversed(postorder):
            if block is entry:
                continue
            done = [p for p in preds[block] if p in idom]
            new = done[0]
            for p in done[1:]:
                new = intersect(p, new)
            if idom.get(block) is not new:
                idom[block] = new
                changed = True

    children: dict[Block, list[Block]] = {block: [] for block in postorder}
    for block in reversed(postorder):  # reverse postorder keeps the order blocks are reached in
        if block is not entry:
            children[idom[block]].append(block)
    order: list[Block] = []
    work = [entry]
    while work:
        block = work.pop()
        order.append(block)
        work += reversed(children[block])
    return order


def terminator_uses(block: Block) -> set[int]:
    match block.terminator:
        case Jump(_, args):
            return {a.id for a in args}
        case Branch(cond):
            return {cond.id}
        case Return(value, outs):
            return {value.id, *(out.id for out in outs)}
        case Yield(value):
            return {value.id}
        case Trap(value=value) if value is not None:
            return {value.id}
    return set()


def liveness(function: Function, blocks: list[Block]) -> tuple[dict[Block, set[int]], dict[Block, list[set[int]]]]:
    """Which values are live where: at the start of each block (its parameters included, if used), and after
    each of its instructions.

    A value passed by a jump is used at the end of the block that jumps;
    the target's parameter is a new value defined where the target starts.
    """
    reachable = set(blocks)
    live_in: dict[Block, set[int]] = {block: set() for block in blocks}

    def live_out(block: Block) -> set[int]:
        live = terminator_uses(block)
        for successor in successors(block):
            if successor in reachable:
                live |= live_in[successor] - {p.id for p in successor.params}
        return live

    changed = True
    while changed:
        changed = False
        for block in reversed(blocks):
            live = live_out(block)
            for instr in reversed(block.instrs):
                live -= {v.id for v in defined_by(instr)}
                live |= {a.id for a in instr.args}
            if live != live_in[block]:
                live_in[block] = live
                changed = True

    live_after: dict[Block, list[set[int]]] = {}
    for block in blocks:
        live = live_out(block)
        after: list[set[int]] = []
        for instr in reversed(block.instrs):
            after.append(set(live))
            live -= {v.id for v in defined_by(instr)}
            live |= {a.id for a in instr.args}
        after.reverse()
        live_after[block] = after
    return live_in, live_after
