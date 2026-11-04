# Ella: An Open Textbook on Compiler Construction, SSA, Region-Based Memory Safety, and Native Code Generation

Welcome to the definitive companion textbook for **Ella**, a statically typed, multi-stage programming language and compiler targeting Linux x86-64.

Ella is engineered to be an open, readable implementation of foundational compiler techniques. Rather than relying on heavyweight, opaque dependencies (such as LLVM or large C++ runtimes) or falling back to a tracing garbage collector, Ella demonstrates how modern programming languages can achieve **complete memory safety without a garbage collector and without a Rust-style borrow checker**.

---

## Table of Contents

- [Chapter 0: The Compiler Architecture & Pipeline Overview](#chapter-0-the-compiler-architecture--pipeline-overview)
- [Chapter 1: Lexical Analysis & Layout-Sensitive Grammars (`src/ella/lexer.py`)](#chapter-1-lexical-analysis--layout-sensitive-grammars)
- [Chapter 2: Abstract Syntax Trees & Expression-Oriented Grammars (`src/ella/syntax.py`)](#chapter-2-abstract-syntax-trees--expression-oriented-grammars)
- [Chapter 3: Top-Down Operator Precedence & Pratt Parsing (`src/ella/parser.py`)](#chapter-3-top-down-operator-precedence--pratt-parsing)
- [Chapter 4: Intermediate Representation (IR) & Static Single Assignment (`src/ella/ir.py`)](#chapter-4-intermediate-representation-ir--static-single-assignment)
- [Chapter 5: AST Lowering & Compiler-Directed Region Inference (`src/ella/lower.py`)](#chapter-5-ast-lowering--compiler-directed-region-inference)
- [Chapter 6: The IR Optimization Pipeline (`src/ella/passes/`)](#chapter-6-the-ir-optimization-pipeline)
  - [Chapter 6.1: SSA Construction (`src/ella/passes/mem2reg.py`)](#chapter-61-ssa-construction)
  - [Chapter 6.2: Value-Semantics Optimization (`src/ella/passes/moves.py`)](#chapter-62-value-semantics-optimization)
  - [Chapter 6.3: Constant Folding & Propagation (`src/ella/passes/fold.py`)](#chapter-63-constant-folding--propagation)
  - [Chapter 6.4: CFG Canonicalization (`src/ella/passes/simplify.py`)](#chapter-64-cfg-canonicalization)
  - [Chapter 6.5: Dead Code Elimination (`src/ella/passes/dce.py`)](#chapter-65-dead-code-elimination)
  - [Chapter 6.6: Procedure Inlining (`src/ella/passes/inline.py`)](#chapter-66-procedure-inlining)
  - [Chapter 6.7: Call Devirtualization (`src/ella/passes/direct.py`)](#chapter-67-call-devirtualization)
  - [Chapter 6.8: Array Loop Fusion (`src/ella/passes/fuse.py`)](#chapter-68-array-loop-fusion)
- [Chapter 7: Operational Semantics & the Reference Interpreter (`src/ella/interpreter.py`)](#chapter-7-operational-semantics--the-reference-interpreter)
- [Chapter 8: Machine Code Generation & x64 Native Execution (`src/ella/codegen.py`)](#chapter-8-machine-code-generation--x64-native-execution)
- [Chapter 9: The Low-Level Runtime Engine & Memory Subsystem (`src/ella/runtime.c`)](#chapter-9-the-low-level-runtime-engine--memory-subsystem)
- [Chapter 10: Multi-Stage Programming & Metaprogramming (`src/ella/staging.py`)](#chapter-10-multi-stage-programming--metaprogramming)
- [Chapter 11: Static Type Systems, Unification & Bidirectional Checking (`src/ella/checker.py`)](#chapter-11-static-type-systems-unification--bidirectional-checking)
- [Chapter 12: Runtime Value Representation & Object Model (`src/ella/values.py`)](#chapter-12-runtime-value-representation--object-model)
- [Chapter 13: Operator Semantics & Arithmetic Invariants (`src/ella/operators.py`)](#chapter-13-operator-semantics--arithmetic-invariants)
- [Chapter 14: Standard Runtime Primitives & the Host Environment (`src/ella/library.py`)](#chapter-14-standard-runtime-primitives--the-host-environment)
- [Chapter 15: Module Systems, Namespacing & Link-Time Synthesis (`src/ella/modules.py`)](#chapter-15-module-systems-namespacing--link-time-synthesis)
- [Chapter 16: Pattern Matching Exhaustiveness & Maranget's Matrix Algorithm (`src/ella/exhaustive.py`)](#chapter-16-pattern-matching-exhaustiveness--marangets-matrix-algorithm)
- [Chapter 17: SSA Register Allocation & Chordal Graph Coloring (`src/ella/regalloc.py`)](#chapter-17-ssa-register-allocation--chordal-graph-coloring)
- [Chapter 18: Resilient Parsing & IDE Static Code Intelligence (`src/ella/analysis.py`)](#chapter-18-resilient-parsing--ide-static-code-intelligence)
- [Chapter 19: Native AOT Driver & Host Toolchain Orchestration (`src/ella/native.py`)](#chapter-19-native-aot-driver--host-toolchain-orchestration)
- [Chapter 20: Language Server Protocol (LSP) Architecture (`src/ella/lsp.py`)](#chapter-20-language-server-protocol-lsp-architecture)
- [Chapter 21: The IR Virtual Machine & Hardware Arena Simulator (`src/ella/ir_interpreter.py`)](#chapter-21-the-ir-virtual-machine--hardware-arena-simulator)
- [Appendix: In-Depth Module Essays](#appendix-in-depth-module-essays)

---

## Chapter 0: The Compiler Architecture & Pipeline Overview

*Primary Module:* [`src/ella/__init__.py`](./src/ella/__init__.py)

### 1. Architectural Philosophy

Ella prioritizes **Simplicity > Speed > Memory**:

1. Spending virtual memory to keep compiler algorithms clear and transparent is preferred over opaque, fragile heuristics.
2. The front-end and middle-end are implemented in pure Python 3.15, leveraging modern features (PEP 695 type aliases, pattern matching, frozen slotted dataclasses).
3. The backend produces raw x86-64 assembly in Intel syntax, linked against a lean, zero-dependency C runtime (`src/ella/runtime.c`).
4. **Execution Parity Invariant:** Every program must yield byte-for-byte identical output and matching runtime errors across the AST tree-walking interpreter, the IR interpreter, and native compiled execution.

### 2. The Compilation Pipeline

```text
      Source Code (.ella)
             │
             ▼  [1. Lexical Analysis] (src/ella/lexer.py)
        Token Stream (with INDENT/DEDENT layout & Spans)
             │
             ▼  [2. Syntactic Analysis] (src/ella/parser.py using Pratt parsing)
        Abstract Syntax Tree (AST, defined in src/ella/syntax.py)
             │
             ▼  [3. Multi-Stage Expansion] (src/ella/staging.py: compile-time splices `$`)
        Expanded AST + Module Linker (src/ella/modules.py)
             │
             ▼  [4. Type Checking & Inference] (src/ella/checker.py)
        Typed AST (annotated with types, local bindings, exclusivity rules)
             │
         ┌───┴─────────────────────────────────────────┐
         │                                             │
         ▼ (Reference / Staging)                       ▼ [5. IR Lowering] (src/ella/lower.py)
    AST Interpreter (src/ella/interpreter.py)    Typed CFG IR (src/ella/ir.py)
    (executes stage -1 compile-time splices)           │
                                                       ├─► IR Interpreter (src/ella/ir_interpreter.py)
                                                       │   (simulates arenas & verifies safety)
                                                       │
                                                       ▼ [6. Optimization Pipeline] (src/ella/passes/)
                                                Optimized SSA IR (mem2reg, moves, inline, etc.)
                                                       │
                                                       ▼ [7. Code Generation] (src/ella/codegen.py)
                                                x64 Assembly (.s) + C Runtime (runtime.c)
                                                       │
                                                       ▼ [8. Linker] (gcc via src/ella/native.py)
                                                Native ELF Executable
```

### 3. Pipeline Inspection Tools

The compiler driver exposes flags for inspecting intermediate representations at each phase:

- `ella --tokens prog.ella`: Emits lexical tokens with source coordinates.
- `ella --ast prog.ella`: Emits S-expression serialized syntax trees.
- `ella --expanded prog.ella`: Emits the AST after expanding stage $-1$ compile-time splices.
- `ella --ir prog.ella`: Emits the typed three-address control-flow graph.
- `ella --ir -O prog.ella`: Emits the optimized SSA intermediate representation.
- `ella --placement prog.ella`: Displays the memory arena chosen for each allocation.
- `ella --profile prog.ella`: Executes the IR interpreter and displays peak memory usage per scope.
- `ella --asm prog.ella`: Emits raw Intel-syntax x64 GNU assembly.
- `ella --native prog.ella`: Compiles and executes a native ELF binary.

---

## Chapter 1: Lexical Analysis & Layout-Sensitive Grammars

*Primary Module:* [`src/ella/lexer.py`](./src/ella/lexer.py)

### 1. Theoretical Foundations

- **Maximal Munch (Longest Match Rule):** In lexical analysis (Dragon Book, Chapter 3), when multiple token patterns can match the current stream, the scanner always consumes the longest valid prefix. Ella achieves this by sorting operator definitions longest-first: `//=` is tokenized as a single compound assignment operator rather than `//` followed by `=`.

- **The Off-Side Rule (Peter J. Landin 1966):** Following Landin's landmark paper *"The Next 700 Programming Languages"*, Ella uses 2D indentation to represent block structures without curly braces. The scanner tracks an indentation stack (`self.indents = [0]`) and synthesizes virtual delimiters:

  - `NEWLINE`: Marks the end of a logical statement line.
  - `INDENT`: Synthesized when indentation increases (acts as `{`).
  - `DEDENT`: Synthesized when indentation decreases (acts as `}`).
- **Bracket Sensitivity:** When open parentheses `(` or brackets `[` are active (`self.brackets`), newlines are treated as ordinary whitespace, allowing multi-line expressions without continuation backslashes.

### 2. String Interpolation State Machine

String interpolation (`"The total is {{x + 1}}"`) is parsed without nested scanner recursion by deconstructing the literal into discrete lexical tokens:

```text
STRING_START -> STRING_TEXT("The total is ") -> INTERP_START -> [Token(NAME, "x"), Token(OP, "+"), Token(INT, "1")] -> INTERP_END -> STRING_END
```

This allows the parser to parse interpolated expressions using identical grammar rules to top-level code.

---

## Chapter 2: Abstract Syntax Trees & Expression-Oriented Grammars

*Primary Module:* [`src/ella/syntax.py`](./src/ella/syntax.py)

### 1. Parse Tree vs. Abstract Syntax Tree

A Parse Tree contains concrete syntactic tokens (colons, commas, parenthesis tokens, indentation markers). An Abstract Syntax Tree (AST) retains purely semantic structure:

- Syntactic sugar is normalized during AST construction (e.g., pipeline operator `x |> f(y)` desugars directly into the call `f(x, y)`).
- Every AST node preserves its `Span(line, column, file)` coordinates for precise diagnostic reporting.

### 2. Expression-Oriented Design

In Ella, statements do not exist in isolation; **everything is an expression**:

- An `if-then-else` block evaluates to the value of its executed branch:

  ```ella
  let status = if score >= 50 then "pass" else "fail"
  ```

- Blocks `{ ... }` evaluate to their final expression (or unit `()` if empty).

- Assignments (`x = 5`) evaluate to `()`.

### 3. S-Expression Canonical Serialization (`show()`)

Ella defines an S-expression serialization format for ASTs:

```text
Source:         let x = 1 + 2 * 3
S-Expression:   (let [x] (+ 1 (* 2 3)))
```

This provides human-readable snapshot testing without fragile object comparisons.

---

## Chapter 3: Top-Down Operator Precedence & Pratt Parsing

*Primary Module:* [`src/ella/parser.py`](./src/ella/parser.py)

### 1. Theoretical Breakthrough (Vaughan Pratt, POPL 1973)

Traditional recursive descent parsers require a separate grammar production and mutually recursive function for every precedence level:
$$\text{expr} \to \text{term} (('+' \mid '-') \text{term})^* \quad;\quad \text{term} \to \text{factor} (('*' \mid '/') \text{factor})^*$$
Pratt parsing associates numeric binding powers directly with tokens:

- **Higher number = binds tighter:** e.g., `*` (precedence 14) binds tighter than `+` (precedence 13).

### 2. Null Denotations vs. Left Denotations

- **Null Denotation (`nud` / `parse_prefix`):** What a token means when starting an expression (literals, identifiers, unary `-x`, `(x + 1)`, keyword expressions like `if`).

- **Left Denotation (`led` / `parse_infix`):** What a token means when combining with an expression on its left (`+`, `*`, `==`, member access `.`, indexing `[i]`, calls `(args)`).

### 3. Associativity Mechanics

- **Left-associative (`a - b - c`):** Parse right-hand side with `prec + 1` (rejects equal-precedence operators, forcing left-grouping).

- **Right-associative (`a ** b ** c`):** Parse right-hand side with `prec` (accepts equal-precedence operators, forcing right-grouping).

---

## Chapter 4: Intermediate Representation (IR) & Static Single Assignment

*Primary Module:* [`src/ella/ir.py`](./src/ella/ir.py)

### 1. The Role of an IR

The Intermediate Representation (IR) bridges high-level syntax trees and bare-metal machine code:

- Nested expressions are flattened into explicit control-flow basic blocks.
- Variable mutations are made explicit via loads, stores, and SSA values.
- Memory allocations are tagged with concrete region targets.

### 2. Block Parameters vs. Classical $\phi$-Nodes

In classical SSA (e.g. LLVM), control-flow joins use $\phi$-functions:
$$\%z = \phi [ \%x, \text{block}_1 ], [ \%y, \text{block}_2 ]$$
$\phi$-nodes suffer from placement ordering constraints and parallel-copy hazards.

Following Andrew Appel (1998, *"SSA is Functional Programming"*) and modern IRs (Swift SIL, MLIR, Rust Cranelift), Ella replaces $\phi$-nodes with **Block Parameters**:

```text
then_block:
    jump join_block(%val_then)
else_block:
    jump join_block(%val_else)
join_block(%result: i64):
    ...
```

Blocks act like local functions; control-flow branches pass arguments directly.

### 3. Region Annotations

Every heap-allocated object explicitly designates its target memory region:

- `@0`: Function frame scope level.
- `@1, @2, ...`: Nested loop iteration levels.
- `@result`: The caller's result arena (zero-copy return construction).
- `@mut0`: The arena of a mutable caller variable.
- `@carry1`: Alternating ping-pong loop carried state.
- `@global`: Global top-level process arena.

---

## Chapter 5: AST Lowering & Compiler-Directed Region Inference

*Primary Module:* [`src/ella/lower.py`](./src/ella/lower.py)

### 1. Tree Flattening & Closure Conversion

Lowering flattens nested syntax trees into linear three-address basic blocks:

- **Closure Conversion:** Lambdas capturing local variables are lifted into top-level functions whose captured environment is passed as explicit leading parameters.
- **Return Normalization:** Function returns jump to a unified exit block to ensure postconditions and finalizers execute reliably.

### 2. Simplified Tofte–Talpin Region Inference

Ella manages dynamic memory **without a garbage collector** (Mads Tofte & Jean-Pierre Talpin 1997):

- **Destination-Driven Lowering:** Expressions are compiled with a designated *destination region*. When evaluating `return [1, 2, 3]`, the list is constructed directly in `@result` (the caller's arena), completely avoiding return copies.
- **Tracking `borrows` and `lives`:** The lowering tracks which variables an object aliases (`borrows`) and the shortest-lived region it depends on (`lives`). Deep copies are inserted only if:
  1. A mutable container is stored somewhere that could observe in-place mutations.
  2. An object would outlive its enclosing scope arena.

### 3. Taming Region Bloat: Alternating Loop Arenas

In classic region systems, loop accumulators (`for i in 0..n: acc = acc + [i]`) leak memory in the outer scope until the entire function returns. Ella completely eliminates loop region bloat:

- Outer variables carried across iterations are detected automatically (`carried_by`).
- The loop uses two alternating scope arenas: `@k` (current iteration) and `@carryk` (previous state).
- At the iteration latch (`swap k`), the current iteration's arena becomes the carried arena, and the old carried arena is freed.
- At most **two versions** of carried state are alive at any time, reducing memory from $O(N)$ to $O(1)$.

---

## Chapter 6: The IR Optimization Pipeline

*Primary Module:* [`src/ella/passes/__init__.py`](./src/ella/passes/__init__.py)

### 1. Iterative Fixed-Point Transformation

Optimization passes are decoupled into focused, single-purpose transformations. Because passes create optimization opportunities for one another (e.g. constant folding enables branch deletion, which enables dead code elimination, which enables copy-to-move elision), passes run iteratively until no further transformations occur (`CLEANUP = ["moves", "fold", "simplify", "dce"]`).

### 2. The Trap Preservation Invariant

A compiler optimization must never alter observable program behavior, **including dynamic runtime traps**:

- Division by zero (`x / 0`), integer overflow, or out-of-bounds indexing must NEVER be folded or speculatively deleted if that error would have occurred during unoptimized execution.

### 3. Continuous Verification

The IR verifier (`verify()`) runs before and after every single optimization pass, asserting SSA dominance, type consistency, and block terminator invariants.

---

### Chapter 6.1: SSA Construction

*Primary Module:* [`src/ella/passes/mem2reg.py`](./src/ella/passes/mem2reg.py)

#### 1. Braun et al. (CC 2013) vs. Cytron et al. (1991)

Classical SSA construction (Cytron 1991) requires computing dominator trees, iterated dominance frontiers ($IDF$), and variable stacks.

Ella implements the algorithm of Matthias Braun et al. (Compiler Construction 2013):

- To resolve a variable's value at instruction $I$, look backwards in the basic block for the latest `store`.
- If not found, query the block's predecessors:
  - One predecessor: Take the value from the predecessor's end.
  - Multiple predecessors: Add a new block argument (parameter) and query all incoming branch edges recursively.
- Trivial block parameters (parameters that receive the same value from all paths) are pruned via `remove_trivial_params`.

---

### Chapter 6.2: Value-Semantics Optimization

*Primary Module:* [`src/ella/passes/moves.py`](./src/ella/passes/moves.py)

#### 1. Moves as Compiler Optimizations

In Rust or C++, moving is a user-visible language semantic rule requiring programmers to manage ownership and learn "use-after-move" errors.

In Ella, **everything is conceptually a deep copy**. Moving is an invisible compiler optimization:

- When value `%1` is copied (`%2 = copy %1`) and the compiler proves that `%1` is never read again, the copy is elided and `%1` is moved into place.
- Safety invariants:
  1. `%1` has exactly one use in the entire function (`uses[original.id] == 1`).
  2. `%1`'s definition dominates `%2`, and `%2` post-dominates `%1`.
  3. The path between `%1` and `%2` does not cross a loop cycle.

---

### Chapter 6.3: Constant Folding & Propagation

*Primary Module:* [`src/ella/passes/fold.py`](./src/ella/passes/fold.py)

- **Constant Folding:** Replaces instructions whose operands are literal constants with pre-computed `const` values.
- **Constant Propagation:** Follows Kildall's semi-lattice dataflow framework, replacing block parameters with constants when all incoming branches pass identical literal values.
- **Trap Safety:** If folding an operation triggers an error (such as division by zero or integer overflow), folding is skipped so the trap fires dynamically at runtime.

---

### Chapter 6.4: CFG Canonicalization

*Primary Module:* [`src/ella/passes/simplify.py`](./src/ella/passes/simplify.py)

Performs four canonical control-flow graph tidy-ups:

1. **Branch Folding:** Replaces boolean constant branches with unconditional jumps.
2. **Unreachable Code Elimination:** Deletes blocks unreachable from function entry.
3. **Jump Threading:** Short-circuits empty trampoline blocks that do nothing but jump onward.
4. **Block Coalescing:** Merges block $B$ into block $A$ when $A$ jumps unconditionally to $B$ and $B$ has $A$ as its sole predecessor.

---

### Chapter 6.5: Dead Code Elimination

*Primary Module:* [`src/ella/passes/dce.py`](./src/ella/passes/dce.py)

- Sweeps instructions whose results are unused.
- **Purity Guarantee:** Only instructions proven to be side-effect-free and trap-free are deleted (e.g. constant generation, tuple extraction, IEEE float math). Memory indexing or integer arithmetic that could trap is retained.
- **Level Trimming:** Calls `trim_levels()` to delete scope arena allocations if all allocating instructions in that level were eliminated.

---

### Chapter 6.6: Procedure Inlining

*Primary Module:* [`src/ella/passes/inline.py`](./src/ella/passes/inline.py)

- Eliminates call overhead and exposes callee bodies to intraprocedural optimization.
- **Preconditions:** Callee size $\le$ `INLINE_LIMIT = 50`, non-recursive (verified by cycle detection on the call graph), and non-generator.
- **Bottom-Up Ordering:** Inlines callees before callers so that leaf functions are absorbed first.
- **Arena Level Remapping:** Shifts callee arena levels above all caller levels to guarantee strict LIFO deallocation.

---

### Chapter 6.7: Call Devirtualization

*Primary Module:* [`src/ella/passes/direct.py`](./src/ella/passes/direct.py)

- Identifies global variables holding static top-level closures that are initialized once at startup.
- Converts indirect closure invocations (`call %closure(args)`) into direct relative function calls (`call_direct func_name(args)`).
- Unlocks subsequent inlining for devirtualized call sites.

---

### Chapter 6.8: Array Loop Fusion

*Primary Module:* [`src/ella/passes/fuse.py`](./src/ella/passes/fuse.py)

- **Defeating the Memory Wall (Wulf & McKee 1995):** In numerical pipelines (`a * 2.0 + b - c`), executing separate loops incurs severe memory bandwidth penalties by reading and writing intermediate temporary buffers to RAM.
- **Kernel Fusion:** Combines consecutive array operations into a single AVX2 SIMD loop. Intermediate results remain resident in 256-bit YMM vector registers, completely eliminating temporary buffer allocations.
- **Reduction Fusion:** Fuses horizontal reductions (`total`, `dot`) directly onto arithmetic chains, computing scalar values with zero heap array allocation.

---

## Chapter 7: Operational Semantics & the Reference Interpreter

*Primary Module:* [`src/ella/interpreter.py`](./src/ella/interpreter.py)

### 1. Dual Operational Roles

1. **The Ground-Truth Language Specification:** Defines the normative operational semantics of Ella before any IR lowering or native compilation occurs.
2. **The Stage -1 Metaprogramming Engine:** When compile-time splices (`$expr`) appear outside quotes, this interpreter executes them during compilation, producing residual syntax trees.

### 2. Big-Step Natural Semantics

Implements big-step evaluation ($\text{eval}(e, \sigma) \Downarrow (v, \sigma')$) recursively matching on `Expr` nodes. Control flow alterations (`return`, `break`, `continue`, `?`) are modeled via private Python exception signals unwinding the evaluation stack.

### 3. Cooperative Generator Coroutines

Generators are executed on cooperative host threads (`threading.Thread`), synchronizing control transfer tokens across `yield` and `next`.

---

## Chapter 8: Machine Code Generation & x64 Native Execution

*Primary Module:* [`src/ella/codegen.py`](./src/ella/codegen.py)

### 1. Unified 64-Bit Word Model

Every runtime Ella value occupies exactly one 64-bit word:

- `i64`: 64-bit two's complement integer.
- `bool`: `0` or `1`.
- `f64`: Raw IEEE-754 bits in general registers, moved into XMM registers for arithmetic.
- Boxed objects (strings, lists, arrays, tuples, closures): 64-bit pointers into arena memory.

### 2. Calling Conventions

- **Internal Ella Convention:** Uses a uniform stack-based convention that passes closure headers, caller result arena depths, arguments, and mutable argument arena depths.

- **System V AMD64 ABI:** Switches to standard C ABI register passing (`rdi, rsi, rdx, rcx, r8, r9`) with 16-byte stack alignment when invoking the C runtime engine.

### 3. Parallel Move Cycle Resolution

When transferring values to target registers during block branches, mutual register dependencies (e.g. `rax <- rbx` and `rbx <- rax`) are resolved using cycle detection and scratch register `r11`.

---

## Chapter 9: The Low-Level Runtime Engine & Memory Subsystem

*Primary Module:* [`src/ella/runtime.c`](./src/ella/runtime.c)

### 1. 32 TiB Virtual Memory Reservation

Ella does not use `malloc()` and `free()`. Instead:

- The runtime reserves up to 32 TiB of virtual address space upfront via `mmap(..., MAP_NORESERVE)`.
- Allocation is an ultra-fast bump pointer increment:

  ```c
  top = top + round16(size);
  ```

- Deallocation at scope exit is an $O(1)$ pointer reset. Pages exceeding 16 MiB are lazily released back to the OS via `madvise(MADV_DONTNEED)`.

### 2. Mitigating CPU Cache Conflict Thrashing

Set-associative L1/L2 caches map power-of-two address multiples to identical cache lines. If alternating loop arenas (`@k` and `@carryk`) started at exact power-of-two boundaries, ping-ponging values would thrash the identical cache sets. The runtime solves this by staggering each arena's base by a non-power-of-two prime offset:

```c
#define STAGGER ((size_t)65 << 12)
```

### 3. Generational Pool Handles

Long-lived objects reside in a `Pool[T]`. Handles pack:

```text
[ 24 bits: Pool ID | 20 bits: Generation | 20 bits: Slot Index ]
```

Dereferencing a handle performs an $O(1)$ generation check against the slot table, safely trapping Use-After-Free bugs without a garbage collector.

---

## Chapter 10: Multi-Stage Programming & Metaprogramming

*Primary Module:* [`src/ella/staging.py`](./src/ella/staging.py)

### 1. Multi-Stage Foundations (Taha & Sheard 1997)

Computations are organized into discrete integer stages:

- `quote e`: Lifts computation to stage $n+1$, producing a `Code[T]` value.
- `$c`: Drops computation to stage $n-1$, splicing computed residual code.
- `run(c)`: Compiles and executes code at the current stage.

### 2. Cross-Stage Persistence & Hygiene

- **Cross-Stage Persistence (CSP):** A variable bound at stage $n$ can be carried into stage $n+k$ as an embedded literal constant (`reify()`).

- **Macro Hygiene:** Quoted variables are automatically $\alpha$-renamed (`x` becomes `x.4`), preventing accidental variable capture.
- **Types as Values:** Types are first-class values (`Type[T]`), allowing compile-time reflection (`fields(t)`) and automatic code generation (`derive_eq`) without macros.

---

## Chapter 11: Static Type Systems, Unification & Bidirectional Checking

*Primary Module:* [`src/ella/checker.py`](./src/ella/checker.py)

### 1. Bidirectional Type Checking (Pierce & Turner 2000)

- **Synthesis Mode (`infer`):** Types are inferred upwards from sub-expressions.

- **Checking Mode (`check`):** Expected types are propagated downwards into untyped lambdas and literals.

### 2. First-Order Unification (Robinson 1965, Martelli-Montanari 1982)

Type placeholders (`?1, ?2`) are solved through unification:

- Solves type equations over structural terms.
- The **Occurs Check** prevents infinite, circular types ($?1 \doteq [?1]$).
- Unconstrained free type variables default safely to `()`.

### 3. Exclusivity for `mut` Parameters

Statically validates that mutable parameters do not alias any other argument within the same call, enforcing value-semantic independence at compile time.

---

## Chapter 12: Runtime Value Representation & Object Model

*Primary Module:* [`src/ella/values.py`](./src/ella/values.py)

- Maps Ella types to host Python objects in the interpreter while enforcing 64-bit bounded semantics (`I64_MIN..I64_MAX`).
- Implements explicit structural deep copying (`copy_value()`) to maintain pure value semantics.
- Implements structural equality protocols (`equal()`) delegating to user-defined `eq` methods.

---

## Chapter 13: Operator Semantics & Arithmetic Invariants

*Primary Module:* [`src/ella/operators.py`](./src/ella/operators.py)

- **Arithmetic Parity:** Fixed-width two's complement integer overflow trapping and truncation-towards-zero integer division matching x64 machine hardware.
- **IEEE-754 Standards:** Floating-point division and exponentiation returning signed infinities or NaNs without host exceptions.
- **SIMD Reductions:** Four-accumulator lane summation matching native AVX2 reduction orders.

---

## Chapter 14: Standard Runtime Primitives & the Host Environment

*Primary Module:* [`src/ella/library.py`](./src/ella/library.py)

- **$O(1)$ UTF-32 Code Point Indexing:** Strings are represented as 32-bit Unicode code points (`uint32_t`), making random indexing and slicing strictly $O(1)$.
- **Hermetic `Host` Context:** External I/O and process environments are encapsulated in a `Host` object, allowing deterministic test execution.

---

## Chapter 15: Module Systems, Namespacing & Link-Time Synthesis

*Primary Module:* [`src/ella/modules.py`](./src/ella/modules.py)

- **The AST Linker (`link`):** Traverses module dependency graphs, detects import cycles, and topologically sorts units.
- **Static Namespace Mangling:** Prefixes module-scoped symbols (`geo.area`, `geo.Circle`), unifying multi-file programs into a single flat AST for downstream compiler passes.
- Automatically injects the standard library Prelude (`lists`, `maps`, `strings`, `math`).

---

## Chapter 16: Pattern Matching Exhaustiveness & Maranget's Matrix Algorithm

*Primary Module:* [`src/ella/exhaustive.py`](./src/ella/exhaustive.py)

- **Luc Maranget (JFP 2007):** Represents match patterns as rows in a decision matrix.
- **Usefulness & Exhaustiveness:** Specializes matrices over constructor signatures to verify whether every input value is covered.
- **Witness Synthesis:** Generates concrete counterexample witnesses (e.g. `Some(None)`) when patterns are non-exhaustive, and flags dead pattern arms as compiler warnings.

---

## Chapter 17: SSA Register Allocation & Chordal Graph Coloring

*Primary Module:* [`src/ella/regalloc.py`](./src/ella/regalloc.py)

- **The Hack (2007) / Pereira & Palsberg (2005) Theorem:** Live ranges of programs in SSA form construct **Chordal Graphs**.
- **Linear-Time Optimal Coloring:** Chordal interference graphs can be colored in polynomial time via greedy assignment in dominator-tree order.
- Values spanning across calls receive callee-saved registers (`rbx, r12..r15`); short-lived values receive caller-saved registers (`rsi, rdi, r8..r10`); excess values spill to stack slots.

---

## Chapter 18: Resilient Parsing & IDE Static Code Intelligence

*Primary Module:* [`src/ella/analysis.py`](./src/ella/analysis.py)

- Implements fault-tolerant compiler services for interactive editors (LSP).
- **Lenient Lexing:** Heuristically patches unclosed strings and dangling delimiters without altering source span coordinates.
- **Partial Parsing:** Recovers from syntactic statement errors by inserting synthetic unit expressions `()` and synchronizing at statement boundaries.
- Extracts live type hover data, semantic tokens, and autocompletion symbols.

---

## Chapter 19: Native AOT Driver & Host Toolchain Orchestration

*Primary Module:* [`src/ella/native.py`](./src/ella/native.py)

- Ahead-Of-Time (AOT) native compilation driver.
- **Content-Addressed Runtime Caching:** Hashes `runtime.c` with compiler flags using SHA-256, caching compiled objects in `/tmp` to make repeated native builds instantaneous.
- Executes GCC to assemble Intel-syntax assembly and link the final native ELF binary.

---

## Chapter 20: Language Server Protocol (LSP) Architecture

*Primary Module:* [`src/ella/lsp.py`](./src/ella/lsp.py)

- Implements standard JSON-RPC Language Server Protocol over stdio (`pygls`).
- **Worker Process Isolation:** Runs static analysis and compile-time splices inside an isolated worker subprocess with timeouts, preventing infinite loops or `stdout` pollution from crashing the editor connection.
- Bridges UTF-16 editor offsets and 1-based Unicode character coordinates.

---

## Chapter 21: The IR Virtual Machine & Hardware Arena Simulator

*Primary Module:* [`src/ella/ir_interpreter.py`](./src/ella/ir_interpreter.py)

- Executes low-level three-address CFG IR directly.
- **Hardware Arena Simulation:** Tags runtime values with `Tag(depth, generation)` indicating the shortest-lived arena containing any part of the object.
- **Active Memory Invariants:** Dynamically traps Use-After-Free and dangling containment stores, serving as the ground-truth verification oracle for the compiler's memory placement decisions.

---

## Appendix: In-Depth Module Essays

These expanded notes complement the chapters above. Essays follow the chapter
order, with a link to the implementation module; each module retains a short
description in its source file.

### [`src/ella/__init__.py`](./src/ella/__init__.py)

Ella: a statically typed language for learning and fun.

Welcome to the Ella codebase! This codebase is structured to serve as an open,
readable textbook on practical compiler construction, intermediate representations
(IR), static single assignment (SSA), memory region inference without garbage
collection, and native x64 code generation.

#### The Compilation Pipeline

Ella compiles programs through a classical multi-pass architecture with distinct
intermediate abstractions:

```ascii
      Source Code (.ella)
             │
             ▼  [1. Lexical Analysis] (ella.lexer)
        Token Stream (with INDENT/DEDENT layout & Spans)
             │
             ▼  [2. Syntactic Analysis] (ella.parser using Pratt parsing)
        Abstract Syntax Tree (AST, defined in ella.syntax)
             │
             ▼  [3. Multi-Stage Expansion] (ella.staging: compile-time splices `$`)
        Expanded AST + Module Linker (ella.modules)
             │
             ▼  [4. Type Checking & Inference] (ella.checker)
        Typed AST (annotated with types, local bindings, exclusivity rules)
             │
         ┌───┴─────────────────────────────────────────┐
         │                                             │
         ▼ (Reference / Staging)                       ▼ [5. IR Lowering] (ella.lower)
    AST Interpreter (ella.interpreter)          Typed CFG IR (ella.ir)
    (executes stage -1 compile-time splices)           │
                                                       ├─► IR Interpreter (ella.ir_interpreter)
                                                       │   (simulates arenas & verifies safety)
                                                       │
                                                       ▼ [6. Optimization Pipeline] (ella.passes)
                                                Optimized SSA IR (mem2reg, moves, inline, etc.)
                                                       │
                                                       ▼ [7. Code Generation] (ella.codegen)
                                                x64 Assembly (.s) + C Runtime (runtime.c)
                                                       │
                                                       ▼ [8. Linker] (gcc via ella.native)
                                                Native ELF Executable
```

#### Suggested Reading Order (a Textbook Syllabus)

To study this compiler like a textbook, read the modules in this progression:

  1. `ella.lexer`        - Lexical analysis, finite state tokenization, Python-style
                           indentation (Peter Landin's off-side rule), and string interpolation.
  2. `ella.syntax`       - Abstract Syntax Tree (AST) design using modern Python dataclasses
                           and algebraic expression unions.
  3. `ella.parser`       - Top-Down Operator Precedence (Pratt parsing) for expressions,
                           combined with recursive descent for language statements.
  4. `ella.checker`      - Bidirectional type checking, constraint satisfaction, and static
                           exclusivity validation for mutable arguments.
  5. `ella.staging`      - Multi-stage metaprogramming (`quote`, `$`, and `run`), enabling
                           types as values and monomorphized generics.
  6. `ella.ir`           - Control-flow graph (CFG) design, three-address instructions, basic
                           blocks, and block parameters as SSA phi nodes.
  7. `ella.lower`        - AST-to-IR translation, closure conversion, and compiler-directed
                           memory region placement (simplified Tofte-Talpin inference).
  8. `ella.passes`       - Classical compiler optimizations: SSA promotion (`mem2reg`), copy
                           elimination (`moves`), inlining, and constant folding.
  9. `ella.interpreter`  - Operational semantics: AST tree-walking interpreter.
 10. `ella.ir_interpreter` - Executing the CFG IR directly with simulated hardware arenas.
 11. `ella.codegen`      - Machine code generation: x64 instruction selection, stack frame
                           layouts, and dominator-tree SSA register allocation (`regalloc.py`).
 12. `src/ella/runtime.c`- The low-level runtime engine: 64-bit virtual memory reservation,
                           bump allocation, cache-staggering, and generational pool handles.

#### External References & Classic Literature

This implementation builds on foundational concepts documented in classic computer
science literature:

- Parsing: Vaughan Pratt, "Top Down Operator Precedence", ACM POPL 1973.
- Layout: Peter J. Landin, "The Next 700 Programming Languages", CACM 1966 (The Off-Side Rule).
- SSA Form: Cytron, Ferrante, Rosen, Wegman, and Zadeck, "Efficiently Computing Static
    Single Assignment Form and the Control Dependence Graph", ACM TOPLAS 1991.
- SSA Block Arguments: Andrew Appel, "SSA is Functional Programming", ACM SIGPLAN Notices 1998;
    Richard Kelsey, "A Compiling with Continuations Transformation to SSA Form", LOPSTR 1995.
- Region Inference: Mads Tofte and Jean-Pierre Talpin, "Region-Based Memory Management",
    Information and Computation 1997.
- Compiler Architecture: Aho, Lam, Sethi, Ullman, "Compilers: Principles, Techniques,
    and Tools" (The Dragon Book), 2nd Edition, Addison-Wesley 2006.

### [src/ella/lexer.py](./src/ella/lexer.py)

Tokenizer: turns Ella source text into a flat list of tokens.

#### Theoretical Foundation: Regular Languages & Automata

Lexical analysis (tokenization) is the first phase of a compiler. Its purpose
is to convert an unstructured stream of Unicode characters into a structured
stream of atomic symbols called *tokens*.

In classical compiler theory (see Aho, Lam, Sethi, Ullman, "The Dragon Book",
Chapter 3), programming language lexemes are specified as regular expressions
and recognized by Deterministic Finite Automata (DFA). Ella implements this
as an efficient manual scanner in Python, utilizing two key principles:

- **Maximal Munch (Longest Match Rule):**
    When multiple token patterns could match starting at the current position,
    the scanner chooses the longest matching sequence of characters. For example,
    the character sequence `//=` is tokenized as a single compound assignment operator
    `OP("//=")`, rather than integer division `//` followed by `=` or slash `/` followed
    by `/=`. This is guaranteed by sorting `OPERATORS` longest-first.

- **Lookahead (k-character lookahead):**
    The lexer inspects characters ahead of the current scan pointer (`peek()`,
    `at()`) without consuming them, resolving lexical ambiguities (such as distinguishing
    `..` range from `.` member access, or floating-point `1.0` from tuple access `x.0`).

#### The Off-Side Rule & Indentation-Based Block Structure

Ella uses significant whitespace (indentation) to delimit blocks, following
Peter Landin's seminal 1966 paper "The Next 700 Programming Languages":

```quote
    "The off-side rule: Any non-whitespace line which is indented further
     than the first token of the current enclosing phrase is a continuation;
     any token to the left begins a new phrase."
```

Instead of forcing the parser to track character columns, the lexer maintains
an indentation stack (`self.indents = [0]`) and synthesizes virtual delimiter tokens:

- `NEWLINE`: Marks the end of a logical statement line.
- `INDENT` : Emitted when a line increases indentation beyond the current stack top.
               Acts syntactically like an opening brace `{`.
- `DEDENT` : Emitted (potentially multiple times) when a line's indentation drops,
               unwinding the stack. Acts syntactically like a closing brace `}`.

Implicit Line Joining inside Brackets:
Parentheses `(...)` and brackets `[...]` suspend the off-side rule. While
`self.brackets` is non-empty, newlines are treated as ordinary whitespace,
allowing multi-line expressions without explicit line-continuation backslashes.

#### String Interpolation & Lexer State Re-Entrancy

String interpolation (`"Result is: {{x + 1}}"`) requires interleaving string
literal scanning with full expression parsing.

Instead of parsing expressions inside the lexer, Ella deconstructs interpolated
strings into lexical pieces:
  `STRING_START` -> `STRING_TEXT("Result is: ")` -> `INTERP_START` ->
  `[Token(NAME, "x"), Token(OP, "+"), Token(INT, "1")]` ->
  `INTERP_END` -> `STRING_END`

This allows the Pratt parser (Chapter 3) to parse the interpolated expression
with the exact same grammar rules as any top-level expression.

#### Step-by-Step Worked Example

Source snippet:

```ella
if x > 0:
    print(x)
done()
```

Token stream generated:

```text
1. Token(KEYWORD, "if", Span(1, 1))
2. Token(NAME, "x", Span(1, 4))
3. Token(OP, ">", Span(1, 6))
4. Token(INT, "0", Span(1, 8), value=0)
5. Token(OP, ":", Span(1, 9))
6. Token(NEWLINE, "\n", Span(1, 10))
7. Token(INDENT, "", Span(2, 5))         <-- Synthesized block opener
8. Token(NAME, "print", Span(2, 5))
9. Token(OP, "(", Span(2, 10))
   10. Token(NAME, "x", Span(2, 11))
   11. Token(OP, ")", Span(2, 12))
   12. Token(NEWLINE, "\n", Span(2, 13))
   13. Token(DEDENT, "", Span(3, 1))         <-- Synthesized block closer
   14. Token(NAME, "done", Span(3, 1))
   15. Token(OP, "(", Span(3, 5))
   16. Token(OP, ")", Span(3, 6))
   17. Token(NEWLINE, "\n", Span(3, 7))
18. Token(EOF, "", Span(3, 7))
```

See also:

- docs/grammar.md §1 (Lexical structure)
- docs/grammar.md §2 (Layout & Indentation)

### [src/ella/syntax.py](./src/ella/syntax.py)

Syntax tree for Ella, as produced by the parser.

#### Concrete Syntax Tree (Parse Tree) vs. Abstract Syntax Tree (AST)

A Concrete Syntax Tree (Parse Tree) records every character, semicolon,
parenthesis, keyword, and layout token matched by the grammar productions.
While useful during parsing, parse trees are bloated and clumsy for semantic
analysis and optimization.

An Abstract Syntax Tree (AST) strips away syntactic sugar and formatting:

- Punctuation (commas, colons, indentation tokens) is eliminated.
- Operator precedence has already been resolved into tree hierarchy: in `1 + 2 * 3`,
    the multiplication node is explicitly nested as the child of the addition node.
- Surface syntax is desugared: for example, the pipeline operator `x |> f(y)` is
    desugared into the canonical function call `f(x, y)` during AST creation.
- Every node retains its source `Span` (line and column) so that later passes
    (type checker, borrow checker, runtime unwinder) can report precise, friendly
    diagnostics.

#### Expression-Oriented Language Design

Unlike statement-oriented languages (C, Java, Python) where statements (`if`, `while`,
assignments) produce no values and cannot be used inside expressions, Ella is
strictly *expression-oriented* (like Rust, OCaml, and Scala):

- An `if-then-else` block is an expression that yields a value:
        `let status = if score > 50 then "pass" else "fail"`
- Blocks `{ ... }` evaluate to their final expression, or unit `()` if empty.
- Loops evaluate to `()` (or the value yielded by a `break expr`).
- Only booleans are truthy (no truthy integers, non-empty lists, or nulls).

#### Algebraic Data Types (ADTs) in Modern Python

The AST is modeled using modern Python (PEP 695 type aliases, dataclasses with `slots=True`,
and structural pattern matching):

- **Product Types (Records):** Defined as frozen dataclasses (`IntLit`, `Binary`, `If`).
    `slots=True` ensures compact memory layout and fast attribute access; `frozen=True`
    guarantees AST immutability between passes.
- **Sum Types (Discriminated Unions):** Grouped into three primary algebraic unions:
  - `type TypeExpr = ...`: Surface syntax type annotations (e.g., `[i64]`, `(T) -> U`).
  - `type Pattern  = ...`: Destructuring patterns in `let` and `match` expressions.
  - `type Expr     = ...`: The universal expression tree of Ella programs.

#### Canonical S-Expression Serialization (`show()`)

To make syntax trees human-readable, testable, and diff-friendly, Ella defines
an S-expression printer (`show()`):

- Source code: `let x = 1 + 2 * 3`
- AST Nodes:   `Let(names=[Binding('x')], init=Binary('+', IntLit(1), Binary('*', IntLit(2), IntLit(3))))`
- S-Expression: `(let [x] (+ 1 (* 2 3)))`

S-expressions allow snapshot testing without fragile object comparisons or verbose JSON.

### [src/ella/parser.py](./src/ella/parser.py)

Pratt parser: turns tokens into the syntax tree defined in ella.syntax.

#### Theoretical Foundation: The Limits of Recursive Descent

In traditional recursive descent parsers (Dragon Book, Chapter 4), parsing expressions
with many operator precedence levels requires creating a separate grammar production
for every level of precedence:

```ebnf
expr     -> term (('+' | '-') term)*
term     -> factor (('*' | '/') factor)*
factor   -> power ('**' power)*
power    -> unary
...
```

This creates dozens of mutually recursive functions, deep call stacks, and severe
code bloat.

In 1973, Vaughan Pratt published "Top Down Operator Precedence" (ACM POPL), introducing
an elegant, data-driven alternative. Instead of encoding precedence in grammar non-terminals,
operators are associated with numeric *binding powers* (precedence levels):

```text
Higher number = binds more tightly (higher precedence)
e.g., '*' (14) binds tighter than '+' (13)
```

#### Null Denotation (Nud) vs. Left Denotation (Led)

In Pratt's formalism:

- **Null Denotation (`nud`, or prefix):**
    What the token does when there is nothing on its left.
    Examples: integer literals (`42`), identifiers (`x`), unary operators (`-x`, `not b`),
    grouping parentheses (`(x + 1)`), and keyword expressions (`if`, `match`).
    Implemented in Ella as `self.parse_prefix()`.

- **Left Denotation (`led`, or infix/postfix):**
    What the token does to combine with an expression already parsed on its left (`left`).
    Examples: binary arithmetic (`+`, `-`, `*`), comparisons (`==`, `<`), assignments (`=`),
    member access (`left.field`), indexing (`left[i]`), and function calls (`left(arg)`).
    Implemented in Ella as `self.parse_infix()`.

#### The Algorithm: `parse_expr(min_prec)`

The heart of Pratt parsing is a single while-loop:

  1. Call `parse_prefix()` to parse the initial expression (`left`).
  2. Inspect the next token. If it is an infix/postfix operator whose binding power
     is greater than or equal to `min_prec`:
       - Consume the operator.
       - Parse the right-hand side recursively with a new binding power:
           - Left-associative  (`+`, `-`): recurse with `prec + 1` (rejects equal operator).
           - Right-associative (`**`):     recurse with `prec` (accepts equal operator).
       - Construct a new `left` node combining old left and right.
  3. When an operator with binding power < `min_prec` is seen, break and return `left`.

#### Step-by-Step Trace: `1 + 2 * 3 - 4`

Precedence: `+` = 13 (left-assoc), `*` = 14 (left-assoc), `-` = 13 (left-assoc).

  Step 1: `parse_expr(min_prec=LOWEST)`
          - `parse_prefix()` consumes `1` -> left = 1
          - Sees `+` (prec 13 >= 0): consumes `+`
          - Recurses: right = `parse_expr(min_prec=14)` (since `+` is left-assoc, 13 + 1)
              * In inner `parse_expr(14)`:
                  - `parse_prefix()` consumes `2` -> left = 2
                  - Sees `*` (prec 14 >= 14): consumes `*`
                  - Recurses: right = `parse_expr(min_prec=15)`
                      - `parse_prefix()` consumes `3` -> left = 3
                      - Next token is `-` (prec 13). Since 13 < 15, inner loop terminates!
                      - Returns 3.
                  - Combines: left = `Binary('*', 2, 3)`
                  - Next token is `-` (prec 13). Since 13 < 14, loop terminates!
                  - Returns `Binary('*', 2, 3)`.
          - Combines: left = `Binary('+', 1, Binary('*', 2, 3))`
          - Loop continues at top level! Next token is `-` (prec 13 >= 0).
          - Consumes `-`.
          - Recurses: right = `parse_expr(min_prec=14)` -> parses `4`.
          - Combines: left = `Binary('-', Binary('+', 1, Binary('*', 2, 3)), 4)`
          - EOF reached -> Returns `((1 + (2 * 3)) - 4)`.

#### Hybrid Architecture in Ella

Ella combines Pratt parsing for expressions with recursive descent for structured
declarations (`parse_let`, `parse_struct`, `parse_enum`, `parse_import`).
See docs/grammar.md §8 and §12 for the exact grammar specification.

### [src/ella/ir.py](./src/ella/ir.py)

The intermediate representation (IR): a typed control-flow graph.

#### The Role of an IR in Modern Compilers

High-level ASTs are deeply nested and rich in language-specific syntax (loops,
pattern matching, list comprehensions, methods). Machine code, by contrast, is flat:
linear memory, registers, conditional branches, and arithmetic ops.

The Intermediate Representation (IR) bridges this semantic chasm. By translating
Ella's AST into a typed, control-flow graph (CFG) representation:

- Control-flow becomes explicit (basic blocks and branch terminators).
- Dataflow dependencies become explicit (SSA values and dominance).
- Memory allocation regions and object copies become explicit operations.
- Target-independent optimization passes (mem2reg, copy-elimination, inlining,
    constant folding) can operate uniformly on a single clean data structure.

#### Control-Flow Graphs (CFGs) & Basic Blocks

In compiler theory (Aho et al., Dragon Book Chapter 8), a *Basic Block* is a maximal
linear sequence of instructions satisfying two strict invariants:

  1. Single Entry: Execution enters strictly at the first instruction (no jumps
     into the middle of a block).
  2. Single Exit: Execution leaves strictly at the last instruction via a *Terminator*
     (no jumps out from the middle).

Ella's terminators enforce explicit control flow:

- `jump target(args)`         - Unconditional branch with arguments.
- `branch cond, then, else`   - Conditional two-way branch.
- `return value`              - Exits the function and yields execution to caller.
- `yield value, target`       - Suspends generator coroutine and yields to caller.
- `trap "msg", value`         - Halts with a checked runtime error or panic.

#### Static Single Assignment (SSA) & Block Parameters vs. Phi-Nodes

A program is in SSA form (Cytron et al., 1991) if:

  1. Every value `%n` is defined by exactly one instruction.
  2. Every use of `%n` is strictly dominated by its definition.

The Classical Phi-Node Dilemma:
In traditional SSA (such as LLVM IR), when multiple control paths converge, a special
`phi` instruction is used:

```text
%result = phi [ %val_then, block_then ], [ %val_else, block_else ]
```

However, phi-nodes introduce subtle compiler bugs: they must be placed strictly at the
head of a block, have order-dependent semantics during parallel copies, and require
critical edge splitting.

Ella's Choice: Block Parameters (Appel 1998, Swift SIL, Cranelift, MLIR):
Following Andrew Appel's insight that "SSA is Functional Programming" (1998), Ella
replaces phi-nodes with explicit block arguments, making blocks look like local functions:

```text
then_block:
    jump join_block(%val_then)
else_block:
    jump join_block(%val_else)
join_block(%result: i64):
    ...
```

Block parameters make SSA completely clean: values are simply arguments to branch targets.

#### Three-Address Code & Value Flow

Instructions are *three-address code*: each computes one primitive result from at most
two operands (`%3: i64 = add %1, %2`). Every instruction produces an immutable `Value`
tagged with an explicit Ella type.

#### Region-Annotated Memory Architecture

In Ella, heap-like boxed objects (lists, arrays, strings, tuples, closures) live in
compiler-directed *arenas* (docs/memory-model.md §5, §14).
Every allocation instruction specifies its target *Region*:

- `@0, @1, ...`   - Scope levels (0 = function body, 1 = outermost loop, etc.).
- `@result`       - The caller's result arena (enables zero-copy returns).
- `@mut0, ...`    - The arena of a caller's mutable argument.
- `@carry1, ...`  - The alternating ping-pong arena for loop carried state.
- `@global`       - The top-level process arena.

The lowering (ella.lower) turns a checked syntax tree into this IR, and
ella.ir_interpreter runs it. The passes in ella.passes optimize it, and
ella.codegen compiles it to x64.

#### Structure

A `Module` holds functions. Each `Function` is a list of basic blocks, the
first being its entry. A `Block` is a straight-line list of instructions with
no jumps in or out of the middle, ended by exactly one *terminator* that says
where control goes next:

```text
jump target(args)         continue in another block
branch cond, then, else   continue in one of two blocks, by a bool
return value              leave the function
yield value, target       (in a generator function) give value to `resume`'s
                          caller; the next `resume` carries on at target
trap "message", value     stop the program with a run-time error
trap error e              stop the program with the Error e that `main` returned
```

Instructions are *three-address code*: each does one small thing to
operands that are already computed, like `%3: i64 = add %1, %2`.

#### Values

Each instruction that produces something defines a new `Value`, written `%n`,
which has a type and is never changed afterwards. Values flow between blocks
in two ways:

- A value can be used in any block that the defining block *dominates*
  (every path from the entry to it passes through the definer).
- Where paths join, such as after an `if`, the result arrives as a *block
  parameter*: `jump join(%4)` passes `%4` to `join(%5: i64)`. Block
  parameters do the job of SSA "phi" nodes.

#### Variables

Ella variables can change, but values can't. For now, each variable lives
in a named, typed *local slot* of its function, read with `load` and written
with `store`. The mem2reg pass (ella.passes.mem2reg) promotes slots to
values, which is what "SSA form" means; that is easier to do as an
optimization than in the lowering.
Top-level variables are *globals*, read and written by name. Reading a
global before it is set is a run-time error.

#### The IR Memory Model

Lists, arrays, strings, tuples, ranges, enum values and closures are *boxed*: a
value is a reference to an object in memory. (i64, f64, bool and () are
not.) Ella has value semantics, so no two variables may share an object
that can change; the only objects that ever change are lists, through
`set_index` and `append`, arrays, through `set_index`, and structs, through `set_field`. The lowering makes this safe with explicit `copy`
instructions wherever a value gets a second owner (docs/memory-model.md §3).

Every boxed object lives in an *arena*, and every instruction that makes
one names the arena, its *region*, written after an `@`:

```text
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
```

`reset k` frees levels k and up when control leaves a loop iteration, and
returning frees all of the function's levels.

#### Generators in the IR

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

#### Operations

```text
const c                      a literal: i64, f64, bool, str or () (strings are static)
load slot, store slot, v     read or write a local variable
global_get name, global_set name, v
add sub mul div floordiv mod pow    arithmetic (`div` always gives f64;
                             `add` also joins strings and lists). `add`, `sub`,
                             `mul`, `div` and `neg` also work on arrays item by item
                             (two of the same shape, or an array and a number on
                             either side), giving a new array in the instruction's
                             region; native code runs them as AVX2 loops
bitand bitor bitxor shl shr  bitwise operations, on i64
neg not bitnot               unary operations
eq ne lt le gt ge            comparisons, giving bool
contains item, container     `in` for lists, strings and ranges
range lo, hi                 `lo..hi`; `range_incl` for `lo..=hi`
tuple a, b, ...              build a tuple; `tuple_get t, n` reads item n
list a, b, ...               build a list
index c, i                   item i of a list, string (a 1-char string) or range, or
                             item i of an array's items (in row-major order),
                             which must be in range: `array_offset` or a loop
                             over the array gives i
array_offset a, i, j, ...    where item (i, j, ...) is in array a's items, checking
                             that there is an index for each dimension, in range
array_slice a, i, r, ... @r  a new array of a's items at the indexes, an i64 or a range
                             for each dimension (a range keeps its dimension)
fused a, b, ... [steps] @r   several of `add`, `sub`, `mul`, `div` and `neg` on arrays
                             (and numbers) in a row, as one (made by ella.passes.fuse),
                             maybe ending with `total` or `dot` (then giving a number,
                             and @r is for arrays the steps one by one would make):
                             each step works on the operands (a, b, ...: 0, 1, ...)
                             and the results of earlier steps (s0, s1, ...), and has
                             the span of the operation it was; the last step's array
                             is the result. Natively one loop goes over all the items,
                             with no arrays in between; it gives what the steps one by
                             one would give, errors and all. A step marked `keep`
                             gives its array too, as an extra result (`outs`, in its
                             region of `out_regions`), for code after it
slice c, r @r                a view of items r.start up to r.stop of a list or string:
                             a new header in @r that shares c's items (or characters)
set_index xs, i, v @r        replace item i of list xs in place; xs lives in region r
                             (or of an array's items, i given by `array_offset`)
append xs, ys @r             xs with copies of ys's items added, growing xs in place
                             if it can; xs lives in region r and isn't used again
copy v @r                    a deep copy of v in region r
move v @r                    v, which can't be copied, moved to region r with
                             its finalizers (a returned local variable)
reset k                      free the function's scope levels k and up, putting the
                             two arenas of each loop there that carries values back
                             in order (`swap` exchanges them)
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
len c                        length of a list, string or range, an array's number of
                             items, or a pool's or arena's count
builtin a, ... @r            a library function (in `name`), such as split(a, b),
                             whose result is new objects in @r (ella.library), or
                             for those in VIEWS views of a's characters; abs, min,
                             max and the array functions take i64s, f64s or f32s (or
                             arrays of them), and `array` its result's item type
to_text v                    the text form of any value
print v                      write the text form of v and a newline
eprint v                     the same, to standard error
variant Name(a, ...)         make an enum value or struct (registering its finalizer)
is_variant v, Name           does v hold that variant?
variant_field v, Name.n      field n of v, which holds that variant
set_field v, x @r            change field n (in `index`) of struct v in place
closure f(a, ...)            a function value: IR function f with captured values;
                             one that captures nothing is static, with no region
call f(a, mut b @r, ...)     call a function value; the result goes in the call's
                             region. A `mut` argument b, which lives in region r,
                             is passed in and its new value comes back as an extra
                             result: `%5: (), %6: [i64] = call %1(mut %2 @0) @0`.
call_direct f(a, ...)        the same for IR function f (in `name`), which captures
                             nothing: `call_direct fib(%3) @0`
enter                        an inlined call starts: it counts toward the call depth,
                             and stops the program if that is too deep, as `call` does
leave                        an inlined call ends
assert c, "message"          stop with the message if c is false
```

A function with `mut` parameters returns their final values along with its
result: `return %9, mut %7`.

Inlining (ella.passes.inline) puts a copy of a function's body where it is
called. Its scope levels become levels of the caller above all of the
caller's own, so they are freed first, as a callee's arenas are; a `reset`
frees them where the copy returns, and `enter` and `leave` keep counting
the call toward the call depth limit.

Operations that fail at run time (overflow, an index out of range, a failed
assert, ...) carry the source span to report. A run-time error also reports
the calls in progress, each from its instruction's span and `within`. One
at Span(0, 0) (in the function made for a library function used as a
value) is reported at the innermost call instead (`EllaError.traced`).

### [src/ella/lower.py](./src/ella/lower.py)

Lowering: turns a type-checked syntax tree into IR (see ella.ir).

#### The Lowering Pass: Flattening Trees to Control Flow

The Abstract Syntax Tree is recursive and nested: expressions contain sub-expressions,
which contain conditional blocks, which contain loops. The CPU, however, executes flat
sequences of instructions with jumps.

The *Lowering* pass (also known in compiler literature as intermediate code generation
or desugaring; Dragon Book Chapter 6) walks the typed AST and emits flat three-address
instructions into the *current basic block*. Whenever control flow diverges (an `if`,
`match`, `loop`, or short-circuit boolean), lowering:

  1. Closes the current block with a branch terminator (`branch` or `jump`).
  2. Allocates new basic blocks for the divergent paths.
  3. Recursively lowers each branch into its respective block.
  4. Merges control flow into a join block using block parameters (SSA block arguments).

#### Closure Conversion

Ella supports first-class functions and closures (lambdas capturing local variables).
Hardware CPUs cannot execute "closures" directly—they can only jump to static code addresses.

Lowering eliminates closures via *Closure Conversion*:

- Every lambda is extracted into a standalone, top-level IR function.
- Any local variables the lambda accesses from outer scopes (free variables) are
    converted into explicit leading parameters of the IR function.
- At the definition site, a `closure` instruction allocates a closure object pairing
    the function pointer with the captured values.
- When called, the runtime unpacks the captured values and prepends them to the call arguments.

#### Memory Region Inference (Simplified Tofte-Talpin)

Ella manages dynamic memory **without a garbage collector** and **without lifetime annotations**.
It achieves this through compiler-directed region inference (inspired by Mads Tofte &
Jean-Pierre Talpin, "Region-Based Memory Management", Information & Computation 1997).

The Core Invariant:
Every allocated object belongs to an *Arena* (Region) tied to a scope level:

- Scope `@0`: The function frame.
- Scope `@1, @2, ...`: Nested loop bodies.
- Scope `@result`: The caller's arena.
- Scope `@mut0`: The arena of a mutable caller variable.

Destination-Driven Lowering:
Instead of allocating objects locally and copying them out on return, lowering passes a
*destination region* down into expression compilation:
    `return [1, 2, 3]`
The list constructor is told that its destination is `@result`. It allocates directly in
the caller's arena. When the function returns, no data needs to be copied!

#### Value Semantics & Copy Insertion

Ella has pure value semantics (no shared mutable state). Lowering tracks `Info` for
every value:

- `borrows`: Which variables this value shares memory with.
- `lives`: The shortest-lived region this value depends on.

The lowering automatically inserts an explicit `copy` instruction whenever:

  1. Aliasing could observe mutation: A list/array/struct borrows from a variable
     that is later mutated in-place (`xs[i] = v`, `xs += ys`).
  2. Escape / Use-After-Free: A value would outlive its enclosing arena (e.g. storing
     a loop-local temporary into an outer variable).

#### Taming Region Bloat: Alternating Loop Arenas

The fatal flaw of classic region systems (like MLKit) was "region bloat": in a loop
`for i in 0..n: acc = acc + [i]`, if `acc` lives in the outer scope, every iteration
allocates a new version in the outer arena, leaking memory until the entire function returns.

Ella completely solves region bloat:

- Carried variables are detected automatically (`carried_by`).
- Loops that carry outer variables allocate two alternating scope arenas: `@k` (current iteration)
    and `@carryk` (carried state).
- At each iteration boundary (`swap k`), the current iteration's arena becomes the carried
    arena, and the old carried arena is completely freed!
- At most TWO versions of carried state are ever alive, reducing memory from O(N) to O(1).

#### Memory Placement and Copies

The lowering also decides where every object lives and when values are
copied (docs/memory-model.md §3, §5, §6). Each function body is scope level
0, and each loop adds a level for its iterations: the loop frees that level
(`reset`) when an iteration ends. Every expression is lowered with a
*destination*, the region its value will end up in, and objects are built
directly there: `let xs = [1, 2]` makes the list in the region of `xs`, and a
returned list is made in the caller's region (`@result`).

Values are references to objects, and reading a variable doesn't copy. So
for each boxed value, the lowering tracks two things (`Info`):

- what it *borrows*: the variables whose objects it may share. A value read
  from `xs`, an item of it, or a slice of it (a *view* that shares its
  items, docs/memory-model.md §18) borrows `xs`. A new object borrows nothing.
- how long it *lives*: a region that every object in it outlives.

A value is copied when it is stored (in a variable, a list, a returned
value, ...) if either check fails:

- Sharing would be visible. Only lists (and arrays and structs, which
  work alike) ever change, and only in place, through `xs[i] = v` and
  `xs += ys`. A value with lists in it is copied
  if it borrows from a variable that is changed in place, or is stored into
  a `mut` variable, which may change it in place itself.
- It would outlive its objects: it lives in a region that doesn't outlive
  the one it is stored in. A value built inside a loop and stored in a
  variable declared outside it is an example.

One more case needs a copy. Reading `xs` and then changing it in place
before the read value is used, as in `xs + [(xs[0] = 5)]`, would show the
change. Ella's semantics are that a read gives the value at the time of
reading, so `protect` copies a read value if a later part of the same
expression changes its variable in place.

And one case needs none: `x = y`, where y is a variable of the same block
that nothing reads afterwards, moves y's value into x instead of copying
it, since no one can see the sharing.

#### Loops That Carry Values

A loop that gives a variable declared outside it a new value on every
iteration would pile up one version per iteration in the variable's arena
("region bloat", docs/memory-model.md §6). Instead, such a loop *carries*
the variable (`carried_by` decides which): inside the loop it lives in a
slot of its own, whose values are in the loop's arenas. New values are made
in the iteration's level, @k. At the end of an iteration (the *latch*),
a variable that still holds a value from the last iteration, in @carryk,
is copied into @k (`carry`), and `swap k` frees @carryk and makes @k the
carried arena. When the loop ends, each variable is copied back to its own
region. So the loop holds at most two versions of each variable.

`for i in 0..n: s = s + "x"` becomes, roughly:

```text
store s.1, (load s)                 # the loop's slot for s
for:
  swap 1
  ...
body:
  %1 = add (load s.1), "x" @1       # the new value, in the iteration's arena
  store s.1, %1
next:
  store s.1, (carry (load s.1) @1)  # copied only if it is still in @carry1
  jump for
exit:
  store s, (copy (load s.1) @0)
  reset 1
```

### [`src/ella/passes/__init__.py`](./src/ella/passes/__init__.py)

Optimization passes over the IR.

#### The Optimization Pipeline: Iterative Fixed-Point Rewrites

In compiler engineering (Muchnick, "Advanced Compiler Design and Implementation",
Chapters 8-12), optimization is rarely a single monolithic algorithm. Instead, it is
structured as a sequence of small, decoupled *transformation passes*.

Each pass specializes in one clean rewrite. Because passes exhibit strong *synergy*
(e.g., constant folding makes a branch constant -> CFG simplification eliminates the
dead branch -> Dead Code Elimination removes instructions in that dead branch ->
moves pass notices the original object now has only one user and elides the copy),
cleanup passes are run iteratively until a *fixed point* is reached (no pass makes
further changes) or `MAX_ROUNDS` is exceeded.

#### The Golden Invariant: Preserving Observable Semantics & Traps

A compiler optimization pass is *sound* if and only if it preserves all observable
program behavior byte-for-byte.

Crucially in Ella, this includes **runtime errors and traps**:

- An expression that might trigger a divide-by-zero trap (`x / 0`), an array
    out-of-bounds error, or integer overflow must NEVER be speculatively removed
    or folded away if that error would have occurred during normal execution.
- Side effects (I/O, printing, mutable global state) must maintain exact sequencing.

#### Continuous Invariant Verification

Writing compiler optimizations is notoriously bug-prone. Ella enforces correctness by
running the IR verifier (`verify()`, defined in `ella.ir`) before and after *every single
pass*. If a pass violates SSA dominance, introduces mismatched types, or leaves a dangling
branch edge, the compiler crashes immediately at that exact pass with a full diagnostic.

#### The Optimization Pass Catalog

The passes implemented in this package:

- `mem2reg`  - SSA construction: promotes mutable stack/local slots (`load`/`store`)
                 to pure SSA values and block arguments.
- `direct`   - Direct call devirtualization: resolves known function references into
                 direct calls, avoiding indirect closure invocation overhead.
- `moves`    - Copy elimination: analyzes liveness and transforms semantic `copy`
                 instructions into zero-cost moves whenever the original value is dead.
- `fold`     - Constant folding & propagation: evaluates constant expressions at
                 compile time and simplifies algebraic identities (e.g., `x + 0` -> `x`).
- `simplify` - CFG simplification: merges adjacent basic blocks, removes unconditional
                 trampoline jumps, and deletes dead code blocks.
- `dce`      - Dead Code Elimination: sweeps instructions whose results are unused
                 and have no side effects.
- `inline`   - Function inlining: replaces calls to small, non-recursive functions with
                 their bodies, bottom-up in the call graph.
- `fuse`     - Array loop fusion: combines consecutive element-wise operations on arrays
                 into a single AVX2 SIMD loop, minimizing memory bandwidth round-trips.

### [src/ella/passes/mem2reg.py](./src/ella/passes/mem2reg.py)

mem2reg: promote local slots to SSA values.

#### Motivation: Memory Slots vs. Pure SSA Values

When `ella.lower` first emits IR, it takes a conservative, simple approach:
every local variable is placed in a mutable stack slot. Statements like `x = x + 1`
are emitted as:

```text
%1: i64 = load x
%2: i64 = add %1, %3
store x, %2
```

While trivial to generate, stack slots severely obstruct optimization:

- Optimizations cannot see through `load` and `store` without expensive alias analysis.
- Registers cannot easily be allocated across basic block boundaries for stack slots.

The `mem2reg` pass (Memory-to-Register) promotes slots to pure SSA values. After promotion,
loads and stores vanish completely: `%2` simply becomes the value representing `x` from
that point forward.

#### The Evolution of SSA Construction Algorithms

- The Classical Cytron et al. Approach (1991):
    For decades, SSA construction required:
      1. Computing the dominator tree.
      2. Computing iterated dominance frontiers ($IDF$).
      3. Placing $\phi$-nodes at dominance frontiers.
      4. Renaming variables during a recursive dominator-tree traversal with variable stacks.
    This algorithm is mathematically complex, difficult to teach, and notoriously hard
    to implement correctly.

- The Modern Braun et al. Approach (CC 2013):
    Matthias Braun, Nicole Buchwald, Sebastian Hack, Roland Leißa, Christoph Mallon,
    and Andreas Zwinkau published "Simple and Efficient Construction of Static Single
    Assignment Form" (Compiler Construction 2013).
    Ella implements this algorithm adapted for block arguments:
  - Variable lookup is local and recursive: To find a slot's current value at
        instruction $I$, search backwards in the same basic block for the most recent `store`.
  - If no `store` is found in the block, ask the block's *predecessors*:
    - Exactly one predecessor: Directly take the value from the predecessor's end.
    - Multiple predecessors: Add a new block argument (parameter) to the block,
            and recursively query each predecessor to pass its value into the jump instruction.
  - Cyclic control flow (loops) is handled cleanly by creating a placeholder parameter
        before recursing into loop predecessors, breaking infinite loops.

#### Cleanup: Removing Trivial Block Parameters

Because Braun's algorithm creates block parameters whenever multiple predecessors exist,
it may synthesize "trivial" parameters—parameters that receive the exact same value
along every incoming branch (or only from themselves in a loop).

`remove_trivial_params` detects these redundant parameters, replaces all their uses
with the unique incoming value, and deletes the parameter from the block signature
and jump sites.

#### Critical Edge Splitting

An edge from block $A$ to block $B$ is *critical* if $A$ has multiple successors
(e.g., a conditional branch) AND $B$ has multiple predecessors (a join point).
Critical edges create ambiguity for where phi-values / block arguments should be passed.
`split_critical_edges` inserts an empty synthetic basic block along any critical edge,
guaranteeing a clean landing pad for argument passing.

### [src/ella/passes/moves.py](./src/ella/passes/moves.py)

Moves: remove copies of values that nothing else uses.

#### Philosophical Foundation: Moves as Compiler Optimizations

In systems languages with affine type systems (like Rust) or explicit rvalue
references (like C++11), "moving" is a primary language semantic rule:

- Moving transfers ownership of resources.
- Reading from a moved-from variable produces a compile-time error ("use of moved value"
    in Rust) or leaves the object in an unspecified "valid but unspecified" state (in C++).

Ella inverts this relationship:

- **Semantics:** Everything is a pure, independent value. Deep copies are the mental model.
- **Optimization:** Moves are an *invisible compiler optimization*. When the compiler
    can prove that an original value will never be read again after being copied, it
    silently elides the deep copy and transforms it into a zero-cost pointer move.
- **Pedagogical Benefit:** Programmers get the clean mental model of pure values
    without paying the performance tax of redundant heap copying, and without learning
    complex lifetime annotations or borrow checkers!

#### Safety Invariants for Copy Elision

Under what mathematical conditions can `%2 = copy %1 @region` be safely replaced by `%1`?
This pass proves three formal conditions:

  1. **Strict Liveness / Single-Use Invariant:**
     The copy instruction `%2 = copy %1` is the *sole* user of value `%1` in the
     entire function (`uses[original.id] == 1`). If `%1` were used anywhere else,
     a real copy would be required to prevent mutating aliasing.

  2. **Dominance & Post-dominance Ordering:**
     In the control-flow graph (Dragon Book Chapter 9):
       - The definition of `%1` must *dominate* the copy instruction `%2` (meaning every
         execution path leading to `%2` must first execute `%1`).
       - The copy instruction `%2` must *post-dominate* `%1` (meaning every path leaving
         `%1` towards function exit must inevitably execute `%2`).
       - The path between `%1` and `%2` must not cross a loop boundary (a cycle), which
         could cause `%1` to be reused across iterations.

  3. **Object Freshness (No Hidden Internal Aliases):**
     The source object must be freshly constructed (a call result, string split,
     fresh list literal, range, etc.) rather than an interior alias borrowing from
     an outer variable.

#### Destination Region Promotion

When a copy is successfully transformed into a move:

- `%2` is replaced with `%1` across all subsequent instructions.
- The original allocation instruction that created `%1` is modified so that its
    destination region is updated to the copy's target region.
- Result: The object is constructed directly in its final resting place, achieving
    true zero-copy execution.

### [src/ella/passes/fold.py](./src/ella/passes/fold.py)

Constant folding and propagation: work out at compile time what doesn't depend on run time.

#### Theoretical Foundation: Constant Propagation (Kildall 1973)

In compiler optimization (Gary Kildall, "A Unified Approach to Global Program
Optimization", ACM POPL 1973), constant propagation is modeled as abstract
interpretation over a flat semi-lattice for each variable:

```text
Top: Undetermined / not yet executed
 │
Constant: Known compile-time literal value
 │
Bottom: Over-determined / dynamic variable value
```

In Ella's SSA representation, propagation is straightforward:

- **Constant Folding:** When all operands of a pure three-address instruction
    are statically known `const` values, the compiler computes the result at
    compile time and replaces the instruction with a single `const` result.
- **Block Parameter Propagation:** If a basic block parameter receives the exact
    same constant value along all incoming predecessor edges, the parameter is replaced
    by that constant across the entire block body.
- **Trapping Invariant:** If folding an operation would fail (e.g., `1 // 0` or
    integer overflow), the compiler leaves the instruction unfolded so that the
    proper dynamic exception will fire at the exact runtime moment it is executed.

### [src/ella/passes/simplify.py](./src/ella/passes/simplify.py)

Simplify the control-flow graph: fewer, bigger blocks, with no detours.

#### Motivation: The Accumulation of CFG Detours

Front-end lowerings and upstream optimization passes inevitably generate redundant
control-flow overhead:

- Lowering leaves empty join points and fall-through trampoline jumps.
- Constant folding converts conditional branches into unconditional jumps, leaving
    the unchosen branch completely unreachable.
- Dead branches leave behind isolated "island" blocks with zero incoming edges.

#### The Four Classical CFG Canonicalization Tidy-Ups

`simplify_cfg` runs four graph transformations iteratively until reaching a fixed point:

  1. **Branch Folding:**
     If a `branch cond, then, else` tests a compile-time boolean constant (from `fold`),
     or if `then` and `else` point to the identical target block, replace the branch
     with an unconditional `jump`.
  2. **Unreachable Block Elimination:**
     Compute the set of blocks reachable from function `entry` via graph traversal;
     permanently delete all unreachable blocks and discard their instructions.
  3. **Jump Threading (Trampoline Elimination):**
     If block $B$ contains only an unconditional jump to block $C$, rewrite all edges
     targeting $B$ to jump directly to $C$, eliminating useless branch hops.
  4. **Block Coalescing / Merging:**
     If block $A$ jumps unconditionally to block $B$, and $B$ has $A$ as its *sole*
     predecessor, append $B$'s instructions directly into $A$ and remove $B$.
     Block parameters in $B$ are replaced by the corresponding jump arguments from $A$.
     This increases basic block length, expanding the scope for local instruction
     scheduling and register allocation.

### [src/ella/passes/dce.py](./src/ella/passes/dce.py)

Dead-code elimination: remove instructions whose results are never used.

#### Theoretical Foundation: Dead Code vs. Unreachable Code

In compiler optimization (Muchnick Chapter 18):

- **Unreachable Code:** Code that can never be reached by any valid control flow path
    (handled by `simplify_cfg`).
- **Dead Code:** Instructions that execute, but whose computed results are never read
    by any subsequent instruction and produce no observable side effects.

#### The Trap Preservation Invariant (Safe vs. Unsafe Purity)

Under what conditions can an unused instruction `%dst = op %src1, %src2` be deleted?
It is NOT sufficient that `%dst` is unused!

The operation must be **provably pure and trap-free**:

- `xs[5]` without readers CANNOT be deleted: If `len(xs) <= 5`, the instruction must
    terminate the program with "index out of range". Deleting it would silently hide
    a legitimate runtime crash.
- `a // b` CANNOT be deleted: If `b == 0`, it must trigger a division-by-zero trap.
- `i64` arithmetic CANNOT be deleted: It may overflow 64-bit integer limits.

Operations that ARE provably safe to eliminate:

- Literal constant loading (`const`).
- Pure composite constructors and destructors (`tuple`, `tuple_get`, `list`, `variant`, `range`, `len`).
- IEEE-754 floating-point arithmetic (which generates infinities or NaNs without trapping).
- Bitwise logic (`bitand`, `bitor`, `bitxor`) that cannot overflow.

#### Iterative Fixed-Point Sweeping & Level Trimming

DCE proceeds backwards:

- Deleting a dead instruction eliminates uses of its source operands.
- Those operands may now have zero uses, becoming dead in turn.
- Unused basic block parameters and their corresponding jump arguments are stripped.
- Finally, `trim_levels()` detects if entire scope arena levels have become empty,
    pruning redundant `reset` arena management instructions.

### [src/ella/passes/inline.py](./src/ella/passes/inline.py)

Inlining: put a copy of a small function's body where it is called.

#### Theoretical Motivation: Breaking Interprocedural Boundaries

In modern compilers (Muchnick Chapter 15), function calls establish hard optimization
barriers: intraprocedural passes cannot see through function boundaries without expensive
whole-program interprocedural analysis.

Procedure Inlining replaces a call site with a specialized copy of the callee's body:

- **Eliminating Call Overhead:** Avoids stack frame setup, argument pushing, callee-saved
    register preservation, and indirect branch branch-target-buffer (BTB) mispredictions.
- **Unleashing Intraprocedural Optimizations:** Concrete arguments passed by the caller
    can now be folded directly into the callee body by `fold`; dead branches inside the
    callee vanish via `simplify_cfg`; transient return objects are eliminated by `moves`.

#### Heuristics & Termination Invariants

Naive inlining can cause infinite loops (on recursive functions) or catastrophic code
bloat (blowing out instruction caches).
Ella enforces strict inlining preconditions:

- **Size Threshold:** The callee body must contain $\le$ `INLINE_LIMIT` non-constant instructions.
- **Cycle Detection (Non-Recursion):** Call graph reachability analysis asserts that the
    callee cannot reach itself through any path of calls, closures, or generators.
- **Bottom-Up Traversal (`bottom_up`):** Inlining proceeds bottom-up along the call graph
    (callees before callers), ensuring leaf functions are absorbed into intermediate functions
    before those functions are inlined into callers.

#### Arena Level Transformation & Scope Preservation

Because Ella allocates in scope arenas (docs/memory-model.md §14), inlining requires
remapping the callee's allocation regions:

- The callee's result region `@result` maps to the caller's target region.
- The callee's scope levels are shifted upwards above all existing caller levels.
    This guarantees that when the inlined body finishes (`reset`), its temporaries are
    freed immediately before any caller temporaries, maintaining strict LIFO stack discipline.
- Synthetic `enter` and `leave` instructions ensure that inlined executions still count
    towards the language recursion ceiling (`MAX_CALL_DEPTH`), preserving identical stack
    overflow limits.

### [src/ella/passes/direct.py](./src/ella/passes/direct.py)

Direct calls: call a known function by name instead of through a function value.

#### The Cost of Indirect Closure Calls

When `ella.lower` emits code, it adopts a uniform representation: all function calls
are indirect invocations through closure values (`call %closure, args...`).
A top-level function reference is emitted as reading a global variable (`global_get`)
that holds the function's closure.

In native hardware, indirect calls incur noticeable performance penalties:

- Pointer dereferencing: Loading code pointers through closure headers.
- Branch Target Buffer (BTB) pressure: Hardware branch predictors struggle to predict
    indirect jumps compared to direct relative calls (`call label`).
- Optimization barrier: The inliner (`inline.py`) cannot inline an indirect call
    because the target function identity is not statically fixed in the instruction opcode.

#### Devirtualization via Static Global Analysis

The `direct` pass identifies static functions whose definitions are invariant:

- If a global variable is assigned exactly once at module startup to a non-capturing
    function, its value is immutable.
- `global_get` instructions reading that global are replaced with direct static references.
- Calls invoking those static closures are devirtualized:
        `%6: i64 = call %closure(%arg)`  ──►  `%6: i64 = call_direct func_name(%arg)`
- This exposes the call site directly to `ella.passes.inline`, enabling subsequent
    function inlining and argument constant propagation.

### [src/ella/passes/fuse.py](./src/ella/passes/fuse.py)

Fusion: arithmetic on arrays in a row becomes one `fused` instruction.

#### The Memory Wall & the Motivation for Loop Fusion

In high-performance numerical computing (Kennedy & Allen, "Optimizing Compilers for
Modern Architectures"), memory bandwidth is almost always the primary bottleneck (the
"Memory Wall", Wulf & McKee 1995).

Consider a naive array pipeline:
    `result = a * 2.0 + b - c`
Without fusion, this compiles into three separate loops:

  1. Loop 1: Allocates `temp1`, loads all of `a`, multiplies by 2, writes `temp1` to RAM.
  2. Loop 2: Allocates `temp2`, loads `temp1` and `b`, adds them, writes `temp2` to RAM.
  3. Loop 3: Allocates `result`, loads `temp2` and `c`, subtracts them, writes `result`.
For large arrays that exceed CPU cache sizes, memory bandwidth is throttled by thrashing
intermediate arrays in and out of DRAM.

#### Kernel Fusion (Single-Pass Vector Pipelines)

The `fuse` pass combines adjacent element-wise array operations into a single `fused`
instruction:

- Native x64 assembly (`ella.codegen`) emits a single unified loop using 256-bit AVX2
    SIMD instructions.
- Intermediate values never touch memory: they remain resident in YMM registers
    across pipeline stages.
- Memory bandwidth requirements are slashed by 3x-5x, and intermediate heap allocations
    drop to zero.

#### Reduction Fusion (`total`, `dot`)

If an array arithmetic chain terminates in a reduction operation like `total(a)` or `dot(a, b)`:

- The fusion pass incorporates the reduction directly into the SIMD loop body.
- Elements are accumulated on the fly using four parallel vector lanes (`lane_sum`).
- The intermediate array is never allocated at all—the entire expression evaluates
    from source arrays straight to a single scalar float or integer!

#### Hardware Vector Register Constraints

x86-64 AVX2 provides sixteen 256-bit registers (`ymm0` through `ymm15`).
To prevent register spilling within the inner SIMD loop:

- Groups are bounded by `MAX_STEPS = 6`, `MAX_ARRAYS = 4`, and `MAX_NUMBERS = 3`.
- If intermediate array results are also referenced outside the fused block, the loop
    emits them as secondary results without re-scanning memory.

### [src/ella/interpreter.py](./src/ella/interpreter.py)

Tree-walking interpreter: runs a program by evaluating its syntax tree directly.

#### The Dual Role of the Tree-Walking Interpreter

In modern multi-stage compiler design, a tree-walking AST interpreter serves two
vital architectural purposes:

  1. **The Executable Language Specification:**
     Before introducing intermediate representations, register allocators, and assembly
     generators, the tree-walker defines the normative *operational semantics* of Ella.
     It answers questions like: "What is the exact evaluation order of binary operands?",
     "What happens when a list is sliced?", and "When do finalizers run?"
     All later compiler backends (IR interpreter, optimized IR, native x64) are tested
     against this interpreter to ensure identical byte-for-byte behavior.

  2. **The Compile-Time Stage -1 Evaluator:**
     Ella is a multi-stage programming language (docs/grammar.md §11). When compile-time
     splices (`$expr`) appear outside quotes, this interpreter executes them during
     compilation, generating residual AST nodes that are then fed into the IR lowering pass.

#### Big-Step Operational Semantics (Natural Semantics)

The interpreter implements big-step operational semantics (Plotkin 1981, Kahn 1987):
    `eval(Expression, Environment) -> (Value, Environment)`

The evaluation function `Interpreter.eval(expr, scope)` matches over every variant of
the `Expr` algebraic data type:

- Literals evaluate to themselves.
- Variable names look up bindings in the current lexical `Scope`.
- Binary expressions evaluate their left operand, then their right operand, and combine them.
- Blocks evaluate their statements sequentially in a newly pushed nested scope.

#### Lexical Scoping & Environment Chains

Variables reside in `Scope` objects (ella.values). Scopes form a parent-pointer tree
mirroring lexical block nesting:
    [Global Scope] <--- [Function Scope] <--- [Block Scope]
Reading a variable walks outward along the parent chain. Assigning to an existing variable
updates the nearest enclosing binding; `let` introduces a new binding in the current scope.

#### Non-Local Control Flow via Exception Signals

When an expression alters normal sequential evaluation (`return`, `break`, `continue`, `?`),
it raises a private Python signal exception (`ReturnSignal`, `BreakSignal`, `ContinueSignal`).
These unwind the host Python recursion stack until caught by the enclosing function or loop
handler, cleanly restoring the appropriate evaluation state.

#### Generators & Suspended Execution

A generator is a function that can suspend at `yield` and resume at `next`.
Because the AST interpreter does not maintain a native call stack or heap frame, it runs
each generator's body on a cooperative Python thread (`threading.Thread`), synchronizing
execution through handoff events so that only one thread executes at a time.

### [src/ella/codegen.py](./src/ella/codegen.py)

x64 code generation: turns IR into GNU assembly (Intel syntax) for Linux.

#### The Compiler Backend: Bridging SSA to Hardware Silicon

The final phase of a native compiler (Dragon Book, Chapters 8-10) lowers intermediate
abstractions to bare-metal instructions that can execute directly on the host CPU.

Ella targets Linux x86-64 (AMD64) using GNU assembler (GAS) with Intel syntax:

- **Instruction Selection:** Translates typed IR instructions into concrete x64 opcodes
    (`add`, `sub`, `imul`, `idiv`, `cqo`, `vmovsd`, `vaddsd`, `cmp`, `je`, `jmp`).
- **Unified Word Representation:** Every Ella value occupies exactly one 64-bit word:
  - `i64`: 64-bit two's complement integer.
  - `bool`: `0` (false) or `1` (true).
  - `()`: `0`.
  - `f64`: Raw 64-bit IEEE-754 floating-point bits in a general register, loaded into
        XMM registers (`xmm0`, `xmm1`) for AVX2 scalar and vector operations.
  - Boxed objects (strings, lists, arrays, tuples, enums, closures): 64-bit pointers
        into 64-bit virtual address arena space.

#### Register Allocation (SSA Chordal Coloring)

Classical register allocation (Chaitin 1982) models register assignment as graph coloring
on arbitrary interference graphs, which is NP-complete.

However, because Ella's IR is in Static Single Assignment (SSA) form:

- The live ranges of SSA values form a *chordal graph* (Hack 2007; Pereira & Palsberg 2005).
- Chordal graphs can be colored optimally in polynomial time!
- `ella.regalloc` colors the CFG using a greedy allocator traversing the dominator tree:
  - Long-lived values live across function calls are assigned callee-saved registers
        (`rbx`, `r12`, `r13`, `r14`, `r15`).
  - Short-lived values get caller-saved registers (`rsi`, `rdi`, `r8`, `r9`, `r10`).
  - Any values exceeding available registers cleanly *spill* to local 8-byte stack slots
        addressed relative to the base pointer (`[rbp - 8*k]`).

#### Ella's Internal Calling Convention vs. System V AMD64 Abi

Ella employs a hybrid calling architecture:

- **Internal Ella Functions:** Use a uniform stack-based convention that cleanly passes
    dynamic closures, variable numbers of arguments, and explicit result arena regions:
  - `[rbp + 16]`: The Closure pointer.
  - `[rbp + 24]`: The Result Arena depth (where the caller wants the return value allocated).
  - `[rbp + 32 + 8*i]`: Declared parameter $i$.
  - `[rbp + 32 + 8*(n + j)]`: Arena depth of mutable parameter $j$.
- **C Runtime Calls:** When invoking the Ella runtime engine (`runtime.c`), the code
    generator switches to the standard System V AMD64 ABI:
  - Arguments in `rdi`, `rsi`, `rdx`, `rcx`, `r8`, `r9`.
  - Strict 16-byte stack pointer (`rsp`) alignment before the `call` instruction.

#### Parallel Moves & Cycle Resolution

At basic block boundaries and function calls, multiple values must be moved to target
registers simultaneously (`parallel_move`). If registers form a permutation cycle
(e.g., `rax <- rbx` and `rbx <- rax`), naive sequential moves would clobber one value.
Ella’s parallel move solver detects cycles and uses scratch registers (`r11`) or stack
spill locations to break permutation cycles correctly.

#### Internal Calling Conventions & Stack Layout

Ella functions don't use the C calling convention. The caller pushes the
depths of the arenas its `mut` arguments live in (last first), then the
arguments from last to first, then the depth of the arena for the result,
then the closure being called, and calls the closure's code. The result
comes back in `rax`. A direct call (`call_direct`) pushes 0 for the
closure. So inside a function with n declared parameters:

```text
[rbp + 16]              the closure (its captured values start 24 bytes in)
[rbp + 24]              where to put the result: an arena depth
[rbp + 32 + 8*i]        declared parameter i
[rbp + 32 + 8*(n + j)]  the depth of the arena of `mut` parameter j
[rbp - 8*k]             the slot of each other value that has no register, and of
                        each callee-saved register the function uses
```

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

#### Generator Code Generation

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

#### Arrays and SIMD

An array (runtime.c) has its length and where its items are in the same
places as a list, so `len`, a `for` loop and reading an item are the same
few instructions; `array_offset` checks the indexes against the shape
inline. Arithmetic on arrays, `total` and `dot` are loops of AVX2
instructions over four items at a time, in the ymm registers (which no
value is ever in), then one at a time for the rest; see `array_arithmetic`
and `reduction`. They give exactly what the interpreters give: f64 results
are IEEE 754 either way, multiplying and adding stay separate (no fused
multiply-adds), and the sums of `total` and `dot` are added in the order
the language defines for them (docs/grammar.md §9, "Arrays"). An i64
overflow is noticed in the vector code, and the runtime finds the first
one again to report it. A `fused` instruction (ella.passes.fuse) does
several steps in one loop, keeping each step's items in a ymm register,
with masked loads and stores for the last few items (`fused`). The program sets `ella_uses_avx2`, so that the
runtime stops at once on a CPU without AVX2. `vzeroupper` ends each loop,
since the runtime's code uses the older SSE instructions.

### [src/ella/staging.py](./src/ella/staging.py)

Multi-stage programming: code as values, made with `quote` and combined with `$`.

#### Theoretical Foundation: Multi-Stage Programming (MSP)

In conventional languages, metaprogramming is typically handled by textual macro
preprocessors (C/C++ `#define`), token-stream macros (Rust `macro_rules!`), or
template systems (C++ templates). These approaches often suffer from lack of type
safety, cryptic error messages, and accidental variable capture.

Ella implements formal **Multi-Stage Programming (MSP)**, pioneered by Walid Taha
and Tim Sheard (ACM PEPM 1997, "Multi-Stage Programming with Explicit Annotations";
MetaML / MetaOCaml):

- **First-Class Code Values:** Code is not text or raw tokens; it is a typed,
    well-formed syntax tree value of type `Code[T]`.
- **Explicit Stage Leveling:** Computations are organized into discrete *stages*
    (integer levels $n \in \mathbb{Z}$):
  - Stage $0$: Normal runtime execution.
  - Stage $1$: The code generated for the next stage (the body of a `quote`).
  - Stage $-1$: Compile-time metaprogramming (splices `$` occurring outside quotes).
  - Stages nest without limit in both directions ($+2, +3, \dots$ or $-2, -3, \dots$).
- **The Staging Operators:**
  - `quote e` (Quasiquotation): Defers execution of `e`, yielding a `Code[T]` value.
  - `$c`       (Splice / Escape): Evaluates `c` one stage earlier and splices its
                                      residual code directly into the surrounding quote.
  - `run(c)`  (Evaluation): Compiles and executes code value `c` at the current stage.

#### Cross-Stage Persistence (CSP) & Stage Discipline

A foundational theorem of multi-stage type systems is *Stage Consistency*:

- **Cross-Stage Persistence:** A variable bound at stage $n$ can be safely referenced
    at a later stage $n+k$ ($k \ge 1$). Its runtime value is evaluated and carried into
    the residual code as an embedded constant (`reify()`). For example, if `let factor = 3`,
    then `quote x * factor` reifies into the residual syntax tree `x * 3`.
- **No Future-Stage References:** Referencing a variable bound at stage $n+k$ inside an
    earlier stage $n$ is statically prohibited (the variable does not yet exist).
    The type checker enforces this stage invariant across all variable accesses.

#### Macro Hygiene & Automatic Alpha-Renaming

A classic bug in macro systems is *variable capture* (Kohlbecker et al., 1986):
if a macro introduces a variable named `temp`, it might inadvertently shadow or be
shadowed by a variable named `temp` in the user's surrounding code.

Ella guarantees mathematical *Hygiene* by automatic $\alpha$-renaming:

- When `build_code()` constructs AST nodes inside a quote, any newly bound variable
    receives a globally unique identifier (e.g. `x` becomes `x.4`).
- Splicing code into other quotes can never capture or interfere with surrounding
    variable names, guaranteeing referential transparency.

#### Types as First-Class Values & Zero-Cost Derivation

Ella treats types as values at compile time (`type Point` is a `Type[Point]` value).
Combined with reflection built-ins:

- `fields(t)` inspects the field names and types of a struct at compile time.
- `$t` splices a computed type into a type annotation.
- `v.$f` accesses a field dynamically by a string name.

This enables writing generic code generators directly in Ella without a separate
macro language:

```ella
let derive_eq[T] = (t: Type[T]) -> Code[(T, T) -> bool] =>
    quote (a: $t, b: $t) -> bool => $(all_equal(fields(t), quote a, quote b))
```

#### The Expansion Pipeline

Before lowering to IR, `prepare()` identifies all stage $-1$ compile-time splices,
evaluates them using the reference AST interpreter (`ella.interpreter`), and replaces
them with the generated residual AST. The expanded tree is then checked and lowered
normally; the IR and native compiler never see staging primitives.

### [src/ella/checker.py](./src/ella/checker.py)

Static type checker: works out the type of every expression before the program runs.

#### Theoretical Foundation: Type Safety & Soundness

In type theory (Benjamin Pierce, "Types and Programming Languages", TAPL), a type system
is a tractable syntactic method for proving the absence of certain program behaviors
by classifying phrases according to the kinds of values they compute. Robin Milner's
famous slogan "Well-typed programs cannot go wrong" (1978) forms the bedrock of Ella:
type errors are completely eliminated before intermediate code generation or execution.

#### Bidirectional Type Checking & Inference

Ella employs *Bidirectional Type Checking* (Pierce & Turner 2000):

- **Synthesis / Inference Mode (`infer`):**
    The checker inspects an AST node and synthesizes its type upwards from its sub-expressions.
    For example, `1 + 2` synthesizes `i64` because `+` on two `i64`s produces an `i64`.
- **Checking Mode (`check`):**
    The checker pushes an expected type downwards into an expression.
    For example, untyped lambdas `xs |> map(n => n * 2)` have their parameter types
    inferred downwards from `map`'s function argument type `(T) -> U`.

#### Type Variables & First-Order Unification (Robinson 1965, Martelli-Montanari 1982)

When a type cannot be fully determined immediately (such as an empty list literal `[]`
or `None`), the checker instantiates a *type variable* (written `?1`, `?2`).

As the program uses the value, the checker solves type equations through *unification*
(`unify()`):

- If `seen: [?1]` is concatenated with `[5]: [i64]`, unification equates `[?1] = [i64]`,
    binding `?1 := i64`.
- **The Occurs Check:** Prevents circular, infinite types by ensuring a type variable `?1`
    does not appear inside its own replacement type (e.g. `?1 = [?1]`).
- Unconstrained type variables that remain free at the end of checking default safely to `()`.

#### Structural Constraints & Monomorphization

Ella generic functions specify constraints: `let total[T: Shape] = (items: [T]) -> f64`.

- Constraints are resolved *structurally*: any struct or enum that implements the
    required methods (`area: (Self) -> f64`) satisfies `Shape`.
- Every generic function is monomorphized during lowering—compiled into a specialized,
    direct-dispatch IR function for each concrete type argument, guaranteeing zero runtime
    boxing or vtable overhead.

#### Static Law of Exclusivity for `mut` Parameters

To maintain value semantics without dynamic borrow locks:

- Passing a mutable variable as an argument (`foo(mut xs, xs)`) is checked statically.
- The checker rejects any call where a `mut` parameter could alias another argument
    in the same call, preventing simultaneous mutable aliasing at compile time.

#### Non-Invasive Metadata Architecture

The checker treats AST nodes as completely immutable (frozen). It records all inferred
types, resolved variable identities, and constructor metadata in dictionaries keyed by
`id(node)`:

- `checker.types[id(expr)]`: The checked Ella `Type` of every expression.
- `checker.kinds[id(name)]`: Resolves names to local slots, globals, or constructors.
This keeps the AST purely syntactic and decouples passes cleanly.

### [src/ella/values.py](./src/ella/values.py)

Run-time values, shared by the interpreters.

#### Dual Value Models: Interpreter Objects vs. Machine Words

Compilers often utilize two complementary representations for runtime values:

  1. **High-Level Interpreted Values (Host Objects):**
     In `ella.interpreter` and `ella.ir_interpreter`, values map to idiomatic Python
     objects (`int`, `float`, `list`, `tuple`) wrapped in slotted dataclasses. This
     prioritizes debuggability, clean interactive inspection, and rapid prototyping.
  2. **Low-Level Native Values (64-Bit Machine Words):**
     In `ella.codegen` and `runtime.c`, every value is lowered to a uniform 64-bit word:
     scalars fit immediately in registers; boxed composite types are 64-bit pointers
     into pre-reserved virtual memory arenas.

#### Value Mapping Architecture

| Ella type | Python interpreter representation | Native x64 representation |
| --- | --- | --- |
| `i64` | `int` (clamped to [-2^63, 2^63 - 1]) | `int64_t` in a general register |
| `f64` | `float` (64-bit IEEE-754) | `uint64_t` bits / XMM register |
| `f32` | `F32` float wrapper | `uint32_t` in an XMM register |
| `i32` | `I32` int wrapper | `int32_t` in a general register |
| `bool` | `bool` (`True` / `False`) | `0` or `1` in a general register |
| `()` | `()` (empty Python tuple) | `0` |
| `str` | `str` | Pointer to a `Str` struct |
| `(T, U)` | `tuple` | Pointer to a `Tuple` struct |
| `[T]` | `list` (deep-copied for value semantics) | Pointer to a `List` struct |
| `Array[T]` | `ArrayValue` (flat buffer and shape) | Pointer to an `Array` struct (AVX2) |
| `a..b` | `range` | Pointer to a `Range` struct |
| Variant | `VariantValue` (tag and fields) | Pointer to a `Variant` struct |
| `Pool[T]` | `PoolValue` | Handle: `[Pool\|Gen\|Slot]` |
| `Arena` | `ArenaValue` | Ref: `[Arena\|Gen\|Index]` |
| `Gen[T]` | `GenValue` (coroutine state) | Pointer to a `Gen` struct |
| `Code[T]` | `CodeValue` (syntax tree) | Interpreted / JIT |

#### Value Semantics & Deep Copying (`copy_value`)

In accordance with Ella's core philosophy (docs/memory-model.md §3), no two variable
bindings can observe shared mutable state.

- In the reference AST interpreter, every variable read or list access invokes
    `copy_value()`, performing an explicit deep structural copy of lists, arrays,
    and structs.
- In the optimizing IR compiler, these copies are eliminated whenever the compiler
    can prove the source value has no other readers (`moves` pass and last-use analysis).

#### Scopes & Call Frames (`Scope`)

Local variables live in `Scope` objects linked in a chain from innermost block outwards
to the global namespace. Looking up a variable traverses parent pointers until found.
`MAX_CALL_DEPTH` enforces a uniform recursion ceiling across all backends to trap
runaway recursion before the host machine runs out of native stack memory.

### [src/ella/operators.py](./src/ella/operators.py)

Ella's operators on run-time values, shared by every interpreter.

#### The Principle of Cross-Backend Arithmetic Parity

A common pitfall in compiler construction is divergence between high-level interpreter
arithmetic and native machine execution. Python and x86-64 hardware differ in subtle ways:

- **Integer Overflow:** Python integers have arbitrary precision (`math.inf` is never
    reached for integers). Ella's `i64` and `i32` are strictly bounded signed two's-complement
    words (e.g. `[-2^63, 2^63 - 1]`). Operations that exceed these bounds (like negating
    the minimum integer `-( -2^63 )`) must deterministically trigger an overflow trap.
- **Division Semantics:** Python's `//` operator performs floor division towards negative
    infinity (`-7 // 2 == -4`), whereas x64 assembly `idiv` truncates towards zero (`-7 / 2 == -3`).
    Ella standardizes integer division to match the target machine architecture and raises
    identical runtime traps for division by zero.
- **IEEE-754 Compliance:** Floating-point division (`float_div`) and exponentiation
    (`float_pow`) implement IEEE-754 standards, returning signed infinity or NaN without
    raising uncaught Python exceptions.

#### Multidimensional Array Operations & SIMD Lane Emulation

Ella features first-class multidimensional numerical arrays (`Array[T]`):

- **Element-Wise Operations:** Binary operations on arrays apply element-by-element,
    broadcasting scalar operands across array dimensions.
- **SIMD Reduction Parity:** Functions like `total` and `dot` sum array elements using
    four interleaved accumulators (`lane_sum`). This ensures that floating-point rounding
    errors in the Python interpreters match the exact out-of-order 4-wide/8-wide lane
    accumulation of native AVX2 SIMD vector instructions.

#### Method-Based Operator Protocols

Operators are not hardcoded to primitives. For user-defined structs and enums, the checker
and interpreters route operators through standard method protocols:

- `a + b`   -> `a.add(b)`
- `a == b`  -> `a.eq(b)`
- `a[i]`    -> `a.get(i)`
- `a[i] = v`-> `mut a.set(i, v)`
- `x in c`  -> `c.contains(x)`

### [src/ella/library.py](./src/ella/library.py)

The library: built-in functions with fixed types, such as `split` and `read_line`.

#### The Standard Library in Compiler Architecture

A programming language requires an interface between pure linguistic expressions
and operating system facilities. The standard library provides primitives for:

- Input/Output: `read_line`, `read_file`, `write_file`, `print`, `eprint`.
- Unicode & String Manipulation: `split`, `join`, `find`, `replace`, `trim`, `chars`.
- Conversions & Parsing: `parse_i64`, `parse_f64`, `to_text`.
- Mathematical Functions: Trigonometric, logarithmic, and power functions.

#### Unicode Model: O(1) Code Point Indexing

In modern languages, string representation is a trade-off between memory and speed:

- UTF-8 is compact, but random indexing `s[i]` and string slicing are $O(N)$ operations
    because characters vary from 1 to 4 bytes in length.
- Ella represents strings as buffers of 32-bit Unicode code points (`uint32_t *chars`
    in `runtime.c`, validated to stay below `0x10FFFF` excluding surrogates).
- **Result:** `len(s)` and random indexing `s[i]` are strictly $O(1)$ operations,
    dramatically simplifying compiler indexing and parser implementations.
- External I/O is automatically validated as strict UTF-8 upon reading and writing.

#### The `Host` Environment Abstraction for Reproducible Testing

Rather than directly binding to global Python standard streams (`sys.stdin`, `sys.argv`),
Ella routes all process interaction through a `Host` context object:

- Standard input, command-line arguments, and error streams can be mocked in unit tests.
- Allows running hundreds of test programs within a single Python test process without
    leaking state or corrupting real process file descriptors.

#### Dual Runtime Parity Invariant

Every library function implemented in this module has a 1-to-1 C equivalent in
`src/ella/runtime.c` (`ella_lib_split`, `ella_lib_parse_i64`, etc.). The test suite
enforces that both backends produce identical results and matching error messages.

### [src/ella/modules.py](./src/ella/modules.py)

Modules and imports: a program's files, linked into one program (docs/grammar.md §3).

#### The Role of a Module System in Compiler Architecture

Programming languages require mechanisms to partition large software systems into
cohesive, reusable components:

- **Hierarchical Namespaces:** Preventing name collisions across distinct libraries.
- **Encapsulation & Access Control:** Restricting visibility of internal helpers
    (private identifiers starting with `_`).
- **Dependency Graph Resolution:** Discovering, parsing, and topologically ordering
    imported units while detecting circular dependencies.

#### The AST Linker: Whole-Program Unification

In traditional C/C++ compilation, every translation unit is compiled into a separate
machine object file (`.o`), and a low-level linker (`ld`) resolves symbols using symbol
tables and address relocations.

Ella implements an elegant **AST Linker** (`link()`):

- **Dependency Traversal & Topological Ordering:**
    Starting from the entry file, `link()` traverses `import` statements recursively.
    Modules are loaded from the root directory, search paths (`--path` / `ELLA_PATH`),
    or standard library sources (`src/ella/lib/`). Modules are ordered topologically
    so that dependencies are evaluated strictly before their consumers.
- **Static Name Mangling & Disambiguation:**
    To allow downstream compiler phases (checker, lowerer, optimizer) to operate on
    a single flat program without namespace confusion:
  - Top-level names receive module-qualified prefixes: `area` in `geo.ella` becomes `geo.area`.
  - Variant constructors and user-defined types are prefixed accordingly (`geo.Circle`).
  - Local variables continue to shadow module names naturally.
- **The Standard Prelude:**
    Core standard library modules (`lists.ella`, `maps.ella`, `strings.ella`, `math.ella`)
    form the standard *Prelude*. Their public functions (`map`, `filter`, `sort`, `lines`)
    are automatically injected into the link graph, available everywhere without explicit imports.

#### Access Control & Immutability Enforcement

The linker enforces privacy:

- An identifier prefixed with `_` cannot be imported or accessed outside its declaring file.
- Imported top-level variables are strictly read-only to external modules (preventing
    cross-module mutable state corruption).

A file is a module, named by its path from the *root*, the entry file's
directory: `geo/poly.ella` is the module `geo.poly`. Imports not found
from the root are found from each directory of the *package path* in turn
(`--path` and the ELLA_PATH environment variable, `package_path`), as if
it were the root: a *package* is a module found there, with the modules
below it. The standard library's modules are files in src/ella/lib, found
last; their public names are the *prelude*, which every module sees
without importing them.

Linking (`link`) loads the modules the entry file uses, directly or not:
the ones it imports, and the library modules whose prelude names it uses.
It makes them one program, in the order their code runs (a module's
imports first), so the later stages don't need to know about modules. For
that, every top-level name in the program is made different:

- A module's top-level names get its name in front: `area` in `geo.ella`
  is `geo.area`, and so are its variants (`geo.Circle`) and types
  (`geo.Shape`). The entry file's names stay as they are, so a program of
  one file is unchanged.
- Every name that refers to a top-level name becomes that name, whichever
  way it is written: `area` in `geo.ella` itself, `geo.area` (parsed as a
  member of `geo`), `p.area` with `import geo as p`, or `map` from the
  prelude (`lists.map`). The linker keeps track of local variables, which
  hide top-level names, as the checker does.

Each module's spans have its file (`geo/poly.ella`, `<std>/maps.ella`, and
a package's module by its path from the root, `../libs/json.ella`), so
that errors say where they are. A bare name left in a module other than
the entry file is a built-in (the linker reports any other name it doesn't
know), so the checker and the interpreter give such code the built-ins
even if the entry file has a top-level name like `len` (see their
`lookup`). This is also where privacy (names starting with `_`) and
read-only imported variables are checked; private fields and methods are
checked by the checker, which knows the types of values.

### [src/ella/exhaustive.py](./src/ella/exhaustive.py)

Exhaustiveness: do a `match`'s cases cover every value of its subject's type?

#### The Problem: Safety and Reachability in Pattern Matching

Pattern matching is a cornerstone of expressive statically typed functional and
systems languages (ML, Haskell, Rust, Swift). However, pattern matching introduces
two severe safety hazards if unchecked:

  1. **Non-Exhaustive Matching:** The programmer omits a case (e.g., handles `Some(x)`
     but forgets `None`). At runtime, encountering the unhandled variant would cause
     an unhandled branch panic.
  2. **Redundant / Dead Patterns:** A pattern is preceded by a broader pattern (e.g.
     `_` or a duplicate pattern) that matches all its inputs. The dead branch is dead
     code that should be flagged with compiler warnings.

#### Maranget's Matrix Specialization Algorithm (JFP 2007)

Ella implements the canonical algorithm developed by Luc Maranget ("Warnings for
Pattern Matching", Journal of Functional Programming 2007):

The Matrix Formulation:
Match patterns are structured as a pattern matrix $P$, where:

- Each row represents one `case` arm.
- Each column represents a component of the subject value being inspected.

The Usefulness Test (`useful(P, q)`):
A pattern vector $q$ is *useful* with respect to matrix $P$ if there exists at least
one concrete value that matches $q$ without matching any row in $P$.

- If the vector of wildcards `(_, _, ...)` is useful against $P$, the match is
    **non-exhaustive**!
- If a row $r_i$ is NOT useful against the preceding rows $r_1 \dots r_{i-1}$,
    row $r_i$ is **redundant** (dead code)!

#### Constructor Specialization & Witness Synthesis

To test usefulness, Maranget's algorithm inspects the first column's type:

- **Complete Signature ($\Sigma$ fully covered):**
    If the column's type has a closed set of constructors (e.g. `true` and `false` for
    booleans; variants for an enum) and every constructor appears in the column:
    The matrix is *specialized* for each constructor $c \in \Sigma$, expanding $c$'s
    sub-fields into new columns, and usefulness is tested recursively.
- **Incomplete Signature:**
    If some constructor is omitted (or the type has infinite inhabitants like `i64` or `str`),
    the algorithm constructs a concrete *witness counterexample* (e.g. `Some(None)` or `(false, _)`).
    The compiler reports this witness to the user:
        "error: match does not cover every value (e.g. Some(None))"

### [src/ella/regalloc.py](./src/ella/regalloc.py)

Register allocation: which IR values live in registers, for the code generator (ella.codegen).

#### Theoretical Revolution: Chordal Graphs in SSA Form

For decades, compiler backends treated register allocation as graph coloring on
arbitrary interference graphs (Chaitin 1982, Briggs 1994). Finding an optimal k-coloring
for an arbitrary graph is NP-complete, requiring complex heuristics (Kempe chains,
iterative spilling).

In the mid-2000s, Sebastian Hack (2007) and Fernando Pereira & Jens Palsberg (2005)
proved a breakthrough theorem:
    "The interference graph of a program in SSA form is a Chordal Graph."

A *Chordal Graph* (or triangulated graph) is one in which every cycle of four or more
vertices contains a chord (an edge connecting two non-consecutive vertices of the cycle).
Because of the strict dominance properties of SSA definitions:

- Two SSA values interfere if and only if one dominates the other and the later is
    defined while the earlier is still live.
- A chordal graph can be colored **optimally in linear time** using a simple greedy
    algorithm that follows a perfect elimination ordering (such as dominator-tree order)!

#### The Greedy Dominator-Tree Allocation Algorithm

Ella leverages this SSA property directly:

  1. **Liveness Analysis:** Backwards dataflow analysis computes the exact set of
     simultaneously live SSA values at every instruction.
  2. **Dominator-Tree Order Traversal:** Basic blocks are visited in dominator-tree
     order (every block before the blocks it dominates).
  3. **Greedy Color Assignment:** When an instruction defines value `%n`, the allocator
     picks any currently free physical register that does not interfere with any
     currently live value.
  4. **Spilling:** If all physical registers are exhausted, `%n` spills to an 8-byte
     stack slot for its lifetime.

#### Hardware Register Partitioning (Callee-Saved vs. Caller-Saved)

x86-64 hardware enforces standard calling convention register roles:

- **Callee-Saved Registers (`rbx`, `r12`..`r15`):** Preserved across function calls.
    Any SSA value whose live range spans across a call instruction (`calls`, `calls_partway`)
    is restricted to callee-saved registers (or spills). The function prologue saves them;
    the epilogue restores them.
- **Caller-Saved Registers (`rsi`, `rdi`, `r8`..`r10`):** Fast, volatile scratch registers.
    Allocated only to temporary values that are strictly dead across calls.
- **Reserved Scratch Registers (`rax`, `rcx`, `rdx`, `r11`):** Never allocated by
    `regalloc.py`; reserved for codegen scratch operations, division (`rax:rdx`), and
    parallel move cycle resolution (`r11`).

### [src/ella/analysis.py](./src/ella/analysis.py)

What an editor needs to know about a program: its errors, what each name in it is, and what could be typed next.

#### The Paradigm Shift: Batch Compilers vs. IDE Language Services

Classical compiler architecture (Dragon Book) was conceived for batch compilation:
read a complete, syntactically pristine file from disk, abort on the first syntax
error, and produce machine code.

Modern developer tooling (Language Server Protocol - LSP, Roslyn, Rust Analyzer) requires
an entirely different paradigm:

- **Ubiquitous Syntax Errors:** The user's code is incomplete, half-typed, and
    syntactically invalid most of the time.
- **Non-Stop Fault Tolerance:** The compiler must never crash on malformed inputs;
    it must recover locally and analyze as much of the surrounding program as possible.
- **Interactive Code Intelligence:** Providing live type hover information, semantic
    syntax highlighting, go-to-definition navigation, and contextual autocompletions
    within milliseconds of every keystroke.

#### Resilient Multi-Stage Error Recovery

Ella implements a multi-tier recovery architecture:

  1. **Lenient Lexing (`tokenize_leniently`):**
     If the user types an unclosed quote `let s = "hello` or leaves dangling brackets,
     the lenient tokenizer synthetically patches delimiters at newline boundaries
     without shifting character coordinate spans, allowing the parser to proceed.
  2. **Partial Parsing (`parse_partial`):**
     If a statement has a syntax error, the Pratt parser replaces the broken line
     with a unit expression `()` and synchronizes to the next line of the block.
  3. **Secondary Error Suppression:**
     Semantic errors on lines that failed syntactic parsing are suppressed to avoid
     overwhelming the user with unhelpful secondary diagnostics.

#### Semantic Token Extraction & Hover Resolution

`analyze()` converts the resiliently checked AST into structured editor data:

- **Symbol Extraction:** Every identifier is mapped to its inferred Ella `Type`,
    definition span, and semantic token type (`variable`, `function`, `type`, `keyword`).
- **Hover Information:** Queries at position `(line, column)` locate the innermost
    AST node and format its type signature (`(i64, i64) -> bool`).
- **Contextual Autocompletion (`complete`):**
    Extracts visible variable names, struct field accessors (`p.x`), enum variants,
    and library functions available at the active cursor position.

### [src/ella/native.py](./src/ella/native.py)

Building and running native executables.

#### The Ahead-of-Time (AOT) Native Driver

In a complete native compiler, the language front-end and middle-end generate
target-specific assembly code, but rely on standard system toolchains (GNU GCC,
Clang, binutils) for assembly and link editing:

```text
Ella Source (.ella)
       │
       ▼ compile_program()
    Typed IR (ella.ir)
       │
       ▼ optimize()
  Optimized IR
       │
       ▼ generate()
  x64 Assembly (.s) (Intel syntax)
       │
       ▼ gcc (assembling & linking with cached runtime.o)
Native ELF Binary (Executable and Linkable Format)
```

#### Content-Addressed Runtime Caching

Compiling `src/ella/runtime.c` from C source with GCC on every single test run
would introduce noticeable compilation latency.
To achieve instantaneous sub-second testing:

- `runtime_object()` computes a SHA-256 cryptographic digest over the source code
    of `runtime.c` and compiler flags (`CFLAGS`).
- The compiled object is cached in the system temporary directory (`/tmp/ella-runtime-<hash>.o`).
- The runtime is compiled only once per source revision; subsequent builds simply link
    the cached object file in milliseconds.

#### GCC Compilation Invariants & Floating-Point Parity

Flags: `-std=gnu17 -O2 -Wall -Wextra -ffp-contract=off -lm`:

- `-ffp-contract=off`: Prohibits GCC from automatically fusing floating-point multiplies
    and adds into single hardware FMA (Fused Multiply-Add) instructions. FMA instructions
    round only once rather than twice, which would cause subtle numeric differences
    compared to the reference Python interpreter's two-step `(a * b) + c` evaluation.

#### Native Process Supervision & Stack Trace Translation

`finish_run()` parses the standard error stream of the executed binary:

- Translates raw numeric call site lines back into Ella source coordinate spans.
- Reconstructs human-readable stack traces with source file locations and function names.

### [src/ella/lsp.py](./src/ella/lsp.py)

A language server for Ella, built on pygls: editors talk to it over stdin and stdout (the Language Server Protocol).

#### Decoupling Compilers from Editors via LSP

Historically, supporting $M$ programming languages across $N$ text editors (VS Code,
Vim/Neovim, Emacs, Sublime) required $M  imes N$ separate editor plugins, each
duplicating lexing, parsing, and type inference logic.

Microsoft's Language Server Protocol (LSP) revolutionized language tooling by introducing
a standardized JSON-RPC wire protocol over stdio/IPC:

- The language implementation provides an out-of-process Language Server.
- Any LSP-compliant editor acts as a generic UI client.
- Complexity is reduced from $O(M  imes N)$ to $O(M + N)$.

#### Key Capabilities Provided by `ella-lsp`

- **Continuous Diagnostics:** Real-time syntax and type error squiggles as the user types.
- **Hover (`textDocument/hover`):** Inspecting the static type of variables and expressions.
- **Go-to-Definition (`textDocument/definition`):** Jumping to definitions across modules.
- **Semantic Tokens (`textDocument/semanticTokens`):** AST-aware syntax highlighting.
- **Completions (`textDocument/completion`):** Contextual autocomplete for names and fields.

#### Process Isolation for Compile-Time Evaluation

Ella is a multi-stage language where compile-time code (`$expr`) executes during checking.
Running user code inside the language server process poses severe risks:

- Unbounded computation: A user might accidentally write an infinite loop in a splice.
- Channel corruption: User code printing to `stdout` would inject garbage into the
    raw JSON-RPC communication channel, crashing the LSP connection.

Ella solves this with **Worker Process Isolation**:

- All static analysis runs inside an isolated worker subprocess (`ProcessPoolExecutor`).
- Worker stdout is redirected away from the JSON-RPC pipe.
- Stalled worker evaluations are automatically terminated via timeouts without
    affecting the editor UI responsiveness.

#### Coordinate System Bridging (1-Based Code Points vs. UTF-16 Code Units)

LSP defines positions using 0-based lines and UTF-16 code units (originating from JavaScript
and VS Code's internal string representation). Ella internally uses 1-based lines and
Unicode character code point counts. The LSP adapter cleanly maps between these coordinate
systems.

### [src/ella/ir_interpreter.py](./src/ella/ir_interpreter.py)

IR interpreter: runs a module of IR (see ella.ir) directly.

#### The Role of an IR Interpreter in Compiler Architecture

While an AST interpreter defines language-level semantics, an **Intermediate
Representation (IR) Interpreter** (or IR Virtual Machine) executes the flattened,
typed control-flow graph directly.

In Ella's architecture, the IR interpreter serves two crucial roles:

  1. **Phase Boundary Verification:** Validates that lowering from AST to IR preserved
     exact program semantics without altering computation results or error behaviors.
  2. **The Memory Safety Oracle:** Before emitting native x86-64 machine code and relying
     on bare virtual memory pointers, the IR interpreter *simulates* the physical scope
     arenas, validating the compiler's memory placement decisions at runtime.

#### Runtime Simulation of Scope Arenas & Lifetime Tagging

To verify the compiler's region inference (docs/memory-model.md §5, §14):

- Every simulated arena is identified by its *depth* (depth 0 is global; depth 1+ are
    function scopes and nested loops) and an incrementing *generation* counter.
- Every boxed runtime object carries a **lifetime tag**:
        `Tag(depth, generation)`
    indicating the shortest-lived arena containing any part of the object.

#### Continuous Runtime Memory Invariants

Every instruction executed by the IR interpreter dynamically asserts two golden rules:

  1. **No Use-After-Free (UAF):**
     Whenever a value is read or operated upon, the interpreter verifies that its
     underlying arena has not been reset since allocation (`tag.generation == arena.generation`).
     Reading from a deallocated arena raises `EllaError("memory check failed: use after free")`.
  2. **No Dangling Stores (Lifetime Containment):**
     When storing a value into a variable, list, pool slot, or return region, the interpreter
     asserts that the destination region lives no longer than the source value:
         `destination_arena.lifetime <= value.lifetime`
     Storing a loop-local temporary into an outer variable raises
     `EllaError("memory check failed: value outlives its destination")`.

Any failure of these checks is treated not as a user error, but as an internal
**compiler defect** in `ella.lower`'s region placement logic.

#### Memory Profiling (`--profile`)

The IR interpreter tracks high-water memory marks per scope depth and bytes allocated
per source line, estimating native struct sizes to provide developers with detailed
arena profiling reports.

When a sub-arena or pool dies, its

A generator function's frame (docs/memory-model.md §17) is kept in its
Gen between resumes, along with its scope levels, which are slot arenas
the Gen owns: `reset` there clears them, and they are given back when the
Gen dies, after the values in them are finalized, deepest level first.

When an arena is freed, the finalizers of the values in it run first,
newest first (docs/memory-model.md §7). The interpreter also adds up roughly how
many bytes native code would use (`--profile` shows them): the high-water
mark of each scope level, and what each allocating instruction made.
