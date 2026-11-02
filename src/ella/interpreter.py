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

import math
import sys
from dataclasses import fields, is_dataclass
from typing import TextIO

from ella.lexer import EllaError, Span
from ella.parser import parse
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
    Continue,
    EnumDef,
    Expr,
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
    Name,
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
    Unary,
    UnitLit,
    VariantPattern,
    While,
    WildcardPattern,
)
from ella.values import (
    UNIT,
    Builtin,
    Closure,
    Constructor,
    Scope,
    Value,
    Var,
    VariantValue,
    copy_value,
    equal,
    fits_i64,
    to_text,
    type_name,
)

# How deeply Ella functions may call each other before we report a stack
# overflow. Each Ella call uses a few dozen Python frames, so the Python
# recursion limit is raised to match while a program runs.
MAX_CALL_DEPTH = 1000
PYTHON_FRAMES_PER_CALL = 50


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

    Raises EllaError for syntax and run-time errors.
    """
    Interpreter(out).run(parse(source))


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


def float_div(a: float, b: float) -> float:
    """`a / b` as IEEE 754 floating point (and so x64 hardware) defines it: dividing
    by zero gives an infinity, or NaN for 0 / 0, rather than an error."""
    if b == 0.0:
        if a == 0.0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a) * math.copysign(1.0, b)
    return a / b


def float_pow(a: float, b: float) -> float:
    """`a ** b` for f64, giving infinity or NaN where Python would raise an error."""
    try:
        result = a**b
    except ZeroDivisionError:  # 0.0 ** -1.0
        return math.inf
    except OverflowError:
        return -math.inf if a < 0 and b % 2 == 1 else math.inf
    # A negative number to a fractional power has no real answer; Python
    # would give a complex number.
    return math.nan if isinstance(result, complex) else result


class Interpreter:
    def __init__(self, out: TextIO | None = None) -> None:
        # Look up sys.stdout now rather than as a default argument, which
        # would be fixed when this module is imported.
        self.out = out if out is not None else sys.stdout
        self.depth = 0  # how many Ella function calls are in progress

        # The global scope holds the built-ins and every top-level `let`.
        self.globals = Scope(None)
        self.globals.define("print", Builtin("print", 1, self.builtin_print))
        self.globals.define("len", Builtin("len", 1, self.builtin_len))
        # Result and Optional are ordinary enums that happen to be predefined.
        self.globals.define("Ok", Constructor("Result", "Ok", 1))
        self.globals.define("Error", Constructor("Result", "Error", 1))
        self.globals.define("Some", Constructor("Optional", "Some", 1))
        self.globals.define("None", VariantValue("Optional", "None"))

    # Programs

    def run(self, program: Program) -> None:
        """Run the top-level expressions in order, then `main` if there is one."""
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, MAX_CALL_DEPTH * PYTHON_FRAMES_PER_CALL))
        try:
            self.declare_top_level(program)
            for expr in program.body:
                self.eval(expr, self.globals)
            main = self.globals.vars.get("main")
            if main is not None:
                self.call(main.value, [], Span(1, 1))
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
                return self.unary(op, self.eval(operand, scope), span)
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
                return copy_value(self.index_into(container, self.eval(index, scope), span))
            case Member(span=span):
                raise EllaError("member access ('.name') isn't supported yet", span)
            case Try(operand, span):
                return self.eval_try(self.eval(operand, scope), span)

            # Functions
            case Lambda():
                return Closure(expr, self.capture(scope))
            case Call(callee, args, span):
                function = self.eval(callee, scope)
                return self.call(function, [self.eval(arg, scope) for arg in args], span)
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
                while self.eval_bool(cond, scope, "a 'while' condition"):
                    if (signal := self.run_loop_body(body, scope, label)) is not None:
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
                return self.index_into(container, self.eval(index, scope), span)
            case _ if for_assignment:
                raise EllaError("can only assign to a variable or an element of one", expr.span)
            case _:
                return self.eval(expr, scope)

    def eval_assign(self, op: str, target: Name | Index, value_expr: Expr, scope: Scope, span: Span) -> Value:
        """`target = value`, or `target op= value`, which means `target = target op value`."""
        value = self.eval(value_expr, scope)
        match target:
            case Name(name, name_span):
                var = self.lookup_mutable(name, scope, name_span)
                if op != "=":
                    value = self.binary(op.removesuffix("="), var.value, value, span)
                var.value = value
            case Index(container_expr, index_expr, index_span):
                container = self.reference(container_expr, scope, for_assignment=True)
                index = self.eval(index_expr, scope)
                old = self.index_into(container, index, index_span)
                if not isinstance(container, list):
                    raise EllaError(f"can't assign to an element of a {type_name(container)}", index_span)
                assert isinstance(index, int)  # checked by index_into
                if op != "=":
                    value = self.binary(op.removesuffix("="), old, value, span)
                container[index] = value
        return copy_value(value)

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

    def call(self, function: Value, args: list[Value], span: Span) -> Value:
        """Call a function value with already evaluated arguments."""
        match function:
            case Builtin(name, arity, run):
                self.check_arity(name, arity, args, span)
                return run(args, span)
            case Constructor(enum, variant, arity):
                self.check_arity(variant, arity, args, span)
                return VariantValue(enum, variant, tuple(args))
            case Closure(lambda_, captured):
                self.check_arity("function", len(lambda_.params), args, span)
                return self.call_closure(lambda_, captured, args, span)
        raise EllaError(f"can't call a value of type {type_name(function)}", span)

    def check_arity(self, name: str, arity: int, args: list[Value], span: Span) -> None:
        if len(args) != arity:
            plural = "" if arity == 1 else "s"
            raise EllaError(f"{name} takes {arity} argument{plural} but was given {len(args)}", span)

    def call_closure(self, lambda_: Lambda, captured: Scope, args: list[Value], span: Span) -> Value:
        """Run a function's body with its parameters bound, checking its `where` conditions."""
        if self.depth >= MAX_CALL_DEPTH:
            raise EllaError(f"stack overflow: more than {MAX_CALL_DEPTH} nested calls", span)
        # Parameters live in a new scope inside the captured one. They are immutable.
        local = Scope(captured)
        for param, arg in zip(lambda_.params, args):
            local.define(param.name, arg)

        postconditions = [cond for cond in lambda_.where if mentions_return(cond.expr)]
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
        return result

    def check_condition(self, cond: Assert, scope: Scope, kind: str) -> None:
        """Check an `assert` or `where` condition, reporting its source if it is false."""
        if not self.eval_bool(cond.expr, scope, f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"):
            raise EllaError(f"{kind} failed: {cond.source}", cond.span)

    def eval_try(self, value: Value, span: Span) -> Value:
        """Postfix `?`: unwrap `Ok(v)` or `Some(v)`, or return the `Error`/`None` from the function."""
        match value:
            case VariantValue("Result", "Ok", (inner,)) | VariantValue("Optional", "Some", (inner,)):
                return inner
            case VariantValue("Result", "Error") | VariantValue("Optional", "None"):
                raise ReturnSignal(value, span)
        raise EllaError(f"'?' needs a Result or Optional, not {type_name(value)}", span)

    # Operators

    def unary(self, op: str, value: Value, span: Span) -> Value:
        match op, value:
            case "not", bool():
                return not value
            case _, bool():
                pass  # bools aren't numbers, though Python thinks they are
            case "-", int():
                return self.check_i64(-value, span)
            case "-", float():
                return -value
            case "~", int():
                return ~value
        raise EllaError(f"'{op}' doesn't work on {type_name(value)}", span)

    def binary(self, op: str, a: Value, b: Value, span: Span) -> Value:
        """Apply an arithmetic, bitwise or range operator to two values."""
        # There are no implicit conversions, so both sides must have the same type.
        if type_name(a) != type_name(b):
            raise EllaError(f"'{op}' needs two values of the same type, not {type_name(a)} and {type_name(b)}", span)
        match a, b:
            case bool(), bool():
                pass
            case int(), int() if op in ("..", "..="):
                return range(a, b + 1 if op == "..=" else b)
            case int(), int():
                return self.int_op(op, a, b, span)
            case float(), float():
                return self.float_op(op, a, b, span)
            case (str(), str()) | (list(), list()) if op == "+":
                return a + b  # type: ignore[operator]  # mypy can't pair the cases up
        raise EllaError(f"'{op}' doesn't work on {type_name(a)}", span)

    def check_i64(self, n: int, span: Span) -> int:
        """i64 arithmetic traps on overflow: a result that doesn't fit is an error."""
        if not fits_i64(n):
            raise EllaError(f"i64 overflow: the result {n} doesn't fit in 64 bits", span)
        return n

    def int_op(self, op: str, a: int, b: int, span: Span) -> Value:
        """i64 arithmetic. Python computes the exact result, then we check it fits."""
        match op:
            case "+":
                return self.check_i64(a + b, span)
            case "-":
                return self.check_i64(a - b, span)
            case "*":
                return self.check_i64(a * b, span)
            case "/":
                return float_div(float(a), float(b))  # `/` always gives f64
            case "//" | "%":
                if b == 0:
                    raise EllaError("integer division by zero", span)
                # Python's // and % already round toward negative infinity, as
                # Ella's do. Only `I64_MIN // -1` can overflow.
                return self.check_i64(a // b, span) if op == "//" else a % b
            case "**":
                if b < 0:
                    raise EllaError("an i64 can't be raised to a negative power", span)
                # Any base other than -1, 0 or 1 overflows long before an
                # exponent of 64, so don't compute an enormous number first.
                if abs(a) > 1 and b >= 64:
                    raise EllaError(f"i64 overflow: {a} ** {b} doesn't fit in 64 bits", span)
                return self.check_i64(a**b, span)
            case "&":
                return a & b
            case "|":
                return a | b
            case "^":
                return a ^ b
            case "<<" | ">>":
                if not 0 <= b < 64:
                    raise EllaError(f"shift amount must be between 0 and 63, not {b}", span)
                # `a << b` is `a * 2**b`, so like `*` it traps if the result
                # doesn't fit: shifting a bit into or past the sign bit is an
                # error, not a silent change of sign. `>>` can't overflow;
                # Python's >> on a negative number is an arithmetic shift, as
                # Ella's is.
                return self.check_i64(a << b, span) if op == "<<" else a >> b
        raise EllaError(f"'{op}' doesn't work on i64", span)

    def float_op(self, op: str, a: float, b: float, span: Span) -> Value:
        """f64 arithmetic, following IEEE 754: no errors, but infinities and NaNs."""
        match op:
            case "+":
                return a + b
            case "-":
                return a - b
            case "*":
                return a * b
            case "/":
                return float_div(a, b)
            case "**":
                return float_pow(a, b)
        raise EllaError(f"'{op}' doesn't work on f64", span)

    def compare(self, op: str, a: Value, b: Value, span: Span) -> bool:
        """One comparison of a chain: `==`, `<`, `in` and so on."""
        if op in ("in", "not in"):
            return self.contains(b, a, span) == (op == "in")
        if type_name(a) != type_name(b):
            raise EllaError(f"'{op}' needs two values of the same type, not {type_name(a)} and {type_name(b)}", span)
        if type_name(a) == "function":
            raise EllaError("functions can't be compared", span)
        if op == "==":
            return equal(a, b)
        if op == "!=":
            return not equal(a, b)
        # Ordering works on numbers and strings (strings compare by code point).
        match a, b:
            case bool(), bool():
                pass
            case (int(), int()) | (float(), float()) | (str(), str()):
                match op:
                    case "<":
                        return a < b  # type: ignore[operator]  # mypy can't pair the cases up
                    case "<=":
                        return a <= b  # type: ignore[operator]
                    case ">":
                        return a > b  # type: ignore[operator]
                    case ">=":
                        return a >= b  # type: ignore[operator]
        raise EllaError(f"'{op}' doesn't work on {type_name(a)}", span)

    def contains(self, container: Value, item: Value, span: Span) -> bool:
        """`item in container` for a list, a string (substring) or a range."""
        match container:
            case list():
                return any(equal(element, item) for element in container)
            case str() if isinstance(item, str):
                return item in container
            case range() if isinstance(item, int) and not isinstance(item, bool):
                return item in container
            case str() | range():
                raise EllaError(f"'in' can't look for a {type_name(item)} in a {type_name(container)}", span)
        raise EllaError(f"'in' needs a list, str or range on the right, not {type_name(container)}", span)

    def index_into(self, container: Value, index: Value, span: Span) -> Value:
        """`container[index]` for a list, or a string (giving a one-character string)."""
        if not isinstance(index, int) or isinstance(index, bool):
            raise EllaError(f"an index must be an i64, not {type_name(index)}", span)
        if not isinstance(container, list | str):
            raise EllaError(f"can't index a {type_name(container)}", span)
        # No Python-style negative indexes: -1 is an error, not the last element.
        if not 0 <= index < len(container):
            raise EllaError(
                f"index {index} is out of range for a {type_name(container)} of length {len(container)}", span
            )
        return container[index]

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
        self, patterns: tuple[Pattern, ...], values: tuple[Value, ...], scope: Scope
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

    def run_loop_body(self, body: Expr, scope: Scope, label: str | None) -> BreakSignal | None:
        """Run one iteration of a loop body. Returns the BreakSignal if the loop should stop.

        A `break` or `continue` for an outer loop (with a different label)
        isn't ours, so it is raised again for the outer loop to catch.
        """
        try:
            self.eval(body, scope)
        except BreakSignal as signal:
            if signal.label not in (None, label):
                raise
            return signal
        except ContinueSignal as signal:
            if signal.label not in (None, label):
                raise
        return None

    def check_no_break_value(self, signal: BreakSignal, loop: str) -> None:
        """Only `loop` has a value; `for` and `while` are always ()."""
        if signal.value is not None:
            raise EllaError(f"only 'loop' can break with a value, not '{loop}'", signal.span)

    def eval_for(
        self, label: str | None, pattern: Pattern, iterable: Value, body: Expr, scope: Scope, span: Span
    ) -> Value:
        """`for pattern in iterable`: run the body once for each item."""
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
        """Make each variant a name: a value if it has no fields, a constructor if it does."""
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
        """`len(x)` is the length of a list, string or range."""
        value = args[0]
        if not isinstance(value, list | str | range):
            raise EllaError(f"len() needs a list, str or range, not {type_name(value)}", span)
        return len(value)
