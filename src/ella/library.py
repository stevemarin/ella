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
import sys
from dataclasses import dataclass, field
from typing import BinaryIO, TextIO

from ella.lexer import EllaError, Span
from ella.values import Value, VariantValue, fits_i64, to_text

MAX_CODE_POINT = 0x10FFFF
SURROGATES = range(0xD800, 0xE000)  # code points that aren't characters, and can't be written in UTF-8

# What `parse_i64` and `parse_f64` accept.
I64_TEXT = re.compile(r"[+-]?[0-9]+")
F64_TEXT = re.compile(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf|nan)")
WHITESPACE = " \t\n\r\v\f"  # what `trim` removes
MAX_DIGITS = 100  # how many decimals format_f64 can write


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


def err(message: str) -> Value:
    return VariantValue("Result", "Err", [VariantValue("Error", "Error", [message])])


def quoted(text: str) -> str:
    """A string as it appears inside another value: `"a\\nb"`."""
    return to_text(text, nested=True)


def call(name: str, args: list[Value], span: Span, host: Host) -> Value:
    """Run the library function `name`. The checker has made sure the arguments have its types."""
    match name, args:
        case "to_f64", [int() as n]:
            return float(n)
        case "to_i64", [float() as x]:
            if math.isnan(x) or math.isinf(x) or not fits_i64(math.trunc(x)):
                raise EllaError(f"to_i64() can't convert {to_text(x)} to an i64", span)
            return math.trunc(x)
        case "parse_i64", [str() as text]:
            if not I64_TEXT.fullmatch(text):
                return err(f"{quoted(text)} isn't an i64")
            n = int(text)
            return ok(n) if fits_i64(n) else err(f"{quoted(text)} doesn't fit in an i64")
        case "parse_f64", [str() as text]:
            return ok(float(text)) if F64_TEXT.fullmatch(text) else err(f"{quoted(text)} isn't an f64")
        case "read_line", []:
            line = (host.stdin or sys.stdin.buffer).readline()
            if not line:
                return NONE
            text = decode(line, "read_line(): the input isn't valid UTF-8", span)
            return some(text.removesuffix("\n").removesuffix("\r"))
        case "read_file", [str() as path]:
            return read_file(path)
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
            return write_file(path, text)
        case "exit", [int() as status]:
            if not 0 <= status <= 255:
                raise EllaError(f"exit() needs a status from 0 to 255, not {status}", span)
            raise Exit(status, span)
        case "abs", [int() as n]:
            if not fits_i64(abs(n)):
                raise EllaError(f"i64 overflow: abs({n}) doesn't fit in 64 bits", span)
            return abs(n)
        case "abs", [float() as x]:
            return abs(x)
        case "min", [int() | float() as a, int() | float() as b]:
            return min(a, b)
        case "max", [int() | float() as a, int() | float() as b]:
            return max(a, b)
    raise AssertionError(f"no library function {name} for {args}")


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


def read_file(path: str) -> Value:
    """The whole file, or an error saying why it can't be read (the C library's words, as in C)."""
    if "\0" in path:
        return err(f"can't read {quoted(path)}: {os.strerror(errno.EINVAL)}")  # as open() gives in C
    try:
        with open(path, "rb") as file:
            data = file.read()
    except OSError as error:
        return err(f"can't read {quoted(path)}: {error.strerror}")
    try:
        return ok(data.decode("utf-8"))
    except UnicodeDecodeError:
        return err(f"can't read {quoted(path)}: it isn't valid UTF-8")


def write_file(path: str, text: str) -> Value:
    """Write text to a file in UTF-8, replacing what it held, or give an error saying why it can't."""
    if "\0" in path:
        return err(f"can't write {quoted(path)}: {os.strerror(errno.EINVAL)}")
    try:
        with open(path, "wb") as file:
            file.write(text.encode("utf-8"))
    except OSError as error:
        return err(f"can't write {quoted(path)}: {error.strerror}")
    return ok(())
