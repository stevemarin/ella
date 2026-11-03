"""Run-time values, shared by the interpreters.

Ella values are represented by plain Python values where one fits:

    Ella            Python
    i64             int (checked to stay in the i64 range)
    f64             float
    bool            bool
    str             str
    ()              () (the empty tuple: unit is the tuple with no items)
    (T, U)          tuple
    [T]             list
    a..b, a..=b     range
    enum variants   VariantValue, or Constructor for a variant with fields
    structs         VariantValue too (a struct is an enum with one variant),
                    whose fields can change
    functions       Closure (written in Ella) or Builtin (written in Python)
                    in the AST interpreter; FunctionRef in the IR interpreter
    Pool[T]         PoolValue, and Handle[T] is HandleValue
    Arena           ArenaValue, and Ref[T] is RefValue
    Gen[T]          GenValue, whose state belongs to the interpreter running it
    Code[T]         CodeValue, holding the code's syntax tree

Python's `bool` is a subclass of `int`, so code that tells values apart must
test for `bool` before `int`. `type_name` does that, and the interpreter uses
it for every type check.

Ella has value semantics: binding, passing and returning all copy. Lists are
mutable Python lists, so the AST interpreter calls `copy_value` whenever it
reads a value out of a variable or a list. The copies cost memory and time,
but they make the rules easy to see. The compiler copies only where a
program could tell (see ella.lower), and the IR interpreter runs its `copy`
instructions with `copy_value` too.
"""

import itertools
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ella.checker import Type, show_type
from ella.lexer import EllaError, Span
from ella.syntax import Expr, Lambda, bare_name, show

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1

# How deeply Ella functions may call each other before the program stops
# with "stack overflow", by default. Every backend has the same limit.
MAX_CALL_DEPTH = 10_000


@dataclass(eq=False, slots=True)
class VariantValue:
    """An enum or struct value: `Some(1)` is `VariantValue("Optional", "Some", [1])`.

    A struct's fields can change in place. `nocopy` marks a value of a nocopy
    struct, which `copy_value` leaves as it is: it is never copied.
    """

    enum: str
    variant: str
    fields: list[Value] = field(default_factory=list)
    nocopy: bool = False


@dataclass(eq=False, slots=True)
class PoolValue:
    """A `Pool[T]`: slots holding values, each with a generation that goes up when it is freed.

    A slot holds None while it is free. Freed slots are reused, the most
    recently freed first.
    """

    slots: list[Value | None] = field(default_factory=list)
    generations: list[int] = field(default_factory=list)
    free: list[int] = field(default_factory=list)

    def live(self) -> int:
        return sum(1 for slot in self.slots if slot is not None)


@dataclass(frozen=True, slots=True)
class HandleValue:
    """A `Handle[T]`: which slot of a pool, and its generation when the value was put there."""

    slot: int
    generation: int


# Numbers that tell arenas apart, for their refs.
arena_ids = itertools.count()


@dataclass(eq=False, slots=True)
class ArenaValue:
    """An `Arena`: values that are freed together, all at once.

    Freeing them (`free(mut arena)`) bumps the arena's generation, so refs
    to them are caught. `id` tells arenas apart, so a ref used with another
    arena is caught too.
    """

    values: list[Value] = field(default_factory=list)
    generation: int = 0
    id: int = field(default_factory=lambda: next(arena_ids))


@dataclass(eq=False, slots=True)
class GenValue:
    """A `Gen[T]`: a generator, which runs its body a step at a time (docs/grammar.md §9).

    `state` is the interpreter's: a thread running the body in the AST
    interpreter, a kept frame in the IR interpreter. It can't be copied.
    """

    state: object = None


@dataclass(frozen=True, slots=True)
class RefValue:
    """A `Ref[T]`: which value of an arena, the arena, and the arena's generation when it was put there."""

    arena: int
    generation: int
    index: int


@dataclass(frozen=True, slots=True)
class TypeValue:
    """A `Type[T]`: a type, as a value for code that generates code (`type T`)."""

    type: Type


@dataclass(frozen=True, slots=True)
class CodeValue:
    """A `Code[T]`: the syntax tree of code for a later stage, made by `quote`."""

    expr: Expr


@dataclass(frozen=True, slots=True)
class Constructor:
    """A variant that has fields, such as `Some`. Calling it makes a VariantValue."""

    enum: str
    variant: str
    arity: int


@dataclass(eq=False, slots=True)
class Closure:
    """A function value: a lambda together with the variables it captured.

    `eq=False` makes closures compare by identity, as functions have no
    meaningful structural equality.
    """

    function: Lambda
    scope: Scope


@dataclass(eq=False, slots=True)
class Builtin:
    """A function implemented in Python, such as `print`."""

    name: str
    arity: int
    run: Callable[[list[Value], Span], Value]


@dataclass(frozen=True, slots=True)
class FunctionRef:
    """A function value in the IR interpreter: an IR function and its captured values."""

    name: str
    captured: tuple[Value, ...] = ()


type Value = (
    int
    | float
    | bool
    | str
    | tuple[Value, ...]
    | list[Value]
    | range
    | VariantValue
    | Constructor
    | Closure
    | Builtin
    | FunctionRef
    | PoolValue
    | HandleValue
    | ArenaValue
    | GenValue
    | RefValue
    | CodeValue
    | TypeValue
)

UNIT: Value = ()


@dataclass(slots=True)
class Var:
    """A variable: its current value and whether it may be assigned to.

    `ready` is False for a top-level name whose `let` hasn't run yet. Top-level
    names exist from the start, so functions can refer to each other in any
    order, but reading one too early is an error.
    """

    value: Value
    mutable: bool
    ready: bool = True


@dataclass(slots=True)
class Scope:
    """The variables of one block or function call, plus a link to the enclosing scope.

    Looking a name up walks outward through `parent` until it is found, so
    inner scopes see (and can shadow) the names of outer ones.
    """

    parent: Scope | None
    vars: dict[str, Var] = field(default_factory=dict)

    def lookup(self, name: str) -> Var | None:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.vars:
                return scope.vars[name]
            scope = scope.parent
        return None

    def define(self, name: str, value: Value, mutable: bool = False) -> None:
        """Add a variable to this scope, replacing (shadowing) any with the same name."""
        self.vars[name] = Var(value, mutable)


STATUS_ERROR = "main returned {}, but an exit status must be from 0 to 255"


def check_status(status: int, span: Span) -> int:
    """The exit status `main` returned, which must fit in a byte."""
    if not 0 <= status <= 255:
        raise EllaError(STATUS_ERROR.format(status), span)
    return status


def fits_i64(n: int) -> bool:
    """Python integers never overflow, so i64 results are checked with this."""
    return I64_MIN <= n <= I64_MAX


def type_name(value: Value) -> str:
    """The Ella type of a value, as it appears in error messages."""
    match value:
        case bool():  # before int, since bool is a subclass of int
            return "bool"
        case int():
            return "i64"
        case float():
            return "f64"
        case str():
            return "str"
        case tuple() if not value:
            return "()"
        case tuple():
            return "tuple"
        case list():
            return "list"
        case range():
            return "range"
        case VariantValue(enum=enum):
            return enum
        case Constructor() | Closure() | Builtin() | FunctionRef():
            return "function"
        case PoolValue():
            return "Pool"
        case HandleValue():
            return "Handle"
        case ArenaValue():
            return "Arena"
        case GenValue():
            return "Gen"
        case RefValue():
            return "Ref"
        case CodeValue():
            return "Code"
        case TypeValue():
            return "Type"


def copy_value(value: Value) -> Value:
    """A copy of `value` that shares no parts that can change with it (lists and structs).

    Values that can't be copied (nocopy structs, pools and arenas) are returned as
    they are.
    """
    match value:
        case list():
            return [copy_value(item) for item in value]
        case tuple():
            return tuple(copy_value(item) for item in value)
        case VariantValue(enum, variant, fields, nocopy) if fields and not nocopy:
            return VariantValue(enum, variant, [copy_value(item) for item in fields])
        case _:
            return value


def equal(a: Value, b: Value) -> bool:
    """Structural equality: lists, tuples and variants are equal if their parts are.

    Values of different types are never equal. (Python would say `1 == True`.)
    """
    if type_name(a) != type_name(b):
        return False
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    if isinstance(a, VariantValue) and isinstance(b, VariantValue):
        return a.variant == b.variant and equal(a.fields, b.fields)
    if isinstance(a, PoolValue | ArenaValue | GenValue | CodeValue):
        return a is b
    return a == b


def to_text(value: Value, nested: bool = False) -> str:
    """The text form of a value, used by `print` and string interpolation.

    A string is its own text, but inside a list or other value it is quoted, so
    that `["a, b"]` and `["a", "b"]` print differently.
    """
    match value:
        case bool():
            return "true" if value else "false"
        case int() | float():
            return str(value)
        case str():
            return json.dumps(value, ensure_ascii=False) if nested else value
        case tuple() if len(value) == 1:
            return f"({to_text(value[0], True)},)"
        case tuple():
            return "(" + ", ".join(to_text(item, True) for item in value) + ")"
        case list():
            return "[" + ", ".join(to_text(item, True) for item in value) + "]"
        case range():
            return f"{value.start}..{value.stop}"
        case VariantValue(variant=variant, fields=fields):
            if not fields:
                return bare_name(variant)
            return f"{bare_name(variant)}(" + ", ".join(to_text(item, True) for item in fields) + ")"
        case Closure() | FunctionRef() | Builtin() | Constructor():
            return "<function>"
        case PoolValue():
            return f"<pool of {value.live()}>"
        case HandleValue(slot, generation):
            return f"<handle {slot}:{generation}>"
        case ArenaValue(values):
            return f"<arena of {len(values)}>"
        case RefValue(index=index):
            return f"<ref {index}>"
        case GenValue():
            return "<gen>"
        case CodeValue(expr):
            return f"<code {show(expr)}>"
        case TypeValue(t):
            return f"<type {show_type(t)}>"


def run_deep[T](run: Callable[[], T], max_depth: int, frames_per_call: int) -> T:
    """Run an interpreter, which recurses for each Ella call, so that `max_depth` nested calls fit.

    Python's recursion limit is raised to match, and the interpreter runs in
    a thread of its own with a big stack: the main thread's is too small
    for the C code under deep Python recursion. (The stack's memory is only
    used as it is needed.)
    """
    results: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            results.append(run())
        except BaseException as error:  # noqa: BLE001  (raised again in the calling thread)
            errors.append(error)

    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, max_depth * frames_per_call + 1000))
    size = threading.stack_size(max(64 << 20, max_depth * (16 << 10)))
    try:
        thread = threading.Thread(target=target, daemon=True)  # a Ctrl-C in the main thread ends the program
        thread.start()
    finally:
        threading.stack_size(size)
    try:
        thread.join()
    finally:
        sys.setrecursionlimit(limit)
    if errors:
        raise errors[0]
    return results[0]
