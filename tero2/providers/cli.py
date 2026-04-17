"""CLI provider — wraps external CLI tools (opencode, codex, kilo, claude)."""

from __future__ import annotations

import asyncio
import json
import logging
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

        if stdin_data and proc.stdin:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

        assert proc.stdout is not None
        stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None

        lines: list[str] = []
        async for line in proc.stdout:
            lines.append(line.decode(errors="replace"))

        stderr_bytes = await stderr_task if stderr_task else b""
        await proc.wait()

        if proc.returncode != 0:
            err_msg = stderr_bytes.decode(errors="replace").strip()
            log.error("%s exited %d: %s", self._name, proc.returncode, err_msg)
            raise ProviderError(f"{self._name} exited {proc.returncode}: {err_msg}")

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    yield parsed
                else:
                    yield {"type": "text", "text": stripped}
            except json.JSONDecodeError:
                yield {"type": "text", "text": stripped}

        yield {"type": "turn_end", "text": ""}
