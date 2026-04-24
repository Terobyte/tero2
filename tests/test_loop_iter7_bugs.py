"""Autonomous bug-loop iteration 7.

Three real, deterministic, user-observable bugs in still-unexplored areas:

1. EscalationLevel2JournalLiesAboutReset
   Where:  tero2/escalation.py, ``execute_escalation`` BACKTRACK_COACH branch.
   What:   When Level-2 escalation fires but the operator has set
           ``config.escalation.backtrack_to_last_checkpoint = False`` the
           code correctly skips the counter reset (bug-272 path), yet still
           writes a fixed journal entry that claims "Resetting to last
           checkpoint." The user-visible EVENT_JOURNAL.md then contradicts
           the configured behaviour; a post-mortem reader looking at the
           journal will believe a reset happened even though none did. This
           is a real disk-observable lie — not a log-level side note.
   Fix:    Branch the journal text on ``action.should_backtrack``. E.g.
                reset_line = (
                    "Resetting to last checkpoint.\n"
                    if action.should_backtrack
                    else "Backtrack disabled by config — no reset applied.\n"
                )
                disk.append_file(..., reset_line + ...)
           The second line (Signal/Details) stays unchanged.

2. UsagePanelRejectsProviderNamesWithDot
   Where:  tero2/tui/widgets/usage.py, ``UsagePanel._sync_rows``.
   What:   Row creation uses ``_ProviderRow(name, fraction, id=f"provider-{name}")``
           which passes the raw provider name verbatim into Textual's
           ``id=`` constructor. Textual rejects any id that contains chars
           outside ``[A-Za-z0-9_-]`` (notably dots and colons) with
           ``textual.css.errors.BadIdentifier``. Real provider / model names
           routinely contain dots — "claude-3.5", "gpt-4.1", "kimi-k2.5" —
           so the FIRST attempt by ``runner._refresh_usage`` to push such a
           limit into the TUI raises and takes the whole update code path
           down (the try/except in _sync_rows wraps mount, not the
           _ProviderRow constructor).
   Fix:    Sanitise the id before the widget constructor, e.g.
                import re
                safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", name)
                row = _ProviderRow(name, fraction, id=f"provider-{safe_id}")
           (Keep the original ``name`` for the label text; only the id
            needs the CSS-safe form.) Track name → safe_id mapping in
            self._rows if you want to evict by original name.

3. ConfigWriterListOfFloatsRoundTripsAsStrings
   Where:  tero2/config_writer.py, ``_simple_toml_dumps._item``.
   What:   The fallback TOML writer handles list items with a three-branch
           ``if isinstance(i, bool) ... elif isinstance(i, int) ... else``
           chain. The ``else`` branch quotes its ``str(i)`` output as a
           TOML string. Floats fall into ``else`` and are serialised as
           ``"0.5"`` (quoted string) instead of ``0.5`` (bare float). On
           the next ``_load_toml`` round-trip they come back as ``str``
           values, so any numeric config like
               retry.backoffs = [0.5, 1.0, 2.0]
           silently mutates into ``["0.5", "1.0", "2.0"]``. Downstream
           code that does ``float * n`` then raises ``TypeError`` at the
           first use — but ONLY after the user has already written the
           corrupted config. Reachable today because ``tomli_w`` is an
           optional dep and is NOT installed in the tero2 venv, so the
           fallback path is live.
   Fix:    Add a float branch to ``_item``, e.g.
                elif isinstance(i, float):
                    return repr(i)  # repr handles nan/inf/precision correctly
           (``str(0.1)`` is fine here too, but ``repr`` preserves
            round-trip precision and matches how the scalar path formats
            floats.)

Each test is independent; no cross-test state is required.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Bug 1: escalation level 2 journal message lies when backtrack=False ──


class TestLoopIter7EscalationLevel2JournalLiesAboutReset:
    """Level-2 escalation's EVENT_JOURNAL entry contradicts the no-backtrack config."""

    def test_journal_does_not_claim_reset_when_backtrack_disabled(self):
        """When should_backtrack=False, journal must not say 'Resetting'."""
        from tero2.escalation import (
            EscalationAction,
            EscalationLevel,
            execute_escalation,
        )
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckResult, StuckSignal

        state = AgentState()
        disk = MagicMock()
        notifier = MagicMock()
        notifier.notify = AsyncMock()
        checkpoint = MagicMock()
        checkpoint.save = MagicMock(side_effect=lambda s: s)

        action = EscalationAction(
            level=EscalationLevel.BACKTRACK_COACH,
            should_backtrack=False,  # config says: do NOT reset
        )
        sr = StuckResult(
            signal=StuckSignal.STEP_LIMIT, details="step_limit hit", severity=2,
        )

        asyncio.run(
            execute_escalation(
                action, state, disk, notifier, checkpoint, stuck_result=sr,
            )
        )

        # Gather everything appended to the journal.
        appended_text = ""
        for call in disk.append_file.call_args_list:
            # call.args = (relative_path, content)
            appended_text += call.args[1]

        assert "Resetting to last checkpoint" not in appended_text, (
            "EVENT_JOURNAL.md says 'Resetting to last checkpoint.' even though "
            "config.escalation.backtrack_to_last_checkpoint is False. The journal "
            "lies to post-mortem readers — it must branch on should_backtrack. "
            f"Full journal text: {appended_text!r}"
        )

    def test_journal_still_mentions_reset_when_backtrack_enabled(self):
        """Sanity: with should_backtrack=True, the 'Resetting' line MUST appear."""
        from tero2.escalation import (
            EscalationAction,
            EscalationLevel,
            execute_escalation,
        )
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckResult, StuckSignal

        state = AgentState()
        disk = MagicMock()
        notifier = MagicMock()
        notifier.notify = AsyncMock()
        checkpoint = MagicMock()
        checkpoint.save = MagicMock(side_effect=lambda s: s)

        action = EscalationAction(
            level=EscalationLevel.BACKTRACK_COACH,
            should_backtrack=True,  # config says: DO reset
        )
        sr = StuckResult(
            signal=StuckSignal.RETRY_EXHAUSTED, details="retries gone", severity=2,
        )

        asyncio.run(
            execute_escalation(
                action, state, disk, notifier, checkpoint, stuck_result=sr,
            )
        )

        appended_text = ""
        for call in disk.append_file.call_args_list:
            appended_text += call.args[1]

        # When backtrack IS enabled, the journal must still describe the reset.
        assert (
            "reset" in appended_text.lower() or "checkpoint" in appended_text.lower()
        ), (
            "When should_backtrack=True the journal must describe the reset; "
            f"got: {appended_text!r}"
        )


# ── Bug 2: UsagePanel crashes on provider names containing '.' ───────────


class TestLoopIter7UsagePanelRejectsProviderNamesWithDot:
    """_ProviderRow id= is raw provider name; Textual rejects dots → BadIdentifier."""

    def test_provider_row_accepts_realistic_claude_3_5_name(self):
        """`claude-3.5` is a legitimate provider/model id and must not crash."""
        from tero2.tui.widgets.usage import _ProviderRow

        # Will raise textual.css.errors.BadIdentifier before the bug is fixed.
        try:
            _ProviderRow("claude-3.5", 0.42, id="provider-claude-3.5")
        except Exception as exc:
            pytest.fail(
                "_ProviderRow must handle provider names containing '.' — got "
                f"{type(exc).__name__}: {exc}. A name like 'claude-3.5' is "
                "realistic and currently causes UsagePanel._sync_rows to die on "
                "the first usage_update event."
            )

    def test_update_limits_does_not_raise_on_dotted_names(self):
        """UsagePanel.update_limits(...) with a dotted provider name must not raise.

        Simulates what happens during a real runner.refresh_usage() call when a
        provider is configured as e.g. "claude-3.5-sonnet".
        """
        from tero2.tui.widgets.usage import UsagePanel

        panel = UsagePanel()
        # mock out Textual I/O so we don't need a running app
        panel.query_one = MagicMock(side_effect=Exception("no widgets mounted"))
        panel.mount = MagicMock()

        try:
            panel.update_limits({"claude-3.5": 0.12, "gpt-4.1": 0.77})
        except Exception as exc:
            pytest.fail(
                "update_limits with realistic dotted provider names must not "
                f"raise ({type(exc).__name__}: {exc}). The bug is that _sync_rows "
                "constructs _ProviderRow with id=f'provider-{name}' and passes "
                "the raw name with dots intact — Textual rejects it before the "
                "mount try/except can swallow the error."
            )


# ── Bug 3: _simple_toml_dumps quotes floats inside lists ─────────────────


class TestLoopIter7ConfigWriterListOfFloatsRoundTripsAsStrings:
    """List of floats is written as quoted strings and round-trips as str."""

    def test_list_of_floats_stays_float_on_roundtrip(self, monkeypatch):
        """Write a [float] list; re-read; every item must still be float."""
        import tomllib

        # Force fallback path (tomli_w not installed in tero2 env anyway).
        monkeypatch.setattr("tero2.config_writer._HAS_TOMLI_W", False)
        from tero2.config_writer import write_global_config_section

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            write_global_config_section(
                path,
                "retry",
                {"backoffs": [0.5, 1.0, 2.0]},
            )
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))

        backoffs = parsed["retry"]["backoffs"]
        # Before the fix every item is str, not float.
        assert all(isinstance(x, float) for x in backoffs), (
            "Float list round-tripped as non-float types: "
            f"{[type(x).__name__ for x in backoffs]} (values: {backoffs!r}). "
            "_simple_toml_dumps._item() has no float branch so floats fall into "
            "the string-quoting 'else' arm and come back as str. Any config key "
            "shaped like retry.backoffs = [0.5, 1.0, 2.0] is silently corrupted."
        )
        assert backoffs == [0.5, 1.0, 2.0], (
            f"Float values mangled on round-trip: {backoffs!r}"
        )

    def test_simple_toml_dumps_emits_bare_floats_in_list(self, monkeypatch):
        """The serialiser itself must not quote floats in lists."""
        monkeypatch.setattr("tero2.config_writer._HAS_TOMLI_W", False)
        from tero2.config_writer import _simple_toml_dumps

        out = _simple_toml_dumps({"vals": [0.5, 1.0]})
        # Buggy output: 'vals = ["0.5", "1.0"]'
        # Correct output contains a bare 0.5 with no quotes around it.
        assert '"0.5"' not in out, (
            "Floats in lists must not be emitted as quoted strings. "
            f"_simple_toml_dumps returned: {out!r}"
        )

    def test_list_of_ints_still_works(self, monkeypatch):
        """Sanity regression: int lists keep working after the fix."""
        monkeypatch.setattr("tero2.config_writer._HAS_TOMLI_W", False)
        from tero2.config_writer import _simple_toml_dumps
        import tomllib

        out = _simple_toml_dumps({"ports": [80, 443]})
        parsed = tomllib.loads(out)
        assert parsed == {"ports": [80, 443]}
        assert all(isinstance(x, int) for x in parsed["ports"])
