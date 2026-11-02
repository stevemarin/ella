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
starts with "memory check failed". The interpreter also adds up roughly how
many bytes native code would use (`--profile` shows them): the high-water
mark of each scope level, and what each allocating instruction made.
"""

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TextIO

from ella.checker import EnumTy, ListTy
from ella.ir import Block, Branch, Function, Instr, Jump, Module, Return, Trap, allocates, is_boxed, region_level
from ella.ir import Value as IRValue
from ella.lexer import EllaError, Span
from ella.lower import BINARY_OPS, COMPARE_OPS, compile_program
from ella.operators import binary, compare, contains, index_into, unary
from ella.values import UNIT, FunctionRef, Value, VariantValue, copy_value, to_text

# The other direction of ella.lower's tables: IR operation -> Ella operator.
OPERATORS = {op: symbol for symbol, op in (BINARY_OPS | COMPARE_OPS).items()}
UNARY = {"neg": "-", "not": "not", "bitnot": "~"}

MAX_CALL_DEPTH = 1000  # the same limit as the AST interpreter
PYTHON_FRAMES_PER_CALL = 10

# Where a value's objects may be: (arena depth, generation), or None for
# values that aren't boxed or are static constants.
type Tag = tuple[int, int] | None


def run_ir(source: str, out: TextIO | None = None) -> None:
    """Compile a program to IR and run it. Raises EllaError."""
    IRInterpreter(compile_program(source), out).run()


@dataclass(slots=True)
class Arena:
    generation: int = 0
    used: int = 0  # estimated bytes in use
    owner: tuple[str, int] = ("global", 0)  # the function and scope level using it


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
    def __init__(self, module: Module, out: TextIO | None = None) -> None:
        self.module = module
        self.out = out if out is not None else sys.stdout
        self.globals: dict[str, Value] = {}  # a global is missing until it is first set
        self.global_tags: dict[str, Tag] = {}
        self.depth = 0  # how many calls are in progress
        self.arenas = [Arena()]  # by depth; 0 is the global arena
        self.next_depth = 1
        self.high_water: dict[tuple[str, int], int] = defaultdict(int)
        self.sites: dict[tuple[str, Span, str], Site] = defaultdict(Site)

    def run(self) -> None:
        limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(limit, MAX_CALL_DEPTH * PYTHON_FRAMES_PER_CALL))
        try:
            self.call(self.module.functions[self.module.entry], [], [], 0, {}, Span(1, 1))
        finally:
            sys.setrecursionlimit(limit)

    # Arenas

    def arena(self, depth: int) -> Arena:
        while len(self.arenas) <= depth:
            self.arenas.append(Arena())
        return self.arenas[depth]

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
        """Free the arenas at depths first, ..., end - 1."""
        for depth in range(first, end):
            arena = self.arena(depth)
            arena.generation += 1
            arena.used = 0

    def check_alive(self, tag: Tag, what: str, span: Span) -> None:
        if tag is not None and self.arena(tag[0]).generation != tag[1]:
            raise EllaError(f"memory check failed: {what} is used after its arena (depth {tag[0]}) was freed", span)

    def check_fits(self, tag: Tag, depth: int, what: str, span: Span) -> None:
        """A value tagged `tag` may be stored in a place in the arena at `depth`: it lives at least as long."""
        if tag is not None and tag[0] > depth:
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
        if self.depth >= MAX_CALL_DEPTH:
            raise EllaError(f"stack overflow: more than {MAX_CALL_DEPTH} nested calls", span)
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
                        return self.leave(value, outs, frame, span)
                    case Trap(message, value, trap_span):
                        if value is not None:
                            self.check_alive(frame.tags.get(value.id), str(value), trap_span)
                            message = message.replace("{}", to_text(frame.values[value.id], nested=True))
                        raise EllaError(message, trap_span)
                    case None:
                        raise AssertionError(f"{function.name}: {block.name} has no terminator")
        finally:
            self.free(base, base + function.levels)
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
            case "set_index":
                container, i, item = args
                index_into(container, i, span)  # check that the index is in range
                assert isinstance(container, list) and isinstance(i, int)
                self.check_fits(tags[0], depth, f"the list {instr.args[0]}", span)
                self.check_fits(tags[2], depth, f"the item {instr.args[2]}", span)
                container[i] = item
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
            case "reset":
                self.free(frame.base + instr.index, frame.base + frame.function.levels)
            case "len":
                container = args[0]
                assert isinstance(container, list | str | range)
                result = len(container)
            case "to_text":
                result = to_text(args[0])
            case "print":
                self.out.write(to_text(args[0]) + "\n")
            case "variant":
                assert instr.dest is not None and isinstance(instr.dest.type, EnumTy)
                result = VariantValue(instr.dest.type.name, instr.name, tuple(args))
                tag = shortest(*tags)
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
            if allocates(instr) and instr.op not in ("append", "copy", "call"):
                tag = shortest(tag, self.allocate(depth, shallow_size(result), instr, frame))
            frame.values[instr.dest.id] = result
            frame.tags[instr.dest.id] = tag if is_boxed(instr.dest.type) else None

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
            lines.append(f"  {site.bytes:>12,}  {site.count:>8,}x  {span.line}:{span.column} {op} in {function}")
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
