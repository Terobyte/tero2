"""Direct tests for run_execute — covering paths identified by code review.

Issues under test:
    1a. retry_count never incremented after Builder failures (line ~263 fix)
    1b. retry_count never incremented after Verifier FAIL  (line ~332 fix)
    2.  ANOMALY verdict must write EVENT_JOURNAL before check_triggers runs
    3.  Interrupted-task recovery seeding used wrong guard (start_index > 0)
        — task 0 crash never seeded; clean task-1 start was wrongly seeded
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, ReflexionConfig, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.notifier import Notifier
from tero2.phases.context import RunnerContext
from tero2.phases.execute_phase import run_execute
from tero2.players.architect import SlicePlan, Task
from tero2.players.builder import BuilderResult
from tero2.players.verifier import Verdict, VerifierResult
from tero2.state import AgentState, SoraPhase


# ── Shared helpers ─────────────────────────────────────────────────────────


def _make_ctx(tmp_path: Path, state: AgentState | None = None) -> RunnerContext:
    """Build a minimal RunnerContext with real disk/checkpoint and mocked chain."""
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    disk = DiskLayer(project)
    disk.init()
    config = Config()
    config.telegram = TelegramConfig()
    config.reflexion = ReflexionConfig(max_cycles=1)  # default: 2 attempts
    config.roles["builder"] = RoleConfig(provider="fake")
    checkpoint = CheckpointManager(disk)
    notifier = Notifier(TelegramConfig())
    cb_registry = CircuitBreakerRegistry()
    _state = state if state is not None else AgentState(sora_phase=SoraPhase.ARCHITECT)
    ctx = RunnerContext(config, disk, checkpoint, notifier, _state, cb_registry)
    # Stub build_chain so no real provider registry is needed
    ctx.build_chain = MagicMock(return_value=MagicMock())
    return ctx


def _one_task() -> SlicePlan:
    return SlicePlan(
        slice_id="S01",
        slice_dir="milestones/M001/S01",
        tasks=[Task(id="T01", description="write some code")],
    )


def _two_tasks() -> SlicePlan:
    return SlicePlan(
        slice_id="S01",
        slice_dir="milestones/M001/S01",
        tasks=[
            Task(id="T01", description="first task"),
            Task(id="T02", description="second task"),
        ],
    )


def _builder_ok() -> BuilderResult:
    return BuilderResult(success=True, output_file="out.md", captured_output="done")


def _builder_fail() -> BuilderResult:
    return BuilderResult(success=False, error="timeout", captured_output="partial")


def _verifier_fail() -> VerifierResult:
    return VerifierResult(success=False, verdict=Verdict.FAIL, captured_output="tests failed")


def _verifier_anomaly() -> VerifierResult:
    return VerifierResult(
        success=False, verdict=Verdict.ANOMALY, captured_output="process crashed"
    )


# ── Issue 1a: retry_count incremented after Builder failure ────────────────


class TestRetryCountAfterBuilderFailure:
    """Builder failures must increment ctx.state.retry_count each attempt."""

    async def test_retry_count_nonzero_after_failures(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=_builder_fail())
            MockB.return_value = inst

            result = await run_execute(ctx, _one_task())

        assert not result.success
        assert ctx.state.retry_count > 0, (
            f"retry_count must be > 0 after builder failures, got {ctx.state.retry_count}"
        )

    async def test_retry_count_matches_attempt_count(self, tmp_path: Path) -> None:
        """max_cycles=1 → 2 attempts → retry_count == 2 after both fail."""
        ctx = _make_ctx(tmp_path)
        ctx.config.reflexion = ReflexionConfig(max_cycles=1)

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=_builder_fail())
            MockB.return_value = inst

            await run_execute(ctx, _one_task())

        assert ctx.state.retry_count == 2, (
            f"expected retry_count==2 (one per failed attempt), got {ctx.state.retry_count}"
        )

    async def test_check_stuck_can_detect_retries(self, tmp_path: Path) -> None:
        """After builder failures, check_stuck sees a non-zero retry_count."""
        from tero2.stuck_detection import StuckSignal, check_stuck

        ctx = _make_ctx(tmp_path)
        ctx.config.reflexion = ReflexionConfig(max_cycles=2)
        ctx.config.stuck_detection.max_retries = 2  # will fire after 2 failures

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=_builder_fail())
            MockB.return_value = inst

            await run_execute(ctx, _one_task())

        # check_stuck should see RETRY_EXHAUSTED since retry_count was incremented
        result = check_stuck(ctx.state, ctx.config.stuck_detection)
        assert result.signal == StuckSignal.RETRY_EXHAUSTED, (
            f"expected RETRY_EXHAUSTED with retry_count={ctx.state.retry_count}, "
            f"got {result.signal}"
        )


# ── Issue 1b: retry_count incremented after Verifier FAIL ─────────────────


class TestRetryCountAfterVerifierFail:
    """Verifier FAIL verdicts must also increment ctx.state.retry_count."""

    async def test_retry_count_nonzero_after_verifier_fail(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.config.roles["verifier"] = RoleConfig(provider="fake")
        ctx.config.reflexion = ReflexionConfig(max_cycles=1)

        with (
            patch("tero2.phases.execute_phase.BuilderPlayer") as MockB,
            patch("tero2.phases.execute_phase.VerifierPlayer") as MockV,
        ):
            mb = MagicMock()
            mb.run = AsyncMock(return_value=_builder_ok())
            MockB.return_value = mb

            mv = MagicMock()
            mv.run = AsyncMock(return_value=_verifier_fail())
            MockV.return_value = mv

            result = await run_execute(ctx, _one_task())

        assert not result.success
        assert ctx.state.retry_count > 0, (
            f"retry_count must be > 0 after verifier FAILs, got {ctx.state.retry_count}"
        )

    async def test_retry_count_matches_verifier_fail_count(self, tmp_path: Path) -> None:
        """max_cycles=1 → 2 verifier FAILs → retry_count == 2."""
        ctx = _make_ctx(tmp_path)
        ctx.config.roles["verifier"] = RoleConfig(provider="fake")
        ctx.config.reflexion = ReflexionConfig(max_cycles=1)

        with (
            patch("tero2.phases.execute_phase.BuilderPlayer") as MockB,
            patch("tero2.phases.execute_phase.VerifierPlayer") as MockV,
        ):
            mb = MagicMock()
            mb.run = AsyncMock(return_value=_builder_ok())
            MockB.return_value = mb

            mv = MagicMock()
            mv.run = AsyncMock(return_value=_verifier_fail())
            MockV.return_value = mv

            await run_execute(ctx, _one_task())

        assert ctx.state.retry_count == 2, (
            f"expected retry_count==2 (one per verifier FAIL), got {ctx.state.retry_count}"
        )


# ── Issue 2: ANOMALY must write EVENT_JOURNAL before check_triggers ────────


class TestAnomalyEventJournal:
    """Verifier ANOMALY verdict must persist to EVENT_JOURNAL before check_triggers."""

    async def test_anomaly_verdict_writes_event_journal(self, tmp_path: Path) -> None:
        ctx = _make_ctx(tmp_path)
        ctx.config.roles["verifier"] = RoleConfig(provider="fake")
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)  # single attempt

        with (
            patch("tero2.phases.execute_phase.BuilderPlayer") as MockB,
            patch("tero2.phases.execute_phase.VerifierPlayer") as MockV,
            patch("tero2.phases.coach_phase.run_coach", new=AsyncMock(return_value=None)),
        ):
            mb = MagicMock()
            mb.run = AsyncMock(return_value=_builder_ok())
            MockB.return_value = mb

            mv = MagicMock()
            mv.run = AsyncMock(return_value=_verifier_anomaly())
            MockV.return_value = mv

            await run_execute(ctx, _one_task())

        journal = ctx.disk.read_file("persistent/EVENT_JOURNAL.md")
        assert "ANOMALY" in journal, (
            f"EVENT_JOURNAL.md must contain 'ANOMALY' after anomaly verdict, got: {journal!r}"
        )

    async def test_anomaly_journal_contains_task_id(self, tmp_path: Path) -> None:
        """The ANOMALY journal entry must identify the task that triggered it."""
        ctx = _make_ctx(tmp_path)
        ctx.config.roles["verifier"] = RoleConfig(provider="fake")
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        with (
            patch("tero2.phases.execute_phase.BuilderPlayer") as MockB,
            patch("tero2.phases.execute_phase.VerifierPlayer") as MockV,
            patch("tero2.phases.coach_phase.run_coach", new=AsyncMock(return_value=None)),
        ):
            mb = MagicMock()
            mb.run = AsyncMock(return_value=_builder_ok())
            MockB.return_value = mb

            mv = MagicMock()
            mv.run = AsyncMock(return_value=_verifier_anomaly())
            MockV.return_value = mv

            await run_execute(ctx, _one_task())

        journal = ctx.disk.read_file("persistent/EVENT_JOURNAL.md")
        assert "T01" in journal, f"Expected task_id T01 in ANOMALY journal entry, got: {journal!r}"

    async def test_check_triggers_fires_on_anomaly(self, tmp_path: Path) -> None:
        """check_triggers must return should_fire=True after ANOMALY writes the journal."""
        from tero2.triggers import check_triggers, CoachTrigger

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["verifier"] = RoleConfig(provider="fake")
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        coach_calls: list[str] = []

        async def fake_coach(run_ctx, trigger):
            coach_calls.append(trigger.value)
            return None

        with (
            patch("tero2.phases.execute_phase.BuilderPlayer") as MockB,
            patch("tero2.phases.execute_phase.VerifierPlayer") as MockV,
            patch("tero2.phases.coach_phase.run_coach", new=fake_coach),
        ):
            mb = MagicMock()
            mb.run = AsyncMock(return_value=_builder_ok())
            MockB.return_value = mb

            mv = MagicMock()
            mv.run = AsyncMock(return_value=_verifier_anomaly())
            MockV.return_value = mv

            await run_execute(ctx, _one_task())

        # After run_execute, the journal must contain ANOMALY (written before check_triggers)
        journal = ctx.disk.read_file("persistent/EVENT_JOURNAL.md")
        assert "ANOMALY" in journal
        # check_triggers would have returned ANOMALY trigger, causing Coach to be called
        assert coach_calls, (
            "run_coach should have been called after ANOMALY verdict; "
            "check_triggers can only fire if ANOMALY was written to EVENT_JOURNAL first"
        )
        assert coach_calls[0] == CoachTrigger.ANOMALY.value, (
            f"expected ANOMALY trigger, got {coach_calls[0]!r}"
        )


# ── Issue 3: correct interrupted-task recovery seeding ────────────────────


class TestCrashRecoverySeeding:
    """task_in_progress flag must drive seeding — not the start_index > 0 guard."""

    async def test_task0_crash_is_seeded(self, tmp_path: Path) -> None:
        """Crash during task 0 (task_in_progress=True, index=0) → seed reflexion."""
        state = AgentState(current_task_index=0, task_in_progress=True, sora_phase=SoraPhase.ARCHITECT)
        ctx = _make_ctx(tmp_path, state=state)
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        captured: list[str] = []

        async def capture(**kw):
            captured.append(kw.get("reflexion_context", ""))
            return _builder_ok()

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(side_effect=capture)
            MockB.return_value = inst

            await run_execute(ctx, _one_task())

        assert captured, "BuilderPlayer.run was never called"
        assert "crash recovery" in captured[0], (
            f"Task 0 interrupt must seed crash-recovery context, got: {captured[0]!r}"
        )

    async def test_fresh_start_task0_not_seeded(self, tmp_path: Path) -> None:
        """Fresh run (task_in_progress=False, index=0) must NOT seed reflexion."""
        state = AgentState(current_task_index=0, task_in_progress=False, sora_phase=SoraPhase.ARCHITECT)
        ctx = _make_ctx(tmp_path, state=state)
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        captured: list[str] = []

        async def capture(**kw):
            captured.append(kw.get("reflexion_context", ""))
            return _builder_ok()

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(side_effect=capture)
            MockB.return_value = inst

            await run_execute(ctx, _one_task())

        assert captured, "BuilderPlayer.run was never called"
        assert "crash recovery" not in captured[0], (
            f"Fresh start must NOT seed crash recovery, got: {captured[0]!r}"
        )

    async def test_clean_advance_task1_not_seeded(self, tmp_path: Path) -> None:
        """Clean task-0 completion (task_in_progress=False, index=1) must NOT seed task 1."""
        state = AgentState(current_task_index=1, task_in_progress=False, sora_phase=SoraPhase.ARCHITECT)
        ctx = _make_ctx(tmp_path, state=state)
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        captured: list[str] = []

        async def capture(**kw):
            captured.append(kw.get("reflexion_context", ""))
            return _builder_ok()

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(side_effect=capture)
            MockB.return_value = inst

            await run_execute(ctx, _two_tasks())

        # Only T02 runs; must not receive crash-recovery seed
        assert captured, "BuilderPlayer.run was never called"
        assert "crash recovery" not in captured[0], (
            f"Clean advance must NOT seed crash recovery for task 1, got: {captured[0]!r}"
        )

    async def test_task1_crash_is_seeded(self, tmp_path: Path) -> None:
        """Crash during task 1 (task_in_progress=True, index=1) → seed reflexion.

        Task 0 must have completed successfully before task 1 began, so its
        ``T01-SUMMARY.md`` is pre-populated — this distinguishes the scenario
        from the bug-102 case (summary missing → re-run), and lets the test
        focus on what it actually asserts: that task 1's builder call carries
        the crash-recovery reflexion context.
        """
        state = AgentState(current_task_index=1, task_in_progress=True, sora_phase=SoraPhase.ARCHITECT)
        ctx = _make_ctx(tmp_path, state=state)
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        # Task 0 succeeded before the crash → its summary is on disk.
        t01 = ctx.disk.sora_dir / "milestones/M001/S01/T01-SUMMARY.md"
        t01.parent.mkdir(parents=True, exist_ok=True)
        t01.write_text("# T01 Summary\nprior run completed task 0")

        captured: list[str] = []

        async def capture(**kw):
            captured.append(kw.get("reflexion_context", ""))
            return _builder_ok()

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(side_effect=capture)
            MockB.return_value = inst

            await run_execute(ctx, _two_tasks())

        assert captured, "BuilderPlayer.run was never called"
        assert "crash recovery" in captured[0], (
            f"Task 1 interrupt must seed crash-recovery context, got: {captured[0]!r}"
        )
