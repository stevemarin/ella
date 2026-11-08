"""Dead-code elimination: remove instructions whose results are never used.

After the other passes, many values are left over: the `const ()` an
expression made but nobody needed, a loaded value that propagation replaced.
An instruction can be removed when nothing uses its result, *and* running it
has no effect besides computing that result.

That second condition is subtle. `print` and `store` have effects, so they
always stay. But so does `xs[5]`, even if its value is unused: it might
stop the program with "index out of range", and removing it would hide the
error. Only instructions that can never fail are removed:

* building and taking apart values: `const`, `tuple`, `list`, `variant`,
  `closure`, `tuple_get`, `is_variant`, `variant_field`, `range`, `len`,
  `to_text`, `copy`, `carry`, `quote` (but not making a struct with a
  finalizer, which will run)
* comparisons, `not`, and the bitwise operations that can't overflow
* floating point arithmetic, which never fails (IEEE 754 gives infinities
  and NaNs instead), and joining strings and lists with `add`

Arithmetic on arrays can fail (their shapes may differ), so it stays.

`i64` arithmetic can overflow, so unused `i64` arithmetic stays.

Unused block parameters are removed too, along with the values jumps pass
to them, which may then become unused in turn. The pass repeats until
nothing more can go.

Finally, scope levels that nothing allocates in any more are dropped, with
the `reset`s that free them (ella.ir.trim_levels).
"""

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
