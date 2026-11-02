"""The intermediate representation (IR): a typed control-flow graph.

The lowering (ella.lower) turns a checked syntax tree into this IR, and
ella.ir_interpreter runs it. Later stages will optimize it and compile it to
x64.

Structure
---------

A `Module` holds functions. Each `Function` is a list of basic blocks, the
first being its entry. A `Block` is a straight-line list of instructions with
no jumps in or out of the middle, ended by exactly one *terminator* that says
where control goes next:

    jump target(args)         continue in another block
    branch cond, then, else   continue in one of two blocks, by a bool
    return value              leave the function
    trap "message", value     stop the program with a run-time error

Instructions are *three-address code*: each does one small thing to
operands that are already computed, like `%3: i64 = add %1, %2`.

Values
------

Each instruction that produces something defines a new `Value`, written `%n`,
which has a type and is never changed afterwards. Values flow between blocks
in two ways:

* A value can be used in any block that the defining block *dominates*
  (every path from the entry to it passes through the definer).
* Where paths join, such as after an `if`, the result arrives as a *block
  parameter*: `jump join(%4)` passes `%4` to `join(%5: i64)`. Block
  parameters do the job of SSA "phi" nodes.

Variables
---------

Ella variables can change, but values can't. For now, each variable lives
in a named, typed *local slot* of its function, read with `load` and written
with `store`. (A later pass can promote slots to values, which is what "SSA
form" means; it is easier to do as an optimization than in the lowering.)
Top-level variables are *globals*, read and written by name. Reading a
global before it is set is a run-time error.

Lists are values too: `set_index` gives a new list rather than changing one,
which is exactly Ella's value semantics. Reading never needs a copy.

Operations
----------

    const c                      a literal: i64, f64, bool, str or ()
    load slot, store slot, v     read or write a local variable
    global_get name, global_set name, v
    add sub mul div floordiv mod pow    arithmetic (`div` always gives f64;
                                 `add` also joins strings and lists)
    bitand bitor bitxor shl shr  bitwise operations, on i64
    neg not bitnot               unary operations
    eq ne lt le gt ge            comparisons, giving bool
    contains item, container     `in` for lists, strings and ranges
    range lo, hi                 `lo..hi`; `range_incl` for `lo..=hi`
    tuple a, b, ...              build a tuple; `tuple_get t, n` reads item n
    list a, b, ...               build a list
    index c, i                   item i of a list,string (a 1-char string) or range
    set_index xs, i, v           a copy of list xs with item i replaced by v
    len c                        length of a list, string or range
    to_text v                    the text form of any value
    print v                      write the text form of v and a newline
    variant Name(a, ...)         make an enum value
    is_variant v, Name           does v hold that variant?
    variant_field v, Name.n      field n of v, which holds that variant
    closure f(a, ...)            a function value: IR function f with captured values
    call f(a, ...)               call a function value
    assert c, "message"          stop with the message if c is false

Operations that fail at run time (overflow, an index out of range, a failed
assert, ...) carry the source span to report.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import assert_never

from ella.checker import (
    BOOL,
    F64,
    I64,
    RANGE,
    STR,
    UNIT,
    EnumInfo,
    EnumTy,
    FuncTy,
    ListTy,
    PrimTy,
    TupleTy,
    Type,
    show_type,
    substitute,
)
from ella.lexer import Span
from ella.syntax import show_literal

# The literal values `const` can hold. Unit is the empty tuple, as at run time.
type Constant = int | float | bool | str | tuple[()]


@dataclass(eq=False, slots=True)
class Value:
    """A value computed by an instruction or received as a block parameter."""

    id: int
    type: Type

    def __str__(self) -> str:
        return f"%{self.id}"


@dataclass(slots=True)
class Instr:
    """One instruction. Which of `name`, `index` and `const` matter depends on `op`.

    * `name`: the slot, global, variant, function or assert message.
    * `index`: the item of `tuple_get` or field of `variant_field`.
    * `const`: the literal of `const`.
    """

    op: str
    dest: Value | None  # None for instructions that only have an effect
    args: list[Value]
    span: Span
    name: str = ""
    index: int = 0
    const: Constant = ()


@dataclass(slots=True)
class Jump:
    target: Block
    args: list[Value]


@dataclass(slots=True)
class Branch:
    cond: Value
    then: Block
    else_: Block


@dataclass(slots=True)
class Return:
    value: Value


@dataclass(slots=True)
class Trap:
    """Stop with a run-time error. A `{}` in the message is replaced by the value's text."""

    message: str
    value: Value | None
    span: Span


type Terminator = Jump | Branch | Return | Trap


@dataclass(eq=False, slots=True)
class Block:
    name: str
    params: list[Value] = field(default_factory=list)
    instrs: list[Instr] = field(default_factory=list)
    terminator: Terminator | None = None


@dataclass(slots=True)
class Function:
    """A function. Its parameters are the entry block's parameters."""

    name: str
    result: Type
    span: Span
    blocks: list[Block] = field(default_factory=list)
    locals: dict[str, Type] = field(default_factory=dict)  # slot name -> type

    @property
    def params(self) -> list[Value]:
        return self.blocks[0].params


@dataclass(slots=True)
class Module:
    """A whole program. Running it means calling `entry`, which runs the top-level code."""

    functions: dict[str, Function] = field(default_factory=dict)
    globals: dict[str, Type] = field(default_factory=dict)
    enums: dict[str, EnumInfo] = field(default_factory=dict)
    entry: str = "top"


def successors(block: Block) -> list[Block]:
    """The blocks control can go to next."""
    match block.terminator:
        case Jump(target):
            return [target]
        case Branch(_, then, else_):
            return [then, else_]
        case _:
            return []


# Printing


def format_module(module: Module) -> str:
    return "\n\n".join(format_function(function) for function in module.functions.values()) + "\n"


def format_function(function: Function) -> str:
    params = ", ".join(f"{p}: {show_type(p.type)}" for p in function.params)
    lines = [f"function {function.name}({params}) -> {show_type(function.result)}"]
    for slot, t in function.locals.items():
        lines.append(f"    local {slot}: {show_type(t)}")
    for block in function.blocks:
        block_params = ", ".join(f"{p}: {show_type(p.type)}" for p in block.params)
        lines.append(f"  {block.name}({block_params}):" if block.params else f"  {block.name}:")
        lines.extend(f"    {format_instr(instr)}" for instr in block.instrs)
        lines.append(f"    {format_terminator(block.terminator)}")
    return "\n".join(lines)


def format_instr(instr: Instr) -> str:
    args = ", ".join(map(str, instr.args))
    match instr.op:
        case "const":
            operands = "()" if instr.const == () else show_literal(instr.const)  # type: ignore[arg-type]
        case "load" | "global_get":
            operands = instr.name
        case "store" | "global_set":
            operands = f"{instr.name}, {args}"
        case "variant":
            operands = f"{instr.name}({args})" if instr.args else instr.name
        case "is_variant":
            operands = f"{args}, {instr.name}"
        case "variant_field":
            operands = f"{args}, {instr.name}.{instr.index}"
        case "tuple_get":
            operands = f"{args}, {instr.index}"
        case "closure":
            operands = f"{instr.name}({args})"
        case "call":
            callee, *rest = instr.args
            operands = f"{callee}(" + ", ".join(map(str, rest)) + ")"
        case "assert":
            operands = f"{args}, {show_literal(instr.name)}"
        case _:
            operands = args
    text = f"{instr.op} {operands}".rstrip()
    if instr.dest is None:
        return text
    return f"{instr.dest}: {show_type(instr.dest.type)} = {text}"


def format_terminator(terminator: Terminator | None) -> str:
    match terminator:
        case Jump(target, args):
            return f"jump {target.name}(" + ", ".join(map(str, args)) + ")" if args else f"jump {target.name}"
        case Branch(cond, then, else_):
            return f"branch {cond}, {then.name}, {else_.name}"
        case Return(value):
            return f"return {value}"
        case Trap(message, value):
            return f"trap {show_literal(message)}" + (f", {value}" if value is not None else "")
        case None:
            return "<missing terminator>"
        case _:
            assert_never(terminator)


# Verifying


class IRError(Exception):
    """The IR breaks one of its rules. This is a bug in the compiler, not in the Ella program."""


def verify(module: Module) -> None:
    """Check the module's structural and type rules, raising IRError for the first problem.

    The lowering should always produce valid IR; the verifier catches the
    cases where it doesn't, before they turn into confusing behavior later.
    """
    for function in module.functions.values():
        Verifier(module, function).verify()


class Verifier:
    def __init__(self, module: Module, function: Function) -> None:
        self.module = module
        self.function = function

    def fail(self, message: str, block: Block | None = None) -> IRError:
        where = f"{self.function.name}" + (f", {block.name}" if block is not None else "")
        return IRError(f"{where}: {message}")

    def verify(self) -> None:
        blocks = self.function.blocks
        if not blocks:
            raise self.fail("has no blocks")
        names = [block.name for block in blocks]
        if len(set(names)) != len(names):
            raise self.fail("has two blocks with the same name")

        # Each value is defined exactly once: as a block parameter or by an instruction.
        defined_in: dict[int, Block] = {}
        for block in blocks:
            for value in [*block.params, *(i.dest for i in block.instrs if i.dest is not None)]:
                if value.id in defined_in:
                    raise self.fail(f"{value} is defined twice", block)
                defined_in[value.id] = block

        dominators = self.dominators()
        for block in blocks:
            if block.terminator is None:
                raise self.fail("has no terminator", block)
            seen = {p.id for p in block.params}  # values defined so far in this block

            def check_use(value: Value, block: Block = block, seen: set[int] = seen) -> None:
                # A value may be used after its definition in the same block, or
                # anywhere in a block its defining block dominates.
                definer = defined_in.get(value.id)
                if definer is None:
                    raise self.fail(f"{value} is used but never defined", block)
                if definer is block and value.id not in seen:
                    raise self.fail(f"{value} is used before it is defined", block)
                if definer is not block and definer not in dominators[block]:
                    raise self.fail(f"{value} is used where its definition doesn't dominate", block)

            for instr in block.instrs:
                for arg in instr.args:
                    check_use(arg)
                self.check_instr(instr, block)
                if instr.dest is not None:
                    seen.add(instr.dest.id)
            self.check_terminator(block, check_use)

    def dominators(self) -> dict[Block, set[Block]]:
        """For each block, the blocks that every path from the entry to it goes through.

        The classic iterative algorithm: start by assuming every block is
        dominated by all blocks, then repeatedly narrow each block's set to
        itself plus what all its predecessors have in common, until nothing
        changes.
        """
        blocks = self.function.blocks
        entry = blocks[0]
        predecessors: dict[Block, list[Block]] = {block: [] for block in blocks}
        for block in blocks:
            for successor in successors(block):
                if successor not in predecessors:
                    raise self.fail(f"jumps to {successor.name}, which isn't in this function", block)
                predecessors[successor].append(block)
        everything = set(blocks)
        dominators = {block: {block} if block is entry else set(everything) for block in blocks}
        changed = True
        while changed:
            changed = False
            for block in blocks[1:]:
                common = (
                    set.intersection(*(dominators[p] for p in predecessors[block])) if predecessors[block] else set()
                )
                new = {block} | common
                if new != dominators[block]:
                    dominators[block] = new
                    changed = True
        return dominators

    def check_terminator(self, block: Block, check_use: Callable[[Value], None]) -> None:
        match block.terminator:
            case Jump(target, args):
                for arg in args:
                    check_use(arg)
                if [a.type for a in args] != [p.type for p in target.params]:
                    raise self.fail(f"jump to {target.name} passes the wrong number or types of values", block)
            case Branch(cond, then, else_):
                check_use(cond)
                self.require(cond.type == BOOL, "branch condition isn't a bool", block)
                if then.params or else_.params:
                    raise self.fail("branch targets can't take parameters", block)
            case Return(value):
                check_use(value)
                self.require(value.type == self.function.result, "returns a value of the wrong type", block)
            case Trap(_, value):
                if value is not None:
                    check_use(value)

    def require(self, condition: bool, message: str, block: Block) -> None:
        if not condition:
            raise self.fail(message, block)

    def check_instr(self, instr: Instr, block: Block) -> None:
        """Check that an instruction's operands and result have the types its operation needs."""
        args = [a.type for a in instr.args]
        dest = instr.dest.type if instr.dest is not None else None
        effects = ("store", "global_set", "print", "assert")
        self.require((dest is None) == (instr.op in effects), f"{instr.op} has the wrong kind of result", block)

        def need(condition: bool) -> None:
            self.require(condition, f"bad operand or result types for {format_instr(instr)}", block)

        def kind(t: Type) -> str:
            return t.name if isinstance(t, PrimTy) else type(t).__name__

        match instr.op:
            case "const":
                need(dest == constant_type(instr.const))
            case "load":
                need(dest == self.function.locals.get(instr.name))
            case "store":
                need(args == [self.function.locals.get(instr.name)])
            case "global_get":
                need(dest == self.module.globals.get(instr.name))
            case "global_set":
                need(args == [self.module.globals.get(instr.name)])
            case "add":
                need(
                    len(args) == 2 and args[0] == args[1] == dest and kind(args[0]) in ("i64", "f64", "str", "ListTy")
                )
            case "sub" | "mul" | "pow":
                need(len(args) == 2 and args[0] == args[1] == dest and dest in (I64, F64))
            case "div":
                need(len(args) == 2 and args[0] == args[1] and args[0] in (I64, F64) and dest == F64)
            case "floordiv" | "mod" | "bitand" | "bitor" | "bitxor" | "shl" | "shr":
                need(args == [I64, I64] and dest == I64)
            case "neg":
                need(args == [dest] and dest in (I64, F64))
            case "bitnot":
                need(args == [I64] and dest == I64)
            case "not":
                need(args == [BOOL] and dest == BOOL)
            case "eq" | "ne":
                need(len(args) == 2 and args[0] == args[1] and dest == BOOL)
            case "lt" | "le" | "gt" | "ge":
                need(len(args) == 2 and args[0] == args[1] and args[0] in (I64, F64, STR) and dest == BOOL)
            case "contains":
                need(len(args) == 2 and dest == BOOL and args[0] == item_type(args[1]))
            case "range" | "range_incl":
                need(args == [I64, I64] and dest == RANGE)
            case "tuple":
                need(dest == TupleTy(tuple(args)))
            case "tuple_get":
                need(len(args) == 1 and isinstance(args[0], TupleTy) and dest == args[0].items[instr.index])
            case "list":
                need(isinstance(dest, ListTy) and all(a == dest.element for a in args))
            case "index":
                need(len(args) == 2 and args[1] == I64 and dest == item_type(args[0]))
            case "set_index":
                need(len(args) == 3 and isinstance(args[0], ListTy) and args[1:] == [I64, args[0].element])
                need(dest == args[0])
            case "len":
                need(len(args) == 1 and item_type(args[0]) is not None and dest == I64)
            case "to_text":
                need(len(args) == 1 and dest == STR)
            case "print":
                need(len(args) == 1)
            case "variant":
                need(isinstance(dest, EnumTy) and args == list(self.field_types(dest, instr.name, block)))
            case "is_variant":
                need(len(args) == 1 and isinstance(args[0], EnumTy) and dest == BOOL)
                if isinstance(args[0], EnumTy):
                    self.field_types(args[0], instr.name, block)
            case "variant_field":
                need(len(args) == 1 and isinstance(args[0], EnumTy))
                if isinstance(args[0], EnumTy):
                    fields = self.field_types(args[0], instr.name, block)
                    need(instr.index < len(fields) and dest == fields[instr.index])
            case "closure":
                function = self.module.functions.get(instr.name)
                self.require(function is not None, f"closure of unknown function {instr.name}", block)
                assert function is not None
                captured = [p.type for p in function.params[: len(args)]]
                rest = tuple(p.type for p in function.params[len(args) :])
                need(captured == args and dest == FuncTy(rest, function.result))
            case "call":
                need(len(args) >= 1 and isinstance(args[0], FuncTy))
                if isinstance(args[0], FuncTy):
                    need(tuple(args[1:]) == args[0].params and dest == args[0].result)
            case "assert":
                need(args == [BOOL])
            case _:
                raise self.fail(f"unknown operation {instr.op}", block)

    def field_types(self, enum_type: EnumTy, variant: str, block: Block) -> tuple[Type, ...]:
        enum = self.module.enums.get(enum_type.name)
        if enum is None or variant not in enum.variants:
            raise self.fail(f"{show_type(enum_type)} has no variant {variant}", block)
        params = dict(zip(enum.params, enum_type.args))
        return tuple(substitute(f, params) for f in enum.variants[variant])


def constant_type(const: Constant) -> Type:
    match const:
        case bool():  # before int, since bool is a subclass of int
            return BOOL
        case int():
            return I64
        case float():
            return F64
        case str():
            return STR
        case ():
            return UNIT


def item_type(t: Type) -> Type | None:
    """The type of the items of a list, string or range (as `index` and `for` see them)."""
    match t:
        case ListTy(element):
            return element
        case PrimTy("str"):
            return STR
        case PrimTy("range"):
            return I64
        case _:
            return None
