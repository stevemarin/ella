"""Pratt parser: turns tokens into the syntax tree defined in ella.syntax.

This follows docs/grammar.md: operator precedence from §8, and the grammar
summary and parsing notes from §12.

Most of the parser is "recursive descent": one method per kind of thing
(`parse_if`, `parse_let`, `parse_type`, ...), each consuming its tokens and
calling the others for the parts inside it.

Expressions with operators use Pratt parsing (also called top-down operator
precedence). `parse_expr(min_prec)` works in two steps:

1. Parse one operand with `parse_prefix`: a literal, a name, `(...)`, a prefix
   operator such as `-x`, or a keyword form such as `if ...`.
2. Loop: while the next token is an infix or postfix operator whose
   precedence is at least `min_prec`, consume it and combine it with the
   operand so far. For a binary operator, the right operand is parsed by a
   recursive `parse_expr` with a higher `min_prec`, so it only takes
   operators that bind tighter.

For example, `1 + 2 * 3 - 4`: we parse `1` and see `+` (13). The right operand
is `parse_expr(14)`, which reads `2`, sees `*` (14, allowed), and reads `3`.
It then sees `-` (13, below 14) and stops, giving `2 * 3`. Back at the top,
`-` (13) is allowed, so the result is `(1 + (2 * 3)) - 4`.

Precedence lives in the `self.infix` and `self.prefix` tables.

The first error normally stops the parser. For editors, `parse_partial`
keeps going: a line of the program or of a block with an error becomes `()`,
and parsing goes on with the next line of the same block (`parse_line`).
"""

from collections.abc import Callable
from enum import Enum, auto

from ella.lexer import EllaError, Span, Token, TokenKind, tokenize
from ella.syntax import (
    Assert,
    Assign,
    Binary,
    Binding,
    Block,
    BoolLit,
    Break,
    Call,
    Case,
    Compare,
    ConstraintDef,
    Continue,
    Dim,
    EnumDef,
    Expr,
    FieldSplice,
    FloatLit,
    For,
    FunctionType,
    If,
    Import,
    Index,
    IntLit,
    Lambda,
    Let,
    LetPattern,
    ListLit,
    ListType,
    LiteralPattern,
    LiteralValue,
    Loop,
    MapLit,
    Match,
    Member,
    Method,
    MutArg,
    Name,
    NamedType,
    NamePattern,
    OrPattern,
    Param,
    Pattern,
    Program,
    Quote,
    RangePattern,
    Return,
    SetLit,
    Splice,
    StringLit,
    Try,
    TupleLit,
    TuplePattern,
    TupleType,
    TypeExpr,
    TypeLit,
    TypeSplice,
    Unary,
    UnitLit,
    UnitType,
    Variant,
    VariantPattern,
    While,
    WildcardPattern,
    Yield,
)

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1

LOWEST = 0  # a precedence that accepts every operator

ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "//=", "%="})

LOOP_KEYWORDS = ("for", "while", "loop")

# Keywords that can begin an expression. `break` and `return` use this to
# decide whether a value follows them.
EXPR_KEYWORDS = frozenset({
    "assert", "break", "constraint", "continue", "enum", "false", "for", "if", "let", "loop", "match", "mut",
    "nocopy", "not", "quote", "return", "struct", "true", "while", "yield",
})  # fmt: skip


class Assoc(Enum):
    """How an operator combines with itself, as in `a op b op c`."""

    LEFT = auto()  # `a - b - c` is `(a - b) - c`
    RIGHT = auto()  # `a ** b ** c` is `a ** (b ** c)`
    NONE = auto()  # not allowed: `a..b..c` is an error
    CHAIN = auto()  # comparisons: `a < b <= c` becomes one Compare node
    POSTFIX = auto()  # call, index, member and `?`, which have no right operand


def parse(source: str, file: str = "") -> Program:
    """Tokenize and parse a source file, whose spans have the file `file` (see Span). Raises EllaError on bad input."""
    return Parser(tokenize(source, file), source).parse_program()


def parse_partial(tokens: list[Token], source: str) -> tuple[Program, list[EllaError]]:
    """Parse as much of a program as possible, for editors: the program, with `()` for each line
    that has an error, and the errors."""
    parser = Parser(tokens, source, recover=True)
    program = parser.parse_program()
    assert parser.errors is not None
    return program, parser.errors


def block_depths(tokens: list[Token]) -> list[int]:
    """How many indented blocks each token is inside (a block's INDENT isn't, its DEDENT is)."""
    depths = []
    depth = 0
    for token in tokens:
        depths.append(depth)
        if token.kind is TokenKind.INDENT:
            depth += 1
        elif token.kind is TokenKind.DEDENT:
            depth -= 1
    return depths


def describe(token: Token) -> str:
    """How a token is named in error messages, as in "expected X, found <this>"."""
    match token.kind:
        case TokenKind.NEWLINE:
            return "end of line"
        case TokenKind.INDENT:
            return "an indented line"
        case TokenKind.DEDENT:
            return "end of block"
        case TokenKind.EOF:
            return "end of file"
        case TokenKind.STRING_START:
            return "a string"
        case _:
            return f"'{token.text}'"


class Parser:
    """Reads a list of tokens from front to back, building syntax tree nodes.

    The parser only ever looks a few tokens ahead (`peek`) and never backs up.
    """

    def __init__(self, tokens: list[Token], source: str, recover: bool = False) -> None:
        self.tokens = tokens
        self.pos = 0  # index of the current (next unconsumed) token
        # With `recover`, errors in lines are collected here rather than raised (see `parse_line`).
        self.errors: list[EllaError] | None = [] if recover else None
        self.depths = block_depths(tokens) if recover else []
        # The source text, used to keep the text of `assert` and `where`
        # conditions for error messages. Line endings are normalized the same
        # way as in the tokenizer, so that spans line up.
        self.source = source.replace("\r\n", "\n")
        # The index in `source` where each line starts, to turn spans into indexes.
        self.line_starts = [0] + [i + 1 for i, char in enumerate(self.source) if char == "\n"]
        # Inside a `where` block, `return` names the function's result rather
        # than exiting the function.
        self.in_where = False
        # Labels of the loops we are currently inside, innermost last. After
        # `break` or `continue`, a name in this list is a label rather than a
        # value. Each lambda body starts with an empty list, because `break`
        # can't jump out of a function.
        self.labels: list[str] = []

        # Infix and postfix operators: precedence (higher binds tighter) and
        # associativity. The numbers are the levels in docs/grammar.md §8; the
        # gaps (5 and 15) are the prefix operators below.
        self.infix: dict[str, tuple[int, Assoc]] = {
            **{op: (1, Assoc.RIGHT) for op in ASSIGN_OPS},
            "=>": (2, Assoc.RIGHT),
            "or": (3, Assoc.LEFT),
            "and": (4, Assoc.LEFT),
            **{op: (6, Assoc.CHAIN) for op in ("==", "!=", "<", "<=", ">", ">=", "in", "not in")},
            "|>": (7, Assoc.LEFT),
            "..": (8, Assoc.NONE),
            "..=": (8, Assoc.NONE),
            "|": (9, Assoc.LEFT),
            "^": (10, Assoc.LEFT),
            "&": (11, Assoc.LEFT),
            "<<": (12, Assoc.LEFT),
            ">>": (12, Assoc.LEFT),
            "+": (13, Assoc.LEFT),
            "-": (13, Assoc.LEFT),
            "*": (14, Assoc.LEFT),
            "/": (14, Assoc.LEFT),
            "//": (14, Assoc.LEFT),
            "%": (14, Assoc.LEFT),
            "**": (16, Assoc.RIGHT),
            **{op: (17, Assoc.POSTFIX) for op in ("(", "[", ".", "?")},
        }
        # Prefix operators and their precedence. The operand is parsed one
        # level up, so it only takes operators that bind tighter: `not a == b`
        # is `not (a == b)`, and `-a * b` is `(-a) * b`.
        self.prefix: dict[str, int] = {"not": 5, "-": 15, "~": 15}

    # Looking at tokens

    def peek(self, offset: int = 0) -> Token:
        """The token `offset` places ahead. Past the end, that's always EOF."""
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def previous(self) -> Token:
        """The token consumed most recently."""
        return self.tokens[self.pos - 1]

    def at(self, text: str, offset: int = 0) -> bool:
        """Is the token at `offset` the keyword or operator `text`?"""
        token = self.peek(offset)
        return token.kind in (TokenKind.KEYWORD, TokenKind.OP) and token.text == text

    def at_kind(self, kind: TokenKind, offset: int = 0) -> bool:
        return self.peek(offset).kind is kind

    def at_block(self) -> bool:
        """Does an indented block start here?"""
        return self.at_kind(TokenKind.NEWLINE) and self.at_kind(TokenKind.INDENT, 1)

    def at_label(self) -> bool:
        """Is this a loop label, as in `outer: for ...`?"""
        return self.at_kind(TokenKind.NAME) and self.at(":", 1) and any(self.at(kw, 2) for kw in LOOP_KEYWORDS)

    def advance(self) -> Token:
        """Consume the current token and return it. EOF is never consumed."""
        token = self.peek()
        if token.kind is not TokenKind.EOF:
            self.pos += 1
        return token

    def accept(self, text: str) -> bool:
        """Consume the keyword or operator `text` if it is next."""
        if self.at(text):
            self.advance()
            return True
        return False

    def expect(self, text: str) -> Token:
        """Consume the keyword or operator `text`, which must be next."""
        if not self.at(text):
            raise self.unexpected(f"'{text}'")
        return self.advance()

    def expect_kind(self, kind: TokenKind, expected: str) -> Token:
        """Consume a token of `kind`, which must be next. `expected` describes it for errors."""
        if not self.at_kind(kind):
            raise self.unexpected(expected)
        return self.advance()

    def source_since(self, first: Token) -> str:
        """The source text from token `first` through the last token consumed."""
        last = self.previous()
        start = self.line_starts[first.span.line - 1] + first.span.column - 1
        end = self.line_starts[last.span.line - 1] + last.span.column - 1 + len(last.text)
        return self.source[start:end].strip()

    def unexpected(self, expected: str) -> EllaError:
        """An error for the current token, saying what should have been there."""
        token = self.peek()
        return EllaError(f"expected {expected}, found {describe(token)}", token.span)

    def parse_comma_list[T](self, parse_item: Callable[[], T], close: str) -> tuple[list[T], bool]:
        """Parse `item, item, ...` and the closing bracket, allowing a trailing comma.

        Used for argument lists, list literals, tuples, parameters, type
        arguments and so on, with `parse_item` parsing one item. Also returns
        whether there was a trailing comma, which tells `(x,)` (a tuple) apart
        from `(x)` (grouping).
        """
        items: list[T] = []
        trailing_comma = False
        while not self.accept(close):
            items.append(parse_item())
            trailing_comma = self.accept(",")
            if not trailing_comma:
                self.expect(close)
                break
        return items, trailing_comma

    def parse_braces(self) -> Expr:
        """`{k: v, ...}` (a Map), `{x, ...}` (a Set), `{}` (an empty Map), or `{/}` (an empty Set)."""
        span = self.expect("{").span
        if self.accept("}"):
            return MapLit((), span)
        if self.at("/") and self.peek(1).text == "}":
            self.advance()
            self.advance()
            return SetLit((), span)
        first = self.parse_expr()
        if self.accept(":"):
            entries = [(first, self.parse_expr())]
            if self.accept(","):
                entries += self.parse_comma_list(self.parse_entry, "}")[0]
            else:
                self.expect("}")
            return MapLit(tuple(entries), span)
        items = [first]
        if self.accept(","):
            items += self.parse_comma_list(self.parse_expr, "}")[0]
        else:
            self.expect("}")
        return SetLit(tuple(items), span)

    def parse_entry(self) -> tuple[Expr, Expr]:
        """`key: value` in a map literal."""
        key = self.parse_expr()
        self.expect(":")
        return key, self.parse_expr()

    # Program structure and blocks

    def parse_program(self) -> Program:
        """A file is its `import` lines, then a sequence of expressions, one per line."""
        imports = []
        while self.at("import"):
            start = self.pos
            try:
                imports.append(self.parse_import())
            except EllaError as error:
                if self.errors is None:
                    raise
                self.errors.append(error)
                self.skip_line(start)
        body = []
        while not self.at_kind(TokenKind.EOF):
            body.append(self.parse_line())
        return Program(tuple(body), Span(1, 1, self.tokens[0].span.file), tuple(imports))

    def parse_import(self) -> Import:
        """`import geo.poly`, or `import geo.poly as p`."""
        span = self.expect("import").span
        path = [self.expect_kind(TokenKind.NAME, "a module name").text]
        while self.accept("."):
            path.append(self.expect_kind(TokenKind.NAME, "a name after '.'").text)
        alias = self.expect_kind(TokenKind.NAME, "a name after 'as'").text if self.accept("as") else None
        self.end_of_line()
        return Import(tuple(path), alias, span)

    def parse_dotted(self, first: Token) -> str:
        """A name that may be a path into a module, after its first part: `geo.Point` in a type or pattern."""
        name = first.text
        while self.at(".") and self.at_kind(TokenKind.NAME, 1):
            self.advance()
            name += "." + self.advance().text
        return name

    def parse_line(self) -> Expr:
        """A line of the program or of a block, through its end.

        When recovering from errors, a line with an error is recorded and
        becomes `()`, and parsing goes on at the next line of the same block,
        or at the block's end.
        """
        if self.errors is None:
            expr = self.parse_expr()
            self.end_of_line()
            return expr
        start, labels, in_where = self.pos, list(self.labels), self.in_where
        try:
            expr = self.parse_expr()
            self.end_of_line()
            return expr
        except EllaError as error:
            self.errors.append(error)
            self.labels, self.in_where = labels, in_where
            self.skip_line(start)
            return UnitLit(self.tokens[start].span)

    def skip_line(self, start: int) -> None:
        """Skip the rest of the line that starts at token `start`, and the blocks inside it."""
        depth = self.depths[start]
        self.pos = max(self.pos, start + 1)
        while not self.at_kind(TokenKind.EOF) and self.depths[self.pos] >= depth:
            token = self.peek()
            if self.depths[self.pos] == depth:
                if token.kind is TokenKind.DEDENT:
                    return  # the end of the block
                after_line = self.previous().kind in (TokenKind.NEWLINE, TokenKind.DEDENT)
                if after_line and token.kind is not TokenKind.INDENT:
                    return  # the next line
            self.advance()

    def end_of_line(self) -> None:
        """Consume the NEWLINE that ends a line.

        An expression that ended with its own indented block, like an `if`
        with a block body, has already consumed that NEWLINE: the tokens are
        `... NEWLINE DEDENT`, and the block took both.
        """
        if self.previous().kind is not TokenKind.DEDENT:
            self.expect_kind(TokenKind.NEWLINE, "end of line")

    def parse_block(self) -> Block:
        """An indented block: NEWLINE INDENT expr { NEWLINE expr } NEWLINE DEDENT."""
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        indent = self.expect_kind(TokenKind.INDENT, "an indented block")
        return Block(self.parse_lines(), indent.span)

    def parse_lines(self) -> tuple[Expr, ...]:
        """The lines of a block, after its INDENT, through its DEDENT."""
        exprs = []
        while True:
            exprs.append(self.parse_line())
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                return tuple(exprs)
            if self.at_kind(TokenKind.EOF):  # only after an error, when recovering
                return tuple(exprs)

    def parse_body(self) -> Expr:
        """A body (after `then`, `else`, a loop header, ...) is an expression on
        the same line, or an indented block starting on the next line."""
        return self.parse_block() if self.at_block() else self.parse_inline_body()

    def parse_inline_body(self) -> Expr:
        """A body on the same line. An assignment there has the value (), as
        it would as a block's last line, so it is made a block of one line."""
        expr = self.parse_expr()
        return Block((expr,), expr.span) if isinstance(expr, Assign) else expr

    # Expressions

    def parse_expr(self, min_prec: int = LOWEST) -> Expr:
        """Parse an expression, taking only operators with precedence >= `min_prec`.

        This is the heart of the Pratt parser; see the module docstring.
        """
        left = self.parse_prefix()
        while (op := self.infix_op()) is not None and self.infix[op][0] >= min_prec:
            left = self.parse_infix(left, op)
        return left

    def infix_op(self) -> str | None:
        """The infix or postfix operator at the current token, if there is one."""
        # An expression that ended with an indented block ends there. The next
        # line can't continue it with an operator.
        if self.previous().kind is TokenKind.DEDENT:
            return None
        # `not in` is two tokens but one operator.
        if self.at("not") and self.at("in", 1):
            return "not in"
        token = self.peek()
        if token.kind in (TokenKind.KEYWORD, TokenKind.OP) and token.text in self.infix:
            return token.text
        return None

    def parse_infix(self, left: Expr, op: str) -> Expr:
        """Combine `left` with the operator `op` (not yet consumed) and its right side."""
        prec, assoc = self.infix[op]
        if assoc is Assoc.POSTFIX:
            return self.parse_postfix(left)
        if op == "=>":
            return self.parse_untyped_lambda(left)

        span = self.take_binary_op(op)
        if assoc is Assoc.CHAIN:
            return self.parse_comparison(left, op, span)
        # The right operand may only contain tighter operators (prec + 1), so
        # `a - b - c` groups to the left. For a right-associative operator it
        # may also contain the same operator again (prec), so `a ** b ** c` is
        # `a ** (b ** c)`.
        right = self.parse_expr(prec if assoc is Assoc.RIGHT else prec + 1)
        if assoc is Assoc.NONE and (next_op := self.infix_op()) is not None and self.infix[next_op][0] == prec:
            raise EllaError(f"'{next_op}' can't follow '{op}' without parentheses", self.peek().span)
        if op in ASSIGN_OPS:
            if not isinstance(left, Name | Index | Member):
                raise EllaError("can only assign to a name, an index like xs[0], or a field like p.x", span)
            return Assign(op, left, right, span)
        if op == "|>":
            return self.make_pipe(left, right, span)
        return Binary(op, left, right, span)

    def take_binary_op(self, op: str) -> Span:
        """Consume a binary operator and return its span.

        The operator must not end the line. Outside brackets, a line break
        ends the expression, so a dangling operator would be confusing. The
        fix is to wrap the expression in parentheses, inside which line breaks
        are ignored.
        """
        token = self.advance()
        if op == "not in":
            self.advance()
        if self.at_kind(TokenKind.NEWLINE):
            raise EllaError(f"a line can't end with '{op}'; wrap the expression in parentheses", token.span)
        return token.span

    def parse_comparison(self, first: Expr, op: str, span: Span) -> Compare:
        """`a < b <= c` is one node, meaning `a < b and b <= c` with `b` evaluated once."""
        operand_prec = self.infix[op][0] + 1
        rest = [(op, self.parse_expr(operand_prec))]
        while (next_op := self.infix_op()) is not None and self.infix[next_op][1] is Assoc.CHAIN:
            self.take_binary_op(next_op)
            rest.append((next_op, self.parse_expr(operand_prec)))
        return Compare(first, tuple(rest), span)

    def make_pipe(self, left: Expr, right: Expr, span: Span) -> Call:
        """`x |> f` is `f(x)`, and `x |> f(a, b)` is `f(x, a, b)`.

        Pipes are rewritten into ordinary calls here, so later stages never
        see them.
        """
        match right:
            case Name():
                return Call(right, (left,), span)
            case Call(callee=callee, args=args):
                return Call(callee, (left, *args), span)
        raise EllaError("the right side of '|>' must be a name or a call", span)

    def parse_postfix(self, left: Expr) -> Expr:
        """`left(args)`, `left[index]`, `left.name` or `left?`."""
        token = self.advance()
        match token.text:
            case "(":
                args, _ = self.parse_comma_list(self.parse_arg, ")")
                return Call(left, tuple(args), token.span)
            case "[":
                # `a[i, j]` indexes with the tuple `(i, j)`: an array's item, or a key of a type with `get`.
                index = self.parse_expr()
                if self.at(","):
                    indexes = [index]
                    while self.at(","):
                        self.advance()
                        indexes.append(self.parse_expr())
                    index = TupleLit(tuple(indexes), index.span)
                self.expect("]")
                return Index(left, index, token.span)
            case ".":
                if self.at("$"):
                    splice = self.parse_splice()
                    return FieldSplice(left, splice.code, token.span)
                # A tuple's item: `t.0`. The lexer reads `t.0.1` as `t`, `.`, `0.1`.
                items = (
                    self.peek().text.split(".") if self.at_kind(TokenKind.INT) or self.at_kind(TokenKind.FLOAT) else []
                )
                if items and all(item.isdigit() and str(int(item)) == item for item in items):
                    self.advance()
                    for item in items:
                        left = Member(left, item, token.span)
                    return left
                name = self.expect_kind(TokenKind.NAME, "a member name")
                return Member(left, name.text, token.span)
            case _:
                return Try(left, token.span)

    def parse_prefix(self) -> Expr:
        """Parse an expression that doesn't continue an operand: literals, names,
        brackets, prefix operators, and the keyword forms.

        Because everything is an expression, `let`, `if`, loops and so on are
        handled here too, and may appear anywhere an operand can.
        """
        token = self.peek()
        match token.kind, token.text:
            case TokenKind.INT, _:
                self.advance()
                assert isinstance(token.value, int)
                return IntLit(self.check_int(token.value, token.span), token.span)
            case TokenKind.FLOAT, _:
                self.advance()
                assert isinstance(token.value, float)
                return FloatLit(token.value, token.span)
            case TokenKind.NAME, _ if self.at_label():
                return self.parse_labeled_loop()
            case TokenKind.NAME, _:
                self.advance()
                return Name(token.text, token.span)
            case TokenKind.STRING_START, _:
                return self.parse_string()
            case TokenKind.KEYWORD, ("true" | "false"):
                self.advance()
                return BoolLit(token.text == "true", token.span)
            case TokenKind.OP | TokenKind.KEYWORD, ("-" | "~" | "not"):
                return self.parse_unary()
            case TokenKind.OP, "(":
                return self.parse_parens()
            case TokenKind.OP, "[":
                self.advance()
                items, _ = self.parse_comma_list(self.parse_expr, "]")
                return ListLit(tuple(items), token.span)
            case TokenKind.OP, "{":
                return self.parse_braces()
            case TokenKind.KEYWORD, "let":
                return self.parse_let()
            case TokenKind.KEYWORD, "if":
                return self.parse_if()
            case TokenKind.KEYWORD, "match":
                return self.parse_match()
            case TokenKind.KEYWORD, "for":
                return self.parse_for(None)
            case TokenKind.KEYWORD, "while":
                return self.parse_while(None)
            case TokenKind.KEYWORD, "loop":
                return self.parse_loop(None)
            case TokenKind.KEYWORD, "break":
                return self.parse_break()
            case TokenKind.KEYWORD, "continue":
                self.advance()
                return Continue(self.parse_optional_label(), token.span)
            case TokenKind.KEYWORD, "return":
                return self.parse_return()
            case TokenKind.KEYWORD, "yield":
                self.advance()
                return Yield(self.parse_expr(), token.span)
            case TokenKind.KEYWORD, "assert":
                self.advance()
                return self.parse_condition()
            case TokenKind.KEYWORD, "enum":
                return self.parse_enum()
            case TokenKind.KEYWORD, ("struct" | "nocopy"):
                return self.parse_struct()
            case TokenKind.KEYWORD, "constraint":
                return self.parse_constraint()
            case TokenKind.KEYWORD, "mut":
                return self.parse_mut_method_call()
            case TokenKind.KEYWORD, "quote":
                self.advance()
                return Quote(self.parse_body(), token.span)
            case TokenKind.OP, "$":
                return self.parse_splice()
            case TokenKind.KEYWORD, "where":
                raise EllaError("'where' is only allowed as the first clause of a function's block body", token.span)
            case TokenKind.KEYWORD, "type":
                self.advance()
                return TypeLit(self.parse_type(), token.span)
            case TokenKind.KEYWORD, "import":
                raise EllaError("'import' lines must come first in a file, before any other line", token.span)
            case TokenKind.KEYWORD, "comptime":
                raise EllaError(f"'{token.text}' is reserved for later and can't be used yet", token.span)
        raise self.unexpected("an expression")

    def starts_expr(self) -> bool:
        """Can the current token begin an expression?

        `break` and `return` take an optional value, and this decides whether
        one is there: `break n` has one, `break` at the end of a line doesn't.
        """
        token = self.peek()
        match token.kind:
            case TokenKind.INT | TokenKind.FLOAT | TokenKind.NAME | TokenKind.STRING_START:
                return True
            case TokenKind.OP:
                return token.text in ("(", "[", "-", "~", "$")
            case TokenKind.KEYWORD:
                return token.text in EXPR_KEYWORDS
            case _:
                return False

    def check_int(self, value: int, span: Span) -> int:
        """Integer literals must fit in an i64."""
        if not I64_MIN <= value <= I64_MAX:
            raise EllaError("integer literal doesn't fit in i64", span)
        return value

    def parse_arg(self) -> Expr:
        """A call argument: an expression, or `mut target` for a `mut` parameter.

        A method call can't be a `mut` argument, so `f(mut s.pop())` passes
        what `mut s.pop()` gives, as on its own.
        """
        token = self.peek()
        if self.accept("mut"):
            match self.parse_expr():
                case Call(Member(target, name, member_span), args, span) if not isinstance(target, MutArg):
                    return Call(Member(MutArg(target, token.span), name, member_span), args, span)
                case target:
                    return MutArg(target, token.span)
        return self.parse_expr()

    def parse_mut_method_call(self) -> Call:
        """`mut s.push(3)`: a method call that changes its receiver, which becomes `MutArg(s)`.

        Only postfix operators are taken after `mut`, so `mut s.pop() + 1`
        adds 1 to what `pop` gives.
        """
        token = self.advance()
        match self.parse_expr(self.infix["("][0]):
            case Call(Member(target, name, member_span), args, span) if not isinstance(target, MutArg):
                return Call(Member(MutArg(target, token.span), name, member_span), args, span)
        raise EllaError(
            "'mut' here must be followed by a method call that changes its value, like 'mut s.push(3)'", token.span
        )

    def parse_splice(self) -> Splice:
        """`$name`, `$(expr)` or `$` and a block: splice in code.

        It applies to just the name or parentheses that follow. A type (`$t`
        in a type) or a field name (`v.$f`) is spliced the same way.
        """
        token = self.advance()
        if self.at_block():
            return Splice(self.parse_block(), token.span)
        if self.at_kind(TokenKind.NAME):
            name = self.advance()
            return Splice(Name(name.text, name.span), token.span)
        if self.accept("("):
            # Just one expression: `$(t) => ...` in a lambda's result type isn't a parameter list.
            code = self.parse_expr()
            self.expect(")")
            return Splice(code, token.span)
        raise self.unexpected("a name or '(' after '$'")

    def parse_unary(self) -> Expr:
        """A prefix operator and its operand: `-x`, `~x`, `not x`."""
        token = self.advance()
        # `-` directly before an integer literal makes a negative literal. This
        # is the only way to write the smallest i64, -9223372036854775808,
        # because 9223372036854775808 alone doesn't fit. It is skipped if the
        # literal is followed by an operator that binds tighter than `-` (such
        # as `**`), so `-2 ** 2` is still `-(2 ** 2)`.
        if token.text == "-" and self.at_kind(TokenKind.INT) and not self.binds_tighter_than_prefix(1):
            literal = self.advance()
            assert isinstance(literal.value, int)
            return IntLit(self.check_int(-literal.value, token.span), token.span)
        operand = self.parse_expr(self.prefix[token.text] + 1)
        return Unary(token.text, operand, token.span)

    def binds_tighter_than_prefix(self, offset: int) -> bool:
        """Is the token at `offset` an operator that binds tighter than prefix `-`?"""
        token = self.peek(offset)
        if token.kind is not TokenKind.OP or token.text not in self.infix:
            return False
        return self.infix[token.text][0] > self.prefix["-"]

    def parse_string(self) -> StringLit:
        """A string: STRING_START, then text and `{{ expr }}` parts, then STRING_END."""
        start = self.expect_kind(TokenKind.STRING_START, "a string")
        parts: list[str | Expr] = []
        while not self.at_kind(TokenKind.STRING_END):
            if self.at_kind(TokenKind.STRING_TEXT):
                text = self.advance().value
                assert isinstance(text, str)
                parts.append(text)
            else:
                self.expect_kind(TokenKind.INTERP_START, "'{{'")
                parts.append(self.parse_expr())
                self.expect_kind(TokenKind.INTERP_END, "'}}'")
        self.advance()
        return StringLit(tuple(parts), start.span)

    def parse_parens(self) -> Expr:
        """`(` starts a unit `()`, a grouping `(x)`, a tuple `(x, y)`, or the
        parameters of a lambda `(x: i64) -> i64 => ...`.

        We can't tell which until after the `)`, so items are parsed as
        expressions that may also be `name: type`. If `->` or `=>` follows the
        `)`, the items become lambda parameters. This avoids backtracking.
        """
        open_paren = self.advance()
        items, trailing_comma = self.parse_comma_list(self.parse_paren_item, ")")
        if self.at("->") or self.at("=>"):
            return self.parse_lambda(items, open_paren.span)

        # Not a lambda, so no `name: type` items are allowed.
        exprs: list[Expr] = []
        for item in items:
            if isinstance(item, Param):
                what = f"'mut {item.name}'" if item.mutable else f"'{item.name}: ...' type annotations"
                raise EllaError(f"{what} only allowed on lambda parameters", item.span)
            exprs.append(item)
        if not exprs:
            return UnitLit(open_paren.span)
        if len(exprs) == 1 and not trailing_comma:
            return exprs[0]
        return TupleLit(tuple(exprs), open_paren.span)

    def parse_paren_item(self) -> Expr | Param:
        """One item inside `(...)`: an expression, or `name: type` or `mut name: type` in case this is a lambda."""
        if self.accept("mut"):
            name = self.expect_kind(TokenKind.NAME, "a parameter name")
            self.expect(":")
            return Param(name.text, self.parse_type(), name.span, mutable=True)
        if self.at_kind(TokenKind.NAME) and self.at(":", 1) and not self.at_label():
            name = self.advance()
            self.advance()
            return Param(name.text, self.parse_type(), name.span)
        return self.parse_expr()

    # Functions

    def parse_lambda(self, items: list[Expr | Param], span: Span) -> Lambda:
        """The rest of `(params) -> ReturnType => body`, after the `)`."""
        params = []
        for item in items:
            match item:
                case Param():
                    params.append(item)
                case Name(name, name_span):
                    # An untyped parameter, as in `(x) => x`.
                    params.append(Param(name, None, name_span))
                case _:
                    raise EllaError("lambda parameters must look like 'x: i64'", item.span)
        return_type = self.parse_type() if self.accept("->") else None
        self.expect("=>")
        where, body = self.parse_lambda_body()
        return Lambda(tuple(params), return_type, where, body, span)

    def parse_untyped_lambda(self, left: Expr) -> Lambda:
        """`x => body`, which parses but is rejected later until there is type inference."""
        arrow = self.advance()
        if not isinstance(left, Name):
            raise EllaError("expected parameters like '(x: i64) -> i64' before '=>'", arrow.span)
        where, body = self.parse_lambda_body()
        return Lambda((Param(left.name, None, left.span),), None, where, body, left.span)

    def parse_lambda_body(self) -> tuple[tuple[Assert, ...], Expr]:
        """A lambda body and its `where` conditions, which may only start a block body.

        The body extends as far right as possible: `(x: i64) -> i64 => x + 1`
        has the body `x + 1`.
        """
        # Loop labels outside the function aren't visible inside it.
        outer_labels, self.labels = self.labels, []
        if not self.at_block():
            body = self.parse_inline_body()
            self.labels = outer_labels
            return (), body
        self.advance()  # NEWLINE
        indent = self.advance()
        where: tuple[Assert, ...] = ()
        if self.at("where"):
            self.advance()
            outer_where, self.in_where = self.in_where, True
            self.expect_kind(TokenKind.NEWLINE, "end of line")
            self.expect_kind(TokenKind.INDENT, "an indented block of conditions")
            where = self.parse_conditions()
            self.in_where = outer_where
        body = Block(self.parse_lines(), indent.span)
        self.labels = outer_labels
        return where, body

    def parse_conditions(self) -> tuple[Assert, ...]:
        """The lines of a `where` block, after its INDENT, through its DEDENT."""
        conditions = []
        while True:
            conditions.append(self.parse_condition())
            self.end_of_line()
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                return tuple(conditions)

    def parse_condition(self) -> Assert:
        """A condition for `assert` or `where`, keeping its source text for error messages."""
        first = self.peek()
        expr = self.parse_expr()
        return Assert(expr, self.source_since(first), first.span)

    def parse_return(self) -> Expr:
        """`return` or `return value`. Inside `where`, `return` is the function's result."""
        token = self.advance()
        if self.in_where:
            return Name("return", token.span)
        value = self.parse_expr() if self.starts_expr() else None
        return Return(value, token.span)

    # Bindings

    def parse_let(self) -> Let | LetPattern:
        """`let a = mut b: i64 = c = value`: every target in the chain is a new binding.

        After each `=`, another target follows if the next tokens are `mut`,
        or a name followed by `=` or `:`. Otherwise the value starts.

        `let (a, b) = value` and `let Point(x, y) = value` take a value apart
        with a pattern instead, perhaps `mut` and with a type:
        `let mut (a, b): (i64, i64) = value`.
        """
        token = self.advance()
        skip = 1 if self.at("mut") else 0
        if self.at("(", skip) or (self.at_kind(TokenKind.NAME, skip) and self.at("(", skip + 1)):
            mutable = self.accept("mut")
            pattern = self.parse_pattern()
            type_ = self.parse_type() if self.accept(":") else None
            self.expect("=")
            return LetPattern(pattern, self.parse_expr(), token.span, mutable, type_)
        bindings = [self.parse_binding()]
        self.expect("=")
        while self.at("mut") or (
            self.at_kind(TokenKind.NAME) and (self.at("=", 1) or self.at(":", 1)) and not self.at_label()
        ):
            # (A generic function's `f[T] =` can only be the first target.)
            bindings.append(self.parse_binding())
            self.expect("=")
        return Let(tuple(bindings), self.parse_expr(), token.span)

    def parse_binding(self) -> Binding:
        """One `let` target: `x`, `mut x`, `x: i64`, or `first[T]` for a generic function."""
        mutable = self.accept("mut")
        name = self.expect_kind(TokenKind.NAME, "a name")
        names, constraints, sizes = self.parse_type_params()
        type_ = self.parse_type() if self.accept(":") else None
        return Binding(name.text, mutable, type_, name.span, names, constraints, sizes)

    def parse_constrained_param(self) -> tuple[str, tuple[str, ...]]:
        """A generic function's type parameter, with its constraints if any: `T`, `T: Ord + Add`."""
        name = self.parse_type_param()
        constraints: list[str] = []
        if self.accept(":"):
            constraints.append(self.parse_dotted(self.expect_kind(TokenKind.NAME, "a constraint")))
            while self.accept("+"):
                constraints.append(self.parse_dotted(self.expect_kind(TokenKind.NAME, "a constraint")))
        return name, tuple(constraints)

    def parse_type_param(self) -> str:
        return self.expect_kind(TokenKind.NAME, "a type parameter").text

    # Control flow

    def parse_if(self) -> If:
        """`if cond then body [else body]`."""
        token = self.advance()
        cond = self.parse_expr()
        self.expect("then")
        then = self.parse_body()
        # `else` may also start the next line, after an inline `then` body.
        # (After a block body, the block has consumed the line end already.)
        if self.at_kind(TokenKind.NEWLINE) and self.at("else", 1):
            self.advance()
        else_ = self.parse_body() if self.accept("else") else None
        return If(cond, then, else_, token.span)

    def parse_match(self) -> Match:
        """`match subject`, then an indented list of cases."""
        token = self.advance()
        subject = self.parse_expr()
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        self.expect_kind(TokenKind.INDENT, "an indented list of cases")
        cases = []
        while not self.at_kind(TokenKind.DEDENT):
            cases.append(self.parse_case())
        self.advance()
        return Match(subject, tuple(cases), token.span)

    def parse_case(self) -> Case:
        """`case pattern [if guard]`, then `=> expr` or an indented block."""
        token = self.expect("case")
        pattern = self.parse_pattern()
        guard = None
        if self.accept("if"):
            # Parse above `=>`'s precedence, so that in `case x if y => 1` the
            # `y => 1` isn't read as an untyped lambda.
            guard = self.parse_expr(self.infix["=>"][0] + 1)
        if self.accept("=>"):
            body = self.parse_inline_body()
            self.end_of_line()
        else:
            body = self.parse_block()
        return Case(pattern, guard, body, token.span)

    def parse_labeled_loop(self) -> Expr:
        """`label: for ...`, `label: while ...` or `label: loop ...`."""
        label = self.advance().text
        self.advance()  # the `:`
        if self.at("for"):
            return self.parse_for(label)
        if self.at("while"):
            return self.parse_while(label)
        return self.parse_loop(label)

    def parse_loop_body(self, label: str | None) -> Expr:
        """A loop body, inside which the loop's label (if any) can be used."""
        if label is None:
            return self.parse_body()
        self.labels.append(label)
        body = self.parse_body()
        self.labels.pop()
        return body

    def parse_for(self, label: str | None) -> For:
        """`for pattern in iterable body`."""
        token = self.advance()
        pattern = self.parse_pattern()
        self.expect("in")
        mut = self.peek()
        iterable = MutArg(self.parse_expr(), mut.span) if self.accept("mut") else self.parse_expr()
        return For(label, pattern, iterable, self.parse_loop_body(label), token.span)

    def parse_while(self, label: str | None) -> While:
        """`while cond body`."""
        token = self.advance()
        cond = self.parse_expr()
        return While(label, cond, self.parse_loop_body(label), token.span)

    def parse_loop(self, label: str | None) -> Loop:
        """`loop body`, which repeats until a `break`."""
        token = self.advance()
        return Loop(label, self.parse_loop_body(label), token.span)

    def parse_break(self) -> Break:
        """`break [label] [value]`."""
        token = self.advance()
        label = self.parse_optional_label()
        value = self.parse_expr() if self.starts_expr() else None
        return Break(label, value, token.span)

    def parse_optional_label(self) -> str | None:
        """The label after `break` or `continue`, if there is one.

        A name is a label only if it names a loop we're inside. Otherwise
        `break n` is a break with the value `n`. To break with a variable
        that has the same name as a label, write `break (name)`.
        """
        if self.at_kind(TokenKind.NAME) and self.peek().text in self.labels:
            return self.advance().text
        return None

    # Enums

    def parse_enum(self) -> EnumDef:
        """`enum Name[T, ...]`, then an indented list of variants and methods."""
        token = self.advance()
        name = self.expect_kind(TokenKind.NAME, "an enum name")
        type_params, constraints, sizes = self.parse_type_params()
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        self.expect_kind(TokenKind.INDENT, "an indented list of variants")
        variants: list[Variant] = []
        methods: list[Method] = []
        while True:
            if self.at_kind(TokenKind.NAME) and self.at("=", 1):
                methods.append(self.parse_method(methods))
            else:
                variants.append(self.parse_variant())
            self.end_of_line()
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                if not variants:
                    raise EllaError(f"enum {name.text} needs at least one variant", name.span)
                return EnumDef(
                    name.text,
                    type_params,
                    tuple(variants),
                    token.span,
                    methods=tuple(methods),
                    constraints=constraints,
                    size_params=sizes,
                )

    def parse_struct(self) -> EnumDef:
        """`struct Name[T, ...]` or `nocopy struct ...`, then an indented list of fields and methods.

        A method is `name = function`. A `nocopy` struct may have a
        `finalize` method, run when a value dies.
        """
        nocopy = self.accept("nocopy")
        token = self.expect("struct")
        name = self.expect_kind(TokenKind.NAME, "a struct name")
        type_params, constraints, sizes = self.parse_type_params()
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        self.expect_kind(TokenKind.INDENT, "an indented list of fields")
        names: list[str] = []
        types: list[TypeExpr] = []
        methods: list[Method] = []
        while True:
            if self.at_kind(TokenKind.NAME) and self.at("=", 1):
                methods.append(self.parse_method(methods))
            else:
                field = self.expect_kind(TokenKind.NAME, "a field name")
                if field.text in names:
                    raise EllaError(f"'{field.text}' is already a field of {name.text}", field.span)
                self.expect(":")
                names.append(field.text)
                types.append(self.parse_type())
            self.end_of_line()
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                variant = Variant(name.text, tuple(types), name.span)
                return EnumDef(
                    name.text,
                    type_params,
                    (variant,),
                    token.span,
                    True,
                    tuple(names),
                    nocopy,
                    tuple(methods),
                    constraints,
                    sizes,
                )

    def parse_type_params(self) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...], tuple[str, ...]]:
        """A generic's `[T: Ord, U; N, M]`, if any: the type parameters' names, their constraints (empty if none
        have any), and the size parameters, after a `;` (`[; N]` has only one)."""
        params: list[tuple[str, tuple[str, ...]]] = []
        sizes: list[str] = []
        if self.accept("["):
            params, semicolon = self.parse_until_semicolon(self.parse_constrained_param)
            if semicolon:
                sizes, _ = self.parse_comma_list(self.parse_size_param, "]")
                if not sizes:
                    raise EllaError("expected a size parameter after ';'", self.peek().span)
            elif not params:
                raise EllaError("expected a type parameter", self.peek().span)
        constraints = tuple(c for _, c in params) if any(c for _, c in params) else ()
        return tuple(name for name, _ in params), constraints, tuple(sizes)

    def parse_until_semicolon[T](self, parse_item: Callable[[], T]) -> tuple[list[T], bool]:
        """Parse `item, item, ...` up to a `;` or the closing `]`, allowing a trailing comma. Was it a `;`?"""
        items: list[T] = []
        while not self.at(";") and not self.accept("]"):
            items.append(parse_item())
            if not self.accept(","):
                if self.at(";"):
                    break
                self.expect("]")
                return items, False
        return items, self.accept(";")

    def parse_size_param(self) -> str:
        token = self.expect_kind(TokenKind.NAME, "a size parameter")
        if token.text == "_":
            raise EllaError("a size parameter needs a name", token.span)
        return token.text

    def parse_dim(self) -> Dim:
        """One dimension of an array type: `3`, `N` or `_`."""
        token = self.peek()
        if self.at_kind(TokenKind.INT):
            self.advance()
            assert isinstance(token.value, int)
            return Dim(token.value, token.span)
        return Dim(self.expect_kind(TokenKind.NAME, "a size, a size name or '_'").text, token.span)

    def parse_method(self, methods: list[Method]) -> Method:
        """`name = function` in a struct or enum."""
        name = self.advance()
        if any(m.name == name.text for m in methods):
            raise EllaError(f"there is already a method named '{name.text}'", name.span)
        self.expect("=")
        return Method(name.text, self.parse_expr(), name.span)

    def parse_constraint(self) -> ConstraintDef:
        """`constraint Name`, then an indented list of methods with their types: `area: (Self) -> f64`."""
        token = self.advance()
        name = self.expect_kind(TokenKind.NAME, "a constraint name")
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        self.expect_kind(TokenKind.INDENT, "an indented list of methods, like 'area: (Self) -> f64'")
        methods: list[Param] = []
        while True:
            method = self.expect_kind(TokenKind.NAME, "a method name")
            if any(m.name == method.text for m in methods):
                raise EllaError(f"'{method.text}' is already a method of {name.text}", method.span)
            self.expect(":")
            methods.append(Param(method.text, self.parse_type(), method.span))
            self.end_of_line()
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                return ConstraintDef(name.text, tuple(methods), token.span)

    def parse_variant(self) -> Variant:
        """One variant: `Empty`, or `Rect(f64, f64)` with field types."""
        name = self.expect_kind(TokenKind.NAME, "a variant name")
        fields: list[TypeExpr] = []
        if self.accept("("):
            fields, _ = self.parse_comma_list(self.parse_type, ")")
        return Variant(name.text, tuple(fields), name.span)

    # Types
    #
    # Types have their own small grammar, used after `:` and `->`. That's why
    # `[str]` here is a list type, while in an expression `[x]` is a list.

    def parse_type(self) -> TypeExpr:
        """`i64`, `Result[T, E]`, `[T]`, `()`, `(T, U)` or `(T, U) -> R`, or `$t` in a quote."""
        token = self.peek()
        if self.at("$"):
            splice = self.parse_splice()
            return TypeSplice(splice.code, token.span)
        if self.at_kind(TokenKind.NAME):
            self.advance()
            name = self.parse_dotted(token)
            args: list[TypeExpr] = []
            dims: list[Dim] | None = None
            if self.accept("["):
                args, semicolon = self.parse_until_semicolon(self.parse_type)
                if semicolon:
                    if not args:
                        raise EllaError("expected an item type before ';'", self.peek().span)
                    dims, _ = self.parse_comma_list(self.parse_dim, "]")
                    if not dims:
                        raise EllaError("an array needs at least one dimension after ';'", self.peek().span)
            return NamedType(name, tuple(args), token.span, None if dims is None else tuple(dims))
        if self.accept("["):
            element = self.parse_type()
            self.expect("]")
            return ListType(element, token.span)
        if self.accept("("):
            # `(...)` is a function type if `->` follows, otherwise unit or a tuple.
            # Only a function type's parameters may be marked `mut`.
            marked = self.parse_comma_list(self.parse_param_type, ")")
            pairs, trailing_comma = marked
            items = [item for _, item in pairs]
            mutable = tuple(i for i, (is_mut, _) in enumerate(pairs) if is_mut)
            if self.accept("->"):
                return FunctionType(tuple(items), self.parse_type(), token.span, mutable)
            if mutable:
                raise EllaError("only a function type's parameters can be marked 'mut'", token.span)
            if not items:
                return UnitType(token.span)
            if len(items) == 1 and not trailing_comma:
                raise EllaError("a one-element tuple type needs a trailing comma, like (i64,)", token.span)
            return TupleType(tuple(items), token.span)
        raise self.unexpected("a type")

    def parse_param_type(self) -> tuple[bool, TypeExpr]:
        """An item of `(...)` in a type: a type, possibly marked `mut` if this turns out to be a function type."""
        is_mut = self.accept("mut")
        return is_mut, self.parse_type()

    # Patterns
    #
    # Patterns also have their own grammar, used after `case` and `for`.

    def parse_pattern(self) -> Pattern:
        """A pattern, or alternatives separated by `|`: `0 | 1`."""
        first = self.parse_pattern_atom()
        if not self.at("|"):
            return first
        alternatives = [first]
        while self.accept("|"):
            alternatives.append(self.parse_pattern_atom())
        return OrPattern(tuple(alternatives), first.span)

    def parse_pattern_atom(self) -> Pattern:
        """One pattern without `|`."""
        token = self.peek()
        if self.accept("mut"):
            name = self.peek()
            if not self.at_kind(TokenKind.NAME) or name.text == "_" or self.at("(", 1):
                raise EllaError("'mut' in a pattern must be followed by a name, like 'mut x'", token.span)
            self.advance()
            return NamePattern(name.text, name.span, mutable=True)
        if self.at_kind(TokenKind.NAME):
            self.advance()
            if token.text == "_":
                return WildcardPattern(token.span)
            text = self.parse_dotted(token)  # a path like `geo.Circle` is always a variant
            if self.accept("("):
                args, _ = self.parse_comma_list(self.parse_pattern, ")")
                return VariantPattern(text, tuple(args), token.span)
            # Whether a bare name like `None` is a variant or a new binding is
            # decided later, by name resolution.
            return NamePattern(text, token.span)
        if self.accept("("):
            items, trailing_comma = self.parse_comma_list(self.parse_pattern, ")")
            if len(items) < 2 and not trailing_comma:
                raise EllaError("a tuple pattern needs a comma, like (a, b)", token.span)
            return TuplePattern(tuple(items), token.span)
        # Anything else must be a literal, possibly the start of a range.
        low = self.parse_literal_value()
        if self.at("..") or self.at("..="):
            inclusive = self.advance().text == "..="
            return RangePattern(low, self.parse_literal_value(), inclusive, token.span)
        return LiteralPattern(low, token.span)

    def parse_literal_value(self) -> LiteralValue:
        """A literal in a pattern: a number (optionally negative), a plain string, or a bool."""
        token = self.peek()
        sign = -1 if self.accept("-") else 1
        number = self.peek()
        if number.kind is TokenKind.INT:
            self.advance()
            assert isinstance(number.value, int)
            return self.check_int(sign * number.value, token.span)
        if number.kind is TokenKind.FLOAT:
            self.advance()
            assert isinstance(number.value, float)
            return sign * number.value
        if sign == -1:
            raise self.unexpected("a number after '-'")
        if token.kind is TokenKind.STRING_START:
            string = self.parse_string()
            if not all(isinstance(part, str) for part in string.parts):
                raise EllaError("string patterns can't contain interpolation", string.span)
            return "".join(str(part) for part in string.parts)
        if self.at("true") or self.at("false"):
            return self.advance().text == "true"
        raise self.unexpected("a pattern")
