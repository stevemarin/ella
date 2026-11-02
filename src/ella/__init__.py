"""Ella: a statically typed language for learning and fun."""

import argparse
import sys
from pathlib import Path

from ella.interpreter import run
from ella.lexer import EllaError, tokenize
from ella.parser import parse
from ella.syntax import show


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: run a file, or show its tokens or syntax tree."""
    args = argparse.ArgumentParser(prog="ella", description="Run an Ella program.")
    args.add_argument("file", type=Path)
    shown = args.add_mutually_exclusive_group()
    shown.add_argument("--tokens", action="store_true", help="print the tokens instead of running")
    shown.add_argument("--ast", action="store_true", help="print the syntax tree instead of running")
    options = args.parse_args(argv)

    source = options.file.read_text(encoding="utf-8")
    try:
        if options.tokens:
            for token in tokenize(source):
                print(f"{token.span}\t{token.kind.name}\t{token.text!r}")
        elif options.ast:
            for expr in parse(source).body:
                print(show(expr))
        else:
            run(source)
    except EllaError as error:
        # Flush what the program printed first, so the error comes after it.
        sys.stdout.flush()
        print(f"{options.file}:{error}", file=sys.stderr)
        return 1
    return 0
