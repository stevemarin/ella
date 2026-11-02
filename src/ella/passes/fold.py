"""Constant folding and propagation: work out at compile time what doesn't depend on run time.

*Folding* evaluates an instruction whose operands are all constants and
replaces it by a `const`:

    %1: i64 = const 2
    %2: i64 = const 3
    %3: i64 = mul %1, %2        becomes        %3: i64 = const 6

*Propagation* carries constants further. A block parameter that receives the
same constant on every path is that constant, and folding the instructions
that use it may make more constants, so the pass repeats until nothing
changes. (The simplify pass then turns a branch on a constant into a jump.)

Folding uses the same operator functions as the interpreters
(ella.operators), so a folded result is exactly what running the program
would give. An operation that would fail at run time, like `1 // 0` or an
overflow, is left alone: the program must still fail when it gets there,
and only if it gets there.
"""

from typing import TypeIs

from ella.ir import Constant, Function, Instr, Jump, Module, predecessors, remove_param
from ella.lexer import EllaError
from ella.operators import binary, compare, contains, unary
from ella.values import to_text

# IR operations that can be folded, and the Ella operator each one is.
BINARY = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "div": "/",
    "floordiv": "//",
    "mod": "%",
    "pow": "**",
    "bitand": "&",
    "bitor": "|",
    "bitxor": "^",
    "shl": "<<",
    "shr": ">>",
}
COMPARE = {"eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
UNARY = {"neg": "-", "not": "not", "bitnot": "~"}


def fold_constants(function: Function, module: Module) -> bool:
    """Fold and propagate constants until nothing changes. Returns whether anything did."""
    changed_any = False
    while fold_once(function) or propagate_params(function):
        changed_any = True
    return changed_any


def constants(function: Function) -> dict[int, Constant]:
    """The values defined by `const` instructions: value id -> constant."""
    return {i.dest.id: i.const for b in function.blocks for i in b.instrs if i.op == "const" and i.dest is not None}


def fold_once(function: Function) -> bool:
    known = constants(function)
    changed = False
    for block in function.blocks:
        for instr in block.instrs:
            if instr.op == "const" or instr.dest is None or not all(a.id in known for a in instr.args):
                continue
            result = evaluate(instr, [known[a.id] for a in instr.args])
            if result is not None:
                # The instruction becomes a constant, keeping its result value.
                instr.op, instr.args, instr.const = "const", [], result
                assert instr.dest is not None
                known[instr.dest.id] = result
                changed = True
    return changed


def evaluate(instr: Instr, args: list[Constant]) -> Constant | None:
    """The result of an instruction on constant operands, or None if it can't be folded."""
    span = instr.span
    try:
        match instr.op:
            case op if op in BINARY:
                result = binary(BINARY[op], args[0], args[1], span)
            case op if op in COMPARE:
                result = compare(COMPARE[op], args[0], args[1], span)
            case op if op in UNARY:
                result = unary(UNARY[op], args[0], span)
            case "contains" if isinstance(args[1], str):
                result = contains(args[1], args[0], span)
            case "len" if isinstance(args[0], str):
                result = len(args[0])
            case "to_text":
                result = to_text(args[0])
            case _:
                return None
    except EllaError:
        return None  # it fails at run time, and must still fail there
    return result if is_constant(result) else None


def is_constant(value: object) -> TypeIs[Constant]:
    """Whether a run-time value can be written as a `const`."""
    return isinstance(value, int | float | str) or value == ()


def same_constant(a: Constant, b: Constant) -> bool:
    """Whether two constants are the same, telling apart 1 and true, and 0.0 and -0.0."""
    return type(a) is type(b) and repr(a) == repr(b)


def propagate_params(function: Function) -> bool:
    """Turn a block parameter that always receives the same constant into that constant."""
    known = constants(function)
    preds = predecessors(function)
    for block in function.blocks[1:]:  # the entry's parameters are the function's arguments
        for index, param in enumerate(block.params):
            incoming = []
            for predecessor in preds[block]:
                jump = predecessor.terminator
                assert isinstance(jump, Jump)
                incoming.append(jump.args[index])
            if not incoming or not all(arg.id in known for arg in incoming):
                continue
            first = known[incoming[0].id]
            if all(same_constant(known[arg.id], first) for arg in incoming):
                # The parameter's value becomes a `const` at the start of the block.
                remove_param(function, block, index)
                block.instrs.insert(0, Instr("const", param, [], function.span, const=first))
                return True
    return False
