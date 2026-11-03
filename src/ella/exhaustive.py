"""Exhaustiveness: do a `match`'s cases cover every value of its subject's type?

The checker calls `missing` once all types are known. It returns an example
of a value that no pattern matches, written like a pattern (`Some(None)`,
`(false, _)`), or None if every value is matched. The same check makes sure
that the patterns of `let` and `for` match every value.

This is the classic algorithm for ML-style patterns (Maranget, "Warnings
for pattern matching", 2007), simplified. The patterns are rows of a
matrix, with a column for each value still to be matched; at first there
is one column, the subject. Looking at the first column:

* If its type has a fixed set of *constructors* (an enum's variants, the
  one shape of a struct or tuple, `true` and `false`) and the patterns name
  every one of them, each constructor is tried in turn: keep the rows that
  can match it, with its fields' patterns in place of the column.
  Wildcards match every constructor, with wildcards for the fields.
* Otherwise (some constructor isn't named, or the type has no fixed set,
  like `i64` or `str`), only the rows starting with a wildcard can match
  the values that aren't named. The example is one of those values,
  followed by whatever those rows miss in the other columns.

With no columns left, a value is missed if no rows are left either. Cases
with a guard don't count, since the guard may be false; literals other
than `true` and `false`, and ranges, never cover their whole type.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ella.syntax import (
    LiteralPattern,
    NamePattern,
    OrPattern,
    Pattern,
    RangePattern,
    TuplePattern,
    VariantPattern,
    WildcardPattern,
)

TUPLE = "()"  # the constructor of tuples (and of `()`, the empty tuple)


@dataclass(frozen=True, slots=True)
class Shape:
    """A constructor in a pattern, with patterns for its fields: a variant, a tuple, `true` or `false`."""

    name: str
    args: tuple[Pat, ...]


# A pattern reduced to what matters here: a wildcard (None), a constructor,
# or PARTIAL for a literal or range, which matches only some values.
PARTIAL = "partial"
type Pat = Shape | str | None

# A type's constructors, with the types of their fields, or None if it has no fixed set.
type Constructors[T] = Callable[[T], list[tuple[str, tuple[T, ...]]] | None]


def missing[T](patterns: Sequence[Pattern], t: T, constructors: Constructors[T], variants: set[int]) -> str | None:
    """An example of a value of type `t` that none of the patterns match, or None if they match every value.

    `variants` has the ids of the name patterns that name a variant, like
    `None`, rather than bind a variable.
    """
    rows = [[pat] for pattern in patterns for pat in reduce(pattern, variants)]
    found = Matrix(constructors).missing(rows, [t])
    return None if found is None else found[0]


def reduce(pattern: Pattern, variants: set[int]) -> list[Pat]:
    """The reduced patterns a pattern stands for: one, or one for each alternative of an or-pattern."""
    match pattern:
        case WildcardPattern():
            return [None]
        case NamePattern(name):
            return [Shape(name, ())] if id(pattern) in variants else [None]
        case LiteralPattern(bool() as value):
            return [Shape("true" if value else "false", ())]
        case LiteralPattern() | RangePattern():
            return [PARTIAL]
        case OrPattern(alternatives):
            return [pat for alternative in alternatives for pat in reduce(alternative, variants)]
        case VariantPattern(name, args):
            return [Shape(name, combined) for combined in combinations(args, variants)]
        case TuplePattern(items):
            return [Shape(TUPLE, combined) for combined in combinations(items, variants)]


def combinations(patterns: Sequence[Pattern], variants: set[int]) -> list[tuple[Pat, ...]]:
    """Every combination of the patterns' alternatives: `(0 | 1, x)` is `(0, x)` and `(1, x)`."""
    result: list[tuple[Pat, ...]] = [()]
    for pattern in patterns:
        result = [(*done, pat) for done in result for pat in reduce(pattern, variants)]
    return result


class Matrix[T]:
    def __init__(self, constructors: Constructors[T]) -> None:
        self.constructors = constructors

    def missing(self, rows: list[list[Pat]], types: list[T]) -> list[str] | None:
        """Values, one for each column, that no row matches, written as patterns; None if the rows match all."""
        if not types:
            return None if rows else []
        t, rest = types[0], types[1:]
        constructors = self.constructors(t)
        named = {row[0].name for row in rows if isinstance(row[0], Shape)}
        if constructors is not None and all(name in named for name, _ in constructors):
            for name, fields in constructors:
                found = self.missing(specialize(rows, name, len(fields)), [*fields, *rest])
                if found is not None:
                    return [show(name, found[: len(fields)]), *found[len(fields) :]]
            return None
        found = self.missing([row[1:] for row in rows if row[0] is None], rest)
        if found is None:
            return None
        if constructors is None:
            return ["_", *found]
        name, fields = next(c for c in constructors if c[0] not in named)
        return [show(name, ["_"] * len(fields)), *found]


def specialize(rows: list[list[Pat]], name: str, arity: int) -> list[list[Pat]]:
    """The rows that can match constructor `name`, with its fields' patterns in place of their first column."""
    result = []
    for first, *rest in rows:
        if first is None:
            result.append([None] * arity + rest)
        elif isinstance(first, Shape) and first.name == name:
            result.append([*first.args, *rest])
    return result


def show(name: str, args: Sequence[str]) -> str:
    """A constructor with patterns for its fields, as Ella writes it."""
    if name == TUPLE:
        return f"({args[0]},)" if len(args) == 1 else f"({', '.join(args)})"
    return f"{name}({', '.join(args)})" if args else name
