"""Shell provider — runs commands via subprocess."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

from tero2.errors import ProviderError
from tero2.providers.base import BaseProvider

log = logging.getLogger(__name__)


class ShellProvider(BaseProvider):
    _kind = "shell"

    @property
    def display_name(self) -> str:
        return "shell"

    @property
    def command(self) -> str:
        return "bash"

    async def run(self, **kwargs: Any) -> Any:
        prompt = kwargs.get("prompt", "")
        # Split into tokens so shell metacharacters (;, &&, $(), |) are never
        # interpreted - each token is passed directly to execvp, not to a shell.
        args = shlex.split(prompt) if prompt.strip() else ["bash"]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await proc.communicate()
        except Exception:
            proc.terminate()
            await proc.wait()
            # proc.stdout / proc.stderr are asyncio.StreamReader objects which
            # have no .close() method — the event loop closes their underlying
            # _PipeReadTransport automatically once the process has exited.
            raise
        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            log.error("shell provider exited %d: %s", proc.returncode, err_msg)
            raise ProviderError(f"shell exited {proc.returncode}: {err_msg}")
        yield stdout.decode(errors="replace")
