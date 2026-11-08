"""Print a source file's outline: each class, function and method with its lines and the first line of its docstring.

    uv run python tools/outline.py src/ella/checker.py            # everything
    uv run python tools/outline.py src/ella/checker.py Checker    # only these classes' or functions' parts
    uv run python tools/outline.py src/ella/runtime.c              # a C file: its functions and section comments

Meant for reading a big file's shape before reading the parts that matter (`Read` with an offset).
"""

import ast
import re
import sys
from pathlib import Path


def outline_python(source: str, only: set[str]) -> list[str]:
    lines: list[str] = []

    def walk(body: list[ast.stmt], depth: int, shown: bool) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                show = shown or not only or node.name in only
                if show:
                    doc = ast.get_docstring(node)
                    first = doc.splitlines()[0] if doc else ""
                    lines.append(f"{'  ' * depth}{node.lineno}-{node.end_lineno} {node.name}  {first}".rstrip())
                if isinstance(node, ast.ClassDef):
                    walk(node.body, depth + 1, show)

    walk(ast.parse(source).body, 0, False)
    return lines


C_FUNCTION = re.compile(r"^[A-Za-z_][\w \*]*?\b(\w+)\(.*\)\s*\{\s*$")
C_SECTION = re.compile(r"^// (\S.*)$")


def outline_c(source: str, only: set[str]) -> list[str]:
    """Functions defined at the top level, and the first line of each comment block before one."""
    lines: list[str] = []
    comment = ""
    for number, line in enumerate(source.splitlines(), 1):
        if match := C_SECTION.match(line):
            comment = comment or match.group(1)
        elif match := C_FUNCTION.match(line):
            if not only or match.group(1) in only:
                lines.append(f"{number} {match.group(1)}  {comment}".rstrip())
            comment = ""
        elif line.strip() == "" or not line.startswith(" "):
            comment = "" if line.strip() else comment
    return lines


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    only = set(sys.argv[2:])
    source = path.read_text()
    print("\n".join(outline_c(source, only) if path.suffix in (".c", ".h") else outline_python(source, only)))


if __name__ == "__main__":
    main()
