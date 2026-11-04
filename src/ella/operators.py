"""Ella's operators on run-time values, shared by every interpreter.

The AST interpreter and the IR interpreter both call these functions, so that
`7 // -2`, an out-of-range index or an i64 overflow behave (and fail, with the
same message) identically however a program is run.

Each function takes the operator's source span, used for the error message
if the operation fails.
"""

import math
from collections.abc import Callable

from ella.lexer import EllaError, Span
from ella.values import (
    F32,
    I32,
    ArrayValue,
    Value,
    VariantValue,
    equal,
    fits_i32,
    fits_i64,
    to_f32,
    to_text,
    type_name,
)


def float_div(a: float, b: float) -> float:
    """`a / b` as IEEE 754 floating point (and so x64 hardware) defines it: dividing
    by zero gives an infinity, or NaN for 0 / 0, rather than an error."""
    if b == 0.0:
        if a == 0.0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def float_pow(a: float, b: float) -> float:
    """`a ** b` for f64, giving infinity or NaN where Python would raise an error."""
    try:
        result = a**b
    except ZeroDivisionError:  # 0.0 ** -1.0
        return math.inf
    except OverflowError:
        return -math.inf if a < 0 and b % 2 == 1 else math.inf
    # A negative number to a fractional power has no real answer; Python
    # would give a complex number.
    return math.nan if isinstance(result, complex) else result


def unary(op: str, value: Value, span: Span) -> Value:
    match op, value:
        case "not", bool():
            return not value
        case _, bool():
            pass  # bools aren't numbers, though Python thinks they are
        case "-", I32():
            if not fits_i32(-value):
                raise EllaError(f"i32 overflow: -({value}) doesn't fit in 32 bits", span)
            return I32(-value)
        case "-", int():
            return check_i64(-value, f"-({value})", span)
        case "-", F32():
            return F32(-value)
        case "-", float():
            return -value
        case "-", ArrayValue(shape, items, item):
            return ArrayValue(shape, [unary(op, x, span) for x in items], item)
        case "~", int():
            return ~value
    raise EllaError(f"'{op}' doesn't work on {type_name(value)}", span)


def binary(op: str, a: Value, b: Value, span: Span) -> Value:
    """Apply an arithmetic, bitwise or range operator to two values."""
    if isinstance(a, ArrayValue) or isinstance(b, ArrayValue):
        return array_op(op, a, b, span)
    # There are no implicit conversions, so both sides must have the same type.
    if type_name(a) != type_name(b):
        raise EllaError(f"'{op}' needs two values of the same type, not {type_name(a)} and {type_name(b)}", span)
    match a, b:
        case bool(), bool():
            pass
        case int(), int() if op in ("..", "..="):
            return range(a, b + 1 if op == "..=" else b)
        case I32(), I32():
            return i32_op(op, a, b, span)
        case int(), int():
            return int_op(op, a, b, span)
        case F32(), F32():
            # Exact in f64 (or, for `**`, as C's pow), then rounded to an f32.
            return to_f32(float_op(op, a, b, span))  # type: ignore[arg-type]
        case float(), float():
            return float_op(op, a, b, span)
        case (str(), str()) | (list(), list()) if op == "+":
            return a + b  # type: ignore[operator]  # mypy can't pair the cases up
    raise EllaError(f"'{op}' doesn't work on {type_name(a)}", span)


def check_i64(n: int, expr: str, span: Span) -> int:
    """i64 arithmetic traps on overflow: a result that doesn't fit is an error.

    The message shows the operation, like `9223372036854775807 + 1`, rather
    than the result, which can be huge. (Native code reports overflow the
    same way, and its operands always fit in 64 bits.)
    """
    if not fits_i64(n):
        raise EllaError(f"i64 overflow: {expr} doesn't fit in 64 bits", span)
    return n


def i32_op(op: str, a: int, b: int, span: Span) -> Value:
    """i32 arithmetic: exact, then checked to fit in 32 bits (`/` gives an f64, as for i64)."""
    match op:
        case "/":
            return float_div(float(a), float(b))
        case "//" | "%" if b == 0:
            raise EllaError("integer division by zero", span)
        case "**" if b < 0:
            raise EllaError("an i32 can't be raised to a negative power", span)
        case "**" if abs(a) > 1 and b >= 32:
            raise EllaError(f"i32 overflow: {a} ** {b} doesn't fit in 32 bits", span)
        case "+" | "-" | "*" | "//" | "%" | "**":
            exact = {"+": a + b, "-": a - b, "*": a * b}.get(op) if op in "+-*" else None
            if exact is None:
                exact = a // b if op == "//" else a % b if op == "%" else a**b
            if not fits_i32(exact):
                raise EllaError(f"i32 overflow: {a} {op} {b} doesn't fit in 32 bits", span)
            return I32(exact)
    raise EllaError(f"'{op}' doesn't work on i32", span)


def int_op(op: str, a: int, b: int, span: Span) -> Value:
    """i64 arithmetic. Python computes the exact result, then we check it fits."""
    match op:
        case "+":
            return check_i64(a + b, f"{a} + {b}", span)
        case "-":
            return check_i64(a - b, f"{a} - {b}", span)
        case "*":
            return check_i64(a * b, f"{a} * {b}", span)
        case "/":
            return float_div(float(a), float(b))  # `/` always gives f64
        case "//" | "%":
            if b == 0:
                raise EllaError("integer division by zero", span)
            # Python's // and % already round toward negative infinity, as
            # Ella's do. Only `I64_MIN // -1` can overflow.
            return check_i64(a // b, f"{a} // {b}", span) if op == "//" else a % b
        case "**":
            if b < 0:
                raise EllaError("an i64 can't be raised to a negative power", span)
            # Any base other than -1, 0 or 1 overflows long before an
            # exponent of 64, so don't compute an enormous number first.
            if abs(a) > 1 and b >= 64:
                raise EllaError(f"i64 overflow: {a} ** {b} doesn't fit in 64 bits", span)
            return check_i64(a**b, f"{a} ** {b}", span)
        case "&":
            return a & b
        case "|":
            return a | b
        case "^":
            return a ^ b
        case "<<" | ">>":
            if not 0 <= b < 64:
                raise EllaError(f"shift amount must be between 0 and 63, not {b}", span)
            # `a << b` is `a * 2**b`, so like `*` it traps if the result
            # doesn't fit: shifting a bit into or past the sign bit is an
            # error, not a silent change of sign. `>>` can't overflow;
            # Python's >> on a negative number is an arithmetic shift, as
            # Ella's is.
            return check_i64(a << b, f"{a} << {b}", span) if op == "<<" else a >> b
    raise EllaError(f"'{op}' doesn't work on i64", span)


def float_op(op: str, a: float, b: float, span: Span) -> Value:
    """f64 arithmetic, following IEEE 754: no errors, but infinities and NaNs."""
    match op:
        case "+":
            return a + b
        case "-":
            return a - b
        case "*":
            return a * b
        case "/":
            return float_div(a, b)
        case "**":
            return float_pow(a, b)
    raise EllaError(f"'{op}' doesn't work on f64", span)


def array_op(op: str, a: Value, b: Value, span: Span) -> ArrayValue:
    """`+`, `-`, `*` or `/` on arrays, item by item: two arrays whose shapes broadcast together, or an array and a
    number.

    Shapes *broadcast* (as NumPy's do) if, lined up from their last sizes,
    each two sizes are the same or one of them is 1 (a missing size counts
    as 1): the result has the larger, and an array of size 1 there gives the
    same items all along it. `[2, 3] + [3]` adds the row to each row.

    An i64 overflow is reported for the first item of the result (in
    row-major order) whose operation doesn't fit, with its operands.
    """
    if isinstance(a, ArrayValue) and isinstance(b, ArrayValue):
        if a.shape == b.shape:
            return ArrayValue(a.shape, [binary(op, x, y, span) for x, y in zip(a.items, b.items)], a.item)
        shape = broadcast(a.shape, b.shape)
        if shape is None:
            raise EllaError(
                f"'{op}' needs arrays whose shapes broadcast together, not {shape_text(a.shape)} and "
                f"{shape_text(b.shape)}",
                span,
            )
        if math.prod(max(size, 1) for size in shape) >= 2**48:  # (ella.library.MAX_ITEMS)
            raise EllaError(f"an array of shape {shape_text(shape)} is too big", span)
        left, right = spread(a, shape), spread(b, shape)
        return ArrayValue(shape, [binary(op, a.items[i], b.items[j], span) for i, j in zip(left, right)], a.item)
    if isinstance(a, ArrayValue):
        return ArrayValue(a.shape, [binary(op, x, b, span) for x in a.items], a.item)
    assert isinstance(b, ArrayValue)
    return ArrayValue(b.shape, [binary(op, a, y, span) for y in b.items], b.item)


def broadcast(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...] | None:
    """The shape two shapes broadcast to, or None if they don't."""
    n = max(len(a), len(b))
    shape = []
    for x, y in zip((1,) * (n - len(a)) + a, (1,) * (n - len(b)) + b):
        if x != y and 1 not in (x, y):
            return None
        shape.append(y if x == 1 else x)
    return tuple(shape)


def spread(array: ArrayValue, shape: tuple[int, ...]) -> list[int]:
    """For each item of an array of the broadcast `shape`, in order, which of `array`'s items it is."""
    sizes = (1,) * (len(shape) - len(array.shape)) + array.shape
    offsets = [0]
    for size, wanted in zip(sizes, shape):
        offsets = [offset * size + (i if size != 1 else 0) for offset in offsets for i in range(wanted)]
    return offsets


def shape_text(shape: tuple[int, ...] | list[int]) -> str:
    """An array's shape as error messages show it, like the list `shape(a)` gives: `[2, 3]`."""
    return "[" + ", ".join(map(str, shape)) + "]"


def array_offset(array: ArrayValue, index: Value, span: Span) -> int:
    """Where `array[index]` is in its items: `index` is an i64, or a tuple of one i64 for each dimension.

    `a[i, j]` of an array of shape `[n, m]` is item `i * m + j`.
    """
    indexes = index if isinstance(index, tuple) else (index,)
    check_count(array, indexes, span)
    offset = 0
    for i, size in zip(indexes, array.shape):
        assert isinstance(i, int)
        if not 0 <= i < size:
            shown = to_text(indexes[0] if len(indexes) == 1 else indexes)  # `a[t]` for a 1-tuple t shows its i64
            raise EllaError(f"index {shown} is out of range for an array of shape {shape_text(array.shape)}", span)
        offset = offset * size + i
    return offset


def array_slice(array: ArrayValue, index: Value, span: Span) -> ArrayValue:
    """`a[i, 1..3]`: the items of an array at the given indexes, an i64 or a range for each dimension, as a new
    array. A range keeps its dimension (`stop - start` long), an i64 drops it: `m[0, 1..3]` is two items of row 0.
    The last dimensions can be left out, and are whole: `m[1..3]` is rows 1 and 2.
    """
    given = index if isinstance(index, tuple) else (index,)
    if len(given) > len(array.shape):
        check_count(array, given, span)
    shown = to_text(given[0] if len(given) == 1 else given)
    indexes = given + tuple(range(size) for size in array.shape[len(given) :])
    picks: list[range] = []
    for i, size in zip(indexes, array.shape):
        if isinstance(i, range):
            part = i
        else:
            assert isinstance(i, int)
            part = range(i, i + 1)
        if not 0 <= part.start <= part.stop <= size:
            raise EllaError(f"index {shown} is out of range for an array of shape {shape_text(array.shape)}", span)
        picks.append(part)
    offsets = [0]
    for part, size in zip(picks, array.shape):
        offsets = [offset * size + i for offset in offsets for i in part]
    shape = tuple(len(part) for part, i in zip(picks, indexes) if isinstance(i, range))
    return ArrayValue(shape, [array.items[offset] for offset in offsets], array.item)


def check_count(array: ArrayValue, indexes: tuple[Value, ...], span: Span) -> None:
    if len(indexes) != len(array.shape):
        count = len(array.shape)
        raise EllaError(
            f"an array of shape {shape_text(array.shape)} needs {count} index{'' if count == 1 else 'es'}, "
            f"not {len(indexes)}",
            span,
        )


type Equal = Callable[[VariantValue, VariantValue], bool | None]


def compare(op: str, a: Value, b: Value, span: Span, custom: Equal | None = None) -> bool:
    """One comparison of a chain: `==`, `<`, `in` and so on. `custom` calls `eq` methods (see values.equal)."""
    if op in ("in", "not in"):
        return contains(b, a, span, custom) == (op == "in")
    if type_name(a) != type_name(b):
        raise EllaError(f"'{op}' needs two values of the same type, not {type_name(a)} and {type_name(b)}", span)
    if type_name(a) == "function":
        raise EllaError("functions can't be compared", span)
    if op == "==":
        return equal(a, b, custom)
    if op == "!=":
        return not equal(a, b, custom)
    # Ordering works on numbers and strings (strings compare by code point).
    match a, b:
        case bool(), bool():
            pass
        case (int(), int()) | (float(), float()) | (str(), str()):
            match op:
                case "<":
                    return a < b  # type: ignore[operator]  # mypy can't pair the cases up
                case "<=":
                    return a <= b  # type: ignore[operator]
                case ">":
                    return a > b  # type: ignore[operator]
                case ">=":
                    return a >= b  # type: ignore[operator]
    raise EllaError(f"'{op}' doesn't work on {type_name(a)}", span)


def contains(container: Value, item: Value, span: Span, custom: Equal | None = None) -> bool:
    """`item in container` for a list, a string (substring) or a range."""
    match container:
        case list():
            return any(equal(element, item, custom) for element in container)
        case str() if isinstance(item, str):
            return item in container
        case range() if isinstance(item, int) and not isinstance(item, bool):
            return item in container
        case str() | range():
            raise EllaError(f"'in' can't look for a {type_name(item)} in a {type_name(container)}", span)
    raise EllaError(f"'in' needs a list, str or range on the right, not {type_name(container)}", span)


def index_into(container: Value, index: Value, span: Span) -> Value:
    """`container[index]` for a list, or a string (giving a one-character string), or a slice of either."""
    if isinstance(index, range) and isinstance(container, list | str):
        return slice_of(container, index, span)
    if isinstance(container, ArrayValue):
        parts = index if isinstance(index, tuple) else (index,)
        if any(isinstance(part, range) for part in parts):
            return array_slice(container, index, span)
        return container.items[array_offset(container, index, span)]
    if not isinstance(index, int) or isinstance(index, bool):
        raise EllaError(f"an index must be an i64, not {type_name(index)}", span)
    if not isinstance(container, list | str):
        raise EllaError(f"can't index a {type_name(container)}", span)
    # No Python-style negative indexes: -1 is an error, not the last element.
    if not 0 <= index < len(container):
        raise EllaError(f"index {index} is out of range for a {type_name(container)} of length {len(container)}", span)
    return container[index]


def slice_of(container: list[Value] | str, part: range, span: Span) -> list[Value] | str:
    """`container[a..b]`: a new list or string of the items from a up to b, which must be in order and in range."""
    if not 0 <= part.start <= part.stop <= len(container):
        raise EllaError(
            f"slice {to_text(part)} is out of range for a {type_name(container)} of length {len(container)}", span
        )
    return container[part.start : part.stop]
