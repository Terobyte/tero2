"""Test: Agent crash -> auto-restart from last checkpoint.

Acceptance criterion (MVP0 spec section 6):
  Agent crash -> auto-restart from last checkpoint
  (test: kill -9 the provider subprocess mid-run, verify resume)
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import ProviderError
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain
from tero2.state import AgentState, Phase, SoraPhase


def _setup_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    plan = project / "plan.md"
    plan.write_text("# Plan\n1. A\n2. B\n3. C\n4. D\n5. E")
    config = Config()
    config.roles["executor"] = RoleConfig(provider="fake")
    config.telegram = TelegramConfig(bot_token="", chat_id="")
    return project, plan, config, disk


class _SlowChain:
    """Chain stand-in that yields messages with configurable delay.

    Simulates a real provider that streams tool_result messages over time,
    giving the runner opportunity to save checkpoints between messages.
    """

    def __init__(self, messages: list[dict], delay: float = 0.1):
        self._messages = list(messages)
        self._delay = delay
        self.current_provider_index = 0

    async def run_prompt(self, prompt: str):
        for msg in self._messages:
            yield msg
            await asyncio.sleep(self._delay)


class TestCrashAutoRestart:
    async def test_runner_crash_resumes_from_last_checkpoint(self, tmp_path):
        """
        Core crash-recovery acceptance test:

        1. SlowChain yields tool_result messages every 0.2s
        2. Runner saves checkpoint after each tool_result via increment_step
        3. After ~0.55s the runner task is cancelled (simulates kill -9)
        4. Disk checkpoint has: phase=RUNNING, steps_in_task >= 1
        5. New Runner restores checkpoint and completes successfully
        """
        from tero2.runner import Runner

        project, plan, config, disk = _setup_project(tmp_path)

        msgs = [{"type": "tool_result", "content": f"s{i}"} for i in range(1, 6)]
        runner1 = Runner(project, plan, config=config)
        slow = _SlowChain(msgs, delay=0.2)

        with patch.object(runner1, "_build_chain", return_value=slow):
            task = asyncio.create_task(runner1.run())
            await asyncio.sleep(0.55)
            task.cancel()
            try:
                await task
            except BaseException:
                pass

        st = disk.read_state()
        if st.phase == Phase.COMPLETED:
            pytest.skip("runner finished before cancel")

        assert st.phase == Phase.RUNNING
        saved_steps = st.steps_in_task
        assert saved_steps >= 1, f"expected >= 1 checkpointed step, got {saved_steps}"

        fast = _SlowChain([{"type": "tool_result", "content": "ok"}], delay=0.01)
        runner2 = Runner(project, plan, config=config)
        with patch.object(runner2, "_build_chain", return_value=fast):
            await runner2.run()

        assert disk.read_state().phase == Phase.COMPLETED

    async def test_kill_provider_subprocess_with_sigkill(self, tmp_path):
        """
        Real SIGKILL integration test:

        1. Python subprocess outputs JSON tool_result lines every 0.15s
        2. After 0.5s the subprocess receives SIGKILL
        3. Chain buffers partial output, discards on ProviderError
        4. ProviderError is not recoverable -> propagates to runner
        5. Runner crashes gracefully -- state on disk is consistent
        6. New runner restores state and completes successfully
        """
        from tero2.runner import Runner

        project, plan, config, disk = _setup_project(tmp_path)

        script = tmp_path / "slow_provider.py"
        script.write_text(
            "import json, sys, time\n"
            "for i in range(20):\n"
            "    json.dump({'type':'tool_result','content':f's{i}'},sys.stdout)\n"
            "    sys.stdout.write('\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.15)\n"
        )

        class _Sub(BaseProvider):
            def __init__(self, sp: Path):
                self._sp = sp
                self._proc: asyncio.subprocess.Process | None = None

            @property
            def display_name(self):
                return "sub"

            async def run(self, **kwargs):
                self._proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(self._sp),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert self._proc.stdout
                async for line in self._proc.stdout:
                    yield json.loads(line.decode().strip())
                await self._proc.wait()
                if self._proc.returncode != 0:
                    raise ProviderError(f"subprocess exited {self._proc.returncode}")

        prov = _Sub(script)
        chain = ProviderChain([prov], cb_registry=CircuitBreakerRegistry())

        runner = Runner(project, plan, config=config)
        with patch.object(runner, "_build_chain", return_value=chain):
            task = asyncio.create_task(runner.run())
            await asyncio.sleep(0.5)

            if prov._proc and prov._proc.returncode is None:
                prov._proc.send_signal(signal.SIGKILL)

            try:
                await asyncio.wait_for(task, timeout=5.0)
            except BaseException:
                pass

        st = disk.read_state()
        assert st.phase in (Phase.RUNNING, Phase.FAILED, Phase.IDLE)

        fast = _SlowChain([{"type": "tool_result", "content": "ok"}], delay=0.01)
        runner2 = Runner(project, plan, config=config)
        with patch.object(runner2, "_build_chain", return_value=fast):
            await runner2.run()

        assert disk.read_state().phase == Phase.COMPLETED

    async def test_sigkill_subprocess_mid_stream_verifies_resume(self, tmp_path):
        """
        Full kill -9 -> auto-restart cycle with real subprocess:

        1. Subprocess outputs tool_result JSON lines every 0.2s
        2. Custom streaming chain yields each line immediately (no buffering)
           so the runner saves checkpoints after each tool_result
        3. After ~0.55s, SIGKILL the subprocess
        4. ProviderError is caught as recoverable, runner retries
        5. State on disk: phase=RUNNING, retry_count >= 1
        6. New Runner restores from checkpoint and completes
        """
        from tero2.runner import Runner

        project, plan, config, disk = _setup_project(tmp_path)

        script = tmp_path / "streamer.py"
        script.write_text(
            "import json, sys, time\n"
            "for i in range(10):\n"
            "    json.dump({'type':'tool_result','content':f'step_{i}'},sys.stdout)\n"
            "    sys.stdout.write('\\n')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.2)\n"
        )

        class _StreamingSubprocessChain:
            """Wraps a subprocess and yields JSON lines as they arrive.

            Unlike ProviderChain which buffers all output, this yields
            immediately so the runner can save checkpoints incrementally.
            """

            def __init__(self, script_path: Path):
                self._script = script_path
                self._proc: asyncio.subprocess.Process | None = None
                self.current_provider_index = 0

            async def run_prompt(self, prompt: str):
                self._proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(self._script),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert self._proc.stdout
                async for line in self._proc.stdout:
                    yield json.loads(line.decode().strip())
                await self._proc.wait()
                if self._proc.returncode != 0:
                    raise ProviderError(f"subprocess exited {self._proc.returncode}")

        chain = _StreamingSubprocessChain(script)

        runner = Runner(project, plan, config=config)
        with patch.object(runner, "_build_chain", return_value=chain):
            task = asyncio.create_task(runner.run())
            await asyncio.sleep(0.55)

            if chain._proc and chain._proc.returncode is None:
                chain._proc.send_signal(signal.SIGKILL)

            try:
                await asyncio.wait_for(task, timeout=5.0)
            except BaseException:
                pass

        st = disk.read_state()
        if st.phase == Phase.COMPLETED:
            pytest.skip("runner finished before SIGKILL")

        assert st.phase == Phase.RUNNING, f"expected RUNNING, got {st.phase}"
        assert st.retry_count >= 1, (
            f"expected >= 1 retry after SIGKILL, got {st.retry_count}. "
            "Runner should catch ProviderError from killed subprocess and retry."
        )

        fast = _SlowChain([{"type": "tool_result", "content": "ok"}], delay=0.01)
        runner2 = Runner(project, plan, config=config)
        with patch.object(runner2, "_build_chain", return_value=fast):
            await runner2.run()

        final = disk.read_state()
        assert final.phase == Phase.COMPLETED


# ── CheckpointManager.set_sora_phase ─────────────────────────────────────────


class TestSetSoraPhase:
    """set_sora_phase() persists SoraPhase transitions and restores correctly."""

    def _make_checkpoint(self, tmp_path: Path) -> tuple[CheckpointManager, DiskLayer]:
        disk = DiskLayer(tmp_path)
        disk.init()
        cm = CheckpointManager(disk)
        return cm, disk

    def test_set_sora_phase_updates_state(self, tmp_path):
        """set_sora_phase sets state.sora_phase and returns updated state."""
        cm, disk = self._make_checkpoint(tmp_path)
        state = AgentState()
        updated = cm.set_sora_phase(state, SoraPhase.SCOUT)
        assert updated.sora_phase == SoraPhase.SCOUT

    def test_set_sora_phase_persists_to_disk(self, tmp_path):
        """set_sora_phase writes to disk so restore() returns the new phase."""
        cm, disk = self._make_checkpoint(tmp_path)
        state = AgentState()
        cm.set_sora_phase(state, SoraPhase.ARCHITECT)
        restored = cm.restore()
        assert restored.sora_phase == SoraPhase.ARCHITECT

    def test_set_sora_phase_sequence_all_values(self, tmp_path):
        """Each SoraPhase round-trips through disk correctly."""
        cm, disk = self._make_checkpoint(tmp_path)
        for phase in (
            SoraPhase.HARDENING,
            SoraPhase.SCOUT,
            SoraPhase.COACH,
            SoraPhase.ARCHITECT,
            SoraPhase.EXECUTE,
            SoraPhase.SLICE_DONE,
            SoraPhase.NONE,
        ):
            state = AgentState()
            cm.set_sora_phase(state, phase)
            restored = cm.restore()
            assert restored.sora_phase == phase, (
                f"expected {phase!r} on restore, got {restored.sora_phase!r}"
            )

    def test_set_sora_phase_does_not_alter_run_phase(self, tmp_path):
        """set_sora_phase only touches sora_phase; AgentState.phase is unchanged."""
        cm, disk = self._make_checkpoint(tmp_path)
        state = AgentState(phase=Phase.RUNNING)
        disk.write_state(state)
        updated = cm.set_sora_phase(state, SoraPhase.EXECUTE)
        restored = cm.restore()
        assert restored.phase == Phase.RUNNING
        assert restored.sora_phase == SoraPhase.EXECUTE

    def test_set_sora_phase_updates_last_checkpoint_timestamp(self, tmp_path):
        """save() is called internally, so last_checkpoint is updated."""
        cm, disk = self._make_checkpoint(tmp_path)
        state = AgentState()
        before = state.last_checkpoint
        updated = cm.set_sora_phase(state, SoraPhase.COACH)
        assert updated.last_checkpoint != before or updated.last_checkpoint != ""

    def test_set_sora_phase_returns_same_object(self, tmp_path):
        """set_sora_phase mutates and returns the passed-in state object."""
        cm, disk = self._make_checkpoint(tmp_path)
        state = AgentState()
        returned = cm.set_sora_phase(state, SoraPhase.HARDENING)
        assert returned is state

    def test_crash_recovery_restores_sora_phase(self, tmp_path):
        """Simulate crash mid-SORA: disk has sora_phase=EXECUTE; new CM restores it."""
        disk = DiskLayer(tmp_path)
        disk.init()
        # First CM writes state mid-pipeline
        cm1 = CheckpointManager(disk)
        state = AgentState(phase=Phase.RUNNING)
        cm1.set_sora_phase(state, SoraPhase.EXECUTE)

        # Second CM (new process after crash) restores from disk
        cm2 = CheckpointManager(disk)
        restored = cm2.restore()
        assert restored.sora_phase == SoraPhase.EXECUTE
        assert restored.phase == Phase.RUNNING
