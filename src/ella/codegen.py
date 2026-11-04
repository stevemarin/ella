"""x64 code generation: turns IR into GNU assembly (Intel syntax) for Linux."""

import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from ella.checker import (
    ARENA,
    F32,
    F64,
    GENERIC_LIBRARY,
    I32,
    I64,
    STR,
    BuiltinTy,
    EnumTy,
    FuncTy,
    ListTy,
    PrimTy,
    TupleTy,
    Type,
    is_builtin_error,
    library_param,
    show_type,
    substitute,
)
from ella.ir import (
    Block,
    Branch,
    Function,
    FusedStep,
    Instr,
    Jump,
    Module,
    Return,
    Trap,
    Value,
    Yield,
    carry_level,
    defined_by,
    format_instr,
    is_array,
    is_boxed,
    item_type,
    region_level,
)
from ella.lexer import Span
from ella.regalloc import allocate, uses_levels
from ella.syntax import bare_name
from ella.values import F32 as F32Value
from ella.values import MAX_CALL_DEPTH

# Type descriptor kinds; these must match `enum Kind` in runtime.c.
KINDS = {"()": 0, "i64": 1, "f64": 2, "bool": 3, "str": 4, "range": 5, "never": 0, "f32": 17, "i32": 18}
LIST_KIND, TUPLE_KIND, FUNCTION_KIND, ENUM_KIND, POOL_KIND, HANDLE_KIND, CODE_KIND = 6, 7, 8, 9, 10, 11, 12
ARENA_KIND, REF_KIND, GEN_KIND, ARRAY_KIND = 13, 14, 15, 16

# A Gen (runtime.c): its code, where it carries on, its number of levels and frame size, then each level's depth.
GEN_RESUME, GEN_DEPTHS = 8, 32

# The registers for the first six arguments of a C call.
C_ARGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]

# Comparison results for i64 (and bool), by IR operation: the `setcc` instruction.
SETCC = {"eq": "sete", "ne": "setne", "lt": "setl", "le": "setle", "gt": "setg", "ge": "setge"}

# The operator of each step of a `fused` instruction, for the runtime.
FUSED_OPS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "neg": "n", "total": "t", "dot": "d"}

# The Ella operator of each arithmetic operation, for overflow messages.
SYMBOLS = {"add": "+", "sub": "-", "mul": "*", "floordiv": "//", "pow": "**", "shl": "<<"}


@dataclass(frozen=True, slots=True)
class Lanes:
    """How AVX2 code works on an array's items: four i64s or f64s, or eight f32s or i32s, to a ymm register."""

    kind: str  # "i64", "i32", "f64" or "f32"

    @property
    def size(self) -> int:
        return 4 if self.kind in ("f32", "i32") else 8

    @property
    def count(self) -> int:
        return 32 // self.size

    @property
    def floats(self) -> bool:
        return self.kind in ("f64", "f32")

    @property
    def q(self) -> str:
        """The suffix of packed integer instructions: `vpaddq`, `vpaddd`."""
        return "d" if self.kind == "i32" else "q"

    @property
    def scalar(self) -> str:
        """The scratch register for one integer item: rax, or eax for an i32."""
        return "eax" if self.kind == "i32" else "rax"

    @property
    def p(self) -> str:
        """The suffix of packed instructions: `vaddps`, `vaddpd`."""
        return "ps" if self.kind == "f32" else "pd"

    @property
    def s(self) -> str:
        """The suffix of instructions on one item: `vaddss`, `vaddsd`."""
        return "ss" if self.kind == "f32" else "sd"

    @property
    def ptr(self) -> str:
        return "dword ptr" if self.size == 4 else "qword ptr"

    @property
    def masks(self) -> str:
        """The runtime's masks for the last n % count items."""
        return "ella_tail_masks32" if self.size == 4 else "ella_tail_masks"

    @property
    def broadcast(self) -> str:
        return {"f32": "vbroadcastss", "i32": "vpbroadcastd"}.get(self.kind, "vpbroadcastq")

    @property
    def signs(self) -> str:
        """The instruction that gathers the items' sign bits into a register: vmovmskpd, or vmovmskps."""
        return "vmovmskps" if self.size == 4 else "vmovmskpd"

    @property
    def sign(self) -> int:
        """The sign bit of an item, as a word."""
        return 1 << 31 if self.kind == "f32" else -(1 << 63)


def lanes(t: Type) -> Lanes:
    """The lanes of an array type's items, or of a number type's (for `total` and `dot`)."""
    item = item_type(t) if is_array(t) else t
    assert isinstance(item, PrimTy)
    return Lanes(item.name)


class Label(str):
    """An address in the program, passed to the runtime with `lea`."""


class Region(str):
    """A region, passed to the runtime as the depth of its arena."""


class Register(str):
    """A register whose value is passed to the runtime."""


@dataclass(slots=True)
class Layout:
    """Where a function keeps its values: a register, or each one's distance below the frame's base (rbp, or r12
    in a generator function), or for declared parameters of other functions, the caller's slot above rbp.

    `levels_at` is the slot of the depth of its scope level 0 (0 if it has none of its own), and `current`
    and `carried` those of the depths of each loop that carries values, by level. `size` is in bytes.
    """

    registers: dict[int, Register] = field(default_factory=dict)  # value id -> its register, if it has one
    saved: dict[str, int] = field(default_factory=dict)  # callee-saved register -> the offset of its slot
    below: dict[int, int] = field(default_factory=dict)  # value id -> offset below the base
    above: dict[int, int] = field(default_factory=dict)  # value id -> offset above rbp
    locals: dict[str, int] = field(default_factory=dict)
    levels_at: int = 0
    current: dict[int, int] = field(default_factory=dict)
    carried: dict[int, int] = field(default_factory=dict)
    size: int = 0


def generate(module: Module, max_depth: int = MAX_CALL_DEPTH, registers: int | None = None) -> str:
    """The assembly text for a whole module, whose functions may call each other `max_depth` deep.

    `registers` limits how many registers of each kind (callee-saved and caller-saved) values may have, so that
    tests can make values spill.
    """
    return CodeGen(module, max_depth, registers).generate()


class CodeGen:
    def __init__(self, module: Module, max_depth: int = MAX_CALL_DEPTH, registers: int | None = None) -> None:
        self.module = module
        self.max_depth = max_depth
        self.registers = registers  # at most this many registers of each kind (ella.regalloc), for tests
        self.text: list[str] = []  # code
        self.rodata: list[str] = []  # constants: strings, type descriptors
        self.labels = 0  # a counter for new labels
        self.strings: dict[str, Label] = {}  # Ella string constants
        self.c_strings: dict[str, Label] = {}  # C strings: messages and names
        self.type_labels: dict[Type, Label] = {}
        self.static_closures: set[str] = set()
        self.sites: dict[tuple[Span, str], Label] = {}  # call sites, for stack traces
        self.function_labels = {name: f"ella_fn_{i}" for i, name in enumerate(module.functions)}
        self.layouts: dict[str, Layout] = {}
        self.files = [""]  # the files of spans, numbered for the runtime's errors (0: the entry file)
        self.uses_avx2 = False  # whether the program has AVX2 instructions (for arrays)

        # The function being generated: the stack slot of each value and local,
        # and of the depth of its scope level 0 if it allocates in its levels.
        self.function = Function("", I64, Span(1, 1))
        self.slots: dict[int, str] = {}  # value id -> its register or stack slot
        self.saved: dict[str, str] = {}  # callee-saved register -> where the prologue saved it
        self.local_slots: dict[str, str] = {}
        # For each loop that carries values, by level: the slots of the depths of its level and its carried values.
        self.current_slots: dict[int, str] = {}
        self.carried_slots: dict[int, str] = {}
        self.base_slot = ""

    def emit(self, line: str) -> None:
        self.text.append(f"    {line}")

    def label(self, hint: str) -> str:
        """A new label: `.Lloop16_7`. (The `_` keeps `loop1` and `loop16` labels apart.)"""
        self.labels += 1
        return f".L{hint}_{self.labels}"

    # The program

    def generate(self) -> str:
        lines = [
            "# Generated by ella. Assemble and link with src/ella/runtime.c.",
            "    .intel_syntax noprefix",
            "    .text",
            "",
            "# The program's entry point, called by main() in the runtime: run the top-level code",
            "# on the stack whose top is given, ella_main(stack), which gives the exit status.",
            "    .globl ella_main",
            "ella_main:",
            "    push rbp",
            "    mov rbp, rsp",
            "    mov rsp, rdi                # switch to the program's own stack",
            "    push 0                      # its result (none) goes in the global arena",
            "    push 0                      # the top-level code isn't a closure",
            f"    call {self.function_labels[self.module.entry]}",
            "    leave                       # back to the runtime's stack",
            "    ret",
            "",
            "# Call an Ella closure with one argument, from the runtime:",
            "# ella_call1(closure, argument, result depth, site), giving its result.",
            "# It runs finalizers and `text` methods, and counts as a call at the site given",
            "# (ella_finalizer_site or ella_text_site), as in the interpreters.",
            "    .globl ella_call1",
            "ella_call1:",
            "    push rbp",
            "    mov rbp, rsp",
            "    mov rax, qword ptr [rip + ella_depth]",
            "    lea r8, [rip + ella_trace]",
            "    mov qword ptr [r8 + 8*rax], rcx",
            "    inc qword ptr [rip + ella_depth]",
            "    push rsi                    # the argument",
            "    push rdx                    # where the result goes",
            "    push rdi                    # the closure",
            "    call qword ptr [rdi]",
            "    dec qword ptr [rip + ella_depth]",
            "    leave",
            "    ret",
            "",
            "# The same with two arguments, for `eq` methods: ella_call2(closure, a, b, result depth, site).",
            "    .globl ella_call2",
            "ella_call2:",
            "    push rbp",
            "    mov rbp, rsp",
            "    mov rax, qword ptr [rip + ella_depth]",
            "    lea r9, [rip + ella_trace]",
            "    mov qword ptr [r9 + 8*rax], r8",
            "    inc qword ptr [rip + ella_depth]",
            "    push rdx                    # the second argument",
            "    push rsi                    # the first",
            "    push rcx                    # where the result goes",
            "    push rdi                    # the closure",
            "    call qword ptr [rdi]",
            "    dec qword ptr [rip + ella_depth]",
            "    leave",
            "    ret",
        ]
        for name, function in self.module.functions.items():
            self.text = []
            self.function_body(function)
            lines += ["", f"# function {name}", f"{self.function_labels[name]}:", *self.text]
        # Constants go in .data.rel.ro rather than .rodata because type
        # descriptors hold addresses, which the loader fills in when the
        # program starts (it is a position-independent executable). They are
        # read-only after that.
        # The file names, for errors in modules other than the entry file.
        files = [f"    .quad {self.c_string(file)}" for file in self.files]
        lines += ["", "    .section .data.rel.ro", *self.rodata]
        lines += [
            "",
            "# The files of the program's modules.",
            "    .globl ella_files",
            "    .balign 8",
            "ella_files:",
            *files,
        ]
        # Different global names give different symbols (a name with dots is a module's).
        lines += [
            "",
            "# Whether the program has AVX2 instructions, which the runtime checks the CPU has.",
            "    .globl ella_uses_avx2",
            "    .balign 8",
            "ella_uses_avx2:",
            f"    .quad {int(self.uses_avx2)}",
        ]
        lines += [
            "",
            "# How deeply Ella functions may call each other, for the runtime's stack.",
            "    .globl ella_max_depth",
            "    .balign 8",
            "ella_max_depth:",
            f"    .quad {self.max_depth}",
        ]
        lines += ["", "# Global variables, and whether each has been set yet.", "    .bss", "    .balign 8"]
        for name in self.module.globals:
            lines += [f"ella_global.{name}:", "    .zero 8", f"ella_global_set.{name}:", "    .zero 8"]
        lines += [
            "",
            "# The site of each call in progress, by depth, for stack traces.",
            "    .globl ella_trace",
            "ella_trace:",
            f"    .zero {8 * (self.max_depth + 2)}",
        ]
        lines += ["", '    .section .note.GNU-stack, "", @progbits', ""]
        return "\n".join(lines)

    # Functions

    def function_body(self, function: Function) -> None:
        """The prologue, then each block."""
        self.function = function
        layout = self.layout(function)
        base = "r12" if function.generator else "rbp"
        self.slots = {v: f"qword ptr [{base} - {k}]" for v, k in layout.below.items()}
        self.slots |= {v: f"qword ptr [rbp + {k}]" for v, k in layout.above.items()}
        self.local_slots = {name: f"qword ptr [{base} - {k}]" for name, k in layout.locals.items()}
        self.base_slot = f"qword ptr [rbp - {layout.levels_at}]" if layout.levels_at else ""
        self.current_slots = {level: f"qword ptr [{base} - {k}]" for level, k in layout.current.items()}
        self.carried_slots = {level: f"qword ptr [{base} - {k}]" for level, k in layout.carried.items()}
        self.slots |= layout.registers
        self.saved = {register: f"qword ptr [rbp - {k}]" for register, k in layout.saved.items()}
        params = function.params
        if function.generator:
            self.generator_prologue(function, layout)
            return
        frame = layout.size

        if layout.registers:
            self.text.append("    # registers: " + ", ".join(f"%{v} {r}" for v, r in sorted(layout.registers.items())))
        self.emit("push rbp")
        self.emit("mov rbp, rsp")
        self.emit(f"sub rsp, {(frame + 15) // 16 * 16}")
        for register, slot in self.saved.items():
            self.emit(f"mov {slot}, {register}   # callee-saved: restored before returning")
        self.emit("and rsp, -16              # align the stack for calls into the runtime")
        if self.base_slot:
            self.emit(f"mov rdi, {function.levels}")
            self.emit("call ella_enter            # take arenas for the scope levels")
            self.emit(f"mov {self.base_slot}, rax")
            for level in self.current_slots:
                self.emit(f"lea rcx, [rax + {level}]")
                self.emit(f"mov {self.current_slots[level]}, rcx")
                self.emit(f"lea rcx, [rax + {level + 1}]")
                self.emit(f"mov {self.carried_slots[level]}, rcx")
        if function.captures:
            self.emit("mov rax, qword ptr [rbp + 16]   # the closure")
            for i in range(function.captures):
                self.emit(f"mov rcx, qword ptr [rax + {24 + 8 * i}]")
                self.emit(f"mov {self.slots[params[i].id]}, rcx")
        for i, param in enumerate(params[function.captures :]):
            if param.id in layout.registers:
                self.emit(f"mov {layout.registers[param.id]}, {self.param_slot(i)}")
        for block in function.blocks:
            self.text.append(f"{self.block_label(block)}:")
            tail = self.tail_call(block)
            for instr in block.instrs:
                if instr is tail:
                    continue
                self.instr(instr)
            self.terminator(block)

    def tail_call(self, block: Block) -> Instr | None:
        """A scalar self-call in return position can reuse this native frame.

        Scalar arguments can be moved into the reused frame before its scope
        arenas are reset. Calls with captures, mutable parameters, generators,
        or boxed parameters retain the ordinary call behavior.
        """
        term = block.terminator
        if self.function.captures or self.function.mut_params or self.function.generator:
            return None
        if any(not isinstance(param.type, PrimTy) or param.type.name == "str" for param in self.function.params):
            return None
        if not block.instrs:
            return None
        instr = block.instrs[-1]
        if instr.dest is None or instr.mutable:
            return None
        if isinstance(term, Return):
            returns_call = instr.dest is term.value
        elif isinstance(term, Jump):
            returns_call = self.forwards_return_to(block, instr.dest)
        else:
            returns_call = False
        if not returns_call:
            return None
        if instr.op == "call_direct":
            if instr.name != self.function.name:
                return None
            args = instr.args
        elif instr.op == "call":
            closure = next(
                (candidate for candidate in reversed(block.instrs[:-1]) if candidate.dest is instr.args[0]), None
            )
            if closure is None or closure.op != "closure" or closure.name != self.function.name or closure.args:
                return None
            args = instr.args[1:]
        else:
            return None
        if len(args) != len(self.function.params):
            return None
        return instr

    @staticmethod
    def forwards_return_to(block: Block, value: Value) -> bool:
        """Whether this block jumps through empty forwarding blocks to return `value`."""
        term = block.terminator
        if not isinstance(term, Jump):
            return False
        mapping = {param.id: arg for param, arg in zip(term.target.params, term.args)}
        current = term.target
        seen: set[str] = set()
        while current.name not in seen and not current.instrs:
            seen.add(current.name)
            term = current.terminator
            if isinstance(term, Return):
                returned = mapping.get(term.value.id, term.value)
                return returned is value
            if not isinstance(term, Jump):
                return False
            mapping = {param.id: mapping.get(arg.id, arg) for param, arg in zip(term.target.params, term.args)}
            current = term.target
        return False

    def emit_tail_call(self, instr: Instr) -> None:
        """Transfer scalar arguments into the incoming slots and jump to entry."""
        args = instr.args if instr.op == "call_direct" else instr.args[1:]
        self.parallel_move([(self.param_slot(i), self.slots[arg.id]) for i, arg in enumerate(args)])
        if self.base_slot:
            self.emit(f"mov rdi, {self.base_slot}")
            self.emit("call ella_leave            # end this logical invocation's arenas")
            self.emit(f"mov rdi, {self.function.levels}")
            self.emit("call ella_enter            # reuse the frame with fresh arenas")
            self.emit(f"mov {self.base_slot}, rax")
            for level in self.current_slots:
                self.emit(f"lea rcx, [rax + {level}]")
                self.emit(f"mov {self.current_slots[level]}, rcx")
                self.emit(f"lea rcx, [rax + {level + 1}]")
                self.emit(f"mov {self.carried_slots[level]}, rcx")
        for i, param in enumerate(self.function.params):
            if param.id in self.layout(self.function).registers:
                self.emit(f"mov {self.slots[param.id]}, {self.param_slot(i)}")
        self.emit("mov rax, qword ptr [rip + ella_depth]")
        self.emit("lea rcx, [rip + ella_trace]")
        self.emit(f"lea rdx, [rip + {self.site(instr.span, self.function.name)}]")
        self.emit("mov qword ptr [rcx + 8*rax - 8], rdx   # latest tail-call site")
        self.emit(f"jmp {self.block_label(self.function.blocks[0])}")

    def layout(self, function: Function) -> Layout:
        """Where a function's values are: a register (ella.regalloc), or else a stack slot (8 bytes); also a slot
        for each local, each callee-saved register it uses, and the depths of its levels.

        A generator function's are all in its frame, in the Gen, its
        parameters too; its levels' depths are in the Gen itself.
        """
        if function.name in self.layouts:
            return self.layouts[function.name]
        allocation = allocate(function, self.registers)
        layout = Layout(registers={v: Register(r) for v, r in allocation.registers.items()})

        def new_slot() -> int:
            layout.size += 8
            return layout.size

        for register in allocation.saved:
            layout.saved[register] = new_slot()
        for i, param in enumerate(function.params):
            if param.id in layout.registers:
                continue
            if i < function.captures or function.generator:
                layout.below[param.id] = new_slot()  # copied in from the closure, or by gen_new
            else:
                layout.above[param.id] = 32 + 8 * (i - function.captures)
        for block in function.blocks:
            for value in block.params if block is not function.blocks[0] else []:
                if value.id not in layout.registers:
                    layout.below[value.id] = new_slot()
            for instr in block.instrs:
                for value in defined_by(instr):
                    if value.id not in layout.registers:
                        layout.below[value.id] = new_slot()
        for name in function.locals:
            layout.locals[name] = new_slot()
        if uses_levels(function) and not function.generator:
            layout.levels_at = new_slot()
        for level in sorted({i.index for block in function.blocks for i in block.instrs if i.op == "swap"}):
            layout.current[level], layout.carried[level] = new_slot(), new_slot()
        self.layouts[function.name] = layout
        return layout

    def generator_prologue(self, function: Function, layout: Layout) -> None:
        """A generator function starts by finding its frame (r12), and carries on where it stopped."""
        self.emit("push rbp")
        self.emit("mov rbp, rsp")
        self.emit("push r12                  # restored before returning")
        self.emit("and rsp, -16              # align the stack for calls into the runtime")
        self.emit("mov rax, qword ptr [rbp + 16]   # the Gen")
        self.emit(f"lea r12, [rax + {GEN_DEPTHS + 8 * function.levels + layout.size}]   # the end of its frame")
        self.emit(f"jmp qword ptr [rax + {GEN_RESUME}]   # where it stopped")
        for block in function.blocks:
            self.text.append(f"{self.block_label(block)}:")
            for instr in block.instrs:
                self.instr(instr)
            self.terminator(block)

    def gen_new(self, instr: Instr) -> None:
        """A new Gen for a generator function, with its arguments (copied or moved into its level 0) in its frame."""
        body = self.module.functions[instr.name]
        layout = self.layout(body)
        frame_end = GEN_DEPTHS + 8 * body.levels + layout.size
        code, start = Label(self.function_labels[body.name]), Label(self.block_label(body.blocks[0], body))
        made = instr.dest
        assert made is not None
        self.call_c("ella_gen_new", code, start, body.levels, layout.size, Region(instr.region))
        self.store(made)
        for arg, param in zip(instr.args, body.params):
            if is_boxed(param.type):
                self.call_c("ella_gen_arg", made, arg, self.type_descriptor(param.type))
            else:
                self.load("rax", arg)
            self.load("rcx", made)
            self.emit(f"mov qword ptr [rcx + {frame_end - layout.below[param.id]}], rax")
        self.load("rcx", made)
        for level in layout.current:
            for slot, depth in ((layout.current[level], level), (layout.carried[level], level + 1)):
                self.emit(f"mov rax, qword ptr [rcx + {GEN_DEPTHS + 8 * depth}]")
                self.emit(f"mov qword ptr [rcx + {frame_end - slot}], rax")

    def resume(self, instr: Instr) -> None:
        """`resume g`: call the generator's body, as a closure with no arguments; the Gen is the closure."""
        self.start_call(instr)
        self.region_to("rax", instr.region)
        self.emit("push rax                  # where the result goes")
        self.emit(f"push {self.slots[instr.args[0].id]}")
        self.load("rax", instr.args[0])
        self.emit("call qword ptr [rax]       # the generator's code")
        self.emit("add rsp, 16")
        self.emit("dec qword ptr [rip + ella_depth]")
        self.store(instr.dest)

    def param_slot(self, i: int) -> str:
        """Where declared parameter i is: the caller pushed it."""
        return f"qword ptr [rbp + {32 + 8 * i}]"

    def region_to(self, register: str, region: str) -> None:
        """Put the depth of a region's arena in a register."""
        level, carried = region_level(region), carry_level(region)
        if level in self.current_slots:
            self.emit(f"mov {register}, {self.current_slots[level]}")
        elif carried is not None:
            self.emit(f"mov {register}, {self.carried_slots[carried]}")
        elif level is not None and self.function.generator:
            self.emit(f"mov {register}, qword ptr [rbp + 16]")
            self.emit(f"mov {register}, qword ptr [{register} + {GEN_DEPTHS + 8 * level}]   # its level's slot arena")
        elif level is not None:
            self.emit(f"mov {register}, {self.base_slot}")
            if level:
                self.emit(f"add {register}, {level}")
        elif region == "result":
            self.emit(f"mov {register}, qword ptr [rbp + 24]")
        elif region.startswith("mut"):
            declared = len(self.function.params) - self.function.captures
            j = self.function.mut_params.index(int(region[3:]))
            self.emit(f"mov {register}, qword ptr [rbp + {32 + 8 * (declared + j)}]")
        else:
            self.emit(f"mov {register}, 0")

    def block_label(self, block: Block, function: Function | None = None) -> str:
        return f".L{self.function_labels[(function or self.function).name]}_{block.name}"

    # Moving values

    def load(self, register: str, value: Value) -> None:
        if self.slots[value.id] != register:
            self.emit(f"mov {register}, {self.slots[value.id]}")

    def store(self, value: Value | None, register: str = "rax") -> None:
        """Put a register's value in a value's register or slot. (`movsd` or `movq` for an `xmm` register.)"""
        assert value is not None
        place = self.slots[value.id]
        if register.startswith("xmm"):
            self.emit(f"{'movq' if self.in_register(value) else 'movsd'} {place}, {register}")
        elif place != register:
            self.emit(f"mov {place}, {register}")

    def in_register(self, value: Value) -> bool:
        return isinstance(self.slots[value.id], Register)

    def load_xmm(self, register: str, value: Value) -> None:
        """Put an f64 value in an xmm register."""
        self.emit(f"{'movq' if self.in_register(value) else 'movsd'} {register}, {self.slots[value.id]}")

    def xmm_operand(self, value: Value, spare: str = "xmm1") -> str:
        """An f64 value as the second operand of an SSE instruction: its slot, or `spare` holding it."""
        if not self.in_register(value):
            return self.slots[value.id]
        self.load_xmm(spare, value)
        return spare

    def call_c(self, function: str, *args: Value | int | Label | Region | Register) -> None:
        """Call a runtime function with the C calling convention. The result is in rax.

        Values may be in the very registers that arguments go in, so they are
        moved all at once (`parallel_move`), before the arguments that
        aren't values.
        """
        moves = []
        for register, arg in zip(C_ARGS, args):
            match arg:
                case Value():
                    moves.append((register, self.slots[arg.id]))
                case Register():
                    moves.append((register, str(arg)))
        self.parallel_move(moves)
        for register, arg in zip(C_ARGS, args):
            match arg:
                case Label():
                    self.emit(f"lea {register}, [rip + {arg}]")
                case Region():
                    self.region_to(register, arg)
                case int():
                    self.emit(f"mov {register}, {arg}")
        self.emit(f"call {function}")

    def parallel_move(self, moves: list[tuple[str, str]]) -> None:
        """Do moves (destination, source) between registers and stack slots as if all at once.

        Each move is done once nothing else still needs to read its
        destination. When the rest form cycles (`a <- b, b <- a`), one
        destination is saved in r11 first. A move from a slot to a slot goes
        through rax. Destinations are all different.
        """
        pending = {dest: source for dest, source in moves if dest != source}
        while pending:
            ready = [dest for dest in pending if dest not in pending.values()]
            if ready:
                dest = ready[0]
                source = pending.pop(dest)
                if "[" in dest and "[" in source:
                    self.emit(f"mov rax, {source}")
                    source = "rax"
                self.emit(f"mov {dest}, {source}")
            else:  # a cycle: free one destination by keeping its value in r11
                dest = next(iter(pending))
                self.emit(f"mov r11, {dest}")
                pending = {d: "r11" if source == dest else source for d, source in pending.items()}

    def span_args(self, span: Span) -> tuple[int, int]:
        """A place in the source for the runtime's errors: the line, with the number of its file above its low 32 bits."""
        if span.file not in self.files:
            self.files.append(span.file)
        return self.files.index(span.file) << 32 | span.line, span.column

    # Constants

    def string(self, text: str) -> Label:
        """A static Ella string: its length and where its code points are, which is right after them."""
        if text not in self.strings:
            label = Label(f".Lstr{len(self.strings)}")
            self.strings[text] = label
            self.rodata += ["    .balign 8", f"{label}:", f"    .quad {len(text)}, {label} + 16"]
            if text:
                self.rodata.append("    .long " + ", ".join(str(ord(c)) for c in text))
        return self.strings[text]

    def site(self, span: Span, function: str) -> Label:
        """A call's site, for stack traces: where it is (as `span_args`), and the C string of the function it is in."""
        if (span, function) not in self.sites:
            label = Label(f".Lsite{len(self.sites)}")
            self.sites[span, function] = label
            line, column = self.span_args(span)
            self.rodata += ["    .balign 8", f"{label}:", f"    .quad {line}, {column}, {self.c_string(function)}"]
        return self.sites[span, function]

    def c_string(self, text: str) -> Label:
        """A static C string (UTF-8, ending with a zero byte), for messages and names."""
        if text not in self.c_strings:
            label = Label(f".Lcstr{len(self.c_strings)}")
            self.c_strings[text] = label
            escaped = "".join(
                chr(b) if 32 <= b < 127 and chr(b) not in '"\\' else f"\\{b:03o}" for b in text.encode("utf-8")
            )
            self.rodata += [f"{label}:", f'    .asciz "{escaped}"']
        return self.c_strings[text]

    def type_descriptor(self, t: Type) -> Label:
        """A static description of a type, for the runtime's equality and text form."""
        if t in self.type_labels:
            return self.type_labels[t]
        label = Label(f".Ltype{len(self.type_labels)}")
        # Record the label first: an enum's fields may refer back to the enum itself.
        self.type_labels[t] = label
        parts: list[str] = []
        match t:
            case PrimTy(name):
                kind, count = KINDS[name], 0
            case ListTy(element):
                kind, count, parts = LIST_KIND, 1, [self.type_descriptor(element)]
            case BuiltinTy("Array", (element,)):
                kind, count, parts = ARRAY_KIND, 1, [self.type_descriptor(element)]
            case TupleTy(items):
                kind, count, parts = TUPLE_KIND, len(items), [self.type_descriptor(item) for item in items]
            case FuncTy():
                kind, count = FUNCTION_KIND, 0
            case BuiltinTy("Pool"):
                kind, count = POOL_KIND, 0
            case BuiltinTy("Handle"):
                kind, count = HANDLE_KIND, 0
            case BuiltinTy("Arena"):
                kind, count = ARENA_KIND, 0
            case BuiltinTy("Ref"):
                kind, count = REF_KIND, 0
            case BuiltinTy("Gen"):
                kind, count = GEN_KIND, 0
            case BuiltinTy("Code" | "Type"):
                kind, count = CODE_KIND, 0  # never made: native code can't build code at run time
            case EnumTy(name, args):
                enum = self.module.enums[name]
                mapping = dict(zip(enum.params, args))
                kind, count = ENUM_KIND, len(enum.variants)
                finalize = self.finalizer(name) if enum.finalize else "0"
                # Its text, eq and hash methods, which the runtime calls.
                protocols = [
                    self.static_closure(table[t]) if t in table else "0"
                    for table in (self.module.texts, self.module.eqs, self.module.hashes)
                ]
                error = int(is_builtin_error(self.module.enums, name))
                for variant, fields in enum.variants.items():
                    field_labels = [self.type_descriptor(substitute(f, mapping)) for f in fields]
                    variant_label = self.label("variant")
                    self.rodata += [
                        "    .balign 8",
                        f"{variant_label}:",
                        f"    .quad {self.c_string(bare_name(variant))}, {len(fields)}, {finalize}, {error}, "
                        + ", ".join(protocols)
                        + "".join(f", {f}" for f in field_labels),
                    ]
                    parts.append(variant_label)
            case _:
                raise AssertionError(f"no descriptor for type {t}")
        self.rodata += [
            "    .balign 8",
            f"{label}:  # {t}",
            f"    .quad {kind}, {count}" + "".join(f", {p}" for p in parts),
        ]
        return label

    def finalizer(self, struct: str) -> Label:
        """A static closure of a struct's finalizer (which captures nothing)."""
        return self.static_closure(self.module.enums[struct].finalize)

    def static_closure(self, function: str) -> Label:
        """A closure of an IR function that captures nothing, as static data: its code, no captures."""
        code = self.function_labels[function]
        label = Label(f"ella_closure.{code}")
        if label not in self.static_closures:
            self.static_closures.add(label)
            empty = self.type_descriptor(TupleTy(()))
            self.rodata += ["    .balign 8", f"{label}:", f"    .quad {code}, 0, {empty}"]
        return label

    def tag(self, enum_type: Type, variant: str) -> int:
        """A variant's number: its position in the enum's definition."""
        assert isinstance(enum_type, EnumTy)
        return list(self.module.enums[enum_type.name].variants).index(variant)

    # Instructions

    def instr(self, instr: Instr) -> None:
        op, args, dest, span = instr.op, instr.args, instr.dest, instr.span
        line, column = self.span_args(span)
        self.text.append(f"    # {format_instr(instr)}")  # the IR instruction, as a comment
        match op:
            case "const":
                self.const(instr)
            case "load":
                self.emit(f"mov rax, {self.local_slots[instr.name]}")
                self.store(dest)
            case "store":
                self.load("rax", args[0])
                self.emit(f"mov {self.local_slots[instr.name]}, rax")
            case "global_get":
                ok = self.label("set")
                self.emit(f"cmp qword ptr [rip + ella_global_set.{instr.name}], 0")
                self.emit(f"jne {ok}")
                self.call_c("ella_unset_global", self.c_string(instr.name), line, column)
                self.text.append(f"{ok}:")
                self.emit(f"mov rax, qword ptr [rip + ella_global.{instr.name}]")
                self.store(dest)
            case "global_set":
                self.load("rax", args[0])
                self.emit(f"mov qword ptr [rip + ella_global.{instr.name}], rax")
                self.emit(f"mov qword ptr [rip + ella_global_set.{instr.name}], 1")
            case "add" | "sub" | "mul" | "div" | "neg" if dest is not None and is_array(dest.type):
                self.array_arithmetic(instr)
            case "add" | "sub" | "mul" | "div" | "pow":
                self.arithmetic(instr)
            case "floordiv" | "mod" if args[0].type == I32:
                self.call_c(f"ella_{'floordiv' if op == 'floordiv' else 'mod'}_i32", args[0], args[1], line, column)
                self.store(dest)
            case "floordiv" | "mod":
                self.divide(instr)
            case "shl" | "shr":
                self.call_c(f"ella_{op}", args[0], args[1], line, column)
                self.store(dest)
            case "bitand" | "bitor" | "bitxor":
                self.load("rax", args[0])
                self.emit(f"{op.removeprefix('bit')} rax, {self.slots[args[1].id]}")
                self.store(dest)
            case "neg" if args[0].type == F64:
                self.load("rax", args[0])
                self.emit("btc rax, 63               # flip the sign bit")
                self.store(dest)
            case "neg" if args[0].type == I32:
                ok = self.label("ok")
                self.load("rax", args[0])
                self.emit("neg eax")
                self.emit(f"jno {ok}")
                self.call_c("ella_overflow32_neg", args[0], line, column)
                self.text.append(f"{ok}:")
                self.emit("movsxd rax, eax   # an i32 is kept sign-extended")
                self.store(dest)
            case "neg" if args[0].type == F32:
                self.load("rax", args[0])
                self.emit("btc rax, 31               # flip the sign bit (an f32 is the low 32 bits)")
                self.store(dest)
            case "neg":
                ok = self.label("ok")
                self.load("rax", args[0])
                self.emit("neg rax")
                self.emit(f"jno {ok}")
                self.call_c("ella_overflow_neg", args[0], line, column)
                self.text.append(f"{ok}:")
                self.store(dest)
            case "not":
                self.load("rax", args[0])
                self.emit("xor rax, 1")
                self.store(dest)
            case "bitnot":
                self.load("rax", args[0])
                self.emit("not rax")
                self.store(dest)
            case "eq" | "ne" | "lt" | "le" | "gt" | "ge":
                self.comparison(instr)
            case "contains":
                item, container = args
                match container.type:
                    case ListTy():
                        self.call_c("ella_list_contains", container, item, self.type_descriptor(container.type))
                    case PrimTy("str"):
                        self.call_c("ella_str_contains", container, item)
                    case _:
                        self.call_c("ella_range_contains", container, item)
                self.store(dest)
            case "range" | "range_incl":
                self.call_c(f"ella_{op}", args[0], args[1], Region(instr.region))
                self.store(dest)
            case "tuple" | "list":
                # A list's items follow its length, capacity and pointer to them; a tuple's its length and capacity.
                self.call_c(f"ella_{op}_alloc", len(args), Region(instr.region))
                start = 24 if op == "list" else 16
                for i, item in enumerate(args):
                    self.load("rdx", item)
                    self.emit(f"mov qword ptr [rax + {start + 8 * i}], rdx")
                self.store(dest)
            case "tuple_get":
                self.load("rax", args[0])
                self.emit(f"mov rax, qword ptr [rax + {16 + 8 * instr.index}]")
                self.store(dest)
            case "index" if is_array(args[0].type):
                # array_offset (or a loop over the array) has checked the index. (An f32 takes 4 bytes.)
                self.load("rax", args[0])
                self.load("rcx", args[1])
                self.emit("mov rax, qword ptr [rax + 16]   # where its items are")
                if lanes(args[0].type).kind == "f32":
                    self.emit("mov eax, dword ptr [rax + 4 * rcx]")
                elif lanes(args[0].type).kind == "i32":
                    self.emit("movsxd rax, dword ptr [rax + 4 * rcx]   # an i32 is kept sign-extended")
                else:
                    self.emit("mov rax, qword ptr [rax + 8 * rcx]")
                self.store(dest)
            case "array_offset":
                self.array_offset(instr)
            case "array_slice":
                # The indexes go in ella_indexes (a range as its pointer), which bits of the mask say are ranges.
                array, *indexes = args
                for k, index in enumerate(indexes):
                    self.load("rax", index)
                    self.emit(f"mov qword ptr [rip + ella_indexes + {8 * k}], rax")
                ranges = sum(1 << k for k, index in enumerate(indexes) if index.type == PrimTy("range"))
                self.call_c("ella_array_slice", array, len(indexes), ranges, line, column, Region(instr.region))
                self.store(dest)
            case "fused":
                self.fused(instr)
            case "index":
                container, index = args
                match container.type:
                    case ListTy():
                        # Compared unsigned, a negative index is too big too. Out of range, the runtime reports it.
                        ok = self.label("ok")
                        self.load("rax", container)
                        self.load("rcx", index)
                        self.emit("cmp rcx, qword ptr [rax]   # the list's length")
                        self.emit(f"jb {ok}")
                        self.call_c("ella_list_index", container, index, line, column)
                        self.text.append(f"{ok}:")
                        self.emit("mov rax, qword ptr [rax + 16]   # where its items are")
                        self.emit("mov rax, qword ptr [rax + 8 * rcx]")
                    case PrimTy("str"):
                        self.call_c("ella_str_index", container, index, line, column, Region(instr.region))
                    case _:
                        self.load("rax", container)
                        self.emit("mov rax, qword ptr [rax]   # the range's start")
                        self.emit(f"add rax, {self.slots[index.id]}")
                self.store(dest)
            case "slice" if args[0].type == STR:
                self.call_c("ella_str_slice", args[0], args[1], line, column, Region(instr.region))
                self.store(dest)
            case "slice":
                self.call_c("ella_list_slice", args[0], args[1], line, column, Region(instr.region))
                self.store(dest)
            case "builtin" if (instr.name == "total" and lanes(args[0].type).kind != "i32") or (
                instr.name == "dot" and lanes(args[0].type).floats
            ):
                self.reduction(instr)
            case "builtin":
                # Library functions take their arguments, the call's place for errors, and where the result goes.
                # abs, min, max and the array functions have one for each number type: ella_lib_abs_i64, ...;
                # hash takes its argument's type too.
                result_region = Region(instr.region) if instr.region else 0
                symbol = f"ella_lib_{instr.name}"
                if GENERIC_LIBRARY.get(instr.name) == "Num":
                    symbol += f"_{show_type(library_param(instr.name, [a.type for a in args]))}"
                if GENERIC_LIBRARY.get(instr.name) == "Eq":
                    self.call_c(symbol, *args, self.type_descriptor(args[0].type), line, column, result_region)
                else:
                    self.call_c(symbol, *args, line, column, result_region)
                self.store(dest)
            case "set_index" if is_array(args[0].type):
                # A number, at an offset array_offset has checked; even in a pool's slot, nothing needs copying.
                self.load("rax", args[0])
                self.load("rcx", args[1])
                self.load("rdx", args[2])
                self.emit("mov rax, qword ptr [rax + 16]   # where its items are")
                if lanes(args[0].type).size == 4:
                    self.emit("mov dword ptr [rax + 4 * rcx], edx")
                else:
                    self.emit("mov qword ptr [rax + 8 * rcx], rdx")
            case "set_index":
                self.slot_value(instr, args[0], args[2])
                self.call_c("ella_list_set", args[0], args[1], Register("rax"), line, column)
            case "set_field":
                self.slot_value(instr, args[0], args[1])
                self.emit("mov rdx, rax")
                self.load("rax", args[0])
                self.emit(f"mov qword ptr [rax + {16 + 8 * instr.index}], rdx")
            case "append":
                assert isinstance(args[0].type, ListTy)
                item_type = self.type_descriptor(args[0].type.element)
                region: Region | int = -1 if instr.region == "slot" else Region(instr.region)
                self.call_c("ella_list_append", args[0], args[1], item_type, region)
                self.store(dest)
            case "move":
                self.call_c("ella_move", args[0], self.type_descriptor(args[0].type), Region(instr.region))
                self.store(dest)
            case "pool_new" | "arena_new":
                self.call_c(f"ella_{op}", Region(instr.region))
                self.store(dest)
            case "alloc" if args[0].type == ARENA:
                self.call_c("ella_arena_alloc", args[0], args[1], self.type_descriptor(args[1].type))
                self.store(dest)
            case "free" if args[0].type == ARENA:
                self.call_c("ella_arena_free", args[0])
            case "pool_get" if args[0].type == ARENA:
                self.call_c("ella_arena_get", args[0], args[1], line, column)
                self.store(dest)
            case "pool_set" if args[0].type == ARENA:
                t = self.type_descriptor(args[2].type)
                self.call_c("ella_arena_set", args[0], args[1], args[2], t, line, column)
            case "alloc":
                self.call_c("ella_alloc", args[0], args[1], self.type_descriptor(args[1].type))
                self.store(dest)
            case "free" | "pool_get":
                self.call_c(f"ella_{op}", args[0], args[1], line, column)
                if dest is not None:
                    self.store(dest)
            case "pool_set":
                t = self.type_descriptor(args[2].type)
                self.call_c("ella_pool_set", args[0], args[1], args[2], t, line, column)
            case "quote_begin":
                self.emit("mov rax, 0")
                self.store(dest)
            case "quote" | "run" | "type_value" | "reflect":
                # A function may use quotes only when called while compiling.
                # If native code gets here, it stops.
                message = (
                    f"'{op}' at run time needs an interpreter: native code can only build and run code while compiling"
                )
                self.call_c("ella_error", line, column, self.c_string(message))
            case "copy":
                self.call_c("ella_copy", args[0], self.type_descriptor(args[0].type), Region(instr.region))
                self.store(dest)
            case "swap" if self.function.generator:
                current, carried = self.current_slots[instr.index], self.carried_slots[instr.index]
                self.emit("mov rdi, qword ptr [rbp + 16]")
                self.emit(f"mov rsi, {instr.index + 2}")
                self.emit(f"mov rdx, {current}")
                self.emit(f"mov rcx, {carried}")
                self.emit("call ella_gen_swap")
                self.emit(f"mov rax, {current}")
                self.emit(f"mov rcx, {carried}")
                self.emit(f"mov {current}, rcx")
                self.emit(f"mov {carried}, rax")
            case "swap":
                current, carried = self.current_slots[instr.index], self.carried_slots[instr.index]
                self.emit(f"mov rdi, {self.base_slot}")
                self.emit(f"add rdi, {instr.index + 2}")
                self.emit(f"mov rsi, {self.base_slot}")
                self.emit(f"add rsi, {self.function.levels}")
                self.emit(f"mov rdx, {current}")
                self.emit(f"mov rcx, {carried}")
                self.emit("call ella_swap")
                self.emit(f"mov rax, {current}")
                self.emit(f"mov rcx, {carried}")
                self.emit(f"mov {current}, rcx")
                self.emit(f"mov {carried}, rax")
            case "carry":
                carried = Region(f"carry{instr.index}")
                self.call_c("ella_carry", args[0], self.type_descriptor(args[0].type), carried, Region(instr.region))
                self.store(dest)
            case "reset" if self.function.generator:
                self.emit("mov rdi, qword ptr [rbp + 16]")
                self.emit(f"mov rsi, {instr.index}")
                self.emit("call ella_gen_reset")
                self.restore_swaps(instr.index)
            case "reset":
                if self.base_slot:
                    self.emit(f"mov rdi, {self.base_slot}")
                    self.emit(f"add rdi, {instr.index}")
                    self.emit(f"mov rsi, {self.base_slot}")
                    self.emit(f"add rsi, {self.function.levels}")
                    self.emit("call ella_reset")
                    self.restore_swaps(instr.index)
            case "len" if isinstance(args[0].type, PrimTy) and args[0].type.name == "range":
                self.call_c("ella_range_len", args[0])
                self.store(dest)
            case "len" if isinstance(args[0].type, BuiltinTy) and not is_array(args[0].type):
                self.call_c("ella_arena_len" if args[0].type == ARENA else "ella_pool_len", args[0])
                self.store(dest)
            case "len":
                self.load("rax", args[0])
                self.emit("mov rax, qword ptr [rax]    # strings, lists and arrays start with their length")
                self.store(dest)
            case "to_text":
                self.call_c("ella_to_text", args[0], self.type_descriptor(args[0].type), Region(instr.region))
                self.store(dest)
            case "print" | "eprint":
                self.call_c(f"ella_{instr.op}", args[0], self.type_descriptor(args[0].type))
            case "variant":
                assert dest is not None
                self.call_c("ella_variant_alloc", self.tag(dest.type, instr.name), len(args), Region(instr.region))
                for i, field in enumerate(args):
                    self.load("rdx", field)
                    self.emit(f"mov qword ptr [rax + {16 + 8 * i}], rdx")
                self.store(dest)
                assert isinstance(dest.type, EnumTy)
                if self.module.enums[dest.type.name].finalize:
                    finalizer = self.finalizer(dest.type.name)
                    self.call_c("ella_register", dest, finalizer, Region(instr.region))
            case "is_variant":
                self.load("rax", args[0])
                self.emit(f"cmp qword ptr [rax], {self.tag(args[0].type, instr.name)}")
                self.emit("sete al")
                self.emit("movzx eax, al")
                self.store(dest)
            case "variant_field":
                self.load("rax", args[0])
                self.emit(f"mov rax, qword ptr [rax + {16 + 8 * instr.index}]")
                self.store(dest)
            case "closure" if not args:
                self.emit(f"lea rax, [rip + {self.static_closure(instr.name)}]")
                self.store(dest)
            case "closure":
                types = self.type_descriptor(TupleTy(tuple(a.type for a in args)))
                code = Label(self.function_labels[instr.name])
                self.call_c("ella_closure_alloc", code, len(args), types, Region(instr.region))
                for i, captured in enumerate(args):
                    self.load("rdx", captured)
                    self.emit(f"mov qword ptr [rax + {24 + 8 * i}], rdx")
                self.store(dest)
            case "call" | "call_direct":
                self.call(instr)
            case "enter":
                self.start_call(instr)
            case "leave":
                self.emit("dec qword ptr [rip + ella_depth]")
            case "gen_new":
                self.gen_new(instr)
            case "resume":
                self.resume(instr)
            case "assert":
                ok = self.label("ok")
                self.load("rax", args[0])
                self.emit("test rax, rax")
                self.emit(f"jnz {ok}")
                self.call_c("ella_error", line, column, self.c_string(instr.name))
                self.text.append(f"{ok}:")
            case _:
                raise AssertionError(f"no code for IR operation {op}")

    def restore_swaps(self, first: int) -> None:
        """After `reset first`: put the two arenas of each loop at levels `first` and up that carries values back in
        order (see restore_swaps in ella.ir_interpreter). They are freed, so they are interchangeable."""
        for level in sorted(level for level in self.current_slots if level >= first):
            if self.function.generator:
                self.emit("mov rax, qword ptr [rbp + 16]   # the Gen")
                self.emit(f"mov rcx, qword ptr [rax + {GEN_DEPTHS + 8 * level}]")
                self.emit(f"mov {self.current_slots[level]}, rcx")
                self.emit(f"mov rcx, qword ptr [rax + {GEN_DEPTHS + 8 * (level + 1)}]")
                self.emit(f"mov {self.carried_slots[level]}, rcx")
            else:
                self.emit(f"mov rax, {self.base_slot}")
                self.emit(f"lea rcx, [rax + {level}]")
                self.emit(f"mov {self.current_slots[level]}, rcx")
                self.emit(f"lea rcx, [rax + {level + 1}]")
                self.emit(f"mov {self.carried_slots[level]}, rcx")

    def slot_value(self, instr: Instr, container: Value, value: Value) -> None:
        """Put the value to store into `container` in rax: copied into its slot arena if it is a pool's or arena's value."""
        if instr.region != "slot":
            self.load("rax", value)
            return
        self.call_c("ella_store_slot", container, value, self.type_descriptor(value.type))

    def const(self, instr: Instr) -> None:
        value = instr.const
        assert instr.dest is not None
        register = self.slots[instr.dest.id] if self.in_register(instr.dest) else "rax"
        match value:
            case bool():
                self.emit(f"mov {register}, {int(value)}")
            case int():
                self.emit(f"mov {register}, {value}")
            case F32Value():
                [bits] = struct.unpack("<I", struct.pack("<f", value))
                self.emit(f"mov {register}, {bits}              # f32 {value!r}")
            case float():
                [bits] = struct.unpack("<q", struct.pack("<d", value))
                self.emit(f"mov {register}, {bits}              # {value!r}")
            case str():
                self.emit(f"lea {register}, [rip + {self.string(value)}]")
            case _:
                self.emit(f"mov {register}, 0                 # ()")
        self.store(instr.dest, register)

    def arithmetic(self, instr: Instr) -> None:
        """`add`, `sub`, `mul`, `div` and `pow`, whose code depends on the operands' type."""
        op, (a, b), dest = instr.op, instr.args, instr.dest
        line, column = self.span_args(instr.span)
        match a.type:
            case PrimTy("i32") if op != "div":
                # 32-bit arithmetic, which sets the overflow flag if the result doesn't fit in an i32.
                if op == "pow":
                    self.call_c("ella_pow_i32", a, b, line, column)
                    self.store(dest)
                    return
                ok = self.label("ok")
                self.load("rax", a)
                self.load("rcx", b)
                self.emit(f"{'imul' if op == 'mul' else op} eax, ecx")
                self.emit(f"jno {ok}")
                self.call_c("ella_overflow32", a, b, self.c_string(SYMBOLS[op]), line, column)
                self.text.append(f"{ok}:")
                self.emit("movsxd rax, eax   # an i32 is kept sign-extended")
                self.store(dest)
            case PrimTy("f32"):
                # In the low 32 bits of a word; `movd eax, xmm0` clears the high ones.
                if op == "pow":
                    self.call_c("ella_pow_f32", a, b)
                    self.store(dest)
                    return
                self.load("rax", a)
                self.load("rcx", b)
                self.emit("movd xmm0, eax")
                self.emit("movd xmm1, ecx")
                self.emit(f"{op}ss xmm0, xmm1")
                self.emit("movd eax, xmm0")
                self.store(dest)
            case PrimTy("f64"):
                if op == "pow":
                    self.call_c("ella_pow_f64", a, b)
                    self.store(dest)
                    return
                self.load_xmm("xmm0", a)
                self.emit(f"{op}sd xmm0, {self.xmm_operand(b)}")
                self.store(dest, "xmm0")
            case PrimTy("str"):
                self.call_c("ella_str_concat", a, b, Region(instr.region))
                self.store(dest)
            case ListTy():
                self.call_c("ella_list_concat", a, b, Region(instr.region))
                self.store(dest)
            case _ if op == "div":
                # `/` on i64s divides as f64.
                self.emit(f"cvtsi2sd xmm0, {self.slots[a.id]}")
                self.emit(f"cvtsi2sd xmm1, {self.slots[b.id]}")
                self.emit("divsd xmm0, xmm1")
                self.store(dest, "xmm0")
            case _ if op == "pow":
                self.call_c("ella_pow", a, b, line, column)
                self.store(dest)
            case _:
                # i64 `+`, `-`, `*`: the hardware sets the overflow flag if
                # the result doesn't fit, and then we report it.
                ok = self.label("ok")
                self.load("rax", a)
                self.emit(f"{'imul' if op == 'mul' else op} rax, {self.slots[b.id]}")
                self.emit(f"jno {ok}")
                self.call_c("ella_overflow", a, b, self.c_string(SYMBOLS[op]), line, column)
                self.text.append(f"{ok}:")
                self.store(dest)

    # Arrays

    def array_offset(self, instr: Instr) -> None:
        """`array_offset a, i, j, ...`: where item (i, j, ...) is in the items, checking the indexes against the shape.

        Each index is compared unsigned, so a negative one is too big too. A
        failed check stores the indexes in `ella_indexes` for the runtime's
        message. The sizes multiply to less than 2**48, so the offset can't overflow.
        """
        array, *indexes = instr.args
        line, column = self.span_args(instr.span)
        fail, done = self.label("bad_index"), self.label("index_ok")
        self.load("rax", array)
        self.emit(f"cmp qword ptr [rax + 8], {len(indexes)}   # its number of dimensions")
        self.emit(f"jne {fail}")
        for k, index in enumerate(indexes):
            size = f"qword ptr [rax + {32 + 8 * k}]"
            self.load("rcx", index)
            self.emit(f"cmp rcx, {size}   # the size of dimension {k}")
            self.emit(f"jae {fail}")
            if k == 0:
                self.emit("mov rdx, rcx")
            else:
                self.emit(f"imul rdx, {size}")
                self.emit("add rdx, rcx")
        self.emit(f"jmp {done}")
        self.text.append(f"{fail}:")
        for k, index in enumerate(indexes):
            self.load("rcx", index)
            self.emit(f"mov qword ptr [rip + ella_indexes + {8 * k}], rcx")
        self.call_c("ella_array_index_error", array, len(indexes), line, column)
        self.text.append(f"{done}:")
        self.store(instr.dest, "rdx")

    def array_arithmetic(self, instr: Instr) -> None:
        """`+`, `-`, `*`, `/` and unary `-` on arrays, item by item, as an AVX2 loop over four items at a time (eight
        f32s).

        Either operand may be a number instead of an array, used for every
        item: a vector of four of it. The runtime makes the result (checking
        the shapes; for two arrays of different shapes, it broadcasts them and
        works out the whole result itself, `ella_array_done`); the operands
        live through that call (ella.regalloc's `calls_partway`), and nothing
        else is in a caller-saved register, so the loop has those as well as
        the scratch registers:

            rsi, rdi    the operands' items (or the numbers), ymm2 and ymm3 the numbers' vectors
            rdx, rcx    the result's items, and how many
            r9, r10     the item reached, and the last multiple of the vector's count
            r8          the result

        `-a` is `0 - a` for integers, and flips the sign bits of floats. Float
        arithmetic never fails. Integer `+` and `-` work out each item's
        overflow as a sign bit and gather them in ymm7; AVX2 has no 64-bit
        multiply, and none that notices a 32-bit one overflow, so integer `*`
        has no vector part. If an integer overflowed, the runtime finds the
        first and reports it.
        """
        assert instr.dest is not None
        self.uses_avx2 = True
        lane = lanes(instr.dest.type)
        floats, size = lane.floats, lane.size
        line, column = self.span_args(instr.span)
        op = instr.op
        left: Value | int
        right: Value | int
        if op == "neg" and floats:
            op, left, right = "xor", instr.args[0], lane.sign  # flip the sign bits
        elif op == "neg":
            op, left, right = "sub", 0, instr.args[0]
        else:
            left, right = instr.args
        flags = (
            int(isinstance(left, Value) and is_array(left.type))
            | int(isinstance(right, Value) and is_array(right.type)) << 1
        )
        symbol = "neg" if instr.op == "neg" else SYMBOLS.get(op, "/")
        operator = {"add": "+", "sub": "-", "mul": "*", "div": "/"}.get(instr.op, "-")
        self.call_c("ella_array_like", left, right, flags | ord(operator) << 8, line, column, Region(instr.region))
        broadcast = self.label("broadcast")
        self.emit("cmp qword ptr [rip + ella_array_done], 0")
        self.emit(f"jne {broadcast}   # the shapes differ: the runtime did it all")
        self.emit("mov r8, rax")
        self.emit("mov rcx, qword ptr [rax]   # how many items")
        self.emit("mov rdx, qword ptr [rax + 16]   # where they are")
        sides = []  # each operand: an item at r9, as (vector, scalar) operands
        for register, vector, side, is_left in (("rsi", "ymm2", left, True), ("rdi", "ymm3", right, False)):
            if isinstance(side, Value):
                self.load(register, side)
            else:
                self.emit(f"mov {register}, {side}")
            if flags & (1 if is_left else 2):
                self.emit(f"mov {register}, qword ptr [{register} + 16]")
                sides.append((f"ymmword ptr [{register} + {size} * r9]", f"{lane.ptr} [{register} + {size} * r9]"))
            else:
                self.emit(f"vmovq xmm{vector[-1]}, {register}")
                self.emit(f"{lane.broadcast} {vector}, xmm{vector[-1]}")
                low = {"rsi": "esi", "rdi": "edi"}[register] if lane.kind == "i32" else register
                sides.append((vector, low if not floats else f"xmm{vector[-1]}"))
        (a_vector, a_scalar), (b_vector, b_scalar) = sides
        overflow, done = self.label("overflow"), self.label("done")
        vector_loop = floats or op != "mul"
        self.emit("xor r9d, r9d")
        self.emit("mov r10, rcx")
        self.emit(f"and r10, {-lane.count}")
        loop4, test4, loop1, test1 = (self.label(hint) for hint in ("loop4", "test4", "loop1", "test1"))
        if vector_loop:
            if not floats:
                self.emit("vpxor ymm7, ymm7, ymm7   # the overflows seen")
            self.emit(f"jmp {test4}")
            self.text.append(f"{loop4}:")
            self.emit(f"{'vmovu' if '[' in a_vector else 'vmova'}{lane.p} ymm0, {a_vector}")
            if floats:
                self.emit(f"v{op}{lane.p} ymm0, ymm0, {b_vector}")
                self.emit(f"vmovu{lane.p} ymmword ptr [rdx + {size} * r9], ymm0")
            else:
                self.emit(f"{'vmovdqu' if '[' in b_vector else 'vmovdqa'} ymm1, {b_vector}")
                self.emit(f"vp{op}{lane.q} ymm4, ymm0, ymm1")
                # Overflow sets the sign bit of (a ^ r) & (b ^ r) for +, of (a ^ b) & (a ^ r) for -.
                self.emit(f"vpxor ymm5, ymm0, {'ymm4' if op == 'add' else 'ymm1'}")
                self.emit(f"vpxor ymm6, {'ymm1' if op == 'add' else 'ymm0'}, ymm4")
                self.emit("vpand ymm5, ymm5, ymm6")
                self.emit("vpor ymm7, ymm7, ymm5")
                self.emit(f"vmovdqu ymmword ptr [rdx + {size} * r9], ymm4")
            self.emit(f"add r9, {lane.count}")
            self.text.append(f"{test4}:")
            self.emit("cmp r9, r10")
            self.emit(f"jb {loop4}")
            if not floats:
                self.emit(f"{lane.signs} eax, ymm7")
                self.emit("test eax, eax")
                self.emit(f"jnz {overflow}")
        # The rest one at a time (all of them for an integer `*`).
        self.emit(f"jmp {test1}")
        self.text.append(f"{loop1}:")
        if floats:
            self.emit(f"{'vmov' + lane.s if '[' in a_scalar else 'vmova' + lane.p} xmm0, {a_scalar}")
            self.emit(f"v{op}{lane.p if op == 'xor' else lane.s} xmm0, xmm0, {b_scalar}")
            self.emit(f"vmov{lane.s} {lane.ptr} [rdx + {size} * r9], xmm0")
        else:
            work = lane.scalar
            self.emit(f"mov {work}, {a_scalar}")
            self.emit(f"{'imul' if op == 'mul' else op} {work}, {b_scalar}")
            self.emit(f"jo {overflow}")
            self.emit(f"mov {lane.ptr} [rdx + {size} * r9], {work}")
        self.emit("inc r9")
        self.text.append(f"{test1}:")
        self.emit("cmp r9, rcx")
        self.emit(f"jb {loop1}")
        self.emit("vzeroupper   # (the runtime's code uses SSE)")
        self.emit("mov rax, r8")
        if not floats:
            self.emit(f"jmp {done}")
            self.text.append(f"{overflow}:")
            self.emit("vzeroupper")
            self.call_c("ella_array_overflow", left, right, flags, self.c_string(symbol), line, column)
            self.text.append(f"{done}:")
        self.text.append(f"{broadcast}:")
        self.store(instr.dest)

    def fused_plan(self, instr: Instr, lane: Lanes) -> Label:
        """A `fused` instruction's steps, for the runtime (FusedPlan in runtime.c)."""
        label = Label(self.label("plan"))
        arrays = sum(1 << n for n, arg in enumerate(instr.args) if is_array(arg.type))
        words = [("i64", "f64", "f32").index(lane.kind), len(instr.args), arrays, len(instr.steps)]
        kept = iter(range(len(instr.outs)))
        for step in instr.steps:
            line, column = self.span_args(step.span)
            right = step.right if step.right is not None else 0
            words += [ord(FUSED_OPS[step.op]), step.left, right, line, column, next(kept) if step.kept else -1]
        self.rodata += ["    .balign 8", f"{label}:", "    .quad " + ", ".join(map(str, words))]
        return label

    def fused(self, instr: Instr) -> None:
        """Several steps of arithmetic on arrays (ella.passes.fuse) in one AVX2 loop, four items at a time (eight
        f32s).

        The runtime checks the arrays' shapes and makes the result
        (`ella_fused_start`), from the operands, which go in
        `ella_fused_operands` first; if the shapes differ, it does the steps
        itself (`ella_fused_done`). In the loop, step k's items are in
        ymm k, the numbers among the operands in ymm8 and up, and the
        arrays' items are read where they are, at r9 (rsi, rdi, r11, rax
        point to them). The last few items go through the same code, with
        loads and stores masked by ymm7 (`ella_tail_masks`). For `-x`, ymm6
        holds the sign bits (floats) or 0 (i64, `0 - x`). A `*` whose product is
        only added or subtracted becomes part of that step, a fused
        multiply-add (`fma_steps`). A last step `total` or `dot` adds into
        its own register instead, and the loop stores nothing. As in
        `array_arithmetic`, i64 overflows gather in ymm11 (ymm14 and ymm15
        work them out); if any, the runtime does the steps one by one, which
        finds the first.
        """
        assert instr.dest is not None
        self.uses_avx2 = True
        summed = instr.steps[-1].op in ("total", "dot")
        lane = lanes(instr.dest.type)
        floats = lane.floats
        plan = self.fused_plan(instr, lane)
        for n, arg in enumerate(instr.args):
            self.load("rax", arg)
            self.emit(f"mov qword ptr [rip + ella_fused_operands + {8 * n}], rax")
        for j, region in enumerate(instr.out_regions):  # where the arrays steps keep go
            self.region_to("rax", region)
            self.emit(f"mov qword ptr [rip + ella_fused_depths + {8 * j}], rax")
        self.call_c("ella_fused_start", plan, Region(instr.region))
        finished = self.label("fused_done")
        self.emit("cmp qword ptr [rip + ella_fused_done], 0")
        self.emit(f"jne {finished}   # the shapes differ: the runtime did the steps one by one")
        self.emit("mov r8, rax")
        if summed:
            self.emit(f"mov rcx, qword ptr [rip + ella_fused_operands + {8 * self.first_array(instr)}]")
            self.emit("mov rcx, qword ptr [rcx]   # how many items")
        else:
            self.emit("mov rcx, qword ptr [rax]   # how many items")
            self.emit("mov rdx, qword ptr [rax + 16]   # where they go")
        self.emit("mov r10, rcx")
        self.emit(f"and r10, {-lane.count}")
        self.emit("mov rax, rcx")
        self.emit("sub rax, r10")
        self.emit("shl rax, 5")
        self.emit(f"lea r9, [rip + {lane.masks}]")
        self.emit("vmovdqa ymm7, ymmword ptr [r9 + rax]   # the mask for the last few items")
        if floats:
            self.emit(f"mov rax, {lane.sign}")
            self.emit("vmovq xmm6, rax")
            self.emit(f"{lane.broadcast} ymm6, xmm6   # sign bits")
        else:
            self.emit("vpxor ymm6, ymm6, ymm6")
            self.emit("vpxor ymm11, ymm11, ymm11   # the overflows seen")
        if summed:
            last = len(instr.steps) - 1
            self.emit(f"vpxor ymm{last}, ymm{last}, ymm{last}   # the sums")
        places: list[str] = []  # each operand: its register, or its array's base register
        pointers, numbers = iter(["rsi", "rdi", "r11", "rax"]), iter(range(8, 11))
        for arg in instr.args:
            if is_array(arg.type):
                register = next(pointers)
                self.load(register, arg)
                self.emit(f"mov {register}, qword ptr [{register} + 16]")
                places.append(register)
            else:
                vector = next(numbers)
                self.load("r9", arg)
                self.emit(f"vmovq xmm{vector}, r9")
                self.emit(f"{lane.broadcast} ymm{vector}, xmm{vector}")
                places.append(f"ymm{vector}")
        keeps: list[str] = []  # where each kept step's items go
        for j in range(len(instr.outs)):
            register = next(pointers)
            self.emit(f"mov {register}, qword ptr [rip + ella_fused_outs + {8 * j}]")
            self.emit(f"mov {register}, qword ptr [{register} + 16]   # where kept array {j}'s items go")
            keeps.append(register)
        folded = self.fma_steps(instr) if floats else {}
        loop, test = self.label("loop4"), self.label("test4")
        self.emit("xor r9d, r9d")
        self.emit(f"jmp {test}")
        self.text.append(f"{loop}:")
        self.fused_steps(instr, places, lane, folded, keeps, tail=False)
        self.emit(f"add r9, {lane.count}")
        self.text.append(f"{test}:")
        self.emit("cmp r9, r10")
        self.emit(f"jb {loop}")
        self.fused_steps(instr, places, lane, folded, keeps, tail=True)
        failed = self.label("fused_failed")
        if not floats:
            self.emit("vmovmskpd eax, ymm11")
            self.emit("test eax, eax")
            self.emit(f"jnz {failed}")
        if summed and floats:
            self.sum_vector(lane, f"ymm{len(instr.steps) - 1}")
        elif summed:  # (s0 + s2) + (s1 + s3), checked, as ella.library.lane_sum
            last = len(instr.steps) - 1
            self.emit(f"vextracti128 xmm12, ymm{last}, 1")
            self.emit(f"vmovq rax, xmm{last}")
            self.emit(f"vpextrq rcx, xmm{last}, 1")
            self.emit("vmovq rdx, xmm12")
            self.emit("vpextrq r11, xmm12, 1")
            self.emit("vzeroupper   # (the runtime's code uses SSE)")
            self.emit("add rax, rdx")
            self.emit(f"jo {failed}")
            self.emit("add rcx, r11")
            self.emit(f"jo {failed}")
            self.emit("add rax, rcx")
            self.emit(f"jo {failed}")
        else:
            self.emit("vzeroupper   # (the runtime's code uses SSE)")
            self.emit("mov rax, r8")
        if not floats:
            self.emit(f"jmp {finished}")
            self.text.append(f"{failed}:")
            self.emit("vzeroupper")
            self.call_c("ella_fused_fail", plan)
        self.text.append(f"{finished}:")
        self.store(instr.dest)
        for j, out in enumerate(instr.outs):
            self.emit(f"mov rax, qword ptr [rip + ella_fused_outs + {8 * j}]")
            self.store(out)

    def sum_vector(self, lane: Lanes, vector: str) -> None:
        """Add up the floats in a ymm register into rax (an f32 in its low 32 bits). (Clobbers ymm12, ymm13.)"""
        number = vector.replace("ymm", "xmm")
        self.emit(f"vextractf128 xmm12, {vector}, 1")
        self.emit(f"vadd{lane.p} xmm12, {number}, xmm12")
        self.emit("vunpckhpd xmm13, xmm12, xmm12   # the high 64 bits")
        self.emit(f"vadd{lane.p} xmm12, xmm12, xmm13")
        if lane.kind == "f32":
            self.emit("vmovshdup xmm13, xmm12   # the second f32")
            self.emit("vaddss xmm12, xmm12, xmm13")
            self.emit("vmovd eax, xmm12")
        else:
            self.emit("vmovq rax, xmm12")
        self.emit("vzeroupper   # (the runtime's code uses SSE)")

    @staticmethod
    def first_array(instr: Instr) -> int:
        return next(n for n, arg in enumerate(instr.args) if is_array(arg.type))

    @staticmethod
    def fma_steps(instr: Instr) -> dict[int, int]:
        """The float `+` and `-` steps that can do a `*` step's work too, as one fused multiply-add: step -> the `*`.

        The product must be used by that step alone (natively floats may round once there: docs/grammar.md §9).
        """
        refs = [ref for step in instr.steps for ref in (step.left, step.right) if ref is not None]
        folded: dict[int, int] = {}
        for k, step in enumerate(instr.steps):
            if step.op not in ("add", "sub") or step.right is None:
                continue
            for ref in (step.left, step.right):
                j = -1 - ref
                product = instr.steps[j] if ref < 0 else None
                if (
                    product
                    and product.op == "mul"
                    and not product.kept
                    and refs.count(ref) == 1
                    and j not in folded.values()
                ):
                    folded[k] = j
                    break
        return folded

    def fused_steps(
        self, instr: Instr, places: list[str], lane: Lanes, folded: dict[int, int], keeps: list[str], tail: bool
    ) -> None:
        """The steps of a fused instruction on the items at r9 (the last few, masked, if `tail`). A kept step's items
        are stored where `keeps` points, in order."""
        last = len(instr.steps) - 1
        kept = iter(keeps)
        p, size = lane.p, lane.size
        temps = iter(["ymm12", "ymm13", "ymm14"])

        def operand(ref: int, register: bool = False) -> str:
            """Where an operand's items are: a register, or (if not `register`, and not in the tail) memory."""
            if ref < 0:
                return f"ymm{-1 - ref}"
            if places[ref].startswith("ymm"):
                return places[ref]
            memory = f"ymmword ptr [{places[ref]} + {size} * r9]"
            if tail or register:
                temp = next(temps)
                self.emit(f"vmaskmov{p} {temp}, ymm7, {memory}" if tail else f"vmovu{p} {temp}, {memory}")
                return temp
            return memory

        products = set(folded.values())
        for k, step in enumerate(instr.steps):
            temps = iter(["ymm12", "ymm13", "ymm14"])
            dest = f"ymm{k}"
            if k in products:
                continue  # done by the step that adds it
            if k in folded:
                product = instr.steps[folded[k]]
                assert product.right is not None and step.right is not None
                other = step.right if step.left == -1 - folded[k] else step.left
                x, y = operand(product.left, register=True), operand(product.right)
                z = operand(other)
                self.emit(f"{'vmovu' if '[' in z else 'vmova'}{p} {dest}, {z}")
                if step.op == "add":
                    self.emit(f"vfmadd231{p} {dest}, {x}, {y}   # x * y + z, rounded once")
                elif step.left == -1 - folded[k]:
                    self.emit(f"vfmsub231{p} {dest}, {x}, {y}   # x * y - z, rounded once")
                else:
                    self.emit(f"vfnmadd231{p} {dest}, {x}, {y}   # z - x * y, rounded once")
            elif step.op == "total" or step.op == "dot":
                self.fused_sum(step, dest, operand, lane, tail)
                continue
            elif step.op == "neg" and lane.floats:
                self.emit(f"vxor{p} {dest}, ymm6, {operand(step.left)}")
            elif lane.floats:
                assert step.right is not None
                a, b = operand(step.left, register=True), operand(step.right)
                self.emit(f"v{step.op}{p} {dest}, {a}, {b}")
            else:
                assert step.op == "neg" or step.right is not None
                a = "ymm6" if step.op == "neg" else operand(step.left, register=True)
                b = operand(step.left if step.op == "neg" else step.right)  # type: ignore[arg-type]
                if step.op == "add":  # overflow: the sign bit of (a ^ r) & (b ^ r)
                    self.emit(f"vpaddq ymm15, {a}, {b}")
                    self.emit(f"vpxor ymm14, {a}, ymm15")
                    self.emit(f"vpxor {dest}, ymm15, {b}")
                else:  # sub, or neg as 0 - b: the sign bit of (a ^ b) & (a ^ r)
                    self.emit(f"vpxor ymm14, {a}, {b}")
                    self.emit(f"vpsubq ymm15, {a}, {b}")
                    self.emit(f"vpxor {dest}, {a}, ymm15")
                self.emit(f"vpand ymm14, ymm14, {dest}")
                if tail:
                    self.emit("vpand ymm14, ymm14, ymm7   # only the items there are")
                self.emit("vpor ymm11, ymm11, ymm14")
                self.emit(f"vmovdqa {dest}, ymm15")
            if k == last or step.kept:
                base = next(kept) if step.kept else "rdx"
                if tail:
                    self.emit(f"vmaskmov{p} ymmword ptr [{base} + {size} * r9], ymm7, {dest}")
                else:
                    self.emit(f"vmovu{p} ymmword ptr [{base} + {size} * r9], {dest}")

    def fused_sum(self, step: FusedStep, sums: str, operand: Callable[..., str], lane: Lanes, tail: bool) -> None:
        """A last step `total(x)` or `dot(x, y)`: add more items (or products) to the sums in `sums`.

        In the tail, the lanes past the end are 0 (a step's register there holds
        nothing that counts, so it is masked first)."""
        p = lane.p
        x = operand(step.left, register=True)
        if tail and x.startswith("ymm") and x not in ("ymm12", "ymm13", "ymm14"):
            # (ymm14 is free in float code, where dot's operands use at most ymm12 and ymm13; an i64 total has one.)
            masked = "ymm14" if lane.floats else "ymm12"
            self.emit(f"vpand {masked}, {x}, ymm7   # only the items there are")
            x = masked
        if step.op == "dot":
            assert step.right is not None
            y = operand(step.right)
            if tail:  # a product of what is past the end could be NaN, so mask the products
                self.emit(f"vmul{p} ymm15, {x}, {y}")
                self.emit(f"vand{p} ymm15, ymm15, ymm7")
                self.emit(f"vadd{p} {sums}, {sums}, ymm15")
            else:
                self.emit(f"vfmadd231{p} {sums}, {x}, {y}")
        elif lane.floats:
            self.emit(f"vadd{p} {sums}, {sums}, {x}")
        else:  # i64: the overflows gather in ymm11, as for `+`
            self.emit(f"vpaddq ymm15, {sums}, {x}")
            self.emit(f"vpxor ymm14, {sums}, ymm15")
            self.emit(f"vpxor ymm13, {x}, ymm15")
            self.emit("vpand ymm14, ymm14, ymm13")
            self.emit("vpor ymm11, ymm11, ymm14")
            self.emit(f"vmovdqa {sums}, ymm15")

    def reduction(self, instr: Instr) -> None:
        """`total(a)` of an Array[i64]: four sums at once in ymm0, item i going into sum i % 4.

        (`total` and `dot` of Array[f64]s are `f64_reduction`'s.) The last
        `n % 4` items are loaded with a mask (`ella_tail_masks`), which gives
        0 for the other lanes: adding 0 changes no sum. Then the sums are
        added as `(s0 + s2) + (s1 + s3)`, as ella.library.lane_sum does. The
        overflows are gathered as in `array_arithmetic`, and the runtime
        reports the first.
        """
        assert instr.dest is not None
        self.uses_avx2 = True
        floats = instr.dest.type in (F64, F32)
        if floats:
            self.float_reduction(instr)
            return
        line, column = self.span_args(instr.span)
        dot = instr.name == "dot"
        if dot:
            self.call_c("ella_check_same_shape", instr.args[0], instr.args[1], self.c_string("dot"), line, column)
            self.load("rdi", instr.args[1])
            self.emit("mov rdi, qword ptr [rdi + 16]")
        self.load("rsi", instr.args[0])
        self.emit("mov rcx, qword ptr [rsi]   # how many items")
        self.emit("mov rsi, qword ptr [rsi + 16]   # where they are")
        self.emit("vpxor ymm0, ymm0, ymm0   # the four sums")
        if not floats:
            self.emit("vpxor ymm7, ymm7, ymm7   # the overflows seen")
        self.emit("xor r9d, r9d")
        self.emit("mov r10, rcx")
        self.emit("and r10, -4")
        overflow, done, loop4, test4 = (self.label(hint) for hint in ("overflow", "done", "loop4", "test4"))

        def add_items(load: str, load_b: str) -> None:
            """Add four items (or products) to the sums: `load` puts them in ymm1."""
            self.emit(load)
            if dot:
                self.emit(load_b)
                self.emit("vmulpd ymm1, ymm1, ymm2")
            if floats:
                self.emit("vaddpd ymm0, ymm0, ymm1")
                return
            self.emit("vpaddq ymm4, ymm0, ymm1")
            self.emit("vpxor ymm5, ymm0, ymm4")
            self.emit("vpxor ymm6, ymm1, ymm4")
            self.emit("vpand ymm5, ymm5, ymm6")
            self.emit("vpor ymm7, ymm7, ymm5")
            self.emit("vmovdqa ymm0, ymm4")

        self.emit(f"jmp {test4}")
        self.text.append(f"{loop4}:")
        add_items("vmovupd ymm1, ymmword ptr [rsi + 8 * r9]", "vmovupd ymm2, ymmword ptr [rdi + 8 * r9]")
        self.emit("add r9, 4")
        self.text.append(f"{test4}:")
        self.emit("cmp r9, r10")
        self.emit(f"jb {loop4}")
        self.emit("sub rcx, r10   # the last n % 4 items")
        self.emit("shl rcx, 5")
        self.emit("lea rax, [rip + ella_tail_masks]")
        self.emit("vmovdqa ymm3, ymmword ptr [rax + rcx]")
        add_items(
            "vmaskmovpd ymm1, ymm3, ymmword ptr [rsi + 8 * r9]", "vmaskmovpd ymm2, ymm3, ymmword ptr [rdi + 8 * r9]"
        )
        self.emit("vextractf128 xmm1, ymm0, 1   # (s2, s3)")
        if floats:
            self.emit("vaddpd xmm0, xmm0, xmm1   # (s0 + s2, s1 + s3)")
            self.emit("vunpckhpd xmm1, xmm0, xmm0")
            self.emit("vaddsd xmm0, xmm0, xmm1")
            self.emit("vmovq rax, xmm0")
            self.emit("vzeroupper   # (the runtime's code uses SSE)")
        else:
            self.emit("vmovmskpd eax, ymm7")
            self.emit("test eax, eax")
            self.emit(f"jnz {overflow}")
            self.emit("vmovq rax, xmm0")
            self.emit("vpextrq rcx, xmm0, 1")
            self.emit("vmovq rdx, xmm1")
            self.emit("vpextrq r11, xmm1, 1")
            self.emit("vzeroupper   # (the runtime's code uses SSE)")
            self.emit("add rax, rdx   # s0 + s2")
            self.emit(f"jo {overflow}")
            self.emit("add rcx, r11   # s1 + s3")
            self.emit(f"jo {overflow}")
            self.emit("add rax, rcx")
            self.emit(f"jno {done}")
            self.text.append(f"{overflow}:")
            self.emit("vzeroupper")
            self.call_c("ella_total_overflow", instr.args[0], line, column)
            self.text.append(f"{done}:")
        self.store(instr.dest)

    def float_reduction(self, instr: Instr) -> None:
        """`total(a)` or `dot(a, b)` of arrays of floats, in four vectors of sums at once (ymm0 to ymm3), so that
        the additions don't wait for each other. `dot` multiplies and adds with one rounding (`vfmadd231`), and
        calls the runtime to check the shapes first. (Native code may add floats in its own order:
        docs/grammar.md §9.) The last few items are loaded with a mask, which gives 0 for the other lanes."""
        assert instr.dest is not None
        lane = lanes(instr.dest.type)
        p, size = lane.p, lane.size
        line, column = self.span_args(instr.span)
        dot = instr.name == "dot"
        if dot:
            self.call_c("ella_check_same_shape", instr.args[0], instr.args[1], self.c_string("dot"), line, column)
            self.load("rdi", instr.args[1])
            self.emit("mov rdi, qword ptr [rdi + 16]")
        self.load("rsi", instr.args[0])
        self.emit("mov rcx, qword ptr [rsi]   # how many items")
        self.emit("mov rsi, qword ptr [rsi + 16]   # where they are")
        for q in range(4):
            self.emit(f"vxor{p} ymm{q}, ymm{q}, ymm{q}")
        self.emit("xor r9d, r9d")

        def add(sums: int, load: str, offset: int = 0) -> None:
            """Add the items (or products) at r9 + offset bytes to ymm `sums`, loaded by `load` into ymm4."""
            items = f"ymmword ptr [rsi + {size} * r9 + {offset}]"
            others = f"ymmword ptr [rdi + {size} * r9 + {offset}]"
            if load == "masked":
                self.emit(f"vmaskmov{p} ymm4, ymm6, {items}")
                if dot:
                    self.emit(f"vmaskmov{p} ymm5, ymm6, {others}")
                    self.emit(f"vfmadd231{p} ymm{sums}, ymm4, ymm5")
                else:
                    self.emit(f"vadd{p} ymm{sums}, ymm{sums}, ymm4")
            elif dot:
                self.emit(f"vmovu{p} ymm4, {items}")
                self.emit(f"vfmadd231{p} ymm{sums}, ymm4, {others}")
            else:
                self.emit(f"vadd{p} ymm{sums}, ymm{sums}, {items}")

        loop16, test16, loop4, test4 = (self.label(hint) for hint in ("loop16", "test16", "loop4", "test4"))
        self.emit("mov r10, rcx")
        self.emit(f"and r10, {-4 * lane.count}")
        self.emit(f"jmp {test16}")
        self.text.append(f"{loop16}:")
        for q in range(4):
            add(q, "plain", 32 * q)
        self.emit(f"add r9, {4 * lane.count}")
        self.text.append(f"{test16}:")
        self.emit("cmp r9, r10")
        self.emit(f"jb {loop16}")
        self.emit("mov r10, rcx")
        self.emit(f"and r10, {-lane.count}")
        self.emit(f"jmp {test4}")
        self.text.append(f"{loop4}:")
        add(0, "plain")
        self.emit(f"add r9, {lane.count}")
        self.text.append(f"{test4}:")
        self.emit("cmp r9, r10")
        self.emit(f"jb {loop4}")
        self.emit("sub rcx, r10   # the last few items")
        self.emit("shl rcx, 5")
        self.emit(f"lea rax, [rip + {lane.masks}]")
        self.emit("vmovdqa ymm6, ymmword ptr [rax + rcx]")
        add(1, "masked")
        self.emit(f"vadd{p} ymm0, ymm0, ymm1")
        self.emit(f"vadd{p} ymm2, ymm2, ymm3")
        self.emit(f"vadd{p} ymm0, ymm0, ymm2")
        self.sum_vector(lane, "ymm0")
        self.store(instr.dest)

    def divide(self, instr: Instr) -> None:
        """`//` and `%` on i64s, rounding toward negative infinity (so a remainder has the sign of the divisor).

        Division by zero, and `//` overflowing (the smallest i64 by -1), go to
        the runtime's own function, which reports the error.
        """
        op, (a, b), dest = instr.op, instr.args, instr.dest
        line, column = self.span_args(instr.span)
        slow, minus_one, done = self.label("slow"), self.label("minus_one"), self.label("done")
        self.load("rax", a)
        self.load("rcx", b)
        self.emit("test rcx, rcx")
        self.emit(f"jz {slow}")
        self.emit("cmp rcx, -1")
        self.emit(f"je {minus_one}             # the hardware can't divide the smallest i64 by -1")
        self.emit("cqo")
        self.emit("idiv rcx                  # rax: the quotient, rdx: the remainder, rounded toward zero")
        if op == "mod":
            self.emit("mov rax, rdx")
        self.emit("test rdx, rdx")
        self.emit(f"jz {done}")
        self.emit("xor rdx, rcx")
        self.emit(f"jns {done}                # the remainder and the divisor have the same sign")
        self.emit("add rax, rcx" if op == "mod" else "dec rax")
        self.emit(f"jmp {done}")
        self.text.append(f"{minus_one}:")
        if op == "mod":
            self.emit("xor eax, eax")
        else:
            self.emit("neg rax")
            self.emit(f"jo {slow}")
        self.emit(f"jmp {done}")
        self.text.append(f"{slow}:")
        self.call_c(f"ella_{op}", a, b, line, column)
        self.text.append(f"{done}:")
        self.store(dest)

    def comparison(self, instr: Instr) -> None:
        op, (a, b), dest = instr.op, instr.args, instr.dest
        match a.type:
            case PrimTy("i64" | "i32" | "bool" | "()"):
                self.load("rax", a)
                self.emit(f"cmp rax, {self.slots[b.id]}")
                self.emit(f"{SETCC[op]} al")
            case PrimTy("f64" | "f32"):
                # `ucomisd` compares floats. It reports NaN as "unordered",
                # which the `seta`/`setae` used here treat as false. For `<`
                # and `<=`, compare the other way round.
                if op in ("lt", "le"):
                    a, b = b, a
                if a.type == F32:  # an f32 is in the low 32 bits of its word
                    self.load("rax", a)
                    self.load("rcx", b)
                    self.emit("movd xmm0, eax")
                    self.emit("movd xmm1, ecx")
                    self.emit("ucomiss xmm0, xmm1")
                else:
                    self.load_xmm("xmm0", a)
                    self.emit(f"ucomisd xmm0, {self.xmm_operand(b)}")
                match op:
                    case "eq":
                        self.emit("sete al")
                        self.emit("setnp cl                # and not NaN")
                        self.emit("and al, cl")
                    case "ne":
                        self.emit("setne al")
                        self.emit("setp cl                 # or NaN")
                        self.emit("or al, cl")
                    case "lt" | "gt":
                        self.emit("seta al")
                    case _:
                        self.emit("setae al")
            case PrimTy("str") if op not in ("eq", "ne"):
                self.call_c("ella_str_compare", a, b)
                self.emit("cmp rax, 0")
                self.emit(f"{SETCC[op]} al")
            case _:
                self.call_c("ella_equal", a, b, self.type_descriptor(a.type))
                if op == "ne":
                    self.emit("xor al, 1")
        self.emit("movzx eax, al")
        self.store(dest)

    def start_call(self, instr: Instr) -> None:
        """A call starts: stop with "stack overflow" if calls are nested as deeply as they may be, put the call's
        site in `ella_trace` at the call depth, and count the call."""
        line, column = self.span_args(instr.span)
        ok = self.label("depth")
        self.emit("mov rax, qword ptr [rip + ella_depth]")
        self.emit(f"cmp rax, {self.max_depth}")
        self.emit(f"jl {ok}")
        self.call_c("ella_stack_overflow", self.max_depth, line, column)
        self.text.append(f"{ok}:")
        self.emit("lea rcx, [rip + ella_trace]")
        if instr.within is None:
            self.emit("mov qword ptr [rcx + 8*rax], 0     # the call of main")
        else:
            self.emit(f"lea rdx, [rip + {self.site(instr.span, instr.within)}]")
            self.emit("mov qword ptr [rcx + 8*rax], rdx   # the call's site")
        self.emit("inc rax")
        self.emit("mov qword ptr [rip + ella_depth], rax")

    def call(self, instr: Instr) -> None:
        """Call an Ella function value, or a function directly (`call_direct`), with Ella's calling convention.

        A direct call passes no closure (0): the function captures nothing (see the module docstring).
        """
        if instr.op == "call_direct":
            closure, args, mutable = None, instr.args, instr.mutable
        else:
            closure, *args = instr.args
            assert isinstance(closure.type, FuncTy)
            mutable = closure.type.mutable
        self.start_call(instr)
        # A mut argument that is a pool's value lives in its slot's arena, found
        # from its address. Its new value's slot holds that until the call returns.
        mut_args = [args[i] for i in mutable]
        for region, mut_arg, out in zip(instr.out_regions, mut_args, instr.outs):
            if region == "slot":
                self.call_c("ella_region_of", mut_arg)
                self.store(out)
        for region, out in reversed(list(zip(instr.out_regions, instr.outs))):
            if region == "slot":
                self.emit(f"push {self.slots[out.id]}")
            else:
                self.region_to("rax", region)
                self.emit("push rax                  # where a mut argument lives")
        for arg in reversed(args):
            self.emit(f"push {self.slots[arg.id]}")
        self.region_to("rax", instr.region or "global")
        self.emit("push rax                  # where the result goes")
        if closure is None:
            self.emit("push 0                    # no closure")
            self.emit(f"call {self.function_labels[instr.name]}")
        else:
            self.emit(f"push {self.slots[closure.id]}")
            self.load("rax", closure)
            self.emit("call qword ptr [rax]       # the closure's code")
        for out, position in zip(instr.outs, mutable):
            self.emit(f"mov rcx, qword ptr [rsp + {16 + 8 * position}]   # a mut argument's new value")
            self.emit(f"mov {self.slots[out.id]}, rcx")
        self.emit(f"add rsp, {8 * (len(args) + 2 + len(instr.out_regions))}")
        self.emit("dec qword ptr [rip + ella_depth]")
        self.store(instr.dest)

    # Terminators

    def terminator(self, block: Block) -> None:
        match block.terminator:
            case Jump(target, args):
                tail = self.tail_call(block)
                if tail is not None:
                    self.emit_tail_call(tail)
                    return
                # Move parameters together so loop-carried values can swap safely.
                self.parallel_move([(self.slots[p.id], self.slots[a.id]) for p, a in zip(target.params, args)])
                self.emit(f"jmp {self.block_label(target)}")
            case Branch(cond, then, else_):
                register = self.slots[cond.id] if self.in_register(cond) else "rax"
                self.load(register, cond)
                self.emit(f"test {register}, {register}")
                self.emit(f"jnz {self.block_label(then)}")
                self.emit(f"jmp {self.block_label(else_)}")
            case Return(value, outs):
                tail = self.tail_call(block)
                if tail is not None:
                    self.emit_tail_call(tail)
                    return
                if self.base_slot:
                    self.emit(f"mov rdi, {self.base_slot}")
                    self.emit("call ella_leave            # free the scope levels")
                for out, position in zip(outs, self.function.mut_params):
                    self.load("rax", out)
                    self.emit(f"mov {self.param_slot(position)}, rax   # a mut parameter's final value")
                self.load("rax", value)
                for register, slot in self.saved.items():
                    self.emit(f"mov {register}, {slot}")
                self.emit("leave")
                self.emit("ret")
            case Yield(value, target):
                self.emit("mov rax, qword ptr [rbp + 16]   # the Gen")
                self.emit(f"lea rcx, [rip + {self.block_label(target)}]")
                self.emit(f"mov qword ptr [rax + {GEN_RESUME}], rcx   # where to carry on next time")
                self.load("rax", value)
                self.emit("mov r12, qword ptr [rbp - 8]")
                self.emit("leave")
                self.emit("ret")
            case Trap(value=value, error=True):
                assert value is not None
                self.call_c("ella_main_error", value)
            case Trap(message, value, span):
                line, column = self.span_args(span)
                if value is None:
                    self.call_c("ella_error", line, column, self.c_string(message))
                else:
                    descriptor = self.type_descriptor(value.type)
                    self.call_c("ella_trap_value", self.c_string(message), value, descriptor, line, column)
            case None:
                raise AssertionError(f"{block.name} has no terminator")
