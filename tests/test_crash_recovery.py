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

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import ProviderError
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain
from tero2.state import Phase


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
        4. Generator stops, runner task ends with error
        5. State on disk: phase=RUNNING, steps_in_task >= 1
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
        assert st.steps_in_task >= 1, (
            f"expected >= 1 checkpointed step after SIGKILL, got {st.steps_in_task}"
        )

        fast = _SlowChain([{"type": "tool_result", "content": "ok"}], delay=0.01)
        runner2 = Runner(project, plan, config=config)
        with patch.object(runner2, "_build_chain", return_value=fast):
            await runner2.run()

        final = disk.read_state()
        assert final.phase == Phase.COMPLETED
