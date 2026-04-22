"""Architect player -- decomposes Slice into Tasks.

Reads STRATEGY + CONTEXT_MAP + PLAN.md (fallback: ROADMAP.md).
Produces S0X-PLAN.md with N atomic Tasks, each with must-haves.
Failure is **fatal** for the Slice -- Architect is re-invoked on error.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from tero2.disk_layer import DiskLayer
from tero2.errors import ProviderError, RateLimitError
from tero2.players.base import BasePlayer, PlayerResult
from tero2.providers.chain import ProviderChain

log = logging.getLogger(__name__)

_MAX_TASKS = 7


# ── Result types ─────────────────────────────────────────────────────────


@dataclass
class Task:
    """A single atomic task parsed from S0X-PLAN.md."""

    index: int = 0
    id: str = ""
    description: str = ""
    must_haves: list[str] = field(default_factory=list)


@dataclass
class SlicePlan:
    """Structured representation of a parsed S0X-PLAN.md.

    Produced by :func:`_parse_slice_plan` and stored in the Architect
    result so that phase handlers can drive the Builder/Verifier loop
    without re-parsing the file on disk.
    """

    slice_id: str  # e.g. "S01"
    slice_dir: str  # e.g. "milestones/M001/S01"
    tasks: list[Task] = field(default_factory=list)
    dropped_headers: list[str] = field(default_factory=list)


@dataclass
class ArchitectResult(PlayerResult):
    """Result from Architect decomposition."""

    plan: str = ""
    task_count: int = 0
    slice_plan: SlicePlan | None = None  # None only on failure


# ── Player ────────────────────────────────────────────────────────────────


class ArchitectPlayer(BasePlayer):
    """Decompose a Slice into atomic Tasks.

    Reads (from disk or kwargs):
        - ``strategic/STRATEGY.md``
        - ``{milestone_path}/CONTEXT_MAP.md``
        - ``{milestone_path}/PLAN.md`` → fallback ``{milestone_path}/ROADMAP.md``

    Writes:
        - ``{milestone_path}/{slice_id}/{slice_id}-PLAN.md``

    On failure returns ``ArchitectResult(success=False)`` — callers should
    treat this as fatal for the current Slice.
    """

    role = "architect"

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        super().__init__(chain, disk, working_dir=working_dir)

    async def run(self, **kwargs: Any) -> ArchitectResult:
        slice_id: str = kwargs.get("slice_id", "S01")
        milestone_path: str = kwargs.get("milestone_path", "milestones/M001")
        persona_prompt: str = kwargs.get("persona_prompt", "")

        # Callers may inject content directly (used in tests / phase handlers).
        # If not provided, read from disk with the standard fallback chain.
        roadmap: str = kwargs.get("roadmap", "") or self._read_roadmap(milestone_path)
        strategy: str = kwargs.get("strategy", "") or self.disk.read_file("strategic/STRATEGY.md") or ""
        context_map: str = kwargs.get("context_map", "") or self.disk.read_file(
            f"{milestone_path}/CONTEXT_MAP.md"
        ) or ""

        prompt = self._build_prompt(persona_prompt, roadmap, strategy, context_map)
        try:
            output = await self._run_prompt(prompt)
            plan = output.strip()
            errors = validate_plan(plan)
            if errors:
                for e in errors:
                    log.error("plan validation error: %s", e)
                # Only attempt disk recovery when the plan has zero tasks —
                # that is the only case where an agent CLI may have written
                # the plan to a file instead of returning it as text.
                if _count_tasks(plan) == 0:
                    recovered = self._recover_plan_from_disk(slice_id, milestone_path)
                    if recovered is not None:
                        recovered_path, recovered_content = recovered
                        log.warning("architect: using recovered plan from %s", recovered_path)
                        recovered_errors = validate_plan(recovered_content)
                        if not recovered_errors:
                            plan = recovered_content
                            errors = []
                        else:
                            for e in recovered_errors:
                                log.error("recovered plan also invalid: %s", e)
                if errors:
                    return ArchitectResult(
                        success=False,
                        error="plan validation failed: " + "; ".join(errors),
                        plan=plan,
                    )
            output_path = f"{milestone_path}/{slice_id}/{slice_id}-PLAN.md"
            self.disk.write_file(output_path, plan)
            task_count = _count_tasks(plan)
            slice_plan = _parse_slice_plan(plan, slice_id, milestone_path)
            return ArchitectResult(
                success=True,
                output_file=output_path,
                captured_output=plan,
                plan=plan,
                task_count=task_count,
                slice_plan=slice_plan,
            )
        except (ProviderError, RateLimitError):
            raise
        except Exception as exc:
            log.error("architect failed: %s", exc)
            return ArchitectResult(success=False, error=str(exc))

    def _read_roadmap(self, milestone_path: str) -> str:
        """Read PLAN.md, falling back to ROADMAP.md when absent."""
        plan = self.disk.read_file(f"{milestone_path}/PLAN.md")
        if plan:
            return plan
        return self.disk.read_file(f"{milestone_path}/ROADMAP.md") or ""

    def _recover_plan_from_disk(
        self, slice_id: str, milestone_path: str
    ) -> tuple[str, str] | None:
        """Try to read a plan file that the agent wrote to disk.

        Checks candidate locations in order:
        1. ``{working_dir}/{slice_id}-PLAN.md`` — agent CLI working dir (project root)
        2. ``{milestone_path}/{slice_id}/{slice_id}-PLAN.md`` — .sora milestone dir

        Returns ``(path_str, content)`` for the first non-empty file whose
        contents pass ``validate_plan``, or the first non-empty file if none
        pass validation (caller is responsible for re-validating the result).

        Returns ``None`` when no non-empty candidate exists.
        """
        import pathlib

        candidate_paths: list[pathlib.Path] = []

        if self.working_dir:
            candidate_paths.append(pathlib.Path(self.working_dir) / f"{slice_id}-PLAN.md")

        # .sora milestone location via disk layer
        sora_path = self.disk.sora_dir / milestone_path / slice_id / f"{slice_id}-PLAN.md"
        candidate_paths.append(sora_path)

        found: list[tuple[str, str]] = []
        for path in candidate_paths:
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    log.info("architect: found plan candidate at %s", path)
                    found.append((str(path), content))
            except (OSError, FileNotFoundError):
                continue

        # Prefer the first candidate that passes validation; fall back to the
        # first non-empty candidate so the caller can surface a meaningful error.
        for path_str, content in found:
            if not validate_plan(content):
                return (path_str, content)
        if found:
            return found[0]
        return None

    @staticmethod
    def _build_prompt(
        persona_prompt: str,
        roadmap: str,
        strategy: str,
        context_map: str,
    ) -> str:
        parts: list[str] = []
        if persona_prompt:
            parts.append(persona_prompt)
            parts.append("---")
        if roadmap:
            parts.append(f"## Roadmap\n{roadmap}")
        if strategy:
            parts.append(f"## Strategy\n{strategy}")
        if context_map:
            parts.append(f"## Context Map\n{context_map}")
        parts.append(
            "Decompose the Slice into atomic Tasks. "
            f"Maximum {_MAX_TASKS} Tasks. Each Task must have verifiable must-haves."
        )
        return "\n\n".join(parts)


# ── SlicePlan parser ─────────────────────────────────────────────────────


def _parse_slice_plan(
    content: str,
    slice_id: str,
    milestone_path: str = "milestones/M001",
) -> SlicePlan:
    """Parse S0X-PLAN.md content into a :class:`SlicePlan`.

    Splits on ``^## T\\d+:`` headers.  For each block the parser extracts:

    - **task_id** — the ``T\\d+`` token from the header (e.g. ``"T01"``)
    - **description** — text between the header line and the
      ``**Must-haves:**`` marker (stripped)
    - **must_haves** — bullet-list items (``- ...``) following the
      ``**Must-haves:**`` line

    Args:
        content: Raw markdown text produced by the Architect LLM.
        slice_id: Slice identifier (e.g. ``"S01"``).
        milestone_path: Base directory under ``.sora/`` (used to build
            ``SlicePlan.slice_dir``).

    Returns:
        A :class:`SlicePlan` (possibly with an empty ``tasks`` list when
        the content has no parseable ``## T0X:`` headers).
    """
    slice_dir = f"{milestone_path}/{slice_id}"
    tasks: list[Task] = []
    dropped_headers: list[str] = []

    # Split on ALL level-2 headers so malformed ones (missing T-code) are
    # captured and can be tracked in dropped_headers rather than silently
    # swallowed into the previous task's body.
    split_re = re.compile(r"^(## [^\n]+)\n", re.MULTILINE)
    parts = split_re.split(content)
    # parts layout: [preamble, header1, body1, header2, body2, ...]
    # pairs start at index 1
    for idx in range(1, len(parts) - 1, 2):
        header = parts[idx]  # e.g. "## T01: Setup module"
        body = parts[idx + 1] if idx + 1 < len(parts) else ""

        tid_match = _TASK_ID_RE.search(header)
        if not tid_match:
            log.warning("_parse_slice_plan: dropping header with no task ID: %r", header)
            dropped_headers.append(header)
            continue
        task_id = tid_match.group(0)

        # Split body at **Must-haves:** marker (case-insensitive, bold optional).
        must_have_split = re.split(r"\*?\*?[Mm]ust.{0,3}[Hh]aves?\*?\*?:?", body, maxsplit=1, flags=re.IGNORECASE)
        description = must_have_split[0].strip()
        must_haves: list[str] = []
        if len(must_have_split) > 1:
            for line in must_have_split[1].splitlines():
                line = line.strip()
                if line.startswith("-"):
                    must_haves.append(line.lstrip("- ").strip())

        task = Task(id=task_id, description=description, must_haves=must_haves)
        tasks.append(task)
        tasks[-1].index = len(tasks) - 1

    return SlicePlan(slice_id=slice_id, slice_dir=slice_dir, tasks=tasks, dropped_headers=dropped_headers)


# ── Plan validator ───────────────────────────────────────────────────────


def validate_plan(plan: str) -> list[str]:
    """Validate the Architect's plan.

    Checks:
        - Each Task has must-haves
        - Task count <= 7 (Architect rule)
        - Dependencies reference valid task IDs

    Returns list of validation errors (empty = valid).
    """
    errors: list[str] = []
    task_count = _count_tasks(plan)
    if task_count == 0:
        errors.append("plan contains no tasks")
    elif task_count > _MAX_TASKS:
        errors.append(f"plan has {task_count} tasks (max {_MAX_TASKS})")
    task_ids = _extract_task_ids(plan)
    for dep_match in re.finditer(r"[Dd]epend(?:s|enc(?:y|ies)).*?:?\s*(.+)", plan):
        for ref in re.findall(r"T\d{2}", dep_match.group(1)):
            if ref not in task_ids:
                errors.append(f"dependency references unknown task {ref}")
    parts = _TASK_SPLIT_RE.split(plan)
    # parts: [preamble, header1, body1, header2, body2, ...]
    for idx in range(1, len(parts) - 1, 2):
        header = parts[idx]
        body = parts[idx + 1] if idx + 1 < len(parts) else ""
        tid = _TASK_ID_RE.search(header)
        tid_str = tid.group(0) if tid else f"#{(idx + 1) // 2}"
        if not _MUST_HAVE_RE.search(body):
            errors.append(f"task {tid_str} is missing must-haves")
        must_have_split = re.split(r"\*?\*?[Mm]ust.{0,3}[Hh]aves?\*?\*?:?", body, maxsplit=1, flags=re.IGNORECASE)
        description = must_have_split[0].strip()
        if not description:
            errors.append(f"task {tid_str} has empty description")
    return errors


# ── Internal helpers ─────────────────────────────────────────────────────

# Task header regexes must accept both the strict form ``## T01: ...`` and the
# natural LLM-produced form ``## Task T01: ...`` (optional words between the
# heading markers and the task ID). The lazy ``[^\n]*?`` bounds the prefix
# to a single line and keeps the anchoring on ``T\d{2}[:\s]`` unambiguous.
_TASK_RE = re.compile(r"^##\s+[^\n]*?T\d{2}[:\s]", re.MULTILINE)
_TASK_ID_RE = re.compile(r"T\d{2}")
_MUST_HAVE_RE = re.compile(r"must.{0,3}have", re.IGNORECASE)
_TASK_SPLIT_RE = re.compile(r"^(##\s+[^\n]*?T\d{2}[:\s][^\n]*)", re.MULTILINE)


def _count_tasks(plan: str) -> int:
    return len(_TASK_RE.findall(plan))


def _extract_task_ids(plan: str) -> set[str]:
    ids: set[str] = set()
    for m in _TASK_RE.finditer(plan):
        tid = _TASK_ID_RE.search(m.group(0))
        if tid:
            ids.add(tid.group(0))
    return ids


# Public alias used by tests and external callers.
parse_plan = _parse_slice_plan
