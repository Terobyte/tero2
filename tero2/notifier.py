"""Telegram notification sender."""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from pathlib import Path

import requests

from tero2.config import TelegramConfig

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
        self._enabled = bool(config.bot_token and config.chat_id)

    async def send(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> bool:
        if not self._enabled:
            return False
        try:
            resp = await asyncio.to_thread(
                requests.post,
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                data={"chat_id": self.config.chat_id, "text": text},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            log.warning("telegram send failed", exc_info=True)
            return False

    async def send_voice(self, text: str) -> bool:
        if not self._enabled:
            return False
        audio_path = await asyncio.to_thread(self._generate_tts, text)
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

    async def notify(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> bool:
        try:
            ok = await self.send(text, level)
            if level == NotifyLevel.DONE and self.config.voice_on_done:
                await self.send_voice(text)
            elif level == NotifyLevel.STUCK and self.config.voice_on_stuck:
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
            import sys

            target = str(TTS_SCRIPT.parent.parent)
            if target not in sys.path:
                sys.path.insert(0, target)
            from library.tts_fish_audio import tts_fish_audio_simple

            result = tts_fish_audio_simple(text)
            return Path(result)
        except Exception:
            log.warning("TTS generation failed", exc_info=True)
            return None
