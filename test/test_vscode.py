"""Checks that the VS Code extension in editors/vscode fits the language."""

import json
import re
from pathlib import Path
from typing import Any

from ella.checker import BUILTINS, REFLECTION
from ella.lexer import KEYWORDS
from ella.modules import prelude

EXTENSION = Path(__file__).parent.parent / "editors" / "vscode"


def load(name: str) -> Any:
    return json.loads((EXTENSION / name).read_text())


def test_files_named_in_the_manifest_exist() -> None:
    manifest = load("package.json")
    contributes = manifest["contributes"]
    paths = [manifest["main"]]
    paths += [language["configuration"] for language in contributes["languages"]]
    paths += [grammar["path"] for grammar in contributes["grammars"]]
    for path in paths:
        load(path) if path.endswith(".json") else (EXTENSION / path).read_text()


def test_grammar_highlights_every_keyword() -> None:
    grammar = load("syntaxes/ella.tmLanguage.json")
    words: set[str] = set()
    for rule in grammar["repository"]["keyword"]["patterns"]:
        found = re.fullmatch(r"\\b\(\?:([a-z|]+)\)\\b", rule["match"])
        assert found, rule["match"]
        words |= set(found.group(1).split("|"))
    assert words == KEYWORDS


def test_grammar_highlights_every_built_in_function() -> None:
    grammar = json.dumps(load("syntaxes/ella.tmLanguage.json"))
    found = re.search(r"\\\\b\(\?:((?:[a-z_0-9]+\|)+[a-z_0-9]+)\)\(\?=", grammar)
    assert found, "no rule for built-in functions"
    assert set(found.group(1).split("|")) == {
        *BUILTINS,
        *REFLECTION,
        *(name for name, item in prelude().items() if item.kind == "function"),
    }
