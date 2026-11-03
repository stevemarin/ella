"""Ella's operators on run-time values, shared by every interpreter.

The AST interpreter and the IR interpreter both call these functions, so that
`7 // -2`, an out-of-range index or an i64 overflow behave (and fail, with the
same message) identically however a program is run.

Each function takes the operator's source span, used for the error message
if the operation fails.
"""

import math

from ella.lexer import EllaError, Span
from ella.values import Value, equal, fits_i64, to_text, type_name


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
        case "-", int():
            return check_i64(-value, f"-({value})", span)
        case "-", float():
            return -value
        case "~", int():
            return ~value
    raise EllaError(f"'{op}' doesn't work on {type_name(value)}", span)


def binary(op: str, a: Value, b: Value, span: Span) -> Value:
    """Apply an arithmetic, bitwise or range operator to two values."""
    # There are no implicit conversions, so both sides must have the same type.
    if type_name(a) != type_name(b):
        raise EllaError(f"'{op}' needs two values of the same type, not {type_name(a)} and {type_name(b)}", span)
    match a, b:
        case bool(), bool():
            pass
        case int(), int() if op in ("..", "..="):
            return range(a, b + 1 if op == "..=" else b)
        case int(), int():
            return int_op(op, a, b, span)
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


def compare(op: str, a: Value, b: Value, span: Span) -> bool:
    """One comparison of a chain: `==`, `<`, `in` and so on."""
    if op in ("in", "not in"):
        return contains(b, a, span) == (op == "in")
    if type_name(a) != type_name(b):
        raise EllaError(f"'{op}' needs two values of the same type, not {type_name(a)} and {type_name(b)}", span)
    if type_name(a) == "function":
        raise EllaError("functions can't be compared", span)
    if op == "==":
        return equal(a, b)
    if op == "!=":
        return not equal(a, b)
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


def contains(container: Value, item: Value, span: Span) -> bool:
    """`item in container` for a list, a string (substring) or a range."""
    match container:
        case list():
            return any(equal(element, item) for element in container)
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
