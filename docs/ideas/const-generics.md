# Const generics, sized types, and staged computation

**Status:** design exploration, not a feature specification or implementation
plan. This memo collects the discussion of const-generic-style types, their
relationship to staging and dependent types, possible benefits for Ella, and
relevant language designs and research. It does not propose that Ella adopt
any particular syntax.

## 1. The idea

A const-generic type is parameterized by a compile-time value as well as by
types. For example, `Vector[i64, 4]` could represent a vector whose length is
four. A function can express a relationship between lengths:

```text
# Hypothetical syntax, not valid Ella today.
struct Vector[T, const N]
    items: [T; N]

let append[T, const N] =
    (v: Vector[T, N], x: T) -> Vector[T, N + 1] => ...

let dot[T, const N] =
    (a: Vector[T, N], b: Vector[T, N]) -> T => ...
```

If a call supplies a `Vector[i64, 4]` to `append`, substitution and evaluation
of `N + 1` give the result type `Vector[i64, 5]`. `dot` requires the same `N`
on both arguments, so a call with lengths four and five is rejected during
type checking.

The central guarantee is conditional: when sizes are known statically and
the type signatures express the relevant relationship, a well-typed program
cannot make that shape-mismatched call. It does not remove checks for data
whose size is only known at runtime.

## 2. Is this dependent typing?

In the broad type-theoretic sense, a type indexed by a value depends on that
value; const generics are therefore a restricted form of value dependency.
They are not unrestricted dependent types. A typical const-generic system
limits indices to compile-time constants of selected types and permits only
specified expressions in type positions. It does not let arbitrary runtime
values determine types.

Another nearby design is a **refinement type**, which adds a logical
predicate to a type, such as “the length is `N + 1`.” Refinements and
size-indexed types can involve arithmetic solvers and are more expressive
than simply substituting a concrete const argument. They also bring more
complexity in deciding which facts can be proved.

For Ella, it is useful to distinguish:

1. **Concrete const evaluation:** given `N = 4`, compute `N + 1 = 5`.
2. **Symbolic arithmetic reasoning:** prove facts such as
   `M = N + 1` or `N < M` when `N` and `M` are still generic.
3. **General dependent typing:** let types depend on arbitrary values and
   potentially require general proofs.

The first is a plausible small feature. The second requires a deliberately
chosen arithmetic theory and solver strategy. The third is well beyond what
const generics require.

## 3. What const generics would buy Ella

### Static shape checking

An API can require equal dimensions, and the compiler can reject a known
mismatch before execution. This is the main safety benefit for fixed-size
vectors, matrices, and similar data structures.

### Types that describe results

An operation can state how its result size relates to its inputs. For
example, appending one element to a length-`N` vector gives length `N + 1`.
Such relationships can make generic APIs more precise without requiring a
separate implementation for each size.

### Layout and specialization

A fixed size can inform representation and code generation. Knowing `N` may
enable bounds-check elimination, loop unrolling, or other specialization.
Those are possible optimizations, not guaranteed benefits: code specialization
can also increase compile time and binary size.

### Limits

The information must be static for the static guarantee to apply. If the
length comes from input or a runtime computation, the program needs a
runtime-sized representation and may need runtime checks.

An ordinary runtime loop also cannot change one variable's static type on each
iteration. In a hypothetical fixed-size-vector API, `append` returns a new
type at every step. A three-step sequence can be written as a chain of
type-changing calls, but a conventional loop body has one type on every
iteration. A staged loop could generate an unrolled chain only if the
iteration count is known while compiling and the language has a way to build
that sequence of differently typed expressions.

Likewise, an in-place append cannot make a value's compile-time length change
as it runs. A resizable vector with a runtime length is a better fit for
ordinary mutation. A fixed-capacity vector could mutate its contents while
keeping capacity in its type, but that type would not prove the current
logical length.

Finally, indexing a type by `N` only proves that the stored data really has
length `N` if the representation and constructors preserve that invariant.
A field of ordinary type `[T]` does not itself enforce a fixed length.

## 4. Staging and possible implementation

Ella already has compile-time splices, types as values, reflection, computed
types, and generic specialization. A compile-time evaluator could help
evaluate expressions such as `N + 1`; however, current staging does not
automatically provide const-generic types. Ella's generic parameters are
currently types, `new_struct` creates non-generic computed structs, and
types cannot yet be spliced into struct definitions. See [the staging
reference](../grammar.md#11-stages), [generic
functions](../grammar.md#generic-functions), and [computed
types](../grammar.md#computed-types).

A conservative design could proceed as follows:

1. **Add const parameters to generic definitions.** Initially support a
   bounded integer type, with explicit distinction from runtime parameters.
2. **Allow a small expression grammar in type positions.** Start with
   literals, const parameter names, and checked `+`, `-`, and perhaps `*`
   with one constant operand. Avoid arbitrary compile-time Ella programs in
   type expressions at first.
3. **Evaluate after substitution.** When a generic is specialized with
   concrete const arguments, evaluate its type expressions, construct the
   resulting ordinary types, and run the existing checker and compiler on
   that specialization.
4. **Canonicalize type identity.** Decide whether equivalent expressions
   such as `N + 1` and `1 + N` denote the same type when `N` is known, and
   ensure caches use the resolved const values and types.
5. **Specify arithmetic failures.** Overflow, negative sizes, division by
   zero, and unreasonable allocation sizes need explicit compile-time
   diagnostics and consistent integer semantics.
6. **Bound work and specialization growth.** Cache by the full concrete
   specialization key, and report when compile-time evaluation or code
   generation exceeds a defined budget.

This approach reuses staging as an evaluator where useful, but keeps a small
type-level expression language with predictable behavior. Allowing arbitrary
staged programs to generate types would be more flexible, but harder to
bound, cache, diagnose, and reason about. A later extension could support
symbolic constraints if concrete substitution proves insufficient.

## 5. Runtime checks and a JIT

A JIT can inspect runtime lengths, guard assumptions, and compile code
specialized for observed sizes. This can improve performance and detect a
mismatch when the operation executes. It does not by itself make a source
program statically well-typed with respect to those runtime values.

The distinction is:

| Mechanism | What it can guarantee |
| --- | --- |
| Runtime shape check | A mismatch is detected when the checked operation runs. |
| JIT specialization | Code can be optimized for observed values, usually with guards or fallback paths. |
| Const-generic static checking | A mismatch between statically known, type-indexed sizes is rejected before the program runs. |

A JIT can complement const generics for dynamically sized values, but does
not replace their compile-time API guarantees.

## 6. Complexity and design boundary

The simplest useful version is concrete substitution plus a small checked
integer evaluator. The difficult boundary is symbolic arithmetic: if a
generic function needs the compiler to prove `M = N + 1`, merely evaluating
expressions after concrete arguments are supplied may not be enough. Richer
constraints require deciding which arithmetic is supported, what is
decidable, how proofs or failures are reported, and whether type equality is
structural or normalized.

This suggests a staged development path:

1. Prototype a few fixed-size APIs and verify that static shape mismatch
   rejection is valuable in Ella.
2. Implement const parameters and concrete arithmetic substitution only.
3. Add a narrowly scoped symbolic arithmetic fragment only if real examples
   cannot be expressed without it.
4. Consider refinement or more general dependent-type ideas only if the
   narrower system demonstrably blocks important use cases.

The feature is therefore not required to make Ella's current arrays work.
It would introduce a new type-level capability, with meaningful costs in
checker complexity, generic specialization, diagnostics, compile time, and
potential binary size. For a learning language, the simplest useful subset
may be preferable to matching a larger language's full feature set.

## 7. Research and language references

The research and documentation below are relevant designs, not all recent
papers specifically proposing extensions to const generics. A quick literature
search found more directly applicable implementation and design discussion
around Rust's generic constant expressions, alongside foundational work on
size-indexed and refinement types. It did not establish a large, recent
research literature devoted specifically to extending Rust-style const
generics.

### Languages and implementation work

* **Rust Reference, “Generic parameters / Const generics.”** Documents const
  parameters, permitted types and uses, and current restrictions on
  expressions in type positions:
  <https://doc.rust-lang.org/reference/items/generics.html#const-generics>
* **Rust Unstable Book, `generic_const_exprs`.** Describes the experimental
  feature that permits nontrivial generic constants in types, with examples
  such as `N + 1`:
  <https://doc.rust-lang.org/unstable-book/language-features/generic-const-exprs.html>
* **Rust issue #76560, “generic_const_exprs.”** Tracks implementation and
  design questions. The issue notes that stable const arguments are
  restricted to concrete expressions or bare const parameters, while the
  feature aims to allow expressions such as `bar(N + 2)`:
  <https://github.com/rust-lang/rust/issues/76560>
* **Rust Reference, “Array types.”** Defines fixed-size arrays `[T; N]` and
  says the length is a constant expression:
  <https://doc.rust-lang.org/reference/types/array.html>
* **C++ constant (non-type) template parameters.** A mature example of
  templates parameterized by values; class and function templates are
  instantiated with those values:
  <https://en.cppreference.com/w/cpp/language/template_parameters.html>
* **Zig language reference, “comptime.”** Documents compile-time execution,
  a different route to specializing declarations and types using values
  known to the compiler:
  <https://ziglang.org/documentation/master/#comptime>

### Research on restricted dependent and refinement types

* Hongwei Xi and Frank Pfenning, **“Dependent Types in Practical
  Programming,”** POPL 1999, pp. 214–227,
  <https://doi.org/10.1145/292540.292560>. The DML approach uses restricted
  value indices and constraints for practical programming. It is foundational
  rather than recent, and is a limited dependent-type design—not evidence
  that all const generics require a general dependent type system.
* Niki Vazou, Eric L. Seidel, Ranjit Jhala, Dimitrios Vytiniotis, and Simon
  Peyton Jones, **“Refinement Types for Haskell,”** ICFP 2014,
  <https://doi.org/10.1145/2628136.2628161>. Liquid Haskell uses refinement
  predicates and automated checking to express and verify logical facts about
  values. It is a richer neighboring approach, not a direct const-generics
  implementation.

The Rust documentation and tracker are particularly useful for Ella's
design because they expose the boundary between easy concrete evaluation and
harder symbolic generic expressions. DML and refinement types are useful if
Ella later considers arithmetic relationships that cannot be discharged by
substitution alone.
