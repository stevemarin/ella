# Ella

A Linux, x64 statically-typed language for fun and learning, implemented in pure Python.

Ella is an experiment in building a language from the tokenizer through to native
code generation. The compiler will use a sequence of representations, starting
with one small intermediate representation (IR). The first consumer of that IR
will be an interpreter; a later compiler will lower it to x64 assembly.

## Goals

* Keep the implementation as simple as possible without hiding important ideas.
* Use a lightweight, extensible syntax and grammar.
* Learn how interpreters, IRs, optimization, and native code generation fit together.
* Explore memory safety without GC
* Compile to x64 on Linux.
* Leverage modern Python as much as possible in the design
* All user-facing integer types are signed
* Everything is an expression
* Only booleans are truthy
* Built-in vector types
* No implicit type conversions
* SIMD everywhere
* Pass-by-value semantics; immutable bindings unless marked `mut`

The language itself is described in [docs/grammar.md](docs/grammar.md), and
[example_syntax.ella](example_syntax.ella) is a tour of the syntax.

## Design

### Initial compiler pipeline

```text
source → tokenizer → parser → AST → type checker → IR → IR interpreter
                                ↓                     └→ optimizer → x64 code generation
                                AST interpreter
```

Start with one IR. Add another representation only when a later lowering needs
to make a real change in abstraction, such as replacing structured operations
with explicit memory and machine-level operations.

The IR is a typed control-flow graph (specified in
[src/ella/ir.py](src/ella/ir.py)):

* Functions contain basic blocks.
* Blocks contain simple, three-address instructions and end in a terminator
  (`branch`, `jump`, `return`, or `trap` for run-time errors).
* Values have explicit types and unique IDs, and never change.
* Control-flow joins use block parameters: `jump join(%4)` passes a value to
  `join(%5: i64)`. They do the job of SSA `phi` nodes.
* The lowering keeps variables in named, typed local slots (`load`/`store`).
  The mem2reg optimization pass then promotes them to values, giving full SSA
  form; that is easier as a pass than in the lowering.
* Lists are values: `set_index` makes a new list, so value semantics are
  explicit and nothing is ever copied behind the scenes.
* Lambdas are closure converted: each becomes an IR function taking its
  captured variables as extra parameters.
* Side effects such as calls, printing and global writes are explicit operations.

The IR interpreter defines and exercises the meaning of these operations. The
IR verifier checks structural, dominance and type rules before interpretation,
optimization, or code generation. Optimization passes operate on the IR and
must preserve its behavior.

### Staged programming

Staging is separate from the number of compiler passes. The language should
eventually distinguish compile-time evaluation from run-time code generation.
Compile-time evaluation may compute values now; when an expression depends on a
run-time value, compilation preserves it as residual code to execute later.
The initial implementation should make this boundary explicit in the language
and IR rather than treating every compiler pass as a stage of program execution.

## Implementation roadmap

### 1. Language front end

* Define token types and a tokenizer.
* Build a small expression language with a Pratt (TDOP) parser.
* Add functions, and types incrementally.
* Done when [example_syntax.ella](example_syntax.ella) tokenizes and parses.
* Keep syntax and operator definitions modular where that helps experimentation.

### 2. Source interpreter

* Evaluate the initial expression language directly from its AST.
* Use it to settle basic language behavior before introducing IR.

### 3. IR and IR interpreter

* Specify the first IR operations, types, blocks, and terminators.
* Lower the supported AST into IR.
* Implement an IR verifier and a readable IR printer.
* Interpret IR and compare its results with the AST interpreter.
* Add branches, loops, calls, and side effects to the IR as the language gains them.

### 4. Optimization

* Promote local slots to SSA values (the classic "mem2reg" pass).
* Add small, understandable passes such as constant folding, constant
  propagation, and dead-code elimination.
* Keep passes separate and inspectable; verify IR before and after each pass.
* Later, experiment with implementing passes in Ella and compiling them with
  Ella itself. Later still: inlining, loop-invariant code motion, and removing
  `assert`s that can be proven true.

### 5. Native code generation

* Lower the verified IR to x64 assembly.
* Define the calling convention and runtime support needed by generated code.
* Compare native execution against the IR interpreter on the same programs.

### 6. Staged programming and runtime memory

* Add explicit compile-time and run-time evaluation rules.
* Represent generated run-time computations as residual IR.
* Explore memory management without a garbage collector: stack-first
  allocation, arenas for objects with related lifetimes, and pools for
  long-lived fixed-size objects. The design is in
  [docs/memory-model.md](docs/memory-model.md). Until then, the interpreters back lists with
  Python lists: the AST interpreter copies them to get value semantics, and
  the IR builds new lists instead of changing them.

## Current status

The syntax is specified in [docs/grammar.md](docs/grammar.md).

* Stage 1 is done: the tokenizer ([src/ella/lexer.py](src/ella/lexer.py)) and
  Pratt parser ([src/ella/parser.py](src/ella/parser.py)) turn source into the
  syntax tree in [src/ella/syntax.py](src/ella/syntax.py).
* Stage 2 is done: the tree-walking interpreter
  ([src/ella/interpreter.py](src/ella/interpreter.py), with values in
  [src/ella/values.py](src/ella/values.py)) runs programs.
  [test/programs/](test/programs/) holds programs with their expected
  output, for every interpreter and backend to share.
* Stage 3 is done: the type checker ([src/ella/checker.py](src/ella/checker.py))
  works out every expression's type, the lowering
  ([src/ella/lower.py](src/ella/lower.py)) turns the checked tree into IR
  ([src/ella/ir.py](src/ella/ir.py), which also has the printer and verifier),
  and the IR interpreter ([src/ella/ir_interpreter.py](src/ella/ir_interpreter.py))
  runs it. Every test program gives the same results through both interpreters.
* Stage 4 is done: optimization passes in [src/ella/passes/](src/ella/passes/)
  promote slots to SSA values (mem2reg), fold and propagate constants,
  simplify the control-flow graph, and remove dead code. The IR is verified
  after every pass, and every test program also runs through the optimized IR.

```sh
uv run ella example_syntax.ella           # check and run a program (through the IR)
uv run ella --tree example_syntax.ella    # check and run it with the AST interpreter
uv run ella -O example_syntax.ella        # optimize the IR, then run it
uv run ella --ir example_syntax.ella      # print the IR (add -O to optimize it,
                                          # or --passes mem2reg,fold to pick passes)
uv run ella --ast example_syntax.ella     # print the syntax tree
uv run ella --tokens example_syntax.ella  # print the tokens
```

Native code generation and the later roadmap items are design goals, not
implemented features yet.
