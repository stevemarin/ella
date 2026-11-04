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

The first error normally stops the check. Editors want every error, and the
types of everything that could be checked, so they set `errors` to a list:
each step of `check_program` (defining an enum, checking a top-level line or
a function body, ...) then records its error and the check goes on. See
`attempt`.
"""

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import partial
from itertools import count

from ella.exhaustive import missing, redundant
from ella.lexer import EllaError, Span
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
    EnumDef,
    Expr,
    FieldSplice,
    FloatLit,
    For,
    FunctionType,
    If,
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
    VariantPattern,
    While,
    WildcardPattern,
    Yield,
    is_generator,
    mentions_return,
    parts,
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
    """`(T, U) -> R`. `mutable` holds the positions of `mut` parameters: `(mut [i64]) -> ()` has (0,)."""

    params: tuple[Type, ...]
    result: Type
    mutable: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EnumTy:
    """An enum type with its type arguments: `Shape`, `Optional[i64]`, `Result[f64, str]`."""

    name: str
    args: tuple[Type, ...] = ()


@dataclass(frozen=True, slots=True)
class BuiltinTy:
    """A built-in type: `Code[T]`, `Pool[T]`, `Handle[T]`, `Arena`, `Ref[T]`, `Gen[T]` or `Type[T]`."""

    name: str
    args: tuple[Type, ...]


# The built-in types, and how many type arguments each takes.
BUILTIN_TYPES = {"Code": 1, "Pool": 1, "Handle": 1, "Arena": 0, "Ref": 1, "Gen": 1, "Type": 1}
ARENA = BuiltinTy("Arena", ())


@dataclass(frozen=True, slots=True, eq=False)
class DeferredTy:
    """The type of a value in quoted code that is only known once the code is built.

    `v.$f` reads a field whose name is computed while building the code,
    so its type can't be known before. A deferred type fits anything, and
    allows any operation. That is safe because code is always checked again
    once it is built: when it is run, or after splices are expanded.

    It is equal to every type, so that the IR, where `Code[?]` and
    `Code[i64]` are the same opaque value, accepts it. (So test for it with
    isinstance, not ==.)
    """

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return 0


DEFERRED = DeferredTy()


@dataclass(frozen=True, slots=True)
class ParamTy:
    """A type parameter, like the `T` of `enum Optional[T]` or `let sum[T: Add]`.

    It is replaced by the actual type argument wherever the enum or function
    is used. A generic function's type parameter has `constraints`: what its
    body may do with values of it (CONSTRAINTS), with the ones they imply.
    """

    name: str
    constraints: frozenset[str] = frozenset()


# The constraints a generic function's type parameters can have
# (docs/grammar.md §9), and the constraints each one implies, itself included.
CONSTRAINTS = {
    "Eq": frozenset({"Eq"}),  # `==`, `!=`, `in`
    "Ord": frozenset({"Ord", "Eq"}),  # `<`, `<=`, `>`, `>=`
    "Add": frozenset({"Add"}),  # `+`: numbers, strings and lists
    "Num": frozenset({"Num", "Add", "Ord", "Eq"}),  # `+`, `-`, `*`, `/`, `**`, unary `-`: i64 and f64
}

# The constraint each operator needs of a type parameter.
NEEDS = {"+": "Add", "-": "Num", "*": "Num", "/": "Num", "**": "Num"}

# The methods that give a struct or enum an operator (docs/grammar.md §9):
# `a + b` calls `a.add(b)`, `-a` calls `a.neg()`, and `a < b` is `a.compare(b) < 0`.
OPERATOR_METHODS = {"+": "add", "-": "sub", "*": "mul", "/": "div"}


@dataclass(eq=False, slots=True)
class TyVar:
    """A type not known yet. Once unification decides it, `ref` points to the answer.

    `eq=False` gives identity comparison: two different unknowns are
    different even before either is decided.
    """

    id: int
    ref: Type | None = None


type Type = PrimTy | ListTy | TupleTy | FuncTy | EnumTy | BuiltinTy | ParamTy | TyVar | DeferredTy


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
        case FuncTy(params, result, mutable):
            return FuncTy(tuple(finish(param) for param in params), finish(result), mutable)
        case EnumTy(name, args):
            return EnumTy(name, tuple(finish(arg) for arg in args))
        case BuiltinTy(name, args):
            return BuiltinTy(name, tuple(finish(arg) for arg in args))
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
        case FuncTy(params, result, mutable):
            shown = (("mut " if i in mutable else "") + show_type(p) for i, p in enumerate(params))
            return "(" + ", ".join(shown) + f") -> {show_type(result)}"
        case EnumTy(name, args) | BuiltinTy(name, args) if args:
            return f"{name}[" + ", ".join(map(show_type, args)) + "]"
        case EnumTy(name) | BuiltinTy(name):
            return name
        case ParamTy(name):
            return name
        case TyVar(id):
            return f"?{id}"
        case DeferredTy():
            return "?"


def substitute(t: Type, params: dict[str, Type]) -> Type:
    """Replace the type parameters in `t`: `Some(T)` with T = i64 has a field of type i64."""
    match t:
        case ParamTy(name):
            return params[name]
        case ListTy(element):
            return ListTy(substitute(element, params))
        case TupleTy(items):
            return TupleTy(tuple(substitute(item, params) for item in items))
        case FuncTy(args, result, mutable):
            return FuncTy(tuple(substitute(arg, params) for arg in args), substitute(result, params), mutable)
        case EnumTy(name, args):
            return EnumTy(name, tuple(substitute(arg, params) for arg in args))
        case BuiltinTy(name, args):
            return BuiltinTy(name, tuple(substitute(arg, params) for arg in args))
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
        case EnumTy(name) | BuiltinTy(name):
            return name
        case ParamTy(name):
            return name
        case TyVar():
            return "unknown"
        case DeferredTy():
            return "?"


@dataclass(frozen=True, slots=True)
class EnumInfo:
    """An enum's definition: its type parameters and each variant's field types.

    A struct is an enum with one variant, named after it, and also has field
    names. Both can have methods: each is a function named `Type.method`
    (specialized like a generic function when the type has type
    parameters). A `nocopy` struct may have a finalizer, the IR function
    `finalize` (its `finalize` method), which is run on each value when it dies.
    """

    name: str
    params: tuple[str, ...]
    variants: dict[str, tuple[Type, ...]]
    field_names: tuple[str, ...] = ()
    struct: bool = False
    nocopy: bool = False
    finalize: str = ""
    created: bool = False  # made by compile-time code (`new_struct`), not defined in the program
    # The methods' types. Their type parameters are the enum's, with the
    # constraints in `constraints` (one tuple per type parameter, or none).
    methods: dict[str, FuncTy] = field(default_factory=dict)
    constraints: tuple[tuple[str, ...], ...] = ()


# `Self` in a constraint's methods: the type that meets it.
SELF = ParamTy("Self")

# The built-in enums, and the built-in struct `Error`. Variants' fields refer
# to the type parameters. An Error's `at` is where it was made (docs/grammar.md
# §9, "Errors"): its constructor takes only the message and the cause (see
# `error_constructor`), and its patterns may leave out the last fields.
BUILTIN_ENUMS = {
    "Result": EnumInfo("Result", ("T", "E"), {"Ok": (ParamTy("T"),), "Err": (ParamTy("E"),)}),
    "Optional": EnumInfo("Optional", ("T",), {"Some": (ParamTy("T"),), "None": ()}),
    "Error": EnumInfo(
        "Error",
        (),
        {"Error": (STR, EnumTy("Optional", (EnumTy("Error"),)), STR)},
        ("message", "cause", "at"),
        struct=True,
    ),
}


MAIN_TYPES = "main must have type () -> (), () -> i64, () -> Result[(), E] or () -> Result[i64, E]"


def is_builtin_error(enums: dict[str, EnumInfo], name: str) -> bool:
    """Is `name` the built-in struct Error (which a program may replace with its own)?"""
    return name == "Error" and enums.get(name) == BUILTIN_ENUMS["Error"]


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
    # The stage the variable belongs to: 0 for the program, 1 inside a quote,
    # -1 in a splice outside quotes, ... None for things usable at every
    # stage: top-level functions, types and built-ins.
    level: int | None = None
    binder: object = None  # the node that binds a local variable
    type_params: tuple[str, ...] = ()  # a generic function's
    constraints: tuple[tuple[str, ...], ...] = ()  # and theirs
    # A variable of a quote that a function is defined in a splice of, as the
    # function sees it. The IR can't reach it from inside the function yet.
    outer_quote: bool = False


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
    """A function body waiting to be checked, with the scope and stage it was created in.

    `inferred` says the function left out types, which checking its body may decide.
    """

    function: Lambda
    captured: Env
    type: FuncTy
    level: int
    type_params: dict[str, Type]
    quotes: int
    inferred: bool = False


# The library: built-in functions with fixed types (see ella.library). A few
# work on either number type: their T is a type parameter that must be Num.
# `hash` works on any type with `==`: its T must be Eq.
ERROR = EnumTy("Error")
NUM_T = ParamTy("T", CONSTRAINTS["Num"])
EQ_T = ParamTy("T", CONSTRAINTS["Eq"])
LIBRARY: dict[str, FuncTy] = {
    "to_f64": FuncTy((I64,), F64),
    "to_i64": FuncTy((F64,), I64),
    "parse_i64": FuncTy((STR,), EnumTy("Result", (I64, ERROR))),
    "parse_f64": FuncTy((STR,), EnumTy("Result", (F64, ERROR))),
    "read_line": FuncTy((), EnumTy("Optional", (STR,))),
    "read_file": FuncTy((STR,), EnumTy("Result", (STR, ERROR))),
    "args": FuncTy((), ListTy(STR)),
    "split": FuncTy((STR, STR), ListTy(STR)),
    "join": FuncTy((ListTy(STR), STR), STR),
    "find": FuncTy((STR, STR), EnumTy("Optional", (I64,))),
    "replace": FuncTy((STR, STR, STR), STR),
    "trim": FuncTy((STR,), STR),
    "chars": FuncTy((STR,), ListTy(STR)),
    "ord": FuncTy((STR,), I64),
    "chr": FuncTy((I64,), STR),
    "sqrt": FuncTy((F64,), F64),
    "floor": FuncTy((F64,), F64),
    "ceil": FuncTy((F64,), F64),
    "round": FuncTy((F64,), F64),
    "exp": FuncTy((F64,), F64),
    "ln": FuncTy((F64,), F64),
    "sin": FuncTy((F64,), F64),
    "cos": FuncTy((F64,), F64),
    "tan": FuncTy((F64,), F64),
    "asin": FuncTy((F64,), F64),
    "acos": FuncTy((F64,), F64),
    "atan": FuncTy((F64,), F64),
    "atan2": FuncTy((F64, F64), F64),
    "log10": FuncTy((F64,), F64),
    "log2": FuncTy((F64,), F64),
    "upper": FuncTy((STR,), STR),
    "lower": FuncTy((STR,), STR),
    "starts_with": FuncTy((STR, STR), BOOL),
    "ends_with": FuncTy((STR, STR), BOOL),
    "pad_left": FuncTy((STR, I64, STR), STR),
    "pad_right": FuncTy((STR, I64, STR), STR),
    "format_f64": FuncTy((F64, I64), STR),
    "hash": FuncTy((EQ_T,), I64),
    "env": FuncTy((STR,), EnumTy("Optional", (STR,))),
    "write_file": FuncTy((STR, STR), EnumTy("Result", (UNIT, ERROR))),
    "exit": FuncTy((I64,), NEVER),
    "abs": FuncTy((NUM_T,), NUM_T),
    "min": FuncTy((NUM_T, NUM_T), NUM_T),
    "max": FuncTy((NUM_T, NUM_T), NUM_T),
}
# The generic library functions, with the constraint on their T.
GENERIC_LIBRARY = {
    name: "Num" if NUM_T in t.params else "Eq" for name, t in LIBRARY.items() if NUM_T in t.params or EQ_T in t.params
}
NOT_VALUES = frozenset({"exit"})  # library functions that can only be called, since their result is never

BUILTINS = ("print", "eprint", "len", "run", "next", "pool_new", "arena_new", "alloc", "free", *LIBRARY)
WRAPPED = frozenset({"print", "eprint", "len"})  # other built-ins that are values (taking one argument)


def show_library(name: str) -> str:
    """A library function's type, for hover text: `(str) -> str`, or `[T: Num] (T) -> T`."""
    return (f"[T: {GENERIC_LIBRARY[name]}] " if name in GENERIC_LIBRARY else "") + show_type(LIBRARY[name])


# Built-ins that look at types, and make them, for code that generates code
# (docs/grammar.md §11), with the types of their arguments and results.
ANY_TYPE = BuiltinTy("Type", (DEFERRED,))  # `Type`: a type, which may only be known when the code runs
REFLECTION: dict[str, tuple[tuple[Type, ...], Type]] = {
    "fields": ((ANY_TYPE,), ListTy(PrimTy("str"))),
    "field_type": ((ANY_TYPE, PrimTy("str")), ANY_TYPE),
    "has_field": ((ANY_TYPE, PrimTy("str")), PrimTy("bool")),
    "type_name": ((ANY_TYPE,), PrimTy("str")),
    "type_kind": ((ANY_TYPE,), PrimTy("str")),
    "variants": ((ANY_TYPE,), ListTy(PrimTy("str"))),
    "new_struct": ((PrimTy("str"), ListTy(PrimTy("str")), ListTy(ANY_TYPE)), ANY_TYPE),
}


def check(program: Program) -> Checker:
    """Type check a program, returning the checker with its tables. Raises EllaError."""
    checker = Checker()
    checker.check_program(program)
    return checker


def check_code(code: Expr, program: Checker) -> Checker:
    """Type check code made by `quote`, to run it with `run`: an expression that may use the program's top-level names.

    Returns a new checker, with tables for the code's nodes. They start
    with the program's, since the code may use a generic function or a
    method that the program never did, which is then lowered for the first time.
    """
    checker = Checker()
    checker.enums = program.enums
    checker.globals = Env(None, dict(program.globals.vars))
    checker.constraints = program.constraints
    checker.method_functions = program.method_functions
    checker.predefined = program.predefined
    checker.module_names = program.module_names
    checker.enum_files = program.enum_files
    tables = (
        "types",
        "kinds",
        "resolved",
        "members",
        "methods",
        "instances",
        "type_values",
        "quote_levels",
        "wrappers",
        "updates",
    )
    for table in tables:
        setattr(checker, table, dict(getattr(program, table)))
    checker.checked = set(program.checked)
    checker.check(code, checker.globals)
    while checker.pending_bodies:
        checker.check_body(checker.pending_bodies.pop(0))
    checker.finish_checks()
    return checker


class Checker:
    def __init__(self) -> None:
        self.types: dict[int, Type] = {}
        self.kinds: dict[int, str] = {}
        self.enums: dict[str, EnumInfo] = dict(BUILTIN_ENUMS)
        self.counter = count(1)  # numbers for new type variables
        self.resolved: dict[int, VarInfo] = {}  # Name node -> what it refers to
        self.members: dict[int, int] = {}  # `.field` node -> the field's position
        self.quote_levels: dict[int, int] = {}  # `quote` node -> the stage of its body
        self.type_values: dict[int, Type] = {}  # `type T` node -> T
        # Splices in types outside quotes run while checking (docs/grammar.md §11):
        # `compile_time` runs one (see ella.staging.Expander), and `spliced_types`
        # keeps what each gave, by node (a type, or the struct `$(t)(...)` makes).
        self.compile_time: Callable[[Expr], Type] | None = None
        self.spliced_types: dict[int, Type] = {}
        self.checked: set[int] = set()  # functions whose bodies have been checked
        self.top_level: dict[int, Let] = {}  # top-level function -> its `let`
        self.quotes = 0  # how many quotes the code being checked is in (minus splices)
        self.env = Env(None)  # the scope of the expression being checked, for splices in its types
        self.instances: dict[int, tuple[Type, ...]] = {}  # use of a generic function -> its type arguments
        self.constraints: dict[str, dict[str, FuncTy]] = {}  # user-defined constraint -> its methods' types, with SELF
        self.method_functions: dict[tuple[str, str], Lambda] = {}  # (struct or enum, method) -> its function
        self.methods: dict[int, str] = {}  # `.name` node called as a method -> the method's name
        self.wrappers: dict[int, Lambda] = {}  # `print`, `len` or a constructor used as a value -> a lambda calling it
        self.updates: dict[int, Block] = {}  # `v[k] op= x` with v's `update` method -> the code that runs instead
        self.lines: set[int] = set()  # assignments that are lines of blocks, whose values aren't used
        self.defined: set[int] = set()  # top-level enums already defined
        self.item_sets: set[int] = set()  # `v[k]` places stored with v's `set` method
        self.level = 0  # the stage being checked
        self.type_params: dict[str, Type] = {}  # in a generic function's body: T -> ParamTy("T")
        # Checks that need final types, made once everything has been checked.
        self.later_stage_uses: list[tuple[str, Type, Span]] = []  # variables used in a later stage
        # Patterns that must match every value of a type: a match's cases, a let's or a for's pattern.
        self.coverage: list[tuple[str, list[Pattern], Type, Span]] = []
        self.matches: list[tuple[tuple[Case, ...], Type]] = []  # every match's cases, to find ones that never match
        # Problems that don't stop the program, such as a case that can never match.
        self.warnings: list[EllaError] = []
        # For warnings: local variables and parameters, to find the ones never used, the local
        # variables declared `mut` and those changed (by binder), and the top-level functions.
        self.declared: dict[int, Binding | NamePattern | Param] = {}
        self.mutable_vars: set[int] = set()
        self.changed_vars: set[int] = set()
        self.functions: dict[int, Let] = {}
        # Uses of generic functions: the function, its type arguments, the
        # constraints of each type parameter, and where.
        self.generic_uses: list[tuple[str, tuple[Type, ...], tuple[tuple[str, tuple[str, ...]], ...], Span]] = []

        self.globals = Env(None)
        self.builtins = {name: VarInfo(UNIT, False, "builtin") for name in (*BUILTINS, *REFLECTION)}
        self.globals.vars.update(self.builtins)
        self.env = self.globals
        for enum in BUILTIN_ENUMS.values():
            self.define_variants(enum, self.globals)
        # The built-ins and built-in variants, which code in a module other than the entry file always
        # sees by their names, even if the entry file defines the names (see `visible`).
        self.predefined = dict(self.globals.vars)
        # For private names: the name of the module of each file, as in spans (see ella.modules),
        # and the file each top-level struct or enum is defined in.
        self.module_names: dict[str, str] = {}
        self.enum_files: dict[str, str] = {}
        # Top-level names whose `let` hasn't been checked yet, for a clearer error.
        self.pending_globals: set[str] = set()

        self.return_type: Type | None = None  # the current function's result type
        # In a generator's body: the type of the values it yields, and how many quotes its body is in.
        self.yield_type: Type | None = None
        self.yield_quotes = 0
        self.generators: list[tuple[Type, Span]] = []  # each generator's item type, which mustn't be nocopy
        # How each `for` loops (by node): whether it calls `iter` first, and over what: "items" (a
        # list, str or range), "next" (an iterator, by its next method) or "gen" (a generator).
        self.iterations: dict[int, tuple[bool, str]] = {}
        self.loops: list[LoopInfo] = []  # the loops we are inside, innermost last
        self.pending_bodies: list[PendingBody] = []

        # For editors. With `errors` a list, errors are collected there rather
        # than raised (see `attempt`), and `failed` has the names whose
        # definitions had errors. `scopes` has the scope each name was looked
        # up in, for completing names.
        self.errors: list[EllaError] | None = None
        # Whether the entry file is looked at as a module of some program (by an editor), whose
        # unused public functions may be used by other modules, so they aren't reported.
        self.library = False
        self.failed: set[str] = set()
        self.scopes: dict[int, Env] = {}

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
        if isinstance(a, DeferredTy) or isinstance(b, DeferredTy):  # it is checked once the code is built
            return True
        match a, b:
            case ListTy(x), ListTy(y):
                return self.unify(x, y)
            case TupleTy(xs), TupleTy(ys):
                return len(xs) == len(ys) and all(self.unify(x, y) for x, y in zip(xs, ys))
            case FuncTy(xs, x, xm), FuncTy(ys, y, ym):
                return (
                    len(xs) == len(ys)
                    and xm == ym
                    and all(self.unify(p, q) for p, q in zip(xs, ys))
                    and self.unify(x, y)
                )
            case EnumTy(n, xs), EnumTy(m, ys):
                return n == m and all(self.unify(x, y) for x, y in zip(xs, ys))
            case BuiltinTy(n, xs), BuiltinTy(m, ys):
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
            case EnumTy(_, args) | BuiltinTy(_, args):
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
            if all(isinstance(resolve(t), BuiltinTy) and resolve(t).name == "Type" for t in (a, b)):  # type: ignore[union-attr]
                return ANY_TYPE  # types of different types, like the field types of a struct
            raise EllaError(f"{what} must have the same type, not {show_type(a)} and {show_type(b)}", span)
        return a

    def known(self, t: Type, span: Span) -> Type:
        """A type that must already be decided, such as an operand whose operator depends on it.

        A deferred type counts as known: it is checked once the code is built.
        A lambda that left out types may decide it, as in `let ys = map(xs,
        (n) => n * 2)` followed by `-ys[0]`, so the waiting bodies of such
        lambdas are checked first.
        """
        t = resolve(t)
        if isinstance(t, TyVar):
            for pending in [pending for pending in self.pending_bodies if pending.inferred]:
                if any(other is pending for other in self.pending_bodies):  # (checking one may check another)
                    self.pending_bodies = [other for other in self.pending_bodies if other is not pending]
                    self.check_body(pending)
            t = resolve(t)
        if isinstance(t, TyVar):
            raise EllaError("can't tell the type of this value yet; add a type annotation", span)
        return t

    # Names and annotations

    def visible(self, name: str, env: Env, span: Span) -> VarInfo | None:
        """What a name refers to in code at `span`, if anything.

        Linking (ella.modules) gave the top-level names of modules other
        than the entry file names with dots in them, and left only the
        built-ins bare in their code. So such code finds a bare top-level
        name only if it is predefined, and then always the built-in one.
        """
        info = env.lookup(name)
        if span.file and "." not in name and info is not None and info is self.globals.vars.get(name):
            return self.predefined.get(name)
        return info

    def private(self, name: str, owner: str, span: Span) -> None:
        """A private field or method (`_name`) of a type can only be used in the type's module, and those below it."""
        if not name.startswith("_"):
            return
        defined = self.module_names.get(self.enum_files.get(owner, ""), "")
        used = self.module_names.get(span.file, "")
        if used != defined and not used.startswith(defined + "."):
            raise EllaError(f"'{name}' is private to module '{defined}'", span)

    def lookup(self, name: str, env: Env, span: Span) -> VarInfo:
        info = self.visible(name, env, span)
        if info is not None:
            return info
        if name in self.pending_globals:
            if self.level < 0:
                raise EllaError(f"'{name}' belongs to a later stage, so it can't be used while compiling", span)
            raise EllaError(f"'{name}' is used before its 'let' has run", span)
        raise EllaError(f"undefined name '{name}'", span)

    def bind_name(
        self,
        env: Env,
        name: str,
        t: Type,
        mutable: bool,
        span: Span,
        binder: object = None,
        function: bool = False,
        type_params: tuple[str, ...] = (),
        constraints: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        """Add a variable. A top-level name keeps one type, even when bound again.

        Top-level functions can be used at every stage; other variables belong
        to the stage they are bound in.
        """
        if env is self.globals:
            self.pending_globals.discard(name)
            old = env.vars.get(name)
            if old is not None and old.kind == "global" and not self.unify(t, old.type):
                raise EllaError(
                    f"'{name}' is a top-level {show_type(old.type)}, and can't be bound again as {show_type(t)}",
                    span,
                )
            level = None if function and not mutable else 0
            env.vars[name] = VarInfo(
                t, mutable, "global", level=level, binder=binder, type_params=type_params, constraints=constraints
            )
        else:
            env.vars[name] = VarInfo(t, mutable, "local", level=self.level, binder=binder)
            if isinstance(binder, Binding | NamePattern):
                self.declared[id(binder)] = binder
                if mutable:
                    self.mutable_vars.add(id(binder))

    def annotation(self, texpr: TypeExpr, params: dict[str, Type] | None = None) -> Type:
        """The type a type annotation stands for. `params` are an enum's type parameters.

        Inside a generic function, its type parameters can be used too.
        """
        params = params if params is not None else self.type_params
        match texpr:
            case NamedType(name, (), span) if name in params:
                return params[name]
            case NamedType(name, (), span) if name in PRIMITIVE_NAMES:
                return PRIMITIVE_NAMES[name]
            case NamedType(name, args, span) if name in self.enums:
                enum = self.enums[name]
                if len(args) != len(enum.params):
                    raise EllaError(f"{name} takes {len(enum.params)} type arguments, not {len(args)}", span)
                return EnumTy(name, tuple(self.annotation(arg, params) for arg in args))
            case NamedType("Type", (), span):
                return ANY_TYPE
            case NamedType("Code", (), span):
                return BuiltinTy("Code", (DEFERRED,))  # code whose type is only known once it is built
            case TypeSplice(code, span) if self.quotes <= 0:
                # `$t` in a type outside quotes: run t while checking, to get the type.
                return self.splice_now(texpr, BuiltinTy("Type", (self.fresh(),)), "the spliced type")
            case TypeSplice(code, span):
                # `$t` in a type in quoted code: the type t holds, known to be T if t is a Type[T].
                inner: Type = self.fresh()
                code_type = self.at_stage(self.level - 1, code, self.env, -1)
                self.expect(code_type, BuiltinTy("Type", (inner,)), code.span, "the spliced type")
                return inner
            case NamedType(name, args, span) if name in BUILTIN_TYPES:
                if len(args) != BUILTIN_TYPES[name]:
                    count = BUILTIN_TYPES[name]
                    raise EllaError(
                        f"{name} takes {count} type argument{'' if count == 1 else 's'}, not {len(args)}", span
                    )
                return BuiltinTy(name, tuple(self.annotation(arg, params) for arg in args))
            case NamedType(name, _, span):
                raise EllaError(f"unknown type '{name}'", span)
            case ListType(element):
                return ListTy(self.annotation(element, params))
            case UnitType():
                return UNIT
            case TupleType(items):
                return TupleTy(tuple(self.annotation(item, params) for item in items))
            case FunctionType(args, result, _, mutable):
                return FuncTy(
                    tuple(self.annotation(arg, params) for arg in args), self.annotation(result, params), mutable
                )

    # Enums

    def declare_enum(self, enum: EnumDef) -> None:
        """Make an enum's name known, so types can refer to it before its variants are read."""
        self.enums[enum.name] = EnumInfo(enum.name, enum.type_params, {})

    def define_enum(self, enum: EnumDef, env: Env) -> None:
        """Read an enum's variants (or a struct's fields) and methods, and make the variants names in `env`."""
        if env is self.globals:
            self.defined.add(id(enum))
            self.enum_files[enum.name] = enum.span.file
        self.declare_enum(enum)
        params: dict[str, Type] = {name: ParamTy(name) for name in enum.type_params}
        if any(has_type_splice(f) for v in enum.variants for f in v.fields):
            raise EllaError("a type can't be spliced into a definition yet; use new_struct()", enum.span)
        variants = {v.name: tuple(self.annotation(f, params) for f in v.fields) for v in enum.variants}
        has_finalize = any(m.name == "finalize" for m in enum.methods)
        finalize = f"{enum.name}.finalize" if has_finalize else ""
        info = EnumInfo(
            enum.name,
            enum.type_params,
            variants,
            enum.field_names,
            enum.struct,
            enum.nocopy,
            finalize,
            constraints=enum.constraints,
        )
        self.enums[enum.name] = info
        if enum.struct and not enum.nocopy:
            for field_name, field_type in zip(enum.field_names, variants[enum.name]):
                if is_nocopy(field_type, self.enums):
                    raise EllaError(
                        f"field '{field_name}' can't be copied, so {enum.name} must be a nocopy struct too", enum.span
                    )
        if enum.nocopy and not enum.field_names:
            raise EllaError("a nocopy struct needs at least one field", enum.span)
        if has_finalize:
            if not enum.nocopy:
                raise EllaError("only a nocopy struct can have a finalize method", enum.span)
            if enum.type_params:
                raise EllaError("a struct with a finalize method can't be generic", enum.span)
        if enum.methods and env is not self.globals:
            raise EllaError("only a top-level struct or enum can have methods", enum.span)
        # The type parameters' constraints are checked even without methods, which use them.
        self.param_types(enum.type_params, enum.constraints, enum.span)
        methods = {method.name: self.define_method(enum, method) for method in enum.methods}
        if "get" in methods and "set" in methods:
            get, set_ = methods["get"], methods["set"]
            if set_.params[1:] != (get.params[1], get.result):
                raise EllaError(
                    f"set must take the key and value types get uses: ({show_type(get.params[1])}, "
                    f"{show_type(get.result)})",
                    enum.span,
                )
        if "eq" in methods and "hash" not in methods:
            raise EllaError(
                "a type with an eq method needs a hash method too, so that equal values hash the same", enum.span
            )
        if "update" in methods:
            if "get" not in methods or "set" not in methods:
                raise EllaError("a type with an update method needs get and set methods too", enum.span)
            get, update = methods["get"], methods["update"]
            if update.params[1:] != (get.params[1], FuncTy((get.result,), UNIT, (0,))):
                raise EllaError(
                    f"update must take the key type get uses, and a (mut {show_type(get.result)}) -> ()",
                    enum.span,
                )
        self.enums[enum.name] = dataclasses.replace(info, methods=methods)
        self.define_variants(self.enums[enum.name], env)

    def define_method(self, enum: EnumDef, method: Method) -> FuncTy:
        """A method's type. It is checked like a top-level generic function with the enum's type parameters."""
        function = method.function
        if method.name in enum.field_names:
            raise EllaError(f"'{method.name}' is already a field of {enum.name}", method.span)
        if not isinstance(function, Lambda):
            raise EllaError(
                f"a method must be a function, like '{method.name} = (x: {enum.name}) -> ... => ...'", function.span
            )
        t = self.check_lambda(function, self.globals, [], enum.type_params, enum.constraints)
        self.types[id(function)] = t
        self.method_functions[enum.name, method.name] = function
        params = self.param_types(enum.type_params, enum.constraints, enum.span)
        own = EnumTy(enum.name, tuple(params[name] for name in enum.type_params))
        if not t.params or t.params[0] != own:
            raise EllaError(
                f"a method's first parameter is the value it is called on, so it must be a {show_type(own)}",
                function.span,
            )
        # Methods with these names give operators and built-ins, so their types are fixed.
        binary = "(Self, T) -> R, without 'mut'"
        iterable = describe_kind(t.result) in ("list", "str", "range", "Gen") or isinstance(resolve(t.result), EnumTy)
        expected = {
            "finalize": ("(Self) -> ()", t == FuncTy((own,), UNIT)),
            "compare": ("(Self, Self) -> i64", t == FuncTy((own, own), I64)),
            "len": ("(Self) -> i64", t == FuncTy((own,), I64)),
            "text": ("(Self) -> str", t == FuncTy((own,), STR)),
            "eq": ("(Self, Self) -> bool", t == FuncTy((own, own), BOOL)),
            "hash": ("(Self) -> i64", t == FuncTy((own,), I64)),
            "neg": ("(Self) -> R, without 'mut'", len(t.params) == 1 and not t.mutable),
            **{name: (binary, len(t.params) == 2 and not t.mutable) for name in OPERATOR_METHODS.values()},
            "get": ("(Self, K) -> V, without 'mut'", len(t.params) == 2 and not t.mutable),
            "set": ("(mut Self, K, V) -> ()", len(t.params) == 3 and t.mutable == (0,) and t.result == UNIT),
            "update": (
                "(mut Self, K, (mut V) -> ()) -> ()",
                len(t.params) == 3
                and t.mutable == (0,)
                and t.result == UNIT
                and isinstance(change := resolve(t.params[2]), FuncTy)
                and len(change.params) == 1
                and change.mutable == (0,)
                and change.result == UNIT,
            ),
            "contains": (
                "(Self, T) -> bool, without 'mut'",
                len(t.params) == 2 and not t.mutable and t.result == BOOL,
            ),
            "iter": (
                "(Self) -> a list, str, range, iterator or Gen",
                len(t.params) == 1 and not t.mutable and iterable,
            ),
            "next": (
                "(mut Self) -> Optional[T]",
                len(t.params) == 1 and t.mutable == (0,) and describe_kind(t.result) == "Optional",
            ),
        }
        if method.name in expected and not expected[method.name][1]:
            what = expected[method.name][0].replace("Self", show_type(own))
            raise EllaError(f"the method '{method.name}' must have the type {what}", function.span)
        return t

    def define_constraint(self, constraint: ConstraintDef) -> None:
        """A user-defined constraint: the methods, with their types, that a type needs to meet it."""
        if constraint.name in CONSTRAINTS or constraint.name in self.constraints:
            raise EllaError(f"there is already a constraint named {constraint.name}", constraint.span)
        methods: dict[str, FuncTy] = {}
        for method in constraint.methods:
            assert method.type is not None
            t = self.annotation(method.type, {"Self": SELF})
            if not isinstance(t, FuncTy) or not t.params or t.params[0] != SELF:
                raise EllaError(
                    f"a constraint's method is a function taking Self first, like '{method.name}: (Self) -> i64'",
                    method.span,
                )
            methods[method.name] = t
        self.constraints[constraint.name] = methods

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
        self.module_names = dict(program.modules)
        self.warnings += program.warnings
        # Enums first, in two steps so that they can refer to each other.
        # Constraints come in between: their methods' types may use enums,
        # and enums' type parameters may have constraints.
        for expr in program.body:
            if isinstance(expr, EnumDef):
                self.declare_enum(expr)
        for expr in program.body:
            if isinstance(expr, ConstraintDef):
                self.attempt(expr, [expr.name], partial(self.define_constraint, expr))
        for expr in program.body:
            if isinstance(expr, EnumDef):
                self.attempt(expr, defined_names(expr), partial(self.define_enum, expr, self.globals))
        # Top-level functions exist from the start (docs/grammar.md §3), and
        # their types are known from their annotations.
        functions = [expr for expr in program.body if isinstance(expr, Let) and isinstance(expr.value, Lambda)]
        for let in functions:
            self.top_level[id(let.value)] = let
        # Functions whose types have splices go last: the splices may call the others.
        later = [expr for expr in program.body if expr in functions and has_type_splice(expr.value)]
        for expr in [e for e in program.body if not any(e is f for f in later)] + later:
            match expr:
                case Let(bindings, Lambda()):
                    self.attempt(expr, defined_names(expr), partial(self.declare_function, expr))
                case Let(bindings):
                    self.pending_globals.update(b.name for b in bindings if b.name not in self.globals.vars)
                case LetPattern(pattern):
                    names = (n.name for n in pattern_names(pattern))
                    self.pending_globals.update(name for name in names if name not in self.globals.vars)

        for expr in program.body:
            if not self.attempt(expr, defined_names(expr), partial(self.check, expr, self.globals)):
                # Give the names it defines unknown types, so that later lines can still be checked.
                self.bind_unknown(expr, self.globals)
        # Now every top-level name has its type, so function bodies can be checked.
        while self.pending_bodies:
            pending = self.pending_bodies.pop(0)
            self.attempt(pending.function, [], partial(self.check_body, pending))

        self.attempt(None, [], partial(self.check_main, program))
        self.attempt(None, [], self.finish_checks)
        if not self.errors:  # after an error, some uses may not have been found
            self.warn_unused()
        self.warnings.sort(key=lambda warning: (warning.span.file, warning.span.line, warning.span.column))
        if not self.errors:  # the copy rules need everything checked
            self.attempt(None, [], partial(check_copies, program.body, self))

    def warn_unused(self) -> None:
        """Warn about names that are never used, and `mut` variables that never change.

        Local variables and parameters count, unless their names start
        with `_`. So do top-level functions (other than `main`) of the entry
        file, which nothing imports, and the private ones (`_name`) of other
        modules. A function's uses in its own body don't count.
        """
        used = {id(info.binder) for info in self.resolved.values()}
        hint = "start its name with '_' if that is on purpose"
        for node in self.declared.values():
            if node.name.startswith("_"):
                continue
            what = "parameter " if isinstance(node, Param) else ""
            if id(node) not in used:
                self.warnings.append(EllaError(f"{what}'{node.name}' is never used; {hint}", node.span))
            elif id(node) in self.mutable_vars and id(node) not in self.changed_vars:
                self.warnings.append(EllaError(f"'{node.name}' never changes, so it doesn't need 'mut'", node.span))
        for let in self.functions.values():
            inside = {id(node) for node in nodes(let.value)}
            outside = {id(info.binder) for name, info in self.resolved.items() if name not in inside}
            for binding in let.bindings:
                name = binding.name.rpartition(".")[2]
                reported = (not binding.span.file and not self.library) or name.startswith("_")
                if id(binding) not in outside and binding.name != "main" and reported:
                    self.warnings.append(EllaError(f"function '{name}' is never used", binding.span))

    def declare_function(self, let: Let) -> None:
        """Give a top-level function's names its type, from its annotations, before anything is checked."""
        assert isinstance(let.value, Lambda)
        self.functions[id(let)] = let
        type_params, constraints = let.bindings[0].type_params, let.bindings[0].constraints
        t = self.lambda_type(let.value, type_params, constraints)
        for binding in let.bindings:
            self.bind_name(
                self.globals,
                binding.name,
                t,
                binding.mutable,
                binding.span,
                binding,
                function=True,
                type_params=type_params,
                constraints=constraints,
            )

    def check_main(self, program: Program) -> None:
        """`main` is `() -> ()`, or `() -> i64` to give the program's exit status, or gives a Result of either,
        whose Err stops the program with an error (docs/grammar.md §3)."""
        main = self.globals.vars.get("main")
        if main is None or main.kind != "global":
            return
        t = resolve(main.type)
        if isinstance(t, FuncTy) and not t.params:
            result = finish(t.result)
            if (
                isinstance(result, EnumTy)
                and result.name == "Result"
                and self.enums["Result"] == BUILTIN_ENUMS["Result"]
            ):
                result = result.args[0]
            if result in (I64, UNIT):
                return
        if not self.unify(main.type, FuncTy((), UNIT)):
            raise EllaError(f"{MAIN_TYPES}, not {show_type(main.type)}", program.span)

    def attempt(self, node: object, defines: Iterable[str], step: Callable[[], object]) -> bool:
        """Take one step of checking a program, about `node`, which defines the names `defines`. Did it work?

        Without `errors`, an error stops the check, as usual. With it, the
        error is recorded and the check goes on from the top level. An error
        in a step that uses a name whose definition failed isn't recorded,
        since it may only follow from that one.
        """
        if self.errors is None:
            step()
            return True
        try:
            step()
            return True
        except EllaError as error:
            self.record(error, node, defines)
            self.env, self.level, self.quotes, self.type_params = self.globals, 0, 0, {}
            self.return_type, self.loops = None, []
            return False

    def record(self, error: EllaError, node: object, defines: Iterable[str]) -> None:
        """Keep an error found while collecting them, unless `node` uses a name whose definition failed."""
        assert self.errors is not None
        if not names_used(node, inside_lambdas=True) & self.failed:
            self.errors.append(error)
        self.failed.update(defines)

    def check_line(self, line: Expr, env: Env) -> Type:
        """Check a line of a block. When collecting errors, a line with one has an unknown type, as do its names."""
        if self.errors is None:
            return self.check(line, env)
        outer = self.level, self.quotes, self.return_type, list(self.loops), self.type_params
        try:
            return self.check(line, env)
        except EllaError as error:
            self.level, self.quotes, self.return_type, self.loops, self.type_params = outer
            self.record(error, line, defined_names(line))
            self.bind_unknown(line, env)
            return self.fresh()

    def bind_unknown(self, line: Expr, env: Env) -> None:
        """After an error in a `let`, give the names it binds unknown types (at the top level, unless bound already)."""
        top = env is self.globals
        match line:
            case Let(bindings):
                for binding in bindings:
                    if not (top and binding.name in env.vars):
                        self.bind_name(env, binding.name, self.fresh(), binding.mutable, binding.span, binding)
            case LetPattern(pattern, mutable=mutable):
                for node in pattern_names(pattern):
                    info = env.lookup(node.name)
                    if not (info is not None and (top or info.kind == "variant")):
                        self.bind_name(env, node.name, self.fresh(), mutable or node.mutable, node.span, node)

    def finish_checks(self) -> None:
        """The checks that need every type decided."""
        variants = {node for node, kind in self.kinds.items() if kind == "variant"}
        for cases, t in self.matches:
            t = finish(t)
            if any_part(t, self.enums, lambda part: isinstance(part, DeferredTy)):
                continue
            for i in redundant([(c.pattern, c.guard is not None) for c in cases], t, self.constructors, variants):
                self.warnings.append(
                    EllaError(
                        "this case can never match: the cases before it match every value it does", cases[i].span
                    )
                )
        for what, patterns, t, span in self.coverage:
            t = finish(t)
            if any_part(t, self.enums, lambda part: isinstance(part, DeferredTy)):
                continue  # checked once the code is built
            example = missing(patterns, t, self.constructors, variants)
            if example is None:
                continue
            if what == "match" and example == "_":
                raise EllaError(f"'match' on {show_type(t)} needs a 'case _' to cover every value", span)
            if what == "match":
                raise EllaError(f"'match' doesn't cover every value: add a case for {example}", span)
            example = "every value" if example == "_" else example
            raise EllaError(
                f"'{what}' needs a pattern that matches every value, and this one doesn't match {example}", span
            )
        for item, span in self.generators:
            item = finish(item)
            if is_nocopy(item, self.enums):
                raise EllaError(f"a generator can't yield {show_type(item)} values, which can't be copied", span)
        for name, t, span in self.later_stage_uses:
            t = finish(t)
            if not persistable(t, self.enums):
                raise EllaError(
                    f"'{name}' is a {show_type(t)}, which can't be carried into a later stage (only data can)", span
                )
        for name, args, constraints, span in self.generic_uses:
            for arg, (param, needs) in zip(args, constraints):
                arg = finish(arg)
                if is_nocopy(arg, self.enums):
                    raise EllaError(f"{name} can't be used with {show_type(arg)}, which can't be copied", span)
                for constraint in needs:
                    reason = self.unmet(arg, constraint)
                    if reason is not None:
                        raise EllaError(
                            f"{name} needs {param} to be {constraint}, and {show_type(arg)} isn't{reason}", span
                        )

    def constructors(self, t: Type) -> list[tuple[str, tuple[Type, ...]]] | None:
        """For exhaustiveness: a type's constructors, with their fields' types, or None if it has no fixed set."""
        match t:
            case EnumTy(name) if name in self.enums:
                return [(variant, self.variant_fields(t, variant)) for variant in self.enums[name].variants]
            case TupleTy(items):
                return [("()", items)]
            case _ if t == BOOL:
                return [("true", ()), ("false", ())]
            case _ if t == UNIT:
                return [("()", ())]
        return None

    def unmet(self, t: Type, constraint: str) -> str | None:
        """Why a type doesn't meet a constraint (perhaps ""), or None if it does.

        A struct or enum meets a user-defined constraint if it has each of
        its methods, with the same types (with Self standing for the type).
        """
        t = finish(t)
        if constraint not in self.constraints or not isinstance(t, EnumTy):
            return None if satisfies(t, constraint, self.enums) else ""
        for name, required in self.constraints[constraint].items():
            wanted = substitute(required, {"Self": t})
            info = self.enums[t.name]
            if name not in info.methods:
                return f": it has no method '{name}'"
            method = finish(substitute(info.methods[name], dict(zip(info.params, t.args))))
            if method != wanted:
                return f": its method '{name}' is {show_type(method)}, not {show_type(wanted)}"
        return None

    # Expressions

    def check(self, expr: Expr, env: Env) -> Type:
        """Check an expression and return its type, recording it in `self.types`."""
        outer, self.env = self.env, env
        try:
            t = self.check_expr(expr, env)
        finally:
            self.env = outer
        self.types[id(expr)] = t
        return t

    def item_update(self, assign: Assign, env: Env) -> Block | None:
        """An assignment through an item of a type with an `update` method: the code that runs instead.

        `v[k] op= x`, or `v[k].f = x`, `v[k][j] op= x` and so on, changes the item in place: it is
        `let %value = x`, then `mut v.update(k, (mut %item) => %item... op= %value)`, so the item
        isn't copied out with `get` and back in with `set` (which can't store a part of one).
        `v[k] = x` still uses `set`, which can add a key. The first such item from the variable
        is used; the assignment in the lambda is changed the same way if it needs to be.

        None if there's no such item, or the assignment's value is used: only a line of a block
        is changed this way. The places are checked here to find their types, and again in that
        code, so they may not hold functions, whose second check would differ.
        """
        if has_functions(assign.target):
            return None
        steps: list[Index | Member] = []  # the place's steps, from the variable out
        place: Expr = assign.target
        while isinstance(place, Index | Member):
            steps.insert(0, place)
            place = place.target
        for i, step in enumerate(steps):
            last = i == len(steps) - 1
            if not isinstance(step, Index) or (last and assign.op == "="):
                continue
            if self.method_type(self.check(step.target, env), "update", assign.span) is None:
                continue
            span = assign.span
            # The rest of the place, from the item.
            rest: Name | Index | Member = Name("%item", span)
            for outer in steps[i + 1 :]:
                rest = dataclasses.replace(outer, target=rest)
            value = Binding("%value", False, None, span)
            item = Param("%item", None, span, mutable=True)
            body = Block((Assign(assign.op, rest, Name("%value", span), span),), span)
            change = Lambda((item,), None, (), body, span)
            receiver = Member(MutArg(step.target, step.span), "update", span)
            call = Call(receiver, (step.index, change), span)
            return Block((Let((value,), assign.value, span), call), span)
        return None

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
                self.scopes[id(expr)] = env
                info = self.lookup(name, env, span)
                self.kinds[id(expr)] = info.kind
                self.resolved[id(expr)] = info
                match info.kind:
                    case "builtin" if name in LIBRARY and name not in NOT_VALUES:
                        return self.library_type(name, span)
                    case "builtin" if name in WRAPPED:
                        return self.wrap(expr, 1, env)
                    case "constructor" if is_builtin_error(self.enums, info.enum):
                        return self.wrap(expr, 1, env)  # the message
                    case "constructor":
                        return self.wrap(expr, len(self.enums[info.enum].variants[name]), env)
                    case "builtin":
                        raise EllaError(f"'{name}' can only be called, not used as a value", span)
                    case "variant":
                        enum_type, _ = self.instantiate(info.enum, name)
                        return enum_type
                    case _:
                        self.check_stage(name, info, span)
                        if info.outer_quote:
                            raise EllaError(
                                f"'{name}' belongs to a quote outside this function, which a quote inside it "
                                "can't use yet",
                                span,
                            )
                        if info.type_params:
                            # Each use of a generic function gets its own type arguments.
                            type_args: dict[str, Type] = {param: self.fresh() for param in info.type_params}
                            self.instances[id(expr)] = tuple(type_args.values())
                            needs = info.constraints or tuple(() for _ in info.type_params)
                            self.generic_uses.append(
                                (name, tuple(type_args.values()), tuple(zip(info.type_params, needs)), span)
                            )
                            return substitute(info.type, type_args)
                        return info.type
            case Let(bindings, value_expr):
                type_params = bindings[0].type_params
                if type_params and (env is not self.globals or not isinstance(value_expr, Lambda)):
                    raise EllaError("only a top-level function can have type parameters", bindings[0].span)
                if isinstance(value_expr, Lambda):
                    # A `let`-bound function can call itself by name. (A generic
                    # function's own name is its top-level name.)
                    self_names = [] if type_params else list(bindings)
                    t: Type = self.check_lambda(
                        value_expr, env, self_names, type_params, bindings[0].constraints, env is not self.globals
                    )
                    self.types[id(value_expr)] = t
                else:
                    t = self.check(value_expr, env)
                for binding in bindings:
                    binding_type = t
                    if binding.type is not None:
                        binding_type = self.annotation(binding.type)
                        self.expect(t, binding_type, value_expr.span, f"the value of '{binding.name}'")
                    self.types[id(binding)] = binding_type
                    function = isinstance(value_expr, Lambda)
                    self.bind_name(
                        env,
                        binding.name,
                        binding_type,
                        binding.mutable,
                        binding.span,
                        binding,
                        function,
                        type_params,
                        bindings[0].constraints,
                    )
                return t
            case LetPattern(pattern, value_expr, span, mutable, type_expr):
                t = self.check(value_expr, env)
                if type_expr is not None:
                    declared = self.annotation(type_expr)
                    self.expect(t, declared, value_expr.span, "the value of the 'let'")
                    t = declared
                bound = Env(env)
                self.check_pattern(pattern, t, bound)
                self.coverage.append(("let", [pattern], t, span))
                for name, info in bound.vars.items():
                    assert isinstance(info.binder, NamePattern)
                    self.bind_name(env, name, info.type, mutable or info.mutable, info.binder.span, info.binder)
                return t
            case Assign(span=span) if id(expr) in self.lines and (update := self.item_update(expr, env)):
                self.updates[id(expr)] = update
                self.check(update, env)
                return UNIT  # its value isn't used
            case Assign(op, target, value_expr, span):
                value_type = self.check(value_expr, env)
                target_type = self.check_place(target, env)
                if op != "=" and isinstance(target, Index) and id(target) in self.item_sets:
                    # `v[k] += x` reads the item with `get`, then stores it with `set`.
                    getter = self.method_type(self.types[id(target.target)], "get", span)
                    if getter is None:
                        raise EllaError(f"'{op}' on an item needs a get method as well as set", span)
                    self.expect(getter.result, target_type, span, "the item get gives")
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
                if op == "-" and (method := self.operator_method(t, "neg", span)) is not None:
                    return method.result
                if isinstance(param := resolve(t), ParamTy) and op != "not":
                    self.require_constraint(param, "Num" if op == "-" else None, op, span)
                    return t
                kind = describe_kind(self.known(t, operand.span))
                if (op == "-" and kind in ("i64", "f64", "?")) or (op == "~" and kind in ("i64", "?")):
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
                if isinstance(container, DeferredTy):
                    return DEFERRED
                if (getter := self.method_type(container, "get", span)) is not None:
                    self.expect(index_type, getter.params[1], index.span, "the index")
                    return getter.result
                if isinstance(container, BuiltinTy) and container.name in ("Pool", "Arena"):
                    return self.handle_item(container, index_type, index.span)
                if resolve(index_type) == RANGE and (isinstance(container, ListTy) or container == STR):
                    return container  # a slice: `xs[1..3]`
                if not self.unify(index_type, I64):
                    raise EllaError(f"an index must be an i64, not {show_type(index_type)}", index.span)
                match container:
                    case ListTy(element):
                        return element
                    case PrimTy("str"):
                        return STR
                raise EllaError(f"can't index a {show_type(container)}", span)
            case Member(target, name, span):
                return self.member_type(expr, self.check(target, env))
            case Quote(body, span):
                # The body is code for the next stage: it can't return or break out of this one.
                self.quote_levels[id(expr)] = self.level + 1
                return BuiltinTy("Code", (self.at_stage(self.level + 1, body, env, +1),))
            case Splice(code, span):
                # The code is computed one stage earlier: while the quote is built,
                # or, outside quotes, while compiling.
                code_type = self.at_stage(self.level - 1, code, env, -1)
                inner: Type = self.fresh()
                self.expect(code_type, BuiltinTy("Code", (inner,)), code.span, "the spliced value")
                return inner
            case TypeLit(texpr, span):
                # Like a quote of a type: `$t` in it is a type value of this stage.
                outer = self.level, self.quotes
                self.level, self.quotes = self.level + 1, self.quotes + 1
                try:
                    t = self.annotation(texpr)
                finally:
                    self.level, self.quotes = outer
                self.type_values[id(expr)] = t
                return BuiltinTy("Type", (t,))
            case FieldSplice(target, code, span):
                # `v.$f`: a field named while the code is built, so its type is only known then.
                if self.quotes <= 0:
                    raise EllaError("a field name can only be spliced into quoted code", span)
                self.check(target, env)
                name_type = self.at_stage(self.level - 1, code, env, -1)
                self.expect(name_type, STR, code.span, "the spliced field name")
                return DEFERRED
            case MutArg(span=span):
                raise EllaError("'mut' marks an argument for a 'mut' parameter, and only works in a call", span)
            case Try(operand, span):
                return self.check_try(self.check(operand, env), span)

            # Functions
            case Lambda():
                return self.check_lambda(expr, env, [], infer=True)
            case Call(callee, args, span):
                return self.check_call(callee, args, env, span)
            case Return(value, span):
                if self.return_type is None:
                    raise EllaError("'return' outside of a function", span)
                if value is not None and self.yield_type is not None:
                    raise EllaError("a generator's 'return' can't have a value: it just ends the generator", span)
                value_type = UNIT if value is None else self.check(value, env)
                self.expect(value_type, self.return_type, span, "the returned value")
                return NEVER

            case Yield(value, span):
                if self.yield_type is None:
                    raise EllaError("'yield' can only be used in a function, which it makes a generator", span)
                if self.quotes != self.yield_quotes:
                    raise EllaError("'yield' can't be used in a quote", span)
                self.expect(self.check(value, env), self.yield_type, value.span, "the value given to 'yield'")
                return UNIT

            # Control flow
            case Block(exprs):
                local = Env(env)
                self.lines.update(id(line) for line in exprs if isinstance(line, Assign))
                t = UNIT
                finished = True  # whether the lines so far can finish, so that the next one runs
                for line in exprs:
                    if finished and resolve(t) is NEVER:
                        finished = False
                        message = "this code never runs: the line before it never finishes"
                        self.warnings.append(EllaError(message, start_of(line)))
                    t = self.check_line(line, local)
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
                unguarded = [case.pattern for case in cases if case.guard is None]
                self.coverage.append(("match", unguarded, subject_type, span))
                self.matches.append((cases, subject_type))
                return match_type
            case For(label, pattern, iterable, body, span):
                item_type = self.loop_item(expr, iterable, env, span)
                loop_env = Env(env)
                self.check_pattern(pattern, item_type, loop_env)
                self.coverage.append(("for", [pattern], item_type, span))
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
                if id(expr) not in self.defined:  # top-level ones are defined before the program is checked
                    self.define_enum(expr, env)
                return UNIT
            case ConstraintDef(name, span=span):
                if env is not self.globals or name not in self.constraints:
                    raise EllaError("a constraint can only be defined at the top level", span)
                return UNIT
            case MapLit(span=span) | SetLit(span=span):
                # Linking (ella.modules) makes them calls of the library's map_of and set_of.
                raise EllaError("a map or set literal in a program that wasn't linked", span)

    # Stages

    def at_stage(self, level: int, expr: Expr, env: Env, quotes: int = 0) -> Type:
        """Check an expression of another stage, outside this stage's functions and loops.

        `quotes` is +1 going into a quote and -1 going into a splice.
        """
        outer = self.level, self.return_type, self.loops, self.quotes
        self.level, self.return_type, self.loops = level, None, []
        self.quotes += quotes
        try:
            return self.check(expr, env)
        finally:
            self.level, self.return_type, self.loops, self.quotes = outer

    def check_stage(self, name: str, info: VarInfo, span: Span) -> None:
        """A variable can be used at its own stage, or carried into a later one as data."""
        if info.level is None or info.level == self.level:
            return
        if info.level > self.level:
            where = "while compiling" if self.level < 0 else "outside the quote it is bound in"
            raise EllaError(f"'{name}' belongs to a later stage, so it can't be used {where}", span)
        self.later_stage_uses.append((name, info.type, span))

    def splice_now(self, node: TypeSplice | Splice, expected: Type, what: str) -> Type:
        """Run a splice of a type outside quotes while checking, and give the type it computes.

        Its code is stage -1: it runs before the program, and can't use the
        program's variables. The result is kept, so the splice runs once.
        """
        if id(node) in self.spliced_types:
            return self.spliced_types[id(node)]
        code_type = self.at_stage(self.level - 1, node.code, self.env, -1)
        self.expect(code_type, expected, node.code.span, what)
        if self.compile_time is None:
            raise EllaError("a type can only be spliced into a program while compiling it", node.span)
        t = finish(self.compile_time(node.code))
        if isinstance(t, DeferredTy):
            raise EllaError("this type is only known once its code is built", node.span)
        self.spliced_types[id(node)] = t
        return t

    def check_now(self, function: Lambda) -> None:
        """Check a function's body now, because compile-time code is about to run it."""
        if id(function) in self.checked:
            return
        for pending in self.pending_bodies:
            if pending.function is function:
                self.pending_bodies.remove(pending)
                self.check_body(pending)
                return
        let = self.top_level.get(id(function))
        if let is None:
            return  # a function made at run time of the compile-time code: already checked
        self_names = [] if let.bindings[0].type_params else list(let.bindings)
        self.check_lambda(function, self.globals, self_names, let.bindings[0].type_params, let.bindings[0].constraints)
        self.check_now(function)

    # Structs

    def member_type(self, expr: Member, t: Type) -> Type:
        """The type of `value.field`, recording which field it is."""
        struct = self.known(t, expr.target.span)
        if isinstance(struct, DeferredTy):
            return DEFERRED
        if isinstance(struct, TupleTy) and expr.name.isdigit():
            index = int(expr.name)
            if index >= len(struct.items):
                raise EllaError(f"a tuple of {len(struct.items)} items has no item {index}", expr.span)
            self.members[id(expr)] = index
            return struct.items[index]
        if isinstance(struct, EnumTy) and self.enums[struct.name].struct:
            info = self.enums[struct.name]
            if expr.name in info.field_names:
                self.private(expr.name, struct.name, expr.span)
                index = info.field_names.index(expr.name)
                self.members[id(expr)] = index
                return self.variant_fields(struct, struct.name)[index]
            raise EllaError(f"{struct.name} has no field '{expr.name}'", expr.span)
        raise EllaError(f"a {show_type(struct)} has no fields", expr.span)

    # Operators

    def binary_type(self, op: str, a: Type, b: Type, span: Span) -> Type:
        """The result type of an arithmetic, bitwise or range operator."""
        if op in OPERATOR_METHODS and (method := self.operator_method(a, OPERATOR_METHODS[op], span)) is not None:
            self.expect(b, method.params[1], span, f"the right side of '{op}'")
            return method.result
        if not self.unify(a, b):
            raise EllaError(f"'{op}' needs two values of the same type, not {show_type(a)} and {show_type(b)}", span)
        if isinstance(resolve(a), DeferredTy) or isinstance(resolve(b), DeferredTy):
            return DEFERRED if op not in ("/", "..", "..=") else (F64 if op == "/" else RANGE)
        if isinstance(param := resolve(a), ParamTy):
            self.require_constraint(param, NEEDS.get(op), op, span)
            return F64 if op == "/" else a
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
        if isinstance(resolve(a), DeferredTy) or isinstance(resolve(b), DeferredTy):
            return
        if op in ("in", "not in"):
            if (method := self.method_type(self.known(b, span), "contains", span)) is not None:
                self.expect(a, method.params[1], span, f"the value looked for with '{op}'")
                return
            match self.known(b, span):
                case ListTy(element):
                    self.expect(a, element, span, f"the value looked for with '{op}'")
                    if isinstance(param := resolve(element), ParamTy):
                        self.require_constraint(param, "Eq", op, span)
                case PrimTy("str"):
                    self.expect(a, STR, span, f"the value looked for with '{op}'")
                case PrimTy("range"):
                    self.expect(a, I64, span, f"the value looked for with '{op}'")
                case other:
                    raise EllaError(f"'in' needs a list, str or range on the right, not {show_type(other)}", span)
            return
        if not self.unify(a, b):
            raise EllaError(f"'{op}' needs two values of the same type, not {show_type(a)} and {show_type(b)}", span)
        if isinstance(param := resolve(a), ParamTy):
            self.require_constraint(param, "Eq" if op in ("==", "!=") else "Ord", op, span)
            return
        if op not in ("==", "!=") and self.operator_method(a, "compare", span) is not None:
            return
        kind = describe_kind(self.known(a, span))
        if kind in ("function", "Pool", "Arena", "Gen", "Code"):
            raise EllaError(
                f"{'functions' if kind == 'function' else show_type(a) + ' values'} can't be compared", span
            )
        if op not in ("==", "!=") and kind not in ("i64", "f64", "str"):
            raise EllaError(f"'{op}' doesn't work on {show_type(a)}", span)

    def require_constraint(self, param: ParamTy, constraint: str | None, op: str, span: Span) -> None:
        """An operator on a value of a type parameter: the parameter needs the constraint that allows it."""
        if constraint is None:
            raise EllaError(f"'{op}' doesn't work on {param.name}", span)
        if constraint not in param.constraints:
            raise EllaError(
                f"'{op}' on {param.name} needs the constraint {constraint}, like [{param.name}: {constraint}]", span
            )

    def check_try(self, t: Type, span: Span) -> Type:
        """Postfix `?`: the function must return the same kind of enum, so the error can pass through."""
        if self.return_type is None:
            raise EllaError("'?' can only be used inside a function", span)
        if isinstance(resolve(t), DeferredTy):
            return DEFERRED
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

    def check_place(self, target: Expr, env: Env, verb: str = "assign to", last: bool = True) -> Type:
        """The type of something being assigned to (or passed as `mut`): a `mut` variable, or an element of one.

        `last` says whether `target` is the whole place, rather than a part
        of a longer one: only the last step can store an item with a `set` method.
        """
        match target:
            case Name(name, span):
                self.scopes[id(target)] = env
                info = self.lookup(name, env, span)
                self.kinds[id(target)] = info.kind
                self.resolved[id(target)] = info
                if info.kind not in ("local", "global") or not info.mutable:
                    action = f"pass '{name}' as 'mut'" if verb == "pass" else f"assign to '{name}'"
                    raise EllaError(f"can't {action} because it isn't mutable; declare it with 'let mut'", span)
                self.check_stage(name, info, span)
                if info.level is not None and info.level != self.level:
                    raise EllaError(f"can't change '{name}' from a later stage", span)
                t = info.type
                self.changed_vars.add(id(info.binder))
            case Index(container_expr, index, span):
                container = self.known(self.check_place(container_expr, env, verb, False), container_expr.span)
                index_type = self.check(index, env)
                setter = self.method_type(container, "set", span)
                if setter is not None or self.method_type(container, "get", span) is not None:
                    if setter is None or not last or verb != "assign to":
                        raise EllaError(
                            f"an item of a {show_type(container)} is a copy, so it can't be changed in place; "
                            "only a set method can store one, with 'v[k] = x'",
                            span,
                        )
                    self.expect(index_type, setter.params[1], index.span, "the index")
                    self.item_sets.add(id(target))
                    t = setter.params[2]
                elif isinstance(container, BuiltinTy) and container.name in ("Pool", "Arena"):
                    t = self.handle_item(container, index_type, index.span)
                else:
                    if resolve(index_type) == RANGE:
                        raise EllaError(f"can't {verb} a slice; change the items one by one", span)
                    if not self.unify(index_type, I64):
                        raise EllaError(f"an index must be an i64, not {show_type(index_type)}", index.span)
                    match container:
                        case ListTy(element):
                            t = element
                        case _:
                            raise EllaError(f"can't {verb} an element of a {show_type(container)}", span)
            case Member(container_expr, member, span):
                container = self.check_place(container_expr, env, verb, False)
                if isinstance(resolve(container), TupleTy):
                    raise EllaError(f"a tuple can't be changed in place, so you can't {verb} its items", span)
                if member == "at" and resolve(container) == ERROR and is_builtin_error(self.enums, "Error"):
                    raise EllaError(f"an Error's 'at' is where it was made, so you can't {verb} it", span)
                t = self.member_type(target, container)
            case _:
                raise EllaError(f"can only {verb} a variable or a part of one", target.span)
        self.types[id(target)] = t
        return t

    # Functions

    def param_types(
        self, type_params: tuple[str, ...], constraints: tuple[tuple[str, ...], ...], span: Span
    ) -> dict[str, Type]:
        """A generic function's type parameters, as types, with what their constraints imply."""
        result: dict[str, Type] = {}
        for i, name in enumerate(type_params):
            implied: frozenset[str] = frozenset()
            for constraint in constraints[i] if constraints else ():
                if constraint in self.constraints:
                    implied |= {constraint}
                    continue
                if constraint not in CONSTRAINTS:
                    known = ", ".join([*CONSTRAINTS, *self.constraints])
                    raise EllaError(f"unknown constraint '{constraint}'; the constraints are {known}", span)
                implied |= CONSTRAINTS[constraint]
            result[name] = ParamTy(name, implied)
        return result

    def lambda_type(
        self,
        function: Lambda,
        type_params: tuple[str, ...] = (),
        constraints: tuple[tuple[str, ...], ...] = (),
        infer: bool = False,
    ) -> FuncTy:
        """A function's type, from its annotations.

        A generic function's type parameters stand for themselves: `T` is
        ParamTy("T"), which only matches itself until a use fills it in.

        With `infer`, the types left out are type variables, which the
        lambda's use decides (passing it to `map` makes its parameter the
        list's item type), or failing that its body, checked later. Without,
        they are required: a top-level function's type, and a method's, are
        known before anything is checked.
        """
        outer = self.type_params
        self.type_params = outer | self.param_types(type_params, constraints, function.span)
        try:
            params: list[Type] = []
            for param in function.params:
                if param.type is not None:
                    params.append(self.annotation(param.type))
                elif infer:
                    params.append(self.fresh())
                else:
                    raise EllaError(
                        f"parameter '{param.name}' needs a type, like '{param.name}: i64', as a top-level "
                        "function's or a method's always do",
                        param.span,
                    )
                self.types[id(param)] = params[-1]
            if function.return_type is not None:
                result = self.annotation(function.return_type)
            elif infer:
                result = BuiltinTy("Gen", (self.fresh(),)) if is_generator(function) else self.fresh()
            else:
                raise EllaError(
                    "a top-level function or method needs a return type, like '(x: i64) -> i64 => ...'",
                    function.span,
                )
            mutable = tuple(i for i, param in enumerate(function.params) if param.mutable)
            if mutable and is_generator(function):
                raise EllaError(
                    "a generator can't have 'mut' parameters; its parameters are its own variables, which it "
                    "can change",
                    function.params[mutable[0]].span,
                )
            return FuncTy(tuple(params), result, mutable)
        finally:
            self.type_params = outer

    def check_lambda(
        self,
        function: Lambda,
        env: Env,
        self_names: list[Binding],
        type_params: tuple[str, ...] = (),
        constraints: tuple[tuple[str, ...], ...] = (),
        infer: bool = False,
    ) -> FuncTy:
        """A lambda's type. Its body is checked later, once all top-level names are known.

        `self_names` are the `let` bindings it can call itself by. `infer`
        allows types to be left out (see `lambda_type`).
        """
        t = self.lambda_type(function, type_params, constraints, infer)
        captured = self.capture(env)
        for binding in self_names:
            captured.vars[binding.name] = VarInfo(t, False, "local", level=self.level, binder=binding)
        params = self.type_params | self.param_types(type_params, constraints, function.span)
        if id(function) not in self.checked:
            inferred = any(param.type is None for param in function.params) or function.return_type is None
            self.pending_bodies.append(PendingBody(function, captured, t, self.level, params, self.quotes, inferred))
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
                    outer = info.outer_quote or (info.level is not None and info.level > self.level)
                    captured.vars[name] = VarInfo(
                        info.type, False, "local", level=info.level, binder=info.binder, outer_quote=outer
                    )
                else:  # a local enum's variants
                    captured.vars[name] = info
        return captured

    def check_body(self, pending: PendingBody) -> None:
        """Check a function's `where` conditions and body against its type."""
        function, t = pending.function, pending.type
        self.checked.add(id(function))
        outer = self.return_type, self.loops, self.level, self.type_params, self.quotes
        outer_yield = self.yield_type, self.yield_quotes
        self.return_type, self.loops = t.result, []  # `break` can't leave a function
        self.level, self.type_params, self.quotes = pending.level, pending.type_params, pending.quotes
        generator = is_generator(function)
        self.yield_type = None
        if generator:
            # A generator gives a Gen of what it yields; its body's own value is unused.
            self.yield_type, self.yield_quotes = self.fresh(), self.quotes
            if not self.unify(t.result, BuiltinTy("Gen", (self.yield_type,))):
                raise EllaError(
                    f"a generator (a function with 'yield') gives a Gen[T], not {show_type(t.result)}",
                    function.return_type.span if function.return_type is not None else function.span,
                )
            self.return_type = UNIT
            self.generators.append((self.yield_type, function.span))

        local = Env(pending.captured)
        for param, param_type in zip(function.params, t.params):
            # A `mut` parameter is a variable the function may change; the
            # caller's variable gets its final value (docs/grammar.md §5).
            # A generator's parameters are its own variables.
            mutable = param.mutable or generator
            local.vars[param.name] = VarInfo(param_type, mutable, "local", level=self.level, binder=param)
            self.declared[id(param)] = param
        for cond in function.where:
            if mentions_return(cond.expr) and generator:
                raise EllaError("a generator can't have postconditions, since it returns nothing", cond.span)
            if mentions_return(cond.expr):
                result_env = Env(local)
                result_env.vars["return"] = VarInfo(t.result, False, "local", level=self.level, binder=cond)
                self.check_bool(cond.expr, result_env, "a postcondition")
            else:
                self.check_bool(cond.expr, local, "a precondition")
        body_type = self.check(function.body, local)
        if not generator:
            self.expect(body_type, t.result, function.body.span, "the function's result")

        self.return_type, self.loops, self.level, self.type_params, self.quotes = outer
        self.yield_type, self.yield_quotes = outer_yield

    def check_call(self, callee: Expr, args: tuple[Expr, ...], env: Env, span: Span) -> Type:
        """A call. Built-ins and variant constructors are only ever called directly, and checked here."""
        if isinstance(callee, Splice):
            struct = self.check_spliced_callee(callee, args, env, span)
            if struct is not None:
                self.types[id(callee)] = UNIT
                return struct
        if isinstance(callee, Member):
            return self.check_member_call(callee, args, env, span)
        if isinstance(callee, Name):
            self.scopes[id(callee)] = env
            info = self.lookup(callee.name, env, callee.span)
            if info.kind in ("builtin", "constructor"):
                self.resolved[id(callee)] = info
            if info.kind == "builtin":
                self.kinds[id(callee)] = "builtin"
                return self.check_builtin(callee.name, args, env, span)
            if info.kind == "constructor":
                self.kinds[id(callee)] = "constructor"
                enum_type, fields = self.instantiate(info.enum, callee.name)
                if is_builtin_error(self.enums, info.enum):
                    # Error(message) or Error(message, cause); where it is made is its `at`.
                    if len(args) not in (1, 2):
                        raise EllaError(f"Error takes 1 or 2 arguments but was given {len(args)}", span)
                    fields = (STR, ERROR)
                else:
                    self.check_arity(callee.name, len(fields), args, span)
                for arg, field_type in zip(args, fields):
                    self.expect(self.check(arg, env), field_type, arg.span, f"the argument of {callee.name}")
                return enum_type
        return self.check_function_call(self.check(callee, env), callee, args, env, span)

    def check_function_call(self, t: Type, callee: Expr, args: tuple[Expr, ...], env: Env, span: Span) -> Type:
        """A call of a function value of type `t`."""
        function = self.known(t, callee.span)
        if isinstance(function, DeferredTy):
            self.check_deferred_args(args, env)
            return DEFERRED
        if not isinstance(function, FuncTy):
            raise EllaError(f"can't call a value of type {show_type(function)}", span)
        self.check_arity("function", len(function.params), args, span)
        self.check_args(function, args, env)
        self.check_exclusive(callee, args)
        return function.result

    def check_deferred_args(self, args: tuple[Expr, ...], env: Env) -> None:
        """The arguments of a call whose function is only known once the code is built."""
        for arg in args:
            if isinstance(arg, MutArg):
                self.types[id(arg)] = self.check_place(arg.target, env, "pass")
            else:
                self.check(arg, env)

    def check_args(self, function: FuncTy, args: tuple[Expr, ...], env: Env, receiver: Type | None = None) -> None:
        """Check a call's arguments against the function's parameters.

        `receiver` is the type of the first argument of a method call, which
        has been checked already.
        """
        for i, (arg, param_type) in enumerate(zip(args, function.params)):
            known = receiver if i == 0 else None
            if i in function.mutable:
                if not isinstance(arg, MutArg):
                    raise EllaError(f"argument {i + 1} is for a 'mut' parameter, so mark it: 'mut ...'", arg.span)
                arg_type = known if known is not None else self.check_place(arg.target, env, "pass")
                self.types[id(arg)] = arg_type
                # The function changes the variable itself, so the types must match exactly.
                self.expect(arg_type, param_type, arg.span, f"argument {i + 1}")
            else:
                if isinstance(arg, MutArg):
                    raise EllaError(f"argument {i + 1} isn't for a 'mut' parameter, so it can't be 'mut'", arg.span)
                arg_type = known if known is not None else self.check(arg, env)
                self.expect(arg_type, param_type, arg.span, f"argument {i + 1}")

    # Methods

    def check_member_call(self, callee: Member, args: tuple[Expr, ...], env: Env, span: Span) -> Type:
        """`v.name(args)`: a call of v's method `name`, which gets v first, or of a function in v's field `name`.

        `mut v.name(args)` calls a method whose first parameter is `mut`.
        """
        receiver, name = callee.target, callee.name
        if isinstance(receiver, MutArg):
            t = self.check_place(receiver.target, env, "pass")
        else:
            t = self.check(receiver, env)
        self.types[id(receiver)] = t
        value_type = self.known(t, receiver.span)
        if isinstance(value_type, DeferredTy):
            self.types[id(callee)] = DEFERRED
            self.check_deferred_args(args, env)
            return DEFERRED
        method = self.method_type(value_type, name, span)
        if method is None:
            fields = isinstance(value_type, EnumTy) and name in self.enums[value_type.name].field_names
            if isinstance(receiver, MutArg) or not fields:
                hint = " (its constraints don't have one)" if isinstance(value_type, ParamTy) else ""
                raise EllaError(f"{show_type(value_type)} has no method '{name}'{hint}", callee.span)
            # A field holding a function.
            field_type = self.member_type(callee, value_type)
            self.types[id(callee)] = field_type
            return self.check_function_call(field_type, callee, args, env, span)
        if name == "finalize" and isinstance(value_type, EnumTy) and self.enums[value_type.name].finalize:
            raise EllaError("a finalize method runs by itself when the value dies, so it can't be called", span)
        if isinstance(value_type, EnumTy):
            self.private(name, value_type.name, callee.span)
        self.methods[id(callee)] = name
        self.types[id(callee)] = method
        if 0 in method.mutable and not isinstance(receiver, MutArg):
            raise EllaError(f"'{name}' changes the value it is called on, so mark it: 'mut x.{name}(...)'", span)
        if 0 not in method.mutable and isinstance(receiver, MutArg):
            raise EllaError(f"'{name}' doesn't change the value it is called on, so it can't be 'mut'", span)
        self.check_arity(name, len(method.params) - 1, args, span)
        self.check_args(method, (receiver, *args), env, t)
        self.check_exclusive(UnitLit(span), (receiver, *args))
        return method.result

    def method_type(self, t: Type, name: str, span: Span) -> FuncTy | None:
        """The type of the method `name` of values of type `t`, if they have one.

        A struct's or enum's methods are its own. A type parameter has the
        methods of its user-defined constraints.
        """
        match resolve(t):
            case EnumTy(enum_name, args) if name in self.enums[enum_name].methods:
                info = self.enums[enum_name]
                method = substitute(info.methods[name], dict(zip(info.params, args)))
                assert isinstance(method, FuncTy)
                if info.params:
                    # A method is a generic function of the type's parameters.
                    needs = info.constraints or tuple(() for _ in info.params)
                    self.generic_uses.append((f"{enum_name}.{name}", args, tuple(zip(info.params, needs)), span))
                return method
            case ParamTy(_, constraints) as param:
                found = {
                    c: self.constraints[c][name]
                    for c in sorted(constraints)
                    if c in self.constraints and name in self.constraints[c]
                }
                if len(set(found.values())) > 1:
                    raise EllaError(
                        f"the constraints {' and '.join(found)} have different methods named '{name}'", span
                    )
                if found:
                    method = substitute(next(iter(found.values())), {"Self": param})
                    assert isinstance(method, FuncTy)
                    return method
        return None

    def operator_method(self, t: Type, name: str, span: Span) -> FuncTy | None:
        """A struct's or enum's method that gives it an operator or `len` (not through a constraint)."""
        t = resolve(t)
        if isinstance(t, EnumTy) and name in self.enums[t.name].methods:
            return self.method_type(t, name, span)
        return None

    def may_share(self, other: Type, passed: Type) -> bool:
        """Whether an argument of type `other` could share storage that a `mut` argument of type `passed` changes.

        Changing a list in place leaves its old items alive, so only values
        with parts that can change are at risk. A pool or an arena frees its
        values, so anything boxed taken from one is.
        """
        if has_pool(passed, self.enums):
            return other not in (I64, F64, BOOL, UNIT)
        return has_mutable(other, self.enums)

    def check_exclusive(self, callee: Expr, args: tuple[Expr, ...]) -> None:
        """A variable passed as `mut` can't be used in any other argument of the same call.

        The function changes its `mut` parameters in place, so another argument
        that shared their storage would see the changes. Arguments without
        lists are fine, because they can't share anything mutable (a str or an
        i64 read from the variable is unaffected). This is checked by name, so
        it rejects some safe calls, such as `swap(mut a[0], mut a[1])`; see
        docs/memory-model.md §4.
        """
        roots = [(arg, place_root(arg.target)) for arg in args if isinstance(arg, MutArg)]
        for arg, root in roots:
            for other in (callee, *args):
                if other is arg:
                    continue
                if isinstance(other, MutArg):
                    if place_root(other.target) == root:
                        raise EllaError(f"'{root}' can't be passed as 'mut' twice in one call", other.span)
                elif root in names_used(other) and self.may_share(self.type_of(other), self.type_of(arg)):
                    raise EllaError(
                        f"'{root}' is passed as 'mut', so it can't also be used in another argument of the same call",
                        other.span,
                    )

    def check_spliced_callee(self, callee: Splice, args: tuple[Expr, ...], env: Env, span: Span) -> Type | None:
        """`$(t)(...)`: make a struct of the type t holds. Returns None if t is code instead.

        Outside quotes, t runs while checking, to find the struct. Inside a
        quote, the struct is only known once the code is built.
        """
        if self.quotes > 0:
            code_type = resolve(self.at_stage(self.level - 1, callee.code, env, -1))
            self.types[id(callee.code)] = code_type
            if not (isinstance(code_type, BuiltinTy) and code_type.name == "Type"):
                return None
            for arg in args:
                self.check(arg, env)
            return code_type.args[0]
        if not any_part(
            self.peek_type(callee.code, env), self.enums, lambda t: isinstance(t, BuiltinTy) and t.name == "Type"
        ):
            return None
        struct = self.splice_now(callee, BuiltinTy("Type", (self.fresh(),)), "the spliced type")
        if not isinstance(struct, EnumTy) or not self.enums[struct.name].struct or struct.args:
            raise EllaError(f"only a struct type that isn't generic can be called, not {show_type(struct)}", span)
        fields = self.variant_fields(struct, struct.name)
        self.check_arity(struct.name, len(fields), args, span)
        for arg, field_type in zip(args, fields):
            self.expect(self.check(arg, env), field_type, arg.span, f"the argument of {struct.name}")
        return struct

    def peek_type(self, code: Expr, env: Env) -> Type:
        """The type of a splice's code, checked at its stage (once: the result is remembered)."""
        if id(code) not in self.types:
            self.at_stage(self.level - 1, code, env, -1)
        return self.types[id(code)]

    def handle_item(self, container: BuiltinTy, index_type: Type, span: Span) -> Type:
        """The type of `pool[h]` or `arena[r]`: a pool's item, or what the ref refers to."""
        if container.name == "Pool":
            self.expect(index_type, BuiltinTy("Handle", container.args), span, "a pool's index")
            return container.args[0]
        item: Type = self.fresh()
        self.expect(index_type, BuiltinTy("Ref", (item,)), span, "an arena's index")
        return item

    def check_builtin(self, name: str, args: tuple[Expr, ...], env: Env, span: Span) -> Type:
        """`print`, `len`, `run`, the pool and arena functions, reflection, and the library.

        The pool and arena functions are `pool_new`, `arena_new`, `alloc` and
        `free`; `free(mut arena)` frees all of an arena's values at once.
        """
        if name in LIBRARY:
            function = self.library_type(name, span)
            self.check_arity(name, len(function.params), args, span)
            for i, (arg, t) in enumerate(zip(args, function.params), 1):
                if isinstance(arg, MutArg):
                    raise EllaError(f"{name}() has no 'mut' parameter", arg.span)
                self.expect(self.check(arg, env), t, arg.span, f"argument {i} of {name}()")
            return function.result
        if name in REFLECTION:
            params, reflected = REFLECTION[name]
            self.check_arity(name, len(params), args, span)
            for i, (arg, t) in enumerate(zip(args, params), 1):
                if isinstance(t, BuiltinTy):
                    t = BuiltinTy("Type", (self.fresh(),))  # any type will do
                self.expect(self.check(arg, env), t, arg.span, f"argument {i} of {name}()")
            return reflected
        if name in ("print", "eprint"):
            # Any number of values, written with spaces between them.
            for arg in args:
                if isinstance(arg, MutArg):
                    raise EllaError(f"{name}() has no 'mut' parameter", arg.span)
                self.check(arg, env)
            return UNIT
        if name in ("alloc", "free"):
            return self.check_alloc(name, args, env, span)
        if name == "next":
            self.check_arity(name, 1, args, span)
            arg = args[0]
            if not isinstance(arg, MutArg):
                raise EllaError("next() changes its generator or iterator, so mark it: 'next(mut g)'", arg.span)
            t = self.known(self.check_place(arg.target, env, "pass"), arg.span)
            self.types[id(arg)] = t
            found = self.iterator_item(t, span)
            if found is None:
                raise EllaError(
                    f"next() needs a generator or an iterator (a type with a next method), not {show_type(t)}",
                    arg.span,
                )
            return EnumTy("Optional", (found[0],))
        arity = {"len": 1, "run": 1, "pool_new": 0, "arena_new": 0}[name]
        self.check_arity(name, arity, args, span)
        for arg in args:
            if isinstance(arg, MutArg):
                raise EllaError(f"{name}() has no 'mut' parameter", arg.span)
        if name == "pool_new":
            return BuiltinTy("Pool", (self.fresh(),))
        if name == "arena_new":
            return ARENA
        arg_type = self.check(args[0], env)
        if name == "run":
            result: Type = self.fresh()
            self.expect(arg_type, BuiltinTy("Code", (result,)), args[0].span, "the code given to run()")
            if self.level >= 0 and any_part(result, self.enums, lambda t: isinstance(t, DeferredTy)):
                raise EllaError(
                    "this code's type is only known once it is built, so it can only run while compiling",
                    args[0].span,
                )
            return result
        if name == "len":
            arg_type = self.known(arg_type, args[0].span)
            method = self.method_type(arg_type, "len", span)
            if method is not None:
                self.expect(method, FuncTy((arg_type,), I64), args[0].span, "its len method")
                return I64
            if describe_kind(arg_type) not in ("list", "str", "range", "Pool", "Arena", "?"):
                raise EllaError(
                    f"len() needs a list, str, range, Pool or Arena, not {show_type(arg_type)}", args[0].span
                )
            return I64
        return UNIT

    def check_alloc(self, name: str, args: tuple[Expr, ...], env: Env, span: Span) -> Type:
        """`alloc(mut pool, value) -> Handle[T]`, `free(mut pool, handle)`, `alloc(mut arena, value) -> Ref[T]`
        and `free(mut arena)`."""
        if not args:
            raise EllaError(f"{name}() needs a pool or an arena, marked 'mut'", span)
        holder = args[0]
        if not isinstance(holder, MutArg):
            what = "arena" if resolve(self.check(holder, env)) == ARENA else "pool"
            rest = "" if name == "free" and what == "arena" else ", ..."
            raise EllaError(f"{name}() changes its {what}, so mark it: '{name}(mut {what}{rest})'", holder.span)
        holder_type = self.known(self.check_place(holder.target, env, "pass"), holder.span)
        self.types[id(holder)] = holder_type
        if holder_type == ARENA:
            self.check_arity(name, 2 if name == "alloc" else 1, args, span)
            if name == "free":
                return UNIT
            value = args[1]
            return BuiltinTy("Ref", (self.check(value, env),))
        self.check_arity(name, 2, args, span)
        item: Type = self.fresh()
        self.expect(holder_type, BuiltinTy("Pool", (item,)), holder.span, f"the pool or arena given to {name}()")
        value = args[1]
        expected = item if name == "alloc" else BuiltinTy("Handle", (item,))
        self.expect(self.check(value, env), expected, value.span, f"the value given to {name}()")
        return BuiltinTy("Handle", (item,)) if name == "alloc" else UNIT

    def wrap(self, name: Name, arity: int, env: Env) -> Type:
        """`print`, `eprint`, `len` or a constructor used as a value: a lambda that calls it, like `(_1) => Some(_1)`.

        The lambda's types are inferred, so each use has its own: `map(xs,
        Some)` makes Optionals of xs's items. It is kept in `wrappers`, for
        the lowering to make into a function. (The interpreters' built-ins
        and constructors are values already.)
        """
        span = name.span
        params = tuple(Param(f"_{i}", None, span) for i in range(1, arity + 1))
        call = Call(Name(name.name, span), tuple(Name(param.name, span) for param in params), span)
        wrapper = Lambda(params, None, (), call, span)
        self.wrappers[id(name)] = wrapper
        t = self.check_lambda(wrapper, env, [], infer=True)
        self.types[id(wrapper)] = t
        return t

    def library_type(self, name: str, span: Span) -> FuncTy:
        """The type of a library function at one use: abs, min and max get their own T, which must be Num (Eq for hash)."""
        function = LIBRARY[name]
        # The runtime makes the built-in Optional, Result and Error, which a program may replace.
        for part, info in BUILTIN_ENUMS.items():
            if self.enums[part] != info and part in enum_names(function.result):
                raise EllaError(f"{name}() needs the built-in {part}, which this program replaces", span)
        if name not in GENERIC_LIBRARY:
            return function
        arg = self.fresh()
        self.generic_uses.append((name, (arg,), (("T", (GENERIC_LIBRARY[name],)),), span))
        t = substitute(function, {"T": arg})
        assert isinstance(t, FuncTy)
        return t

    def check_arity(self, name: str, arity: int, args: tuple[Expr, ...], span: Span) -> None:
        if len(args) != arity:
            plural = "" if arity == 1 else "s"
            raise EllaError(f"{name} takes {arity} argument{plural} but was given {len(args)}", span)

    # Loops

    def loop_item(self, loop: For, iterable: Expr, env: Env, span: Span) -> Type:
        """The type of the items a `for` loop goes through, recording how it loops (`iterations`).

        It can loop over a list, str or range, a generator, an iterator (a
        struct or enum with a `next` method), or what a type's `iter` method
        gives. `for x in mut it` advances an iterator or generator in place.
        """
        if isinstance(iterable, MutArg):
            t = self.known(self.check_place(iterable.target, env, "pass"), iterable.span)
            self.types[id(iterable)] = t
            found = self.iterator_item(t, span)
            if found is None:
                raise EllaError(
                    f"only an iterator or a generator can be looped over with 'mut', not {a_or_an(show_type(t))} "
                    f"{show_type(t)}",
                    iterable.span,
                )
            self.iterations[id(loop)] = (False, found[1])
            return found[0]
        t = self.known(self.check(iterable, env), iterable.span)
        via_iter = False
        if (iterator := self.method_type(t, "iter", span)) is not None:
            # `for x in v` loops over what `v.iter()` gives.
            given = self.known(iterator.result, iterable.span)
            if describe_kind(given) not in ("list", "str", "range", "Gen") and self.iterator_item(given, span) is None:
                raise EllaError(
                    f"the iter method of {show_type(t)} must give a list, str, range, iterator or Gen, "
                    f"not {show_type(given)}",
                    span,
                )
            t, via_iter = given, True
        if (found := self.iterator_item(t, span)) is not None:
            self.iterations[id(loop)] = (via_iter, found[1])
            return found[0]
        self.iterations[id(loop)] = (via_iter, "items")
        match t:
            case DeferredTy():
                return DEFERRED
            case ListTy(element):
                return element
            case PrimTy("str"):
                return STR
            case PrimTy("range"):
                return I64
        raise EllaError(f"can't loop over a {show_type(t)}", span)

    def iterator_item(self, t: Type, span: Span) -> tuple[Type, str] | None:
        """For a generator or an iterator (with a `next` method), the type of its items and "gen" or "next"."""
        match resolve(t):
            case BuiltinTy("Gen", (item,)):
                return item, "gen"
        method = self.method_type(t, "next", span)
        if method is None:
            return None
        match resolve(method.result):
            case EnumTy("Optional", (item,)):
                return item, "next"
        return None

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
            case NamePattern(name, span, mutable):
                # A name is a variant (like `None`) if one is in scope, and otherwise a new variable.
                info = self.visible(name, env, span)
                if info is not None and info.kind == "variant":
                    if mutable:
                        raise EllaError(f"'mut' marks a variable, and '{name}' is a variant", span)
                    self.kinds[id(pattern)] = "variant"
                    enum_type, _ = self.instantiate(info.enum, name)
                    self.expect(enum_type, t, span, f"the pattern '{name}'")
                else:
                    self.kinds[id(pattern)] = "binding"
                    env.vars[name] = VarInfo(t, mutable, "local", level=self.level, binder=pattern)
                    self.declared[id(pattern)] = pattern
                    if mutable:
                        self.mutable_vars.add(id(pattern))
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
                        if info.mutable != first[name].mutable:
                            raise EllaError(f"'{name}' must be 'mut' in every alternative of '|', or in none", span)
                env.vars.update(first)
                # Uses of the names find the first alternative's, so only those can be unused.
                for alternative in alternatives[1:]:
                    for node in pattern_names(alternative):
                        self.declared.pop(id(node), None)
            case VariantPattern(name, args, span):
                info = self.visible(name, env, span)
                if info is None or info.kind not in ("variant", "constructor"):
                    raise EllaError(f"'{name}' isn't an enum variant", span)
                enum_type, fields = self.instantiate(info.enum, name)
                self.expect(enum_type, t, span, f"the pattern '{name}(...)'")
                # An Error's pattern may leave out its cause and where it was made: Error(message).
                short = is_builtin_error(self.enums, info.enum) and 0 < len(args) < len(fields)
                if len(args) != len(fields) and not short:
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


def place_root(place: Expr) -> str:
    """The variable at the root of an assignment target: `xs` in `xs[i].y[j]`."""
    while isinstance(place, Index | Member):
        place = place.target
    assert isinstance(place, Name), "the checker only accepts names and indexes as places"
    return place.name


def names_used(node: object, inside_lambdas: bool = False) -> set[str]:
    """Every name an expression reads, not looking inside nested lambdas unless asked to."""
    match node:
        case Name(name):
            return {name}
        case Lambda() if not inside_lambdas:
            return set()
        case tuple():
            return set().union(*(names_used(item, inside_lambdas) for item in node))
        case _ if dataclasses.is_dataclass(node) and not isinstance(node, type):
            return set().union(*(names_used(getattr(node, f.name), inside_lambdas) for f in dataclasses.fields(node)))
    return set()


def nodes(node: object) -> Iterable[object]:
    """Every syntax tree node in some syntax, itself included."""
    if isinstance(node, tuple):
        for item in node:
            yield from nodes(item)
    elif dataclasses.is_dataclass(node) and not isinstance(node, type | Span):
        yield node
        for f in dataclasses.fields(node):
            yield from nodes(getattr(node, f.name))


def start_of(node: object) -> Span:
    """Where an expression starts: the first of its parts' spans (a call's own span is its `(`)."""
    spans = []
    for f in dataclasses.fields(node):  # type: ignore[arg-type]
        part = getattr(node, f.name)
        if isinstance(part, Span):
            spans.append(part)
        elif isinstance(part, tuple):
            spans += [start_of(item) for item in part if dataclasses.is_dataclass(item)]
        elif dataclasses.is_dataclass(part) and not isinstance(part, type):
            spans.append(start_of(part))
    return min(spans, key=lambda span: (span.line, span.column))


def defined_names(expr: object) -> list[str]:
    """The names a top-level line defines: a `let`'s, or an enum's and its variants'."""
    match expr:
        case Let(bindings):
            return [binding.name for binding in bindings]
        case LetPattern(pattern):
            return [node.name for node in pattern_names(pattern)]
        case EnumDef(name, variants=variants):
            return [name, *(variant.name for variant in variants)]
    return []


def enum_names(t: Type) -> set[str]:
    """The enums and structs a type is made of, as written: `Result[i64, Error]` has Result and Error."""
    match t:
        case EnumTy(name, args):
            return {name}.union(*map(enum_names, args))
        case ListTy(item):
            return enum_names(item)
        case TupleTy(items) | BuiltinTy(_, items):
            return set().union(*map(enum_names, items))
        case FuncTy(params, result):
            return enum_names(result).union(*map(enum_names, params))
    return set()


def pattern_names(pattern: Pattern) -> list[NamePattern]:
    """The bare names in a pattern: the variables it binds, and variants like `None`."""
    match pattern:
        case NamePattern():
            return [pattern]
        case OrPattern(items) | VariantPattern(_, items) | TuplePattern(items):
            return [node for item in items for node in pattern_names(item)]
    return []


def has_functions(node: object) -> bool:
    """Whether code has a lambda or a quote in it."""
    return isinstance(node, Lambda | Quote) or any(has_functions(part) for part in parts(node))


def any_part(
    t: Type, enums: dict[str, EnumInfo], test: Callable[[Type], bool], seen: frozenset[str] = frozenset()
) -> bool:
    """Whether `t`, or any type a value of it holds (list items, fields, ...), passes `test`.

    Function types count as holding nothing: a closure's captured values can
    never change, or be reached from outside.
    """
    t = resolve(t)
    if test(t):
        return True
    match t:
        case ListTy(item):
            return any_part(item, enums, test, seen)
        case TupleTy(items):
            return any(any_part(item, enums, test, seen) for item in items)
        case EnumTy(name, args) if name not in seen and name in enums:
            enum = enums[name]
            mapping = dict(zip(enum.params, args))
            fields = [substitute(f, mapping) for fs in enum.variants.values() for f in fs]
            return any(any_part(f, enums, test, seen | {name}) for f in fields)
        case BuiltinTy("Pool" | "Type", (item,)):
            return any_part(item, enums, test, seen)
        case _:
            return False


def has_mutable(t: Type, enums: dict[str, EnumInfo] | None = None) -> bool:
    """Whether values of type `t` hold anything Ella can change in place: a list, a struct with fields, a pool, an arena.

    Values without such parts can be shared freely: nothing can change them.
    `enums` gives the definitions of enum and struct types (the built-in
    ones by default).
    """
    enums = enums if enums is not None else BUILTIN_ENUMS

    def changes(t: Type) -> bool:
        match t:
            case ListTy() | BuiltinTy("Pool" | "Arena" | "Gen"):
                return True
            case EnumTy(name) if name in enums:
                return enums[name].struct and bool(enums[name].field_names)
        return False

    return any_part(t, enums, changes)


def is_nocopy(t: Type, enums: dict[str, EnumInfo]) -> bool:
    """Whether values of type `t` can't be copied: they are, or hold, a nocopy struct, a pool, an arena or a generator."""

    def nocopy(t: Type) -> bool:
        match t:
            case BuiltinTy("Pool" | "Arena" | "Gen"):
                return True
            case EnumTy(name) if name in enums:
                return enums[name].nocopy
        return False

    return any_part(t, enums, nocopy)


def a_or_an(word: str) -> str:
    """The article for a word, by its first letter: "an Arena", "a Pool[str]"."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def has_pool(t: Type, enums: dict[str, EnumInfo]) -> bool:
    """Whether values of type `t` hold a pool or an arena, which free the values in them."""
    return any_part(t, enums, lambda t: isinstance(t, BuiltinTy) and t.name in ("Pool", "Arena"))


def persistable(t: Type, enums: dict[str, EnumInfo]) -> bool:
    """Whether a value of type `t` can be carried into a later stage, as constants in its code.

    Data can: numbers, strings, lists and so on. Functions can't (only
    top-level ones, by name), and neither can values that can't be copied.
    """
    return not is_nocopy(t, enums) and not any_part(
        t,
        enums,
        lambda t: isinstance(t, FuncTy | DeferredTy) or (isinstance(t, BuiltinTy) and t.name in ("Handle", "Ref")),
    )


# Values that can't be copied


def check_copies(program: tuple[Expr, ...], checker: Checker) -> None:
    """Check that values of nocopy types are never copied (docs/memory-model.md §3).

    Such a value is made in place and stays there. Reading it (a variable,
    an item or a field) is only allowed where the value is borrowed: as a
    function argument, to read one of its fields, to compare or print it, or
    as the subject of a `match`. The one exception is a function returning
    one of its own local variables (declared outside loops), which moves it
    to the caller. New values, made by calling a constructor or a function,
    can go anywhere a copy could, except into a list.
    """
    CopyChecker(checker).check_all(program)


class CopyChecker:
    def __init__(self, checker: Checker) -> None:
        self.checker = checker
        self.movable: set[int] = set()  # binders of the current function's local variables outside loops
        self.function_binders: set[int] = set()  # binders of the current function's variables
        self.loop_depth = 0

    def nocopy(self, expr: Expr) -> bool:
        t = self.checker.types.get(id(expr))
        return t is not None and is_nocopy(finish(t), self.checker.enums)

    def check_all(self, exprs: tuple[Expr, ...]) -> None:
        for expr in exprs:
            self.walk(expr, "copy")

    def error(self, expr: Expr, what: str) -> EllaError:
        t = show_type(finish(self.checker.types[id(expr)]))
        return EllaError(f"{a_or_an(t)} {t} can't be copied, so it can't be {what}", expr.span)

    def walk(self, node: object, context: str) -> None:
        """Check a node whose value is used in `context`: "copy", "borrow", or "return" (moved to the caller)."""
        match node:
            case Name() | Index() | Member() if context != "borrow" and self.nocopy(node):
                if not (context == "return" and isinstance(node, Name) and self.is_movable(node)):
                    raise self.error(
                        node, "used here" if context == "copy" else "returned unless it is a local variable"
                    )
                self.walk(node, "borrow")
            case Name() as name:
                info = self.checker.resolved.get(id(name))
                if (
                    info is not None
                    and info.kind == "local"
                    and id(info.binder) not in self.function_binders
                    and self.nocopy(name)
                ):
                    raise self.error(name, "captured by a closure")
            case Index(target, index):
                self.walk(target, "borrow")
                self.walk(index, "copy")
            case Member(target):
                self.walk(target, "borrow")
            case Let(bindings, value):
                self.walk(value, "copy")
                if self.loop_depth == 0:
                    self.movable.update(id(binding) for binding in bindings)
                self.function_binders.update(id(binding) for binding in bindings)
            case LetPattern(pattern, value):
                self.walk(value, "borrow")  # its parts are borrowed, as a match's are
                self.function_binders.update(id(node) for node in pattern_names(pattern))
            case Assign(_, target, value):
                if self.nocopy(target):
                    t = show_type(finish(self.checker.types[id(target)]))
                    raise EllaError(
                        f"{a_or_an(t)} {t} can't be copied, so it can't be assigned; change its fields instead",
                        target.span,
                    )
                self.walk_place(target)
                self.walk(value, "copy")
            case Call(callee, args):
                takes = self.gives_generator(node)
                if isinstance(callee, Member) and isinstance(callee.target, MutArg):
                    self.walk_place(callee.target.target)  # `mut v.method(...)`
                elif isinstance(callee, Member) and id(callee) in self.checker.methods and takes:
                    self.walk_taken(callee.target)
                else:
                    self.walk(callee, "borrow")
                for arg in args:
                    if isinstance(arg, MutArg):
                        self.walk_place(arg.target)
                    elif takes:
                        self.walk_taken(arg)
                    elif (
                        isinstance(callee, Name)
                        and callee.name == "alloc"
                        and self.checker.kinds.get(id(callee)) == "builtin"
                    ):
                        self.walk(arg, "copy")  # moved into the pool
                    else:
                        self.walk(arg, "borrow")
            case Compare(first, rest):
                self.walk(first, "borrow")
                for _, operand in rest:
                    self.walk(operand, "borrow")
            case StringLit(parts):
                for part in parts:
                    if not isinstance(part, str):
                        self.walk(part, "borrow")
            case ListLit(items):
                for item in items:
                    if self.nocopy(item):
                        raise EllaError("a list can't hold values that can't be copied; use a Pool", item.span)
                    self.walk(item, "copy")
            case Match(subject, cases):
                self.walk(subject, "borrow")
                for case in cases:
                    self.function_binders.update(id(node) for node in pattern_names(case.pattern))
                    self.walk(case.guard, "copy")
                    self.walk(case.body, context)
            case If(cond, then, else_):
                self.walk(cond, "copy")
                self.walk(then, context)
                self.walk(else_, context)
            case Block(exprs):
                for line in exprs[:-1]:
                    self.walk(line, "copy")
                self.walk(exprs[-1], context)
            case Return(value):
                self.walk(value, "return")
            case For(_, pattern, iterable, body):
                via_iter, kind = self.checker.iterations.get(id(node), (False, "items"))
                if isinstance(iterable, MutArg):
                    self.walk_place(iterable.target)
                elif not via_iter and kind != "items" and self.nocopy(iterable) and is_place(iterable):
                    t = show_type(finish(self.checker.types[id(iterable)]))
                    raise EllaError(
                        f"{a_or_an(t)} {t} can't be copied, so a loop over it must advance it in place: "
                        "'for ... in mut ...'",
                        iterable.span,
                    )
                else:
                    self.walk(iterable, "borrow")
                self.function_binders.update(id(node) for node in pattern_names(pattern))
                self.walk_loop(body)
            case While(_, cond, body):
                self.walk(cond, "copy")
                self.walk_loop(body)
            case Loop(_, body):
                self.walk_loop(body)
            case Lambda(params, _, where, body):
                outer = self.movable, self.function_binders, self.loop_depth
                self.movable, self.function_binders, self.loop_depth = set(), {id(p) for p in params}, 0
                self.function_binders.update(id(c) for c in where)
                for condition in where:
                    self.walk(condition.expr, "borrow")
                self.walk(body, "return")
                self.movable, self.function_binders, self.loop_depth = outer
            case EnumDef(methods=methods):
                for method in methods:
                    self.walk(method.function, "copy")
            case _:
                self.walk_parts(node)

    def gives_generator(self, call: Call) -> bool:
        """Whether a call gives a generator, which takes its arguments rather than borrowing them."""
        t = self.checker.types.get(id(call))
        return t is not None and describe_kind(finish(t)) == "Gen"

    def walk_taken(self, arg: Expr) -> None:
        """An argument a generator takes: one that can't be copied must be a new value."""
        if self.nocopy(arg) and is_place(arg):
            t = show_type(finish(self.checker.types[id(arg)]))
            raise EllaError(
                f"{a_or_an(t)} {t} can't be copied, and a function that gives a generator takes its arguments, "
                "so this must be a new value",
                arg.span,
            )
        self.walk(arg, "copy")

    def walk_parts(self, node: object) -> None:
        """Walk a node's parts, as copies."""
        if isinstance(node, tuple):
            for item in node:
                self.walk(item, "copy")
        elif dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                value = getattr(node, f.name)
                if isinstance(value, tuple) or (dataclasses.is_dataclass(value) and not isinstance(value, type)):
                    self.walk(value, "copy")

    def walk_place(self, place: Expr) -> None:
        """A place being changed or passed as `mut`: its variable is used where it is, not copied."""
        match place:
            case Index(target, index):
                self.walk_place(target)
                self.walk(index, "copy")
            case Member(target):
                self.walk_place(target)
            case _:
                self.walk(place, "borrow")

    def walk_loop(self, body: Expr) -> None:
        self.loop_depth += 1
        self.walk(body, "copy")
        self.loop_depth -= 1

    def is_movable(self, name: Name) -> bool:
        info = self.checker.resolved.get(id(name))
        return info is not None and info.kind == "local" and id(info.binder) in self.movable


def is_place(expr: Expr) -> bool:
    """Whether an expression reads a variable, or a part of one, rather than making a new value."""
    match expr:
        case Name():
            return True
        case Index(target) | Member(target):
            return is_place(target)
    return False


def has_type_splice(node: object) -> bool:
    """Whether some syntax has a splice in a type (outside `type ...`, which is a value)."""
    match node:
        case TypeSplice():
            return True
        case TypeLit() | Quote():
            return False
        case tuple():
            return any(has_type_splice(item) for item in node)
        case _ if dataclasses.is_dataclass(node) and not isinstance(node, type | Span):
            return any(has_type_splice(getattr(node, f.name)) for f in dataclasses.fields(node))
    return False


def satisfies(t: Type, constraint: str, enums: dict[str, EnumInfo]) -> bool:
    """Whether a type meets a constraint of a generic function's type parameter (CONSTRAINTS)."""
    t = finish(t)
    if isinstance(t, DeferredTy):
        return True  # checked once the code is built
    if isinstance(t, ParamTy):
        return constraint in t.constraints
    match constraint:
        case "Eq":
            # Anything but functions, pools, arenas and code, and type parameters that aren't Eq themselves.
            def unequal(part: Type) -> bool:
                if isinstance(part, ParamTy):
                    return "Eq" not in part.constraints
                return isinstance(part, FuncTy) or (
                    isinstance(part, BuiltinTy) and part.name in ("Pool", "Arena", "Gen", "Code")
                )

            return not any_part(t, enums, unequal)
        case "Ord":
            # Structs and enums with a `compare` method too (its type is checked where it is defined).
            return t in (I64, F64, STR) or (isinstance(t, EnumTy) and "compare" in enums[t.name].methods)
        case "Add":
            # Structs and enums with an `add` method that gives one of the same type.
            if isinstance(t, EnumTy) and "add" in enums[t.name].methods:
                info = enums[t.name]
                return finish(substitute(info.methods["add"], dict(zip(info.params, t.args)))) == FuncTy((t, t), t)
            return t in (I64, F64, STR) or isinstance(t, ListTy)
        case "Num":
            return t in (I64, F64)
        case _:  # user-defined constraints are checked by Checker.unmet
            return False
