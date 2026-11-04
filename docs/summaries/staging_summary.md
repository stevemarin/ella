# Staged programming

Ella treats code as a typed value and supports compile-time as well as
run-time staging. The syntax and examples are in
[`docs/grammar.md`](docs/grammar.md) §11. The implementation is primarily in
[`src/ella/staging.py`](src/ella/staging.py), with stage checks and type
metadata in [`src/ella/checker.py`](src/ella/checker.py).

## Core model

`quote e` constructs code for `e` rather than evaluating it. If `e` has type
`T`, its code has type `Code[T]`. `$c` splices code computed at the preceding
stage into the quote being built. Stages nest without a fixed limit: entering
a quote moves one stage later; evaluating its splice moves one stage earlier.
`run(c)` evaluates a `Code[T]` at the stage where `run` appears.

A splice outside every quote runs at stage -1, during compilation. Its
resulting syntax replaces the splice before normal checking/lowering, so the
program that reaches IR contains residual code rather than the compile-time
generator. Compile-time evaluation uses the AST interpreter. `--expanded`
shows the result of top-level expansion and is the best first debugging tool
for unexpected generated syntax.

This distinction is important: staging is a language feature, not merely an
optimizer or compiler macro pass. `quote` can build code inside a running
program for later `run` in an interpreter, while an out-of-quote splice is
evaluated by the compiler.

## Binding, persistence, and hygiene

Every variable belongs to the stage where it was bound. A value from an
earlier stage can persist into later-stage code as a literal/constant, such
as `let n = 3; quote n + 1`. A variable cannot be used at an earlier stage
than its binding because its value does not exist yet. Only copyable data
values persist this way; functions (except top-level functions available at
each stage), handles, and non-copyable values are restricted.

When code is built, binders inside the quote are renamed to fresh names.
This alpha-renaming keeps spliced code from capturing a same-named variable
at the insertion site. Top-level names remain resolved to their defining
module, including private helpers, so generated code does not silently bind
to a caller's similarly named function. Stage validation and name resolution
are intertwined; changes should preserve checker metadata used by
`CodeBuilder`.

## Types as values and reflection

`type T` constructs a value of type `Type[T]`. These values allow generated
code to depend on concrete types. Type splices insert a known type into
quoted syntax; field splices such as `value.$name` generate member access
whose field is selected from a string. Reflection built-ins inspect field
names, field types, variants, and type kinds.

Some reflected information has a type that is not known until compile-time
code runs. Such code can be built and checked after expansion, but it cannot
always be executed as ordinary run-time code. `new_struct` creates a struct
type at compile time; its identity is based on its name and matching
definition. It allows computed types even though arbitrary spliced types in
struct/enum declarations are not supported.

## Implementation phases

The checker records quote levels, resolved names, binding kinds, and known
types. `staging.py` walks syntax trees to build quoted code, substitutes
splices, reifies earlier-stage values, and gives bindings fresh names. The
expander evaluates compile-time splices with the AST interpreter, merges
checker tables across repeated checks, and replaces splices with generated
syntax. The expanded program then goes through ordinary checking and
compilation.

Code generation while compiling must not accidentally depend on a backend
representation: a splice should observe language values and return syntax or
type values using the staging API. The IR lowering has corresponding code
building support for quotes that remain in the program.

## Runtime boundary and limitations

The AST and IR interpreters can execute dynamically built code through
`run`. Native code currently supports compile-time expansion but not
run-time code construction/execution; reaching unsupported staging operations
in a native executable raises a runtime error. This is a missing JIT or
runtime compiler, not a restriction on compile-time splices.

The checker currently rejects a quote inside a function defined within a
splice when that quote refers to the surrounding quote's variables. Such a
function needs an explicit environment/capture path that the current builder
does not provide. Compile-time top-level variables also cannot be read by
stage -1 code because they belong to run-time stage 0. Types produced by
reflection may not be persistable when their concrete type is not statically
known.

When modifying staging, test nested quotes and splices, cross-stage
persistence, shadowing/hygiene, private names, generated types, errors after
expansion, and behavior in both interpreters and native compile-time builds.

