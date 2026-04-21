"""Tests for DashboardApp event-stream wiring.

Covers:
- EventDispatcher subscription lifecycle (subscribe on mount, unsubscribe on unmount)
- Event routing: each event kind flows to the correct widget
- Command queue wiring: actions push the correct Command to command_queue
- check_action guard for stuck actions
- on_worker_state_changed logs a critical error on runner worker failure
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from textual.worker import WorkerState

from tero2.events import Command, EventDispatcher, make_event
from tero2.tui.app import DashboardApp
from tero2.tui.widgets.log_view import LogView
from tero2.tui.widgets.stuck_hint import StuckHintWidget


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_app_with_mock_dispatcher() -> tuple[DashboardApp, MagicMock, asyncio.Queue]:
    """Return (app, dispatcher_mock, event_queue).

    The dispatcher mock's subscribe() returns a pre-created real asyncio.Queue
    so tests can push events to the queue and have _consume_events process them.
    """
    runner = MagicMock()
    runner.run = AsyncMock()
    runner.config.roles = {"builder": MagicMock(), "scout": MagicMock()}
    runner.project_path = None

    dispatcher = MagicMock()
    event_queue: asyncio.Queue = asyncio.Queue()
    dispatcher.subscribe.return_value = event_queue

    command_queue: asyncio.Queue[Command] = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=command_queue)
    return app, dispatcher, event_queue


def _make_app_with_real_dispatcher() -> tuple[DashboardApp, EventDispatcher, asyncio.Queue[Command]]:
    """Return (app, real_dispatcher, command_queue)."""
    runner = MagicMock()
    runner.run = AsyncMock()
    runner.config.roles = {}
    runner.project_path = None

    dispatcher = EventDispatcher()
    command_queue: asyncio.Queue[Command] = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=command_queue)
    return app, dispatcher, command_queue


# ── subscription lifecycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_mount_subscribes_to_dispatcher() -> None:
    """App must call dispatcher.subscribe() exactly once on mount."""
    app, dispatcher, _ = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True):
        dispatcher.subscribe.assert_called_once()


@pytest.mark.asyncio
async def test_on_mount_stores_event_queue() -> None:
    """After mount, _event_queue must be the queue returned by dispatcher.subscribe()."""
    app, dispatcher, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True):
        assert app._event_queue is event_queue


@pytest.mark.asyncio
async def test_on_unmount_unsubscribes_event_queue() -> None:
    """After exit, dispatcher.unsubscribe() must be called with the stored queue."""
    app, dispatcher, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True):
        pass  # exits the context manager, triggering on_unmount
    dispatcher.unsubscribe.assert_called_once_with(event_queue)


# ── event routing: phase_change ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_change_event_updates_pipeline() -> None:
    """'phase_change' event must update the PipelinePanel role status to 'активно'."""
    from tero2.tui.widgets.pipeline import PipelinePanel

    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        event = make_event("phase_change", role="builder", data={"sora_phase": "scout"})
        await event_queue.put(event)
        await pilot.pause(0.2)
        pipeline = app.query_one("#pipeline", PipelinePanel)
        assert pipeline._role_statuses.get("builder") == "активно"


@pytest.mark.asyncio
async def test_phase_change_updates_role_status() -> None:
    """'phase_change' with a role sets that role's status on the pipeline."""
    from tero2.tui.widgets.pipeline import PipelinePanel

    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        event = make_event("phase_change", role="builder", data={"sora_phase": "execute"})
        await event_queue.put(event)
        await pilot.pause(0.2)
        pipeline = app.query_one("#pipeline", PipelinePanel)
        assert pipeline._role_statuses.get("builder") == "активно"


# ── event routing: error ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_event_pushes_message_to_log_view() -> None:
    """'error' event must push a message containing the error text to LogView."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        log_view = app.query_one("#log-view", LogView)
        with patch.object(log_view, "push_message", wraps=log_view.push_message) as spy:
            event = make_event("error", data={"message": "something failed"})
            await event_queue.put(event)
            await pilot.pause(0.2)
        assert spy.called
        pushed = [c.args[0] for c in spy.call_args_list]
        assert any("something failed" in m for m in pushed)


# ── event routing: done ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_done_event_hides_stuck_hint() -> None:
    """'done' event must clear stuck mode and hide StuckHintWidget."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        # First trigger stuck mode
        stuck_event = make_event("stuck", data={})
        await event_queue.put(stuck_event)
        await pilot.pause(0.1)

        # Then send done
        done_event = make_event("done", data={})
        await event_queue.put(done_event)
        await pilot.pause(0.2)

        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is False


@pytest.mark.asyncio
async def test_done_event_does_not_crash() -> None:
    """'done' event with empty data must not crash the consume loop."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        event = make_event("done", data={})
        await event_queue.put(event)
        await pilot.pause(0.2)  # no crash = test passes


# ── event routing: stuck ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stuck_event_shows_stuck_hint() -> None:
    """'stuck' event must show the StuckHintWidget."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        event = make_event("stuck", data={})
        await event_queue.put(event)
        await pilot.pause(0.2)
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is True


# ── event routing: provider_switch ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_switch_event_routes_to_log_view() -> None:
    """'provider_switch' event must push a message mentioning the provider to LogView."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        log_view = app.query_one("#log-view", LogView)
        with patch.object(log_view, "push_message", wraps=log_view.push_message) as spy:
            event = make_event(
                "provider_switch",
                role="builder",
                data={"provider": "claude", "role": "builder"},
            )
            await event_queue.put(event)
            await pilot.pause(0.2)
        assert spy.called
        pushed = [c.args[0] for c in spy.call_args_list]
        assert any("claude" in m for m in pushed)


# ── event routing: usage_update ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_update_event_updates_usage_panel() -> None:
    """'usage_update' event must call update_limits on UsagePanel with the supplied data."""
    from tero2.tui.widgets.usage import UsagePanel

    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        panel = app.query_one("#usage-panel", UsagePanel)
        with patch.object(panel, "update_limits", wraps=panel.update_limits) as spy:
            event = make_event("usage_update", data={"limits": {"claude": 0.5}})
            await event_queue.put(event)
            await pilot.pause(0.2)
        assert spy.called
        assert panel._limits == {"claude": 0.5}


# ── command queue wiring ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_action_puts_command_in_queue() -> None:
    """action_pause() must enqueue Command('pause', source='tui')."""
    app, dispatcher, _ = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        await pilot.press("p")
        await pilot.pause()

    # Drain the command queue from the app object (not exposed, read via _command_queue)
    assert not app._command_queue.empty()
    cmd = app._command_queue.get_nowait()
    assert cmd.kind == "pause"
    assert cmd.source == "tui"


@pytest.mark.asyncio
async def test_skip_action_puts_skip_task_command() -> None:
    """action_skip() must enqueue Command('skip_task', source='tui')."""
    app, dispatcher, _ = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        await pilot.press("k")
        await pilot.pause()

    cmd = app._command_queue.get_nowait()
    assert cmd.kind == "skip_task"
    assert cmd.source == "tui"


# ── check_action guard for stuck actions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_stuck_actions_blocked_when_not_in_stuck_mode() -> None:
    """Stuck option keys 1-5 must be blocked when StuckHintWidget is hidden."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        # By default StuckHintWidget is hidden → stuck actions disabled
        result = app.check_action("stuck_option_1", ())
        assert result is False

        result = app.check_action("stuck_option_5", ())
        assert result is False


@pytest.mark.asyncio
async def test_stuck_actions_allowed_when_in_stuck_mode() -> None:
    """Stuck option keys 1-5 must be allowed when StuckHintWidget is visible."""
    app, _, event_queue = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        # Trigger stuck mode
        await event_queue.put(make_event("stuck", data={}))
        await pilot.pause(0.2)

        assert app.check_action("stuck_option_1", ()) is True
        assert app.check_action("stuck_option_3", ()) is True


@pytest.mark.asyncio
async def test_non_stuck_actions_always_allowed() -> None:
    """Regular actions (pause, skip, etc.) must always be allowed by check_action."""
    app, _, _ = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        assert app.check_action("pause", ()) is True
        assert app.check_action("roles", ()) is True
        assert app.check_action("steer", ()) is True


# ── on_worker_state_changed ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_state_error_logs_critical_message() -> None:
    """on_worker_state_changed must log a critical error when the runner worker enters ERROR state."""
    app, _, _ = _make_app_with_mock_dispatcher()
    async with app.run_test(headless=True) as pilot:
        log_view = app.query_one("#log-view", LogView)
        # Build a mock WorkerStateChanged event pointing at the real runner worker
        mock_event = MagicMock()
        mock_event.worker = app._runner_worker  # same object reference is required
        mock_event.state = WorkerState.ERROR
        with patch.object(log_view, "push_message", wraps=log_view.push_message) as spy:
            app.on_worker_state_changed(mock_event)
        assert spy.called
        pushed = [c.args[0] for c in spy.call_args_list]
        assert any("критическая" in m.lower() or "runner" in m.lower() for m in pushed)


# ── real EventDispatcher round-trip ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_real_dispatcher_event_reaches_app() -> None:
    """Events emitted via a real EventDispatcher reach the app's consume loop."""
    app, dispatcher, command_queue = _make_app_with_real_dispatcher()
    async with app.run_test(headless=True) as pilot:
        await dispatcher.emit(make_event("stuck", data={}))
        await pilot.pause(0.3)
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is True


@pytest.mark.asyncio
async def test_real_dispatcher_done_event_clears_stuck() -> None:
    """A done event via real dispatcher clears stuck mode."""
    app, dispatcher, command_queue = _make_app_with_real_dispatcher()
    async with app.run_test(headless=True) as pilot:
        await dispatcher.emit(make_event("stuck", data={}))
        await pilot.pause(0.1)
        await dispatcher.emit(make_event("done", data={}))
        await pilot.pause(0.3)
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is False
