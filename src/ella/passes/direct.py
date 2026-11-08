"""Direct calls: call a known function by name instead of through a function value.

The lowering calls every function through a value: a top-level function is
a global holding a closure, read with `global_get`, and a function's own
name (for recursion) makes a closure of it:

    %4: (i64) -> i64 = global_get fib
    %6: i64 = call %4(%5)

When the value is known to be a closure that captures nothing, the call can
name its function instead, which needs no closure and lets inlining
(ella.passes.inline) see what is called:

    %6: i64 = call_direct fib(%5)

A global is known to hold a function when the program sets it exactly once,
to a closure that captures nothing, before any of the program's code runs:
the top-level functions are set first thing in the entry function
(docs/grammar.md §3), and one that can be assigned to, or is bound by more
than one `let`, is set again later. Its `global_get`s then can't fail, and
become the closure itself, which is static data. The `closure` and
`global_get` instructions the calls no longer use are left for dce.
"""

from collections import Counter

from ella.ir import Function, Instr, Module


def make_calls_direct(function: Function, module: Module) -> bool:
    known = known_functions(module)
    changed = False
    closures: dict[int, str] = {}  # value id -> the function of a closure that captures nothing
    for block in function.blocks:
        for instr in block.instrs:
            if instr.op == "global_get" and instr.name in known:
                instr.op, instr.name = "closure", known[instr.name]
                changed = True
            if instr.op == "closure" and not instr.args and instr.dest is not None:
                closures[instr.dest.id] = instr.name
    for block in function.blocks:
        for instr in block.instrs:
            if instr.op == "call" and instr.args[0].id in closures:
                name = closures[instr.args[0].id]
                instr.op, instr.name, instr.args = "call_direct", name, instr.args[1:]
                instr.mutable = module.functions[name].mut_params
                changed = True
    return changed


def known_functions(module: Module) -> dict[str, str]:
    """The globals that always hold the same function, which captures nothing: global name -> function name."""
    sets = Counter(
        instr.name
        for function in module.functions.values()
        for block in function.blocks
        for instr in block.instrs
        if instr.op == "global_set"
    )
    closures: dict[int, str] = {}
    known: dict[str, str] = {}
    entry: list[Instr] = module.functions[module.entry].blocks[0].instrs
    # Top-level declarations run before user code, so these initial assignments are stable.
    for instr in entry:  # the top-level functions, set before anything else
        if instr.op == "closure" and not instr.args and instr.dest is not None:
            closures[instr.dest.id] = instr.name
        elif instr.op == "global_set" and instr.args[0].id in closures:
            if sets[instr.name] == 1:
                known[instr.name] = closures[instr.args[0].id]
        elif instr.op != "const":
            break
    return known
