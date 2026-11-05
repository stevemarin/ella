"""Modules and imports (docs/grammar.md §3): linking a program's files, and the rules for names across them."""

import io
from pathlib import Path

import pytest

from ella import main
from ella.analysis import Place, analyze, complete
from ella.interpreter import Interpreter
from ella.lexer import EllaError
from ella.lower import compile_program
from ella.modules import LIBRARY_DIR, link, package_path
from ella.parser import parse
from ella.staging import prepare
from ella.syntax import Import, Lambda, Let, Match, NamedType, NamePattern, VariantPattern


def write(root: Path, files: dict[str, str]) -> Path:
    """Write a program's files under `root`; the entry file is `main.ella`."""
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root / "main.ella"


def run(root: Path, files: dict[str, str]) -> str:
    """Check and run a program of several files, giving what it prints."""
    path = write(root, files)
    out = io.StringIO()
    program, checker = prepare(parse(path.read_text()), out, path)
    Interpreter(out, checker).run(program)
    return out.getvalue()


def error(root: Path, files: dict[str, str]) -> str:
    """The error a program of several files stops with, while checking it."""
    path = write(root, files)
    with pytest.raises(EllaError) as info:
        prepare(parse(path.read_text()), io.StringIO(), path)
    return str(info.value)


def warnings(root: Path, files: dict[str, str]) -> list[str]:
    path = write(root, files)
    _, checker = prepare(parse(path.read_text()), io.StringIO(), path)
    return [str(warning) for warning in checker.warnings]


GEO = """\
enum Shape
    Circle(f64)
    Square(f64)

struct Point
    x: f64
    y: f64

let area = (s: Shape) -> f64 =>
    match s
        case Circle(r) => 3.0 * r * r
        case Square(side) => side * side

let mut count = 0
let _helper = () -> i64 => 42
"""


# Parsing


def test_imports_come_first() -> None:
    program = parse("import geo\nimport geo.poly as p\nprint(1)\n")
    assert program.imports == (
        Import(("geo",), None, program.imports[0].span),
        Import(("geo", "poly"), "p", program.imports[1].span),
    )
    with pytest.raises(EllaError, match="'import' lines must come first in a file"):
        parse("print(1)\nimport geo\n")


def test_paths_in_types_and_patterns() -> None:
    let = parse("let f[T: geo.Sized] = (p: geo.Point) -> geo.Pair[T] => p\n").body[0]
    assert isinstance(let, Let) and isinstance(let.value, Lambda)
    assert let.bindings[0].constraints == (("geo.Sized",),)
    param_type, result_type = let.value.params[0].type, let.value.return_type
    assert isinstance(param_type, NamedType) and param_type.name == "geo.Point"
    assert isinstance(result_type, NamedType) and result_type.name == "geo.Pair"
    match_ = parse("match s\n    case geo.Circle(r) => r\n    case geo.Empty => 0.0\n").body[0]
    assert isinstance(match_, Match)
    circle, empty = (case.pattern for case in match_.cases)
    assert isinstance(circle, VariantPattern) and circle.name == "geo.Circle"
    assert isinstance(empty, NamePattern) and empty.name == "geo.Empty"


# Importing


def test_module_names_go_with_the_whole_import_path(tmp_path: Path) -> None:
    files = {
        "geo.ella": GEO,
        "geo/poly.ella": "let sides = (n: i64) -> i64 => n\n",
        "main.ella": "import geo\nimport geo.poly\nimport geo.poly as p\n"
        "print(geo.area(geo.Circle(1.0)), geo.poly.sides(3), p.sides(4))\n",
    }
    assert run(tmp_path, files) == "3.0 3 4\n"


def test_importing_a_module_below_doesnt_import_the_one_above(tmp_path: Path) -> None:
    files = {"geo.ella": GEO, "geo/poly.ella": "let n = 1\n", "main.ella": "import geo.poly\nprint(geo.area)\n"}
    assert error(tmp_path, files).endswith("'geo' is part of the name of the module 'geo.poly', not a value")


def test_importing_an_item(tmp_path: Path) -> None:
    files = {
        "geo.ella": GEO,
        "main.ella": """\
import geo.Shape
import geo.Point as P
import geo.area as size

let shapes: [geo.Shape] = [geo.Circle(1.0), geo.Square(2.0)]
for s in shapes
    match s
        case geo.Circle(r) => print("circle", size(s))
        case geo.Square(_) => print("square", size(s))
print(P(1.0, 2.0))
""",
    }
    assert run(tmp_path, files) == "circle 3.0\nsquare 4.0\nPoint(1.0, 2.0)\n"


def test_an_enum_imported_with_as_brings_bare_variants(tmp_path: Path) -> None:
    files = {
        "geo.ella": GEO,
        "main.ella": "import geo.Shape as Shape\nlet s: Shape = Square(3.0)\nmatch s\n"
        "    case Circle(_) => print(0)\n    case Square(side) => print(side)\n",
    }
    assert run(tmp_path, files) == "3.0\n"


def test_types_and_constraints_from_modules(tmp_path: Path) -> None:
    files = {
        "sized.ella": "constraint Sized\n    size: (Self) -> i64\n\nstruct Box\n    n: i64\n    size = (b: Box) -> i64 => b.n\n",
        "main.ella": "import sized\nlet total[T: sized.Sized] = (xs: [T]) -> i64 =>\n"
        "    let mut n = 0\n    for x in xs\n        n += x.size()\n    n\nprint(total([sized.Box(2), sized.Box(3)]))\n",
    }
    assert run(tmp_path, files) == "5\n"


def test_methods_come_with_their_type(tmp_path: Path) -> None:
    files = {
        "points.ella": "struct Point\n    x: i64\n    norm = (p: Point) -> i64 => p.x * p.x\n"
        "let origin = () -> Point => Point(3)\n",
        "uses.ella": "import points\nlet start = () -> points.Point => points.origin()\n",
        "main.ella": "import uses\nprint(uses.start().norm())\n",  # main doesn't import points
    }
    assert run(tmp_path, files) == "9\n"


def test_type_namespace_functions_are_imported_with_their_type(tmp_path: Path) -> None:
    files = {
        "stack.ella": "struct Stack[T]\n    items: [T]\n    new = () -> Stack[T] => Stack([])\n",
        "main.ella": "import stack\nlet value: stack.Stack[i64] = stack.Stack.new()\nprint(value.items)\n",
    }
    assert run(tmp_path, files) == "[]\n"


def test_each_module_runs_once_and_imports_first(tmp_path: Path) -> None:
    files = {
        "log.ella": 'print("log")\nlet lines = ["first"]\n',
        "a.ella": 'import log\nprint("a", log.lines)\n',
        "b.ella": 'import log\nprint("b")\n',
        "main.ella": 'import a\nimport b\nprint("main")\n',
    }
    assert run(tmp_path, files) == 'log\na ["first"]\nb\nmain\n'


def test_main_only_runs_in_the_entry_file(tmp_path: Path) -> None:
    files = {
        "tool.ella": 'let main = () -> () => print("tool")\n',
        "main.ella": 'import tool\nlet main = () -> () =>\n    print("program")\n    tool.main()\n',
    }
    assert run(tmp_path, files) == "program\ntool\n"


def test_library_modules_can_be_imported(tmp_path: Path) -> None:
    files = {
        "main.ella": "import maps\nimport lists.reverse as backwards\n"
        "let mut m = maps.new_map()\nm[1] = 2\nprint(len(m), backwards([1, 2]))\n"
    }
    assert run(tmp_path, files) == "1 [2, 1]\n"


def test_literals_mean_the_librarys_maps_and_sets(tmp_path: Path) -> None:
    # Whatever the program calls map_of, set_of or Map, and in a module that imports nothing.
    files = {
        "tool.ella": "let pairs = () -> i64 => len({1: 2, 3: 4})\n",
        "main.ella": "import tool\nlet map_of = 1\nlet set_of = 2\nprint(len({'a': 1}), {3, 3}.items(), tool.pairs(), map_of)\n",
    }
    assert run(tmp_path, files) == "1 [3] 2 1\n"


# The names a module sees


def test_a_modules_code_never_sees_the_entry_files_names(tmp_path: Path) -> None:
    files = {
        "tool.ella": "let count = (xs: [i64]) -> i64 => len(xs)\n",
        # The entry file's own len, and a variant named like a variable in tool's code.
        "main.ella": "import tool\nlet len = 5\nenum E\n    xs\nprint(tool.count([1, 2]), len, xs)\n",
    }
    assert run(tmp_path, files) == "2 5 xs\n"
    files = {"tool.ella": "let f = () -> i64 => helper()\n", "main.ella": "import tool\nlet helper = () -> i64 => 1\n"}
    assert error(tmp_path, files) == "tool.ella:1:22: undefined name 'helper'"


def test_local_variables_hide_imports(tmp_path: Path) -> None:
    files = {
        "geo.ella": GEO,
        "main.ella": "import geo\nlet f = () -> f64 =>\n    let geo = geo.Point(1.0, 2.0)\n    geo.y\nprint(f())\n",
    }
    assert run(tmp_path, files) == "2.0\n"


def test_a_programs_names_can_be_its_modules_names(tmp_path: Path) -> None:
    files = {"geo.ella": GEO, "main.ella": "import geo\nlet area = 1\nprint(area, geo.area(geo.Square(2.0)))\n"}
    assert run(tmp_path, files) == "1 4.0\n"


def test_a_module_named_like_a_library_module(tmp_path: Path) -> None:
    files = {
        "lists.ella": "let map = (n: i64) -> i64 => n + 1\n",
        "main.ella": "import lists\nprint(lists.map(1), map([1], (n) => n * 10))\n",  # the prelude's map is still there
    }
    assert run(tmp_path, files) == "2 [10]\n"
    assert warnings(tmp_path, files) == [
        "1:1: 'lists.ella' has the name of a standard library module, and is used instead of it"
    ]


# Errors


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"main.ella": "import nowhere\n"}, "1:1: there is no module 'nowhere'"),
        ({"geo.ella": GEO, "main.ella": "import geo.nothing\n"}, "1:1: module 'geo' has no 'nothing'"),
        ({"geo.ella": GEO, "main.ella": "import geo\nprint(geo.nothing)\n"}, "2:11: module 'geo' has no 'nothing'"),
        ({"geo.ella": GEO, "main.ella": "import geo\nprint(geo)\n"}, "2:7: 'geo' is a module, not a value"),
        ({"geo.ella": GEO, "main.ella": "import geo\nimport geo\n"}, "2:1: 'geo' is imported twice"),
        ({"geo.ella": GEO, "main.ella": "import geo\nlet geo = 1\n"}, "1:1: 'geo' is imported, and defined here too"),
        ({"geo.ella": GEO, "main.ella": "import geo\nlet s = geo.Shape\n"}, "2:13: 'Shape' is a type, not a value"),
        ({"geo.ella": GEO, "main.ella": "import geo\nlet p: geo.area = 1\n"}, "2:8: 'geo.area' isn't a type"),
        (
            {"a.ella": "import b\n", "b.ella": "import c\n", "c.ella": "import a\n", "main.ella": "import a\n"},
            "c.ella:1:1: these imports form a cycle: a -> b -> c -> a",
        ),
        (
            {"tool.ella": "import main\n", "main.ella": "import tool\n"},
            "tool.ella:1:1: these imports form a cycle: main -> tool -> main",
        ),
    ],
)
def test_import_errors(tmp_path: Path, files: dict[str, str], message: str) -> None:
    assert error(tmp_path, files) == message


def test_errors_in_modules_give_their_file(tmp_path: Path) -> None:
    files = {"geo/poly.ella": "let f = () -> i64 => 'x'\n", "main.ella": "import geo.poly\nprint(geo.poly.f())\n"}
    assert error(tmp_path, files) == "geo/poly.ella:1:22: the function's result should be i64, not str"
    files = {"geo/poly.ella": "let f = (\n", "main.ella": "import geo.poly\n"}
    assert error(tmp_path, files).startswith("geo/poly.ella:")


# Private names, and other modules' variables


def test_private_names(tmp_path: Path) -> None:
    files = {"geo.ella": GEO, "main.ella": "import geo\nprint(geo._helper())\n"}
    assert error(tmp_path, files) == "2:11: '_helper' is private to module 'geo'"
    files = {"geo.ella": GEO, "main.ella": "import geo._helper as h\n"}
    assert error(tmp_path, files) == "1:1: '_helper' is private to module 'geo'"
    # Modules below geo can use them, and siblings can't.
    files = {
        "geo.ella": GEO,
        "geo/inner.ella": "import geo\nlet f = () -> i64 => geo._helper()\n",
        "geo/inner/deeper.ella": "import geo\nlet g = () -> i64 => geo._helper()\n",
        "main.ella": "import geo.inner\nimport geo.inner.deeper\nprint(geo.inner.f(), geo.inner.deeper.g())\n",
    }
    assert run(tmp_path, files) == "42 42\n"
    files = {
        "geo.ella": GEO,
        "geometry.ella": "import geo\nlet f = () -> i64 => geo._helper()\n",
        "main.ella": "import geometry\n",
    }
    assert error(tmp_path, files) == "geometry.ella:2:26: '_helper' is private to module 'geo'"


STACK = """\
struct Stack
    _items: [i64]
    push = (mut s: Stack, n: i64) -> () =>
        s._items += [n]
    _top = (s: Stack) -> i64 => s._items[len(s._items) - 1]
    top = (s: Stack) -> i64 => s._top()

let empty = () -> Stack => Stack([])
"""


def test_private_fields_and_methods(tmp_path: Path) -> None:
    uses = "import stack\nlet mut s = stack.empty()\nmut s.push(4)\n"
    assert run(tmp_path, {"stack.ella": STACK, "main.ella": uses + "print(s.top(), s)\n"}) == "4 Stack([4])\n"
    for line, message in [
        ("print(s._items)", "4:8: '_items' is private to module 'stack'"),
        ("print(s._top())", "4:8: '_top' is private to module 'stack'"),
        (
            "print(stack.Stack([1]))",
            "4:13: Stack has private fields, so only module 'stack' can make one or take one apart",
        ),
        (
            "match s\n    case stack.Stack(items) => print(items)",
            "5:10: Stack has private fields, so only module 'stack' can make one or take one apart",
        ),
    ]:
        assert error(tmp_path, {"stack.ella": STACK, "main.ella": uses + line + "\n"}) == message


def test_other_modules_variables_are_read_only(tmp_path: Path) -> None:
    files = {"geo.ella": GEO, "main.ella": "import geo\nprint(geo.count)\n"}
    assert run(tmp_path, files) == "0\n"
    message = "2:5: 'geo.count' is a variable of module 'geo', so it can't be changed here"
    assert error(tmp_path, {"geo.ella": GEO, "main.ella": "import geo\ngeo.count += 1\n"}) == message
    files = {
        "geo.ella": "let mut xs = [1]\n",
        "main.ella": "import geo\nlet add = (mut ys: [i64]) -> () =>\n    ys += [2]\nadd(mut geo.xs)\n",
    }
    assert error(tmp_path, files) == "4:13: 'geo.xs' is a variable of module 'geo', so it can't be changed here"


# Warnings


def test_unused_imports_and_functions(tmp_path: Path) -> None:
    files = {
        "tool.ella": "let used = () -> i64 => _inner()\nlet _inner = () -> i64 => 1\nlet _unused = () -> i64 => 2\n"
        "let public = () -> i64 => 3\n",
        "other.ella": "let x = 1\n",
        "main.ella": "import tool\nimport other\nprint(tool.used())\n",
    }
    # A module's public functions may be used by other modules, so only private ones are reported.
    assert warnings(tmp_path, files) == [
        "2:1: 'other' is imported but never used",
        "tool.ella:3:5: function '_unused' is never used",
    ]


def test_the_library_gives_no_warnings() -> None:
    for path in LIBRARY_DIR.glob("*.ella"):
        _, checker = prepare(parse(f"import {path.stem}\nprint({path.stem}.{first_public(path)})\n"))
        assert checker.warnings == [], path


def first_public(path: Path) -> str:
    return next(
        line.split()[1].split("[")[0]
        for line in path.read_text().splitlines()
        if line.startswith("let ") and line[4] != "_"
    )


# Linking


def test_linked_programs_have_every_modules_code_in_order(tmp_path: Path) -> None:
    path = write(tmp_path, {"geo.ella": GEO, "main.ella": "import geo\nprint(geo.area(geo.Circle(1.0)), pi)\n"})
    linked = link(parse(path.read_text()), path)
    assert linked.modules == (("geo.ella", "geo"), ("<std>/math.ella", "math"), ("", "main"))
    assert link(linked, path) is linked


def test_unused_functions_of_modules_arent_lowered(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        {
            "geo.ella": GEO,
            "main.ella": "import geo\nlet unused = () -> () => ()\nprint(geo.area(geo.Circle(1.0)), range(0, 4, 2))\n",
        },
    )
    functions = set(compile_program(path.read_text(), path=path).functions)
    assert {"geo.area", "lists.range", "unused"} <= functions  # the entry file's are all there
    assert "geo._helper" not in functions


def test_quotes_mean_what_they_mean_where_they_are_written(tmp_path: Path) -> None:
    files = {
        "gen.ella": "let code = () -> Code[i64] => quote _secret() + 1\nlet _secret = () -> i64 => 41\n",
        "main.ella": "import gen\nlet _secret = () -> i64 => 0\nprint($(gen.code()), run(gen.code()), _secret())\n",
    }
    assert run(tmp_path, files) == "42 42 0\n"


# The command line


def test_cli_errors_name_the_modules_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(
        tmp_path,
        {"geo/poly.ella": "let f = () -> i64 => 1 // 0\n", "main.ella": "import geo.poly\nprint(geo.poly.f())\n"},
    )
    assert main([str(path)]) == 1
    assert capsys.readouterr().err == (
        f"{(tmp_path / 'geo/poly.ella').as_posix()}:1:24: integer division by zero\n  called at {path}:2:17\n"
    )


def test_cli_shows_each_module_with_splices(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = {
        "gen.ella": "let one = () -> Code[i64] => quote 1\nlet two = () -> i64 => $(one()) + 1\n",
        "plain.ella": "let three = 3\n",
        "main.ella": "import gen\nimport plain\nprint($(gen.one()), gen.two(), plain.three)\n",
    }
    path = write(tmp_path, files)
    assert main(["--expanded", str(path)]) == 0
    assert capsys.readouterr().out == (
        f"# {(tmp_path / 'gen.ella').as_posix()}\n"
        "(let [gen.one] (lambda () -> Code[i64] (quote 1)))\n"
        "(let [gen.two] (lambda () -> i64 (+ 1 1)))\n"
        f"# {path}\n"
        "(call print 1 (call gen.two) plain.three)\n"
    )
    assert main(["--ast", str(path)]) == 0
    assert capsys.readouterr().out.startswith("(import gen)\n(import plain)\n")


def test_cli_finds_packages_in_the_given_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "libs", JSON)
    write(tmp_path / "more", {"extra.ella": "let f = () -> i64 => 1 // 0\n"})
    path = write(tmp_path / "app", {"main.ella": "import json\nimport extra\nprint(json.count('a,b'), extra.f())\n"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ELLA_PATH", "more")
    assert main(["--path", "libs", "app/main.ella"]) == 1
    assert capsys.readouterr() == (
        "",
        "more/extra.ella:1:24: integer division by zero\n  called at app/main.ella:3:33\n",
    )
    assert main(["--native", "--path", str(tmp_path / "libs"), str(path)]) == 1
    assert capsys.readouterr().err.startswith(f"{tmp_path}/more/extra.ella:1:24: integer division by zero\n")


# Packages

JSON = {
    "json.ella": "import json.lex\nlet count = (s: str) -> i64 => len(json.lex.tokens(s))\n",
    "json/lex.ella": "let tokens = (s: str) -> [str] => split(s, _sep)\nlet _sep = ','\n",
}


def test_packages_are_found_after_the_programs_modules(tmp_path: Path) -> None:
    write(tmp_path / "libs", JSON)
    write(tmp_path / "other", {"json.ella": "let count = (s: str) -> i64 => 0\n", "tool.ella": "let n = 7\n"})
    path = write(tmp_path / "app", {"main.ella": "import json\nimport tool\nprint(json.count('a,b,c'), tool.n)\n"})
    out = io.StringIO()
    program, checker = prepare(parse(path.read_text()), out, path, [tmp_path / "libs", tmp_path / "other"])
    Interpreter(out, checker).run(program)
    assert out.getvalue() == "3 7\n"
    assert ("../libs/json/lex.ella", "json.lex") in program.modules
    module = compile_program(path.read_text(), path=path, packages=[tmp_path / "libs", tmp_path / "other"])
    assert "json.count" in module.functions
    write(tmp_path / "app", {"json.ella": "let count = (s: str) -> i64 => 9\n"})  # the program's own comes first
    out = io.StringIO()
    Interpreter(out).run(link(parse(path.read_text()), path, [tmp_path / "libs", tmp_path / "other"]))
    assert out.getvalue() == "9 7\n"


def test_the_package_path_comes_from_ella_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "libs", JSON)
    write(tmp_path / "other", {"lists.ella": "let three = 3\n"})
    monkeypatch.setenv("ELLA_PATH", f"{tmp_path / 'nothing'}:{tmp_path / 'libs'}::{tmp_path / 'other'}")
    assert package_path([Path("first")]) == (
        Path("first"),
        tmp_path / "nothing",
        tmp_path / "libs",
        tmp_path / "other",
    )
    path = write(tmp_path / "app", {"main.ella": "import json\nimport lists\nprint(json.count('a'), lists.three)\n"})
    out = io.StringIO()
    program, checker = prepare(parse(path.read_text()), out, path)
    Interpreter(out, checker).run(program)
    assert out.getvalue() == "1 3\n"
    assert [str(w) for w in checker.warnings] == [
        "2:1: 'lists.ella' has the name of a standard library module, and is used instead of it"
    ]


def test_a_packages_private_names_stay_in_it(tmp_path: Path) -> None:
    write(tmp_path / "libs", JSON)
    path = write(tmp_path / "app", {"main.ella": "import json.lex\nprint(json.lex._sep)\n"})
    with pytest.raises(EllaError, match="2:16: '_sep' is private to module 'json.lex'"):
        prepare(parse(path.read_text()), io.StringIO(), path, [tmp_path / "libs"])
    path = write(tmp_path / "app", {"main.ella": "import yaml\n"})
    with pytest.raises(EllaError, match="1:1: there is no module 'yaml'"):
        prepare(parse(path.read_text()), io.StringIO(), path, [tmp_path / "libs"])


# Editors


def test_analysis_shows_errors_of_imports_at_the_import_line(tmp_path: Path) -> None:
    path = write(tmp_path, {"geo/poly.ella": "let f = () -> i64 => 'x'\n", "main.ella": "print(1)\nimport geo.poly\n"})
    analysis = analyze(path.read_text(), Place(str(path)))
    assert [(p.message, p.line) for p in analysis.problems] == [
        ("'import' lines must come first in a file, before any other line", 2)
    ]
    analysis = analyze("import geo.poly\nprint(geo.poly.f())\n", Place(str(path)))
    assert [(p.message, p.line, p.column) for p in analysis.problems] == [
        ("geo/poly.ella:1:22: the function's result should be i64, not str", 1, 1)
    ]


def test_analysis_reads_open_files_first(tmp_path: Path) -> None:
    path = write(tmp_path, {"tool.ella": "let f = () -> i64 => 1\n", "main.ella": ""})
    source = "import tool\nprint(tool.f())\n"
    assert analyze(source, Place(str(path))).problems == []
    edited = {str(tmp_path / "tool.ella"): "let g = () -> i64 => 1\n"}  # not saved yet
    problems = analyze(source, Place(str(path), files=edited)).problems
    assert [p.message for p in problems] == ["module 'tool' has no 'f'"]


def test_analysis_finds_definitions_in_other_files(tmp_path: Path) -> None:
    path = write(tmp_path, {"geo.ella": GEO, "main.ella": ""})
    found = {
        (s.where.line, s.where.column): s
        for s in analyze("import geo\nprint(geo.area(geo.Circle(1.0)))\n", Place(str(path))).symbols or []
    }
    assert found[(2, 11)].hover == "geo.area: (geo.Shape) -> f64"
    assert found[(2, 11)].definition is not None and found[(2, 11)].definition.file == str(tmp_path / "geo.ella")
    assert (found[(2, 11)].definition.line, found[(2, 11)].definition.column) == (9, 5)
    assert found[(1, 8)].hover == "module geo"
    assert found[(1, 8)].kind == "namespace"


def test_analysis_completes_a_modules_names(tmp_path: Path) -> None:
    path = write(tmp_path, {"geo.ella": GEO, "main.ella": ""})
    found = complete("import geo\nprint(geo.)\n", 2, 11, Place(str(path)))
    assert {c.label for c in found} == {"Shape", "Circle", "Square", "Point", "area", "count"}  # not _helper
    names = {c.label for c in complete("import geo\nprint()\n", 2, 7, Place(str(path)))}
    assert {"geo", "map", "Map", "pi"} <= names
    assert not any("." in name for name in names)


def test_analysis_of_a_module_doesnt_report_unused_public_functions(tmp_path: Path) -> None:
    problems = analyze(
        "let f = () -> i64 => 1\nlet _g = () -> i64 => 2\n", Place(str(tmp_path / "tool.ella"))
    ).problems
    assert [p.message for p in problems] == ["function '_g' is never used"]


def test_analysis_finds_the_root_between_the_file_and_the_workspace_folder(tmp_path: Path) -> None:
    path = write(
        tmp_path / "program",
        {
            "geo.ella": "import geo.units\nlet area = () -> f64 => geo.units.scale(2.0)\n",
            "geo/units.ella": "let scale = (x: f64) -> f64 => x * 2.0\n",
            "geo/poly.ella": "import geo\nlet f = () -> f64 => geo.area()\n",
            "main.ella": "import geo.poly\nprint(geo.poly.f())\n",
        },
    )
    for file in [path, path.parent / "geo.ella", path.parent / "geo" / "poly.ella"]:
        assert analyze(file.read_text(), Place(str(file), str(tmp_path))).problems == []


def test_analysis_finds_packages_on_ella_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "libs", {**JSON, "bad.ella": "let f = () -> i64 => 'x'\n"})
    monkeypatch.setenv("ELLA_PATH", str(tmp_path / "libs"))
    path = write(tmp_path / "app", {"main.ella": ""})
    found = {
        (s.where.line, s.where.column): s
        for s in analyze("import json\nprint(json.count('a'))\n", Place(str(path), str(tmp_path / "app"))).symbols
        or []
    }
    assert found[(2, 12)].definition is not None
    assert Path(found[(2, 12)].definition.file).resolve() == tmp_path / "libs" / "json.ella"
    problems = analyze("import bad\nprint(bad.f())\n", Place(str(path), str(tmp_path / "app"))).problems
    assert [(p.message, p.line) for p in problems] == [
        ("../libs/bad.ella:1:22: the function's result should be i64, not str", 1)
    ]
