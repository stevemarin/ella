"""Run-time values, shared by the interpreters.

Ella values are represented by plain Python values where one fits:

    Ella            Python
    i64             int (checked to stay in the i64 range)
    f64             float
    f32             F32, a float an f32 can hold
    i32             I32, an int an i32 can hold
    bool            bool
    str             str
    ()              () (the empty tuple: unit is the tuple with no items)
    (T, U)          tuple
    [T]             list
    Array[T]        ArrayValue: its shape and its items, in one flat list
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
import math
import struct
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ella.checker import Type, show_type
from ella.lexer import EllaError, Span, parse_place
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
    struct, which `copy_value` leaves as it is: it is never copied. `error`
    marks a built-in Error (its fields are message, cause and at), whose
    equality and text form leave out where it was made (`at`), and show its
    cause without the `Some`: `Error("bad row", Error("not a number"))`.
    """

    enum: str
    variant: str
    fields: list[Value] = field(default_factory=list)
    nocopy: bool = False
    error: bool = False


def main_error(error: Value, span: Span) -> EllaError:
    """The error that stops the program when `main` (at `span`) returns `Err(error)`.

    A built-in Error is reported where it was made, with its causes after
    it; any other error as what main returned.
    """
    if not (isinstance(error, VariantValue) and error.error):
        return EllaError(f"main returned Err({to_text(error, nested=True)})", span)
    causes: list[tuple[Span, str]] = []
    while True:
        message, cause, at = error.fields
        assert isinstance(message, str) and isinstance(cause, VariantValue) and isinstance(at, str)
        causes.append((parse_place(at) or span, message))
        if not cause.fields:
            break
        error = cause.fields[0]
        assert isinstance(error, VariantValue)
    (place, message), *rest = causes
    result = EllaError(message, place)
    result.causes = rest
    return result


def make_error(message: str, span: Span, cause: Value | None = None) -> VariantValue:
    """A built-in Error, made at `span`."""
    optional = VariantValue("Optional", "Some", [cause]) if cause is not None else VariantValue("Optional", "None")
    return VariantValue("Error", "Error", [message, optional, str(span)], error=True)


class I32(int):
    """An `i32`: an int from -2**31 to 2**31 - 1. Arithmetic on two of them traps if the result doesn't fit."""

    __slots__ = ()


def fits_i32(n: int) -> bool:
    return -(2**31) <= n < 2**31


class F32(float):
    """An `f32`: a float that an f32 holds exactly. Arithmetic on two of them is done exactly (in f64, which
    has room for it) and then rounded to an f32 (`to_f32`), which gives what f32 hardware gives."""

    __slots__ = ()


def to_f32(x: float) -> F32:
    """The f32 nearest x (rounding halfway cases to even), or an infinity if it is too big for one."""
    try:
        return F32(struct.unpack("<f", struct.pack("<f", x))[0])
    except OverflowError:  # rounded, it is past the largest f32
        return F32(math.copysign(math.inf, x))


def f32_text(x: F32) -> str:
    """The shortest decimal that is nearest to x of all f32s, written as `repr` writes an f64: `0.1`, `1e-05`."""
    if not math.isfinite(x):
        return repr(float(x))
    for digits in range(1, 10):
        text = f"{x:.{digits}g}"
        if to_f32(float(text)) == x:
            return repr(float(text))
    raise AssertionError(x)


@dataclass(eq=False, slots=True)
class ArrayValue:
    """An `Array[T]`: a shape (the size of each dimension), and its items in row-major order.

    `items` has the product of the sizes, and can change in place (`a[i, j] = x`).
    `item` is T's name, "i64", "f64" or "f32", which an empty array's items can't show.
    """

    shape: tuple[int, ...]
    items: list[Value]  # all of type `item`
    item: str

    @property
    def zero(self) -> Value:
        """0 of the items' type."""
        return {"i64": 0, "i32": I32(0), "f64": 0.0, "f32": F32(0.0)}[self.item]


pool_ids = itertools.count()
MAX_POOL_SLOTS = 1 << 20
MAX_HANDLE_GENERATION = 1 << 20


@dataclass(eq=False, slots=True)
class PoolValue:
    """A `Pool[T]`: slots holding values, each with a generation that goes up when it is freed.

    A slot holds None while it is free. Freed slots are reused, the most
    recently freed first.
    """

    slots: list[Value | None] = field(default_factory=list)
    generations: list[int] = field(default_factory=list)
    free: list[int] = field(default_factory=list)
    id: int = field(default_factory=lambda: next(pool_ids))

    def live(self) -> int:
        return sum(1 for slot in self.slots if slot is not None)


@dataclass(frozen=True, slots=True)
class HandleValue:
    """A `Handle[T]`: its pool identity, slot and generation."""

    slot: int
    generation: int
    pool: int


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
    """A variant that has fields, such as `Some`. Calling it makes a VariantValue.

    The built-in Error's (`error`) takes a message and maybe a cause. Used as
    a value, its Errors are made where it is used (`at`), as the lowering's
    function for it makes them.
    """

    enum: str
    variant: str
    arity: int
    error: bool = False
    at: Span | None = None


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
    | ArrayValue
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
        case I32():
            return "i32"
        case int():
            return "i64"
        case F32():
            return "f32"
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
        case ArrayValue():
            return "Array"
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
        case ArrayValue(shape, items, item):
            return ArrayValue(shape, items.copy(), item)
        case VariantValue(enum, variant, fields, nocopy, error) if fields and not nocopy:
            return VariantValue(enum, variant, [copy_value(item) for item in fields], error=error)
        case _:
            return value


def equal(a: Value, b: Value, custom: Callable[[VariantValue, VariantValue], bool | None] | None = None) -> bool:
    """Structural equality: lists, tuples and variants are equal if their parts are.

    Values of different types are never equal. (Python would say `1 == True`.)
    `custom` compares two struct or enum values whose type has an `eq` method
    (by calling it), or gives None; without it, as in patterns, equality is
    always structural.
    """
    if type_name(a) != type_name(b):
        return False
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(equal(x, y, custom) for x, y in zip(a, b))
    if isinstance(a, ArrayValue) and isinstance(b, ArrayValue):
        return a.shape == b.shape and all(x == y for x, y in zip(a.items, b.items))
    if isinstance(a, VariantValue) and isinstance(b, VariantValue):
        if custom is not None and (same := custom(a, b)) is not None:
            return same
        if a.error:  # where an Error was made doesn't count
            return equal(a.fields[:2], b.fields[:2])
        return a.variant == b.variant and equal(a.fields, b.fields, custom)
    if isinstance(a, PoolValue | ArenaValue | GenValue | CodeValue):
        return a is b
    return a == b


def to_text(value: Value, nested: bool = False, custom: Callable[[VariantValue], str | None] | None = None) -> str:
    """The text form of a value, used by `print` and string interpolation.

    A string is its own text, but inside a list or other value it is quoted, so
    that `["a, b"]` and `["a", "b"]` print differently. `custom` gives the text
    of a struct or enum value whose type has a `text` method (by calling it),
    or None; without it, as in error messages, every value has its built-in form.
    """

    def part(item: Value) -> str:
        return to_text(item, True, custom)

    match value:
        case bool():
            return "true" if value else "false"
        case F32():
            return f32_text(value)
        case int() | float():
            return str(value)
        case str():
            return json.dumps(value, ensure_ascii=False) if nested else value
        case tuple() if len(value) == 1:
            return f"({part(value[0])},)"
        case tuple():
            return "(" + ", ".join(map(part, value)) + ")"
        case list():
            return "[" + ", ".join(map(part, value)) + "]"
        case range():
            return f"{value.start}..{value.stop}"
        case ArrayValue(shape, items):
            return f"array({nested_text(shape, items)})"
        case VariantValue() if custom is not None and (text := custom(value)) is not None:
            return text
        case VariantValue(fields=[message, VariantValue(fields=cause), _], error=True):
            return f"Error({part(message)}" + "".join(f", {part(c)}" for c in cause) + ")"
        case VariantValue(variant=variant, fields=fields):
            if not fields:
                return bare_name(variant)
            return f"{bare_name(variant)}(" + ", ".join(map(part, fields)) + ")"
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


def nested_text(shape: tuple[int, ...], items: list[Value]) -> str:
    """An array's items as nested lists, one level per dimension: `[[1.0, 2.0], [3.0, 4.0]]`."""
    if len(shape) == 1:
        return "[" + ", ".join(map(to_text, items)) + "]"
    step = len(items) // shape[0] if shape[0] else 0
    rows = (nested_text(shape[1:], items[i * step : (i + 1) * step]) for i in range(shape[0]))
    return "[" + ", ".join(rows) + "]"


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
