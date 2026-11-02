"""Tree-walking interpreter: runs a program by evaluating its syntax tree directly.

This is stage 2 of the roadmap. It defines what Ella programs mean, before
there is an IR or a compiler, and later stages are checked against it.

The core is `Interpreter.eval(expr, scope)`, one `match` over every kind of
expression. Each case evaluates the parts of the node (recursively, with
`eval`) and combines them. Variables live in `Scope`s (see ella.values),
chained from the innermost block out to the global scope.

Control flow that jumps out of the middle of an evaluation (`break`,
`continue`, `return` and `?`) is done with Python exceptions. For example,
`break` raises BreakSignal, which unwinds through any nested `eval` calls
until the loop it belongs to catches it.

There is no type checker yet, so types are checked as the program runs: `1 +
true` is a run-time error here, where a later stage will reject it before the
program starts.
"""

import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from ella.checker import OPERATOR_METHODS, REFLECTION, Checker, check_code, finish
from ella.lexer import EllaError, Span
from ella.operators import binary, compare, index_into, unary
from ella.parser import parse
from ella.staging import (
    AnySplice,
    Tables,
    has_splices,
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
    ListLit,
    LiteralPattern,
    Loop,
    Match,
    Member,
    MutArg,
    Name,
    NamePattern,
    OrPattern,
    Pattern,
    Program,
    Quote,
    RangePattern,
    Return,
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
    mentions_return,
)
from ella.values import (
    UNIT,
    Builtin,
    Closure,
    CodeValue,
    Constructor,
    HandleValue,
    PoolValue,
    Scope,
    TypeValue,
    Value,
    Var,
    VariantValue,
    copy_value,
    equal,
    to_text,
    type_name,
)

# How deeply Ella functions may call each other before we report a stack
# overflow. Each Ella call uses a few dozen Python frames, so the Python
# recursion limit is raised to match while a program runs.
MAX_CALL_DEPTH = 1000

# How `a.compare(b)` decides each ordering comparison.
ORDERING: dict[str, Callable[[int], bool]] = {
    "<": lambda order: order < 0,
    "<=": lambda order: order <= 0,
    ">": lambda order: order > 0,
    ">=": lambda order: order >= 0,
}
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


def run(source: str, out: TextIO | None = None) -> None:
    """Parse and run a program, writing what it prints to `out` (standard output by default).

    Raises EllaError for syntax and run-time errors. The program isn't type
    checked first, unless it builds code (with `quote` or `$`), which needs
    the checker.
    """
    program = parse(source)
    if uses_staging(program):
        program, checker = prepare(program, out)
        Interpreter(out, checker).run(program)
    else:
        Interpreter(out).run(program)


class Interpreter:
    def __init__(
        self, out: TextIO | None = None, checker: Checker | None = None, tables: Tables | None = None
    ) -> None:
        """`checker` is the checked program's checker, which quotes and `run` need; `tables` may share its tables."""
        # Look up sys.stdout now rather than as a default argument, which
        # would be fixed when this module is imported.
        self.out = out if out is not None else sys.stdout
        self.depth = 0  # how many Ella function calls are in progress
        self.checker = checker
        self.tables = tables if tables is not None else Tables().add(checker) if checker is not None else Tables()
        # While compiling, a top-level function's splices are expanded before it runs.
        self.prepare_function: Callable[[Lambda], Lambda] | None = None
        self.structs: dict[str, tuple[str, ...]] = {"Error": ("message",)}  # struct -> its field names
        self.nocopy: set[str] = set()  # the nocopy structs
        self.finalizers: dict[str, Value] = {}  # struct -> its finalize function
        self.methods: dict[str, dict[str, Value]] = {}  # struct or enum -> its methods
        # Values with finalizers, by owner: the program (globals), and each
        # function call and loop iteration in progress, innermost last. When
        # an owner ends, it finalizes its values, newest first.
        self.owners: list[list[Value]] = [[]]
        self.renamings: list[dict[int, str]] = []  # the fresh names of the quotes being built

        # The global scope holds the built-ins and every top-level `let`.
        self.globals = Scope(None)
        self.globals.define("print", Builtin("print", 1, self.builtin_print))
        self.globals.define("len", Builtin("len", 1, self.builtin_len))
        self.globals.define("run", Builtin("run", 1, self.builtin_run))
        for name, (params, _) in REFLECTION.items():
            self.globals.define(name, Builtin(name, len(params), self.builtin_reflect(name)))
        self.globals.define("pool_new", Builtin("pool_new", 0, lambda args, span: self.new_pool()))
        # alloc and free change their pool, and are handled in `call_pool`.
        self.globals.define("alloc", Builtin("alloc", 2, lambda args, span: UNIT))
        self.globals.define("free", Builtin("free", 2, lambda args, span: UNIT))
        # Result, Optional and Error are ordinary enums and structs that happen to be predefined.
        self.globals.define("Ok", Constructor("Result", "Ok", 1))
        self.globals.define("Err", Constructor("Result", "Err", 1))
        self.globals.define("Error", Constructor("Error", "Error", 1))
        self.globals.define("Some", Constructor("Optional", "Some", 1))
        self.globals.define("None", VariantValue("Optional", "None"))

    # Programs

    def run(self, program: Program) -> None:
        """Run the top-level expressions in order, then `main` if there is one."""
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, MAX_CALL_DEPTH * PYTHON_FRAMES_PER_CALL))
        try:
            self.declare_top_level(program)
            self.owners.append([])  # the top-level code's own values
            for expr in program.body:
                self.eval(expr, self.globals)
            main = self.globals.vars.get("main")
            if main is not None:
                self.call(main.value, [], Span(1, 1))
            self.close_owner()
            self.close_owner()  # the globals
        # Signals that escape to the top level weren't inside anything that
        # could catch them.
        except ReturnSignal as signal:
            raise EllaError("'return' outside of a function", signal.span) from None
        except BreakSignal as signal:
            raise EllaError("'break' outside of a loop", signal.span) from None
        except ContinueSignal as signal:
            raise EllaError("'continue' outside of a loop", signal.span) from None
        finally:
            sys.setrecursionlimit(limit)

    def declare_top_level(self, program: Program) -> None:
        """Make every top-level name exist before the program starts (docs/grammar.md §3).

        This is what lets top-level functions call each other in any order:
        functions and enums are defined right away, and other values are
        declared but not ready until their `let` runs.
        """
        for expr in program.body:
            match expr:
                case Let(bindings, Lambda() as function):
                    closure = Closure(function, Scope(self.globals))
                    for binding in bindings:
                        self.globals.define(binding.name, closure, binding.mutable)
                case Let(bindings):
                    for binding in bindings:
                        if binding.name not in self.globals.vars:
                            self.globals.vars[binding.name] = Var(UNIT, binding.mutable, ready=False)
                case EnumDef():
                    self.define_enum(expr, self.globals)

    # Expressions

    def eval(self, expr: Expr, scope: Scope) -> Value:
        """Evaluate an expression in `scope` and return its value."""
        match expr:
            # Literals
            case IntLit(literal) | FloatLit(literal) | BoolLit(literal):
                return literal
            case StringLit(parts):
                # Interpolated expressions are evaluated and turned into text.
                return "".join(part if isinstance(part, str) else to_text(self.eval(part, scope)) for part in parts)
            case UnitLit():
                return UNIT
            case TupleLit(items):
                return tuple(self.eval(item, scope) for item in items)
            case ListLit(items):
                return [self.eval(item, scope) for item in items]

            # Variables
            case Name(name, span):
                # Reading a variable copies its value: Ella has value semantics.
                return copy_value(self.lookup(name, scope, span).value)
            case Let(bindings, value_expr):
                value = self.eval(value_expr, scope)
                if scope is self.globals:
                    self.adopt(value, -1, 0)  # a global's values live as long as the program
                for binding in bindings:
                    scope.define(binding.name, copy_value(value), binding.mutable)
                # A function bound with `let` can call itself by name. Its
                # captured scope was made before the `let` bound the name, so
                # add the name there too.
                if isinstance(value, Closure) and isinstance(value_expr, Lambda):
                    for binding in bindings:
                        value.scope.define(binding.name, value)
                return value
            case Assign(op, target, value_expr, span):
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
                return self.binary(op, self.eval(left, scope), self.eval(right, scope), span)
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
                if isinstance(function, Builtin) and function.name in ("alloc", "free"):
                    return self.call_pool(function.name, args, scope, span)
                return self.call_with_args(function, args, scope, span)
            case MutArg(span=span):
                raise EllaError("'mut' marks an argument for a 'mut' parameter, and only works in a call", span)
            case Return(returned, span):
                raise ReturnSignal(UNIT if returned is None else self.eval(returned, scope), span)

            # Control flow
            case Block(exprs):
                return self.eval_block(exprs, scope)
            case If(cond, then, else_):
                if self.eval_bool(cond, scope, "an 'if' condition"):
                    result = self.eval(then, scope)
                    # Without `else`, `if` has the value (), whatever the body gives.
                    return result if else_ is not None else UNIT
                return self.eval(else_, scope) if else_ is not None else UNIT
            case Match(subject, cases, span):
                return self.eval_match(subject, cases, scope, span)
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

    def eval_bool(self, expr: Expr, scope: Scope, what: str) -> bool:
        """Evaluate an expression that must give a bool, since only bools are truthy."""
        value = self.eval(expr, scope)
        if not isinstance(value, bool):
            raise EllaError(f"{what} needs a bool, not {type_name(value)}", expr.span)
        return value

    def eval_block(self, exprs: tuple[Expr, ...], scope: Scope) -> Value:
        """Run a block's lines in a new scope. Its value is the last line's value."""
        local = Scope(scope)
        value = UNIT
        for expr in exprs:
            value = self.eval(expr, local)
        # A block that ends with an assignment has the value () (docs/grammar.md §2).
        return UNIT if isinstance(exprs[-1], Assign) else value

    # Variables

    def lookup(self, name: str, scope: Scope, span: Span) -> Var:
        """Find a variable, which must exist and (for top-level names) have had its `let` run."""
        var = scope.lookup(name)
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
            case Index(target, index, span):
                container = self.reference(target, scope, for_assignment)
                return self.item(container, self.eval(index, scope), span)
            case Member(target, name, span):
                struct = self.reference(target, scope, for_assignment)
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
        """`container[index]`: an item of a list or string, a pool's value, or what a `get` method gives."""
        if (getter := self.find_method(container, "get")) is not None:
            return self.call(getter, [container, index], span)
        if isinstance(container, PoolValue):
            assert isinstance(index, HandleValue)
            return self.pool_slot(container, index, span)
        return index_into(container, index, span)

    def set_item(self, container: Value, index: Value, value: Value, span: Span) -> None:
        if isinstance(container, PoolValue):
            assert isinstance(index, HandleValue)
            self.pool_slot(container, index, span)  # check the handle
            container.slots[index.slot] = value
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

    # Methods

    def call_member(self, callee: Member, args: tuple[Expr, ...], scope: Scope, span: Span) -> Value:
        """`v.name(args)`: v's method `name`, called with v first, or a function held in v's field `name`.

        In `mut v.name(args)`, the method changes v.
        """
        receiver, name = callee.target, callee.name
        if isinstance(receiver, MutArg):
            root, indexes = self.evaluate_place(receiver.target, scope)
            value = copy_value(self.read_place(root, indexes, scope))
            function = self.method(value, name, callee.span)
            return self.call_with_args(function, args, scope, span, (value, (root, indexes, receiver.span)))
        value = self.eval(receiver, scope)
        if (
            self.find_method(value, name) is None
            and isinstance(value, VariantValue)
            and name in self.structs.get(value.enum, ())
        ):
            return self.call_with_args(value.fields[self.field_index(value, name, callee.span)], args, scope, span)
        return self.call_with_args(self.method(value, name, callee.span), args, scope, span, (value, None))

    def find_method(self, value: Value, name: str) -> Value | None:
        """The method `name` of a struct or enum value, if its type has one."""
        if isinstance(value, VariantValue):
            return self.methods.get(value.enum, {}).get(name)
        return None

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
        return compare(op, left, right, span)

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
                self.check_arity(name, arity, args, span)
                return run(args, span)
            case Constructor(enum, variant, arity):
                self.check_arity(variant, arity, args, span)
                value = VariantValue(enum, variant, list(args), nocopy=enum in self.nocopy)
                if enum in self.finalizers:
                    self.owners[-1].append(value)
                return value
            case Closure(lambda_, captured):
                self.check_arity("function", len(lambda_.params), args, span)
                if self.prepare_function is not None:
                    lambda_ = self.prepare_function(lambda_)
                return self.call_closure(lambda_, captured, args, span, finals)
        raise EllaError(f"can't call a value of type {type_name(function)}", span)

    def check_arity(self, name: str, arity: int, args: list[Value], span: Span) -> None:
        if len(args) != arity:
            plural = "" if arity == 1 else "s"
            raise EllaError(f"{name} takes {arity} argument{plural} but was given {len(args)}", span)

    def call_closure(
        self, lambda_: Lambda, captured: Scope, args: list[Value], span: Span, finals: list[Value] | None = None
    ) -> Value:
        """Run a function's body with its parameters bound, checking its `where` conditions."""
        if self.depth >= MAX_CALL_DEPTH:
            raise EllaError(f"stack overflow: more than {MAX_CALL_DEPTH} nested calls", span)
        # Parameters live in a new scope inside the captured one. Only `mut` ones can change.
        local = Scope(captured)
        for param, arg in zip(lambda_.params, args):
            local.define(param.name, arg, param.mutable)

        postconditions = [cond for cond in lambda_.where if mentions_return(cond.expr)]
        self.owners.append([])
        for cond in lambda_.where:
            if cond not in postconditions:
                self.check_condition(cond, local, "precondition")

        self.depth += 1
        try:
            result = self.eval(lambda_.body, local)
        except ReturnSignal as signal:
            result = signal.value
        # Loops can't be exited from inside a function called in their body.
        except BreakSignal as signal:
            raise EllaError("'break' outside of a loop", signal.span) from None
        except ContinueSignal as signal:
            raise EllaError("'continue' outside of a loop", signal.span) from None
        finally:
            self.depth -= 1

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
            case PoolValue():
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
        for value in reversed(self.owners.pop()):
            self.finalize(value)

    def finalize(self, value: Value) -> None:
        """Run a value's finalizer; a pool finalizes its values, from the last slot to the first."""
        if isinstance(value, PoolValue):
            for slot in reversed(value.slots):
                if slot is not None:
                    self.finalize_held(slot)
        elif isinstance(value, VariantValue) and value.enum in self.finalizers:
            self.call(self.finalizers[value.enum], [value], Span(0, 0))

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
            case PoolValue():
                self.finalize(value)
            case list() | tuple():
                for item in reversed(value):
                    self.finalize_held(item)

    # Pools

    def new_pool(self) -> Value:
        pool = PoolValue()
        self.owners[-1].append(pool)
        return pool

    def pool_slot(self, pool: PoolValue, handle: HandleValue, span: Span) -> Value:
        """The value a handle refers to, which must still be there."""
        if handle.slot >= len(pool.slots) or pool.generations[handle.slot] != handle.generation:
            raise EllaError(f"handle {to_text(handle)} is stale: its value was freed", span)
        value = pool.slots[handle.slot]
        assert value is not None
        return value

    def call_pool(self, name: str, args: tuple[Expr, ...], scope: Scope, span: Span) -> Value:
        """`alloc(mut pool, value)` and `free(mut pool, handle)`, which change the pool in place."""
        pool_arg, value_expr = args
        assert isinstance(pool_arg, MutArg)
        root, indexes = self.evaluate_place(pool_arg.target, scope)
        pool = self.read_place(root, indexes, scope)
        assert isinstance(pool, PoolValue)
        value = self.eval(value_expr, scope)
        if name == "alloc":
            # The pool owns the value from now on, and finalizes it when it is freed.
            for owner in range(len(self.owners)):
                moving = {id(v) for v in self.finalizable(value)}
                self.owners[owner] = [v for v in self.owners[owner] if id(v) not in moving]
            if pool.free:
                slot = pool.free.pop()
                pool.slots[slot] = value
            else:
                slot = len(pool.slots)
                pool.slots.append(value)
                pool.generations.append(0)
            return HandleValue(slot, pool.generations[slot])
        assert isinstance(value, HandleValue)
        held = self.pool_slot(pool, value, span)
        self.finalize_held(held)
        pool.slots[value.slot] = None
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

        # Quotes inside this one's splices may use its variables, by their fresh names.
        builder = start_code(quote, self.tables)
        self.renamings.append(builder.renamed)
        try:
            return CodeValue(builder.build(splice, persist, self.renamings[:-1]))
        finally:
            self.renamings.pop()

    def builtin_reflect(self, name: str) -> Callable[[list[Value], Span], Value]:
        def run(args: list[Value], span: Span) -> Value:
            if self.checker is None:
                raise EllaError(f"{name}() needs a checked program", span)
            return reflect(name, args, self.checker.enums, span)

        return run

    def builtin_run(self, args: list[Value], span: Span) -> Value:
        """`run(code)`: check the code, then run it like the body of a function with no parameters."""
        code = args[0]
        assert isinstance(code, CodeValue)
        if self.checker is None:
            raise EllaError("run() needs a checked program", span)
        self.tables.add(check_code(code.expr, self.checker))
        return self.run_code(code.expr)

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
        return self.run_code(expr)

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
            case NamePattern(name):
                # A bare name is a variant (like `None` or `Empty`) if one by
                # that name is in scope, and otherwise a new binding.
                var = scope.lookup(name)
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
                if not isinstance(value, VariantValue) or value.variant != name or len(value.fields) != len(args):
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

    def eval_match(self, subject_expr: Expr, cases: tuple[Case, ...], scope: Scope, span: Span) -> Value:
        """Try each case in order; the first whose pattern matches (and guard holds) wins."""
        subject = self.eval(subject_expr, scope)
        for case in cases:
            bindings = self.match_pattern(case.pattern, subject, scope)
            if bindings is None:
                continue
            # The pattern's variables are visible in the guard and the body.
            case_scope = Scope(scope)
            for name, value in bindings.items():
                case_scope.define(name, value)
            if case.guard is not None and not self.eval_bool(case.guard, case_scope, "a case guard"):
                continue
            return self.eval(case.body, case_scope)
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

    def check_no_break_value(self, signal: BreakSignal, loop: str) -> None:
        """Only `loop` has a value; `for` and `while` are always ()."""
        if signal.value is not None:
            raise EllaError(f"only 'loop' can break with a value, not '{loop}'", signal.span)

    def eval_for(
        self, label: str | None, pattern: Pattern, iterable: Value, body: Expr, scope: Scope, span: Span
    ) -> Value:
        """`for pattern in iterable`: run the body once for each item (of what an `iter` method gives)."""
        if (iterator := self.find_method(iterable, "iter")) is not None:
            iterable = self.call(iterator, [iterable], span)
        match iterable:
            case list() | range():
                items: list[Value] | range = iterable
            case str():
                items = list(iterable)  # one-character strings
            case _:
                raise EllaError(f"can't loop over a {type_name(iterable)}", span)
        for item in items:
            bindings = self.match_pattern(pattern, item, scope)
            if bindings is None:
                raise EllaError(f"the 'for' pattern doesn't match {to_text(item, nested=True)}", span)
            # Each iteration gets a fresh scope for the pattern's variables.
            loop_scope = Scope(scope)
            for name, value in bindings.items():
                loop_scope.define(name, value)
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
        if enum.methods:
            self.methods[enum.name] = {method.name: self.eval(method.function, scope) for method in enum.methods}
            if "finalize" in self.methods[enum.name]:
                self.finalizers[enum.name] = self.methods[enum.name]["finalize"]
        for variant in enum.variants:
            if variant.fields:
                scope.define(variant.name, Constructor(enum.name, variant.name, len(variant.fields)))
            else:
                scope.define(variant.name, VariantValue(enum.name, variant.name))

    def builtin_print(self, args: list[Value], span: Span) -> Value:
        """`print(value)` writes the value's text and a newline."""
        self.out.write(to_text(args[0]) + "\n")
        return UNIT

    def builtin_len(self, args: list[Value], span: Span) -> Value:
        """`len(x)` is the length of a list, string or range, or how many values a pool holds."""
        value = args[0]
        if (method := self.find_method(value, "len")) is not None:
            return self.call(method, [value], span)
        if isinstance(value, PoolValue):
            return value.live()
        if not isinstance(value, list | str | range):
            raise EllaError(f"len() needs a list, str, range or Pool, not {type_name(value)}", span)
        return len(value)
