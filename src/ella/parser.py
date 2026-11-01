"""Pratt parser: turns tokens into the syntax tree defined in ella.syntax.

This follows docs/grammar.md: operator precedence from §8, and the grammar
summary and parsing notes from §11.

A Pratt parser handles operator precedence like this: `parse_expr(min_prec)`
parses one operand (a "prefix" form: a literal, a name, `-x`, `if ...`, and so
on), then keeps extending it with infix and postfix operators for as long as
they bind at least as tightly as `min_prec`. Precedence lives in the
`self.infix` and `self.prefix` tables.
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
    Continue,
    EnumDef,
    Expr,
    FloatLit,
    For,
    FunctionType,
    If,
    Index,
    IntLit,
    Lambda,
    Let,
    ListLit,
    ListType,
    LiteralPattern,
    LiteralValue,
    Loop,
    Match,
    Member,
    Name,
    NamedType,
    NamePattern,
    OrPattern,
    Param,
    Pattern,
    Program,
    RangePattern,
    Return,
    StringLit,
    Try,
    TupleLit,
    TuplePattern,
    TupleType,
    TypeExpr,
    Unary,
    UnitLit,
    UnitType,
    Variant,
    VariantPattern,
    While,
    WildcardPattern,
)

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1

LOWEST = 0  # precedence that accepts any operator

ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "//=", "%="})

# Keywords that can begin an expression, used to decide whether `break` and
# `return` are followed by a value.
EXPR_KEYWORDS = frozenset(
    {
        "assert",
        "break",
        "continue",
        "enum",
        "false",
        "for",
        "if",
        "let",
        "loop",
        "match",
        "not",
        "return",
        "true",
        "while",
    }
)


class Assoc(Enum):
    LEFT = auto()
    RIGHT = auto()
    NONE = auto()  # can't be chained: `a..b..c` is an error
    CHAIN = auto()  # comparisons: `a < b <= c` becomes one Compare node
    POSTFIX = auto()  # call, index, member, `?`


def parse(source: str) -> Program:
    """Tokenize and parse a source file. Raises EllaError on bad input."""
    return Parser(tokenize(source)).parse_program()


def describe(token: Token) -> str:
    """How a token is named in error messages."""
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
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0  # index of the current token
        self.in_where = False  # inside a `where` block, `return` is a name

        # Infix and postfix operators: precedence (higher binds tighter) and
        # associativity. The numbers are the levels in docs/grammar.md §8.
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
        # Prefix operators and their precedence. The operand is parsed at the
        # next level up, so `not a == b` is `not (a == b)` and `-a * b` is `(-a) * b`.
        self.prefix: dict[str, int] = {"not": 5, "-": 15, "~": 15}

    # Looking at tokens

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def previous(self) -> Token:
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

    def advance(self) -> Token:
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
        if not self.at(text):
            raise self.unexpected(f"'{text}'")
        return self.advance()

    def expect_kind(self, kind: TokenKind, expected: str) -> Token:
        if not self.at_kind(kind):
            raise self.unexpected(expected)
        return self.advance()

    def unexpected(self, expected: str) -> EllaError:
        token = self.peek()
        return EllaError(f"expected {expected}, found {describe(token)}", token.span)

    def parse_comma_list[T](self, parse_item: Callable[[], T], close: str) -> tuple[list[T], bool]:
        """Parse `item, item, ...` and the closing bracket, allowing a trailing comma.

        Also returns whether there was a trailing comma, which tells `(x,)` (a
        tuple) apart from `(x)` (grouping).
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

    # Program structure and blocks

    def parse_program(self) -> Program:
        body = []
        while not self.at_kind(TokenKind.EOF):
            body.append(self.parse_expr())
            self.end_of_line()
        return Program(tuple(body), Span(1, 1))

    def end_of_line(self) -> None:
        """Each line in a block ends with NEWLINE. An expression that ended with
        its own indented block has already consumed it."""
        if self.previous().kind is not TokenKind.DEDENT:
            self.expect_kind(TokenKind.NEWLINE, "end of line")

    def parse_block(self) -> Block:
        """NEWLINE INDENT expr { NEWLINE expr } NEWLINE DEDENT"""
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        indent = self.expect_kind(TokenKind.INDENT, "an indented block")
        return Block(self.parse_lines(), indent.span)

    def parse_lines(self) -> tuple[Expr, ...]:
        """The lines of a block, after its INDENT, through its DEDENT."""
        exprs = []
        while True:
            exprs.append(self.parse_expr())
            self.end_of_line()
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                return tuple(exprs)

    def parse_body(self) -> Expr:
        """A body is an expression on the same line, or an indented block."""
        return self.parse_block() if self.at_block() else self.parse_expr()

    # Expressions

    def parse_expr(self, min_prec: int = LOWEST) -> Expr:
        left = self.parse_prefix()
        while (op := self.infix_op()) is not None and self.infix[op][0] >= min_prec:
            left = self.parse_infix(left, op)
        return left

    def infix_op(self) -> str | None:
        """The infix or postfix operator at the current token, if there is one."""
        # An expression that ended with an indented block ends there.
        if self.previous().kind is TokenKind.DEDENT:
            return None
        if self.at("not") and self.at("in", 1):
            return "not in"
        token = self.peek()
        if token.kind in (TokenKind.KEYWORD, TokenKind.OP) and token.text in self.infix:
            return token.text
        return None

    def parse_infix(self, left: Expr, op: str) -> Expr:
        prec, assoc = self.infix[op]
        if assoc is Assoc.POSTFIX:
            return self.parse_postfix(left)
        if op == "=>":
            return self.parse_untyped_lambda(left)

        span = self.take_binary_op(op)
        if assoc is Assoc.CHAIN:
            return self.parse_comparison(left, op, span)
        # A right-associative operator's right side may contain the same
        # operator again: `a ** b ** c` is `a ** (b ** c)`.
        right = self.parse_expr(prec if assoc is Assoc.RIGHT else prec + 1)
        if assoc is Assoc.NONE and (next_op := self.infix_op()) is not None and self.infix[next_op][0] == prec:
            raise EllaError(f"'{next_op}' can't follow '{op}' without parentheses", self.peek().span)
        if op in ASSIGN_OPS:
            if not isinstance(left, Name | Index):
                raise EllaError("can only assign to a name or an index like xs[0]", span)
            return Assign(op, left, right, span)
        if op == "|>":
            return self.make_pipe(left, right, span)
        return Binary(op, left, right, span)

    def take_binary_op(self, op: str) -> Span:
        """Consume a binary operator, which must not end the line."""
        token = self.advance()
        if op == "not in":
            self.advance()
        if self.at_kind(TokenKind.NEWLINE):
            raise EllaError(f"a line can't end with '{op}'; wrap the expression in parentheses", token.span)
        return token.span

    def parse_comparison(self, first: Expr, op: str, span: Span) -> Compare:
        """`a < b <= c` is one node, so that `b` is evaluated once."""
        operand_prec = self.infix[op][0] + 1
        rest = [(op, self.parse_expr(operand_prec))]
        while (next_op := self.infix_op()) is not None and self.infix[next_op][1] is Assoc.CHAIN:
            self.take_binary_op(next_op)
            rest.append((next_op, self.parse_expr(operand_prec)))
        return Compare(first, tuple(rest), span)

    def make_pipe(self, left: Expr, right: Expr, span: Span) -> Call:
        """`x |> f` is `f(x)`, and `x |> f(a, b)` is `f(x, a, b)`."""
        match right:
            case Name():
                return Call(right, (left,), span)
            case Call(callee=callee, args=args):
                return Call(callee, (left, *args), span)
        raise EllaError("the right side of '|>' must be a name or a call", span)

    def parse_postfix(self, left: Expr) -> Expr:
        token = self.advance()
        match token.text:
            case "(":
                args, _ = self.parse_comma_list(self.parse_expr, ")")
                return Call(left, tuple(args), token.span)
            case "[":
                index = self.parse_expr()
                self.expect("]")
                return Index(left, index, token.span)
            case ".":
                name = self.expect_kind(TokenKind.NAME, "a member name")
                return Member(left, name.text, token.span)
            case _:
                return Try(left, token.span)

    def parse_prefix(self) -> Expr:
        """Parse an expression that doesn't start with an operand: literals, names,
        brackets, prefix operators, and the keyword forms."""
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
            case TokenKind.NAME, _:
                self.advance()
                return Name(token.text, token.span)
            case TokenKind.STRING_START, _:
                return self.parse_string()
            case TokenKind.LABEL, _:
                return self.parse_labeled_loop()
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
            case TokenKind.KEYWORD, "assert":
                self.advance()
                return Assert(self.parse_expr(), token.span)
            case TokenKind.KEYWORD, "enum":
                return self.parse_enum()
            case TokenKind.KEYWORD, "where":
                raise EllaError("'where' is only allowed as the first clause of a function's block body", token.span)
            case TokenKind.KEYWORD, ("comptime" | "import" | "struct" | "type"):
                raise EllaError(f"'{token.text}' is reserved for later and can't be used yet", token.span)
        raise self.unexpected("an expression")

    def starts_expr(self) -> bool:
        """Can the current token begin an expression? (For the optional values of `break` and `return`.)"""
        token = self.peek()
        match token.kind:
            case TokenKind.INT | TokenKind.FLOAT | TokenKind.NAME | TokenKind.STRING_START | TokenKind.LABEL:
                return True
            case TokenKind.OP:
                return token.text in ("(", "[", "-", "~")
            case TokenKind.KEYWORD:
                return token.text in EXPR_KEYWORDS
            case _:
                return False

    def check_int(self, value: int, span: Span) -> int:
        if not I64_MIN <= value <= I64_MAX:
            raise EllaError("integer literal doesn't fit in i64", span)
        return value

    def parse_unary(self) -> Expr:
        token = self.advance()
        # `-` directly before an integer literal makes a negative literal, which
        # is how the smallest i64, -9223372036854775808, can be written. This is
        # skipped if the literal is followed by an operator that binds tighter
        # than `-` (such as `**`), so `-2 ** 2` is still `-(2 ** 2)`.
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
        parameters of a lambda. Items are parsed as expressions that may also be
        `name: type`; only once we reach `)` can we tell whether `->` or `=>`
        follows, making them lambda parameters."""
        open_paren = self.advance()
        items, trailing_comma = self.parse_comma_list(self.parse_paren_item, ")")
        if self.at("->") or self.at("=>"):
            return self.parse_lambda(items, open_paren.span)

        exprs: list[Expr] = []
        for item in items:
            if isinstance(item, Param):
                raise EllaError(
                    f"'{item.name}: ...' type annotations are only allowed on lambda parameters", item.span
                )
            exprs.append(item)
        if not exprs:
            return UnitLit(open_paren.span)
        if len(exprs) == 1 and not trailing_comma:
            return exprs[0]
        return TupleLit(tuple(exprs), open_paren.span)

    def parse_paren_item(self) -> Expr | Param:
        if self.at_kind(TokenKind.NAME) and self.at(":", 1):
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

    def parse_lambda_body(self) -> tuple[tuple[Expr, ...], Expr]:
        """A lambda body, and its `where` conditions, which may only start a block body."""
        if not self.at_block():
            return (), self.parse_expr()
        self.advance()
        indent = self.advance()
        where: tuple[Expr, ...] = ()
        if self.at("where"):
            self.advance()
            outer, self.in_where = self.in_where, True
            where = self.parse_block().exprs
            self.in_where = outer
        return where, Block(self.parse_lines(), indent.span)

    def parse_return(self) -> Expr:
        token = self.advance()
        if self.in_where:
            # In a `where` condition, `return` names the function's result.
            return Name("return", token.span)
        value = self.parse_expr() if self.starts_expr() else None
        return Return(value, token.span)

    # Bindings

    def parse_let(self) -> Let:
        """`let a = mut b: i64 = c = value`: every target in the chain is a new binding."""
        token = self.advance()
        bindings = [self.parse_binding()]
        self.expect("=")
        while self.at("mut") or (self.at_kind(TokenKind.NAME) and (self.at("=", 1) or self.at(":", 1))):
            bindings.append(self.parse_binding())
            self.expect("=")
        return Let(tuple(bindings), self.parse_expr(), token.span)

    def parse_binding(self) -> Binding:
        mutable = self.accept("mut")
        name = self.expect_kind(TokenKind.NAME, "a name")
        type_ = self.parse_type() if self.accept(":") else None
        return Binding(name.text, mutable, type_, name.span)

    # Control flow

    def parse_if(self) -> If:
        token = self.advance()
        cond = self.parse_expr()
        self.expect("then")
        then = self.parse_body()
        # `else` may also start the next line.
        if self.at_kind(TokenKind.NEWLINE) and self.at("else", 1):
            self.advance()
        else_ = self.parse_body() if self.accept("else") else None
        return If(cond, then, else_, token.span)

    def parse_match(self) -> Match:
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
            # Stop before `=>` rather than reading it as an untyped lambda.
            guard = self.parse_expr(self.infix["=>"][0] + 1)
        if self.accept("=>"):
            body = self.parse_expr()
            self.end_of_line()
        else:
            body = self.parse_block()
        return Case(pattern, guard, body, token.span)

    def parse_labeled_loop(self) -> Expr:
        label = self.advance()
        assert isinstance(label.value, str)
        if self.at("for"):
            return self.parse_for(label.value)
        if self.at("while"):
            return self.parse_while(label.value)
        if self.at("loop"):
            return self.parse_loop(label.value)
        raise EllaError("a label must be followed by 'for', 'while' or 'loop'", label.span)

    def parse_for(self, label: str | None) -> For:
        token = self.advance()
        pattern = self.parse_pattern()
        self.expect("in")
        iterable = self.parse_expr()
        return For(label, pattern, iterable, self.parse_body(), token.span)

    def parse_while(self, label: str | None) -> While:
        token = self.advance()
        cond = self.parse_expr()
        return While(label, cond, self.parse_body(), token.span)

    def parse_loop(self, label: str | None) -> Loop:
        token = self.advance()
        return Loop(label, self.parse_body(), token.span)

    def parse_break(self) -> Break:
        token = self.advance()
        label = self.parse_optional_label()
        value = self.parse_expr() if self.starts_expr() else None
        return Break(label, value, token.span)

    def parse_optional_label(self) -> str | None:
        if not self.at_kind(TokenKind.LABEL):
            return None
        label = self.advance().value
        assert isinstance(label, str)
        return label

    # Enums

    def parse_enum(self) -> EnumDef:
        token = self.advance()
        name = self.expect_kind(TokenKind.NAME, "an enum name")
        type_params: list[str] = []
        if self.accept("["):
            type_params, _ = self.parse_comma_list(
                lambda: self.expect_kind(TokenKind.NAME, "a type parameter").text, "]"
            )
        self.expect_kind(TokenKind.NEWLINE, "end of line")
        self.expect_kind(TokenKind.INDENT, "an indented list of variants")
        variants = []
        while True:
            variants.append(self.parse_variant())
            self.expect_kind(TokenKind.NEWLINE, "end of line")
            if self.at_kind(TokenKind.DEDENT):
                self.advance()
                return EnumDef(name.text, tuple(type_params), tuple(variants), token.span)

    def parse_variant(self) -> Variant:
        name = self.expect_kind(TokenKind.NAME, "a variant name")
        fields: list[TypeExpr] = []
        if self.accept("("):
            fields, _ = self.parse_comma_list(self.parse_type, ")")
        return Variant(name.text, tuple(fields), name.span)

    # Types

    def parse_type(self) -> TypeExpr:
        token = self.peek()
        if self.at_kind(TokenKind.NAME):
            self.advance()
            args: list[TypeExpr] = []
            if self.accept("["):
                args, _ = self.parse_comma_list(self.parse_type, "]")
            return NamedType(token.text, tuple(args), token.span)
        if self.accept("["):
            element = self.parse_type()
            self.expect("]")
            return ListType(element, token.span)
        if self.accept("("):
            items, trailing_comma = self.parse_comma_list(self.parse_type, ")")
            if self.accept("->"):
                return FunctionType(tuple(items), self.parse_type(), token.span)
            if not items:
                return UnitType(token.span)
            if len(items) == 1 and not trailing_comma:
                raise EllaError("a one-element tuple type needs a trailing comma, like (i64,)", token.span)
            return TupleType(tuple(items), token.span)
        raise self.unexpected("a type")

    # Patterns

    def parse_pattern(self) -> Pattern:
        first = self.parse_pattern_atom()
        if not self.at("|"):
            return first
        alternatives = [first]
        while self.accept("|"):
            alternatives.append(self.parse_pattern_atom())
        return OrPattern(tuple(alternatives), first.span)

    def parse_pattern_atom(self) -> Pattern:
        token = self.peek()
        if self.at_kind(TokenKind.NAME):
            self.advance()
            if token.text == "_":
                return WildcardPattern(token.span)
            if self.accept("("):
                args, _ = self.parse_comma_list(self.parse_pattern, ")")
                return VariantPattern(token.text, tuple(args), token.span)
            return NamePattern(token.text, token.span)
        if self.accept("("):
            items, trailing_comma = self.parse_comma_list(self.parse_pattern, ")")
            if len(items) < 2 and not trailing_comma:
                raise EllaError("a tuple pattern needs a comma, like (a, b)", token.span)
            return TuplePattern(tuple(items), token.span)
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
