# Getting started with Ella

Ella is a small, statically typed language for learning about interpreters, compiler pipelines, native code, memory management, and staged programming. It runs on Linux x64. The implementation includes a tree-walking interpreter, a typed IR and IR interpreter, an optimizer, and an x64 native backend with a small C runtime. The language is experimental; its syntax and library can change.

This guide introduces the ideas and gives you enough syntax to read and write small programs. The full working syntax reference is [`docs/grammar.md`](docs/grammar.md), and the detailed memory design is in [`docs/memory-model.md`](docs/memory-model.md).

## Run a program

From the repository, run an Ella source file with:

```sh
uv run ella example_syntax.ella
```

The default path checks and lowers the program to IR before interpreting it. Other useful modes are:

```sh
uv run ella --tree example_syntax.ella       # source tree interpreter
uv run ella -O example_syntax.ella            # optimize IR, then run it
uv run ella --native example_syntax.ella      # compile and run native x64 code
uv run ella --ir example_syntax.ella          # show intermediate representation
uv run ella --placement example_syntax.ella   # show compiler memory placement
uv run ella --profile example_syntax.ella     # report arena use after running
```

Native builds need `gcc` and an x64 Linux system. Native executables cannot run code constructed dynamically at run time; that needs a JIT, which Ella does not yet have. The examples in [`test/programs/`](test/programs/) are useful small programs organized by feature.

## A small program

```ella
let greet = (name: str) -> str => "Hello, {{name}}!"

let main = () -> () =>
    let mut numbers = [1, 2, 3]
    numbers += [4]
    for n in numbers
        print(greet("number {{n}}"))
```

`let` introduces a binding. Bindings cannot be reassigned unless declared `mut`. Functions are values: the lambda above takes a `str`, returns a `str`, and uses `=>` for its expression body. A function can also have a block body with indented lines. `main` is the entry point when present. String interpolation uses `{{ expression }}`.

## Syntax and basic semantics

Ella uses indentation to delimit blocks. There are no braces around function, `if`, `match`, or loop bodies; braces are reserved for map and set literals. Comments begin with `#`. Expressions can span lines when nested in brackets, and a final expression gives a block its value.

```ella
let abs = (x: i64) -> i64 => if x < 0 then -x else x

let describe = (x: Optional[i64]) -> str =>
    match x
        case Some(n) => "value {{n}}"
        case None => "empty"

let mut total = 0
for n in 0..10
    if n % 2 == 0 then total += n
print(total, describe(Some(42)))
```

Conditions must be `bool`; there is no truthiness. `match` must cover every possible value, so adding a case is required when a type has an uncovered variant. Ranges use `a..b` for a half-open range and `a..=b` for an inclusive end. `break`, `continue`, `while`, and value-producing `loop` are available.

Functions have explicit parameter and result types at top level. Lambda parameter types can often be inferred from context. A function type looks like `(i64, str) -> bool`. `mut` parameters opt into changing a caller's variable, and the call site marks the argument too: `push(mut items, x)`. Functions and closures capture values by copy. The pipe operator puts its left operand first: `items |> map(f)` means `map(items, f)`.

Structs, enums, tuples, patterns, methods, operator methods, protocols, modules, imports, and generators are available. A file is a module; names starting with `_` are private. See the reference and syntax tour for the full surface area.

## Types, numbers, and arrays

Ella is statically typed and does not implicitly convert between types. Its common scalar types are `i64`, `i32`, `f64`, `f32`, `bool`, and `str`. All integer types are signed. `i8` and `i16` are not implemented. Convert explicitly with functions such as `to_i32(x)`, `to_i64(x)`, `to_f32(x)`, and `to_f64(x)`.

Integer arithmetic is checked: an operation whose exact result does not fit the integer type raises a run-time error rather than wrapping. Integer division and remainder by zero also fail. `/` is true division; `//` rounds toward negative infinity, and `%` follows that division rule. Floating-point operations follow IEEE 754. Array arithmetic is item-wise, supports broadcasting, and native code uses AVX2 where implemented.

Lists have type `[T]`; multidimensional numeric arrays have type `Array[T]`. An array stores a flat row-major buffer plus a shape, so its rank can vary at run time. Create one with `array(values)`, `fill(shape, value)`, or reshape an existing array. Index each dimension with a comma-separated index:

```ella
let mut grid = reshape(array([1, 2, 3, 4, 5, 6]), [2, 3])
grid[1, 2] = 9
print(grid[1, 2], shape(grid), total(grid))
```

Array indices and shapes use `i64`. Slicing arrays currently creates a new array. Numeric array support includes `i64`, `i32`, `f64`, and `f32`; native SIMD coverage varies by type and operation. See the Arrays section in [`docs/grammar.md`](docs/grammar.md#arrays-provisional-syntax) for the current details.

## Values, copies, views, and mutation

Ella has value semantics: assigning, passing, returning, or capturing a copyable value behaves as if an independent value were made. For example, changing `a` below cannot change `b`:

```ella
let mut a = [1, 2]
let b = a
a[0] = 9
print(a, b) # [9, 2] [1, 2]
```

The compiler inserts copies where the program could observe shared storage. It can move a value at its last use when that preserves the same behavior. `mut` allows in-place changes to a binding's own value, including list append, indexed assignment, and struct fields. A `mut` function parameter is an explicit, exclusive way to mutate the caller's value. The checker rejects overlapping mutable arguments it cannot prove safe.

Compiled list and string slices can be *views* of the original storage when the compiler can prove nobody can observe the sharing. Where sharing could change the value semantics, the slice is copied. The AST interpreter keeps the same value semantics. Views retained inside structs or generators still copy, and array slices currently copy. These rules are designed to be invisible to a program; `--placement` and `--profile` help inspect the implementation behavior.

## Memory management: arenas and pools

Ella has no garbage collector. The compiler chooses storage based on how long each value needs to live:

| Storage | Typical use | Lifetime |
| --- | --- | --- |
| Stack | Small fixed-size locals | Function call |
| Scope arena | Dynamic or larger temporary values | Scope exit; loop iterations have scopes |
| `Pool[T]` | Long-lived objects of one type | Until `free(mut pool, handle)` |
| `Arena` | A group of values that should be managed together | Explicit `free(mut arena)` |

The implicit scope arenas are compiler-managed: users generally do not choose one. Returning values are placed so they outlive the callee, and loop iterations reset temporary storage. When a loop replaces a value carried from an earlier iteration, two arenas take turns so discarded versions do not accumulate.

Pools provide stable, long-lived slots. `alloc(mut pool, value)` returns a `Handle[T]`; access uses `pool[handle]`. Each handle has a generation, so using one after its slot has been freed traps instead of accidentally accessing a new object that reused the slot. An `Arena` is a user-managed sub-arena: it holds different types, accessed by `Ref[T]`, and frees all its contents together. Both mechanisms support values that need explicit finalization.

Types marked `nocopy` represent resources or identity-bearing values that cannot be duplicated. Their finalizers run when their owning storage is released. The memory model and compiler placement rules are described in [`docs/memory-model.md`](docs/memory-model.md).

## Staged programming: `quote`, `$`, and `run`

Ella code can construct Ella code. `quote expression` produces a `Code[T]` value, and `$expression` inside a quote splices code into it. A splice outside a quote executes while compiling and inserts the resulting code into the program. That makes compile-time specialization and generated types possible.

```ella
let power = (n: i64, x: Code[i64]) -> Code[i64] =>
    if n == 0 then quote 1 else quote $x * $(power(n - 1, x))

let cube = (x: i64) -> i64 => $(power(3, quote x))
print(cube(4))
```

The recursive generator runs during compilation; the generated multiplication is ordinary program code. Quotes can nest, and bound names are freshened to avoid accidental capture. Types can be inspected and computed at compile time, including structs built with reflection. Code generated and then evaluated with `run` works in the interpreters; native run-time evaluation is not implemented because there is no JIT.

## How Ella compares to other languages

These are useful points of reference, not claims that the languages have the same design:

| Language or family | Similarity | Important difference |
| --- | --- | --- |
| Python | Readable indentation, expression-oriented constructs, chained comparisons, `//` rounding down | Ella is statically typed, has no implicit conversions or truthiness, and checks integer overflow |
| Rust | Static types, enums and exhaustive matching, explicit mutable bindings, value-oriented programming | Ella has no borrow checker or general references; `mut` parameters are checked for exclusivity, and memory is managed by arenas and pools |
| NumPy | Multidimensional arrays, broadcasting, array arithmetic | Ella's arrays are built-in typed values with a focused numeric feature set and x64/AVX2 implementation |
| Zig / macro systems | Compile-time evaluation and generated, specialized code | Ella uses `quote`/`$` multi-stage code values and interpreters; it does not yet compile runtime-generated code natively |
| Ada / region systems | Explicit parameter modes, finalization, and region-based storage ideas | Ella is a learning project with compiler-inferred scope placement and a small runtime, not a mature systems toolchain |

## Major design decisions

* **Small, inspectable compiler:** Python implementation, a typed IR, and a separate native backend make the path from source to machine code visible.
* **Types and errors before execution:** programs are type checked, integer overflow and invalid operations trap, and there are no implicit casts.
* **Value semantics without a tracing collector:** copies define observable behavior; arenas and explicit pools provide deterministic lifetimes, while compiler analysis can remove unobservable copies.
* **Explicit mutability:** bindings are immutable by default, and mutation through function calls is marked at both declaration and call site.
* **Staging as a language feature:** compile-time code uses the same language concepts and interpreter rather than a separate preprocessor syntax.
* **Linux x64 as a learning target:** native code generation, runtime support, and AVX2 arrays are intentionally focused rather than portable across many architectures.

## Missing and limited features

Ella is still evolving. Current gaps include:

* Native execution of code generated at run time (requires a JIT or embedded interpreter).
* Concurrency and async functions; the runtime and memory model are single-threaded.
* Broader portability: the native backend targets Linux x64, with no 32-bit or WASM backend.
* More storable views/references. A value retained by a struct, iterator, or generator cannot generally borrow another value; it is copied instead.
* Some array operations are scalar or unfused, and array slices copy. SIMD coverage depends on element type and operation.
* Integer widths are limited to `i32` and `i64`; `i8` and `i16` are absent.
* The standard library, diagnostics, and tooling are smaller than those of a production language. Check [`TODO.md`](TODO.md) for current gaps and [`docs/grammar.md`](docs/grammar.md#open-questions) for open language questions.

For a compact syntax tour, open [`example_syntax.ella`](example_syntax.ella). For feature-focused programs, browse [`test/programs/`](test/programs/).
