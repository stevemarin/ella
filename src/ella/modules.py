"""Modules and imports: a program's files, linked into one program (docs/grammar.md §3)."""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import cache, partial
from pathlib import Path

from ella.checker import (
    BUILTIN_ENUMS,
    BUILTIN_TYPES,
    BUILTINS,
    CONSTRAINTS,
    LAZY,
    PRIMITIVE_NAMES,
    REFLECTION,
)
from ella.lexer import STD, EllaError, Span
from ella.parser import parse
from ella.syntax import (
    Assign,
    Binding,
    Block,
    Call,
    ConstraintDef,
    EnumDef,
    Expr,
    For,
    Implementation,
    Import,
    Index,
    Lambda,
    Let,
    LetPattern,
    ListLit,
    MapLit,
    Match,
    Member,
    Method,
    MutArg,
    Name,
    NamedType,
    NamePattern,
    OrPattern,
    Param,
    Pattern,
    Program,
    Quote,
    SetLit,
    TupleLit,
    TuplePattern,
    TypeExpr,
    UnitLit,
    Variant,
    VariantPattern,
    bare_name,
    rebuild,
    rename_sizes,
    size_names,
)

LIBRARY_DIR = Path(__file__).parent / "lib"

# The names a module can use without defining or importing them.
BUILTIN_NAMES = frozenset({*BUILTINS, *REFLECTION}) | {v for e in BUILTIN_ENUMS.values() for v in e.variants}
BUILTIN_TYPE_NAMES = frozenset({*PRIMITIVE_NAMES, *BUILTIN_TYPES, *BUILTIN_ENUMS, "Iter"})

# The environment variable with the package path: directories separated by os.pathsep (':').
PACKAGE_PATH = "ELLA_PATH"

# Reads a module's file: its text, or None if there is no such file.
type Reader = Callable[[Path], str | None]


def read_file(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.is_file() else None


def package_path(given: Sequence[Path] = ()) -> tuple[Path, ...]:
    """The directories packages are found in: those given (by `--path`), then those of ELLA_PATH."""
    found = os.environ.get(PACKAGE_PATH, "").split(os.pathsep)
    return (*given, *(Path(directory) for directory in found if directory))


def find_root(
    path: Path, imports: tuple[Import, ...], top: Path, read: Reader = read_file, packages: Sequence[Path] = ()
) -> Path:
    """The root of the program a file is part of, for editors, which only know the workspace folder (`top`): of
    the directories from the file's up to `top`, the one its imports are found from (the most of them, the
    nearest if they tie). An import is found if its module is, or the module its item is from, there or in
    a package."""

    def found(root: Path, line: Import) -> bool:
        paths = [line.path, line.path[:-1]] if len(line.path) > 1 else [line.path]
        files = (place.joinpath(*p).with_suffix(".ella") for place in (root, *packages) for p in paths)
        return any(read(file) is not None for file in files) or library_module(line.path[0]) is not None

    places = [path.parent, *path.parent.parents]
    places = places[: places.index(top) + 1] if top in places else places[:1]
    return max(places, key=lambda root: (sum(found(root, line) for line in imports), -places.index(root)))


@dataclass(frozen=True, slots=True)
class Item:
    """A top-level name of a module: its name in the linked program, and what it is.

    `kind` is "function", "variable", "enum", "struct", "variant" or
    "constraint". `module` and `file` are its module's name and file. An
    enum also has its variants' names, and a struct is `sealed` if it has
    a private field.
    """

    name: str
    kind: str
    module: str
    file: str
    variants: tuple[str, ...] = ()
    sealed: bool = False


@dataclass(slots=True)
class Module:
    """A module being linked: its file, its top-level names, and what its imports bind.

    `bindings` has the names (paths like `geo.poly`, or aliases) that its
    import lines bind, to a module or an item, and `uses` which import line
    each came from, to find imports that are never used.
    """

    name: str
    file: str
    program: Program
    # Put in front of its top-level names: "geo." in geo.ella, "" in the entry file, and "./lists."
    # in a module of the program named like a standard library module, which may be used too.
    prefix: str
    entry: bool = False
    top: dict[str, Item] = field(default_factory=dict)
    bindings: dict[str, Module | Item] = field(default_factory=dict)
    uses: dict[str, Import] = field(default_factory=dict)
    used: set[int] = field(default_factory=set)  # import lines that were used, by id
    body: tuple[Expr, ...] = ()  # its code, linked


def link(program: Program, path: Path | None = None, packages: Sequence[Path] | None = None) -> Program:
    """A program (the entry file, at `path` if it has one) linked with the modules it uses. Raises EllaError.

    Its imports are found from the file's directory, then from the package path (`packages`, by default
    `package_path()`). Linking a linked program changes nothing.
    """
    if program.modules:
        return program
    return Linker(path, packages=packages).link(program)


def module_name(file: str) -> str:
    """The name of a module, from its file as in spans: `geo/poly.ella` is `geo.poly`."""
    return file.removeprefix(f"{STD}/").removesuffix(".ella").replace("/", ".")


def visible(defined_in: str, used_in: str) -> bool:
    """Whether a private name of one module can be used in another: in itself, and in the modules below it."""
    return used_in == defined_in or used_in.startswith(defined_in + ".")


@cache
def library_module(name: str) -> Program | None:
    """A standard library module, parsed (once), or None if there's none by that name."""
    path = LIBRARY_DIR / f"{name}.ella"
    return parse(path.read_text(encoding="utf-8"), f"{STD}/{name}.ella") if path.is_file() else None


@cache
def prelude() -> dict[str, Item]:
    """The prelude: each public name of the standard library, by its name as written.

    The lazy `map`, `filter`, `zip` and `enumerate` of `iters` share names with the eager ones of `lists`,
    which the prelude has; they are `iters.map` and so on (`import iters`).
    """
    names: dict[str, Item] = {}
    paths = sorted(LIBRARY_DIR.glob("*.ella"), key=lambda path: (path.stem == LAZY, path.stem))
    for path in paths:  # `iters` last, so that the others' names win
        program = library_module(path.stem)
        assert program is not None
        for name, item in top_level(program.body, path.stem + ".", path.stem, f"{STD}/{path.name}").items():
            if not name.startswith("_") and name not in names:
                names[name] = item
    return names


def top_level(body: tuple[Expr, ...], prefix: str, module: str, file: str) -> dict[str, Item]:
    """The top-level names that a module's code defines, by their names as written."""
    found: dict[str, Item] = {}
    for expr in body:
        match expr:
            case Let(bindings, value):
                kind = "function" if isinstance(value, Lambda) else "variable"
                for binding in bindings:
                    found[binding.name] = Item(prefix + binding.name, kind, module, file)
                    for size in size_names(binding.type):  # a size name a `let` defines is an i64 variable
                        found.setdefault(size, Item(prefix + size, "variable", module, file))
            case EnumDef(name, variants=variants, struct=True, field_names=names):
                sealed = any(n.startswith("_") for n in names)
                found[name] = Item(prefix + name, "struct", module, file, (name,), sealed)
            case EnumDef(name, variants=variants):
                found[name] = Item(prefix + name, "enum", module, file, tuple(v.name for v in variants))
                for variant in variants:
                    found[variant.name] = Item(prefix + variant.name, "variant", module, file)
            case ConstraintDef(name):
                found[name] = Item(prefix + name, "constraint", module, file)
    for expr in body:  # a `let` with a pattern binds the names in it that aren't variants
        if isinstance(expr, LetPattern):
            for node in names_in(expr.pattern):
                if node.name not in found and node.name not in BUILTIN_NAMES:
                    found[node.name] = Item(prefix + node.name, "variable", module, file)
    return found


def names_in(pattern: Pattern) -> list[NamePattern]:
    match pattern:
        case NamePattern():
            return [pattern]
        case OrPattern(items) | VariantPattern(_, items) | TuplePattern(items):
            return [node for item in items for node in names_in(item)]
    return []


class Linker:
    """Links a program from its entry file (see `link`).

    For editors, which look at any file of a program as if it were the
    entry file, the root can be given (see `find_root`) and the file
    named as the module it is from there. `read` reads files (an editor
    has some open). With `every`, the whole prelude is linked in, used or
    not, to offer its names. With `errors` a list, errors are recorded
    there rather than raised, and a line with one is left out. `packages`
    is the package path (by default `package_path()`).
    """

    def __init__(
        self,
        path: Path | None,
        read: Reader = read_file,
        root: Path | None = None,
        every: bool = False,
        errors: list[EllaError] | None = None,
        packages: Sequence[Path] | None = None,
    ) -> None:
        self.root = root if root is not None else path.parent if path is not None else None
        self.packages = tuple(packages) if packages is not None else package_path()
        self.entry_name = "main"
        if path is not None:
            inside = self.root is not None and path.is_relative_to(self.root)
            self.entry_name = module_name(path.relative_to(self.root).as_posix() if inside else path.name)  # type: ignore[arg-type]
        self.read = read
        self.every = every
        self.errors = errors
        self.modules: dict[str, Module] = {}  # the program's, by name
        self.libraries: dict[str, Module] = {}  # the standard library's, by name
        self.files: dict[str, Module] = {}  # all of them, by file (as in spans)
        self.order: list[Module] = []  # in the order their code runs
        self.loading: list[Module] = []  # the modules whose imports are being loaded, outermost first
        self.items: dict[str, Item] = {}  # every module's top-level names, by their names in the linked program
        self.local_names: set[str] = set()  # the linked names of local types and their variants (`local_name`)
        self.warnings: list[EllaError] = []

    def link(self, program: Program) -> Program:
        if self.every:
            for name in sorted({item.module for item in prelude().values()}):
                self.library(name, program.span)
        self.entry = Module(self.entry_name, "", program, "", entry=True)
        self.modules[self.entry.name] = self.files[""] = self.entry
        self.load(self.entry)
        body = tuple(expr for module in self.order for expr in module.body)
        modules = tuple((module.file, module.name) for module in self.order)
        return replace(program, body=body, modules=modules, warnings=tuple(self.warnings))

    def attempt[T](self, step: Callable[[], T], otherwise: T) -> T:
        """Take a step of linking. When collecting errors, an error is recorded, and gives `otherwise`."""
        if self.errors is None:
            return step()
        try:
            return step()
        except EllaError as error:
            self.errors.append(error)
            return otherwise

    def load(self, module: Module) -> None:
        """Link a module: load its imports (and theirs, first), then link its code."""
        self.loading.append(module)
        module.top = top_level(module.program.body, module.prefix, module.name, module.file)
        self.items.update({item.name: item for item in module.top.values()})
        for line in module.program.imports:
            self.attempt(partial(self.bind_import, module, line), None)
        for name in module.bindings:
            head = name.partition(".")[0]
            if head in module.top:
                message = f"'{head}' is imported, and defined here too"
                self.attempt(partial(self.fail, message, module.uses[name].span), None)
        resolver = Resolver(self, module)
        body: list[Expr] = []
        for expr in module.program.body:
            linked = self.line(partial(resolver.top_line, expr), expr.span)
            body += resolver.hoisted  # its local types, defined first
            resolver.hoisted = []
            body.append(linked)
        module.body = tuple(body)
        for line in module.program.imports:
            if id(line) not in module.used:
                self.warnings.append(EllaError(f"'{'.'.join(line.path)}' is imported but never used", line.span))
        self.loading.pop()
        # Append after imports so dependencies' top-level code runs first.
        self.order.append(module)

    def line(self, step: Callable[[], Expr], span: Span) -> Expr:
        """Link a line of code. When collecting errors, a line with one becomes `()`."""
        blank: Expr = UnitLit(span)
        return self.attempt(step, blank)

    def fail(self, message: str, span: Span) -> None:
        raise EllaError(message, span)

    def bind_import(self, module: Module, line: Import) -> None:
        """Bind the names an import line gives: its path (or alias), to a module or an item, and an enum's variants."""
        target: Module | Item
        found = self.find(line.path, line.span)
        if found is not None:
            target = found
        else:
            owner = self.find(line.path[:-1], line.span) if len(line.path) > 1 else None
            path = ".".join(line.path)
            if owner is None:
                raise EllaError(f"there is no module '{path}'", line.span)
            item = owner.top.get(line.path[-1])
            if item is None:
                raise EllaError(f"module '{owner.name}' has no '{line.path[-1]}'", line.span)
            if item.name.rpartition(".")[2].startswith("_") and not visible(owner.name, module.name):
                raise EllaError(f"'{line.path[-1]}' is private to module '{owner.name}'", line.span)
            target = item
        key = line.alias or ".".join(line.path)
        names = {key: target}
        if isinstance(target, Item) and target.kind == "enum":
            # Its variants come with it, named alongside it (or bare, if it has an alias).
            where = "" if line.alias else ".".join(line.path[:-1]) + "."
            prefix = target.name.rpartition(".")[0] + "."
            names |= {where + variant: self.items[prefix + variant] for variant in target.variants}
        for name, bound in names.items():
            if name in module.bindings:
                raise EllaError(f"'{name}' is imported twice", line.span)
            module.bindings[name] = bound
            module.uses[name] = line

    def find(self, path: tuple[str, ...], span: Span) -> Module | None:
        """The module a path names, loaded if it hasn't been, or None if there's no such module."""
        name = ".".join(path)
        if name in self.modules:
            self.check_cycle(self.modules[name], span)
            return self.modules[name]
        file = "/".join(path) + ".ella"
        text = self.read(self.root / file) if self.root is not None else None
        for package in self.packages if text is None else ():
            text = self.read(package / file)
            if text is not None:
                # Named by its path from the root, as other modules are, so that it can be found from its span.
                found = package / file
                file = (
                    Path(os.path.relpath(found, self.root)).as_posix() if self.root is not None else found.as_posix()
                )
                break
        if text is None:
            return self.library(name, span) if len(path) == 1 else None
        shadows = library_module(path[0]) is not None
        if shadows and len(path) == 1:
            self.warnings.append(
                EllaError(f"'{name}.ella' has the name of a standard library module, and is used instead of it", span)
            )
        module = Module(name, file, parse(text, file), ("./" if shadows else "") + name + ".")
        self.modules[name] = self.files[file] = module
        self.load(module)
        return module

    def library(self, name: str, span: Span) -> Module | None:
        """A standard library module, loaded if it hasn't been."""
        if name in self.libraries:
            self.check_cycle(self.libraries[name], span)
            return self.libraries[name]
        program = library_module(name)
        if program is None:
            return None
        module = Module(name, f"{STD}/{name}.ella", program, name + ".")
        self.libraries[name] = self.files[module.file] = module
        self.load(module)
        return module

    def extension_function(self, name: str, span: Span) -> None:
        """Load the prelude function that may serve as a built-in receiver method."""
        item = prelude().get(name)
        if item is not None and item.kind == "function":
            self.library(item.module, span)

    def check_cycle(self, module: Module, span: Span) -> None:
        """A module being loaded can't be imported again by one of its imports."""
        if any(m is module for m in self.loading):
            names = [m.name for m in self.loading[[id(m) for m in self.loading].index(id(module)) :]]
            raise EllaError(f"these imports form a cycle: {' -> '.join([*names, module.name])}", span)


@dataclass(slots=True)
class Scope:
    """The local names at some point of a module's code: variables, types (and type parameters), and variants.

    A local type and its variants have names in the linked program (`linked`), since they are moved to
    the top level (see `Resolver.local_name`); type parameters keep theirs.
    """

    parent: Scope | None
    values: set[str] = field(default_factory=set)
    types: set[str] = field(default_factory=set)
    variants: set[str] = field(default_factory=set)
    linked: dict[str, str] = field(default_factory=dict)

    def value(self, name: str) -> str | None:
        """What a local name is, "value" or "variant", or None if it isn't local."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.values:
                return "value"
            if name in scope.variants:
                return "variant"
            scope = scope.parent
        return None

    def has_type(self, name: str) -> bool:
        return self.type_name(name) is not None

    def type_name(self, name: str) -> str | None:
        """A local type's (or type parameter's) name in the linked program, or None if it isn't local."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.types:
                return scope.linked.get(name, name)
            scope = scope.parent
        return None

    def variant_name(self, name: str) -> str:
        """A local variant's name in the linked program (`value(name)` is "variant")."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.variants:
                return scope.linked.get(name, name)
            scope = scope.parent
        return name


TOP = Scope(None)  # the scope of top-level code, where nothing is local


class Resolver:
    """Links one module's code: gives its top-level names their linked names, and every use of a top-level name too."""

    def __init__(self, linker: Linker, module: Module) -> None:
        self.linker = linker
        self.module = module
        self.owner = ""  # the linked name of the top-level line being linked, in front of its local types' names
        self.hoisted: list[EnumDef] = []  # its local types, moved to the top level in front of it
        self.quotes = 0  # how many quotes the code being linked is in: types in them stay where they are

    # Lines

    def top_line(self, expr: Expr) -> Expr:
        match expr:
            case Let(bindings) if bindings:
                self.owner = self.top_name(bindings[0].name)
            case _:
                self.owner = self.top_name("top")
        return self.expr(expr, TOP)

    def lines(self, exprs: tuple[Expr, ...], scope: Scope) -> tuple[Expr, ...]:
        """A block's lines. When collecting errors, a line with one is left out.

        A type defined here is moved to the top level (see `enum`), and so leaves the block, unless it is
        its last line, which gives the block's value, `()`.
        """
        lines = [self.linker.line(partial(self.expr, expr, scope), expr.span) for expr in exprs]
        return tuple(
            line
            for i, (expr, line) in enumerate(zip(exprs, lines))
            if not (isinstance(expr, EnumDef) and isinstance(line, UnitLit)) or i == len(exprs) - 1
        )

    # Names

    def top_name(self, name: str) -> str:
        """A name defined at the top level of this module, as it is in the linked program."""
        return self.module.prefix + name

    def item(self, name: str, span: Span) -> Item | None:
        """The top-level item a bare name refers to here: the module's own, an import's, or the prelude's."""
        module = self.module
        if name in module.top:
            return module.top[name]
        if name in module.bindings:
            module.used.add(id(module.uses[name]))
            target = module.bindings[name]
            if isinstance(target, Module):
                raise EllaError(f"'{name}' is a module, not a value", span)
            return target
        for key, target in module.bindings.items():
            if key.startswith(name + "."):
                raise EllaError(f"'{name}' is part of the name of the module '{key}', not a value", span)
        library = prelude().get(name)
        if library is not None and library.file != module.file:
            imported = self.linker.library(library.module, span)
            assert imported is not None
            return imported.top[name]
        return None

    def path_item(self, path: list[tuple[str, Span]]) -> tuple[Item, int] | None:
        """The item a dotted path starts with through an import: `geo.poly.area` with `import geo.poly`.

        Returns it and how many parts of the path it took, or None if no import binds a start of the path.
        """
        module = self.module
        names = [name for name, _ in path]
        for i in range(len(names), 0, -1):
            key = ".".join(names[:i])
            if key not in module.bindings:
                continue
            module.used.add(id(module.uses[key]))
            target = module.bindings[key]
            if isinstance(target, Item):
                return target, i
            if i == len(names):
                raise EllaError(f"'{key}' is a module, not a value", path[0][1])
            name, span = path[i]
            item = target.top.get(name)
            if item is None:
                raise EllaError(f"module '{target.name}' has no '{name}'", span)
            self.check_private(item, span)
            return item, i + 1
        return None

    def check_private(self, item: Item, span: Span) -> None:
        if bare_name(item.name).startswith("_") and not visible(item.module, self.module.name):
            raise EllaError(f"'{bare_name(item.name)}' is private to module '{item.module}'", span)

    def check_sealed(self, item: Item, span: Span) -> None:
        """A struct with a private field can only be made, or taken apart, in its module (and below)."""
        if item.sealed and not visible(item.module, self.module.name):
            raise EllaError(
                f"{bare_name(item.name)} has private fields, so only module '{item.module}' can make one or take one apart",
                span,
            )

    def unknown(self, name: str, span: Span, builtins: frozenset[str], what: str) -> str:
        """A bare name that isn't a top-level one: a built-in, or (in the entry file) for the checker to report."""
        if self.module.entry or name in builtins:
            return name
        raise EllaError(f"undefined {what} '{name}'", span)

    def value_name(self, name: str, span: Span, scope: Scope) -> str:
        """A name used as a value."""
        if name == "return":
            return name
        if (local := scope.value(name)) is not None:
            return scope.variant_name(name) if local == "variant" else name
        item = self.item(name, span)
        if item is None:
            return self.unknown(name, span, BUILTIN_NAMES, "name")
        return self.use_value(item, span)

    def use_value(self, item: Item, span: Span) -> str:
        match item.kind:
            case "enum" | "constraint":
                raise EllaError(f"'{bare_name(item.name)}' is a type, not a value", span)
            case "struct":
                self.check_sealed(item, span)
        return item.name

    def variant_name(self, name: str, span: Span, scope: Scope) -> str | None:
        """The variant a name in a pattern refers to, or None if it isn't one (so it binds a variable)."""
        local = scope.value(name) if "." not in name else None
        if local is not None:
            return scope.variant_name(name) if local == "variant" else None
        item: Item | None
        if "." in name:
            path = [(part, span) for part in name.split(".")]
            found = self.path_item(path)
            if found is None or found[1] != len(path):
                raise EllaError(f"'{name}' isn't imported", span)
            item = found[0]
        else:
            item = self.item(name, span) if name in self.module.top or name in self.module.bindings else None
            if item is None:
                return name if name in BUILTIN_NAMES and name[0].isupper() else None
        if item.kind not in ("variant", "struct"):
            if "." in name:
                raise EllaError(f"'{name}' isn't an enum variant", span)
            return None
        if item.kind == "struct":
            self.check_sealed(item, span)
        return item.name

    def type_name(self, name: str, span: Span, scope: Scope) -> str:
        if "." not in name and (local := scope.type_name(name)) is not None:
            return local
        if "." in name:
            found = self.path_item([(part, span) for part in name.split(".")])
            if found is None or found[1] != len(name.split(".")):
                raise EllaError(f"unknown type '{name}'", span)
            item: Item | None = found[0]
        else:
            item = self.item(name, span) if name not in BUILTIN_TYPE_NAMES or name in self.module.top else None
        if item is None:
            return self.unknown(name, span, BUILTIN_TYPE_NAMES, "type")
        if item.kind not in ("enum", "struct"):
            raise EllaError(f"'{name}' isn't a type", span)
        return item.name

    def constraint_name(self, name: str, span: Span) -> str:
        if name in CONSTRAINTS and name not in self.module.top:
            return name
        found = self.path_item([(part, span) for part in name.split(".")]) if "." in name else None
        item = found[0] if found is not None else self.item(name, span) if "." not in name else None
        if item is None:
            return self.unknown(name, span, frozenset(CONSTRAINTS), "constraint")
        if item.kind != "constraint":
            raise EllaError(f"'{name}' isn't a constraint", span)
        return item.name

    def constraints(self, constraints: tuple[tuple[str, ...], ...], span: Span) -> tuple[tuple[str, ...], ...]:
        return tuple(tuple(self.constraint_name(c, span) for c in needs) for needs in constraints)

    # Types and patterns

    def type(self, texpr: TypeExpr | None, scope: Scope) -> TypeExpr | None:
        match texpr:
            case None:
                return None
            case NamedType(name, args, span):
                resolved = self.type_name(name, span, scope)
                return replace(texpr, name=resolved, args=tuple(self.type(arg, scope) for arg in args))  # type: ignore[misc]
        return rebuild(texpr, lambda part: self.part(part, scope))  # type: ignore[return-value]

    def pattern(self, pattern: Pattern, scope: Scope, bind: set[str], top: bool = False) -> Pattern:
        """A pattern, adding the names it binds to `bind`. At the top level, they are top-level names."""
        match pattern:
            case NamePattern(name, span, mutable):
                variant = self.variant_name(name, span, scope)
                if variant is not None and not mutable:
                    return replace(pattern, name=variant)
                if "." in name:
                    raise EllaError(f"'{name}' isn't an enum variant", span)
                bind.add(name)
                return replace(pattern, name=self.top_name(name)) if top else pattern
            case VariantPattern(name, args, span):
                variant = self.variant_name(name, span, scope)
                if variant is None:
                    raise EllaError(f"'{name}' isn't an enum variant", span)
                return VariantPattern(variant, tuple(self.pattern(arg, scope, bind, top) for arg in args), span)
            case OrPattern(items, span):
                return OrPattern(tuple(self.pattern(item, scope, bind, top) for item in items), span)
            case TuplePattern(items, span):
                return TuplePattern(tuple(self.pattern(item, scope, bind, top) for item in items), span)
        return pattern

    # Expressions

    def part(self, node: object, scope: Scope) -> object:
        """Any part of an expression, in the scope it is in."""
        match node:
            case tuple():
                return rebuild(node, lambda item: self.part(item, scope))
            case NamedType():
                return self.type(node, scope)
            case Binding() | Param():
                return replace(node, type=self.type(node.type, scope))
            case _ if isinstance(node, Expr.__value__):
                return self.expr(node, scope)
            case _ if hasattr(node, "__dataclass_fields__") and not isinstance(node, Span):
                return rebuild(node, lambda item: self.part(item, scope))
        return node

    def declared[N: (Binding, Param)](self, node: N, scope: Scope) -> N:
        """A binding's or parameter's type annotation."""
        return replace(node, type=self.type(node.type, scope))

    def expr(self, expr: Expr, scope: Scope) -> Expr:
        top = scope is TOP
        match expr:
            case Name(name, span):
                return Name(self.value_name(name, span, scope), span)
            case Member():
                return self.member(expr, scope)
            case Let(bindings, value, span):
                # A function's size parameters are i64 variables in it, as are the size names a `let`'s types
                # have after it (at the top level, top-level names).
                inner = Scope(scope, set(bindings[0].size_params), types=set(bindings[0].type_params))
                sizes = [size for b in bindings for size in size_names(b.type)]
                bindings = tuple(
                    replace(
                        self.declared(b, inner),
                        name=self.top_name(b.name) if top else b.name,
                        constraints=self.constraints(b.constraints, b.span),
                    )
                    for b in bindings
                )
                if top:
                    bindings = tuple(rename_sizes(b, self.top_name) for b in bindings)  # type: ignore[misc]
                if isinstance(value, Lambda) and not top:  # a local function can call itself
                    scope.values.update(b.name for b in bindings)
                value = self.expr(value, inner)
                if not top:
                    scope.values.update(b.name for b in bindings)
                    scope.values.update(sizes)
                return Let(bindings, value, span)
            case LetPattern(pattern, value, span, mutable, texpr):
                value = self.expr(value, scope)
                bound: set[str] = set()
                pattern = self.pattern(pattern, scope, bound, top)
                if not top:
                    scope.values |= bound
                return LetPattern(pattern, value, span, mutable, self.type(texpr, scope))
            case Assign(op, target, value, span):
                value = self.expr(value, scope)
                target = self.expr(target, scope)  # type: ignore[assignment]
                self.check_changeable(target, scope)
                return Assign(op, target, value, span)
            case MutArg(place, span):
                resolved = MutArg(self.expr(place, scope), span)
                self.check_changeable(resolved.target, scope)
                return resolved
            case Lambda(params, result, where, body, span):
                inner = Scope(scope, {p.name for p in params})
                params = tuple(self.declared(p, scope) for p in params)
                where = tuple(self.part(cond, inner) for cond in where)  # type: ignore[misc]
                return Lambda(params, self.type(result, scope), where, self.expr(body, inner), span)
            case Block(exprs, span):
                return Block(self.lines(exprs, Scope(scope)), span)
            case Match(subject, cases, span):
                subject = self.expr(subject, scope)
                resolved_cases = []
                for case in cases:
                    inner = Scope(scope)
                    pattern = self.pattern(case.pattern, scope, inner.values)
                    guard = self.expr(case.guard, inner) if case.guard is not None else None
                    resolved_cases.append(
                        replace(case, pattern=pattern, guard=guard, body=self.expr(case.body, inner))
                    )
                return Match(subject, tuple(resolved_cases), span)
            case For(label, pattern, iterable, body, span):
                iterable = self.expr(iterable, scope)
                inner = Scope(scope)
                pattern = self.pattern(pattern, scope, inner.values)
                return For(label, pattern, iterable, self.expr(body, inner), span)
            case EnumDef():
                return self.enum(expr, scope)
            case ConstraintDef(name, methods, span):
                inner = Scope(scope, types={"Self"})
                methods = tuple(self.declared(m, inner) for m in methods)
                return ConstraintDef(
                    self.top_name(name),
                    methods,
                    span,
                    tuple(self.constraint_name(parent, span) for parent in expr.parents),
                    tuple(
                        replace(const, type=self.type(const.type, inner))
                        for const in expr.constants
                        if const.type is not None
                    ),
                )
            case Quote(body, span):
                self.quotes += 1
                try:
                    return Quote(self.expr(body, Scope(scope)), span)
                finally:
                    self.quotes -= 1
            case MapLit(entries, span):
                pairs = ListLit(tuple(TupleLit((key, value), key.span) for key, value in entries), span)
                return Call(
                    Name(self.library_function("maps", "map_of", span), span), (self.expr(pairs, scope),), span
                )
            case SetLit(items, span):
                items_list = self.expr(ListLit(items, span), scope)
                return Call(Name(self.library_function("sets", "set_of", span), span), (items_list,), span)
        return rebuild(expr, lambda part: self.part(part, scope))  # type: ignore[return-value]

    def library_function(self, module: str, name: str, span: Span) -> str:
        """The linked name of a standard library function that syntax stands for, whatever this module defines."""
        library = self.linker.library(module, span)
        assert library is not None
        return library.top[name].name

    def local_name(self, name: str) -> str:
        """The linked name of a type or variant defined inside the top-level line being linked: `f#S` for S in
        the function f, `f#2#S` for another one, `geo.area#S` in module geo.

        Such a type is moved to the top level (`hoisted`), so that it is a top-level type to the checker,
        the interpreters and the lowering, which find types by name, and it can give constants. That is
        possible since it can't use its function's variables or type parameters, nor have methods. The
        `#` keeps the name apart from any top-level name of a module (`f.S` could be module f's S), and
        `bare_name` leaves out what is in front of it, so its values print as written.
        """
        linked = f"{self.owner}#{name}"
        n = 1
        while linked in self.linker.local_names:
            n += 1
            linked = f"{self.owner}#{n}#{name}"
        self.linker.local_names.add(linked)
        return linked

    def enum(self, enum: EnumDef, scope: Scope) -> Expr:
        """An enum or struct definition. At the top level, it and its variants get their linked names.

        A local one gets new names (`local_name`) and is moved to the top level, in front of the line it is
        in; it leaves `()` where it was. One in a quote stays there, as part of the code the quote makes.
        """
        top = scope is TOP
        hoist = not top and not self.quotes
        linked: dict[str, str] = {}
        if not top:
            scope.types.add(enum.name)
            scope.variants.update(v.name for v in enum.variants)
        if hoist:
            linked = {
                name: self.local_name(name) for name in dict.fromkeys([enum.name, *(v.name for v in enum.variants)])
            }
            scope.linked.update(linked)
        inner = Scope(scope, types=set(enum.type_params))
        variants = tuple(
            Variant(
                self.top_name(v.name) if top else linked.get(v.name, v.name),
                tuple(self.type(f, inner) for f in v.fields),  # type: ignore[misc]
                v.span,
            )
            for v in enum.variants
        )
        methods = tuple(Method(m.name, self.expr(m.function, inner), m.span) for m in enum.methods)
        implementations = tuple(
            Implementation(
                self.constraint_name(impl.constraint, impl.span),
                tuple(Method(m.name, self.expr(m.function, inner), m.span) for m in impl.methods),
                impl.span,
                tuple(replace(const, value=self.expr(const.value, inner)) for const in impl.constants),
            )
            for impl in enum.implementations
        )
        resolved = replace(
            enum,
            name=self.top_name(enum.name) if top else linked.get(enum.name, enum.name),
            variants=variants,
            methods=methods,
            constraints=self.constraints(enum.constraints, enum.span),
            implementations=implementations,
        )
        if not hoist:
            return resolved
        self.hoisted.append(resolved)
        return UnitLit(enum.span)

    def member(self, expr: Member, scope: Scope) -> Expr:
        """`a.b.c`: a path to an imported module's name (or an imported item), or members of a value."""
        path: list[tuple[str, Span]] = []
        members: list[Member] = []
        node: Expr = expr
        while isinstance(node, Member):
            members.append(node)
            node = node.target
        members.reverse()
        if not isinstance(node, Name) or node.name == "return" or scope.value(node.name) is not None:
            self.linker.extension_function(expr.name, expr.span)
            return rebuild(expr, lambda part: self.part(part, scope))  # type: ignore[return-value]
        path = [(node.name, node.span)] + [
            (m.name, Span(m.span.line, m.span.column + 1, m.span.file)) for m in members
        ]
        found = self.path_item(path)
        if found is None:
            type_item = self.module.top.get(node.name)
            if (local := scope.type_name(node.name)) is not None:  # a local type, or a type parameter: `T.ZERO`
                result: Expr = Name(local, node.span)
            elif type_item is not None and type_item.kind == "enum":
                result = Name(type_item.name, node.span)
            else:
                self.linker.extension_function(members[-1].name, members[-1].span)
                result = Name(self.value_name(node.name, node.span, scope), node.span)
            taken = 1
        else:
            item, taken = found
            value = (
                item.name if item.kind == "enum" and taken < len(path) else self.use_value(item, path[taken - 1][1])
            )
            result = Name(value, path[taken - 1][1])
        for member in members[taken - 1 :]:
            result = Member(result, member.name, member.span)
        return result

    def check_changeable(self, place: Expr, scope: Scope) -> None:
        """A place that is assigned to, or passed as `mut`, can't be a variable of another module."""
        root = place
        while isinstance(root, Index | Member):
            root = root.target
        if not isinstance(root, Name) or scope.value(root.name) is not None:
            return
        item = self.linker.items.get(root.name)
        if item is not None and item.kind == "variable" and item.file != self.module.file:
            raise EllaError(
                f"'{root.name}' is a variable of module '{item.module}', so it can't be changed here", root.span
            )
