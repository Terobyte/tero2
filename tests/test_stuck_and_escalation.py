"""Tests for MVP2: stuck detection + escalation.

Covers acceptance criteria:
  - Stuck detection: 3+ retries → RETRY_EXHAUSTED signal
  - Stuck detection: 15+ steps → STEP_LIMIT signal
  - Stuck detection: same tool call 2x → TOOL_REPEAT signal
  - Escalation Level 1: diversification prompt injected
  - Escalation Level 2: backtrack (counters reset), EVENT_JOURNAL written
  - Escalation Level 3: STUCK_REPORT.md written, runner paused
  - Runner integration: stuck + escalation flow end-to-end
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


from tero2.config import Config, EscalationConfig, RoleConfig, StuckDetectionConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.escalation import (
    EscalationAction,
    EscalationLevel,
    decide_escalation,
    execute_escalation,
    write_stuck_report,
)
from tero2.notifier import NotifyLevel
from tero2.runner import Runner
from tero2.state import AgentState, Phase
from tero2.stuck_detection import (
    StuckResult,
    StuckSignal,
    check_stuck,
    compute_tool_hash,
    update_tool_hash,
)


# ── fixtures / helpers ────────────────────────────────────────────────


class _AlwaysFailChain:
    """Chain that always raises RateLimitError → _run_agent returns False."""

    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        raise RateLimitError("fake chain always fails")
        # unreachable, but satisfies async generator protocol
        yield  # type: ignore[misc]


class _CapturingChain:
    """Chain that records the plan it receives and then raises to trigger failure."""

    current_provider_index = 0

    def __init__(self) -> None:
        self.plans: list[str] = []

    async def run_prompt(self, prompt: str):
        self.plans.append(prompt)
        raise RateLimitError("fake chain always fails")
        yield  # type: ignore[misc]


async def _noop_notify(text: str, level=NotifyLevel.PROGRESS) -> bool:
    return True


def _make_config(
    max_retries: int = 3,
    max_steps: int = 15,
    tool_repeat: int = 2,
    div_max_steps: int = 2,
    backtrack: bool = True,
) -> Config:
    cfg = Config()
    cfg.stuck_detection = StuckDetectionConfig(
        max_retries=max_retries,
        max_steps_per_task=max_steps,
        tool_repeat_threshold=tool_repeat,
    )
    cfg.escalation = EscalationConfig(
        diversification_max_steps=div_max_steps,
        backtrack_to_last_checkpoint=backtrack,
    )
    cfg.telegram = TelegramConfig()  # disabled (no token)
    cfg.roles["executor"] = RoleConfig(provider="fake", timeout_s=5)
    return cfg


def _make_project(tmp_path: Path) -> tuple[Path, Path, Config, DiskLayer]:
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    plan = project / "plan.md"
    plan.write_text("# Plan\n1. do something\n")
    config = _make_config()
    config.telegram = TelegramConfig(bot_token="tok", chat_id="chat")
    return project, plan, config, disk


# ══════════════════════════════════════════════════════════════════════
# Part 1: stuck_detection.py
# ══════════════════════════════════════════════════════════════════════


class TestStuckDetectionRetry:
    """3+ retries → RETRY_EXHAUSTED signal."""

    def test_no_signal_below_threshold(self) -> None:
        state = AgentState(retry_count=2)
        result = check_stuck(state, StuckDetectionConfig(max_retries=3))
        assert result.signal == StuckSignal.NONE
        assert result.severity == 0

    def test_signal_at_threshold(self) -> None:
        state = AgentState(retry_count=3)
        result = check_stuck(state, StuckDetectionConfig(max_retries=3))
        assert result.signal == StuckSignal.RETRY_EXHAUSTED
        assert result.severity == 2

    def test_signal_above_threshold(self) -> None:
        state = AgentState(retry_count=5)
        result = check_stuck(state, StuckDetectionConfig(max_retries=3))
        assert result.signal == StuckSignal.RETRY_EXHAUSTED


class TestStuckDetectionStepLimit:
    """15+ steps on task → STEP_LIMIT signal."""

    def test_no_signal_below_threshold(self) -> None:
        state = AgentState(steps_in_task=14)
        result = check_stuck(state, StuckDetectionConfig(max_steps_per_task=15))
        assert result.signal == StuckSignal.NONE

    def test_signal_at_threshold(self) -> None:
        state = AgentState(steps_in_task=15)
        result = check_stuck(state, StuckDetectionConfig(max_steps_per_task=15))
        assert result.signal == StuckSignal.STEP_LIMIT
        assert result.severity == 2

    def test_step_limit_text_in_details(self) -> None:
        state = AgentState(steps_in_task=20)
        result = check_stuck(state, StuckDetectionConfig(max_steps_per_task=15))
        assert "20" in result.details


class TestStuckDetectionToolRepeat:
    """Same tool call repeated 2x → TOOL_REPEAT / deadlock signal."""

    def test_no_signal_first_call(self) -> None:
        state = AgentState()
        result = check_stuck(state, StuckDetectionConfig(tool_repeat_threshold=2))
        assert result.signal == StuckSignal.NONE

    def test_signal_at_threshold(self) -> None:
        state = AgentState(tool_repeat_count=2, last_tool_hash="abc123", tool_hash_updated=True)
        result = check_stuck(state, StuckDetectionConfig(tool_repeat_threshold=2))
        assert result.signal == StuckSignal.TOOL_REPEAT
        assert result.severity == 2

    def test_compute_tool_hash_stable(self) -> None:
        h1 = compute_tool_hash("read_file(path=foo.py)")
        h2 = compute_tool_hash("read_file(path=foo.py)")
        assert h1 == h2
        assert len(h1) == 16

    def test_compute_tool_hash_different_calls(self) -> None:
        h1 = compute_tool_hash("read_file(path=foo.py)")
        h2 = compute_tool_hash("write_file(path=bar.py)")
        assert h1 != h2

    def test_update_tool_hash_detects_repeat(self) -> None:
        state = AgentState()
        state, is_repeat = update_tool_hash(state, "read_file(path=foo.py)")
        assert not is_repeat
        assert state.tool_repeat_count == 0
        state, is_repeat = update_tool_hash(state, "read_file(path=foo.py)")
        assert is_repeat
        assert state.tool_repeat_count == 1

    def test_update_tool_hash_resets_on_new_call(self) -> None:
        state = AgentState()
        state, _ = update_tool_hash(state, "read_file(path=foo.py)")
        state, _ = update_tool_hash(state, "read_file(path=foo.py)")
        assert state.tool_repeat_count == 1
        state, is_repeat = update_tool_hash(state, "write_file(path=bar.py)")
        assert not is_repeat
        assert state.tool_repeat_count == 0

    def test_two_identical_calls_trigger_tool_repeat(self) -> None:
        """Integrated: three identical update_tool_hash calls with threshold=2 → TOOL_REPEAT."""
        state = AgentState()
        state, _ = update_tool_hash(state, "read_file(path=foo.py)")
        state, _ = update_tool_hash(state, "read_file(path=foo.py)")
        state, _ = update_tool_hash(state, "read_file(path=foo.py)")
        result = check_stuck(state, StuckDetectionConfig(tool_repeat_threshold=2))
        assert result.signal == StuckSignal.TOOL_REPEAT
        assert result.severity == 2

    def test_retry_exhausted_takes_priority_over_step_limit(self) -> None:
        state = AgentState(retry_count=5, steps_in_task=20)
        result = check_stuck(state, StuckDetectionConfig(max_retries=3, max_steps_per_task=15))
        assert result.signal == StuckSignal.RETRY_EXHAUSTED


# ══════════════════════════════════════════════════════════════════════
# Part 2: escalation.py — decide_escalation
# ══════════════════════════════════════════════════════════════════════


def _stuck(signal: StuckSignal = StuckSignal.RETRY_EXHAUSTED) -> StuckResult:
    return StuckResult(signal=signal, details="test stuck", severity=2)


def _no_stuck() -> StuckResult:
    return StuckResult(signal=StuckSignal.NONE, details="", severity=0)


class TestDecideEscalation:
    def test_no_stuck_returns_none_action(self) -> None:
        action = decide_escalation(_no_stuck(), EscalationLevel.NONE, 0, EscalationConfig())
        assert action.level == EscalationLevel.NONE
        assert action.inject_prompt == ""

    def test_first_stuck_goes_to_level_1(self) -> None:
        action = decide_escalation(_stuck(), EscalationLevel.NONE, 0, EscalationConfig())
        assert action.level == EscalationLevel.DIVERSIFICATION
        assert action.inject_prompt != ""
        assert not action.should_pause

    def test_level1_within_window_stays_at_level_1(self) -> None:
        action = decide_escalation(
            _stuck(),
            EscalationLevel.DIVERSIFICATION,
            0,
            EscalationConfig(diversification_max_steps=2),
        )
        assert action.level == EscalationLevel.DIVERSIFICATION
        assert action.inject_prompt != ""

    def test_level1_window_exhausted_goes_to_level_2(self) -> None:
        action = decide_escalation(
            _stuck(),
            EscalationLevel.DIVERSIFICATION,
            2,  # = diversification_max_steps
            EscalationConfig(diversification_max_steps=2),
        )
        assert action.level == EscalationLevel.BACKTRACK_COACH
        assert not action.should_trigger_coach  # Coach deferred in MVP2

    def test_level1_window_exceeded_goes_to_level_2(self) -> None:
        action = decide_escalation(
            _stuck(),
            EscalationLevel.DIVERSIFICATION,
            5,
            EscalationConfig(diversification_max_steps=2),
        )
        assert action.level == EscalationLevel.BACKTRACK_COACH

    def test_level2_stuck_goes_to_level_3(self) -> None:
        action = decide_escalation(
            _stuck(), EscalationLevel.BACKTRACK_COACH, 0, EscalationConfig()
        )
        assert action.level == EscalationLevel.HUMAN
        assert action.should_pause

    def test_backtrack_flag_from_config(self) -> None:
        action = decide_escalation(
            _stuck(),
            EscalationLevel.DIVERSIFICATION,
            10,
            EscalationConfig(backtrack_to_last_checkpoint=True),
        )
        assert action.should_backtrack is True

    def test_no_backtrack_when_disabled(self) -> None:
        action = decide_escalation(
            _stuck(),
            EscalationLevel.DIVERSIFICATION,
            10,
            EscalationConfig(backtrack_to_last_checkpoint=False),
        )
        assert action.should_backtrack is False


# ══════════════════════════════════════════════════════════════════════
# Part 3: escalation.py — execute_escalation + write_stuck_report
# ══════════════════════════════════════════════════════════════════════


class TestExecuteEscalationLevel1:
    """Level 1 diversification: state updated, notify called."""

    async def test_escalation_level1_updates_state(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        notifier = Notifier(TelegramConfig())
        checkpoint = CheckpointManager(disk)
        state = AgentState(phase=Phase.RUNNING)

        action = EscalationAction(
            level=EscalationLevel.DIVERSIFICATION, inject_prompt="try something else"
        )
        result = await execute_escalation(action, state, disk, notifier, checkpoint)
        assert result.escalation_level == EscalationLevel.DIVERSIFICATION.value

    async def test_escalation_level1_sends_notification(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        notified = []

        class _FakeNotifier(Notifier):
            async def notify(self, text: str, level=NotifyLevel.PROGRESS) -> bool:
                notified.append((text, level))
                return True

        state = AgentState(phase=Phase.RUNNING)
        checkpoint = CheckpointManager(disk)
        action = EscalationAction(level=EscalationLevel.DIVERSIFICATION, inject_prompt="try again")
        await execute_escalation(action, state, disk, _FakeNotifier(TelegramConfig()), checkpoint)
        assert any(lvl == NotifyLevel.STUCK for _, lvl in notified)


class TestExecuteEscalationLevel2:
    """Level 2 backtrack: counters reset, EVENT_JOURNAL written."""

    async def test_backtrack_resets_counters(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        state = AgentState(
            phase=Phase.RUNNING,
            retry_count=3,
            steps_in_task=12,
            tool_repeat_count=2,
            last_tool_hash="abc",
        )
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())
        action = EscalationAction(
            level=EscalationLevel.BACKTRACK_COACH,
            should_backtrack=True,
        )
        result = await execute_escalation(
            action, state, disk, notifier, checkpoint, stuck_result=_stuck()
        )
        assert result.retry_count == 0
        assert result.steps_in_task == 0
        assert result.tool_repeat_count == 0
        assert result.last_tool_hash == ""

    async def test_backtrack_writes_event_journal(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        state = AgentState(phase=Phase.RUNNING)
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())
        action = EscalationAction(level=EscalationLevel.BACKTRACK_COACH, should_backtrack=True)
        await execute_escalation(action, state, disk, notifier, checkpoint, stuck_result=_stuck())
        journal = disk.read_file("persistent/EVENT_JOURNAL.md")
        assert "Level 2" in journal
        assert "retry_exhausted" in journal

    async def test_no_backtrack_when_disabled(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        state = AgentState(phase=Phase.RUNNING, retry_count=3, steps_in_task=10)
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())
        action = EscalationAction(level=EscalationLevel.BACKTRACK_COACH, should_backtrack=False)
        result = await execute_escalation(action, state, disk, notifier, checkpoint)
        # Counters NOT reset (backtrack disabled)
        assert result.retry_count == 3
        assert result.steps_in_task == 10


class TestExecuteEscalationLevel3:
    """Level 3 human: STUCK_REPORT.md written, runner paused."""

    async def test_stuck_report_written(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        state = AgentState(phase=Phase.RUNNING, current_task="task-1", steps_in_task=5)
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())
        action = EscalationAction(level=EscalationLevel.HUMAN, should_pause=True)
        await execute_escalation(
            action,
            state,
            disk,
            notifier,
            checkpoint,
            stuck_result=_stuck(StuckSignal.STEP_LIMIT),
            escalation_history=[EscalationLevel.DIVERSIFICATION, EscalationLevel.BACKTRACK_COACH],
        )
        report = disk.read_file("human/STUCK_REPORT.md")
        assert "Stuck Report" in report
        assert "step_limit" in report
        assert "Level 1 diversification" in report
        assert "Level 2 backtrack" in report

    async def test_runner_paused_after_level3(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        from tero2.checkpoint import CheckpointManager
        from tero2.notifier import Notifier

        state = AgentState(phase=Phase.RUNNING)
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())
        action = EscalationAction(level=EscalationLevel.HUMAN, should_pause=True)
        result = await execute_escalation(action, state, disk, notifier, checkpoint)
        assert result.phase == Phase.PAUSED


class TestWriteStuckReport:
    def test_report_contains_required_fields(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        state = AgentState(current_task="my-task", steps_in_task=7, retry_count=3)
        stuck = StuckResult(
            signal=StuckSignal.TOOL_REPEAT, details="hash abc repeated 2x", severity=2
        )
        write_stuck_report(disk, state, stuck, [EscalationLevel.DIVERSIFICATION])
        report = disk.read_file("human/STUCK_REPORT.md")
        assert "my-task" in report
        assert "tool_repeat" in report
        assert "7" in report
        assert "Level 1 diversification" in report
        assert "STEER.md" in report

    def test_report_overwrites_previous(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        state = AgentState()
        stuck = StuckResult(signal=StuckSignal.NONE, details="", severity=0)
        write_stuck_report(disk, state, stuck, [])
        write_stuck_report(disk, state, stuck, [EscalationLevel.DIVERSIFICATION])
        report = disk.read_file("human/STUCK_REPORT.md")
        assert report.count("Stuck Report") == 1


# ══════════════════════════════════════════════════════════════════════
# Part 4: Runner integration — stuck detection + escalation
# ══════════════════════════════════════════════════════════════════════


class TestRunnerStuckEscalation:
    """Runner integration: stuck detected → escalation triggered."""

    async def test_runner_reaches_level3_and_pauses(self, tmp_path: Path) -> None:
        """When all retries fail and stuck is detected, runner should pause (Level 3)."""
        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 6
        config.retry.chain_retry_wait_s = 0
        config.stuck_detection = StuckDetectionConfig(
            max_retries=1,  # stuck after just 1 retry
            max_steps_per_task=15,
            tool_repeat_threshold=2,
        )
        config.escalation = EscalationConfig(
            diversification_max_steps=1,  # escalate fast for testing
            backtrack_to_last_checkpoint=True,
        )

        notified: list[tuple[str, NotifyLevel]] = []

        runner = Runner(project, plan, config=config)

        async def _fake_notify(text: str, level=NotifyLevel.PROGRESS) -> bool:
            notified.append((text, level))
            return True

        runner.notifier.notify = _fake_notify  # type: ignore[method-assign]

        with patch.object(runner, "_build_chain", return_value=_AlwaysFailChain()):
            await runner.run()

        state = disk.read_state()
        stuck_notified = [t for t, lvl in notified if lvl == NotifyLevel.STUCK]
        # Strict assertion: escalation must have reached Level 3 (PAUSED).
        # Phase.FAILED would mean the escalation pipeline was never invoked.
        assert state.phase == Phase.PAUSED, (
            f"expected Level 3 escalation (PAUSED) but got phase={state.phase}, "
            f"escalation_level={state.escalation_level}, notified={notified}"
        )
        # At least one stuck-level notification should have been sent
        assert stuck_notified, f"expected stuck notification, got: {notified}"

    async def test_runner_injects_diversification_prompt_on_level1(self, tmp_path: Path) -> None:
        """Level 1: diversification prompt injected into effective_plan."""
        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 4
        config.retry.chain_retry_wait_s = 0
        config.stuck_detection = StuckDetectionConfig(
            max_retries=1,  # stuck after 1 retry
            max_steps_per_task=15,
            tool_repeat_threshold=2,
        )
        config.escalation = EscalationConfig(
            diversification_max_steps=3,  # stay at Level 1 long enough to observe
            backtrack_to_last_checkpoint=True,
        )

        capturing_chain = _CapturingChain()
        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

        with patch.object(runner, "_build_chain", return_value=capturing_chain):
            await runner.run()

        # Later attempts should have the diversification notice
        injected = [p for p in capturing_chain.plans if "Notice" in p or "dead end" in p]
        assert injected, (
            f"expected diversification prompt injection, got plans: {capturing_chain.plans}"
        )

    async def test_runner_writes_stuck_report_at_level3(self, tmp_path: Path) -> None:
        """Level 3: STUCK_REPORT.md is written to .sora/human/."""
        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 8
        config.retry.chain_retry_wait_s = 0
        config.reflexion.max_cycles = 7
        config.stuck_detection = StuckDetectionConfig(
            max_retries=1,
            max_steps_per_task=15,
            tool_repeat_threshold=2,
        )
        config.escalation = EscalationConfig(
            diversification_max_steps=1,
            backtrack_to_last_checkpoint=True,
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

        with patch.object(runner, "_build_chain", return_value=_AlwaysFailChain()):
            await runner.run()

        report = disk.read_file("human/STUCK_REPORT.md")
        assert report, "STUCK_REPORT.md should have been written"
        assert "Stuck Report" in report

    async def test_retry_max_equals_stuck_max_triggers_level3(self, tmp_path: Path) -> None:
        """Boundary: retry.max_retries == stuck_detection.max_retries.

        When both thresholds are equal, RETRY_EXHAUSTED only fires *after* the loop
        (the in-loop check at attempt>0 never sees retry_count == max_retries because
        increment_retry runs *after* a failed attempt).  The post-loop escalation path
        must catch this and escalate to Level 3, not silently call mark_failed.
        """
        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 3
        config.retry.chain_retry_wait_s = 0
        config.reflexion.max_cycles = 2
        config.stuck_detection = StuckDetectionConfig(
            max_retries=3,  # == retry.max_retries → RETRY_EXHAUSTED only fires post-loop
            max_steps_per_task=15,
            tool_repeat_threshold=2,
        )
        config.escalation = EscalationConfig(
            diversification_max_steps=2,
            backtrack_to_last_checkpoint=True,
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

        with patch.object(runner, "_build_chain", return_value=_AlwaysFailChain()):
            await runner.run()

        state = disk.read_state()
        # Strict Level 3 assertion: must be PAUSED, not FAILED.
        # Phase.FAILED here would prove the post-loop escalation path is missing.
        assert state.phase == Phase.PAUSED, (
            f"expected Level 3 (PAUSED) but got phase={state.phase}, "
            f"escalation_level={state.escalation_level}"
        )
        # STUCK_REPORT.md is a Level 3 artifact — it must exist
        report = disk.read_file("human/STUCK_REPORT.md")
        assert report, "STUCK_REPORT.md must be written when Level 3 is reached"
        assert "Stuck Report" in report


# ══════════════════════════════════════════════════════════════════════
# Part 5: Runner — mid-step stuck detection + per-attempt signal check
# ══════════════════════════════════════════════════════════════════════


class TestRunnerMidStepStuckDetection:
    """_run_agent detects TOOL_REPEAT mid-step; per-attempt boundary check fires on each retry."""

    async def test_tool_repeat_mid_step_aborts_run_agent(self, tmp_path: Path) -> None:
        """Three identical tool_result messages mid-step with threshold=2 → _run_agent returns False."""
        project, plan, config, disk = _make_project(tmp_path)
        # Default config has tool_repeat_threshold=2 — three identical calls trigger TOOL_REPEAT

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

        same_msg = {"type": "tool_result", "content": "same-repeated-action"}

        class _RepeatToolChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                yield same_msg  # first occurrence — hash stored, no repeat yet
                yield same_msg  # second — count=1, threshold=2 not met
                yield same_msg  # third — count=2 >= threshold=2 → TOOL_REPEAT → abort
                yield {"type": "turn_end"}  # never reached if abort works correctly

        state = AgentState(phase=Phase.RUNNING)
        ctx = runner._build_runner_context(state, None)
        success, _ = await ctx.run_agent(_RepeatToolChain(), "plan")

        assert not success, "Expected run_agent to return False on mid-step TOOL_REPEAT"

    async def test_tool_repeat_mid_step_increments_repeat_count(self, tmp_path: Path) -> None:
        """After mid-step TOOL_REPEAT abort, state.tool_repeat_count reflects the repeated call."""
        project, plan, config, disk = _make_project(tmp_path)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

        same_msg = {"type": "tool_result", "content": "repeated-tool-call"}

        class _RepeatToolChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                yield same_msg
                yield same_msg

        state = AgentState(phase=Phase.RUNNING)
        ctx = runner._build_runner_context(state, None)
        await ctx.run_agent(_RepeatToolChain(), "plan")
        state = ctx.state
        assert state.tool_repeat_count >= 1, (
            f"Expected tool_repeat_count >= 1 after mid-step TOOL_REPEAT, "
            f"got {state.tool_repeat_count}"
        )

    async def test_check_stuck_called_at_attempt_boundary(self, tmp_path: Path) -> None:
        """Per-attempt boundary: check_stuck fires on attempt > 0 for each retry."""
        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 3
        config.retry.chain_retry_wait_s = 0
        # High stuck threshold so in-loop checks return NONE (no early escalation)
        config.stuck_detection = StuckDetectionConfig(
            max_retries=10,
            max_steps_per_task=15,
            tool_repeat_threshold=2,
        )

        boundary_retry_counts: list[int] = []

        def tracking_check(state, cfg):
            boundary_retry_counts.append(state.retry_count)
            return check_stuck(state, cfg)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

        with (
            patch.object(runner, "_build_chain", return_value=_AlwaysFailChain()),
            patch("tero2.runner.check_stuck", tracking_check),
        ):
            await runner.run()

        # check_stuck called at each attempt boundary (attempt > 0) plus post-loop
        # With max_retries=3: retry_counts seen are [1, 2, 3]
        assert len(boundary_retry_counts) >= 2, (
            f"Expected per-attempt stuck checks on retries after attempt 0, "
            f"got {len(boundary_retry_counts)}: retry_counts={boundary_retry_counts}"
        )
        # All boundary calls see retry_count > 0 — check_stuck never fires at attempt 0
        assert all(rc > 0 for rc in boundary_retry_counts), (
            f"check_stuck fired at attempt 0 (retry_count=0), "
            f"but it must only fire at attempt > 0: {boundary_retry_counts}"
        )
