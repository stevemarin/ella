"""Ella: a statically typed language for learning and fun."""

import argparse
import sys
from pathlib import Path

from ella.checker import check
from ella.interpreter import Interpreter
from ella.ir import format_module
from ella.ir_interpreter import IRInterpreter
from ella.lexer import EllaError, tokenize
from ella.lower import compile_program
from ella.parser import parse
from ella.syntax import show


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: run a file, or show what a stage of the compiler makes of it."""
    args = argparse.ArgumentParser(prog="ella", description="Run an Ella program.")
    args.add_argument("file", type=Path)
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--tokens", action="store_true", help="print the tokens")
    mode.add_argument("--ast", action="store_true", help="print the syntax tree")
    mode.add_argument("--ir", action="store_true", help="print the IR")
    mode.add_argument("--tree", action="store_true", help="run with the tree-walking interpreter instead of the IR")
    options = args.parse_args(argv)

    source = options.file.read_text(encoding="utf-8")
    try:
        if options.tokens:
            for token in tokenize(source):
                print(f"{token.span}\t{token.kind.name}\t{token.text!r}")
        elif options.ast:
            for expr in parse(source).body:
                print(show(expr))
        elif options.tree:
            program = parse(source)
            check(program)
            Interpreter().run(program)
        else:
            module = compile_program(source)
            if options.ir:
                print(format_module(module), end="")
            else:
                IRInterpreter(module).run()
    except EllaError as error:
        # Flush what the program printed first, so the error comes after it.
        sys.stdout.flush()
        print(f"{options.file}:{error}", file=sys.stderr)
        return 1
    return 0
