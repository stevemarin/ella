"""Inlining: put a copy of a small function's body where it is called.

A call costs more than the few instructions of a small function: pushing
arguments, saving the frame, and taking arenas for its scope levels. And
with the body in the caller, the other passes can work across it: a
constant argument folds, a struct made only to be taken apart goes away.

A call is inlined when it calls a known function (`call_direct`, or `call`
of a `closure` made in the same function, whose captured values become
arguments) that

* is small: at most INLINE_LIMIT instructions, not counting constants,
* isn't recursive: it can't reach itself through calls, closures or
  generators, so inlining always ends, and
* isn't a generator's body (those are only resumed).

A call that passes a `mut` argument that is a pool's or arena's value (its
region is @slot) isn't inlined: the callee may make objects in that
argument's region, which only the callee can name.

The call's block is split in two. The first half ends with `enter` (the
inlined call still counts toward the call depth limit, and fails as the
call would) and jumps to the copy of the callee's entry block, passing the
arguments as its parameters. Each `return` of the copy becomes `reset`
(freeing the callee's levels, running finalizers as returning would),
`leave`, and a jump to the second half, whose parameters are the call's
results.

The callee's regions become the caller's: @result is the call's region,
@mutj the region of the argument passed to `mut` parameter j, and its scope
levels go above all of the caller's own levels (docs/memory-model.md §14),
so they are freed before any of the caller's, just as a callee's arenas
are. Two inlined calls in one function are never running at once, so they
share those levels.

Functions are inlined into their callers bottom up (`bottom_up`), callees
first, so a callee's own small calls are already in the copy. The copy
isn't inlined into again.
"""

from collections.abc import Iterator
from dataclasses import replace
from itertools import count

from ella.ir import (
    Block,
    Branch,
    Function,
    Instr,
    Jump,
    Module,
    Return,
    Terminator,
    Trap,
    Value,
    Yield,
    carry_level,
    next_value_id,
    region_level,
)

INLINE_LIMIT = 30


def inline_calls(function: Function, module: Module) -> bool:
    recursive = recursive_functions(module)
    base = function.levels  # where inlined calls' levels start
    closures = {
        instr.dest.id: instr
        for block in function.blocks
        for instr in block.instrs
        if instr.op == "closure" and instr.dest is not None
    }
    changed = False
    work = list(function.blocks)
    while work:
        block = work.pop(0)
        for k, instr in enumerate(block.instrs):
            callee, captured = called(instr, closures, module)
            if callee is None or not inlinable(callee, function, recursive) or "slot" in instr.out_regions:
                continue
            work.insert(0, inline(function, block, k, callee, captured, base))
            function.levels = max(function.levels, base + callee.levels)
            changed = True
            break
    return changed


def called(instr: Instr, closures: dict[int, Instr], module: Module) -> tuple[Function | None, list[Value]]:
    """The function a call calls, if it is known, and the captured values to pass it."""
    if instr.op == "call_direct":
        return module.functions[instr.name], []
    if instr.op == "call" and instr.args[0].id in closures:
        closure = closures[instr.args[0].id]
        return module.functions[closure.name], closure.args
    return None, []


def inlinable(callee: Function, caller: Function, recursive: set[str]) -> bool:
    return (
        callee is not caller and not callee.generator and callee.name not in recursive and size(callee) <= INLINE_LIMIT
    )


def size(function: Function) -> int:
    """How many instructions a function has, not counting constants."""
    return sum(1 for block in function.blocks for instr in block.instrs if instr.op != "const")


def inline(function: Function, block: Block, k: int, callee: Function, captured: list[Value], base: int) -> Block:
    """Inline the call `block.instrs[k]` to `callee`. Returns the block that carries on after it."""
    call = block.instrs[k]
    assert call.dest is not None
    args = [*captured, *(call.args if call.op == "call_direct" else call.args[1:])]
    copy = Copier(function, callee, call, base)
    copies = [copy.block(b) for b in callee.blocks]
    after = Block(
        copy.name(f"after.{callee.name}"),
        [call.dest, *call.outs],
        block.instrs[k + 1 :],
        block.terminator,
        block.level,
    )
    for b in copies:
        if isinstance(b.terminator, Return):
            value, outs = b.terminator.value, b.terminator.outs
            b.instrs += [Instr("reset", None, [], call.span, index=base), Instr("leave", None, [], call.span)]
            b.terminator = Jump(after, [value, *outs])
    block.instrs = [*block.instrs[:k], Instr("enter", None, [], call.span)]
    block.terminator = Jump(copies[0], args)
    at = function.blocks.index(block) + 1
    function.blocks[at:at] = [*copies, after]
    return after


class Copier:
    """Copies a callee's blocks into a caller: new values, block names and local slots, and the caller's regions."""

    def __init__(self, function: Function, callee: Function, call: Instr, base: int) -> None:
        self.function = function
        self.callee = callee
        self.base = base
        self.ids = count(next_value_id(function))
        self.values: dict[int, Value] = {}
        names = {b.name for b in function.blocks} | set(function.locals)
        self.number = next(n for n in count(1) if not any(name.endswith(f".i{n}") for name in names))
        self.blocks = {b.name: Block(self.name(b.name), level=b.level + base) for b in callee.blocks}
        self.regions = {"result": call.region or "global", "global": "global", "slot": "slot", "": ""}
        self.regions |= {f"mut{p}": r for p, r in zip(callee.mut_params, call.out_regions)}
        self.locals = {slot: f"{slot}.i{self.number}" for slot in callee.locals}
        for slot, t in callee.locals.items():
            function.locals[self.locals[slot]] = t
            if slot in callee.local_regions:
                function.local_regions[self.locals[slot]] = self.region(callee.local_regions[slot])

    def name(self, name: str) -> str:
        return f"{name}.i{self.number}"

    def region(self, region: str) -> str:
        level, carried = region_level(region), carry_level(region)
        if level is not None:
            return str(self.base + level)
        if carried is not None:
            return f"carry{self.base + carried}"
        return self.regions[region]

    def value(self, value: Value) -> Value:
        if value.id not in self.values:
            self.values[value.id] = Value(next(self.ids), value.type)
        return self.values[value.id]

    def block(self, block: Block) -> Block:
        new = self.blocks[block.name]
        new.params = [self.value(p) for p in block.params]
        new.instrs = [self.instr(instr) for instr in block.instrs]
        new.terminator = self.terminator(block.terminator)
        return new

    def instr(self, instr: Instr) -> Instr:
        levels = instr.op in ("reset", "swap", "carry")
        return replace(
            instr,
            dest=self.value(instr.dest) if instr.dest is not None else None,
            args=[self.value(arg) for arg in instr.args],
            name=self.locals[instr.name] if instr.op in ("load", "store") else instr.name,
            index=self.base + instr.index if levels else instr.index,
            region=self.region(instr.region),
            outs=[self.value(out) for out in instr.outs],
            out_regions=[self.region(r) for r in instr.out_regions],
        )

    def terminator(self, terminator: Terminator | None) -> Terminator:
        match terminator:
            case Jump(target, args):
                return Jump(self.blocks[target.name], [self.value(a) for a in args])
            case Branch(cond, then, else_):
                return Branch(self.value(cond), self.blocks[then.name], self.blocks[else_.name])
            case Return(value, outs):
                return Return(self.value(value), [self.value(out) for out in outs])
            case Trap(message, value, span):
                return Trap(message, self.value(value) if value is not None else None, span)
            case Yield() | None:
                raise AssertionError(f"can't inline {self.callee.name}: {terminator}")


# The call graph


def references(function: Function) -> Iterator[str]:
    """The functions a function may call: those it calls directly, makes closures of, or makes generators of."""
    for block in function.blocks:
        for instr in block.instrs:
            if instr.op in ("call_direct", "closure", "gen_new"):
                yield instr.name


def components(module: Module) -> list[list[str]]:
    """The call graph's strongly connected components, callees before callers (Tarjan's algorithm)."""
    graph = {name: sorted(set(references(f))) for name, f in module.functions.items()}
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(name: str) -> None:
        index[name] = low[name] = len(index)
        stack.append(name)
        on_stack.add(name)
        for callee in graph[name]:
            if callee not in graph:
                continue
            if callee not in index:
                visit(callee)
                low[name] = min(low[name], low[callee])
            elif callee in on_stack:
                low[name] = min(low[name], index[callee])
        if low[name] == index[name]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == name:
                    break
            result.append(component)

    for name in graph:
        if name not in index:
            visit(name)
    return result


def recursive_functions(module: Module) -> set[str]:
    """The functions that may call themselves, directly or through others."""
    recursive: set[str] = set()
    for component in components(module):
        [first, *_] = component
        if len(component) > 1 or first in references(module.functions[first]):
            recursive.update(component)
    return recursive


def bottom_up(module: Module) -> list[Function]:
    """The module's functions, callees before their callers (as far as recursion allows)."""
    return [module.functions[name] for component in components(module) for name in component]


def unused_functions(module: Module) -> list[str]:
    """The functions the program can no longer reach from its entry or a finalizer, now that calls are inlined."""
    kept = {module.entry} | {e.finalize for e in module.enums.values() if e.finalize}
    reachable: set[str] = set()
    work = [*kept]
    while work:
        name = work.pop()
        if name in reachable or name not in module.functions:
            continue
        reachable.add(name)
        work += references(module.functions[name])
    return [name for name in module.functions if name not in reachable]
