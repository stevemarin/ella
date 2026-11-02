"""Simplify the control-flow graph: fewer, bigger blocks, with no detours.

The lowering makes many small blocks, and the other passes leave behind
blocks that do nothing but jump on. This pass repeats four tidy-ups until
none applies:

1. A branch on a constant becomes a jump to the side it always takes (and a
   branch with the same block on both sides becomes a jump).
2. Blocks that can no longer be reached are removed.
3. A jump or branch to a block that only jumps on goes straight to where
   that block goes.
4. A block reached only by a jump from one other block is merged into it.
   Its parameters are replaced by the values the jump passed.
"""

from ella.ir import (
    Block,
    Branch,
    Function,
    Jump,
    Module,
    Value,
    predecessors,
    reachable,
    replace_values,
)
from ella.passes.fold import constants


def simplify_cfg(function: Function, module: Module) -> bool:
    changed_any = False
    while (
        fold_branches(function) | remove_unreachable(function) | skip_empty_blocks(function) | merge_blocks(function)
    ):
        changed_any = True
    return changed_any


def fold_branches(function: Function) -> bool:
    known = constants(function)
    changed = False
    for block in function.blocks:
        branch = block.terminator
        if not isinstance(branch, Branch):
            continue
        if branch.then is branch.else_:
            block.terminator = Jump(branch.then, [])
            changed = True
        elif branch.cond.id in known:
            block.terminator = Jump(branch.then if known[branch.cond.id] else branch.else_, [])
            changed = True
    return changed


def remove_unreachable(function: Function) -> bool:
    blocks = reachable(function)
    changed = len(blocks) != len(function.blocks)
    function.blocks = blocks
    return changed


def is_empty_jump(block: Block) -> bool:
    """A block with no parameters or instructions, that only jumps on."""
    return not block.params and not block.instrs and isinstance(block.terminator, Jump)


def skip_empty_blocks(function: Function) -> bool:
    """Go straight past blocks that only jump on.

    For a jump this is always correct: the empty block's jump only passes
    values that are defined before it, on every path. A branch can't pass
    values, so it only skips an empty block whose jump passes none.
    """
    changed = False
    entry = function.blocks[0]
    for block in function.blocks:
        match block.terminator:
            case Jump(target) if is_empty_jump(target) and target is not block and target is not entry:
                onward = target.terminator
                assert isinstance(onward, Jump)
                if onward.target is not target:  # not an empty infinite loop
                    block.terminator = Jump(onward.target, list(onward.args))
                    changed = True
            case Branch() as branch:
                for side in ("then", "else_"):
                    target = getattr(branch, side)
                    if is_empty_jump(target) and target is not entry:
                        onward = target.terminator
                        assert isinstance(onward, Jump)
                        if not onward.args and not onward.target.params and onward.target is not target:
                            setattr(branch, side, onward.target)
                            changed = True
    return changed


def merge_blocks(function: Function) -> bool:
    """Merge a block into its only predecessor, when that predecessor jumps to it."""
    preds = predecessors(function)
    for block in function.blocks[1:]:
        if len(preds[block]) != 1:
            continue
        [predecessor] = preds[block]
        jump = predecessor.terminator
        if predecessor is block or not isinstance(jump, Jump):
            continue
        # The block's parameters are just the values the jump passed.
        replacements: dict[int, Value] = {param.id: arg for param, arg in zip(block.params, jump.args)}
        predecessor.instrs.extend(block.instrs)
        predecessor.terminator = block.terminator
        function.blocks.remove(block)
        replace_values(function, replacements)
        return True
    return False
