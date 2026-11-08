"""Tree-walking interpreter: runs a program by evaluating its syntax tree directly."""

import sys
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TextIO

from ella.checker import (
    BUILTIN_ENUMS,
    F64,
    LIBRARY,
    OPERATOR_METHODS,
    REFLECTION,
    BuiltinTy,
    Checker,
    EnumTy,
    FuncTy,
    ListTy,
    ParamTy,
    ShapeCheck,
    TupleTy,
    Type,
    array_item,
    check_code,
    finish,
    pattern_names,
    show_type,
    substitute,
)
from ella.lexer import EQ_SITE, FINALIZER_SITE, HASH_SITE, TEXT_SITE, CallSite, EllaError, Span
from ella.library import Exit, Host, hash_value
from ella.library import call as call_library
from ella.modules import link, prelude
from ella.operators import array_offset, binary, compare, index_into, unary
from ella.parser import parse
from ella.staging import (
    AnySplice,
    Tables,
    has_splices,
    materialize_hygiene,
    prepare,
    reflect,
    reify,
    splice_value,
    start_code,
    type_from,
    uses_staging,
)
from ella.syntax import (
    Assert,
    Assign,
    Binary,
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
    If,
    Index,
    IntLit,
    Lambda,
    Let,
    LetPattern,
    ListLit,
    LiteralPattern,
    Loop,
    MapLit,
    Match,
    Member,
    MutArg,
    Name,
    NamedType,
    NamePattern,
    OrPattern,
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
    TypeLit,
    Unary,
    UnitLit,
    VariantPattern,
    While,
    WildcardPattern,
    Yield,
    is_generator,
    mentions_return,
    size_names,
)
from ella.values import (
    BUILTIN_CONSTANT_VALUES,
    F32,
    MAX_CALL_DEPTH,
    MAX_HANDLE_GENERATION,
    MAX_POOL_SLOTS,
    UNIT,
    ArenaValue,
    ArrayValue,
    Builtin,
    Closure,
    CodeValue,
    Constructor,
    GenValue,
    HandleValue,
    PoolValue,
    RefValue,
    Scope,
    TypeValue,
    Value,
    Var,
    VariantValue,
    check_status,
    copy_value,
    equal,
    main_error,
    make_error,
    run_deep,
    to_text,
    type_name,
)

# How `a.compare(b)` decides each ordering comparison.
ORDERING: dict[str, Callable[[int], bool]] = {
    "<": lambda order: order < 0,
    "<=": lambda order: order <= 0,
    ">": lambda order: order > 0,
    ">=": lambda order: order >= 0,
}
# Each Ella call uses a few dozen Python frames, so the Python recursion
# limit is raised to match while a program runs.
PYTHON_FRAMES_PER_CALL = 50


# A `mut` argument's place, found when the arguments are evaluated: its root
# variable, its indexes (values) and fields (Member nodes), and its span.
type Place = tuple[Expr, list[tuple[Value | Member, Span]], Span]


class BreakSignal(Exception):
    """Raised by `break`, caught by the loop it names (or the innermost loop).

    `value` is None for a plain `break`, so that `for` and `while`, which
    can't produce a value, can tell `break x` apart from `break`.
    """

    def __init__(self, label: str | None, value: Value | None, span: Span) -> None:
        self.label = label
        self.value = value
        self.span = span


class ContinueSignal(Exception):
    """Raised by `continue`, caught by the loop it names (or the innermost loop)."""

    def __init__(self, label: str | None, span: Span) -> None:
        self.label = label
        self.span = span


class ReturnSignal(Exception):
    """Raised by `return` (and by `?` on an error), caught by the function call."""

    def __init__(self, value: Value, span: Span) -> None:
        self.value = value
        self.span = span


class TailCallSignal(Exception):
    """A direct self-tail-call with scalar arguments, handled by the current frame."""

    def __init__(self, closure: Closure, args: list[Value], span: Span) -> None:
        self.closure = closure
        self.call_args = args
        self.span = span


class GeneratorClosed(Exception):
    """Raised at a `yield` when its generator dies before its body finishes, to end the body's thread."""


@dataclass(eq=False)
class Generator:
    """The state of a generator in the AST interpreter (a GenValue's `state`).

    `owners` is the body's owner stack: the values with finalizers it made,
    by function call and loop iteration, innermost last; the first holds the
    arguments it took. The body's thread starts at the first `next`.
    `outcome` is what the body did last: ("yield", value), ("end", None),
    ("error", exception) or ("closed", None).
    """

    function: Lambda
    scope: Scope
    name: str  # the generator's, for stack traces
    owners: list[list[Value]] = field(default_factory=lambda: [[]])
    thread: threading.Thread | None = None
    go: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    back: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    outcome: tuple[str, Value | BaseException | None] = ("end", None)
    done: bool = False
    closing: bool = False


def run(source: str, out: TextIO | None = None, host: Host | None = None, path: Path | None = None) -> int:
    """Parse and run a program, writing what it prints to `out` (standard output by default). Returns its exit status.

    Raises EllaError for syntax and run-time errors. The program isn't type
    checked first, unless it builds code (with `quote` or `$`), which needs
    the checker. `path` is its file, which its imports are found from.
    """
    program = link(parse(source), path)
    if uses_staging(program):
        program, checker = prepare(program, out)
        return Interpreter(out, checker, host=host).run(program)
    return Interpreter(out, host=host).run(program)


class Interpreter:
    def __init__(
        self,
        out: TextIO | None = None,
        checker: Checker | None = None,
        tables: Tables | None = None,
        host: Host | None = None,
        max_depth: int = MAX_CALL_DEPTH,
    ) -> None:
        """`checker` is the checked program's checker, which quotes and `run` need; `tables` may share its tables.

        `host` has the program's input and arguments, and `max_depth` is how
        deeply its functions may call each other.
        """
        # Look up sys.stdout now rather than as a default argument, which
        # would be fixed when this module is imported.
        self.out = out if out is not None else sys.stdout
        self.host = host if host is not None else Host()
        self.depth = 0  # how many Ella function calls are in progress
        self.max_depth = max_depth
        # The calls in progress, outermost first, for stack traces (None for the call of `main`, which
        # the program doesn't write), and the name of the function running (see `CallSite`): None while
        # the program calls `main`.
        self.calls: list[CallSite | None] = []
        self.running: str | None = ""
        self.unwinding = False
        self.next_pool_id = 0
        self.names: dict[int, str] = {}  # id of a lambda -> the function's name, from its `let`
        self.checker = checker
        self.tables = tables if tables is not None else Tables().add(checker) if checker is not None else Tables()
        # While compiling, a top-level function's splices are expanded before it runs.
        self.prepare_function: Callable[[Lambda], Lambda] | None = None
        self.prepare_code: Callable[[Expr], Expr] | None = None  # and a constant's code, before it is computed
        self.structs: dict[str, tuple[str, ...]] = {"Error": BUILTIN_ENUMS["Error"].field_names}  # struct -> fields
        self.nocopy: set[str] = set()  # the nocopy structs
        self.finalizers: dict[str, Value] = {}  # struct -> its finalize function
        self.methods: dict[str, dict[str, Value]] = {}  # struct or enum -> its methods
        # The structs and enums whose type parameters have a constraint with constants: their values
        # carry their type arguments (VariantValue.type_args), for `T.NAME` in their methods.
        self.typed_enums: set[str] = set()
        # Values with finalizers, by owner: the program (globals), and each
        # function call and loop iteration in progress, innermost last. When
        # an owner ends, it finalizes its values, newest first.
        self.owners: list[list[Value]] = [[]]
        self.quote_symbols: list[dict[int, int]] = []  # hygienic identities of binders in quotes being built
        self.generator: Generator | None = None  # the generator whose body is running, if any
        # (generic function, its type arguments) -> the function knowing them (`with_type_args`)
        self.typed_closures: dict[tuple[int, tuple[Type, ...]], tuple[Closure, Closure]] = {}
        # (type, constraint it implements, constant) -> the constant's value, and those being computed
        self.constants: dict[tuple[str, str, str], Value] = {}
        self.computing: set[tuple[str, str, str]] = set()

        # The global scope holds the built-ins and every top-level `let`.
        self.globals = Scope(None)
        self.globals.define("print", Builtin("print", -1, self.builtin_print))
        self.globals.define("eprint", Builtin("eprint", -1, self.builtin_eprint))
        self.globals.define("len", Builtin("len", 1, self.builtin_len))
        self.globals.define("run", Builtin("run", 1, self.builtin_run))
        for name, (params, _) in REFLECTION.items():
            self.globals.define(name, Builtin(name, len(params), self.builtin_reflect(name)))
        for name, function in LIBRARY.items():
            self.globals.define(name, Builtin(name, len(function.params), self.builtin_library(name)))
        self.globals.define("pool_new", Builtin("pool_new", 0, lambda args, span: self.new_pool()))
        self.globals.define("arena_new", Builtin("arena_new", 0, lambda args, span: self.new_arena()))
        # alloc and free change their pool or arena, and are handled in `call_pool`; next changes its
        # generator or iterator, in `call_next`.
        self.globals.define("alloc", Builtin("alloc", 2, lambda args, span: UNIT))
        self.globals.define("free", Builtin("free", 2, lambda args, span: UNIT))
        self.globals.define("next", Builtin("next", 1, lambda args, span: UNIT))
        # Result, Optional and Error are ordinary enums and structs that happen to be predefined.
        self.globals.define("Ok", Constructor("Result", "Ok", 1))
        self.globals.define("Err", Constructor("Result", "Err", 1))
        self.globals.define("Error", Constructor("Error", "Error", 1, error=True))
        self.globals.define("Some", Constructor("Optional", "Some", 1))
        self.globals.define("None", VariantValue("Optional", "None"))
        # Code in a module other than the entry file always sees these by their names (see `visible`).
        self.predefined = dict(self.globals.vars)

    # Programs

    def run(self, program: Program) -> int:
        """Run the top-level expressions in order, then `main` if there is one. Returns the exit status."""
        return run_deep(lambda: self.run_program(program), self.max_depth, PYTHON_FRAMES_PER_CALL)

    def run_program(self, program: Program) -> int:
        status = 0
        self.depth = 1  # the top-level code counts as a call, as in the IR and native code
        try:
            self.declare_top_level(program)
            self.owners.append([])  # the top-level code's own values
            for expr in program.body:
                self.eval(expr, self.globals)
            main = self.globals.vars.get("main")
            if main is not None:
                self.running = None  # the program calls main
                result = self.call(main.value, [], Span(1, 1))
                self.running = ""
                match result:
                    case VariantValue("Result", "Ok", [value]):
                        result = value
                    case VariantValue("Result", "Err", [error]):
                        raise main_error(error, program.span)
                if isinstance(result, int) and not isinstance(result, bool):  # main gave an exit status
                    status = check_status(result, program.span)
            self.close_owner()
            self.close_owner()  # the globals
            return status
        except Exit as stop:  # `exit(status)`: nothing more runs, not even finalizers
            self.out.flush()
            return stop.status
        except (EllaError, ReturnSignal, BreakSignal, ContinueSignal) as error:
            if isinstance(error, ReturnSignal):
                error = EllaError("'return' outside of a function", error.span)
            elif isinstance(error, BreakSignal):
                error = EllaError("'break' outside of a loop", error.span)
            elif isinstance(error, ContinueSignal):
                error = EllaError("'continue' outside of a loop", error.span)
            error.traced([])  # one in the top-level code
            self.unwinding = True
            try:
                while self.owners:
                    self.close_owner()
            finally:
                self.unwinding = False
            raise error from None

    def declare_top_level(self, program: Program) -> None:
        """Make every top-level name exist before the program starts (docs/grammar.md §3).

        This is what lets top-level functions call each other in any order:
        functions and enums are defined right away, and other values are
        declared but not ready until their `let` runs.
        """
        for expr in program.body:
            match expr:
                case Let(bindings, Lambda() as function):
                    self.names[id(function)] = bindings[0].name
                    closure = Closure(function, Scope(self.globals))
                    for binding in bindings:
                        self.globals.define(binding.name, closure, binding.mutable)
                case Let(bindings):
                    for binding in bindings:
                        for name in (binding.name, *size_names(binding.type)):
                            if name not in self.globals.vars:
                                self.globals.vars[name] = Var(
                                    UNIT, binding.mutable and name == binding.name, ready=False
                                )
                case LetPattern(pattern):
                    for node in pattern_names(pattern):
                        if node.name not in self.globals.vars:
                            self.globals.vars[node.name] = Var(UNIT, False, ready=False)
                case EnumDef():
                    self.define_enum(expr, self.globals)

    # Expressions

    def eval(self, expr: Expr, scope: Scope, tail: bool = False) -> Value:
        """Evaluate an expression in `scope` and return its value."""
        match expr:
            # Literals
            case IntLit(literal) | FloatLit(literal) | BoolLit(literal):
                return literal
            case StringLit(parts):
                # Interpolated expressions are evaluated and turned into text.
                return "".join(part if isinstance(part, str) else self.text(self.eval(part, scope)) for part in parts)
            case UnitLit():
                return UNIT
            case TupleLit(items):
                return tuple(self.eval(item, scope) for item in items)
            case ListLit(items):
                return [self.eval(item, scope) for item in items]

            # Variables
            case Name(name, span):
                # Reading a variable copies its value: Ella has value semantics.
                value = copy_value(self.lookup(name, scope, span).value)
                if (
                    isinstance(value, Constructor)
                    and value.error
                    and self.checker
                    and id(expr) in self.checker.wrappers
                ):
                    return replace(value, at=span)  # Error used as a value makes its Errors here
                if (
                    isinstance(value, Builtin)
                    and value.name == "array"
                    and self.checker
                    and id(expr) in self.checker.wrappers
                ):
                    return self.eval(self.checker.wrappers[id(expr)], scope)  # whose call knows the item type
                if (
                    isinstance(value, Closure)
                    and self.checker
                    and self.checker.associated_constants
                    and (type_args := self.checker.instances.get(id(expr))) is not None
                ):
                    params = self.checker.resolved[id(expr)].type_params
                    return self.with_type_args(value, params, type_args, scope)
                if (
                    isinstance(value, Constructor | VariantValue)
                    and value.enum in self.typed_enums
                    and not value.type_args  # not one a variable holds (in `map`, f is `(T) -> U`)
                    and self.checker
                    and id(expr) in self.checker.types  # not a constructor's name in its call: that has the type
                ):
                    return replace(value, type_args=self.made_type_args(expr, scope))
                return value
            case Let(bindings, value_expr):
                if isinstance(value_expr, Lambda):
                    self.names[id(value_expr)] = bindings[0].name
                value = self.eval(value_expr, scope)
                if scope is self.globals:
                    self.adopt(value, -1, 0)  # a global's values live as long as the program
                for binding in bindings:
                    scope.define(binding.name, copy_value(value), binding.mutable)
                self.check_shapes(expr, scope)
                # A function bound with `let` can call itself by name. Its
                # captured scope was made before the `let` bound the name, so
                # add the name there too.
                if isinstance(value, Closure) and isinstance(value_expr, Lambda):
                    for binding in bindings:
                        value.scope.define(binding.name, value)
                return UNIT if bindings[0].type_params and scope is not self.globals else value
            case LetPattern(pattern, value_expr, span, mutable):
                value = self.eval(value_expr, scope)
                bound = self.match_pattern(pattern, value, scope)
                if bound is None:
                    raise EllaError(f"the 'let' pattern doesn't match {to_text(value, nested=True)}", span)
                if scope is self.globals:
                    self.adopt(value, -1, 0)
                mutables = mutable_names(pattern)
                for name, part in bound.items():
                    scope.define(name, copy_value(part), mutable or name in mutables)
                return value
            case Assign(op, target, value_expr, span):
                if self.checker and (update := self.checker.updates.get(id(expr))) is not None:
                    # `v[k] op= x` changing the item in place, with v's `update` method.
                    self.eval(update, scope)
                    return UNIT
                return self.eval_assign(op, target, value_expr, scope, span)

            # Operators
            case Unary(op, operand, span):
                value = self.eval(operand, scope)
                if op == "-" and (method := self.find_method(value, "neg")) is not None:
                    return self.call(method, [value], span)
                return unary(op, value, span)
            case Binary("and", left, right):
                # `and` and `or` only evaluate their right side if needed.
                return self.eval_bool(left, scope, "'and'") and self.eval_bool(right, scope, "'and'")
            case Binary("or", left, right):
                return self.eval_bool(left, scope, "'or'") or self.eval_bool(right, scope, "'or'")
            case Binary(op, left, right, span):
                a, b = self.eval(left, scope), self.eval(right, scope)
                if op == "/" and isinstance(a, F32) and self.checker and finish(self.checker.types[id(expr)]) == F64:
                    assert isinstance(b, F32)
                    a, b = float(a), float(b)  # `/` on a type parameter gives an f64 (as ella.lower does it)
                return self.binary(op, a, b, span)
            case Compare(first, rest, span):
                # `a < b <= c` evaluates each operand once, stopping at the
                # first comparison that is false.
                left_value = self.eval(first, scope)
                for op, operand in rest:
                    right_value = self.eval(operand, scope)
                    if not self.compare(op, left_value, right_value, span):
                        return False
                    left_value = right_value
                return True
            case Index(target, index, span):
                # Index into the stored list without copying all of it, then
                # copy just the element.
                container = self.reference(target, scope, for_assignment=False)
                return copy_value(self.item(container, self.eval(index, scope), span))
            case Member() if self.checker and id(expr) in self.checker.associated_constants:
                return self.associated_constant(expr, scope)
            case Member():
                return copy_value(self.reference(expr, scope, for_assignment=False))
            case Quote():
                return self.quote(expr, scope)
            case TypeLit(texpr):
                if not has_splices(texpr):
                    return TypeValue(finish(self.tables.type_values[id(expr)]))
                assert self.checker is not None
                return TypeValue(type_from(texpr, self.checker.enums, lambda node: self.eval(node.code, scope)))
            case FieldSplice(span=span):
                raise EllaError("a field name can only be spliced into quoted code", span)
            case Splice(span=span):
                raise EllaError("a splice outside a quote runs while compiling, and this one wasn't", span)
            case Try(operand, span):
                return self.eval_try(self.eval(operand, scope), span)

            # Functions
            case Lambda():
                return Closure(expr, self.capture(scope))
            case Call(Member() as callee, args, span):
                return self.call_member(callee, args, scope, span)
            case Call(callee, args, span):
                function = self.eval(callee, scope)
                if isinstance(function, Constructor) and function.enum in self.typed_enums and not function.type_args:
                    function = replace(function, type_args=self.made_type_args(expr, scope))
                if isinstance(function, Builtin) and function.name in ("alloc", "free"):
                    return self.call_pool(function.name, args, scope, span)
                if isinstance(function, Builtin) and function.name == "next":
                    return self.call_next(args, scope, span)
                if isinstance(function, Builtin) and function.name == "array" and len(args) == 1:
                    return self.make_array(expr, self.eval(args[0], scope), span)
                if tail and isinstance(function, Closure) and all(not isinstance(arg, MutArg) for arg in args):
                    values = [self.eval(arg, scope) for arg in args]
                    if (
                        len(values) == len(function.function.params)
                        and all(not param.mutable for param in function.function.params)
                        and all(isinstance(value, (int, float)) for value in values)
                    ):
                        raise TailCallSignal(function, values, span)
                return self.call_with_args(function, args, scope, span)
            case MutArg(span=span):
                raise EllaError("'mut' marks an argument for a 'mut' parameter, and only works in a call", span)
            case Return(returned, span):
                raise ReturnSignal(UNIT if returned is None else self.eval(returned, scope, tail), span)
            case Yield(value_expr, span):
                return self.yield_value(self.eval(value_expr, scope), span)

            # Control flow
            case Block(exprs):
                return self.eval_block(exprs, scope, tail)
            case If(cond, then, else_):
                if self.eval_bool(cond, scope, "an 'if' condition"):
                    result = self.eval(then, scope, tail and else_ is not None)
                    # Without `else`, `if` has the value (), whatever the body gives.
                    return result if else_ is not None else UNIT
                return self.eval(else_, scope, tail) if else_ is not None else UNIT
            case Match(subject, cases, span):
                return self.eval_match(subject, cases, scope, span, tail)
            case For(label, pattern, MutArg(target), body, span):
                root, indexes = self.evaluate_place(target, scope)
                place = (root, indexes, span)
                iterator = self.read_place(root, indexes, scope)
                return self.eval_for(label, pattern, iterator, body, scope, span, place)
            case For(label, pattern, iterable, body, span):
                return self.eval_for(label, pattern, self.eval(iterable, scope), body, scope, span)
            case While(label, cond, body):
                while True:
                    if (signal := self.run_loop_body(body, scope, label, cond)) is not None:
                        self.check_no_break_value(signal, "while")
                        break
                return UNIT
            case Loop(label, body):
                while True:
                    if (signal := self.run_loop_body(body, scope, label)) is not None:
                        return UNIT if signal.value is None else signal.value
            case Break(label, broken, span):
                raise BreakSignal(label, None if broken is None else self.eval(broken, scope), span)
            case Continue(label, span):
                raise ContinueSignal(label, span)

            # Others
            case Assert():
                self.check_condition(expr, scope, "assertion")
                return UNIT
            case EnumDef():
                self.define_enum(expr, scope)
                return UNIT
            case ConstraintDef():
                return UNIT  # constraints are only for the checker
            case MapLit(span=span) | SetLit(span=span):
                # Linking (ella.modules) makes them calls of the library's map_of and set_of.
                raise EllaError("a map or set literal in a program that wasn't linked", span)

    def eval_bool(self, expr: Expr, scope: Scope, what: str) -> bool:
        """Evaluate an expression that must give a bool, since only bools are truthy."""
        value = self.eval(expr, scope)
        if not isinstance(value, bool):
            raise EllaError(f"{what} needs a bool, not {type_name(value)}", expr.span)
        return value

    def eval_block(self, exprs: tuple[Expr, ...], scope: Scope, tail: bool = False) -> Value:
        """Run a block's lines in a new scope. Its value is the last line's value."""
        local = Scope(scope)
        value = UNIT
        for index, expr in enumerate(exprs):
            value = self.eval(expr, local, tail and index == len(exprs) - 1)
        # A block that ends with an assignment has the value () (docs/grammar.md §2).
        return UNIT if isinstance(exprs[-1], Assign) else value

    # Variables

    def visible(self, name: str, scope: Scope, span: Span) -> Var | None:
        """The variable a name refers to in code at `span`, if any.

        As in the checker: code in a module other than the entry file only
        has built-ins left as bare top-level names (see ella.modules), so it
        gets those, even if the entry file defines the names.
        """
        var = scope.lookup(name)
        if span.file and "." not in name and var is not None and var is self.globals.vars.get(name):
            return self.predefined.get(name)
        return var

    def lookup(self, name: str, scope: Scope, span: Span) -> Var:
        """Find a variable, which must exist and (for top-level names) have had its `let` run."""
        var = self.visible(name, scope, span)
        if var is None:
            raise EllaError(f"undefined name '{name}'", span)
        if not var.ready:
            raise EllaError(f"'{name}' is used before its 'let' has run", span)
        return var

    def lookup_mutable(self, name: str, scope: Scope, span: Span) -> Var:
        """Find a variable that is about to be assigned to, which must be `mut`."""
        var = self.lookup(name, scope, span)
        if not var.mutable:
            raise EllaError(f"can't assign to '{name}' because it isn't mutable; declare it with 'let mut'", span)
        return var

    def reference(self, expr: Expr, scope: Scope, for_assignment: bool) -> Value:
        """The stored value that `expr` names, without copying it.

        For `grid[i][j] = 0` this finds the actual list `grid[i]` so it can be
        changed in place. Assigning needs the variable at the root to be `mut`
        (and `mut` is deep: it covers the elements too). Reading uses this to
        avoid copying a whole list just to read one element.
        """
        match expr:
            case Name(name, span):
                var = self.lookup_mutable(name, scope, span) if for_assignment else self.lookup(name, scope, span)
                return var.value
            case Member() if self.checker and id(expr) in self.checker.associated_constants:
                return self.associated_constant(expr, scope)  # `T.NAME[i]`, `T.NAME.x`: a copy, never changed
            case Index(target, index, span):
                container = self.reference(target, scope, for_assignment)
                return self.item(container, self.eval(index, scope), span)
            case Member(target, name, span):
                struct = self.reference(target, scope, for_assignment)
                if isinstance(struct, tuple) and name.isdigit():
                    if for_assignment:
                        raise EllaError("a tuple can't be changed in place", span)
                    if int(name) >= len(struct):
                        raise EllaError(f"a tuple of {len(struct)} items has no item {name}", span)
                    return struct[int(name)]
                position = self.field_index(struct, name, span)
                assert isinstance(struct, VariantValue)
                return struct.fields[position]
            case _ if for_assignment:
                raise EllaError("can only assign to a variable or a part of one", expr.span)
            case _:
                return self.eval(expr, scope)

    def eval_assign(self, op: str, target: Name | Index | Member, value_expr: Expr, scope: Scope, span: Span) -> Value:
        """`target = value`, or `target op= value`, which means `target = target op value`."""
        value = self.eval(value_expr, scope)
        match target:
            case Name(name, name_span):
                var = self.lookup_mutable(name, scope, name_span)
                if op != "=":
                    value = self.binary(op.removesuffix("="), var.value, value, span)
                var.value = value
            case Index(container_expr, index_expr, index_span):
                root, steps = self.evaluate_place(container_expr, scope)
                if not isinstance(root, Name):
                    raise EllaError("can only assign to a variable or a part of one", container_expr.span)
                container = self.read_place(root, steps, scope)
                index = self.eval(index_expr, scope)
                if (setter := self.find_method(container, "set")) is not None:
                    # `v[k] = x` is `mut v.set(k, x)`, and `v[k] op= x` gets the item first.
                    if op != "=":
                        old = self.call(self.method(container, "get", index_span), [container, index], span)
                        value = self.binary(op.removesuffix("="), old, value, span)
                    finals: list[Value] = []
                    self.call(setter, [copy_value(container), index, value], span, finals)
                    self.write_place(root, steps, finals[0], scope, index_span)
                    return copy_value(value)
                old = self.item(container, index, index_span)
                if op != "=":
                    value = self.binary(op.removesuffix("="), old, value, span)
                self.set_item(container, index, value, index_span)
            case Member(struct_expr, name, member_span):
                struct = self.reference(struct_expr, scope, for_assignment=True)
                assert isinstance(struct, VariantValue)
                i = self.field_index(struct, name, member_span)
                if op != "=":
                    value = self.binary(op.removesuffix("="), struct.fields[i], value, span)
                struct.fields[i] = value
        return copy_value(value)

    def item(self, container: Value, index: Value, span: Span) -> Value:
        """`container[index]`: an item of a list or string, a pool's or arena's value, or what a `get` method gives."""
        if (getter := self.find_method(container, "get")) is not None:
            return self.call(getter, [container, index], span)
        if isinstance(container, PoolValue):
            assert isinstance(index, HandleValue)
            return self.pool_slot(container, index, span)
        if isinstance(container, ArenaValue):
            assert isinstance(index, RefValue)
            return self.arena_slot(container, index, span)
        return index_into(container, index, span)

    def set_item(self, container: Value, index: Value, value: Value, span: Span) -> None:
        if isinstance(container, PoolValue):
            assert isinstance(index, HandleValue)
            self.pool_slot(container, index, span)  # check the handle
            container.slots[index.slot] = value
            return
        if isinstance(container, ArenaValue):
            assert isinstance(index, RefValue)
            self.arena_slot(container, index, span)  # check the ref
            container.values[index.index] = value
            return
        if isinstance(container, ArrayValue):
            container.items[array_offset(container, index, span)] = value
            return
        index_into(container, index, span)  # check that the index is in range
        if not isinstance(container, list):
            raise EllaError(f"can't assign to an element of a {type_name(container)}", span)
        assert isinstance(index, int)
        container[index] = value

    def field_index(self, struct: Value, name: str, span: Span) -> int:
        if not isinstance(struct, VariantValue) or name not in self.structs.get(struct.enum, ()):
            raise EllaError(f"a {type_name(struct)} has no field '{name}'", span)
        return self.structs[struct.enum].index(name)

    # Functions

    def capture(self, scope: Scope) -> Scope:
        """The scope a new closure captures: copies of all local variables.

        Lambdas capture by copy (docs/grammar.md §5), so the closure gets a
        snapshot of the locals as they are now, and later changes to them
        aren't seen. The copies are immutable. Global names aren't copied:
        they stay visible directly, which is how top-level functions see each
        other.
        """
        # Collect the local scopes, innermost first, stopping at the globals.
        chain = []
        current: Scope | None = scope
        while current is not None and current is not self.globals:
            chain.append(current)
            current = current.parent
        captured = Scope(self.globals)
        # Copy outermost first, so that inner variables shadow outer ones.
        for local in reversed(chain):
            for name, var in local.vars.items():
                captured.define(name, copy_value(var.value))
        return captured

    def call_with_args(
        self,
        function: Value,
        args: tuple[Expr, ...],
        scope: Scope,
        span: Span,
        receiver: tuple[Value, Place | None] | None = None,
    ) -> Value:
        """Evaluate a call's arguments and call the function, passing `mut` arguments in and out ("value-result").

        Each `mut` argument's place is found when the arguments are evaluated:
        the variable, and the values of any indexes. The function gets a copy
        of the place's value in a `mut` parameter, and when it returns, the
        parameter's final value is assigned back to the place, as `place =
        value` would. The compiled code passes the variable by reference
        instead, which no program can tell apart (docs/grammar.md §5).

        A method call's `receiver` is the value it is called on, already
        evaluated, and the place it was read from if it is `mut`.
        """
        values: list[Value] = []
        places: list[Place] = []
        if receiver is not None:
            value, place = receiver
            values.append(value)
            if place is not None:
                places.append(place)
        for arg in args:
            if isinstance(arg, MutArg):
                root, indexes = self.evaluate_place(arg.target, scope)
                places.append((root, indexes, arg.span))
                values.append(copy_value(self.read_place(root, indexes, scope)))
            else:
                values.append(self.eval(arg, scope))
        if not places:
            return self.call(function, values, span)
        finals: list[Value] = []
        result = self.call(function, values, span, finals)
        for (root, indexes, place_span), final in zip(places, finals):
            self.write_place(root, indexes, final, scope, place_span)
        return result

    # Associated constants

    def with_type_args(
        self, closure: Closure, params: tuple[str, ...], type_args: tuple[Type, ...], scope: Scope
    ) -> Closure:
        """A generic function that knows its type arguments, for `T.NAME` in it: each is `type T` in its scope.

        Only programs with associated constants need them. The same function
        with the same types gives the same closure, so that a tail call to
        itself still reuses its frame.
        """
        concrete = tuple(self.bound_type(t, scope) for t in type_args)
        key = (id(closure), concrete)
        if (known := self.typed_closures.get(key)) is None:
            inner = Scope(closure.scope, {f"type {p}": Var(TypeValue(t), False) for p, t in zip(params, concrete)})
            known = self.typed_closures[key] = (closure, Closure(closure.function, inner))
        return known[1]

    def bound_type(self, t: Type, scope: Scope) -> Type:
        """A type the checker found, with the type parameters of the generic functions running filled in."""
        t = finish(t)
        bound: dict[str, Type] = {}
        for name in param_names(t):
            if (var := scope.lookup(f"type {name}")) is not None and isinstance(var.value, TypeValue):
                bound[name] = var.value.type
        return substitute(t, bound) if bound else t

    def associated_constant(self, expr: Member, scope: Scope) -> Value:
        """`T.NAME`: the constant T's implementation gives, computed here (as the lowering does)."""
        assert self.checker is not None
        param, constraint, name = self.checker.associated_constants[id(expr)]
        var = scope.lookup(f"type {param}")
        assert var is not None and isinstance(var.value, TypeValue), f"{param} has no type argument"
        t = var.value.type
        if not isinstance(t, EnumTy):  # a built-in type's (checker.BUILTIN_CONSTANTS)
            return [] if isinstance(t, ListTy) else BUILTIN_CONSTANT_VALUES[show_type(t), name]
        return self.constant_value(t.name, self.checker.implementation_of(t.name, constraint), name)

    def constant_value(self, type_name: str, constraint: str, name: str) -> Value:
        """The value a type's implementation of a constraint gives a constant, computed the first time it is
        needed. While compiling (ella.staging) that is when the constant is written into the code, which
        then holds it as a literal."""
        assert self.checker is not None
        key = (type_name, constraint, name)
        if key not in self.constants:
            expr = self.checker.implementation_constants[key]
            if key in self.computing:
                raise EllaError(f"constant {type_name}.{name} needs its own value to be computed", expr.span)
            self.computing.add(key)
            try:
                if self.prepare_code is not None:
                    expr = self.prepare_code(expr)
                self.constants[key] = self.eval(expr, Scope(self.globals))
            finally:
                self.computing.discard(key)
        return copy_value(self.constants[key])

    def compute_constant(self, type_name: str, constraint: str, name: str) -> Value:
        """A constant's value, computed while compiling."""

        def run() -> Value:
            self.owners.append([])
            result = self.constant_value(type_name, constraint, name)
            self.close_owner(result)
            return result

        return self.compile_time(run)

    # Methods

    def call_member(self, callee: Member, args: tuple[Expr, ...], scope: Scope, span: Span) -> Value:
        """`v.name(args)`: v's method `name`, called with v first, or a function held in v's field `name`.

        In `mut v.name(args)`, the method changes v.
        """
        if self.checker is not None:
            if (static := self.checker.static_methods.get(id(callee))) is not None:
                enum_name, name, static_args = static
                namespace_function = self.methods[enum_name][name]
                if enum_name in self.typed_enums and isinstance(namespace_function, Closure):
                    params = self.checker.enums[enum_name].params
                    namespace_function = self.with_type_args(namespace_function, params, static_args, scope)
                return self.call_with_args(namespace_function, args, scope, span)
            if (function_name := self.checker.builtin_methods.get(id(callee))) is not None:
                builtin_function = self.lookup(function_name, scope, callee.span).value
                assert builtin_function is not None
                return self.call_with_args(builtin_function, (callee.target, *args), scope, span)
            if (function_name := self.checker.extension_methods.get(id(callee))) is not None:
                extension_function = self.lookup(function_name, scope, callee.span).value
                assert extension_function is not None
                type_args = self.checker.instances.get(id(callee))
                if isinstance(extension_function, Closure) and type_args and self.checker.associated_constants:
                    params = self.checker.globals.vars[function_name].type_params
                    extension_function = self.with_type_args(extension_function, params, type_args, scope)
                return self.call_with_args(extension_function, (callee.target, *args), scope, span)
        receiver, name = callee.target, callee.name
        if isinstance(receiver, MutArg):
            root, indexes = self.evaluate_place(receiver.target, scope)
            value = copy_value(self.read_place(root, indexes, scope))
            function = self.method(value, name, callee.span)
            return self.call_with_args(function, args, scope, span, (value, (root, indexes, receiver.span)))
        value = self.eval(receiver, scope)
        if isinstance(value, Constructor | VariantValue):
            static_function = self.methods.get(value.enum, {}).get(name)
            if isinstance(static_function, Closure) and not self.has_instance_receiver(static_function, value.enum):
                return self.call_with_args(static_function, args, scope, span)
        if (
            self.find_method(value, name) is None
            and isinstance(value, VariantValue)
            and name in self.structs.get(value.enum, ())
        ):
            return self.call_with_args(value.fields[self.field_index(value, name, callee.span)], args, scope, span)
        if self.checker is None and self.find_method(value, name) is None:
            item = prelude().get(name)
            if name == "len":
                length_function = self.lookup(name, scope, callee.span).value
                assert length_function is not None
                return self.call_with_args(length_function, (receiver, *args), scope, span)
            if name in LIBRARY:
                function_name = name
            elif item is not None and item.kind == "function":
                function_name = item.name
            else:
                function_name = ""
            if function_name:
                receiver_function = self.lookup(function_name, scope, callee.span).value
                assert receiver_function is not None
                return self.call_with_args(receiver_function, (receiver, *args), scope, span)
        return self.call_with_args(self.method(value, name, callee.span), args, scope, span, (value, None))

    def has_instance_receiver(self, function: Closure, enum: str) -> bool:
        """Whether a type-body function's first parameter makes it an instance method."""
        if not function.function.params:
            return False
        parameter_type = function.function.params[0].type
        if isinstance(parameter_type, NamedType):
            return parameter_type.name == enum
        return False

    def text(self, value: Value) -> str:
        """A value's text form, as `print` writes it: through the `text` methods of the types in it."""

        def custom(variant: VariantValue) -> str | None:
            method = self.find_method(variant, "text")
            if method is None:
                return None
            result = self.call(method, [variant], TEXT_SITE)
            assert isinstance(result, str)
            return result

        return to_text(value, custom=custom)

    def find_method(self, value: Value, name: str) -> Value | None:
        """The method `name` of a struct or enum value, if its type has one."""
        if isinstance(value, VariantValue):
            method = self.methods.get(value.enum, {}).get(name)
            return None if method is None else self.typed_method(value, method)
        return None

    def typed_method(self, value: VariantValue, method: Value) -> Value:
        """A method of value's type that knows the type arguments value carries, for `T.NAME` in it."""
        if not value.type_args or not isinstance(method, Closure):
            return method
        assert self.checker is not None
        params = self.checker.enums[value.enum].params
        return self.with_type_args(method, params, value.type_args, self.globals)

    def made_type_args(self, expr: Expr, scope: Scope) -> tuple[Type, ...]:
        """The type arguments of the values a constructor, its call, or a variant without fields (`expr`) makes."""
        assert self.checker is not None
        t = finish(self.checker.types[id(expr)])
        if isinstance(t, FuncTy):
            t = finish(t.result)
        assert isinstance(t, EnumTy), f"{show_type(t)} isn't a struct or enum"
        return tuple(self.bound_type(arg, scope) for arg in t.args)

    def method(self, value: Value, name: str, span: Span) -> Value:
        method = self.find_method(value, name)
        if method is None:
            raise EllaError(f"a {type_name(value)} has no method '{name}'", span)
        return method

    def binary(self, op: str, left: Value, right: Value, span: Span) -> Value:
        """An arithmetic operator, which a struct or enum may give with a method (`a + b` is `a.add(b)`)."""
        if op in OPERATOR_METHODS and (method := self.find_method(left, OPERATOR_METHODS[op])) is not None:
            return self.call(method, [left, right], span)
        return binary(op, left, right, span)

    def compare(self, op: str, left: Value, right: Value, span: Span) -> bool:
        """A comparison. `a < b` on a struct or enum with a `compare` method is `a.compare(b) < 0`, and
        `a in b` is `b.contains(a)` if b has a `contains` method."""
        if op in ("in", "not in") and (method := self.find_method(right, "contains")) is not None:
            found = self.call(method, [right, left], span)
            assert isinstance(found, bool)
            return found if op == "in" else not found
        if op in ORDERING and (method := self.find_method(left, "compare")) is not None:
            order = self.call(method, [left, right], span)
            assert isinstance(order, int)
            return ORDERING[op](order)
        return compare(op, left, right, span, self.custom_equal)

    def custom_equal(self, a: VariantValue, b: VariantValue) -> bool | None:
        """Whether two struct or enum values are equal by their type's `eq` method, if it has one."""
        method = self.find_method(a, "eq")
        if method is None:
            return None
        result = self.call(method, [a, b], EQ_SITE)
        assert isinstance(result, bool)
        return result

    def custom_hash(self, value: VariantValue) -> int | None:
        """A struct or enum value's hash by its type's `hash` method, if it has one."""
        method = self.find_method(value, "hash")
        if method is None:
            return None
        result = self.call(method, [value], HASH_SITE)
        assert isinstance(result, int)
        return result

    def evaluate_place(self, target: Expr, scope: Scope) -> tuple[Expr, list[tuple[Value | Member, Span]]]:
        """The root variable of `xs[i].f[j]`, and its indexes and fields, left to right, with their spans.

        A field is given by its name, as a Member node.
        """
        if isinstance(target, Index):
            root, indexes = self.evaluate_place(target.target, scope)
            return root, [*indexes, (self.eval(target.index, scope), target.span)]
        if isinstance(target, Member):
            root, indexes = self.evaluate_place(target.target, scope)
            return root, [*indexes, (target, target.span)]
        return target, []

    def step(self, container: Value, index: Value | Member, span: Span) -> Value:
        """One step into a place: an item, or a field (given as its Member node)."""
        if isinstance(index, Member):
            position = self.field_index(container, index.name, span)
            assert isinstance(container, VariantValue)
            return container.fields[position]
        return self.item(container, index, span)

    def read_place(self, root: Expr, indexes: list[tuple[Value | Member, Span]], scope: Scope) -> Value:
        assert isinstance(root, Name)
        value = self.lookup_mutable(root.name, scope, root.span).value
        for index, span in indexes:
            value = self.step(value, index, span)
        return value

    def write_place(
        self, root: Expr, indexes: list[tuple[Value | Member, Span]], value: Value, scope: Scope, span: Span
    ) -> None:
        """Assign to a place found earlier by `evaluate_place`, checking its indexes again."""
        assert isinstance(root, Name)
        var = self.lookup_mutable(root.name, scope, root.span)
        if not indexes:
            var.value = value
            return
        container = var.value
        for index, _ in indexes[:-1]:
            container = self.step(container, index, span)
        last, _ = indexes[-1]
        if isinstance(last, Member):
            position = self.field_index(container, last.name, span)
            assert isinstance(container, VariantValue)
            container.fields[position] = value
        else:
            self.set_item(container, last, value, span)

    def call(self, function: Value, args: list[Value], span: Span, finals: list[Value] | None = None) -> Value:
        """Call a function value with already evaluated arguments.

        The final values of `mut` parameters are appended to `finals`.
        """
        match function:
            case Builtin(name, arity, run):
                if arity >= 0:  # print and eprint take any number
                    self.check_arity(name, arity, args, span)
                return run(args, span)
            case Constructor(error=True, at=at):
                if len(args) not in (1, 2):
                    raise EllaError(f"Error takes 1 or 2 arguments but was given {len(args)}", span)
                assert isinstance(args[0], str)
                return make_error(args[0], at or span, args[1] if len(args) == 2 else None)
            case Constructor(enum, variant, arity, type_args=type_args):
                self.check_arity(variant, arity, args, span)
                value = VariantValue(enum, variant, list(args), nocopy=enum in self.nocopy, type_args=type_args)
                if enum in self.finalizers:
                    self.owners[-1].append(value)
                return value
            case Closure(lambda_, captured):
                self.check_arity("function", len(lambda_.params), args, span)
                name = self.names.get(id(lambda_), "lambda")  # the name of the function as written
                if self.prepare_function is not None:
                    lambda_ = self.prepare_function(lambda_)
                return self.call_closure(lambda_, captured, args, span, name, finals)
        raise EllaError(f"can't call a value of type {type_name(function)}", span)

    def check_arity(self, name: str, arity: int, args: list[Value], span: Span) -> None:
        if len(args) != arity:
            plural = "" if arity == 1 else "s"
            raise EllaError(f"{name} takes {arity} argument{plural} but was given {len(args)}", span)

    def call_closure(
        self,
        lambda_: Lambda,
        captured: Scope,
        args: list[Value],
        span: Span,
        name: str,
        finals: list[Value] | None = None,
    ) -> Value:
        """Run a function's body with its parameters bound, checking its `where` conditions.

        `name` is the function's, for stack traces.

        Calling a generator only makes its GenValue (`make_generator`).
        """
        if self.depth >= self.max_depth:
            raise EllaError(f"stack overflow: more than {self.max_depth} nested calls", span)
        self.calls.append(CallSite(span, self.running) if self.running is not None else None)
        caller, self.running = self.running, name
        try:
            if is_generator(lambda_):
                return self.make_generator(lambda_, captured, args)
            return self.run_closure(lambda_, captured, args, finals)
        except EllaError as error:
            error.traced(self.trace())
            raise
        finally:
            self.calls.pop()
            self.running = caller

    def trace(self) -> list[CallSite]:
        """The calls in progress that a stack trace shows, outermost first."""
        return [call for call in self.calls if call is not None]

    def run_closure(self, lambda_: Lambda, captured: Scope, args: list[Value], finals: list[Value] | None) -> Value:
        # Parameters live in a new scope inside the captured one. Only `mut` ones can change.
        local = Scope(captured)
        for param, arg in zip(lambda_.params, args):
            local.define(param.name, arg, param.mutable)
        self.check_shapes(lambda_, local)

        postconditions = [cond for cond in lambda_.where if mentions_return(cond.expr)]
        self.owners.append([])
        for cond in lambda_.where:
            if cond not in postconditions:
                self.check_condition(cond, local, "precondition")

        self.depth += 1
        try:
            while True:
                try:
                    result = self.eval(lambda_.body, local, tail=not lambda_.where)
                    break
                except ReturnSignal as signal:
                    result = signal.value
                    break
                except TailCallSignal as signal:
                    if signal.closure.function is not lambda_ or signal.closure.scope is not captured:
                        result = self.call(signal.closure, signal.call_args, signal.span)
                        break
                    # Reuse this frame for a self-tail-call instead of growing Python's stack.
                    self.close_owner()
                    self.owners.append([])
                    for param, param_value in zip(lambda_.params, signal.call_args):
                        local.vars[param.name].value = param_value
                    self.check_shapes(lambda_, local)
                    if self.calls and self.calls[-1] is not None:
                        self.calls[-1] = CallSite(signal.span, self.running or "")
                    for cond in lambda_.where:
                        if cond not in postconditions:
                            self.check_condition(cond, local, "precondition")
        # Loops can't be exited from inside a function called in their body.
        except BreakSignal as signal:
            raise EllaError("'break' outside of a loop", signal.span) from None
        except ContinueSignal as signal:
            raise EllaError("'continue' outside of a loop", signal.span) from None
        finally:
            self.depth -= 1

        if self.checker and (check := self.checker.result_shapes.get(id(lambda_))) is not None:
            self.check_shape(check, result, local)
        # Postconditions see the result as `return`.
        if postconditions:
            result_scope = Scope(local)
            result_scope.define("return", result)
            for cond in postconditions:
                self.check_condition(cond, result_scope, "postcondition")
        if finals is not None:
            finals.extend(local.vars[param.name].value for param in lambda_.params if param.mutable)
        self.close_owner(result)  # the result belongs to the caller
        return result

    # Finalizers

    def finalizable(self, value: Value) -> list[Value]:
        """The values with finalizers that `value` holds, itself included: those that move with it."""
        match value:
            case VariantValue(enum, fields=fields):
                inner = [v for item in fields for v in self.finalizable(item)]
                return ([value] if enum in self.finalizers else []) + inner
            case PoolValue() | ArenaValue() | GenValue():
                return [value]
            case list() | tuple():
                return [v for item in value for v in self.finalizable(item)]
        return []

    def adopt(self, value: Value, source: int, target: int) -> None:
        """Move the values with finalizers that `value` holds from one owner to another."""
        moving = {id(v) for v in self.finalizable(value)}
        if not moving:
            return
        self.owners[target].extend(v for v in self.owners[source] if id(v) in moving)
        self.owners[source] = [v for v in self.owners[source] if id(v) not in moving]

    def close_owner(self, keep: Value | None = None) -> None:
        """End the innermost owner, finalizing its values (newest first), except those `keep` holds."""
        if keep is not None and len(self.owners) > 1:
            self.adopt(keep, -1, -2)
        failure: EllaError | None = None
        for value in reversed(self.owners.pop()):
            try:
                self.finalize(value)
            except EllaError as error:
                if failure is None:
                    failure = error
        if failure is not None and not self.unwinding:
            raise failure

    def finalize(self, value: Value) -> None:
        """Run a value's finalizer; a pool finalizes its values, from the last slot to the first, and an arena
        its values, newest first."""
        if isinstance(value, PoolValue):
            for slot in reversed(value.slots):
                if slot is not None:
                    self.finalize_held(slot)
        elif isinstance(value, ArenaValue):
            for held in reversed(value.values):
                self.finalize_held(held)
        elif isinstance(value, GenValue):
            assert isinstance(value.state, Generator)
            self.close_generator(value.state)
        elif isinstance(value, VariantValue) and value.enum in self.finalizers:
            self.call(self.typed_method(value, self.finalizers[value.enum]), [value], FINALIZER_SITE)

    def finalize_held(self, value: Value) -> None:
        """Finalize everything a value holds: itself first, then its parts, last first.

        That is the reverse of the order they were made in, since parts are
        made before what holds them.
        """
        match value:
            case VariantValue(fields=fields):
                self.finalize(value)
                for item in reversed(fields):
                    self.finalize_held(item)
            case PoolValue() | ArenaValue() | GenValue():
                self.finalize(value)
            case list() | tuple():
                for item in reversed(value):
                    self.finalize_held(item)

    # Generators

    def make_generator(self, function: Lambda, captured: Scope, args: list[Value]) -> Value:
        """Call a generator: check its preconditions, and make its GenValue, which takes the arguments.

        Its parameters are its own variables, which it can change. The values
        with finalizers that the arguments hold now belong to the generator.
        """
        local = Scope(captured)
        for param, arg in zip(function.params, args):
            local.define(param.name, arg, True)
        self.check_shapes(function, local)
        for cond in function.where:
            self.check_condition(cond, local, "precondition")
        generator = Generator(function, local, self.running or "lambda")
        for arg in args:
            moving = {id(v) for v in self.finalizable(arg)}
            for owner in range(len(self.owners)):
                generator.owners[0] += [v for v in self.owners[owner] if id(v) in moving]
                self.owners[owner] = [v for v in self.owners[owner] if id(v) not in moving]
        value = GenValue(generator)
        self.owners[-1].append(value)
        return value

    def resume(self, generator: Generator, span: Span) -> Value:
        """`next` on a generator: run its body until it yields (Some(value)) or finishes (None, from then on)."""
        if generator.done:
            return VariantValue("Optional", "None")
        if self.depth >= self.max_depth:
            raise EllaError(f"stack overflow: more than {self.max_depth} nested calls", span)
        self.calls.append(CallSite(span, self.running) if self.running is not None else None)
        caller, self.running = self.running, generator.name
        try:
            self.switch(generator, "resume")
            kind, value = generator.outcome
            if kind == "error":
                assert isinstance(value, BaseException)
                if isinstance(value, EllaError):
                    value.traced(self.trace())
                raise value
        finally:
            self.calls.pop()
            self.running = caller
        if kind == "end":
            generator.done = True
            return VariantValue("Optional", "None")
        assert value is not None and not isinstance(value, BaseException)
        return VariantValue("Optional", "Some", [value])

    def switch(self, generator: Generator, why: str) -> None:
        """Hand control to a generator's thread (starting it the first time), until it hands it back.

        While its body runs, the generator's owner stack is the interpreter's.
        """
        saved = self.owners, self.generator, self.depth
        self.owners, self.generator = generator.owners, generator
        self.depth += 1
        try:
            if generator.thread is None:
                size = threading.stack_size(max(64 << 20, self.max_depth * (16 << 10)))
                try:
                    generator.thread = threading.Thread(target=self.run_generator, args=(generator,), daemon=True)
                    generator.thread.start()
                finally:
                    threading.stack_size(size)
            else:
                generator.go.release()
            generator.back.acquire()
        finally:
            self.owners, self.generator, self.depth = saved

    def run_generator(self, generator: Generator) -> None:
        """A generator's thread: run its body, then hand control back with how it ended."""
        try:
            try:
                self.eval(generator.function.body, generator.scope)
            except ReturnSignal:
                pass
            except BreakSignal as signal:
                raise EllaError("'break' outside of a loop", signal.span) from None
            except ContinueSignal as signal:
                raise EllaError("'continue' outside of a loop", signal.span) from None
            while self.owners:
                self.close_owner()  # the body's values, then the arguments it took
            generator.outcome = ("end", None)
        except GeneratorClosed:
            generator.outcome = ("closed", None)
        except BaseException as error:  # noqa: BLE001  (raised again in the thread that called next)
            # The generator's thread has stopped, so its owners must be
            # unwound here. Mark it done before handing the error back: the
            # caller's error cleanup will see the GenValue and must not try
            # to resume this already-dead thread while closing it.
            self.unwinding = True
            try:
                while self.owners:
                    self.close_owner()
            finally:
                self.unwinding = False
            generator.done = True
            generator.outcome = ("error", error)
        generator.back.release()

    def yield_value(self, value: Value, span: Span) -> Value:
        """`yield value`, in a generator's thread: hand the value to `next`, and wait for the next `next`."""
        generator = self.generator
        if generator is None:
            raise EllaError("'yield' can only be used in a generator", span)
        generator.outcome = ("yield", value)
        generator.back.release()
        generator.go.acquire()
        if generator.closing:
            raise GeneratorClosed
        return UNIT

    def close_generator(self, generator: Generator) -> None:
        """A generator dies. If its body hasn't finished, its values are finalized, innermost owner first,
        and its thread ends without running anything more."""
        if generator.done:
            return
        generator.done = True
        saved = self.owners
        self.owners = generator.owners
        try:
            while self.owners:
                self.close_owner()
        finally:
            self.owners = saved
        if generator.thread is not None:
            generator.closing = True
            self.switch(generator, "close")

    def call_next(self, args: tuple[Expr, ...], scope: Scope, span: Span) -> Value:
        """`next(mut g)`: the next item of a generator, or of an iterator, by its `next` method."""
        if len(args) != 1 or not isinstance(args[0], MutArg):
            raise EllaError("next() takes one generator or iterator, marked 'mut'", span)
        root, indexes = self.evaluate_place(args[0].target, scope)
        return self.advance(self.read_place(root, indexes, scope), (root, indexes, args[0].span), scope, span)

    def advance(self, iterator: Value, place: Place | list[Value], scope: Scope, span: Span) -> Value:
        """The next item of a generator or an iterator, as an Optional.

        An iterator's `next` method changes it, and its new value goes back to
        `place`, or to the one-item list holding a loop's copy of it; a
        generator changes in place.
        """
        if isinstance(iterator, GenValue):
            assert isinstance(iterator.state, Generator)
            return self.resume(iterator.state, span)
        method = self.find_method(iterator, "next")
        if method is None:
            raise EllaError(f"next() needs a generator or an iterator, not a {type_name(iterator)}", span)
        finals: list[Value] = []
        result = self.call(method, [copy_value(iterator)], span, finals)
        if isinstance(place, list):
            place[0] = finals[0]
        else:
            root, indexes, place_span = place
            self.write_place(root, indexes, finals[0], scope, place_span)
        return result

    # Pools

    def new_pool(self) -> Value:
        pool = PoolValue(id=self.next_pool_id)
        self.next_pool_id += 1
        self.owners[-1].append(pool)
        return pool

    def pool_slot(self, pool: PoolValue, handle: HandleValue, span: Span) -> Value:
        """The value a handle refers to, which must still be there."""
        if handle.pool != pool.id:
            raise EllaError("handle belongs to another pool", span)
        if handle.slot < 0 or handle.slot >= len(pool.slots) or pool.generations[handle.slot] != handle.generation:
            raise EllaError(f"handle {to_text(handle)} is stale: its value was freed", span)
        value = pool.slots[handle.slot]
        assert value is not None
        return value

    def new_arena(self) -> Value:
        arena = ArenaValue()
        self.owners[-1].append(arena)
        return arena

    def arena_slot(self, arena: ArenaValue, ref: RefValue, span: Span) -> Value:
        """The value a ref refers to, which must be in this arena and not freed."""
        if (
            ref.arena != arena.id
            or ref.generation != arena.generation
            or ref.index < 0
            or ref.index >= len(arena.values)
        ):
            raise EllaError(f"ref {to_text(ref)} is stale: its arena was freed, or it is another arena's", span)
        return arena.values[ref.index]

    def call_pool(self, name: str, args: tuple[Expr, ...], scope: Scope, span: Span) -> Value:
        """`alloc(mut pool, value)`, `free(mut pool, handle)`, `alloc(mut arena, value)` and `free(mut arena)`.

        They change the pool or arena in place.
        """
        pool_arg, *rest = args
        assert isinstance(pool_arg, MutArg)
        root, indexes = self.evaluate_place(pool_arg.target, scope)
        pool = self.read_place(root, indexes, scope)
        if isinstance(pool, ArenaValue) and name == "free":
            # Every value goes at once, newest first.
            self.finalize(pool)
            pool.values = []
            pool.generation += 1
            return UNIT
        value = self.eval(rest[0], scope)
        if name == "alloc":
            if isinstance(pool, PoolValue) and not pool.free and len(pool.slots) == MAX_POOL_SLOTS:
                raise EllaError("a pool holds too many values", span)
            # The pool or arena owns the value from now on, and finalizes it when it is freed.
            for owner in range(len(self.owners)):
                moving = {id(v) for v in self.finalizable(value)}
                self.owners[owner] = [v for v in self.owners[owner] if id(v) not in moving]
            if isinstance(pool, ArenaValue):
                pool.values.append(value)
                return RefValue(pool.id, pool.generation, len(pool.values) - 1)
            assert isinstance(pool, PoolValue)
            if pool.free:
                slot = pool.free.pop()
                pool.slots[slot] = value
            else:
                slot = len(pool.slots)
                pool.slots.append(value)
                pool.generations.append(0)
            return HandleValue(slot, pool.generations[slot], pool.id)
        assert isinstance(pool, PoolValue) and isinstance(value, HandleValue)
        held = self.pool_slot(pool, value, span)
        self.finalize_held(held)
        pool.slots[value.slot] = None
        if pool.generations[value.slot] < MAX_HANDLE_GENERATION - 1:
            pool.generations[value.slot] += 1
            pool.free.append(value.slot)
        return UNIT

    # Staging (see ella.staging)

    def quote(self, quote: Quote, scope: Scope) -> Value:
        """Build the code a `quote` gives, running its splices now."""

        def splice(node: AnySplice) -> object:
            return splice_value(node, self.eval(node.code, scope))

        def persist(name: Name) -> Expr:
            return reify(copy_value(self.lookup(name.name, scope, name.span).value), name.span)

        # Quotes inside this one's splices may use its variables by identity.
        builder = start_code(quote, self.tables)
        self.quote_symbols.append(builder.symbols)
        try:
            return CodeValue(builder.build(splice, persist, self.quote_symbols[:-1]))
        finally:
            self.quote_symbols.pop()

    def builtin_reflect(self, name: str) -> Callable[[list[Value], Span], Value]:
        def run(args: list[Value], span: Span) -> Value:
            if self.checker is None:
                raise EllaError(f"{name}() needs a checked program", span)
            return reflect(name, args, self.checker.enums, span)

        return run

    def builtin_library(self, name: str) -> Callable[[list[Value], Span], Value]:
        if name == "hash":  # through `hash` methods
            return lambda args, span: hash_value(args[0], self.custom_hash)
        return lambda args, span: call_library(name, args, span, self.host)

    def builtin_run(self, args: list[Value], span: Span) -> Value:
        """`run(code)`: check the code, then run it like the body of a function with no parameters."""
        code = args[0]
        assert isinstance(code, CodeValue)
        if self.checker is None:
            raise EllaError("run() needs a checked program", span)
        expr = materialize_hygiene(code.expr, self.checker.globals.vars)
        self.tables.add(check_code(expr, self.checker))
        return self.run_code(expr)

    def run_code(self, expr: Expr) -> Value:
        self.owners.append([])
        try:
            result = self.eval(expr, Scope(self.globals))
        except ReturnSignal as signal:
            raise EllaError("'return' outside of a function", signal.span) from None
        self.close_owner(result)
        return result

    def run_compile_time(self, expr: Expr) -> Value:
        """Run the code of a splice outside quotes, while compiling (see ella.staging)."""
        return self.compile_time(lambda: self.run_code(expr))

    def compile_time(self, run: Callable[[], Value]) -> Value:
        try:
            return run_deep(run, self.max_depth, PYTHON_FRAMES_PER_CALL)
        except Exit as stop:
            raise EllaError("exit() can't stop the program while it is being compiled", stop.span) from None

    def check_shapes(self, node: Lambda | Let, scope: Scope) -> None:
        """Check the shapes of a function's parameters (with `scope` its call's) or of a `let`'s variables, and
        define the size names they give (docs/grammar.md §9, "Static shapes")."""
        if self.checker is None:
            return
        for check in self.checker.shape_checks.get(id(node), ()):
            var = scope.lookup(check.name)
            assert var is not None
            self.check_shape(check, var.value, scope)

    def check_shape(self, check: ShapeCheck, array: Value, scope: Scope) -> None:
        expected: list[Value] = []
        for size in check.expected:
            var = scope.lookup(size) if isinstance(size, str) else None
            expected.append(var.value if var is not None else size)
        shape = call_library("match_shape", [array, expected, check.what], check.span, self.host)
        assert isinstance(shape, list)
        for name, i in check.binds:
            scope.define(name, shape[i])

    def check_condition(self, cond: Assert, scope: Scope, kind: str) -> None:
        """Check an `assert` or `where` condition, reporting its source if it is false."""
        if not self.eval_bool(cond.expr, scope, f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"):
            raise EllaError(f"{kind} failed: {cond.source}", cond.span)

    def eval_try(self, value: Value, span: Span) -> Value:
        """Postfix `?`: unwrap `Ok(v)` or `Some(v)`, or return the `Err`/`None` from the function."""
        match value:
            case VariantValue("Result", "Ok", (inner,)) | VariantValue("Optional", "Some", (inner,)):
                return inner
            case VariantValue("Result", "Err") | VariantValue("Optional", "None"):
                raise ReturnSignal(value, span)
        raise EllaError(f"'?' needs a Result or Optional, not {type_name(value)}", span)

    # Patterns

    def match_pattern(self, pattern: Pattern, value: Value, scope: Scope) -> dict[str, Value] | None:
        """Match a value against a pattern.

        Returns the variables the pattern binds (possibly none), or None if
        the value doesn't match.
        """
        match pattern:
            case WildcardPattern():
                return {}
            case NamePattern(name, span):
                # A bare name is a variant (like `None` or `Empty`) if one by
                # that name is in scope, and otherwise a new binding.
                var = self.visible(name, scope, span)
                if var is not None and isinstance(var.value, VariantValue) and var.value.variant == name:
                    return {} if equal(value, var.value) else None
                return {name: value}
            case LiteralPattern(literal):
                return {} if equal(value, literal) else None
            case RangePattern(low, high, inclusive):
                if type_name(value) != type_name(low) or type_name(value) not in ("i64", "f64"):
                    return None
                assert (
                    isinstance(value, int | float) and isinstance(low, int | float) and isinstance(high, int | float)
                )
                in_range = low <= value <= high if inclusive else low <= value < high
                return {} if in_range else None
            case OrPattern(alternatives):
                for alternative in alternatives:
                    if (bindings := self.match_pattern(alternative, value, scope)) is not None:
                        return bindings
                return None
            case VariantPattern(name, args):
                # An Error's pattern may leave out its last fields (the checker allows that only for it).
                if not isinstance(value, VariantValue) or value.variant != name or len(value.fields) < len(args):
                    return None
                return self.match_all(args, value.fields, scope)
            case TuplePattern(items):
                if not isinstance(value, tuple) or len(value) != len(items):
                    return None
                return self.match_all(items, value, scope)

    def match_all(
        self, patterns: tuple[Pattern, ...], values: Sequence[Value], scope: Scope
    ) -> dict[str, Value] | None:
        """Match each value against the pattern in the same position; all must match."""
        bindings: dict[str, Value] = {}
        for pattern, value in zip(patterns, values):
            matched = self.match_pattern(pattern, value, scope)
            if matched is None:
                return None
            bindings |= matched
        return bindings

    def eval_match(
        self, subject_expr: Expr, cases: tuple[Case, ...], scope: Scope, span: Span, tail: bool = False
    ) -> Value:
        """Try each case in order; the first whose pattern matches (and guard holds) wins."""
        subject = self.eval(subject_expr, scope)
        for case in cases:
            bindings = self.match_pattern(case.pattern, subject, scope)
            if bindings is None:
                continue
            # The pattern's variables are visible in the guard and the body.
            case_scope = Scope(scope)
            define_all(case_scope, bindings, mutable_names(case.pattern))
            if case.guard is not None and not self.eval_bool(case.guard, case_scope, "a case guard"):
                continue
            return self.eval(case.body, case_scope, tail)
        raise EllaError(f"no case matches {to_text(subject, nested=True)}", span)

    # Loops

    def run_loop_body(
        self, body: Expr, scope: Scope, label: str | None, cond: Expr | None = None
    ) -> BreakSignal | None:
        """Run one iteration of a loop body (after its `while` condition, if any).

        Returns a BreakSignal if the loop should stop. A `break` or `continue`
        for an outer loop (with a different label) isn't ours, so it is
        raised again for the outer loop to catch. The iteration owns the
        values with finalizers it makes, except a value given to `break` or
        `return`, which goes to the owner outside it.
        """
        self.owners.append([])
        try:
            if cond is not None and not self.eval_bool(cond, scope, "a 'while' condition"):
                self.close_owner()
                return BreakSignal(label, None, cond.span)
            self.eval(body, scope)
        except BreakSignal as signal:
            self.close_owner(signal.value)
            if signal.label not in (None, label):
                raise
            return signal
        except ContinueSignal as signal:
            self.close_owner()
            if signal.label not in (None, label):
                raise
            return None
        except ReturnSignal as signal:
            self.close_owner(signal.value)
            raise
        self.close_owner()
        return None

    def items_of(self, iterator: Value, place: Place | None, scope: Scope, span: Span) -> Iterator[Value]:
        """The items of a generator or an iterator, asking for each as the loop needs it.

        An iterator not at a `place` is a copy, which the loop keeps.
        """
        holder = [copy_value(iterator)]
        while True:
            if place is None:
                result = self.advance(holder[0], holder, scope, span)
            else:
                result = self.advance(self.read_place(place[0], place[1], scope), place, scope, span)
            assert isinstance(result, VariantValue)
            if result.variant == "None":
                return
            yield result.fields[0]

    def check_no_break_value(self, signal: BreakSignal, loop: str) -> None:
        """Only `loop` has a value; `for` and `while` are always ()."""
        if signal.value is not None:
            raise EllaError(f"only 'loop' can break with a value, not '{loop}'", signal.span)

    def eval_for(
        self,
        label: str | None,
        pattern: Pattern,
        iterable: Value,
        body: Expr,
        scope: Scope,
        span: Span,
        place: Place | None = None,
    ) -> Value:
        """`for pattern in iterable`: run the body once for each item (of what an `iter` method gives).

        A generator or an iterator gives its items one at a time; in `for x in
        mut it`, `place` is where it is, and it changes there.
        """
        if place is None and (iterator := self.find_method(iterable, "iter")) is not None:
            iterable = self.call(iterator, [iterable], span)
        items: Iterable[Value]
        match iterable:
            case list() | range():
                items = iterable
            case ArrayValue():
                items = list(iterable.items)  # in row-major order
            case str():
                items = list(iterable)  # one-character strings
            case GenValue() | VariantValue() if isinstance(iterable, GenValue) or self.find_method(iterable, "next"):
                items = self.items_of(iterable, place, scope, span)
            case _:
                raise EllaError(f"can't loop over a {type_name(iterable)}", span)
        for item in items:
            bindings = self.match_pattern(pattern, item, scope)
            if bindings is None:
                raise EllaError(f"the 'for' pattern doesn't match {to_text(item, nested=True)}", span)
            # Each iteration gets a fresh scope for the pattern's variables.
            loop_scope = Scope(scope)
            define_all(loop_scope, bindings, mutable_names(pattern))
            if (signal := self.run_loop_body(body, loop_scope, label)) is not None:
                self.check_no_break_value(signal, "for")
                break
        return UNIT

    # Enums and built-ins

    def define_enum(self, enum: EnumDef, scope: Scope) -> None:
        """Make each variant a name: a value if it has no fields, a constructor if it does.

        A struct also gets its field names. Methods are functions, and a
        struct's `finalize` method is its finalizer.
        """
        if enum.struct:
            self.structs[enum.name] = enum.field_names
            if enum.nocopy:
                self.nocopy.add(enum.name)
        if self.checker is not None and self.checker.needs_type_args(enum.name):
            self.typed_enums.add(enum.name)
        if enum.methods:
            for method in enum.methods:
                self.names[id(method.function)] = f"{enum.name}.{method.name}"
            self.methods[enum.name] = {method.name: self.eval(method.function, scope) for method in enum.methods}
            if "finalize" in self.methods[enum.name]:
                self.finalizers[enum.name] = self.methods[enum.name]["finalize"]
        for variant in enum.variants:
            if variant.fields:
                scope.define(variant.name, Constructor(enum.name, variant.name, len(variant.fields)))
            else:
                scope.define(variant.name, VariantValue(enum.name, variant.name))

    def builtin_print(self, args: list[Value], span: Span) -> Value:
        """`print(a, b, ...)` writes the values' text, with spaces between them, and a newline."""
        self.out.write(" ".join(self.text(arg) for arg in args) + "\n")
        return UNIT

    def builtin_eprint(self, args: list[Value], span: Span) -> Value:
        """`eprint(a, b, ...)` writes like `print`, to standard error (after what was printed so far)."""
        self.out.flush()
        stderr = self.host.stderr if self.host.stderr is not None else sys.stderr
        stderr.write(" ".join(self.text(arg) for arg in args) + "\n")
        stderr.flush()
        return UNIT

    def make_array(self, call: Call, items: Value, span: Span) -> Value:
        """`array(xs)`, whose item type the checker knows (an empty list can't show it)."""
        t = self.checker.types.get(id(call)) if self.checker is not None else None
        item = array_item(finish(t)) if t is not None else None
        if item is not None:
            name = show_type(item)
        else:
            name = type_name(items[0]) if isinstance(items, list) and items else "i64"
        return call_library("array", [items, name], span, self.host)

    def builtin_len(self, args: list[Value], span: Span) -> Value:
        """`len(x)` is the length of a list, string or range, or how many values a pool or arena holds."""
        value = args[0]
        if (method := self.find_method(value, "len")) is not None:
            return self.call(method, [value], span)
        if isinstance(value, PoolValue):
            return value.live()
        if isinstance(value, ArenaValue):
            return len(value.values)
        if isinstance(value, ArrayValue):
            return len(value.items)
        if not isinstance(value, list | str | range):
            raise EllaError(f"len() needs a list, str, range, Array, Pool or Arena, not {type_name(value)}", span)
        return len(value)


def param_names(t: Type) -> set[str]:
    """The type parameters a type mentions."""
    match t:
        case ParamTy(name):
            return {name}
        case ListTy(part):
            return param_names(part)
        case TupleTy(parts) | EnumTy(_, parts) | BuiltinTy(_, parts):
            return set().union(*map(param_names, parts))
        case FuncTy(params, result):
            return set().union(*map(param_names, params), param_names(result))
        case _:
            return set()


def mutable_names(pattern: Pattern) -> set[str]:
    """The names a pattern binds as `mut x`."""
    return {node.name for node in pattern_names(pattern) if node.mutable}


def define_all(scope: Scope, bindings: dict[str, Value], mutables: set[str]) -> None:
    """Define the variables a pattern bound. A `mut` one gets a copy of its value, which it may change."""
    for name, value in bindings.items():
        mutable = name in mutables
        scope.define(name, copy_value(value) if mutable else value, mutable)
