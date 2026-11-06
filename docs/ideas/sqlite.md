# SQLite integration for Ella

**Status:** design exploration, not an implementation plan. This memo collects
the current design questions for making SQLite feel like a natural part of
Ella: queries should be syntax checked, type checked where possible, runnable
through Ella's backends, and return ordinary Ella values.

The goal is more than calling a C library. It is to let Ella understand enough
about database schemas, SQL expressions, and result shapes to check and generate
the bridge code, while leaving SQLite responsible for SQL parsing and execution.

## 1. Goals and boundaries

The desired experience includes:

* Declare or associate Ella types with tables.
* Write SQL in Ella and get useful syntax and type diagnostics before running
  the program, when the schema is known.
* Bind Ella values safely as query parameters.
* Return rows as ordinary, statically typed Ella values.
* Use Ella methods in queries when their bodies can be translated to SQL, and
  use the same domain types and business rules in ordinary Ella code.
* Support joins and projections without forcing every result to be a table
  type.
* Keep database access explicit enough that a query's side effects and runtime
  execution are apparent.

These are related but distinct features:

1. **Database access** opens connections, prepares statements, binds values,
   steps through results, and closes resources.
2. **Schema mapping** relates Ella types and fields to database tables and
   columns.
3. **SQL checking and translation** checks embedded SQL against a schema,
   maps query result columns to Ella types, and translates eligible Ella
   expressions into SQL.
4. **Code generation** can generate binders, row decoders, and DDL from Ella
   types and schema metadata.

An FFI is only one possible implementation of the first part. It does not by
itself provide schema mapping, query checking, or translation.

## 2. Schema information and compile-time checking

SQLite should parse and prepare SQL; Ella should not grow a second SQL parser
just to check queries. For a constant query and a known schema, a checker can
ask SQLite to prepare the statement and inspect its parameters and result
columns. This can catch SQL syntax errors and report such facts as parameter
count and column names.

There are two plausible sources of schema information:

* **Ella declarations or migrations.** These can be deterministic compile-time
  inputs. They can drive static checking and generate DDL or mapping code.
* **An existing SQLite database.** SQLite can inspect its schema and prepare
  queries against it. The database file must be available to the checker, and
  its schema becomes a build input. The database used at runtime may differ
  from the one checked.

Prefer an Ella schema or migration definition as the reproducible source of
truth. Optionally compare it with a real database during development,
deployment, or an explicit validation step. Do not silently run migrations or
mutate a database as a side effect of compiling Ella code.

SQLite's dynamic typing limits what compile-time checking can prove. Declared
column types do not guarantee the storage class of every value. Expressions,
aggregates, `CASE`, nullability, and joins can make result types uncertain.
Compile-time checks can validate query structure and known result shape, but
decoding may still need runtime checks and errors.

Staging can help generate Ella syntax and types from known metadata, using
reflection much as the staged derive example does. It does not parse SQL, open
arbitrary runtime connections at compile time, or make runtime database state
available to the type checker. Runtime data and compile-time schema metadata
must remain separate.

## 3. Mapping structs to tables

The motivating sketch associates a normal Ella struct with a table:

```ella
let AppDB: SqliteDB = ...

struct User
    context sql => SqliteTable in AppDB
    id: i64
    name: str
    age: i64

    is_adult = (u: User) -> bool => u.age >= 18
```

This is illustrative syntax, not current Ella syntax. `@` is not currently a
token, and Ella uses `i64`, not `int`. The important idea is that `User`
remains an ordinary value type. The declaration adds a database
representation; it does not make `User` itself a database connection or
statement.

The association needs compile-time metadata: table name, database/schema
identity, column mappings, and any nullability or key information. The
connection itself is normally a runtime resource. If `AppDB` is an ordinary
runtime value, the compiler cannot assume it can inspect that connection
during type checking. A schema identity or schema declaration may need to be
separate from the connection value.

Generating adapters from a struct and its schema is a natural use of staging:

* Encode fields as insert/update parameters.
* Decode result columns into struct fields.
* Generate table metadata and optionally `CREATE TABLE` statements.
* Check field names and compatible types against a declared or inspected
  schema.

The generated result need not always be the table's struct. A projection,
aggregate, alias, or join has its own result shape. That shape can be inferred
when possible, represented as a tuple, or named explicitly by the programmer.

## 4. Contexts, constraints, and method resolution

Ella's existing user-defined constraints describe method capabilities:
structural conformance lets generic code call the required methods, and
specialization makes those calls direct. See
[grammar.md's user-defined constraints](../grammar.md#user-defined-constraints-provisional-syntax)
and the `Lookup` example in
[protocols.ella](../../test/programs/protocols.ella).

A database integration benefits from separating three concepts:

* A **constraint** says what operations or representation capabilities a type
  provides.
* A **context** selects an interpretation or set of implementations, such as
  how a `User` value maps to a particular SQL schema.
* A **query expression** builds a typed SQL/relational expression and runs it
  against a database.

A constraint like this is a useful starting point for generic row operations,
but is only illustrative:

```ella
constraint SqlRow
    encode: (Self) -> [SqlValue]
    decode: [SqlValue] -> Result[Self, DbError]
```

The more ambitious proposal is a context constraint that describes SQL
operations for a result type:

```ella
constraint SQL[T]
    select: PreparedStatement -> T
    insert: [T] -> None
    ...

struct User[SQL]
    context sql => SqliteTable in AppDB
    id: i64
    name: str
    age: i64

    context SQL
        select = PreparedStatement -> User => ...
        insert = User -> None => ...

    is_adult = (u: User) -> bool => u.age >= 18

with SQL:
    SELECT * FROM User WHERE User.is_adult()
```

This is the proposal being explored, not valid Ella syntax. It captures the
intent that `User` remains an ordinary type, has a SQL table mapping, and can
have implementations selected in SQL context. In current Ella, `None` is
the empty `Optional` variant, not the unit type (written `()`); methods are
function-valued definitions. The sketch is intentionally using shorthand.

The proposed `constraint SQL[T]` with `select: PreparedStatement -> T` is not
directly expressible by today's constraint rules. A query's result type
depends on its selected columns and expressions, rather than only on a
prepared-statement input and one fixed `T`. Table metadata also depends on a
schema/database identity. Supporting parameterized constraints, associated
types, or context-specific implementations would be a language design
extension; it should not be assumed to be a small syntax-only change.

The proposed form:

```ella
struct User[SQL]
```

would also need careful interpretation. In existing Ella syntax, brackets on a
struct declare generic type parameters, while constraints restrict those
parameters. It does not currently mean that the struct implements a
constraint. An explicit conformance form or context declaration would be
clearer than overloading generic parameters for that meaning.

### Context-scoped implementations

A lexical context could select SQL-specific methods while leaving ordinary
methods available outside it. Conceptually:

```ella
struct User
    id: i64
    name: str
    age: i64

    context sql
        is_adult = (u: User) -> SqlExpr[bool] => u.age >= 18
```

An expression such as `with SQL: SELECT ...` could enter that translation
context for the query. A call like `User.is_adult()` would then need to resolve
to a SQL expression builder, not execute an ordinary Ella function. This too
is illustrative; the compiler would need rules for:

* How a context is introduced and exited, including nested contexts.
* Which methods are visible and how ambiguity with ordinary methods is
  diagnosed.
* Whether a context implementation is selected lexically, by an explicit
  qualifier, or by both.
* How imported modules and generic code discover the relevant context.
* Whether context selection is static metadata or a runtime value.

This is not just Python-style context-manager behavior. `__enter__` and
`__exit__` can manage runtime resources, but do not change static method lookup
or translate Ella expressions into SQL. A SQL context needs compiler-visible
meaning. It can select a translation environment and target schema, while
the database connection remains a runtime resource.

### Reusing methods in SQL

Using `User.is_adult()` in ordinary code and in a SQL query is attractive, but
not every Ella method can be translated. A method that reads fields and uses
supported comparisons may map to a SQL expression. A method that prints,
mutates a list, performs arbitrary I/O, or calls an unsupported function
generally cannot.

Two approaches are possible:

1. Define an explicit SQL-context implementation.
2. Permit ordinary methods in SQL when their bodies belong to a restricted,
   compiler-checked, SQL-translatable subset.

The second can avoid duplicated business logic, but the subset and its
translation rules must be explicit. A SQL-context method should construct a
SQL expression; it should not execute arbitrary Ella code or perform an
implicit database query merely because a query is in scope. Unsupported
operations should produce a compile-time diagnostic.

## 5. Query expressions and result types

A dedicated SQL expression or block is a clearer first design than introducing
SQL keywords as general Ella grammar. For example:

```ella
let adults: [User] =
    sql AppDB
        SELECT * FROM User WHERE age >= ?
    with (18,)
```

This is illustrative, not proposed final grammar. It keeps SQL text and
parameter values separate. Bound parameters should be the normal way to pass
values; interpolating values into SQL text risks quoting errors and injection.
The compiler can check constant SQL against the associated schema. A runtime
assembled statement can only be fully prepared and checked at runtime.

A typed relational DSL could provide stronger static checking and more
reliable Ella-expression translation than raw SQL, but it is a separate
surface/API choice. Raw SQL with SQLite-backed prepare/describe checks is a
smaller initial feature. The two could coexist later.

### Joins and projections

A join selecting one row from each table naturally returns a tuple:

```ella
let pairs: [(User, Car)] =
    sql AppDB
        SELECT User.*, Car.*
        FROM User JOIN Car ON User.id = Car.owner_id
```

An inner join can therefore produce `(User, Car)`. A self-join needs aliases
to distinguish the two table instances. An outer join must represent a
missing side, for example `(User, Optional[Car])`. Projections and aggregates
should produce result types matching the selected expressions, rather than
being forced into a table's struct. Options include inferred tuples, generated
anonymous row shapes, or explicitly named result structs.

`SELECT *` is convenient but ties the result shape to the schema. Explicit
columns are easier to check and less likely to silently change when a table
changes. Either behavior needs a documented policy.

## 6. Runtime boundary and value conversion

For an initial native implementation, a narrow C shim calling SQLite is
likely simpler than exposing general C FFI. Ella's native executables already
link generated code with a C runtime through GCC; the shim could expose a
small, controlled set of operations. This is native-backend support, not
automatic support in the AST and IR interpreters.

If programs must run across all backends, an interpreter adapter can use
Python's `sqlite3` module, but its errors, conversions, and resource behavior
must match the native implementation. Alternatively, the language can
document SQLite as native-only and report a clear unsupported-operation error
in interpreter backends. It should not silently emulate calls with different
semantics.

The Ella-visible boundary should use typed values, not SQLite pointers:

* Bind supported Ella scalars (integers, floats, booleans, strings, and
  optional/null values) using SQLite's binding APIs.
* Represent returned dynamic values with a `SqlValue`-like enum, or decode
  directly into a declared result type with runtime conversion checks.
* Return explicit database and conversion errors, for example through
  `Result[T, DbError]`.
* Decide whether blobs are supported. Ella would need a byte-string type or
  another explicit representation; text should not be used as an implicit
  byte container.
* Keep database and statement handles opaque. Give them explicit close or
  ownership semantics; non-copyable wrappers and finalizers may help.

Initially, copy text/blob data across the boundary rather than letting SQLite
retain pointers into Ella-managed memory. Ella frees arena memory by scope,
and the memory model already identifies pinning and retained pointers as an
open FFI question. Any future zero-copy or retained-buffer API needs explicit
ownership, lifetime, and pinning rules.

For query results, eager collection into `[T]` is the simplest first API.
Streaming rows can follow, but a row iterator then owns or borrows a live
statement and connection. Its lifetime, early termination, and finalization
behavior must be specified.

## 7. Staged implementation path

The following sequence separates useful database access from the larger
language-design questions:

1. **Agree on schema and type mapping.** Define supported column types,
   nullability, names, and conversion/error rules. Choose whether Ella
   declarations/migrations or an existing database is the schema source of
   truth.
2. **Add a narrow runtime API.** Implement connection and statement ownership,
   parameter binding, stepping, and error conversion behind a C shim. Decide
   whether interpreters get a Python adapter or report native-only support.
3. **Add explicit query syntax.** Start with constant SQL, bound parameters,
   and eager results. Keep SQL delimited in a way that gives Ella's parser
   clear boundaries.
4. **Add result mapping.** Generate binders and decoders for declared structs
   and tuples, with runtime validation where SQLite's dynamic typing prevents
   static guarantees.
5. **Add schema-backed checking.** Prepare constant queries against declared
   migrations/schema or an explicitly configured database. Check syntax,
   parameter count, result column names, and known type compatibility.
6. **Add struct/table generation.** Generate table metadata and DDL from Ella
   types, keeping migration execution explicit.
7. **Explore SQL contexts and method translation.** First support a small,
   well-defined set of pure field-based expressions. Add broader context
   implementations only when resolution, imports, and diagnostics are clear.
8. **Expand query shapes.** Add aliases, joins, projections, outer-join
   optionals, aggregates, and streaming once the result and resource models
   are specified.

The main design risk is not calling SQLite. It is giving static meaning to
schema-dependent queries and context-sensitive method calls without making
ordinary Ella methods unpredictably effectful. Keeping schema metadata,
compile-time translation, runtime database resources, and row conversion as
separate concepts makes that boundary easier to explain and implement.
