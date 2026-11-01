"""Tokenizer: turns Ella source text into a flat list of tokens.

Besides ordinary tokens, the tokenizer produces the layout tokens NEWLINE,
INDENT and DEDENT from line breaks and indentation, and splits string literals
into pieces so the parser can handle interpolated expressions. See
docs/grammar.md §1 (lexical structure) and §2 (layout).
"""

from dataclasses import dataclass
from enum import Enum, auto
from string import ascii_letters


@dataclass(frozen=True, slots=True)
class Span:
    """A position in the source: 1-based line and column."""

    line: int
    column: int

    def __str__(self) -> str:
        return f"{self.line}:{self.column}"


class EllaError(Exception):
    """The one error type for problems in Ella source, with where they were found."""

    def __init__(self, message: str, span: Span) -> None:
        super().__init__(f"{span}: {message}")
        self.message = message
        self.span = span


class TokenKind(Enum):
    NAME = auto()  # identifiers, including type names and `_`
    KEYWORD = auto()  # `let`, `if`, `true`, ...
    OP = auto()  # operators and punctuation: `+`, `//=`, `(`, ...
    INT = auto()
    FLOAT = auto()
    LABEL = auto()  # `'outer`
    STRING_START = auto()  # opening `"` or `"""`
    STRING_TEXT = auto()  # literal text inside a string
    INTERP_START = auto()  # `{{` inside a string
    INTERP_END = auto()  # `}}` closing an interpolation
    STRING_END = auto()  # closing `"` or `"""`
    NEWLINE = auto()  # end of a logical line
    INDENT = auto()
    DEDENT = auto()
    EOF = auto()


# The meaning of a token's text, where it has one: the number for INT and FLOAT,
# the text with escapes decoded for STRING_TEXT, the name for LABEL.
type TokenValue = int | float | str | None


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str  # the exact source text ("" for INDENT, DEDENT and EOF)
    span: Span
    value: TokenValue = None


KEYWORDS = frozenset({
    "and", "assert", "break", "case", "comptime", "continue", "else", "enum", "false", "for", "if", "import", "in",
    "let", "loop", "match", "mut", "not", "or", "return", "struct", "then", "true", "type", "where", "while",
})  # fmt: skip

# Sorted longest first, so that `//=` is matched before `//` and `/`.
OPERATORS = sorted(
    [
        "+", "-", "*", "/", "//", "%", "**", "&", "|", "^", "~", "<<", ">>",
        "==", "!=", "<", "<=", ">", ">=", "=", "+=", "-=", "*=", "/=", "//=", "%=",
        "->", "=>", "|>", "..", "..=", "?", ".", ",", ":", "(", ")", "[", "]",
    ],
    key=len,
    reverse=True,
)  # fmt: skip

CLOSING_BRACKETS = {")": "(", "]": "["}

# Sets rather than strings, so that the empty string returned by `peek` at the
# end of the source is never "in" them.
DIGITS = frozenset("0123456789")
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
NAME_START = frozenset(ascii_letters + "_")
NAME_CHARS = NAME_START | DIGITS

ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t", "0": "\0", "{": "{"}


def tokenize(source: str) -> list[Token]:
    """Split `source` into tokens, ending with EOF. Raises EllaError on bad input."""
    return Lexer(source).run()


class Lexer:
    def __init__(self, source: str) -> None:
        self.source = source.replace("\r\n", "\n")
        self.pos = 0  # index of the next character
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []
        self.indents = [0]  # stack of indentation widths
        self.brackets: list[Token] = []  # currently open `(` and `[` tokens
        self.at_line_start = True

    # Reading characters

    def peek(self, offset: int = 0) -> str:
        """The character `offset` places ahead, or "" past the end of the source."""
        index = self.pos + offset
        return self.source[index] if index < len(self.source) else ""

    def at(self, text: str) -> bool:
        return self.source.startswith(text, self.pos)

    def advance(self, count: int = 1) -> str:
        """Consume `count` characters, keeping line and column up to date."""
        text = self.source[self.pos : self.pos + count]
        for char in text:
            if char == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
        self.pos += len(text)
        return text

    def span(self) -> Span:
        return Span(self.line, self.column)

    def add(self, kind: TokenKind, start: int, span: Span, value: TokenValue = None) -> Token:
        """Add a token whose text runs from `start` to the current position."""
        token = Token(kind, self.source[start : self.pos], span, value)
        self.tokens.append(token)
        return token

    # Layout

    def run(self) -> list[Token]:
        while self.pos < len(self.source):
            if self.at_line_start:
                self.scan_indentation()
                continue
            char = self.peek()
            if char == " ":
                self.advance()
            elif char == "#":
                self.skip_comment()
            elif char == "\n":
                start, span = self.pos, self.span()
                self.advance()
                # Inside brackets, line breaks are just whitespace.
                if not self.brackets:
                    self.add(TokenKind.NEWLINE, start, span)
                    self.at_line_start = True
            else:
                self.scan_token()
        return self.finish()

    def scan_indentation(self) -> None:
        """Measure a new line's indentation and emit INDENT or DEDENTs for it.

        Blank and comment-only lines are skipped without affecting indentation.
        """
        while self.peek() == " ":
            self.advance()
        if self.peek() in ("", "\n", "#"):
            self.skip_comment()
            self.advance()  # the newline, if any
            return

        self.at_line_start = False
        width = self.column - 1
        span = self.span()
        if width > self.indents[-1]:
            self.indents.append(width)
            self.add(TokenKind.INDENT, self.pos, span)
            return
        while width < self.indents[-1]:
            self.indents.pop()
            self.add(TokenKind.DEDENT, self.pos, span)
        if width != self.indents[-1]:
            raise EllaError("this line's indentation doesn't match any enclosing block", span)

    def skip_comment(self) -> None:
        """Skip to the end of the line (but not past the newline)."""
        while self.peek() not in ("", "\n"):
            self.advance()

    def finish(self) -> list[Token]:
        """End the last line, close any open blocks, and add EOF."""
        if self.brackets:
            bracket = self.brackets[-1]
            raise EllaError(f"'{bracket.text}' is never closed", bracket.span)
        span = self.span()
        if self.tokens and self.tokens[-1].kind is not TokenKind.NEWLINE:
            self.add(TokenKind.NEWLINE, self.pos, span)
        for _ in self.indents[1:]:
            self.add(TokenKind.DEDENT, self.pos, span)
        self.add(TokenKind.EOF, self.pos, span)
        return self.tokens

    # Tokens

    def scan_token(self) -> None:
        """Scan one token starting at the current character."""
        char = self.peek()
        if char == '"':
            self.scan_string()
        elif char in DIGITS:
            self.scan_number()
        elif char in NAME_START:
            self.scan_name()
        elif char == "'":
            self.scan_label()
        else:
            self.scan_operator()

    def scan_name(self) -> None:
        start, span = self.pos, self.span()
        while self.peek() in NAME_CHARS:
            self.advance()
        kind = TokenKind.KEYWORD if self.source[start : self.pos] in KEYWORDS else TokenKind.NAME
        self.add(kind, start, span)

    def scan_label(self) -> None:
        start, span = self.pos, self.span()
        self.advance()  # the '
        if self.peek() not in NAME_START:
            raise EllaError("expected a label name after \"'\", like 'outer", span)
        while self.peek() in NAME_CHARS:
            self.advance()
        self.add(TokenKind.LABEL, start, span, self.source[start + 1 : self.pos])

    def scan_operator(self) -> None:
        start, span = self.pos, self.span()
        for op in OPERATORS:
            if self.at(op):
                self.advance(len(op))
                self.track_bracket(self.add(TokenKind.OP, start, span))
                return
        char = self.peek()
        if char == "\t":
            raise EllaError("tab characters are not allowed; indent with spaces", span)
        raise EllaError(f"unexpected character {char!r}", span)

    def track_bracket(self, token: Token) -> None:
        """Keep the stack of open brackets up to date, checking that they match."""
        if token.text in ("(", "["):
            self.brackets.append(token)
        elif token.text in CLOSING_BRACKETS:
            if not self.brackets or self.brackets[-1].text != CLOSING_BRACKETS[token.text]:
                raise EllaError(f"unmatched '{token.text}'", token.span)
            self.brackets.pop()

    # Numbers

    def scan_number(self) -> None:
        start, span = self.pos, self.span()
        if self.at("0x") or self.at("0X"):
            self.advance(2)
            self.add(TokenKind.INT, start, span, int(self.scan_digits(HEX_DIGITS), 16))
        else:
            whole = self.scan_digits(DIGITS)
            if len(whole) > 1 and whole[0] == "0":
                raise EllaError("integer literals can't have leading zeros", span)
            # A float needs a digit after the `.`, so `0..n` is `0`, `..`, `n`.
            if self.peek() == "." and self.peek(1) in DIGITS:
                self.advance()
                text = f"{whole}.{self.scan_digits(DIGITS)}"
                if self.peek() in ("e", "E"):
                    text += self.advance()
                    if self.peek() in ("+", "-"):
                        text += self.advance()
                    text += self.scan_digits(DIGITS)
                self.add(TokenKind.FLOAT, start, span, float(text))
            else:
                self.add(TokenKind.INT, start, span, int(whole))
        if self.peek() in NAME_CHARS:
            raise EllaError(f"unexpected {self.peek()!r} in number literal", self.span())

    def scan_digits(self, allowed: frozenset[str]) -> str:
        """Scan digits with optional single `_` separators between them; return the digits."""
        if self.peek() not in allowed:
            raise EllaError("expected a digit", self.span())
        digits = []
        while True:
            if self.peek() in allowed:
                digits.append(self.advance())
            elif self.peek() == "_" and self.peek(1) in allowed:
                self.advance()
            else:
                return "".join(digits)

    # Strings

    def scan_string(self) -> None:
        """Scan a string as STRING_START, then text and interpolations, then STRING_END."""
        start, span = self.pos, self.span()
        quote = '"""' if self.at('"""') else '"'
        multiline = quote == '"""'
        self.advance(len(quote))
        self.add(TokenKind.STRING_START, start, span)

        text: list[str] = []  # decoded characters of the current run of text
        text_start, text_span = self.pos, self.span()
        while not self.at(quote):
            if self.at("{{"):
                if text:
                    self.add(TokenKind.STRING_TEXT, text_start, text_span, "".join(text))
                self.scan_interpolation(multiline)
                text = []
                text_start, text_span = self.pos, self.span()
            elif self.peek() == "":
                raise EllaError("this string is never closed", span)
            elif self.peek() == "\n" and not multiline:
                raise EllaError('line break in a "..." string; use \\n or a """...""" string', self.span())
            elif self.peek() == "\\":
                text.append(self.scan_escape())
            else:
                text.append(self.advance())
        if text:
            self.add(TokenKind.STRING_TEXT, text_start, text_span, "".join(text))

        end_start, end_span = self.pos, self.span()
        self.advance(len(quote))
        self.add(TokenKind.STRING_END, end_start, end_span)

    def scan_escape(self) -> str:
        """Scan a backslash escape and return the character it stands for."""
        span = self.span()
        self.advance()  # the backslash
        char = self.peek()
        if char in ESCAPES:
            self.advance()
            return ESCAPES[char]
        if char == "x":
            self.advance()
            digits = self.peek() + self.peek(1)
            if len(digits) != 2 or not set(digits) <= HEX_DIGITS:
                raise EllaError("expected two hex digits after \\x", span)
            self.advance(2)
            if int(digits, 16) > 0x7F:
                raise EllaError("\\x escapes go up to \\x7F; use \\u{...} beyond that", span)
            return chr(int(digits, 16))
        if char == "u":
            self.advance()
            if self.peek() != "{":
                raise EllaError("expected '{' after \\u, as in \\u{1F600}", span)
            self.advance()
            digits = ""
            while self.peek() in HEX_DIGITS:
                digits += self.advance()
            if self.peek() != "}" or not 1 <= len(digits) <= 6:
                raise EllaError("expected 1 to 6 hex digits and '}' in \\u{...}", span)
            self.advance()
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                raise EllaError(f"\\u{{{digits}}} is not a valid Unicode code point", span)
            return chr(code)
        raise EllaError(f"unknown escape '\\{char}'", span)

    def scan_interpolation(self, multiline: bool) -> None:
        """Scan `{{ expr }}` as INTERP_START, ordinary tokens, then INTERP_END."""
        start, span = self.pos, self.span()
        self.advance(2)
        self.add(TokenKind.INTERP_START, start, span)

        # Brackets inside the interpolation must match among themselves.
        outer_brackets, self.brackets = self.brackets, []
        while not self.at("}}"):
            char = self.peek()
            if char == "":
                raise EllaError("this interpolation is never closed with '}}'", span)
            elif char == " ":
                self.advance()
            elif char == "#":
                self.skip_comment()
            elif char == "\n":
                if not multiline:
                    raise EllaError('line break inside {{ }} in a "..." string', self.span())
                self.advance()
            else:
                self.scan_token()
        if self.tokens[-1].kind is TokenKind.INTERP_START:
            raise EllaError("empty interpolation; put an expression inside {{ }}", span)
        if self.brackets:
            bracket = self.brackets[-1]
            raise EllaError(f"'{bracket.text}' is never closed", bracket.span)
        self.brackets = outer_brackets

        end_start, end_span = self.pos, self.span()
        self.advance(2)
        self.add(TokenKind.INTERP_END, end_start, end_span)
