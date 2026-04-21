"""Negative tests for bugs N16-N22 and Bug 57 from bugs.md.

Convention:
  - Each test FAILS when the bug is present (red).
  - Each test PASSES when the bug is fixed (green / regression guard).

Run:  pytest tests/test_n16_n22_bugs.py -v
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# N16 — notifier.py: Notifier ignores config.enabled
# ═══════════════════════════════════════════════════════════════════════


class TestN16NotifierIgnoresEnabled:
    """Notifier.__init__ sets _enabled = bool(bot_token and chat_id),
    completely ignoring config.enabled.  Setting enabled=False in the
    config has no effect — notifications are still sent.

    Fix: _enabled = config.enabled and bool(config.bot_token and config.chat_id)
    """

    def test_notifier_disabled_when_enabled_false(self) -> None:
        """enabled=False in config must make Notifier._enabled False,
        even when bot_token and chat_id are non-empty."""
        from tero2.config import TelegramConfig
        from tero2.notifier import Notifier

        cfg = TelegramConfig(enabled=False, bot_token="validtoken", chat_id="12345")
        notifier = Notifier(cfg)

        assert not notifier._enabled, (
            "Bug N16: Notifier._enabled is True despite config.enabled=False. "
            "_enabled = bool(bot_token and chat_id) ignores config.enabled. "
            "Fix: _enabled = config.enabled and bool(config.bot_token and config.chat_id)."
        )

    @pytest.mark.asyncio
    async def test_send_returns_false_when_config_disabled(self) -> None:
        """send() must return False without making any HTTP call when enabled=False."""
        from tero2.config import TelegramConfig
        from tero2.notifier import Notifier

        cfg = TelegramConfig(enabled=False, bot_token="validtoken", chat_id="12345")
        notifier = Notifier(cfg)

        with patch("tero2.notifier.requests.post") as mock_post:
            result = await notifier.send("hello world")

        assert result is False, (
            "Bug N16: send() returned True with config.enabled=False — "
            "notification was sent despite user disabling it."
        )
        mock_post.assert_not_called()

    def test_notifier_enabled_when_both_conditions_met(self) -> None:
        """Regression guard: enabled=True with valid token/chat should be enabled."""
        from tero2.config import TelegramConfig
        from tero2.notifier import Notifier

        cfg = TelegramConfig(enabled=True, bot_token="tok", chat_id="999")
        notifier = Notifier(cfg)
        assert notifier._enabled, (
            "Regression: Notifier should be enabled when enabled=True and creds present."
        )

    def test_notifier_disabled_when_token_missing(self) -> None:
        """enabled=True but no token should still be disabled."""
        from tero2.config import TelegramConfig
        from tero2.notifier import Notifier

        cfg = TelegramConfig(enabled=True, bot_token="", chat_id="999")
        notifier = Notifier(cfg)
        assert not notifier._enabled, (
            "Notifier must be disabled when bot_token is empty, even if enabled=True."
        )


# ═══════════════════════════════════════════════════════════════════════
# N17 — architect.py: must-have parser regex is case-sensitive
# ═══════════════════════════════════════════════════════════════════════


class TestN17MustHaveParserCaseSensitive:
    """_parse_slice_plan uses r"[Mm]ust.{0,3}[Hh]aves?" (no IGNORECASE).
    LLM output "MUST HAVE" passes the validator (_MUST_HAVE_RE has IGNORECASE)
    but the parser fails to split at that marker — must_haves ends up [].

    Fix: add re.IGNORECASE flag to the must-have split in _parse_slice_plan.
    """

    _PLAN_TEMPLATE = """\
## T01: Fix authentication

Implement OAuth2 login flow.

{must_marker}
- Token must be stored in keychain
- Session must expire after 1 hour
"""

    def _make_plan(self, marker: str) -> str:
        return self._PLAN_TEMPLATE.format(must_marker=marker)

    def test_uppercase_must_haves_parsed(self) -> None:
        """'MUST HAVES:' (all caps) must produce a non-empty must_haves list."""
        from tero2.players.architect import _parse_slice_plan

        plan = self._make_plan("MUST HAVES:")
        result = _parse_slice_plan(plan, "S01")

        assert len(result.tasks) == 1
        assert result.tasks[0].must_haves, (
            "Bug N17: _parse_slice_plan returned empty must_haves for 'MUST HAVES:' "
            "because the split regex '[Mm]ust.{0,3}[Hh]aves?' is case-sensitive. "
            "Fix: add re.IGNORECASE or rewrite as re.split(..., flags=re.IGNORECASE)."
        )

    def test_uppercase_must_have_singular_parsed(self) -> None:
        """'MUST HAVE:' (singular, no 's') must also parse correctly."""
        from tero2.players.architect import _parse_slice_plan

        plan = self._make_plan("MUST HAVE:")
        result = _parse_slice_plan(plan, "S01")

        assert len(result.tasks) == 1
        assert result.tasks[0].must_haves, (
            "Bug N17: 'MUST HAVE:' (no 's') not parsed — case-sensitive regex. "
            "Fix: use re.IGNORECASE on the must-have split pattern."
        )

    def test_validator_accepts_uppercase_must_haves(self) -> None:
        """Validator must pass the same plan that causes the parser bug.
        This confirms the inconsistency between validator and parser."""
        from tero2.players.architect import validate_plan

        plan = self._make_plan("MUST HAVES:")
        errors = validate_plan(plan)
        assert not errors, (
            f"Validator rejected the plan: {errors}. "
            "The test relies on the validator accepting 'MUST HAVES:' to demonstrate "
            "the parser inconsistency."
        )

    def test_lowercase_must_haves_still_parsed(self) -> None:
        """Regression guard: standard '**Must-haves:**' must still work."""
        from tero2.players.architect import _parse_slice_plan

        plan = self._make_plan("**Must-haves:**")
        result = _parse_slice_plan(plan, "S01")

        assert len(result.tasks) == 1
        assert result.tasks[0].must_haves, (
            "Regression: standard '**Must-haves:**' marker stopped working."
        )


# ═══════════════════════════════════════════════════════════════════════
# N18 — execute_phase.py: character-level truncation vs byte-level
# ═══════════════════════════════════════════════════════════════════════


class TestN18ExecutePhaseTruncationNotByteLevel:
    """execute_phase.py line 291 truncates with [:MAX_BUILDER_OUTPUT_CHARS]
    (character slice), while reflexion.py:73 uses byte-level truncation:
        output.encode("utf-8")[:MAX_BUILDER_OUTPUT_CHARS].decode(...)

    For Cyrillic text, 2000 chars = ~4000 bytes — double the intended limit.

    Fix: use byte-level truncation in execute_phase consistent with reflexion.py.
    """

    def test_execute_phase_uses_byte_level_truncation(self) -> None:
        """Source check: truncation in execute_phase.py must be byte-level."""
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module)
        # byte-level pattern: .encode("utf-8")[:...].decode(...)
        has_byte_truncation = (
            'encode("utf-8")' in source or "encode('utf-8')" in source
        ) and (
            ".decode(" in source
        )
        assert has_byte_truncation, (
            "Bug N18: execute_phase.py truncates captured_output with "
            "[:MAX_BUILDER_OUTPUT_CHARS] (character-level). For Cyrillic/CJK text "
            "this produces up to 4× more bytes than the 2000-char limit implies. "
            "reflexion.py uses byte-level truncation — execute_phase must match. "
            "Fix: use output.encode('utf-8')[:MAX_BUILDER_OUTPUT_CHARS].decode('utf-8', errors='ignore')."
        )

    def test_cyrillic_truncation_respects_byte_limit(self) -> None:
        """2000 Cyrillic chars must not exceed MAX_BUILDER_OUTPUT_CHARS bytes
        after byte-level truncation."""
        from tero2.reflexion import MAX_BUILDER_OUTPUT_CHARS

        cyrillic_text = "Текст " * 400  # 2400 chars, each 2 bytes = 4800 bytes

        # Byte-level truncation (reflexion.py approach — correct):
        byte_truncated = (
            cyrillic_text.encode("utf-8")[:MAX_BUILDER_OUTPUT_CHARS]
            .decode("utf-8", errors="ignore")
        )
        assert len(byte_truncated.encode("utf-8")) <= MAX_BUILDER_OUTPUT_CHARS, (
            "Byte-level truncation must respect MAX_BUILDER_OUTPUT_CHARS in bytes."
        )

        # Character-level truncation (execute_phase approach — bug):
        char_truncated = cyrillic_text[:MAX_BUILDER_OUTPUT_CHARS]
        char_bytes = len(char_truncated.encode("utf-8"))
        assert char_bytes > MAX_BUILDER_OUTPUT_CHARS, (
            "Test setup error: Cyrillic text should exceed byte limit after char truncation."
        )


# ═══════════════════════════════════════════════════════════════════════
# N19 — triggers.py: _check_anomaly false-positive on substring match
# ═══════════════════════════════════════════════════════════════════════


class TestN19CheckAnomalyFalsePositive:
    """_check_anomaly returns True if 'ANOMALY' appears anywhere in the
    journal — including unrelated mentions like 'discussed ANOMALY detection'.

    Fix: require a structured marker (e.g. '## ANOMALY' or '[ANOMALY]')
    rather than a bare substring match.
    """

    def _make_disk(self, journal_content: str) -> MagicMock:
        disk = MagicMock()
        disk.read_file.return_value = journal_content
        return disk

    def test_unrelated_anomaly_mention_does_not_trigger(self) -> None:
        """Journal that mentions 'ANOMALY' in a non-structured context
        must NOT trigger the coach."""
        from tero2.triggers import _check_anomaly

        journal = (
            "## 2026-04-20 Step 5 complete\n"
            "The team discussed ANOMALY detection methodology in the retrospective.\n"
            "No issues found.\n"
        )
        disk = self._make_disk(journal)
        result = _check_anomaly(disk)

        assert result is False, (
            "Bug N19: _check_anomaly returned True for a journal that mentions "
            "'ANOMALY' in a non-anomaly context. Bare 'ANOMALY' in journal "
            "is too broad — 'discussed ANOMALY detection' is not an anomaly event. "
            "Fix: require a structured marker like '## ANOMALY' or '[ANOMALY]'."
        )

    def test_structured_anomaly_marker_does_trigger(self) -> None:
        """A real anomaly entry (structured marker) must still trigger."""
        from tero2.triggers import _check_anomaly

        journal = (
            "## 2026-04-20 Step 5 complete\n"
            "## ANOMALY: Builder repeated same output 3 times\n"
            "Detected loop at step 12.\n"
        )
        disk = self._make_disk(journal)
        result = _check_anomaly(disk)

        assert result is True, (
            "Regression: structured '## ANOMALY:' entry must still trigger _check_anomaly."
        )

    def test_empty_journal_does_not_trigger(self) -> None:
        """Empty journal must never trigger."""
        from tero2.triggers import _check_anomaly

        disk = self._make_disk("")
        assert _check_anomaly(disk) is False

    def test_none_journal_does_not_trigger(self) -> None:
        """None journal (file missing) must never trigger."""
        from tero2.triggers import _check_anomaly

        disk = MagicMock()
        disk.read_file.return_value = None
        assert _check_anomaly(disk) is False


# ═══════════════════════════════════════════════════════════════════════
# N20 — runner.py:419: read_text() without encoding="utf-8"
# ═══════════════════════════════════════════════════════════════════════


class TestN20RunnerPlanFileEncodingMissing:
    """runner.py:419 calls self.plan_file.read_text() without encoding="utf-8".
    On non-UTF-8 default systems (Windows cp1252, some Linux locales) plan
    files with non-ASCII content (e.g. Cyrillic task names) raise UnicodeDecodeError
    or silently corrupt the content.

    Fix: add encoding="utf-8" to read_text() — same fix applied in Bug 56.
    """

    def test_runner_plan_file_read_uses_utf8(self) -> None:
        """Source check: plan_file.read_text() in Runner._start_sora must specify encoding."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._execute_sora)
        # Find the read_text() call near plan_file
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "plan_file" in line and "read_text" in line:
                has_encoding = "encoding" in line or (
                    i + 1 < len(lines) and "encoding" in lines[i + 1]
                )
                assert has_encoding, (
                    f"Bug N20: plan_file.read_text() at runner.py:~419 has no encoding= "
                    f"argument. Line: {line.strip()!r}. "
                    "Fix: use self.plan_file.read_text(encoding='utf-8')."
                )
                return

        pytest.skip("plan_file.read_text() not found in Runner._start_sora — check method name")


# ═══════════════════════════════════════════════════════════════════════
# N21 — zai.py: read_text() without encoding="utf-8"
# ═══════════════════════════════════════════════════════════════════════


class TestN21ZaiReadTextEncodingMissing:
    """zai.py:60 (_read_settings_key) and :83 (_load_token) call read_text()
    without encoding="utf-8".  Settings files with non-ASCII API keys or paths
    will fail or corrupt on non-UTF-8 default locales.

    Fix: add encoding="utf-8" to all read_text() calls in zai.py.
    """

    def test_read_settings_key_uses_utf8(self) -> None:
        """_read_settings_key must read settings.json with UTF-8 encoding."""
        from tero2.providers import zai as zai_module

        source = inspect.getsource(zai_module._read_settings_key)
        assert "encoding" in source, (
            "Bug N21: _read_settings_key calls read_text() without encoding='utf-8'. "
            "Settings files with non-ASCII content fail on non-UTF-8 systems. "
            "Fix: use p.read_text(encoding='utf-8')."
        )

    def test_load_token_uses_utf8(self) -> None:
        """_load_token must read settings.json with UTF-8 encoding."""
        from tero2.providers import zai as zai_module

        source = inspect.getsource(zai_module._load_token)
        assert "encoding" in source, (
            "Bug N21: _load_token calls settings_path.read_text() without encoding='utf-8'. "
            "Fix: use settings_path.read_text(encoding='utf-8')."
        )


# ═══════════════════════════════════════════════════════════════════════
# N22 — history.py: read_text() without encoding="utf-8"
# ═══════════════════════════════════════════════════════════════════════


class TestN22HistoryReadTextEncodingMissing:
    """history.py:25 calls HISTORY_FILE.read_text() without encoding="utf-8".
    On systems with non-UTF-8 default locale (Windows, some Linux setups),
    history files with non-ASCII project paths fail to load silently.

    Fix: use HISTORY_FILE.read_text(encoding="utf-8").
    """

    def test_load_history_uses_utf8(self) -> None:
        """Source check: load_history() must read with encoding='utf-8'."""
        import tero2.history as history_module

        source = inspect.getsource(history_module.load_history)
        assert "encoding" in source, (
            "Bug N22: load_history() calls HISTORY_FILE.read_text() without encoding='utf-8'. "
            "Project paths with non-ASCII chars (Cyrillic dirs) fail on non-UTF systems. "
            "Fix: use HISTORY_FILE.read_text(encoding='utf-8')."
        )

    def test_load_history_roundtrips_unicode_path(self, tmp_path: Path) -> None:
        """Functional: history with a Cyrillic project path must round-trip correctly."""
        import tero2.history as history_module
        from tero2.history import HistoryEntry

        entry = HistoryEntry(
            path="/Users/тест/проект",
            name="проект",
            last_run="2026-04-20T10:00:00+00:00",
            last_plan=None,
            run_count=1,
        )
        history_path = tmp_path / "history.json"
        import json
        history_path.write_text(
            json.dumps({"version": 1, "entries": [entry.__dict__]}),
            encoding="utf-8",
        )

        with patch.object(history_module, "HISTORY_FILE", history_path):
            loaded = history_module.load_history()

        assert len(loaded) == 1
        assert loaded[0].path == "/Users/тест/проект", (
            "Bug N22: Cyrillic path was corrupted or not loaded — "
            "likely due to missing encoding='utf-8' in read_text()."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 57 — architect.py: _parse_slice_plan silently drops tasks with
#           space-separated headers (no colon after task ID)
# ═══════════════════════════════════════════════════════════════════════


class TestBug57ParseSlicePlanDropsSpaceHeaders:
    """_parse_slice_plan split regex `^(## T\\d+:[^\\n]*)\\n` requires a colon
    after the task ID.  LLM output with `## T01 Fix the bug` (space, no colon)
    is not matched — the task is silently dropped.

    Meanwhile, _count_tasks/_extract_task_ids use `_TASK_RE = ^##\\s+T\\d{2}[:\\s]`
    which accepts both colon and space.  So validate_plan counts the task as
    present but _parse_slice_plan never sees it — inconsistency.

    Fix: update _parse_slice_plan split regex to match `[:\\s]` like _TASK_RE,
    or emit a clear error when a counted task is missing from the parsed result.
    """

    _PLAN_WITH_COLON = """\
## T01: Setup the module

Implement the base class.

**Must-haves:**
- Class must have __init__ method
- Must export public API
"""

    _PLAN_WITHOUT_COLON = """\
## T01 Setup the module

Implement the base class.

**Must-haves:**
- Class must have __init__ method
- Must export public API
"""

    def test_task_with_space_header_is_not_dropped(self) -> None:
        """Task header `## T01 Setup` (space, no colon) must be parsed, not silently dropped."""
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan(self._PLAN_WITHOUT_COLON, "S01")
        assert len(result.tasks) >= 1, (
            "Bug 57: _parse_slice_plan silently dropped '## T01 Setup' (no colon). "
            "split_re requires '## T\\d+:' (colon mandatory) but _count_tasks accepts "
            "'## T\\d{2}[:\\s]' (colon or space). "
            "Fix: update split_re to match both: r'^(## T\\d+[:\\s][^\\n]*)\\n'."
        )

    def test_task_with_colon_header_still_parsed(self) -> None:
        """Regression guard: standard `## T01:` headers must continue to work."""
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan(self._PLAN_WITH_COLON, "S01")
        assert len(result.tasks) == 1
        assert result.tasks[0].id == "T01"

    def test_validator_counts_space_header_task(self) -> None:
        """Confirm validator sees the task that the parser drops.
        This demonstrates the validator/parser inconsistency."""
        from tero2.players.architect import _count_tasks, validate_plan

        count = _count_tasks(self._PLAN_WITHOUT_COLON)
        assert count >= 1, (
            "Test setup error: _count_tasks must see the space-header task."
        )

        errors = validate_plan(self._PLAN_WITHOUT_COLON)
        assert not errors, (
            f"Test setup error: validator must accept the plan. Errors: {errors}"
        )

    def test_parser_and_validator_agree_on_task_count(self) -> None:
        """Task count from _parse_slice_plan must equal _count_tasks."""
        from tero2.players.architect import _count_tasks, _parse_slice_plan

        for label, plan in [
            ("colon header", self._PLAN_WITH_COLON),
            ("space header", self._PLAN_WITHOUT_COLON),
        ]:
            counted = _count_tasks(plan)
            parsed = len(_parse_slice_plan(plan, "S01").tasks)
            assert parsed == counted, (
                f"Bug 57: [{label}] validator counts {counted} task(s) but parser "
                f"produced {parsed}. Silent drop of space-separated headers. "
                "Fix: make split_re consistent with _TASK_RE."
            )
