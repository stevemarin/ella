"""Lowering: turns a type-checked syntax tree into IR (see ella.ir)."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from itertools import count
from pathlib import Path
from typing import TextIO

from ella.checker import (
    BOOL,
    ERROR,
    F32,
    F64,
    I64,
    LIBRARY,
    NEVER,
    OPERATOR_METHODS,
    RANGE,
    STR,
    UNIT,
    BuiltinTy,
    Checker,
    EnumTy,
    FuncTy,
    ListTy,
    ParamTy,
    TupleTy,
    Type,
    finish,
    has_mutable,
    has_pool,
    is_builtin_error,
    is_nocopy,
    literal_type,
    names_used,
    pattern_names,
    place_root,
    show_type,
    substitute,
)
from ella.ir import (
    VIEWS,
    Block,
    Branch,
    Constant,
    Function,
    Instr,
    Jump,
    Module,
    Return,
    Trap,
    Value,
    Yield,
    allocates,
    carry_level,
    is_array,
    is_boxed,
    outlives,
    shortest,
    successors,
    trim_levels,
    verify,
)
from ella.lexer import EllaError, Span
from ella.parser import parse
from ella.staging import AnySplice, Tables, prepare, start_code
from ella.syntax import (
    Assert,
    Assign,
    Binary,
    Binding,
    BoolLit,
    Break,
    Call,
    Case,
    Compare,
    ConstraintDef,
    Continue,
    EnumDef,
    Expr,
    FieldSplice,
    FloatLit,
    For,
    If,
    Index,
    IntLit,
    Lambda,
    Let,
    LetPattern,
    ListLit,
    LiteralPattern,
    Loop,
    MapLit,
    Match,
    Member,
    MutArg,
    Name,
    NamePattern,
    OrPattern,
    Pattern,
    Program,
    Quote,
    RangePattern,
    SetLit,
    Splice,
    StringLit,
    Try,
    TupleLit,
    TuplePattern,
    TypeLit,
    TypeSplice,
    Unary,
    UnitLit,
    VariantPattern,
    While,
    WildcardPattern,
    is_generator,
    mentions_return,
)
from ella.syntax import (
    Block as BlockExpr,
)
from ella.syntax import (
    Return as ReturnExpr,
)
from ella.syntax import (
    Yield as YieldExpr,
)
from ella.values import STATUS_ERROR

# Ella operators and the IR operations that implement them.
BINARY_OPS = {
    "+": "add",
    "-": "sub",
    "*": "mul",
    "/": "div",
    "//": "floordiv",
    "%": "mod",
    "**": "pow",
    "&": "bitand",
    "|": "bitor",
    "^": "bitxor",
    "<<": "shl",
    ">>": "shr",
    "..": "range",
    "..=": "range_incl",
}
COMPARE_OPS = {"==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
UNARY_OPS = {"-": "neg", "not": "not", "~": "bitnot"}

# In a scope, the slot for a name. SELF marks a function's own name inside its
# body (`let f = ... => f(...)`); it isn't a real slot.
SELF = "<self>"

# In the set of names a piece of code changes in place: it calls an Ella
# function, which may change any global.
CALLS = "<calls>"


def compile_program(
    source: str, out: TextIO | None = None, path: Path | None = None, packages: Sequence[Path] | None = None
) -> Module:
    """Parse, type check and lower a program, and verify the IR. Raises EllaError.

    Splices outside quotes run first, while compiling (see ella.staging);
    what they print goes to `out`. `path` is the program's file, which its
    imports are found from, and then from the package path (`packages`).
    """
    program, checker = prepare(parse(source), out, path, packages)
    module = Lowerer(checker).lower_program(program)
    verify(module)
    return module


@dataclass(frozen=True, slots=True)
class Info:
    """What the lowering knows about a value: the variables it may share objects with, and how long it lives.

    Borrowed variables are named by their slot, or `global name` for globals.
    """

    borrows: frozenset[str] = frozenset()
    lives: str = "global"


@dataclass(slots=True)
class SlotInfo:
    """A local variable's slot.

    `kind` is "local" for `let` and pattern variables, which die when the
    function returns, or "param", "capture" or "mut" (a `mut` parameter).
    `region` is where its value lives. An immutable variable may share its
    value's objects, and `borrows` says with what.
    """

    name: str
    mutable: bool
    kind: str
    region: str
    borrows: frozenset[str] = frozenset()
    binding: int | None = None


@dataclass(slots=True)
class PendingFunction:
    """A lambda whose IR function hasn't been made yet."""

    function: Lambda
    name: str
    captures: list[tuple[str, Type]]  # the captured variables, in parameter order
    self_names: list[str]  # names the function has for itself, from `let`
    type: FuncTy
    subst: dict[str, Type] = field(default_factory=dict)  # a generic function's type arguments


@dataclass(slots=True)
class LocalGeneric:
    """A local generic function template and the values it captures at its binding."""

    name: str
    captures: tuple[tuple[str, str], ...]
    self_names: list[str]


@dataclass(slots=True)
class Carried:
    """A variable a loop carries between iterations: its name, its kind outside the loop, and its slot inside it."""

    name: str
    kind: str  # "local" or "global"
    slot: str


@dataclass(slots=True)
class LoopTarget:
    """Where `break` and `continue` go for one loop being lowered, and the level outside it."""

    label: str | None
    exit: Block  # `break` jumps here, with the value if the exit block takes one
    next: Block  # `continue` jumps here
    outer_level: int
    breaks: list[Value] = field(default_factory=list)  # the values given to `break`


@dataclass(slots=True)
class SelfInfo:
    """What a function needs to make a closure of itself: its name, type, and capture slots."""

    name: str
    type: FuncTy
    capture_slots: list[str] = field(default_factory=list)


# One step into a place: an index (a list's or a pool's) with its value, an array's
# indexes (one for each dimension), or a field.
type Step = tuple[Index | Member, Value | list[Value] | None]


@dataclass(slots=True)
class MutPlace:
    """A `mut` argument being passed: its variable, its steps (indexes and fields), and its region."""

    root: Name
    kind: str
    steps: list[Step]
    region: str
    span: Span


# The most specializations one generic function may have, to stop
# polymorphic recursion like `f[T]` calling `f[[T]]` from going on forever.
MAX_SPECIALIZATIONS = 100


class Lowerer:
    def __init__(self, checker: Checker, module: Module | None = None) -> None:
        """Lower a checked program into a new module, or more code (to `run`) into an existing one."""
        self.checker = checker
        if module is None:
            module = Module(enums=checker.enums, checker=checker, tables=Tables())
            for name, info in checker.globals.vars.items():
                if info.kind == "global" and not info.type_params:
                    module.globals[name] = finish(info.type)
        self.module = module
        self.module.tables.add(checker)
        self.pending: list[PendingFunction] = []
        self.subst: dict[str, Type] = {}  # in a specialized generic function: its type arguments
        self.local_generics: dict[int, LocalGeneric] = {}
        self.local_generic_ids = count(1)
        self.quotes: list[Value] = []  # the `quote_begin`s of the quotes whose splices are being lowered
        self.globals_changed: set[str] = set()  # globals changed in place anywhere

        # The function being lowered. `start_function` resets all of these.
        self.function = Function("", UNIT, Span(1, 1))
        self.within = ""  # the current function's name in stack traces
        self.block = Block("")
        self.next_id = 0  # numbers for new values and blocks
        self.scopes: list[dict[str, str]] = []  # Ella name -> slot, innermost last
        self.loops: list[LoopTarget] = []
        self.exit = Block("")  # where `return` goes
        self.self_info: SelfInfo | None = None
        self.level = 0  # the current scope level
        self.max_level = 0
        self.slots: dict[str, SlotInfo] = {}
        self.info: dict[int, Info] = {}  # value id -> what is known about it
        self.changed: set[str] = set()  # names of variables changed in place in this function
        self.changed_bindings: set[int] = set()
        self.carrying: set[int] = set()  # the levels of the loops being lowered that carry variables
        self.promoted: set[str] = set()  # globals that the loops being lowered carry, in local slots
        self.last_uses: dict[int, str] = {}  # id of an `x = y` -> y, if nothing reads y afterwards
        self.returned: set[int] = set()  # local binding identities this function may return

    # Building blocks

    def start_function(
        self, name: str, result: Type, span: Span, body: Expr | tuple[Expr, ...], within: str = ""
    ) -> None:
        """Start IR function `name`. `within` is its name in stack traces (see `Instr.within`)."""
        self.function = Function(name, result, span)
        self.within = within
        self.module.functions[name] = self.function
        self.next_id = 0
        self.level = self.max_level = 0
        self.block = self.new_block("entry")
        self.scopes, self.loops, self.self_info = [], [], None
        self.slots, self.info = {}, {}
        self.carrying, self.promoted, self.last_uses = set(), set(), {}
        self.changed = changed_in_place(body, self.checker, into_lambdas=False)
        self.changed_bindings = changed_bindings(body, self.checker, into_lambdas=False)
        self.returned = returned_bindings(body, self.checker) if name != "top" else set()

    def finish_function(self) -> None:
        """Remove the blocks that can't be reached from the entry, such as dead blocks."""
        reachable = {self.function.blocks[0]}
        work = [self.function.blocks[0]]
        while work:
            for successor in successors(work.pop()):
                if successor not in reachable:
                    reachable.add(successor)
                    work.append(successor)
        # Keep the blocks in the order they were made, but with the exit last,
        # which reads more naturally.
        blocks = [block for block in self.function.blocks if block in reachable and block is not self.exit]
        self.function.blocks = blocks + ([self.exit] if self.exit in reachable else [])
        self.function.levels = self.max_level + 1
        trim_levels(self.function)

    def new_value(self, t: Type) -> Value:
        self.next_id += 1
        return Value(self.next_id, t)

    def new_block(self, hint: str, level: int | None = None) -> Block:
        """A new block, at the current scope level unless `level` says otherwise."""
        self.next_id += 1
        block = Block(f"{hint}{self.next_id}", level=self.level if level is None else level)
        self.function.blocks.append(block)
        return block

    def emit(
        self, op: str, t: Type, args: list[Value], span: Span, name: str = "", index: int = 0, region: str = ""
    ) -> Value:
        """Add an instruction with a result to the current block, and return the result.

        `region` is where an allocating instruction puts a new object; it is
        dropped for instructions that turn out not to make one (a call with
        an i64 result).
        """
        dest = self.new_value(t)
        instr = Instr(op, dest, args, span, name, index, region=region)
        if op in ("call", "call_direct", "resume"):
            instr.within = self.within
        if not allocates(instr):
            instr.region = ""
        self.block.instrs.append(instr)
        if instr.region:
            self.info[dest.id] = Info(frozenset(), instr.region)
        return dest

    def emit_effect(
        self, op: str, args: list[Value], span: Span, name: str = "", region: str = "", index: int = 0
    ) -> None:
        """Add an instruction that has no result, like `store`."""
        self.block.instrs.append(Instr(op, None, args, span, name, index, region=region))

    def const(self, value: Constant, span: Span) -> Value:
        t = UNIT if isinstance(value, tuple) else literal_type(value)
        dest = self.new_value(t)
        self.block.instrs.append(Instr("const", dest, [], span, const=value))
        return dest

    def jump(self, target: Block, args: list[Value] | None = None) -> None:
        self.block.terminator = Jump(target, args or [])

    def branch(self, cond: Value, then: Block, else_: Block) -> None:
        self.block.terminator = Branch(cond, then, else_)

    def leave(self, span: Span) -> Value:
        """After a jump away (`return`, `break`, ...): continue in a dead block.

        The expression that jumped has type never, so its "value" is never
        used; a placeholder stands in for it.
        """
        self.block = self.new_block("dead")
        return self.const((), span)

    def join_block(self, t: Type, level: int | None = None) -> tuple[Block, Value | None]:
        """A block where branches meet, with a parameter for their value if there is one."""
        join = self.new_block("join", level)
        if t in (UNIT, NEVER):
            return join, None
        param = self.new_value(t)
        join.params.append(param)
        return join, param

    def result_of(self, param: Value | None, values: list[Value], span: Span) -> Value:
        """The value of a joined expression: its join parameter, or () if it has no value.

        The parameter may hold any of `values`, so it shares what they share
        and lives as long as the shortest-lived of them.
        """
        if param is None:
            return self.const((), span)
        infos = [self.info_of(v) for v in values]
        lives = "global"
        for info in infos:
            lives = shortest(lives, info.lives)
        self.info[param.id] = Info(frozenset().union(*(info.borrows for info in infos)), lives)
        return param

    # Memory

    def here(self) -> str:
        """The region of the current scope level: where temporary objects go."""
        return str(self.level)

    def info_of(self, value: Value) -> Info:
        """What is known about a value. Values not recorded are new objects, unboxed values, or constants.

        An unboxed value (a number read from a variable, say) shares nothing
        and lives anywhere, whatever is recorded for it.
        """
        if not is_boxed(value.type):
            return Info()
        return self.info.get(value.id, Info())

    def needs_copy(self, t: Type, info: Info | None = None) -> bool:
        """Whether sharing a value of type `t` could ever be seen: it has parts that can change.

        A value borrowed from a pool or arena always needs copying to be kept: it
        may be freed.
        """
        if info is not None and any(self.from_pool(b) for b in info.borrows):
            return is_boxed(t)
        return has_mutable(t, self.module.enums)

    def from_pool(self, borrowed: str) -> bool:
        """Whether a borrowed variable holds a pool or an arena."""
        if borrowed.startswith("global "):
            t = self.module.globals.get(borrowed.removeprefix("global "))
        else:
            t = self.function.locals.get(borrowed)
        return t is not None and has_pool(t, self.module.enums)

    def unstable(self, borrowed: str) -> bool:
        """Whether a borrowed variable's objects may change in place while the borrower is alive."""
        if borrowed.startswith("global "):
            name = borrowed.removeprefix("global ")
            return name in self.globals_changed and self.checker.globals.vars[name].mutable
        slot = self.slots.get(borrowed)
        return (
            slot is not None
            and slot.mutable
            and (slot.binding in self.changed_bindings if slot.binding is not None else slot.name in self.changed)
        )

    def copy(self, value: Value, region: str, span: Span) -> Value:
        return self.emit("copy", value.type, [value], span, region=region)

    def place(self, value: Value, region: str, span: Span, mutable: bool) -> Value:
        """The value to store in `region`, copying it if it can't be stored there as it is.

        `mutable` says whether the place may change the value in place (a
        `mut` variable or an element of one); then it must not share objects.
        """
        if not is_boxed(value.type):
            return value
        info = self.info_of(value)
        if self.nocopy(value.type):
            # Never copied: the checker makes sure it is made where it stays.
            if not outlives(info.lives, region):
                raise EllaError(f"a {show_type(value.type)} would have to be copied here", span)
            return value
        shared = (
            bool(info.borrows)
            and self.needs_copy(value.type, info)
            and (mutable or any(self.unstable(b) for b in info.borrows))
        )
        if shared or not outlives(info.lives, region):
            return self.copy(value, region, span)
        return value

    def place_return(self, value: Value, span: Span) -> Value:
        """The value to return. It mustn't share objects with anything that outlives the call.

        The function's own local variables die when it returns, so a value
        borrowed only from them can be returned as it is.
        """
        if not is_boxed(value.type):
            return value
        info = self.info_of(value)
        if self.nocopy(value.type):
            # A local variable moves to the caller (the checker allows nothing else).
            if not outlives(info.lives, "result"):
                return self.emit("move", value.type, [value], span, region="result")
            return value
        dying = all(b in self.slots and self.slots[b].kind == "local" for b in info.borrows)
        pooled = any(self.from_pool(b) for b in info.borrows)
        if (self.needs_copy(value.type, info) and (not dying or pooled)) or not outlives(info.lives, "result"):
            return self.copy(value, "result", span)
        return value

    def protect(self, value: Value, later: Sequence[Expr], span: Span, calls: bool = False) -> Value:
        """Copy a value read from a variable if `later` code, run before it is used, changes that variable in place.

        `calls` says that an Ella function is called before the value is
        used, which may change any global.
        """
        info = self.info_of(value)
        if not is_boxed(value.type) or self.nocopy(value.type) or not self.needs_copy(value.type, info):
            return value
        if not info.borrows:
            return value
        changed: set[str] = set()
        changed_ids: set[int] = set()
        for expr in later:
            changed |= changed_in_place(expr, self.checker, into_lambdas=False)
            changed_ids |= changed_bindings(expr, self.checker, into_lambdas=False)
        for borrowed in info.borrows:
            if not self.unstable(borrowed):
                continue
            if borrowed.startswith("global "):
                if calls or CALLS in changed or borrowed.removeprefix("global ") in changed:
                    return self.copy(value, self.here(), span)
            else:
                slot = self.slots[borrowed]
                if slot.binding in changed_ids if slot.binding is not None else slot.name in changed:
                    return self.copy(value, self.here(), span)
        return value

    def view_of(self, value: Value, region: str) -> Info:
        """What is known about a view of a list's items or a string's characters, made in `region`.

        It shares what the list or string shares, and lives no longer than its items (docs/memory-model.md §18).
        """
        info = self.info_of(value)
        return Info(info.borrows, shortest(region, info.lives))

    def merge(self, values: list[Value], region: str) -> Info:
        """What is known about a new object in `region` that holds `values`."""
        lives = region
        borrows: frozenset[str] = frozenset()
        for value in values:
            info = self.info_of(value)
            borrows |= info.borrows
            lives = shortest(lives, info.lives)
        return Info(borrows, lives)

    def build(self, op: str, t: Type, items: list[Value], span: Span, region: str, name: str = "") -> Value:
        """A new tuple, list or variant holding `items`, which it shares."""
        result = self.emit(op, t, items, span, name, region=region)
        self.info[result.id] = self.merge(items, region)
        return result

    def error_fields(self, items: list[Value], span: Span, dest: str) -> list[Value]:
        """The fields of a built-in Error made by `Error(message)` or `Error(message, cause)`: the message, the
        cause as an Optional, and where it is made."""
        cause_type = EnumTy("Optional", (ERROR,))
        if len(items) == 2:
            cause = self.build("variant", cause_type, [items[1]], span, dest, "Some")
        else:
            cause = self.emit("variant", cause_type, [], span, "None", region=dest)
        return [items[0], cause, self.const(str(span), span)]

    def lower_items(self, items: Sequence[Expr], dest: str) -> list[Value]:
        """Lower the items of a list, tuple or variant, protecting each from changes made by later ones."""
        values = []
        for i, item in enumerate(items):
            values.append(self.protect(self.lower(item, dest), items[i + 1 :], item.span))
        return values

    # Variables

    def new_slot(self, name: str, t: Type) -> str:
        """A local slot for a variable. Shadowed names get new slots: `x`, `x.1`, ..."""
        slot, n = name, 0
        while slot in self.function.locals:
            n += 1
            slot = f"{name}.{n}"
        self.function.locals[slot] = t
        return slot

    def home(self, name: str, t: Type | None = None, binding: int | None = None) -> str:
        """The region for a new local variable: the current level, or the caller's if the function returns it.

        A value that can't be copied stays where it is made, and is moved
        out if it is returned, so that it is finalized in the right place.
        """
        if not self.scopes:
            return "global"
        if (
            self.level == 0
            and binding is not None
            and binding in self.returned
            and not (t is not None and self.nocopy(t))
        ):
            return "result"
        return self.here()

    def bind(
        self,
        name: str,
        value: Value,
        span: Span,
        mutable: bool = False,
        region: str = "",
        kind: str = "local",
        binding: int | None = None,
    ) -> Value:
        """Make `name` a variable in the innermost scope, holding `value`. Returns the value stored."""
        if not self.scopes:  # at top level, `let` makes a global
            value = self.place(value, "global", span, mutable)
            self.emit_effect("global_set", [value], span, name)
            self.info[value.id] = Info(frozenset({f"global {name}"}), "global")
            return value
        region = region or self.here()
        value = self.place(value, region, span, mutable)
        # The alternatives of an or-pattern bind the same names, and must use
        # the same slots. Otherwise each binding gets a new slot.
        slot = self.scopes[-1].get(name)
        old = self.slots.get(slot) if slot is not None else None
        if slot is None or old is None or old.kind != "local" or self.function.locals[slot] != value.type:
            slot, old = self.new_slot(name, value.type), None
        self.scopes[-1][name] = slot
        # A mutable variable never shares objects that could change (`place` copied them).
        borrows = frozenset() if mutable else self.info_of(value).borrows
        if old is not None:
            borrows |= old.borrows
            binding = old.binding if old.binding is not None else binding
        self.slots[slot] = SlotInfo(name, mutable, kind, region, borrows, binding)
        self.function.local_regions[slot] = region
        self.emit_effect("store", [value], span, slot)
        self.info[value.id] = Info(frozenset({slot}) | borrows, region)
        return value

    def find_slot(self, name: str) -> str:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise AssertionError(f"the checker resolved '{name}' as local, but it has no slot")

    def read_local(self, name: str, span: Span, dest: str = "") -> Value:
        slot = self.find_slot(name)
        if slot == SELF:
            # A function's own name: rebuild its closure from its own captured values.
            assert self.self_info is not None
            region = dest or self.here()
            captured = [self.read_local(self.slots[s].name, span) for s in self.self_info.capture_slots]
            captured = [self.place(value, region, span, mutable=False) for value in captured]
            return self.emit("closure", self.self_info.type, captured, span, self.self_info.name, region=region)
        info = self.slots[slot]
        value = self.emit("load", self.function.locals[slot], [], span, slot)
        self.info[value.id] = Info(frozenset({slot}) | info.borrows, info.region)
        return value

    def resolve(self, name: str, kind: str) -> str:
        """How to reach a variable: a global that a loop carries is in a local slot while the loop runs."""
        return "local" if kind == "global" and name in self.promoted else kind

    def read_name(self, name: str, kind: str, span: Span, dest: str = "") -> Value:
        kind = self.resolve(name, kind)
        if kind == "local":
            return self.read_local(name, span, dest)
        value = self.emit("global_get", self.module.globals[name], [], span, name)
        self.info[value.id] = Info(frozenset({f"global {name}"}), "global")
        return value

    def write_name(self, name: str, kind: str, value: Value, span: Span) -> None:
        """Store an already placed value in a variable."""
        if self.resolve(name, kind) == "local":
            slot = self.find_slot(name)
            self.emit_effect("store", [value], span, slot)
            self.info[value.id] = Info(frozenset({slot}), self.slots[slot].region)
        else:
            self.emit_effect("global_set", [value], span, name)
            self.info[value.id] = Info(frozenset({f"global {name}"}), "global")

    def var_region(self, name: str, kind: str) -> str:
        """Where a variable's value lives."""
        return self.slots[self.find_slot(name)].region if self.resolve(name, kind) == "local" else "global"

    def build_region(self, region: str) -> str:
        """Where to make a new value for a variable whose value lives in `region`.

        A variable a loop carries holds values from the last iteration, in
        @carryk, but its new values are made in the iteration's arena, @k.
        """
        level = carry_level(region)
        return str(level) if level is not None else region

    # Programs and functions

    def lower_program(self, program: Program) -> Module:
        """Lower the top-level code into the `top` function, then every function it uses."""
        self.globals_changed = changed_in_place(program.body, self.checker, into_lambdas=True)
        self.start_function("top", I64, program.span, program.body)
        # Generic functions are made for each set of type arguments they are
        # used with. So are methods (`Point.norm`, `Stack.push[i64]`), which
        # are generic in their type's parameters. Finalizers are always made.
        for expr in program.body:
            match expr:
                case Let(bindings, Lambda() as generic) if bindings[0].type_params:
                    for binding in bindings:
                        self.module.generics[binding.name] = (generic, binding.type_params)
        for (type_name, method_name), method in self.checker.method_functions.items():
            self.module.generics[f"{type_name}.{method_name}"] = (method, self.module.enums[type_name].params)
        for type_name, info in self.module.enums.items():
            if info.finalize:
                self.specialize(info.finalize, (), program.span)
        # Top-level functions exist from the start (docs/grammar.md §3). Those
        # of other modules than the entry file that nothing uses are left out.
        hoisted: dict[int, Value] = {}
        unused = unused_functions(program)
        for expr in program.body:
            if isinstance(expr, Let) and (expr.bindings[0].type_params or id(expr) in unused):
                continue
            if isinstance(expr, Let) and isinstance(expr.value, Lambda):
                function = self.lower_lambda(expr.value, [b.name for b in expr.bindings], expr.span, "global")
                for binding in expr.bindings:
                    self.emit_effect("global_set", [function], expr.span, binding.name)
                hoisted[id(expr)] = function

        # How many top-level `let`s bind each name.
        lets = Counter(b.name for expr in program.body if isinstance(expr, Let) for b in expr.bindings)
        for expr in program.body:
            if isinstance(expr, Let) and (expr.bindings[0].type_params or id(expr) in unused):
                continue
            if isinstance(expr, Let) and id(expr) in hoisted:
                # Bind it again where its `let` is if the name could hold
                # something else by now: if another top-level `let` binds it
                # (when hoisting, the last one wins), or it can be assigned to.
                for binding in expr.bindings:
                    if binding.mutable or lets[binding.name] > 1:
                        self.emit_effect("global_set", [hoisted[id(expr)]], expr.span, binding.name)
            else:
                self.lower(expr)

        # Then run `main`, if there is one. The top-level code gives the exit status: main's, or 0.
        main = self.checker.globals.vars.get("main")
        status = self.const(0, program.span)
        if main is not None and main.kind == "global":
            main_type = self.module.globals["main"]
            assert isinstance(main_type, FuncTy)
            function = self.emit("global_get", main_type, [], program.span, "main")
            result = self.emit("call", main_type.result, [function], program.span, region=self.here())
            self.block.instrs[-1].within = None  # the program calls main: not in stack traces
            if isinstance(main_type.result, EnumTy) and main_type.result.name == "Result":
                result = self.main_result(result, program.span)
            if result.type == I64:
                status = result
                fail = self.new_block("bad_status")
                fail.terminator = Trap(STATUS_ERROR, status, program.span)
                self.test(self.emit("ge", BOOL, [status, self.const(0, program.span)], program.span), fail)
                self.test(self.emit("le", BOOL, [status, self.const(255, program.span)], program.span), fail)
        self.block.terminator = Return(status)
        self.finish_function()

        while self.pending:
            self.lower_function(self.pending.pop(0))
        return self.module

    def main_result(self, result: Value, span: Span) -> Value:
        """What `main` gave in an Ok. An Err stops the program with its error (ella.values.main_error)."""
        assert isinstance(result.type, EnumTy)
        value_type, error_type = result.type.args
        failed = self.new_block("main_failed")
        self.test(self.emit("is_variant", BOOL, [result], span, "Ok"), failed)
        passed, self.block = self.block, failed
        error = self.variant_field(result, "Err", 0, error_type, span)
        if error_type == ERROR and is_builtin_error(self.checker.enums, "Error"):
            failed.terminator = Trap("", error, span, error=True)
        else:
            failed.terminator = Trap("main returned Err({})", error, span)
        self.block = passed
        return self.variant_field(result, "Ok", 0, value_type, span)

    def lower_lambda(self, function: Lambda, self_names: list[str], span: Span, dest: str = "") -> Value:
        """A lambda expression: a `closure` of a new IR function, capturing the local variables its code names.

        Captures are copies, so it takes only the variables some name in its code (or in the
        lambdas and quotes inside it) may refer to: a name bound inside it too is taken anyway.
        """
        region = dest or self.here()
        visible: dict[str, str] = {}
        for scope in self.scopes:  # outermost first, so inner names shadow outer ones
            visible.update(scope)
        named = names_in(function)
        # Values that can't be copied can't be captured either (the checker makes sure no lambda uses one).
        visible = {
            n: slot
            for n, slot in visible.items()
            if n in named and (slot == SELF or not self.nocopy(self.function.locals[slot]))
        }
        captured = [self.read_local(name, span) for name in visible]
        # Captured values can't change inside the closure, but they mustn't share
        # objects that change outside it, and must live as long as the closure.
        captured = [self.place(value, region, span, mutable=False) for value in captured]
        base = self_names[0] if self_names else "lambda"
        name, n = base, 0
        while name in self.module.functions or name == "top" or any(p.name == name for p in self.pending):
            n += 1
            name = f"{base}.{n}"
        t = self.type_of(function)
        assert isinstance(t, FuncTy)
        captures = [(var, value.type) for var, value in zip(visible, captured)]
        self.pending.append(PendingFunction(function, name, captures, self_names, t, self.subst))
        return self.emit("closure", t, captured, span, name, region=region)

    def lower_local_generic(self, bindings: tuple[Binding, ...], function: Lambda, span: Span) -> None:
        """Register a local generic function and snapshot its lexical captures."""
        visible: dict[str, str] = {}
        for scope in self.scopes:
            visible.update(scope)
        used = names_in(function)
        visible = {
            name: slot
            for name, slot in visible.items()
            if name in used and (slot == SELF or not self.nocopy(self.function.locals[slot]))
        }
        captures = []
        for index, (name, _) in enumerate(visible.items()):
            value = self.place(self.read_local(name, span), self.here(), span, mutable=False)
            slot_name = f"\0generic.{id(bindings[0])}.{index}"
            self.bind(slot_name, value, span)
            captures.append((name, slot_name))
        generic_name = f"{bindings[0].name}.local.{next(self.local_generic_ids)}"
        self.module.generics[generic_name] = (function, bindings[0].type_params)
        local = LocalGeneric(generic_name, tuple(captures), [binding.name for binding in bindings])
        for binding in bindings:
            self.local_generics[id(binding)] = local

    def lower_function(self, pending: PendingFunction) -> None:
        """Make the IR function for a lambda: captures and parameters, `where`, body, and exit."""
        function, t = pending.function, pending.type
        if is_generator(function):
            self.lower_generator(pending)
            return
        self.subst = pending.subst
        self.start_function(pending.name, t.result, function.span, function.body, source_name(pending.name))
        self.function.captures = len(pending.captures)
        self.function.mut_params = t.mutable
        entry = self.block
        self.scopes = [{}]
        self.self_info = SelfInfo(pending.name, t)

        # Captured variables come first, then the declared parameters. Each is
        # stored in a slot, so it is read like any other variable. Their
        # values belong to the caller (or the closure), which outlives every
        # level of this function; a `mut` parameter's lives in its own region.
        for var, var_type in pending.captures:
            param = self.new_value(var_type)
            entry.params.append(param)
            self.info[param.id] = Info(frozenset(), "0")
            self.bind(var, param, function.span, region="0", kind="capture")
            self.self_info.capture_slots.append(self.scopes[0][var])
        for self_name in pending.self_names:
            self.scopes[0][self_name] = SELF
        mut_slots = []
        for i, (declared, param_type) in enumerate(zip(function.params, t.params)):
            param = self.new_value(param_type)
            entry.params.append(param)
            if i in t.mutable:
                self.info[param.id] = Info(frozenset(), f"mut{i}")
                self.bind(declared.name, param, declared.span, True, f"mut{i}", "mut", id(declared))
                mut_slots.append(self.scopes[0][declared.name])
            else:
                self.info[param.id] = Info(frozenset(), "0")
                self.bind(declared.name, param, declared.span, region="0", kind="param", binding=id(declared))

        self.exit = self.new_block("exit")
        result = self.new_value(t.result)
        self.exit.params.append(result)
        self.info[result.id] = Info(frozenset(), "result")

        for cond in function.where:
            if not mentions_return(cond.expr):
                self.lower_condition(cond, "precondition")
        self.jump(self.exit, [self.place_return(self.lower(function.body, "result"), function.span)])

        self.block = self.exit
        postconditions = [cond for cond in function.where if mentions_return(cond.expr)]
        if postconditions:
            self.scopes.append({})
            self.bind("return", result, function.span, region="result")
            for cond in postconditions:
                self.lower_condition(cond, "postcondition")
            self.scopes.pop()
        # The final values of `mut` parameters go back to the caller.
        outs = [self.emit("load", self.function.locals[slot], [], function.span, slot) for slot in mut_slots]
        self.block.terminator = Return(result, outs)
        self.finish_function()

    def lower_generator(self, pending: PendingFunction) -> None:
        """A generator: a function that makes its Gen, and the generator function for its body.

        The first checks the preconditions and makes the Gen with copies of
        its captured values and arguments, which the body (named NAME.body)
        gets as its parameters, in its level 0. They are its own variables,
        which it can change. The body ends by freeing its levels, then yields
        None from then on.
        """
        function, t = pending.function, pending.type
        self.subst = pending.subst
        body_name, n = f"{pending.name}.body", 0
        while body_name in self.module.functions or any(p.name == body_name for p in self.pending):
            n += 1
            body_name = f"{pending.name}.body.{n}"
        self.start_function(pending.name, t.result, function.span, function.body, source_name(pending.name))
        self.function.captures = len(pending.captures)
        self.scopes = [{}]
        values = self.generator_params(pending, function.span, "param")
        for cond in function.where:
            self.lower_condition(cond, "precondition")
        made = self.emit("gen_new", t.result, values, function.span, body_name, region="result")
        self.block.terminator = Return(made)
        self.finish_function()

        assert isinstance(t.result, BuiltinTy)
        result = EnumTy("Optional", t.result.args)
        self.start_function(body_name, result, function.span, function.body, source_name(pending.name))
        self.function.generator = True
        self.returned = set()  # its variables live on: nothing is built in the caller's region
        self.scopes = [{}]
        self.generator_params(pending, function.span, "local")
        self.exit = self.new_block("exit")
        self.lower(function.body)  # its value is unused
        self.jump(self.exit)
        self.block = self.exit
        self.emit_effect("reset", [], function.span, index=0)
        done, again = self.new_block("done"), self.new_block("again")
        self.jump(done)
        self.block = done
        none = self.emit("variant", result, [], function.span, "None", region="result")
        self.block.terminator = Yield(none, again)
        again.terminator = Jump(done, [])
        self.finish_function()

    def generator_params(self, pending: PendingFunction, span: Span, kind: str) -> list[Value]:
        """A generator's captured values and parameters, as the parameters of the function being lowered.

        In the body (`kind` "local"), its values are in its own level 0, and
        its parameters are variables it may change.
        """
        entry = self.block
        self.self_info = SelfInfo(pending.name, pending.type)
        values = []
        for var, var_type in pending.captures:
            param = self.new_value(var_type)
            entry.params.append(param)
            self.info[param.id] = Info(frozenset(), "0")
            self.bind(var, param, span, region="0", kind="capture")
            self.self_info.capture_slots.append(self.scopes[0][var])
            values.append(param)
        for self_name in pending.self_names:
            self.scopes[0][self_name] = SELF
        for declared, param_type in zip(pending.function.params, pending.type.params):
            param = self.new_value(param_type)
            entry.params.append(param)
            self.info[param.id] = Info(frozenset(), "0")
            self.bind(declared.name, param, declared.span, kind == "local", "0", kind, id(declared))
            values.append(param)
        return values

    def lower_yield(self, value_expr: Expr, span: Span) -> Value:
        """`yield value`: give Some(value) to `resume`'s caller, and carry on in a new block next time.

        The generator's variables live on, so a value borrowed from them that
        could change, or that doesn't outlive the caller's region, is copied.
        """
        value = self.lower(value_expr, "result")
        if is_boxed(value.type):
            info = self.info_of(value)
            if (info.borrows and self.needs_copy(value.type, info)) or not outlives(info.lives, "result"):
                value = self.copy(value, "result", span)
        some = self.build("variant", self.function.result, [value], span, "result", "Some")
        after = self.new_block("resume")
        self.block.terminator = Yield(some, after)
        self.block = after
        return self.const((), span)

    def lower_condition(self, cond: Assert, kind: str) -> None:
        value = self.lower(cond.expr)
        self.emit_effect("assert", [value], cond.span, f"{kind} failed: {cond.source}")

    # Expressions

    def lower(self, expr: Expr, dest: str = "") -> Value:
        """Emit the instructions that compute `expr`, and return its value.

        `dest` is the region the value will end up in, where new objects for
        it are made. By default that is the current level: a temporary.
        """
        dest = dest or self.here()
        span = expr.span
        match expr:
            # Literals
            case IntLit(literal) | FloatLit(literal) | BoolLit(literal):
                return self.const(literal, span)
            case StringLit(parts):
                return self.lower_string(parts, span, dest)
            case UnitLit():
                return self.const((), span)
            case TupleLit(items):
                return self.build("tuple", self.type_of(expr), self.lower_items(items, dest), span, dest)
            case ListLit(items):
                return self.build("list", self.type_of(expr), self.lower_items(items, dest), span, dest)

            # Variables
            case Name(name):
                kind = self.checker.kinds[id(expr)]
                if kind == "local" and any(scope.get(name) == SELF for scope in reversed(self.scopes)):
                    return self.read_name(name, kind, span, dest)
                if kind == "variant":
                    return self.emit("variant", self.type_of(expr), [], span, name, region=dest)
                if (wrapper := self.checker.wrappers.get(id(expr))) is not None:
                    # `print`, `len` or a constructor as a value: a lambda that calls it, which captures nothing.
                    scopes, self.scopes = self.scopes, []
                    try:
                        return self.lower_lambda(wrapper, [], span, dest)
                    finally:
                        self.scopes = scopes
                if id(expr) in self.checker.instances:
                    args = tuple(self.concrete(arg) for arg in self.checker.instances[id(expr)])
                    binding = self.checker.resolved[id(expr)].binder
                    if isinstance(binding, Binding) and id(binding) in self.local_generics:
                        local = self.local_generics[id(binding)]
                        captured = [
                            self.place(self.read_local(slot, span), dest, span, mutable=False)
                            for _, slot in local.captures
                        ]
                        captures = [(name, value.type) for (name, _), value in zip(local.captures, captured)]
                        function = self.specialize(local.name, args, span, captures, local.self_names, self.subst)
                        return self.emit("closure", self.type_of(expr), captured, span, function, region=dest)
                    function = self.specialize(name, args, span)
                    return self.emit("closure", self.type_of(expr), [], span, function, region=dest)
                if kind == "builtin":  # a library function as a value
                    t = self.type_of(expr)
                    assert isinstance(t, FuncTy)
                    return self.emit("closure", t, [], span, self.library_function(name, t, span), region=dest)
                return self.read_name(name, kind, span, dest)
            case Let(bindings, value_expr):
                if bindings[0].type_params and isinstance(value_expr, Lambda) and self.scopes:
                    self.lower_local_generic(bindings, value_expr, span)
                    return self.const((), span)
                t = self.type_of(value_expr)
                region = self.home(bindings[0].name, t, id(bindings[0]))
                if isinstance(value_expr, Lambda):
                    value = self.lower_lambda(value_expr, [b.name for b in bindings], span, region)
                else:
                    value = self.lower(value_expr, region)
                for binding in bindings:
                    home = self.home(binding.name, t, id(binding))
                    value = self.bind(binding.name, value, binding.span, binding.mutable, home, binding=id(binding))
                return value
            case LetPattern(pattern, value_expr, _, mutable):
                value = self.lower(value_expr, self.here() if self.scopes else "global")
                no_match = self.new_block("nomatch")
                self.lower_pattern(pattern, value, no_match, mutable)
                matched = self.block
                self.block = no_match
                self.block.terminator = Trap("the 'let' pattern doesn't match {}", value, span)
                self.block = matched
                return value
            case Assign(op, target, value_expr):
                if (update := self.checker.updates.get(id(expr))) is not None:
                    # `v[k] op= x` changing the item in place, with v's `update` method.
                    self.lower(update)
                    return self.const((), span)
                if isinstance(target, Name):
                    return self.lower_assign_name(op, target, value_expr, span, self.last_uses.get(id(expr), ""))
                if isinstance(target, Index) and self.has_method(self.type_of(target.target), "set"):
                    return self.lower_assign_set(op, target, value_expr, span)
                return self.lower_assign_place(op, target, value_expr, span)

            # Operators
            case Unary(op, operand):
                value = self.lower(operand)
                if op == "-" and self.has_method(value.type, "neg"):
                    return self.call_method("neg", [value], self.type_of(expr), span, dest)
                return self.emit(UNARY_OPS[op], self.type_of(expr), [value], span, region=dest)  # (an array's)
            case Binary(("and" | "or") as op, left, right):
                return self.lower_logic(op, left, right, span)
            case Binary(op, left, right):
                return self.lower_binary(op, left, right, self.type_of(expr), span, dest)
            case Compare(first, rest):
                return self.lower_compare(first, rest, span)
            case Index(target, index):
                container = self.lower(target)
                if self.has_method(container.type, "get"):
                    return self.call_method("get", [container, self.lower(index)], self.type_of(expr), span, dest)
                if is_array(container.type):
                    indexes = self.array_indexes(index)
                    if any(i.type == RANGE for i in indexes):  # a slice: a new array
                        return self.emit("array_slice", container.type, [container, *indexes], span, region=dest)
                    return self.array_item(container, indexes, self.type_of(expr), span)
                if isinstance(container.type, BuiltinTy):
                    return self.pool_get(container, self.lower(index), self.type_of(expr), span)
                return self.lower_index(container, self.lower(index), self.type_of(expr), span, dest)
            case Member(target):
                struct = self.lower(target)
                position = self.checker.members[id(expr)]
                if isinstance(struct.type, TupleTy):
                    item = self.emit("tuple_get", self.type_of(expr), [struct], span, index=position)
                    self.info[item.id] = self.info_of(struct)
                    return item
                return self.field(struct, position, self.type_of(expr), span)
            case Quote():
                return self.lower_quote(expr, span)
            case TypeLit(texpr):
                # A type as a value, which only the interpreters can make. The
                # operands are the types spliced into it (`type [$t]`), if any.
                number = len(self.module.types)
                self.module.types.append((texpr, self.concrete(self.checker.type_values[id(expr)])))
                splices = [self.lower(node.code) for node in type_splices(texpr)]
                return self.emit("type_value", self.type_of(expr), splices, span, index=number)
            case FieldSplice():
                raise AssertionError("the checker only allows field splices in quotes")
            case Splice():
                raise AssertionError("splices outside quotes are expanded before lowering")
            case Try(operand):
                return self.lower_try(self.lower(operand), self.type_of(expr), span)
            case MutArg():
                raise AssertionError("the checker rejects 'mut' outside calls")

            # Functions
            case Lambda():
                return self.lower_lambda(expr, [], span, dest)
            case Call(callee, args):
                return self.lower_call(expr, callee, args, span, dest)
            case ReturnExpr(value) if self.function.generator:
                self.jump(self.exit)  # a generator's `return` has no value
                return self.leave(span)
            case ReturnExpr(value):
                result = self.const((), span) if value is None else self.lower(value, "result")
                self.jump(self.exit, [self.place_return(result, span)])
                return self.leave(span)
            case YieldExpr(value_expr):
                return self.lower_yield(value_expr, span)

            # Control flow
            case BlockExpr(exprs):
                self.scopes.append({})
                declared: set[str] = set()
                for i, line in enumerate(exprs):
                    match line:
                        case Assign("=", Name(), Name(name)) if name in declared:
                            if name not in names_used(exprs[i + 1 :], inside_lambdas=True):
                                self.last_uses[id(line)] = name
                        case Let(bindings):
                            declared |= {binding.name for binding in bindings}
                        case LetPattern(pattern):
                            declared |= {node.name for node in pattern_names(pattern)}
                for line in exprs[:-1]:
                    self.lower(line)
                result = self.lower(exprs[-1], dest)
                self.scopes.pop()
                # A block that ends with an assignment has the value ().
                return self.const((), span) if isinstance(exprs[-1], Assign) else result
            case If(cond, then, else_):
                return self.lower_if(cond, then, else_, self.type_of(expr), span, dest)
            case Match(subject):
                return self.lower_match(expr, self.lower(subject), span, dest)
            case For():
                return self.lower_for(expr)
            case While(label, cond, body):
                # header: evaluate cond, then go to body or exit. body: ..., back to header.
                names = self.carried_by(expr)
                outer = self.enter_loop(bool(names))
                carried = self.begin_carrying(names, span)
                header, body_block = self.new_block("while"), self.new_block("body")
                latch = self.latch(carried, header, span)
                exit = self.new_block("exit", outer)
                self.jump(header)
                self.block = header
                self.end_iteration(carried, span)
                self.branch(self.lower(cond), body_block, exit)
                self.block = body_block
                self.lower_loop_body(LoopTarget(label, exit, latch, outer), body)
                self.jump(latch)
                self.leave_loop(exit, outer, span, carried)
                return self.const((), span)
            case Loop(label, body):
                names = self.carried_by(expr)
                outer = self.enter_loop(bool(names))
                carried = self.begin_carrying(names, span)
                body_block = self.new_block("loop")
                latch = self.latch(carried, body_block, span)
                exit, param = self.join_block(self.type_of(expr), outer)
                self.jump(body_block)
                self.block = body_block
                self.end_iteration(carried, span)
                loop = LoopTarget(label, exit, latch, outer)
                self.lower_loop_body(loop, body)
                self.jump(latch)
                self.leave_loop(exit, outer, span, carried)
                return self.result_of(param, loop.breaks, span)
            case Break(label, value):
                loop = self.find_loop(label)
                if loop.exit.params:  # a `loop` with a value, which goes to the level outside the loop
                    region = str(loop.outer_level)
                    broken = self.const((), span) if value is None else self.lower(value, region)
                    broken = self.place(broken, region, span, mutable=False)
                    loop.breaks.append(broken)
                    self.jump(loop.exit, [broken])
                else:
                    if value is not None:
                        self.lower(value)
                    self.jump(loop.exit)
                return self.leave(span)
            case Continue(label):
                self.jump(self.find_loop(label).next)
                return self.leave(span)

            # Others
            case Assert():
                self.lower_condition(expr, "assertion")
                return self.const((), span)
            case EnumDef() | ConstraintDef():
                return self.const((), span)  # enums and constraints are only types; nothing runs
            case MapLit() | SetLit():
                raise AssertionError("linking makes map and set literals calls")

    def type_of(self, expr: Expr) -> Type:
        return self.concrete(self.checker.types[id(expr)])

    def concrete(self, t: Type) -> Type:
        """A checked type, with a specialized generic function's type arguments filled in."""
        t = finish(t)
        return substitute(t, self.subst) if self.subst else t

    def nocopy(self, t: Type) -> bool:
        return is_nocopy(t, self.module.enums)

    def lower_string(self, parts: tuple[str | Expr, ...], span: Span, dest: str) -> Value:
        """`"a{{x}}b"` becomes `"a" + to_text(x) + "b"`. Only the final string goes in `dest`."""
        count = len(parts)
        pieces = []
        for i, part in enumerate(parts):
            if isinstance(part, str):
                pieces.append(self.const(part, span))
            else:
                value = self.lower(part)
                region = dest if count == 1 else self.here()
                pieces.append(value if value.type == STR else self.emit_text(value, part.span, region))
        if not pieces:
            return self.const("", span)
        result = pieces[0]
        for i, piece in enumerate(pieces[1:], 2):
            result = self.emit("add", STR, [result, piece], span, region=dest if i == len(pieces) else self.here())
        return result

    def lower_binary(self, op: str, left: Expr, right: Expr, t: Type, span: Span, dest: str) -> Value:
        """Arithmetic, joining strings and lists, and ranges."""
        if op == "+" and isinstance(t, ListTy):
            # The new list holds the operands' items, so if they are objects,
            # they should already be where the result is going.
            item_dest = dest if is_boxed(t.element) else self.here()
            a = self.protect(self.lower(left, item_dest), [right], span)
            b = self.lower(right, item_dest)
            result = self.emit("add", t, [a, b], span, region=dest)
            if is_boxed(t.element):
                info = self.merge([a, b], dest)
                borrows = info.borrows if self.needs_copy(t.element) else frozenset()
                self.info[result.id] = Info(borrows, info.lives)
            return result
        a = self.protect(self.lower(left), [right], span)
        b = self.lower(right)
        return self.operate(op, a, b, t, span, dest)

    def operate(self, op: str, a: Value, b: Value, t: Type, span: Span, region: str) -> Value:
        """A binary operator on two values, which may be a method of a struct or enum (`a + b` is `a.add(b)`)."""
        if op in OPERATOR_METHODS and self.has_method(a.type, OPERATOR_METHODS[op]):
            return self.call_method(OPERATOR_METHODS[op], [a, b], t, span, region)
        if op == "/" and a.type == F32 and t == F64:
            # `/` on a type parameter gives an f64 (docs/grammar.md §9): f32s are made f64s first, as i64s are.
            a, b = (self.emit("builtin", F64, [v], span, "to_f64") for v in (a, b))
        return self.emit(BINARY_OPS[op], t, [a, b], span, region=region)

    def lower_index(self, container: Value, index: Value, t: Type, span: Span, dest: str) -> Value:
        """`xs[i]` is an item already in the list; `s[i]` makes a new string, and a slice a view of the items."""
        if index.type == RANGE:
            region = dest or self.here()
            result = self.emit("slice", t, [container, index], span, region=region)
            self.info[result.id] = self.view_of(container, region)
            return result
        result = self.emit("index", t, [container, index], span, region=dest or self.here())
        if isinstance(container.type, ListTy):
            self.info[result.id] = self.info_of(container)
        return result

    def array_indexes(self, index: Expr) -> list[Value]:
        """An array's indexes, one for each dimension: `a[i, j]` gives i and j, without making a tuple."""
        if isinstance(index, TupleLit):
            return [self.lower(item) for item in index.items]
        value = self.lower(index)
        if not isinstance(value.type, TupleTy):
            return [value]
        return [self.emit("tuple_get", t, [value], index.span, index=i) for i, t in enumerate(value.type.items)]

    def array_item(self, array: Value, indexes: list[Value], t: Type, span: Span) -> Value:
        """`a[i, j]`: a number, from where `array_offset` finds it in the items."""
        offset = self.emit("array_offset", I64, [array, *indexes], span)
        return self.emit("index", t, [array, offset], span)

    def lower_assign_name(self, op: str, target: Name, value_expr: Expr, span: Span, moved: str = "") -> Value:
        """`x = v` and `x op= v`. `moved` is a variable v reads for the last time, whose value can move to x."""
        kind = self.checker.kinds[id(target)]
        if is_append(Assign(op, target, value_expr, span), self.checker):
            items = value_expr.right if isinstance(value_expr, Binary) and op == "=" else value_expr
            return self.lower_append(target, kind, items, span)
        region = self.build_region(self.var_region(target.name, kind))
        value = self.lower(value_expr, region if op == "=" else "")
        if op != "=":
            old = self.read_name(target.name, kind, target.span)
            value = self.operate(op.removesuffix("="), old, value, old.type, span, region)
        elif moved:
            # Nothing reads the variable again, so sharing its value can't be seen.
            info = self.info_of(value)
            self.info[value.id] = Info(info.borrows - {self.find_slot(moved)}, info.lives)
        value = self.place(value, region, span, mutable=True)
        self.write_name(target.name, kind, value, span)
        return value

    def lower_append(self, target: Name, kind: str, items_expr: Expr, span: Span) -> Value:
        """`xs += ys` (or `xs = xs + ys`): add copies of ys's items to xs in place."""
        region = self.var_region(target.name, kind)
        items = self.lower(items_expr)
        old = self.read_name(target.name, kind, target.span)
        new = self.emit("append", old.type, [old, items], span, region=region)
        self.write_name(target.name, kind, new, span)
        return new

    def lower_assign_place(self, op: str, target: Index | Member, value_expr: Expr, span: Span) -> Value:
        """`xs[i].y = v` and `xs[i].y op= v`: change the last list, struct, pool or arena in place.

        A pool's values live in arenas of their own (one per slot), and an
        arena's values in its own slot arena, so a value stored into one is
        copied there by the operation itself (`@slot`).
        """
        root, chain = place_steps(target)
        kind = self.checker.kinds[id(root)]
        region = self.var_region(root.name, kind)
        in_pool = any(self.is_pool_step(node) for node in chain)
        value = self.lower(value_expr, region if op == "=" and not in_pool else "")
        value = self.protect(value, [node.index for node in chain if isinstance(node, Index)], span)
        container = self.read_name(root.name, kind, root.span)
        for node in chain[:-1]:
            container, region = self.step_into(container, (node, self.step_key(node)), region)
        last = chain[-1]
        key = self.step_key(last)
        if op != "=":
            # Reading the old value also checks the index or handle.
            old, old_region = self.step_into(container, (last, key), region)
            if op == "+=" and isinstance(old.type, ListTy):
                # Add to the list in place, like `xs += ys` (it may move, so it is stored back).
                value = self.emit("append", old.type, [old, value], span, region=old_region)
            else:
                new_region = self.here() if region == "slot" or self.is_pool_step(last) else region
                value = self.operate(op.removesuffix("="), old, value, old.type, span, new_region)
        self.store_step(container, (last, key), value, region, span)
        self.info[value.id] = Info(frozenset({self.borrow_key(root.name, kind)}), region)
        return value

    def is_pool_step(self, node: Index | Member) -> bool:
        if not isinstance(node, Index):
            return False
        t = self.type_of(node.target)
        return isinstance(t, BuiltinTy) and not is_array(t)

    def step_key(self, node: Index | Member) -> Value | list[Value] | None:
        """Evaluate a step's index, or an array's indexes (fields need nothing)."""
        if isinstance(node, Index) and is_array(self.type_of(node.target)):
            return self.array_indexes(node.index)
        return self.lower(node.index) if isinstance(node, Index) else None

    def step_into(self, container: Value, step: Step, region: str, span: Span | None = None) -> tuple[Value, str]:
        """The part of `container` a step names, borrowed, and the region it lives in.

        Errors are reported at the step, or at `span` if given.
        """
        node, key = step
        t = self.type_of(node)
        where = span or node.span
        if isinstance(node, Member):
            return self.field(container, self.checker.members[id(node)], t, where), region
        assert key is not None
        if isinstance(key, list):
            return self.array_item(container, key, t, where), region
        if isinstance(container.type, BuiltinTy):
            return self.pool_get(container, key, t, where), "slot"
        return self.lower_index(container, key, t, where, ""), region

    def store_step(
        self, container: Value, step: Step, value: Value, region: str, span: Span, at_step: bool = True
    ) -> None:
        """Change the part of `container` a step names to `value`, in place.

        Errors are reported at the step, or at `span` unless `at_step`.
        """
        node, key = step
        where = node.span if at_step else span
        if isinstance(key, list):  # an array's item, a number
            offset = self.emit("array_offset", I64, [container, *key], where)
            self.emit_effect("set_index", [container, offset, value], where, region=region)
            return
        if isinstance(container.type, BuiltinTy):
            assert key is not None and not isinstance(key, list)
            self.emit_effect("pool_set", [container, key, value], where)
            return
        if region != "slot":
            value = self.place(value, region, span, mutable=True)
        if isinstance(node, Member):
            enum = container.type
            assert isinstance(enum, EnumTy)
            index = self.checker.members[id(node)]
            self.block.instrs.append(
                Instr("set_field", None, [container, value], where, enum.name, index, region=region)
            )
        else:
            assert key is not None and not isinstance(key, list)
            self.emit_effect("set_index", [container, key, value], where, region=region)

    def field(self, struct: Value, index: int, t: Type, span: Span) -> Value:
        """A struct's field, borrowed."""
        assert isinstance(struct.type, EnumTy)
        return self.variant_field(struct, struct.type.name, index, t, span)

    def pool_get(self, pool: Value, handle: Value, t: Type, span: Span) -> Value:
        """A pool's value, borrowed: it lives in the slot's own arena, until the slot is freed.

        Or an arena's value, which lives in the arena until all of it is freed.
        """
        result = self.emit("pool_get", t, [pool, handle], span)
        self.info[result.id] = self.info_of(pool)
        return result

    def borrow_key(self, name: str, kind: str) -> str:
        return self.find_slot(name) if kind == "local" else f"global {name}"

    def lower_logic(self, op: str, left: Expr, right: Expr, span: Span) -> Value:
        """`a and b` only evaluates `b` if `a` is true; `a or b` only if `a` is false."""
        a = self.lower(left)
        rhs, short = self.new_block("rhs"), self.new_block("short")
        join, param = self.join_block(BOOL)
        assert param is not None
        if op == "and":
            self.branch(a, rhs, short)
        else:
            self.branch(a, short, rhs)
        self.block = short
        self.jump(join, [self.const(op == "or", span)])
        self.block = rhs
        self.jump(join, [self.lower(right)])
        self.block = join
        return param

    def lower_compare(self, first: Expr, rest: tuple[tuple[str, Expr], ...], span: Span) -> Value:
        """`a < b <= c`: compare in turn, stopping with false at the first comparison that fails."""
        operands = [operand for _, operand in rest]
        if len(rest) == 1:  # a single comparison needs no blocks
            op, operand = rest[0]
            left = self.protect(self.lower(first), operands, span)
            return self.compare(op, left, self.lower(operand), span)
        join, param = self.join_block(BOOL)
        assert param is not None
        fail = self.new_block("false")
        left = self.protect(self.lower(first), operands, span)
        for n, (op, operand) in enumerate(rest):
            right = self.protect(self.lower(operand), operands[n + 1 :], span)
            result = self.compare(op, left, right, span)
            if n == len(rest) - 1:
                self.jump(join, [result])
            else:
                next_block = self.new_block("compare")
                self.branch(result, next_block, fail)
                self.block = next_block
            left = right
        self.block = fail
        self.jump(join, [self.const(False, span)])
        self.block = join
        return param

    def compare(self, op: str, left: Value, right: Value, span: Span) -> Value:
        if op in ("in", "not in"):
            if self.has_method(right.type, "contains"):
                result = self.call_method("contains", [right, left], BOOL, span, self.here())
            else:
                self.need_methods(left.type, "eq", self.module.eqs, span)
                result = self.emit("contains", BOOL, [left, right], span)
            return self.emit("not", BOOL, [result], span) if op == "not in" else result
        if op not in ("==", "!=") and self.has_method(left.type, "compare"):
            # `a < b` is `a.compare(b) < 0`.
            order = self.call_method("compare", [left, right], I64, span, self.here())
            return self.emit(COMPARE_OPS[op], BOOL, [order, self.const(0, span)], span)
        if op in ("==", "!="):
            self.need_methods(left.type, "eq", self.module.eqs, span)
        return self.emit(COMPARE_OPS[op], BOOL, [left, right], span)

    def lower_try(self, value: Value, t: Type, span: Span) -> Value:
        """`v?`: if v is `Ok(x)` (or `Some(x)`) the result is x; otherwise return the error."""
        assert isinstance(value.type, EnumTy)
        ok, error = ("Ok", "Err") if value.type.name == "Result" else ("Some", "None")
        ok_block, error_block = self.new_block("ok"), self.new_block("error")
        self.branch(self.emit("is_variant", BOOL, [value], span, ok), ok_block, error_block)
        # The error is rebuilt with the function's own result type, which may
        # differ from v's in the Ok type: Result[i64, str] into Result[(), str].
        self.block = error_block
        fields = [self.variant_field(value, error, i, t_, span) for i, t_ in enumerate(self.fields(value.type, error))]
        fields = [self.place(f, "result", span, mutable=False) for f in fields]
        rebuilt = self.build("variant", self.function.result, fields, span, "result", error)
        self.jump(self.exit, [self.place_return(rebuilt, span)])
        self.block = ok_block
        return self.variant_field(value, ok, 0, t, span)

    def variant_field(self, value: Value, variant: str, index: int, t: Type, span: Span) -> Value:
        result = self.emit("variant_field", t, [value], span, variant, index)
        self.info[result.id] = self.info_of(value)
        return result

    def fields(self, enum_type: Type, variant: str) -> tuple[Type, ...]:
        assert isinstance(enum_type, EnumTy)
        return tuple(self.concrete(t) for t in self.checker.variant_fields(enum_type, variant))

    def lower_call(self, expr: Call, callee: Expr, args: tuple[Expr, ...], span: Span, dest: str) -> Value:
        kind = self.checker.kinds.get(id(callee))
        if isinstance(callee, Name) and kind == "builtin":
            return self.lower_builtin(expr, callee.name, args, span, dest)
        if isinstance(callee, Name) and kind == "constructor":
            items = self.lower_items(args, dest)
            if is_builtin_error(self.checker.enums, self.checker.resolved[id(callee)].enum):
                items = self.error_fields(items, span, dest)
            return self.build("variant", self.type_of(expr), items, span, dest, callee.name)
        if isinstance(callee, Member) and id(callee) in self.checker.static_methods:
            enum_name, name, type_args = self.checker.static_methods[id(callee)]
            function = self.namespace_method(enum_name, name, type_args, span)
        elif isinstance(callee, Member) and id(callee) in self.checker.methods:
            # `v.name(args)` calls v's method with v first. A `mut v` is passed like any `mut` argument.
            receiver = callee.target
            function = self.method(self.type_of(receiver), callee.name, span)
            args = (receiver, *args)
        elif isinstance(callee, Member) and id(callee) in self.checker.builtin_methods:
            name = self.checker.builtin_methods[id(callee)]
            return self.lower_builtin(expr, name, (callee.target, *args), span, dest)
        elif isinstance(callee, Member) and id(callee) in self.checker.extension_methods:
            name = self.checker.extension_methods[id(callee)]
            if id(callee) in self.checker.instances:
                type_args = tuple(self.concrete(arg) for arg in self.checker.instances[id(callee)])
                function_name = self.specialize(name, type_args, span)
                function = self.emit("closure", self.type_of(callee), [], span, function_name, region=dest)
            else:
                function = self.read_name(name, "global", span, dest)
            args = (callee.target, *args)
        else:
            function = self.lower(callee)
        values: list[Value] = []
        places: list[MutPlace] = []
        for i, arg in enumerate(args):
            if isinstance(arg, MutArg):
                place, value = self.mut_place(arg)
                values.append(value)
                places.append(place)
            else:
                # An Ella function may change globals in place, so the argument
                # needs protecting from the call itself, as well as from later arguments.
                values.append(self.protect(self.lower(arg), args[i + 1 :], arg.span, calls=True))
        return self.emit_call(function, values, places, self.type_of(expr), span, dest)

    def namespace_method(self, enum: str, name: str, args: tuple[Type, ...], span: Span) -> Value:
        """A generic function in a type's namespace, without a receiver."""
        key = f"{enum}.{name}"
        type_args = tuple(self.concrete(arg) for arg in args)
        function_name = self.specialize(key, type_args, span)
        function, params = self.module.generics[key]
        function_type = self.generic_type(function, dict(zip(params, type_args)))
        return self.emit("closure", function_type, [], span, function_name, region=self.here())

    def emit_call(
        self, function: Value, values: list[Value], places: list[MutPlace], t: Type, span: Span, dest: str
    ) -> Value:
        """A call, whose `mut` arguments' final values are written back to their places."""
        result = self.emit("call", t, [function, *values], span, region=dest)
        call = self.block.instrs[-1]
        for place in places:
            out = self.new_value(self.mut_type(function, len(call.outs)))
            self.info[out.id] = Info(frozenset(), place.region)
            call.outs.append(out)
            call.out_regions.append(place.region)
        for place, out in zip(places, call.outs):
            self.write_back(place, out)
        return result

    def lower_builtin(self, expr: Call, name: str, args: tuple[Expr, ...], span: Span, dest: str) -> Value:
        """`print`, `len`, `run`, `next`, the pool and arena functions, reflection, and the library."""
        if name == "next":
            arg = args[0]
            assert isinstance(arg, MutArg)
            place, value = self.mut_place(arg)
            return self.advance(value, place, self.type_of(expr), span, dest)
        if name in ("alloc", "free"):
            pool_arg, *rest = args
            assert isinstance(pool_arg, MutArg)
            _, pool = self.mut_place(pool_arg)
            values = [self.lower(value_expr) for value_expr in rest]  # none for `free(mut arena)`
            if name == "free":
                self.emit_effect("free", [pool, *values], span)
                return self.const((), span)
            # The pool copies the value into an arena of its own, and an arena
            # into itself (a value that can't be copied is moved there, with its
            # finalizers).
            return self.emit("alloc", self.type_of(expr), [pool, *values], span)
        if name in LIBRARY:
            # Its result is new objects, made where they are wanted, or views of its first argument.
            values = [self.protect(self.lower(arg), args[i + 1 :], arg.span) for i, arg in enumerate(args)]
            if name == "hash":
                self.need_methods(values[0].type, "hash", self.module.hashes, span)
            region = dest or self.here()
            result = self.emit("builtin", self.type_of(expr), values, span, name, region=region)
            if name in VIEWS:
                self.info[result.id] = self.view_of(values[0], region)
            if result.type == NEVER:  # exit() doesn't return
                self.block.terminator = Trap(f"{name}() returned", None, span)
                return self.leave(span)
            return result
        operands = [self.protect(self.lower(arg), args[i + 1 :], arg.span) for i, arg in enumerate(args)]
        match name:
            case "len" if self.has_method(operands[0].type, "len"):
                return self.call_method("len", operands, I64, span, dest)
            case "len":
                return self.emit("len", I64, operands, span)
            case "print" | "eprint":
                self.emit_effect(name, [self.printed(operands, span)], span)
                return self.const((), span)
            case "pool_new" | "arena_new":
                return self.emit(name, self.type_of(expr), [], span, region=dest)
            case "run":
                return self.emit("run", self.type_of(expr), operands, span, region=dest)
            case _:  # reflection, which only the interpreters can do
                return self.emit("reflect", self.type_of(expr), operands, span, name, region=dest)

    def printed(self, values: list[Value], span: Span) -> Value:
        """What `print` writes for several values: their text forms with spaces between (one is printed as it is)."""
        if len(values) == 1:
            self.need_texts(values[0].type, span)
            return values[0]
        texts = [v if v.type == STR else self.emit_text(v, span, self.here()) for v in values]
        result = texts[0] if texts else self.const("", span)
        for text in texts[1:]:
            spaced = self.emit("add", STR, [result, self.const(" ", span)], span, region=self.here())
            result = self.emit("add", STR, [spaced, text], span, region=self.here())
        return result

    def library_function(self, name: str, t: FuncTy, span: Span) -> str:
        """The IR function for a library function used as a value, such as `trim(str)` or `abs(f64)`.

        It only calls the library function, with its result made where the caller wants it. A
        function's result shares nothing with its arguments, so a view (`trim`) is copied there.
        """
        label = f"{name}({', '.join(show_type(param) for param in t.params)})"
        if label not in self.module.functions:
            params = [Value(i, param) for i, param in enumerate(t.params)]
            result = Value(len(params), t.result)
            region = "" if not is_boxed(t.result) else "0" if name in VIEWS else "result"
            # Its errors are reported where it is called (EllaError.traced), as the interpreter does.
            if name == "hash":
                self.need_methods(t.params[0], "hash", self.module.hashes, span)
            instrs = [Instr("builtin", result, list(params), Span(0, 0), name, region=region)]
            if name in VIEWS:
                copied = Value(len(params) + 1, t.result)
                instrs.append(Instr("copy", copied, [result], span, region="result"))
                result = copied
            entry = Block("entry", params, instrs, Return(result))
            self.module.functions[label] = Function(label, t.result, span, [entry])
        return label

    def lower_quote(self, quote: Quote, span: Span) -> Value:
        """`quote e`: code, built at run time by the interpreter from the quote's syntax tree.

        The splices of the quote, and the variables it carries into the
        later stage, are evaluated here first, in the order the interpreter
        will want them (see ella.staging.build_code). The `quote` instruction
        gets the `quote_begin`s of the quotes it is inside the splices of
        too, for their hygienic identities.
        """
        events: list[AnySplice | Name] = []

        def splice(node: AnySplice) -> object:
            events.append(node)
            return "_" if isinstance(node, FieldSplice) else node  # a field's name; any will do here

        def persist(node: Name) -> Expr:
            events.append(node)
            return node

        start_code(quote, self.module.tables).build(splice, persist)  # names aren't needed here
        number = len(self.module.templates)
        outer = list(self.quotes)
        self.module.templates[number] = (quote, len(outer))
        # `quote_begin` chooses binder identities, which quotes in the splices may use.
        begin = self.emit("quote_begin", UNIT, [], span, index=number)
        self.quotes.append(begin)
        values = []
        for event in events:
            if not isinstance(event, Name):
                values.append(self.lower(event.code))
            else:
                values.append(self.read_name(event.name, "local", event.span))
        self.quotes.pop()
        return self.emit("quote", self.type_of(quote), [begin, *outer, *values], span, index=number)

    def has_method(self, t: Type, name: str) -> bool:
        """Whether values of type `t` (a struct or enum) have a method, such as the `add` that gives them `+`."""
        return isinstance(t, EnumTy) and name in self.module.enums[t.name].methods

    def emit_text(self, value: Value, span: Span, region: str) -> Value:
        """The text form of a value, as `print` writes it."""
        self.need_texts(value.type, span)
        return self.emit("to_text", STR, [value], span, region=region)

    def need_texts(self, t: Type, span: Span) -> None:
        """Make the `text` methods that writing a value of type t calls: those of the structs and enums in it."""
        self.need_methods(t, "text", self.module.texts, span)

    def need_methods(self, t: Type, method: str, table: dict[Type, str], span: Span) -> None:
        """Make the methods `method` (text, eq or hash) of the structs and enums a value of type t may hold.

        They go in `table` (Module.texts, eqs or hashes), specialized for their type arguments, for the
        IR interpreter and the runtime to call. A value of a type with one is handled by calling it,
        so its parts aren't looked into.
        """
        seen: set[Type] = set()
        work = [t]
        while work:
            part = work.pop()
            if part in seen:
                continue
            seen.add(part)
            match part:
                case ListTy(element):
                    work.append(element)
                case TupleTy(items):
                    work += items
                case EnumTy(name, args) if method in self.module.enums[name].methods:
                    if part not in table:
                        table[part] = self.specialize(f"{name}.{method}", args, span)
                case EnumTy(name, args):
                    info = self.module.enums[name]
                    mapping = dict(zip(info.params, args))
                    work += [substitute(f, mapping) for fields in info.variants.values() for f in fields]

    def method(self, t: Type, name: str, span: Span) -> Value:
        """A closure of the method `name` of a struct or enum type, specialized for its type arguments."""
        assert isinstance(t, EnumTy)
        function = self.specialize(f"{t.name}.{name}", t.args, span)
        lambda_, params = self.module.generics[f"{t.name}.{name}"]
        function_type = self.generic_type(lambda_, dict(zip(params, t.args)))
        return self.emit("closure", function_type, [], span, function, region=self.here())

    def call_method(self, name: str, values: list[Value], t: Type | None, span: Span, dest: str) -> Value:
        """Call the method that gives an operator or another protocol, on values already lowered.

        The result has type `t`, or the method's result type if it is None.
        An Ella function may change globals in place, so the values are
        protected from the call itself.
        """
        values = [self.protect(value, [], span, calls=True) for value in values]
        function = self.method(values[0].type, name, span)
        assert isinstance(function.type, FuncTy)
        return self.emit("call", t or function.type.result, [function, *values], span, region=dest)

    def lower_assign_set(self, op: str, target: Index, value_expr: Expr, span: Span) -> Value:
        """`v[k] = x` on a struct or enum calls `mut v.set(k, x)`; `v[k] op= x` gets the item first.

        The value is evaluated first, then the place and the key, as for any assignment.
        """
        value = self.protect(self.lower(value_expr), [target.index], span, calls=True)
        place, container = self.mut_place(MutArg(target.target, target.span))
        key = self.lower(target.index)
        if op != "=":
            old = self.call_method("get", [container, key], None, span, self.here())
            value = self.operate(op.removesuffix("="), old, value, old.type, span, self.here())
        function = self.method(container.type, "set", span)
        self.emit_call(function, [container, key, value], [place], UNIT, span, self.here())
        return value

    def specialize(
        self,
        name: str,
        args: tuple[Type, ...],
        span: Span,
        captures: list[tuple[str, Type]] | None = None,
        self_names: list[str] | None = None,
        outer_subst: dict[str, Type] | None = None,
    ) -> str:
        """The IR function for a generic function with these type arguments: `first[i64]`.

        A method of a type without type parameters is just `Point.norm`.
        """
        key = (name, args)
        if key not in self.module.specializations:
            function, params = self.module.generics[name]
            if sum(1 for n, _ in self.module.specializations if n == name) >= MAX_SPECIALIZATIONS:
                raise EllaError(f"{name} is used with too many different types", span)
            specialized = f"{name}[{', '.join(show_type(arg) for arg in args)}]" if params else name
            self.module.specializations[key] = specialized
            subst = (outer_subst or {}) | dict(zip(params, args))
            self.pending.append(
                PendingFunction(
                    function,
                    specialized,
                    captures or [],
                    self_names or [],
                    self.generic_type(function, subst),
                    subst,
                )
            )
        return self.module.specializations[key]

    def generic_type(self, function: Lambda, subst: dict[str, Type]) -> FuncTy:
        """The type of a generic function (or a method) with its type arguments.

        Code being lowered to `run` has a checker of its own, without the program's functions.
        """
        types = self.checker.types if id(function) in self.checker.types else self.module.checker.types
        t = substitute(finish(types[id(function)]), subst)
        assert isinstance(t, FuncTy)
        return t

    def lower_code(self, code: Expr) -> str:
        """Lower code made by `quote` into a function of no parameters, to `run` it. Returns its name."""
        name = f"run.{len(self.module.functions)}"
        self.start_function(name, self.type_of(code), code.span, code)
        self.scopes = [{}]
        self.exit = self.new_block("exit")
        result = self.new_value(self.function.result)
        self.exit.params.append(result)
        self.jump(self.exit, [self.place_return(self.lower(code, "result"), code.span)])
        self.block = self.exit
        self.block.terminator = Return(result)
        self.finish_function()
        while self.pending:
            self.lower_function(self.pending.pop(0))
        return name

    def mut_type(self, function: Value, n: int) -> Type:
        """The type of the n-th `mut` parameter of a function value."""
        t = function.type
        assert isinstance(t, FuncTy)
        return t.params[t.mutable[n]]

    def mut_place(self, arg: MutArg) -> tuple[MutPlace, Value]:
        """Evaluate a `mut` argument's place, its variable and steps, and get its current value.

        The value itself is passed, to be changed in place. A global's is
        copied first, so that the function sees the global's old value if it
        reads the global directly, as with the AST interpreter, which passes
        every `mut` argument as a copy. (A value that can't be copied is
        passed as it is, there too.)
        """
        root, chain = place_steps(arg.target) if not isinstance(arg.target, Name) else (arg.target, [])
        kind = self.checker.kinds[id(root)]
        steps: list[Step] = [(node, self.step_key(node)) for node in chain]
        region = self.var_region(root.name, kind)
        value = self.read_name(root.name, kind, root.span)
        for step in steps:
            value, region = self.step_into(value, step, region)
        if kind != "local" and not self.nocopy(value.type):
            value = self.copy(value, "global", arg.span)
        return MutPlace(root, kind, steps, region, arg.span), value

    def write_back(self, place: MutPlace, value: Value) -> None:
        """Give a `mut` argument's place its new value after the call."""
        if not place.steps:
            self.write_name(place.root.name, place.kind, value, place.span)
            return
        region = self.var_region(place.root.name, place.kind)
        container = self.read_name(place.root.name, place.kind, place.span)
        for step in place.steps[:-1]:
            container, region = self.step_into(container, step, region, place.span)
        self.store_step(container, place.steps[-1], value, region, place.span, at_step=False)

    def lower_if(self, cond: Expr, then: Expr, else_: Expr | None, t: Type, span: Span, dest: str) -> Value:
        then_block, else_block = self.new_block("then"), self.new_block("else")
        join, param = self.join_block(t if else_ is not None else UNIT)
        self.branch(self.lower(cond), then_block, else_block)
        values = []
        for block, body in ((then_block, then), (else_block, else_)):
            self.block = block
            value = self.lower(body, dest) if body is not None else None
            if param is not None and value is not None:
                values.append(value)
                self.jump(join, [value])
            else:
                self.jump(join)
        self.block = join
        return self.result_of(param, values, span)

    def lower_match(self, expr: Match, subject: Value, span: Span, dest: str) -> Value:
        """Each case tests its pattern (and guard), going on to the next case if it fails.

        If no case matches, the program stops with a trap.
        """
        later: list[Expr] = [e for case in expr.cases for e in (case.guard, case.body) if e is not None]
        subject = self.protect(subject, later, span)
        join, param = self.join_block(self.type_of(expr))
        values = []
        for case in expr.cases:
            next_case = self.new_block("case")
            self.scopes.append({})
            self.lower_pattern(case.pattern, subject, next_case)
            if case.guard is not None:
                body = self.new_block("guarded")
                self.branch(self.lower(case.guard), body, next_case)
                self.block = body
            value = self.lower(case.body, dest)
            values.append(value)
            self.jump(join, [value] if param is not None else [])
            self.scopes.pop()
            self.block = next_case
        self.block.terminator = Trap("no case matches {}", subject, span)
        self.block = join
        return self.result_of(param, values, span)

    # Loops

    def enter_loop(self, carries: bool = False) -> int:
        """Start lowering a loop's iterations, one level deeper. Returns the level outside it.

        Inside a loop that carries variables, the next level is its spare
        arena, so loops inside it go two levels deeper.
        """
        outer = self.level
        self.level += 2 if outer in self.carrying else 1
        self.max_level = max(self.max_level, self.level + (1 if carries else 0))
        if carries:
            self.carrying.add(self.level)
        return outer

    def leave_loop(self, exit: Block, outer: int, span: Span, carried: Sequence[Carried] = ()) -> None:
        """Continue after a loop, at its exit, which copies out what it carried and frees the loop's levels."""
        level = self.level
        self.carrying.discard(level)
        self.level = outer
        self.block = exit
        self.end_carrying(carried, level, span)
        self.emit_effect("reset", [], span, index=level)

    def carried_by(self, loop: For | While | Loop) -> list[tuple[str, str]]:
        """The variables declared outside a loop that it carries, with their kinds: those it gives new values.

        Leaving the loop copies them back to their own regions, so it must not
        be left another way than its exit: a `break` or `continue` of a loop
        outside it means none. Left out are:

        * variables the loop changes in place (`xs += ys`, `xs[i] = v`, `mut
          xs`), which grow or change where they are and need no new versions,
        * names declared inside the loop, which may be other variables,
        * values that are never copied or aren't objects, and
        * globals, if the loop calls Ella functions or makes closures or
          code, which could see them while the loop has them in a slot.
        """
        parts: list[object] = [loop.body, loop.cond] if isinstance(loop, While) else [loop.body]
        inside = list(loop_nodes(tuple(parts)))
        labels = {loop.label} | {node.label for node in inside if isinstance(node, For | While | Loop)}
        if any(isinstance(node, Break | Continue) and node.label not in (None, *labels) for node in inside):
            return []
        declared: set[int] = set()
        if isinstance(loop, For):
            declared |= {id(node) for node in pattern_names(loop.pattern)}
        for node in inside:
            match node:
                case Let(bindings):
                    declared |= {id(binding) for binding in bindings}
                case LetPattern(pattern) | For(pattern=pattern) | Case(pattern=pattern):
                    declared |= {id(name) for name in pattern_names(pattern)}
        changed = changed_in_place(tuple(parts), self.checker, into_lambdas=False)
        changed_ids = changed_bindings(tuple(parts), self.checker, into_lambdas=False)
        if isinstance(loop, For) and isinstance(loop.iterable, MutArg):
            changed |= {place_root(loop.iterable.target), CALLS}  # it changes as the loop advances it
            root = loop.iterable.target
            if not isinstance(root, Name):
                root, _ = place_steps(root)
            info = self.checker.resolved.get(id(root))
            if info is not None and info.kind == "local":
                changed_ids.add(id(info.binder))
        closed = CALLS not in changed and not any(isinstance(node, Lambda | Quote) for node in inside)
        names: list[tuple[str, str]] = []
        seen_bindings: set[int] = set()
        for node in inside:
            if not isinstance(node, Assign) or not isinstance(node.target, Name) or is_append(node, self.checker):
                continue
            name = node.target.name
            kind = self.resolve(name, self.checker.kinds.get(id(node.target), ""))
            binding: int | None = None
            if kind == "local":
                info = self.checker.resolved.get(id(node.target))
                if info is None:
                    continue
                binding = id(info.binder)
                if binding in declared or binding in changed_ids or binding in seen_bindings:
                    continue
                slot = self.find_slot(name)
                if slot == SELF:
                    continue
                if self.slots[slot].binding != binding:
                    continue
            elif name in changed or (name, kind) in names:
                continue
            if kind == "local" and self.find_slot(name) != SELF:
                t = self.function.locals[self.find_slot(name)]
            elif kind == "global" and closed:
                t = self.module.globals[name]
            else:
                continue
            if is_boxed(t) and not self.nocopy(t) and not has_pool(t, self.module.enums):
                names.append((name, kind))
                if binding is not None:
                    seen_bindings.add(binding)
        return names

    def begin_carrying(self, names: list[tuple[str, str]], span: Span) -> list[Carried]:
        """Before a loop: give each variable it carries a slot of its own, in a new scope, holding its value."""
        if not names:
            return []
        carried, scope = [], {}
        for name, kind in names:
            value = self.read_name(name, kind, span)
            slot = self.new_slot(name, value.type)
            region = f"carry{self.level}"
            self.function.local_regions[slot] = region
            binding = self.slots[self.find_slot(name)].binding if kind == "local" else None
            self.slots[slot] = SlotInfo(name, True, "local", region, binding=binding)
            self.emit_effect("store", [value], span, slot)
            scope[name] = slot
            carried.append(Carried(name, kind, slot))
        self.scopes.append(scope)
        self.promoted |= {c.name for c in carried if c.kind == "global"}
        return carried

    def latch(self, carried: list[Carried], header: Block, span: Span) -> Block:
        """Where an iteration of a loop ends: its header, or a block that keeps the carried values, then goes there.

        A variable whose value is still in @carryk (it got no new value this
        time) is copied into the iteration's arena, which the loop keeps.
        """
        if not carried:
            return header
        block, self.block = self.block, self.new_block("latch")
        latch = self.block
        self.keep_carried(carried, span)
        self.jump(header)
        self.block = block
        return latch

    def keep_carried(self, carried: list[Carried], span: Span) -> None:
        for c in carried:
            t = self.function.locals[c.slot]
            value = self.emit("load", t, [], span, c.slot)
            kept = self.emit("carry", t, [value], span, index=self.level, region=self.here())
            self.emit_effect("store", [kept], span, c.slot)

    def end_iteration(self, carried: list[Carried], span: Span) -> None:
        """At a loop's header: free the last iteration's arena, or swap the arenas of a loop that carries values."""
        self.emit_effect("swap" if carried else "reset", [], span, index=self.level)

    def end_carrying(self, carried: Sequence[Carried], level: int, span: Span) -> None:
        """After a loop: copy each variable it carried back to where the variable lives."""
        if not carried:
            return
        self.scopes.pop()
        self.promoted -= {c.name for c in carried if c.kind == "global"}
        for c in carried:
            value = self.emit("load", self.function.locals[c.slot], [], span, c.slot)
            self.info[value.id] = Info(frozenset({c.slot}), f"carry{level}")
            region = self.build_region(self.var_region(c.name, c.kind))
            self.write_name(c.name, c.kind, self.place(value, region, span, mutable=True), span)

    def advance(self, iterator: Value, place: MutPlace, t: Type, span: Span, dest: str) -> Value:
        """The next item of a generator (`resume`), or of an iterator (its `next` method), as an Optional.

        An iterator's new value is written back to its place; a generator
        changes in place.
        """
        if isinstance(iterator.type, BuiltinTy):
            return self.emit("resume", t, [iterator], span, region=dest)
        function = self.method(iterator.type, "next", span)
        return self.emit_call(function, [iterator], [place], t, span, dest)

    def place_value(self, place: MutPlace) -> Value:
        """The value at a place found earlier (by `mut_place`), read again."""
        value = self.read_name(place.root.name, place.kind, place.span)
        region = self.var_region(place.root.name, place.kind)
        for step in place.steps:
            value, region = self.step_into(value, step, region, place.span)
        return value

    def lower_for_iterator(self, expr: For, via_iter: bool) -> Value:
        """`for pattern in g`, over a generator or an iterator, asks for each item until it gets None:

        header: free the last iteration; r = the next item; if r is Some go to body, else exit
        body:   item = the value in r; match the pattern; body; go to header

        In `for x in mut it`, the iterator or generator is at a place, which it
        changes. Otherwise a generator is a new value, and an iterator is
        copied into a hidden variable, `for.it`, which the loop changes.
        """
        span = expr.span
        if isinstance(expr.iterable, MutArg):
            place, iterator = self.mut_place(expr.iterable)
        else:
            iterator = self.protect(self.lower(expr.iterable), [expr.body], span)
            if via_iter:
                iterator = self.call_method("iter", [iterator], None, span, self.here())
            self.scopes.append({})
            self.bind("for.it", iterator, span, mutable=True)
            place = MutPlace(Name("for.it", span), "local", [], self.here(), span)
        names = self.carried_by(expr)
        outer = self.enter_loop(bool(names))
        carried = self.begin_carrying(names, span)
        header, body, next_block = (self.new_block(h) for h in ("for", "body", "next"))
        exit = self.new_block("exit", outer)
        self.jump(header)

        self.block = header
        self.end_iteration(carried, span)
        item_type = self.concrete(self.checker.types[id(expr.pattern)])
        result_type = EnumTy("Optional", (item_type,))
        result = self.advance(self.place_value(place), place, result_type, span, self.here())
        self.branch(self.emit("is_variant", BOOL, [result], span, "Some"), body, exit)

        self.block = body
        item = self.variant_field(result, "Some", 0, item_type, span)
        no_match = self.new_block("nomatch")
        self.scopes.append({})
        self.lower_pattern(expr.pattern, item, no_match)
        self.lower_loop_body(LoopTarget(expr.label, exit, next_block, outer), expr.body)
        self.scopes.pop()
        self.jump(next_block)

        self.block = no_match
        self.block.terminator = Trap("the 'for' pattern doesn't match {}", item, span)

        self.block = next_block
        self.keep_carried(carried, span)
        self.jump(header)

        self.leave_loop(exit, outer, span, carried)
        if not isinstance(expr.iterable, MutArg):
            self.scopes.pop()
        return self.const((), span)

    def lower_for(self, expr: For) -> Value:
        """`for pattern in xs body` counts through the items with a hidden counter:

        n = len xs; i = 0
        header: free the last iteration; if i < n go to body, else exit
        body:   item = index xs, i; match the pattern; body; go to next
        next:   i = i + 1; go to header
        """
        span = expr.span
        via_iter, kind = self.checker.iterations[id(expr)]
        if kind != "items":
            return self.lower_for_iterator(expr, via_iter)
        iterable = self.protect(self.lower(expr.iterable), [expr.body], span)
        if self.has_method(iterable.type, "iter"):
            iterable = self.call_method("iter", [iterable], None, span, self.here())  # a new list, str or range
        count = self.emit("len", I64, [iterable], span)
        counter = self.new_slot("for.i", I64)
        self.function.local_regions[counter] = self.here()
        self.emit_effect("store", [self.const(0, span)], span, counter)
        names = self.carried_by(expr)
        outer = self.enter_loop(bool(names))
        carried = self.begin_carrying(names, span)
        header, body, next_block = (self.new_block(h) for h in ("for", "body", "next"))
        exit = self.new_block("exit", outer)
        self.jump(header)

        self.block = header
        self.end_iteration(carried, span)
        i = self.emit("load", I64, [], span, counter)
        self.branch(self.emit("lt", BOOL, [i, count], span), body, exit)

        self.block = body
        item_type = self.concrete(self.checker.types[id(expr.pattern)])
        item = self.lower_index(iterable, i, item_type, span, "")
        no_match = self.new_block("nomatch")
        self.scopes.append({})
        self.lower_pattern(expr.pattern, item, no_match)
        self.lower_loop_body(LoopTarget(expr.label, exit, next_block, outer), expr.body)
        self.scopes.pop()
        self.jump(next_block)

        self.block = no_match
        self.block.terminator = Trap("the 'for' pattern doesn't match {}", item, span)

        self.block = next_block
        self.keep_carried(carried, span)
        i = self.emit("load", I64, [], span, counter)
        self.emit_effect("store", [self.emit("add", I64, [i, self.const(1, span)], span)], span, counter)
        self.jump(header)

        self.leave_loop(exit, outer, span, carried)
        return self.const((), span)

    def lower_loop_body(self, target: LoopTarget, body: Expr) -> None:
        self.loops.append(target)
        self.lower(body)  # its value is discarded
        self.loops.pop()

    def find_loop(self, label: str | None) -> LoopTarget:
        for loop in reversed(self.loops):
            if label is None or loop.label == label:
                return loop
        raise AssertionError("the checker rejects 'break' and 'continue' outside loops")

    # Patterns

    def lower_pattern(self, pattern: Pattern, value: Value, fail: Block, mutable: bool = False) -> None:
        """Test `value` against `pattern`, binding its variables (all mutable for `let mut`); go to `fail` if not.

        The tests are emitted one after another. Each failing test branches to
        `fail`, and lowering continues in the block where it succeeded.
        """
        span = pattern.span
        match pattern:
            case WildcardPattern():
                pass
            case NamePattern(name, mutable=mutable_name):
                if self.checker.kinds[id(pattern)] == "variant":
                    self.test(self.emit("is_variant", BOOL, [value], span, name), fail)
                else:
                    self.bind(name, value, span, mutable or mutable_name, binding=id(pattern))
            case LiteralPattern(literal):
                self.test(self.emit("eq", BOOL, [value, self.const(literal, span)], span), fail)
            case RangePattern(low, high, inclusive):
                self.test(self.emit("ge", BOOL, [value, self.const(low, span)], span), fail)
                self.test(self.emit("le" if inclusive else "lt", BOOL, [value, self.const(high, span)], span), fail)
            case OrPattern(alternatives):
                # Try each alternative in turn; any success goes on to `matched`.
                matched = self.new_block("matched")
                for alternative in alternatives[:-1]:
                    next_alternative = self.new_block("or")
                    self.lower_pattern(alternative, value, next_alternative, mutable)
                    self.jump(matched)
                    self.block = next_alternative
                self.lower_pattern(alternatives[-1], value, fail, mutable)
                self.jump(matched)
                self.block = matched
            case VariantPattern(name, args):
                self.test(self.emit("is_variant", BOOL, [value], span, name), fail)
                for i, (arg, field_type) in enumerate(zip(args, self.fields(value.type, name))):
                    self.lower_pattern(arg, self.variant_field(value, name, i, field_type, span), fail, mutable)
            case TuplePattern(items):
                assert isinstance(value.type, TupleTy)
                for i, (item, item_type) in enumerate(zip(items, value.type.items)):
                    item_value = self.emit("tuple_get", item_type, [value], span, index=i)
                    self.info[item_value.id] = self.info_of(value)
                    self.lower_pattern(item, item_value, fail, mutable)

    def test(self, cond: Value, fail: Block) -> None:
        """Continue in a new block if `cond` is true, otherwise go to `fail`."""
        passed = self.new_block("match")
        self.branch(cond, passed, fail)
        self.block = passed


# Finding what code does, for the decisions above


def source_name(name: str) -> str:
    """An IR function's name as the program writes it, for stack traces: `id` for `id[i64]`, `g` for `g.1`,
    `Stack.push` for `Stack.push[i64]`."""
    name = name.split("[")[0]
    base, _, number = name.rpartition(".")
    return base if number.isdigit() else name


def unused_functions(program: Program) -> set[int]:
    """The top-level functions of modules other than the entry file that nothing in the program uses, by `let`.

    Linking (ella.modules) brings in whole modules, the prelude's too, so
    this keeps their unused functions out of the IR. A function is used if
    code other than such functions uses its name (in a quote too), or a
    used function does.
    """
    functions = {
        binding.name: expr
        for expr in program.body
        if isinstance(expr, Let) and isinstance(expr.value, Lambda) and expr.span.file
        for binding in expr.bindings
    }
    others = [expr for expr in program.body if not any(expr is f for f in functions.values())]
    wanted = set().union(*(names_used(expr, inside_lambdas=True) for expr in others))
    used: set[str] = set()
    while wanted:
        name = wanted.pop()
        if name not in used:
            used.add(name)
            if name in functions:
                wanted |= names_used(functions[name].value, inside_lambdas=True)
    return {id(expr) for expr in functions.values() if not any(b.name in used for b in expr.bindings)}


def loop_nodes(node: object) -> list[object]:
    """The syntax tree nodes in some code, itself included, but not those inside lambdas and quotes, which run elsewhere."""
    if isinstance(node, tuple):
        return [n for item in node for n in loop_nodes(item)]
    if not is_dataclass(node) or isinstance(node, type | Span):
        return []
    if isinstance(node, Lambda | Quote):
        return [node]
    return [node, *(n for child in children(node) for n in loop_nodes(child))]


def type_splices(node: object) -> list[TypeSplice]:
    """The splices in a type, in order."""
    if isinstance(node, TypeSplice):
        return [node]
    return [s for part in children(node) for s in type_splices(part)]


def place_steps(place: Expr) -> tuple[Name, list[Index | Member]]:
    """The variable at the root of a place, and the indexes and fields from it out: `xs[i].y` has [xs[i], xs[i].y]."""
    chain: list[Index | Member] = []
    while isinstance(place, Index | Member):
        chain.insert(0, place)
        place = place.target
    assert isinstance(place, Name)
    return place, chain


def is_append(assign: Assign, checker: Checker) -> bool:
    """Whether an assignment adds to a list variable in place: `xs += ys`, or `xs = xs + ys`.

    `xs = xs + ys` reads xs before evaluating ys, so it only counts if ys
    can't change xs. (`xs += ys` evaluates ys first, as the append does.)
    """
    target, value = assign.target, assign.value
    if not isinstance(target, Name) or not isinstance(checker.type_of(target), ListTy):
        return False
    if assign.op == "+=":
        return True
    if assign.op != "=" or not isinstance(value, Binary) or value.op != "+":
        return False
    if not isinstance(value.left, Name) or value.left.name != target.name:
        return False
    written = names_written(value.right, checker)
    is_global = checker.kinds.get(id(target)) == "global"
    return target.name not in written and not (is_global and CALLS in written)


def names_written(node: object, checker: Checker) -> set[str]:
    """The variables some code assigns to or passes as `mut` (and CALLS if it calls an Ella function)."""
    match node:
        case Assign(_, target, value):
            return {place_root(target)} | names_written(target, checker) | names_written(value, checker)
        case MutArg(target):
            return {place_root(target)} | names_written(target, checker)
        case Call(callee, args):
            calls = {CALLS} if checker.kinds.get(id(callee)) not in ("builtin", "constructor") else set()
            calls |= {CALLS} if calls_method(node, checker) else set()
            return calls | names_written(callee, checker) | names_written(args, checker)
        case Lambda():
            return set()
    calls = {CALLS} if calls_method(node, checker) else set()
    return calls.union(*(names_written(child, checker) for child in children(node)))


def changed_in_place(node: object, checker: Checker, into_lambdas: bool) -> set[str]:
    """The variables some code changes in place, by name, and CALLS if it calls an Ella function.

    Lists and structs change in place through an assignment to an item or
    field (`xs[i] = v`, `p.x = v`), `xs += ys`, and `mut` arguments. Nested lambdas are only searched if
    `into_lambdas`: their code runs when they are called, not where they are.
    """
    match node:
        case Assign(_, Index() | Member() as target):
            changed = {place_root(target)}
        case Assign(_, Name(name)) as assign if is_append(assign, checker):
            changed = {name}
        case MutArg(target):
            changed = {place_root(target)}
        case Call(callee):
            changed = {CALLS} if checker.kinds.get(id(callee)) not in ("builtin", "constructor") else set()
        case Lambda() if not into_lambdas:
            return set()
        case _:
            changed = set()
    if calls_method(node, checker):
        changed |= {CALLS}
    return changed.union(*(changed_in_place(child, checker, into_lambdas) for child in children(node)))


def changed_bindings(node: object, checker: Checker, into_lambdas: bool) -> set[int]:
    """The identities of local bindings whose mutable objects this code may change."""
    root: Name | None
    match node:
        case Assign(_, Index() | Member() as target):
            root, _ = place_steps(target)
        case Assign(_, Name() as target) as assign if is_append(assign, checker):
            root = target
        case MutArg(Name() as root):
            pass
        case MutArg(Index() | Member() as target):
            root, _ = place_steps(target)
        case Lambda() if not into_lambdas:
            return set()
        case _:
            root = None
    changed: set[int] = set()
    if root is not None:
        info = checker.resolved.get(id(root))
        if info is not None and info.kind == "local":
            changed.add(id(info.binder))
    return changed.union(*(changed_bindings(child, checker, into_lambdas) for child in children(node)))


def calls_method(node: object, checker: Checker) -> bool:
    """Whether an operator (or `len`, indexing or a loop) may call a method of a struct or enum, an Ella function.

    A value of a type parameter may turn out to be a struct with the method.
    """

    def has(expr: Expr, name: str) -> bool:
        t = finish(checker.types.get(id(expr), UNIT))
        return isinstance(t, ParamTy) or (isinstance(t, EnumTy) and name in checker.enums[t.name].methods)

    match node:
        case Binary(op, left) if op in OPERATOR_METHODS:
            return has(left, OPERATOR_METHODS[op])
        case Assign(op, target):
            operator = op.removesuffix("=")
            return (isinstance(target, Index) and has(target.target, "set")) or (
                operator in OPERATOR_METHODS and has(target, OPERATOR_METHODS[operator])
            )
        case Index(target):
            return has(target, "get")
        case For(iterable=iterable):
            # A loop over a generator or an iterator runs its code for each item.
            t = finish(checker.types.get(id(iterable), UNIT))
            return has(iterable, "iter") or has(iterable, "next") or (isinstance(t, BuiltinTy) and t.name == "Gen")
        case Call(Name("next")) if checker.kinds.get(id(node.callee)) == "builtin":
            return True
        case Unary("-", operand):
            return has(operand, "neg")
        case Compare(first, rest):
            operands = [first, *(operand for _, operand in rest)]
            return any(
                has(b, "contains") if op in ("in", "not in") else op not in ("==", "!=") and has(a, "compare")
                for a, (op, b) in zip(operands, rest)
            )
        case Call(Name("len"), (arg,)) if checker.kinds.get(id(node.callee)) == "builtin":
            return has(arg, "len")
    return False


def returned_bindings(body: Expr | tuple[Expr, ...], checker: Checker) -> set[int]:
    """Local bindings a function may return directly, as its last value or from a `return`."""

    def tails(expr: Expr | None) -> set[int]:
        match expr:
            case Name():
                info = checker.resolved.get(id(expr))
                return {id(info.binder)} if info is not None and info.kind == "local" else set()
            case BlockExpr(exprs):
                return tails(exprs[-1])
            case If(_, then, else_):
                return tails(then) | tails(else_)
            case Match(_, cases):
                return set().union(*(tails(case.body) for case in cases))
        return set()

    def returns(node: object) -> set[int]:
        match node:
            case ReturnExpr(value):
                return tails(value) | returns(value)
            case Lambda():
                return set()
        return set().union(*(returns(child) for child in children(node)))

    if isinstance(body, tuple):
        return set()
    return tails(body) | returns(body)


def names_in(node: object) -> set[str]:
    """Every name some code uses, anywhere in it."""
    if isinstance(node, Name):
        return {node.name}
    return set().union(*(names_in(child) for child in children(node)))


def children(node: object) -> list[object]:
    """The parts of a syntax tree node (or the items of a tuple of nodes)."""
    if isinstance(node, tuple):
        return list(node)
    if is_dataclass(node) and not isinstance(node, type):
        return [getattr(node, f.name) for f in fields(node)]
    return []
