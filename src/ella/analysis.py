"""What an editor needs to know about a program: its errors, what each name in it is, and what could be typed next."""

import io
import re
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

from ella.checker import (
    BUILTIN_TYPES,
    GENERIC_LIBRARY,
    HIDDEN_LIBRARY,
    LIBRARY,
    PRIMITIVE_NAMES,
    RANGE,
    STR,
    BuiltinTy,
    Checker,
    EnumTy,
    Env,
    FuncTy,
    ListTy,
    ParamTy,
    Type,
    VarInfo,
    check,
    resolve,
    show_library,
    show_type,
    substitute,
)
from ella.lexer import KEYWORDS, STD, EllaError, Span, Token, TokenKind, tokenize
from ella.modules import LIBRARY_DIR, Linker, Module, find_root, package_path, prelude, read_file
from ella.parser import parse_partial
from ella.staging import Expander, has_constants, has_splices, uses_staging
from ella.syntax import (
    Binary,
    Binding,
    Compare,
    ConstraintDef,
    Dim,
    EnumDef,
    Expr,
    If,
    Let,
    LetPattern,
    Loop,
    Match,
    Member,
    Name,
    NamedType,
    NamePattern,
    Param,
    Program,
    Quote,
    Splice,
    Try,
    Unary,
    VariantPattern,
    bare_name,
    parts,
    show,
)

# Stands for the name being typed when there's none yet, as in `p.` or `f(`.
PLACEHOLDER = "__ella_complete__"

# The semantic token types and modifiers symbols use, in the order LSP numbers them.
TOKEN_TYPES = (
    "variable",
    "parameter",
    "function",
    "method",
    "property",
    "struct",
    "enum",
    "enumMember",
    "interface",
    "type",
    "typeParameter",
    "namespace",
)
TOKEN_MODIFIERS = ("declaration", "readonly", "defaultLibrary")

BUILTIN_ENUMS = ("Result", "Optional", "Error")  # defined by the language, not the program


@dataclass(frozen=True, slots=True)
class Problem:
    """An error, or a warning about something that doesn't stop the program (a case that never matches)."""

    message: str
    line: int
    column: int
    warning: bool = False


@dataclass(frozen=True, slots=True)
class Location:
    """A place in the file being analyzed, or in another one (`file`, its path)."""

    line: int
    column: int
    length: int
    file: str = ""


@dataclass(frozen=True, slots=True)
class Symbol:
    """A name or operator in the source, and what it is.

    `hover` is Ella text, such as `x: i64`. `kind` is a semantic token type,
    or "" for an operator, which the grammar highlights already.
    """

    where: Location
    hover: str
    kind: str = ""
    modifiers: tuple[str, ...] = ()
    definition: Location | None = None


@dataclass(frozen=True, slots=True)
class Analysis:
    """A program's errors, and its symbols, sorted by position.

    `symbols` is None when the text is too broken to tokenize; an editor can
    keep using the symbols it had.
    """

    problems: list[Problem]
    symbols: list[Symbol] | None


@dataclass(frozen=True, slots=True)
class Completion:
    """Something that could be typed: a name, field, method, keyword or type. `kind` is as for symbols, or "keyword"."""

    label: str
    kind: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Place:
    """Where the source being analyzed is: its file, if it has one, the root its imports are found from,
    and the text of the files the editor has open, by path."""

    path: str | None = None
    root: str | None = None
    files: dict[str, str] | None = None


def analyze(source: str, place: Place | None = None) -> Analysis:
    """The errors in a program, and its symbols."""
    try:
        checked = Checked(source, expand=True, place=place or Place())
    except EllaError as error:
        return Analysis([problem(error)], None)
    except RecursionError:
        return Analysis([Problem("this program is nested too deeply to check", 1, 1)], None)
    return Analysis(checked.problems, Symbols(checked).find())


def complete(source: str, line: int, column: int, place: Place | None = None) -> list[Completion]:
    """What could be typed at `line` and `column`: the names in scope there, or the fields and methods after `.`,
    or the names of a module after its name and `.`."""
    lines = source.split("\n")
    if not 1 <= line <= len(lines):
        return []
    before = lines[line - 1][: column - 1]
    word = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", before)
    start = column - (len(word.group()) if word else 0)
    if word is None:
        # Nothing typed yet: put a name there to find the scope of.
        lines[line - 1] = before + PLACEHOLDER + lines[line - 1][column - 1 :]
    try:
        checked = Checked("\n".join(lines), expand=False, place=place or Place())
    except EllaError, RecursionError:
        return keywords()
    path = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.$", before[: start - 1])
    if path is not None and isinstance(checked.linker.entry.bindings.get(path.group(1)), Module):
        return checked.module_names(checked.linker.entry.bindings[path.group(1)])  # type: ignore[arg-type]
    token = checked.token_at(Span(line, start))
    if token is not None and token.kind is TokenKind.KEYWORD:
        return keywords()
    if token is None or token.kind is not TokenKind.NAME:
        return []  # in a comment or string, or not at a name
    for node in walk(checked.program):
        if isinstance(node, Member) and checked.after(node.span) is token:
            return checked.members(node)
        if isinstance(node, Name) and node.span == token.span:
            return checked.names_in_scope(node) + keywords()
    return keywords()


def follow_edits(symbols: list[Symbol], old: str, new: str) -> list[Symbol]:
    """The symbols of the source `old`, moved to where their lines are in `new`. Those on changed lines are dropped.

    An editor uses this until the new source has been analyzed, or when it is too broken to analyze.
    """
    if old == new:
        return symbols
    moved: dict[int, int] = {}  # line in `old` -> the same line in `new`
    matcher = SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False)
    # Only unchanged lines retain their symbols; edited lines need fresh analysis.
    for a, b, size in matcher.get_matching_blocks():
        for i in range(1, size + 1):
            moved[a + i] = b + i
    result = []
    for symbol in symbols:
        if symbol.where.line not in moved:
            continue
        where = replace(symbol.where, line=moved[symbol.where.line])
        target = symbol.definition
        if target is not None and target.file:  # in another file
            definition: Location | None = target
        else:
            definition = (
                None if target is None or target.line not in moved else replace(target, line=moved[target.line])
            )
        result.append(replace(symbol, where=where, definition=definition))
    return result


def keywords() -> list[Completion]:
    return [Completion(word, "keyword") for word in sorted(KEYWORDS)] + [
        Completion(name, "type", "built-in type") for name in PRIMITIVE_NAMES
    ]


def problem(error: EllaError, warning: bool = False) -> Problem:
    return Problem(error.message, error.span.line, error.span.column, warning)


# Fixes for lexer errors, so the rest of the program can be read: by message,
# what to put in at the error, or at the end of its line.
CLOSE_AT_END = {"'(' is never closed": ")", "'[' is never closed": "]", "this interpolation is never closed": "}}"}
CLOSE_HERE = {"line break inside {{ }}": "}}"}
BLANK = ("unmatched", "unexpected character", "tab characters")


def tokenize_leniently(source: str) -> tuple[list[Token], str, list[EllaError]]:
    """Tokenize, fixing the text after each error. Returns the tokens, the text they're from, and the errors.

    Raises the error if there's no fix for it.
    """
    errors: list[EllaError] = []
    for _ in range(100):
        try:
            return tokenize(source), source, errors
        except EllaError as error:
            errors.append(error)
            source = fix(source, error)
    raise errors[0]


def fix(source: str, error: EllaError) -> str:
    """The source, changed so that `error` doesn't happen there again (without moving any of its text)."""
    lines = source.split("\n")
    row, column = error.span.line - 1, error.span.column - 1
    text = lines[row] if row < len(lines) else ""
    if any(error.message.startswith(start) for start in BLANK) and column < len(text):
        lines[row] = text[:column] + " " + text[column + 1 :]
    elif (close := next((c for m, c in CLOSE_AT_END.items() if error.message.startswith(m)), None)) is not None:
        lines[row] = text + close
    elif (close := next((c for m, c in CLOSE_HERE.items() if error.message.startswith(m)), None)) is not None:
        lines[row] = text[:column] + close + text[column:]
    elif error.message.startswith(("this string is never closed", "line break in a single-line string")):
        quote = re.match(r"\"\"\"|'''|\"|'", text[column:]) if "never" in error.message else None
        if quote is None:  # a line break in a string: close it before the break
            start = max(text.rfind('"', 0, column), text.rfind("'", 0, column))
            quote = re.match(r"\"|'", text[start:]) if start >= 0 else None
        if quote is None:
            raise error
        lines[row] = text + quote.group()
    else:
        raise error
    return "\n".join(lines)


class Checked:
    """A program, parsed and checked as far as possible."""

    def __init__(self, source: str, expand: bool, place: Place) -> None:
        self.tokens, source, lex_errors = tokenize_leniently(source)
        self.starts = {(token.span.line, token.span.column): i for i, token in enumerate(self.tokens)}
        parsed, parse_errors = parse_partial(self.tokens, source)
        path = Path(place.path) if place.path is not None else None
        root = Path(place.root) if place.root is not None else None
        files = place.files or {}

        def read(file: Path) -> str | None:
            return files[str(file)] if str(file) in files else read_file(file)

        packages = package_path()
        if path is not None and root is not None:
            root = find_root(path, parsed.imports, root, read, packages)
        # Every prelude module is linked in, so that completions offer its names.
        link_errors: list[EllaError] = []
        self.linker = Linker(path, read, root, every=True, errors=link_errors, packages=packages)
        self.root = self.linker.root
        self.linked = self.linker.link(parsed)
        # This file's code, linked (with its local types moved to the top level); the checker's tables are
        # about it.
        self.program = replace(parsed, body=self.linker.entry.body)
        self.checker = Checker()
        self.checker.errors = []
        self.checker.library = path is not None and is_library(parsed)
        expander = Expander(self.linked, self.checker, io.StringIO()) if uses_staging(self.linked) else None
        self.checker.check_program(self.linked)

        # Errors in a top-level line with a parse error likely follow from it.
        lines = self.top_level_lines()
        broken = {bisect_right(lines, error.span.line) for error in parse_errors}
        errors = lex_errors + parse_errors + link_errors
        errors += [
            error
            for error in self.checker.errors
            if error.span.file or bisect_right(lines, error.span.line) not in broken
        ]
        # Splices outside quotes, and types' constants, run while compiling: that may fail too.
        staged = has_splices(self.linked) or has_constants(self.linked)
        if expand and not errors and expander is not None and staged:
            try:
                check(expander.expand_program())
            except EllaError as error:
                errors.append(error)
        problems = [self.problem(error) for error in errors]
        problems += [problem(warning, True) for warning in self.checker.warnings if not warning.span.file]
        self.problems = sorted(dict.fromkeys(p for p in problems if p is not None), key=lambda p: (p.line, p.column))

    def problem(self, error: EllaError) -> Problem | None:
        """An error, shown in this file: one in an imported module is shown at the import line it comes through."""
        if not error.span.file:
            return problem(error)
        if error.span.file.startswith(STD):
            return None  # the standard library's own code (it has no errors)
        line = self.import_line(self.linker.files[error.span.file].name)
        return Problem(f"{error.span}: {error.message}", line.line, line.column)

    def import_line(self, name: str) -> Span:
        """Where this file imports the module `name`, directly or through other modules."""

        def needs(module: Module, seen: set[str]) -> bool:
            if module.name == name:
                return True
            seen.add(module.name)
            targets = [t if isinstance(t, Module) else self.linker.files.get(t.file) for t in module.bindings.values()]
            return any(t is not None and t.name not in seen and needs(t, seen) for t in targets)

        for key, target in self.linker.entry.bindings.items():
            module = target if isinstance(target, Module) else self.linker.files.get(target.file)
            if module is not None and needs(module, set()):
                return self.linker.entry.uses[key].span
        return self.program.imports[0].span if self.program.imports else Span(1, 1)

    def file_path(self, file: str) -> str:
        """The path of a file of the program, from its name in spans."""
        if file.startswith(STD + "/"):
            return str(LIBRARY_DIR / file.removeprefix(STD + "/"))
        return str((self.root or Path()) / file)

    def locate(self, span: Span, name: str) -> Location | None:
        """Where something named `name` is, at `span`: at its token in this file, or in another file."""
        if span.file:
            return Location(span.line, span.column, len(bare_name(name)), self.file_path(span.file))
        token = self.token_at(span)
        return None if token is None else location(token)

    def module_names(self, module: Module) -> list[Completion]:
        """The public names of a module, after its name and `.`."""
        found = []
        for name, item in module.top.items():
            if name.startswith("_"):
                continue
            info = self.checker.globals.vars.get(item.name)
            kind = {"enum": "enum", "struct": "struct", "variant": "enumMember", "constraint": "interface"}
            if item.kind in kind:
                found.append(Completion(name, kind[item.kind]))
            else:
                detail = "" if info is None else describe(name, info)
                found.append(Completion(name, "function" if item.kind == "function" else "variable", detail))
        return found

    def shown(self, name: str, at: Span | None = None) -> str | None:
        """How a top-level name is written in this file (at `at`), or None if it can't be: the prelude's without
        their module, and a local type's (`f#S`, moved to the top level by linking) only in its own function."""
        if "#" in name:
            owner = name.rpartition("#")[0].partition("#")[0]
            return bare_name(name) if at is not None and self.owner_at(at) == owner else None
        if "." not in name:
            return name
        item = prelude().get(bare_name(name))
        return bare_name(name) if item is not None and item.name == name else None

    def owner_at(self, span: Span) -> str | None:
        """The linked name of the top-level line of this file that `span` is in, which its local types' names
        start with (`Resolver.local_name`)."""
        owner = None
        for expr in self.program.body:
            if isinstance(expr, EnumDef) and "#" in expr.name:
                continue  # a local type, moved in front of its line
            if (expr.span.line, expr.span.column) > (span.line, span.column):
                break
            owner = expr.bindings[0].name if isinstance(expr, Let) and expr.bindings else "top"
        return owner

    def top_level_lines(self) -> list[int]:
        """The lines where top-level lines of the program start."""
        lines = []
        depth = 0
        for i, token in enumerate(self.tokens):
            starts_line = i == 0 or self.tokens[i - 1].kind in (TokenKind.NEWLINE, TokenKind.DEDENT)
            if depth == 0 and starts_line and token.kind not in (TokenKind.INDENT, TokenKind.EOF):
                lines.append(token.span.line)
            depth += {TokenKind.INDENT: 1, TokenKind.DEDENT: -1}.get(token.kind, 0)
        return lines

    def token_at(self, span: Span) -> Token | None:
        """The token that starts where `span` does."""
        i = self.starts.get((span.line, span.column))
        return None if i is None else self.tokens[i]

    def after(self, span: Span) -> Token | None:
        """The token after the one that starts where `span` does: a member's name, after its `.`."""
        i = self.starts.get((span.line, span.column))
        return None if i is None or i + 1 >= len(self.tokens) else self.tokens[i + 1]

    def type_of(self, node: object) -> Type | None:
        return self.checker.types.get(id(node))

    def names_in_scope(self, name: Name) -> list[Completion]:
        """The names that can be used where `name` is: those bound before it in its scope, and the top-level ones."""
        line, column = name.span.line, name.span.column
        env: Env | None = self.checker.scopes.get(id(name), self.checker.globals)
        seen: set[str] = set()
        found = []
        while env is not None:
            for var, info in env.vars.items():
                bound = getattr(info.binder, "span", None)
                later = info.kind == "local" and bound is not None and (bound.line, bound.column) > (line, column)
                shown = self.shown(var, name.span) if env.parent is None else var
                if shown is None or shown in seen or shown in ("return", PLACEHOLDER) or later:
                    continue
                seen.add(shown)
                found.append(Completion(shown, name_kind(info, self.checker), describe(shown, info)))
            env = env.parent
        for key, target in self.linker.entry.bindings.items():
            if key not in seen:
                seen.add(key)
                found.append(Completion(key, "namespace", "module") if isinstance(target, Module) else self.item(key))
        for enum, enum_info in self.checker.enums.items():
            shown = self.shown(enum, name.span)
            if shown is not None and shown not in seen:
                found.append(Completion(shown, "struct" if enum_info.struct else "enum"))
        found += [
            Completion(shown, "interface", "constraint")
            for constraint in self.checker.constraints
            if (shown := self.shown(constraint)) is not None
        ]
        return found

    def item(self, key: str) -> Completion:
        """An imported item, by the name the import gives it."""
        target = self.linker.entry.bindings[key]
        assert not isinstance(target, Module)
        kinds = {"enum": "enum", "struct": "struct", "variant": "enumMember", "constraint": "interface"}
        info = self.checker.globals.vars.get(target.name)
        if target.kind in kinds:
            return Completion(key, kinds[target.kind])
        return Completion(
            key, "function" if target.kind == "function" else "variable", describe(key, info) if info else ""
        )

    def members(self, member: Member) -> list[Completion]:
        """The fields and methods of the value before `.`."""
        if isinstance(member.target, Name):
            name_info = self.checker.resolved.get(id(member.target))
            enum_name = (
                name_info.enum
                if name_info is not None and name_info.kind in ("constructor", "variant")
                else member.target.name
                if name_info is None
                else ""
            )
            enum = self.checker.enums.get(enum_name)
            if enum is not None and enum.static_methods:
                return [Completion(name, "method", show_type(method)) for name, method in enum.static_methods.items()]

        t = self.type_of(member.target)
        value_type = resolve(t) if t is not None else None
        match value_type:
            case EnumTy(name, args):
                info = self.checker.enums[name]
                params = dict(zip(info.params, args))
                fields = info.variants.get(name, ()) if info.struct else ()
                found = [
                    Completion(field, "property", show_type(substitute(field_type, params)))
                    for field, field_type in zip(info.field_names, fields)
                ]
                found += [
                    Completion(method, "method", show_type(substitute(method_type, params)))
                    for method, method_type in info.methods.items()
                ]
                assert value_type is not None
                return found + self.extension_members(value_type, {item.label for item in found})
            case ParamTy(_, constraints) as param if (
                isinstance(member.target, Name) and id(member.target) not in self.checker.resolved
            ):
                # `T.`: the constants T's constraints require (a constraint has its parents' too)
                constants = {
                    constant: constant_type
                    for constraint in sorted(constraints)
                    for constant, constant_type in self.checker.constraint_constants.get(constraint, {}).items()
                }
                return [
                    Completion(constant, "constant", show_type(substitute(constant_type, {"Self": param})))
                    for constant, constant_type in constants.items()
                ]
            case ParamTy(_, constraints) as param:
                methods = {
                    method: method_type
                    for constraint in sorted(constraints)
                    for method, method_type in self.checker.constraints.get(constraint, {}).items()
                }
                found = [
                    Completion(method, "method", show_type(substitute(method_type, {"Self": param})))
                    for method, method_type in methods.items()
                ]
                assert value_type is not None
                return found + self.extension_members(value_type, {item.label for item in found})
            case _ if value_type is not None:
                return self.extension_members(value_type, set())
        return []

    def extension_members(self, receiver: Type, seen: set[str]) -> list[Completion]:
        """Receiver-first built-in and standard-library functions applicable to a value."""
        found = []
        names = set(self.checker.extension_functions) | (set(LIBRARY) - HIDDEN_LIBRARY) | {"len"}
        for name in sorted(names - seen):
            if name == "len":
                value_type = resolve(receiver)
                if (
                    isinstance(value_type, ListTy)
                    or value_type in (STR, RANGE)
                    or isinstance(value_type, BuiltinTy)
                    and value_type.name in ("Array", "Pool", "Arena")
                ):
                    found.append(Completion(name, "method"))
                continue
            candidates: list[tuple[FuncTy, tuple[str, ...], tuple[tuple[str, ...], ...]]] = []
            if name in LIBRARY:
                constraints: tuple[tuple[str, ...], ...] = (
                    ((GENERIC_LIBRARY[name],),) if name in GENERIC_LIBRARY else ()
                )
                candidates.append((LIBRARY[name], ("T",) if name in GENERIC_LIBRARY else (), constraints))
            else:
                for function_name in self.checker.extension_functions.get(name, ()):
                    info = self.checker.globals.vars.get(function_name)
                    if info is not None and isinstance(info.type, FuncTy):
                        candidates.append((info.type, info.type_params, info.constraints))
            for function, type_params, constraints in candidates:
                if not isinstance(function, FuncTy) or not function.params:
                    continue
                receiver_pattern = function.params[0]
                type_args: dict[str, Type] = dict(zip(type_params, (self.checker.fresh() for _ in type_params)))
                if type_params:
                    specialized = substitute(function, type_args)
                    assert isinstance(specialized, FuncTy)
                    function = specialized
                if self.checker.can_unify(receiver, function.params[0]):
                    inferred: dict[str, Type] = {}

                    def bind(
                        pattern: Type,
                        value: Type,
                        type_params: tuple[str, ...] = type_params,
                        inferred: dict[str, Type] = inferred,
                    ) -> None:
                        pattern, value = resolve(pattern), resolve(value)
                        if isinstance(pattern, ParamTy) and pattern.name in type_params:
                            inferred[pattern.name] = value
                        elif isinstance(pattern, ListTy) and isinstance(value, ListTy):
                            bind(pattern.element, value.element)
                        elif isinstance(pattern, EnumTy) and isinstance(value, EnumTy) and pattern.name == value.name:
                            for left, right in zip(pattern.args, value.args):
                                bind(left, right)

                    bind(receiver_pattern, receiver)
                    if any(
                        param in inferred
                        and any(self.checker.unmet(inferred[param], need) is not None for need in needs)
                        for param, needs in zip(type_params, constraints)
                    ):
                        continue
                    found.append(Completion(name, "method"))
                    break
        return found


# A local type's name in the linked program (`f#S`, `geo.area#2#S`), which hover text shows as written: `S`.
LOCAL_TYPE = re.compile(r"[\w.]+#(?:\d+#)?(\w+)")


def walk(node: object) -> Iterator[object]:
    """A syntax tree node and every node inside it, parents first."""
    yield node
    for part in parts(node):
        yield from walk(part)


def name_kind(info: VarInfo, checker: Checker) -> str:
    """The semantic token type of a name: what kind of thing it names."""
    match info.kind:
        case "builtin":
            return "function"
        case "variant" | "constructor":
            return "struct" if checker.enums[info.enum].struct else "enumMember"
    if isinstance(info.binder, Param):
        return "parameter"
    return "function" if isinstance(resolve(info.type), FuncTy) else "variable"


def describe(name: str, info: VarInfo) -> str:
    """A name's type, or what it is, for completions."""
    match info.kind:
        case "builtin":
            return show_library(name) if name in LIBRARY else "built-in function"
        case "variant" | "constructor":
            return f"variant of {info.enum}"
    return ("mut " if info.mutable else "") + show_type(info.type)


class Symbols:
    """Finds the symbols of a checked program."""

    def __init__(self, checked: Checked) -> None:
        self.checked = checked
        self.checker = checked.checker
        self.found: dict[tuple[int, int], Symbol] = {}
        # Where the program defines each enum, struct and constraint.
        self.enums: dict[str, EnumDef] = {}
        self.constraints: dict[str, ConstraintDef] = {}
        for node in walk(checked.linked):
            if isinstance(node, EnumDef):
                self.enums.setdefault(node.name, node)
            elif isinstance(node, ConstraintDef):
                self.constraints.setdefault(node.name, node)

    def find(self) -> list[Symbol]:
        program = self.checked.program
        for line in program.imports:
            self.import_line(line.path, line.span)
        for node in walk(program):
            self.named(node)
        # Operators and keyword expressions show their type, where nothing else is.
        for node in walk(program):
            match node:
                case Binary() | Unary() | Compare() | If() | Match() | Loop() | Quote() | Splice() | Try():
                    if (t := self.checked.type_of(node)) is not None:
                        self.add(self.checked.token_at(node.span), show_type(t))
        return sorted(self.found.values(), key=lambda s: (s.where.line, s.where.column))

    def add(
        self,
        token: Token | None,
        hover: str,
        kind: str = "",
        modifiers: tuple[str, ...] = (),
        definition: Location | None = None,
    ) -> None:
        """Add a symbol at a token, unless there is one already."""
        if token is None or token.kind not in (TokenKind.NAME, TokenKind.KEYWORD, TokenKind.OP):
            return
        key = (token.span.line, token.span.column)
        if key not in self.found:
            self.found[key] = Symbol(location(token), LOCAL_TYPE.sub(r"\1", hover), kind, modifiers, definition)

    def show(self, node: object) -> str:
        t = self.checked.type_of(node)
        return "?" if t is None else show_type(t)

    def named(self, node: object) -> None:
        """The symbol for a node with a name, if it has one."""
        checked = self.checked
        match node:
            case Name(name, span) if id(node) in self.checker.resolved:
                info = self.checker.resolved[id(node)]
                kind = "" if name == "return" else name_kind(info, self.checker)  # `return` in `where` is a keyword
                hover = f"{name}: {self.show(node)}"
                if info.kind == "builtin":
                    hover = f"{name}: {show_library(name)}" if name in LIBRARY else f"{name}: built-in function"
                elif info.kind in ("variant", "constructor"):
                    hover = self.variant_text(info.enum, name)
                self.add(checked.token_at(span), hover, kind, self.modifiers(info), self.definition(info, name))
            case Binding(name, mutable, _, span):
                t = self.checked.type_of(node)
                function = t is not None and isinstance(resolve(t), FuncTy)
                params = f"[{', '.join(node.type_params)}]" if node.type_params else ""
                modifiers = ("declaration",) if mutable else ("declaration", "readonly")
                hover = f"let {'mut ' if mutable else ''}{name}{params}: {self.show(node)}"
                self.add(checked.token_at(span), hover, "function" if function else "variable", modifiers)
            case Param(name, _, span, mutable):
                modifiers = ("declaration",) if mutable else ("declaration", "readonly")
                hover = f"{'mut ' if mutable else ''}{name}: {self.show(node)}"
                self.add(checked.token_at(span), hover, "parameter", modifiers)
            case NamePattern(name, span) if self.checker.kinds.get(id(node)) == "variant":
                enum = self.enum_of(node)
                definition = self.variant_location(enum, name)
                self.add(checked.token_at(span), self.variant_text(enum, name), "enumMember", (), definition)
            case NamePattern(name, span, mutable):
                modifiers = ("declaration",) if mutable else ("declaration", "readonly")
                hover = f"{'mut ' if mutable else ''}{name}: {self.show(node)}"
                self.add(checked.token_at(span), hover, "variable", modifiers)
            case VariantPattern(name, _, span):
                enum = self.enum_of(node)
                kind = "struct" if enum in self.checker.enums and self.checker.enums[enum].struct else "enumMember"
                self.add(
                    checked.token_at(span), self.variant_text(enum, name), kind, (), self.variant_location(enum, name)
                )
            case Member(target, name, span):
                self.member(node, target, name, span)
            case NamedType(name, _, span):
                self.type_name(name, span)
            case EnumDef():
                self.definition_symbols(node)
            case ConstraintDef(name, methods, span):
                self.add(checked.after(span), f"constraint {name}", "interface", ("declaration",))
                for method in methods:
                    hover = f"{name}.{method.name}: {self.show_annotation(method)}"
                    self.add(checked.token_at(method.span), hover, "method", ("declaration",))
                for constant in node.constants:
                    assert constant.type is not None
                    hover = f"{name}.{constant.name}: {show(constant.type)}"
                    self.add(checked.token_at(constant.span), hover, "variable", ("declaration", "readonly"))

    def enum_of(self, pattern: object) -> str:
        """The enum a variant's pattern matches."""
        t = self.checked.type_of(pattern)
        owner = resolve(t) if t is not None else None
        return owner.name if isinstance(owner, EnumTy) else ""

    def show_annotation(self, param: Param) -> str:
        return "?" if param.type is None else show(param.type)

    def import_line(self, path: tuple[str, ...], span: Span) -> None:
        """The names in an import line: each module in its path, and an item at its end."""
        i = self.checked.starts.get((span.line, span.column))
        if i is None:
            return
        names = [t for t in self.checked.tokens[i + 1 : i + 2 * len(path)] if t.kind is TokenKind.NAME]
        for n, token in enumerate(names):
            name = ".".join(path[: n + 1])
            module = self.checked.linker.modules.get(name) or self.checked.linker.libraries.get(name)
            if module is not None:
                where = Location(1, 1, 0, self.checked.file_path(module.file))
                self.add(token, f"module {module.name}", "namespace", (), where)

    def modifiers(self, info: VarInfo) -> tuple[str, ...]:
        if info.kind == "builtin" or (info.kind in ("variant", "constructor") and info.enum in BUILTIN_ENUMS):
            return ("defaultLibrary",)
        if in_library(info.binder):
            return ("defaultLibrary", "readonly")
        return () if info.mutable else ("readonly",)

    def definition(self, info: VarInfo, name: str) -> Location | None:
        """Where a name is defined: its `let`, parameter or pattern, or its variant."""
        if info.kind in ("variant", "constructor"):
            return self.variant_location(info.enum, name)
        if isinstance(info.binder, Binding | Param | NamePattern):
            return self.checked.locate(info.binder.span, info.binder.name)
        if isinstance(info.binder, Dim):  # a size name, defined by the dimension of an array that gives it
            return self.checked.locate(info.binder.span, name)
        return None

    def variant_text(self, enum: str, variant: str) -> str:
        info = self.checker.enums.get(enum)
        if info is None:
            return variant
        if info.struct:
            return self.enum_text(enum)
        fields = info.variants.get(variant, ())
        shown = f"({', '.join(map(show_type, fields))})" if fields else ""
        params = f"[{', '.join(info.params)}]" if info.params else ""
        return f"{enum}{params}.{variant}{shown}"

    def variant_location(self, enum: str, variant: str) -> Location | None:
        definition = self.enums.get(enum)
        if definition is None:
            return None
        for v in definition.variants:
            if v.name == variant:
                return self.checked.locate(v.span, v.name)
        return None

    def enum_location(self, enum: str) -> Location | None:
        definition = self.enums.get(enum)
        if definition is None:
            return None
        if definition.span.file:
            return self.checked.locate(definition.variants[0].span if definition.struct else definition.span, enum)
        token = self.checked.after(definition.span)
        return None if token is None else location(token)

    def type_name(self, name: str, span: Span) -> None:
        """A type's name in an annotation."""
        token = self.checked.token_at(span)
        if name in PRIMITIVE_NAMES or name in BUILTIN_TYPES:
            self.add(token, f"{name}: built-in type", "type", ("defaultLibrary",))
        elif name in self.checker.enums:
            info = self.checker.enums[name]
            kind = "struct" if info.struct else "enum"
            library = ("defaultLibrary",) if name in BUILTIN_ENUMS else ()
            self.add(token, self.enum_text(name), kind, library, self.enum_location(name))
        elif name in self.checker.constraints or name == "Self":
            self.add(token, name, "interface")
        else:
            self.add(token, f"{name}: type parameter", "typeParameter")

    def enum_text(self, name: str) -> str:
        info = self.checker.enums[name]
        params = f"[{', '.join(info.params)}]" if info.params else ""
        if info.struct:
            fields = ", ".join(f"{n}: {show_type(t)}" for n, t in zip(info.field_names, info.variants.get(name, ())))
            return f"{'nocopy ' if info.nocopy else ''}struct {name}{params}({fields})"
        variants = " | ".join(v + (f"({', '.join(map(show_type, f))})" if f else "") for v, f in info.variants.items())
        return f"enum {name}{params} = {variants}"

    def definition_symbols(self, enum: EnumDef) -> None:
        """A struct's or enum's name, its variants or fields, and its methods."""
        checked = self.checked
        info = self.checker.enums.get(enum.name)
        kind = "struct" if enum.struct else "enum"
        name_token = checked.token_at(enum.variants[0].span) if enum.struct else checked.after(enum.span)
        hover = self.enum_text(enum.name) if info is not None else enum.name
        self.add(name_token, hover, kind, ("declaration",))
        if enum.struct:
            # A field's name is two tokens before its type, which follows `:`.
            for field, texpr in zip(enum.field_names, enum.variants[0].fields):
                i = checked.starts.get((texpr.span.line, texpr.span.column))
                if i is not None and i >= 2:
                    field_type = self.field_type(enum.name, field)
                    self.add(checked.tokens[i - 2], f"{enum.name}.{field}: {field_type}", "property", ("declaration",))
        else:
            for variant in enum.variants:
                self.add(
                    checked.token_at(variant.span),
                    self.variant_text(enum.name, variant.name),
                    "enumMember",
                    ("declaration",),
                )
        for method in enum.methods:
            t = info.methods.get(method.name) if info is not None else None
            hover = f"{enum.name}.{method.name}: {'?' if t is None else show_type(t)}"
            self.add(checked.token_at(method.span), hover, "method", ("declaration",))
        for implementation in enum.implementations:
            for constant in implementation.constants:
                hover = f"{enum.name}.{constant.name}: {self.show(constant.value)}"
                where = self.constant_location(implementation.constraint, constant.name)
                self.add(checked.token_at(constant.span), hover, "variable", ("declaration", "readonly"), where)

    def field_type(self, enum: str, field: str) -> str:
        info = self.checker.enums.get(enum)
        if info is None or field not in info.field_names:
            return "?"
        return show_type(info.variants[enum][info.field_names.index(field)])

    def member(self, node: Member, target: Expr, name: str, span: Span) -> None:
        """`.name`: a field, a method, or a type parameter's constant, shown at the name."""
        checked = self.checked
        token = checked.after(span)
        t = checked.type_of(target)
        owner = resolve(t) if t is not None else None
        if (constant := self.checker.associated_constants.get(id(node))) is not None:
            param, constraint, _ = constant
            self.add(checked.token_at(target.span), f"{param}: type parameter", "typeParameter")
            hover = f"{param}.{name}: {self.show(node)}"
            self.add(token, hover, "variable", ("readonly",), self.constant_location(constraint, name))
        elif id(node) in self.checker.methods:
            owner_text = owner.name if isinstance(owner, EnumTy) else "?" if owner is None else show_type(owner)
            hover = f"{owner_text}.{name}: {self.show(node)}"
            self.add(token, hover, "method", (), self.method_location(owner, name))
        elif id(node) in self.checker.members and isinstance(owner, EnumTy):
            hover = f"{owner.name}.{name}: {self.show(node)}"
            self.add(token, hover, "property", (), self.field_location(owner.name, name))

    def method_location(self, owner: Type | None, name: str) -> Location | None:
        match owner:
            case EnumTy(enum) if enum in self.enums:
                for method in self.enums[enum].methods:
                    if method.name == name:
                        return self.checked.locate(method.span, name)
            case ParamTy(_, constraints):
                for constraint in sorted(constraints):
                    for param in self.constraints[constraint].methods if constraint in self.constraints else ():
                        if param.name == name:
                            return self.checked.locate(param.span, name)
        return None

    def constant_location(self, constraint: str, name: str) -> Location | None:
        """Where a constraint's constant is declared: in it, or in the parent it comes from."""
        for required in sorted(self.checker.constraint_closure.get(constraint, ())):
            if name in self.checker.constraint_own_constants.get(required, {}) and required in self.constraints:
                for param in self.constraints[required].constants:
                    if param.name == name:
                        return self.checked.locate(param.span, name)
        return None

    def field_location(self, enum: str, field: str) -> Location | None:
        definition = self.enums.get(enum)
        if definition is None or field not in definition.field_names:
            return None
        texpr = definition.variants[0].fields[definition.field_names.index(field)]
        if texpr.span.file:  # the field's name is at the start of its line, before its type
            return Location(
                texpr.span.line,
                texpr.span.column - len(field) - 2,
                len(field),
                self.checked.file_path(texpr.span.file),
            )
        i = self.checked.starts.get((texpr.span.line, texpr.span.column))
        return None if i is None or i < 2 else location(self.checked.tokens[i - 2])


def location(token: Token) -> Location:
    return Location(token.span.line, token.span.column, len(token.text))


def in_library(node: object) -> bool:
    """Whether a syntax tree node (a name's binder, say) is in the standard library."""
    span = getattr(node, "span", None)
    return isinstance(span, Span) and span.file.startswith(STD)


def is_library(program: Program) -> bool:
    """Whether a file seems to be a module of a program rather than its entry file: it has no
    `main`, and nothing at the top level but definitions."""
    for expr in program.body:
        match expr:
            case Let(bindings) if any(b.name == "main" for b in bindings):
                return False
            case Let() | LetPattern() | EnumDef() | ConstraintDef():
                continue
            case _:
                return False
    return True
