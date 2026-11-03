import io
import shutil
from pathlib import Path

import pytest

from ella import main

EXAMPLE = Path(__file__).parent.parent / "example_syntax.ella"


def write(tmp_path: Path, source: str) -> str:
    path = tmp_path / "program.ella"
    path.write_text(source)
    return str(path)


def test_runs_a_program(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([write(tmp_path, 'print("hi {{1 + 2}}")\n')]) == 0
    assert capsys.readouterr().out == "hi 3\n"


def test_prints_tree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--ast", write(tmp_path, "let x = 1 + 2\n")]) == 0
    assert capsys.readouterr().out == "(let [x] (+ 1 2))\n"


def test_prints_tokens(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--tokens", write(tmp_path, "x")]) == 0
    assert capsys.readouterr().out.splitlines() == ["1:1\tNAME\t'x'", "1:2\tNEWLINE\t''", "1:2\tEOF\t''"]


def test_reports_syntax_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "let = 1\n")
    assert main([path]) == 1
    assert capsys.readouterr().err == f"{path}:1:5: expected a name, found '='\n"


def test_reports_runtime_errors_after_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, 'print("before")\nassert 1 == 2\n')
    assert main([path]) == 1
    captured = capsys.readouterr()
    assert captured.out == "before\n"
    assert captured.err == f"{path}:2:8: assertion failed: 1 == 2\n"


def test_runs_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(EXAMPLE)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "done"


def test_prints_ir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--ir", write(tmp_path, "print(1 + 2)\n")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("function top() -> ()\n")
    assert "add %" in out and "print %" in out


def test_runs_with_tree_interpreter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--tree", write(tmp_path, 'print("hi {{1 + 2}}")\n')]) == 0
    assert capsys.readouterr().out == "hi 3\n"


@pytest.mark.parametrize("mode", [[], ["--tree"], ["--ir"]])
def test_reports_type_errors_before_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: list[str]
) -> None:
    path = write(tmp_path, 'print("never printed")\nlet x = 1 + true\n')
    assert main([*mode, path]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{path}:2:11: '+' needs two values of the same type, not i64 and bool\n"


def test_optimizes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "print(6 * 7)\n")
    assert main(["-O", path]) == 0
    assert capsys.readouterr().out == "42\n"
    assert main(["--ir", "-O", path]) == 0
    out = capsys.readouterr().out
    assert "const 42" in out and "mul" not in out


def test_chooses_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "let f = (a: i64) -> i64 => a + 1\n")
    assert main(["--ir", "--passes", "mem2reg", path]) == 0
    out = capsys.readouterr().out
    assert "load" not in out and "store" not in out


def test_rejects_unknown_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--passes", "mem2reg,nope", write(tmp_path, "1\n")])
    assert "unknown pass 'nope'" in capsys.readouterr().err


def test_prints_placement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = "let f = () -> [i64] =>\n    let mut xs = []\n    for i in 0..3\n        xs += [i]\n    xs\nprint(f())\n"
    assert main(["--placement", write(tmp_path, source)]) == 0
    out = capsys.readouterr().out
    assert "function f\n" in out
    assert "append    @result  [i64]  <- in a loop (level 1), but outlives the iteration" in out


def test_prints_profile_after_running(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--profile", write(tmp_path, 'let xs = [1, 2]\nprint("{{xs}}")\n')]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[1, 2]\n"
    assert captured.err.startswith("Arena profile")
    assert "global" in captured.err and "top level 0" in captured.err


def test_prints_expanded_program(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = """\
let power = (n: i64, x: Code[i64]) -> Code[i64] => if n == 0 then quote 1 else quote $x * $(power(n - 1, x))
let square = (x: i64) -> i64 => $(power(2, quote x))
"""
    assert main(["--expanded", write(tmp_path, source)]) == 0
    assert "(let [square] (lambda (x: i64) -> i64 (* x (* x 1))))" in capsys.readouterr().out


def test_program_arguments_follow_two_dashes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "print(args())\n")
    for mode in (["--tree"], [], ["--native"] if shutil.which("gcc") else []):
        assert main([*mode, path, "--", "a b", "--tree"]) == 0
        assert capsys.readouterr().out == '["a b", "--tree"]\n'
    assert main([path]) == 0
    assert capsys.readouterr().out == "[]\n"


def test_reads_standard_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, "print(read_line())\nprint(read_line())\n")
    for mode in (["--tree"], []):
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO("héllo\r\n".encode())))
        assert main([*mode, path]) == 0
        assert capsys.readouterr().out == 'Some("héllo")\nNone\n'


def test_max_depth(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, "let down = (n: i64) -> i64 => if n == 0 then 0 else 1 + down(n - 1)\nprint(down(50))\n")
    for mode in (["--tree"], [], ["--native"] if shutil.which("gcc") else []):
        assert main([*mode, path]) == 0
        assert capsys.readouterr().out == "50\n"
        assert main([*mode, "--max-depth", "20", path]) == 1
        assert capsys.readouterr().err == f"{path}:1:61: stack overflow: more than 20 nested calls\n"
