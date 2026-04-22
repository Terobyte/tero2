"""Bug 116: ``CoachPlayer`` reads ``.sora/human/STEER.md`` on every invocation
but never clears it, so the same human directive is repeatedly injected into
every subsequent strategic pass — forever, until the operator manually
deletes the file.

The Coach contract, per its own docstring, is that it "reads all relevant
project context from disk and produces up to four strategic documents".
STEER.md is specifically listed as the **one-shot human input channel**
(see tero2/phases/coach_phase.py). Once Coach has folded the human's
directive into ``strategic/STRATEGY.md``, ``strategic/CONTEXT_HINTS.md`` etc.,
the source STEER.md has served its purpose. Leaving it in place means:

1. Every subsequent Coach trigger (END_OF_SLICE, ANOMALY, BUDGET_60) re-reads
   the same text and re-injects it into the prompt — the model sees
   "Human Steer: ..." in the prompt on slice N+1 even though the operator
   intended it only for slice N. The strategy docs get progressively more
   skewed by a stale directive.
2. The ``_check_human_steer`` trigger condition in ``tero2.triggers`` stays
   permanently True on the same input. Currently the trigger is gated by
   ``check_triggers`` being called only in the ANOMALY branch of
   ``execute_phase`` — but any future caller that wires the trigger to the
   phase-boundary dispatcher will immediately hit an infinite-Coach loop.

Fix: after a **successful** Coach run — i.e., one that wrote at least one
strategy document — clear STEER.md. Failed runs must not clear (the
operator's directive hasn't been applied yet). Empty STEER.md must not
be touched.

Per feedback_tdd_order.md: these tests are written test-first and will be
RED against the current ``CoachPlayer.run`` implementation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tero2.disk_layer import DiskLayer
from tero2.players.coach import CoachPlayer


@pytest.fixture
def disk(tmp_path: Path) -> DiskLayer:
    d = DiskLayer(tmp_path)
    d.init()
    return d


def _mock_chain_yielding(text: str) -> MagicMock:
    """A ProviderChain mock whose run_prompt_collected returns *text*."""
    chain = MagicMock()
    chain.run_prompt_collected = AsyncMock(return_value=text)
    return chain


_COACH_OUTPUT_VALID = (
    "## STRATEGY\n"
    "focus on auth next\n\n"
    "## TASK_QUEUE\n"
    "- [ ] S01: wire auth\n\n"
    "## RISK\n"
    "token expiry edge case\n\n"
    "## CONTEXT_HINTS\n"
    "use jwt\n"
)

_COACH_OUTPUT_EMPTY = "no sections recognized, just prose"


class TestSteerClearedAfterSuccessfulCoach:
    """Happy path: STEER.md has operator input; Coach runs successfully; the
    file is cleared so the same directive is not re-read next time."""

    @pytest.mark.asyncio
    async def test_steer_cleared_after_success(self, disk: DiskLayer) -> None:
        disk.write_steer("please drop feature X, focus on core")
        assert disk.read_steer() == "please drop feature X, focus on core"

        coach = CoachPlayer(
            chain=_mock_chain_yielding(_COACH_OUTPUT_VALID),
            disk=disk,
            working_dir=str(disk.project_path),
        )
        result = await coach.run(
            trigger="human_steer",
            persona_prompt="",
            slice_id="S01",
            milestone_path="milestones/M001",
        )

        assert result.success, f"expected success, got error={result.error!r}"
        assert disk.read_steer() == "", (
            "bug 116 contract: after a successful coach run that wrote "
            "strategy docs, STEER.md must be cleared so the same human "
            "directive does not get re-read on the next invocation. got: "
            f"{disk.read_steer()!r}"
        )

    @pytest.mark.asyncio
    async def test_strategy_docs_still_written(self, disk: DiskLayer) -> None:
        """Clearing STEER.md must not short-circuit the strategy-doc writes."""
        disk.write_steer("any steer")
        coach = CoachPlayer(
            chain=_mock_chain_yielding(_COACH_OUTPUT_VALID),
            disk=disk,
            working_dir=str(disk.project_path),
        )
        result = await coach.run(
            trigger="human_steer",
            persona_prompt="",
            slice_id="S01",
            milestone_path="milestones/M001",
        )

        assert result.success
        # STRATEGY.md (the canonical section) must exist.
        assert disk.read_file("strategic/STRATEGY.md"), (
            "strategy document must be written even when STEER.md is cleared"
        )


class TestSteerPreservedOnFailure:
    """If coach fails (exception, empty output, zero strategy sections), the
    operator's directive must not be lost — they should get to try again."""

    @pytest.mark.asyncio
    async def test_steer_kept_when_coach_raises(self, disk: DiskLayer) -> None:
        disk.write_steer("do not drop this")

        chain = MagicMock()
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("provider down"))

        coach = CoachPlayer(
            chain=chain, disk=disk, working_dir=str(disk.project_path)
        )
        result = await coach.run(
            trigger="human_steer",
            persona_prompt="",
            slice_id="S01",
            milestone_path="milestones/M001",
        )

        assert not result.success, "expected failure from provider error"
        assert disk.read_steer() == "do not drop this", (
            "STEER.md must survive a coach failure — the human directive "
            "has not been applied yet, so clearing it would lose operator "
            "intent"
        )

    @pytest.mark.asyncio
    async def test_steer_kept_when_output_has_no_sections(
        self, disk: DiskLayer
    ) -> None:
        """When the LLM returns text with no recognisable section headers,
        no strategy docs are written. STEER.md must remain for next attempt."""
        disk.write_steer("test directive")

        coach = CoachPlayer(
            chain=_mock_chain_yielding(_COACH_OUTPUT_EMPTY),
            disk=disk,
            working_dir=str(disk.project_path),
        )
        result = await coach.run(
            trigger="end_of_slice",
            persona_prompt="",
            slice_id="S01",
            milestone_path="milestones/M001",
        )

        # The existing Coach contract returns success=True for "no exceptions"
        # even when no strategy sections were parsed — no strategy files get
        # written in that case (see the `if strategy:` guards). STEER.md must
        # NOT be cleared in that case, because nothing acted on it.
        if result.success:
            strategy_written = bool(disk.read_file("strategic/STRATEGY.md"))
            task_queue_written = bool(disk.read_file("strategic/TASK_QUEUE.md"))
            risk_written = bool(disk.read_file("strategic/RISK.md"))
            hints_written = bool(disk.read_file("strategic/CONTEXT_HINTS.md"))
            if not any([
                strategy_written, task_queue_written, risk_written, hints_written
            ]):
                assert disk.read_steer() == "test directive", (
                    "coach returned 'success' but wrote no strategy documents "
                    "— STEER.md must not be cleared because the operator's "
                    "directive was never actually folded into anything. got: "
                    f"{disk.read_steer()!r}"
                )


class TestEmptySteerUntouched:
    """If STEER.md is empty to begin with (common case: no human input),
    the clear logic must be a no-op that neither errors nor creates the file."""

    @pytest.mark.asyncio
    async def test_empty_steer_stays_empty(self, disk: DiskLayer) -> None:
        # Do not write_steer — file does not exist.
        coach = CoachPlayer(
            chain=_mock_chain_yielding(_COACH_OUTPUT_VALID),
            disk=disk,
            working_dir=str(disk.project_path),
        )
        result = await coach.run(
            trigger="end_of_slice",
            persona_prompt="",
            slice_id="S01",
            milestone_path="milestones/M001",
        )

        assert result.success
        assert disk.read_steer() == ""
