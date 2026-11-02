"""Static type checker: works out the type of every expression before the program runs.

Ella is statically typed. Function parameters and results are annotated, and
the checker infers everything else, mostly by reading types off values: in
`let x = 1 + 2`, `x` is an `i64` because `1 + 2` is.

Some types can't be read off right away. An empty list `[]` is a list of
something, and `None` is an `Optional` of something. For these the checker
uses a *type variable*, a placeholder to be filled in later. Using the value
then decides it:

    let mut seen = []        # seen: [?1]
    seen = seen + [5]        # [?1] must equal [i64], so ?1 is i64

Making two types equal, filling in type variables as needed, is called
*unification*, and it is the heart of the checker (`unify` below). A type
variable that is never decided defaults to `()`.

The checker doesn't change the syntax tree. It records what it learns in
tables keyed by node, which the lowering to IR reads:

* `types`: the type of every expression, binding, parameter and pattern.
* `kinds`: what each name refers to: a "local" or "global" variable, a
  "variant" without fields, a "constructor" of a variant with fields, or a
  "builtin" function.

The tables are keyed by `id(node)` (the node's identity) rather than by the
node itself, since two separate nodes can be equal as values.

Function bodies are checked last. A function may use top-level names defined
after it, as long as it is called after they are defined, so its body is only
checked once every top-level name's type is known. See `check_program`.
"""

from dataclasses import dataclass, field
from itertools import count

from ella.lexer import EllaError, Span
from ella.syntax import (
    Assert,
    Assign,
    Binary,
    Block,
    BoolLit,
    Break,
    Call,
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
    VariantPattern,
    While,
    WildcardPattern,
    mentions_return,
)

# Types


@dataclass(frozen=True, slots=True)
class PrimTy:
    """A built-in type without parts: `i64`, `f64`, `bool`, `str`, `()`, `range`, or `never`.

    `never` is the type of expressions that don't produce a value because they
    jump away, like `return x` or `break`. It fits anywhere: in
    `if c then 1 else return 0`, the `else` doesn't spoil the `i64`.
    """

    name: str


I64 = PrimTy("i64")
F64 = PrimTy("f64")
BOOL = PrimTy("bool")
STR = PrimTy("str")
UNIT = PrimTy("()")
RANGE = PrimTy("range")
NEVER = PrimTy("never")


@dataclass(frozen=True, slots=True)
class ListTy:
    """`[T]`"""

    element: Type


@dataclass(frozen=True, slots=True)
class TupleTy:
    """`(T, U)`"""

    items: tuple[Type, ...]


@dataclass(frozen=True, slots=True)
class FuncTy:
    """`(T, U) -> R`"""

    params: tuple[Type, ...]
    result: Type


@dataclass(frozen=True, slots=True)
class EnumTy:
    """An enum type with its type arguments: `Shape`, `Optional[i64]`, `Result[f64, str]`."""

    name: str
    args: tuple[Type, ...] = ()


@dataclass(frozen=True, slots=True)
class ParamTy:
    """A type parameter inside an enum definition, like the `T` of `enum Optional[T]`.

    It is replaced by the actual type argument wherever the enum is used.
    """

    name: str


@dataclass(eq=False, slots=True)
class TyVar:
    """A type not known yet. Once unification decides it, `ref` points to the answer.

    `eq=False` gives identity comparison: two different unknowns are
    different even before either is decided.
    """

    id: int
    ref: Type | None = None


type Type = PrimTy | ListTy | TupleTy | FuncTy | EnumTy | ParamTy | TyVar


def resolve(t: Type) -> Type:
    """Follow decided type variables to what they stand for (one level)."""
    while isinstance(t, TyVar) and t.ref is not None:
        t = t.ref
    return t


def finish(t: Type) -> Type:
    """The type with every type variable replaced by its answer, all the way down.

    Type variables that were never decided become `()`.
    """
    match resolve(t):
        case TyVar():
            return UNIT
        case ListTy(element):
            return ListTy(finish(element))
        case TupleTy(items):
            return TupleTy(tuple(finish(item) for item in items))
        case FuncTy(params, result):
            return FuncTy(tuple(finish(param) for param in params), finish(result))
        case EnumTy(name, args):
            return EnumTy(name, tuple(finish(arg) for arg in args))
        case other:
            return other


def show_type(t: Type) -> str:
    """A type as written in Ella, for error messages and the IR printer."""
    match resolve(t):
        case PrimTy(name):
            return name
        case ListTy(element):
            return f"[{show_type(element)}]"
        case TupleTy(items) if len(items) == 1:
            return f"({show_type(items[0])},)"
        case TupleTy(items):
            return "(" + ", ".join(map(show_type, items)) + ")"
        case FuncTy(params, result):
            return "(" + ", ".join(map(show_type, params)) + f") -> {show_type(result)}"
        case EnumTy(name, args) if args:
            return f"{name}[" + ", ".join(map(show_type, args)) + "]"
        case EnumTy(name):
            return name
        case ParamTy(name):
            return name
        case TyVar(id):
            return f"?{id}"


def substitute(t: Type, params: dict[str, Type]) -> Type:
    """Replace the type parameters in `t`: `Some(T)` with T = i64 has a field of type i64."""
    match t:
        case ParamTy(name):
            return params[name]
        case ListTy(element):
            return ListTy(substitute(element, params))
        case TupleTy(items):
            return TupleTy(tuple(substitute(item, params) for item in items))
        case FuncTy(args, result):
            return FuncTy(tuple(substitute(arg, params) for arg in args), substitute(result, params))
        case EnumTy(name, args):
            return EnumTy(name, tuple(substitute(arg, params) for arg in args))
        case _:
            return t


def describe_kind(t: Type) -> str:
    """The general kind of a type, used to check what operators allow: "i64", "list", "function", ..."""
    match resolve(t):
        case PrimTy(name):
            return name
        case ListTy():
            return "list"
        case TupleTy():
            return "tuple"
        case FuncTy():
            return "function"
        case EnumTy(name):
            return name
        case ParamTy(name):
            return name
        case TyVar():
            return "unknown"


@dataclass(frozen=True, slots=True)
class EnumInfo:
    """An enum's definition: its type parameters and each variant's field types."""

    name: str
    params: tuple[str, ...]
    variants: dict[str, tuple[Type, ...]]


# The built-in enums. Their variants' fields refer to the type parameters.
BUILTIN_ENUMS = {
    "Result": EnumInfo("Result", ("T", "E"), {"Ok": (ParamTy("T"),), "Error": (ParamTy("E"),)}),
    "Optional": EnumInfo("Optional", ("T",), {"Some": (ParamTy("T"),), "None": ()}),
}

PRIMITIVE_NAMES = {"i64": I64, "f64": F64, "bool": BOOL, "str": STR}


# Scopes


@dataclass(slots=True)
class VarInfo:
    """What the checker knows about a name in scope.

    `kind` is "local" or "global" for variables, "variant" for a variant
    without fields, "constructor" for one with fields, and "builtin" for
    `print` and `len`. Variants and constructors also name their enum.
    """

    type: Type
    mutable: bool
    kind: str
    enum: str = ""


@dataclass(slots=True)
class Env:
    """A scope of names during checking, like the interpreter's Scope but holding types."""

    parent: Env | None
    vars: dict[str, VarInfo] = field(default_factory=dict)

    def lookup(self, name: str) -> VarInfo | None:
        env: Env | None = self
        while env is not None:
            if name in env.vars:
                return env.vars[name]
            env = env.parent
        return None


@dataclass(slots=True)
class LoopInfo:
    """A loop being checked, for `break` and `continue` to find.

    For a `loop`, `break_type` collects the type of the values its breaks give.
    """

    label: str | None
    kind: str  # "for", "while" or "loop"
    break_type: Type | None = None


@dataclass(slots=True)
class PendingBody:
    """A function body waiting to be checked, with the scope it was created in."""

    function: Lambda
    captured: Env
    type: FuncTy


def check(program: Program) -> Checker:
    """Type check a program, returning the checker with its tables. Raises EllaError."""
    checker = Checker()
    checker.check_program(program)
    return checker


class Checker:
    def __init__(self) -> None:
        self.types: dict[int, Type] = {}
        self.kinds: dict[int, str] = {}
        self.enums: dict[str, EnumInfo] = dict(BUILTIN_ENUMS)
        self.counter = count(1)  # numbers for new type variables

        self.globals = Env(None)
        self.globals.vars["print"] = VarInfo(UNIT, False, "builtin")
        self.globals.vars["len"] = VarInfo(UNIT, False, "builtin")
        for enum in BUILTIN_ENUMS.values():
            self.define_variants(enum, self.globals)
        # Top-level names whose `let` hasn't been checked yet, for a clearer error.
        self.pending_globals: set[str] = set()

        self.return_type: Type | None = None  # the current function's result type
        self.loops: list[LoopInfo] = []  # the loops we are inside, innermost last
        self.pending_bodies: list[PendingBody] = []

    def type_of(self, node: object) -> Type:
        """The final type of a checked node (with type variables filled in)."""
        return finish(self.types[id(node)])

    # Type variables and unification

    def fresh(self) -> TyVar:
        return TyVar(next(self.counter))

    def unify(self, a: Type, b: Type) -> bool:
        """Try to make two types equal by deciding type variables. Returns whether it worked."""
        a, b = resolve(a), resolve(b)
        if a is b or a is NEVER or b is NEVER:
            return True
        if isinstance(a, TyVar):
            return self.bind(a, b)
        if isinstance(b, TyVar):
            return self.bind(b, a)
        match a, b:
            case ListTy(x), ListTy(y):
                return self.unify(x, y)
            case TupleTy(xs), TupleTy(ys):
                return len(xs) == len(ys) and all(self.unify(x, y) for x, y in zip(xs, ys))
            case FuncTy(xs, x), FuncTy(ys, y):
                return len(xs) == len(ys) and all(self.unify(p, q) for p, q in zip(xs, ys)) and self.unify(x, y)
            case EnumTy(n, xs), EnumTy(m, ys):
                return n == m and all(self.unify(x, y) for x, y in zip(xs, ys))
            case _:
                return a == b

    def bind(self, var: TyVar, t: Type) -> bool:
        """Decide a type variable, refusing infinite types like ?1 = [?1]."""
        if self.occurs(var, t):
            return False
        var.ref = t
        return True

    def occurs(self, var: TyVar, t: Type) -> bool:
        match resolve(t):
            case TyVar() as other:
                return other is var
            case ListTy(element):
                return self.occurs(var, element)
            case TupleTy(items):
                return any(self.occurs(var, item) for item in items)
            case FuncTy(params, result):
                return any(self.occurs(var, param) for param in params) or self.occurs(var, result)
            case EnumTy(_, args):
                return any(self.occurs(var, arg) for arg in args)
            case _:
                return False

    def expect(self, actual: Type, expected: Type, span: Span, what: str) -> None:
        """Require `actual` to be (or become) `expected`; `what` describes it for the error."""
        if not self.unify(actual, expected):
            raise EllaError(f"{what} should be {show_type(expected)}, not {show_type(actual)}", span)

    def join(self, a: Type, b: Type, span: Span, what: str) -> Type:
        """The type of a value that comes from either of two places, like the two sides of an `if`."""
        if resolve(a) is NEVER:
            return b
        if resolve(b) is NEVER:
            return a
        if not self.unify(a, b):
            raise EllaError(f"{what} must have the same type, not {show_type(a)} and {show_type(b)}", span)
        return a

    def known(self, t: Type, span: Span) -> Type:
        """A type that must already be decided, such as an operand whose operator depends on it."""
        t = resolve(t)
        if isinstance(t, TyVar):
            raise EllaError("can't tell the type of this value yet; add a type annotation", span)
        return t

    # Names and annotations

    def lookup(self, name: str, env: Env, span: Span) -> VarInfo:
        info = env.lookup(name)
        if info is not None:
            return info
        if name in self.pending_globals:
            raise EllaError(f"'{name}' is used before its 'let' has run", span)
        raise EllaError(f"undefined name '{name}'", span)

    def bind_name(self, env: Env, name: str, t: Type, mutable: bool, span: Span) -> None:
        """Add a variable. A top-level name keeps one type, even when bound again."""
        if env is self.globals:
            self.pending_globals.discard(name)
            old = env.vars.get(name)
            if old is not None and old.kind == "global" and not self.unify(t, old.type):
                raise EllaError(
                    f"'{name}' is a top-level {show_type(old.type)}, and can't be bound again as {show_type(t)}",
                    span,
                )
            env.vars[name] = VarInfo(t, mutable, "global")
        else:
            env.vars[name] = VarInfo(t, mutable, "local")

    def annotation(self, texpr: TypeExpr, params: dict[str, Type] | None = None) -> Type:
        """The type a type annotation stands for. `params` are an enum's type parameters."""
        params = params or {}
        match texpr:
            case NamedType(name, (), span) if name in params:
                return params[name]
            case NamedType(name, (), span) if name in PRIMITIVE_NAMES:
                return PRIMITIVE_NAMES[name]
            case NamedType("Error", (), span):
                # Provisional: until there is a proper error type, `Error` in a
                # type means a message string (docs/grammar.md, open questions).
                return STR
            case NamedType(name, args, span) if name in self.enums:
                enum = self.enums[name]
                if len(args) != len(enum.params):
                    raise EllaError(f"{name} takes {len(enum.params)} type arguments, not {len(args)}", span)
                return EnumTy(name, tuple(self.annotation(arg, params) for arg in args))
            case NamedType(name, _, span):
                raise EllaError(f"unknown type '{name}'", span)
            case ListType(element):
                return ListTy(self.annotation(element, params))
            case UnitType():
                return UNIT
            case TupleType(items):
                return TupleTy(tuple(self.annotation(item, params) for item in items))
            case FunctionType(args, result):
                return FuncTy(tuple(self.annotation(arg, params) for arg in args), self.annotation(result, params))

    # Enums

    def declare_enum(self, enum: EnumDef) -> None:
        """Make an enum's name known, so types can refer to it before its variants are read."""
        self.enums[enum.name] = EnumInfo(enum.name, enum.type_params, {})

    def define_enum(self, enum: EnumDef, env: Env) -> None:
        """Read an enum's variants, and make them names in `env`."""
        self.declare_enum(enum)
        params: dict[str, Type] = {name: ParamTy(name) for name in enum.type_params}
        variants = {v.name: tuple(self.annotation(f, params) for f in v.fields) for v in enum.variants}
        self.enums[enum.name] = EnumInfo(enum.name, enum.type_params, variants)
        self.define_variants(self.enums[enum.name], env)

    def define_variants(self, enum: EnumInfo, env: Env) -> None:
        for name, fields in enum.variants.items():
            env.vars[name] = VarInfo(UNIT, False, "constructor" if fields else "variant", enum.name)

    def instantiate(self, enum_name: str, variant: str) -> tuple[EnumTy, tuple[Type, ...]]:
        """A fresh use of a variant: its enum type, with new type variables for the
        type arguments, and its field types. Each use of `None` can then be an
        Optional of something different."""
        enum = self.enums[enum_name]
        args: dict[str, Type] = {param: self.fresh() for param in enum.params}
        fields = tuple(substitute(f, args) for f in enum.variants[variant])
        return EnumTy(enum_name, tuple(args.values())), fields

    def variant_fields(self, enum_type: EnumTy, variant: str) -> tuple[Type, ...]:
        """The field types of a variant of a particular enum type, like `Some` in Optional[i64]."""
        enum = self.enums[enum_type.name]
        args = dict(zip(enum.params, enum_type.args))
        return tuple(substitute(f, args) for f in enum.variants[variant])

    # Programs

    def check_program(self, program: Program) -> None:
        # Enums first, in two steps so that they can refer to each other.
        for expr in program.body:
            if isinstance(expr, EnumDef):
                self.declare_enum(expr)
        for expr in program.body:
            if isinstance(expr, EnumDef):
                self.define_enum(expr, self.globals)
        # Top-level functions exist from the start (docs/grammar.md §3), and
        # their types are known from their annotations.
        for expr in program.body:
            match expr:
                case Let(bindings, Lambda() as function):
                    for binding in bindings:
                        self.bind_name(
                            self.globals, binding.name, self.lambda_type(function), binding.mutable, binding.span
                        )
                case Let(bindings):
                    self.pending_globals.update(b.name for b in bindings if b.name not in self.globals.vars)

        for expr in program.body:
            self.check(expr, self.globals)
        # Now every top-level name has its type, so function bodies can be checked.
        while self.pending_bodies:
            self.check_body(self.pending_bodies.pop(0))

        main = self.globals.vars.get("main")
        if main is not None and main.kind == "global" and not self.unify(main.type, FuncTy((), UNIT)):
            raise EllaError(f"main must have type () -> (), not {show_type(main.type)}", program.span)

    # Expressions

    def check(self, expr: Expr, env: Env) -> Type:
        """Check an expression and return its type, recording it in `self.types`."""
        t = self.check_expr(expr, env)
        self.types[id(expr)] = t
        return t

    def check_bool(self, expr: Expr, env: Env, what: str) -> None:
        t = self.check(expr, env)
        if not self.unify(t, BOOL):
            raise EllaError(f"{what} needs a bool, not {show_type(t)}", expr.span)

    def check_expr(self, expr: Expr, env: Env) -> Type:
        match expr:
            # Literals
            case IntLit():
                return I64
            case FloatLit():
                return F64
            case BoolLit():
                return BOOL
            case StringLit(parts):
                # Any value can be interpolated: every value has a text form.
                for part in parts:
                    if not isinstance(part, str):
                        self.check(part, env)
                return STR
            case UnitLit():
                return UNIT
            case TupleLit(items):
                return TupleTy(tuple(self.check(item, env) for item in items))
            case ListLit(items):
                element: Type = self.fresh()
                for item in items:
                    element = self.join(element, self.check(item, env), item.span, "list items")
                return ListTy(element)

            # Names
            case Name(name, span):
                info = self.lookup(name, env, span)
                self.kinds[id(expr)] = info.kind
                match info.kind:
                    case "builtin" | "constructor":
                        raise EllaError(f"'{name}' can only be called, not used as a value", span)
                    case "variant":
                        enum_type, _ = self.instantiate(info.enum, name)
                        return enum_type
                    case _:
                        return info.type
            case Let(bindings, value_expr):
                if isinstance(value_expr, Lambda):
                    # A `let`-bound function can call itself by name.
                    t: Type = self.check_lambda(value_expr, env, [b.name for b in bindings])
                    self.types[id(value_expr)] = t
                else:
                    t = self.check(value_expr, env)
                for binding in bindings:
                    binding_type = t
                    if binding.type is not None:
                        binding_type = self.annotation(binding.type)
                        self.expect(t, binding_type, value_expr.span, f"the value of '{binding.name}'")
                    self.types[id(binding)] = binding_type
                    self.bind_name(env, binding.name, binding_type, binding.mutable, binding.span)
                return t
            case Assign(op, target, value_expr, span):
                value_type = self.check(value_expr, env)
                target_type = self.check_place(target, env)
                if op == "=":
                    self.expect(value_type, target_type, value_expr.span, "the assigned value")
                else:
                    result = self.binary_type(op.removesuffix("="), target_type, value_type, span)
                    self.expect(result, target_type, span, f"the result of '{op}'")
                return target_type

            # Operators
            case Unary("not", operand):
                self.check_bool(operand, env, "'not'")
                return BOOL
            case Unary(op, operand, span):
                t = self.check(operand, env)
                kind = describe_kind(self.known(t, operand.span))
                if (op == "-" and kind in ("i64", "f64")) or (op == "~" and kind == "i64"):
                    return t
                raise EllaError(f"'{op}' doesn't work on {show_type(t)}", span)
            case Binary(("and" | "or") as op, left, right):
                self.check_bool(left, env, f"'{op}'")
                self.check_bool(right, env, f"'{op}'")
                return BOOL
            case Binary(op, left, right, span):
                return self.binary_type(op, self.check(left, env), self.check(right, env), span)
            case Compare(first, rest, span):
                left_type = self.check(first, env)
                for op, operand in rest:
                    right_type = self.check(operand, env)
                    self.compare_type(op, left_type, right_type, span)
                    left_type = right_type
                return BOOL
            case Index(target, index, span):
                container = self.known(self.check(target, env), target.span)
                index_type = self.check(index, env)
                if not self.unify(index_type, I64):
                    raise EllaError(f"an index must be an i64, not {show_type(index_type)}", index.span)
                match container:
                    case ListTy(element):
                        return element
                    case PrimTy("str"):
                        return STR
                raise EllaError(f"can't index a {show_type(container)}", span)
            case Member(span=span):
                raise EllaError("member access ('.name') isn't supported yet", span)
            case Try(operand, span):
                return self.check_try(self.check(operand, env), span)

            # Functions
            case Lambda():
                return self.check_lambda(expr, env, [])
            case Call(callee, args, span):
                return self.check_call(callee, args, env, span)
            case Return(value, span):
                if self.return_type is None:
                    raise EllaError("'return' outside of a function", span)
                value_type = UNIT if value is None else self.check(value, env)
                self.expect(value_type, self.return_type, span, "the returned value")
                return NEVER

            # Control flow
            case Block(exprs):
                local = Env(env)
                t = UNIT
                for line in exprs:
                    t = self.check(line, local)
                # A block that ends with an assignment has the value ().
                return UNIT if isinstance(exprs[-1], Assign) else t
            case If(cond, then, else_, span):
                self.check_bool(cond, env, "an 'if' condition")
                then_type = self.check(then, env)
                if else_ is None:
                    return UNIT  # the then body's value is discarded
                return self.join(then_type, self.check(else_, env), span, "both branches of 'if'")
            case Match(subject, cases, span):
                subject_type = self.check(subject, env)
                match_type: Type = NEVER
                for case in cases:
                    case_env = Env(env)
                    self.check_pattern(case.pattern, subject_type, case_env)
                    if case.guard is not None:
                        self.check_bool(case.guard, case_env, "a case guard")
                    match_type = self.join(
                        match_type, self.check(case.body, case_env), case.span, "all cases of 'match'"
                    )
                return match_type
            case For(label, pattern, iterable, body, span):
                iterable_type = self.known(self.check(iterable, env), iterable.span)
                match iterable_type:
                    case ListTy(element):
                        item_type = element
                    case PrimTy("str"):
                        item_type = STR
                    case PrimTy("range"):
                        item_type = I64
                    case _:
                        raise EllaError(f"can't loop over a {show_type(iterable_type)}", span)
                loop_env = Env(env)
                self.check_pattern(pattern, item_type, loop_env)
                self.check_loop_body(LoopInfo(label, "for"), body, loop_env)
                return UNIT
            case While(label, cond, body):
                self.check_bool(cond, env, "a 'while' condition")
                self.check_loop_body(LoopInfo(label, "while"), body, env)
                return UNIT
            case Loop(label, body):
                loop_info = LoopInfo(label, "loop")
                self.check_loop_body(loop_info, body, env)
                # A loop that never breaks never finishes, so it has no value.
                return NEVER if loop_info.break_type is None else loop_info.break_type
            case Break(label, value, span):
                loop = self.find_loop(label, "break", span)
                if value is not None and loop.kind != "loop":
                    raise EllaError(f"only 'loop' can break with a value, not '{loop.kind}'", span)
                value_type = UNIT if value is None else self.check(value, env)
                if loop.break_type is None:
                    loop.break_type = value_type
                else:
                    loop.break_type = self.join(loop.break_type, value_type, span, "the values of a loop's breaks")
                return NEVER
            case Continue(label, span):
                self.find_loop(label, "continue", span)
                return NEVER

            # Others
            case Assert():
                self.check_bool(expr.expr, env, "an assertion")
                return UNIT
            case EnumDef():
                self.define_enum(expr, env)
                return UNIT

    # Operators

    def binary_type(self, op: str, a: Type, b: Type, span: Span) -> Type:
        """The result type of an arithmetic, bitwise or range operator."""
        if not self.unify(a, b):
            raise EllaError(f"'{op}' needs two values of the same type, not {show_type(a)} and {show_type(b)}", span)
        kind = describe_kind(self.known(a, span))
        # Which kinds of operands each operator accepts (docs/grammar.md §8).
        allowed = {
            "+": ("i64", "f64", "str", "list"),
            "-": ("i64", "f64"),
            "*": ("i64", "f64"),
            "/": ("i64", "f64"),
            "**": ("i64", "f64"),
            "..": ("i64",),
            "..=": ("i64",),
        }.get(op, ("i64",))  # `//`, `%` and the bitwise operators
        if kind not in allowed:
            raise EllaError(f"'{op}' doesn't work on {show_type(a)}", span)
        if op == "/":
            return F64
        if op in ("..", "..="):
            return RANGE
        return a

    def compare_type(self, op: str, a: Type, b: Type, span: Span) -> None:
        """Check one comparison of a chain. Comparisons always give a bool."""
        if op in ("in", "not in"):
            match self.known(b, span):
                case ListTy(element):
                    self.expect(a, element, span, f"the value looked for with '{op}'")
                case PrimTy("str"):
                    self.expect(a, STR, span, f"the value looked for with '{op}'")
                case PrimTy("range"):
                    self.expect(a, I64, span, f"the value looked for with '{op}'")
                case other:
                    raise EllaError(f"'in' needs a list, str or range on the right, not {show_type(other)}", span)
            return
        if not self.unify(a, b):
            raise EllaError(f"'{op}' needs two values of the same type, not {show_type(a)} and {show_type(b)}", span)
        kind = describe_kind(self.known(a, span))
        if kind == "function":
            raise EllaError("functions can't be compared", span)
        if op not in ("==", "!=") and kind not in ("i64", "f64", "str"):
            raise EllaError(f"'{op}' doesn't work on {show_type(a)}", span)

    def check_try(self, t: Type, span: Span) -> Type:
        """Postfix `?`: the function must return the same kind of enum, so the error can pass through."""
        if self.return_type is None:
            raise EllaError("'?' can only be used inside a function", span)
        match self.known(t, span):
            case EnumTy("Result", (value, error)):
                if not self.unify(self.return_type, EnumTy("Result", (self.fresh(), error))):
                    raise EllaError(
                        f"'?' on a {show_type(t)} needs the function to return a Result with the same error type, "
                        f"not {show_type(self.return_type)}",
                        span,
                    )
                return value
            case EnumTy("Optional", (value,)):
                if not self.unify(self.return_type, EnumTy("Optional", (self.fresh(),))):
                    raise EllaError(
                        f"'?' on an Optional needs the function to return an Optional, not {show_type(self.return_type)}",
                        span,
                    )
                return value
        raise EllaError(f"'?' needs a Result or Optional, not {show_type(t)}", span)

    def check_place(self, target: Expr, env: Env) -> Type:
        """The type of something being assigned to: a `mut` variable, or an element of one."""
        match target:
            case Name(name, span):
                info = self.lookup(name, env, span)
                self.kinds[id(target)] = info.kind
                if info.kind not in ("local", "global") or not info.mutable:
                    raise EllaError(
                        f"can't assign to '{name}' because it isn't mutable; declare it with 'let mut'", span
                    )
                t = info.type
            case Index(container_expr, index, span):
                container = self.known(self.check_place(container_expr, env), container_expr.span)
                index_type = self.check(index, env)
                if not self.unify(index_type, I64):
                    raise EllaError(f"an index must be an i64, not {show_type(index_type)}", index.span)
                match container:
                    case ListTy(element):
                        t = element
                    case _:
                        raise EllaError(f"can't assign to an element of a {show_type(container)}", span)
            case _:
                raise EllaError("can only assign to a variable or an element of one", target.span)
        self.types[id(target)] = t
        return t

    # Functions

    def lambda_type(self, function: Lambda) -> FuncTy:
        """A function's type, from its annotations, which are required for now."""
        params = []
        for param in function.params:
            if param.type is None:
                raise EllaError(f"parameter '{param.name}' needs a type, like '{param.name}: i64'", param.span)
            params.append(self.annotation(param.type))
            self.types[id(param)] = params[-1]
        if function.return_type is None:
            raise EllaError("a lambda needs a return type, like '(x: i64) -> i64 => ...'", function.span)
        return FuncTy(tuple(params), self.annotation(function.return_type))

    def check_lambda(self, function: Lambda, env: Env, self_names: list[str]) -> FuncTy:
        """A lambda's type. Its body is checked later, once all top-level names are known."""
        t = self.lambda_type(function)
        captured = self.capture(env)
        for name in self_names:
            captured.vars[name] = VarInfo(t, False, "local")
        self.pending_bodies.append(PendingBody(function, captured, t))
        return t

    def capture(self, env: Env) -> Env:
        """The names a new lambda sees: copies of the local variables (immutable,
        since lambdas capture by copy) in front of the global scope."""
        chain = []
        current: Env | None = env
        while current is not None and current is not self.globals:
            chain.append(current)
            current = current.parent
        captured = Env(self.globals)
        for local in reversed(chain):
            for name, info in local.vars.items():
                if info.kind == "local":
                    captured.vars[name] = VarInfo(info.type, False, "local")
                else:  # a local enum's variants
                    captured.vars[name] = info
        return captured

    def check_body(self, pending: PendingBody) -> None:
        """Check a function's `where` conditions and body against its type."""
        function, t = pending.function, pending.type
        outer_return, outer_loops = self.return_type, self.loops
        self.return_type, self.loops = t.result, []  # `break` can't leave a function

        local = Env(pending.captured)
        for param, param_type in zip(function.params, t.params):
            local.vars[param.name] = VarInfo(param_type, False, "local")
        for cond in function.where:
            if mentions_return(cond.expr):
                result_env = Env(local)
                result_env.vars["return"] = VarInfo(t.result, False, "local")
                self.check_bool(cond.expr, result_env, "a postcondition")
            else:
                self.check_bool(cond.expr, local, "a precondition")
        body_type = self.check(function.body, local)
        self.expect(body_type, t.result, function.body.span, "the function's result")

        self.return_type, self.loops = outer_return, outer_loops

    def check_call(self, callee: Expr, args: tuple[Expr, ...], env: Env, span: Span) -> Type:
        """A call. Built-ins and variant constructors are only ever called directly, and checked here."""
        if isinstance(callee, Name):
            info = self.lookup(callee.name, env, callee.span)
            if info.kind == "builtin":
                self.kinds[id(callee)] = "builtin"
                self.check_arity(callee.name, 1, args, span)
                arg_type = self.check(args[0], env)
                if callee.name == "len" and describe_kind(self.known(arg_type, args[0].span)) not in (
                    "list",
                    "str",
                    "range",
                ):
                    raise EllaError(f"len() needs a list, str or range, not {show_type(arg_type)}", args[0].span)
                return I64 if callee.name == "len" else UNIT
            if info.kind == "constructor":
                self.kinds[id(callee)] = "constructor"
                enum_type, fields = self.instantiate(info.enum, callee.name)
                self.check_arity(callee.name, len(fields), args, span)
                for arg, field_type in zip(args, fields):
                    self.expect(self.check(arg, env), field_type, arg.span, f"the argument of {callee.name}")
                return enum_type
        function = self.known(self.check(callee, env), callee.span)
        if not isinstance(function, FuncTy):
            raise EllaError(f"can't call a value of type {show_type(function)}", span)
        self.check_arity("function", len(function.params), args, span)
        for i, (arg, param_type) in enumerate(zip(args, function.params), 1):
            self.expect(self.check(arg, env), param_type, arg.span, f"argument {i}")
        return function.result

    def check_arity(self, name: str, arity: int, args: tuple[Expr, ...], span: Span) -> None:
        if len(args) != arity:
            plural = "" if arity == 1 else "s"
            raise EllaError(f"{name} takes {arity} argument{plural} but was given {len(args)}", span)

    # Loops

    def check_loop_body(self, info: LoopInfo, body: Expr, env: Env) -> None:
        self.loops.append(info)
        self.check(body, env)  # the body's value is discarded
        self.loops.pop()

    def find_loop(self, label: str | None, keyword: str, span: Span) -> LoopInfo:
        """The loop a `break` or `continue` belongs to: the innermost, or the one with the label."""
        for info in reversed(self.loops):
            if label is None or info.label == label:
                return info
        raise EllaError(f"'{keyword}' outside of a loop", span)

    # Patterns

    def check_pattern(self, pattern: Pattern, t: Type, env: Env) -> None:
        """Check that a pattern can match values of type `t`, adding its variables to `env`."""
        self.types[id(pattern)] = t
        match pattern:
            case WildcardPattern():
                pass
            case NamePattern(name, span):
                # A name is a variant (like `None`) if one is in scope, and otherwise a new variable.
                info = env.lookup(name)
                if info is not None and info.kind == "variant":
                    self.kinds[id(pattern)] = "variant"
                    enum_type, _ = self.instantiate(info.enum, name)
                    self.expect(enum_type, t, span, f"the pattern '{name}'")
                else:
                    self.kinds[id(pattern)] = "binding"
                    env.vars[name] = VarInfo(t, False, "local")
            case LiteralPattern(value, span):
                self.expect(literal_type(value), t, span, "this pattern")
            case RangePattern(low, high, _, span):
                low_type = literal_type(low)
                if low_type not in (I64, F64) or literal_type(high) != low_type:
                    raise EllaError("a range pattern needs two numbers of the same type", span)
                self.expect(low_type, t, span, "this range pattern")
            case OrPattern(alternatives, span):
                # Every alternative must bind the same names, with the same types.
                envs = []
                for alternative in alternatives:
                    alternative_env = Env(env)
                    self.check_pattern(alternative, t, alternative_env)
                    envs.append(alternative_env)
                first = envs[0].vars
                for other in envs[1:]:
                    if other.vars.keys() != first.keys():
                        raise EllaError("each alternative of '|' must bind the same names", span)
                    for name, info in other.vars.items():
                        self.expect(info.type, first[name].type, span, f"'{name}' in each alternative")
                env.vars.update(first)
            case VariantPattern(name, args, span):
                info = env.lookup(name)
                if info is None or info.kind not in ("variant", "constructor"):
                    raise EllaError(f"'{name}' isn't an enum variant", span)
                enum_type, fields = self.instantiate(info.enum, name)
                self.expect(enum_type, t, span, f"the pattern '{name}(...)'")
                if len(args) != len(fields):
                    raise EllaError(f"{name} has {len(fields)} fields, but the pattern has {len(args)}", span)
                for arg, field_type in zip(args, fields):
                    self.check_pattern(arg, field_type, env)
            case TuplePattern(items, span):
                item_types: tuple[Type, ...] = tuple(self.fresh() for _ in items)
                self.expect(TupleTy(item_types), t, span, "this tuple pattern")
                for item, item_type in zip(items, item_types):
                    self.check_pattern(item, item_type, env)


def literal_type(value: LiteralValue) -> Type:
    match value:
        case bool():  # before int, since bool is a subclass of int
            return BOOL
        case int():
            return I64
        case float():
            return F64
        case str():
            return STR
