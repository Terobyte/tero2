"""Scout player -- fast codebase reconnaissance.

Runs before each Slice. Cheap model, fast, read-only.
Produces CONTEXT_MAP.md -- a compressed map of the codebase.

Input:  persistent/PROJECT.md (optional) + file tree listing
Output: milestones/M001/CONTEXT_MAP.md
Failure: non-fatal -- Architect works without map (reduced quality).
Skip:   if project has fewer than ``skip_scout_if_files_lt`` files.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from tero2.constants import PROJECT_SCAN_SKIP_DIRS as _SKIP_DIRS
from tero2.disk_layer import DiskLayer
from tero2.players.base import BasePlayer, PlayerResult
from tero2.providers.chain import ProviderChain

log = logging.getLogger(__name__)


@dataclass
class ScoutResult(PlayerResult):
    """Result from Scout reconnaissance."""

    context_map: str = ""
    file_count: int = 0


class ScoutPlayer(BasePlayer):
    """Map the codebase into a compressed CONTEXT_MAP.md.

    Reads ``persistent/PROJECT.md`` (if present) and a live file tree
    listing from *working_dir*, injects both into the LLM prompt, then
    writes the model output to ``{milestone_path}/CONTEXT_MAP.md``.

    Failure is **non-fatal**: any exception causes the player to return
    ``ScoutResult(success=False)`` rather than propagating.

    Use :meth:`should_skip` to check whether the project is too small to
    warrant Scout (threshold comes from ``Config.context.skip_scout_if_files_lt``).
    """

    role = "scout"

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        super().__init__(chain, disk, working_dir=working_dir)

    async def run(self, **kwargs: Any) -> ScoutResult:
        milestone_path: str = kwargs.get("milestone_path", "milestones/M001")
        persona_prompt: str = kwargs.get("persona_prompt", "")

        try:
            project_md = self._read_project_md()
            file_tree = _build_file_tree(self.working_dir or ".")
            content_prompt = self._build_prompt(project_md, file_tree)
            prompt = f"{persona_prompt}\n\n---\n\n{content_prompt}" if persona_prompt else content_prompt
            output = await self._run_prompt(prompt)
            context_map = output.strip()
            output_path = f"{milestone_path}/CONTEXT_MAP.md"
            self.disk.write_file(output_path, context_map)
            file_count = _count_files(self.working_dir or ".")
            return ScoutResult(
                success=True,
                output_file=output_path,
                captured_output=context_map,
                context_map=context_map,
                file_count=file_count,
            )
        except Exception as exc:
            log.error("scout failed: %s", exc)
            return ScoutResult(success=False, error=str(exc))

    def _read_project_md(self) -> str:
        """Read persistent/PROJECT.md from the .sora directory.

        Returns an empty string (with a logged warning) if the file is absent.
        """
        content = self.disk.read_file("persistent/PROJECT.md")
        if not content:
            log.warning(
                "persistent/PROJECT.md not found — Scout will use file tree only"
            )
        return content

    @staticmethod
    def should_skip(working_dir: str, skip_threshold: int) -> bool:
        """Return True if the project is too small to warrant Scout.

        *skip_threshold* should come from
        ``Config.context.skip_scout_if_files_lt`` (default 20).
        """
        return _count_files(working_dir) < skip_threshold

    @staticmethod
    def _build_prompt(project_md: str = "", file_tree: str = "") -> str:
        """Build the LLM prompt for Scout.

        Args:
            project_md: Content of ``persistent/PROJECT.md`` (may be empty).
            file_tree:  Formatted directory listing of the working directory.

        Returns:
            A prompt string that instructs the model to produce CONTEXT_MAP.md.
        """
        parts: list[str] = []
        if project_md:
            parts.append(f"## PROJECT.md\n{project_md}")
        if file_tree:
            parts.append(f"## File Tree\n```\n{file_tree}\n```")
        parts.append(
            "Map this codebase. Write CONTEXT_MAP.md following the format "
            "in your instructions. Include: directory structure (1-2 levels), "
            "entry points, key modules, dependencies, and recent git history."
        )
        return "\n\n".join(parts)

    # Keep the old name for backward compatibility with tests / callers.
    @staticmethod
    def _default_prompt() -> str:
        return ScoutPlayer._build_prompt(project_md="", file_tree="")


def _build_file_tree(working_dir: str, max_depth: int = 2) -> str:
    """Return a compact directory listing (max *max_depth* levels).

    Hidden directories and common noise dirs (``_SKIP_DIRS``) are excluded.
    Files are listed with a leading ``├─`` / ``└─`` prefix for readability.
    """
    lines: list[str] = []
    root_path = os.path.abspath(working_dir)

    def _walk(path: str, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return
        # Filter out hidden and skip-listed entries.
        entries = [
            e
            for e in entries
            if not e.startswith(".") and e not in _SKIP_DIRS
        ]
        for i, entry in enumerate(entries):
            connector = "└─" if i == len(entries) - 1 else "├─"
            full = os.path.join(path, entry)
            lines.append(f"{prefix}{connector} {entry}")
            if os.path.isdir(full):
                extension = "   " if i == len(entries) - 1 else "│  "
                _walk(full, prefix + extension, depth + 1)

    lines.append(os.path.basename(root_path) or working_dir)
    _walk(root_path, "", 1)
    return "\n".join(lines)


def _count_files(working_dir: str) -> int:
    count = 0
    for _root, _dirs, files in os.walk(working_dir):
        _dirs[:] = [d for d in _dirs if d not in _SKIP_DIRS]
        count += len(files)
    return count
