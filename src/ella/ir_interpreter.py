"""IR interpreter: runs a module of IR (see ella.ir) directly.

This gives the IR its meaning, and lets the tests check that lowering keeps
the program's behavior: every program in test/programs must print the same
thing (and fail with the same error) here as with the AST interpreter.

Running a function is a simple loop: execute the current block's
instructions in order, then follow its terminator to the next block, until
a `return`. Each instruction's result is kept in a dictionary from value id
to run-time value. Because IR values never change, there is nothing else to
keep track of besides the function's local slots.

The operators are the same functions the AST interpreter uses (ella.operators),
so results and error messages match exactly.
"""

import sys
from dataclasses import dataclass, field
from typing import TextIO

from ella.checker import EnumTy
from ella.ir import Block, Branch, Function, Instr, Jump, Module, Return, Trap
from ella.lexer import EllaError, Span
from ella.lower import BINARY_OPS, COMPARE_OPS, compile_program
from ella.operators import binary, compare, contains, index_into, unary
from ella.values import UNIT, FunctionRef, Value, VariantValue, to_text

# The other direction of ella.lower's tables: IR operation -> Ella operator.
OPERATORS = {op: symbol for symbol, op in (BINARY_OPS | COMPARE_OPS).items()}
UNARY = {"neg": "-", "not": "not", "bitnot": "~"}

MAX_CALL_DEPTH = 1000  # the same limit as the AST interpreter
PYTHON_FRAMES_PER_CALL = 10


def run_ir(source: str, out: TextIO | None = None) -> None:
    """Compile a program to IR and run it. Raises EllaError."""
    IRInterpreter(compile_program(source), out).run()


@dataclass(slots=True)
class Frame:
    """One function call's state: computed values, and local slots."""

    values: dict[int, Value] = field(default_factory=dict)
    locals: dict[str, Value] = field(default_factory=dict)


class IRInterpreter:
    def __init__(self, module: Module, out: TextIO | None = None) -> None:
        self.module = module
        self.out = out if out is not None else sys.stdout
        self.globals: dict[str, Value] = {}  # a global is missing until it is first set
        self.depth = 0

    def run(self) -> None:
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, MAX_CALL_DEPTH * PYTHON_FRAMES_PER_CALL))
        try:
            self.call(self.module.functions[self.module.entry], [], Span(1, 1))
        finally:
            sys.setrecursionlimit(limit)

    def call(self, function: Function, args: list[Value], span: Span) -> Value:
        """Run a function from its entry block until it returns."""
        if self.depth >= MAX_CALL_DEPTH:
            raise EllaError(f"stack overflow: more than {MAX_CALL_DEPTH} nested calls", span)
        self.depth += 1
        frame = Frame()
        block = function.blocks[0]
        self.enter(block, args, frame)
        try:
            while True:
                for instr in block.instrs:
                    self.execute(instr, frame)
                match block.terminator:
                    case Jump(target, jump_args):
                        self.enter(target, [frame.values[a.id] for a in jump_args], frame)
                        block = target
                    case Branch(cond, then, else_):
                        block = then if frame.values[cond.id] else else_
                    case Return(value):
                        return frame.values[value.id]
                    case Trap(message, value, trap_span):
                        if value is not None:
                            message = message.replace("{}", to_text(frame.values[value.id], nested=True))
                        raise EllaError(message, trap_span)
                    case None:
                        raise AssertionError(f"{function.name}: {block.name} has no terminator")
        finally:
            self.depth -= 1

    def enter(self, block: Block, args: list[Value], frame: Frame) -> None:
        """Give a block's parameters the values passed to it."""
        for param, arg in zip(block.params, args):
            frame.values[param.id] = arg

    def execute(self, instr: Instr, frame: Frame) -> None:
        """Run one instruction, storing its result (if any) in the frame."""
        args = [frame.values[a.id] for a in instr.args]
        span = instr.span
        result: Value = UNIT
        match instr.op:
            case "const":
                result = instr.const
            case "load":
                result = frame.locals[instr.name]
            case "store":
                frame.locals[instr.name] = args[0]
            case "global_get":
                if instr.name not in self.globals:
                    raise EllaError(f"'{instr.name}' is used before its 'let' has run", span)
                result = self.globals[instr.name]
            case "global_set":
                self.globals[instr.name] = args[0]
            case "neg" | "not" | "bitnot":
                result = unary(UNARY[instr.op], args[0], span)
            case "eq" | "ne" | "lt" | "le" | "gt" | "ge":
                result = compare(OPERATORS[instr.op], args[0], args[1], span)
            case "contains":
                result = contains(args[1], args[0], span)
            case op if op in OPERATORS:  # arithmetic, bitwise and ranges
                result = binary(OPERATORS[op], args[0], args[1], span)
            case "tuple":
                result = tuple(args)
            case "tuple_get":
                container = args[0]
                assert isinstance(container, tuple)
                result = container[instr.index]
            case "list":
                result = args
            case "index":
                container, i = args
                if isinstance(container, range):
                    assert isinstance(i, int)
                    result = container[i]
                else:
                    result = index_into(container, i, span)
            case "set_index":
                container, i, item = args
                index_into(container, i, span)  # check that the index is in range
                assert isinstance(container, list) and isinstance(i, int)
                result = [*container[:i], item, *container[i + 1 :]]  # a new list: lists are values
            case "len":
                container = args[0]
                assert isinstance(container, list | str | range)
                result = len(container)
            case "to_text":
                result = to_text(args[0])
            case "print":
                self.out.write(to_text(args[0]) + "\n")
            case "variant":
                assert instr.dest is not None and isinstance(instr.dest.type, EnumTy)
                result = VariantValue(instr.dest.type.name, instr.name, tuple(args))
            case "is_variant":
                value = args[0]
                result = isinstance(value, VariantValue) and value.variant == instr.name
            case "variant_field":
                value = args[0]
                assert isinstance(value, VariantValue)
                result = value.fields[instr.index]
            case "closure":
                result = FunctionRef(instr.name, tuple(args))
            case "call":
                function, *call_args = args
                assert isinstance(function, FunctionRef)
                callee = self.module.functions[function.name]
                result = self.call(callee, [*function.captured, *call_args], span)
            case "assert":
                if not args[0]:
                    raise EllaError(instr.name, span)
            case op:
                raise AssertionError(f"unknown IR operation {op}")
        if instr.dest is not None:
            frame.values[instr.dest.id] = result
