from pathlib import Path

import pytest

from ella.analysis import Location, Symbol, analyze, complete, follow_edits
from ella.modules import LIBRARY_DIR

ROOT = Path(__file__).parent.parent
PROGRAMS = sorted((ROOT / "test" / "programs").glob("*.ella"))


def problems(source: str) -> list[str]:
    return [f"{p.line}:{p.column}: {p.message}" for p in analyze(source).problems]


def symbols(source: str) -> dict[tuple[int, int], Symbol]:
    found = analyze(source).symbols
    assert found is not None
    return {(s.where.line, s.where.column): s for s in found}


def names(source: str, line: int, column: int) -> list[str]:
    return [c.label for c in complete(source, line, column) if c.kind != "keyword"]


@pytest.mark.parametrize("path", [ROOT / "example_syntax.ella", *PROGRAMS], ids=lambda p: p.name)
def test_working_programs_have_no_problems(path: Path) -> None:
    analysis = analyze(path.read_text())
    assert analysis.problems == []
    assert analysis.symbols


def test_every_error_is_found() -> None:
    source = 'let x = 1 + true\nlet f = (n: i64) -> i64 =>\n    print(n + "a")\n    nope\nprint(f(1))\n'
    assert problems(source) == [
        "1:11: '+' needs two values of the same type, not i64 and bool",
        "3:13: '+' needs two values of the same type, not i64 and str",
        "4:5: undefined name 'nope'",
    ]


def test_errors_after_a_parse_error_in_the_same_line_are_not_shown() -> None:
    # The `let y` line can't be read, so `y` is missing below it, but that only follows from the parse error.
    source = "let f = (n: i64) -> i64 =>\n    let y = n *\n    y\nprint(f(1) + true)\n"
    assert problems(source) == [
        "2:15: a line can't end with '*'; wrap the expression in parentheses",
        "4:12: '+' needs two values of the same type, not i64 and bool",
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # An unclosed bracket is closed at the end of its line.
        ("print(1\nprint(nope)\n", ["1:6: '(' is never closed", "2:7: undefined name 'nope'"]),
        # So is an unclosed string.
        (
            'print("abc\nprint(nope)\n',
            [
                "1:6: '(' is never closed",
                "1:11: line break in a single-line string; use \\n or a triple-quoted string",
                "2:7: undefined name 'nope'",
            ],
        ),
        # A stray bracket or character is ignored.
        (
            "let x = 2 ) + true\n",
            ["1:11: unmatched ')'", "1:13: '+' needs two values of the same type, not i64 and bool"],
        ),
        ("let y = 1 @ 2\n", ["1:11: unexpected character '@'", "1:13: expected end of line, found '2'"]),
    ],
)
def test_lexer_errors_are_patched_over(source: str, expected: list[str]) -> None:
    assert problems(source) == expected


def test_unfixable_text_gives_no_symbols() -> None:
    analysis = analyze("let x = 1\n  let y = 2\n let z = 3\n")
    assert [p.message for p in analysis.problems] == ["this line's indentation doesn't match any enclosing block"]
    assert analysis.symbols is None


def test_compile_time_output_isnt_printed(capsys: pytest.CaptureFixture[str]) -> None:
    assert problems('let x = $\n    print("compiling")\n    quote 1\nprint(x)\n') == []
    assert capsys.readouterr().out == ""


def test_expanded_code_is_checked() -> None:
    # Code fits anything, so the error only shows once the splice has made its code (from the quote, as in `ella`).
    source = 'let make = () -> Code => quote "a"\nlet f = (_x: i64) -> i64 => $(make())\nprint(f(1))\n'
    assert problems(source) == ["1:32: the function's result should be i64, not str"]


PROGRAM = """struct Point
    x: f64
    y: f64
    norm2 = (p: Point) -> f64 => p.x * p.x + p.y * p.y

let first[T] = (xs: [T]) -> T => xs[0]
let mut total = 0.0
for p in [Point(3.0, 4.0)]
    total += p.norm2()
match Some(first([1]))
    case Some(n) => print(n)
    case None => print(0)
"""


def test_symbols_say_what_names_are() -> None:
    found = symbols(PROGRAM)

    def show(line: int, column: int) -> tuple[str, str, tuple[str, ...], tuple[int, int] | None]:
        symbol = found[line, column]
        target = symbol.definition
        return symbol.hover, symbol.kind, symbol.modifiers, None if target is None else (target.line, target.column)

    assert show(1, 8) == ("struct Point(x: f64, y: f64)", "struct", ("declaration",), None)
    assert show(2, 5) == ("Point.x: f64", "property", ("declaration",), None)
    assert show(4, 5) == ("Point.norm2: (Point) -> f64", "method", ("declaration",), None)
    assert show(4, 17) == ("struct Point(x: f64, y: f64)", "struct", (), (1, 8))  # in an annotation
    assert show(4, 36) == ("Point.x: f64", "property", (), (2, 5))  # p.x
    assert show(6, 5) == ("let first[T]: ([T]) -> T", "function", ("declaration", "readonly"), None)
    assert show(6, 22) == ("T: type parameter", "typeParameter", (), None)
    assert show(7, 9) == ("let mut total: f64", "variable", ("declaration",), None)
    assert show(8, 5) == ("p: Point", "variable", ("declaration", "readonly"), None)
    assert show(9, 5) == ("total: f64", "variable", (), (7, 9))
    assert show(9, 16) == ("Point.norm2: (Point) -> f64", "method", (), (4, 5))
    assert show(10, 7) == ("Optional[T].Some(T)", "enumMember", ("defaultLibrary",), None)
    assert show(10, 12) == ("first: ([i64]) -> i64", "function", ("readonly",), (6, 5))
    assert show(11, 10) == ("Optional[T].Some(T)", "enumMember", (), None)
    assert show(11, 15) == ("n: i64", "variable", ("declaration", "readonly"), None)
    assert show(11, 27) == ("n: i64", "variable", ("readonly",), (11, 15))
    assert show(10, 1) == ("()", "", (), None)  # `match` shows its type
    assert (1, 1) not in found  # `struct` isn't a symbol


def test_completing_names_in_scope() -> None:
    source = "let f = (n: i64) -> i64 =>\n    let a = 1\n    a\n    let later = 2\n    n\n"
    found = names(source, 3, 6)
    assert found[:3] == ["a", "n", "f"]
    assert "later" not in found  # not bound yet
    assert "print" in found and "Optional" in found


def test_completing_with_nothing_typed_yet() -> None:
    source = "let count = 1\nprint()\n"
    assert "count" in names(source, 2, 7)


def test_completing_fields_and_methods() -> None:
    source = PROGRAM + "let p = Point(1.0, 2.0)\nprint(p.\n"
    assert [(c.label, c.kind, c.detail) for c in complete(source, 14, 9)] == [
        ("x", "property", "f64"),
        ("y", "property", "f64"),
        ("norm2", "method", "(Point) -> f64"),
        ("hash", "method", ""),
    ]


def test_completing_type_namespace_and_receiver_functions() -> None:
    source = "struct Stack[T]\n    items: [T]\n    new = () -> Stack[T] => Stack([])\nprint(Stack.\n"
    assert names(source, 4, 13) == ["new"]
    assert {"map", "len"} <= set(names("let xs = [1, 2]\nprint(xs.\n", 2, 10))
    assert {"trim", "upper"} <= set(names('print("text".\n', 1, 14))


def test_no_completions_in_strings_or_comments() -> None:
    assert complete('print("abc")\n', 1, 10) == []
    assert complete("# some words\n", 1, 8) == []


def test_follow_edits_moves_symbols_with_their_lines() -> None:
    old = "let a = 1\nlet b = a\n"
    symbol = Symbol(Location(2, 9, 1), "a: i64", "variable", (), Location(1, 5, 1))
    other = Symbol(Location(1, 5, 1), "let a: i64")
    new = "# comment\nlet a = 1\nlet b = a\n"
    assert follow_edits([symbol, other], old, new) == [
        Symbol(Location(3, 9, 1), "a: i64", "variable", (), Location(2, 5, 1)),
        Symbol(Location(2, 5, 1), "let a: i64"),
    ]
    # A changed line loses its symbols.
    assert follow_edits([symbol, other], old, "let a = 1\nlet b = a + 1\n") == [other]


def test_hover_on_library_functions_and_let_patterns() -> None:
    found = symbols('let (a, b) = (1, "x")\nprint(split(b, ","))\nlet c = a\n')
    assert found[(1, 6)].hover == "a: i64"
    assert found[(1, 9)].hover == "b: str"
    assert found[(2, 7)].hover == "split: (str, str) -> [str]"
    assert found[(3, 9)].hover == "a: i64"
    assert found[(3, 9)].definition == Location(1, 6, 1)


def test_hover_on_generic_library_functions() -> None:
    found = symbols("let f = abs\nprint(min(1, 2))\n")
    assert found[(1, 9)].hover == "abs: [T: Num] (T) -> T"
    assert found[(2, 7)].hover == "min: [T: Num] (T, T) -> T"


def test_cases_that_never_match_are_warnings() -> None:
    found = analyze("match true\n    case _ => 1\n    case true => 2\n").problems
    assert [(p.line, p.column, p.warning) for p in found] == [(3, 5, True)]


def test_errors_in_a_let_pattern_leave_its_names_defined() -> None:
    assert problems("let (a, b) = nope\nprint(a)\nprint(c)\n") == [
        "1:14: undefined name 'nope'",
        "3:7: undefined name 'c'",
    ]


def test_prelude_functions() -> None:
    found = symbols("let xs = sort([2, 1])\nprint(xs, pi)\n")
    assert found[(1, 10)].hover == "lists.sort: ([i64]) -> [i64]"  # the prelude's names say their module
    assert found[(1, 10)].modifiers == ("defaultLibrary", "readonly")
    definition = found[(1, 10)].definition
    assert definition is not None and definition.file == str(LIBRARY_DIR / "lists.ella")
    line = (LIBRARY_DIR / "lists.ella").read_text().splitlines()[definition.line - 1]
    assert line[definition.column - 1 :].startswith("sort[T: Ord] =")
    assert found[(2, 11)].hover == "math.pi: f64"
    assert {"map", "filter", "sort", "reverse", "pi"} <= set(names("let x = 1\nprint()\n", 2, 7))
    assert problems("let x = 1\n") == []  # nothing from the prelude, though all of it is checked


def test_inferred_lambdas_and_new_warnings() -> None:
    source = "let ys = map([1], (n) => n * 2)\nlet zs = map(ys, Some)\nlet f = (x: i64) -> i64 => 1\nprint(zs)\n"
    found = symbols(source)
    assert found[(1, 20)].hover == "n: i64"
    assert found[(2, 10)].hover == "lists.map: ([i64], (i64) -> Optional[i64]) -> [Optional[i64]]"
    assert problems(source) == [
        "3:5: function 'f' is never used",
        "3:10: parameter 'x' is never used; start its name with '_' if that is on purpose",
    ]
    assert "new_map" in names("let x = 1\nprint()\n", 2, 7)


def test_mut_names_in_patterns() -> None:
    found = symbols("let (mut a, b) = (1, 2)\na += b\n")
    assert found[(1, 10)].hover == "mut a: i64"
    assert found[(1, 10)].modifiers == ("declaration",)
    assert found[(1, 13)].modifiers == ("declaration", "readonly")


def test_unused_variables_and_unreachable_code_are_warnings() -> None:
    found = analyze("let f = () -> i64 =>\n    let a = 1\n    return 2\n    3\nf()\n").problems
    assert [(p.line, p.column, p.warning) for p in found] == [(2, 9, True), (4, 5, True)]
