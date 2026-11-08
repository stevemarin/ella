"""Default methods of constraints (docs/grammar.md §9, "Default methods").

A constraint's default, `describe = (s: Self) -> str => ...` in `constraint Shape`, becomes a
generic function `Shape#.describe[Self: Shape]`, which the checker checks once, like any generic
function: so it can use only what the constraint gives. Each type that implements the
constraint, or one requiring it, and doesn't define `describe` gets a method calling it:
`describe = (s: Square) -> str => Shape#.describe(s)`. `Shape.describe(x)` calls the default
itself. After this, defaults are ordinary code to the checker, the interpreters and the lowering,
which specialize the function for each type, so its method calls are direct calls of that type's
methods (its own `describe`, if it has one).

Linking runs it (ella.modules), and so does `checker.check` for a program that wasn't linked.
Running it again changes nothing.
"""

from dataclasses import dataclass, replace

from ella.lexer import EllaError, Span
from ella.syntax import (
    Binding,
    Call,
    ConstraintDef,
    EnumDef,
    Expr,
    Lambda,
    Let,
    Member,
    Method,
    MutArg,
    Name,
    NamedType,
    Program,
    TypeExpr,
    bare_name,
    rebuild,
)


def default_name(constraint: str, method: str) -> str:
    """The name of the generic function a constraint's default method becomes: `Shape#.describe`.

    The `#` keeps it apart from the names programs can write, and the `.` from the names of local types
    (`f#S`, ella.modules); `bare_name` gives `describe`.
    """
    return f"{constraint}#.{method}"


def is_default(name: str) -> bool:
    """Whether a top-level function is a constraint's default method (`default_name`)."""
    return "#." in name


@dataclass(frozen=True, slots=True)
class Default:
    """A default method a constraint has: its own, or one of a constraint it requires (`origin`)."""

    origin: str
    method: Method


def expand_defaults(program: Program) -> Program:
    """The program with its constraints' default methods made into functions, given to the types that need them.

    Raises EllaError for a type that implements two constraints with defaults of one name and doesn't
    define that method itself.
    """
    constraints = {expr.name: expr for expr in program.body if isinstance(expr, ConstraintDef)}
    if not any(constraint.defaults for constraint in constraints.values()):
        return program
    defined = {binding.name for expr in program.body if isinstance(expr, Let) for binding in expr.bindings}
    closures: dict[str, dict[str, list[Default]]] = {}
    body: list[Expr] = []
    for expr in program.body:
        match expr:
            case ConstraintDef():
                body.append(expr)
                body += [
                    default_function(expr.name, method)
                    for method in expr.defaults
                    if default_name(expr.name, method.name) not in defined
                ]
            case EnumDef():
                body.append(give_defaults(expr, constraints, closures))
            case _:
                body.append(expr)
    calls = tuple(call_defaults(expr, constraints, closures) for expr in body)
    return replace(program, body=calls)  # type: ignore[arg-type]


def default_function(constraint: str, method: Method) -> Let:
    """`Shape#.describe[Self: Shape] = <the default>`."""
    binding = Binding(default_name(constraint, method.name), False, None, method.span, ("Self",), ((constraint,),))
    return Let((binding,), method.function, method.span)


def defaults_of(
    name: str,
    constraints: dict[str, ConstraintDef],
    closures: dict[str, dict[str, list[Default]]],
    active: frozenset[str] = frozenset(),
) -> dict[str, list[Default]]:
    """The default methods a constraint has, its own and those of the constraints it requires, by name.

    A name with more than one comes from constraints that each give one. A constraint's own default
    hides its parents' of that name, which the checker reports.
    """
    if name in closures:
        return closures[name]
    constraint = constraints.get(name)
    if constraint is None or name in active:  # built-in, unknown, or a cycle: the checker reports those
        return {}
    found: dict[str, list[Default]] = {method.name: [Default(name, method)] for method in constraint.defaults}
    own = set(found)
    for parent in constraint.parents:
        for method_name, defaults in defaults_of(parent, constraints, closures, active | {name}).items():
            if method_name in own:
                continue  # a default for a parent's method: the checker reports it
            known = {default.origin for default in found.get(method_name, [])}
            found.setdefault(method_name, []).extend(d for d in defaults if d.origin not in known)
    closures[name] = found
    return found


def give_defaults(
    enum: EnumDef, constraints: dict[str, ConstraintDef], closures: dict[str, dict[str, list[Default]]]
) -> EnumDef:
    """A struct or enum with a method for each default of the constraints it implements that it doesn't define."""
    own = {method.name for method in enum.methods}
    given: dict[str, Default] = {}  # the default each method comes from, so far
    added: list[Method] = []
    for implementation in enum.implementations:
        for name, defaults in defaults_of(implementation.constraint, constraints, closures).items():
            if name in own:
                continue
            if name in given:  # through another implementation
                defaults = [given[name], *(d for d in defaults if d.origin != given[name].origin)]
                if len(defaults) == 1:
                    continue
            if len(defaults) > 1:
                origins = " and ".join(bare_name(default.origin) for default in defaults)
                raise EllaError(
                    f"{bare_name(enum.name)} must define '{name}' itself: {origins} each give a default for it",
                    implementation.span,
                )
            if "#" in enum.name:
                raise EllaError(
                    f"a type defined inside a function can't have methods, so it can't get "
                    f"{bare_name(defaults[0].origin)}'s default '{name}'",
                    implementation.span,
                )
            given[name] = defaults[0]
            if (method := calling_default(enum, defaults[0])) is not None:
                added.append(method)
    return replace(enum, methods=(*enum.methods, *added)) if added else enum


def calling_default(enum: EnumDef, default: Default) -> Method | None:
    """A type's method that calls a default: `describe = (s: Square) -> str => Shape#.describe(s)`.

    None if the default isn't a function taking a parameter, which the checker reports.
    """
    function = default.method.function
    if not isinstance(function, Lambda) or not function.params:
        return None
    span = default.method.span
    own = NamedType(enum.name, tuple(NamedType(param, (), span) for param in enum.type_params), span)
    params = tuple(replace(param, type=with_self(param.type, own)) for param in function.params)
    args: tuple[Expr, ...] = tuple(
        MutArg(Name(param.name, param.span), param.span) if param.mutable else Name(param.name, param.span)
        for param in function.params
    )
    body = Call(Name(default_name(default.origin, default.method.name), span), args, span)
    result = with_self(function.return_type, own)
    return Method(default.method.name, Lambda(params, result, (), body, function.span), span)


def calls_default(method: Method) -> bool:
    """Whether a type's method is one `calling_default` made, which the program doesn't write."""
    match method.function:
        case Lambda(body=Call(Name(name))):
            return is_default(name)
    return False


def with_self[T: TypeExpr | None](t: T, own: TypeExpr) -> T:
    """A type with `Self` replaced by a type."""

    def walk(node: object) -> object:
        if isinstance(node, NamedType) and node.name == "Self" and not node.args:
            return own
        if isinstance(node, Span):
            return node
        return rebuild(node, walk)

    return walk(t)  # type: ignore[return-value]


def call_defaults(
    expr: Expr, constraints: dict[str, ConstraintDef], closures: dict[str, dict[str, list[Default]]]
) -> object:
    """Code with each `Shape.describe` (a constraint's default) made a use of its function."""

    def walk(node: object) -> object:
        match node:
            case Member(Name(name, span), method) if name in constraints:
                defaults = defaults_of(name, constraints, closures).get(method, [])
                if not defaults:
                    raise EllaError(f"{bare_name(name)} has no default method '{method}'", span)
                if len(defaults) > 1:
                    origins = " and ".join(bare_name(default.origin) for default in defaults)
                    raise EllaError(f"{origins} each give a default '{method}': say which", span)
                return Name(default_name(defaults[0].origin, method), span)
            case Span():
                return node
        return rebuild(node, walk)

    return walk(expr)
