"""Optimization passes over the IR.

Each pass is a function `pass(function, module) -> bool` in its own module,
which changes one IR function in place and says whether it changed anything.
Each keeps the program's behavior exactly the same, including its errors:

* `mem2reg`: promote local slots to SSA values (ella.passes.mem2reg)
* `direct`: call known functions by name (ella.passes.direct)
* `moves`: turn copies of values nothing else uses into moves (ella.passes.moves)
* `fold`: constant folding and propagation (ella.passes.fold)
* `simplify`: simplify the control-flow graph (ella.passes.simplify)
* `dce`: dead-code elimination (ella.passes.dce)
* `inline`: put small functions' bodies where they are called (ella.passes.inline)

`optimize` runs passes over a whole module, verifying the IR before and after
every pass, so a pass that breaks the IR's rules is caught right where it
happens. `uv run ella --ir --passes mem2reg,fold file.ella` shows what any
combination of passes does.
"""

from collections.abc import Callable

from ella.ir import Function, IRError, Module, verify
from ella.passes.dce import eliminate_dead_code
from ella.passes.direct import make_calls_direct
from ella.passes.fold import fold_constants
from ella.passes.inline import bottom_up, inline_calls, unused_functions
from ella.passes.mem2reg import promote_slots
from ella.passes.moves import move_copies
from ella.passes.simplify import simplify_cfg

type Pass = Callable[[Function, Module], bool]

PASSES: dict[str, Pass] = {
    "mem2reg": promote_slots,
    "direct": make_calls_direct,
    "moves": move_copies,
    "fold": fold_constants,
    "simplify": simplify_cfg,
    "dce": eliminate_dead_code,
    "inline": inline_calls,
}

# The passes that clean up after each other. Each can make work for the
# others (folding a branch makes code unreachable, removing it makes values
# unused, merging blocks puts a copy next to its original, ...), so they
# repeat until none changes anything.
CLEANUP = ["moves", "fold", "simplify", "dce"]
MAX_ROUNDS = 10


def run_pass(module: Module, name: str) -> bool:
    """Run one pass over every function, then verify the module. Inlining goes bottom up, callees first."""
    changed = False
    for function in bottom_up(module) if name == "inline" else list(module.functions.values()):
        changed |= PASSES[name](function, module)
    try:
        verify(module)
    except IRError as error:
        raise IRError(f"after the {name} pass: {error}") from error
    return changed


def optimize(module: Module, passes: list[str] | None = None) -> Module:
    """Optimize a module in place, and return it.

    With `passes`, run exactly those passes once each, in order. Without, run
    the default pipeline: mem2reg and direct, then the cleanup passes until
    nothing changes, then inlining and the cleanup passes again. Functions
    that inlining leaves unused are dropped, unless the program builds code
    at run time, which may call them.
    """
    verify(module)
    if passes is not None:
        for name in passes:
            run_pass(module, name)
        return module
    run_pass(module, "mem2reg")
    run_pass(module, "direct")
    clean_up(module)
    if run_pass(module, "inline"):
        clean_up(module)
    if not module.templates:
        for name in unused_functions(module):
            del module.functions[name]
    return module


def clean_up(module: Module) -> None:
    """Run the cleanup passes until none changes anything."""
    for _ in range(MAX_ROUNDS):
        changed = False
        for name in CLEANUP:
            changed |= run_pass(module, name)
        if not changed:
            break
