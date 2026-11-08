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
and         as          assert      break       case        comptime
const       constraint  continue    else        enum        false
for         if          implements  import      in          let
loop        match       mut         nocopy      not         or
quote       return      struct      then        true        type
where       while       yield
```

Reserved for later, not yet given a grammar: `comptime`.

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
.    ,    :    (    )    [    ]    {    }    $    ;
```

`!` (alone), `@` and `` ` `` are not tokens; they are errors outside
strings and comments. `{` and `}` make map and set literals
([§10](#the-prelude)). `;` is only used in array shapes (`Array[f64; N, M]`, and size
parameters `[T; N]`, [§9](#static-shapes)); anywhere else it is an error.

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
  bracket or string, nor a map or set literal: in `"{{ {1: 2}}}"`, the
  first `}` closes the literal.
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
* **Inside `(` `)`, `[` `]` and `{` `}`, newlines and indentation are
  ignored.** This is the only way to continue an expression over several
  lines.
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

A source file is a *module*: its `import` lines
([below](#modules-and-imports-provisional-syntax)), then a sequence of
top-level expressions. A program is the file `ella` is given (the *entry
file*) and the modules it imports, directly or not.

1. Top-level `let` names are visible throughout the file, regardless of order.
   This is what lets top-level functions call each other in any order.
   Reading a top-level non-function value before its `let` has run is an
   error: at compile time when the top-level code itself does it, and at run
   time when a function that reads it is called too early.
2. A top-level name keeps one type, even if it is bound again by a later
   `let`, because functions anywhere in the file may refer to it. (Local
   names may be rebound with a different type.)
3. The top-level expressions run in order, like a script.
4. Afterwards, if the entry file defines a top-level `main`, it is called.
   (In other modules, `main` is an ordinary function.)
   `main` has type `() -> ()`, or `() -> i64` to give the program's exit
   status, which must be from 0 to 255 (anything else is a run-time error).
   Without it the status is 0, and a run-time error makes it 1. `main` may
   also give a `Result` of either, `() -> Result[(), E]` or
   `() -> Result[i64, E]`, so that it can use `?`: `Ok(v)` is as if it gave
   `v`, and `Err(e)` stops the program as a run-time error does. An `e`
   that is the built-in `Error` ([§9](#errors)) is reported where it was
   made, with its causes after it; any other is reported as what main
   returned:

   ```text
   prog.ella:16:44: bad row 2
     caused by prog.ella:8:44: bad field ' x'
     caused by prog.ella:6:24: "x" isn't an i64
   prog.ella:1:1: main returned Err("no input")
   ```

   `exit(status)` stops the program anywhere, with that status. The
   program's command-line arguments are `args()` ([§10](#10-built-ins)).
5. Constraints ([§9](#user-defined-constraints-provisional-syntax)), and
   structs and enums with methods, can only be defined at the top level.

### Modules and imports **(provisional syntax)**

A file is a module, named by its path from the *root* without `.ella`,
with `.` between directories: `geo/poly.ella` is the module `geo.poly`.
The root is the entry file's directory. An editor, which may show any
file of a program, takes as root the directory, from the file's up to the
workspace folder, that the file's imports are found from. A directory's own module is the file of the same name beside
it: `geo.ella` is the module `geo`, and `geo/poly.ella` is below it. A
directory needn't have one. Each part of a module's name must be an
identifier.

```ella
import geo                 # geo.ella
import geo.poly            # geo/poly.ella
import geo.poly as p       # the same module, named p here
import geo.Shape as Shape  # one item of geo

let main = () -> () =>
    let s = geo.Circle(1.0)
    print(geo.area(s), p.perimeter([]), geo.poly.perimeter([]))
```

* `import` lines come first in a file, before any other line. An import
  names a module, or a public item of one (a top-level variable or
  function, a struct, an enum or a constraint): `import geo.Shape`.
* An import binds its whole path: after `import geo.poly`, the module's
  names are `geo.poly.perimeter`, and so on. It doesn't bind `geo`'s own
  names; that needs `import geo` as well. `as` gives the module or item
  a name of its own instead: `p.perimeter`, or `Shape`.
* An enum's variants come with it, named alongside it: with `import geo`,
  they are `geo.Circle`, ... (in patterns too: `case geo.Circle(r)`), as
  they are with `import geo.Shape`. An enum imported with `as` brings
  its variants as bare names: after `import geo.Shape as Shape`, they
  are `Circle`, `Rect` and `Empty`. A struct's name is its constructor,
  so `geo.Point(1.0, 2.0)` makes one and `case geo.Point(x, y)` matches
  one.
* Imported types and constraints are named the same way in types:
  `geo.Point`, `p.Pair[i64]`, `[T: geo.Ring]`.
* Methods come with their type: `s.area()` works wherever `s` is a
  `geo.Shape`, even in a module that doesn't import `geo`.
* A module isn't a value: `geo` alone, or `print(geo.poly)`, is an
  error. Imports aren't passed on: a module that imports `geo` doesn't
  give its importers `geo`'s names.
* It is an error for two imports, or an import and a top-level
  definition, to bind the same name (or a top-level definition to have
  the name an import's path starts with). A local variable can shadow an
  imported name, as it can any top-level name.
* A top-level variable of another module can be read but not changed:
  it can't be assigned or passed as `mut`, even if it is `let mut`.
* Imports are found first from the root, then from each directory of
  the package path ([below](#packages)), then in the standard library
  ([§10](#the-prelude)). A module of the program or of a package with the
  name of a library module is used instead of it by imports, with a
  warning (the prelude's names still come from the library).
* Imports can't form a cycle (`a` imports `b`, which imports `a`); the
  error names the modules in it.

**Private names.** A top-level name, method, field or variant that starts
with `_` is *private* to its module and the modules below it: `_helper`
in `geo.ella` can be used in `geo.ella`, `geo/poly.ella` and
`geo/poly/edges.ella`, but not in `main.ella`, nor in `geo.ella` if it is
defined in `geo/poly.ella`. Everything else is public.

* Outside, a struct with a private field can't be made with its
  constructor or matched by a pattern that destructures it (`Point(x,
  y)`), and the field can't be named. Making one is up to the module's
  functions (`new_map()`). `==` and the text form still look at every
  field.
* Outside, a private variant can't be made or matched, so a `match` there
  over its enum needs a `_` case.
* A public function may use private types in its type: callers can use
  the values, but not name the types.

**Running a program.** Each module is checked, and its top-level code
runs, once. A module's imports come first, in the order of its `import`
lines, each with its own imports before it, and the library modules
whose prelude names it uses; then the module itself, as in rules 1 to 3
above. Last comes the entry file, and then its `main`. A module's
functions that nothing in the program uses aren't compiled.

**Locations.** Errors and warnings give `file:line:column`. The file is
the entry file as given to `ella`, other modules by their path from its
directory, joined to it (`geo/poly.ella:12:5` for `ella main.ella`), and
`<std>/maps.ella:136:13` for a library module. A package's module
([below](#packages)) is named the same way, by its path from the entry
file's directory: `ella app/main.ella --path libs` gives
`libs/json/lex.ella:3:9`.

### Packages

A *package* is code from outside the program: a module found in a
directory of the *package path*, with the modules below it. The package
path is the directories given to `ella` with `--path DIR` (which may be
given more than once), then those of the `ELLA_PATH` environment
variable, separated by `:`. Each directory is searched as if it were the
root: with `ELLA_PATH=~/ella/libs`, `import json` finds
`~/ella/libs/json.ella`, and `import json.lex` finds
`~/ella/libs/json/lex.ella`.

```sh
ella --path ../libs main.ella
ELLA_PATH=../libs:../vendor ella main.ella
```

* A package's modules are modules like any other, named by their paths
  from the package's directory: the rules above for imports, private
  names and running hold for them too. So a package's modules import each
  other by their full names (`import json.lex` in `json.ella`), and its
  private names are kept from its users.
* An import is found in the first place that has its module: the root,
  then each directory in order. A module of the program hides a package's
  of the same name, and an earlier directory's a later one's.
* Editors find packages from `ELLA_PATH`, as the editor's environment has
  it.

## 4. Bindings and assignment

### `let`

```ella
let x = 5
let names: [str] = ["bob", "steve", "jane"]
let mut count = 0
```

* `let` introduces a new name in the enclosing block. Bindings are
  **immutable** unless marked `mut`.
* The type annotation is optional where the type checker can infer it
  (from the value, and how it is used). A top-level function's parameters
  and result always need annotations ([§5](#5-functions)).
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
* Targets are names, index expressions (`a[i]`, `pool[h]`, `arena[r]`) and fields
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
* A parameter can also be a pattern ([§7](#7-patterns)) that takes the
  argument apart, as a `let` would, with its type after it:
  `((a, b): (i64, str)) => ...`, `(Point(x, y): Point) -> f64 => ...`, or
  without one where types can be left out: `map(pairs, ((n, word)) =>
  word)`. It must match every value, and can be a tuple, a struct's or
  variant's fields, `_` or names (`mut` ones too), nested; a literal,
  range or `|` can't be a parameter. Its names can be used in the body
  and in `where` conditions. The single parameter without parentheses
  (`n => ...`) is always a name.
* Functions take multiple arguments; there is no currying.
* A function bound at the top level, and a method, give their whole type:
  every parameter's, and `-> ReturnType`. Their types are known before
  anything is checked, which is what lets them call each other in any order.
* Other lambdas may leave out any of these types, and the checker infers
  them, mostly from where the lambda is passed: in `map(xs, (n) => n * 2)`,
  `n` is an item of `xs`. A single parameter needs no parentheses then:
  `xs |> filter(n => n > 0)`. A type nothing decides is an error where it
  is needed (`can't tell the type of this value yet`), and a lambda's
  types, once decided, are the same at every call: a local `let id = (x)
  => x` can't be called with an `i64` and then a `str` (a generic
  function can).
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

### Tail recursion

A direct self-call in tail position is guaranteed to reuse the current call
frame when the function has no captures or `mut` parameters, has no `where`
clauses, and all its parameters are scalar (not `str`). Tail position includes
the final expression of a function or block, either arm of an `if`, a `match`
case, and an explicit `return` expression. The tree interpreter, IR
interpreter, and native backend all support this case; the native backend also
resets the function's scope arenas between iterations, so temporary allocations do not
accumulate. Other tail calls currently use ordinary calls and count toward
the call-depth limit.

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

* `for pattern in expr` iterates over a list, string, or range, a
  generator ([§9](#generators-provisional-syntax)), or a struct or enum
  with an `iter` or `next` method ([§9](#methods)). The
  pattern must match every item, as a `let`'s must.
* `for pattern in mut place` advances an iterator or generator that is a
  `mut` variable (or an element of one), in place: after a `break`, it
  carries on where the loop stopped. Without `mut`, a loop over a struct
  or enum iterator goes through a copy of it, and a generator, which can't
  be copied, must be a new value (`for x in count(3)`) or a variable given
  away at its last use ([§9](#values-that-cant-be-copied-provisional-syntax)).
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
  (Arrays are the exception: `a * 2.0` works item by item, see
  [Arrays](#arrays-provisional-syntax).)
* `+` also concatenates two `str`s or two lists.
* `/` is true division and returns `f64` for `i64` or `f64` operands (and
  `f32` for `f32` ones).
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
  handles, arenas with refs, and structs and enums with `get` and `set` methods with any key
  ([§9](#methods)).
* `a[i, j]` is `a[(i, j)]`: indexing with a tuple. An array takes one `i64`
  for each of its dimensions ([§9](#arrays-provisional-syntax)), and a
  type with `get` and `set` methods can take tuple keys this way: `grid[1, 2]`.
* `a..b` is the half-open range `a <= i < b`; `a..=b` includes `b`.
* Indexing a list or string with a range gives a *slice*, a new list or
  string of those items: `"hello"[1..3]` is `"el"`, `xs[0..=1]` the first
  two items (copies of them). The range must be in order and within the
  value: `0 <= a <= b <= len(xs)`, or it is a run-time error. A slice
  can't be assigned to. (Compiled code shares the items instead of copying
  them wherever no program could tell; see
  [memory-model.md §18](memory-model.md#18-views).)

## 9. Types

```text
i64  i32  f64  f32  bool  str    built-in scalars
()                               unit
[T]                              list of T
Array[T]                         an array of T (i64 or f64), of any number of dimensions
(T, U)                           tuple
(T, U) -> R                      function (parameters always parenthesized)
(mut T, U) -> R                  function with a `mut` parameter
Result[T, E]   Optional[T]       generic types use [ ]
Point   Pair[T]                  structs and enums, by name
Pool[T]   Handle[T]              a pool of values, and a handle to one
Arena   Ref[T]                   an arena of values, and a ref to one
Gen[T]                           a generator of T values
Code[T]                          code that gives a T, for a later stage
Type[T]                          the type T as a value: `type T`
Type   Code                      a type, or code, whose type is only known later
$(t)                             a type computed while compiling (§11)
```

* All integer types are signed. `i32` is an integer of 32 bits, made with
  `to_i32(x)`: its arithmetic (`+ - * // % **`, unary `-`) traps when a
  result doesn't fit in 32 bits, as `i64`'s does for 64 (`i32 overflow:
  ...`), and `/` gives an `f64`. The bitwise operators and indexes are
  `i64` only; `to_i64` makes an `i32` an `i64`. `i8` and `i16` may come later.
* `f32` is a float of single precision (IEEE 754 binary32), made with
  `to_f32(x)` (there are no `f32` literals: `to_f32(0.5)`). Its arithmetic
  works as `f64`'s does, but each result is rounded to an `f32`; `/` on two
  `f32`s gives an `f32`, and `to_f64` makes one an `f64`. An `f32` prints
  with the fewest digits that give it back: `to_f32(0.1)` prints `0.1`
  (`to_f64(to_f32(0.1))` prints `0.10000000149011612`). In a generic
  function, `/` on a type parameter gives an `f64` whatever its type, so
  `f32`s are made `f64`s first, as `i64`s are.
  Arrays of numbers use SIMD instructions in native code
  ([Arrays](#arrays-provisional-syntax)).
* A tuple's items are read with `.0`, `.1`, ...: `pair.0`, `t.1.0`. A
  tuple can't be changed in place; build a new one.
* Types appear after `:` in annotations, after `->` in lambdas, and inside
  other types. The parser uses a separate type grammar there, so `[str]` (a
  type) and `[1, 2]` (a list literal) do not conflict.
* In a lambda, the return type is parsed with the type grammar up to the
  `=>`, so a function returning a function is
  `(x: i64) -> (i64) -> i64 => ...`.

### Type checking

Programs are type checked before they run. The parameters and results of
top-level functions and methods must be annotated; everything else is
inferred from values and how they are used:

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
* `print`, `eprint` and `len` are values too, taking one argument
  (`map(xs, print)`), as are variant constructors (`map(xs, Some)`, or
  `let make = Point`) and library functions like `trim`
  ([§10](#10-built-ins)). A prelude function whose first argument accepts
  a value can also be called as `value.function(args)`, for example
  `" hello ".trim()`, `(0..3).map(f)` or `count(0, 1).take(3)` (but not
  the lazy `iters.map` and the like, which share names with prelude
  functions). The other built-ins can only be called.
* Enums and structs can be generic. Top-level and local functions can also
  have type parameters.

### Warnings

Some mistakes don't stop a program, but probably aren't what was meant.
They are **warnings**: `ella` prints them to standard error (as
`file:line:column: warning: ...`) and runs the program anyway, and
editors show them.

* A `match` case that can never match ([§6](#match)).
* A local variable that is never used: a `let`, or a name in a pattern.
  A name that starts with `_`, like `_guard`, says that is on purpose (for
  a value kept only for its finalizer, say). The same goes for a
  parameter that is never used.
* A private top-level function ([§3](#modules-and-imports-provisional-syntax))
  that is never used outside its own body. Public ones may be used by
  other modules, so they aren't reported, except in the entry file, which
  nothing imports (and other than `main`). Top-level variables aren't
  reported.
* An import that is never used.
* A module of the program that has the name of a standard library
  module, and so is used instead of it.
* A `mut` local variable that never changes: nothing assigns to it,
  passes it as `mut`, or calls a `mut` method on it.
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
* A struct or enum can also be defined inside a function, or any block.
  Its name and its variants' names are local to the block, like a
  variable's, so two functions can each have their own `Point`, and one
  can hide a top-level type. It can't use the function's variables or
  type parameters, and can't have methods ([§3](#3-program-structure)), but
  it can give a constraint's constants (`implements Monoid` with only
  `const`s). Error messages name it with its function: `f#Point`. Its
  values print as `Point(...)`.

### Methods

A struct or enum can define functions of its own, *methods*, alongside its
fields or variants. A function whose first parameter is the type is an
instance method, called with `value.name(...)`. A function without that
receiver is in the type's namespace and is called with `Type.name(...)`:

```ella
struct Point
    x: f64
    y: f64
    norm2 = (p: Point) -> f64 => p.x * p.x + p.y * p.y
    move = (mut p: Point, dx: f64, dy: f64) -> () =>
        p.x += dx
        p.y += dy
    origin = () -> Point => Point(0.0, 0.0)

let mut p = Point(3.0, 4.0)
print(p.norm2())          # 25.0
mut p.move(1.0, 0.0)      # p is Point(4.0, 4.0)
print(Point.origin())

enum Shape
    Circle(f64)
    Rect(f64, f64)
    area = (s: Shape) -> f64 =>
        match s
            case Circle(r) => 3.0 * r * r
            case Rect(w, h) => w * h
```

* An instance method's first parameter must be of its type: `Point`, or
  `Stack[T]` in `struct Stack[T]`. A method of a generic type is generic in
  the type's parameters, and is specialized for each set of type arguments
  it is used with, like a generic function. Namespace functions have no
  implicit receiver, and can use those same type parameters.
* Each type has its own methods, so different types can have methods with
  the same name. A method can't have the name of a field, and only
  top-level structs and enums can have methods. Protocol methods such as
  `get`, `set` and `iter` must be instance methods.
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
| `update` | with `get` and `set`: `v[k] += x`, `v[k].f = x`, ... as a line, changing the item in place | `(mut Self, K, (mut V) -> ()) -> ()` |
| `iter` | `for x in v`: loops over what `v.iter()` gives | `(Self) -> R`, R a list, str, range, iterator or `Gen` |
| `next` | `for x in v`: v is an *iterator*, whose next item `next` gives | `(mut Self) -> Optional[T]` |
| `contains` | `x in v` (`v.contains(x)`), `x not in v` | `(Self, T) -> bool` |
| `text` | the text form, for `print`, `eprint` and `{{v}}` | `(Self) -> str` |
| `eq` | `a == b`, `a != b`, and `in` on lists, inside other values too | `(Self, Self) -> bool` |
| `hash` | `hash(v)`, inside other values too; a type with `eq` must have it | `(Self) -> i64` |

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
* An item that `get` gives is a copy, so without `update` only the last
  step of an assignment can go through `set`: `world.grids[0][(1, 1)] =
  4` works, but `table["a"].x = 1` doesn't, and `f(mut table["a"])`
  never does.
* `update(k, change)` must call `change` on the item of key `k`, in place:
  `mut t.update(k, (mut v) => v += x)` must do what `t[k] = t[k] + x`
  would. An assignment that is a line of a block (so its value isn't used)
  and goes through an item of a type with `update` uses it: `t[k] += [x]`
  runs as `mut t.update(k, (mut item) => item += [x])`, after working out
  `[x]`, so a list in a `Map` grows in place instead of being copied out
  and back in. Parts of items can be assigned to this way too:
  `table["a"].x = 1`, `grid[(1, 1)][0] += 1`. (`v[k] = x` still uses
  `set`, which can add a key.)
* `iter` gives a new list, string, range, iterator or generator, which
  the loop then goes through. (It is called once, before the first
  iteration.) A type with both `iter` and `next` is looped over with
  `iter`.
* An iterator's `next` gives `Some(item)` for each item, then `None`. A
  loop over one calls `next` until it gives `None`, on a copy of the
  iterator, or on the iterator itself in `for x in mut it`. Like any
  method that changes its value, it can also be called directly:
  `mut it.next()`.

```ella
struct Countdown
    n: i64
    next = (mut c: Countdown) -> Optional[i64] =>
        if c.n == 0 then return None
        c.n -= 1
        Some(c.n + 1)

for n in Countdown(3)
    print(n)                     # 3, 2, 1
```

* `text` gives a value's text form wherever it is written: alone, or inside
  a list, a tuple or another struct or enum (`[Celsius(1.5)]` prints
  `[1.5°C]`), whose text is built around it. Its result is used as it is,
  never quoted. It is called each time the value is written, and an error
  in it shows `called to make a value's text` in the stack trace. Error
  messages that show a value (`no case matches ...`) use the built-in
  form, without calling it.

  ```ella
  struct Celsius
      degrees: f64
      text = (c: Celsius) -> str => "{{c.degrees}}°C"

  print([Celsius(21.5)])           # [21.5°C]
  ```

* `eq` decides `==` (and `!=`) wherever values of its type are compared:
  alone, inside lists, tuples and other structs and enums, by `in` on a
  list, and as the keys of a `Map` or items of a `Set`. Since those find
  keys by `hash` first, a type with `eq` must have a `hash` method too,
  giving equal values the same hash; `hash(v)` calls it, inside other
  values too. Patterns still match field by field, and an error in either
  method shows `called to compare two values` (or `called to hash a
  value`) in the stack trace.

  ```ella
  struct Word
      letters: str
      eq = (a: Word, b: Word) -> bool => lower(a.letters) == lower(b.letters)
      hash = (w: Word) -> i64 => hash(lower(w.letters))

  print([Word("Hi")] == [Word("hI")])   # true
  ```

* A user-defined constraint can ask for these methods too, so generic code
  can index, loop over and search its values.
* All these names (and the operators' above, and `finalize`) are kept for
  their meaning: a method with one of them must have its type.

### Arrays **(provisional syntax)**

An `Array[T]` holds numbers, `i64`s, `i32`s, `f64`s or `f32`s, in any
number of dimensions: a vector has one, a matrix two. Its *shape* is the
size of each dimension, and its items are kept together, row by row (the
last index changes fastest), so that native code works on them with SIMD
instructions (AVX2, four numbers at a time, or eight `f32`s or `i32`s).

```ella
let mut m = reshape(array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0]), [2, 3])
print(m)                  # array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
print(shape(m), len(m))   # [2, 3] 6
print(m[1, 2])            # 6.0
m[0, 0] = 10.0
let v = m * 0.5 + m       # item by item
print(total(v), dot(m, m))
print(matmul(m, transpose(m)))
```

* `array(xs)` makes a one-dimensional array of a list's numbers;
  `fill(shape, x)` an array of the shape with every item `x`; and
  `reshape(a, shape)` one with the same items in another shape. A shape is
  a list of sizes: at least one, at most 32, none negative, and those that
  aren't 0 must multiply to less than 2⁴⁸. See [§10](#10-built-ins) for the
  other array functions.
* `a[i, j]` is the item at row i, column j, with an `i64` for each
  dimension (`a[i]` for one dimension, or a tuple of them, `a[t]`). Too many
  or too few indexes, or one out of range, is a run-time error. Items can be
  changed in place in a `mut` array: `m[0, 1] = 2.0`, `m[i, j] += x`.
* A *slice* takes a range for some dimensions: `m[1..3, 0]` is a new array
  of items 0 of rows 1 and 2. A range keeps its dimension (as long as the
  range), an `i64` drops it, and the last dimensions can be left out, which
  takes them whole: `m[1..3]` is rows 1 and 2, `m[0, 1..3]` items 1 and 2 of
  row 0. Each range must be in order and within its dimension. A slice is a
  copy, and can't be assigned to.
* `+`, `-` and `*` work item by item on two arrays of the same type, giving
  a new array; `/` too, on arrays of `f64` or `f32`. Either side can be a
  number of the items' type instead, used for every item: `a * 2.0`,
  `1.0 - a`. Unary `-` negates every item. As for numbers, `i64` items trap
  on overflow: the error is for the first item of the result (row by row)
  that overflows.
* Two arrays of different shapes *broadcast*, as in NumPy: lined up from
  their last sizes, each two sizes must be the same, or one of them 1 (a
  size that isn't there counts as 1). The result has the larger size in
  each place, and an array of size 1 there gives the same items all along
  it: `m + array([10, 20, 30])` adds the row to each row of an `[n, 3]`
  array, and `m * reshape(array([1, -1]), [2, 1])` scales each row of a
  `[2, n]` one. Shapes that don't broadcast are a run-time error.
* `==` and `!=` compare the shapes and the items. Arrays can't be ordered.
* `len(a)` is the number of items, and `for x in a` goes through them in
  order, row by row.
* `total(a)` and `dot(a, b)` add up the items, or the products of two
  arrays' items, in four sums at once: item i goes into sum i % 4, and
  then the sums are added as `(s0 + s2) + (s1 + s3)`, each sum starting
  at 0. `i64` ones trap on the first addition (or product) that
  overflows, in that order. `matmul` adds each item's products in order,
  starting at 0.
* **Float results of arrays may differ in their last bits between
  backends.** The interpreters do each operation as written, rounding after
  each, in the order above. Native code may do a multiplication and an
  addition as one *fused multiply-add*, which rounds once (`a * b + c` in
  an expression of arrays, `dot`, `matmul`), and may add the items of
  `total`, `dot` and `matmul` in other orders, more sums at once. So an
  `f64` (or `f32`) result can be more exact, or less, by a few units in its
  last place, and where that decides it, an infinity or NaN can come out
  differently. Everything else is the same in every backend: `i64`
  results, shapes, and every error (an `i64` overflow is the first in the
  order above). Integer results and errors are the same for `i32`s, which
  trap at 32 bits. Arithmetic on single numbers is always exactly the same.
* Native code does arithmetic on arrays written as one expression, such as
  `total(a * 2.0 + b - c)`, in one loop that makes no arrays in between
  (that is an optimization, with `-O`; what it gives is the same, as above).
* An array is a value like any other: `let b = a` copies, and so does
  passing one to a function, which changes a `mut` parameter's array in place.
* An array prints as nested lists, one level per dimension:
  `array([[1, 2], [3, 4]])`.

#### Static shapes

**(provisional)** An array type can also give its shape after a `;`, so
that the checker finds mismatched shapes while compiling, and the
program checks the rest when it runs:

```ella
Array[f64]              # any rank, any sizes
Array[f64; _, _]        # rank 2, sizes not tracked
Array[f64; N, 3]        # rank 2: N rows (a size name), exactly 3 columns

let mm[T: Num; N, K, M] = (a: Array[T; N, K], b: Array[T; K, M]) -> Array[T; N, M] => matmul(a, b)
let mean[; N] = (xs: Array[f64; N]) -> f64 => total(xs) / to_f64(N)
```

* A dimension is a size (`3`), a size name (`N`) or `_`, which fits any
  size. `Array[T;]`, with no dimensions, is an error, and only `Array` has
  dimensions.
* Size parameters follow a `;` in a top-level function's brackets, after
  any type parameters (`[T: Num; N, K, M]`, or `[; N]` alone). Inside the
  function a size parameter is fixed: `N` against `3`, or against another
  size parameter, is an error. Each call decides them anew, from its
  arguments.
* In a local annotation, a size name's first use takes the value's size,
  and later uses must agree:
  `let a: Array[f64; N, M] = ...` then `let b: Array[f64; M, N] = transpose(a)`.
* The checker knows the shapes the library gives: `transpose` swaps two
  dimensions, `matmul` needs the inner sizes to agree, `dot` needs equal
  shapes, `array(xs)` has one dimension of a size not tracked (`_`),
  `sums(a, axis)` drops dimension `axis` when the axis is a number written
  in the call, and `fill` and `reshape` give an array whose rank isn't
  tracked.
* Indexing checks the number of indexes when the rank is known (`a[0]` on
  an `Array[f64; N, M]` is an error). In a slice, an `i64` drops its
  dimension, a range gives `_`, and dimensions left out keep their sizes.
* Arithmetic broadcasts shapes as at run time, lined up from the right:
  each two sizes must agree, or one be the size 1; `_` gives `_`, and a
  rank not tracked on either side gives a rank not tracked.
* **Run-time checks.** Where the checker can't be sure of a shape, the
  program checks it when it runs:
  * A function checks each parameter whose type is an array with
    dimensions when it is called, before its `where` conditions: its rank,
    each size it names, and that a size name used twice has one size.
  * A function whose result's type has dimensions checks it as it
    returns, unless every value it can return is sure to have that shape.
  * A `let` whose variable's type has dimensions checks the value, unless
    its shape is sure to be right (or it defines size names, below).

  A wrong shape stops the program with an error naming what was expected,
  `_` for any size: `argument 'm' should have shape [_, 2], not [2, 3]`,
  `the value of 'v' should have shape [3], not [4]`, `the function's result
  should have shape [2], not [5]`.
* **Size names are numbers.** A size name that a parameter's array has as
  a dimension is an `i64` variable in the function (in its `where`
  conditions too), and so is one that a `let`'s array introduces, after
  the `let`: `for i in 0..N`. A size the program can't know when it runs
  (one only inside a list's or tuple's type, or only in the result's) is
  an error to use as a number. A parameter that is only there for its
  sizes counts as used.

  ```ella
  let rows[; N, M] = (a: Array[f64; N, M]) -> [f64] =>
      let mut result = []
      for i in 0..N
          result = result + [total(a[i, 0..M])]
      result
  ```
* Shapes are erased after checking: they never make separate copies of a
  function, and the run-time checks of the operations themselves stay.
  Only the arrays themselves are checked, not arrays inside lists, tuples,
  structs or functions' types (`[Array[f64; 3]]`), whose shapes are
  trusted, nor values assigned to a `mut` variable later. Structs and
  enums can't have size parameters yet.

### Errors

`Error` is a built-in struct for the error side of a `Result`, holding a
message, the error that caused it if any, and where it was made:

```ella
struct Error
    message: str
    cause: Optional[Error]
    at: str
```

```ella
let invert = (n: f64) -> Result[f64, Error] =>
    if n == 0.0 then Err(Error("divide by zero")) else Ok(1.0 / n)

match invert(0.0)
    case Ok(v) => print(v)
    case Err(e) => print(e.message)    # divide by zero
print(invert(0.0))                     # Err(Error("divide by zero"))

let read = (text: str) -> Result[i64, Error] =>
    match parse_i64(text)
        case Ok(n) => Ok(n)
        case Err(e) => Err(Error("bad count", e))
print(read("x"))                       # Err(Error("bad count", Error("\"x\" isn't an i64")))
```

* It is made by `Error(message)`, or `Error(message, cause)` with the
  `Error` it comes from, which its `cause` holds as `Some(cause)`. Its `at`
  is where it is made, as an error message writes it: `12:9`, or
  `geo/poly.ella:12:9` in another module. The library functions that give
  an `Err` make its `Error` where they are called. Used as a value
  (`map(messages, Error)`), `Error` makes its errors where it is written.
* Its fields are read like any struct's, and the message and cause can be
  changed, but not `at`.
* Its patterns may leave out the last fields: `Error(message)`,
  `Error(message, Some(cause))`, `Error(message, cause, at)`.
* `==` leaves out where errors were made, and so does the text form, which
  shows the cause without its `Some`: `Error("bad count", Error("..."))`.
* An `Err` that `main` returns stops the program with its `Error`,
  reported at its `at`, with a line for each cause ([§3](#3-program-structure)).
* A program can define its own `Error`, which replaces it, and is an
  ordinary struct.
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

* A value of a nocopy type (a nocopy struct, a pool, an arena, a generator, or
  anything holding one) is made where it stays. It can be bound with `let`, passed to
  functions (read-only, or as `mut`), read field by field, compared and
  printed, held by another nocopy struct, and moved into a pool or an
  arena with `alloc`. A function can return a new one, or one of its own local
  variables declared outside loops (which moves it to the caller).
* It can't be copied: not bound from another variable, assigned, put in a
  list, captured by a closure, returned from a parameter, or used with a
  generic function.
* **Giving one away.** A parameter that *takes* its argument (a
  generator's, or an `Iter[T]` one, [§9](#generic-functions)) and a `for`
  loop without `mut` accept a new value, or a variable at its last use,
  which gives the value away:

  ```ella
  let evens = count(0, 2) |> take(3)
  print(evens.collect())    # gives evens away
  print(evens)              # error: 'evens' was given away at 2:7
  ```

  The variable must be the function's own (a local variable, a
  generator's parameter, or an `Iter[T]` parameter, which takes its
  argument), or a top-level variable that no function uses,
  and no loop may come between where it is declared and where it is given
  away, since the loop's next time round would use it again. After that,
  nothing may use it; if one branch of an `if` or `match` gives it away,
  it counts as given away after them.
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
  dies, and one moved into an arena when the arena is freed or dies. Values that die together are finalized newest first: a struct
  before the fields it holds.
* If the program stops with an error, finalizers don't run.

### Generic functions

A function bound with `let` can have type parameters after its name, either
at the top level or inside another function:

```ella
let first[T] = (xs: [T]) -> T => xs[0]
let swap[A, B] = (p: Pair[A, B]) -> Pair[B, A] => Pair(p.second, p.first)

let use = (prefix: str) -> str =>
    let tag[T] = (value: T) -> str => prefix
    tag(1)

print(first([1, 2]))       # T is i64
print(first(["a"]))        # T is str
```

* The type arguments are inferred from each use. Each different set of
  them makes a separate copy of the function when compiling. A local
  generic function can call itself recursively and captures local values
  by copy, like any other lambda; its type parameters are not available as
  an anonymous polymorphic function value.
* Inside the function, a type parameter is a type of its own, which only
  matches itself. Values of type `T` can be passed around, stored and
  printed; anything more needs a *constraint*.
* Type parameters can't be values that can't be copied.

**`Iter` parameters.** A parameter of type `Iter[T]` takes anything a `for`
loop can go through with items of type `T`: a list, a string (`T` is
`str`), a range (`i64`), an array, a generator, an iterator (a struct or
enum with a `next` method), or a value whose `iter` method gives one of
these ([§9](#methods)):

```ella
let total = (xs: Iter[i64]) -> i64 =>
    let mut sum = 0
    for x in xs
        sum += x
    sum

print(total([1, 2]), total(0..10), total(count(5)))   # count gives a Gen[i64]
```

* A function with one is generic: it is made for each type of value
  passed, as for a type parameter, with loops over the parameter compiled
  for that type. Each `Iter[T]` parameter has its own such type; `T` is an
  ordinary type, or one of the function's type parameters, decided by what
  is passed (so `map(0..3, x => -x)` knows `x` is an `i64`). Where what is
  passed has a type not known yet, it is taken to be a list, as it would be
  for a `[T]` parameter; so is the parameter of a generic function used
  only as a value.
* `Iter[T]` can only be the whole type of a parameter of a function bound
  with `let` (not a method, nor a result, nor part of another type).
* Inside, the function can loop over the parameter (`for x in xs`, without
  `mut`) and pass it to another function's `Iter[T]` parameter. Since it
  may be a generator, it can't be copied: not bound to another variable,
  captured, returned, or passed to a type parameter.
* A loop over it goes through it as a loop over the value passed would
  (`for x in count(5)`). **So a second loop depends on what was passed**:
  over a generator, it carries on where the first stopped; over a list,
  range, string or iterator (through a copy), it starts again:

  ```ella
  let twice = (xs: Iter[i64]) -> i64 => total(xs) + total(xs)
  twice([1, 2])                  # 6: each loop goes through 1, 2
  twice(take(count(1, 1), 2))    # 3: the second loop finds the generator done
  ```

  A function that goes through its argument more than once, and should
  behave the same for every kind, collects it first:
  `let items = collect(xs)`. That list can also be copied, which the
  parameter can't (the checker's error says so).
* The parameter takes what it is given, as a generator's do: a generator
  passed must be a new value, like `total(count(5))`, or a variable given
  away at its last use ([§9](#values-that-cant-be-copied-provisional-syntax)).
  Since it takes its argument, the parameter is the function's own: it can
  pass it on to another `Iter[T]` parameter, or give it away, to a
  generator say.

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
| `Eq` | `==`, `!=`, `in` | every type except functions, pools, arenas, generators and code |
| `Ord` | `<`, `<=`, `>`, `>=` (and what `Eq` allows) | `i64`, `i32`, `f64`, `f32`, `str`, types with a `compare` method |
| `Add` | `+` | `i64`, `i32`, `f64`, `f32`, `str`, lists, arrays, types with an `add` method of type `(Self, Self) -> Self` |
| `Zero` | `T.ZERO`, the value `+` starts from | `i64`, `i32`, `f64`, `f32` (`0`), `str` (`""`), lists (`[]`), types that implement it |
| `Num` | `+`, `-`, `*`, `/`, `**`, unary `-` (and what `Ord` and `Zero` allow) | `i64`, `i32`, `f64`, `f32` |

* The body is checked once, against the constraints: `(a: T, b: T) -> T =>
  a + b` needs `[T: Add]`. `/` on a `Num` gives an `f64`, as it does on
  `i64` and `f64` (an `f32` is made an `f64` first).
* Each use is checked against them: `largest([true])` is an error, as
  `bool` isn't `Ord`. A type parameter meets the constraints it has, so a
  generic function can call another one with its own `T`.
* `Num` is only met by numbers: `/` on a `Num` gives an `f64`, which a
  struct's `div` needn't.
* `Zero` is the one built-in constraint with a constant
  ([constants](#user-defined-constraints-provisional-syntax)): `T.ZERO` is
  the value adding starts from, so `sum(xs)` needs no start value. A
  struct or enum meets it by implementing it, as it would a user-defined
  constraint, giving the constant:

  ```ella
  struct Money
      cents: i64
      add = (a: Money, b: Money) -> Money => Money(a.cents + b.cents)
      implements Zero
          const ZERO = Money(0)

  print(sum([Money(5), Money(7)]))     # Money(12)
  ```

  Nothing checks that `x + T.ZERO` is `x`. Arrays don't meet it, since
  their zero depends on a shape their type may not know: `total(a)` adds
  an array's items, and `fold` adds arrays. A user-defined constraint can
  require `Zero` (`constraint Monoid: Zero`); a type implementing that
  gives `ZERO` in its `implements Monoid`, and can't also implement `Zero`.

### User-defined constraints **(provisional syntax)**

A program can define its own constraints: the methods a type must have, with
`Self` standing for the type, and constants it must give. A type meets one by
saying so, with `implements`:

```ella
constraint Shape
    area: (Self) -> f64
    name: (Self) -> str

struct Square
    side: f64
    implements Shape
        area = (s: Square) -> f64 => s.side * s.side
        name = (_s: Square) -> str => "square"

let total[T: Shape] = (xs: [T]) -> f64 =>
    let mut sum = 0.0
    for x in xs
        sum += x.area()
    sum

print(total([Square(2.0), Square(1.0)]))     # 5.0
```

* A struct or enum meets a user-defined constraint only if it declares
  `implements Shape`: having the methods isn't enough. Its methods can be in
  the `implements` block, or among its other methods (then the line is just
  `implements Shape`). The checker verifies at the declaration that each
  required method exists and has the right type (with `Self` replaced by the
  type).
* In a generic function, a value of a type parameter has the methods of its
  constraints: `x.area()`. A method can be `mut` (`tick: (mut Self) -> ()`),
  and is then called as `mut x.tick()`. Two constraints of one type
  parameter can't have different methods with the same name.
* User-defined and built-in constraints combine: `[T: Shape + Ord]`. The
  built-in ones are still met as their table says, without `implements`
  (except that a struct or enum meets `Zero` by implementing it).
* A constraint's method named `len` gives `len(x)` in the generic function;
  operators still need the built-in constraints.
* Each use is still compiled for its own types, so a method call is a
  direct call: there is no dynamic dispatch.

**Composition.** A constraint can require others, user-defined or built-in,
after `:`, combined with `+`. It then has their methods and constants too, and
a type that implements it implements them:

```ella
constraint Named
    name: (Self) -> str

constraint Ranked: Named + Ord
    score: (Self) -> i64

struct Player
    nick: str
    points: i64
    compare = (a: Player, b: Player) -> i64 => a.points - b.points
    implements Ranked
        name = (p: Player) -> str => p.nick
        score = (p: Player) -> i64 => p.points

let label[T: Named] = (x: T) -> str => "<{{x.name()}}>"
let best[T: Ranked] = (a: T, b: T) -> str => label(if a > b then a else b)

print(best(Player("ann", 3), Player("bob", 5)))     # <bob>
```

* A `Ranked` type parameter is also `Named` and `Ord`, so `best` can use
  `>` and pass its `T` to `label`.
* The `implements Ranked` block gives the methods of `Ranked` and of
  `Named`. The type must also meet the built-in constraints `Ranked`
  requires, as their table says (`Player` has `compare`, so it is `Ord`).
* The constraints required, directly or not, can't include the constraint
  itself, and must agree: two with a method (or constant) of the same name
  must give it the same type.
* A type implements each user-defined constraint once: after
  `implements Ranked` it can't also say `implements Named`.

**Constants.** A constraint can require typed constants, `const NAME: Type`
(`Self` may be in the type). Each implementation gives their values, as
`const NAME = expression`, and generic code reads them as `T.NAME`:

```ella
constraint Monoid
    combine: (Self, Self) -> Self
    const EMPTY: Self

struct Sum
    n: i64
    implements Monoid
        combine = (a: Sum, b: Sum) -> Sum => Sum(a.n + b.n)
        const EMPTY = Sum(0)

let fold_all[T: Monoid] = (xs: [T]) -> T =>
    let mut acc = T.EMPTY
    for x in xs
        acc = acc.combine(x)
    acc

let none: [Sum] = []
print(fold_all([Sum(2), Sum(3)]))     # Sum(5)
print(fold_all(none))                 # Sum(0)
```

* `T.NAME` is the constant of the type `T` stands for: each use of the
  function is compiled for its own types, so it is that type's value.
* A constant is computed once, while compiling, and the program holds its
  value, made before the program's code runs (so reading a list constant
  in a loop doesn't make it again). Its expression is compile-time code,
  like a splice outside quotes ([§11](#11-stages)). It can call top-level
  functions (defined before or after it), make values of any type, and
  read other constants through generic functions, but it can't use the
  program's variables (`let g = 5` then `const N = g` is an error), since
  they don't exist yet. What it prints comes out while compiling. An error
  in it, a constant that needs its own value, and `exit()` stop the
  compilation; editors show them too.
* So a constant's type can only hold values that can be written as code:
  numbers, `bool`s, strings, tuples, lists, structs and enums (`Optional`,
  `Map`, ...), but not functions, arrays, or nocopy values.
* A constant is an ordinary value in the program: it can't change
  (`T.NAME = x` is an error), and it can't be used in a type
  (`Array[f64; T.SIZE]`) yet.
* A local variable named `T` hides the type parameter in `T.NAME`.
* A struct's or enum's own type parameters can have such a constraint
  too, and its methods read `T.NAME` for the type it holds: in
  `struct Acc[T: Monoid]`, `Acc[Sum]`'s methods see `Sum`'s constants. A
  generic type can also implement such a constraint itself
  (`struct Bag[T]` with `implements Monoid`).
* A struct or enum defined inside a function can give constants too, as
  long as the constraint has no methods it would have to give.

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
  ("stale"), even after the slot is reused. It also identifies its pool,
  so using it with another pool of the same type is a run-time error.
  Slots are retired before their generation can wrap.
* A pool can't be copied.

### Arenas **(provisional syntax)**

An arena holds values of any types that are freed together, all at once,
such as the parts of a game level or of a request. Each value is found with
a ref:

```ella
struct Room
    name: str
    exits: [Ref[Room]]

let mut level: Arena = arena_new()
let hall = alloc(mut level, Room("hall", []))
let den = alloc(mut level, Room("den", [hall]))
level[hall].exits += [den]
let title = alloc(mut level, "Level 1")
print(level[title], level[level[den].exits[0]].name)    # Level 1 hall
free(mut level)                                          # all of it, at once
print(level[hall])                                       # error: the ref is stale
```

* `arena_new()` makes an empty `Arena`. `alloc(mut arena, value)` puts a
  copy of the value in the arena (moving one that can't be copied), and
  returns a `Ref[T]` for it, where `T` is the value's type. An arena can
  hold values of different types.
* `arena[r]` is the value, and `arena[r] = v`, `arena[r].f = v` and so on
  change it in place. Replacing a value doesn't free the old one: it stays
  in the arena until the arena is freed. `len(arena)` is how many values it
  holds.
* `free(mut arena)` frees all its values at once, finalizing them newest
  first, and leaves the arena empty, to be used again. An arena that dies
  frees its values too.
* Refs are plain values: they can be copied, stored and compared. Each
  remembers its arena and the arena's *generation*, which goes up when the
  arena is freed, so using a ref after its arena was freed, or with
  another arena, is a run-time error ("stale"). One check covers every
  value of the arena.
* An arena can't be copied. It can hold pools and other arenas, and pools
  can hold arenas.

### Generators **(provisional syntax)**

A function with `yield` in its body is a *generator*: calling it doesn't
run the body, but gives a `Gen[T]`, which runs the body a step at a time,
as its items are asked for. Each `yield value` gives one item:

```ella
let count = (n: i64) -> Gen[i64] =>
    let mut i = 0
    while i < n
        yield i
        i += 1

let squares = (g: Gen[i64]) -> Gen[i64] =>
    for x in mut g
        yield x * x

for x in squares(count(4))
    print(x)                     # 0, 1, 4, 9

let mut g = count(2)
print(next(mut g))               # Some(0)
print(next(mut g), next(mut g))  # Some(1) None
```

* `next(mut g)` runs the body until its next `yield` and gives
  `Some(value)`, or `None` once the body has finished (by its end, or a
  `return`), and every time after that. `for x in g` and `for x in mut g`
  call it until it gives `None`.
* A generator's result type is `Gen[T]`, where `T` is the type of the
  values it yields (a generator that leaves out its result type gets it
  from its `yield`s). `yield value` itself has type `()`. `yield` belongs
  to the function it is written in: one in a nested lambda makes that
  lambda a generator. It can't be used in a quote, and `return` in a
  generator has no value.
* Calling a generator evaluates its arguments and copies them into the
  new `Gen`, along with the variables it captures; preconditions (`where`)
  are checked then, and postconditions aren't allowed. A generator can't
  have `mut` parameters, but its parameters are its own variables, which
  its body can assign to and pass as `mut`.
* A `Gen` can't be copied (§9, [values that can't be
  copied](#values-that-cant-be-copied-provisional-syntax)), so a function
  that gives one *takes* its arguments that can't be copied: each must be
  a new value, such as `squares(count(4))`, or a variable given away at its
  last use ([§9](#values-that-cant-be-copied-provisional-syntax)). (Pipes
  read well with this: `count(4) |> squares`.) The values it yields can't
  be nocopy.
* A generator's variables, and the values with finalizers that it makes,
  live as long as it does: until its body finishes, or the `Gen` dies.
  When a `Gen` dies before its body has finished, those values are
  finalized as if the body had stopped where it is: the innermost loop
  iteration's values first, newest first.
* A run-time error in a generator's body stops the program at its
  location, from the `next` that ran it, which its stack trace shows as a
  call. Each `next` counts as a call for the call depth limit.
* A `Gen` prints as `<gen>`, and isn't `Eq`.

## 10. Built-ins

Built-in functions (ordinary names, not keywords):

* `print(a, b, ...)` prints the text forms of its values, with a space
  between each two, and a newline; returns `()`. `print()` prints an empty
  line, and `print("x", 1)` prints `x 1`.
* `eprint(a, b, ...)` prints the same way to standard error, after
  everything printed so far.
* `len(x)` returns the length of a list, string or range as `i64`, the
  number of items of an array, or how many values a pool or arena holds. On a
  struct or enum, it calls its `len` method.
* `next(mut g)` gives the next item of a generator, or of an iterator
  (its `next` method), as an `Optional`
  ([§9](#generators-provisional-syntax)).
* `pool_new()`, `alloc(mut pool, value)` and `free(mut pool, handle)` work on
  pools ([§9](#pools-provisional-syntax)); `arena_new()`,
  `alloc(mut arena, value)` and `free(mut arena)` on arenas
  ([§9](#arenas-provisional-syntax)).
* `run(code)` runs code made with `quote` ([§11](#11-stages)).
* The library functions below, and the prelude's ([below](#the-prelude)).
* `fields(t)`, `field_type(t, name)`, `has_field(t, name)`, `type_name(t)`,
  `type_kind(t)` and `variants(t)` look at a type `t`, and
  `new_struct(name, fields, types)` makes one
  ([§11](#types-as-values-and-generated-code)).

The library: functions with fixed types. A program may use their names for
its own variables. They are values too, as `print`, `eprint` and `len`
are, so `apply(trim, s)`, `let ops: [(i64, i64) -> i64] = [min, max]` and
`let parse = parse_i64` work; such a value prints as `<function>`.

| Function | Type | Gives |
| --- | --- | --- |
| `to_f64(n)` | `[T: Num] (T) -> f64` | `n` as the nearest `f64` |
| `to_f32(n)` | `[T: Num] (T) -> f32` | `n` as the nearest `f32` (an infinity if it is too big for one) |
| `to_i32(n)` | `[T: Num] (T) -> i32` | `n` (without a float's fraction) as an `i32`; an error if that doesn't fit |
| `to_i64(x)` | `[T: Num] (T) -> i64` | `x` (without a float's fraction, toward zero) as an `i64`; an error if that doesn't fit, or `x` is NaN |
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
| `hash(x)` | `[T: Eq] (T) -> i64` | a number made from `x`, the same in every run and backend, and the same for values that are `==` (below) |
| `format_f64(x, digits)` | `(f64, i64) -> str` | `x` with `digits` decimals (from 0 to 100), rounded: `format_f64(2.5, 2)` is `"2.50"`; `nan`, `inf` and `-inf` as `print` writes them |
| `env(name)` | `(str) -> Optional[str]` | the environment variable `name`; `None` if there is none |
| `write_file(path, text)` | `(str, str) -> Result[(), Error]` | writes `text` to the file in UTF-8, replacing what it held, or gives why it can't |
| `exit(status)` | `(i64) -> never` | stops the program at once with `status`, from 0 to 255 |
| `sqrt(x)` | `(f64) -> f64` | the square root of `x`; NaN if `x` is negative |
| `floor(x)` | `(f64) -> f64` | the largest whole number not above `x` (`to_i64(floor(x))` makes it an `i64`) |
| `ceil(x)` | `(f64) -> f64` | the smallest whole number not below `x` |
| `round(x)` | `(f64) -> f64` | the nearest whole number; halfway cases go away from zero (`round(-2.5)` is `-3.0`) |
| `exp(x)`, `ln(x)` | `(f64) -> f64` | e to the power `x`, and the natural logarithm of `x` (`-inf` for 0, NaN below it) |
| `sin(x)`, `cos(x)`, `tan(x)` | `(f64) -> f64` | the sine, cosine and tangent of `x` radians; NaN for an infinite `x` |
| `asin(x)`, `acos(x)`, `atan(x)` | `(f64) -> f64` | the angle in radians whose sine, cosine or tangent is `x`; NaN for `asin` and `acos` outside -1 to 1 |
| `atan2(y, x)` | `(f64, f64) -> f64` | the angle in radians, from -π to π, of the point (`x`, `y`) |
| `log10(x)`, `log2(x)` | `(f64) -> f64` | the logarithm of `x` in base 10 or 2 (`-inf` for 0, NaN below it) |
| `abs(x)` | `[T: Num] (T) -> T` | `x` without its sign; an `i64` error for the smallest `i64` |
| `min(a, b)` | `[T: Num] (T, T) -> T` | `b` if `b < a`, otherwise `a` |
| `max(a, b)` | `[T: Num] (T, T) -> T` | `b` if `b > a`, otherwise `a` |
| `array(xs)` | `[T: Num] ([T]) -> Array[T]` | a one-dimensional array of the list's items |
| `fill(shape, x)` | `[T: Num] ([i64], T) -> Array[T]` | an array of that shape, every item `x` |
| `reshape(a, shape)` | `[T: Num] (Array[T], [i64]) -> Array[T]` | `a`'s items, in order, in another shape with as many items |
| `shape(a)` | `[T: Num] (Array[T]) -> [i64]` | the size of each dimension |
| `flat(a)` | `[T: Num] (Array[T]) -> [T]` | the items, as a list, row by row |
| `total(a)` | `[T: Num] (Array[T]) -> T` | the sum of the items, in four sums ([§9](#arrays-provisional-syntax)) |
| `dot(a, b)` | `[T: Num] (Array[T], Array[T]) -> T` | the sum of the products of the items of two arrays of the same shape, in four sums |
| `matmul(a, b)` | `[T: Num] (Array[T], Array[T]) -> Array[T]` | the matrix product of a `[n, k]` and a `[k, m]` array: item (i, j) is the sum of `a[i, p] * b[p, j]` |
| `transpose(a)` | `[T: Num] (Array[T]) -> Array[T]` | a two-dimensional array with its rows as columns |
| `sums(a, axis)` | `[T: Num] (Array[T], i64) -> Array[T]` | the items added up along dimension `axis` (from 0), which the result doesn't have: `sums(m, 0)` adds up an `[n, m]` array's columns, `sums(m, 1)` its rows. Each sum starts at 0 and adds in order, so it is the same in every backend. `a` needs 2 or more dimensions |

* `hash` is 64-bit FNV-1a: over the code points of a string, and over
  the hashes of the parts of other values (the items of a list or tuple;
  a variant's name, then its fields). Numbers, `bool`s and handles are
  one part each; `0.0` and `-0.0` hash the same, as do all NaNs; a range
  is its length and, unless empty, its start; a ref is its index; and an
  `Error` leaves out where it was made, as `==` does; an array is the
  tuple of its shape and its items. A struct or enum
  with a `hash` method ([§9](#methods)) hashes to what
  it gives, wherever it is.
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
  function with `[T: Num]` ([§9](#generic-functions)).
  Their definitions say what happens with NaN and `-0.0`: `min(nan, 1.0)`
  is NaN, `min(1.0, nan)` is `1.0`, and `min(0.0, -0.0)` is `0.0`.

The text form of a value (used by `print` and interpolation) is how it would
be written in Ella: `42`, `2.5`, `true`, `()`, `[1, 2]`, `(1, "a")`,
`Some(3)`, `None`, `Point(1.0, 2.0)`. A string is its own text, but is
quoted when it appears inside another value. A pool is `<pool of 3>`, a
handle `<handle 0:1>` (its slot and generation), an arena `<arena of 3>`,
a ref `<ref 2>` (which of the arena's values it is), a generator `<gen>`, and code `<code ...>`,
with the code as an S-expression.

Structs and enums can give themselves `len`, operators, indexing,
iteration, `in` and their text form with methods ([§9](#methods)).
A `Map` or `Set` is written as its literal is: `{"a": 1}`, `{1, 2}`.

`assert expr` is a keyword expression rather than a function, so that a
failure can report the asserted expression's source text. It has type `()`.

Run-time errors (failed `assert`, failed `where`, out-of-range index or
slice, division by zero, `i64` overflow, and a few library errors) stop the
program with a message and the source location, then a *stack trace*: the
calls in progress, innermost first, each where it is and in which function
(nothing for the top-level code). It goes to standard error:

```text
lists.ella:5:53: index 5 is out of range for a list of length 3
  called at lists.ella:10:44 in pick
  called at <std>/lists.ella:14:21 in lists.map
  called at lists.ella:11:12 in total
  called at lists.ella:19:16 in main
```

* A function is named by its `let` (a method as `Grid.cell`, a module's
  functions with the module's name, `lists.map`), and is `lambda` if it
  has none. `main`'s own call isn't shown, since the program doesn't
  write it.
* A call that ran a finalizer is `called to finalize a value`, and each
  `next` that ran a generator's body is a call where the `next` is.
* A library function used as a value (`map(codes, chr)`) reports its
  errors where it is called, as if it were called there by name.
* A trace of more than 20 calls shows the innermost and outermost 10, with
  `... N more calls` between them.

Functions can call each other 10,000 deep: one more nested call stops the
program with "stack overflow: more than 10000 nested calls". The top-level
code counts as one of them. `ella --max-depth N` changes the limit. Every
backend has the same limit, so a program behaves the same however it is
run. (Native code has room for about two scope levels, a function body and
one loop, per nested call.)

Type errors such as `1 + true` are found before the program runs. The AST
interpreter also checks types as it goes, so it can still run programs that
haven't been checked.

### The prelude

A few library functions, and a type, are written in Ella itself, in the
modules of the *standard library* (src/ella/lib/): functions that take
other functions, or work on lists of any type. Its modules are `lists`
(the functions in the first table down to `range`), `maps` (`Map`,
`new_map`, `map_of`, `group_by` and `counts`), `sets` (`Set`, `new_set`, `set_of` and `unique`), `math` (the
constants), `strings` (the second table) and `iters` (below). Their
public names are the *prelude*: every module sees them without importing
them, unless it defines or imports the same name itself. The lazy `map`,
`filter`, `zip` and `enumerate` of `iters` share names with the eager ones
of `lists`, which are the prelude's; they are `iters.map` and so on. They can also be imported like any module
([§3](#modules-and-imports-provisional-syntax)): `import maps`, then
`maps.new_map()`.

| Name | Type | Gives |
| --- | --- | --- |
| `reverse(xs)` | `[T] ([T]) -> [T]` | the items of `xs` in the opposite order |
| `sort(xs)` | `[T: Ord] ([T]) -> [T]` | the items of `xs` from smallest to largest, by `<`; equal items keep their order |
| `sort_by(xs, key)` | `[T, K: Ord] ([T], (T) -> K) -> [T]` | the items of `xs` sorted by their keys, as `sort` sorts; `key` is called once per item, in order |
| `map(xs, f)` | `[T, U] (Iter[T], (T) -> U) -> [U]` | `f` of each item of `xs` (a list, string, range, generator, ...: [§9](#generic-functions)), called in order |
| `filter(xs, keep)` | `[T] (Iter[T], (T) -> bool) -> [T]` | the items of `xs` for which `keep` is true, called in order |
| `sum(xs)` | `[T: Add + Zero] (Iter[T]) -> T` | `T.ZERO + x0 + x1 + ...` for the items of `xs`: `0` (or `0.0`, `""`, `[]`, ...) for none ([§9](#generic-functions)) |
| `fold(xs, start, f)` | `[T, A] ([T], A, (A, T) -> A) -> A` | `f(... f(f(start, xs[0]), xs[1]) ..., xs[n - 1])`, or `start` for no items |
| `flatten(xss)` | `[T] ([[T]]) -> [T]` | the items of each list in `xss`, one list after another |
| `flat_map(xs, f)` | `[T, U] ([T], (T) -> [U]) -> [U]` | the items of `f`'s list for each item of `xs`, called in order |
| `first(xs)`, `last(xs)` | `[T] ([T]) -> Optional[T]` | the first (or last) item, if there are any |
| `index_of(xs, x)` | `[T: Eq] ([T], T) -> Optional[i64]` | where the first item `==` to `x` is |
| `min_of(xs)`, `max_of(xs)` | `[T: Ord] ([T]) -> Optional[T]` | the smallest (or largest) item by `<`, the first of equal ones; `None` for no items |
| `min_by(xs, key)`, `max_by(xs, key)` | `[T, K: Ord] ([T], (T) -> K) -> Optional[T]` | the item with the smallest (or largest) key, the first of equal ones; `key` is called once per item, in order |
| `partition(xs, test)` | `[T] ([T], (T) -> bool) -> ([T], [T])` | the items for which `test` is true, and those for which it is false, each in order |
| `chunks(xs, n)` | `[T] ([T], i64) -> [[T]]` | the items in lists of `n`, the last with what is left; `n` must be above 0 |
| `position(xs, test)` | `[T] ([T], (T) -> bool) -> Optional[i64]` | where the first item for which `test` is true is; `test` is called in order, until it is |
| `any(xs, test)`, `all(xs, test)` | `[T] ([T], (T) -> bool) -> bool` | whether `test` is true for some (or every) item; it is called in order, until the answer is known |
| `range(start, stop, step)` | `(i64, i64, i64) -> [i64]` | `start`, `start + step`, ... while before `stop` (below it, or above it if `step` is negative); `step` can't be 0 |
| `new_map()` | `[K: Eq, V] () -> Map[K, V]` | an empty `Map` (below) |
| `map_of(pairs)` | `[K: Eq, V] ([(K, V)]) -> Map[K, V]` | a `Map` of each pair's key to its value; a later pair's value replaces an earlier one's |
| `group_by(xs, key)` | `[T, K: Eq] ([T], (T) -> K) -> Map[K, [T]]` | the items of `xs` by their keys: each key's items in order, the keys in the order they first come |
| `counts(xs)` | `[T: Eq] ([T]) -> Map[T, i64]` | how many times each item comes in `xs`, the items in the order they first come |
| `new_set()` | `[T: Eq] () -> Set[T]` | an empty `Set` (below) |
| `set_of(xs)` | `[T: Eq] ([T]) -> Set[T]` | a `Set` of the items of `xs`, in the order they first appear |
| `unique(xs)` | `[T: Eq] ([T]) -> [T]` | the items of `xs`, each once (by `==`), in the order they first appear |
| `pi`, `tau`, `e` | `f64` | 3.141592653589793, 6.283185307179586 (2π) and 2.718281828459045 |
| `inf`, `nan` | `f64` | infinity (`1.0 / 0.0`) and not a number (`0.0 / 0.0`) |

They are ordinary generic functions ([§9](#generic-functions)),
so they are values too, and a type given to `sort` must be `Ord`: a
number, a string, or a struct or enum with a `compare` method. With pipes
they read left to right: `words |> filter(long) |> map(upper)`.

The module `strings` adds functions on strings to the library's. "Whitespace" is
what `trim` removes: space, `\t`, `\n`, `\r`, `\v` and `\f`.

| Name | Type | Gives |
| --- | --- | --- |
| `repeat(s, n)` | `(str, i64) -> str` | `s`, `n` times over; `""` if `n` is 0 or less |
| `lines(s)` | `(str) -> [str]` | the lines of `s`, without their `\n` or `\r\n`; a line break at the end doesn't start another line, so `lines("a\nb\n")` is `["a", "b"]` and `lines("")` is `[]` |
| `words(s)` | `(str) -> [str]` | the parts of `s` between runs of whitespace, none of them empty |
| `trim_start(s)`, `trim_end(s)` | `(str) -> str` | `s` without whitespace at its start (or end) |
| `strip_prefix(s, prefix)`, `strip_suffix(s, suffix)` | `(str, str) -> Optional[str]` | `s` without `prefix` at its start (or `suffix` at its end), if it is there |
| `split_once(s, sep)` | `(str, str) -> Optional[(str, str)]` | the parts before and after the first `sep`, if there is one; `sep` can't be empty |
| `count_of(s, part)` | `(str, str) -> i64` | how many times `part` appears in `s`, left to right, not overlapping; `part` can't be empty |
| `is_digit(s)`, `is_alpha(s)`, `is_space(s)` | `(str) -> bool` | whether `s` isn't empty, and each of its characters is an ASCII digit, an ASCII letter, or whitespace |

`Map[K, V]` is a struct ([§9](#structs-provisional-syntax)) mapping keys to
values, kept in the order the keys were first set, with protocol methods
([§9](#methods)):

```ella
let mut ages = new_map()       # a Map[str, i64], once it is used
ages["ann"] = 31               # set
ages["ann"] += 1
print(ages["ann"], len(ages))  # 32 1
print("bob" in ages)           # false
for name in ages               # the keys, in order
    print(name, ages[name])
```

* `{k: v, ...}` is a `Map` literal, and `{}` an empty `Map`; `{x, ...}` is
  a `Set` (below), and `{/}` an empty `Set`. They are `map_of([(k, v), ...])` and `set_of([x, ...])`,
  the library's whatever the program defines, so later keys replace
  earlier ones and a set keeps each item once:

  ```ella
  let mut ages = {"ann": 31, "bob": 40}
  let primes = {2, 3, 5, 7}
  let empty: Map[str, i64] = {}
  ```

* `m[k]` is the value of key `k`, and stops the program if there is none
  (`precondition failed: key in m`). `m.lookup(k)` gives an `Optional`
  instead, and `m.get_or(k, default)` gives `default` instead.
* `mut m.remove(k)` removes a key, giving its value if it had one.
* `m.keys()`, `m.values()` and `m.items()` give lists of the keys, the
  values, and `(key, value)` pairs, in order; `m.is_empty()` tells whether
  it has none.
* `m.merge(other)` has the keys of `m`, then the new ones of `other`, with
  `other`'s values where both have a key.
* `Map` has `update` ([§9](#methods)), so as a line,
  `m[k] += [x]` adds to the list in place, and `m[k].count += 1` changes
  a field of a struct in the map; the key must be there already.
* Keys are told apart by `==`, finding them by their `hash`. So `0.0` and
  `-0.0` are the same key, and a NaN key is never found (`nan != nan`):
  setting one adds a new key each time.
* A `Map` is a value like any other: it is copied, and compared with `==`
  (its order too) field by field. It prints as its literal is written
  (`{"a": 1, "b": 2}`), through its `text` method. Its fields are private: they are
  how it works inside.

`Set[T]` is a struct holding a `Map[T, ()]`: a set of values, kept in the
order they were first inserted, told apart as a `Map`'s keys are (by
`==`).

```ella
let mut seen = new_set()       # a Set[str], once it is used
print(mut seen.insert("a"))    # true: it is new
print(mut seen.insert("a"))    # false
print("a" in seen, len(seen))  # true 1
let odd = set_of([1, 3, 5, 3]) # 1, 3, 5
for n in odd                   # the items, in order
    print(n)
```

* `mut s.insert(x)` adds `x`, and `mut s.remove(x)` takes it out; each
  gives whether it changed the set. `mut s.insert_all(xs)` adds each item
  of a list, giving how many were new, and `s.is_empty()` tells whether
  it has none.
* `s.items()` gives a list of the items, in order.
* `s.union(t)` has the items of `s`, then those of `t` that aren't in `s`;
  `s.intersection(t)` and `s.difference(t)` the items of `s` that are (or
  aren't) in `t`, in `s`'s order. `s.is_subset(t)` tells whether every
  item of `s` is in `t`.
* Like a `Map`, a `Set` is copied. It has `eq` and `hash` methods: sets are
  `==` when they have the same items, in any order, so a set can be an
  item of a set. It prints as `{1, 2, 3}`, and as `{/}` when it is empty.

The module `iters` has lazy functions, for when they are wanted: on
sources without end, or to make no lists in between. Each takes anything
a `for` loop can go through (`Iter[T]`, [§9](#generic-functions)): a
list, string, range, array, generator, iterator, `Map` (its keys) or
`Set`, and gives a generator ([§9](#generators-provisional-syntax)),
which asks for items only as its own are asked for. `collect` makes a
list of the items. Its `map`, `filter`, `zip` and `enumerate` are written
`iters.map` and so on (`import iters`), since `lists` has eager ones by
those names; its other names are in the prelude:

```ella
import iters

print(count(1, 1) |> iters.filter(n => n % 2 == 1) |> take(3) |> collect)   # [1, 3, 5]
let squares = iters.map(1..1000000, n => n * n)    # a Gen[i64]: nothing computed yet
print(squares.take(2).collect())                    # [1, 4]
```

In a lazy chain, the functions run item by item: in
`iters.map(xs, f) |> iters.filter(g)`, `f` and `g` take turns. A
generator gives its items once, so a result needed more than once,
indexed or measured with `len` should be collected first; the checker's
errors say so where one is used as a list.

The eager `map`, `filter`, `zip`, `enumerate`, and `collect`, go through
every item, so on a generator without end they never finish:
`count(0, 1) |> map(f) |> take(3)` runs forever, where
`count(0, 1) |> take(3) |> map(f)` doesn't. The checker warns where it
can see that a source has no end (`count(...)`, and lazy functions of it
that no `take` or `take_while` ends).

| Name | Type | Gives |
| --- | --- | --- |
| `each(xs)` | `[T] (Iter[T]) -> Gen[T]` | the items of `xs` |
| `count(start, step)` | `[T: Num] (T, T) -> Gen[T]` | `start`, `start + step`, ... without end |
| `iters.map(xs, f)` | `[T, U] (Iter[T], (T) -> U) -> Gen[U]` | `f` of each item |
| `iters.filter(xs, keep)` | `[T] (Iter[T], (T) -> bool) -> Gen[T]` | the items for which `keep` is true |
| `take(xs, n)`, `skip(xs, n)` | `[T] (Iter[T], i64) -> Gen[T]` | the first `n` items (asking for no more); the items after them |
| `take_while(xs, keep)` | `[T] (Iter[T], (T) -> bool) -> Gen[T]` | the items before the first one for which `keep` is false |
| `chain(a, b)` | `[T] (Iter[T], Iter[T]) -> Gen[T]` | the items of `a`, then those of `b` |
| `iters.enumerate(xs)` | `[T] (Iter[T]) -> Gen[(i64, T)]` | each item with its position: `(0, first)`, `(1, second)`, ... |
| `iters.zip(a, b)` | `[T, U] (Iter[T], Iter[U]) -> Gen[(T, U)]` | pairs of the items at each position, as many as the shorter has |
| `scan(xs, start, f)` | `[T, A] (Iter[T], A, (A, T) -> A) -> Gen[A]` | `start`, `f(start, first)`, ...: each running result |
| `collect(xs)` | `[T] (Iter[T]) -> [T]` | the items, in a list |

Library modules are modules like the program's own, so the program's
names (its own `len`, or a variant `i`) don't change what their code
means. An error in their code, such as a failed precondition, gives a
location in the library module: `<std>/maps.ella:12:5`.

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
  is checked when it is run; the top-level names in it mean what they
  mean where it was quoted (below).
* A splice outside any quote is stage -1: it runs **while compiling**, and
  the code it gives becomes part of the program. A splice inside that one
  runs earlier still, making the code that runs while compiling:

  ```ella
  let cube = (x: i64) -> i64 => $(power(3, quote x))   # x * (x * (x * 1))
  ```

  What compile-time code prints comes out before the program runs.
  `ella --expanded` shows a program with its splices expanded: the
  entry file, and each other module of the program that has splices,
  in the order they run. With more than one, each comes after a comment
  line with its file (`# geo/poly.ella`). Library modules aren't shown.
* Compile-time code can call the functions of the modules its module
  imports, since they are checked (and their own splices expanded) first.
* A variable belongs to the stage it is bound in. It can be used at a later
  stage, where its value is carried into the code as a constant: with
  `let n = 3`, `quote n + 1` is `3 + 1`. Only data can be carried, not
  functions (top-level functions are available at every stage) or values
  that can't be copied or handles. Using a variable at an earlier stage
  than its own is an error: it doesn't exist yet.
* Top-level variables other than functions belong to stage 0, so
  compile-time code can't read them.
* The constants a type's `implements` gives
  ([§9](#user-defined-constraints-provisional-syntax)) are compile-time
  code too: each is computed once while compiling, and `ella --expanded`
  shows its value written in.
* Variables bound inside quoted code get fresh identities when the code is
  built, so spliced code cannot capture them accidentally. The compiler
  preserves readable source names and uses collision-free internal names
  when checking and running the generated code.
* A top-level name in quoted code means what it means in the module where
  the quote is written, even a private one, wherever the code is spliced
  or run: `quote _helper(x)` in `geo.ella` still calls `geo`'s `_helper`
  when `main.ella` splices it in.
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
  `$(Stack(type i64))` is one type wherever it is written, in any
  module. A different struct with the same name is an error.
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
program     = { import NEWLINE } { expr NEWLINE } EOF ;
import      = "import" path [ "as" IDENT ] ;
path        = IDENT { "." IDENT } ;              (* a module, or an item of one: geo.poly, geo.Shape *)
block       = NEWLINE INDENT expr { NEWLINE expr } NEWLINE DEDENT ;
body        = expr | block ;

expr        = let | assignment | lambda | if | match | for | while | loop
            | break | continue | return | yield | assert | enum | struct | constraint
            | quote | mut_call | operator_expr ;

let         = "let" binding [ type_params ] "=" { binding "=" } expr
            | "let" [ "mut" ] pattern [ ":" type ] "=" expr ;  (* destructuring: the pattern starts with "(" or IDENT "(" *)
type_params = "[" ( type_param { "," type_param } [ ";" sizes ] | ";" sizes ) "]" ;
type_param  = IDENT [ ":" path { "+" path } ] ;      (* built-in or user-defined constraints *)
sizes       = IDENT { "," IDENT } ;                  (* size parameters (§9, static shapes) *)
binding     = [ "mut" ] IDENT [ ":" type ] ;      (* type parameters: first binding only *)
assignment  = target ( "=" | "+=" | "-=" | "*=" | "/=" | "//=" | "%=" ) expr ;
target      = IDENT | operator_expr index | operator_expr "." IDENT ;

lambda      = "(" [ param { "," param } [ "," ] ] ")" [ "->" type ] "=>" lambda_body
            | IDENT "=>" body ;                  (* one parameter, of an inferred type *)
param       = "mut" IDENT ":" type | IDENT [ ":" type ]   (* types inferred (§5); `Iter[T]` only here (§9) *)
            | pattern [ ":" type ] ;             (* not a literal, range or "|" *)
lambda_body = expr
            | NEWLINE INDENT [ where NEWLINE ] expr { NEWLINE expr } NEWLINE DEDENT ;
where       = "where" block ;

if          = "if" expr "then" body [ [ NEWLINE ] "else" body ] ;
match       = "match" expr NEWLINE INDENT case { case } DEDENT ;
case        = "case" pattern [ "if" expr ] ( "=>" expr NEWLINE | block ) ;

for         = [ label ] "for" pattern "in" [ "mut" ] expr body ;
while       = [ label ] "while" expr body ;
loop        = [ label ] "loop" body ;
label       = IDENT ":" ;
break       = "break" [ IDENT ] [ expr ] ;   (* IDENT only if it names an enclosing loop *)
continue    = "continue" [ IDENT ] ;
return      = "return" [ expr ] ;
yield       = "yield" expr ;
assert      = "assert" expr ;

enum        = "enum" IDENT [ "[" type_param { "," type_param } "]" ]
              NEWLINE INDENT ( variant | method | implementation ) { NEWLINE ( variant | method | implementation ) }
              NEWLINE DEDENT ;
variant     = IDENT [ "(" type { "," type } ")" ] ;
struct      = [ "nocopy" ] "struct" IDENT [ "[" type_param { "," type_param } "]" ]
              NEWLINE INDENT ( field | method | implementation ) { NEWLINE ( field | method | implementation) }
              NEWLINE DEDENT ;
field       = IDENT ":" type ;
method      = IDENT "=" expr ;                   (* a function; `finalize` for a nocopy struct *)
implementation = "implements" path [ NEWLINE INDENT impl_item { NEWLINE impl_item } NEWLINE DEDENT ] ;
impl_item   = method | "const" IDENT "=" expr ;
constraint  = "constraint" IDENT [ ":" path { "+" path } ]     (* the constraints it requires *)
              NEWLINE INDENT requirement { NEWLINE requirement } NEWLINE DEDENT ;
requirement = IDENT ":" type | "const" IDENT ":" type ;
mut_call    = "mut" operator_expr ;              (* a method call: `mut s.push(3)` *)
quote       = "quote" body ;
splice      = "$" ( IDENT | "(" expr ")" | block ) ;

operator_expr = prefix_op operator_expr
            | operator_expr binary_op operator_expr
            | operator_expr ( call | index | member | "?" )
            | primary ;
call        = "(" [ arg { "," arg } [ "," ] ] ")" ;
arg         = [ "mut" ] expr ;                   (* `mut` only for a place, or before a method call *)
index       = "[" expr { "," expr } "]" ;           (* `a[i, j]` is `a[(i, j)]` *)
member      = "." ( IDENT | INT | "$" ( IDENT | "(" expr ")" ) ) ;  (* `.0`: a tuple's item; `.$f`: a spliced field name *)

primary     = INT | FLOAT | string | "true" | "false" | IDENT
            | "(" ")"                            (* unit *)
            | "(" expr ")"                       (* grouping *)
            | "(" expr "," [ expr { "," expr } ] [ "," ] ")"  (* tuple *)
            | "[" [ expr { "," expr } [ "," ] ] "]"  (* list *)
            | "{" [ expr ":" expr { "," expr ":" expr } [ "," ] ] "}"  (* map; `{}` is an empty one *)
            | "{" expr { "," expr } [ "," ] "}"  (* set *)
            | "{" "/" "}"                        (* an empty set *)
            | "type" type                        (* a type, as a value *)
            | splice ;
string      = STRING_START { STRING_TEXT | INTERP_START expr INTERP_END } STRING_END ;

pattern     = pattern_atom { "|" pattern_atom } ;
pattern_atom = "_" | [ "mut" ] IDENT | literal | literal ( ".." | "..=" ) literal
            | path [ "(" pattern { "," pattern } ")" ]  (* a dotted path is a variant or struct: geo.Circle(r) *)
            | "(" pattern "," [ pattern { "," pattern } ] ")" ;

type        = path [ "[" type { "," type } [ ";" dim { "," dim } ] "]" ]  (* geo.Point, p.Pair[i64], Array[f64; N, 3] *)
            | "$" ( IDENT | "(" expr ")" )      (* in a quote: a spliced type *)
            | "[" type "]"
            | "(" ")"
            | "(" [ [ "mut" ] type { "," [ "mut" ] type } ] ")" "->" type
            | "(" type "," [ type { "," type } ] ")" ;
dim         = INT | IDENT | "_" ;                (* a size, a size name, or any size *)
```

Parsing notes:

* **Lambdas vs. grouping/tuples.** `(` can start a grouping, tuple, unit, or
  lambda parameter list. Parse the parenthesized contents as expressions,
  allowing `name: type` items; if `->` or `=>` follows the `)`, convert the
  items into parameters (each must be `name: type` or `mut name: type`),
  otherwise reject any such items. No backtracking needed. (To allow
  patterns as parameters, the parser first looks ahead for the matching
  `)` followed by `->` or `=>`; if so, it parses the items as parameters
  instead. A pattern parameter becomes a hidden parameter named by its
  position (`#0` for the first), which a `let` at the start of the body
  takes apart (so does each `where` condition using its names, with a
  copy of the `let` keeping only those), and each `Iter[T]` parameter of a
  `let`'s function a hidden type parameter, likewise `Iter#0`, so later
  stages need nothing new for either.)
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
* **Imported names.** In expressions, `geo.poly.area` parses as members
  of the name `geo`; the checker reads it as a name when an import binds
  `geo.poly` (the longest path that one binds), and a local variable `geo`
  shadows it. In patterns and types, a dotted path is always a name.
* **Methods vs. fields and variants.** In a struct or enum, a line starting
  `IDENT "="` is a method; otherwise it is a field or a variant.
* **`mut` before a method call** (`mut s.items[0].push(1)`) is parsed as a
  postfix expression after `mut`, which must end with a call of a member;
  the value the method is called on becomes the `mut` argument:
  `Call(Member(MutArg(s.items[0]), "push"), [1])`. In an argument list,
  `mut` followed by such a call means the same.

## Open questions

* Which other list functions should take `Iter[T]` (`sum`, `any`, `all`,
  `fold`, ...), whether `iters` should have a lazy `flat_map`, and whether
  `Iter[T]` should be allowed in methods and results.
* Lazy adapters as copyable structs rather than generators (as Rust's
  are), so that a lazy result can be named and used twice; it needs
  `Iter[T]` (or associated types) in struct fields.

* Fixed-size SIMD vector types (`f32x4`?) beside `Array[T]`, item types
  narrower than 32 bits, slices of arrays that are views (and can be
  assigned to), and checking static shapes at run time where they can't be
  known while compiling.
* Whether a constraint should be able to require operators beyond the
  built-in constraints', associated types, and constants usable in types
  (computed at compile time).
* Whether `comptime` (still reserved) is worth having as shorthand for a
  splice, now that staging uses `quote` and `$`.
* Compile-time type parameters checked at each use, for template-style
  code that constraints can't express. See docs/memory-model.md §15.
* Generic computed types (`new_struct` makes structs without type
  parameters) and splicing types into struct definitions.
* Importing several names of a module at once (`import geo: area,
  Shape`), passing imports on (re-exports), and a way for a project to
  name its packages (a manifest) rather than the package path.
* Whether imports should be allowed to form cycles, for functions in two
  modules that call each other.
