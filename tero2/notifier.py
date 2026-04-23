"""Telegram notification sender."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path

import requests

from tero2.config import TelegramConfig

# Test-facing alias: NotifierConfig is the TelegramConfig shape the Notifier
# consumes. Keep separate naming so call sites reading notifier.NotifierConfig
# remain stable if the underlying dataclass ever moves.
NotifierConfig = TelegramConfig

log = logging.getLogger(__name__)

TTS_SCRIPT: Path = Path(
    "/Users/terobyte/Desktop/Projects/Active/scripts/library/tts_fish_audio.py"
)


class NotifyLevel(str, Enum):
    HEARTBEAT = "heartbeat"
    PROGRESS = "progress"
    STUCK = "stuck"
    DONE = "done"
    ERROR = "error"


class Notifier:
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._enabled = bool(config.enabled and config.bot_token and config.chat_id)
        self._session = requests.Session()

    _MAX_MESSAGE_LEN = 4096

    async def send(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> bool:
        if not self._enabled:
            return False
        text = text[:self._MAX_MESSAGE_LEN]
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    self._session.post,
                    f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                    data={"chat_id": self.config.chat_id, "text": text},
                    timeout=10,
                ),
                timeout=15,
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                log.warning("telegram rate limited (429), retry after %ds", retry_after)
                await asyncio.sleep(retry_after)
                return False
            if resp.status_code != 200:
                return False
            if not resp.json().get("ok", False):
                log.warning("telegram api error: %s", resp.json())
                return False
            return True
        except asyncio.CancelledError:
            raise
        except (requests.RequestException, asyncio.TimeoutError, OSError, ValueError):
            # requests: network + HTTP errors. asyncio.TimeoutError: wait_for cap.
            # OSError: socket-level. ValueError: resp.json() on non-JSON response
            # or int() cast on a non-numeric Retry-After header.
            log.warning("telegram send failed", exc_info=True)
            return False

    async def send_voice(self, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            audio_path = await asyncio.wait_for(
                asyncio.to_thread(self._generate_tts, text), timeout=30
            )
        except asyncio.TimeoutError:
            log.warning("TTS generation timed out")
            return False
        if audio_path is None:
            return False
        try:
            def _upload() -> int:
                with open(audio_path, "rb") as f:
                    resp = requests.post(
                        f"https://api.telegram.org/bot{self.config.bot_token}/sendVoice",
                        data={"chat_id": self.config.chat_id},
                        files={"voice": f},
                        timeout=30,
                    )
                return resp.status_code

            status = await asyncio.to_thread(_upload)
            return status == 200
        except asyncio.CancelledError:
            raise
        except (requests.RequestException, OSError):
            # requests: network/HTTP errors during upload. OSError: file open
            # failure or socket-level error.
            log.warning("telegram voice send failed", exc_info=True)
            return False
        finally:
            # TTS file is disposable — delete it so repeated voice notifications
            # don't accumulate ~50-200KB each on disk.
            try:
                Path(audio_path).unlink(missing_ok=True)
            except OSError:
                pass

    async def notify(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> bool:
        try:
            ok = await self.send(text, level)
            if ok and level == NotifyLevel.DONE and self.config.voice_on_done:
                await self.send_voice(text)
            elif ok and level == NotifyLevel.STUCK and self.config.voice_on_stuck:
                await self.send_voice(text)
            return ok
        except asyncio.CancelledError:
            raise
        except (requests.RequestException, OSError, AttributeError):
            # send()/send_voice() already catch their own errors, so this is a
            # belt-and-suspenders outer guard. AttributeError covers bad config
            # objects (missing voice_on_done/voice_on_stuck attrs on mocks).
            log.warning("notify failed", exc_info=True)
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _generate_tts(text: str) -> Path | None:
        try:
            import importlib.util

            # Validate TTS script path: must be an absolute path and must exist as a file.
            if not TTS_SCRIPT.is_absolute():
                log.warning("TTS script path is not absolute, refusing to load: %s", TTS_SCRIPT)
                return None
            if not TTS_SCRIPT.is_file():
                log.warning("TTS script not found at path: %s", TTS_SCRIPT)
                return None

            spec = importlib.util.spec_from_file_location(
                "tts_fish_audio", TTS_SCRIPT
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod.tts_fish_audio_simple(text)
            return Path(result)
        except (ImportError, OSError, AttributeError, TypeError, ValueError):
            # Blanket-ish: TTS script is 3rd-party and can fail many ways:
            # ImportError (module missing), OSError (file/network), AttributeError
            # (renamed function), TypeError/ValueError (bad args/responses).
            # Any of those → degrade silently to text-only notification.
            log.warning("TTS generation failed", exc_info=True)
            return None
