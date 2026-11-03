"""What an editor needs to know about a program: its errors, what each name in it is, and what could be typed next.

The language server (ella.lsp) runs these functions in a worker process, so
they take source text and give plain data: `analyze` gives the errors and
the symbols, `complete` the completions at a position. Positions are lines
and columns from 1, counting characters, as in spans.

Unlike `ella FILE`, which stops at the first error, an analysis keeps going:

* The lexer stops at its first error, so `tokenize_leniently` patches the
  text and tries again: it closes unclosed brackets and strings at the end
  of their line, and blanks out a stray `)` or bad character. The patches
  never move the program's own text, so positions stay right.
* The parser makes `()` of each line with an error (`parse_partial`).
* The checker records the error of each top-level line, enum or function
  body, and goes on (`Checker.errors`). Errors in a top-level line the
  parser had trouble with aren't shown: they likely follow from the parse error.

Splices outside quotes are expanded only when everything else is right, as
`prepare` would, to find errors in the code they make.

A *symbol* is a name in the source (or an operator), with what the checker
knows of it: text to show on hover (its type), where it is defined, and its
kind for highlighting (a semantic token type, as LSP names them). Each comes
from a syntax tree node, at the token its span points to.
"""

import io
import re
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from ella.checker import (
    LIBRARY,
    PRIMITIVE_NAMES,
    Checker,
    EnumTy,
    Env,
    FuncTy,
    ParamTy,
    Type,
    VarInfo,
    check,
    resolve,
    show_library,
    show_type,
    substitute,
)
from ella.lexer import KEYWORDS, EllaError, Span, Token, TokenKind, tokenize
from ella.parser import parse_partial
from ella.prelude import in_prelude, with_prelude
from ella.staging import Expander, has_splices, parts, uses_staging
from ella.syntax import (
    Binary,
    Binding,
    Compare,
    ConstraintDef,
    EnumDef,
    Expr,
    If,
    Loop,
    Match,
    Member,
    Name,
    NamedType,
    NamePattern,
    Param,
    Quote,
    Splice,
    Try,
    Unary,
    VariantPattern,
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
    line: int
    column: int
    length: int


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


def analyze(source: str) -> Analysis:
    """The errors in a program, and its symbols."""
    try:
        checked = Checked(source, expand=True)
    except EllaError as error:
        return Analysis([problem(error)], None)
    except RecursionError:
        return Analysis([Problem("this program is nested too deeply to check", 1, 1)], None)
    return Analysis(checked.problems, Symbols(checked).find())


def complete(source: str, line: int, column: int) -> list[Completion]:
    """What could be typed at `line` and `column`: the names in scope there, or the fields and methods after `.`."""
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
        checked = Checked("\n".join(lines), expand=False)
    except EllaError, RecursionError:
        return keywords()
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
    for a, b, size in matcher.get_matching_blocks():
        for i in range(1, size + 1):
            moved[a + i] = b + i
    result = []
    for symbol in symbols:
        if symbol.where.line not in moved:
            continue
        where = replace(symbol.where, line=moved[symbol.where.line])
        target = symbol.definition
        definition = None if target is None or target.line not in moved else replace(target, line=moved[target.line])
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

    def __init__(self, source: str, expand: bool) -> None:
        self.tokens, source, lex_errors = tokenize_leniently(source)
        self.starts = {(token.span.line, token.span.column): i for i, token in enumerate(self.tokens)}
        self.program, parse_errors = parse_partial(self.tokens, source)
        self.checker = Checker()
        self.checker.errors = []
        # Every prelude definition is checked, so that completions offer them.
        program = with_prelude(self.program, every=True)
        expander = Expander(program, self.checker, io.StringIO()) if uses_staging(program) else None
        self.checker.check_program(program)

        # Errors in a top-level line with a parse error likely follow from it.
        lines = self.top_level_lines()
        broken = {bisect_right(lines, error.span.line) for error in parse_errors}
        errors = lex_errors + parse_errors
        errors += [error for error in self.checker.errors if bisect_right(lines, error.span.line) not in broken]
        if expand and not errors and expander is not None and has_splices(self.program):
            try:
                check(expander.expand_program())
            except EllaError as error:
                errors.append(error)
        problems = [problem(error) for error in errors]
        problems += [problem(warning, True) for warning in self.checker.warnings]
        self.problems = sorted(dict.fromkeys(problems), key=lambda p: (p.line, p.column))

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
                if var in seen or var in ("return", PLACEHOLDER) or later:
                    continue
                seen.add(var)
                found.append(Completion(var, name_kind(info, self.checker), describe(var, info)))
            env = env.parent
        for enum in self.checker.enums:
            if enum not in seen:
                found.append(Completion(enum, "struct" if self.checker.enums[enum].struct else "enum"))
        found += [Completion(constraint, "interface", "constraint") for constraint in self.checker.constraints]
        return found

    def members(self, member: Member) -> list[Completion]:
        """The fields and methods of the value before `.`."""
        t = self.type_of(member.target)
        match resolve(t) if t is not None else None:
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
                return found
            case ParamTy(_, constraints) as param:
                return [
                    Completion(method, "method", show_type(substitute(method_type, {"Self": param})))
                    for constraint in sorted(constraints)
                    for method, method_type in self.checker.constraints.get(constraint, {}).items()
                ]
        return []


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
        for node in walk(checked.program):
            if isinstance(node, EnumDef):
                self.enums.setdefault(node.name, node)
            elif isinstance(node, ConstraintDef):
                self.constraints.setdefault(node.name, node)

    def find(self) -> list[Symbol]:
        program = self.checked.program
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
            self.found[key] = Symbol(location(token), hover, kind, modifiers, definition)

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

    def enum_of(self, pattern: object) -> str:
        """The enum a variant's pattern matches."""
        t = self.checked.type_of(pattern)
        owner = resolve(t) if t is not None else None
        return owner.name if isinstance(owner, EnumTy) else ""

    def show_annotation(self, param: Param) -> str:
        return "?" if param.type is None else show(param.type)

    def modifiers(self, info: VarInfo) -> tuple[str, ...]:
        if info.kind == "builtin" or (info.kind in ("variant", "constructor") and info.enum in BUILTIN_ENUMS):
            return ("defaultLibrary",)
        if in_prelude(info.binder):
            return ("defaultLibrary", "readonly")
        return () if info.mutable else ("readonly",)

    def definition(self, info: VarInfo, name: str) -> Location | None:
        """Where a name is defined: its `let`, parameter or pattern, or its variant."""
        if info.kind in ("variant", "constructor"):
            return self.variant_location(info.enum, name)
        if isinstance(info.binder, Binding | Param | NamePattern) and not in_prelude(info.binder):
            token = self.checked.token_at(info.binder.span)
            return None if token is None else location(token)
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
                token = self.checked.token_at(v.span)
                return None if token is None else location(token)
        return None

    def enum_location(self, enum: str) -> Location | None:
        definition = self.enums.get(enum)
        if definition is None:
            return None
        token = self.checked.after(definition.span)
        return None if token is None else location(token)

    def type_name(self, name: str, span: Span) -> None:
        """A type's name in an annotation."""
        token = self.checked.token_at(span)
        if name in PRIMITIVE_NAMES or name in ("Code", "Pool", "Handle", "Type"):
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

    def field_type(self, enum: str, field: str) -> str:
        info = self.checker.enums.get(enum)
        if info is None or field not in info.field_names:
            return "?"
        return show_type(info.variants[enum][info.field_names.index(field)])

    def member(self, node: Member, target: Expr, name: str, span: Span) -> None:
        """`.name`: a field or a method, shown at the name."""
        checked = self.checked
        token = checked.after(span)
        t = checked.type_of(target)
        owner = resolve(t) if t is not None else None
        if id(node) in self.checker.methods:
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
                        token = self.checked.token_at(method.span)
                        return None if token is None else location(token)
            case ParamTy(_, constraints):
                for constraint in sorted(constraints):
                    for param in self.constraints[constraint].methods if constraint in self.constraints else ():
                        if param.name == name:
                            token = self.checked.token_at(param.span)
                            return None if token is None else location(token)
        return None

    def field_location(self, enum: str, field: str) -> Location | None:
        definition = self.enums.get(enum)
        if definition is None or field not in definition.field_names:
            return None
        texpr = definition.variants[0].fields[definition.field_names.index(field)]
        i = self.checked.starts.get((texpr.span.line, texpr.span.column))
        return None if i is None or i < 2 else location(self.checked.tokens[i - 2])


def location(token: Token) -> Location:
    return Location(token.span.line, token.span.column, len(token.text))
