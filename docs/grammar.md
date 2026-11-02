# Ella language reference (draft)

This is the working definition of Ella's syntax and core semantics. The
tokenizer and parser should follow it; when the implementation finds a problem
with a rule, update this file first.

Items marked **(provisional)** are reasonable defaults that have not been
exercised yet. The [open questions](#open-questions) at the end are undecided.

## Contents

1. [Lexical structure](#1-lexical-structure)
2. [Layout and blocks](#2-layout-and-blocks)
3. [Program structure](#3-program-structure)
4. [Bindings and assignment](#4-bindings-and-assignment)
5. [Functions](#5-functions)
6. [Control flow](#6-control-flow)
7. [Patterns](#7-patterns)
8. [Operators](#8-operators)
9. [Types](#9-types)
10. [Built-ins](#10-built-ins)
11. [Stages](#11-stages)
12. [Grammar summary](#12-grammar-summary)
13. [Open questions](#open-questions)

## 1. Lexical structure

Source files are UTF-8. Outside string literals and comments, only ASCII is
allowed. Lines end with `\n` or `\r\n`.

### Whitespace and comments

* Spaces separate tokens. **Tab characters are an error** anywhere outside
  string literals and comments.
* `#` starts a comment that runs to the end of the line. `#` inside a string is
  not a comment.
* Lines that are blank or contain only a comment are ignored entirely (they do
  not affect indentation).

### Identifiers

```text
identifier = [A-Za-z_][A-Za-z0-9_]*
```

`_` on its own is an ordinary identifier token. The parser gives it special
meaning in patterns (wildcard). Type names such as `i64` and `str` are
identifiers, not keywords.

### Keywords

```text
and       assert    break     case      comptime  continue  else
enum      false     for       if        import    in        let
loop      match     mut       nocopy    not       or        quote
return    struct    then      true      type      where     while
```

Reserved for later, not yet given a grammar: `comptime`, `import`.

### Labels

A label is an identifier and a colon written before a loop: `outer: for ...`.
Labels name loops for `break` and `continue` ([§6](#loops)). There is no
label token; the parser recognizes `IDENT ":"` followed by `for`, `while` or
`loop`.

### Operators and punctuation

Longest match wins (`//=` before `//` before `/`).

```text
+    -    *    /    //   %    **
&    |    ^    ~    <<   >>
==   !=   <    <=   >    >=
=    +=   -=   *=   /=   //=  %=
->   =>   |>   ..   ..=  ?
.    ,    :    (    )    [    ]    $
```

`{`, `}`, `;`, `!` (alone), `@` and `` ` `` are not tokens; they are errors
outside strings and comments.

### Integer literals

```text
decimal = [0-9] ( '_'? [0-9] )*
hex     = '0' [xX] [0-9a-fA-F] ( '_'? [0-9a-fA-F] )*
```

* `_` may appear only between digits: `1_000_000`, `0xFF_FF`.
* Leading zeros on decimal literals other than `0` itself are an error
  (`007`), to avoid confusion with C octal.
* There are no negative literals: `-5` is unary minus applied to `5`.
* A literal must fit in `i64`. The special case `-9223372036854775808` is
  handled by the parser folding unary minus on a literal **(provisional)**:
  `-` directly before an integer literal makes one negative literal, unless
  the literal is followed by an operator that binds tighter than prefix `-`
  (`**`, a call, an index, `.` or `?`). So `-2 ** 2` is still `-(2 ** 2)`.

### Float literals

```text
float = decimal '.' decimal ( [eE] [+-]? decimal )?
```

A digit is required on both sides of the `.`: `1.0`, `0.5`, `2.5e-3`. `1.`,
`.5`, and `1e3` are not floats. Because of this rule, `0..n` tokenizes as `0`,
`..`, `n`.

A number immediately followed by a letter, digit or `_` is an error, so `1e3`,
`12abc` and `1_` are rejected rather than split into two tokens.

### String literals

* `"..."` and `'...'` are single-line strings. A raw newline inside one is an
  error.
* `"""..."""` and `'''...'''` are multi-line strings. Their content is kept
  exactly as written, including leading indentation and newlines (no dedent).
* All four forms support the same escapes and interpolation. Double and single
  quotes mean exactly the same thing; pick whichever avoids escapes. A string
  ends only at the quote it started with, so the other kind may appear inside
  unescaped: `"it's"`, `'say "hi"'`.

Escapes:

| Escape | Meaning |
| --- | --- |
| `\\` | backslash |
| `\"` `\'` | double quote, single quote (either works in any string) |
| `\n` `\r` `\t` | newline, carriage return, tab |
| `\0` | NUL |
| `\xHH` | byte value `HH` (two hex digits, must be ≤ `7F`) |
| `\u{H...}` | Unicode code point, 1–6 hex digits |
| `\{` | literal `{` (so `"\{{x}}"` is the text `{{x}}`) |

Any other `\` sequence is an error.

### String interpolation

`{{ expr }}` inside a string evaluates `expr` and inserts its text form. The
expression may be any Ella expression, including calls and nested strings:

```ella
print("Hello, {{name}}!")
print("{{ len(names) }} names, first is {{ names[0] }}")
print("{{ greet("bob") }}")
```

* A single `{` or `}` is ordinary text. Only `{{` starts interpolation.
* The interpolation ends at the first `}}` that is not inside a nested
  bracket or string.
* In a single-line string, the expression may not contain a newline.
* `{{ }}` with no expression is an error.

## 2. Layout and blocks

Indentation defines blocks, as in Python.

* The tokenizer tracks a stack of indentation widths, starting at `[0]`.
* At the start of each logical line, a deeper indent emits `INDENT`; a
  shallower one emits one `DEDENT` per level popped. Dedenting to a width that
  is not on the stack is an error.
* Any consistent increase is a new level; 4 spaces is the convention but not
  enforced.
* Every logical line ends with a `NEWLINE` token. At end of file the tokenizer
  emits `NEWLINE` (if needed), the remaining `DEDENT`s, then `EOF`.
* **Inside `(` `)` and `[` `]`, newlines and indentation are ignored.** This is
  the only way to continue an expression over several lines.
* **A line may not end with a binary operator.** This is a parse error with
  the hint "wrap the expression in parentheses":

  ```ella
  let total = (xs
      |> filter(iseven)
      |> sum())
  ```

### Blocks

A block is one or more expressions on separate lines, indented one level
deeper than the line that opens it. Its value is the value of its last
expression. A block whose last expression is a loop, an assignment, or
another `()`-valued expression has value `()`.

Wherever this document says "body", either form is allowed:

* **inline**: an expression on the same line, or
* **block**: end the line, then an indented block.

Bodies appear after `=>`, `then`, `else`, `case ...`, `for ... in expr`,
`while expr`, `loop`, `where`, and `match expr` (whose body is a list of
cases, never inline).

There are no colons at the ends of block-opening lines.

## 3. Program structure

A source file is a sequence of top-level expressions.

1. Top-level `let` names are visible throughout the file, regardless of order.
   This is what lets top-level functions call each other in any order.
   Reading a top-level non-function value before its `let` has run is an
   error: at compile time when the top-level code itself does it, and at run
   time when a function that reads it is called too early.
2. A top-level name keeps one type, even if it is bound again by a later
   `let`, because functions anywhere in the file may refer to it. (Local
   names may be rebound with a different type.)
3. The top-level expressions run in order, like a script.
4. Afterwards, if the file defines a top-level `main`, it is called.
   `main` has type `() -> ()`. Command-line arguments and exit codes come later.

## 4. Bindings and assignment

### `let`

```ella
let x = 5
let names: [str] = ["bob", "steve", "jane"]
let mut count = 0
```

* `let` introduces a new name in the enclosing block. Bindings are
  **immutable** unless marked `mut`.
* The type annotation is optional where the type checker can infer it (for
  now: from the value). Function parameters always need annotations.
* `let` is an expression; it evaluates to the bound value.
* `let` inside a nested expression still binds in the enclosing block.

### `let` chains

Every target in a `let` chain is a **new** binding, and `mut` applies to one
target only:

```ella
let y = mut x = z = 5   # three new bindings: y and z immutable, x mutable
```

This parses as `let y = (mut x = (z = 5))` with every target being a new
binding. To assign to an existing variable inside a `let`, use parentheses:
`let y = (x = 5)`.

### Shadowing

A `let` may reuse a name already in scope, in the same block or an inner one.
The new binding hides the old one from that point on:

```ella
let x = 4
let x = x + 1   # new x is 5
```

### Assignment

```ella
x = 10
x += 1
xs[0] = 7
p.x = 1.5
```

* `name = expr` assigns to an **existing** binding, which must be `mut`.
  Assigning to an immutable binding or an undefined name is a compile error.
* Targets are names, index expressions (`a[i]`, `pool[h]`) and fields
  (`p.x`), in any combination: `world.players[i].score += 1`.
* Assignment is right-associative and evaluates to the assigned value.
* Compound operators: `+= -= *= /= //= %=`. `x op= e` means `x = x op e`, with
  `x` evaluated once.
* `mut` is deep: if `names` is not `mut`, `names[0] = "x"` is an error.

### Values

Ella uses **pass-by-value**. Binding, passing an argument, returning, and
capturing in a closure all copy, as far as any program can tell. The AST
interpreter copies eagerly (lists are Python lists that get copied). The
compiler copies only where a program could tell the difference, and
otherwise shares values and changes lists in place; see
[memory-model.md](memory-model.md).

Concretely, reading a variable or a list element gives a copy, so changing a
list through one name is never visible through another:

```ella
let a = [1, 2]
let mut b = a
b[0] = 100      # a is still [1, 2]
```

## 5. Functions

Functions are lambda expressions, usually bound with `let`:

```ella
let add = (x: i64, y: i64) -> i64 => x + y

let fibonacci = (n: i64) -> i64 =>
    where
        n >= 0
        return >= 0
    match n
        case 0 | 1 => 1
        case _ => fibonacci(n - 1) + fibonacci(n - 2)
```

* The form is `( params ) -> ReturnType => body`. The parameter list is always
  parenthesized, even with one parameter.
* Each parameter is `name: Type`. Parameters are immutable, except `mut`
  ones (below).
* Functions take multiple arguments; there is no currying.
* `-> ReturnType` is required for now. Untyped lambdas such as `x => x + 2`
  parse, but the type checker rejects them until there is type inference.
* The body extends as far to the right as possible.
* A `let` bound to a lambda can refer to its own name inside the body
  (automatic `let rec`). Combined with [§3](#3-program-structure), top-level
  functions can be mutually recursive.
* Lambdas capture variables from enclosing scopes **by copy**, at the moment
  the lambda is evaluated. Later changes to those variables aren't seen, and
  the captured copies are immutable inside the lambda. Top-level names are not
  captured: a function sees their current values when it runs, which is what
  lets top-level functions refer to each other.
* A `break` or `continue` can't leave the function it is in.

### `mut` parameters

A parameter marked `mut` can be changed by the function, and the caller
sees the change. The argument is marked `mut` too:

```ella
let push_twice = (mut xs: [i64], x: i64) -> () =>
    xs += [x, x]

let mut nums = [1]
push_twice(mut nums, 5)   # nums is now [1, 5, 5]
```

* The argument for a `mut` parameter must be marked `mut`, and other
  arguments must not be. It must be a `mut` variable or an element of one
  (`mut grid[i]`), and its type must be exactly the parameter's.
* Arguments are passed in and out ("value-result"). The parameter starts
  with the argument's value, and when the function returns, its final
  value is assigned back to the argument as `place = value` would, using
  the indexes evaluated for the call (and checking them again). Compiled
  code passes the variable itself instead, which only a global variable
  could tell apart, so a global is passed as a copy.
* A variable passed as `mut` can't be used in any other argument of the
  same call, unless that argument's type has no lists: `f(mut xs, len(xs))`
  is fine, `f(mut xs, xs)` isn't. This is checked by name, so
  `swap(mut a[0], mut a[1])` is rejected too **(provisional)**.
* `mut` parameters are part of a function's type: `(mut [i64], i64) -> ()`.
* A closure captures a `mut` parameter by copy, like any other variable.

### `where` clauses

`where` is a block of conditions checked at run time. If present, it must be
**the first clause of a function's block body**.

* Each line is a `bool` expression.
* Conditions that mention `return` are **postconditions**; `return` there
  names the function's result. They are checked just before returning.
  All others are **preconditions**, checked on entry.
* A failed condition is a run-time error reporting the condition's source:
  `precondition failed: n >= 0`.

Inside `where`, `return` is parsed as a name, not as an early exit.

### `return`

`return expr` exits the innermost enclosing function with `expr`. `return`
alone returns `()`. Its own value has type "never" (it does not produce one).

### Calls

`f(a, b)` calls `f`. Trailing commas are allowed in argument and parameter
lists. Named arguments are not supported (yet).

## 6. Control flow

### `if`

```ella
let iseven = (n: i64) -> bool => if n % 2 == 0 then true else false

if x == 4 then
    print("x={{x}}")
else if x == 5 then
    print("five")
else
    print("not 4")
```

* Conditions must be `bool` (only booleans are truthy).
* `else` may be on the same line as the `then` body or start its own line at
  the same indentation as the `if`.
* `if` without `else` has type `()`; the value of its `then` body is discarded.

### `match`

```ella
match result
    case Ok(value) => value
    case Error(e)
        print("failed: {{e}}")
        0
```

* `match expr` is followed by an indented list of `case` lines.
* Each case is `case pattern [if guard]`, then either `=> expr` on the same
  line or an indented block.
* Cases are tried in order. If none matches, it is a run-time error
  (exhaustiveness checking comes later).

### Loops

```ella
for name in names
    print("Hello, {{name}}!")

for i in 0..len(xs)
    print(xs[i])

while count < 10
    count += 1

outer: for row in grid
    for cell in row
        if cell == 0 then continue outer
        if cell < 0 then break outer

let found = loop
    if done() then break 42
```

* `for pattern in expr` iterates over a list, string, or range.
* `while cond` runs while `cond` (a `bool`) is true.
* `loop` repeats forever until `break`.
* `for` and `while` have value `()`. `loop` has the value given to the
  `break` that ends it (`break value`); plain `break` gives `()`. A `break`
  with a value out of a `for` or `while` is an error.
* `for` over a string gives its characters as one-character strings. Each
  iteration binds the pattern in a fresh scope.
* `break` and `continue` apply to the innermost loop, or to the loop named by
  a label: `break outer`, `continue outer`, `break outer value`.
* A label is written as `name:` directly before `for`, `while`, or `loop`.
* After `break` or `continue`, a name is the label only if it names an
  enclosing loop in the same function; otherwise it is the start of the
  break's value. To break with a variable that has the same name as a label,
  use parentheses: `break (outer)`.

## 7. Patterns

Patterns appear after `case`, in `for`, and (later) in `let`.

| Pattern | Example | Matches |
| --- | --- | --- |
| wildcard | `_` | anything, binds nothing |
| literal | `0`, `"x"`, `true` | an equal value |
| range | `0..10`, `0..=9` | a value in the range |
| binding | `n` | anything, binds it to `n` |
| alternatives | `0 \| 1` | either side; both sides must bind the same names |
| variant | `Ok(v)`, `Some(x)`, `None` | an enum variant, destructuring its fields |
| tuple | `(a, b)` | a tuple, element-wise |

Whether a bare name like `None` is a variant or a new binding is decided by
name resolution after parsing, as in Rust: the parser produces a name
pattern, and the resolver checks whether the name refers to a variant.

Negative literal patterns (`-1`) are allowed as a special case.

## 8. Operators

### Precedence

From loosest to tightest:

| Level | Operators | Associativity |
| --- | --- | --- |
| 1 | `=` `+=` `-=` `*=` `/=` `//=` `%=` | right |
| 2 | `=>` (lambda body) | right |
| 3 | `or` | left |
| 4 | `and` | left |
| 5 | `not` (prefix) | — |
| 6 | `==` `!=` `<` `<=` `>` `>=` `in` `not in` | chained |
| 7 | `\|>` | left |
| 8 | `..` `..=` | none |
| 9 | `\|` | left |
| 10 | `^` | left |
| 11 | `&` | left |
| 12 | `<<` `>>` | left |
| 13 | `+` `-` | left |
| 14 | `*` `/` `//` `%` | left |
| 15 | `-` `~` (prefix) | — |
| 16 | `**` | right |
| 17 | call `f()`, index `a[i]`, member `a.b`, `?` (postfix) | left |

Consequences worth knowing:

* `not a == b` is `not (a == b)` (as in Python).
* `-2 ** 2` is `-(2 ** 2)`, i.e. `-4` (as in Python).
* `x in 0..n` is `x in (0..n)`; `0..n+1` is `0..(n+1)`.
* `xs |> sum() == 10` is `(xs |> sum()) == 10`; `a + b |> f()` is `f(a + b)`.

### Arithmetic

* Operands must have the same type; there are no implicit conversions.
* `+` also concatenates two `str`s or two lists.
* `/` is true division and always returns `f64`, for `i64` or `f64` operands.
* `//` is integer division that **rounds toward negative infinity** (as in
  Python): `-7 // 2 == -4`. `%` matches it, so
  `a == (a // b) * b + a % b` always holds and `%` has the sign of `b`.
  Both are defined for integers only.
* `**` works on `i64` (a negative exponent is a run-time error) and on `f64`.
* Integer division or modulo by zero is a run-time error.
* `i64` arithmetic traps on overflow: if the exact result of `+`, `-`, `*`,
  `//`, `**`, `<<` or unary `-` doesn't fit in an `i64`, it is a run-time
  error.
* `f64` arithmetic follows IEEE 754 and never fails: `1.0 / 0.0` is infinity
  and `0.0 / 0.0` is NaN.

### Comparisons

Comparisons chain as in Python: `a < b <= c` means `a < b and b <= c`, with
`b` evaluated once. The parser represents a chain as a single node holding
the operands and operators, not as nested binary nodes.

`in` and `not in` test membership in a list, string (substring), or range.

* `==` and `!=` compare structurally: lists, tuples and enum values are equal
  when their parts are. Both sides must have the same type, and functions
  can't be compared.
* `<`, `<=`, `>`, `>=` work on `i64`, `f64` and `str` (by code point).

### Logic and bits

* `and`, `or`, `not` take `bool` operands. `and` and `or` short-circuit.
* `& | ^ ~ << >>` work on integers. `>>` is an arithmetic shift.
* `a << b` means `a * 2 ** b`, so it traps on overflow like `*`: `1 << 62` is
  fine, but `1 << 63` is an error because the result doesn't fit. (`-1 << 63`
  is the smallest `i64`.) `& | ^ ~ >>` can never overflow.
* A shift amount outside `0..64` is a run-time error.

### Pipe

`x |> f(a, b)` means `f(x, a, b)`: the left side becomes the **first**
argument. `x |> f` means `f(x)`. The right side must be a name or a call.

### Error propagation

Postfix `?` on a `Result`: `Ok(v)?` gives `v`; `Error(e)?` returns `Error(e)`
from the enclosing function. On an `Optional`: `Some(v)?` gives `v`; `None?`
returns `None`. The enclosing function's return type must be compatible.

### Indexing and ranges

* `a[i]` indexes lists and strings with an `i64`. Out-of-range or negative
  indexes are a run-time error **(provisional; no Python-style negative
  indexing)**. Indexing a string gives a one-character string; strings count
  Unicode code points, and can't be assigned into.
* `a..b` is the half-open range `a <= i < b`; `a..=b` includes `b`.

## 9. Types

```text
i64  f64  bool  str              built-in scalars
()                               unit
[T]                              list of T
(T, U)                           tuple
(T, U) -> R                      function (parameters always parenthesized)
(mut T, U) -> R                  function with a `mut` parameter
Result[T, E]   Optional[T]       generic types use [ ]
Point   Pair[T]                  structs and enums, by name
Pool[T]   Handle[T]              a pool of values, and a handle to one
Code[T]                          code that gives a T, for a later stage
Type[T]                          the type T as a value: `type T`
Type   Code                      a type, or code, whose type is only known later
$(t)                             a type computed while compiling (§11)
```

* All integer types are signed. More widths (`i8`–`i32`, `f32`) and SIMD
  vector types come later.
* Types appear after `:` in annotations, after `->` in lambdas, and inside
  other types. The parser uses a separate type grammar there, so `[str]` (a
  type) and `[1, 2]` (a list literal) do not conflict.
* In a lambda, the return type is parsed with the type grammar up to the
  `=>`, so a function returning a function is
  `(x: i64) -> (i64) -> i64 => ...`.

### Type checking

Programs are type checked before they run. Function parameters and results
must be annotated; everything else is inferred from values and how they are
used:

```ella
let x = 1 + 2            # i64
let mut seen = []        # a list of something not known yet...
seen = seen + [5]        # ...which this decides is [i64]
let found = None         # Optional of something; used as Optional[str] later
```

* A type that nothing decides defaults to `()`: `let xs = []` is a `[()]`.
* An operator whose meaning depends on its operand's type needs that type to
  be known: with `let xs = []` alone, `-xs[0]` is an error.
* `return`, `break`, `continue`, and a `loop` that never breaks have no
  value. They fit wherever a value of any type is expected, as in
  `if n > 0 then n else return 0`.
* The bodies of `for`, `while` and `loop`, the `then` of an `if` without
  `else`, and all but the last line of a block can have any type; their
  values are discarded.
* `print`, `len` and variant constructors like `Some` can only be called, not
  used as values (for now).
* `Error` used as a type means `str` for now **(provisional)**, so
  `Result[f64, Error]` is `Result[f64, str]`; see the open questions.
* Enums, structs and top-level functions can be generic.

### Enums **(provisional syntax)**

```ella
enum Shape
    Circle(f64)
    Rect(f64, f64)
    Empty

enum Optional[T]
    Some(T)
    None
```

Variants are constructed like calls (`Circle(1.0)`, `Empty`) and destructured
in patterns. `Result` and `Optional` are built-in enums with variants
`Ok`/`Error` and `Some`/`None`.

### Structs **(provisional syntax)**

```ella
struct Point
    x: f64
    y: f64

struct Pair[T]
    first: T
    second: T

let mut p = Point(1.0, 2.0)
p.x = 3.0
print(p.x + p.y)       # 5.0
match p
    case Point(x, y) => print(x * y)
```

* A struct is made by calling its name with its fields' values, in order,
  and its fields are read with `.name`. Changing a field (`p.x = v`)
  needs a `mut` variable, as changing a list item does.
* A struct is like an enum with one variant named after it, whose fields
  have names. So patterns, `==` and the text form (`Point(3.0, 2.0)`) work
  the same way.
* Structs are values, like everything else: `let q = p` copies.

### Values that can't be copied **(provisional syntax)**

A `nocopy struct` is for values with an identity, which a copy would
duplicate: a resource that must be released once, say. Its values are
never copied, and it may have a `finalize` function, which runs on each
value when the value dies:

```ella
nocopy struct Logger
    name: str
    finalize = (log: Logger) -> () => print("closing {{log.name}}")

let work = () -> () =>
    let log = Logger("app")
    print("working")
work()                 # prints: working, closing app
```

* A value of a nocopy type (a nocopy struct, a pool, or anything holding
  one) is made where it stays. It can be bound with `let`, passed to
  functions (read-only, or as `mut`), read field by field, compared and
  printed, held by another nocopy struct, and moved into a pool with
  `alloc`. A function can return a new one, or one of its own local
  variables declared outside loops (which moves it to the caller).
* It can't be copied: not bound from another variable, assigned, put in a
  list, captured by a closure, returned from a parameter, or used with a
  generic function.
* A struct with a nocopy field must be nocopy itself. A nocopy struct needs
  at least one field; one with a `finalize` function must be top-level and
  not generic.
* **When values die.** A value dies at the end of the function call or loop
  iteration it is made in (a `let` belongs to the iteration it is in), or at
  the end of the program for top-level variables. A value that is
  returned, or given to `break`, belongs to the call or loop iteration it
  goes to; one moved into a pool dies when it is freed, or when the pool
  dies. Values that die together are finalized newest first: a struct
  before the fields it holds.
* If the program stops with an error, finalizers don't run.

### Generic functions **(provisional syntax)**

A top-level function can have type parameters, after its name:

```ella
let first[T] = (xs: [T]) -> T => xs[0]
let swap[A, B] = (p: Pair[A, B]) -> Pair[B, A] => Pair(p.second, p.first)

print(first([1, 2]))       # T is i64
print(first(["a"]))        # T is str
```

* The type arguments are inferred from each use. Each different set of
  them makes a separate copy of the function when compiling.
* Inside the function, a type parameter is a type of its own, which only
  matches itself. Values of type `T` can be passed around, stored and
  printed; anything more needs a *constraint*.
* Type parameters can't be values that can't be copied.

**Constraints.** A type parameter can name what its function does with it,
from a fixed set, combined with `+`:

```ella
let largest[T: Ord] = (xs: [T]) -> T =>
    let mut best = xs[0]
    for x in xs
        if x > best then best = x
    best

let clamp[T: Ord + Add] = (x: T, low: T, high: T) -> T => ...
```

| Constraint | Allows on `T` | Met by |
| --- | --- | --- |
| `Eq` | `==`, `!=`, `in` | every type except functions, pools and code |
| `Ord` | `<`, `<=`, `>`, `>=` (and what `Eq` allows) | `i64`, `f64`, `str` |
| `Add` | `+` | `i64`, `f64`, `str`, lists |
| `Num` | `+`, `-`, `*`, `/`, `**`, unary `-` (and what `Ord` allows) | `i64`, `f64` |

* The body is checked once, against the constraints: `(a: T, b: T) -> T =>
  a + b` needs `[T: Add]`. `/` on a `Num` gives an `f64`, as it does on
  `i64` and `f64`.
* Each use is checked against them: `largest([true])` is an error, as
  `bool` isn't `Ord`. A type parameter meets the constraints it has, so a
  generic function can call another one with its own `T`.
* There are no user-defined constraints yet.

### Pools **(provisional syntax)**

A pool holds values that live longer than any one function call, such as
the nodes of a graph, each found with a handle:

```ella
struct Node
    name: str
    kids: [Handle[Node]]

let mut nodes: Pool[Node] = pool_new()
let root = alloc(mut nodes, Node("root", []))
let kid = alloc(mut nodes, Node("kid", []))
nodes[root].kids += [kid]
print(nodes[nodes[root].kids[0]].name)    # kid
free(mut nodes, kid)
print(nodes[kid])                         # error: the handle is stale
```

* `pool_new()` makes an empty `Pool[T]`. `alloc(mut pool, value)` puts a
  copy of the value in the pool (moving one that can't be copied), and
  returns a `Handle[T]` for it. `free(mut pool, handle)` removes it,
  finalizing it.
* `pool[h]` is the value, and `pool[h] = v`, `pool[h].f = v` and so on
  change it in place. `len(pool)` is how many values it holds.
* Handles are plain values: they can be copied, stored and compared. Each
  remembers its slot's *generation*, which goes up when the slot's value
  is freed, so using a handle to a freed value is a run-time error
  ("stale"), even after the slot is reused.
* A pool can't be copied. A handle doesn't say which pool it is for, so
  using it with the wrong pool of the same type isn't caught.

## 10. Built-ins

Built-in functions (ordinary names, not keywords):

* `print(value)` prints the text form of `value` and a newline; returns `()`.
* `len(x)` returns the length of a list, string or range as `i64`, or how
  many values a pool holds.
* `pool_new()`, `alloc(mut pool, value)` and `free(mut pool, handle)` work on
  pools ([§9](#pools-provisional-syntax)).
* `run(code)` runs code made with `quote` ([§11](#11-stages)).
* `fields(t)`, `field_type(t, name)`, `has_field(t, name)`, `type_name(t)`,
  `type_kind(t)` and `variants(t)` look at a type `t`, and
  `new_struct(name, fields, types)` makes one
  ([§11](#types-as-values-and-generated-code)).

The text form of a value (used by `print` and interpolation) is how it would
be written in Ella: `42`, `2.5`, `true`, `()`, `[1, 2]`, `(1, "a")`,
`Some(3)`, `None`, `Point(1.0, 2.0)`. A string is its own text, but is
quoted when it appears inside another value. A pool is `<pool of 3>`, a
handle `<handle 0:1>` (its slot and generation), and code `<code ...>`,
with the code as an S-expression.

Python-style protocol methods (`len`, indexing, iteration, and so on for user
types) are planned for when user-defined types exist.

`assert expr` is a keyword expression rather than a function, so that a
failure can report the asserted expression's source text. It has type `()`.

Run-time errors (failed `assert`, failed `where`, out-of-range index, no
matching `case`, division by zero, `i64` overflow) stop the program with a
message and the source location.

Type errors such as `1 + true` are found before the program runs. The AST
interpreter also checks types as it goes, so it can still run programs that
haven't been checked.

## 11. Stages

Ella programs can build code and run it later: they are *multi-stage*.
`quote e` doesn't evaluate `e`: its value is the code of `e`, of type
`Code[T]` when `e` is a `T`. Inside a quote, `$c` (a *splice*) inserts the
code `c` computed while the quote is built:

```ella
let power = (n: i64, x: Code[i64]) -> Code[i64] =>
    if n == 0 then quote 1 else quote $x * $(power(n - 1, x))

print(power(2, quote 5))          # <code (* 5 (* 5 1))>
print(run(power(2, quote 5)))     # 25
```

* Code belongs to a *stage*. The program is stage 0, the body of a quote is
  one stage later, and a splice one stage earlier. They nest without limit:
  `quote quote 1` is code that makes code.
* `run(c)` runs code at the stage it is used in, and gives its value. Code
  is checked when it is run; it can use the program's top-level names.
* A splice outside any quote is stage -1: it runs **while compiling**, and
  the code it gives becomes part of the program. A splice inside that one
  runs earlier still, making the code that runs while compiling:

  ```ella
  let cube = (x: i64) -> i64 => $(power(3, quote x))   # x * (x * (x * 1))
  ```

  What compile-time code prints comes out before the program runs.
  `ella --expanded` shows a program with its splices expanded.
* A variable belongs to the stage it is bound in. It can be used at a later
  stage, where its value is carried into the code as a constant: with
  `let n = 3`, `quote n + 1` is `3 + 1`. Only data can be carried, not
  functions (top-level functions are available at every stage) or values
  that can't be copied or handles. Using a variable at an earlier stage
  than its own is an error: it doesn't exist yet.
* Top-level variables other than functions belong to stage 0, so
  compile-time code can't read them.
* Variables bound inside quoted code are renamed when the code is built
  (`y` becomes `y.1`), so spliced code never captures the wrong variable.
* `return`, `break` and `continue` can't leave a quote or a splice.
* `$` applies to the name or parenthesized expression right after it (`$f(x)`
  calls the spliced `f`), or to an indented block on the next lines.
  `quote` takes everything to its right, or an indented block.
* Native code builds and runs code only while compiling. Code built at run
  time needs an interpreter; native code stops with an error if it gets to
  a `quote`, `run`, type value or reflection built-in.

### Types as values, and generated code

Code that generates code often depends on a type: an equality test for a
struct compares its fields. So types are values too:

* `type T` is the type `T` as a value, of type `Type[T]`. `type Point`,
  `type [str]` and `type Pair[i64]` are examples.
* Inside a quote, `$t` in a type splices in the type `t` holds. If `t` is a
  `Type[T]`, the spliced type is `T`, so generated code has a known type.
* Inside a quote, `v.$f` reads the field whose name the string `f` holds.
  Its type is only known once the code is built, so until then the checker
  lets it be used in any way; the code is checked fully once it is built,
  when it is spliced into the program or run. Code whose type depends on
  such a field can only be run while compiling.
* Built-ins look at a type: `fields(t)` (a struct's field names, in order),
  `field_type(t, name)` (a `Type[?]`), `has_field(t, name)`, `type_name(t)`
  (as written, like `"Pair[i64]"`), `type_kind(t)` (`"struct"`, `"enum"`,
  `"list"`, `"i64"`, ...) and `variants(t)` (an enum's variant names).
* Type values can be compared with `==`, and carried into a later stage
  like data, but types from `field_type` can't (their type isn't known).
* `Type`, with no type argument, is a type value whose type is only known
  when the code runs, and `Code` is code like that. A list holding types of
  different types is a `[Type]`.

```ella
# `a.f == b.f and ...` for the given fields
let all_equal[T] = (names: [str], a: Code[T], b: Code[T]) -> Code[bool] =>
    let mut test = quote true
    for name in names
        test = quote $test and $a.$name == $b.$name
    test

let derive_eq[T] = (t: Type[T]) -> Code[(T, T) -> bool] =>
    quote (a: $t, b: $t) -> bool => $(all_equal(fields(t), quote a, quote b))

let point_eq = $(derive_eq(type Point))   # (a: Point, b: Point) -> bool => ...
```

### Computed types

Outside quotes, `$(...)` in a type runs while the program is being checked
(at stage -1), and the type it gives is used where it is. Together with
`new_struct`, which makes a struct type, functions can compute types:

```ella
let Stack = (t: Type) -> Type =>
    new_struct("Stack[{{type_name(t)}}]", ["items", "size"], [type [$t], type i64])

let push = (mut s: $(Stack(type i64)), x: i64) -> () =>
    s.items += [x]
    s.size += 1

let mut s = $(Stack(type i64))([], 0)    # `$(t)(...)` makes a struct of the type t holds
push(mut s, 3)
print(s)                                  # Stack[i64]([3], 1)
```

* `new_struct(name, fields, types)` makes a struct type with those field
  names and types. Its name is what makes it the same type: asking again
  for the same name with the same fields gives the same type, so
  `$(Stack(type i64))` is one type wherever it is written. A different
  struct with the same name is an error.
* `type ...` builds a type from type values: `type [$t]` is a list of the
  type `t` holds. (`type` is like a quote of a type, so `$t` in it is a value
  of the current stage.)
* `$(t)(args)` calls the constructor of the struct type `t` holds, outside
  or inside quotes.
* A splice in a type can use top-level functions, which are checked (and
  their own splices expanded) before it runs, but not top-level variables,
  which belong to stage 0. It runs once, while checking; what it prints
  comes out then.
* After expanding, the structs that compile-time code made are ordinary
  definitions at the start of the program, and spliced types ordinary
  types: `ella --expanded` shows them.
* A type can't be spliced into a struct or enum definition yet; use
  `new_struct`.

## 12. Grammar summary

Informal EBNF. `NEWLINE`, `INDENT`, `DEDENT` are layout tokens from
[§2](#2-layout-and-blocks). Binary-operator precedence is in
[§8](#precedence) rather than encoded here; the parser is a Pratt parser.

```ebnf
program     = { expr NEWLINE } EOF ;
block       = NEWLINE INDENT expr { NEWLINE expr } NEWLINE DEDENT ;
body        = expr | block ;

expr        = let | assignment | lambda | if | match | for | while | loop
            | break | continue | return | assert | enum | struct | quote
            | operator_expr ;

let         = "let" binding [ "[" type_param { "," type_param } "]" ] "=" { binding "=" } expr ;
type_param  = IDENT [ ":" IDENT { "+" IDENT } ] ;    (* a constraint: Eq, Ord, Add or Num *)
binding     = [ "mut" ] IDENT [ ":" type ] ;      (* type parameters: first binding only *)
assignment  = target ( "=" | "+=" | "-=" | "*=" | "/=" | "//=" | "%=" ) expr ;
target      = IDENT | operator_expr "[" expr "]" | operator_expr "." IDENT ;

lambda      = "(" [ param { "," param } [ "," ] ] ")" "->" type "=>" lambda_body
            | IDENT "=>" body ;                  (* untyped; rejected for now *)
param       = [ "mut" ] IDENT ":" type ;
lambda_body = expr
            | NEWLINE INDENT [ where NEWLINE ] expr { NEWLINE expr } NEWLINE DEDENT ;
where       = "where" block ;

if          = "if" expr "then" body [ [ NEWLINE ] "else" body ] ;
match       = "match" expr NEWLINE INDENT case { case } DEDENT ;
case        = "case" pattern [ "if" expr ] ( "=>" expr NEWLINE | block ) ;

for         = [ label ] "for" pattern "in" expr body ;
while       = [ label ] "while" expr body ;
loop        = [ label ] "loop" body ;
label       = IDENT ":" ;
break       = "break" [ IDENT ] [ expr ] ;   (* IDENT only if it names an enclosing loop *)
continue    = "continue" [ IDENT ] ;
return      = "return" [ expr ] ;
assert      = "assert" expr ;

enum        = "enum" IDENT [ "[" IDENT { "," IDENT } "]" ]
              NEWLINE INDENT variant { NEWLINE variant } NEWLINE DEDENT ;
variant     = IDENT [ "(" type { "," type } ")" ] ;
struct      = [ "nocopy" ] "struct" IDENT [ "[" IDENT { "," IDENT } "]" ]
              NEWLINE INDENT field { NEWLINE field } NEWLINE DEDENT ;
field       = IDENT ":" type | "finalize" "=" expr ;
quote       = "quote" body ;
splice      = "$" ( IDENT | "(" expr ")" | block ) ;

operator_expr = prefix_op operator_expr
            | operator_expr binary_op operator_expr
            | operator_expr ( call | index | member | "?" )
            | primary ;
call        = "(" [ arg { "," arg } [ "," ] ] ")" ;
arg         = [ "mut" ] expr ;                   (* `mut` only for a place: a name or an index *)
index       = "[" expr "]" ;
member      = "." ( IDENT | "$" ( IDENT | "(" expr ")" ) ) ;  (* `.$f`: a spliced field name *)

primary     = INT | FLOAT | string | "true" | "false" | IDENT
            | "(" ")"                            (* unit *)
            | "(" expr ")"                       (* grouping *)
            | "(" expr "," [ expr { "," expr } ] [ "," ] ")"  (* tuple *)
            | "[" [ expr { "," expr } [ "," ] ] "]"  (* list *)
            | "type" type                        (* a type, as a value *)
            | splice ;
string      = STRING_START { STRING_TEXT | INTERP_START expr INTERP_END } STRING_END ;

pattern     = pattern_atom { "|" pattern_atom } ;
pattern_atom = "_" | IDENT | literal | literal ( ".." | "..=" ) literal
            | IDENT "(" pattern { "," pattern } ")"
            | "(" pattern "," [ pattern { "," pattern } ] ")" ;

type        = IDENT [ "[" type { "," type } "]" ]
            | "$" ( IDENT | "(" expr ")" )      (* in a quote: a spliced type *)
            | "[" type "]"
            | "(" ")"
            | "(" [ [ "mut" ] type { "," [ "mut" ] type } ] ")" "->" type
            | "(" type "," [ type { "," type } ] ")" ;
```

Parsing notes:

* **Lambdas vs. grouping/tuples.** `(` can start a grouping, tuple, unit, or
  lambda parameter list. Parse the parenthesized contents as expressions,
  allowing `name: type` items; if `->` or `=>` follows the `)`, convert the
  items into parameters (each must be `name: type` or `mut name: type`),
  otherwise reject any such items. No backtracking needed.
* **`let` chains.** After `let binding =`, another target follows if the next
  tokens are `mut`, or `IDENT` followed by `=` or `:` (but not a labeled loop,
  `IDENT ":" for`).
* **Labels.** `IDENT ":"` followed by `for`, `while` or `loop` is a label.
  The parser keeps a stack of the labels of enclosing loops (reset inside each
  lambda) to decide whether a name after `break` or `continue` is a label.
* **Comparison chains** are one node: `Compare(first, [(op, operand), ...])`.
* **`else` on the next line.** After an `if`'s `then` body, peek past a single
  `NEWLINE` for `else` at the same indentation.
* **`return` in `where`** is parsed as a name.
* **An expression that ends with an indented block ends there.** The block
  already consumed its closing `NEWLINE`, so the next line starts a new
  expression; it can't continue the old one with an operator. Only `else`
  (for `if`) and `case` (inside `match`) may follow a block.
* **Trailing binary operator.** If a binary operator is followed by `NEWLINE`
  (which can only happen outside brackets), report the "wrap in parentheses"
  error.

## Open questions

* What is the `Error` type in `Result[T, Error]`? `Error` is also the name of
  the variant. A built-in error type holding a message is likely. For now,
  `Error` as a type means `str`.
* How SIMD vector types are named and written (`f32x4`?), and how they relate
  to `[T]`.
* User-defined protocol methods (`len`, iteration, ...) for structs.
* Generic functions inside functions, and constraints on type parameters
  (so a generic function can use `+` or `<`).
* Whether `comptime` (still reserved) is worth having as shorthand for a
  splice, now that staging uses `quote` and `$`.
* User-defined constraints (with the protocol methods above), and
  compile-time type parameters checked at each use, for template-style code
  that constraints can't express. See docs/memory-model.md §15.
* Generic computed types (`new_struct` makes structs without type
  parameters) and splicing types into struct definitions.
* Modules and `import`.
