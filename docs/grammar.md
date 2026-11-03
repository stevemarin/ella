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
and         assert      break       case        comptime    constraint
continue    else        enum        false       for         if
import      in          let         loop        match       mut
nocopy      not         or          quote       return      struct
then        true        type        where       while
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

A body that is an assignment has the value `()` either way, as it would as
the last line of a block: `(mut xs: [i64]) -> () => xs += [1]` is fine.
(The parser makes such an inline body a block of one line.)

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
   `main` has type `() -> ()`, or `() -> i64` to give the program's exit
   status, which must be from 0 to 255 (anything else is a run-time error).
   Without it the status is 0, and a run-time error makes it 1.
   `exit(status)` stops the program anywhere, with that status. The
   program's command-line arguments are `args()` ([§10](#10-built-ins)).
5. Constraints ([§9](#user-defined-constraints-provisional-syntax)), and
   structs and enums with methods, can only be defined at the top level.

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

### Destructuring

A `let` can take a value apart with a pattern ([§7](#7-patterns)), binding
each name in it:

```ella
let (q, r) = divmod(17, 5)
let (name, (x, y)) = ("origin", (0.0, 0.0))
let Point(px, py) = p
let mut (low, high): (i64, i64) = (0, 10)
let (mut total, step) = (0, 5)
```

* The pattern must match every value of the type: a tuple of names, a
  struct, `_` and so on, never a literal or one variant of an enum (use
  `match` for those). The checker rejects others, with an example of a
  value they miss.
* The pattern starts with `(` or a name and `(`, perhaps after `mut`. A
  type after the pattern is the value's type, as for any `let`.
* The names are immutable, unless the `let` is `let mut`, which makes all
  of them mutable, or a name has `mut` before it, which makes that one
  mutable (each is a copy, as with `let mut x = y`). There are no chains.

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
* Assignment is right-associative and evaluates to the assigned value, so
  `a = b = 0` sets both. As a body, or the last line of a block, it has
  the value `()` instead ([§2](#blocks)).
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
    case Err(e)
        print("failed: {{e.message}}")
        0
```

* `match expr` is followed by an indented list of `case` lines.
* Each case is `case pattern [if guard]`, then either `=> expr` on the same
  line or an indented block.
* Cases are tried in order, and together they must cover every value of
  the subject's type: the checker rejects a `match` that misses one, with
  an example (`add a case for None`). A case with a guard doesn't count,
  since its guard may be false, and literals and ranges never cover a
  whole `i64`, `f64` or `str`, so a match on one of those needs a `case _`
  or a name. Enums, structs, tuples and `bool` are covered by naming each
  variant (or `true` and `false`).
* A case that can never match, because the unguarded cases before it
  match every value it does, is a warning ([§9](#warnings)). A literal only
  counts as covered by a wildcard, a name or the same literal, so a case
  covered by ranges (`case 1` after `case 0..5`) isn't reported.

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

* `for pattern in expr` iterates over a list, string, or range, or a struct
  or enum with an `iter` method ([§9](#methods-provisional-syntax)). The
  pattern must match every item, as a `let`'s must.
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

Patterns appear after `case`, in `for`, and in `let`. In `for` and `let`
they must match every value ([§4](#destructuring)).

| Pattern | Example | Matches |
| --- | --- | --- |
| wildcard | `_` | anything, binds nothing |
| literal | `0`, `"x"`, `true` | an equal value |
| range | `0..10`, `0..=9` | a value in the range |
| binding | `n` | anything, binds it to `n` |
| mutable binding | `mut n` | anything, binds it to a variable `n` that can change |
| alternatives | `0 \| 1` | either side; both sides must bind the same names |
| variant | `Ok(v)`, `Some(x)`, `None` | an enum variant, destructuring its fields |
| tuple | `(a, b)` | a tuple, element-wise |

Whether a bare name like `None` is a variant or a new binding is decided by
name resolution after parsing, as in Rust: the parser produces a name
pattern, and the resolver checks whether the name refers to a variant.

Negative literal patterns (`-1`) are allowed as a special case.

`mut n` works in every pattern: `let (mut a, b) = ...`, `for (i, mut row)
in rows` and `case Some(mut items)`. The variable holds a copy of the
value, so changing it never changes the value matched. The alternatives of
`|` must agree on which names are `mut`, and a variant can't be `mut`.

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

Postfix `?` on a `Result`: `Ok(v)?` gives `v`; `Err(e)?` returns `Err(e)`
from the enclosing function. On an `Optional`: `Some(v)?` gives `v`; `None?`
returns `None`. The enclosing function's return type must be compatible.

### Indexing and ranges

* `a[i]` indexes lists and strings with an `i64`. Out-of-range or negative
  indexes are a run-time error **(provisional; no Python-style negative
  indexing)**. Indexing a string gives a one-character string; strings count
  Unicode code points, and can't be assigned into. Pools are indexed with
  handles, and structs and enums with `get` and `set` methods with any key
  ([§9](#methods-provisional-syntax)).
* `a..b` is the half-open range `a <= i < b`; `a..=b` includes `b`.
* Indexing a list or string with a range gives a *slice*, a new list or
  string of those items: `"hello"[1..3]` is `"el"`, `xs[0..=1]` the first
  two items (copies of them). The range must be in order and within the
  value: `0 <= a <= b <= len(xs)`, or it is a run-time error. A slice
  can't be assigned to.

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
* A tuple's items are read with `.0`, `.1`, ...: `pair.0`, `t.1.0`. A
  tuple can't be changed in place; build a new one.
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
* `return`, `break`, `continue`, `exit(status)`, and a `loop` that never
  breaks have no value. They fit wherever a value of any type is expected,
  as in `if n > 0 then n else return 0`.
* The bodies of `for`, `while` and `loop`, the `then` of an `if` without
  `else`, and all but the last line of a block can have any type; their
  values are discarded.
* `print`, `len` and variant constructors like `Some` can only be called, not
  used as values (for now). Library functions like `trim` can be both
  ([§10](#10-built-ins)).
* Enums, structs and top-level functions can be generic.

### Warnings

Some mistakes don't stop a program, but probably aren't what was meant.
They are **warnings**: `ella` prints them to standard error (as
`file:line:column: warning: ...`) and runs the program anyway, and
editors show them.

* A `match` case that can never match ([§6](#match)).
* A local variable that is never used: a `let`, or a name in a pattern.
  A name that starts with `_`, like `_guard`, says that is on purpose (for
  a value kept only for its finalizer, say). Parameters and top-level
  names aren't reported.
* Code that never runs, after a line that never finishes: `return`,
  `break`, `continue`, `exit(...)`, an `if` or `match` whose every branch
  is one of those, or a `loop` that never breaks. Only the first such line
  of a block is reported.

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
`Ok`/`Err` and `Some`/`None`.

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

### Methods **(provisional syntax)**

A struct or enum can define functions of its own, *methods*, alongside its
fields or variants. A method takes the value it is called on as its first
parameter, and is called with `value.name(...)`:

```ella
struct Point
    x: f64
    y: f64
    norm2 = (p: Point) -> f64 => p.x * p.x + p.y * p.y
    move = (mut p: Point, dx: f64, dy: f64) -> () =>
        p.x += dx
        p.y += dy

let mut p = Point(3.0, 4.0)
print(p.norm2())          # 25.0
mut p.move(1.0, 0.0)      # p is Point(4.0, 4.0)

enum Shape
    Circle(f64)
    Rect(f64, f64)
    area = (s: Shape) -> f64 =>
        match s
            case Circle(r) => 3.0 * r * r
            case Rect(w, h) => w * h
```

* A method's first parameter must be of its type: `Point`, or `Stack[T]`
  in `struct Stack[T]`. A method of a generic type is generic in the
  type's parameters, and is specialized for each set of type arguments it
  is used with, like a generic function.
* Each type has its own methods, so different types can have methods with
  the same name. A method can't have the name of a field, and only
  top-level structs and enums can have methods.
* A method whose first parameter is `mut` changes the value it is called
  on, which is marked as for any `mut` argument: `mut p.move(1.0, 0.0)`.
  The value must be a place: a `mut` variable, or a field or item of one
  (`mut shelf.stacks[1].push(20)`). `mut` applies to the call after it,
  even as an argument: `print(mut s.pop())`.
* `v.f(x)` calls a function held in a field `f` too, without passing `v`.
* Methods can call each other and any top-level function, in any order.
* A generic type's parameters can have constraints, as a generic
  function's can, for its methods to use. They are checked where a method
  is used:

  ```ella
  struct Set[T: Eq]
      items: [T]
      has = (s: Set[T], x: T) -> bool => x in s.items
  ```

**Operators.** Methods with these names give a struct or enum operators,
and `len`:

| Method | Gives | Type |
| --- | --- | --- |
| `add`, `sub`, `mul`, `div` | `a + b`, `a - b`, `a * b`, `a / b` | `(Self, T) -> R` |
| `neg` | `-a` | `(Self) -> R` |
| `compare` | `a < b` (`a.compare(b) < 0`), `<=`, `>`, `>=` | `(Self, Self) -> i64` |
| `len` | `len(a)` | `(Self) -> i64` |

`Self` is the type itself. The right operand and the result can have any
types, so `mul = (v: Vec, k: f64) -> Vec` scales a vector. Compound
assignments use them too: `total += Money(5)`. `==` and `!=` always compare
values field by field.

```ella
struct Money
    cents: i64
    add = (a: Money, b: Money) -> Money => Money(a.cents + b.cents)
    compare = (a: Money, b: Money) -> i64 => a.cents - b.cents

print(Money(5) + Money(7))       # Money(12)
print(Money(5) < Money(7))       # true
```

**Protocols.** Methods with these names let a struct or enum be indexed,
looped over and searched, like the built-in collections:

| Method | Gives | Type |
| --- | --- | --- |
| `get` | `v[k]` | `(Self, K) -> V` |
| `set` | `v[k] = x`, and with `get`, `v[k] += x` and so on | `(mut Self, K, V) -> ()` |
| `iter` | `for x in v`: loops over what `v.iter()` gives | `(Self) -> R`, R a list, str or range |
| `contains` | `x in v` (`v.contains(x)`), `x not in v` | `(Self, T) -> bool` |

```ella
struct Table[V]
    keys: [str]
    values: [V]
    get = (t: Table[V], key: str) -> V => t.values[t.find(key)]
    set = (mut t: Table[V], key: str, value: V) -> () => ...
    contains = (t: Table[V], key: str) -> bool => key in t.keys
    iter = (t: Table[V]) -> [str] => t.keys
    find = (t: Table[V], key: str) -> i64 => ...

let mut ages = Table([], [])
ages["ann"] = 31
ages["ann"] += 1
for name in ages
    print("{{name}}: {{ages[name]}}")    # ann: 32
```

* The key can have any type: a grid can be indexed with `(row, column)`
  tuples. If a type has both `get` and `set`, they must agree on the key
  and value types.
* An item that `get` gives is a copy, so only the last step of an
  assignment can go through `set`: `world.grids[0][(1, 1)] = 4` works, but
  `table["a"].x = 1` and `f(mut table["a"])` don't.
* `iter` gives a new list, string or range, which the loop then goes
  through. (It is called once, before the first iteration.)
* A user-defined constraint can ask for these methods too, so generic code
  can index, loop over and search its values.
* All these names (and the operators' above, and `finalize`) are kept for
  their meaning: a method with one of them must have its type.

### Errors

`Error` is a built-in struct for the error side of a `Result`, holding a
message:

```ella
struct Error
    message: str
```

```ella
let invert = (n: f64) -> Result[f64, Error] =>
    if n == 0.0 then Err(Error("divide by zero")) else Ok(1.0 / n)

match invert(0.0)
    case Ok(v) => print(v)
    case Err(e) => print(e.message)    # divide by zero
print(invert(0.0))                     # Err(Error("divide by zero"))
```

* It is an ordinary struct that happens to be predefined, so it is built,
  read, compared, printed and matched (`case Err(Error(message))`) like any
  other. A program can define its own `Error`, which replaces it.
* `Result[T, E]` can still hold any error type: `Result[i64, str]` is fine.

### Values that can't be copied **(provisional syntax)**

A `nocopy struct` is for values with an identity, which a copy would
duplicate: a resource that must be released once, say. Its values are
never copied, and it may have a `finalize` method, which runs on each
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
  at least one field; one with a `finalize` method can't be generic. Only
  a nocopy struct can have a `finalize` method, of type `(Self) -> ()`,
  which can't be called directly. Its other methods borrow the value, or
  change it with `mut`.
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
| `Ord` | `<`, `<=`, `>`, `>=` (and what `Eq` allows) | `i64`, `f64`, `str`, types with a `compare` method |
| `Add` | `+` | `i64`, `f64`, `str`, lists, types with an `add` method of type `(Self, Self) -> Self` |
| `Num` | `+`, `-`, `*`, `/`, `**`, unary `-` (and what `Ord` allows) | `i64`, `f64` |

* The body is checked once, against the constraints: `(a: T, b: T) -> T =>
  a + b` needs `[T: Add]`. `/` on a `Num` gives an `f64`, as it does on
  `i64` and `f64`.
* Each use is checked against them: `largest([true])` is an error, as
  `bool` isn't `Ord`. A type parameter meets the constraints it has, so a
  generic function can call another one with its own `T`.
* `Num` is only met by numbers: `/` on a `Num` gives an `f64`, which a
  struct's `div` needn't.

### User-defined constraints **(provisional syntax)**

A program can define its own constraints, as the methods a type must have,
with `Self` standing for the type:

```ella
constraint Shape
    area: (Self) -> f64
    name: (Self) -> str

struct Square
    side: f64
    area = (s: Square) -> f64 => s.side * s.side
    name = (s: Square) -> str => "square"

let total[T: Shape] = (xs: [T]) -> f64 =>
    let mut sum = 0.0
    for x in xs
        sum += x.area()
    sum

print(total([Square(2.0), Square(1.0)]))     # 5.0
```

* A struct or enum meets a constraint if it has each of its methods, with
  the same types (with `Self` replaced by the type). It doesn't say so: any
  type with the methods will do. Other types never meet one.
* In a generic function, a value of a type parameter has the methods of its
  constraints: `x.area()`. A method can be `mut` (`tick: (mut Self) -> ()`),
  and is then called as `mut x.tick()`. Two constraints of one type
  parameter can't have different methods with the same name.
* User-defined and built-in constraints combine: `[T: Shape + Ord]`.
* A constraint's method named `len` gives `len(x)` in the generic function;
  operators still need the built-in constraints.
* Each use is still compiled for its own types, so a method call is a
  direct call: there is no dynamic dispatch.

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

* `print(a, b, ...)` prints the text forms of its values, with a space
  between each two, and a newline; returns `()`. `print()` prints an empty
  line, and `print("x", 1)` prints `x 1`.
* `eprint(a, b, ...)` prints the same way to standard error, after
  everything printed so far.
* `len(x)` returns the length of a list, string or range as `i64`, or how
  many values a pool holds. On a struct or enum, it calls its `len` method.
* `pool_new()`, `alloc(mut pool, value)` and `free(mut pool, handle)` work on
  pools ([§9](#pools-provisional-syntax)).
* `run(code)` runs code made with `quote` ([§11](#11-stages)).
* The library functions below, and the prelude's ([below](#the-prelude)).
* `fields(t)`, `field_type(t, name)`, `has_field(t, name)`, `type_name(t)`,
  `type_kind(t)` and `variants(t)` look at a type `t`, and
  `new_struct(name, fields, types)` makes one
  ([§11](#types-as-values-and-generated-code)).

The library: functions with fixed types. A program may use their names for
its own variables. Unlike the other built-ins they are values too, so
`apply(trim, s)`, `let ops: [(i64, i64) -> i64] = [min, max]` and
`let parse = parse_i64` work; such a value prints as `<function>`.

| Function | Type | Gives |
| --- | --- | --- |
| `to_f64(n)` | `(i64) -> f64` | `n` as the nearest `f64` |
| `to_i64(x)` | `(f64) -> i64` | `x` without its fraction (toward zero); an error if that doesn't fit, or `x` is NaN |
| `parse_i64(s)` | `(str) -> Result[i64, Error]` | the number `s` writes: an optional sign and digits, nothing else |
| `parse_f64(s)` | `(str) -> Result[f64, Error]` | the number `s` writes: `2.5`, `-1e3`, `.5`, `inf`, `nan`, ... |
| `read_line()` | `() -> Optional[str]` | the next line of standard input, without its `\n` (or `\r\n`); `None` at the end |
| `read_file(path)` | `(str) -> Result[str, Error]` | the whole file, or why it can't be read |
| `args()` | `() -> [str]` | the program's command-line arguments |
| `split(s, sep)` | `(str, str) -> [str]` | the parts of `s` between the `sep`s; `sep` can't be empty |
| `join(parts, sep)` | `([str], str) -> str` | the parts, with `sep` between each two |
| `find(s, part)` | `(str, str) -> Optional[i64]` | where `part` first appears in `s` |
| `replace(s, old, new)` | `(str, str, str) -> str` | `s` with each `old` (left to right, not overlapping) replaced; `old` can't be empty |
| `trim(s)` | `(str) -> str` | `s` without spaces, tabs, `\n`, `\r`, `\v` and `\f` at either end |
| `chars(s)` | `(str) -> [str]` | the characters of `s`, as one-character strings |
| `ord(c)` | `(str) -> i64` | the code point of `c`, which must be one character |
| `chr(n)` | `(i64) -> str` | the character with code point `n`: from 0 to 0x10FFFF, but not a surrogate (0xD800 to 0xDFFF) |
| `upper(s)`, `lower(s)` | `(str) -> str` | `s` with its ASCII letters in upper (or lower) case; other characters stay as they are |
| `starts_with(s, part)`, `ends_with(s, part)` | `(str, str) -> bool` | whether `s` starts (or ends) with `part` |
| `pad_left(s, width, fill)`, `pad_right(s, width, fill)` | `(str, i64, str) -> str` | `s` with copies of `fill`, which must be one character, before (or after) it to make it `width` characters long; `s` itself if it is that long already |
| `format_f64(x, digits)` | `(f64, i64) -> str` | `x` with `digits` decimals (from 0 to 100), rounded: `format_f64(2.5, 2)` is `"2.50"`; `nan`, `inf` and `-inf` as `print` writes them |
| `env(name)` | `(str) -> Optional[str]` | the environment variable `name`; `None` if there is none |
| `write_file(path, text)` | `(str, str) -> Result[(), Error]` | writes `text` to the file in UTF-8, replacing what it held, or gives why it can't |
| `exit(status)` | `(i64) -> never` | stops the program at once with `status`, from 0 to 255 |
| `sqrt(x)` | `(f64) -> f64` | the square root of `x`; NaN if `x` is negative |
| `floor(x)` | `(f64) -> f64` | the largest whole number not above `x` (`to_i64(floor(x))` makes it an `i64`) |
| `ceil(x)` | `(f64) -> f64` | the smallest whole number not below `x` |
| `round(x)` | `(f64) -> f64` | the nearest whole number; halfway cases go away from zero (`round(-2.5)` is `-3.0`) |
| `exp(x)`, `ln(x)` | `(f64) -> f64` | e to the power `x`, and the natural logarithm of `x` (`-inf` for 0, NaN below it) |
| `sin(x)`, `cos(x)` | `(f64) -> f64` | the sine and cosine of `x` radians |
| `abs(x)` | `[T: Num] (T) -> T` | `x` without its sign; an `i64` error for the smallest `i64` |
| `min(a, b)` | `[T: Num] (T, T) -> T` | `b` if `b < a`, otherwise `a` |
| `max(a, b)` | `[T: Num] (T, T) -> T` | `b` if `b > a`, otherwise `a` |

* Numbers are written in ASCII only, with no `_` and no spaces around them:
  `parse_i64(trim(line))` reads a number on a line of its own.
* Text read from outside the program must be UTF-8: `read_line` stops with
  an error if it isn't, and `read_file` gives an `Err`. An `Err`'s message
  says what went wrong, like `can't read "data.txt": No such file or
  directory`.
* The results that are enums use the built-in `Result`, `Optional` and
  `Error`, so these functions can't be used in a program that defines its
  own.
* Strings count characters (code points), so `find` and slices do too.
* `exit` stops the program as a run-time error does, after what it has
  printed: nothing more runs, not even finalizers. It can only be called,
  not used as a value, and not while compiling (in a splice outside
  quotes).
* `upper` and `lower` only change `a` to `z` and `A` to `Z`, so that every
  backend agrees without tables of the Unicode alphabets.
* `format_f64` rounds the exact value of `x`, so a halfway case that a
  float holds exactly goes to the even digit (`format_f64(0.5, 0)` is
  `"0"`), and one it can't hold exactly goes by the value it does hold
  (`format_f64(2.675, 2)` is `"2.67"`).
* `abs`, `min` and `max` work on two `i64`s or two `f64`s, like a generic
  function with `[T: Num]` ([§9](#generic-functions-provisional-syntax)).
  Their definitions say what happens with NaN and `-0.0`: `min(nan, 1.0)`
  is NaN, `min(1.0, nan)` is `1.0`, and `min(0.0, -0.0)` is `0.0`.

The text form of a value (used by `print` and interpolation) is how it would
be written in Ella: `42`, `2.5`, `true`, `()`, `[1, 2]`, `(1, "a")`,
`Some(3)`, `None`, `Point(1.0, 2.0)`. A string is its own text, but is
quoted when it appears inside another value. A pool is `<pool of 3>`, a
handle `<handle 0:1>` (its slot and generation), and code `<code ...>`,
with the code as an S-expression.

Structs and enums can give themselves `len`, operators, indexing,
iteration and `in` with methods ([§9](#methods-provisional-syntax)).

`assert expr` is a keyword expression rather than a function, so that a
failure can report the asserted expression's source text. It has type `()`.

Run-time errors (failed `assert`, failed `where`, out-of-range index or
slice, division by zero, `i64` overflow, and a few library errors) stop the
program with a message and the source location.

Functions can call each other 10,000 deep: one more nested call stops the
program with "stack overflow: more than 10000 nested calls". `ella
--max-depth N` changes the limit. Every backend has the same limit, so a
program behaves the same however it is run. (Native code has room for about
two scope levels, a function body and one loop, per nested call.)

Type errors such as `1 + true` are found before the program runs. The AST
interpreter also checks types as it goes, so it can still run programs that
haven't been checked.

### The prelude

A few library functions are written in Ella itself, in the *prelude*
(src/ella/prelude.py): functions that take other functions, or work on
lists of any type. A program sees them as if it had defined them at its
start, unless it defines the same name at the top level itself.

| Name | Type | Gives |
| --- | --- | --- |
| `reverse(xs)` | `[T] ([T]) -> [T]` | the items of `xs` in the opposite order |
| `sort(xs)` | `[T: Ord] ([T]) -> [T]` | the items of `xs` from smallest to largest, by `<`; equal items keep their order |
| `map(xs, f)` | `[T, U] ([T], (T) -> U) -> [U]` | `f` of each item of `xs`, called in order |
| `filter(xs, keep)` | `[T] ([T], (T) -> bool) -> [T]` | the items of `xs` for which `keep` is true, called in order |
| `pi` | `f64` | 3.141592653589793 |

They are ordinary generic functions ([§9](#generic-functions-provisional-syntax)),
so they are values too, and a type given to `sort` must be `Ord`: a
number, a string, or a struct or enum with a `compare` method. With pipes
they read left to right: `words |> filter(long) |> map(upper)`.

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
* A quote inside a function that is defined in a splice can't use the
  variables of the quote the splice is in, for now: the compiler has no
  way to reach them from inside the function. The checker reports it.
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
            | break | continue | return | assert | enum | struct | constraint
            | quote | mut_call | operator_expr ;

let         = "let" binding [ "[" type_param { "," type_param } "]" ] "=" { binding "=" } expr
            | "let" [ "mut" ] pattern [ ":" type ] "=" expr ;  (* destructuring: the pattern starts with "(" or IDENT "(" *)
type_param  = IDENT [ ":" IDENT { "+" IDENT } ] ;    (* built-in or user-defined constraints *)
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

enum        = "enum" IDENT [ "[" type_param { "," type_param } "]" ]
              NEWLINE INDENT ( variant | method ) { NEWLINE ( variant | method ) } NEWLINE DEDENT ;
variant     = IDENT [ "(" type { "," type } ")" ] ;
struct      = [ "nocopy" ] "struct" IDENT [ "[" type_param { "," type_param } "]" ]
              NEWLINE INDENT ( field | method ) { NEWLINE ( field | method ) } NEWLINE DEDENT ;
field       = IDENT ":" type ;
method      = IDENT "=" expr ;                   (* a function; `finalize` for a nocopy struct *)
constraint  = "constraint" IDENT
              NEWLINE INDENT IDENT ":" type { NEWLINE IDENT ":" type } NEWLINE DEDENT ;
mut_call    = "mut" operator_expr ;              (* a method call: `mut s.push(3)` *)
quote       = "quote" body ;
splice      = "$" ( IDENT | "(" expr ")" | block ) ;

operator_expr = prefix_op operator_expr
            | operator_expr binary_op operator_expr
            | operator_expr ( call | index | member | "?" )
            | primary ;
call        = "(" [ arg { "," arg } [ "," ] ] ")" ;
arg         = [ "mut" ] expr ;                   (* `mut` only for a place, or before a method call *)
index       = "[" expr "]" ;
member      = "." ( IDENT | INT | "$" ( IDENT | "(" expr ")" ) ) ;  (* `.0`: a tuple's item; `.$f`: a spliced field name *)

primary     = INT | FLOAT | string | "true" | "false" | IDENT
            | "(" ")"                            (* unit *)
            | "(" expr ")"                       (* grouping *)
            | "(" expr "," [ expr { "," expr } ] [ "," ] ")"  (* tuple *)
            | "[" [ expr { "," expr } [ "," ] ] "]"  (* list *)
            | "type" type                        (* a type, as a value *)
            | splice ;
string      = STRING_START { STRING_TEXT | INTERP_START expr INTERP_END } STRING_END ;

pattern     = pattern_atom { "|" pattern_atom } ;
pattern_atom = "_" | [ "mut" ] IDENT | literal | literal ( ".." | "..=" ) literal
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
* **Tuple items.** After `.`, a decimal integer is a tuple's item. The
  lexer reads `t.0.1` as `t`, `.`, `0.1`, so a float there is two items.
* **`let` with a pattern.** After `let`, `(` or `IDENT "("` starts a
  pattern rather than a binding.
* **Assignments as bodies.** An inline body that is an assignment is made
  a block of one line, so its value is `()`.
* **Methods vs. fields and variants.** In a struct or enum, a line starting
  `IDENT "="` is a method; otherwise it is a field or a variant.
* **`mut` before a method call** (`mut s.items[0].push(1)`) is parsed as a
  postfix expression after `mut`, which must end with a call of a member;
  the value the method is called on becomes the `mut` argument:
  `Call(Member(MutArg(s.items[0]), "push"), [1])`. In an argument list,
  `mut` followed by such a call means the same.

## Open questions

* What else an `Error` should hold: where it was made, or the error that
  caused it.
* How SIMD vector types are named and written (`f32x4`?), and how they relate
  to `[T]`.
* Whether `==` and the text form (`print`) should use methods too, and
  whether iteration should use a stateful iterator (`next`) rather than
  making a list.
* Methods for built-in types (`xs.len()`), and functions in a type's
  namespace that don't take a value of it (`Stack.new()`).
* Whether a constraint should be able to require other constraints, or
  operators (`constraint Ring: Add` ...).
* Generic functions inside functions. Generic functions currently support
  the built-in constraints `Eq`, `Ord`, `Add` and `Num`.
* Whether `comptime` (still reserved) is worth having as shorthand for a
  splice, now that staging uses `quote` and `$`.
* Compile-time type parameters checked at each use, for template-style
  code that constraints can't express. See docs/memory-model.md §15.
* Generic computed types (`new_struct` makes structs without type
  parameters) and splicing types into struct definitions.
* Modules and `import`.
