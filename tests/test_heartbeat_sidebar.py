"""Tests for tero2.tui.widgets.heartbeat_sidebar — HeartbeatSidebar widget.

Coverage:
- Widget composes correctly: title + 7 role cells
- Per-role metrics update when on_stream_event() is called
- tool_count increments on tool_use events
- last_line updates on text events
- status transitions (idle → running → done → error)
- started_at / elapsed_s tracking
- Phase events via on_phase_event() update status correctly
- Unknown roles in stream events are silently ignored
- on_stream_event() with empty role is handled gracefully
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from textual.app import App, ComposeResult

from tero2.stream_bus import make_stream_event
from tero2.tui.widgets.heartbeat_sidebar import (
    SIDEBAR_ROLE_ORDER,
    HeartbeatSidebar,
    RoleMetrics,
)


# ── host app ─────────────────────────────────────────────────────────────────


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield HeartbeatSidebar(id="heartbeat")


# ── RoleMetrics dataclass ─────────────────────────────────────────────────────


class TestRoleMetrics:
    def test_default_values(self) -> None:
        m = RoleMetrics()
        assert m.status == "idle"
        assert m.elapsed_s == 0.0
        assert m.tool_count == 0
        assert m.last_line == ""
        assert m.provider == ""
        assert m.model == ""
        assert m.started_at is None

    def test_fields_are_mutable(self) -> None:
        m = RoleMetrics()
        m.tool_count = 5
        assert m.tool_count == 5

    def test_independent_instances(self) -> None:
        m1 = RoleMetrics()
        m2 = RoleMetrics()
        m1.tool_count = 3
        assert m2.tool_count == 0


# ── SIDEBAR_ROLE_ORDER constant ───────────────────────────────────────────────


class TestSidebarRoleOrder:
    def test_contains_seven_roles(self) -> None:
        assert len(SIDEBAR_ROLE_ORDER) == 7

    def test_expected_roles_present(self) -> None:
        expected = {"scout", "architect", "builder", "coach", "verifier", "reviewer", "executor"}
        assert set(SIDEBAR_ROLE_ORDER) == expected

    def test_order_starts_with_scout(self) -> None:
        assert SIDEBAR_ROLE_ORDER[0] == "scout"

    def test_order_ends_with_executor(self) -> None:
        assert SIDEBAR_ROLE_ORDER[-1] == "executor"


# ── widget composition ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sidebar_composes_without_error() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        assert sidebar is not None


@pytest.mark.asyncio
async def test_sidebar_has_seven_cells() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        assert len(sidebar._cells) == 7


@pytest.mark.asyncio
async def test_all_roles_have_cells() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        for role in SIDEBAR_ROLE_ORDER:
            assert role in sidebar._cells


@pytest.mark.asyncio
async def test_all_roles_initialized_to_idle() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        for role in SIDEBAR_ROLE_ORDER:
            assert sidebar._metrics[role].status == "idle"


# ── on_stream_event — tool_use ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_use_increments_tool_count() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        e = make_stream_event("builder", "tool_use", tool_name="bash")
        sidebar.on_stream_event(e)
        assert sidebar._metrics["builder"].tool_count == 1


@pytest.mark.asyncio
async def test_multiple_tool_use_events_accumulate() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        for tool in ["bash", "read_file", "write_file"]:
            sidebar.on_stream_event(
                make_stream_event("builder", "tool_use", tool_name=tool)
            )
        assert sidebar._metrics["builder"].tool_count == 3


@pytest.mark.asyncio
async def test_tool_use_updates_last_line() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(
            make_stream_event("scout", "tool_use", tool_name="glob_files")
        )
        assert "glob_files" in sidebar._metrics["scout"].last_line


# ── on_stream_event — text ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_event_updates_last_line() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(
            make_stream_event("architect", "text", content="Analysing dependencies...")
        )
        assert "Analysing" in sidebar._metrics["architect"].last_line


@pytest.mark.asyncio
async def test_text_event_uses_first_line_only() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(
            make_stream_event("builder", "text", content="first line\nsecond line\nthird")
        )
        assert "first line" in sidebar._metrics["builder"].last_line
        assert "second line" not in sidebar._metrics["builder"].last_line


@pytest.mark.asyncio
async def test_empty_text_content_does_not_overwrite_last_line() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(
            make_stream_event("builder", "text", content="original")
        )
        sidebar.on_stream_event(
            make_stream_event("builder", "text", content="")   # empty
        )
        # last_line should retain "original" since empty content is skipped
        assert sidebar._metrics["builder"].last_line == "original"


# ── on_stream_event — status transitions ─────────────────────────────────────


@pytest.mark.asyncio
async def test_first_event_sets_status_to_running() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(make_stream_event("coach", "text", content="starting"))
        assert sidebar._metrics["coach"].status == "running"


@pytest.mark.asyncio
async def test_turn_end_sets_status_to_done() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(make_stream_event("builder", "text", content="working"))
        sidebar.on_stream_event(make_stream_event("builder", "turn_end"))
        assert sidebar._metrics["builder"].status == "done"


@pytest.mark.asyncio
async def test_error_event_sets_status_to_error() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(make_stream_event("verifier", "error", content="timeout"))
        assert sidebar._metrics["verifier"].status == "error"


# ── on_stream_event — elapsed tracking ───────────────────────────────────────


@pytest.mark.asyncio
async def test_started_at_set_on_first_event() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        sidebar.on_stream_event(make_stream_event("builder", "text", timestamp=ts, content="go"))
        assert sidebar._metrics["builder"].started_at == ts


@pytest.mark.asyncio
async def test_elapsed_calculated_from_started_at() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        from datetime import timedelta
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=30)
        sidebar.on_stream_event(make_stream_event("scout", "text", timestamp=t0, content="a"))
        sidebar.on_stream_event(make_stream_event("scout", "text", timestamp=t1, content="b"))
        assert sidebar._metrics["scout"].elapsed_s == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_started_at_not_updated_on_subsequent_events() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        from datetime import timedelta
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(seconds=10)
        sidebar.on_stream_event(make_stream_event("architect", "text", timestamp=t0, content="x"))
        sidebar.on_stream_event(make_stream_event("architect", "text", timestamp=t1, content="y"))
        assert sidebar._metrics["architect"].started_at == t0


# ── on_stream_event — role isolation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_events_do_not_cross_roles() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_stream_event(make_stream_event("builder", "tool_use", tool_name="bash"))
        sidebar.on_stream_event(make_stream_event("builder", "tool_use", tool_name="grep"))
        # Scout should have zero tool count
        assert sidebar._metrics["scout"].tool_count == 0
        assert sidebar._metrics["builder"].tool_count == 2


@pytest.mark.asyncio
async def test_unknown_role_is_silently_ignored() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        e = make_stream_event("nonexistent_role", "text", content="hi")
        sidebar.on_stream_event(e)   # must not raise
        # Known roles unaffected
        for role in SIDEBAR_ROLE_ORDER:
            assert sidebar._metrics[role].tool_count == 0


@pytest.mark.asyncio
async def test_empty_role_is_silently_ignored() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        e = make_stream_event("", "status", content="system")
        sidebar.on_stream_event(e)   # must not raise


# ── on_phase_event ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_event_done_marks_role_done() -> None:
    class _FakeEvent:
        kind = "done"
        role = "builder"

    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_phase_event(_FakeEvent())
        assert sidebar._metrics["builder"].status == "done"


@pytest.mark.asyncio
async def test_phase_event_error_marks_role_error() -> None:
    class _FakeEvent:
        kind = "error"
        role = "scout"

    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_phase_event(_FakeEvent())
        assert sidebar._metrics["scout"].status == "error"


@pytest.mark.asyncio
async def test_phase_event_phase_change_sets_running_for_idle_role() -> None:
    class _FakeEvent:
        kind = "phase_change"
        role = "architect"

    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_phase_event(_FakeEvent())
        assert sidebar._metrics["architect"].status == "running"


@pytest.mark.asyncio
async def test_phase_event_unknown_role_is_silent() -> None:
    class _FakeEvent:
        kind = "done"
        role = "unknown_role"

    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_phase_event(_FakeEvent())   # must not raise


@pytest.mark.asyncio
async def test_phase_event_empty_role_is_silent() -> None:
    class _FakeEvent:
        kind = "done"
        role = ""

    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        sidebar.on_phase_event(_FakeEvent())   # must not raise


# ── get_metrics helper ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_known_role() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        m = sidebar.get_metrics("builder")
        assert isinstance(m, RoleMetrics)


@pytest.mark.asyncio
async def test_get_metrics_unknown_role_returns_none() -> None:
    app = _HostApp()
    async with app.run_test(headless=True) as pilot:
        sidebar = app.query_one("#heartbeat", HeartbeatSidebar)
        assert sidebar.get_metrics("totally_unknown") is None
