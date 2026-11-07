# Ella developer summaries

These notes explain implementation ideas and invariants behind Ella's major
subsystems. They are aimed at people changing the language, compiler, runtime,
or tooling. The [language reference](../grammar.md) defines user-facing
syntax; [GETTING_STARTED.md](../../GETTING_STARTED.md) is the shorter introduction.

* [Compiler pipeline and IR](compiler_pipeline_summary.md)
* [Type system, generics, and protocols](type_system_summary.md)
* [Staged programming](staging_summary.md)
* [Generators](generators_summary.md)
* [Arrays and SIMD](arrays_summary.md)
* [Modules and packages](modules_summary.md)
* [Memory model](memory_model_summary.md)

Each summary describes current implementation behavior and points to source
files for deeper investigation. The grammar and TODO may contain provisional
syntax, historical plans, and known gaps; check the implementation before
relying on a detail when changing code.
