"""The prelude: library functions written in Ella, such as `map` and `sort`.

Functions that take other functions, or work on any type, are simplest to
write in Ella itself: every backend then runs the same code, with the same
memory rules. A program gets a prelude definition as if it had written it
at its start, when it uses the name and doesn't define it at the top level
itself (a program may use these names for its own functions, as it may the
library's). So a program that uses none of them is unchanged.

The prelude's code must not depend on the program's names: it uses no
built-ins (a program may define its own `len`), and binds no names in
patterns (a program may have a variant with the name). Each definition
stands alone, and none of them gives a warning.
"""

from ella.checker import defined_names, names_used
from ella.parser import parse
from ella.syntax import Expr, Lambda, Let, Program

SOURCE = """\
let pi = 3.141592653589793

let reverse[T] = (xs: [T]) -> [T] =>
    let mut result: [T] = []
    let mut i = 0
    for _ in xs
        i += 1
    while i > 0
        i -= 1
        result += [xs[i]]
    result

let map[T, U] = (xs: [T], f: (T) -> U) -> [U] =>
    let mut result: [U] = []
    let mut i = 0
    for _ in xs
        result += [f(xs[i])]
        i += 1
    result

let filter[T] = (xs: [T], keep: (T) -> bool) -> [T] =>
    let mut result: [T] = []
    let mut i = 0
    for _ in xs
        if keep(xs[i]) then result += [xs[i]]
        i += 1
    result

# A merge sort, which is stable and compares items with `<` only: runs of
# 1, 2, 4, ... items are merged in pairs, left to right.
let sort[T: Ord] = (xs: [T]) -> [T] =>
    let mut n = 0
    for _ in xs
        n += 1
    let mut items = xs
    let mut width = 1
    while width < n
        let mut merged: [T] = []
        let mut start = 0
        while start < n
            let middle = if start + width < n then start + width else n
            let stop = if middle + width < n then middle + width else n
            let mut i = start
            let mut j = middle
            while i < middle and j < stop
                if items[j] < items[i] then
                    merged += [items[j]]
                    j += 1
                else
                    merged += [items[i]]
                    i += 1
            merged += items[i..middle]
            merged += items[j..stop]
            start = stop
        items = merged
        width *= 2
    items
"""

DEFINITIONS: tuple[Expr, ...] = parse(SOURCE).body
NAMES: dict[str, Let] = {
    binding.name: expr for expr in DEFINITIONS if isinstance(expr, Let) for binding in expr.bindings
}
FUNCTIONS = frozenset(name for name, let in NAMES.items() if isinstance(let.value, Lambda))
BINDINGS = frozenset(id(binding) for expr in NAMES.values() for binding in expr.bindings)


def with_prelude(program: Program, every: bool = False) -> Program:
    """The program with the prelude definitions it uses (or `every` one), ahead of its own code.

    Adding them again changes nothing, since the program then defines them.
    """
    defined = {name for expr in program.body for name in defined_names(expr)}
    used = names_used(program.body, inside_lambdas=True)
    added = [let for name, let in NAMES.items() if name not in defined and (every or name in used)]
    return Program((*added, *program.body), program.span) if added else program


def in_prelude(binder: object) -> bool:
    """Whether a name was bound by the prelude."""
    return id(binder) in BINDINGS
