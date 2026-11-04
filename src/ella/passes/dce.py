"""Dead-code elimination: remove instructions whose results are never used."""

from ella.checker import F32, F64, STR, EnumTy, ListTy
from ella.ir import Function, Instr, Module, remove_param, trim_levels, used_values

# Operations that never fail and have no effect.
SAFE = frozenset(
    {
        "const",
        "tuple",
        "tuple_get",
        "list",
        "variant",
        "is_variant",
        "variant_field",
        "closure",
        "range",
        "range_incl",
        "len",
        "to_text",
        "eq",
        "ne",
        "lt",
        "le",
        "gt",
        "ge",
        "contains",
        "not",
        "bitnot",
        "bitand",
        "bitor",
        "bitxor",
        "copy",
        "carry",
        "quote",
        "quote_begin",
    }
)


def can_remove(instr: Instr, module: Module) -> bool:
    """Whether an instruction whose result is unused can be removed."""
    if instr.dest is None:
        return False  # it is only there for its effect
    if instr.op == "variant" and isinstance(instr.dest.type, EnumTy) and module.enums[instr.dest.type.name].finalize:
        return False  # making it registers its finalizer, which will run
    if instr.op in SAFE:
        return True
    t = instr.dest.type
    if instr.op in ("add", "sub", "mul", "div", "pow", "neg") and t in (F64, F32):
        return True  # (`div` always gives an f64, unless it is an array's)
    return instr.op == "add" and (t == STR or isinstance(t, ListTy))


def eliminate_dead_code(function: Function, module: Module) -> bool:
    changed_any = False
    changed = True
    while changed:
        changed = False
        used = used_values(function)
        for block in function.blocks:
            kept = [i for i in block.instrs if i.dest is None or i.dest.id in used or not can_remove(i, module)]
            if len(kept) != len(block.instrs):
                block.instrs = kept
                changed = True
        # Removing one instruction can make its operands dead on the next sweep.
        for block in function.blocks[1:]:  # the entry's parameters are the function's
            for index in reversed(range(len(block.params))):
                if block.params[index].id not in used:
                    remove_param(function, block, index)
                    changed = True
        changed_any |= changed
    return trim_levels(function) or changed_any
