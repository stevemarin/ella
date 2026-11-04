"""Ella: a statically typed language for learning and fun."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ella.checker import Checker
from ella.codegen import generate
from ella.interpreter import Interpreter
from ella.ir import Module, format_module, format_placement
from ella.ir_interpreter import IRInterpreter
from ella.lexer import STD, EllaError, Span, tokenize
from ella.library import Host
from ella.lower import compile_program
from ella.modules import link, package_path
from ella.native import build, finish_run
from ella.parser import parse
from ella.passes import PASSES, optimize
from ella.staging import has_splices, prepare
from ella.syntax import Import, Program, show
from ella.values import MAX_CALL_DEPTH


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: run a file, or show what a stage of the compiler makes of it."""
    args = argparse.ArgumentParser(prog="ella", description="Run an Ella program.")
    args.add_argument("file", type=Path)
    mode = args.add_mutually_exclusive_group()
    mode.add_argument("--tokens", action="store_true", help="print the tokens")
    mode.add_argument("--ast", action="store_true", help="print the syntax tree")
    mode.add_argument(
        "--expanded",
        action="store_true",
        help="print the syntax tree after running the splices outside quotes (of each module that has some)",
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
    args.add_argument(
        "--path",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help="find packages in this directory too (before those of ELLA_PATH; may be given more than once)",
    )
    args.add_argument("-O", "--optimize", action="store_true", help="optimize the IR before running or printing it")
    args.add_argument(
        "--max-depth",
        type=int,
        default=MAX_CALL_DEPTH,
        help=f"how deeply functions may call each other (default {MAX_CALL_DEPTH})",
    )
    args.add_argument(
        "--passes",
        help=f"run these optimization passes, in order, separated by commas ({', '.join(PASSES)})",
    )
    # Everything after `--` is for the program: `ella prog.ella -- a b`.
    argv = sys.argv[1:] if argv is None else argv
    program_args = argv[argv.index("--") + 1 :] if "--" in argv else []
    options = args.parse_args(argv[: argv.index("--")] if "--" in argv else argv)
    if options.max_depth < 1:
        args.error("--max-depth must be at least 1")
    host = Host(args=program_args)
    passes = None
    if options.passes is not None:
        passes = [name.strip() for name in options.passes.split(",") if name.strip()]
        unknown = [name for name in passes if name not in PASSES]
        if unknown:
            args.error(f"unknown pass {unknown[0]!r}; the passes are {', '.join(PASSES)}")

    source = options.file.read_text(encoding="utf-8")
    packages = package_path(options.path)
    status = 0
    try:
        if options.tokens:
            for token in tokenize(source):
                print(f"{token.span}\t{token.kind.name}\t{token.text!r}")
        elif options.ast:
            program = parse(source)
            for line in program.imports:
                print(show_import(line))
            for expr in program.body:
                print(show(expr))
        elif options.expanded:
            linked = link(parse(source), options.file, packages)
            show_expanded(options.file, linked, prepare(linked)[0])
        elif options.tree:
            program, checker = prepare(parse(source), path=options.file, packages=packages)
            warn(options.file, checker)
            status = Interpreter(checker=checker, host=host, max_depth=options.max_depth).run(program)
        else:
            module = compile_program(source, path=options.file, packages=packages)
            warn(options.file, module.checker)
            if options.optimize or passes is not None:
                optimize(module, passes)
            if options.ir:
                print(format_module(module), end="")
            elif options.placement:
                print(format_placement(module), end="")
            elif options.asm:
                print(generate(module, options.max_depth), end="")
            elif options.output is not None:
                build(module, options.output, options.max_depth)
            elif options.native:
                status = run_executable(module, program_args, options.max_depth)
            elif options.profile:
                interpreter = IRInterpreter(module, host=host, max_depth=options.max_depth)
                try:
                    status = interpreter.run()
                finally:
                    sys.stdout.flush()
                    print(interpreter.format_profile(), end="", file=sys.stderr)
            else:
                status = IRInterpreter(module, host=host, max_depth=options.max_depth).run()
    except EllaError as error:
        # Flush what the program printed first, so the error comes after it.
        sys.stdout.flush()
        print(error.format(lambda span: where(options.file, span)), file=sys.stderr)
        return 1
    return status


def warn(file: Path, checker: Checker) -> None:
    """Print the checker's warnings, which don't stop the program."""
    for warning in checker.warnings:
        print(f"{where(file, warning.span)}: warning: {warning.message}", file=sys.stderr)


def where(file: Path, span: Span) -> str:
    """Where an error is: `prog.ella:3:5`, `geo/poly.ella:3:5` in a module (from the entry file's
    directory, as the entry file was given, which a package's module is too), or `<std>/maps.ella:12:5`
    in the standard library."""
    if not span.file:
        return f"{file}:{span}"
    if span.file.startswith(STD):
        return str(span)
    return f"{Path(os.path.normpath(file.parent / span.file)).as_posix()}:{span.line}:{span.column}"


def show_import(line: Import) -> str:
    return f"(import {'.'.join(line.path)}{f' as {line.alias}' if line.alias else ''})"


def show_expanded(entry: Path, linked: Program, expanded: Program) -> None:
    """The program with its splices expanded: the modules that have splices (the entry file's, at least).

    With more than one module, each comes after a comment line with its file.
    """

    def shown(file: str) -> bool:
        code = tuple(expr for expr in linked.body if expr.span.file == file)
        return not file or not file.startswith(STD) and has_splices(code)

    files = [file for file, _ in expanded.modules if shown(file)]
    for file in files:
        if len(files) > 1:
            print(f"# {(entry.parent / file).as_posix() if file else entry}")
        for expr in expanded.body:
            if expr.span.file == file:
                print(show(expr))


def run_executable(module: Module, program_args: list[str], max_depth: int) -> int:
    """Build a native executable in a temporary directory, and run it with the program's arguments.

    It reads this process's standard input, and its output is passed on. Returns its exit status.
    """
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "program"
        build(module, executable, max_depth)
        sys.stdout.flush()
        result = subprocess.run([str(executable), *program_args], check=False, capture_output=True)
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.flush()
    return finish_run(result.returncode, result.stderr.decode("utf-8"), sys.stderr)
