"""Halal tests for bugs found in autonomous loop iter1 (2026-04-23).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug L1  providers/cli: _drain_stdout_bg steals events from _stream_events
  Bug L2  context_assembly: HARD_FAIL at final check is not raised
  Bug L3  runner: max_slices=1 fires spurious "limit reached" ERROR
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest


# -- Bug L1: providers/cli _drain_stdout_bg steals stdout events --------------


class _FakeStdin:
    """Minimal stdin that records writes and closes cleanly."""

    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeStdoutReplay:
    """Async iterator that yields pre-set lines, one await per line."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> "_FakeStdoutReplay":
        return self

    async def __anext__(self) -> bytes:
        # Yield to the event loop so the drainer task has a chance to run
        # interleaved with _stream_events -- mirrors real subprocess I/O.
        await asyncio.sleep(0)
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _FakeProcWithStdin:
    """Fake process with stdin, stdout, stderr -- matches real subprocess shape."""

    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdoutReplay(stdout_lines)
        self.stderr = _FakeStderr()
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    def kill(self) -> None:
        pass


class TestLoopIter1DrainStdoutStealsEvents:
    """providers/cli.py lines 241-246: the `_drain_stdout_bg` task runs
    concurrently with `_stream_events` on the SAME `proc.stdout` stream.

    When `stdin_data` is non-empty (every real-world call -- prompt is
    always sent via stdin), `CLIProvider.run` spawns a background task
    that does `async for _ in proc.stdout: pass` -- a never-cancelled
    drainer. Then, after closing stdin, `run` enters `_stream_events(proc)`
    which ALSO iterates `proc.stdout`.

    Both tasks race to read each line from the same async iterator.  The
    background drainer silently consumes events that callers expected to
    receive, so a multi-line agent response arrives partial/empty.

    User-observable impact: builder prompts produce fewer events than sent.
    Downstream, `_stream_events` will see fewer tool_use/text events and may
    declare the task failed because no output was parsed.

    Fix: cancel `stdout_task` after stdin is closed (before entering
    `_stream_events`), OR use a Queue pipeline so both readers see the same
    data, OR avoid the background drainer entirely and interleave via
    asyncio.gather while writing stdin.
    """

    async def test_all_stdout_events_yielded_when_stdin_is_written(self) -> None:
        """Given 3 JSON lines on stdout and a prompt on stdin, the consumer
        must receive all 3 events (plus a final turn_end).  The bug causes
        the background drainer to steal some or all of them.
        """
        from tero2.providers.cli import CLIProvider

        lines = [
            b'{"type":"text","content":"first"}\n',
            b'{"type":"text","content":"second"}\n',
            b'{"type":"text","content":"third"}\n',
        ]
        proc = _FakeProcWithStdin(lines)

        async def fake_spawn(*_a: Any, **_kw: Any) -> _FakeProcWithStdin:
            return proc

        with patch("asyncio.create_subprocess_exec", fake_spawn):
            provider = CLIProvider("claude")
            events: list[dict] = []
            async for ev in provider.run(prompt="hello agent"):
                events.append(ev)

        # Expected: 3 text events + 1 turn_end = 4 total.  Bug: the bg drainer
        # consumes most/all of the text events, leaving only turn_end visible.
        text_events = [e for e in events if e.get("type") == "text"]
        assert len(text_events) == 3, (
            f"Bug L1: CLIProvider.run yielded {len(text_events)} text events, "
            f"expected 3.  _drain_stdout_bg task steals events from "
            f"_stream_events. Got events: {events!r}\n"
            f"Fix: cancel stdout_task after stdin close, before "
            f"iterating _stream_events(proc)."
        )


# -- Bug L2: context_assembly HARD_FAIL on final check not raised -------------


class TestLoopIter1AssemblerHardFailNotRaised:
    """context_assembly.py `assemble()` returns an AssembledPrompt with
    `budget_state=HARD_FAIL` when `system_prompt` alone pushes total
    over the hard-fail threshold -- but does NOT raise
    `ContextWindowExceededError`.

    The upfront check at line 143 only considers
    `mandatory_tokens = estimate_tokens(mandatory_user)` (the task_plan
    section), NOT `system_prompt + mandatory_user`.  So a 100K-token
    system_prompt with a tiny task_plan passes line 145's upfront
    hard-fail check, and the final total hits HARD_FAIL at line 190 but
    is silently returned in the result.

    User-observable impact: harden_phase.py builds the assembler with
    reviewer personas as system_prompt (harden_phase.py line 68-74) and
    calls `assemble_reviewer`.  If the reviewer persona is large relative
    to role.context_window, the phase sends a prompt known to be
    over-budget, which the provider rejects with an API error that
    surfaces as "provider failed" rather than a clean budget error.

    Fix: at the end of assemble (line ~190-196), when final status is
    HARD_FAIL, raise `ContextWindowExceededError(total, budget)` instead
    of returning a budget_state=HARD_FAIL result.  Alternatively, the
    upfront check at line 143 should use
    `estimate_tokens(system_prompt + mandatory_user)` so the error is
    raised before optional sections are even considered.
    """

    def test_system_prompt_overflow_raises_context_window_exceeded(self) -> None:
        from tero2.config import Config, RoleConfig
        from tero2.context_assembly import ContextAssembler, ContextWindowExceededError

        cfg = Config()
        cfg.roles["builder"] = RoleConfig(provider="mock", context_window=1000)
        assembler = ContextAssembler(cfg)

        # 100K chars ~ 25K tokens -- far over the effective budget
        # (context_window=1000 * target_ratio=0.6 = 600 tokens).
        massive_system = "X" * 100_000

        with pytest.raises(ContextWindowExceededError):
            assembler.assemble(
                role="builder",
                system_prompt=massive_system,
                task_plan="tiny task",
            )


# -- Bug L3: runner max_slices=1 fires spurious "limit reached" ---------------


class TestLoopIter1MaxSlicesOneFalseLimit:
    """runner.py `_execute_sora`, `while extra_slices_done < max_slices - 1`
    construct (lines 742-809).

    When `config.max_slices == 1` (user wants to run only S01, no extra
    slices), the condition `0 < 0` is False from the start -- the loop
    body never executes.  Python's while..else fires the else branch
    (line 802-809) which:

      1. Sets limit_reached = True
      2. Sends a Telegram notification: "extra slice limit reached ..."
      3. Calls self._emit_error(msg)

    Meanwhile `_slice_loop_completed` stays False, so the `mark_completed`
    branch at line 811 is skipped -- the run state is left in EXECUTE
    phase forever despite S01 having completed.

    Reproduction scenario: User sets `sora.max_slices = 1` in config.
    They expect: "tero2 runs S01 and marks run COMPLETED".
    They observe: spurious ERROR notification + run state stuck in
    EXECUTE phase + `tero2 status` shows "running" forever.

    Fix: before the while, early-return (or mark completed) when
    max_slices <= 1 AND S01 completed cleanly.  Alternatively, convert
    while..else to an explicit counter that only flags limit_reached
    when extra_slices_done == max_slices - 1 AND the loop was entered
    at least once.
    """

    async def test_max_slices_one_does_not_emit_spurious_error(self) -> None:
        """End-to-end sanity: with max_slices=1, S01 completes -> run is
        marked COMPLETED and NO 'limit reached' error is emitted.
        """
        from tero2.runner import Runner
        from tero2.state import Phase, SoraPhase

        # Build a Runner without calling __init__ (too many deps).
        runner = Runner.__new__(Runner)

        # Emission tracking.
        error_msgs: list[str] = []
        done_emitted: list[bool] = []

        async def fake_emit_phase(_phase: SoraPhase) -> None:
            pass

        async def fake_emit_error(msg: str) -> None:
            error_msgs.append(msg)

        async def fake_emit_done() -> None:
            done_emitted.append(True)

        runner._emit_phase = fake_emit_phase  # type: ignore[method-assign]
        runner._emit_error = fake_emit_error  # type: ignore[method-assign]
        runner._emit_done = fake_emit_done  # type: ignore[method-assign]

        class _FakeNotifier:
            async def notify(self, text: str, level: Any) -> bool:
                return True

        runner.notifier = _FakeNotifier()

        async def fake_drain_commands(state: Any) -> tuple[Any, bool]:
            return state, True

        runner._drain_commands = fake_drain_commands  # type: ignore[method-assign]

        # Minimal checkpoint fake.
        class _FakeCheckpoint:
            def set_sora_phase(self, state: Any, phase: SoraPhase) -> Any:
                state.sora_phase = phase
                return state

            def mark_failed(self, state: Any, msg: str) -> Any:
                state.phase = Phase.FAILED
                state.error = msg
                return state

            def mark_completed(self, state: Any) -> Any:
                state.phase = Phase.COMPLETED
                return state

            def save(self, state: Any) -> Any:
                return state

        runner.checkpoint = _FakeCheckpoint()

        # Config: max_slices=1, SORA with builder role only (skip harden/scout/coach).
        class _RoleCfg:
            provider = "mock"
            model = ""
            fallback: list[str] = []
            timeout_s = 60
            context_window = 128000

        class _Config:
            max_slices = 1
            roles = {"builder": _RoleCfg()}

        runner.config = _Config()

        # State: architect + execute have NOT been done yet -- fresh SORA run.
        class _State:
            phase = Phase.RUNNING
            sora_phase = SoraPhase.NONE
            current_slice = "S01"
            current_task_index = 0
            retry_count = 0
            plan_file = ""

        state = _State()
        runner._current_state = state  # type: ignore[attr-defined]
        runner.plan_file = None  # type: ignore[attr-defined]

        # Fake disk just provides a project_path.
        from pathlib import Path

        class _Disk:
            project_path = Path("/tmp/tero2_loop_iter1_test")

            def write_file(self, *_a: Any, **_kw: Any) -> bool:
                return True

            def read_file(self, *_a: Any, **_kw: Any) -> str:
                return ""

        runner.disk = _Disk()

        def fake_build_ctx(state: Any, shutdown: Any) -> Any:
            from tero2.phases.context import RunnerContext

            return RunnerContext(
                config=runner.config,
                disk=runner.disk,
                state=state,
                milestone_path="m/M001",
            )

        runner._build_runner_context = fake_build_ctx  # type: ignore[method-assign]

        # Architect + execute succeed; no next slice available.
        from tero2.phases.context import PhaseResult
        from tero2.players.architect import SlicePlan

        async def fake_run_architect(ctx: Any, slice_id: str = "S01") -> PhaseResult:
            return PhaseResult(
                success=True,
                data={"slice_plan": SlicePlan(slice_id=slice_id, slice_dir=f"m/{slice_id}")},
            )

        async def fake_run_execute(ctx: Any, plan: Any) -> PhaseResult:
            return PhaseResult(success=True, data={"slice_id": "S01", "completed": {}})

        with patch("tero2.runner.run_architect", fake_run_architect), \
             patch("tero2.runner.run_execute", fake_run_execute):
            await runner._execute_sora(state, shutdown_event=None)

        # Bug present: error_msgs contains "extra slice limit reached..."
        # Fix: no spurious limit error.
        limit_errors = [m for m in error_msgs if "limit reached" in m.lower()]
        assert not limit_errors, (
            f"Bug L3: max_slices=1 triggered spurious 'limit reached' error: "
            f"{limit_errors!r}. The while..else branch fires the error even "
            f"though the loop never entered.  Fix: skip the limit_reached "
            f"notification when max_slices <= 1 (user chose to run only S01)."
        )
        # And a successful single-slice run should mark the run DONE.
        assert done_emitted, (
            "Bug L3: with max_slices=1 and S01 completing, the runner should "
            "mark the run COMPLETED and emit 'done' -- but the while..else "
            "bug means _slice_loop_completed stays False, so mark_completed "
            "is skipped.  Run state is left stuck in EXECUTE phase."
        )
