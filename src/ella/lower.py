"""Lowering: turns a type-checked syntax tree into IR (see ella.ir).

The syntax tree is nested: an `if` contains expressions, which contain other
expressions. The IR is flat: blocks of simple instructions, connected by
jumps. Lowering walks the tree and emits instructions into the *current
block*, starting new blocks wherever control can go more than one way.

For example, `let y = if x > 0 then x else -x` becomes:

    entry:
        %1: i64 = load x
        %2: i64 = const 0
        %3: bool = gt %1, %2
        branch %3, then1, else2
    then1:
        %4: i64 = load x
        jump join3(%4)
    else2:
        %5: i64 = load x
        %6: i64 = neg %5
        jump join3(%6)
    join3(%7: i64):
        store y, %7

A few ideas recur:

* After `return`, `break`, `continue` or a failed `?`, nothing more runs in
  that block. Lowering carries on in a fresh "dead" block that nothing jumps
  to, and dead blocks are removed when the function is finished.
* Every Ella function becomes an IR function. A lambda is *closure
  converted*: the IR function takes the captured variables as extra leading
  parameters, and the `closure` instruction packs them with it into a
  function value.
* `return` jumps to the function's exit block rather than returning
  directly, so that postconditions are checked however the function ends.
"""

from collections import Counter
from dataclasses import dataclass, field

from ella.checker import (
    BOOL,
    I64,
    NEVER,
    STR,
    UNIT,
    Checker,
    EnumTy,
    FuncTy,
    TupleTy,
    Type,
    check,
    finish,
    literal_type,
)
from ella.ir import Block, Branch, Constant, Function, Instr, Jump, Module, Return, Trap, Value, successors, verify
from ella.lexer import Span
from ella.parser import parse
from ella.syntax import (
    Assert,
    Assign,
    Binary,
    BoolLit,
    Break,
    Call,
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
    StringLit,
    Try,
    TupleLit,
    TuplePattern,
    Unary,
    UnitLit,
    VariantPattern,
    While,
    WildcardPattern,
    mentions_return,
)
from ella.syntax import (
    Block as BlockExpr,
)
from ella.syntax import (
    Return as ReturnExpr,
)

# Ella operators and the IR operations that implement them.
BINARY_OPS = {
    "+": "add",
    "-": "sub",
    "*": "mul",
    "/": "div",
    "//": "floordiv",
    "%": "mod",
    "**": "pow",
    "&": "bitand",
    "|": "bitor",
    "^": "bitxor",
    "<<": "shl",
    ">>": "shr",
    "..": "range",
    "..=": "range_incl",
}
COMPARE_OPS = {"==": "eq", "!=": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
UNARY_OPS = {"-": "neg", "not": "not", "~": "bitnot"}

# In a scope, the slot for a name. SELF marks a function's own name inside its
# body (`let f = ... => f(...)`); it isn't a real slot.
SELF = "<self>"


def compile_program(source: str) -> Module:
    """Parse, type check and lower a program, and verify the IR. Raises EllaError."""
    program = parse(source)
    module = Lowerer(check(program)).lower_program(program)
    verify(module)
    return module


@dataclass(slots=True)
class PendingFunction:
    """A lambda whose IR function hasn't been made yet."""

    function: Lambda
    name: str
    captures: list[tuple[str, Type]]  # the captured variables, in parameter order
    self_names: list[str]  # names the function has for itself, from `let`
    type: FuncTy


@dataclass(slots=True)
class LoopTarget:
    """Where `break` and `continue` go for one loop being lowered."""

    label: str | None
    exit: Block  # `break` jumps here, with the value if the exit block takes one
    next: Block  # `continue` jumps here


@dataclass(slots=True)
class SelfInfo:
    """What a function needs to make a closure of itself: its name, type, and capture slots."""

    name: str
    type: FuncTy
    capture_slots: list[str] = field(default_factory=list)


class Lowerer:
    def __init__(self, checker: Checker) -> None:
        self.checker = checker
        self.module = Module(enums=dict(checker.enums))
        for name, info in checker.globals.vars.items():
            if info.kind == "global":
                self.module.globals[name] = finish(info.type)
        self.pending: list[PendingFunction] = []

        # The function being lowered. `start_function` resets all of these.
        self.function = Function("", UNIT, Span(1, 1))
        self.block = Block("")
        self.next_id = 0  # numbers for new values and blocks
        self.scopes: list[dict[str, str]] = []  # Ella name -> slot, innermost last
        self.loops: list[LoopTarget] = []
        self.exit = Block("")  # where `return` goes
        self.self_info: SelfInfo | None = None

    # Building blocks

    def start_function(self, name: str, result: Type, span: Span) -> None:
        self.function = Function(name, result, span)
        self.module.functions[name] = self.function
        self.next_id = 0
        self.block = self.new_block("entry")
        self.scopes, self.loops, self.self_info = [], [], None

    def finish_function(self) -> None:
        """Remove the blocks that can't be reached from the entry, such as dead blocks."""
        reachable = {self.function.blocks[0]}
        work = [self.function.blocks[0]]
        while work:
            for successor in successors(work.pop()):
                if successor not in reachable:
                    reachable.add(successor)
                    work.append(successor)
        # Keep the blocks in the order they were made, but with the exit last,
        # which reads more naturally.
        blocks = [block for block in self.function.blocks if block in reachable and block is not self.exit]
        self.function.blocks = blocks + ([self.exit] if self.exit in reachable else [])

    def new_value(self, t: Type) -> Value:
        self.next_id += 1
        return Value(self.next_id, t)

    def new_block(self, hint: str) -> Block:
        self.next_id += 1
        block = Block(f"{hint}{self.next_id}")
        self.function.blocks.append(block)
        return block

    def emit(self, op: str, t: Type, args: list[Value], span: Span, name: str = "", index: int = 0) -> Value:
        """Add an instruction with a result to the current block, and return the result."""
        dest = self.new_value(t)
        self.block.instrs.append(Instr(op, dest, args, span, name, index))
        return dest

    def emit_effect(self, op: str, args: list[Value], span: Span, name: str = "") -> None:
        """Add an instruction that has no result, like `store`."""
        self.block.instrs.append(Instr(op, None, args, span, name))

    def const(self, value: Constant, span: Span) -> Value:
        t = UNIT if isinstance(value, tuple) else literal_type(value)
        dest = self.new_value(t)
        self.block.instrs.append(Instr("const", dest, [], span, const=value))
        return dest

    def jump(self, target: Block, args: list[Value] | None = None) -> None:
        self.block.terminator = Jump(target, args or [])

    def branch(self, cond: Value, then: Block, else_: Block) -> None:
        self.block.terminator = Branch(cond, then, else_)

    def leave(self, span: Span) -> Value:
        """After a jump away (`return`, `break`, ...): continue in a dead block.

        The expression that jumped has type never, so its "value" is never
        used; a placeholder stands in for it.
        """
        self.block = self.new_block("dead")
        return self.const((), span)

    def join_block(self, t: Type) -> tuple[Block, Value | None]:
        """A block where branches meet, with a parameter for their value if there is one."""
        join = self.new_block("join")
        if t in (UNIT, NEVER):
            return join, None
        param = self.new_value(t)
        join.params.append(param)
        return join, param

    def result_of(self, param: Value | None, span: Span) -> Value:
        """The value of a joined expression: its join parameter, or () if it has no value."""
        return param if param is not None else self.const((), span)

    # Variables

    def new_slot(self, name: str, t: Type) -> str:
        """A local slot for a variable. Shadowed names get new slots: `x`, `x.1`, ..."""
        slot, n = name, 0
        while slot in self.function.locals:
            n += 1
            slot = f"{name}.{n}"
        self.function.locals[slot] = t
        return slot

    def bind(self, name: str, value: Value, span: Span) -> None:
        """Make `name` a variable in the innermost scope, holding `value`."""
        if not self.scopes:  # at top level, `let` makes a global
            self.emit_effect("global_set", [value], span, name)
            return
        # The alternatives of an or-pattern bind the same names, and must use
        # the same slots. Otherwise each binding gets a new slot.
        slot = self.scopes[-1].get(name)
        if slot is None or slot == SELF or self.function.locals[slot] != value.type:
            slot = self.new_slot(name, value.type)
        self.scopes[-1][name] = slot
        self.emit_effect("store", [value], span, slot)

    def find_slot(self, name: str) -> str:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        raise AssertionError(f"the checker resolved '{name}' as local, but it has no slot")

    def read_local(self, name: str, span: Span) -> Value:
        slot = self.find_slot(name)
        if slot == SELF:
            # A function's own name: rebuild its closure from its own captured values.
            assert self.self_info is not None
            captured = [self.emit("load", self.function.locals[s], [], span, s) for s in self.self_info.capture_slots]
            return self.emit("closure", self.self_info.type, captured, span, self.self_info.name)
        return self.emit("load", self.function.locals[slot], [], span, slot)

    def read_name(self, name: str, kind: str, span: Span) -> Value:
        if kind == "local":
            return self.read_local(name, span)
        return self.emit("global_get", self.module.globals[name], [], span, name)

    def write_name(self, name: str, kind: str, value: Value, span: Span) -> None:
        if kind == "local":
            self.emit_effect("store", [value], span, self.find_slot(name))
        else:
            self.emit_effect("global_set", [value], span, name)

    # Programs and functions

    def lower_program(self, program: Program) -> Module:
        """Lower the top-level code into the `top` function, then every function it uses."""
        self.start_function("top", UNIT, program.span)
        # Top-level functions exist from the start (docs/grammar.md §3).
        hoisted: dict[int, Value] = {}
        for expr in program.body:
            if isinstance(expr, Let) and isinstance(expr.value, Lambda):
                function = self.lower_lambda(expr.value, [b.name for b in expr.bindings], expr.span)
                for binding in expr.bindings:
                    self.emit_effect("global_set", [function], expr.span, binding.name)
                hoisted[id(expr)] = function

        # How many top-level `let`s bind each name.
        lets = Counter(b.name for expr in program.body if isinstance(expr, Let) for b in expr.bindings)
        for expr in program.body:
            if isinstance(expr, Let) and id(expr) in hoisted:
                # Bind it again where its `let` is if the name could hold
                # something else by now: if another top-level `let` binds it
                # (when hoisting, the last one wins), or it can be assigned to.
                for binding in expr.bindings:
                    if binding.mutable or lets[binding.name] > 1:
                        self.emit_effect("global_set", [hoisted[id(expr)]], expr.span, binding.name)
            else:
                self.lower(expr)

        # Then run `main`, if there is one.
        main = self.checker.globals.vars.get("main")
        if main is not None and main.kind == "global":
            function = self.emit("global_get", self.module.globals["main"], [], program.span, "main")
            self.emit("call", UNIT, [function], program.span)
        self.block.terminator = Return(self.const((), program.span))
        self.finish_function()

        while self.pending:
            self.lower_function(self.pending.pop(0))
        return self.module

    def lower_lambda(self, function: Lambda, self_names: list[str], span: Span) -> Value:
        """A lambda expression: a `closure` of a new IR function, capturing every local variable."""
        visible: dict[str, str] = {}
        for scope in self.scopes:  # outermost first, so inner names shadow outer ones
            visible.update(scope)
        captured = [self.read_local(name, span) for name in visible]
        base = self_names[0] if self_names else "lambda"
        name, n = base, 0
        while name in self.module.functions or name == "top" or any(p.name == name for p in self.pending):
            n += 1
            name = f"{base}.{n}"
        t = self.checker.type_of(function)
        assert isinstance(t, FuncTy)
        captures = [(var, value.type) for var, value in zip(visible, captured)]
        self.pending.append(PendingFunction(function, name, captures, self_names, t))
        return self.emit("closure", t, captured, span, name)

    def lower_function(self, pending: PendingFunction) -> None:
        """Make the IR function for a lambda: captures and parameters, `where`, body, and exit."""
        function, t = pending.function, pending.type
        self.start_function(pending.name, t.result, function.span)
        entry = self.block
        self.scopes = [{}]
        self.self_info = SelfInfo(pending.name, t)

        # Captured variables come first, then the declared parameters. Each is
        # stored in a slot, so it is read like any other variable.
        for var, var_type in pending.captures:
            param = self.new_value(var_type)
            entry.params.append(param)
            slot = self.new_slot(var, var_type)
            self.self_info.capture_slots.append(slot)
            self.scopes[0][var] = slot
            self.emit_effect("store", [param], function.span, slot)
        for self_name in pending.self_names:
            self.scopes[0][self_name] = SELF
        for declared, param_type in zip(function.params, t.params):
            param = self.new_value(param_type)
            entry.params.append(param)
            self.bind(declared.name, param, declared.span)

        self.exit = self.new_block("exit")
        result = self.new_value(t.result)
        self.exit.params.append(result)

        for cond in function.where:
            if not mentions_return(cond.expr):
                self.lower_condition(cond, "precondition")
        self.jump(self.exit, [self.lower(function.body)])

        self.block = self.exit
        postconditions = [cond for cond in function.where if mentions_return(cond.expr)]
        if postconditions:
            self.scopes.append({})
            self.bind("return", result, function.span)
            for cond in postconditions:
                self.lower_condition(cond, "postcondition")
            self.scopes.pop()
        self.block.terminator = Return(result)
        self.finish_function()

    def lower_condition(self, cond: Assert, kind: str) -> None:
        value = self.lower(cond.expr)
        self.emit_effect("assert", [value], cond.span, f"{kind} failed: {cond.source}")

    # Expressions

    def lower(self, expr: Expr) -> Value:
        """Emit the instructions that compute `expr`, and return its value."""
        span = expr.span
        match expr:
            # Literals
            case IntLit(literal) | FloatLit(literal) | BoolLit(literal):
                return self.const(literal, span)
            case StringLit(parts):
                return self.lower_string(parts, span)
            case UnitLit():
                return self.const((), span)
            case TupleLit(items):
                return self.emit("tuple", self.type_of(expr), [self.lower(item) for item in items], span)
            case ListLit(items):
                return self.emit("list", self.type_of(expr), [self.lower(item) for item in items], span)

            # Variables
            case Name(name):
                kind = self.checker.kinds[id(expr)]
                if kind == "variant":
                    return self.emit("variant", self.type_of(expr), [], span, name)
                return self.read_name(name, kind, span)
            case Let(bindings, value_expr):
                if isinstance(value_expr, Lambda):
                    value = self.lower_lambda(value_expr, [b.name for b in bindings], span)
                else:
                    value = self.lower(value_expr)
                for binding in bindings:
                    self.bind(binding.name, value, binding.span)
                return value
            case Assign(op, target, value_expr):
                return self.lower_assign(op, target, value_expr, span)

            # Operators
            case Unary(op, operand):
                return self.emit(UNARY_OPS[op], self.type_of(expr), [self.lower(operand)], span)
            case Binary(("and" | "or") as op, left, right):
                return self.lower_logic(op, left, right, span)
            case Binary(op, left, right):
                return self.emit(BINARY_OPS[op], self.type_of(expr), [self.lower(left), self.lower(right)], span)
            case Compare(first, rest):
                return self.lower_compare(first, rest, span)
            case Index(target, index):
                return self.emit("index", self.type_of(expr), [self.lower(target), self.lower(index)], span)
            case Try(operand):
                return self.lower_try(self.lower(operand), self.type_of(expr), span)
            case Member():
                raise AssertionError("the checker rejects member access")

            # Functions
            case Lambda():
                return self.lower_lambda(expr, [], span)
            case Call(callee, args):
                return self.lower_call(expr, callee, args, span)
            case ReturnExpr(value):
                result = self.const((), span) if value is None else self.lower(value)
                self.jump(self.exit, [result])
                return self.leave(span)

            # Control flow
            case BlockExpr(exprs):
                self.scopes.append({})
                results = [self.lower(line) for line in exprs]
                self.scopes.pop()
                result = results[-1]
                # A block that ends with an assignment has the value ().
                return self.const((), span) if isinstance(exprs[-1], Assign) else result
            case If(cond, then, else_):
                return self.lower_if(cond, then, else_, self.type_of(expr), span)
            case Match(subject):
                return self.lower_match(expr, self.lower(subject), span)
            case For():
                return self.lower_for(expr)
            case While(label, cond, body):
                # header: evaluate cond, then go to body or exit. body: ..., back to header.
                header, body_block, exit = self.new_block("while"), self.new_block("body"), self.new_block("exit")
                self.jump(header)
                self.block = header
                self.branch(self.lower(cond), body_block, exit)
                self.block = body_block
                self.lower_loop_body(LoopTarget(label, exit, header), body)
                self.jump(header)
                self.block = exit
                return self.const((), span)
            case Loop(label, body):
                body_block = self.new_block("loop")
                exit, param = self.join_block(self.type_of(expr))
                self.jump(body_block)
                self.block = body_block
                self.lower_loop_body(LoopTarget(label, exit, body_block), body)
                self.jump(body_block)
                self.block = exit
                return self.result_of(param, span)
            case Break(label, value):
                loop = self.find_loop(label)
                broken = None if value is None else self.lower(value)
                if loop.exit.params:  # a `loop` with a value
                    self.jump(loop.exit, [broken if broken is not None else self.const((), span)])
                else:
                    self.jump(loop.exit)
                return self.leave(span)
            case Continue(label):
                self.jump(self.find_loop(label).next)
                return self.leave(span)

            # Others
            case Assert():
                self.lower_condition(expr, "assertion")
                return self.const((), span)
            case EnumDef():
                return self.const((), span)  # enums are only types; nothing runs

    def type_of(self, expr: Expr) -> Type:
        return self.checker.type_of(expr)

    def lower_string(self, parts: tuple[str | Expr, ...], span: Span) -> Value:
        """`"a{{x}}b"` becomes `"a" + to_text(x) + "b"`."""
        pieces = []
        for part in parts:
            if isinstance(part, str):
                pieces.append(self.const(part, span))
            else:
                value = self.lower(part)
                pieces.append(value if value.type == STR else self.emit("to_text", STR, [value], part.span))
        if not pieces:
            return self.const("", span)
        result = pieces[0]
        for piece in pieces[1:]:
            result = self.emit("add", STR, [result, piece], span)
        return result

    def lower_assign(self, op: str, target: Name | Index, value_expr: Expr, span: Span) -> Value:
        """`x = v`, `x op= v`, and `xs[i][j] = v`.

        Lists are values, so assigning to an element builds new lists from the
        inside out and stores the outermost back in the variable:

            row = index xs, i
            new_row = set_index row, j, v
            new_xs = set_index xs, i, new_row
            store xs, new_xs
        """
        value = self.lower(value_expr)
        # The chain of indexes from the variable out: xs[i][j] has [xs[i], xs[i][j]].
        chain: list[Index] = []
        root: Expr = target
        while isinstance(root, Index):
            chain.insert(0, root)
            root = root.target
        assert isinstance(root, Name)
        kind = self.checker.kinds[id(root)]
        containers = [self.read_name(root.name, kind, root.span)]
        indexes = []
        for node in chain:
            indexes.append(self.lower(node.index))
            # Reading the element also checks the index is in range.
            containers.append(self.emit("index", self.type_of(node), [containers[-1], indexes[-1]], node.span))
        if op != "=":
            old = containers[-1]
            value = self.emit(BINARY_OPS[op.removesuffix("=")], old.type, [old, value], span)
        new = value
        for container, index, node in reversed(list(zip(containers, indexes, chain))):
            new = self.emit("set_index", container.type, [container, index, new], node.span)
        self.write_name(root.name, kind, new, span)
        return value

    def lower_logic(self, op: str, left: Expr, right: Expr, span: Span) -> Value:
        """`a and b` only evaluates `b` if `a` is true; `a or b` only if `a` is false."""
        a = self.lower(left)
        rhs, short = self.new_block("rhs"), self.new_block("short")
        join, param = self.join_block(BOOL)
        assert param is not None
        if op == "and":
            self.branch(a, rhs, short)
        else:
            self.branch(a, short, rhs)
        self.block = short
        self.jump(join, [self.const(op == "or", span)])
        self.block = rhs
        self.jump(join, [self.lower(right)])
        self.block = join
        return param

    def lower_compare(self, first: Expr, rest: tuple[tuple[str, Expr], ...], span: Span) -> Value:
        """`a < b <= c`: compare in turn, stopping with false at the first comparison that fails."""
        if len(rest) == 1:  # a single comparison needs no blocks
            op, operand = rest[0]
            return self.compare(op, self.lower(first), self.lower(operand), span)
        join, param = self.join_block(BOOL)
        assert param is not None
        fail = self.new_block("false")
        left = self.lower(first)
        for n, (op, operand) in enumerate(rest):
            right = self.lower(operand)
            result = self.compare(op, left, right, span)
            if n == len(rest) - 1:
                self.jump(join, [result])
            else:
                next_block = self.new_block("compare")
                self.branch(result, next_block, fail)
                self.block = next_block
            left = right
        self.block = fail
        self.jump(join, [self.const(False, span)])
        self.block = join
        return param

    def compare(self, op: str, left: Value, right: Value, span: Span) -> Value:
        if op in ("in", "not in"):
            result = self.emit("contains", BOOL, [left, right], span)
            return self.emit("not", BOOL, [result], span) if op == "not in" else result
        return self.emit(COMPARE_OPS[op], BOOL, [left, right], span)

    def lower_try(self, value: Value, t: Type, span: Span) -> Value:
        """`v?`: if v is `Ok(x)` (or `Some(x)`) the result is x; otherwise return the error."""
        assert isinstance(value.type, EnumTy)
        ok, error = ("Ok", "Error") if value.type.name == "Result" else ("Some", "None")
        ok_block, error_block = self.new_block("ok"), self.new_block("error")
        self.branch(self.emit("is_variant", BOOL, [value], span, ok), ok_block, error_block)
        # The error is rebuilt with the function's own result type, which may
        # differ from v's in the Ok type: Result[i64, str] into Result[(), str].
        self.block = error_block
        fields = [
            self.emit("variant_field", t_, [value], span, error, i)
            for i, t_ in enumerate(self.fields(value.type, error))
        ]
        self.jump(self.exit, [self.emit("variant", self.function.result, fields, span, error)])
        self.block = ok_block
        return self.emit("variant_field", t, [value], span, ok, 0)

    def fields(self, enum_type: Type, variant: str) -> tuple[Type, ...]:
        assert isinstance(enum_type, EnumTy)
        return tuple(finish(t) for t in self.checker.variant_fields(enum_type, variant))

    def lower_call(self, expr: Call, callee: Expr, args: tuple[Expr, ...], span: Span) -> Value:
        kind = self.checker.kinds.get(id(callee))
        values = [self.lower(arg) for arg in args]
        if isinstance(callee, Name) and kind == "builtin":
            if callee.name == "len":
                return self.emit("len", I64, values, span)
            self.emit_effect("print", values, span)
            return self.const((), span)
        if isinstance(callee, Name) and kind == "constructor":
            return self.emit("variant", self.type_of(expr), values, span, callee.name)
        function = self.lower(callee)
        return self.emit("call", self.type_of(expr), [function, *values], span)

    def lower_if(self, cond: Expr, then: Expr, else_: Expr | None, t: Type, span: Span) -> Value:
        then_block, else_block = self.new_block("then"), self.new_block("else")
        join, param = self.join_block(t if else_ is not None else UNIT)
        self.branch(self.lower(cond), then_block, else_block)
        for block, body in ((then_block, then), (else_block, else_)):
            self.block = block
            value = self.lower(body) if body is not None else None
            self.jump(join, [value] if param is not None and value is not None else [])
        self.block = join
        return self.result_of(param, span)

    def lower_match(self, expr: Match, subject: Value, span: Span) -> Value:
        """Each case tests its pattern (and guard), going on to the next case if it fails.

        If no case matches, the program stops with a trap.
        """
        join, param = self.join_block(self.type_of(expr))
        for case in expr.cases:
            next_case = self.new_block("case")
            self.scopes.append({})
            self.lower_pattern(case.pattern, subject, next_case)
            if case.guard is not None:
                body = self.new_block("guarded")
                self.branch(self.lower(case.guard), body, next_case)
                self.block = body
            value = self.lower(case.body)
            self.jump(join, [value] if param is not None else [])
            self.scopes.pop()
            self.block = next_case
        self.block.terminator = Trap("no case matches {}", subject, span)
        self.block = join
        return self.result_of(param, span)

    # Loops

    def lower_for(self, expr: For) -> Value:
        """`for pattern in xs body` counts through the items with a hidden counter:

        n = len xs; i = 0
        header: if i < n go to body, else exit
        body:   item = index xs, i; match the pattern; body; go to next
        next:   i = i + 1; go to header
        """
        span = expr.span
        iterable = self.lower(expr.iterable)
        count = self.emit("len", I64, [iterable], span)
        counter = self.new_slot("for.i", I64)
        self.emit_effect("store", [self.const(0, span)], span, counter)
        header, body, next_block, exit = (self.new_block(h) for h in ("for", "body", "next", "exit"))
        self.jump(header)

        self.block = header
        i = self.emit("load", I64, [], span, counter)
        self.branch(self.emit("lt", BOOL, [i, count], span), body, exit)

        self.block = body
        item_type = self.checker.types[id(expr.pattern)]
        item = self.emit("index", finish(item_type), [iterable, i], span)
        no_match = self.new_block("nomatch")
        self.scopes.append({})
        self.lower_pattern(expr.pattern, item, no_match)
        self.lower_loop_body(LoopTarget(expr.label, exit, next_block), expr.body)
        self.scopes.pop()
        self.jump(next_block)

        self.block = no_match
        self.block.terminator = Trap("the 'for' pattern doesn't match {}", item, span)

        self.block = next_block
        i = self.emit("load", I64, [], span, counter)
        self.emit_effect("store", [self.emit("add", I64, [i, self.const(1, span)], span)], span, counter)
        self.jump(header)

        self.block = exit
        return self.const((), span)

    def lower_loop_body(self, target: LoopTarget, body: Expr) -> None:
        self.loops.append(target)
        self.lower(body)  # its value is discarded
        self.loops.pop()

    def find_loop(self, label: str | None) -> LoopTarget:
        for loop in reversed(self.loops):
            if label is None or loop.label == label:
                return loop
        raise AssertionError("the checker rejects 'break' and 'continue' outside loops")

    # Patterns

    def lower_pattern(self, pattern: Pattern, value: Value, fail: Block) -> None:
        """Test `value` against `pattern`, binding its variables; go to `fail` if it doesn't match.

        The tests are emitted one after another. Each failing test branches to
        `fail`, and lowering continues in the block where it succeeded.
        """
        span = pattern.span
        match pattern:
            case WildcardPattern():
                pass
            case NamePattern(name):
                if self.checker.kinds[id(pattern)] == "variant":
                    self.test(self.emit("is_variant", BOOL, [value], span, name), fail)
                else:
                    self.bind(name, value, span)
            case LiteralPattern(literal):
                self.test(self.emit("eq", BOOL, [value, self.const(literal, span)], span), fail)
            case RangePattern(low, high, inclusive):
                self.test(self.emit("ge", BOOL, [value, self.const(low, span)], span), fail)
                self.test(self.emit("le" if inclusive else "lt", BOOL, [value, self.const(high, span)], span), fail)
            case OrPattern(alternatives):
                # Try each alternative in turn; any success goes on to `matched`.
                matched = self.new_block("matched")
                for alternative in alternatives[:-1]:
                    next_alternative = self.new_block("or")
                    self.lower_pattern(alternative, value, next_alternative)
                    self.jump(matched)
                    self.block = next_alternative
                self.lower_pattern(alternatives[-1], value, fail)
                self.jump(matched)
                self.block = matched
            case VariantPattern(name, args):
                self.test(self.emit("is_variant", BOOL, [value], span, name), fail)
                for i, (arg, field_type) in enumerate(zip(args, self.fields(value.type, name))):
                    field_value = self.emit("variant_field", field_type, [value], span, name, i)
                    self.lower_pattern(arg, field_value, fail)
            case TuplePattern(items):
                assert isinstance(value.type, TupleTy)
                for i, (item, item_type) in enumerate(zip(items, value.type.items)):
                    self.lower_pattern(item, self.emit("tuple_get", item_type, [value], span, index=i), fail)

    def test(self, cond: Value, fail: Block) -> None:
        """Continue in a new block if `cond` is true, otherwise go to `fail`."""
        passed = self.new_block("match")
        self.branch(cond, passed, fail)
        self.block = passed
