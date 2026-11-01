"""Tokenizer: turns Ella source text into a flat list of tokens.

The tokenizer (or lexer) is the first stage of the compiler. It reads the
source one character at a time and groups characters into tokens: names,
keywords, numbers, operators and so on. The parser then works on tokens and
never has to think about characters, spaces or comments.

Two parts of Ella need more than grouping characters:

* **Layout.** Indentation defines blocks, as in Python. The tokenizer turns
  line structure into three extra tokens: NEWLINE at the end of each line,
  INDENT when a line is indented deeper than the one before, and DEDENT for
  each level it moves back out. For example:

      if x then          KEYWORD NAME KEYWORD NEWLINE
          print(x)       INDENT NAME OP NAME OP NEWLINE
      done()             DEDENT NAME OP OP NEWLINE

  To the parser, INDENT and DEDENT act like the `{` and `}` of other languages.
  Inside `(` and `[` brackets, line breaks are ignored, which is how a long
  expression continues over several lines.

* **String interpolation.** A string like `"Hi, {{name}}!"` contains an
  expression. Instead of one token, the tokenizer produces pieces, with
  ordinary tokens for the expression in the middle:

      STRING_START  STRING_TEXT("Hi, ")  INTERP_START  NAME(name)  INTERP_END
      STRING_TEXT("!")  STRING_END

See docs/grammar.md §1 (lexical structure) and §2 (layout).
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
    STRING_START = auto()  # opening quote: `"`, `'`, `"""` or `'''`
    STRING_TEXT = auto()  # literal text inside a string
    INTERP_START = auto()  # `{{` inside a string
    INTERP_END = auto()  # `}}` closing an interpolation
    STRING_END = auto()  # closing quote, the same as the opening one
    NEWLINE = auto()  # end of a logical line
    INDENT = auto()  # the start of a more deeply indented block
    DEDENT = auto()  # the end of an indented block
    EOF = auto()  # end of the source; always the last token


# The meaning of a token's text, where it has one: the number for INT and
# FLOAT, and the text with escapes decoded for STRING_TEXT.
type TokenValue = int | float | str | None


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str  # the exact source text ("" for INDENT, DEDENT and EOF)
    span: Span  # where the token starts
    value: TokenValue = None


KEYWORDS = frozenset({
    "and", "assert", "break", "case", "comptime", "continue", "else", "enum", "false", "for", "if", "import", "in",
    "let", "loop", "match", "mut", "not", "or", "return", "struct", "then", "true", "type", "where", "while",
})  # fmt: skip

# Sorted longest first, so that `//=` is matched before `//` and `/`. This is
# the "longest match wins" rule: `a//=b` is `a`, `//=`, `b`, never `/`, `/=`.
OPERATORS = sorted(
    [
        "+", "-", "*", "/", "//", "%", "**", "&", "|", "^", "~", "<<", ">>",
        "==", "!=", "<", "<=", ">", ">=", "=", "+=", "-=", "*=", "/=", "//=", "%=",
        "->", "=>", "|>", "..", "..=", "?", ".", ",", ":", "(", ")", "[", "]",
    ],
    key=len,
    reverse=True,
)  # fmt: skip

# Each closing bracket and the opening bracket it must match.
CLOSING_BRACKETS = {")": "(", "]": "["}

# Character classes. These are sets rather than strings because `peek` returns
# "" at the end of the source, and "" is "in" every string but in no set.
DIGITS = frozenset("0123456789")
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
NAME_START = frozenset(ascii_letters + "_")
NAME_CHARS = NAME_START | DIGITS
QUOTES = frozenset("\"'")

# Simple escapes: the character after the backslash, and what it stands for.
# `\x..` and `\u{...}` are handled separately in `scan_escape`.
ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "r": "\r", "t": "\t", "0": "\0", "{": "{"}


def tokenize(source: str) -> list[Token]:
    """Split `source` into tokens, ending with EOF. Raises EllaError on bad input."""
    return Lexer(source).run()


class Lexer:
    """Scans the source from start to end, appending to `self.tokens` as it goes.

    The `scan_*` methods each start at the first character of something (a
    token, a string, a line) and consume it, adding the tokens they find.
    """

    def __init__(self, source: str) -> None:
        # Windows line endings become plain "\n", so nothing else has to care.
        self.source = source.replace("\r\n", "\n")
        self.pos = 0  # index of the next character to read
        self.line = 1  # line and column of that character, for spans
        self.column = 1
        self.tokens: list[Token] = []
        # The indentation widths of the enclosing blocks, innermost last. The
        # top level has width 0; a line indented deeper than the last entry
        # opens a block (INDENT), and a shallower one closes blocks (DEDENT).
        self.indents = [0]
        # The currently open `(` and `[` tokens, innermost last. While any are
        # open, line breaks and indentation are ignored.
        self.brackets: list[Token] = []
        # True when the next character starts a new line, so its indentation
        # has to be measured before anything else.
        self.at_line_start = True

    # Reading characters

    def peek(self, offset: int = 0) -> str:
        """The character `offset` places ahead, or "" past the end of the source."""
        index = self.pos + offset
        return self.source[index] if index < len(self.source) else ""

    def at(self, text: str) -> bool:
        """Does the source continue with `text`?"""
        return self.source.startswith(text, self.pos)

    def advance(self, count: int = 1) -> str:
        """Consume `count` characters and return them, keeping line and column up to date."""
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
        """The position of the next character."""
        return Span(self.line, self.column)

    def add(self, kind: TokenKind, start: int, span: Span, value: TokenValue = None) -> Token:
        """Add a token whose text runs from index `start` to the current position."""
        token = Token(kind, self.source[start : self.pos], span, value)
        self.tokens.append(token)
        return token

    # Layout

    def run(self) -> list[Token]:
        """The main loop: skip spaces and comments, handle line ends, and scan tokens."""
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
                # Inside brackets, a line break is just whitespace. Otherwise
                # it ends the logical line, and the next line's indentation
                # needs measuring.
                if not self.brackets:
                    self.add(TokenKind.NEWLINE, start, span)
                    self.at_line_start = True
            else:
                self.scan_token()
        return self.finish()

    def scan_indentation(self) -> None:
        """Measure a new line's indentation and emit INDENT or DEDENTs for it.

        Blank and comment-only lines are skipped entirely, so they never open
        or close blocks.
        """
        while self.peek() == " ":
            self.advance()
        if self.peek() in ("", "\n", "#"):
            self.skip_comment()
            self.advance()  # the newline, if there is one
            return

        self.at_line_start = False
        width = self.column - 1  # the number of spaces skipped
        span = self.span()
        if width > self.indents[-1]:
            # Deeper than the enclosing block: a new block starts. Any increase
            # works; 4 spaces is the convention.
            self.indents.append(width)
            self.add(TokenKind.INDENT, self.pos, span)
            return
        # Shallower: close blocks until we're back at a matching width. One
        # DEDENT per block closed, so a line can end several blocks at once.
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
        """At the end of the source: end the last line, close open blocks, add EOF."""
        if self.brackets:
            bracket = self.brackets[-1]
            raise EllaError(f"'{bracket.text}' is never closed", bracket.span)
        span = self.span()
        # A file needn't end with a line break, but the parser wants every line
        # to end with NEWLINE.
        if self.tokens and self.tokens[-1].kind is not TokenKind.NEWLINE:
            self.add(TokenKind.NEWLINE, self.pos, span)
        for _ in self.indents[1:]:
            self.add(TokenKind.DEDENT, self.pos, span)
        self.add(TokenKind.EOF, self.pos, span)
        return self.tokens

    # Tokens

    def scan_token(self) -> None:
        """Scan one token, choosing what kind by its first character."""
        char = self.peek()
        if char in QUOTES:
            self.scan_string()
        elif char in DIGITS:
            self.scan_number()
        elif char in NAME_START:
            self.scan_name()
        else:
            self.scan_operator()

    def scan_name(self) -> None:
        """A name or keyword: a letter or `_`, then letters, digits and `_`."""
        start, span = self.pos, self.span()
        while self.peek() in NAME_CHARS:
            self.advance()
        kind = TokenKind.KEYWORD if self.source[start : self.pos] in KEYWORDS else TokenKind.NAME
        self.add(kind, start, span)

    def scan_operator(self) -> None:
        """An operator or punctuation, or an error if nothing matches."""
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
        """An integer (`42`, `1_000`, `0xFF`) or a float (`2.5`, `1.0e-3`)."""
        start, span = self.pos, self.span()
        if self.at("0x") or self.at("0X"):
            self.advance(2)
            self.add(TokenKind.INT, start, span, int(self.scan_digits(HEX_DIGITS), 16))
        else:
            whole = self.scan_digits(DIGITS)
            # `007` is an error rather than a decimal 7, because in C it would
            # be octal, and silently meaning something else is worse.
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
        # Reject things like `1e3` and `12abc` instead of splitting them into
        # a number and a name, which would give a confusing error later.
        if self.peek() in NAME_CHARS:
            raise EllaError(f"unexpected {self.peek()!r} in number literal", self.span())

    def scan_digits(self, allowed: frozenset[str]) -> str:
        """Scan digits with optional single `_` separators between them; return the digits.

        The `_`s are dropped: `1_000` gives "1000". A `_` must have a digit on
        both sides, so `1_` and `1__0` stop early and are rejected by the caller.
        """
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
        """Scan a string as STRING_START, then text and interpolations, then STRING_END.

        Strings can be quoted four ways, all with the same escapes and
        interpolation: `"..."` and `'...'` stay on one line, while tripled
        quotes (three `"` or three `'`) may span lines. The string ends at the
        same quote it started with, so the other kind of quote can appear
        inside freely: `"it's"` and `'say "hi"'`.
        """
        start, span = self.pos, self.span()
        char = self.peek()
        quote = char * 3 if self.at(char * 3) else char
        multiline = len(quote) == 3
        self.advance(len(quote))
        self.add(TokenKind.STRING_START, start, span)

        # Text is collected in runs. A run ends at an interpolation or at the
        # closing quote, and becomes one STRING_TEXT token (if not empty).
        text: list[str] = []  # decoded characters of the current run
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
                raise EllaError("line break in a single-line string; use \\n or a triple-quoted string", self.span())
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
            # `\x41`: exactly two hex digits, limited to ASCII so that `\xHH`
            # always means one character, never a byte of a UTF-8 sequence.
            self.advance()
            digits = self.peek() + self.peek(1)
            if len(digits) != 2 or not set(digits) <= HEX_DIGITS:
                raise EllaError("expected two hex digits after \\x", span)
            self.advance(2)
            if int(digits, 16) > 0x7F:
                raise EllaError("\\x escapes go up to \\x7F; use \\u{...} beyond that", span)
            return chr(int(digits, 16))
        if char == "u":
            # `\u{1F600}`: a Unicode code point in 1 to 6 hex digits.
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
            # Surrogates (D800 to DFFF) are reserved for UTF-16 and aren't characters.
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                raise EllaError(f"\\u{{{digits}}} is not a valid Unicode code point", span)
            return chr(code)
        raise EllaError(f"unknown escape '\\{char}'", span)

    def scan_interpolation(self, multiline: bool) -> None:
        """Scan `{{ expr }}` as INTERP_START, ordinary tokens, then INTERP_END.

        The expression is scanned with the same `scan_token` as everywhere
        else. Since `}` on its own is never a token, the first `}}` we reach
        must be the end. A nested string such as `{{ f("}}") }}` is scanned by
        a recursive `scan_string`, so a `}}` inside it doesn't count.
        """
        start, span = self.pos, self.span()
        self.advance(2)
        self.add(TokenKind.INTERP_START, start, span)

        # Brackets inside the interpolation must match among themselves, so
        # `f("{{ ) }}")` is an error rather than closing the `(` outside.
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
                # Line breaks are allowed only in a multi-line string, where
                # they are whitespace, as inside brackets.
                if not multiline:
                    raise EllaError("line break inside {{ }} in a single-line string", self.span())
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
