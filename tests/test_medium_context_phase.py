"""
Failing tests demonstrating 3 medium bugs from bugs.md.

  A13 — context_assembly.py measures system+user combined tokens against
        the user-only role limit (budget excludes system prompt allocation)
  A12 — context_assembly.py summary labels are inverted: reversed iteration
        paired with len - i produces (3/3),(2/3),(1/3) instead of (1/3),(2/3),(3/3)
  A20 — architect_phase.py failed Architect returns data={"slice_plan": None}
        instead of data=None; downstream result.data["slice_plan"] iteration
        raises TypeError

Each test FAILs against current code and would pass once the bug is fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, ContextConfig, RoleConfig
from tero2.context_assembly import ContextAssembler, estimate_tokens
from tero2.phases.context import PhaseResult, RunnerContext


# ─────────────────────────────────────────────────────────────────────────────
# A13 — budget check measures system+user against user-only role limit
# ─────────────────────────────────────────────────────────────────────────────


def test_a13_budget_check_includes_system_prompt_in_user_limit():
    """A13 — context_assembly.py lines 117-121: mandatory_tokens counts
    system_prompt + user together but checks against the user-only role budget.

    Current code::

        mandatory_tokens = estimate_tokens(system_prompt + mandatory_user)
        status = _check_budget(mandatory_tokens, budget, cfg)
        if status == BudgetState.HARD_FAIL:
            raise ContextWindowExceededError(mandatory_tokens, budget)

    Bug: ``budget = self._role_limit(role)`` is computed from
    ``role_cfg.context_window * target_ratio`` — this is the total token budget
    for the user turn.  The system prompt occupies its own slot in the LLM
    context window and should NOT be counted against the user limit.  By
    adding system_prompt length to mandatory_tokens, the budget check rejects
    valid prompts whose user portion alone is within budget, and may also
    misclassify a combined (system+user) that is within the overall window.

    Expected: a system prompt + a short user prompt whose combined token count
    exceeds the user-only budget but whose user-only token count is within
    budget should NOT raise ContextWindowExceededError.

    Current behaviour: it RAISES ContextWindowExceededError because the system
    prompt tokens are incorrectly folded into the user-budget check.
    """
    from tero2.errors import ContextWindowExceededError

    # Role with a small context_window so we can trigger the bug easily.
    # budget = 300 * 0.70 = 210 tokens
    # hard_fail threshold ratio = hard_fail_ratio / target_ratio = 0.95 / 0.70 ≈ 1.357
    # → hard_fail fires when tokens > 210 * 1.357 ≈ 285 tokens
    role_cfg = RoleConfig(provider="claude", context_window=300)
    config = Config(
        roles={"builder": role_cfg},
        context=ContextConfig(
            target_ratio=0.70,
            warning_ratio=0.80,
            hard_fail_ratio=0.95,
        ),
    )

    assembler = ContextAssembler(config)

    # system_prompt ~ 200 tokens (800 chars)
    system_prompt = "S" * 800
    # user task ~ 100 tokens (400 chars) — within budget on its own (100 < 210)
    # but combined with system: 200 + 100 = 300 > hard_fail threshold (285)
    task_plan = "T" * 400

    sys_tokens = estimate_tokens(system_prompt)   # 200
    task_tokens = estimate_tokens(task_plan)       # 100

    # Sanity: user portion alone is well within budget (210)
    assert task_tokens < 210, f"setup error: task alone ({task_tokens}) should fit in budget (210)"
    # Sanity: combined exceeds hard_fail threshold (budget * hard_fail/target = 210 * 1.357 = 285)
    hard_fail_threshold_tokens = int(210 * (0.95 / 0.70))  # 285
    assert sys_tokens + task_tokens > hard_fail_threshold_tokens, (
        f"setup error: combined ({sys_tokens + task_tokens}) should exceed "
        f"hard_fail threshold ({hard_fail_threshold_tokens})"
    )

    # The assembler is given a system_prompts dict so it uses our system_prompt.
    assembler2 = ContextAssembler(
        config,
        system_prompts={"builder": system_prompt},
    )

    # BUG: raises ContextWindowExceededError even though user portion (100 tok)
    # is well within budget (350 tok).
    # After fix: should NOT raise because user-only tokens are within budget.
    try:
        result = assembler2.assemble_builder(task_plan=task_plan)
    except ContextWindowExceededError as exc:
        pytest.fail(
            f"BUG A13: ContextWindowExceededError raised even though the user "
            f"portion alone ({task_tokens} tokens) fits within the role budget "
            f"(210 tokens). System prompt tokens ({sys_tokens}) were incorrectly "
            f"counted against the user-only limit. Exception: {exc}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# A12 — summary section labels are inverted
# ─────────────────────────────────────────────────────────────────────────────


def test_a12_summary_labels_are_in_order():
    """A12 — context_assembly.py lines 130-132: ``reversed(summaries)`` paired
    with ``len(summaries) - i`` produces inverted labels.

    Current code::

        for i, summary in enumerate(reversed(summaries)):
            idx = len(summaries) - i
            optional.append((f"Summary ({idx}/{len(summaries)})", summary, 0))

    Bug: when iterating ``reversed(summaries)`` with index ``i``:
        i=0 → idx = 3 → label "Summary (3/3)"
        i=1 → idx = 2 → label "Summary (2/3)"
        i=2 → idx = 1 → label "Summary (1/3)"

    The labels count DOWN as the list progresses, but readers expect them to
    read (1/3), (2/3), (3/3) in display order — oldest summary first.

    Expected: the assembled user_prompt contains the summaries labelled
    (1/3), (2/3), (3/3) in that order (or equivalently, the label of the
    first summary block in the output is "Summary (1/3)").

    Current behaviour: first summary block in the output is labelled
    "(3/3)", meaning labels are completely reversed.
    """
    # Config with a generous budget so all summaries are included.
    role_cfg = RoleConfig(provider="claude", context_window=200_000)
    config = Config(
        roles={"builder": role_cfg},
        context=ContextConfig(
            target_ratio=0.90,
            warning_ratio=0.95,
            hard_fail_ratio=0.99,
        ),
    )

    assembler = ContextAssembler(config, system_prompts={"builder": ""})

    summaries = ["summary one", "summary two", "summary three"]

    result = assembler.assemble(
        role="builder",
        system_prompt="",
        task_plan="do the thing",
        summaries=summaries,
    )

    user_prompt = result.user_prompt

    # Extract all "Summary (N/3)" labels in the order they appear.
    found_labels = re.findall(r"Summary \((\d+)/3\)", user_prompt)

    assert found_labels, (
        f"BUG A12: no 'Summary (N/3)' labels found in user_prompt. "
        f"user_prompt snippet: {user_prompt[:400]!r}"
    )

    # Expected order: 1, 2, 3 (ascending)
    assert found_labels == ["1", "2", "3"], (
        f"BUG A12: summary labels are inverted. "
        f"Got labels in order {found_labels!r} but expected ['1', '2', '3']. "
        "The reversed() iteration paired with len - i produces descending "
        "labels (3/3),(2/3),(1/3) instead of ascending (1/3),(2/3),(3/3)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A20 — failed Architect returns data={"slice_plan": None} instead of data=None
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a20_architect_failure_returns_data_none():
    """A20 — architect_phase.py line 84: ``run_architect`` always wraps the
    result in ``data={"slice_plan": result.slice_plan}`` even on failure.

    Current code::

        return PhaseResult(
            success=result.success,
            error=result.error,
            data={"slice_plan": result.slice_plan},   # ← always set, even on failure
        )

    Bug: when the architect fails, ``result.slice_plan`` is ``None``
    (see ArchitectResult dataclass: ``slice_plan: SlicePlan | None = None``).
    The returned PhaseResult therefore has ``data={"slice_plan": None}``.
    Downstream code in runner.py does::

        slice_plan = result.data["slice_plan"]
        # ... later iterates over slice_plan.tasks

    This raises ``AttributeError: 'NoneType' object has no attribute 'tasks'``
    (or TypeError on iteration).  The fix is to return ``data=None`` on failure
    so the runner's ``if not result.success: return`` guard is the only path
    the downstream code needs to handle.

    Expected: when run_architect() fails, result.data is None.
    Current behaviour: result.data is {"slice_plan": None}, masking the failure
    and allowing downstream code to receive a dict that looks valid but contains
    None instead of a SlicePlan — causing a TypeError downstream.
    """
    from tero2.phases.architect_phase import run_architect

    # Build a minimal ctx with a chain that causes the architect to fail.
    config = Config(
        roles={
            "architect": RoleConfig(
                provider="zai",
                context_window=128_000,
            )
        },
    )

    # Mock chain that raises an exception so ArchitectPlayer.run() returns
    # ArchitectResult(success=False, ..., slice_plan=None).
    failing_chain = MagicMock()
    failing_chain.run_prompt = AsyncMock(side_effect=RuntimeError("provider down"))

    disk = MagicMock()
    disk.project_path = MagicMock()
    disk.project_path.__str__ = lambda self: "/fake/project"
    disk.read_file = MagicMock(return_value=None)
    disk.write_file = MagicMock()

    checkpoint = MagicMock()
    checkpoint.save = MagicMock()

    personas = MagicMock()
    personas.load_or_default = MagicMock(
        return_value=MagicMock(system_prompt="you are architect")
    )

    ctx = RunnerContext(
        config=config,
        disk=disk,
        checkpoint=checkpoint,
        personas=personas,
        milestone_path="milestones/M001",
    )

    # Patch build_chain so we control the chain the player receives.
    with patch.object(ctx, "build_chain", return_value=failing_chain):
        result = await run_architect(ctx, slice_id="S01")

    assert not result.success, "setup error: expected architect to fail"

    # BUG: result.data is {"slice_plan": None} instead of None.
    # Downstream code does result.data["slice_plan"] expecting a SlicePlan;
    # it gets None and then crashes with AttributeError on .tasks.
    assert result.data is None, (
        f"BUG A20: run_architect() returned data={result.data!r} on failure "
        f"instead of data=None. When the architect fails, PhaseResult.data "
        f"should be None so the runner's 'if not result.success: return' "
        f"guard is sufficient. Currently data={{'slice_plan': None}} leaks "
        f"through and causes TypeError/AttributeError downstream when "
        f"result.data['slice_plan'].tasks is accessed."
    )

    # Also verify: accessing the slice_plan key and iterating .tasks raises
    # TypeError with current code (belt-and-suspenders: documents the crash).
    if result.data is not None:
        slice_plan = result.data.get("slice_plan")
        try:
            _ = list(slice_plan.tasks)
            pytest.fail(
                "BUG A20: expected TypeError/AttributeError when iterating "
                "slice_plan.tasks on a None slice_plan, but no error was raised."
            )
        except (TypeError, AttributeError):
            pass  # this IS the bug — documents the crash path
