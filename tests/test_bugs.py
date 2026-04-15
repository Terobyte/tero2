"""TDD proof-of-bug tests for OPEN bugs (13-17). RED = bug confirmed."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.checkpoint import CheckpointManager
from tero2.config import Config, RetryConfig, _parse_config
from tero2.errors import (
    ProviderError,
    ProviderNotReadyError,
    ProviderTimeoutError,
    RateLimitError,
)
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain, _is_recoverable_error
from tero2.state import AgentState, Phase


class FakeProvider(BaseProvider):
    def __init__(self, name: str, items: list[Any] | None = None, error=None):
        self._name = name
        self._items = items or []
        self._error = error

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs):
        for item in self._items:
            yield item
        if self._error:
            raise self._error


# ── Bug 13: ProviderError from CLI crash kills runner ──────────────


class TestBug13ProviderErrorKillsRunner:
    async def _collect(self, gen):
        out = []
        async for item in gen:
            out.append(item)
        return out

    async def test_chain_tries_fallback_on_providererror(self):
        fallback = FakeProvider("fallback", items=["ok"])
        crasher = FakeProvider("crasher", error=ProviderError("cli segfaulted"))
        chain = ProviderChain([crasher, fallback], cb_registry=CircuitBreakerRegistry())
        collected = await self._collect(chain.run(prompt="x"))
        assert collected == ["ok"], (
            "ProviderError from a crashed CLI tool should be treated as recoverable "
            "so the chain tries the next provider. Instead it re-raises immediately, "
            "killing the entire runner."
        )

    async def test_run_agent_returns_false_on_providererror(self):
        from tero2.runner import Runner

        runner = Runner.__new__(Runner)
        runner.config = _parse_config({})
        runner.config.roles["executor"] = MagicMock(timeout_s=30)
        runner.checkpoint = MagicMock()
        runner.checkpoint.increment_step = MagicMock(return_value=AgentState(phase=Phase.RUNNING))

        chain = ProviderChain(
            [FakeProvider("crasher", error=ProviderError("cli segfaulted"))],
            cb_registry=CircuitBreakerRegistry(),
        )

        state = AgentState(phase=Phase.RUNNING, provider_index=0)
        result = await runner._run_agent(chain, "do something", state)
        assert result is False, (
            "_run_agent should catch ProviderError and return False instead of "
            "letting it propagate and crash the entire runner process."
        )


# ── Bug 14: config.retry.max_retries ignored ───────────────────────


class TestBug14MaxRetriesIgnored:
    def test_runner_uses_config_max_retries_not_constant(self):
        import tero2.runner as runner_mod

        source = inspect.getsource(runner_mod.Runner._execute_plan)
        assert "MAX_TASK_RETRIES" not in source, (
            "_execute_plan uses hardcoded MAX_TASK_RETRIES constant "
            "instead of self.config.retry.max_retries — user config is ignored."
        )

    def test_config_stores_custom_max_retries(self):
        cfg = _parse_config({"retry": {"max_retries": 10}})
        assert cfg.retry.max_retries == 10


# ── Bug 15: No max_steps_per_task enforcement ──────────────────────


class TestBug15NoMaxStepsEnforcement:
    def test_increment_step_raises_at_limit(self):
        disk = MagicMock()
        cm = CheckpointManager(disk)
        state = AgentState(phase=Phase.RUNNING, steps_in_task=14)
        state = cm.increment_step(state)
        assert state.steps_in_task == 15
        with pytest.raises((RuntimeError, ValueError), match="max_steps"):
            cm.increment_step(state)

    def test_increment_step_source_has_limit_check(self):
        source = inspect.getsource(CheckpointManager.increment_step)
        assert "max_steps" in source, (
            "increment_step() has no step limit check — runaway agents loop forever. "
            "Should check state.steps_in_task >= config.max_steps_per_task."
        )


# ── Bug 16: lock.release() deletes other process's lock ─────────────


class TestBug16LockReleaseDeletesOtherProcessLock:
    def test_release_without_acquire_preserves_lock_file(self, tmp_path):
        from tero2.lock import FileLock

        lock = FileLock(tmp_path / "test.lock")
        lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock.lock_path.write_text("99999\n")
        assert lock.lock_path.exists()

        lock._fd = None
        lock.release()

        assert lock.lock_path.exists(), (
            "release() deleted the lock file even though _fd is None (lock not held "
            "by us). This happens when acquire() raises LockHeldError and the finally "
            "block calls release() — it deletes another process's lock file."
        )

    def test_release_skips_unlink_when_fd_is_none(self, tmp_path):
        from tero2.lock import FileLock

        lock = FileLock(tmp_path / "guard.lock")
        lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock.lock_path.write_text("42\n")
        lock._fd = None
        lock.release()
        assert lock.lock_path.exists(), (
            "release() must not unlink when _fd is None. The unlink is outside "
            "the 'if self._fd is not None' guard in lock.py:46."
        )


# ── Bug 17: Telegram Markdown fails on special chars ────────────────


class TestBug17TelegramMarkdownSpecialChars:
    async def test_send_does_not_pass_raw_markdown(self):
        from tero2.config import TelegramConfig
        from tero2.notifier import Notifier

        notifier = Notifier(TelegramConfig(bot_token="tok", chat_id="chat"))

        captured_data = {}

        def fake_post(url, data=None, **kwargs):
            captured_data.update(data or {})
            resp = MagicMock()
            resp.status_code = 200
            return resp

        with patch("tero2.notifier.requests.post", fake_post):
            await notifier.send("error in step_1: `code` failed [done]")

        sent_text = captured_data.get("text", "")
        parse_mode = captured_data.get("parse_mode", "")
        has_special = "_" in sent_text or "`" in sent_text or "[" in sent_text
        assert not has_special or parse_mode != "Markdown", (
            f"send() passes raw text with Markdown special chars to Telegram with "
            f"parse_mode='Markdown'. Underscores, backticks, brackets cause HTTP 400."
        )

    def test_send_source_escapes_or_drops_markdown(self):
        from tero2.notifier import Notifier

        source = inspect.getsource(Notifier.send)
        has_markdown = "parse_mode" in source and "Markdown" in source
        has_escape = "escape" in source.lower() or "replace" in source.lower()
        assert not has_markdown or has_escape, (
            "Notifier.send uses parse_mode='Markdown' without escaping special "
            "characters (_ * [ ` etc). Telegram rejects messages containing these."
        )
