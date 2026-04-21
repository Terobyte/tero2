"""Negative tests for new bugs N7-N15 from bugs.md.

Convention:
  - Each test FAILS when the bug is present (red).
  - Each test PASSES when the bug is fixed (green / regression guard).

Run:  pytest tests/test_n7_n15_bugs.py -v
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# N7 — settings.py: SettingsScreen doesn't load existing config values
# ═══════════════════════════════════════════════════════════════════════


class TestN7SettingsScreenLoadsExistingConfig:
    """SettingsScreen compose() creates all inputs with empty defaults.
    There is no on_mount that reads the existing config file and pre-fills
    the widgets. Pressing 's' immediately after opening overwrites the
    entire telegram section with empty values.

    Fix: add on_mount() that calls _load_toml(self._config_path) and
    populates widget values from the result.
    """

    def test_settings_screen_has_on_mount(self) -> None:
        import tero2.tui.screens.settings as settings_module

        has_on_mount = hasattr(settings_module.SettingsScreen, "on_mount")
        assert has_on_mount, (
            "Bug N7: SettingsScreen has no on_mount method — existing config values "
            "are never loaded into widgets. Opening settings and saving immediately "
            "overwrites the config with empty token/chat_id/enabled=False. "
            "Fix: add on_mount() that reads _config_path and populates inputs."
        )

    def test_on_mount_reads_config_path(self) -> None:
        import tero2.tui.screens.settings as settings_module

        if not hasattr(settings_module.SettingsScreen, "on_mount"):
            pytest.skip("on_mount does not exist — covered by test above")

        source = inspect.getsource(settings_module.SettingsScreen.on_mount)
        reads_config = "_config_path" in source or "_load_toml" in source
        assert reads_config, (
            "Bug N7: on_mount exists but does not read _config_path. "
            "Widgets are still empty on open — existing values are discarded on save. "
            "Fix: read _load_toml(self._config_path) in on_mount and set widget values."
        )

    @pytest.mark.asyncio
    async def test_settings_screen_prepopulates_token(self, tmp_path: Path) -> None:
        """Functional: existing bot_token must appear in the #tg-token input."""
        from textual.app import App
        from textual.widgets import Input

        from tero2.tui.screens.settings import SettingsScreen

        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[telegram]\nbot_token = "existingtoken"\nenabled = true\n',
            encoding="utf-8",
        )

        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = SettingsScreen(config_path=config_path)
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.2)

            token_input = screen.query_one("#tg-token", Input)
            assert token_input.value == "existingtoken", (
                f"Bug N7: #tg-token shows '{token_input.value}' instead of 'existingtoken'. "
                "SettingsScreen does not load existing config on open. "
                "Fix: add on_mount that reads config_path and sets widget.value."
            )


# ═══════════════════════════════════════════════════════════════════════
# N8 — plan_pick.py: dismiss(None) in worker crashes if screen already closed
# ═══════════════════════════════════════════════════════════════════════


class TestN8PlanPickDismissInWorker:
    """_load_files() is an async worker launched via run_worker().
    If the user presses Esc before the file scan completes, the screen
    is popped. When the worker then calls self.dismiss(None), Textual
    raises ScreenStackError because the screen is no longer on the stack.

    Fix: guard with `if self.is_attached:` before calling dismiss(), or
    wrap in try/except ScreenStackError.
    """

    def test_load_files_guards_dismiss(self) -> None:
        import tero2.tui.screens.plan_pick as pp_module

        source = inspect.getsource(pp_module.PlanPickScreen._load_files)
        lines = source.splitlines()

        dismiss_idx = next(
            (i for i, l in enumerate(lines) if "self.dismiss(None)" in l), None
        )
        if dismiss_idx is None:
            pytest.skip("dismiss(None) call not found in _load_files")

        # Look for a guard on the dismiss call
        context = "\n".join(lines[max(0, dismiss_idx - 5) : dismiss_idx + 2])
        has_guard = (
            "is_attached" in context
            or "ScreenStackError" in context
            or "try:" in context
        )
        assert has_guard, (
            "Bug N8: _load_files calls self.dismiss(None) without checking if the "
            "screen is still attached. If the user closes the screen (Esc) before "
            "the scan finishes, ScreenStackError is raised in the worker. "
            "Fix: guard with `if self.is_attached: self.dismiss(None)` or "
            "wrap in try/except ScreenStackError."
        )


# ═══════════════════════════════════════════════════════════════════════
# N9 — role_swap.py: get_models() failure leaves screen stuck
# ═══════════════════════════════════════════════════════════════════════


class TestN9RoleSwapGetModelsFailure:
    """_handle_provider_selected() awaits get_models(provider) without
    any error handling. If get_models() raises or returns [], an empty
    ModelPickScreen is pushed — user sees a blank list with no explanation,
    or the worker dies silently leaving the screen stuck at step 2.

    Fix: wrap get_models() in try/except and check `if not models:` before
    pushing ModelPickScreen, notifying the user on failure.
    """

    def test_handle_provider_selected_checks_empty_models(self) -> None:
        import tero2.tui.screens.role_swap as rs_module

        source = inspect.getsource(rs_module.RoleSwapScreen._handle_provider_selected)
        has_empty_check = (
            "not models" in source
            or "len(models)" in source
            or "if models" in source
        )
        assert has_empty_check, (
            "Bug N9: _handle_provider_selected does not check if get_models() "
            "returned []. Empty ModelPickScreen is pushed — user sees a blank list. "
            "Fix: check `if not models:` and notify before pushing ModelPickScreen."
        )

    def test_handle_provider_selected_handles_get_models_exception(self) -> None:
        import tero2.tui.screens.role_swap as rs_module

        source = inspect.getsource(rs_module.RoleSwapScreen._handle_provider_selected)
        lines = source.splitlines()
        get_models_idx = next(
            (i for i, l in enumerate(lines) if "get_models" in l), None
        )
        if get_models_idx is None:
            pytest.skip("get_models call not found in _handle_provider_selected")

        context_lines = lines[max(0, get_models_idx - 5) : get_models_idx + 10]
        # Use precise line-start matching to avoid false positives like "entry:"
        has_try = any(l.strip() == "try:" for l in context_lines)
        has_except = any(l.strip().startswith("except") for l in context_lines)
        has_guard = has_try or has_except
        assert has_guard, (
            "Bug N9: get_models() in _handle_provider_selected has no exception "
            "handler. If the network call fails or the provider is unreachable, "
            "the worker dies silently and the screen is stuck on step 2. "
            "Fix: wrap get_models() in try/except and notify on failure."
        )


# ═══════════════════════════════════════════════════════════════════════
# N10 — providers_pick.py: same as N9
# ═══════════════════════════════════════════════════════════════════════


class TestN10ProvidersPickGetModelsFailure:
    """providers_pick._handle_provider_selected() has the same pattern as N9:
    get_models() called without error handling or empty-list check.

    Fix: same as N9 — check empty list, handle exceptions.
    """

    def test_providers_pick_checks_empty_models(self) -> None:
        import tero2.tui.screens.providers_pick as pp_module

        source = inspect.getsource(pp_module.ProvidersPickScreen._handle_provider_selected)
        has_empty_check = (
            "not models" in source
            or "len(models)" in source
            or "if models" in source
        )
        assert has_empty_check, (
            "Bug N10: providers_pick._handle_provider_selected does not check if "
            "get_models() returned []. Empty ModelPickScreen is pushed silently. "
            "Fix: check `if not models:` before pushing ModelPickScreen."
        )

    def test_providers_pick_handles_get_models_exception(self) -> None:
        import tero2.tui.screens.providers_pick as pp_module

        source = inspect.getsource(pp_module.ProvidersPickScreen._handle_provider_selected)
        lines = source.splitlines()
        get_models_idx = next(
            (i for i, l in enumerate(lines) if "get_models" in l), None
        )
        if get_models_idx is None:
            pytest.skip("get_models call not found in providers_pick._handle_provider_selected")

        context_lines = lines[max(0, get_models_idx - 5) : get_models_idx + 10]
        has_try = any(l.strip() == "try:" for l in context_lines)
        has_except = any(l.strip().startswith("except") for l in context_lines)
        has_guard = has_try or has_except
        assert has_guard, (
            "Bug N10: get_models() in providers_pick._handle_provider_selected has "
            "no exception handler. Worker dies silently on failure. "
            "Fix: wrap in try/except and notify user on failure."
        )


# ═══════════════════════════════════════════════════════════════════════
# N11 — model_pick.py: empty entries shows blank list with no message
# ═══════════════════════════════════════════════════════════════════════


class TestN11ModelPickEmptyEntriesNoMessage:
    """When ModelPickScreen is initialized with entries=[], compose() builds
    an empty ListView with no explanation. The user sees a blank screen and
    has no idea what happened or what to do next.

    Fix: when _all_entries is empty, show a "No models available" label
    instead of (or in addition to) the empty list.
    """

    def test_compose_shows_message_for_empty_entries(self) -> None:
        import tero2.tui.screens.model_pick as mp_module

        source = inspect.getsource(mp_module.ModelPickScreen.compose)
        has_empty_message = (
            "not" in source and ("_all_entries" in source or "_filtered" in source)
            or "empty" in source.lower()
            or "нет" in source.lower()
            or "no model" in source.lower()
        )
        assert has_empty_message, (
            "Bug N11: ModelPickScreen.compose() does not handle empty entries. "
            "When get_models() returns [], the user sees a blank list with no "
            "explanation. Fix: check `if not self._all_entries:` in compose() "
            "and yield a Label explaining no models are available."
        )

    @pytest.mark.asyncio
    async def test_model_pick_shows_empty_state_message(self) -> None:
        """Functional: empty entries must render a visible 'no models' label."""
        from textual.app import App
        from textual.widgets import Label

        from tero2.tui.screens.model_pick import ModelPickScreen

        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=[])
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.2)

            labels = screen.query(Label)
            texts = [str(lbl.render()) for lbl in labels]
            has_message = any(
                any(kw in t.lower() for kw in ("нет", "no", "empty", "found", "available"))
                for t in texts
            )
            assert has_message, (
                f"Bug N11: empty ModelPickScreen shows no helpful message. "
                f"Labels found: {texts}. Fix: show 'No models found' label."
            )


# ═══════════════════════════════════════════════════════════════════════
# N12 — project_pick.py: 'd' key deletes instantly without confirmation
# ═══════════════════════════════════════════════════════════════════════


class TestN12ProjectPickDeleteNoConfirmation:
    """action_delete_entry() calls self._entries.pop(idx) immediately when
    'd' is pressed — no confirmation dialog. A fat-finger on 'd' destroys
    a history entry permanently.

    Fix: push a confirmation dialog (e.g. ConfirmScreen) before deleting,
    or at minimum ask the user to press 'd' a second time to confirm.
    """

    def test_delete_requires_confirmation(self) -> None:
        import tero2.tui.screens.project_pick as pp_module

        source = inspect.getsource(pp_module.ProjectPickScreen.action_delete_entry)
        has_confirmation = (
            "push_screen" in source
            or "confirm" in source.lower()
            or "dialog" in source.lower()
            or "Confirm" in source
        )
        assert has_confirmation, (
            "Bug N12: action_delete_entry deletes the entry immediately on 'd' press. "
            "No confirmation dialog is shown — accidental key press destroys data. "
            "Fix: push a confirmation screen or require a second key press before "
            "deleting the history entry."
        )


# ═══════════════════════════════════════════════════════════════════════
# N13 — providers_pick.py: _write_project_config uses fixed .tmp without flock
# ═══════════════════════════════════════════════════════════════════════


class TestN13ProvidersPickFixedTmpNoFlock:
    """_write_project_config() uses config_path.with_suffix('.tmp') — a fixed
    name with no file locking. If two tero2 instances write to the same project
    concurrently, they race on the same .tmp file.

    By contrast, config_writer.write_global_config_section() uses both a
    PID-based temp path AND fcntl.flock — the correct pattern.

    Fix: use a unique tmp name (PID/UUID) and/or fcntl.flock in
    _write_project_config, consistent with config_writer.
    """

    def test_write_project_config_uses_flock_or_unique_tmp(self) -> None:
        import tero2.tui.screens.providers_pick as pp_module

        source = inspect.getsource(pp_module.ProvidersPickScreen._write_project_config)
        has_flock = "flock" in source
        has_unique_tmp = (
            "getpid" in source
            or "uuid" in source.lower()
            or "os.getpid" in source
        )
        assert has_flock or has_unique_tmp, (
            "Bug N13: _write_project_config uses a fixed '.tmp' suffix with no "
            "file locking. Concurrent writes to the same project config race on "
            "the same .tmp file. config_writer.write_global_config_section uses "
            "both flock and a unique tmp — _write_project_config should match. "
            "Fix: add fcntl.flock() or use a PID/UUID-based tmp filename."
        )


# ═══════════════════════════════════════════════════════════════════════
# N14 — tui/app.py: action_new_project is a stub
# ═══════════════════════════════════════════════════════════════════════


class TestN14NewProjectActionStub:
    """DashboardApp.action_new_project() is a stub: it just logs
    'Смена проекта — будет в M2.' and does nothing.  The 'n' key binding
    is shown in the footer, giving the user a false expectation.

    Fix: implement the action — push StartupWizard or equivalent flow.
    """

    def test_action_new_project_is_not_stub(self) -> None:
        import tero2.tui.app as app_module

        if not hasattr(app_module.DashboardApp, "action_new_project"):
            pytest.skip("action_new_project not found in DashboardApp")

        source = inspect.getsource(app_module.DashboardApp.action_new_project)
        stub_markers = ("будет в M2", "будет в M3", "# TODO", "# stub", "pass\n")
        is_stub = any(m in source for m in stub_markers)
        assert not is_stub, (
            "Bug N14: action_new_project is a stub (still has 'будет в M2' marker). "
            "The 'n' key is visible in the footer but does nothing useful — "
            "misleading UX. Fix: implement the new-project flow (e.g. push "
            "StartupWizard with a callback to replace the current runner)."
        )


# ═══════════════════════════════════════════════════════════════════════
# N15 — config_writer.py: _simple_toml_dumps formats all list items as strings
# ═══════════════════════════════════════════════════════════════════════


class TestN15ConfigWriterListItemsAsStrings:
    """_simple_toml_dumps serializes list items as f'"{i}"' unconditionally.
    Boolean or integer values in a list become strings: True → "True",
    1 → "1". TOML readers then see strings, not booleans/ints.

    This only matters when tomli-w is NOT installed (fallback path), but
    it's still incorrect behaviour.

    Fix: check the type of each list item and format accordingly —
    booleans as `true`/`false`, ints as bare numbers, strings as quoted.
    """

    def test_list_with_booleans_serialized_as_toml_bools(self) -> None:
        """_simple_toml_dumps must write True as `true`, not `"True"`."""
        import tero2.config_writer as cw_module

        result = cw_module._simple_toml_dumps({"flags": [True, False]})
        # TOML booleans are lowercase unquoted: true, false
        assert '"True"' not in result and '"False"' not in result, (
            f"Bug N15: _simple_toml_dumps serialized booleans as quoted strings. "
            f"Output: {result!r}. Fix: check isinstance(i, bool) and emit "
            "`true`/`false` without quotes."
        )
        assert "true" in result and "false" in result, (
            f"Bug N15: booleans missing from list output. Got: {result!r}. "
            "Fix: serialize booleans as unquoted `true`/`false`."
        )

    def test_list_with_integers_serialized_as_ints(self) -> None:
        """_simple_toml_dumps must write integers as bare numbers, not strings."""
        import tero2.config_writer as cw_module

        result = cw_module._simple_toml_dumps({"counts": [1, 2, 3]})
        assert '"1"' not in result, (
            f"Bug N15: _simple_toml_dumps serialized integer 1 as '\"1\"'. "
            f"Output: {result!r}. Fix: check isinstance(i, int) and emit bare number."
        )

    def test_list_with_strings_still_quoted(self) -> None:
        """[Regression guard] String list items must remain quoted."""
        import tero2.config_writer as cw_module

        result = cw_module._simple_toml_dumps({"names": ["alice", "bob"]})
        assert '"alice"' in result and '"bob"' in result, (
            f"Bug N15 regression: string list items must stay quoted. Got: {result!r}"
        )

    def test_write_section_with_boolean_list_roundtrips(self, tmp_path: Path) -> None:
        """End-to-end: write a section with boolean list, read back, verify types."""
        try:
            import tomli_w  # noqa: F401
            pytest.skip("tomli_w installed — fallback path not exercised")
        except ImportError:
            pass

        from tero2.config_writer import write_global_config_section, _load_toml

        config_path = tmp_path / "config.toml"
        write_global_config_section(
            config_path, "test_section", {"flags": [True, False]}
        )

        data = _load_toml(config_path)
        flags = data.get("test_section", {}).get("flags", [])
        assert flags == [True, False], (
            f"Bug N15: boolean list did not round-trip correctly. "
            f"Expected [True, False], got {flags!r}. "
            "Fix: serialize booleans as `true`/`false` in _simple_toml_dumps."
        )
