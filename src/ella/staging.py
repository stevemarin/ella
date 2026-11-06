"""Multi-stage programming: code as values, made with `quote` and combined with `$`."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, overload

from ella.checker import (
    BuiltinTy,
    Checker,
    EnumInfo,
    EnumTy,
    FuncTy,
    ListTy,
    ParamTy,
    PrimTy,
    TupleTy,
    Type,
    VarInfo,
    check,
    finish,
    show_type,
    substitute,
)
from ella.lexer import EllaError, Span
from ella.modules import link
from ella.syntax import (
    Binary,
    Binding,
    BoolLit,
    Call,
    EnumDef,
    Expr,
    FieldSplice,
    FloatLit,
    FunctionType,
    IntLit,
    Lambda,
    Let,
    ListLit,
    ListType,
    Member,
    Name,
    NamedType,
    NamePattern,
    OrPattern,
    Param,
    Program,
    Quote,
    Splice,
    StringLit,
    TupleLit,
    TupleType,
    TypeExpr,
    TypeLit,
    TypeSplice,
    UnitLit,
    UnitType,
    Variant,
    parts,
    rebuild,
)
from ella.values import CodeValue, TypeValue, Value, VariantValue

# What can be spliced into a quote: code, a type, or a field's name.
type AnySplice = Splice | TypeSplice | FieldSplice

if TYPE_CHECKING:
    from ella.interpreter import Interpreter


_hygiene_ids = count(1)


@dataclass(slots=True)
class Tables:
    """What the checker found out about code, which building code from it needs.

    Code is checked more than once (when it is run, or after expansion), so
    the tables of several checkers are merged here.
    """

    resolved: dict[int, VarInfo] = field(default_factory=dict)  # Name -> what it refers to
    kinds: dict[int, str] = field(default_factory=dict)  # NamePattern -> "binding" or "variant"
    quote_levels: dict[int, int] = field(default_factory=dict)  # Quote -> the stage of its body
    type_values: dict[int, Type] = field(default_factory=dict)  # `type T` -> T

    @staticmethod
    def sharing(checker: Checker) -> Tables:
        """The tables of a checker that is still checking: they fill in as it goes."""
        return Tables(checker.resolved, checker.kinds, checker.quote_levels, checker.type_values)

    def add(self, checker: Checker) -> Tables:
        self.resolved.update(checker.resolved)
        self.kinds.update(checker.kinds)
        self.quote_levels.update(checker.quote_levels)
        self.type_values.update(checker.type_values)
        return self


# Building code


def build_code(
    quote: Quote,
    tables: Tables,
    splice: Callable[[AnySplice], object],
    persist: Callable[[Name], Expr],
    outer: Sequence[dict[int, int]] = (),
) -> Expr:
    """The code a `quote` gives: its body, with fresh binder identities, constants and spliced code.

    `splice` computes what each splice of this quote inserts (code, a type
    expression, or a field name), and `persist` the constant for each
    variable from an earlier stage, in the order they appear. The IR lowering uses the same walk to find them. `outer` are the
    binder identities of quotes this one is inside the splices of.
    """
    return start_code(quote, tables).build(splice, persist, outer)


def start_code(quote: Quote, tables: Tables) -> CodeBuilder:
    """Start building a quote's code by assigning fresh identities to its binders.

    The identities must be chosen before its splices run: a quote inside one of
    them may refer to the variables (`quote let y = 1 ... $(f(quote y))`).
    """
    level = tables.quote_levels.get(id(quote))
    if level is None:
        raise EllaError("code made while compiling can't build more code while compiling", quote.span)
    builder = CodeBuilder(quote, tables, level)
    builder.collect(quote.body)
    return builder


class CodeBuilder:
    def __init__(self, quote: Quote, tables: Tables, level: int) -> None:
        self.quote = quote
        self.tables = tables
        self.level = level  # the stage of the quote's body
        self.symbols: dict[int, int] = {}  # binder node -> its hygienic identity
        self.outer: Sequence[dict[int, int]] = ()
        self.splice: Callable[[AnySplice], object] = lambda node: node
        self.persist: Callable[[Name], Expr] = lambda node: node

    def build(
        self,
        splice: Callable[[AnySplice], object],
        persist: Callable[[Name], Expr],
        outer: Sequence[dict[int, int]] = (),
    ) -> Expr:
        self.splice, self.persist, self.outer = splice, persist, outer
        return self.walk_expr(self.quote.body, 1)

    def fresh(self, binder: object) -> int | None:
        """The identity of a variable bound inside this quote, or a quote it is spliced into."""
        for symbols in (self.symbols, *self.outer):
            if id(binder) in symbols:
                return symbols[id(binder)]
        return None

    def collect(self, node: object) -> None:
        """Give every variable bound inside the quote a fresh identity.

        The alternatives of an or-pattern bind the same names, and get the same identity.
        """
        match node:
            case OrPattern(alternatives):
                names: dict[str, int] = {}
                for binder in (b for alternative in alternatives for b in binders(alternative, self.tables)):
                    identity = names.get(binder.name)
                    if identity is None:
                        identity = next(_hygiene_ids)
                        names[binder.name] = identity
                    self.symbols[id(binder)] = identity
                for alternative in alternatives:
                    self.collect_parts(alternative)
                return
            case Binding() | Param():
                self.symbols[id(node)] = next(_hygiene_ids)
            case NamePattern() if self.tables.kinds.get(id(node)) == "binding" and id(node) not in self.symbols:
                self.symbols[id(node)] = next(_hygiene_ids)
        self.collect_parts(node)

    def collect_parts(self, node: object) -> None:
        for part in parts(node):
            self.collect(part)

    def walk(self, node: object, depth: int) -> object:
        """Rebuild a node of the quote's body; `depth` is how many quotes deep it is (1 for the body)."""
        match node:
            case Splice(code, span) | TypeSplice(code, span):
                if depth == 1:
                    return self.splice(node)
                return replace(node, code=self.walk_expr(code, depth - 1))
            case FieldSplice(target, code, span):
                # The target comes before the name, as it is written.
                target = self.walk_expr(target, depth)
                if depth == 1:
                    name = self.splice(node)
                    assert isinstance(name, str)
                    return Member(target, name, span)
                return FieldSplice(target, self.walk_expr(code, depth - 1), span)
            case Quote(body, span):
                return Quote(self.walk_expr(body, depth + 1), span)
            case TypeLit(texpr, span):
                # `type ...` is like a quote of a type: `$t` in it is a splice one stage up.
                walked = self.walk(texpr, depth + 1)
                return node if walked is texpr else TypeLit(walked, span)  # type: ignore[arg-type]
            case Name(_, span):
                info = self.tables.resolved.get(id(node))
                if info is None:
                    return node
                fresh = self.fresh(info.binder)
                if fresh is not None:
                    return replace(node, hygiene=fresh)
                if info.kind == "local" and info.level is not None and info.level < self.level:
                    return self.persist(node)
                return node
            case Binding() | Param() | NamePattern() if id(node) in self.symbols:
                # Mark its identity, and walk its type, which may have splices too.
                marked = replace(node, hygiene=self.symbols[id(node)])
                return rebuild(marked, lambda part: self.walk(part, depth))
        return rebuild(node, lambda part: self.walk(part, depth))

    def walk_expr(self, node: Expr, depth: int) -> Expr:
        result = self.walk(node, depth)
        assert not isinstance(result, tuple | str)
        return result  # type: ignore[return-value]


def binders(pattern: object, tables: Tables) -> list[NamePattern]:
    """The variables a pattern binds."""
    if isinstance(pattern, NamePattern):
        return [pattern] if tables.kinds.get(id(pattern)) == "binding" else []
    return [b for part in parts(pattern) for b in binders(part, tables)]


@overload
def materialize_hygiene(node: Expr, reserved: Iterable[str] = ()) -> Expr: ...


@overload
def materialize_hygiene(node: Program, reserved: Iterable[str] = ()) -> Program: ...


def materialize_hygiene(node: object, reserved: Iterable[str] = ()) -> object:
    """Give hygienic identifiers collision-free names for the existing checker and runtime."""
    occupied = set(reserved)
    identities: set[int] = set()

    def collect(current: object) -> None:
        if isinstance(current, Name | Binding | Param | NamePattern):
            occupied.add(current.name)
            if current.hygiene is not None:
                identities.add(current.hygiene)
        for part in parts(current):
            collect(part)

    collect(node)
    names: dict[int, str] = {}
    for identity in sorted(identities):
        candidate = f"__ella_hygiene_{identity}"
        while candidate in occupied:
            candidate += "_"
        names[identity] = candidate
        occupied.add(candidate)

    def rewrite(current: object) -> object:
        if isinstance(current, Name | Binding | Param | NamePattern) and current.hygiene is not None:
            return replace(current, name=names[current.hygiene], hygiene=None)
        return rebuild(current, rewrite)

    return rewrite(node)


def reify(value: Value, span: Span) -> Expr:
    """The code for a value carried into a later stage: a literal, or a constructor call."""
    match value:
        case bool():
            return BoolLit(value, span)
        case int():
            return IntLit(value, span)
        case float():
            return FloatLit(value, span)
        case str():
            return StringLit((value,), span)
        case () if isinstance(value, tuple):
            return UnitLit(span)
        case tuple():
            return TupleLit(tuple(reify(item, span) for item in value), span)
        case list():
            return ListLit(tuple(reify(item, span) for item in value), span)
        case range():
            return Binary("..", IntLit(value.start, span), IntLit(value.stop, span), span)
        case VariantValue(fields=[message, VariantValue(fields=cause), _], error=True):
            # A built-in Error: Error(message) or Error(message, cause), made (`at`) where the code carries it.
            return Call(Name("Error", span), tuple(reify(item, span) for item in [message, *cause]), span)
        case VariantValue(variant=variant, fields=values):
            if not values:
                return Name(variant, span)
            return Call(Name(variant, span), tuple(reify(item, span) for item in values), span)
        case CodeValue(expr):
            return Quote(expr, span)
        case TypeValue(t):
            return TypeLit(reify_type(t, span), span)
    raise EllaError(f"a value like this can't be carried into a later stage: {value!r}", span)


def reify_type(t: Type, span: Span) -> TypeExpr:
    """A type, written as a type annotation, to splice it into code."""
    match finish(t):
        case PrimTy("()"):
            return UnitType(span)
        case PrimTy(name):
            return NamedType(name, (), span)
        case ListTy(item):
            return ListType(reify_type(item, span), span)
        case TupleTy(items):
            return TupleType(tuple(reify_type(item, span) for item in items), span)
        case FuncTy(params, result, mutable):
            return FunctionType(tuple(reify_type(p, span) for p in params), reify_type(result, span), span, mutable)
        case EnumTy(name, args) | BuiltinTy(name, args):
            return NamedType(name, tuple(reify_type(arg, span) for arg in args), span)
        case ParamTy(name):
            return NamedType(name, (), span)
    raise EllaError(f"the type {show_type(t)} is only known once the code is built, so it can't be spliced", span)


def splice_value(node: AnySplice, value: Value) -> object:
    """What a splice inserts, given the value its expression computed: code, a type, or a field name."""
    match node:
        case TypeSplice():
            assert isinstance(value, TypeValue)
            return reify_type(value.type, node.span)
        case FieldSplice():
            assert isinstance(value, str)
            return value
    if isinstance(value, TypeValue):  # `$(t)(...)`: the constructor of a struct type
        return constructor(value.type, node.span)
    assert isinstance(value, CodeValue)
    return value.expr


def constructor(t: Type, span: Span) -> Name:
    """The name that makes values of a struct type, for `$(t)(...)`."""
    t = finish(t)
    if not isinstance(t, EnumTy) or t.args:
        raise EllaError(f"only a struct type that isn't generic can be called, not {show_type(t)}", span)
    return Name(t.name, span)


def struct_definition(info: EnumInfo, span: Span) -> EnumDef:
    """The definition of a struct that compile-time code made, to put in the expanded program."""
    fields = tuple(reify_type(t, span) for t in info.variants[info.name])
    return EnumDef(info.name, (), (Variant(info.name, fields, span),), span, True, info.field_names)


def type_from(texpr: TypeExpr, enums: dict[str, EnumInfo], splice: Callable[[TypeSplice], Value]) -> Type:
    """The type a `type ...` with splices in it gives, when it runs: `type [$t]` is a list of t's type."""

    def fill(node: object) -> object:
        if isinstance(node, TypeSplice):
            value = splice(node)
            assert isinstance(value, TypeValue)
            return reify_type(value.type, node.span)
        return rebuild(node, fill)

    resolver = Checker()
    resolver.enums = enums
    return resolver.annotation(fill(texpr))  # type: ignore[arg-type]


# Reflection: looking at types, for code that generates code


def reflect(name: str, args: list[Value], enums: dict[str, EnumInfo], span: Span) -> Value:
    """The reflection built-ins (see ella.checker.REFLECTION):

    * `fields(t)`: a struct's field names, in order
    * `field_type(t, name)`: the type of one of them
    * `has_field(t, name)`
    * `type_name(t)`: the type as it is written, like `Pair[i64]`
    * `type_kind(t)`: "i64", "f64", "bool", "str", "()", "range", "list",
      "tuple", "function", "struct", "enum", "pool", "handle", "arena", "ref",
      "code" or "type"
    * `variants(t)`: an enum's variants, in order
    """
    if name == "new_struct":
        return new_struct(args, enums, span)
    value = args[0]
    assert isinstance(value, TypeValue)
    t = finish(value.type)
    match name:
        case "type_name":
            return show_type(t)
        case "type_kind":
            return type_kind(t, enums)
        case "variants":
            if not isinstance(t, EnumTy) or enums[t.name].struct:
                raise EllaError(f"{show_type(t)} isn't an enum, so it has no variants", span)
            return list(enums[t.name].variants)
    if not isinstance(t, EnumTy) or not enums[t.name].struct:
        raise EllaError(f"{show_type(t)} isn't a struct, so it has no fields", span)
    info = enums[t.name]
    match name:
        case "fields":
            return list(info.field_names)
        case "has_field":
            return args[1] in info.field_names
    field_name = args[1]
    assert isinstance(field_name, str)
    if field_name not in info.field_names:
        raise EllaError(f"{show_type(t)} has no field '{field_name}'", span)
    field_types = info.variants[t.name]
    mapping = dict(zip(info.params, t.args))
    return TypeValue(substitute(field_types[info.field_names.index(field_name)], mapping))


def new_struct(args: list[Value], enums: dict[str, EnumInfo], span: Span) -> Value:
    """`new_struct(name, fields, types)`: a struct type made by code, named `name`.

    The name is what makes a type the same type: making a struct with the
    name of one made before gives that one, if the fields are the same.
    """
    name, field_names, field_types = args
    assert isinstance(name, str) and isinstance(field_names, list) and isinstance(field_types, list)
    if len(field_names) != len(field_types) or len(set(field_names)) != len(field_names):
        raise EllaError("new_struct() needs one type for each field, and different field names", span)
    names = tuple(str(n) for n in field_names)
    types = tuple(finish(t.type) for t in field_types if isinstance(t, TypeValue))
    info = EnumInfo(name, (), {name: types}, names, struct=True, created=True)
    old = enums.get(name)
    if old is not None:
        if old.created and old.field_names == names and old.variants == info.variants:
            return TypeValue(EnumTy(name))
        raise EllaError(f"there is already a different type named {name}", span)
    enums[name] = info
    return TypeValue(EnumTy(name))


def type_kind(t: Type, enums: dict[str, EnumInfo]) -> str:
    match t:
        case PrimTy(name):
            return name
        case ListTy():
            return "list"
        case TupleTy():
            return "tuple"
        case FuncTy():
            return "function"
        case EnumTy(name):
            return "struct" if enums[name].struct else "enum"
        case BuiltinTy(name):
            return name.lower()
    return "unknown"


# Expanding splices outside quotes


def prepare(
    program: Program, out: TextIO | None = None, path: Path | None = None, packages: Sequence[Path] | None = None
) -> tuple[Program, Checker]:
    """Check a program, and expand its splices outside quotes. Returns the program to run, and its checker.

    A splice in a type runs while the program is being checked, since the
    checker needs the type; other splices run after checking. What the
    compile-time code prints goes to `out`. The program is first linked
    with the modules it uses (see ella.modules); `path` is its file, which
    its imports are found from, and then from the package path, `packages`
    (by default ELLA_PATH's). Raises EllaError.
    """
    program = link(program, path, packages)
    if not uses_staging(program):
        return program, check(program)
    checker = Checker()
    expander = Expander(program, checker, out)
    checker.check_program(program)
    if not has_splices(program):
        return program, checker
    expanded = materialize_hygiene(expander.expand_program(), checker.globals.vars)
    assert isinstance(expanded, Program)
    final = check(expanded)
    # The warnings are about the program as written, splices and all (a function only they call is used).
    final.warnings = checker.warnings
    return expanded, final


def uses_staging(node: object) -> bool:
    """Whether some code has a quote, a splice, or a type as a value."""
    stage_nodes = Quote | Splice | TypeSplice | FieldSplice | TypeLit
    return isinstance(node, stage_nodes) or any(uses_staging(part) for part in parts(node))


def has_splices(node: object, depth: int = 0) -> bool:
    """Whether some code has a splice outside quotes."""
    match node:
        case Splice(code) | TypeSplice(code):
            return depth == 0 or has_splices(code, depth - 1)
        case Quote(body) | TypeLit(body):
            return has_splices(body, depth + 1)
    return any(has_splices(part, depth) for part in parts(node))


class Expander:
    """Runs a program's splices outside quotes while compiling, and puts the code they give in their place.

    It is made before the program is checked, because the checker needs it
    to run splices in types (`checker.compile_time`). Before compile-time
    code calls a function, the function is checked, and its own splices
    expanded, if they haven't been yet.
    """

    def __init__(self, program: Program, checker: Checker, out: TextIO | None) -> None:
        from ella.interpreter import Interpreter  # the interpreter uses this module too

        self.program = program
        self.checker = checker
        self.tables = Tables.sharing(checker)
        self.expanded: dict[int, Lambda] = {}  # top-level function -> its expanded version
        self.expanding: set[int] = set()
        self.interpreter: Interpreter = Interpreter(out, checker, self.tables)
        self.interpreter.prepare_function = self.prepare_function
        # Top-level functions and methods, which compile-time code may call.
        self.top_level = {
            id(expr.value) for expr in program.body if isinstance(expr, Let) and isinstance(expr.value, Lambda)
        } | {id(method.function) for expr in program.body if isinstance(expr, EnumDef) for method in expr.methods}
        self.interpreter.declare_top_level(program)
        checker.compile_time = self.run_while_checking

    def run_while_checking(self, code: Expr) -> Type:
        """Run a splice's code for the checker, which needs the type it gives."""
        value = self.interpreter.run_compile_time(self.expand(code))
        assert isinstance(value, TypeValue)
        return value.type

    def prepare_function(self, function: Lambda) -> Lambda:
        """A function about to run while compiling: checked, and with its splices expanded."""
        self.checker.check_now(function)
        return self.expand_function(function)

    def expand_program(self) -> Program:
        """The program with its splices expanded, after the definitions of the structs compile-time code made."""
        body: list[Expr] = [
            struct_definition(info, self.program.span) for info in self.checker.enums.values() if info.created
        ]
        for expr in self.program.body:
            if isinstance(expr, Let) and isinstance(expr.value, Lambda):
                body.append(replace(expr, value=self.expand_function(expr.value)))
            elif isinstance(expr, EnumDef) and expr.methods:
                methods = tuple(
                    replace(m, function=self.expand_function(m.function)) if isinstance(m.function, Lambda) else m
                    for m in expr.methods
                )
                body.append(replace(expr, methods=methods))
            else:
                body.append(self.expand(expr))
        return replace(self.program, body=tuple(body))

    def expand_function(self, function: Lambda) -> Lambda:
        """A top-level function with its splices expanded: needed before it can run while compiling."""
        if id(function) not in self.top_level:
            return function
        if id(function) in self.expanded:
            return self.expanded[id(function)]
        if id(function) in self.expanding:
            raise EllaError("this function is called while compiling its own code", function.span)
        self.expanding.add(id(function))
        result = self.expand(function)
        assert isinstance(result, Lambda)
        self.expanded[id(function)] = result
        return result

    def expand(self, node: Expr, depth: int = 0) -> Expr:
        return self.expand_node(node, depth)  # type: ignore[return-value]

    def expand_node(self, node: object, depth: int) -> object:
        match node:
            case TypeSplice(code, span) if depth == 0:
                # Run while checking: the checker kept the type.
                return reify_type(self.checker.spliced_types[id(node)], span)
            case Splice(code, span) if depth == 0 and id(node) in self.checker.spliced_types:
                # `$(t)(...)`: a struct's constructor, from a type that was run while checking.
                t = self.checker.spliced_types[id(node)]
                assert isinstance(t, EnumTy)
                return Name(t.name, span)
            case TypeLit(texpr, span):
                expanded = self.expand_node(texpr, depth + 1)
                if expanded is texpr:
                    return node
                literal = TypeLit(expanded, span)  # type: ignore[arg-type]
                self.tables.type_values[id(literal)] = self.tables.type_values[id(node)]
                return literal
            case Splice(code, span) if depth == 0:
                # Its own splices run first: they generate the code that runs now.
                value = self.interpreter.run_compile_time(self.expand(code))
                assert isinstance(value, CodeValue)
                return value.expr
            case Splice(code, span):
                return Splice(self.expand(code, depth - 1), span)
            case Quote(body, span):
                quote = Quote(self.expand(body, depth + 1), span)
                if quote is not node and id(node) in self.tables.quote_levels:
                    self.tables.quote_levels[id(quote)] = self.tables.quote_levels[id(node)]
                return quote
        return rebuild(node, lambda part: self.expand_node(part, depth))
