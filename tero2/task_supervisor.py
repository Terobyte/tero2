"""TaskSupervisor — tracks supervised asyncio tasks for cleanup and error visibility.

Replaces raw ``asyncio.create_task()`` calls at sites where the task is
long-lived or must be cancellable at shutdown.  Three recurring bug classes
in the tero2 history are prevented by using this primitive:

1. **Orphan tasks** — fire-and-forget ``asyncio.create_task(x)`` stored in a
   local variable.  When the outer scope exits, the reference is lost: the
   task continues but is invisible and uncancellable.  (bugs 123, 128, 151)

2. **Silent crashes** — a raised exception in a detached task is swallowed
   by the asyncio loop with at most a noisy log at process exit.  Callers
   never know the background work stopped.  (bug 123)

3. **Non-clean shutdown** — on stop(), pending background work keeps running
   because no one holds the references to cancel.  (bug 151)

Usage::

    class MyService:
        def __init__(self) -> None:
            self.tasks = TaskSupervisor("my_service")

        def launch_worker(self) -> None:
            self.tasks.spawn(self._do_work(), name="worker")

        async def stop(self) -> None:
            await self.tasks.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

log = logging.getLogger(__name__)


class TaskSupervisor:
    """Tracks asyncio tasks so they can be cancelled and their errors logged.

    Each supervisor owns a set of live tasks.  ``spawn(coro)`` starts a task
    and registers it; when the task finishes the supervisor removes it and
    logs any uncaught exception.  ``shutdown()`` cancels all live tasks and
    awaits their completion with a timeout.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        """Start *coro* as a supervised task and return it.

        The task is added to the supervisor's live set and a done-callback is
        attached that removes it on completion and logs any uncaught
        exception.  The name is prefixed with the supervisor's name so
        logs can be traced to the owner.
        """
        task = asyncio.create_task(coro, name=f"{self._name}:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning(
                "supervised task %s raised %s: %s",
                task.get_name(),
                type(exc).__name__,
                exc,
            )

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel all live tasks and await their completion up to *timeout*.

        Safe to call repeatedly; no-op when the set is already empty.
        """
        if not self._tasks:
            return
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "supervisor %s: some tasks did not exit within %.1fs",
                self._name,
                timeout,
            )

    def __len__(self) -> int:
        return len(self._tasks)
