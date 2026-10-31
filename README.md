# Ella

A Linux, x64 language for fun and learning, implemented in pure Python.

Ella is an experiment in building a language from the tokenizer through to native
code generation. The compiler will use a sequence of representations, starting
with one small intermediate representation (IR). The first consumer of that IR
will be an interpreter; a later compiler will lower it to x64 assembly.

## Goals

* Keep the implementation as simple as possible without hiding important ideas.
* Use a lightweight, extensible syntax and grammar.
* Learn how interpreters, IRs, optimization, and native code generation fit together.
* Explore staged programming: distinguish computations performed during
  compilation from code generated to run later.
* Compile to x64 on Linux.
* Explore a non-moving garbage collector and arenas. Arenas can group objects
  with related lifetimes, and the collector can reclaim an arena as a unit.
* Leverage modern Python as much as possible in the design

## Design

### Initial compiler pipeline

```text
source → tokenizer → parser → AST → IR → IR interpreter
                                    └→ optimizer → x64 code generation
```

Start with one IR. Add another representation only when a later lowering needs
to make a real change in abstraction, such as replacing structured operations
with explicit memory and machine-level operations.

The initial IR will be a typed control-flow graph:

* Functions contain basic blocks.
* Blocks contain simple, three-address instructions and end in a terminator
  (`branch`, `jump`, or `return`).
* Values have explicit types and unique IDs.
* Control-flow joins use explicit value merges (eventually SSA `phi` nodes, or
  block parameters if that proves simpler).
* Side effects such as calls and memory writes are explicit operations.

The IR interpreter defines and exercises the meaning of these operations. An
IR verifier will check structural and type rules before interpretation,
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

* Add small, understandable passes such as constant folding, constant
  propagation, and dead-code elimination.
* Keep passes separate and inspectable; verify IR before and after each pass.
* Later, experiment with implementing passes in Ella and compiling them with
  Ella itself.

### 5. Native code generation

* Lower the verified IR to x64 assembly.
* Define the calling convention and runtime support needed by generated code.
* Compare native execution against the IR interpreter on the same programs.

### 6. Staged programming and runtime memory

* Add explicit compile-time and run-time evaluation rules.
* Represent generated run-time computations as residual IR.
* Explore allocation, a non-moving garbage collector, and arenas once the
  language has the functions and object model that need them.

## Current status

The project is at the tokenizer and parser stage. The IR and later roadmap items
are design goals, not implemented features yet.
