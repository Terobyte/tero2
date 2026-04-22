"""Telegram input -- receive plans and commands via Telegram bot.

Long-polling bot that:
    1. Accepts markdown files (.md) -> creates project -> starts runner
    2. Accepts text messages -> treats as plan -> creates project -> starts runner
    3. Accepts commands: /status, /stop, /pause
    4. Only responds to allowed chat_ids (security)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import requests

from tero2.config import Config
from tero2.notifier import Notifier, NotifyLevel
from tero2.project_init import _extract_project_name, init_project

log = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class TelegramConfig:
    """Minimal telegram-bot config consumed by TelegramInput.

    Accepts both this flat shape and the richer tero2.config.TelegramConfig —
    _poll_once pulls the token via getattr so either works.
    """

    bot_token: str = ""
    chat_id: str = ""
    allowed_ids: list[str] = field(default_factory=list)


class TelegramInputBot:
    """Telegram bot for receiving plans and commands.

    Args:
        config: tero2 Config with telegram settings.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.notifier = Notifier(config.telegram)
        self._allowed_ids: set[str] = set(config.telegram.allowed_chat_ids)
        self._plan_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._running = False
        self._paused = False
        # Queue holds (project_name, plan_content) tuples.
        # Polling loop enqueues; a separate consumer coroutine dequeues and processes.
        # This prevents the race where two rapid messages both try to acquire the lock:
        # the second would fail without a queue. With a queue, plans are serialized.

    async def start(self) -> None:
        """Start the long-polling loop and plan consumer. Blocks until stopped.

        Launches two coroutines concurrently:
            - _poll_loop(): getUpdates long-polling, enqueues plans
            - _consume_plans(): dequeues plans and runs init_project + runner
        """
        self._running = True
        await asyncio.gather(
            self._poll_loop(),
            self._consume_plans(),
        )

    async def stop(self) -> None:
        """Stop the bot gracefully. Drains the queue before exiting."""
        self._running = False
        self._paused = False
        while not self._plan_queue.empty():
            try:
                self._plan_queue.get_nowait()
                self._plan_queue.task_done()
            except asyncio.QueueEmpty:
                break
        await self._plan_queue.join()

    async def _poll_loop(self) -> None:
        """Long-poll getUpdates and dispatch messages."""
        offset = 0
        while self._running:
            try:
                updates, offset = await self._poll_once(offset)
                for update in updates:
                    await self._handle_update(update)
            except Exception:
                log.error("poll loop error", exc_info=True)
                await asyncio.sleep(5)

    async def _poll_once(self, offset: int) -> tuple[list[dict], int]:
        """One getUpdates call. Returns (updates, new_offset)."""
        # Support both Config (has .telegram) and TelegramConfig (flat fields).
        tg = getattr(self.config, "telegram", self.config)
        url = _BASE_URL.format(token=tg.bot_token, method="getUpdates")
        resp = await asyncio.to_thread(
            requests.post,
            url,
            json={"offset": offset, "timeout": 30},
            timeout=35,  # slightly longer than long-poll timeout
        )
        # HTTP-level failures (429 rate-limit, 5xx) can carry valid JSON; treat
        # those as poll failures rather than processing the error body as data.
        status_code = getattr(resp, "status_code", 200)
        if status_code != 200:
            log.warning("poll_once: HTTP %s, skipping", status_code)
            return [], offset
        try:
            data = resp.json()
        except Exception:
            log.warning("poll_once: failed to decode JSON response, skipping")
            return [], offset
        updates = data.get("result", [])
        if updates:
            offset = updates[-1]["update_id"] + 1
        return updates, offset

    async def _handle_update(self, update: dict) -> None:
        """Route an update to the correct handler."""
        message = update.get("message")
        if not message:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        if not self._is_allowed(chat_id):
            return

        # Handle documents (.md files)
        document = message.get("document")
        if document:
            file_name = document.get("file_name", "")
            if file_name.endswith(".md"):
                file_id = document.get("file_id")
                if not file_id:
                    await self.notifier.send("Malformed document: missing file_id.", NotifyLevel.PROGRESS)
                    return
                content = await self._download_file(file_id)
                if content:
                    await self._handle_plan(content, chat_id)
                    return
            await self.notifier.send("Please send a .md file or text plan.", NotifyLevel.PROGRESS)
            return

        # Handle text
        text = message.get("text", "")
        if not text:
            return

        # Check for commands
        if text.startswith("/"):
            await self._handle_command(text, chat_id)
            return

        # Treat as plan
        await self._handle_plan(text, chat_id)

    async def _handle_plan(self, plan_content: str, chat_id: str) -> None:
        """Handle an incoming plan: extract name, enqueue for processing."""
        project_name = _extract_project_name(plan_content)
        await self._plan_queue.put((project_name, plan_content))
        await self.notifier.send(
            f"Plan received -- queued as '{project_name}'",
            NotifyLevel.PROGRESS,
        )

    async def _handle_command(self, text: str, chat_id: str) -> None:
        """Handle slash commands: /status, /stop, /pause.

        In group chats and any chat with more than one bot, Telegram appends
        ``@<bot_username>`` to the command so it is routed only to the
        intended bot. Strip that suffix before matching so ``/stop@tero2_bot``
        behaves identically to ``/stop``. Usernames are case-insensitive.
        """
        command = text.strip().split()[0].lower().split("@", 1)[0]

        if command == "/status":
            status = "paused" if self._paused else ("running" if self._running else "stopped")
            await self.notifier.send(
                f"Queue size: {self._plan_queue.qsize()} | Status: {status}",
                NotifyLevel.PROGRESS,
            )
        elif command == "/stop":
            await self.notifier.send("Stopping bot...", NotifyLevel.PROGRESS)
            self._running = False
        elif command == "/pause":
            self._paused = True
            await self.notifier.send(
                "Paused plan consumption (polling continues). Use /resume to continue.",
                NotifyLevel.PROGRESS,
            )
        elif command == "/resume":
            self._paused = False
            await self.notifier.send(
                "Resumed plan consumption.",
                NotifyLevel.PROGRESS,
            )
        else:
            await self.notifier.send(
                f"Unknown command: {command}. Available: /status, /stop, /pause, /resume",
                NotifyLevel.PROGRESS,
            )

    async def _consume_plans(self) -> None:
        """Consume plans from the queue and start runners.

        Runs as a background coroutine alongside _poll_loop().
        Plans are processed sequentially -- one at a time.
        This prevents two runners from fighting over the same project lock.

        Respects the _paused flag: when paused, dequeued plans are put back
        onto the queue and consumption waits until resumed.

        On error (e.g., FileExistsError from init_project), sends a Telegram
        notification and continues to the next plan.
        """
        while self._running:
            # If paused, wait until resumed
            if self._paused:
                await asyncio.sleep(1.0)
                continue

            try:
                project_name, plan_content = await asyncio.wait_for(
                    self._plan_queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue

            # Re-check paused state after dequeueing
            if self._paused:
                # Put the plan back, mark the dequeued item done, and wait
                await self._plan_queue.put((project_name, plan_content))
                self._plan_queue.task_done()
                await asyncio.sleep(1.0)
                continue

            try:
                project_path = init_project(project_name, plan_content, self.config)
                await self.notifier.send(
                    f"project '{project_name}' created -- starting runner",
                    NotifyLevel.PROGRESS,
                )
                # Launch runner as a subprocess to avoid blocking the bot
                await self._launch_runner(project_path)
            except FileExistsError:
                # Project name collision -- notify and skip
                await self.notifier.send(
                    f"project '{project_name}' already exists -- skipping",
                    NotifyLevel.ERROR,
                )
            except Exception as exc:
                log.error(f"failed to process plan '{project_name}': {exc}")
                await self.notifier.send(
                    f"failed to start '{project_name}': {exc}",
                    NotifyLevel.ERROR,
                )
            finally:
                self._plan_queue.task_done()

    async def _launch_runner(self, project_path: Path) -> None:
        """Launch tero2 runner as a subprocess for the given project.

        Schedules a background watcher that calls proc.wait() and checks
        proc.returncode to detect immediate startup failures, then sends
        a Telegram error notification if the runner exits non-zero within
        30 seconds of launch.
        """
        plan_path = project_path / ".sora" / "milestones" / "M001" / "ROADMAP.md"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tero2.cli",
            "run",
            str(project_path),
            "--plan",
            str(plan_path),
            stderr=asyncio.subprocess.PIPE,
        )
        log.info(f"launched runner (PID {proc.pid}) for {project_path.name}")
        asyncio.create_task(self._watch_runner(proc, project_path))
        await asyncio.sleep(0)  # yield so watcher starts before we return

    async def _watch_runner(
        self, proc: asyncio.subprocess.Process, project_path: Path
    ) -> None:
        """Background watcher: detects immediate startup failures and captures stderr."""
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
            if proc.returncode != 0:
                stderr_bytes = await proc.stderr.read()
                stderr_text = stderr_bytes.decode(errors="replace").strip()
                msg = f"runner for '{project_path.name}' failed to start (exit {proc.returncode})"
                if stderr_text:
                    msg += f"\n{stderr_text}"
                log.error(msg)
                await self.notifier.send(msg, NotifyLevel.ERROR)
        except asyncio.TimeoutError:
            # Subprocess is still running past the startup window. If we leave
            # proc.stderr untouched, its pipe buffer will eventually fill and
            # the child will block on write. Spawn a background drain task so
            # the child can keep producing output.
            if proc.stderr is not None:
                async def _drain_stderr() -> None:
                    try:
                        while True:
                            chunk = await proc.stderr.read(4096)
                            if not chunk:
                                break
                    except Exception:
                        pass
                asyncio.create_task(_drain_stderr())

    def _is_allowed(self, chat_id: str) -> bool:
        """Check if chat_id is in the allowed list."""
        if not self._allowed_ids:
            return False
        return str(chat_id) in self._allowed_ids

    _MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

    async def _download_file(self, file_id: str) -> str | None:
        """Download a file from Telegram by file_id. Returns content as string."""
        try:
            url = _BASE_URL.format(token=self.config.telegram.bot_token, method="getFile")
            resp = await asyncio.to_thread(
                requests.post, url, json={"file_id": file_id}, timeout=10
            )
            data = resp.json()
            result = data.get("result", {})
            file_path = result.get("file_path")
            if not file_path:
                return None

            # Bug 117: reject when file_size is missing or zero. Telegram's
            # getFile response reliably includes file_size for legitimate
            # files; its absence means the response is anomalous and we
            # cannot enforce the 10 MB cap, so the defense-in-depth guard
            # has to fail closed.
            file_size = result.get("file_size")
            if not file_size or file_size > self._MAX_FILE_SIZE:
                log.warning(
                    "file rejected (size=%r, cap=%d)",
                    file_size,
                    self._MAX_FILE_SIZE,
                )
                return None

            download_url = (
                f"https://api.telegram.org/file/bot{self.config.telegram.bot_token}/{file_path}"
            )
            resp = await asyncio.to_thread(requests.get, download_url, timeout=30)
            if resp.status_code == 200:
                return resp.text
            return None
        except Exception:
            log.error("file download failed", exc_info=True)
            return None


# Alias for backwards compatibility and test imports
TelegramInput = TelegramInputBot
