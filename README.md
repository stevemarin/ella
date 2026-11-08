# Ella

Ella is a small, statically typed programming language, built for fun and
for learning how languages work. The implementation is pure Python. Native
executables for x64 Linux also link a small C runtime.

```ella
enum Shape
    Circle(f64)
    Rect(f64, f64)

let area = (s: Shape) -> f64 =>
    match s
        case Circle(r) => pi * r * r
        case Rect(w, h) => w * h

let main = () -> () =>
    let shapes = [Circle(1.0), Rect(2.0, 3.0)]
    let areas = shapes |> map(area)
    print("total area: {{sum(areas)}}")
    let counts = {"circle": 1, "rect": 1}
    for name in counts
        print(name, counts[name])
```

## What makes it interesting

* **The whole pipeline is visible.** Ella has a tokenizer, parser, type
  checker, two interpreters (one walking the syntax tree, one running a
  typed IR), an optimizer, and an x64 code generator. They all run the same
  programs and give the same output, byte for byte.
* **Memory safety without a garbage collector.** Values behave as if they
  were always copied, and the compiler decides where each one lives: in an
  arena per function call or loop iteration, freed all at once. Pools and
  explicit arenas cover values that need to live longer.
* **Code that writes code.** `quote` turns code into a value and `$` splices
  it back in. A splice outside a quote runs while compiling, so a program
  can generate specialized functions or even new types.
* **Built-in number arrays.** `Array[f64]` and friends, with broadcasting,
  which native code runs with AVX2 SIMD instructions. Types can give an
  array's shape (`Array[f64; N, 3]`): mismatches are caught while compiling
  where possible and when running otherwise, and `N` is a number you can
  loop to.
* **A familiar feel.** Indentation-based syntax, everything is an
  expression, exhaustive `match`, generics with constraints, methods,
  generators, modules, and a standard library written in Ella. Functions
  can take apart their arguments with patterns (`((a, b)) => a + b`), and
  `map` and `filter` go through anything a `for` loop can (lists, ranges,
  strings, generators). Lazy versions are there when wanted:
  `count(1, 1) |> iters.map(square) |> take(3) |> collect`.

Some deliberate choices: no implicit type conversions, only `bool` is
truthy, integers are signed and trap on overflow, and bindings can't change
unless they're marked `mut`.

## Getting started

You need [uv](https://docs.astral.sh/uv/) (it fetches Python 3.15), and gcc
for native executables.

```sh
uv run ella example_syntax.ella           # run a program
uv run ella --native -O prog.ella         # compile it to native code and run it
uv run ella -o prog prog.ella             # build a native executable
uv run ella prog.ella -- a b              # pass the program arguments
uv run ella --ir -O prog.ella             # see the optimized IR (or --asm for assembly)
uv run ella --help                        # every option
```

Then read [GETTING_STARTED.md](GETTING_STARTED.md), a tour of the
language in one sitting.

For editing, [editors/vscode/](editors/vscode/) has a VS Code extension
with highlighting, errors as you type, hover, go to definition and
completion (`make install-extension`).

## Documentation

* [GETTING_STARTED.md](GETTING_STARTED.md): an introduction to the language.
* [example_syntax.ella](example_syntax.ella): a quick tour of the syntax.
* [docs/grammar.md](docs/grammar.md): the language reference.
* [docs/memory-model.md](docs/memory-model.md): how memory works without a GC.
* [docs/summaries/](docs/summaries/): short notes on each part of the compiler.
* [docs/ideas/](docs/ideas/): ideas being explored (a JIT, const generics, SQLite).
* [TODO.md](TODO.md): what's in progress, next, and missing.

Files named `*-Claude.md` ([README-Claude.md](README-Claude.md),
[TODO-Claude.md](TODO-Claude.md)) and [CLAUDE.md](CLAUDE.md) are detailed
working notes for the AI assistant that helps build Ella. They're fine
to read, but more exhaustive than friendly.

## Status

All seven planned stages are done: front end, tree interpreter, IR and IR
interpreter, optimizer, native code, memory model, and staged programming.
Since then Ella has gained methods and constraints, modules and packages,
generators, arrays with SIMD, stack traces, a growing standard library and
a language server. It's an experiment, so syntax and library may still
change. See [TODO.md](TODO.md) for what's next.

## Development

```sh
make check    # lint, format check, strict type check, and all tests
make bench    # time the benchmarks
make          # list the other recipes
```
