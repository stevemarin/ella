"""Ella: a statically typed language for learning and fun."""

import argparse
import sys
from pathlib import Path

from ella.lexer import EllaError, tokenize
from ella.parser import parse
from ella.syntax import show


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. For now it shows the tokens or syntax tree of a file."""
    args = argparse.ArgumentParser(prog="ella", description="Parse an Ella file and print its syntax tree.")
    args.add_argument("file", type=Path)
    args.add_argument("--tokens", action="store_true", help="print the tokens instead of the syntax tree")
    options = args.parse_args(argv)

    source = options.file.read_text(encoding="utf-8")
    try:
        if options.tokens:
            for token in tokenize(source):
                print(f"{token.span}\t{token.kind.name}\t{token.text!r}")
        else:
            for expr in parse(source).body:
                print(show(expr))
    except EllaError as error:
        print(f"{options.file}:{error}", file=sys.stderr)
        return 1
    return 0
