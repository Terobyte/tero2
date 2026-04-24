"""CLI provider — wraps external CLI tools (opencode, codex, kilo, claude)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

from tero2.config import Config
from tero2.errors import ProviderError
from tero2.providers.base import BaseProvider

log = logging.getLogger(__name__)

_PROVIDER_COMMAND_BUILDERS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "kilo": "kilo",
}


class CLIProvider(BaseProvider):
    def __init__(
        self,
        name: str,
        config: Config | None = None,
        *,
        model_override: str = "",
        working_dir: str = "",
    ) -> None:
        self._name = name
        self._kind = name
        self._config = config
        self._default_model = ""
        self._working_dir = working_dir
        self._extra_env: dict[str, str] = {}
        self._provider_cfg: dict[str, Any] = {}
        if config and name in config.providers:
            self._provider_cfg = config.providers[name]
            self._default_model = self._provider_cfg.get("default_model", "")
        if model_override:
            self._default_model = model_override

    @property
    def display_name(self) -> str:
        return self._name

    @property
    def command(self) -> str:
        cfg_cmd = self._provider_cfg.get("command", "") if self._provider_cfg else ""
        return cfg_cmd or _PROVIDER_COMMAND_BUILDERS.get(self._name, self._name)

    def _build_cmd_claude(self, prompt: str) -> tuple[list[str], bytes, dict[str, str]]:
        cmd_name = self.command
        cmd = [
            cmd_name,
            "-p",
            "--verbose",
            "--max-turns",
            "30",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "stream-json",
            "--settings",
            json.dumps({"autoCompactThreshold": 0.99}),
        ]
        if self._default_model:
            cmd.extend(["--model", self._default_model])
        stdin_data = prompt.encode("utf-8")
        cmd.append("-")
        env_block = {
            k: ""
            for k in [
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL",
                "ZAI_API_KEY",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "CLAUDE_CONFIG_DIR",
            ]
        }
        return cmd, stdin_data, env_block

    def _build_cmd_codex(self, prompt: str) -> tuple[list[str], bytes, dict[str, str]]:
        cmd_name = self.command
        cmd = [
            cmd_name,
            "exec",
            "--json",
            "-C",
            self._working_dir or str(Path.cwd()),
        ]
        if self._provider_cfg.get("bypass_approvals", False):
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        if self._provider_cfg.get("ephemeral", False):
            cmd.append("--ephemeral")
        if self._default_model:
            cmd.extend(["-m", self._default_model])
        cmd.append("-")
        stdin_data = prompt.encode("utf-8")
        env_block: dict[str, str] = {}
        return cmd, stdin_data, env_block

    def _build_cmd_opencode(self, prompt: str) -> tuple[list[str], bytes, dict[str, str]]:
        cmd_name = self.command
        cmd = [
            cmd_name,
            "run",
            "--format",
            "json",
            "--dir",
            self._working_dir or str(Path.cwd()),
        ]
        if self._default_model:
            cmd.extend(["-m", self._default_model])
        cmd.append("-")
        stdin_data = prompt.encode("utf-8")
        return cmd, stdin_data, {}

    def _build_cmd_kilo(self, prompt: str) -> tuple[list[str], bytes, dict[str, str]]:
        cmd_name = self.command
        cmd = [
            cmd_name,
            "run",
            "--format",
            "json",
            "--dir",
            self._working_dir or str(Path.cwd()),
        ]
        if self._default_model:
            cmd.extend(["-m", self._default_model])
        cmd.append("-")
        stdin_data = prompt.encode("utf-8")
        return cmd, stdin_data, {}

    async def _stream_events(self, proc: Any) -> Any:
        """Stream and yield parsed JSON events from a subprocess stdout.

        Extracted for testability.  Non-dict JSON raises ProviderError (A43 bug).
        On stdout exception, stderr_task is cancelled before drain (A46 bug —
        data may be lost).

        Events are yielded inside the stdout read loop (no intermediate
        buffering), so callers receive each parsed message as soon as it
        arrives rather than after ``proc.wait()`` returns.  If stdout raises,
        the stderr task is cancelled immediately and the exception propagates.
        """
        from contextlib import suppress

        stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None

        try:
            async for line in proc.stdout:
                stripped = line.decode(errors="replace").strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    log.warning("non-json line from %s: %r", self._name, stripped)
                    yield {"type": "text", "text": stripped}
                    continue
                if isinstance(parsed, dict):
                    yield parsed
                else:
                    raise ProviderError(
                        f"{self._name}: non-dict JSON response: {stripped!r}"
                    )
        except Exception:
            if stderr_task is not None:
                # Drain stderr (A46): await first so buffered error data isn't
                # lost; then cancel on timeout / completion.
                try:
                    await asyncio.wait_for(asyncio.shield(stderr_task), timeout=5.0)
                except Exception:
                    pass
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
            raise

        stderr_bytes = b""
        if stderr_task is not None:
            try:
                stderr_bytes = stderr_task.result() if stderr_task.done() else await stderr_task
            except Exception:
                stderr_bytes = b""
        await proc.wait()

        if proc.returncode != 0:
            err_msg = stderr_bytes.decode(errors="replace").strip()
            log.error("%s exited %d: %s", self._name, proc.returncode, err_msg)
            raise ProviderError(f"{self._name} exited {proc.returncode}: {err_msg}")

        yield {"type": "turn_end", "text": ""}

    async def run(self, **kwargs: Any) -> Any:
        prompt = kwargs.get("prompt", "")
        builder = {
            "claude": self._build_cmd_claude,
            "codex": self._build_cmd_codex,
            "opencode": self._build_cmd_opencode,
            "kilo": self._build_cmd_kilo,
        }.get(self._name)

        if builder is None:
            cmd = [self._name]
            if self._default_model:
                cmd.extend(["--model", self._default_model])
            cmd.append("-")
            stdin_data = prompt.encode("utf-8")
            env_override: dict[str, str] = {}
        else:
            cmd, stdin_data, env_override = builder(prompt)

        log.info("running %s", cmd[0])

        import os

        env = dict(os.environ)
        env.update(env_override)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Bug 146 + L1: we need a concurrent stdout reader so the child
        # doesn't block on a full stdout pipe buffer while we write stdin
        # (classic deadlock — Bug 146). But the old `_drain_stdout_bg`
        # discarded the events it read, racing with `_stream_events` for
        # the same data (Bug L1). Fix: the concurrent reader pushes every
        # line into a Queue and `_stream_events` consumes from a wrapper
        # that replaces `proc.stdout`. Capture the ORIGINAL stdout into a
        # local before swapping to avoid the pump reading its own queue.
        stdout_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        stdout_task: asyncio.Task | None = None
        _original_stdout = proc.stdout

        async def _stdout_pump() -> None:
            try:
                if _original_stdout is not None:
                    async for line in _original_stdout:
                        await stdout_queue.put(line)
            finally:
                await stdout_queue.put(None)

        if stdin_data and proc.stdin:
            if _original_stdout is not None:
                stdout_task = asyncio.create_task(_stdout_pump())
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            except BrokenPipeError as exc:
                if stdout_task is not None:
                    stdout_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await stdout_task
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                await proc.wait()
                raise ProviderError(
                    f"Broken pipe writing to {self._name}: "
                    f"process exited before stdin was sent"
                ) from exc
            finally:
                try:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (OSError, BrokenPipeError):
                    pass

        if proc.stdout is None:
            raise ProviderError(f"{self._name}: subprocess stdout is None")

        if stdout_task is not None:
            class _QueueStdout:
                def __init__(self, q: asyncio.Queue[bytes | None]) -> None:
                    self._q = q

                def __aiter__(self) -> "_QueueStdout":
                    return self

                async def __anext__(self) -> bytes:
                    item = await self._q.get()
                    if item is None:
                        raise StopAsyncIteration
                    return item

            proc.stdout = _QueueStdout(stdout_queue)  # type: ignore[assignment]

        try:
            async for event in self._stream_events(proc):
                yield event
        finally:
            if stdout_task is not None and not stdout_task.done():
                stdout_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await stdout_task
            # If the consumer broke out of iteration early (cancellation, break,
            # exception), _stream_events may not have awaited process completion.
            # Force the subprocess to exit so we don't leak a zombie.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except (asyncio.TimeoutError, Exception):
                    pass


# Alias for backward compatibility and test imports.
CliProvider = CLIProvider
