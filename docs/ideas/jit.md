# Runtime code in native Ella programs

**Status:** design exploration, not an implementation plan. This note records
the current options for making staged code run in a native executable, and a
way to gather evidence before choosing how much runtime native code generation
Ella needs.

## 1. The problem

Ella can build code with `quote` and `$`, and `run(code)` checks and executes
that code at the stage where `run` appears. The AST and IR interpreters support
runtime-built code. Native code does not: it can build and run code while
compiling, but reports an error if it reaches runtime staging operations. See
[grammar.md §11](../grammar.md#11-stages), [the staging summary](../summaries/staging_summary.md),
and [README-Claude.md](../../README-Claude.md).

Today, the native path is:

```text
Ella source → check/lower → typed IR → optimize → x64 assembly → gcc + C runtime
```

The IR interpreter's runtime `run(code)` path instead:

1. checks the generated expression against the program's checker,
2. lowers it into a function in the current IR module,
3. verifies the module, and
4. interprets that function.

See [`IRInterpreter.run_code`](../../src/ella/ir_interpreter.py) and the
native staging case in [`codegen.py`](../../src/ella/codegen.py).

Thus, supporting native `run` needs both a way to turn runtime code into a
checked/lowered representation and a way to execute the result. Those are
separate problems:

* An **IR VM** can execute IR, but it does not by itself convert generated
  syntax into IR.
* A **JIT backend** can turn IR into machine code, but it also needs the
  runtime checking/lowering path to produce that IR.
* **AOT compilation of known quote shapes** can avoid both at runtime for that
  subset, by compiling templates and supplying captures at run time.

The main design question is therefore not simply “VM or JIT?” It is: **what
forms of code must native programs be able to construct and run, and when is
their structure known?**

## 2. Option: embed the IR interpreter

### 2.1 Compiling the current Python implementation

Tools such as [mypyc](https://mypyc.readthedocs.io/en/stable/introduction.html)
and [Cython](https://cython.readthedocs.io/en/latest/src/userguide/source_files_and_compilation.html)
can compile Python implementation code to native extensions or C. This may
improve the speed of a Python-hosted interpreter, but it is not a compiler from
Ella IR to native Ella instructions: the IR still runs through an interpreter
dispatch loop.

It also does not by itself produce a Python-free native executable:

* mypyc compiles modules to C extensions that run on CPython.
* Cython's `--embed` support can make a host executable and include compiled
  modules, but the documented embedding flow initializes CPython. See the
  [Cython embedding guide](https://cython.readthedocs.io/en/latest/src/tutorial/embedding.html)
  and [Python's embedding documentation](https://docs.python.org/3/extending/embedding.html).
* Packaging must account for the CPython runtime and the Python modules the
  program needs. The actual executable and distribution cost depends on the
  Python build and packaging choices; this note makes no size estimate.

This route may be useful for accelerating or packaging a Python-hosted Ella
tool. It is less compelling if the goal is a small standalone native Ella
program whose dynamic code runs without Python.

### 2.2 A native IR VM

A different option is to implement an IR VM in C (or, after self-hosting, in
Ella) and include it in native executables that use runtime staging. The VM
would interpret dynamically generated IR, while the existing AOT compiler
continues to handle the statically compiled program.

This can support a broad set of runtime-generated programs without requiring
machine code for every generated function. The interpreter remains a semantic
fallback, and potentially a useful baseline against which to compare JIT code.

It is not a small “link the current interpreter” change. The current IR
interpreter is Python and represents values as Python objects, tracks arena
lifetimes with tags, and checks memory placement during execution. Native
code uses 64-bit words and pointers to C runtime layouts. These two value
representations are not directly interchangeable. See
[`values.py`](../../src/ella/values.py),
[`ir_interpreter.py`](../../src/ella/ir_interpreter.py), and
[`runtime.c`](../../src/ella/runtime.c).

A native VM would need to define or reuse representations for:

* constants, boxed and unboxed values, closures, enums and type descriptors;
* calls, call depth, `mut` arguments, errors and source-level traces;
* arenas, pools, generators, finalization and ownership/copy behavior;
* built-ins, library calls, and interactions with the host's input/output.

It would also need runtime compilation of generated syntax into IR (or a
different representation). Merely embedding a VM does not provide that
compiler.

### 2.3 Sharing the C runtime

“Use the C runtime” has two meanings with very different costs:

1. **Call selected C helpers from the current Python IR interpreter.** The
   interpreter could marshal arguments into C-compatible values for a helper,
   call it, then convert results back. This might help differential testing
   or reuse a few leaf operations, but the two representations and memory
   models still need a bridge. The interpreter would continue to use Python
   values and its own arena tracking. Crossing the bridge could cost more than
   the operation being shared.
2. **Build a native IR VM on the C runtime's value and memory substrate.**
   This is a more coherent architecture. The VM and generated machine code
   could use the same object layouts, arenas, type descriptors, and runtime
   helpers. This creates a potential interoperability boundary between
   interpreted and compiled functions, but requires implementing the VM
   against that ABI.

The second direction is more promising if a native VM is wanted. A useful
principle is to share the **runtime substrate and ABI**, rather than forcing
the existing Python interpreter to imitate the native representation. The
Python interpreter can remain the reference implementation and test oracle.

Using C runtime helpers does not necessarily mean that every IR instruction
should call C. Simple arithmetic and control flow can remain in the VM;
operations involving strings, arrays, allocation, errors, or other shared
runtime behavior are natural helper candidates. The boundary should be chosen
to avoid needless marshalling and to keep observable behavior aligned.

## 3. Option: copy-and-patch code generation

Copy-and-patch compilation uses precompiled machine-code fragments, called
**stencils**, with holes for values such as constants, addresses, or branch
targets. A compiler selects fragments, copies them into a function buffer,
patches the holes, and fixes up control-flow edges.

The key reference is Haoran Xu and Fredrik Kjolstad,
[“Copy-and-Patch Compilation: A Fast Compilation Algorithm for High-Level
Languages and Bytecode”](https://doi.org/10.1145/3485513)
([arXiv version](https://arxiv.org/abs/2011.13127), OOPSLA 2021). The paper
describes build-time stencil generators, runtime selection and patching, and
composition of fragments for operations and control flow. It also describes
the PochiVM and WasmNow implementations:
[PochiVM](https://github.com/sillycross/PochiVM),
[WasmNow](https://github.com/sillycross/WasmNow).

### 3.1 Can the stencil library be built iteratively?

There are two meanings of “iteratively build the library”:

* **Iterative engineering from coverage data:** run programs, collect
  unsupported IR patterns, add stencil generators for them, and repeat.
  This is a reasonable way to prioritize and grow Ella's library.
* **Automatic stencil synthesis:** when a compiler encounters a shape it
  cannot compile, infer or synthesize the missing machine-code stencil and
  add it automatically. The paper's described workflow is based on
  programmer-supplied stencil generators and a generated library; it is not
  an automatic learner of missing stencils. A bounded literature review for
  this note did not identify primary research specifically on an open-ended
  process that learns Ella-like stencil libraries from misses. That is not a
  claim that no related work exists.

Even with an iterative workflow, a library built only from observed successful
executions is not complete. An unexecuted branch can still need to compile,
and future programs can use new type or operand combinations. A corpus tells
us what is useful and common; a coverage model tells us what combinations are
supported.

### 3.2 Composition as an abstraction layer

Copy-and-patch need not mean manually copying unrelated byte arrays at every
call site. A compiler can add layers:

1. **Machine stencils** describe instruction bytes and holes, plus explicit
   register/clobber, stack, relocation, and flag behavior.
2. **IR-operation templates** select one or more machine stencils for an Ella
   operation and its types.
3. **Block/function assembly** assigns locations, lays out fragments, resolves
   branches and calls, and applies relocations.

That is an abstraction over copy-and-patch, but it does not erase the hard
parts. Stencils must compose without violating register use, stack alignment,
calling conventions, flags, or error behavior. Control flow and calls need
fixups. The compiler must decide how values are placed in registers or stack
slots. The typed Ella IR is a promising selection layer, but code generation
still has to satisfy these machine-level constraints.

### 3.3 Shape restrictions that make stencils practical

A restricted native staging model might require:

* quote structure and control flow are known when the executable is built;
* runtime captures are ordinary data arguments, not baked-in machine-code
  constants;
* spliced code also has a statically known shape;
* relevant types, method targets, and layouts are statically known; and
* runtime values do not determine how many expressions or blocks are
  generated.

Under those rules, native code can precompile quote templates and patch or
pass captures at invocation. This is closer to **AOT-compiled quote templates
with runtime data** than to a general-purpose JIT.

Ella's staging facilities can build code whose shape depends on values.
For example, code generation can loop over field names and add an equality
test for each field. When the type and field list are known at compile time,
that generated function is a good template candidate. When the number or
shape of generated fragments depends on runtime data, a fixed precompiled
template may not suffice. Such code needs an interpreter, a more general
runtime compiler, or a language restriction.

## 4. Use the IR interpreter to gather evidence

The IR interpreter can provide useful data about runtime compilation and
stencil demand. It should report **both shape coverage and execution
frequency**, because those answer different questions.

### 4.1 What to record

For each IR function produced by the normal compilation pipeline, and each
function produced by runtime `run(code)`, record a normalized description
such as:

* IR operation kinds and result/operand types;
* block and terminator shapes, including branches and loops;
* call kinds (direct, indirect, built-in, runtime helper) and relevant
  signatures;
* generator operations and other features that affect the calling convention
  or frame;
* constants and operands as categories (for example immediate, function
  address, or captured argument), without recording user data unnecessarily;
* whether the shape was statically compiled or created by runtime staging.

Separately count which instructions and functions actually execute, and how
often. A branch that was not taken still exists in the function and may need
to be compiled; execution counts only help prioritize optimization.

The runtime `run` path already exposes a useful instrumentation point: after
checking and lowering the generated code, the interpreter has a function in
the module before it executes it. That is where a prototype can log the
complete lowered shape, not just the instructions traversed in one run.

### 4.2 What the data does and does not establish

The collected corpus can:

* identify common IR operations, type combinations, and control-flow
  structures;
* show which generated-code shapes users actually produce;
* guide which stencil families are worth implementing first;
* reveal unsupported shapes in real examples and preserve them as regression
  cases; and
* compare interpreter and stencil execution costs for the same workloads.

It cannot establish that the observed set is complete for Ella. Nor can
interpreter observations alone determine every backend detail: register
constraints, instruction encodings, relocation forms, ABI decisions, and
machine-specific alternatives are properties of the native backend.

A useful prototype policy would be: if a shape has no stencil, interpret it,
record a structured “stencil miss,” and keep the program correct. The miss
should include the normalized IR and a small reproducer. This makes coverage
growth data-driven without treating observed coverage as a language
guarantee.

## 5. What self-hosting changes

Self-hosting could change the **deployment and implementation-language**
costs:

* A native Ella program could include an Ella-written compiler and IR VM,
  rather than embedding CPython to run the current Python compiler and
  interpreter.
* The compiler and VM could be ordinary parts of a native Ella executable,
  subject to explicit linking and packaging choices.
* The same compiler could check and lower runtime-built code, while the VM
  executes the resulting IR.
* A JIT or stencil assembler could eventually be written in Ella as well.

Self-hosting does not itself provide a VM API, a native compiler, executable
memory management, or a JIT backend. The bootstrap process may still use
Python, and a standalone executable only avoids Python if its shipped runtime
path does not depend on Python. A JIT still needs to emit or assemble
machine code, manage executable memory, obey the target ABI, and interoperate
with the C runtime.

Self-hosting makes a **hybrid** especially plausible: include an Ella IR VM
for broad support, then use copy-and-patch for well-covered, worthwhile
shapes. Unsupported shapes stay correct through interpretation. Over time,
coverage and benchmark data can indicate whether to expand the stencil set or
invest in a more general native compiler.

## 6. A research-through-prototyping path

This is a sequence for learning, not a commitment to implement every stage:

1. **Define the runtime-code questions.** Clarify whether the target is
   statically known quote templates, arbitrary `Code[T]` values, or both.
   Identify which staging operations must work in native executables.
2. **Instrument the IR interpreter.** Capture complete normalized functions
   produced by normal compilation and runtime `run`, plus separate execution
   frequencies. Keep source programs or generated-IR reproducers for
   unsupported cases.
3. **Build a representative corpus.** Include existing tests, examples,
   generated-code tests, and realistic staging programs. Track observed IR
   coverage by operation, type, and control-flow shape.
4. **Prototype a stencil subset.** Support a narrow, explicit part of the IR.
   Have unsupported functions fall back to the VM/interpreter. Verify output,
   errors, traces, and memory behavior against the reference interpreter and
   native AOT execution where applicable.
5. **Measure the boundary.** Track code-generation time, execution time,
   stencil coverage, runtime footprint, and fallback frequency. This can
   reveal whether the important workload needs fixed templates, composable
   IR stencils, or a fully general JIT.
6. **Decide on the runtime substrate.** If the VM is useful as a fallback,
   decide whether it should share the C runtime ABI, and whether the VM
   should be C, self-hosted Ella, or another implementation. Test the cost
   of crossing between interpreted and native functions.

The important distinction is between **coverage** and **optimization**:
coverage asks whether every allowed shape can run (possibly in the VM);
optimization asks which shapes merit native code. A VM can provide
correctness coverage while copy-and-patch incrementally improves performance.

## 7. Open questions

* Is runtime `run` in native executables a core requirement, or is compiling
  known-shape quotes enough?
* Which staging programs create code whose shape depends on runtime values?
  Should those programs run in a VM, be rejected natively, or be constrained?
* Should a native VM use the C runtime's object representation from the
  beginning, or first use a simpler private representation?
* What is the smallest useful IR subset for a stencil backend, and which
  features (generators, dynamic calls, user-defined methods, arrays) are
  essential to cover?
* How should normalized IR shapes be represented, versioned, and compared
  across compiler changes?
* What corpus and coverage criteria are enough to guide prioritization
  without mistaking observed programs for a proof of completeness?
* What are the acceptable executable size, startup, compile-time, and
  runtime costs for a VM/JIT-enabled native program?

## References

* Haoran Xu and Fredrik Kjolstad. [“Copy-and-Patch Compilation: A Fast
  Compilation Algorithm for High-Level Languages and Bytecode.”](https://doi.org/10.1145/3485513)
  PACMPL, OOPSLA 2021. [arXiv:2011.13127](https://arxiv.org/abs/2011.13127).
* [mypyc introduction](https://mypyc.readthedocs.io/en/stable/introduction.html)
  and [compilation units](https://mypyc.readthedocs.io/en/stable/compilation_units.html).
* [Cython source compilation](https://cython.readthedocs.io/en/latest/src/userguide/source_files_and_compilation.html)
  and [embedding Cython modules](https://cython.readthedocs.io/en/latest/src/tutorial/embedding.html).
* [Embedding Python in another application](https://docs.python.org/3/extending/embedding.html).
