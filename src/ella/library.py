"""The library: built-in functions with fixed types, such as `split` and `read_line`.

Their types are in ella.checker.LIBRARY. Both interpreters call `call` here,
and native code calls the runtime's `ella_lib_NAME` functions (runtime.c),
which must behave exactly the same, error messages included.

Strings are sequences of code points. Text from outside (standard input,
files) must be valid UTF-8. Whitespace, digits and the grammar of numbers
are ASCII only, so that C and Python agree on them exactly.
"""

import errno
import math
import os
import re
import string
import struct
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import BinaryIO, TextIO

from ella.lexer import EllaError, Span
from ella.operators import binary, shape_text
from ella.syntax import bare_name
from ella.values import (
    F32,
    I32,
    ArrayValue,
    HandleValue,
    RefValue,
    Value,
    VariantValue,
    fits_i32,
    fits_i64,
    make_error,
    to_f32,
    to_text,
    type_name,
)

MAX_CODE_POINT = 0x10FFFF
SURROGATES = range(0xD800, 0xE000)  # code points that aren't characters, and can't be written in UTF-8

# What `parse_i64` and `parse_f64` accept.
I64_TEXT = re.compile(r"[+-]?[0-9]+")
F64_TEXT = re.compile(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf|nan)")
WHITESPACE = " \t\n\r\v\f"  # what `trim` removes
# Library functions that are the C library's (through Python's math module), but for their errors.
F64_FUNCTIONS: dict[str, Callable[[float], float]] = {
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log10": math.log10,
    "log2": math.log2,
}
MAX_DIGITS = 100  # how many decimals format_f64 can write
MAX_DIMS = 32  # how many dimensions an array can have (ella.checker.MAX_DIMS)
MAX_ITEMS = 2**48  # an array's sizes (those that aren't 0) multiply to less than this
LANES = 4  # how many sums total() and dot() keep, one per item of an AVX2 vector (docs/grammar.md §10)


class Exit(Exception):
    """`exit(status)`: the program stops at once. The interpreters catch it at the top."""

    def __init__(self, status: int, span: Span) -> None:
        super().__init__(status)
        self.status = status
        self.span = span


@dataclass(slots=True)
class Host:
    """What a running program can use besides standard output: its standard input (as bytes), its
    arguments, and standard error (for `eprint`). None means this process's.
    """

    stdin: BinaryIO | None = None
    args: list[str] = field(default_factory=list)
    stderr: TextIO | None = None


def some(value: Value) -> Value:
    return VariantValue("Optional", "Some", [value])


NONE: Value = VariantValue("Optional", "None")


def ok(value: Value) -> Value:
    return VariantValue("Result", "Ok", [value])


def err(message: str, span: Span) -> Value:
    """`Err(Error(message))`, made at `span`: where the library function was called."""
    return VariantValue("Result", "Err", [make_error(message, span)])


def quoted(text: str) -> str:
    """A string as it appears inside another value: `"a\\nb"`."""
    return to_text(text, nested=True)


def call(name: str, args: list[Value], span: Span, host: Host) -> Value:
    """Run the library function `name`. The checker has made sure the arguments have its types."""
    match name, args:
        case "to_f64", [int() | float() as n]:
            return float(n)  # (an f32 as the f64 it is)
        case "to_f32", [int() | float() as n]:
            return to_f32(float(n))
        case "to_i64", [int() as n]:
            return int(n)  # (an i32 as the i64 it is)
        case "to_i64", [float() as x]:
            if math.isnan(x) or math.isinf(x) or not fits_i64(math.trunc(x)):
                raise EllaError(f"to_i64() can't convert {to_text(x)} to an i64", span)
            return math.trunc(x)
        case "to_i32", [int() | float() as x]:
            # A float without its fraction (toward zero), as to_i64; either must then fit in 32 bits.
            if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) or not fits_i32(math.trunc(x)):
                raise EllaError(f"to_i32() can't convert {to_text(x)} to an i32", span)
            return I32(math.trunc(x))
        case "parse_i64", [str() as text]:
            if not I64_TEXT.fullmatch(text):
                return err(f"{quoted(text)} isn't an i64", span)
            n = int(text)
            return ok(n) if fits_i64(n) else err(f"{quoted(text)} doesn't fit in an i64", span)
        case "parse_f64", [str() as text]:
            return ok(float(text)) if F64_TEXT.fullmatch(text) else err(f"{quoted(text)} isn't an f64", span)
        case "read_line", []:
            line = (host.stdin or sys.stdin.buffer).readline()
            if not line:
                return NONE
            text = decode(line, "read_line(): the input isn't valid UTF-8", span)
            return some(text.removesuffix("\n").removesuffix("\r"))
        case "read_file", [str() as path]:
            return read_file(path, span)
        case "args", []:
            for arg in host.args:
                decode(arg.encode("utf-8", "surrogateescape"), "args(): an argument isn't valid UTF-8", span)
            return list(host.args)
        case "split", [str() as text, str() as separator]:
            if not separator:
                raise EllaError("split() needs a separator that isn't empty", span)
            return list(text.split(separator))
        case "join", [list() as parts, str() as separator]:
            return separator.join(str(part) for part in parts)
        case "find", [str() as text, str() as part]:
            position = text.find(part)
            return NONE if position < 0 else some(position)
        case "replace", [str() as text, str() as old, str() as new]:
            if not old:
                raise EllaError("replace() needs a string to replace that isn't empty", span)
            return text.replace(old, new)
        case "trim", [str() as text]:
            return text.strip(WHITESPACE)
        case "chars", [str() as text]:
            return list(text)
        case "ord", [str() as text]:
            if len(text) != 1:
                raise EllaError(f"ord() needs one character, not {quoted(text)}", span)
            return ord(text)
        case "chr", [int() as n]:
            if not 0 <= n <= MAX_CODE_POINT or n in SURROGATES:
                raise EllaError(f"chr() can't make a character from {n}", span)
            return chr(n)
        case "sqrt", [float() as x]:
            return math.sqrt(x) if x >= 0 else math.nan  # NaN for a negative number, as in C
        case "floor", [float() as x]:
            return whole(x, math.floor(x)) if math.isfinite(x) else x
        case "ceil", [float() as x]:
            return whole(x, math.ceil(x)) if math.isfinite(x) else x
        case "round", [float() as x]:
            if not math.isfinite(x):
                return x
            truncated = math.trunc(x)
            if abs(x - truncated) >= 0.5:  # exact, since x - truncated is the fraction of x
                truncated += 1 if x > 0 else -1
            return whole(x, truncated)
        case "exp", [float() as x]:
            try:
                return math.exp(x)
            except OverflowError:
                return math.inf
        case "ln", [float() as x]:
            if x == 0:
                return -math.inf
            return math.log(x) if x > 0 else math.nan  # NaN stays NaN, and inf inf
        case "sin" | "cos", [float() as x]:
            if math.isinf(x):
                return math.nan
            return math.sin(x) if name == "sin" else math.cos(x)
        case "tan" | "asin" | "acos" | "atan", [float() as x]:
            try:
                return F64_FUNCTIONS[name](x)
            except ValueError:  # outside its domain (tan(inf), asin(2.0)): NaN, as in C
                return math.nan
        case "atan2", [float() as y, float() as x]:
            return math.atan2(y, x)
        case "log10" | "log2", [float() as x]:
            if x == 0:
                return -math.inf
            return F64_FUNCTIONS[name](x) if x > 0 else math.nan  # NaN stays NaN, and inf inf
        case "upper", [str() as text]:
            return text.translate(UPPER)
        case "lower", [str() as text]:
            return text.translate(LOWER)
        case "starts_with", [str() as text, str() as part]:
            return text.startswith(part)
        case "ends_with", [str() as text, str() as part]:
            return text.endswith(part)
        case "pad_left" | "pad_right", [str() as text, int() as width, str() as fill]:
            if len(fill) != 1:
                raise EllaError(f"{name}() needs one character to fill with, not {quoted(fill)}", span)
            padding = fill * max(width - len(text), 0)
            return padding + text if name == "pad_left" else text + padding
        case "format_f64", [float() as x, int() as digits]:
            if not 0 <= digits <= MAX_DIGITS:
                raise EllaError(f"format_f64() can write from 0 to {MAX_DIGITS} decimals, not {digits}", span)
            return f"{x:.{digits}f}"
        case "env", [str() as variable]:
            if not variable or "=" in variable or "\0" in variable:
                return NONE
            value = os.environb.get(variable.encode("utf-8"))
            if value is None:
                return NONE
            return some(decode(value, f"env(): the value of {variable} isn't valid UTF-8", span))
        case "write_file", [str() as path, str() as text]:
            return write_file(path, text, span)
        case "exit", [int() as status]:
            if not 0 <= status <= 255:
                raise EllaError(f"exit() needs a status from 0 to 255, not {status}", span)
            raise Exit(status, span)
        case "hash", [value]:
            return hash_value(value)
        case "abs", [I32() as n]:
            if not fits_i32(abs(n)):
                raise EllaError(f"i32 overflow: abs({n}) doesn't fit in 32 bits", span)
            return I32(abs(n))
        case "abs", [int() as n]:
            if not fits_i64(abs(n)):
                raise EllaError(f"i64 overflow: abs({n}) doesn't fit in 64 bits", span)
            return abs(n)
        case "abs", [F32() as x]:
            return F32(abs(x))
        case "abs", [float() as x]:
            return abs(x)
        case "min", [int() | float() as a, int() | float() as b]:
            return min(a, b)
        case "max", [int() | float() as a, int() | float() as b]:
            return max(a, b)
        case "array", [list() as items, str() as item]:
            # The interpreters pass the item type, which an empty list can't show.
            return ArrayValue((len(items),), list(items), item)
        case "fill", [list() as shape, int() | float() as x]:
            size = check_shape(shape, span)
            return ArrayValue(tuple(shape), [x] * size, type_name(x))
        case "reshape", [ArrayValue() as a, list() as shape]:
            if check_shape(shape, span) != len(a.items):
                raise EllaError(
                    f"reshape() can't make an array of shape {shape_text(shape)} "
                    f"from one of shape {shape_text(a.shape)}",
                    span,
                )
            return ArrayValue(tuple(shape), a.items.copy(), a.item)
        case "shape", [ArrayValue() as a]:
            return list(a.shape)
        case "flat", [ArrayValue() as a]:
            return list(a.items)
        case "total", [ArrayValue() as a]:
            return lane_sum(a.items, a.zero, span)
        case "dot", [ArrayValue() as a, ArrayValue() as b]:
            if a.shape != b.shape:
                raise EllaError(
                    f"dot() needs arrays of the same shape, not {shape_text(a.shape)} and {shape_text(b.shape)}", span
                )
            return lane_sum([binary("*", x, y, span) for x, y in zip(a.items, b.items)], a.zero, span)
        case "matmul", [ArrayValue() as a, ArrayValue() as b]:
            return matmul(a, b, span)
        case "transpose", [ArrayValue() as a]:
            if len(a.shape) != 2:
                raise EllaError(
                    f"transpose() needs a 2-dimensional array, not one of shape {shape_text(a.shape)}", span
                )
            rows, columns = a.shape
            items = [a.items[i * columns + j] for j in range(columns) for i in range(rows)]
            return ArrayValue((columns, rows), items, a.item)
    raise AssertionError(f"no library function {name} for {args}")


def check_shape(shape: list[int], span: Span) -> int:
    """The number of items an array of this shape has, if it can have that shape."""
    if not shape:
        raise EllaError("an array's shape needs at least one size, not []", span)
    if len(shape) > MAX_DIMS:
        raise EllaError(f"an array has at most {MAX_DIMS} dimensions, not {len(shape)}", span)
    if any(size < 0 for size in shape):
        raise EllaError(f"an array's sizes can't be negative: {shape_text(shape)}", span)
    if math.prod(max(size, 1) for size in shape) >= MAX_ITEMS:
        raise EllaError(f"an array of shape {shape_text(shape)} is too big", span)
    return math.prod(shape)


def lane_sum(items: list[Value], zero: Value, span: Span) -> Value:
    """`total(a)`: the items added up in LANES sums starting at 0, item i into sum i % LANES, which are then added
    as `(s0 + s2) + (s1 + s3)`, as AVX2 code adds them. (`dot` gives it its products.)

    For i64, an overflow is reported at the first addition that doesn't fit.
    """
    sums = [zero] * LANES
    for i, item in enumerate(items):
        sums[i % LANES] = binary("+", sums[i % LANES], item, span)
    return binary("+", binary("+", sums[0], sums[2], span), binary("+", sums[1], sums[3], span), span)


def matmul(a: ArrayValue, b: ArrayValue, span: Span) -> ArrayValue:
    """`matmul(a, b)`: the matrix product. Item (i, j) is the sum of `a[i, k] * b[k, j]`, from k = 0 up, starting at 0."""
    if len(a.shape) != 2 or len(b.shape) != 2:
        raise EllaError(
            f"matmul() needs two 2-dimensional arrays, not shapes {shape_text(a.shape)} and {shape_text(b.shape)}",
            span,
        )
    (n, k), (k2, m) = a.shape, b.shape
    if k != k2:
        raise EllaError(
            f"matmul() can't multiply arrays of shapes {shape_text(a.shape)} and {shape_text(b.shape)}", span
        )
    zero = a.zero
    items: list[Value] = []
    for i in range(n):
        for j in range(m):
            total = zero
            for p in range(k):
                total = binary("+", total, binary("*", a.items[i * k + p], b.items[p * m + j], span), span)
            items.append(total)
    return ArrayValue((n, m), items, a.item)


# hash() is 64-bit FNV-1a, as a signed i64: over a string's code points, and
# over the hashes of the parts of other values, so that equal values (by `==`)
# hash the same. The runtime's ella_lib_hash must match it exactly.
FNV_OFFSET = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3
CANONICAL_NAN = 0x7FF8000000000000


def hash_value(value: Value, custom: Callable[[VariantValue], int | None] | None = None) -> int:
    """`hash(value)`. `custom` gives the hash of a struct or enum value whose type has a `hash` method
    (by calling it), or None."""
    h = hash_bits(value, custom)
    return h - 2**64 if h >= 2**63 else h


def hash_bits(value: Value, custom: Callable[[VariantValue], int | None] | None = None) -> int:
    if isinstance(value, VariantValue) and custom is not None and (h := custom(value)) is not None:
        return h % 2**64
    match value:
        case str():
            return fnv(FNV_OFFSET, (ord(c) for c in value))
        case bool() | int():
            return fnv(FNV_OFFSET, [value % 2**64])
        case float():
            # 0.0 and -0.0 are equal, so they hash the same; NaNs are never equal, but hash alike.
            bits = CANONICAL_NAN if math.isnan(value) else struct.unpack("<Q", struct.pack("<d", value + 0.0))[0]
            return fnv(FNV_OFFSET, [bits])
        case tuple() | list():  # () too
            return fnv(FNV_OFFSET, (hash_bits(item, custom) for item in value))
        case ArrayValue(shape, items):  # as the tuple of its shape and its items, (shape(a), flat(a))
            return fnv(FNV_OFFSET, [hash_bits(list(shape)), hash_bits(items)])
        case range():
            # Ranges are equal when they hold the same numbers.
            size = max(0, value.stop - value.start) % 2**64
            return fnv(FNV_OFFSET, [size, value.start % 2**64] if size else [size])
        case VariantValue(variant=variant, fields=fields, error=error):
            # Its variant's name, without its module or type arguments; an Error without where it was made.
            name = bare_name(variant).partition("[")[0]
            h = fnv(FNV_OFFSET, name.encode())
            return fnv(h, (hash_bits(field, custom) for field in (fields[:2] if error else fields)))
        case HandleValue(slot, generation):
            return fnv(FNV_OFFSET, [slot | generation << 32])
        case RefValue(index=index):
            return fnv(FNV_OFFSET, [index])
    raise AssertionError(f"hash() of {value!r}")


def fnv(h: int, words: Iterable[int]) -> int:
    for word in words:
        h = ((h ^ word) * FNV_PRIME) % 2**64
    return h


# upper and lower change ASCII letters only, as the runtime does.
UPPER = str.maketrans(string.ascii_lowercase, string.ascii_uppercase)
LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def whole(x: float, n: int) -> float:
    """A whole number that x was rounded to, as a float: -0.0 if it is zero and x is negative, as in C."""
    return math.copysign(0.0, x) if n == 0 else float(n)


def decode(data: bytes, message: str, span: Span) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise EllaError(message, span) from None


def read_file(path: str, span: Span) -> Value:
    """The whole file, or an error saying why it can't be read (the C library's words, as in C)."""
    if "\0" in path:
        return err(f"can't read {quoted(path)}: {os.strerror(errno.EINVAL)}", span)  # as open() gives in C
    try:
        with open(path, "rb") as file:
            data = file.read()
    except OSError as error:
        return err(f"can't read {quoted(path)}: {error.strerror}", span)
    try:
        return ok(data.decode("utf-8"))
    except UnicodeDecodeError:
        return err(f"can't read {quoted(path)}: it isn't valid UTF-8", span)


def write_file(path: str, text: str, span: Span) -> Value:
    """Write text to a file in UTF-8, replacing what it held, or give an error saying why it can't."""
    if "\0" in path:
        return err(f"can't write {quoted(path)}: {os.strerror(errno.EINVAL)}", span)
    try:
        with open(path, "wb") as file:
            file.write(text.encode("utf-8"))
    except OSError as error:
        return err(f"can't write {quoted(path)}: {error.strerror}", span)
    return ok(())
