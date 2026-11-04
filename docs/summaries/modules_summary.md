# Modules and packages

Ella treats each source file as a module. Imports are resolved and linked
before type checking/lowering, producing one whole-program syntax tree with
qualified names. The language rules are in [`docs/grammar.md`](docs/grammar.md)
§3; the implementation is in [`src/ella/modules.py`](src/ella/modules.py).

## Names and module identity

The entry file's directory is the module root. A file such as
`geo/poly.ella` has module name `geo.poly`; its top-level `area` becomes
`geo.area` in the linked program. Variants and types are qualified too. The
entry module keeps its original names, preserving single-file programs.

An import can name a module (optionally with an alias) or one item. The
linker resolves references whether written locally, through a module name,
through an alias, or through a prelude module. It tracks lexical locals so a
local binding can shadow a top-level import. The standard library's prelude
modules are implicitly available; `iters` is intentionally explicit.

## Loading and linking

The linker loads only modules reachable from the entry program's imports,
plus prelude modules whose public names are used. It resolves imports,
rewrites names into unique qualified names, attaches source-file identity to
spans, then returns one program for the checker and later compiler stages.
Later phases therefore operate on a unified namespace rather than resolving
module syntax themselves.

Modules are found first relative to the program root, then through package
paths passed by CLI options or `ELLA_PATH`, and finally in the bundled
standard library. A package is a module found on a package path with possible
child modules. Error locations preserve paths, including `<std>` for
standard-library files.

Imports cannot form cycles. The linker detects a cycle while loading and
reports the chain. Whole-program linking also allows unused module functions
to be removed later by optimization, provided staging does not require
dynamic lookup of them.

## Visibility and mutability

Names beginning with `_` are private to their module and descendants in the
module tree. This convention also applies to fields and methods. A struct
with private fields can only be constructed or destructured within the
allowed module scope. Imported module variables are read-only from outside
their defining module; functions can still update their own module state.
These checks happen during linking/resolution and type checking, not in the
native runtime.

## Source locations and tools

Every loaded module's spans retain its filename, so checker and runtime
errors can point back to the right source. The language server uses the same
linker and can read unsaved open documents, which lets errors in importers
update when an imported buffer changes. For editor analysis, missing or
temporarily malformed files need to be handled without making the batch
compiler's normal first-error behavior confusing.

## Contributor invariants and gaps

* Keep name resolution lexical: a local binding shadows a module/prelude
  name, and aliases must resolve to the same item as the original module.
* Preserve file spans through name rewriting so diagnostics, stack traces,
  and generated code report the source module.
* Apply privacy checks consistently to names, variants, fields, and methods.
* Maintain deterministic import/load ordering; compile-time splices may
  print or define types, so module order is observable during expansion.
* Do not make later compiler phases depend on import syntax unless their
  inputs are changed deliberately: linking currently erases module
  boundaries into qualified names.

Current limitations include no import cycles, no re-exports, one import
target per statement, and no general third-party package registry. See
TODO.md and the grammar's module open questions for changes under
consideration.

