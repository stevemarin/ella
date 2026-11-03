"""The intermediate representation (IR): a typed control-flow graph.

The lowering (ella.lower) turns a checked syntax tree into this IR, and
ella.ir_interpreter runs it. The passes in ella.passes optimize it, and
ella.codegen compiles it to x64.

Structure
---------

A `Module` holds functions. Each `Function` is a list of basic blocks, the
first being its entry. A `Block` is a straight-line list of instructions with
no jumps in or out of the middle, ended by exactly one *terminator* that says
where control goes next:

    jump target(args)         continue in another block
    branch cond, then, else   continue in one of two blocks, by a bool
    return value              leave the function
    yield value, target       (in a generator function) give value to `resume`'s
                              caller; the next `resume` carries on at target
    trap "message", value     stop the program with a run-time error

Instructions are *three-address code*: each does one small thing to
operands that are already computed, like `%3: i64 = add %1, %2`.

Values
------

Each instruction that produces something defines a new `Value`, written `%n`,
which has a type and is never changed afterwards. Values flow between blocks
in two ways:

* A value can be used in any block that the defining block *dominates*
  (every path from the entry to it passes through the definer).
* Where paths join, such as after an `if`, the result arrives as a *block
  parameter*: `jump join(%4)` passes `%4` to `join(%5: i64)`. Block
  parameters do the job of SSA "phi" nodes.

Variables
---------

Ella variables can change, but values can't. For now, each variable lives
in a named, typed *local slot* of its function, read with `load` and written
with `store`. The mem2reg pass (ella.passes.mem2reg) promotes slots to
values, which is what "SSA form" means; that is easier to do as an
optimization than in the lowering.
Top-level variables are *globals*, read and written by name. Reading a
global before it is set is a run-time error.

Memory
------

Lists, strings, tuples, ranges, enum values and closures are *boxed*: a
value is a reference to an object in memory. (i64, f64, bool and () are
not.) Ella has value semantics, so no two variables may share an object
that can change; the only objects that ever change are lists, through
`set_index` and `append`. The lowering makes this safe with explicit `copy`
instructions wherever a value gets a second owner (docs/memory-model.md §3).

Every boxed object lives in an *arena*, and every instruction that makes
one names the arena, its *region*, written after an `@`:

    @0, @1, ...    the function's scope levels: 0 is the function body, and
                   each loop nested in it adds a level for its iterations
    @carry1, ...   (for variables only) the arena that holds the values a
                   loop at level 1 carries from its last iteration
    @result        where the caller wants the function's result
    @mut0, ...     where the variable passed to `mut` parameter 0 lives
    @global        the program's global arena, freed when the program ends
    @slot          (for changing an object) the arena it is in: a pool's
                   value, in its slot's own arena, or an arena's value, in
                   the arena's slot arena; the arena gets a copy

`reset k` frees levels k and up when control leaves a loop iteration, and
returning frees all of the function's levels.

Generators
----------

A generator becomes two functions (docs/memory-model.md §17). The one the
program calls makes a `Gen` with `gen_new`, taking the generator's captured
values and arguments. The other is the body, a *generator function*
(`Function.generator`), whose parameters are those values: `resume g`
runs it from where it last stopped until a `yield` terminator, which gives
`resume` its result, an Optional made in @result. Its values and slots
live in the Gen between resumes, and so do its scope levels, which are
arenas the Gen owns rather than depths of the call: they outlive each
`resume`. A generator function never returns: at its end, it frees its
levels (`reset 0`) and yields None, from a block that yields to itself.

A loop that assigns new values to variables declared outside it *carries*
them from one iteration to the next (docs/memory-model.md §6). Such a loop
at level k has two arenas, level k and a spare (level k+1; loops inside it
start at k+2), and they take turns: new objects go in @k, and the values
carried from the last iteration are in @carryk. Where a plain loop frees
level k after each iteration, `swap k` frees the carried arena and makes
the iteration's arena the carried one. The loop's variables are copied
into the iteration's arena first if they still hold values from the
carried one (`carry`), and copied out when the loop ends. So a loop that
rebuilds a value keeps at most two versions, not one per iteration. A value must never be used
after its arena is freed, and an object must never be stored where it would
outlive its arena; the IR interpreter checks both at run time. Freeing an
arena first runs the finalizers of the values in it that have them (nocopy
structs with `finalize`, pools and arenas), newest first.

Operations
----------

    const c                      a literal: i64, f64, bool, str or () (strings are static)
    load slot, store slot, v     read or write a local variable
    global_get name, global_set name, v
    add sub mul div floordiv mod pow    arithmetic (`div` always gives f64;
                                 `add` also joins strings and lists)
    bitand bitor bitxor shl shr  bitwise operations, on i64
    neg not bitnot               unary operations
    eq ne lt le gt ge            comparisons, giving bool
    contains item, container     `in` for lists, strings and ranges
    range lo, hi                 `lo..hi`; `range_incl` for `lo..=hi`
    tuple a, b, ...              build a tuple; `tuple_get t, n` reads item n
    list a, b, ...               build a list
    index c, i                   item i of a list,string (a 1-char string) or range
    slice c, r @r                items r.start up to r.stop of a list (copies of them) or
                                 string, as a new one
    set_index xs, i, v @r        replace item i of list xs in place; xs lives in region r
    append xs, ys @r             xs with copies of ys's items added, growing xs in place
                                 if it can; xs lives in region r and isn't used again
    copy v @r                    a deep copy of v in region r
    move v @r                    v, which can't be copied, moved to region r with
                                 its finalizers (a returned local variable)
    reset k                      free the function's scope levels k and up
    swap k                       end an iteration of a loop at level k that carries values:
                                 free levels k+2 and up, run the finalizers of @k's
                                 values, free @carryk, and swap the two arenas
    carry v @k                   v, or a copy of it in @k if it is in @carryk (index k)
    pool_new @r                  a new, empty pool
    alloc pool, v                put v in a new slot of the pool, in the slot's own
                                 arena; gives its handle
    pool_get pool, h             the value a handle refers to (checking it isn't stale)
    pool_set pool, h, v          replace that value, with v in a new arena
    free pool, h                 free that value, with its arena
    arena_new @r                 a new, empty Arena, with a slot arena of its own
    alloc arena, v               put a copy of v in the arena; gives its ref
    pool_get arena, r            the value a ref refers to (checking it isn't stale,
                                 and is this arena's)
    pool_set arena, r, v         replace that value, with a copy of v in the arena (the
                                 old one stays there until the arena is freed)
    free arena                   free all the arena's values at once
    gen_new a, ... @r            a new Gen running generator function f (in `name`), with
                                 arenas of its own for f's levels; copies of the values
                                 a, ... (moved, if they can't be copied) in its level 0
                                 are f's parameters
    resume g @r                  run generator g until it yields: its Optional result
    quote_begin                  start building the code of quote n (in `index`),
                                 choosing fresh names for its variables
    quote begin, outer..., v...  the code of quote n: its splices' code and the
                                 values it carries are the operands (after the
                                 `quote_begin`s of the quotes it is spliced into)
    run c @r                     check code made by `quote`, lower it, and run it
    type_value t...               type n (in `index`) of the module's types, as a value,
                                 with the types t... spliced into it (`type [$t]`)
    reflect t, ... @r            a reflection built-in (in `name`) on a type: fields(t), ...
    len c                        length of a list, string or range, or a pool's or arena's count
    builtin a, ... @r            a library function (in `name`), such as split(a, b),
                                 whose result is all new objects (ella.library); abs,
                                 min and max take i64s or f64s
    to_text v                    the text form of any value
    print v                      write the text form of v and a newline
    eprint v                     the same, to standard error
    variant Name(a, ...)         make an enum value or struct (registering its finalizer)
    is_variant v, Name           does v hold that variant?
    variant_field v, Name.n      field n of v, which holds that variant
    set_field v, x @r            change field n (in `index`) of struct v in place
    closure f(a, ...)            a function value: IR function f with captured values
    call f(a, mut b @r, ...)     call a function value; the result goes in the call's
                                 region. A `mut` argument b, which lives in region r,
                                 is passed in and its new value comes back as an extra
                                 result: `%5: (), %6: [i64] = call %1(mut %2 @0) @0`.
    assert c, "message"          stop with the message if c is false

A function with `mut` parameters returns their final values along with its
result: `return %9, mut %7`.

Operations that fail at run time (overflow, an index out of range, a failed
assert, ...) carry the source span to report.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, assert_never

from ella.checker import (
    ARENA,
    BOOL,
    F64,
    GENERIC_LIBRARY,
    I64,
    LIBRARY,
    NEVER,
    RANGE,
    STR,
    UNIT,
    BuiltinTy,
    EnumInfo,
    EnumTy,
    FuncTy,
    ListTy,
    PrimTy,
    TupleTy,
    Type,
    show_type,
    substitute,
)
from ella.lexer import Span
from ella.syntax import show_literal

# The literal values `const` can hold. Unit is the empty tuple, as at run time.
type Constant = int | float | bool | str | tuple[()]


@dataclass(eq=False, slots=True)
class Value:
    """A value computed by an instruction or received as a block parameter."""

    id: int
    type: Type

    def __str__(self) -> str:
        return f"%{self.id}"


@dataclass(slots=True)
class Instr:
    """One instruction. Which of the optional fields matter depends on `op`.

    * `name`: the slot, global, variant, function or assert message.
    * `index`: the item of `tuple_get`, field of `variant_field`, or level of `reset`.
    * `const`: the literal of `const`.
    * `region`: where an allocating instruction puts its result (see "Memory").
    * `outs`, `out_regions`: a call's `mut` arguments: their new values, and
      the regions they live in.
    """

    op: str
    dest: Value | None  # None for instructions that only have an effect
    args: list[Value]
    span: Span
    name: str = ""
    index: int = 0
    const: Constant = ()
    region: str = ""
    outs: list[Value] = field(default_factory=list)
    out_regions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Jump:
    target: Block
    args: list[Value]


@dataclass(slots=True)
class Branch:
    cond: Value
    then: Block
    else_: Block


@dataclass(slots=True)
class Return:
    """Leave the function with `value`, and the final values of its `mut` parameters."""

    value: Value
    outs: list[Value] = field(default_factory=list)


@dataclass(slots=True)
class Yield:
    """In a generator function: give `value` to the caller of `resume`, which next carries on at `target`."""

    value: Value
    target: Block


@dataclass(slots=True)
class Trap:
    """Stop with a run-time error. A `{}` in the message is replaced by the value's text."""

    message: str
    value: Value | None
    span: Span


type Terminator = Jump | Branch | Return | Yield | Trap


@dataclass(eq=False, slots=True)
class Block:
    """A basic block. `level` is the scope level it runs at (until a `reset` in it), for reports."""

    name: str
    params: list[Value] = field(default_factory=list)
    instrs: list[Instr] = field(default_factory=list)
    terminator: Terminator | None = None
    level: int = 0


@dataclass(slots=True)
class Function:
    """A function. Its parameters are the entry block's parameters.

    The first `captures` parameters are the variables a closure captured;
    the rest are the declared parameters. `mut_params` are the positions of
    the `mut` ones among the declared parameters. `levels` is how many scope
    levels the function has (see "Memory"). A `generator` function is a
    generator's body (see "Generators").
    """

    name: str
    result: Type
    span: Span
    blocks: list[Block] = field(default_factory=list)
    locals: dict[str, Type] = field(default_factory=dict)  # slot name -> type
    local_regions: dict[str, str] = field(default_factory=dict)  # slot name -> where its value lives
    captures: int = 0
    mut_params: tuple[int, ...] = ()
    levels: int = 1
    generator: bool = False

    @property
    def params(self) -> list[Value]:
        return self.blocks[0].params


@dataclass(slots=True)
class Module:
    """A whole program. Running it means calling `entry`, which runs the top-level code.

    Code made at run time with `quote` is checked and lowered into the module
    when it is `run`, in an interpreter, which needs some of what the
    lowering knew: the program's checker, the syntax of each quote
    (`templates`, by number), and the generic functions and their
    specializations so far.
    """

    functions: dict[str, Function] = field(default_factory=dict)
    globals: dict[str, Type] = field(default_factory=dict)
    enums: dict[str, EnumInfo] = field(default_factory=dict)
    entry: str = "top"
    checker: Any = None  # ella.checker.Checker
    tables: Any = None  # ella.staging.Tables
    templates: dict[int, Any] = field(default_factory=dict)  # number -> its `quote` syntax tree
    # The types of `type_value`s, by number: as written (which may have splices), and as checked.
    types: list[tuple[Any, Type]] = field(default_factory=list)
    generics: dict[str, Any] = field(default_factory=dict)  # name -> (Lambda, type parameters)
    specializations: dict[tuple[str, tuple[Type, ...]], str] = field(default_factory=dict)


# Memory

# Operations that make a new object, and so name a region (when their result is boxed).
ALLOCATING = frozenset(
    {
        "tuple",
        "list",
        "variant",
        "closure",
        "range",
        "range_incl",
        "add",
        "index",
        "to_text",
        "copy",
        "move",
        "call",
        "append",
        "pool_new",
        "arena_new",
        "gen_new",
        "resume",
        "run",
        "reflect",
        "slice",
        "builtin",
        "carry",
    }
)

# Operations that change an object in place, naming the region it lives in.
CHANGING = frozenset({"set_index", "set_field", "append"})


def allocates(instr: Instr) -> bool:
    """Whether an instruction makes a new object, and so names the region to put it in.

    Indexing a list gives an item that is already in the list; indexing a
    string makes a new one-character string.
    """
    if instr.op not in ALLOCATING or instr.dest is None or not is_boxed(instr.dest.type):
        return False
    return instr.op != "index" or instr.args[0].type == STR


def is_boxed(t: Type) -> bool:
    """Whether values of type `t` are references to objects in an arena.

    A pool's handle is just a number, a slot and a generation, in one word,
    and so is an arena's ref.
    """
    return t not in (I64, F64, BOOL, UNIT, NEVER) and not (isinstance(t, BuiltinTy) and t.name in ("Handle", "Ref"))


def region_level(region: str) -> int | None:
    """The scope level a region names (`"2"` is level 2), or None for carry, result, global and mut regions."""
    return int(region) if region.isdigit() else None


def carry_level(region: str) -> int | None:
    """The level of the loop whose carried values a region holds (`"carry2"` is 2), or None."""
    rest = region.removeprefix("carry")
    return int(rest) if rest != region and rest.isdigit() else None


def rank(region: str) -> float | None:
    """Where a region comes in the order arenas are freed in, the last freed first; None for the caller's regions.

    The values a loop at level k carries are freed when the iteration ends,
    after the loops inside it (k+2, ...) but before the iteration's own
    arena, which then holds the carried values.
    """
    level = region_level(region)
    if level is not None:
        return level
    carried = carry_level(region)
    return carried + 0.5 if carried is not None else None


def outlives(a: str, b: str) -> bool:
    """Whether region `a` is certain to last at least as long as region `b`.

    The global region outlives everything. The result and `mut` regions
    belong to the caller, so they outlive every level of the function, but
    nothing says which of them outlives the others. Level k outlives the
    deeper levels k+1, k+2, ..., and the values carried by the loop at
    level k (@carryk), which outlive the levels inside that loop.
    """
    if a == b or a == "global":
        return True
    rank_a, rank_b = rank(a), rank(b)
    if rank_b is None:
        return False
    return rank_a is None or rank_a <= rank_b


def shortest(a: str, b: str) -> str:
    """A region that both `a` and `b` outlive: where something holding parts from both must count as living."""
    if outlives(a, b):
        return b
    if outlives(b, a):
        return a
    return "0"  # two of the caller's regions: both outlive the function's own level 0


def trim_levels(function: Function) -> bool:
    """Drop scope levels nothing allocates in, and the `reset`s that free them. Returns whether anything changed.

    Freeing an empty arena does nothing, so a loop that makes no objects
    needs no level of its own.
    """
    used = 0
    for block in function.blocks:
        for instr in block.instrs:
            level = region_level(instr.region) if instr.op not in ("set_index", "set_field") else None
            if level is not None:
                used = max(used, level + 1)
            if instr.op == "swap":  # the loop's spare arena is the next level
                used = max(used, instr.index + 2)
    used = max(used, 1)
    changed = used < function.levels
    for block in function.blocks:
        kept = [i for i in block.instrs if not (i.op == "reset" and i.index >= used)]
        changed |= len(kept) != len(block.instrs)
        block.instrs = kept
    function.levels = used
    return changed


def successors(block: Block) -> list[Block]:
    """The blocks control can go to next."""
    match block.terminator:
        case Jump(target):
            return [target]
        case Branch(_, then, else_):
            return [then, else_]
        case Yield(_, target):
            return [target]
        case _:
            return []


# Helpers for passes that change the IR (ella.passes)


def predecessors(function: Function) -> dict[Block, list[Block]]:
    """For each block, the blocks that can go to it next."""
    result: dict[Block, list[Block]] = {block: [] for block in function.blocks}
    for block in function.blocks:
        for successor in successors(block):
            result[successor].append(block)
    return result


def reachable(function: Function) -> list[Block]:
    """The blocks that can be reached from the entry, in their current order."""
    seen = {function.blocks[0]}
    work = [function.blocks[0]]
    while work:
        for successor in successors(work.pop()):
            if successor not in seen:
                seen.add(successor)
                work.append(successor)
    return [block for block in function.blocks if block in seen]


def next_value_id(function: Function) -> int:
    """A value id not used yet in the function."""
    ids = [v.id for block in function.blocks for v in block.params]
    ids += [v.id for block in function.blocks for i in block.instrs for v in defined_by(i)]
    return max(ids, default=0) + 1


def defined_by(instr: Instr) -> list[Value]:
    """The values an instruction defines: its result, and a call's new values of `mut` arguments."""
    return ([instr.dest] if instr.dest is not None else []) + instr.outs


def replace_values(function: Function, replacements: dict[int, Value]) -> None:
    """Replace every use of a value by another, following chains: if %1 -> %2 and %2 -> %3, uses of %1 become %3."""

    def resolve(value: Value) -> Value:
        while value.id in replacements:
            value = replacements[value.id]
        return value

    for block in function.blocks:
        for instr in block.instrs:
            instr.args = [resolve(arg) for arg in instr.args]
        match block.terminator:
            case Jump() as jump:
                jump.args = [resolve(arg) for arg in jump.args]
            case Branch() as branch:
                branch.cond = resolve(branch.cond)
            case Return() as ret:
                ret.value = resolve(ret.value)
                ret.outs = [resolve(out) for out in ret.outs]
            case Yield() as yielded:
                yielded.value = resolve(yielded.value)
            case Trap(value=value) as trap if value is not None:
                trap.value = resolve(value)


def used_values(function: Function) -> set[int]:
    """The ids of all values used as operands anywhere in the function."""
    used = {arg.id for block in function.blocks for instr in block.instrs for arg in instr.args}
    for block in function.blocks:
        match block.terminator:
            case Jump(_, args):
                used.update(arg.id for arg in args)
            case Branch(cond):
                used.add(cond.id)
            case Return(value, outs):
                used.add(value.id)
                used.update(out.id for out in outs)
            case Yield(value):
                used.add(value.id)
            case Trap(value=value) if value is not None:
                used.add(value.id)
    return used


def count_uses(function: Function) -> Counter[int]:
    """How many times each value is used, by instructions and terminators."""
    uses: Counter[int] = Counter()
    for block in function.blocks:
        for instr in block.instrs:
            uses.update(arg.id for arg in instr.args)
        match block.terminator:
            case Jump(_, args):
                uses.update(arg.id for arg in args)
            case Branch(cond):
                uses[cond.id] += 1
            case Return(value, outs):
                uses.update([value.id, *(out.id for out in outs)])
            case Yield(value):
                uses[value.id] += 1
            case Trap(value=value) if value is not None:
                uses[value.id] += 1
    return uses


def remove_param(function: Function, block: Block, index: int) -> None:
    """Remove a block's parameter, and the matching argument of every jump to it."""
    del block.params[index]
    for predecessor in function.blocks:
        if isinstance(predecessor.terminator, Jump) and predecessor.terminator.target is block:
            del predecessor.terminator.args[index]


# Printing


def format_module(module: Module) -> str:
    return "\n\n".join(format_function(function) for function in module.functions.values()) + "\n"


def format_function(function: Function) -> str:
    def param(i: int, p: Value) -> str:
        is_mut = i - function.captures in function.mut_params and i >= function.captures
        return f"{'mut ' if is_mut else ''}{p}: {show_type(p.type)}"

    params = ", ".join(param(i, p) for i, p in enumerate(function.params))
    levels = f"  # levels: {function.levels}" if function.levels > 1 else ""
    kind = "generator" if function.generator else "function"
    lines = [f"{kind} {function.name}({params}) -> {show_type(function.result)}{levels}"]
    for slot, t in function.locals.items():
        region = function.local_regions.get(slot, "") if is_boxed(t) else ""
        lines.append(f"    local {slot}: {show_type(t)}" + (f" @{region}" if region else ""))
    for block in function.blocks:
        block_params = ", ".join(f"{p}: {show_type(p.type)}" for p in block.params)
        lines.append(f"  {block.name}({block_params}):" if block.params else f"  {block.name}:")
        lines.extend(f"    {format_instr(instr)}" for instr in block.instrs)
        lines.append(f"    {format_terminator(block.terminator)}")
    return "\n".join(lines)


def format_instr(instr: Instr) -> str:
    args = ", ".join(map(str, instr.args))
    match instr.op:
        case "const":
            operands = "()" if instr.const == () else show_literal(instr.const)  # type: ignore[arg-type]
        case "load" | "global_get":
            operands = instr.name
        case "store" | "global_set":
            operands = f"{instr.name}, {args}"
        case "variant":
            operands = f"{instr.name}({args})" if instr.args else instr.name
        case "is_variant":
            operands = f"{args}, {instr.name}"
        case "variant_field":
            operands = f"{args}, {instr.name}.{instr.index}"
        case "tuple_get":
            operands = f"{args}, {instr.index}"
        case "closure" | "gen_new":
            operands = f"{instr.name}({args})"
        case "call":
            callee, *rest = instr.args
            mutable = callee.type.mutable if isinstance(callee.type, FuncTy) else ()
            regions = iter(instr.out_regions)
            shown = [f"mut {arg} @{next(regions, '?')}" if i in mutable else str(arg) for i, arg in enumerate(rest)]
            operands = f"{callee}(" + ", ".join(shown) + ")"
        case "assert":
            operands = f"{args}, {show_literal(instr.name)}"
        case "reset" | "swap":
            operands = str(instr.index)
        case _:
            operands = args
    text = f"{instr.op} {operands}".rstrip()
    if instr.region:
        text += f" @{instr.region}"
    if instr.dest is None:
        return text
    results = ", ".join(f"{v}: {show_type(v.type)}" for v in [instr.dest, *instr.outs])
    return f"{results} = {text}"


def format_terminator(terminator: Terminator | None) -> str:
    match terminator:
        case Jump(target, args):
            return f"jump {target.name}(" + ", ".join(map(str, args)) + ")" if args else f"jump {target.name}"
        case Branch(cond, then, else_):
            return f"branch {cond}, {then.name}, {else_.name}"
        case Return(value, outs):
            return f"return {value}" + "".join(f", mut {out}" for out in outs)
        case Yield(value, target):
            return f"yield {value}, {target.name}"
        case Trap(message, value):
            return f"trap {show_literal(message)}" + (f", {value}" if value is not None else "")
        case None:
            return "<missing terminator>"
        case _:
            assert_never(terminator)


def format_placement(module: Module) -> str:
    """Where each allocating instruction puts its object, for `ella --placement`.

    An allocation inside a loop that goes to an arena outside the loop is
    marked: it isn't freed when the iteration ends, so a long loop can pile
    up objects there ("region bloat", docs/memory-model.md §6). Growing a
    list with `append` is the usual exception, as it reuses its space.
    """
    lines = []
    for function in module.functions.values():
        sites = []
        for block in function.blocks:
            level = block.level
            for instr in block.instrs:
                if instr.op == "reset":
                    level = instr.index - 1
                if not allocates(instr):
                    continue
                assert instr.dest is not None
                site = str(instr.span)
                region = f"@{instr.region}"
                text = f"  {site:<8} {instr.op:<9} {region:<8} {show_type(instr.dest.type)}"
                if level > 0 and region_level(instr.region) != level:
                    text += f"  <- in a loop (level {level}), but outlives the iteration"
                sites.append(text)
        if sites:
            lines += [f"function {function.name}", *sites, ""]
    return "\n".join(lines)


# Verifying


class IRError(Exception):
    """The IR breaks one of its rules. This is a bug in the compiler, not in the Ella program."""


def verify(module: Module) -> None:
    """Check the module's structural and type rules, raising IRError for the first problem.

    The lowering should always produce valid IR; the verifier catches the
    cases where it doesn't, before they turn into confusing behavior later.
    """
    for function in module.functions.values():
        Verifier(module, function).verify()


class Verifier:
    def __init__(self, module: Module, function: Function) -> None:
        self.module = module
        self.function = function

    def fail(self, message: str, block: Block | None = None) -> IRError:
        where = f"{self.function.name}" + (f", {block.name}" if block is not None else "")
        return IRError(f"{where}: {message}")

    def verify(self) -> None:
        blocks = self.function.blocks
        if not blocks:
            raise self.fail("has no blocks")
        names = [block.name for block in blocks]
        if len(set(names)) != len(names):
            raise self.fail("has two blocks with the same name")

        # Each value is defined exactly once: as a block parameter or by an instruction.
        defined_in: dict[int, Block] = {}
        for block in blocks:
            for value in [*block.params, *(v for i in block.instrs for v in defined_by(i))]:
                if value.id in defined_in:
                    raise self.fail(f"{value} is defined twice", block)
                defined_in[value.id] = block

        dominators = self.dominators()
        for block in blocks:
            if block.terminator is None:
                raise self.fail("has no terminator", block)
            seen = {p.id for p in block.params}  # values defined so far in this block

            def check_use(value: Value, block: Block = block, seen: set[int] = seen) -> None:
                # A value may be used after its definition in the same block, or
                # anywhere in a block its defining block dominates.
                definer = defined_in.get(value.id)
                if definer is None:
                    raise self.fail(f"{value} is used but never defined", block)
                if definer is block and value.id not in seen:
                    raise self.fail(f"{value} is used before it is defined", block)
                if definer is not block and definer not in dominators[block]:
                    raise self.fail(f"{value} is used where its definition doesn't dominate", block)

            for instr in block.instrs:
                for arg in instr.args:
                    check_use(arg)
                self.check_instr(instr, block)
                seen.update(v.id for v in defined_by(instr))
            self.check_terminator(block, check_use)

    def dominators(self) -> dict[Block, set[Block]]:
        """For each block, the blocks that every path from the entry to it goes through.

        The classic iterative algorithm: start by assuming every block is
        dominated by all blocks, then repeatedly narrow each block's set to
        itself plus what all its predecessors have in common, until nothing
        changes.
        """
        blocks = self.function.blocks
        entry = blocks[0]
        predecessors: dict[Block, list[Block]] = {block: [] for block in blocks}
        for block in blocks:
            for successor in successors(block):
                if successor not in predecessors:
                    raise self.fail(f"jumps to {successor.name}, which isn't in this function", block)
                predecessors[successor].append(block)
        everything = set(blocks)
        dominators = {block: {block} if block is entry else set(everything) for block in blocks}
        changed = True
        while changed:
            changed = False
            for block in blocks[1:]:
                common = (
                    set.intersection(*(dominators[p] for p in predecessors[block])) if predecessors[block] else set()
                )
                new = {block} | common
                if new != dominators[block]:
                    dominators[block] = new
                    changed = True
        return dominators

    def check_terminator(self, block: Block, check_use: Callable[[Value], None]) -> None:
        match block.terminator:
            case Jump(target, args):
                for arg in args:
                    check_use(arg)
                if [a.type for a in args] != [p.type for p in target.params]:
                    raise self.fail(f"jump to {target.name} passes the wrong number or types of values", block)
            case Branch(cond, then, else_):
                check_use(cond)
                self.require(cond.type == BOOL, "branch condition isn't a bool", block)
                if then.params or else_.params:
                    raise self.fail("branch targets can't take parameters", block)
            case Return(value, outs):
                check_use(value)
                self.require(not self.function.generator, "a generator function can't return", block)
                self.require(value.type == self.function.result, "returns a value of the wrong type", block)
                for out in outs:
                    check_use(out)
                declared = self.function.params[self.function.captures :]
                expected = [declared[i].type for i in self.function.mut_params]
                self.require([out.type for out in outs] == expected, "returns the wrong mut parameters", block)
            case Yield(value, target):
                check_use(value)
                self.require(self.function.generator, "only a generator function can yield", block)
                self.require(value.type == self.function.result, "yields a value of the wrong type", block)
                self.require(not target.params, "a yield's target can't take parameters", block)
            case Trap(_, value):
                if value is not None:
                    check_use(value)

    def require(self, condition: bool, message: str, block: Block) -> None:
        if not condition:
            raise self.fail(message, block)

    def check_instr(self, instr: Instr, block: Block) -> None:
        """Check that an instruction's operands and result have the types its operation needs."""
        args = [a.type for a in instr.args]
        dest = instr.dest.type if instr.dest is not None else None
        effects = (
            "store", "global_set", "print", "eprint", "assert", "set_index", "set_field", "reset", "swap", "free",
            "pool_set",
        )  # fmt: skip
        self.require((dest is None) == (instr.op in effects), f"{instr.op} has the wrong kind of result", block)
        self.require(not instr.outs or instr.op == "call", f"{instr.op} can't have extra results", block)
        self.check_region(instr, block)

        def need(condition: bool) -> None:
            self.require(condition, f"bad operand or result types for {format_instr(instr)}", block)

        def kind(t: Type) -> str:
            return t.name if isinstance(t, PrimTy) else type(t).__name__

        match instr.op:
            case "const":
                need(dest == constant_type(instr.const))
            case "load":
                need(dest == self.function.locals.get(instr.name))
            case "store":
                need(args == [self.function.locals.get(instr.name)])
            case "global_get":
                need(dest == self.module.globals.get(instr.name))
            case "global_set":
                need(args == [self.module.globals.get(instr.name)])
            case "add":
                need(
                    len(args) == 2 and args[0] == args[1] == dest and kind(args[0]) in ("i64", "f64", "str", "ListTy")
                )
            case "sub" | "mul" | "pow":
                need(len(args) == 2 and args[0] == args[1] == dest and dest in (I64, F64))
            case "div":
                need(len(args) == 2 and args[0] == args[1] and args[0] in (I64, F64) and dest == F64)
            case "floordiv" | "mod" | "bitand" | "bitor" | "bitxor" | "shl" | "shr":
                need(args == [I64, I64] and dest == I64)
            case "neg":
                need(args == [dest] and dest in (I64, F64))
            case "bitnot":
                need(args == [I64] and dest == I64)
            case "not":
                need(args == [BOOL] and dest == BOOL)
            case "eq" | "ne":
                # Types as values may only be known once code is built: Type[?] can equal Type[str].
                types = all(isinstance(a, BuiltinTy) and a.name == "Type" for a in args)
                need(len(args) == 2 and (args[0] == args[1] or types) and dest == BOOL)
            case "lt" | "le" | "gt" | "ge":
                need(len(args) == 2 and args[0] == args[1] and args[0] in (I64, F64, STR) and dest == BOOL)
            case "contains":
                need(len(args) == 2 and dest == BOOL and args[0] == item_type(args[1]))
            case "range" | "range_incl":
                need(args == [I64, I64] and dest == RANGE)
            case "tuple":
                need(dest == TupleTy(tuple(args)))
            case "tuple_get":
                need(len(args) == 1 and isinstance(args[0], TupleTy) and dest == args[0].items[instr.index])
            case "list":
                need(isinstance(dest, ListTy) and all(a == dest.element for a in args))
            case "index":
                need(len(args) == 2 and args[1] == I64 and dest == item_type(args[0]))
            case "slice":
                need(
                    len(args) == 2
                    and args[1] == RANGE
                    and dest == args[0]
                    and (dest == STR or isinstance(dest, ListTy))
                )
            case "builtin":
                library: Type | None = LIBRARY.get(instr.name)
                if library is not None and instr.name in GENERIC_LIBRARY and args and args[0] in (I64, F64):
                    library = substitute(library, {"T": args[0]})
                need(library is not None and dest is not None and FuncTy(tuple(args), dest) == library)
            case "set_index":
                need(len(args) == 3 and isinstance(args[0], ListTy) and args[1:] == [I64, args[0].element])
            case "append" | "copy" | "move":
                need(len(args) == (2 if instr.op == "append" else 1) and dest == args[0])
                need(instr.op != "append" or (isinstance(dest, ListTy) and args[1] == dest))
            case "set_field":
                need(len(args) == 2 and isinstance(args[0], EnumTy) and args[0].name == instr.name)
                if isinstance(args[0], EnumTy):
                    fields = self.field_types(args[0], instr.name, block)
                    need(instr.index < len(fields) and args[1] == fields[instr.index])
            case "pool_new":
                need(not args and isinstance(dest, BuiltinTy) and dest.name == "Pool")
            case "arena_new":
                need(not args and dest == ARENA)
            case "alloc" | "free" | "pool_get" | "pool_set" if args and args[0] == ARENA:
                item = args[1] if instr.op == "alloc" else dest if instr.op == "pool_get" else args[-1]
                ref = BuiltinTy("Ref", (item,)) if item is not None else None
                expected = {
                    "alloc": ([item], ref),
                    "free": ([], None),
                    "pool_get": ([ref], item),
                    "pool_set": ([ref, item], None),
                }[instr.op]
                need(args[1:] == expected[0] and dest == expected[1])
            case "alloc" | "free" | "pool_get" | "pool_set":
                need(len(args) >= 2 and isinstance(args[0], BuiltinTy) and args[0].name == "Pool")
                if isinstance(args[0], BuiltinTy):
                    item, handle = args[0].args[0], BuiltinTy("Handle", args[0].args)
                    expected = {
                        "alloc": ([item], handle),
                        "free": ([handle], None),
                        "pool_get": ([handle], item),
                        "pool_set": ([handle, item], None),
                    }[instr.op]
                    need(args[1:] == expected[0] and dest == expected[1])
            case "quote_begin":
                need(not args and dest == UNIT and instr.index in self.module.templates)
            case "quote":
                need(isinstance(dest, BuiltinTy) and dest.name == "Code" and instr.index in self.module.templates)
                need(bool(args) and args[0] == UNIT)
            case "run":
                need(dest is not None and args == [BuiltinTy("Code", (dest,))])
            case "type_value":
                need(dest == BuiltinTy("Type", (self.module.types[instr.index][1],)))
            case "reflect":
                first = STR if instr.name == "new_struct" else BuiltinTy("Type", (NEVER,))
                need(bool(args) and (args[0] == first or (isinstance(args[0], BuiltinTy) and args[0].name == "Type")))
            case "reset":
                # A generator function frees all its levels when its body ends.
                lowest = 0 if self.function.generator else 1
                need(not args and lowest <= instr.index < self.function.levels)
            case "swap":
                need(not args and 1 <= instr.index and instr.index + 1 < self.function.levels)
            case "carry":
                need(len(args) == 1 and dest == args[0] and instr.region == str(instr.index))
            case "len":
                pool = len(args) == 1 and isinstance(args[0], BuiltinTy) and args[0].name in ("Pool", "Arena")
                need(len(args) == 1 and (item_type(args[0]) is not None or pool) and dest == I64)
            case "to_text":
                need(len(args) == 1 and dest == STR)
            case "print" | "eprint":
                need(len(args) == 1)
            case "variant":
                need(isinstance(dest, EnumTy) and args == list(self.field_types(dest, instr.name, block)))
            case "is_variant":
                need(len(args) == 1 and isinstance(args[0], EnumTy) and dest == BOOL)
                if isinstance(args[0], EnumTy):
                    self.field_types(args[0], instr.name, block)
            case "variant_field":
                need(len(args) == 1 and isinstance(args[0], EnumTy))
                if isinstance(args[0], EnumTy):
                    fields = self.field_types(args[0], instr.name, block)
                    need(instr.index < len(fields) and dest == fields[instr.index])
            case "closure":
                function = self.module.functions.get(instr.name)
                self.require(function is not None, f"closure of unknown function {instr.name}", block)
                assert function is not None
                need(len(args) == function.captures)
                captured = [p.type for p in function.params[: len(args)]]
                rest = tuple(p.type for p in function.params[len(args) :])
                need(captured == args and dest == FuncTy(rest, function.result, function.mut_params))
            case "call":
                need(len(args) >= 1 and isinstance(args[0], FuncTy))
                if isinstance(args[0], FuncTy):
                    need(tuple(args[1:]) == args[0].params and dest == args[0].result)
                    mut_types = [args[0].params[i] for i in args[0].mutable]
                    need([out.type for out in instr.outs] == mut_types)
                    need(len(instr.out_regions) == len(mut_types))
                    for region in instr.out_regions:
                        self.require(self.valid_region(region, "call"), f"unknown region @{region}", block)
            case "gen_new":
                function = self.module.functions.get(instr.name)
                self.require(function is not None, f"gen_new of unknown function {instr.name}", block)
                assert function is not None
                self.require(function.generator, f"gen_new of {instr.name}, which isn't a generator", block)
                need(args == [p.type for p in function.params])
                need(isinstance(function.result, EnumTy) and dest == BuiltinTy("Gen", function.result.args))
            case "resume":
                need(len(args) == 1 and isinstance(args[0], BuiltinTy) and args[0].name == "Gen")
                need(dest == EnumTy("Optional", args[0].args) if isinstance(args[0], BuiltinTy) else False)
            case "assert":
                need(args == [BOOL])
            case _:
                raise self.fail(f"unknown operation {instr.op}", block)

    def valid_region(self, region: str, op: str = "") -> bool:
        if region == "slot":  # the arena of the object being changed (or passed as mut), a pool's or arena's value
            return op in CHANGING or op == "call"
        level = region_level(region)
        if level is not None:
            return level < self.function.levels
        if region.startswith("mut"):
            return region[3:].isdigit() and int(region[3:]) in self.function.mut_params
        return region in ("result", "global")

    def check_region(self, instr: Instr, block: Block) -> None:
        """Allocating instructions with a boxed result, and those that change an object, name a valid region."""
        if instr.op in CHANGING or allocates(instr):
            valid = self.valid_region(instr.region, instr.op)
            self.require(valid, f"{instr.op} needs a valid region, not @{instr.region}", block)
        else:
            self.require(not instr.region, f"{instr.op} can't have a region", block)

    def field_types(self, enum_type: EnumTy, variant: str, block: Block) -> tuple[Type, ...]:
        enum = self.module.enums.get(enum_type.name)
        if enum is None or variant not in enum.variants:
            raise self.fail(f"{show_type(enum_type)} has no variant {variant}", block)
        params = dict(zip(enum.params, enum_type.args))
        return tuple(substitute(f, params) for f in enum.variants[variant])


def constant_type(const: Constant) -> Type:
    match const:
        case bool():  # before int, since bool is a subclass of int
            return BOOL
        case int():
            return I64
        case float():
            return F64
        case str():
            return STR
        case ():
            return UNIT


def item_type(t: Type) -> Type | None:
    """The type of the items of a list, string or range (as `index` and `for` see them)."""
    match t:
        case ListTy(element):
            return element
        case PrimTy("str"):
            return STR
        case PrimTy("range"):
            return I64
        case _:
            return None
