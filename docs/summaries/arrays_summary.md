# Arrays and SIMD

Ella arrays are multidimensional, homogeneous numeric values with a flat
row-major buffer and run-time shape. Their source syntax and built-ins are
described in [`docs/grammar.md`](docs/grammar.md) §9. The key implementation
areas are `ArrayValue` and numerical operations in
[`src/ella/library.py`](src/ella/library.py), array lowering in
[`src/ella/lower.py`](src/ella/lower.py), fusion in
[`src/ella/passes/fuse.py`](src/ella/passes/fuse.py), and native loops in
[`src/ella/codegen.py`](src/ella/codegen.py) and
[`src/ella/runtime.c`](src/ella/runtime.c).

## Representation and shape

An `Array[T]` contains a flat sequence of `i64`, `i32`, `f64`, or `f32`
items plus a dimension count and shape. Logical multidimensional indexing is
converted to a row-major offset. Rank and dimensions are run-time data; the
type parameter identifies only the item type. Constructors such as `array`,
`fill`, and `reshape` create or reinterpret shape while preserving the item
count rules documented in the grammar.

Array slicing materializes a new array. Unlike list/string slices, it does
not use a backing-storage view. Arrays otherwise follow Ella's value
semantics: copies are independent and updates through a `mut` place are
in-place only where lowering has established the correct ownership.

## Arithmetic and broadcasting

Arithmetic on arrays is element-wise. Two arrays must have compatible
shapes; a scalar number of the item type can be broadcast across the array.
Different array shapes use NumPy-style trailing-dimension broadcasting: each
aligned dimension must be equal or one, with missing leading dimensions
treated as one. Shape incompatibility and invalid indexes are runtime errors.

Integer array arithmetic is checked. Overflow is reported at the first item
in row-major result order that overflows. Equality compares both shape and
items; arrays do not have ordering. Reductions such as `total`, `dot`, and
`matmul` have their own documented accumulation order and overflow behavior.

## Native SIMD and fusion

The native backend uses AVX2 loops for supported operations and item types.
The exact coverage is operation- and type-dependent; consult `codegen.py`
before assuming a given expression vectorizes. `i64` multiplication and
some `i32` operations use non-fused/plain paths because their checked
semantics or instruction support differ.

The `fuse` optimization combines adjacent array arithmetic into a single
element loop. Intermediate arrays that are only consumed by the next step
are never materialized. Some intermediate arrays may be retained when they
also have other uses. Supported reductions can terminate a fused chain, so
the loop can accumulate a scalar without constructing the final array.
Fusion is an optimization: the unfused IR remains the semantic reference.

Integer behavior and errors must match item-by-item semantics. Floating-point
array results can differ in low bits across backends because native SIMD and
reductions may use different evaluation/accumulation orders or fused
multiply-add. Native compilation currently disables implicit C FMA
contraction for the runtime, but generated AVX operations and reduction
order still have specified tolerances/behavior in the language reference.
Avoid promising bitwise-identical floating reductions unless their operation
order is explicitly fixed.

## Contributor checklist

When adding an array operation, keep these paths aligned:

1. Checker rules for accepted item types and operand forms.
2. AST interpreter/library semantics, including shape checks and overflow.
3. IR operation typing and verifier rules.
4. IR interpreter semantics and memory accounting.
5. Optimizer fusion eligibility and fused instruction representation.
6. Native scalar/SIMD code generation and runtime fallback.

Test rank-zero/empty dimensions where allowed, incompatible shapes, scalar
broadcasting, slices, index boundaries, integer overflow order, and float
edge cases. Compare all backends; for fused code, also compare optimized and
unoptimized IR. SIMD alignment and runtime object layout are implementation
details, not visible array semantics.

