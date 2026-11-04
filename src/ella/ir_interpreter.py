"""IR interpreter: runs a module of IR (see ella.ir) directly."""

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ella.checker import (
    BuiltinTy,
    EnumTy,
    ListTy,
    PrimTy,
    TupleTy,
    Type,
    check_code,
    is_builtin_error,
    show_type,
    substitute,
)
from ella.ir import (
    VIEWS,
    Block,
    Branch,
    Function,
    Instr,
    Jump,
    Module,
    Return,
    Trap,
    Yield,
    allocates,
    carry_level,
    is_boxed,
    region_level,
    verify,
)
from ella.ir import Value as IRValue
from ella.lexer import EQ_SITE, FINALIZER_SITE, HASH_SITE, TEXT_SITE, CallSite, EllaError, Span
from ella.library import FNV_OFFSET, Exit, Host, fnv, hash_bits
from ella.library import call as call_library
from ella.lower import BINARY_OPS, COMPARE_OPS, Lowerer, compile_program
from ella.operators import array_offset, array_slice, binary, compare, contains, index_into, slice_of, unary
from ella.staging import AnySplice, CodeBuilder, reflect, reify, splice_value, start_code, type_from
from ella.syntax import Expr, Name, bare_name
from ella.values import (
    MAX_CALL_DEPTH,
    MAX_HANDLE_GENERATION,
    MAX_POOL_SLOTS,
    UNIT,
    ArenaValue,
    ArrayValue,
    CodeValue,
    FunctionRef,
    GenValue,
    HandleValue,
    PoolValue,
    RefValue,
    TypeValue,
    Value,
    VariantValue,
    copy_value,
    equal,
    main_error,
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
STR_VIEW = 16  # bytes of a string's view: its length and where its characters are
LIST_VIEW = 32  # a list's: its length, capacity (0) and where its items are, rounded up
POOL = "<pool>"  # the finalizer of a pool: free its values
ARENA = "<arena>"  # the finalizer of an arena: free its slot arena
GEN = "<gen>"  # the finalizer of a generator: free its levels


class View(list[Value]):
    """A slice of a list: a copy of items start, start + 1, ... of `base`, which natively it shares.

    Its base is None once it changes in place (natively it moves to a home of its own).
    """

    __slots__ = ("base", "start")

    def __init__(self, base: list[Value], start: int, stop: int) -> None:
        super().__init__(base[start:stop])
        self.base: list[Value] | None = base
        self.start = start

    def check(self, span: Span) -> None:
        """Check that the items this view shares are still the same."""
        if self.base is None:
            return
        stop = self.start + len(self)
        if len(self.base) < stop or any(a is not b for a, b in zip(self, self.base[self.start : stop])):
            raise EllaError("memory check failed: a view's list changed while the view was in use", span)


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
    # For each loop that carries values, by level: the depths of its iteration's arena and of the carried one.
    current: dict[int, int] = field(default_factory=dict)
    carried: dict[int, int] = field(default_factory=dict)
    values: dict[int, Value] = field(default_factory=dict)
    tags: dict[int, Tag] = field(default_factory=dict)
    locals: dict[str, Value] = field(default_factory=dict)
    local_tags: dict[str, Tag] = field(default_factory=dict)
    # A generator function's frame, kept in its Gen: the slot arena of each scope level.
    levels: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Generator:
    """A Gen's state (its GenValue's `state`): the body's frame, and the block to carry on at."""

    frame: Frame
    block: Block


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
        # The calls in progress, outermost first, for stack traces: None for one they leave out (of `main`).
        # Nothing takes them off after an error, which stops the program, so they are its trace then.
        self.calls: list[CallSite | None] = []
        self.arenas = [Arena()]  # by depth; 0 is the global arena
        self.next_depth = 1
        self.swaps: dict[str, set[int]] = {}  # function -> the levels of its loops that carry values
        self.keys: dict[int, int] = {}  # depth -> its place in the order arenas are freed in, if not its depth
        self.slot_arenas: dict[int, Arena] = {}
        self.free_slot_arenas: list[int] = []
        self.pool_arenas: dict[int, list[int]] = {}  # id of a pool -> the arena of each of its slots
        self.arena_slots: dict[int, int] = {}  # id of an Arena -> its slot arena
        self.high_water: dict[tuple[str, int], int] = defaultdict(int)
        self.sites: dict[tuple[str, Span, str], Site] = defaultdict(Site)
        self.unwinding = False
        self.next_pool_id = 0

    def run(self) -> int:
        """Run the program, returning its exit status."""
        return run_deep(self.run_program, self.max_depth, PYTHON_FRAMES_PER_CALL)

    def run_program(self) -> int:
        try:
            status, _, _ = self.call(self.module.functions[self.module.entry], [], [], 0, {}, Span(1, 1))
            self.free(0, 1)  # the globals, at the end of the program
        except EllaError as error:
            error.traced([call for call in self.calls if call is not None])
            self.unwinding = True
            try:
                for depth in reversed(range(len(self.arenas))):
                    self.free_arena(depth)
            finally:
                self.unwinding = False
            raise
        except Exit as stop:  # `exit(status)`: nothing more runs, not even finalizers
            self.out.flush()
            return stop.status
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
        if level is not None and frame.levels:
            # A level nothing allocates in may have been trimmed away; its variables are checked as the deepest's.
            return frame.current.get(level, frame.levels[min(level, len(frame.levels) - 1)])
        if level is not None:
            return frame.current.get(level, frame.base + level)
        carried = carry_level(region)
        if carried is not None:
            return frame.carried[carried]
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

    def free_levels(self, frame: Frame, first: int) -> None:
        """Free a frame's scope levels `first` and up, deepest first.

        A generator function's levels are slot arenas its Gen keeps, so they
        are only cleared.
        """
        if not frame.levels:
            self.free(frame.base + first, frame.base + frame.function.levels)
        else:
            for depth in reversed(frame.levels[first:]):
                self.clear_arena(depth)
        self.restore_swaps(frame, first)

    def restore_swaps(self, frame: Frame, first: int) -> None:
        """Put the two arenas of each loop at levels `first` and up that carries values back in order.

        `swap` exchanges them, so after an odd number of iterations the loop's
        level is at the next level's depth. Code after the loop may use that
        next level as a level of its own; once both are freed, they are
        interchangeable again.
        """
        depths = frame.levels or [frame.base + level for level in range(frame.function.levels)]
        for level in frame.current:
            if level >= first:
                frame.current[level], frame.carried[level] = depths[level], depths[level + 1]
                if not frame.levels:
                    self.keys.pop(depths[level], None)
                    self.keys.pop(depths[level + 1], None)

    def clear_arena(self, depth: int) -> None:
        """Free an arena's values, keeping the arena (a slot arena that something still owns)."""
        arena = self.finalize_all(depth)
        arena.generation += 1
        arena.used = 0

    def free_arena(self, depth: int) -> None:
        arena = self.finalize_all(depth)
        arena.generation += 1
        arena.used = 0
        if depth >= SLOT_DEPTH:
            self.free_slot_arenas.append(depth)

    def finalize_all(self, depth: int) -> Arena:
        """Run the finalizers of the values in an arena, newest first."""
        arena = self.arena(depth)
        while arena.finals:
            value, finalizer = arena.finals.pop()
            try:
                self.finalize(value, finalizer)
            except EllaError:
                if not self.unwinding:
                    raise
        return arena

    def swap(self, level: int, frame: Frame) -> None:
        """`swap`: end an iteration of a loop that carries values, which keeps its arena for the next one.

        The loops inside it are freed, the iteration's values are finalized
        (only the values it carries are kept, and those can't have
        finalizers), and the carried arena is freed and becomes the next
        iteration's.
        """
        self.free_levels(frame, level + 2)
        current, carried = frame.current[level], frame.carried[level]
        self.finalize_all(current)
        if frame.levels:
            self.clear_arena(carried)
        else:
            self.free_arena(carried)
        frame.current[level], frame.carried[level] = carried, current
        if not frame.levels:
            self.keys[current], self.keys[carried] = self.key(carried), self.key(current)

    def key(self, depth: int) -> int:
        return self.keys.get(depth, depth)

    def shortest(self, *tags: Tag) -> Tag:
        """The tag of something holding values with all these tags: the one freed first."""
        present = [tag for tag in tags if tag is not None]
        return max(present, key=lambda tag: self.key(tag[0])) if present else None

    def finalize(self, value: Value, finalizer: str) -> None:
        if finalizer == GEN:
            # Its levels' values are finalized, deepest level first, and its slot arenas given back.
            assert isinstance(value, GenValue) and isinstance(value.state, Generator)
            frame = value.state.frame
            self.free_levels(frame, 0)
            for depth in reversed(frame.levels):
                self.free_arena(depth)
            frame.levels = []
            return
        if finalizer == ARENA:
            assert isinstance(value, ArenaValue)
            self.free_arena(self.arena_slots.pop(id(value)))
            value.values = []
            return
        if finalizer == POOL:
            assert isinstance(value, PoolValue)
            for slot in reversed(range(len(value.slots))):
                if value.slots[slot] is not None:
                    self.free_arena(self.pool_arenas[id(value)][slot])
                    value.slots[slot] = None
            return
        self.call(
            self.module.functions[finalizer], [value], [None], 0, {}, FINALIZER_SITE, CallSite(FINALIZER_SITE, "")
        )

    def call_protocol(self, table: dict[Type, str], t: Type, args: list[Value], site: Span, span: Span) -> Value:
        """Call the `eq` or `hash` method of a type (Module.eqs or hashes), whose result is a number or bool."""
        function = self.module.functions[table[t]]
        result, _, _ = self.call(function, args, [None] * len(args), 0, {}, span, CallSite(site, ""))
        return result

    def equal(self, a: Value, b: Value, t: Type, span: Span) -> bool:
        """`a == b` for values of type t: through the `eq` methods of the types in it (Module.eqs)."""
        match t:
            case EnumTy() if t in self.module.eqs:
                result = self.call_protocol(self.module.eqs, t, [a, b], EQ_SITE, span)
                assert isinstance(result, bool)
                return result
            case ListTy(element):
                assert isinstance(a, list) and isinstance(b, list)
                return len(a) == len(b) and all(self.equal(x, y, element, span) for x, y in zip(a, b))
            case TupleTy(items):
                assert isinstance(a, tuple) and isinstance(b, tuple)
                return all(self.equal(x, y, item, span) for x, y, item in zip(a, b, items))
            case EnumTy(name, args) if isinstance(a, VariantValue) and isinstance(b, VariantValue) and not a.error:
                if a.variant != b.variant:
                    return False
                info = self.module.enums[name]
                mapping = dict(zip(info.params, args))
                types = [substitute(f, mapping) for f in info.variants[a.variant]]
                return all(self.equal(x, y, field, span) for x, y, field in zip(a.fields, b.fields, types))
        return equal(a, b)

    def hash(self, value: Value, t: Type, span: Span) -> int:
        """`hash(value)` for a value of type t, as unsigned bits: through the `hash` methods (Module.hashes)."""
        match t:
            case EnumTy() if t in self.module.hashes:
                result = self.call_protocol(self.module.hashes, t, [value], HASH_SITE, span)
                assert isinstance(result, int)
                return result % 2**64
            case ListTy(element):
                assert isinstance(value, list)
                return fnv(FNV_OFFSET, (self.hash(item, element, span) for item in value))
            case TupleTy(items) if items:
                assert isinstance(value, tuple)
                return fnv(FNV_OFFSET, (self.hash(item, part, span) for item, part in zip(value, items)))
            case EnumTy(name, args) if isinstance(value, VariantValue) and not value.error:
                info = self.module.enums[name]
                mapping = dict(zip(info.params, args))
                types = [substitute(f, mapping) for f in info.variants[value.variant]]
                h = fnv(FNV_OFFSET, bare_name(value.variant).partition("[")[0].encode())
                return fnv(h, (self.hash(item, field, span) for item, field in zip(value.fields, types)))
        return hash_bits(value)

    def text(self, value: Value, t: Type, span: Span, nested: bool = False) -> str:
        """A value's text form, as `print` writes it: through the `text` methods of the types in it (Module.texts).

        Their functions are specialized for type arguments, so this follows the value's type.
        """
        match t:
            case _ if not self.module.texts:
                return to_text(value, nested)
            case EnumTy() if t in self.module.texts:
                # The method's result goes in a level of its own, freed once it has been read.
                scratch = self.next_depth
                self.next_depth += 1
                try:
                    function = self.module.functions[self.module.texts[t]]
                    result, _, _ = self.call(function, [value], [None], scratch, {}, span, CallSite(TEXT_SITE, ""))
                finally:
                    self.free(scratch, scratch + 1)
                    self.next_depth = scratch
                assert isinstance(result, str)
                return result
            case ListTy(element):
                assert isinstance(value, list)
                return "[" + ", ".join(self.text(item, element, span, True) for item in value) + "]"
            case TupleTy(items) if items:
                assert isinstance(value, tuple)
                texts = [self.text(item, item_type, span, True) for item, item_type in zip(value, items)]
                return "(" + ", ".join(texts) + ("," if len(texts) == 1 else "") + ")"
            case EnumTy(name, args) if isinstance(value, VariantValue) and not value.error and value.fields:
                info = self.module.enums[name]
                mapping = dict(zip(info.params, args))
                types = [substitute(f, mapping) for f in info.variants[value.variant]]
                texts = [self.text(item, item_type, span, True) for item, item_type in zip(value.fields, types)]
                return f"{bare_name(value.variant)}(" + ", ".join(texts) + ")"
        return to_text(value, nested)

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
        if tag is not None and self.key(tag[0]) > self.key(depth) and tag[0] < SLOT_DEPTH and depth < SLOT_DEPTH:
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
        site: CallSite | None = None,
    ) -> tuple[Value, Tag, list[tuple[Value, Tag]]]:
        """Run a function from its entry block until it returns.

        Returns its result and the final values of its `mut` parameters, each
        with its tag. `site` is the call's, for stack traces (None if they leave it out).
        """
        if self.depth >= self.max_depth:
            raise EllaError(f"stack overflow: more than {self.max_depth} nested calls", span)
        self.depth += 1
        self.calls.append(site)
        base = self.next_depth
        self.next_depth += function.levels
        for level in range(function.levels):
            self.arena(base + level).owner = (function.name, level)
        frame = Frame(function, base, result_depth, mut_depths)
        self.start_swaps(frame, [base + level for level in range(function.levels)])
        block = function.blocks[0]
        self.enter(block, args, arg_tags, frame)
        try:
            returned, _ = self.run_frame(frame, block, span)
            self.free(base, base + function.levels)
            self.calls.pop()
            return returned
        except EllaError:
            if self.unwinding:
                self.free(base, base + function.levels)
            raise
        finally:
            self.next_depth = base
            self.depth -= 1
            for depth in range(base, base + function.levels):
                self.keys.pop(depth, None)

    def start_swaps(self, frame: Frame, depths: list[int]) -> None:
        """Give each loop of a frame that carries values its two arenas: its level's, and the next one."""
        function = frame.function
        if function.name not in self.swaps:
            self.swaps[function.name] = function_swaps(function)
        for level in self.swaps[function.name]:
            frame.current[level], frame.carried[level] = depths[level], depths[level + 1]
            self.arena(depths[level + 1]).owner = (function.name, level)

    def run_frame(
        self, frame: Frame, block: Block, span: Span
    ) -> tuple[tuple[Value, Tag, list[tuple[Value, Tag]]], Block | None]:
        """Run a function's blocks from `block` until it returns, or yields (giving the block to carry on at)."""
        function = frame.function
        while True:
            tail = tail_call(block, function)
            for instr in block.instrs:
                if instr is tail:
                    continue
                self.execute(instr, frame)
            if tail is not None:
                args = tail.args if tail.op == "call_direct" else tail.args[1:]
                values = [frame.values[arg.id] for arg in args]
                self.free_levels(frame, 0)
                if not frame.levels:
                    self.start_swaps(frame, [frame.base + level for level in range(function.levels)])
                for param, param_value in zip(function.params, values):
                    frame.values[param.id] = param_value
                    frame.tags[param.id] = None
                if self.calls and self.calls[-1] is not None:
                    self.calls[-1] = site(tail)
                block = function.blocks[0]
                continue
            match block.terminator:
                case Jump(target, jump_args):
                    values = [frame.values[a.id] for a in jump_args]
                    self.enter(target, values, [frame.tags.get(a.id) for a in jump_args], frame)
                    block = target
                case Branch(cond, then, else_):
                    block = then if frame.values[cond.id] else else_
                case Return(value, outs):
                    return self.leave(value, outs, frame, span), None
                case Yield(value, target):
                    return self.leave(value, [], frame, span), target
                case Trap(value=value, span=trap_span, error=True):
                    assert value is not None
                    raise main_error(frame.values[value.id], trap_span)
                case Trap(message, value, trap_span):
                    if value is not None:
                        self.check_alive(frame.tags.get(value.id), str(value), trap_span)
                        message = message.replace("{}", to_text(frame.values[value.id], nested=True))
                    raise EllaError(message, trap_span)
                case None:
                    raise AssertionError(f"{function.name}: {block.name} has no terminator")

    def new_generator(self, instr: Instr, args: list[Value]) -> Value:
        """`gen_new`: a Gen with a frame for the generator function, and a slot arena for each of its levels.

        The arguments are copied into its level 0 (or moved, with their finalizers, if they can't be copied).
        """
        function = self.module.functions[instr.name]
        levels = [self.new_slot_arena() for _ in range(function.levels)]
        for level, depth in enumerate(levels):
            self.arena(depth).owner = (function.name, level)
        frame = Frame(function, 0, 0, {}, levels=levels)
        self.start_swaps(frame, levels)
        values, tags = [], []
        for arg, param in zip(args, function.params):
            value = copy_value(arg)  # the same object if it can't be copied
            self.move_finals(value, levels[0])
            values.append(value)
            tags.append(self.allocate(levels[0], deep_size(value), instr, frame) if is_boxed(param.type) else None)
        block = function.blocks[0]
        self.enter(block, values, tags, frame)
        return GenValue(Generator(frame, block))

    def resume(self, generator: Value, depth: int, instr: Instr) -> tuple[Value, Tag]:
        """`resume g`: run a generator's body from where it stopped until it yields, giving its result there."""
        assert isinstance(generator, GenValue) and isinstance(generator.state, Generator)
        state = generator.state
        if self.depth >= self.max_depth:
            raise EllaError(f"stack overflow: more than {self.max_depth} nested calls", instr.span)
        self.depth += 1
        self.calls.append(site(instr))
        try:
            state.frame.result_depth = depth
            (value, tag, _), target = self.run_frame(state.frame, state.block, instr.span)
        finally:
            self.depth -= 1
        self.calls.pop()
        assert target is not None
        state.block = target
        return value, tag

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
        for arg, arg_tag, value in zip(instr.args, tags, args):
            self.check_alive(arg_tag, str(arg), span)
            # Views share list storage, so check the source is unchanged before using it.
            if isinstance(value, View):
                value.check(span)
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
            case "eq" | "ne" if self.module.eqs:  # through `eq` methods
                compare(OPERATORS[instr.op], args[0], args[1], span)  # for its errors
                result = self.equal(args[0], args[1], instr.args[0].type, span) == (instr.op == "eq")
            case "eq" | "ne" | "lt" | "le" | "gt" | "ge":
                result = compare(OPERATORS[instr.op], args[0], args[1], span)
            case "contains" if self.module.eqs and isinstance(args[1], list):
                result = any(self.equal(item, args[0], instr.args[0].type, span) for item in args[1])
            case "contains":
                result = contains(args[1], args[0], span)
            case op if op in OPERATORS:  # arithmetic, bitwise and ranges
                result = binary(OPERATORS[op], args[0], args[1], span)
                t = instr.dest.type if instr.dest is not None else None
                if isinstance(t, ListTy) and is_boxed(t.element):
                    tag = self.shortest(tags[0], tags[1])  # a joined list holds the operands' items
            case "tuple":
                result = tuple(args)
                tag = self.shortest(*tags)
            case "tuple_get":
                container = args[0]
                assert isinstance(container, tuple)
                result, tag = container[instr.index], tags[0]
            case "list":
                result = args
                tag = self.shortest(*tags)
            case "index":
                container, i = args
                if isinstance(container, range):
                    assert isinstance(i, int)
                    result = container[i]
                elif isinstance(container, ArrayValue):
                    assert isinstance(i, int) and 0 <= i < len(container.items)  # array_offset checked it
                    result = container.items[i]
                else:
                    result = index_into(container, i, span)
                    if isinstance(container, list):
                        tag = tags[0]  # an item of a list is in the list; a string's is a new string
            case "slice":
                # A view: a new header, sharing the items.
                container, part = args
                assert isinstance(container, list | str) and isinstance(part, range)
                result = slice_of(container, part, span)
                if isinstance(container, list):
                    result = View(container, part.start, part.stop)
                size = LIST_VIEW if isinstance(result, View) else STR_VIEW
                tag = self.shortest(tags[0], self.allocate(depth, size, instr, frame))
            case "builtin" if instr.name == "hash":  # through `hash` methods
                h = self.hash(args[0], instr.args[0].type, span)
                result = h - 2**64 if h >= 2**63 else h
            case "fused":
                # The steps one by one, as they were before fusion (native code does them in one loop).
                results: list[Value] = []
                for step in instr.steps:
                    left = args[step.left] if step.left >= 0 else results[-1 - step.left]
                    right = (
                        None
                        if step.right is None
                        else args[step.right]
                        if step.right >= 0
                        else results[-1 - step.right]
                    )
                    if step.op in ("total", "dot"):
                        results.append(
                            call_library(step.op, [left] if right is None else [left, right], step.span, self.host)
                        )
                    elif right is None:
                        results.append(unary("-", left, step.span))
                    else:
                        results.append(binary(OPERATORS[step.op], left, right, step.span))
                result = results[-1]
                kept = [value for step, value in zip(instr.steps, results) if step.kept]
                for out, region, value in zip(instr.outs, instr.out_regions, kept):  # the arrays code after it needs
                    frame.values[out.id] = value
                    frame.tags[out.id] = self.allocate(self.depth_of(region, frame), shallow_size(value), instr, frame)
            case "array_slice":
                container = args[0]
                assert isinstance(container, ArrayValue)
                result = array_slice(container, tuple(args[1:]) if len(args) > 2 else args[1], span)
            case "array_offset":
                container = args[0]
                assert isinstance(container, ArrayValue)
                result = array_offset(container, tuple(args[1:]) if len(args) > 2 else args[1], span)
            case "builtin":
                if instr.name == "array":  # it needs the item type, which an empty list can't show
                    assert instr.dest is not None and isinstance(instr.dest.type, BuiltinTy)
                    args = [*args, show_type(instr.dest.type.args[0])]
                try:
                    result = call_library(instr.name, args, self.place(span), self.host)
                except EllaError as error:
                    error.span = span  # Span(0, 0) is reported at the call, without this function's (`traced`)
                    raise
                if instr.name in VIEWS:  # views of the first argument's characters
                    size = shallow_size(result) + STR_VIEW * len(result) if isinstance(result, list) else STR_VIEW
                    tag = self.shortest(tags[0], self.allocate(depth, size, instr, frame))
                elif allocates(instr):
                    tag = self.allocate(depth, deep_size(result), instr, frame)
            case "set_index" if isinstance(args[0], ArrayValue):
                container, i, item = args
                assert isinstance(container, ArrayValue) and isinstance(i, int)
                assert 0 <= i < len(container.items)  # array_offset checked it
                self.check_fits(tags[0], depth, f"the object {instr.args[0]}", span)
                container.items[i] = item
            case "set_index":
                container, i, item = args
                index_into(container, i, span)  # check that the index is in range
                assert isinstance(container, list) and isinstance(i, int)
                container[i] = self.store_in(item, tags[0], tags[2], depth, instr, frame)
                if isinstance(container, View):
                    container.base = None
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
                if isinstance(items, View):
                    items.base = None
                result, tag = items, self.shortest(tags[0], self.allocate(depth, size, instr, frame))
            case "copy":
                result = copy_value(args[0])
                tag = self.allocate(depth, deep_size(result), instr, frame)
            case "move":
                # Something that can't be copied, moved to another arena (a returned local variable).
                result = copy_value(args[0])  # the same object: it isn't copied
                self.move_finals(result, depth)
                tag = self.allocate(depth, deep_size(result), instr, frame)
            case "pool_new":
                result = PoolValue(id=self.next_pool_id)
                self.next_pool_id += 1
                self.pool_arenas[id(result)] = []
            case "arena_new":
                result = ArenaValue()
                self.arena_slots[id(result)] = self.new_slot_arena()
            case "gen_new":
                result = self.new_generator(instr, args)
            case "resume":
                result, tag = self.resume(args[0], depth, instr)
            case "alloc" | "pool_get" | "pool_set" | "free" if isinstance(args[0], ArenaValue):
                result, tag = self.execute_arena(instr, args, frame)
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
                self.free_levels(frame, instr.index)
            case "swap":
                self.swap(instr.index, frame)
            case "carry":
                # Kept for the next iteration: copied out of the carried arena, which is about to be freed.
                if tags[0] is not None and tags[0][0] == frame.carried[instr.index]:
                    result = copy_value(args[0])
                    tag = self.allocate(depth, deep_size(result), instr, frame)
                else:
                    result, tag = args[0], tags[0]
            case "len":
                container = args[0]
                if isinstance(container, PoolValue):
                    result = container.live()
                elif isinstance(container, ArenaValue):
                    result = len(container.values)
                elif isinstance(container, ArrayValue):
                    result = len(container.items)
                else:
                    assert isinstance(container, list | str | range)
                    result = len(container)
            case "to_text":
                result = self.text(args[0], instr.args[0].type, instr.span)
            case "print":
                self.out.write(self.text(args[0], instr.args[0].type, instr.span) + "\n")
            case "eprint":
                self.out.flush()
                stderr = self.host.stderr if self.host.stderr is not None else sys.stderr
                stderr.write(self.text(args[0], instr.args[0].type, instr.span) + "\n")
                stderr.flush()
            case "variant":
                assert instr.dest is not None and isinstance(instr.dest.type, EnumTy)
                enum = self.module.enums[instr.dest.type.name]
                builtin = is_builtin_error(self.module.enums, enum.name)
                result = VariantValue(enum.name, instr.name, list(args), nocopy=enum.nocopy, error=builtin)
                tag = self.shortest(*tags)
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
                tag = self.shortest(*tags)
            case "call":
                function = args[0]
                assert isinstance(function, FunctionRef)
                # Captured values are in the closure, so they live as long as it does.
                captures = [tags[0]] * len(function.captured)
                result, tag = self.execute_call(
                    instr, function.name, [*function.captured, *args[1:]], captures + tags[1:], depth, frame
                )
            case "call_direct":
                result, tag = self.execute_call(instr, instr.name, args, tags, depth, frame)
            case "enter":
                if self.depth >= self.max_depth:
                    raise EllaError(f"stack overflow: more than {self.max_depth} nested calls", span)
                self.depth += 1
                self.calls.append(site(instr))
            case "leave":
                self.depth -= 1
                self.calls.pop()
            case "assert":
                if not args[0]:
                    raise EllaError(instr.name, span)
            case op:
                raise AssertionError(f"unknown IR operation {op}")
        if instr.dest is not None:
            if instr.op in ("pool_new", "arena_new", "gen_new"):
                self.arena(depth).finals.append((result, {"pool_new": POOL, "arena_new": ARENA}.get(instr.op, GEN)))
            if allocates(instr) and instr.op not in (
                "append", "copy", "move", "call", "call_direct", "run", "slice", "builtin", "carry", "resume"
            ):  # fmt: skip
                tag = self.shortest(tag, self.allocate(depth, shallow_size(result), instr, frame))
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
            if not pool.free and len(pool.slots) == MAX_POOL_SLOTS:
                raise EllaError("a pool holds too many values", instr.span)
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
            return HandleValue(slot, pool.generations[slot], pool.id), None
        assert isinstance(key, HandleValue)
        if key.pool != pool.id:
            raise EllaError("handle belongs to another pool", instr.span)
        if key.slot < 0 or key.slot >= len(pool.slots) or pool.generations[key.slot] != key.generation:
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
                if pool.generations[key.slot] < MAX_HANDLE_GENERATION - 1:
                    pool.generations[key.slot] += 1
                    pool.free.append(key.slot)
        return UNIT, None

    def execute_arena(self, instr: Instr, args: list[Value], frame: Frame) -> tuple[Value, Tag]:
        """`alloc`, `free`, `pool_get` and `pool_set` on an arena, whose values all live in its slot arena."""
        arena = args[0]
        assert isinstance(arena, ArenaValue)
        depth = self.arena_slots[id(arena)]
        match instr.op:
            case "alloc":
                value = copy_value(args[1])  # a value that can't be copied is moved, finalizers and all
                self.move_finals(value, depth)
                self.allocate(depth, deep_size(value), instr, frame)
                arena.values.append(value)
                return RefValue(arena.id, arena.generation, len(arena.values) - 1), None
            case "free":
                # All at once: the slot arena is freed, and kept for the arena's next values.
                self.finalize_all(depth)
                self.arena(depth).generation += 1
                self.arena(depth).used = 0
                arena.values = []
                arena.generation += 1
                return UNIT, None
        ref = args[1]
        assert isinstance(ref, RefValue)
        if (
            ref.arena != arena.id
            or ref.generation != arena.generation
            or ref.index < 0
            or ref.index >= len(arena.values)
        ):
            raise EllaError(f"ref {to_text(ref)} is stale: its arena was freed, or it is another arena's", instr.span)
        if instr.op == "pool_get":
            return arena.values[ref.index], (depth, self.arena(depth).generation)
        # pool_set: the new value is copied into the arena; the old one stays there until it is freed.
        value = copy_value(args[2])
        self.allocate(depth, deep_size(value), instr, frame)
        arena.values[ref.index] = value
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

    def place(self, span: Span) -> Span:
        """Where an instruction is, for an Error a library function makes there: Span(0, 0) is in a function made
        for a library function used as a value, which is where it is called (as in EllaError.traced)."""
        if span == Span(0, 0):
            return next((call.span for call in reversed(self.calls) if call is not None), span)
        return span

    def execute_call(
        self, instr: Instr, name: str, args: list[Value], tags: list[Tag], depth: int, frame: Frame
    ) -> tuple[Value, Tag]:
        """Call IR function `name` with all its parameters' values (captured ones first), and their tags."""
        callee = self.module.functions[name]
        arg_tags = [tag if is_boxed(param.type) else None for param, tag in zip(callee.params, tags)]
        mut_depths = {
            position: self.depth_of(region, frame) for position, region in zip(callee.mut_params, instr.out_regions)
        }
        result, tag, outs = self.call(callee, args, arg_tags, depth, mut_depths, instr.span, site(instr))
        for out, (value, out_tag) in zip(instr.outs, outs):
            frame.values[out.id] = value
            frame.tags[out.id] = out_tag if is_boxed(out.type) else None
        return result, tag

    # The profile

    def format_profile(self) -> str:
        """The arena profile: high-water marks of each scope level, and the allocation sites."""
        copied = [site for (_, _, op), site in self.sites.items() if op == "copy"]
        copy_bytes = sum(site.bytes for site in copied)
        copy_count = sum(site.count for site in copied)
        allocated_bytes = sum(site.bytes for site in self.sites.values())
        allocation_count = sum(site.count for site in self.sites.values())
        lines = [
            "Arena profile (bytes as native code would use them, roughly)",
            "",
            f"Total allocations: {allocation_count:,} ({allocated_bytes:,} bytes)",
            f"Explicit deep copies: {copy_count:,} ({copy_bytes:,} bytes)",
            "",
            "High water by scope:",
        ]
        for (function, level), size in sorted(self.high_water.items(), key=lambda item: -item[1]):
            scope = "global" if function == "global" else f"{function} level {level}"
            lines.append(f"  {size:>12,}  {scope}")
        lines += ["", "Allocations by site:"]
        for (function, span, op), site in sorted(self.sites.items(), key=lambda item: -item[1].bytes):
            lines.append(f"  {site.bytes:>12,}  {site.count:>8,}x  {span} {op} in {function}")
        return "\n".join(lines) + "\n"


def site(instr: Instr) -> CallSite | None:
    """Where a call is, for stack traces (None for the call of `main`)."""
    return CallSite(instr.span, instr.within) if instr.within is not None else None


def tail_call(block: Block, function: Function) -> Instr | None:
    """Find a scalar self-call whose result flows straight to the function return."""
    if (
        function.captures
        or function.mut_params
        or function.generator
        or any(not isinstance(param.type, PrimTy) or param.type.name == "str" for param in function.params)
    ):
        return None
    if not block.instrs:
        return None
    instr = block.instrs[-1]
    if instr.dest is None or instr.mutable:
        return None
    if instr.op == "call_direct":
        if instr.name != function.name:
            return None
    elif instr.op == "call":
        closure = next((item for item in reversed(block.instrs[:-1]) if item.dest is instr.args[0]), None)
        if closure is None or closure.op != "closure" or closure.name != function.name or closure.args:
            return None
    else:
        return None
    if len(instr.args if instr.op == "call_direct" else instr.args[1:]) != len(function.params):
        return None
    term = block.terminator
    if isinstance(term, Return):
        return instr if term.value is instr.dest else None
    if not isinstance(term, Jump):
        return None
    mapping = {param.id: arg for param, arg in zip(term.target.params, term.args)}
    current = term.target
    seen: set[str] = set()
    while current.name not in seen and not current.instrs:
        seen.add(current.name)
        term = current.terminator
        if isinstance(term, Return):
            return instr if mapping.get(term.value.id, term.value) is instr.dest else None
        if not isinstance(term, Jump):
            return None
        mapping = {param.id: mapping.get(arg.id, arg) for param, arg in zip(term.target.params, term.args)}
        current = term.target
    return None


def function_swaps(function: Function) -> set[int]:
    """The levels of a function's loops that carry values (that have a `swap`)."""
    return {instr.index for block in function.blocks for instr in block.instrs if instr.op == "swap"}


def round16(size: int) -> int:
    """Native code aligns every object to 16 bytes."""
    return (size + 15) // 16 * 16


def shallow_size(value: Value) -> int:
    """About how many bytes native code uses for one object (not counting the objects it refers to).

    A list's view is only its header. (A string doesn't say if it is a view: see STR_VIEW.)
    """
    match value:
        case str():
            return round16(16 + 4 * len(value))
        case View():
            return LIST_VIEW
        case list():
            return round16(24 + 8 * len(value))
        case tuple():
            return round16(16 + 8 * len(value))
        case range():
            return 32
        case ArrayValue(shape, items, item):
            # Its length, dimensions, where its items are and their kind, its sizes, then its items, at a 32-byte
            # boundary (an f32 takes 4 bytes).
            return round16(32 + 8 * len(shape) + 24 + (4 if item == "f32" else 8) * len(items))
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
        case PoolValue() | ArenaValue() | GenValue():
            return [value]
    return []
