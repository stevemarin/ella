"""x64 code generation: turns IR into GNU assembly (Intel syntax) for Linux.

The generated code is linked with the C runtime (src/ella/runtime.c) into a
native executable; see ella.native.

The design favors being easy to follow over fast code:

* **An IR value lives in a register or in its own 8-byte stack slot.**
  ella.regalloc chooses which values get registers; the rest spill to
  slots. An instruction reads its operands where they are (a register or
  a slot work alike as most operands), computes in the scratch registers
  rax, rcx, rdx, r11 and xmm0/xmm1, and puts the result where it goes.
  Values live across a call are in callee-saved registers, which each
  function saves in its frame first and restores before it returns.
  Values passed to a runtime function or a jump's target move all at once
  (`parallel_move`), since they may be in each other's registers.
* **Every value is one 64-bit word.** An `i64` is itself, a bool is 0 or 1,
  `()` is 0, and an `f64` is its raw bits (moved into an `xmm` register to
  do arithmetic). Strings, lists, tuples, ranges, enum values and closures
  are pointers to objects made by the runtime.
* **Objects live in arenas**, numbered by depth (see "Arenas" in runtime.c).
  Each runtime function that makes an object takes the depth of the arena
  to make it in, worked out from the instruction's region: a function that
  allocates in its own scope levels asks the runtime for depths when it
  starts (`ella_enter`), keeps the first in a stack slot, and frees them
  when it returns (`ella_leave`). A loop that carries values has two arenas
  that take turns (`swap`), so the depths of its level and of its carried
  values are kept in stack slots of their own.

Ella's calling convention
-------------------------

Ella functions don't use the C calling convention. The caller pushes the
depths of the arenas its `mut` arguments live in (last first), then the
arguments from last to first, then the depth of the arena for the result,
then the closure being called, and calls the closure's code. The result
comes back in `rax`. A direct call (`call_direct`) pushes 0 for the
closure. So inside a function with n declared parameters:

    [rbp + 16]              the closure (its captured values start 24 bytes in)
    [rbp + 24]              where to put the result: an arena depth
    [rbp + 32 + 8*i]        declared parameter i
    [rbp + 32 + 8*(n + j)]  the depth of the arena of `mut` parameter j
    [rbp - 8*k]             the slot of each other value that has no register, and of
                            each callee-saved register the function uses

The prologue loads the parameters that have registers. A function returns
the final values of its `mut` parameters by writing them over the
parameters' own words, where the caller reads them after the call.
Pushing everything keeps calls uniform: any number of arguments, and floats
travel like everything else. Calls into the runtime do use the C convention
(arguments in rdi, rsi, rdx, rcx, r8, r9), with the stack aligned to 16
bytes as it requires; each function's prologue aligns it.

Each call increments a call depth, so that runaway recursion stops with
the same "stack overflow" error as in the interpreters; so does each
inlined call (`enter` and `leave`). Before it does, it puts its *site* in
`ella_trace` at the depth: where it is and the function it is in, for the
runtime's stack traces (`Site` in runtime.c; 0 for the call of `main`,
which they leave out).

Generators
----------

A generator function (a generator's body, see "Generators" in ella.ir)
keeps its frame in its Gen (runtime.c), so that its values survive a
`yield`: it addresses them from `r12`, which points to the end of the
frame, instead of from `rbp`. `resume g` calls it like a closure, with the
Gen as the closure, and its prologue sets `r12` and jumps to where it
last stopped. A `yield` records the block to carry on at, and returns.
Other Ella functions save `r12` before using it, as they do the other
callee-saved registers, and a generator function restores it before it
returns. A generator function keeps all its values in its frame, none in
registers. Its scope levels are slot arenas the Gen keeps (`depths`), so
regions name those rather than depths taken by `ella_enter`. `gen_new`
fills in the frame's parameters, and the depths of the loops that carry
values, at offsets known from the body's layout.
"""

import struct
from dataclasses import dataclass, field

from ella.checker import (
    ARENA,
    F64,
    GENERIC_LIBRARY,
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
    show_type,
    substitute,
)
from ella.ir import (
    Block,
    Branch,
    Function,
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
    is_boxed,
    region_level,
)
from ella.lexer import Span
from ella.regalloc import allocate, uses_levels
from ella.syntax import bare_name
from ella.values import MAX_CALL_DEPTH

# Type descriptor kinds; these must match `enum Kind` in runtime.c.
KINDS = {"()": 0, "i64": 1, "f64": 2, "bool": 3, "str": 4, "range": 5, "never": 0}
LIST_KIND, TUPLE_KIND, FUNCTION_KIND, ENUM_KIND, POOL_KIND, HANDLE_KIND, CODE_KIND = 6, 7, 8, 9, 10, 11, 12
ARENA_KIND, REF_KIND, GEN_KIND = 13, 14, 15

# A Gen (runtime.c): its code, where it carries on, its number of levels and frame size, then each level's depth.
GEN_RESUME, GEN_DEPTHS = 8, 32

# The registers for the first six arguments of a C call.
C_ARGS = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]

# Comparison results for i64 (and bool), by IR operation: the `setcc` instruction.
SETCC = {"eq": "sete", "ne": "setne", "lt": "setl", "le": "setle", "gt": "setg", "ge": "setge"}

# The Ella operator of each arithmetic operation, for overflow messages.
SYMBOLS = {"add": "+", "sub": "-", "mul": "*", "floordiv": "//", "pow": "**", "shl": "<<"}


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
        self.labels += 1
        return f".L{hint}{self.labels}"

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
            "# Call an Ella closure with one argument, from the runtime: ella_call1(closure, argument).",
            "# It runs finalizers, whose results are () and need no arena.",
            "# It counts as a call, at the runtime's finalizer site, as in the interpreters.",
            "    .globl ella_call1",
            "ella_call1:",
            "    push rbp",
            "    mov rbp, rsp",
            "    mov rax, qword ptr [rip + ella_depth]",
            "    lea rcx, [rip + ella_trace]",
            "    lea rdx, [rip + ella_finalizer_site]",
            "    mov qword ptr [rcx + 8*rax], rdx",
            "    inc qword ptr [rip + ella_depth]",
            "    push rsi                    # the argument",
            "    push 0                      # where the result goes",
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
            for instr in block.instrs:
                self.instr(instr)
            self.terminator(block)

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
                error = int(is_builtin_error(self.module.enums, name))
                for variant, fields in enum.variants.items():
                    field_labels = [self.type_descriptor(substitute(f, mapping)) for f in fields]
                    variant_label = self.label("variant")
                    self.rodata += [
                        "    .balign 8",
                        f"{variant_label}:",
                        f"    .quad {self.c_string(bare_name(variant))}, {len(fields)}, {finalize}, {error}"
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
            case "add" | "sub" | "mul" | "div" | "pow":
                self.arithmetic(instr)
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
            case "builtin":
                # Library functions take their arguments, the call's place for errors, and where the result goes.
                # abs, min and max have one for each number type: ella_lib_abs_i64, ...; hash takes its
                # argument's type too.
                result_region = Region(instr.region) if instr.region else 0
                symbol = f"ella_lib_{instr.name}"
                if GENERIC_LIBRARY.get(instr.name) == "Num":
                    symbol += f"_{show_type(args[0].type)}"
                if GENERIC_LIBRARY.get(instr.name) == "Eq":
                    self.call_c(symbol, *args, self.type_descriptor(args[0].type), line, column, result_region)
                else:
                    self.call_c(symbol, *args, line, column, result_region)
                self.store(dest)
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
            case "len" if isinstance(args[0].type, BuiltinTy):
                self.call_c("ella_arena_len" if args[0].type == ARENA else "ella_pool_len", args[0])
                self.store(dest)
            case "len":
                self.load("rax", args[0])
                self.emit("mov rax, qword ptr [rax]    # strings and lists start with their length")
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
            case PrimTy("i64" | "bool" | "()"):
                self.load("rax", a)
                self.emit(f"cmp rax, {self.slots[b.id]}")
                self.emit(f"{SETCC[op]} al")
            case PrimTy("f64"):
                # `ucomisd` compares floats. It reports NaN as "unordered",
                # which the `seta`/`setae` used here treat as false. For `<`
                # and `<=`, compare the other way round.
                if op in ("lt", "le"):
                    a, b = b, a
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
                # Pass values to the target's parameters, all at once, since
                # parameters can swap values, as loop variables can.
                self.parallel_move([(self.slots[p.id], self.slots[a.id]) for p, a in zip(target.params, args)])
                self.emit(f"jmp {self.block_label(target)}")
            case Branch(cond, then, else_):
                register = self.slots[cond.id] if self.in_register(cond) else "rax"
                self.load(register, cond)
                self.emit(f"test {register}, {register}")
                self.emit(f"jnz {self.block_label(then)}")
                self.emit(f"jmp {self.block_label(else_)}")
            case Return(value, outs):
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
