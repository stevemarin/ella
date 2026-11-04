"""Direct calls: call a known function by name instead of through a function value."""

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
