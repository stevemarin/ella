"""Lowering: turns a type-checked syntax tree into IR (see ella.ir).

The syntax tree is nested: an `if` contains expressions, which contain other
expressions. The IR is flat: blocks of simple instructions, connected by
jumps. Lowering walks the tree and emits instructions into the *current
block*, starting new blocks wherever control can go more than one way.

For example, `let y = if x > 0 then x else -x` becomes:

    entry:
        %1: i64 = load x
        %2: i64 = const 0
        %3: bool = gt %1, %2
        branch %3, then1, else2
    then1:
        %4: i64 = load x
        jump join3(%4)
    else2:
        %5: i64 = load x
        %6: i64 = neg %5
        jump join3(%6)
    join3(%7: i64):
        store y, %7

A few ideas recur:

* After `return`, `break`, `continue` or a failed `?`, nothing more runs in
  that block. Lowering carries on in a fresh "dead" block that nothing jumps
  to, and dead blocks are removed when the function is finished.
* Every Ella function becomes an IR function. A lambda is *closure
  converted*: the IR function takes the captured variables as extra leading
  parameters, and the `closure` instruction packs them with it into a
  function value.
* `return` jumps to the function's exit block rather than returning
  directly, so that postconditions are checked however the function ends.

Memory
------

The lowering also decides where every object lives and when values are
copied (docs/memory-model.md §3, §5, §6). Each function body is scope level
0, and each loop adds a level for its iterations: the loop frees that level
(`reset`) when an iteration ends. Every expression is lowered with a
*destination*, the region its value will end up in, and objects are built
directly there: `let xs = [1, 2]` makes the list in the region of `xs`, and a
returned list is made in the caller's region (`@result`).

Values are references to objects, and reading a variable doesn't copy. So
for each boxed value, the lowering tracks two things (`Info`):

* what it *borrows*: the variables whose objects it may share. A value read
  from `xs`, or an item of it, borrows `xs`. A new object borrows nothing.
* how long it *lives*: a region that every object in it outlives.

A value is copied when it is stored (in a variable, a list, a returned
value, ...) if either check fails:

* Sharing would be visible. Only lists ever change, and only in place,
  through `xs[i] = v` and `xs += ys`. A value with lists in it is copied
  if it borrows from a variable that is changed in place, or is stored into
  a `mut` variable, which may change it in place itself.
* It would outlive its objects: it lives in a region that doesn't outlive
  the one it is stored in. A value built inside a loop and stored in a
  variable declared outside it is an example.

One more case needs a copy. Reading `xs` and then changing it in place
before the read value is used, as in `xs + [(xs[0] = 5)]`, would show the
change. Ella's semantics are that a read gives the value at the time of
reading, so `protect` copies a read value if a later part of the same
expression changes its variable in place.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from itertools import count
from typing import TextIO

from ella.checker import (
    BOOL,
    I64,
    NEVER,
    STR,
    UNIT,
    BuiltinTy,
    Checker,
    EnumTy,
    FuncTy,
    ListTy,
    TupleTy,
    Type,
    finish,
    has_mutable,
    has_pool,
    is_nocopy,
    literal_type,
    place_root,
    show_type,
    substitute,
)
from ella.ir import (
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
    allocates,
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
    BoolLit,
    Break,
    Call,
    Compare,
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
    ListLit,
    LiteralPattern,
    Loop,
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
    mentions_return,
)
from ella.syntax import (
    Block as BlockExpr,
)
from ella.syntax import (
    Return as ReturnExpr,
)

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


def compile_program(source: str, out: TextIO | None = None) -> Module:
    """Parse, type check and lower a program, and verify the IR. Raises EllaError.

    Splices outside quotes run first, while compiling (see ella.staging);
    what they print goes to `out`.
    """
    program, checker = prepare(parse(source), out)
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


# One step into a place: an index (a list's or a pool's) with its value, or a field.
type Step = tuple[Index | Member, Value | None]


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
        self.quotes: list[Value] = []  # the `quote_begin`s of the quotes whose splices are being lowered
        self.globals_changed: set[str] = set()  # globals changed in place anywhere

        # The function being lowered. `start_function` resets all of these.
        self.function = Function("", UNIT, Span(1, 1))
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
        self.returned: set[str] = set()  # names of variables this function may return

    # Building blocks

    def start_function(self, name: str, result: Type, span: Span, body: Expr | tuple[Expr, ...]) -> None:
        self.function = Function(name, result, span)
        self.module.functions[name] = self.function
        self.next_id = 0
        self.level = self.max_level = 0
        self.block = self.new_block("entry")
        self.scopes, self.loops, self.self_info = [], [], None
        self.slots, self.info = {}, {}
        self.changed = changed_in_place(body, self.checker, into_lambdas=False)
        self.returned = returned_names(body) if name != "top" else set()

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
        """What is known about a value. Values not recorded are new objects, unboxed values, or constants."""
        return self.info.get(value.id, Info())

    def needs_copy(self, t: Type, info: Info | None = None) -> bool:
        """Whether sharing a value of type `t` could ever be seen: it has parts that can change.

        A value borrowed from a pool always needs copying to be kept: the
        pool may free it.
        """
        if info is not None and any(self.from_pool(b) for b in info.borrows):
            return is_boxed(t)
        return has_mutable(t, self.module.enums)

    def from_pool(self, borrowed: str) -> bool:
        """Whether a borrowed variable holds a pool."""
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
        return slot is not None and slot.mutable and slot.name in self.changed

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
        for expr in later:
            changed |= changed_in_place(expr, self.checker, into_lambdas=False)
        for borrowed in info.borrows:
            if not self.unstable(borrowed):
                continue
            if borrowed.startswith("global "):
                if calls or CALLS in changed or borrowed.removeprefix("global ") in changed:
                    return self.copy(value, self.here(), span)
            elif self.slots[borrowed].name in changed:
                return self.copy(value, self.here(), span)
        return value

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

    def home(self, name: str, t: Type | None = None) -> str:
        """The region for a new local variable: the current level, or the caller's if the function returns it.

        A value that can't be copied stays where it is made, and is moved
        out if it is returned, so that it is finalized in the right place.
        """
        if not self.scopes:
            return "global"
        if self.level == 0 and name in self.returned and not (t is not None and self.nocopy(t)):
            return "result"
        return self.here()

    def bind(
        self, name: str, value: Value, span: Span, mutable: bool = False, region: str = "", kind: str = "local"
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
        self.slots[slot] = SlotInfo(name, mutable, kind, region, borrows)
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

    def read_name(self, name: str, kind: str, span: Span, dest: str = "") -> Value:
        if kind == "local":
            return self.read_local(name, span, dest)
        value = self.emit("global_get", self.module.globals[name], [], span, name)
        self.info[value.id] = Info(frozenset({f"global {name}"}), "global")
        return value

    def write_name(self, name: str, kind: str, value: Value, span: Span) -> None:
        """Store an already placed value in a variable."""
        if kind == "local":
            slot = self.find_slot(name)
            self.emit_effect("store", [value], span, slot)
            self.info[value.id] = Info(frozenset({slot}), self.slots[slot].region)
        else:
            self.emit_effect("global_set", [value], span, name)
            self.info[value.id] = Info(frozenset({f"global {name}"}), "global")

    def var_region(self, name: str, kind: str) -> str:
        """Where a variable's value lives."""
        return self.slots[self.find_slot(name)].region if kind == "local" else "global"

    # Programs and functions

    def lower_program(self, program: Program) -> Module:
        """Lower the top-level code into the `top` function, then every function it uses."""
        self.globals_changed = changed_in_place(program.body, self.checker, into_lambdas=True)
        self.start_function("top", UNIT, program.span, program.body)
        # Generic functions are made for each set of type arguments they are
        # used with, and finalizers are functions of their own.
        for expr in program.body:
            match expr:
                case Let(bindings, Lambda() as generic) if bindings[0].type_params:
                    for binding in bindings:
                        self.module.generics[binding.name] = (generic, binding.type_params)
                case EnumDef(name, finalize=Lambda() as finalizer):
                    t = self.type_of(finalizer)
                    assert isinstance(t, FuncTy)
                    self.pending.append(PendingFunction(finalizer, self.module.enums[name].finalize, [], [], t))
        # Top-level functions exist from the start (docs/grammar.md §3).
        hoisted: dict[int, Value] = {}
        for expr in program.body:
            if isinstance(expr, Let) and expr.bindings[0].type_params:
                continue
            if isinstance(expr, Let) and isinstance(expr.value, Lambda):
                function = self.lower_lambda(expr.value, [b.name for b in expr.bindings], expr.span, "global")
                for binding in expr.bindings:
                    self.emit_effect("global_set", [function], expr.span, binding.name)
                hoisted[id(expr)] = function

        # How many top-level `let`s bind each name.
        lets = Counter(b.name for expr in program.body if isinstance(expr, Let) for b in expr.bindings)
        for expr in program.body:
            if isinstance(expr, Let) and expr.bindings[0].type_params:
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

        # Then run `main`, if there is one.
        main = self.checker.globals.vars.get("main")
        if main is not None and main.kind == "global":
            function = self.emit("global_get", self.module.globals["main"], [], program.span, "main")
            self.emit("call", UNIT, [function], program.span)
        self.block.terminator = Return(self.const((), program.span))
        self.finish_function()

        while self.pending:
            self.lower_function(self.pending.pop(0))
        return self.module

    def lower_lambda(self, function: Lambda, self_names: list[str], span: Span, dest: str = "") -> Value:
        """A lambda expression: a `closure` of a new IR function, capturing every local variable."""
        region = dest or self.here()
        visible: dict[str, str] = {}
        for scope in self.scopes:  # outermost first, so inner names shadow outer ones
            visible.update(scope)
        # Values that can't be copied can't be captured either (the checker makes sure no lambda uses one).
        visible = {
            n: slot for n, slot in visible.items() if slot == SELF or not self.nocopy(self.function.locals[slot])
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

    def lower_function(self, pending: PendingFunction) -> None:
        """Make the IR function for a lambda: captures and parameters, `where`, body, and exit."""
        function, t = pending.function, pending.type
        self.subst = pending.subst
        self.start_function(pending.name, t.result, function.span, function.body)
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
                self.bind(declared.name, param, declared.span, True, f"mut{i}", "mut")
                mut_slots.append(self.scopes[0][declared.name])
            else:
                self.info[param.id] = Info(frozenset(), "0")
                self.bind(declared.name, param, declared.span, region="0", kind="param")

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
                if kind == "variant":
                    return self.emit("variant", self.type_of(expr), [], span, name, region=dest)
                if id(expr) in self.checker.instances:
                    args = tuple(self.concrete(arg) for arg in self.checker.instances[id(expr)])
                    function = self.specialize(name, args, span)
                    return self.emit("closure", self.type_of(expr), [], span, function, region=dest)
                return self.read_name(name, kind, span, dest)
            case Let(bindings, value_expr):
                t = self.type_of(value_expr)
                region = self.home(bindings[0].name, t)
                if isinstance(value_expr, Lambda):
                    value = self.lower_lambda(value_expr, [b.name for b in bindings], span, region)
                else:
                    value = self.lower(value_expr, region)
                for binding in bindings:
                    home = self.home(binding.name, t)
                    value = self.bind(binding.name, value, binding.span, binding.mutable, home)
                return value
            case Assign(op, target, value_expr):
                if isinstance(target, Name):
                    return self.lower_assign_name(op, target, value_expr, span)
                return self.lower_assign_place(op, target, value_expr, span)

            # Operators
            case Unary(op, operand):
                return self.emit(UNARY_OPS[op], self.type_of(expr), [self.lower(operand)], span)
            case Binary(("and" | "or") as op, left, right):
                return self.lower_logic(op, left, right, span)
            case Binary(op, left, right):
                return self.lower_binary(op, left, right, self.type_of(expr), span, dest)
            case Compare(first, rest):
                return self.lower_compare(first, rest, span)
            case Index(target, index):
                container = self.lower(target)
                if isinstance(container.type, BuiltinTy):
                    return self.pool_get(container, self.lower(index), self.type_of(expr), span)
                return self.lower_index(container, self.lower(index), self.type_of(expr), span, dest)
            case Member(target):
                struct = self.lower(target)
                return self.field(struct, self.checker.members[id(expr)], self.type_of(expr), span)
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
            case ReturnExpr(value):
                result = self.const((), span) if value is None else self.lower(value, "result")
                self.jump(self.exit, [self.place_return(result, span)])
                return self.leave(span)

            # Control flow
            case BlockExpr(exprs):
                self.scopes.append({})
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
                outer = self.enter_loop()
                header, body_block = self.new_block("while"), self.new_block("body")
                exit = self.new_block("exit", outer)
                self.jump(header)
                self.block = header
                self.emit_effect("reset", [], span, index=self.level)
                self.branch(self.lower(cond), body_block, exit)
                self.block = body_block
                self.lower_loop_body(LoopTarget(label, exit, header, outer), body)
                self.jump(header)
                self.leave_loop(exit, outer, span)
                return self.const((), span)
            case Loop(label, body):
                outer = self.enter_loop()
                body_block = self.new_block("loop")
                exit, param = self.join_block(self.type_of(expr), outer)
                self.jump(body_block)
                self.block = body_block
                self.emit_effect("reset", [], span, index=self.level)
                loop = LoopTarget(label, exit, body_block, outer)
                self.lower_loop_body(loop, body)
                self.jump(body_block)
                self.leave_loop(exit, outer, span)
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
            case EnumDef():
                return self.const((), span)  # enums are only types; nothing runs

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
                pieces.append(
                    value if value.type == STR else self.emit("to_text", STR, [value], part.span, region=region)
                )
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
        return self.emit(BINARY_OPS[op], t, [a, b], span, region=dest)

    def lower_index(self, container: Value, index: Value, t: Type, span: Span, dest: str) -> Value:
        """`xs[i]` is an item already in the list; `s[i]` makes a new string."""
        result = self.emit("index", t, [container, index], span, region=dest or self.here())
        if isinstance(container.type, ListTy):
            self.info[result.id] = self.info_of(container)
        return result

    def lower_assign_name(self, op: str, target: Name, value_expr: Expr, span: Span) -> Value:
        """`x = v` and `x op= v`."""
        kind = self.checker.kinds[id(target)]
        if is_append(Assign(op, target, value_expr, span), self.checker):
            items = value_expr.right if isinstance(value_expr, Binary) and op == "=" else value_expr
            return self.lower_append(target, kind, items, span)
        region = self.var_region(target.name, kind)
        value = self.lower(value_expr, region if op == "=" else "")
        if op != "=":
            old = self.read_name(target.name, kind, target.span)
            value = self.emit(BINARY_OPS[op.removesuffix("=")], old.type, [old, value], span, region=region)
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
        """`xs[i].y = v` and `xs[i].y op= v`: change the last list, struct or pool in place.

        A pool's values live in arenas of their own (one per slot), so a value
        stored into one is copied there by the operation itself (`@slot`).
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
                value = self.emit(BINARY_OPS[op.removesuffix("=")], old.type, [old, value], span, region=new_region)
        self.store_step(container, (last, key), value, region, span)
        self.info[value.id] = Info(frozenset({self.borrow_key(root.name, kind)}), region)
        return value

    def is_pool_step(self, node: Index | Member) -> bool:
        return isinstance(node, Index) and isinstance(self.type_of(node.target), BuiltinTy)

    def step_key(self, node: Index | Member) -> Value | None:
        """Evaluate a step's index (fields need nothing)."""
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
        if isinstance(container.type, BuiltinTy):
            assert key is not None
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
            assert key is not None
            self.emit_effect("set_index", [container, key, value], where, region=region)

    def field(self, struct: Value, index: int, t: Type, span: Span) -> Value:
        """A struct's field, borrowed."""
        assert isinstance(struct.type, EnumTy)
        return self.variant_field(struct, struct.type.name, index, t, span)

    def pool_get(self, pool: Value, handle: Value, t: Type, span: Span) -> Value:
        """A pool's value, borrowed: it lives in the slot's own arena, until the slot is freed."""
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
            result = self.emit("contains", BOOL, [left, right], span)
            return self.emit("not", BOOL, [result], span) if op == "not in" else result
        return self.emit(COMPARE_OPS[op], BOOL, [left, right], span)

    def lower_try(self, value: Value, t: Type, span: Span) -> Value:
        """`v?`: if v is `Ok(x)` (or `Some(x)`) the result is x; otherwise return the error."""
        assert isinstance(value.type, EnumTy)
        ok, error = ("Ok", "Error") if value.type.name == "Result" else ("Some", "None")
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
            return self.build("variant", self.type_of(expr), self.lower_items(args, dest), span, dest, callee.name)
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
        result = self.emit("call", self.type_of(expr), [function, *values], span, region=dest)
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
        """`print`, `len`, `run`, and the pool functions."""
        if name in ("alloc", "free"):
            pool_arg, value_expr = args
            assert isinstance(pool_arg, MutArg)
            _, pool = self.mut_place(pool_arg)
            value = self.lower(value_expr)
            if name == "free":
                self.emit_effect("free", [pool, value], span)
                return self.const((), span)
            # The pool copies the value into an arena of its own (a value that
            # can't be copied is moved there, with its finalizers).
            return self.emit("alloc", self.type_of(expr), [pool, value], span)
        operands = [self.lower(arg) for arg in args]
        match name:
            case "len":
                return self.emit("len", I64, operands, span)
            case "print":
                self.emit_effect("print", operands, span)
                return self.const((), span)
            case "pool_new":
                return self.emit("pool_new", self.type_of(expr), [], span, region=dest)
            case "run":
                return self.emit("run", self.type_of(expr), operands, span, region=dest)
            case _:  # reflection, which only the interpreters can do
                return self.emit("reflect", self.type_of(expr), operands, span, name, region=dest)

    def lower_quote(self, quote: Quote, span: Span) -> Value:
        """`quote e`: code, built at run time by the interpreter from the quote's syntax tree.

        The splices of the quote, and the variables it carries into the
        later stage, are evaluated here first, in the order the interpreter
        will want them (see ella.staging.build_code). The `quote` instruction
        gets the `quote_begin`s of the quotes it is inside the splices of
        too, for their fresh names.
        """
        events: list[AnySplice | Name] = []

        def splice(node: AnySplice) -> object:
            events.append(node)
            return "_" if isinstance(node, FieldSplice) else node  # a field's name; any will do here

        def persist(node: Name) -> Expr:
            events.append(node)
            return node

        start_code(quote, self.module.tables, count()).build(splice, persist)  # names aren't needed here
        number = len(self.module.templates)
        outer = list(self.quotes)
        self.module.templates[number] = (quote, len(outer))
        # `quote_begin` chooses the fresh names, which quotes in the splices may use.
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

    def specialize(self, name: str, args: tuple[Type, ...], span: Span) -> str:
        """The IR function for a generic function with these type arguments: `first[i64]`."""
        key = (name, args)
        if key not in self.module.specializations:
            function, params = self.module.generics[name]
            if sum(1 for n, _ in self.module.specializations if n == name) >= MAX_SPECIALIZATIONS:
                raise EllaError(f"{name} is used with too many different types", span)
            specialized = f"{name}[{', '.join(show_type(arg) for arg in args)}]"
            self.module.specializations[key] = specialized
            subst = dict(zip(params, args))
            t = substitute(finish(self.checker.types[id(function)]), subst)
            assert isinstance(t, FuncTy)
            self.pending.append(PendingFunction(function, specialized, [], [], t, subst))
        return self.module.specializations[key]

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

    def enter_loop(self) -> int:
        """Start lowering a loop's iterations, one level deeper. Returns the level outside it."""
        outer = self.level
        self.level += 1
        self.max_level = max(self.max_level, self.level)
        return outer

    def leave_loop(self, exit: Block, outer: int, span: Span) -> None:
        """Continue after a loop, at its exit, which frees the loop's level."""
        self.level = outer
        self.block = exit
        self.emit_effect("reset", [], span, index=outer + 1)

    def lower_for(self, expr: For) -> Value:
        """`for pattern in xs body` counts through the items with a hidden counter:

        n = len xs; i = 0
        header: free the last iteration; if i < n go to body, else exit
        body:   item = index xs, i; match the pattern; body; go to next
        next:   i = i + 1; go to header
        """
        span = expr.span
        iterable = self.protect(self.lower(expr.iterable), [expr.body], span)
        count = self.emit("len", I64, [iterable], span)
        counter = self.new_slot("for.i", I64)
        self.function.local_regions[counter] = self.here()
        self.emit_effect("store", [self.const(0, span)], span, counter)
        outer = self.enter_loop()
        header, body, next_block = (self.new_block(h) for h in ("for", "body", "next"))
        exit = self.new_block("exit", outer)
        self.jump(header)

        self.block = header
        self.emit_effect("reset", [], span, index=self.level)
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
        i = self.emit("load", I64, [], span, counter)
        self.emit_effect("store", [self.emit("add", I64, [i, self.const(1, span)], span)], span, counter)
        self.jump(header)

        self.leave_loop(exit, outer, span)
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

    def lower_pattern(self, pattern: Pattern, value: Value, fail: Block) -> None:
        """Test `value` against `pattern`, binding its variables; go to `fail` if it doesn't match.

        The tests are emitted one after another. Each failing test branches to
        `fail`, and lowering continues in the block where it succeeded.
        """
        span = pattern.span
        match pattern:
            case WildcardPattern():
                pass
            case NamePattern(name):
                if self.checker.kinds[id(pattern)] == "variant":
                    self.test(self.emit("is_variant", BOOL, [value], span, name), fail)
                else:
                    self.bind(name, value, span)
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
                    self.lower_pattern(alternative, value, next_alternative)
                    self.jump(matched)
                    self.block = next_alternative
                self.lower_pattern(alternatives[-1], value, fail)
                self.jump(matched)
                self.block = matched
            case VariantPattern(name, args):
                self.test(self.emit("is_variant", BOOL, [value], span, name), fail)
                for i, (arg, field_type) in enumerate(zip(args, self.fields(value.type, name))):
                    self.lower_pattern(arg, self.variant_field(value, name, i, field_type, span), fail)
            case TuplePattern(items):
                assert isinstance(value.type, TupleTy)
                for i, (item, item_type) in enumerate(zip(items, value.type.items)):
                    item_value = self.emit("tuple_get", item_type, [value], span, index=i)
                    self.info[item_value.id] = self.info_of(value)
                    self.lower_pattern(item, item_value, fail)

    def test(self, cond: Value, fail: Block) -> None:
        """Continue in a new block if `cond` is true, otherwise go to `fail`."""
        passed = self.new_block("match")
        self.branch(cond, passed, fail)
        self.block = passed


# Finding what code does, for the decisions above


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
            return calls | names_written(callee, checker) | names_written(args, checker)
        case Lambda():
            return set()
    return set().union(*(names_written(child, checker) for child in children(node)))


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
    return changed.union(*(changed_in_place(child, checker, into_lambdas) for child in children(node)))


def returned_names(body: Expr | tuple[Expr, ...]) -> set[str]:
    """The names of variables a function body may return as they are: its last value, or a `return`'s."""

    def tails(expr: Expr | None) -> set[str]:
        match expr:
            case Name(name):
                return {name}
            case BlockExpr(exprs):
                return tails(exprs[-1])
            case If(_, then, else_):
                return tails(then) | tails(else_)
            case Match(_, cases):
                return set().union(*(tails(case.body) for case in cases))
        return set()

    def returns(node: object) -> set[str]:
        match node:
            case ReturnExpr(value):
                return tails(value) | returns(value)
            case Lambda():
                return set()
        return set().union(*(returns(child) for child in children(node)))

    if isinstance(body, tuple):
        return set()
    return tails(body) | returns(body)


def children(node: object) -> list[object]:
    """The parts of a syntax tree node (or the items of a tuple of nodes)."""
    if isinstance(node, tuple):
        return list(node)
    if is_dataclass(node) and not isinstance(node, type):
        return [getattr(node, f.name) for f in fields(node)]
    return []
