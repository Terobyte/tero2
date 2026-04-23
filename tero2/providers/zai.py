"""Z.AI provider — runs Claude Code agent with Z.AI API (GLM-5.1)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tero2.constants import DEFAULT_CONTEXT_LIMIT
from tero2.providers.base import BaseProvider

_ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
_ZAI_DEFAULT_MODEL = "glm-5.1"
_DEFAULT_COMPACT_THRESHOLD = 0.8

# Inline context window table — tero2 has no config.get_context_window()
_CONTEXT_WINDOWS: dict[str, int] = {
    "glm-5.1": 128_000,
    "glm-4.7": 128_000,
    "glm": 128_000,
    "claude": 200_000,
    "gpt-4": 128_000,
    "gemini": 1_000_000,
}


def _get_context_window(model: str) -> int:
    model_lower = model.lower()
    for key, limit in _CONTEXT_WINDOWS.items():
        if key in model_lower:
            return limit
    return 0  # unknown model — caller's `or context_limit` fallback is reachable


try:
    from claude_agent_sdk import query, ClaudeAgentOptions  # type: ignore[import]

    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False
    query = None  # type: ignore[assignment]
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]


@dataclass
class ZaiConfig:
    """Configuration for the Z.AI provider."""

    claude_home: str = "~/.claude-zai"
    default_model: str = _ZAI_DEFAULT_MODEL


def _read_settings_key() -> str | None:
    """Read ZAI_API_KEY from ~/.claude-zai/settings.json."""
    p = Path.home() / ".claude-zai" / "settings.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Try top-level keys first, then nested env block
        return (
            data.get("api_key")
            or data.get("apiKey")
            or data.get("env", {}).get("ZAI_API_KEY")
            or data.get("env", {}).get("ANTHROPIC_AUTH_TOKEN")
        ) or None
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        # Disease 1: an undecodable settings.json must not crash provider
        # bootstrap — treat it the same as missing/invalid JSON.
        return None


def _load_token(claude_home: str) -> str:
    """Read ZAI_API_KEY from env, then fall back to settings.json in claude_home."""
    token = (
        os.environ.get("ZAI_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
    )
    if token:
        return token

    settings_path = Path(os.path.expanduser(claude_home)) / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            env_vals = data.get("env", {})
            return (
                env_vals.get("ZAI_API_KEY")
                or env_vals.get("ANTHROPIC_AUTH_TOKEN")
                or ""
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # Disease 1: token discovery must be robust to non-UTF-8
            # settings files (operator may have copied a BOM-prefixed file).
            pass
    return ""


def _make_compact_hooks(context_limit: int, threshold: float) -> dict:
    compact_at = int(context_limit * threshold)

    async def on_pre_compact(hook_input, tool_name, context) -> dict:
        return {
            "continue_": True,
            "systemMessage": (
                "Summarize the conversation compactly. Preserve: "
                "completed steps with proof, file paths changed, "
                "current implementation state, pending work. "
                f"Target: under {compact_at // 1000}k tokens."
            ),
        }

    return {
        "PreCompact": [{"matcher": None, "hooks": [on_pre_compact], "timeout": None}]
    }


class ZaiProvider(BaseProvider):
    """Z.AI provider — uses Claude Code agent loop with GLM-5.1 via api.z.ai."""

    def __init__(self, config: ZaiConfig | None = None):
        self.config = config or ZaiConfig()
        self._kind = "zai"

    async def run(  # type: ignore[override]
        self,
        prompt: str = "",
        system_prompt: str = "",
        working_dir: str = "",
        max_turns: int = 30,
        model: str = "",
        context_limit: int = DEFAULT_CONTEXT_LIMIT,
        compact_threshold: float = _DEFAULT_COMPACT_THRESHOLD,
        **kwargs,
    ):
        """Run a turn using the Z.AI API via Claude Code agent loop.

        Yields SDK messages as they stream in.
        """
        if not SDK_AVAILABLE:
            raise ImportError(
                "claude-agent-sdk not installed. Run: pip install claude-agent-sdk"
            )

        token = _load_token(self.config.claude_home)
        if not token:
            raise ValueError("No Z.AI auth token. Set ZAI_API_KEY env var.")

        resolved_model = model or self.config.default_model

        # Isolate CLAUDE_CONFIG_DIR per run so parallel tero instances don't conflict.
        base_claude_home = os.path.expanduser(self.config.claude_home)
        tmp_claude_home = None
        try:
            tmp_claude_home = tempfile.mkdtemp(prefix="claude-zai-run-")
            base_settings = Path(base_claude_home) / "settings.json"
            if base_settings.exists():
                shutil.copy2(base_settings, Path(tmp_claude_home) / "settings.json")
            env = {
                "ANTHROPIC_BASE_URL": _ZAI_BASE_URL,
                "ANTHROPIC_AUTH_TOKEN": token,
                "ANTHROPIC_MODEL": resolved_model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": resolved_model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": resolved_model,
                "CLAUDE_CONFIG_DIR": tmp_claude_home,
                "CLAUDECODE": "",
            }

            model_window = _get_context_window(resolved_model) or context_limit
            target_compact_tokens = int(context_limit * compact_threshold)
            adjusted_threshold = max(
                0.1, min(0.9, target_compact_tokens / model_window)
            )
            settings = json.dumps(
                {"autoCompactThreshold": round(adjusted_threshold, 3)}
            )

            options = ClaudeAgentOptions(
                system_prompt=system_prompt,
                cwd=working_dir or str(Path.cwd()),
                env=env,
                permission_mode="bypassPermissions",
                max_turns=max_turns,
                hooks=_make_compact_hooks(context_limit, compact_threshold),
                settings=settings,
            )

            async for message in query(prompt=prompt, options=options):
                yield message
        finally:
            if tmp_claude_home:
                shutil.rmtree(tmp_claude_home, ignore_errors=True)

    def check_ready(self) -> tuple[bool, str]:
        """Check if Z.AI provider is ready to use."""
        if not SDK_AVAILABLE:
            return (
                False,
                "claude-agent-sdk not installed. Run: pip install claude-agent-sdk",
            )
        key = os.environ.get("ZAI_API_KEY") or _read_settings_key()
        if not key:
            return False, "ZAI_API_KEY not set and ~/.claude-zai/settings.json not found"
        return True, ""

    @property
    def display_name(self) -> str:
        model = self.config.default_model
        lower = model.lower()
        if "glm-5.1" in lower:
            model_name = "GLM-5.1"
        elif "glm-4.7" in lower:
            model_name = "GLM-4.7"
        elif "glm" in lower:
            model_name = "GLM"
        else:
            model_name = model.split("/")[-1][:10]
        return f"ZAI ({model_name})"
