"""Ella: a statically typed language for learning and fun."""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from ella.codegen import generate
from ella.interpreter import Interpreter
from ella.ir import Module, format_module, format_placement
from ella.ir_interpreter import IRInterpreter
from ella.lexer import EllaError, tokenize
from ella.lower import compile_program
from ella.native import build, parse_error
from ella.parser import parse
from ella.passes import PASSES, optimize
from ella.staging import prepare
from ella.syntax import show


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: run a file, or show what a stage of the compiler makes of it."""
    args = argparse.ArgumentParser(prog="ella", description="Run an Ella program.")
    args.add_argument("file", type=Path)
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--tokens", action="store_true", help="print the tokens")
    mode.add_argument("--ast", action="store_true", help="print the syntax tree")
    mode.add_argument(
        "--expanded", action="store_true", help="print the syntax tree after running the splices outside quotes"
    )
    mode.add_argument("--ir", action="store_true", help="print the IR")
    mode.add_argument("--asm", action="store_true", help="print the x64 assembly")
    mode.add_argument("--placement", action="store_true", help="print which arena each allocation goes to")
    mode.add_argument(
        "--profile", action="store_true", help="run through the IR, then print how much memory each arena used"
    )
    mode.add_argument("--tree", action="store_true", help="run with the tree-walking interpreter instead of the IR")
    mode.add_argument("--native", action="store_true", help="compile to a native executable and run it")
    mode.add_argument("-o", "--output", type=Path, help="compile to a native executable with this name")
    args.add_argument("-O", "--optimize", action="store_true", help="optimize the IR before running or printing it")
    args.add_argument(
        "--passes",
        help=f"run these optimization passes, in order, separated by commas ({', '.join(PASSES)})",
    )
    options = args.parse_args(argv)
    passes = None
    if options.passes is not None:
        passes = [name.strip() for name in options.passes.split(",") if name.strip()]
        unknown = [name for name in passes if name not in PASSES]
        if unknown:
            args.error(f"unknown pass {unknown[0]!r}; the passes are {', '.join(PASSES)}")

    source = options.file.read_text(encoding="utf-8")
    try:
        if options.tokens:
            for token in tokenize(source):
                print(f"{token.span}\t{token.kind.name}\t{token.text!r}")
        elif options.ast:
            for expr in parse(source).body:
                print(show(expr))
        elif options.expanded:
            program, _ = prepare(parse(source))
            for expr in program.body:
                print(show(expr))
        elif options.tree:
            program = parse(source)
            program, checker = prepare(program)
            Interpreter(checker=checker).run(program)
        else:
            module = compile_program(source)
            if options.optimize or passes is not None:
                optimize(module, passes)
            if options.ir:
                print(format_module(module), end="")
            elif options.placement:
                print(format_placement(module), end="")
            elif options.asm:
                print(generate(module), end="")
            elif options.output is not None:
                build(module, options.output)
            elif options.native:
                run_executable(module)
            elif options.profile:
                interpreter = IRInterpreter(module)
                try:
                    interpreter.run()
                finally:
                    sys.stdout.flush()
                    print(interpreter.format_profile(), end="", file=sys.stderr)
            else:
                IRInterpreter(module).run()
    except EllaError as error:
        # Flush what the program printed first, so the error comes after it.
        sys.stdout.flush()
        print(f"{options.file}:{error}", file=sys.stderr)
        return 1
    return 0


def run_executable(module: Module) -> None:
    """Build a native executable in a temporary directory, run it, and pass on its output."""
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "program"
        build(module, executable)
        result = subprocess.run([str(executable)], check=False, capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        raise parse_error(result.stderr)
