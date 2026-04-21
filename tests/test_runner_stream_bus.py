"""Failing tests for Task 26: Runner owns StreamBus.

These tests verify that:
  A — Runner creates a StreamBus instance on construction
  B — Runner exposes it via a .stream_bus property
  C — Runner._build_runner_context passes the bus to RunnerContext

All tests FAIL until tero2/runner.py is updated to create and thread the bus.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tero2.config import Config, RoleConfig
from tero2.runner import Runner
from tero2.stream_bus import StreamBus


def _make_runner(tmp_path: Path) -> Runner:
    """Construct a Runner against a minimal scratch project."""
    project = tmp_path / "project"
    project.mkdir(parents=True)
    (project / "plan.md").write_text("# Plan\n1. do something")
    config = Config()
    config.roles["builder"] = RoleConfig(provider="fake", timeout_s=5)
    return Runner(project, project / "plan.md", config=config)


# ── A: Runner creates StreamBus ───────────────────────────────────────────────


class TestRunnerCreatesStreamBus:
    def test_stream_bus_attribute_exists(self, tmp_path: Path) -> None:
        """Runner must expose a .stream_bus attribute after construction."""
        runner = _make_runner(tmp_path)
        assert hasattr(runner, "stream_bus")

    def test_stream_bus_is_stream_bus_instance(self, tmp_path: Path) -> None:
        """runner.stream_bus must be a StreamBus, not None or another type."""
        runner = _make_runner(tmp_path)
        assert isinstance(runner.stream_bus, StreamBus)

    def test_two_runners_have_independent_buses(self, tmp_path: Path) -> None:
        """Each Runner instance must own its own StreamBus (no shared state)."""
        r1 = _make_runner(tmp_path / "r1")
        r2 = _make_runner(tmp_path / "r2")
        assert r1.stream_bus is not r2.stream_bus


# ── B: Runner accepts an injected StreamBus ───────────────────────────────────


class TestRunnerAcceptsInjectedStreamBus:
    def test_injected_bus_is_stored(self, tmp_path: Path) -> None:
        """When stream_bus kwarg is supplied, Runner must store it unchanged."""
        project = tmp_path / "project"
        project.mkdir()
        (project / "plan.md").write_text("# Plan\n1. do something")
        config = Config()
        bus = StreamBus()
        runner = Runner(
            project, project / "plan.md", config=config, stream_bus=bus
        )
        assert runner.stream_bus is bus

    def test_default_bus_created_when_none_given(self, tmp_path: Path) -> None:
        """When stream_bus is not supplied, Runner must create one itself."""
        runner = _make_runner(tmp_path)
        assert runner.stream_bus is not None


# ── C: _build_runner_context threads the bus into RunnerContext ────────────────


class TestRunnerContextReceivesStreamBus:
    def test_context_stream_bus_matches_runner(self, tmp_path: Path) -> None:
        """RunnerContext built by Runner must have stream_bus == runner.stream_bus."""
        import asyncio
        from tero2.state import AgentState

        runner = _make_runner(tmp_path)
        shutdown = asyncio.Event()
        state = AgentState()
        ctx = runner._build_runner_context(state, shutdown)
        assert ctx.stream_bus is runner.stream_bus

    def test_context_stream_bus_is_not_none(self, tmp_path: Path) -> None:
        """RunnerContext.stream_bus must be a real StreamBus (not None)."""
        import asyncio
        from tero2.state import AgentState
        from tero2.stream_bus import StreamBus

        runner = _make_runner(tmp_path)
        shutdown = asyncio.Event()
        state = AgentState()
        ctx = runner._build_runner_context(state, shutdown)
        assert isinstance(ctx.stream_bus, StreamBus)
