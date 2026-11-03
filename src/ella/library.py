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
import sys
from dataclasses import dataclass, field
from typing import BinaryIO

from ella.lexer import EllaError, Span
from ella.values import Value, VariantValue, fits_i64, to_text

# What `parse_i64` and `parse_f64` accept.
I64_TEXT = re.compile(r"[+-]?[0-9]+")
F64_TEXT = re.compile(r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|inf|nan)")
WHITESPACE = " \t\n\r\v\f"  # what `trim` removes


@dataclass(slots=True)
class Host:
    """What a running program can read: its standard input (as bytes; None for this process's) and its arguments."""

    stdin: BinaryIO | None = None
    args: list[str] = field(default_factory=list)


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
    raise AssertionError(f"no library function {name} for {args}")


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
