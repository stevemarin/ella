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
