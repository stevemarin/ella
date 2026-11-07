# Type system, generics, and protocols

This note describes the current checker model. The language details are in
[`docs/grammar.md`](../grammar.md), especially §§9–10. Implementation is
centered in [`src/ella/checker.py`](../../src/ella/checker.py), with method and
operator lowering in [`src/ella/lower.py`](../../src/ella/lower.py).

## Type representation and inference

Ella is statically typed and has no implicit numeric conversions. The
checker represents primitive, list, tuple, function, enum, and built-in
types as Python data classes. Unknown types are `TyVar` instances; unification
links them when expressions constrain one another. For example, an empty
list starts with an unknown element type and a later use can determine it.
An unresolved type variable defaults to unit in the current checker.

The syntax tree is not rewritten to hold types. Instead, the checker records
tables keyed by node identity: expression types, name resolution, pattern
kinds, generic instantiations, methods, and stage/type metadata. Lowering
consumes these tables. Editor analysis uses the same checker in a recovery
mode that accumulates errors rather than stopping at the first one.

Top-level functions and methods declare complete signatures so their names
can be mutually referenced before bodies are checked. Local lambdas can
infer parameter and result types from context, but a lambda has one fixed
type. Generic functions are the mechanism for using one definition at
multiple concrete types.

## Generic functions and constraints

Generic functions declare type parameters and optional constraints. A
constraint describes the operations permitted on a parameter type. Built-in
constraints include `Eq`, `Ord`, `Add`, and `Num`; implication is encoded
centrally (`Ord` includes equality, while `Num` includes arithmetic and
ordering capabilities). At a generic call, the checker infers or reads
concrete type arguments, verifies the constraints, and records the
instantiation for lowering.

User-defined constraints declare required methods with a `Self` type, and
can require other constraints (`constraint Ranked: Named + Ord`) and
typed constants (`const EMPTY: Self`). A struct or enum meets a
user-defined constraint only by declaring `implements Shape` (with a block
of methods, or after defining them); having matching methods is not enough.
The checker verifies the required methods and signatures at the
declaration, and a type implements each user-defined constraint once.
Built-in constraints are still met as their table says. Generic code reads
a constant as `T.NAME`; each implementation's value is computed once at
compile time (`staging.Expander.write_constant`) and written into the code
as a literal, or a global made before the program runs. A struct's own type
parameters can carry such constraints (`struct Acc[T: Monoid]`), so its
methods see `T.EMPTY` too. Dispatch stays static: specialized functions
call concrete methods, and the backend needs no runtime dictionaries or
virtual dispatch. A generic function can be bound locally with `let`; it may capture
local values, and is specialized independently at each use. This is a
generic function binding, not an anonymous polymorphic function value.

Specialization/monomorphization means each concrete instantiation can produce
its own IR function. This simplifies runtime representation and optimization,
at the cost of compile time and generated code size. When changing
specialization, preserve identity between a generic use, its concrete types,
and the corresponding IR function. Types created by staging can participate
in the same checker machinery.

## Methods, operators, and protocols

Methods are associated with struct or enum types. A function whose first
parameter is the owning type is an instance method; one without that
receiver is a type-namespace function such as `Stack.new()`. Instance
method calls resolve from the receiver's checked type and are recorded in
checker tables for lowering. Operator methods provide defined behavior for
user types (for example, `add` for `+` and `compare` for ordering).
Compatible receiver-first prelude functions can also be called as
`value.function(args)`; these lower to ordinary calls with the value
prepended. The checker validates method signatures and operator
requirements before lowering.

Protocols such as `get`, `set`, `iter`, `next`, `contains`, and `len` are
also structural conventions: syntax or built-ins select the corresponding
method when the type provides it. This lets user-defined types work with
indexing, iteration, containment, and length without a runtime protocol
object. Be careful to keep the checker selection, lowering operation, and
runtime/builtin path synchronized when adding a protocol operation.

## Enums, structs, and exhaustiveness

Enums are algebraic data types with named variants and typed fields. A
struct is represented in the checker similarly to a one-variant enum, with
field names and methods layered on. Pattern checking resolves constructors
and fields, and exhaustiveness analysis reports missing or redundant match
cases. Match is an expression, so each arm must produce compatible types.

Visibility applies to fields and methods as well as top-level names. A
private field restricts construction and destructuring to its module and
descendant modules. These rules are checked statically; the runtime object
does not carry access-control metadata.

## Mutation and non-copyable types

Bindings are immutable unless marked `mut`. Mutable parameters explicitly
authorize value-result updates at a call site and are part of the function
type. The checker enforces exclusivity by tracking names/places used by
mutable arguments; some disjoint projections can be rejected because the
analysis is conservative.

`nocopy` types integrate the type checker with the memory model. Such values
cannot be duplicated and must be moved through permitted contexts. Generators
and resource-owning structs are examples. The checker prevents many invalid
copies and lifetime escapes before lowering; the detailed runtime ownership
rules are in [memory_model_summary.md](memory_model_summary.md).

## Extending the checker safely

When adding a type form or capability, update all relevant layers: type data
structures and display/substitution, unification, well-formedness and
constraint checks, expression and pattern checking, generic specialization,
lowering, IR type verification, and tests for all execution paths. Keep
diagnostics tied to source spans. Be especially cautious where a new feature
affects both type equality and runtime representation.

Array types may carry static shapes (`Array[f64; N, 3]`, size parameters
`[; N]`): sizes are `SizeTy`s or type variables inside the `Array`
`BuiltinTy`, unified like types and erased by `finish` before lowering
(docs/grammar.md §9, "Static shapes").

Known limits include no anonymous polymorphic function values, conservative
name-based exclusivity, limitations in exhaustiveness reasoning over integer
ranges, no operators or associated types in user-defined constraints,
constants that can't be used in types, and no run-time checks of static
shapes yet. Consult [TODO.md](../../TODO.md) (or the detailed
[TODO-Claude.md](../../TODO-Claude.md)) and the grammar's open questions
for current status.
