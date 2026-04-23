"""Subprocess cleanup helpers that are safe on the cancellation path.

Cancellation-safety matters because ``asyncio.CancelledError`` inherits from
``BaseException`` (not ``Exception``) since Python 3.8.  An ``except
Exception:`` block never runs on cancellation, so subprocess cleanup that
lives in such a block silently leaks the child process and its FDs when a
caller's task is cancelled mid-operation.  (bug 145)

Call :func:`kill_and_wait` from a ``finally`` or ``except BaseException:``
block that owns the subprocess to guarantee cleanup regardless of how the
wait exited.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


async def kill_and_wait(
    proc: asyncio.subprocess.Process,
    *,
    term_timeout: float = 0.2,
) -> None:
    """Best-effort terminate/kill + wait for *proc*.

    Sends ``SIGTERM`` first, then escalates to ``SIGKILL`` if the child does
    not exit within *term_timeout* seconds.  After the process has exited the
    stdio streams are closed to release file descriptors immediately rather
    than waiting for garbage collection.

    Callable from any exception path, including ``CancelledError``.  Never
    raises back to the caller.
    """
    if proc.returncode is not None:
        _close_streams(proc)
        return

    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    except Exception:
        log.debug("kill_and_wait: terminate raised", exc_info=True)

    try:
        await asyncio.wait_for(proc.wait(), timeout=term_timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            log.debug("kill_and_wait: kill raised", exc_info=True)
        try:
            await proc.wait()
        except Exception:
            log.debug("kill_and_wait: wait after kill raised", exc_info=True)
    except Exception:
        log.debug("kill_and_wait: wait raised", exc_info=True)

    _close_streams(proc)


def _close_streams(proc: asyncio.subprocess.Process) -> None:
    """Close stdout/stderr/stdin streams to release FDs promptly.

    Uses getattr so duck-typed Process stand-ins (mocks in tests, minimal
    wrappers) without stream attributes do not raise AttributeError.
    """
    for name in ("stdout", "stderr", "stdin"):
        stream = getattr(proc, name, None)
        if stream is None:
            continue
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
