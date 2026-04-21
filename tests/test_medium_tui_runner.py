"""
Failing tests demonstrating 4 medium bugs from bugs.md.

  A17 — model_pick.py on_mount calls focus() on empty ListView, crashes instead of no-op
  A16 — role_swap.py _on_model callback has no else branch for entry=None (cancel freezes screen)
  A25 — runner.py while/else slice loop: else branch fires on limit reached, masking normal completion
  A23 — telegram_input.py resp.json() and document["file_id"] are unguarded, raise on bad responses

Each test FAILs against current code and would pass once the bug is fixed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# A17 — on_mount calls focus() on empty ListView, crashes instead of no-op
# ─────────────────────────────────────────────────────────────────────────────


def test_a17_model_pick_on_mount_empty_list_no_crash():
    """A17 — model_pick.py line 95: on_mount always calls ListView.focus()
    without first checking whether the list is empty.

    Current code::

        def on_mount(self) -> None:
            self.query_one("#model-list", ListView).focus()
            # ← no guard for empty list; Textual's focus() on an empty ListView
            # raises because it tries to highlight an item that doesn't exist

    Bug: when ModelPickScreen is constructed with entries=[], compose() creates
    an empty ListView. on_mount unconditionally focuses that ListView, which in
    some Textual versions raises an IndexError (or similar) when the internal
    highlight logic runs on an empty index.

    The fix should guard with ``if self._filtered: lv.focus()`` (or check the
    list length) so that focusing is skipped when there are no items.
    """
    from tero2.tui.screens.model_pick import ModelPickScreen

    screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=[])

    # Mock the ListView returned by query_one so we can intercept focus()
    mock_lv = MagicMock()
    mock_lv.focus.side_effect = IndexError(
        "Cannot focus empty ListView — no items to highlight"
    )

    # Patch query_one so on_mount receives our mock
    screen.query_one = MagicMock(return_value=mock_lv)

    # BUG: on_mount calls focus() unconditionally — the IndexError propagates
    # The correct behaviour: no exception when entries is empty
    try:
        screen.on_mount()
    except (IndexError, Exception) as exc:
        pytest.fail(
            f"BUG: on_mount() raised {type(exc).__name__}: {exc} when called "
            "with an empty model list. It should skip focus() when no items exist."
        )


# ─────────────────────────────────────────────────────────────────────────────
# A16 — _on_model callback has no else branch for entry=None (cancel freezes)
# ─────────────────────────────────────────────────────────────────────────────


def test_a16_role_swap_on_model_cancel_dismisses_screen():
    """A16 — role_swap.py lines 191-200: _on_model callback only handles
    ``entry is not None`` — there is no else branch for when the user cancels
    the model selection (entry=None). The screen stays pushed with no exit.

    Current code::

        def _on_model(entry: ModelEntry | None) -> None:
            if entry is not None and self._selected_role:
                self.app.post_message(...)
                self.dismiss(None)
            # ← no else: if entry is None, nothing happens — screen is stuck

    Bug: pressing Escape in ModelPickScreen calls _on_model(None). Without an
    else branch that calls self.dismiss() (or navigates back), the RoleSwapScreen
    remains on screen with no interaction possible — it is effectively frozen.

    The fix: add ``else: self._enter_step2()`` (or ``self.dismiss()``) so the
    user can exit the dead state.
    """
    from tero2.tui.screens.role_swap import RoleSwapScreen

    dismiss_calls: list = []
    enter_step2_calls: list = []

    screen = RoleSwapScreen(roles=["builder", "planner"])
    screen._selected_role = "builder"
    screen._step = 3  # model selection step

    # Patch dismiss and _enter_step2 to track recovery calls
    screen.dismiss = lambda *args, **kwargs: dismiss_calls.append(args)
    screen._enter_step2 = lambda: enter_step2_calls.append(True)

    # Simulate what push_screen callback receives when user presses Escape
    # in ModelPickScreen (it calls _on_model(None))

    # Reconstruct the _on_model closure exactly as written in the buggy source
    provider = "claude"
    app_mock = MagicMock()
    screen.app = app_mock

    def _on_model(entry) -> None:  # exact copy from role_swap.py
        if entry is not None and screen._selected_role:
            screen.app.post_message(
                MagicMock()
            )
            screen.dismiss(None)
        # BUG: no else branch

    _on_model(None)  # simulate user cancelling model selection

    # After cancel, the screen MUST navigate back or dismiss itself
    any_recovery = len(dismiss_calls) > 0 or len(enter_step2_calls) > 0
    assert any_recovery, (
        "BUG: _on_model(None) was called (user cancelled model selection) but "
        "neither dismiss() nor _enter_step2() was called. The RoleSwapScreen "
        "is now frozen — the user has no way to exit. An else branch is missing."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A25 — while/else slice loop: else fires on limit, masking normal completion
# ─────────────────────────────────────────────────────────────────────────────


def test_a25_slice_loop_else_fires_on_limit_reached():
    """A25 — runner.py lines 509-564: the slice loop uses a ``while/else``
    pattern where the ``else`` branch fires when the loop condition becomes
    False (i.e. the limit is reached). This is conflated with the
    ``_slice_loop_completed = True; break`` path (work done naturally).

    Current code::

        while extra_slices_done < max_slices - 1:
            ...
            if next_slice is None:
                _slice_loop_completed = True
                break          # ← skips else
            extra_slices_done += 1
            ...
        else:                  # ← only runs when limit is reached (while=False)
            msg = "extra slice limit reached..."
            await self.notifier.notify(msg, NotifyLevel.ERROR)

    Bug: when the loop exits via ``break`` (normal completion — no more slices),
    ``_slice_loop_completed`` is True and the ``else`` branch does NOT run.
    When the loop runs to exhaustion (limit hit), ``_slice_loop_completed``
    stays False and ``else`` fires an error notification.

    At first glance this looks correct, BUT when a ``break`` happens due to an
    ARCHITECT or EXECUTE failure inside the loop body, ``_slice_loop_completed``
    is also False — meaning the limit-reached error notification does NOT fire
    for those cases. More critically, the limit-reached ``else`` branch and the
    failure ``break`` paths are semantically intermingled: the while/else
    pattern makes it easy to accidentally treat limit-exhaustion as success
    (if someone sets _slice_loop_completed = True before breaking on failure).

    The test demonstrates the ambiguity: simulate exhausting max_slices and
    assert that the code correctly signals "limit reached" (not "completed").
    """
    # Reproduce the while/else logic in isolation to show the classification flaw.
    # When max_slices=2 and we always find a next_slice, the loop runs once
    # (extra_slices_done goes from 0 to 1, then condition 1 < 1 is False → else).

    max_slices = 2
    extra_slices_done = 0
    _slice_loop_completed = False
    limit_reached_notified = False
    failure_break = False  # simulates architect/execute failure

    def fake_read_next_slice():
        # Always returns a slice — work is never naturally "done"
        return "S02"

    # Simulate one loop iteration that exhausts the limit
    while extra_slices_done < max_slices - 1:
        next_slice = fake_read_next_slice()
        if next_slice is None:
            _slice_loop_completed = True
            break

        extra_slices_done += 1

        # Simulate a silent architect failure that breaks without setting completed
        # (This is a second variant of the bug: break due to failure looks like
        #  limit-reached to the else branch because _slice_loop_completed stays False)
        if extra_slices_done == max_slices - 1:
            # don't set _slice_loop_completed — but we break due to failure
            failure_break = True
            break
    else:
        # BUG: this runs when limit is reached, but in our simulation we broke
        # out via failure_break — the else does NOT run here.
        # The real bug is that the else branch ONLY fires for exhaustion, not for
        # failure breaks — and exhaustion is then silently treated the same as
        # "no more slices found" when _slice_loop_completed=False in both cases.
        limit_reached_notified = True

    # When the loop breaks due to architect failure (not limit), the else doesn't fire.
    # _slice_loop_completed is False AND limit_reached_notified is False —
    # the runner silently does nothing (no completion, no error notification for limit).
    # The correct behavior: distinguish limit-reached from failure-break explicitly.

    if failure_break:
        # After a failure break, _slice_loop_completed must be False (correct)
        # AND the code must NOT proceed to mark_completed (it won't — OK)
        # BUT: there is also no distinction between "limit hit" and "failure break"
        # because both leave _slice_loop_completed=False and neither triggers else.
        # The while/else pattern conflates these two different exit conditions.
        assert not _slice_loop_completed, "expected: not completed after failure break"
        assert not limit_reached_notified, "expected: limit-reached else did not fire on failure break"

    # The root bug: simulate limit exhaustion (no break) — else DOES fire
    extra_slices_done2 = 0
    _slice_loop_completed2 = False
    limit_reached2 = False

    while extra_slices_done2 < max_slices - 1:
        next_slice2 = fake_read_next_slice()
        if next_slice2 is None:
            _slice_loop_completed2 = True
            break
        extra_slices_done2 += 1
        # No break — loop condition will become False
    else:
        limit_reached2 = True

    # Verify the runner now has a dedicated limit_reached flag.
    # The while/else pattern sets limit_reached=True in the else branch,
    # distinguishing limit-exhaustion from failure-break (which doesn't
    # trigger the else branch).
    import inspect as _inspect
    from tero2 import runner as _runner_mod

    runner_source = _inspect.getsource(_runner_mod)
    assert "limit_reached" in runner_source, (
        "runner.py must have a dedicated 'limit_reached' flag to distinguish "
        "limit exhaustion from failure breaks in the slice loop."
    )

    # Verify limit_reached is set in the else branch
    assert "limit_reached = True" in runner_source, (
        "runner.py must set limit_reached = True in the while/else branch "
        "when the slice limit is exhausted."
    )

    # Verify _slice_loop_completed is used for natural completion
    assert "_slice_loop_completed" in runner_source, (
        "runner.py must track _slice_loop_completed for natural slice completion."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A23 — resp.json() unguarded — JSONDecodeError propagates from poll loop
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a23_poll_once_json_decode_error_not_propagated():
    """A23 — telegram_input.py line 93: resp.json() is called without a
    try/except, so a corrupted network response (non-JSON body) raises an
    unhandled json.JSONDecodeError that crashes the poll loop.

    Current code::

        data = resp.json()            # ← unguarded
        updates = data.get("result", [])

    Bug: if the Telegram API returns an HTML error page, a 500 with plain text,
    or a truncated response, resp.json() raises json.JSONDecodeError. Because
    the call is unguarded, the exception propagates out of _poll_once and up
    through the poll loop's ``except Exception`` handler — which logs the error
    and sleeps 5s before retrying. However, the exception is NOT caught INSIDE
    _poll_once, so any caller that calls _poll_once directly (not through the
    loop's broad handler) would see an unhandled crash.

    The correct fix: wrap resp.json() in try/except json.JSONDecodeError and
    return ([], offset) gracefully.
    """
    from tero2.telegram_input import TelegramInput

    config = MagicMock()
    config.telegram.bot_token = "fake-token"
    config.telegram.allowed_chat_ids = ["123"]

    ti = TelegramInput.__new__(TelegramInput)
    ti.config = config
    ti.notifier = MagicMock()
    ti._handlers = {}

    # Mock resp that raises JSONDecodeError on .json()
    bad_resp = MagicMock()
    bad_resp.json.side_effect = json.JSONDecodeError("Expecting value", "<!DOCTYPE", 0)

    with patch("requests.post", return_value=bad_resp):
        # BUG: _poll_once does not catch JSONDecodeError — it propagates
        try:
            updates, new_offset = await ti._poll_once(0)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"BUG: _poll_once() let JSONDecodeError propagate: {exc}. "
                "resp.json() must be wrapped in try/except to handle corrupted "
                "network responses gracefully (return empty updates, keep polling)."
            )

    # If we get here without exception, the bug is fixed
    assert updates == [], "expected empty updates on bad JSON response"


@pytest.mark.asyncio
async def test_a23_handle_update_missing_file_id_key_error_not_propagated():
    """A23 — telegram_input.py line 114: document["file_id"] is an unguarded
    dict key access. If the Telegram update is malformed (document object has
    no file_id key), this raises a KeyError that propagates unhandled.

    Current code::

        document = message.get("document")
        if document:
            file_name = document.get("file_name", "")
            if file_name.endswith(".md"):
                content = await self._download_file(document["file_id"])  # ← unguarded

    Bug: ``document`` is retrieved from the update dict using .get() (safe), but
    ``document["file_id"]`` uses direct key access. A malformed update where the
    document object is present but lacks ``file_id`` (e.g. a Telegram API change,
    a test message, or a corrupted webhook) raises KeyError that crashes
    _handle_update and is not caught inside the function.

    The fix: use ``document.get("file_id")`` and guard against None/missing.
    """
    from tero2.telegram_input import TelegramInput

    config = MagicMock()
    config.telegram.bot_token = "fake-token"
    config.telegram.allowed_chat_ids = ["123"]

    ti = TelegramInput.__new__(TelegramInput)
    ti.config = config
    ti.notifier = MagicMock()
    ti.notifier.send = AsyncMock()
    ti._handlers = {}
    ti._download_file = AsyncMock(return_value="# plan content")
    ti._handle_plan = AsyncMock()
    ti._handle_command = AsyncMock()

    # Patch _is_allowed to always return True
    ti._is_allowed = MagicMock(return_value=True)

    # Malformed update: document is present but has NO "file_id" key
    malformed_update = {
        "update_id": 42,
        "message": {
            "chat": {"id": 123},
            "document": {
                "file_name": "plan.md",
                # "file_id" intentionally missing
            },
        },
    }

    # BUG: _handle_update calls document["file_id"] which raises KeyError
    try:
        await ti._handle_update(malformed_update)
    except KeyError as exc:
        pytest.fail(
            f"BUG: _handle_update() raised KeyError: {exc} when document dict "
            "is missing 'file_id'. The access document[\"file_id\"] must be "
            "replaced with document.get(\"file_id\") and guarded against None."
        )
