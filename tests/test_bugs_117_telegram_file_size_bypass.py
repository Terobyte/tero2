"""Bug 117: ``TelegramInputBot._download_file`` 10 MB cap is bypassed when
the API response omits ``file_size``.

The check is::

    file_size = result.get("file_size", 0)
    if file_size and file_size > self._MAX_FILE_SIZE:
        log.warning(...)
        return None

When ``file_size`` is missing (or 0), the ``if file_size and ...`` short-
circuits to False — we skip the rejection branch and fall through to the
download. The 10 MB ``_MAX_FILE_SIZE`` cap is silently bypassed. The then-
unbounded ``requests.get(download_url, timeout=30)`` pulls whatever the
remote server returns and reads it all into memory via ``resp.text``.

For tero2, the remote server is ``api.telegram.org`` and Telegram's API
reliably returns ``file_size``, so the real-world impact today is limited.
But it is still a defense-in-depth bug: the cap exists to stop a malicious
or compromised API response from exhausting memory, and a guard that
silently trusts a missing field is not a guard.

Fix: treat missing ``file_size`` as "unknown → reject", same as the
oversized branch. A well-formed Telegram API response always includes
``file_size``; its absence is already an anomaly.

Test-first per feedback_tdd_order.md.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, TelegramConfig
from tero2.telegram_input import TelegramInputBot


def _make_bot(tmp_path: Path) -> TelegramInputBot:
    config = Config()
    config.projects_dir = str(tmp_path / "projects")
    config.telegram = TelegramConfig(
        bot_token="test-token",
        chat_id="123",
        allowed_chat_ids=["123"],
    )
    bot = TelegramInputBot(config)
    bot.notifier = MagicMock()
    bot.notifier.send = AsyncMock(return_value=True)
    return bot


def _mk_response(json_body: dict, status: int = 200, text: str = "downloaded"):
    """Synthetic ``requests.Response``-like object."""
    resp = MagicMock()
    resp.json.return_value = json_body
    resp.status_code = status
    resp.text = text
    resp.content = text.encode("utf-8")
    return resp


class TestMissingFileSizeRejected:
    """A well-formed Telegram response always includes ``file_size``. Its
    absence is anomalous and must cause us to reject rather than trust the
    endpoint."""

    @pytest.mark.asyncio
    async def test_no_file_size_returns_none(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)

        # getFile response missing file_size; download_url never hit.
        get_file_resp = _mk_response({
            "ok": True,
            "result": {"file_path": "doc/foo.md"},  # no file_size
        })

        with patch(
            "tero2.telegram_input.asyncio.to_thread",
            new=AsyncMock(return_value=get_file_resp),
        ):
            result = await bot._download_file("file-id-1")

        assert result is None, (
            "missing file_size must cause _download_file to return None — "
            "skipping the cap on an anomalous API response defeats the "
            "defense-in-depth the cap exists to provide"
        )

    @pytest.mark.asyncio
    async def test_zero_file_size_returns_none(self, tmp_path: Path) -> None:
        """``file_size: 0`` means "unknown/empty" in Telegram's API. Treat it
        like missing — reject rather than trust."""
        bot = _make_bot(tmp_path)
        get_file_resp = _mk_response({
            "ok": True,
            "result": {"file_path": "doc/foo.md", "file_size": 0},
        })
        with patch(
            "tero2.telegram_input.asyncio.to_thread",
            new=AsyncMock(return_value=get_file_resp),
        ):
            result = await bot._download_file("file-id-2")
        assert result is None


class TestLegitimateDownloadStillWorks:
    """Regression guards: normal small-file downloads must succeed."""

    @pytest.mark.asyncio
    async def test_small_file_downloads(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)

        # First getFile call returns valid metadata; second call (download) returns body.
        get_file_resp = _mk_response({
            "ok": True,
            "result": {"file_path": "doc/foo.md", "file_size": 1234},
        })
        download_resp = _mk_response(
            {}, status=200, text="# real plan content\n"
        )

        # asyncio.to_thread is called twice — first with requests.post for
        # getFile, then with requests.get for the download.
        call_log: list = []

        async def fake_to_thread(fn, *args, **kwargs):
            call_log.append((fn.__name__ if hasattr(fn, "__name__") else str(fn), args, kwargs))
            if len(call_log) == 1:
                return get_file_resp
            return download_resp

        with patch("tero2.telegram_input.asyncio.to_thread", new=fake_to_thread):
            result = await bot._download_file("file-id-ok")

        assert result == "# real plan content\n"

    @pytest.mark.asyncio
    async def test_oversize_file_still_rejected(self, tmp_path: Path) -> None:
        """Pre-existing oversized branch must still fire."""
        bot = _make_bot(tmp_path)
        too_big = bot._MAX_FILE_SIZE + 1
        get_file_resp = _mk_response({
            "ok": True,
            "result": {"file_path": "doc/huge.md", "file_size": too_big},
        })
        with patch(
            "tero2.telegram_input.asyncio.to_thread",
            new=AsyncMock(return_value=get_file_resp),
        ):
            result = await bot._download_file("file-id-huge")
        assert result is None
