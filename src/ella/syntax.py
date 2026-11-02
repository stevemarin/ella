"""Syntax tree for Ella, as produced by the parser.

Nodes are frozen dataclasses grouped into three unions: `Expr`, `Pattern` and
`TypeExpr` (type annotations as written, not checked types). Later stages
`match` over them. Every node has a `span`: where it starts, or for infix and
postfix nodes, where the operator is.

Nodes keep only what later stages need. Punctuation, keywords and layout are
gone, and a few conveniences are already rewritten: `x |> f(a)` arrives here
as the call `f(x, a)`, and `-5` as the literal -5.

`show` renders any node as a compact S-expression, for tests and the CLI.
For example, `let x = 1 + 2 * 3` is shown as `(let [x] (+ 1 (* 2 3)))`.
"""

import json
from dataclasses import dataclass, fields, is_dataclass

from ella.lexer import Span

# Type annotations


@dataclass(frozen=True, slots=True)
class NamedType:
    """`i64`, `str`, or a generic type with arguments: `Result[f64, Error]`."""

    name: str
    args: tuple[TypeExpr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class ListType:
    """`[T]`"""

    element: TypeExpr
    span: Span


@dataclass(frozen=True, slots=True)
class UnitType:
    """`()`"""

    span: Span


@dataclass(frozen=True, slots=True)
class TupleType:
    """`(T, U)`"""

    items: tuple[TypeExpr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class FunctionType:
    """`(T, U) -> R`, or `(mut T, U) -> R`; `mutable` lists the positions of `mut` parameters."""

    params: tuple[TypeExpr, ...]
    result: TypeExpr
    span: Span
    mutable: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TypeSplice:
    """`$t` in a type, inside a quote: the type that `t`, a `Type[T]`, holds."""

    code: Expr
    span: Span


type TypeExpr = NamedType | ListType | UnitType | TupleType | FunctionType | TypeSplice


# Patterns

type LiteralValue = int | float | bool | str


@dataclass(frozen=True, slots=True)
class WildcardPattern:
    """`_` matches anything and binds nothing."""

    span: Span


@dataclass(frozen=True, slots=True)
class NamePattern:
    """A bare name: binds the value, unless name resolution finds it is a variant like `None`."""

    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class LiteralPattern:
    """`0`, `-1`, `2.5`, `"x"`, `true`"""

    value: LiteralValue
    span: Span


@dataclass(frozen=True, slots=True)
class RangePattern:
    """`0..10` or, when inclusive, `0..=9`"""

    low: LiteralValue
    high: LiteralValue
    inclusive: bool
    span: Span


@dataclass(frozen=True, slots=True)
class OrPattern:
    """`0 | 1`"""

    alternatives: tuple[Pattern, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class VariantPattern:
    """`Ok(v)`: an enum variant with patterns for its fields."""

    name: str
    args: tuple[Pattern, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class TuplePattern:
    """`(a, b)`"""

    items: tuple[Pattern, ...]
    span: Span


type Pattern = (
    WildcardPattern | NamePattern | LiteralPattern | RangePattern | OrPattern | VariantPattern | TuplePattern
)


# Pieces of expressions that are not expressions themselves


@dataclass(frozen=True, slots=True)
class Binding:
    """One target of a `let`: `x`, `mut x`, `x: i64`, or a generic function's `sum[T: Add]`.

    `constraints` has the constraints of each type parameter: `[T: Ord + Add]`
    has (("Ord", "Add"),).
    """

    name: str
    mutable: bool
    type: TypeExpr | None
    span: Span
    type_params: tuple[str, ...] = ()
    constraints: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class Param:
    """A lambda parameter: `x: i64`, or `mut xs: [i64]`.

    `type` is None only in untyped lambdas like `x => x + 1`.
    """

    name: str
    type: TypeExpr | None
    span: Span
    mutable: bool = False


@dataclass(frozen=True, slots=True)
class Case:
    """`case pattern if guard => body`"""

    pattern: Pattern
    guard: Expr | None
    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Variant:
    """An enum variant declaration: `Rect(f64, f64)` or `Empty`."""

    name: str
    fields: tuple[TypeExpr, ...]
    span: Span


# Expressions


@dataclass(frozen=True, slots=True)
class IntLit:
    value: int
    span: Span


@dataclass(frozen=True, slots=True)
class FloatLit:
    value: float
    span: Span


@dataclass(frozen=True, slots=True)
class BoolLit:
    value: bool
    span: Span


@dataclass(frozen=True, slots=True)
class StringLit:
    """A string literal: a sequence of text and interpolated expressions."""

    parts: tuple[str | Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class UnitLit:
    """`()`"""

    span: Span


@dataclass(frozen=True, slots=True)
class TupleLit:
    items: tuple[Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class ListLit:
    items: tuple[Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class Name:
    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class Unary:
    """Prefix `-`, `~` or `not`."""

    op: str
    operand: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Binary:
    """Arithmetic, bitwise, `and`, `or`, and ranges (`..`, `..=`)."""

    op: str
    left: Expr
    right: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Compare:
    """A comparison chain: `a < b <= c` is `Compare(a, (("<", b), ("<=", c)))`."""

    first: Expr
    rest: tuple[tuple[str, Expr], ...]
    span: Span


@dataclass(frozen=True, slots=True)
class Call:
    """`f(a, b)`. Pipes are turned into calls: `x |> f(a)` becomes `f(x, a)`."""

    callee: Expr
    args: tuple[Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class MutArg:
    """`mut target` as a call argument, for a `mut` parameter: `push(mut xs, 5)`.

    The target must be a `mut` variable or an element of one. The parser
    allows any expression here; the checker enforces that.
    """

    target: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Index:
    """`target[index]`"""

    target: Expr
    index: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Member:
    """`target.name`"""

    target: Expr
    name: str
    span: Span


@dataclass(frozen=True, slots=True)
class Try:
    """Postfix `?`: unwrap a value or return the error from the enclosing function."""

    operand: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Let:
    """`let y = mut x = 5` has two bindings (`y`, `mut x`) and the value `5`."""

    bindings: tuple[Binding, ...]
    value: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Assign:
    """`target = value`, or a compound assignment such as `target += value`."""

    op: str
    target: Name | Index | Member
    value: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Lambda:
    """`(params) -> return_type => body`, with optional `where` conditions."""

    params: tuple[Param, ...]
    return_type: TypeExpr | None
    where: tuple[Assert, ...]
    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Block:
    """Indented lines; the value is that of the last one."""

    exprs: tuple[Expr, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class If:
    """`if cond then ... else ...`; `else_` is None when there is no `else`."""

    cond: Expr
    then: Expr
    else_: Expr | None
    span: Span


@dataclass(frozen=True, slots=True)
class Match:
    """`match subject` and its cases, tried in order."""

    subject: Expr
    cases: tuple[Case, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class For:
    """`for pattern in iterable body`. Loops may have a label: `outer: for ...`."""

    label: str | None
    pattern: Pattern
    iterable: Expr
    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class While:
    """`while cond body`."""

    label: str | None
    cond: Expr
    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Loop:
    """`loop body`, which repeats until a `break`."""

    label: str | None
    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Break:
    """`break [label] [value]`. The value becomes the value of a `loop`."""

    label: str | None
    value: Expr | None
    span: Span


@dataclass(frozen=True, slots=True)
class Continue:
    """`continue [label]`."""

    label: str | None
    span: Span


@dataclass(frozen=True, slots=True)
class Return:
    """`return [value]`, exiting the innermost function."""

    value: Expr | None
    span: Span


@dataclass(frozen=True, slots=True)
class Assert:
    """`assert expr`, or one condition of a `where` block.

    `source` is the condition as written, so that a failure can show it:
    "assertion failed: fibonacci(5) == 8".
    """

    expr: Expr
    source: str
    span: Span


@dataclass(frozen=True, slots=True)
class Method:
    """A function in a struct or enum definition: `area = (c: Circle) -> f64 => ...`."""

    name: str
    function: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class EnumDef:
    """`enum Name[T]` followed by an indented list of variants, or a struct.

    A struct is an enum with one variant named after it, whose fields have
    names: `struct Point` with fields `x: f64` and `y: f64` is like `enum
    Point` with the variant `Point(f64, f64)`, so constructing, matching,
    comparing and printing work the same way. `nocopy` structs can't be
    copied, and may have a `finalize` method, run when a value dies.

    Both can have methods, called as `value.name(...)`. `constraints` has
    the constraints of each type parameter, as in Binding.
    """

    name: str
    type_params: tuple[str, ...]
    variants: tuple[Variant, ...]
    span: Span
    struct: bool = False
    field_names: tuple[str, ...] = ()
    nocopy: bool = False
    methods: tuple[Method, ...] = ()
    constraints: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ConstraintDef:
    """`constraint Name` followed by the methods a type needs to meet it: `area: (Self) -> f64`."""

    name: str
    methods: tuple[Param, ...]
    span: Span


@dataclass(frozen=True, slots=True)
class TypeLit:
    """`type T`: the type T as a value, of type `Type[T]`, for code that works on types."""

    type: TypeExpr
    span: Span


@dataclass(frozen=True, slots=True)
class FieldSplice:
    """`target.$name`, inside a quote: the field whose name `name`, a str, gives."""

    target: Expr
    code: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Quote:
    """`quote expr`: the code of `expr`, as a value, to run at a later stage."""

    body: Expr
    span: Span


@dataclass(frozen=True, slots=True)
class Splice:
    """`$x` or `$(expr)`: inside a quote, insert the code `expr` gives; outside, run it while compiling."""

    code: Expr
    span: Span


type Expr = (
    IntLit
    | FloatLit
    | BoolLit
    | StringLit
    | UnitLit
    | TupleLit
    | ListLit
    | Name
    | Unary
    | Binary
    | Compare
    | Call
    | MutArg
    | Index
    | Member
    | Try
    | Let
    | Assign
    | Lambda
    | Block
    | If
    | Match
    | For
    | While
    | Loop
    | Break
    | Continue
    | Return
    | Assert
    | EnumDef
    | ConstraintDef
    | Quote
    | Splice
    | TypeLit
    | FieldSplice
)


@dataclass(frozen=True, slots=True)
class Program:
    """A source file: top-level expressions, run in order."""

    body: tuple[Expr, ...]
    span: Span


# Helpers


def mentions_return(node: object) -> bool:
    """Does a `where` condition use `return`, making it a postcondition?

    This walks any syntax tree node generically, through its dataclass fields.
    It doesn't look inside nested lambdas, whose `return` is their own.
    """
    match node:
        case Name(name="return"):
            return True
        case Lambda():
            return False
        case tuple():
            return any(mentions_return(item) for item in node)
        case _ if is_dataclass(node) and not isinstance(node, type):
            return any(mentions_return(getattr(node, f.name)) for f in fields(node))
    return False


# Printing


def show(node: Expr | Pattern | TypeExpr) -> str:
    """Render a node compactly: expressions and patterns as S-expressions, types as Ella syntax."""
    match node:
        # Types
        case NamedType(name, args):
            return f"{name}[{show_list(args)}]" if args else name
        case ListType(element):
            return f"[{show(element)}]"
        case UnitType():
            return "()"
        case TupleType(items):
            return f"({show(items[0])},)" if len(items) == 1 else f"({show_list(items)})"
        case TypeSplice(code):
            return f"$({show(code)})"
        case FunctionType(params, result, mutable=mutable):
            shown = ", ".join(("mut " if i in mutable else "") + show(p) for i, p in enumerate(params))
            return f"({shown}) -> {show(result)}"

        # Patterns
        case WildcardPattern():
            return "_"
        case NamePattern(name):
            return name
        case LiteralPattern(value):
            return show_literal(value)
        case RangePattern(low, high, inclusive):
            return f"{show_literal(low)}{'..=' if inclusive else '..'}{show_literal(high)}"
        case OrPattern(alternatives):
            return sexpr("|", *map(show, alternatives))
        case VariantPattern(name, args):
            return sexpr(name, *map(show, args))
        case TuplePattern(items):
            return sexpr("tuple", *map(show, items))

        # Expressions
        case IntLit(value) | FloatLit(value) | BoolLit(value):
            return show_literal(value)
        case StringLit(parts):
            if all(isinstance(part, str) for part in parts):
                return show_literal("".join(str(part) for part in parts))
            return sexpr("str", *(show_literal(part) if isinstance(part, str) else show(part) for part in parts))
        case UnitLit():
            return "()"
        case TupleLit(items):
            return sexpr("tuple", *map(show, items))
        case ListLit(items):
            return sexpr("list", *map(show, items))
        case Name(name):
            return name
        case Unary(op, operand):
            return sexpr(op, show(operand))
        case Binary(op, left, right):
            return sexpr(op, show(left), show(right))
        case Compare(first, rest):
            return sexpr("compare", show(first), *(f"{op} {show(operand)}" for op, operand in rest))
        case Call(callee, args):
            return sexpr("call", show(callee), *map(show, args))
        case MutArg(target):
            return sexpr("mut", show(target))
        case Index(target, index):
            return sexpr("index", show(target), show(index))
        case Member(target, name):
            return sexpr(".", show(target), name)
        case Try(operand):
            return sexpr("?", show(operand))
        case Let(bindings, value):
            return sexpr("let", f"[{', '.join(map(show_binding, bindings))}]", show(value))
        case Assign(op, target, value):
            return sexpr(op, show(target), show(value))
        case Lambda(params, return_type, where, body):
            signature = f"({', '.join(map(show_param, params))})"
            if return_type is not None:
                signature += f" -> {show(return_type)}"
            conditions = [sexpr("where", *(show(condition.expr) for condition in where))] if where else []
            return sexpr("lambda", signature, *conditions, show(body))
        case Block(exprs):
            return sexpr("block", *map(show, exprs))
        case If(cond, then, else_):
            return sexpr("if", show(cond), show(then), *([show(else_)] if else_ is not None else []))
        case Match(subject, cases):
            return sexpr("match", show(subject), *map(show_case, cases))
        case For(label, pattern, iterable, body):
            return sexpr("for", *show_label(label), show(pattern), show(iterable), show(body))
        case While(label, cond, body):
            return sexpr("while", *show_label(label), show(cond), show(body))
        case Loop(label, body):
            return sexpr("loop", *show_label(label), show(body))
        case Break(label, value):
            return sexpr("break", *show_label(label), *([show(value)] if value is not None else []))
        case Continue(label):
            return sexpr("continue", *show_label(label))
        case Return(value):
            return sexpr("return", *([show(value)] if value is not None else []))
        case Assert(expr):
            return sexpr("assert", show(expr))
        case EnumDef(variants=variants, struct=True, field_names=names, nocopy=nocopy, methods=methods):
            fields = (f"{n}: {show(t)}" for n, t in zip(names, variants[0].fields))
            functions = [sexpr(m.name, show(m.function)) for m in methods]
            return sexpr("nocopy struct" if nocopy else "struct", show_head(node), *fields, *functions)
        case EnumDef(variants=variants, methods=methods):
            return sexpr(
                "enum",
                show_head(node),
                *(sexpr(v.name, *map(show, v.fields)) if v.fields else v.name for v in variants),
                *(sexpr(m.name, show(m.function)) for m in methods),
            )
        case ConstraintDef(name, methods):
            return sexpr("constraint", name, *map(show_param, methods))
        case Quote(body):
            return sexpr("quote", show(body))
        case Splice(code):
            return sexpr("$", show(code))
        case TypeLit(t):
            return sexpr("type", show(t))
        case FieldSplice(target, code):
            return sexpr(".", show(target), sexpr("$", show(code)))


def sexpr(head: str, *items: str) -> str:
    """`sexpr("+", "1", "2")` is `(+ 1 2)`."""
    return f"({' '.join((head, *items))})"


def show_list(nodes: tuple[TypeExpr, ...]) -> str:
    return ", ".join(map(show, nodes))


def show_literal(value: LiteralValue) -> str:
    """Literals as Ella writes them: `true` rather than `True`, strings quoted."""
    match value:
        case bool():
            return "true" if value else "false"
        case str():
            return json.dumps(value)
        case _:
            return str(value)


def show_label(label: str | None) -> list[str]:
    """Labels are shown with a colon, `outer:`, to tell them apart from values."""
    return [] if label is None else [f"{label}:"]


def show_type_params(params: tuple[str, ...], constraints: tuple[tuple[str, ...], ...]) -> str:
    """`[T: Ord + Add, U]`, or nothing without type parameters."""
    if not params:
        return ""
    constraints = constraints or tuple(() for _ in params)
    return f"[{', '.join(f'{p}: {" + ".join(c)}' if c else p for p, c in zip(params, constraints))}]"


def show_head(enum: EnumDef) -> str:
    return enum.name + show_type_params(enum.type_params, enum.constraints)


def show_binding(binding: Binding) -> str:
    text = f"mut {binding.name}" if binding.mutable else binding.name
    text += show_type_params(binding.type_params, binding.constraints)
    return text if binding.type is None else f"{text}: {show(binding.type)}"


def show_param(param: Param) -> str:
    text = f"mut {param.name}" if param.mutable else param.name
    return text if param.type is None else f"{text}: {show(param.type)}"


def show_case(case: Case) -> str:
    guard = [] if case.guard is None else ["if", show(case.guard)]
    return sexpr("case", show(case.pattern), *guard, show(case.body))
