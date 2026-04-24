"""Autonomous bug-loop iteration 8.

Two real, deterministic, user-observable bugs in still-unexplored areas:

1. KiloErrorContentAcceptsDictPayload
   Where:  tero2/providers/normalizers/kilo.py, ``KiloNormalizer.normalize``
           error branch (the same shape is also broken in
           tero2/providers/normalizers/zai.py line 137).
   What:   The error-branch builds ``content`` via
               msg = raw.get("text", "") or raw.get("error", "")
           When kilo echoes the Claude-shaped error
               {"type":"error","error":{"message":"rate limited","code":429}}
           ``raw.get("error")`` returns the **dict**, so ``msg`` is a dict
           and the emitted ``StreamEvent.content`` is a dict instead of
           the mandatory ``str``.
           Codex (tero2/providers/normalizers/codex.py, the L7 comment
           branch) already fixes this exact shape by unwrapping the
           nested ``.message`` / ``.error`` field before yielding. Kilo
           never got that same fix.
           Downstream TUI code does string operations on
           ``event.content`` — tero2/tui/widgets/heartbeat_sidebar.py
           lines 192/196 call ``event.content.splitlines()`` and
           ``event.content[:30]``. These raise
               AttributeError: 'dict' object has no attribute 'splitlines'
           the first time a kilo rate-limit error arrives, killing the
           widget update coroutine and leaving the sidebar frozen.
   Fix:    Mirror codex's L7 unwrap. After picking ``msg_or_dict`` from
           the candidate keys, check ``isinstance(msg_or_dict, dict)`` and
           drill into its ``.message`` / ``.error`` before yielding:
                msg_or_dict = raw.get("text", "") or raw.get("error") or ""
                if isinstance(msg_or_dict, dict):
                    msg = (
                        msg_or_dict.get("message")
                        or msg_or_dict.get("error")
                        or "unknown error"
                    )
                else:
                    msg = msg_or_dict
                yield StreamEvent(..., content=str(msg), ...)
           Applying the same fix to ``zai.py`` closes that sibling bug.

2. BuilderRecoverReadsFromCwdWhenWorkingDirEmpty
   Where:  tero2/players/builder.py, ``_recover_summary_from_disk``.
   What:   The function guards against ``working_dir is None`` but NOT
           the empty string. With ``working_dir=""``,
               pathlib.Path("") / f"{task_id}-SUMMARY.md"
           evaluates to the BARE relative name ``"T01-SUMMARY.md"``,
           which ``Path.read_text()`` resolves against the process CWD —
           not the (empty) project path.
           BuilderPlayer.__init__ accepts ``working_dir: str = ""`` by
           default, so any caller that forgets to pass an explicit path
           (unit test harness, partially-constructed RunnerContext,
           phase handler during a crash-recovery retry where working_dir
           wasn't restored) will read a stray ``T01-SUMMARY.md`` sitting
           in the runner's CWD and silently "recover" that content as
           the builder's summary. That summary then flows into the
           verifier check and ends up committed to the .sora tree —
           real disk-observable content confusion.
   Fix:    Treat empty/whitespace-only working_dir the same as None:
                if not working_dir or not working_dir.strip():
                    return ""
           (The explicit None check can be dropped — the falsy guard
            covers both None and ``""``.)

Each test is independent; no cross-test state is required.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


# ── Bug 1: Kilo error normalizer produces non-string content ────────────────


class TestLoopIter8KiloErrorContentAcceptsDictPayload:
    """Kilo normalizer on ``{"type":"error","error":{...}}`` leaves content as dict."""

    def test_kilo_dict_error_produces_string_content(self) -> None:
        from tero2.providers.normalizers.kilo import KiloNormalizer

        normalizer = KiloNormalizer()
        # Claude-shaped error payload that kilo may echo back when a
        # rate-limit or server-side error pokes through as JSON.
        raw = {"type": "error", "error": {"message": "rate limited", "code": 429}}
        events = list(normalizer.normalize(raw, role="builder"))

        assert len(events) == 1, "error branch must yield exactly one event"
        ev = events[0]
        assert ev.kind == "error"
        # BUG: content is a dict — should be a string containing the message.
        assert isinstance(ev.content, str), (
            f"kilo: error content must be str for downstream str-ops, "
            f"got {type(ev.content).__name__}: {ev.content!r}"
        )
        # And the useful text should be preserved (so the user can see
        # "rate limited" in the TUI, not an opaque "{'message': ...}" repr).
        assert "rate limited" in ev.content, (
            f"kilo: error message lost on unwrap — got content {ev.content!r}"
        )

    def test_kilo_dict_error_content_supports_downstream_str_ops(self) -> None:
        """heartbeat_sidebar.py calls ``.splitlines()`` and ``[:30]`` on content.

        When content is a dict these operations raise AttributeError /
        TypeError, silently breaking the TUI update coroutine for any
        error event emitted by a kilo-backed provider.
        """
        from tero2.providers.normalizers.kilo import KiloNormalizer

        normalizer = KiloNormalizer()
        raw = {"type": "error", "error": {"message": "boom", "code": 500}}
        ev = list(normalizer.normalize(raw, role="builder"))[0]

        # Both ops must not raise — content has to be a real string.
        lines = ev.content.splitlines()
        assert isinstance(lines, list)
        trimmed = ev.content[:30]
        assert isinstance(trimmed, str)

    def test_kilo_plain_string_error_still_works(self) -> None:
        """Regression: the fix must not break the common string-error shape."""
        from tero2.providers.normalizers.kilo import KiloNormalizer

        normalizer = KiloNormalizer()
        raw = {"type": "error", "error": "plain string error"}
        ev = list(normalizer.normalize(raw, role="builder"))[0]
        assert ev.kind == "error"
        assert ev.content == "plain string error"


# ── Bug 2: Builder summary recovery reads from CWD when working_dir="" ──────


class TestLoopIter8BuilderRecoverReadsFromCwdWhenWorkingDirEmpty:
    """_recover_summary_from_disk('', ...) resolves to CWD instead of rejecting."""

    def test_empty_working_dir_does_not_leak_to_cwd(self) -> None:
        from tero2.players.builder import _recover_summary_from_disk

        prev_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                # Place a DECOY T01-SUMMARY.md in the runner's CWD. The
                # builder must not read this file — the project dir
                # (which should be working_dir) is an entirely different
                # (empty) location.
                decoy_cwd = Path(td) / "decoy_cwd"
                decoy_cwd.mkdir()
                (decoy_cwd / "T01-SUMMARY.md").write_text(
                    "SECRET_CWD_LEAK_CONTENT", encoding="utf-8"
                )
                os.chdir(decoy_cwd)

                # Call with an empty working_dir — MUST NOT return the
                # decoy content.  The None check in the current code
                # misses the empty-string case.
                result = _recover_summary_from_disk("T01", "")
                assert result == "", (
                    f"builder: empty working_dir leaked to CWD — "
                    f"expected empty, got {result!r}"
                )
        finally:
            os.chdir(prev_cwd)

    def test_none_working_dir_still_rejected(self) -> None:
        """Existing None-guard regression — must keep working after the fix."""
        from tero2.players.builder import _recover_summary_from_disk

        # Should not raise even though Path(None) would.
        result = _recover_summary_from_disk("T01", None)  # type: ignore[arg-type]
        assert result == ""

    def test_whitespace_only_working_dir_also_rejected(self) -> None:
        """A working_dir of pure whitespace is also invalid — should be treated
        like empty / None rather than leaking to CWD."""
        from tero2.players.builder import _recover_summary_from_disk

        prev_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as td:
                decoy_cwd = Path(td) / "ws_cwd"
                decoy_cwd.mkdir()
                (decoy_cwd / "T01-SUMMARY.md").write_text(
                    "WHITESPACE_LEAK_CONTENT", encoding="utf-8"
                )
                os.chdir(decoy_cwd)

                result = _recover_summary_from_disk("T01", "   ")
                assert result == "", (
                    f"builder: whitespace working_dir leaked to CWD — "
                    f"expected empty, got {result!r}"
                )
        finally:
            os.chdir(prev_cwd)
