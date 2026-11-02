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
    functions       Closure (written in Ella) or Builtin (written in Python)

Python's `bool` is a subclass of `int`, so code that tells values apart must
test for `bool` before `int`. `type_name` does that, and the interpreter uses
it for every type check.

Ella has value semantics: binding, passing and returning all copy. Lists are
mutable Python lists, so the interpreter calls `copy_value` whenever it reads a
value out of a variable or a list. The copies cost memory and time, but they
make the rules easy to see. Avoiding copies is a job for the optimizer later.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ella.lexer import Span
from ella.syntax import Lambda

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1


@dataclass(frozen=True, slots=True)
class VariantValue:
    """An enum value: `Some(1)` is `VariantValue("Optional", "Some", (1,))`."""

    enum: str
    variant: str
    fields: tuple[Value, ...] = ()


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


type Value = (
    int | float | bool | str | tuple[Value, ...] | list[Value] | range | VariantValue | Constructor | Closure | Builtin
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


def fits_i64(n: int) -> bool:
    """Python integers never overflow, so i64 results are checked with this."""
    return I64_MIN <= n <= I64_MAX


def wrap(n: int) -> int:
    """Keep the low 64 bits of an integer, as two's complement hardware does.

    Arithmetic overflow is an error in Ella, but `<<` is a bit operation and
    simply loses the bits shifted out: `wrap(1 << 63) == I64_MIN`.
    """
    return (n - I64_MIN) % 2**64 + I64_MIN


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
        case Constructor() | Closure() | Builtin():
            return "function"


def copy_value(value: Value) -> Value:
    """A copy of `value` that shares no mutable parts with it (only lists are mutable)."""
    match value:
        case list():
            return [copy_value(item) for item in value]
        case tuple():
            return tuple(copy_value(item) for item in value)
        case VariantValue(enum, variant, fields) if fields:
            return VariantValue(enum, variant, tuple(copy_value(item) for item in fields))
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
                return variant
            return f"{variant}(" + ", ".join(to_text(item, True) for item in fields) + ")"
        case Builtin(name=name):
            return f"<built-in function {name}>"
        case Constructor(variant=variant):
            return f"<constructor {variant}>"
        case Closure():
            return "<function>"
