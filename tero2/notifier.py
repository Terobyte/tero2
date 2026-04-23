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

    async def send(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> bool:
        if not self._enabled:
            return False
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    requests.post,
                    f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                    data={"chat_id": self.config.chat_id, "text": text},
                    timeout=10,
                ),
                timeout=15,
            )
            if resp.status_code != 200:
                return False
            if not resp.json().get("ok", False):
                log.warning("telegram api error: %s", resp.json())
                return False
            return True
        except Exception:
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
        except Exception:
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
        except Exception:
            log.warning("notify failed", exc_info=True)
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _generate_tts(text: str) -> Path | None:
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "tts_fish_audio", TTS_SCRIPT
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            result = mod.tts_fish_audio_simple(text)
            return Path(result)
        except Exception:
            log.warning("TTS generation failed", exc_info=True)
            return None
