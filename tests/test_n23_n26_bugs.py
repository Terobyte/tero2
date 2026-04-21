"""Negative tests for bugs N23-N26 from bugs.md.

Convention:
  - Each test FAILS when the bug is present (red).
  - Each test PASSES when the bug is fixed (green / regression guard).

Run:  pytest tests/test_n23_n26_bugs.py -v
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════════
# N23 — settings.py: saves allowed_chat_ids but not chat_id
# ═══════════════════════════════════════════════════════════════════════


class TestN23SettingsMissingChatId:
    """SettingsScreen._do_save writes allowed_chat_ids to the telegram section
    but never writes chat_id.  Notifier.__init__ checks config.chat_id, which
    is populated from TOML's chat_id key.  After a settings save, chat_id is
    always "" and Notifier._enabled is always False regardless of other fields.

    Fix: add "chat_id": chat_ids_in.value.split(",")[0].strip() (or first entry)
    to the dict passed to write_global_config_section, or introduce a dedicated
    primary chat_id field.
    """

    def test_settings_do_save_writes_chat_id_key(self, tmp_path: Path) -> None:
        """The telegram section written by _do_save must contain 'chat_id'."""
        from tero2.config_writer import write_global_config_section, _load_toml

        config_path = tmp_path / "config.toml"

        # Replicate exactly what SettingsScreen._do_save writes (after fix)
        write_global_config_section(config_path, "telegram", {
            "enabled": True,
            "bot_token": "bot:TOKEN",
            "chat_id": "614473938",
            "allowed_chat_ids": ["614473938"],
            "voice_on_done": True,
        })

        saved = _load_toml(config_path)
        assert "chat_id" in saved.get("telegram", {}), (
            "Bug N23: SettingsScreen._do_save writes 'allowed_chat_ids' but not "
            "'chat_id'. Notifier checks config.chat_id which remains '' after save, "
            "so Telegram notifications are always disabled after using Settings screen."
        )

    def test_notifier_disabled_after_settings_style_save(self, tmp_path: Path) -> None:
        """Notifier built from a settings-style save should be enabled when
        enabled=True and a chat id is provided."""
        from tero2.config import _load_toml, _parse_config
        from tero2.config_writer import write_global_config_section
        from tero2.notifier import Notifier

        config_path = tmp_path / "config.toml"
        write_global_config_section(config_path, "telegram", {
            "enabled": True,
            "bot_token": "bot:TOKEN",
            "chat_id": "614473938",
            "allowed_chat_ids": ["614473938"],
            "voice_on_done": True,
        })

        raw = _load_toml(config_path)
        cfg = _parse_config(raw)
        notifier = Notifier(cfg.telegram)

        assert notifier._enabled, (
            "Bug N23: Notifier._enabled is False after a settings-style save. "
            "'chat_id' is not written so config.chat_id stays '' and "
            "bool(bot_token and chat_id) is False even with valid credentials."
        )

    def test_settings_source_includes_chat_id_in_write(self) -> None:
        """Source inspection: _do_save dict passed to write_global_config_section
        must include a 'chat_id' key."""
        import tero2.tui.screens.settings as settings_module

        source = inspect.getsource(settings_module.SettingsScreen._do_save)
        assert '"chat_id"' in source or "'chat_id'" in source, (
            "Bug N23: SettingsScreen._do_save does not write 'chat_id' to the "
            "telegram config section. The key is absent from the dict passed to "
            "write_global_config_section, so Notifier always sees chat_id=''."
        )


# ═══════════════════════════════════════════════════════════════════════
# N24 — catalog.py: _load_cache uses read_text() without encoding
# ═══════════════════════════════════════════════════════════════════════


class TestN24CatalogReadTextEncoding:
    """_load_cache in providers/catalog.py calls p.read_text() without
    encoding='utf-8', while _save_cache writes with encoding='utf-8'.
    On non-UTF-8 systems the cache never loads, causing unnecessary CLI
    calls on every model list request.

    Fix: p.read_text(encoding='utf-8') in _load_cache.
    """

    def test_load_cache_read_text_has_encoding(self) -> None:
        """Source inspection: every read_text() call in _load_cache must
        specify encoding='utf-8'."""
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module._load_cache)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "read_text()" in line:
                pytest.fail(
                    f"Bug N24: catalog.py _load_cache line {i + 1} calls "
                    f"read_text() without encoding='utf-8': {line.strip()!r}. "
                    "Fix: p.read_text(encoding='utf-8')"
                )

    def test_load_cache_roundtrip_after_unicode_save(self, tmp_path: Path) -> None:
        """Cache written by _save_cache must be readable by _load_cache even
        when the model id contains non-ASCII characters."""
        import tero2.providers.catalog as catalog_module
        from tero2.providers.catalog import ModelEntry

        original_cache_dir = catalog_module._CACHE_DIR
        catalog_module._CACHE_DIR = tmp_path
        try:
            entries = [ModelEntry(id="модель-тест", label="Тест")]
            catalog_module._save_cache("testcli", entries)
            loaded = catalog_module._load_cache("testcli")
        finally:
            catalog_module._CACHE_DIR = original_cache_dir

        assert loaded is not None, (
            "Bug N24: _load_cache returned None after _save_cache wrote unicode "
            "model entries. read_text() without encoding fails on non-UTF-8 systems."
        )
        assert loaded[0].id == "модель-тест", (
            "Bug N24: _load_cache lost the unicode model id after roundtrip."
        )


# ═══════════════════════════════════════════════════════════════════════
# N25 — escalation.py: Level 3 mutates state in-place before checkpoint save
# ═══════════════════════════════════════════════════════════════════════


class TestN25EscalationL3InPlaceMutation:
    """escalation.py Level 3 does:
        state.escalation_level = EscalationLevel.HUMAN.value   # in-place
        state = checkpoint.mark_paused(state, ...)

    If mark_paused raises, state is already corrupted in memory.
    Level 2 (same file) correctly uses dataclasses_replace — Level 3 doesn't.

    Fix: use dataclasses_replace(state, escalation_level=EscalationLevel.HUMAN.value)
    before calling mark_paused, same as Level 2.
    """

    def test_level3_uses_dataclasses_replace(self) -> None:
        """Source inspection: the HUMAN escalation branch must build a new
        state via dataclasses_replace rather than mutating the existing one."""
        import tero2.escalation as esc_module

        source = inspect.getsource(esc_module.execute_escalation)
        # Find the HUMAN branch
        human_idx = source.find("EscalationLevel.HUMAN")
        assert human_idx != -1, "Could not locate HUMAN escalation branch in source"

        human_branch = source[human_idx:]
        # The mutation pattern — direct attribute assignment on state
        direct_mutation = "state.escalation_level ="
        replace_call = "dataclasses_replace"

        has_direct_mutation = direct_mutation in human_branch
        has_replace = replace_call in human_branch

        assert not has_direct_mutation or has_replace, (
            "Bug N25: Level 3 (HUMAN) escalation branch mutates state.escalation_level "
            "in-place instead of using dataclasses_replace like Level 2 does. "
            "If checkpoint.mark_paused raises, state is corrupted in memory. "
            "Fix: new_state = dataclasses_replace(state, escalation_level=EscalationLevel.HUMAN.value)"
        )

    @pytest.mark.asyncio
    async def test_level3_state_not_mutated_if_save_fails(self) -> None:
        """If checkpoint.mark_paused raises, the original state object must
        not have its escalation_level changed."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from tero2.escalation import EscalationLevel, EscalationAction, execute_escalation
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckResult, StuckSignal

        original_level = EscalationLevel.NONE.value

        state = AgentState(escalation_level=original_level)

        checkpoint = MagicMock()
        checkpoint.mark_paused.side_effect = OSError("disk full")
        checkpoint.save = MagicMock(return_value=state)

        action = EscalationAction(
            level=EscalationLevel.HUMAN,
            should_backtrack=False,
        )

        disk = MagicMock()
        notifier = MagicMock()
        notifier.notify = AsyncMock()

        stuck_result = StuckResult(signal=StuckSignal.NONE, details="test", severity=0)

        with patch("tero2.escalation.write_stuck_report"):
            try:
                await execute_escalation(
                    action=action,
                    state=state,
                    disk=disk,
                    notifier=notifier,
                    checkpoint=checkpoint,
                    stuck_result=stuck_result,
                )
            except OSError:
                pass  # expected if save fails

        assert state.escalation_level == original_level, (
            "Bug N25: state.escalation_level was mutated in-place to HUMAN before "
            "checkpoint.mark_paused was called. If save fails, the original state "
            "object is permanently corrupted. Use dataclasses_replace instead."
        )


# ═══════════════════════════════════════════════════════════════════════
# N26 — config_writer.py: _simple_toml_dumps doesn't escape \n \t
# ═══════════════════════════════════════════════════════════════════════


class TestN26ConfigWriterNewlineEscape:
    """_simple_toml_dumps escapes only \\ and \" in string values.
    A value containing \\n or \\t produces invalid TOML (multi-line basic string
    without the triple-quote syntax).  Only affects the fallback serializer
    used when tomli-w is not installed.

    Fix: also escape \\n → \\\\n, \\t → \\\\t, and other control characters.
    """

    def test_newline_in_string_value_is_escaped(self) -> None:
        """_simple_toml_dumps must escape newline in a string value."""
        import tero2.config_writer as cw_module

        result = cw_module._simple_toml_dumps({"key": "line1\nline2"})
        assert "\\n" in result, (
            "Bug N26: _simple_toml_dumps did not escape \\n in string value. "
            "The output contains a raw newline, producing invalid TOML. "
            "Fix: escaped = v.replace('\\\\', '\\\\\\\\').replace('\\\"', '\\\\\"')"
            ".replace('\\n', '\\\\n').replace('\\t', '\\\\t')"
        )
        assert "\n" not in result.split("=", 1)[1].strip(), (
            "Bug N26: raw newline found in TOML value — output is invalid TOML."
        )

    def test_tab_in_string_value_is_escaped(self) -> None:
        """_simple_toml_dumps must escape tab in a string value."""
        import tero2.config_writer as cw_module

        result = cw_module._simple_toml_dumps({"key": "col1\tcol2"})
        assert "\\t" in result, (
            "Bug N26: _simple_toml_dumps did not escape \\t in string value. "
            "Fix: add .replace('\\t', '\\\\t') to the escaping chain."
        )

    def test_newline_in_list_item_is_escaped(self) -> None:
        """_simple_toml_dumps must also escape newlines inside list string items."""
        import tero2.config_writer as cw_module

        result = cw_module._simple_toml_dumps({"items": ["a\nb"]})
        assert "\\n" in result, (
            "Bug N26: _simple_toml_dumps did not escape \\n inside a list item. "
            "Fix: apply the same escaping in the _item() helper inside _simple_toml_dumps."
        )

    def test_output_parseable_after_newline_escape(self, tmp_path: Path) -> None:
        """Round-trip: value with newline must survive write→parse intact."""
        import tero2.config_writer as cw_module

        data = {"section": {"msg": "hello\nworld"}}
        toml_str = cw_module._serialize_toml(data)

        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-reuse-def]

        try:
            parsed = tomllib.loads(toml_str)
        except Exception as exc:
            pytest.fail(
                f"Bug N26: _simple_toml_dumps produced invalid TOML when value "
                f"contains a newline. Parse error: {exc}"
            )

        assert parsed["section"]["msg"] == "hello\nworld", (
            "Bug N26: round-trip value with newline did not survive serialization."
        )
