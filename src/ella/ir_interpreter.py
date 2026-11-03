"""IR interpreter: runs a module of IR (see ella.ir) directly.

This gives the IR its meaning, and lets the tests check that lowering keeps
the program's behavior: every program in test/programs must print the same
thing (and fail with the same error) here as with the AST interpreter.

Running a function is a simple loop: execute the current block's
instructions in order, then follow its terminator to the next block, until
a `return`. Each instruction's result is kept in a dictionary from value id
to run-time value. Because IR values never change, there is nothing else to
keep track of besides the function's local slots.

The operators are the same functions the AST interpreter uses (ella.operators),
so results and error messages match exactly.

Arenas
------

The interpreter also simulates the arenas that native code allocates in
(docs/memory-model.md §5), to check the compiler's decisions about where
objects live. Arenas are numbered by *depth*: 0 is the global arena, and
each call takes the next few depths for its scope levels, so deeper arenas
are always freed first. Freeing an arena (`reset`, or returning) bumps its
*generation*.

Every boxed value carries a tag: the depth and generation of the
shortest-lived arena it may have objects in. (A list made in arena 1 with
strings from arena 3 is tagged with arena 3.) The interpreter checks that

* no value is used after its arena has been freed: its tag's generation
  must still be current, and
* no value is stored where it would outlive its arena: a variable, a list
  being changed, a returned value or a global must not live longer than
  the value's tag says it can.

A failed check is a bug in the compiler, reported as a run-time error that
starts with "memory check failed".

A pool's values each live in an arena of their own, a *slot arena*, which
is freed when the value is (`free`) or replaced. Slot arenas are numbered
from SLOT_DEPTH up, and aren't freed in any particular order, so they are
only checked for use after they are freed. Operations that put a value in
a pool copy it into the slot's arena.

When an arena is freed, the finalizers of the values in it run first,
newest first (docs/memory-model.md §7). The interpreter also adds up roughly how
many bytes native code would use (`--profile` shows them): the high-water
mark of each scope level, and what each allocating instruction made.
"""

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ella.checker import EnumTy, ListTy, check_code
from ella.ir import (
    Block,
    Branch,
    Function,
    Instr,
    Jump,
    Module,
    Return,
    Trap,
    allocates,
    is_boxed,
    region_level,
    verify,
)
from ella.ir import Value as IRValue
from ella.lexer import EllaError, Span
from ella.library import Exit, Host
from ella.library import call as call_library
from ella.lower import BINARY_OPS, COMPARE_OPS, Lowerer, compile_program
from ella.operators import binary, compare, contains, index_into, slice_of, unary
from ella.staging import AnySplice, CodeBuilder, reflect, reify, splice_value, start_code, type_from
from ella.syntax import Expr, Name
from ella.values import (
    MAX_CALL_DEPTH,
    UNIT,
    CodeValue,
    FunctionRef,
    HandleValue,
    PoolValue,
    TypeValue,
    Value,
    VariantValue,
    copy_value,
    run_deep,
    to_text,
)

# The other direction of ella.lower's tables: IR operation -> Ella operator.
OPERATORS = {op: symbol for symbol, op in (BINARY_OPS | COMPARE_OPS).items()}
UNARY = {"neg": "-", "not": "not", "bitnot": "~"}

PYTHON_FRAMES_PER_CALL = 10

# Where a value's objects may be: (arena depth, generation), or None for
# values that aren't boxed or are static constants.
type Tag = tuple[int, int] | None

SLOT_DEPTH = 1 << 20  # slot arenas are numbered from here
POOL = "<pool>"  # the finalizer of a pool: free its values


def run_ir(source: str, out: TextIO | None = None, host: Host | None = None, path: Path | None = None) -> int:
    """Compile a program to IR and run it, returning its exit status. Raises EllaError."""
    return IRInterpreter(compile_program(source, out, path), out, host).run()


@dataclass(slots=True)
class Arena:
    generation: int = 0
    used: int = 0  # estimated bytes in use
    owner: tuple[str, int] = ("global", 0)  # the function and scope level using it
    # The values in it with finalizers, oldest first, with the finalizer's IR function.
    finals: list[tuple[Value, str]] = field(default_factory=list)


@dataclass(slots=True)
class Frame:
    """One function call's state: computed values, local slots, and where its regions are."""

    function: Function
    base: int  # the depth of its scope level 0
    result_depth: int
    mut_depths: dict[int, int]  # mut parameter position -> depth of its region
    values: dict[int, Value] = field(default_factory=dict)
    tags: dict[int, Tag] = field(default_factory=dict)
    locals: dict[str, Value] = field(default_factory=dict)
    local_tags: dict[str, Tag] = field(default_factory=dict)


@dataclass(slots=True)
class Site:
    """What one allocating instruction made, for the profile."""

    count: int = 0
    bytes: int = 0


class IRInterpreter:
    def __init__(
        self, module: Module, out: TextIO | None = None, host: Host | None = None, max_depth: int = MAX_CALL_DEPTH
    ) -> None:
        self.module = module
        self.out = out if out is not None else sys.stdout
        self.host = host if host is not None else Host()
        self.max_depth = max_depth
        self.globals: dict[str, Value] = {}  # a global is missing until it is first set
        self.global_tags: dict[str, Tag] = {}
        self.depth = 0  # how many calls are in progress
        self.arenas = [Arena()]  # by depth; 0 is the global arena
        self.next_depth = 1
        self.slot_arenas: dict[int, Arena] = {}
        self.free_slot_arenas: list[int] = []
        self.pool_arenas: dict[int, list[int]] = {}  # id of a pool -> the arena of each of its slots
        self.high_water: dict[tuple[str, int], int] = defaultdict(int)
        self.sites: dict[tuple[str, Span, str], Site] = defaultdict(Site)

    def run(self) -> int:
        """Run the program, returning its exit status."""
        return run_deep(self.run_program, self.max_depth, PYTHON_FRAMES_PER_CALL)

    def run_program(self) -> int:
        try:
            status, _, _ = self.call(self.module.functions[self.module.entry], [], [], 0, {}, Span(1, 1))
        except Exit as stop:  # `exit(status)`: nothing more runs, not even finalizers
            self.out.flush()
            return stop.status
        self.free(0, 1)  # the globals, at the end of the program
        assert isinstance(status, int)
        return status

    # Arenas

    def arena(self, depth: int) -> Arena:
        if depth >= SLOT_DEPTH:
            return self.slot_arenas[depth]
        while len(self.arenas) <= depth:
            self.arenas.append(Arena())
        return self.arenas[depth]

    def new_slot_arena(self) -> int:
        if self.free_slot_arenas:
            return self.free_slot_arenas.pop()
        depth = SLOT_DEPTH + len(self.slot_arenas)
        self.slot_arenas[depth] = Arena(owner=("pool slots", 0))
        return depth

    def depth_of(self, region: str, frame: Frame) -> int:
        level = region_level(region)
        if level is not None:
            return frame.base + level
        if region == "result":
            return frame.result_depth
        if region.startswith("mut"):
            return frame.mut_depths[int(region[3:])]
        return 0  # global

    def allocate(self, depth: int, size: int, instr: Instr, frame: Frame) -> Tag:
        arena = self.arena(depth)
        arena.used += size
        self.high_water[arena.owner] = max(self.high_water[arena.owner], arena.used)
        site = self.sites[(frame.function.name, instr.span, instr.op)]
        site.count += 1
        site.bytes += size
        return (depth, arena.generation)

    def free(self, first: int, end: int) -> None:
        """Free the arenas at depths end - 1, ..., first, after running their finalizers."""
        for depth in reversed(range(first, end)):
            self.free_arena(depth)

    def free_arena(self, depth: int) -> None:
        arena = self.arena(depth)
        while arena.finals:
            value, finalizer = arena.finals.pop()
            self.finalize(value, finalizer)
        arena.generation += 1
        arena.used = 0
        if depth >= SLOT_DEPTH:
            self.free_slot_arenas.append(depth)

    def finalize(self, value: Value, finalizer: str) -> None:
        if finalizer == POOL:
            assert isinstance(value, PoolValue)
            for slot in reversed(range(len(value.slots))):
                if value.slots[slot] is not None:
                    self.free_arena(self.pool_arenas[id(value)][slot])
                    value.slots[slot] = None
            return
        self.call(self.module.functions[finalizer], [value], [None], 0, {}, Span(0, 0))

    def move_finals(self, value: Value, depth: int) -> None:
        """Move the finalizers of the values `value` holds to the arena at `depth`, keeping their order."""
        moving = {id(v) for v in held_values(value)}
        moved: list[tuple[Value, str]] = []
        for arena in [*self.arenas, *self.slot_arenas.values()]:
            if any(id(v) in moving for v, _ in arena.finals):
                moved += [(v, f) for v, f in arena.finals if id(v) in moving]
                arena.finals = [(v, f) for v, f in arena.finals if id(v) not in moving]
        self.arena(depth).finals += moved

    def check_alive(self, tag: Tag, what: str, span: Span) -> None:
        if tag is not None and self.arena(tag[0]).generation != tag[1]:
            raise EllaError(f"memory check failed: {what} is used after its arena (depth {tag[0]}) was freed", span)

    def check_fits(self, tag: Tag, depth: int, what: str, span: Span) -> None:
        """A value tagged `tag` may be stored in a place in the arena at `depth`: it lives at least as long.

        Slot arenas don't come in order, so they are only checked when used.
        """
        if tag is not None and tag[0] > depth and tag[0] < SLOT_DEPTH and depth < SLOT_DEPTH:
            raise EllaError(
                f"memory check failed: {what} would outlive its arena (depth {tag[0]} stored at depth {depth})", span
            )

    # Running functions

    def call(
        self,
        function: Function,
        args: list[Value],
        arg_tags: list[Tag],
        result_depth: int,
        mut_depths: dict[int, int],
        span: Span,
    ) -> tuple[Value, Tag, list[tuple[Value, Tag]]]:
        """Run a function from its entry block until it returns.

        Returns its result and the final values of its `mut` parameters, each with its tag.
        """
        if self.depth >= self.max_depth:
            raise EllaError(f"stack overflow: more than {self.max_depth} nested calls", span)
        self.depth += 1
        base = self.next_depth
        self.next_depth += function.levels
        for level in range(function.levels):
            self.arena(base + level).owner = (function.name, level)
        frame = Frame(function, base, result_depth, mut_depths)
        block = function.blocks[0]
        self.enter(block, args, arg_tags, frame)
        try:
            while True:
                for instr in block.instrs:
                    self.execute(instr, frame)
                match block.terminator:
                    case Jump(target, jump_args):
                        values = [frame.values[a.id] for a in jump_args]
                        self.enter(target, values, [frame.tags.get(a.id) for a in jump_args], frame)
                        block = target
                    case Branch(cond, then, else_):
                        block = then if frame.values[cond.id] else else_
                    case Return(value, outs):
                        returned = self.leave(value, outs, frame, span)
                        self.free(base, base + function.levels)
                        return returned
                    case Trap(message, value, trap_span):
                        if value is not None:
                            self.check_alive(frame.tags.get(value.id), str(value), trap_span)
                            message = message.replace("{}", to_text(frame.values[value.id], nested=True))
                        raise EllaError(message, trap_span)
                    case None:
                        raise AssertionError(f"{function.name}: {block.name} has no terminator")
        finally:
            # After an error, the program stops: nothing is freed or finalized.
            self.next_depth = base
            self.depth -= 1

    def leave(
        self, value: IRValue, outs: list[IRValue], frame: Frame, span: Span
    ) -> tuple[Value, Tag, list[tuple[Value, Tag]]]:
        """Check what a function returns: it must outlive the function's own arenas."""
        result_tag = frame.tags.get(value.id)
        self.check_alive(result_tag, f"the result {value}", span)
        self.check_fits(result_tag, frame.result_depth, f"the result {value}", span)
        returned = []
        for position, out in zip(frame.function.mut_params, outs):
            tag = frame.tags.get(out.id)
            self.check_alive(tag, f"mut parameter {out}", span)
            self.check_fits(tag, frame.mut_depths[position], f"mut parameter {out}", span)
            returned.append((frame.values[out.id], tag))
        return frame.values[value.id], result_tag, returned

    def enter(self, block: Block, args: list[Value], tags: list[Tag], frame: Frame) -> None:
        """Give a block's parameters the values passed to it."""
        for param, arg, tag in zip(block.params, args, tags):
            frame.values[param.id] = arg
            frame.tags[param.id] = tag

    def execute(self, instr: Instr, frame: Frame) -> None:
        """Run one instruction, storing its result (if any) in the frame."""
        args = [frame.values[a.id] for a in instr.args]
        tags = [frame.tags.get(a.id) for a in instr.args]
        span = instr.span
        for arg, arg_tag in zip(instr.args, tags):
            self.check_alive(arg_tag, str(arg), span)
        result: Value = UNIT
        tag: Tag = None
        if instr.region == "slot":
            # The arena of the object being changed: a pool's value, in its slot's arena.
            assert tags[0] is not None
            depth = tags[0][0]
        else:
            depth = self.depth_of(instr.region, frame) if instr.region else 0
        match instr.op:
            case "const":
                result = instr.const
            case "load":
                result, tag = frame.locals[instr.name], frame.local_tags.get(instr.name)
            case "store":
                region = frame.function.local_regions.get(instr.name)
                if region is not None:
                    self.check_fits(tags[0], self.depth_of(region, frame), f"the value stored in {instr.name}", span)
                frame.locals[instr.name], frame.local_tags[instr.name] = args[0], tags[0]
            case "global_get":
                if instr.name not in self.globals:
                    raise EllaError(f"'{instr.name}' is used before its 'let' has run", span)
                result, tag = self.globals[instr.name], self.global_tags.get(instr.name)
            case "global_set":
                self.check_fits(tags[0], 0, f"the value of global {instr.name}", span)
                self.globals[instr.name], self.global_tags[instr.name] = args[0], tags[0]
            case "neg" | "not" | "bitnot":
                result = unary(UNARY[instr.op], args[0], span)
            case "eq" | "ne" | "lt" | "le" | "gt" | "ge":
                result = compare(OPERATORS[instr.op], args[0], args[1], span)
            case "contains":
                result = contains(args[1], args[0], span)
            case op if op in OPERATORS:  # arithmetic, bitwise and ranges
                result = binary(OPERATORS[op], args[0], args[1], span)
                t = instr.dest.type if instr.dest is not None else None
                if isinstance(t, ListTy) and is_boxed(t.element):
                    tag = shortest(tags[0], tags[1])  # a joined list holds the operands' items
            case "tuple":
                result = tuple(args)
                tag = shortest(*tags)
            case "tuple_get":
                container = args[0]
                assert isinstance(container, tuple)
                result, tag = container[instr.index], tags[0]
            case "list":
                result = args
                tag = shortest(*tags)
            case "index":
                container, i = args
                if isinstance(container, range):
                    assert isinstance(i, int)
                    result = container[i]
                else:
                    result = index_into(container, i, span)
                    if isinstance(container, list):
                        tag = tags[0]  # an item of a list is in the list; a string's is a new string
            case "slice":
                container, part = args
                assert isinstance(container, list | str) and isinstance(part, range)
                result = copy_value(slice_of(container, part, span))
                tag = self.allocate(depth, deep_size(result), instr, frame)
            case "builtin":
                result = call_library(instr.name, args, span, self.host)
                if allocates(instr):
                    tag = self.allocate(depth, deep_size(result), instr, frame)
            case "set_index":
                container, i, item = args
                index_into(container, i, span)  # check that the index is in range
                assert isinstance(container, list) and isinstance(i, int)
                container[i] = self.store_in(item, tags[0], tags[2], depth, instr, frame)
            case "set_field":
                struct, item = args
                assert isinstance(struct, VariantValue)
                struct.fields[instr.index] = self.store_in(item, tags[0], tags[1], depth, instr, frame)
            case "append":
                items, new_items = args
                assert isinstance(items, list) and isinstance(new_items, list)
                self.check_fits(tags[0], depth, f"the list {instr.args[0]}", span)
                copies = [copy_value(item) for item in new_items]
                size = 8 * len(copies) + sum(deep_size(item) for item in copies)
                items.extend(copies)  # the items may be the same list as new_items: copy them first
                result, tag = items, shortest(tags[0], self.allocate(depth, size, instr, frame))
            case "copy":
                result = copy_value(args[0])
                tag = self.allocate(depth, deep_size(result), instr, frame)
            case "move":
                # Something that can't be copied, moved to another arena (a returned local variable).
                result = copy_value(args[0])  # the same object: it isn't copied
                self.move_finals(result, depth)
                tag = self.allocate(depth, deep_size(result), instr, frame)
            case "pool_new":
                result = PoolValue()
                self.pool_arenas[id(result)] = []
            case "alloc" | "pool_get" | "pool_set" | "free":
                result, tag = self.execute_pool(instr, args, tags, frame)
            case "quote_begin":
                quote, _ = self.module.templates[instr.index]
                result = start_code(quote, self.module.tables)  # type: ignore[assignment]
            case "quote":
                result = self.build_code(instr, args)
            case "run":
                result, tag = self.run_code(args[0], depth, span)
            case "type_value":
                texpr, static = self.module.types[instr.index]
                if args:
                    spliced = iter(args)
                    result = TypeValue(type_from(texpr, self.module.enums, lambda node: next(spliced)))
                else:
                    result = TypeValue(static)
            case "reflect":
                result = reflect(instr.name, args, self.module.enums, span)
            case "reset":
                self.free(frame.base + instr.index, frame.base + frame.function.levels)
            case "len":
                container = args[0]
                if isinstance(container, PoolValue):
                    result = container.live()
                else:
                    assert isinstance(container, list | str | range)
                    result = len(container)
            case "to_text":
                result = to_text(args[0])
            case "print":
                self.out.write(to_text(args[0]) + "\n")
            case "eprint":
                self.out.flush()
                stderr = self.host.stderr if self.host.stderr is not None else sys.stderr
                stderr.write(to_text(args[0]) + "\n")
                stderr.flush()
            case "variant":
                assert instr.dest is not None and isinstance(instr.dest.type, EnumTy)
                enum = self.module.enums[instr.dest.type.name]
                result = VariantValue(enum.name, instr.name, list(args), nocopy=enum.nocopy)
                tag = shortest(*tags)
                if enum.finalize:
                    self.arena(depth).finals.append((result, enum.finalize))
            case "is_variant":
                value = args[0]
                result = isinstance(value, VariantValue) and value.variant == instr.name
            case "variant_field":
                value = args[0]
                assert isinstance(value, VariantValue)
                result, tag = value.fields[instr.index], tags[0]
            case "closure":
                result = FunctionRef(instr.name, tuple(args))
                tag = shortest(*tags)
            case "call":
                result, tag = self.execute_call(instr, args, tags, depth, frame)
            case "assert":
                if not args[0]:
                    raise EllaError(instr.name, span)
            case op:
                raise AssertionError(f"unknown IR operation {op}")
        if instr.dest is not None:
            if instr.op == "pool_new":
                self.arena(depth).finals.append((result, POOL))
            if allocates(instr) and instr.op not in ("append", "copy", "move", "call", "run", "slice", "builtin"):
                tag = shortest(tag, self.allocate(depth, shallow_size(result), instr, frame))
            frame.values[instr.dest.id] = result
            frame.tags[instr.dest.id] = tag if is_boxed(instr.dest.type) else None

    def store_in(
        self, item: Value, container_tag: Tag, item_tag: Tag, depth: int, instr: Instr, frame: Frame
    ) -> Value:
        """The value to store into an object in the arena at `depth`, checking it lives long enough.

        A pool's value lives in its slot's arena, so a value stored into it is
        copied there.
        """
        if depth >= SLOT_DEPTH:
            copied = copy_value(item)
            self.allocate(depth, deep_size(copied), instr, frame)
            return copied
        self.check_fits(container_tag, depth, f"the object {instr.args[0]}", instr.span)
        self.check_fits(item_tag, depth, f"the value {instr.args[-1]}", instr.span)
        return item

    def execute_pool(self, instr: Instr, args: list[Value], tags: list[Tag], frame: Frame) -> tuple[Value, Tag]:
        """`alloc`, `free`, `pool_get` and `pool_set`. Each value of a pool lives in its slot's own arena."""
        pool, key = args[0], args[1]
        assert isinstance(pool, PoolValue)
        arenas = self.pool_arenas[id(pool)]
        if instr.op == "alloc":
            depth = self.new_slot_arena()
            value = copy_value(key)  # a value that can't be copied is moved, finalizers and all
            self.move_finals(value, depth)
            self.allocate(depth, deep_size(value), instr, frame)
            if pool.free:
                slot = pool.free.pop()
                pool.slots[slot], arenas[slot] = value, depth
            else:
                slot = len(pool.slots)
                pool.slots.append(value)
                pool.generations.append(0)
                arenas.append(depth)
            return HandleValue(slot, pool.generations[slot]), None
        assert isinstance(key, HandleValue)
        if key.slot >= len(pool.slots) or pool.generations[key.slot] != key.generation:
            raise EllaError(f"handle {to_text(key)} is stale: its value was freed", instr.span)
        depth = arenas[key.slot]
        match instr.op:
            case "pool_get":
                held = pool.slots[key.slot]
                assert held is not None
                return held, (depth, self.arena(depth).generation)
            case "pool_set":
                # Copy the new value into a new arena before freeing the old one: it may share parts of the old value.
                new_depth = self.new_slot_arena()
                value = copy_value(args[2])
                self.allocate(new_depth, deep_size(value), instr, frame)
                self.free_arena(depth)
                pool.slots[key.slot], arenas[key.slot] = value, new_depth
            case _:  # free
                self.free_arena(depth)
                pool.slots[key.slot] = None
                pool.generations[key.slot] += 1
                pool.free.append(key.slot)
        return UNIT, None

    def build_code(self, instr: Instr, args: list[Value]) -> Value:
        """`quote`: build the code from the quote's syntax tree, with the splices and constants computed for it."""
        _, outer_count = self.module.templates[instr.index]
        builder = args[0]
        assert isinstance(builder, CodeBuilder)
        outer = [b.renamed for b in args[1 : 1 + outer_count] if isinstance(b, CodeBuilder)]
        values = iter(args[1 + outer_count :])

        def splice(node: AnySplice) -> object:
            return splice_value(node, next(values))

        def persist(node: Name) -> Expr:
            return reify(copy_value(next(values)), node.span)

        return CodeValue(builder.build(splice, persist, outer))

    def run_code(self, code: Value, depth: int, span: Span) -> tuple[Value, Tag]:
        """`run`: check the code and lower it into the module as a function of its own, then call it."""
        assert isinstance(code, CodeValue)
        checker = check_code(code.expr, self.module.checker)
        name = Lowerer(checker, self.module).lower_code(code.expr)
        verify(self.module)
        result, tag, _ = self.call(self.module.functions[name], [], [], depth, {}, span)
        return result, tag

    def execute_call(
        self, instr: Instr, args: list[Value], tags: list[Tag], depth: int, frame: Frame
    ) -> tuple[Value, Tag]:
        function, *call_args = args
        assert isinstance(function, FunctionRef)
        callee = self.module.functions[function.name]
        # Captured values are in the closure, so they live as long as it does.
        captures = callee.params[: len(function.captured)]
        arg_tags = [tags[0] if is_boxed(capture.type) else None for capture in captures] + tags[1:]
        mut_depths = {
            position: self.depth_of(region, frame) for position, region in zip(callee.mut_params, instr.out_regions)
        }
        result, tag, outs = self.call(
            callee, [*function.captured, *call_args], arg_tags, depth, mut_depths, instr.span
        )
        for out, (value, out_tag) in zip(instr.outs, outs):
            frame.values[out.id] = value
            frame.tags[out.id] = out_tag if is_boxed(out.type) else None
        return result, tag

    # The profile

    def format_profile(self) -> str:
        """The arena profile: high-water marks of each scope level, and the allocation sites."""
        lines = ["Arena profile (bytes as native code would use them, roughly)", "", "High water by scope:"]
        for (function, level), size in sorted(self.high_water.items(), key=lambda item: -item[1]):
            scope = "global" if function == "global" else f"{function} level {level}"
            lines.append(f"  {size:>12,}  {scope}")
        lines += ["", "Allocations by site:"]
        for (function, span, op), site in sorted(self.sites.items(), key=lambda item: -item[1].bytes):
            lines.append(f"  {site.bytes:>12,}  {site.count:>8,}x  {span} {op} in {function}")
        return "\n".join(lines) + "\n"


def shortest(*tags: Tag) -> Tag:
    """The tag of something holding values with all these tags: the deepest, which is freed first."""
    present = [tag for tag in tags if tag is not None]
    return max(present) if present else None


def round16(size: int) -> int:
    """Native code aligns every object to 16 bytes."""
    return (size + 15) // 16 * 16


def shallow_size(value: Value) -> int:
    """About how many bytes native code uses for one object (not counting the objects it refers to)."""
    match value:
        case str():
            return round16(8 + 4 * len(value))
        case list() | tuple():
            return round16(16 + 8 * len(value))
        case range():
            return 32
        case VariantValue(fields=fields):
            return round16(16 + 8 * len(fields))
        case FunctionRef(captured=captured):
            return round16(24 + 8 * len(captured))
        case _:
            return 0


def deep_size(value: Value) -> int:
    """About how many bytes a deep copy of a value uses."""
    match value:
        case list() | tuple():
            return shallow_size(value) + sum(deep_size(item) for item in value)
        case VariantValue(fields=fields):
            return shallow_size(value) + sum(deep_size(item) for item in fields)
        case FunctionRef(captured=captured):
            return shallow_size(value) + sum(deep_size(item) for item in captured)
        case _:
            return shallow_size(value)


def held_values(value: Value) -> list[Value]:
    """The objects a value holds, itself included."""
    match value:
        case VariantValue(fields=fields):
            return [value, *(v for item in fields for v in held_values(item))]
        case list() | tuple():
            return [v for item in value for v in held_values(item)]
        case PoolValue():
            return [value]
    return []
