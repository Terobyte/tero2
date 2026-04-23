"""Coach player -- episodic strategic advisor.

Wakes up at trigger boundaries (end-of-slice, anomaly, budget, stuck),
reads full project state from disk, produces four strategic documents,
and terminates. No context accumulation across invocations.

Reads:  milestones/{M}/ROADMAP.md, milestones/{M}/CONTEXT_MAP.md,
        milestones/{M}/{S}/T0X-SUMMARY.md, persistent/DECISIONS.md,
        persistent/EVENT_JOURNAL.md, reports/metrics.json,
        strategic/CONTEXT_HINTS.md (previous pass), human/STEER.md
Writes: strategic/STRATEGY.md, strategic/TASK_QUEUE.md,
        strategic/RISK.md, strategic/CONTEXT_HINTS.md
        (only sections with non-empty content are written)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from tero2.disk_layer import DiskLayer
from tero2.players.base import BasePlayer, PlayerResult
from tero2.providers.chain import ProviderChain

log = logging.getLogger(__name__)

_SECTIONS = ("STRATEGY", "TASK_QUEUE", "RISK", "CONTEXT_HINTS")
_MAX_TASKS = 7  # mirrors ArchitectPlayer._MAX_TASKS


@dataclass
class CoachResult(PlayerResult):
    """Result from Coach strategic pass."""

    strategy: str = ""
    task_queue: str = ""
    risk: str = ""
    context_hints: str = ""


class CoachPlayer(BasePlayer):
    """Episodic strategic advisor. Reads disk state, writes strategy docs, terminates."""

    role = "coach"

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        super().__init__(chain, disk, working_dir=working_dir)

    async def run(self, **kwargs: Any) -> CoachResult:
        trigger: str = kwargs.get("trigger", "end_of_slice")
        persona_prompt: str = kwargs.get("persona_prompt", "")
        slice_id: str = kwargs.get("slice_id", "S01")
        milestone_path: str = kwargs.get("milestone_path", "milestones/M001")

        context = self._gather_context(milestone_path, slice_id)
        prompt = self._build_prompt(persona_prompt, trigger, context)
        try:
            output = await self._run_prompt(prompt)
            sections = _parse_sections(output)
            strategy = sections.get("STRATEGY", "")
            task_queue = sections.get("TASK_QUEUE", "")
            risk = sections.get("RISK", "")
            context_hints = sections.get("CONTEXT_HINTS", "")

            # Only write sections that have content — guards against overwriting
            # existing files with empty strings when the LLM response is malformed.
            if strategy:
                self.disk.write_file("strategic/STRATEGY.md", strategy)
            if task_queue:
                self.disk.write_file("strategic/TASK_QUEUE.md", task_queue)
            if risk:
                self.disk.write_file("strategic/RISK.md", risk)
            if context_hints:
                self.disk.write_file("strategic/CONTEXT_HINTS.md", context_hints)

            # Bug 116: clear STEER.md only when we actually folded it into a
            # strategy document. A successful parse with zero sections means
            # nothing was applied, so the operator's directive must survive
            # for the next attempt.
            wrote_any = any([strategy, task_queue, risk, context_hints])
            if wrote_any and self.disk.read_steer():
                self.disk.clear_steer()

            return CoachResult(
                success=True,
                output_file="strategic/STRATEGY.md",
                captured_output=output,
                strategy=strategy,
                task_queue=task_queue,
                risk=risk,
                context_hints=context_hints,
            )
        except Exception as exc:
            log.error("coach failed (trigger=%s): %s", trigger, exc)
            return CoachResult(success=False, error=str(exc))

    def _gather_context(self, milestone_path: str, slice_id: str) -> dict[str, str]:
        """Read all relevant context from disk for this Coach invocation.

        Reads (per spec):
            milestones/{M}/ROADMAP.md
            milestones/{M}/CONTEXT_MAP.md
            milestones/{M}/{S}/T0X-SUMMARY.md  (up to _MAX_TASKS)
            persistent/DECISIONS.md
            persistent/EVENT_JOURNAL.md
            reports/metrics.json
            strategic/CONTEXT_HINTS.md  (previous pass)
            human/STEER.md              (if exists)
        """
        summaries: list[str] = []
        total_size = 0
        _SIZE_CAP = 50_000

        milestone_abs = self.disk.sora_dir / milestone_path
        slice_dirs = sorted(
            d.name for d in milestone_abs.iterdir()
            if d.is_dir() and re.fullmatch(r"S\d+", d.name)
        ) if milestone_abs.is_dir() else []

        for sid in slice_dirs:
            for i in range(1, _MAX_TASKS + 1):
                tid = f"T{i:02d}"
                content = self.disk.read_file(f"{milestone_path}/{sid}/{tid}-SUMMARY.md")
                if not content:
                    break
                if content:
                    entry = f"### {sid}/{tid}\n{content}"
                    if total_size + len(entry) > _SIZE_CAP:
                        summaries.append("[TRUNCATED — context limit reached]")
                        break
                    summaries.append(entry)
                    total_size += len(entry)

        metrics_raw = self.disk.read_metrics()
        metrics_str = json.dumps(metrics_raw, indent=2) if metrics_raw else ""

        return {
            "roadmap": self.disk.read_file(f"{milestone_path}/ROADMAP.md") or "",
            "context_map": self.disk.read_file(f"{milestone_path}/CONTEXT_MAP.md") or "",
            "task_summaries": "\n\n".join(summaries),
            "decisions": self.disk.read_file("persistent/DECISIONS.md") or "",
            "event_journal": self.disk.read_file("persistent/EVENT_JOURNAL.md") or "",
            "metrics": metrics_str,
            "context_hints": self.disk.read_file("strategic/CONTEXT_HINTS.md") or "",
            "steer": self.disk.read_steer(),
        }

    @staticmethod
    def _build_prompt(
        persona_prompt: str,
        trigger: str,
        context: dict[str, str],
    ) -> str:
        parts: list[str] = []
        if persona_prompt:
            parts.append(persona_prompt)
            parts.append("---")
        parts.append(f"## Trigger\n{trigger}")
        if context.get("roadmap"):
            parts.append(f"## Roadmap\n{context['roadmap']}")
        if context.get("context_map"):
            parts.append(f"## Context Map\n{context['context_map']}")
        if context.get("task_summaries"):
            parts.append(f"## Task Summaries\n{context['task_summaries']}")
        if context.get("decisions"):
            parts.append(f"## Decisions\n{context['decisions']}")
        if context.get("event_journal"):
            parts.append(f"## Event Journal\n{context['event_journal']}")
        if context.get("metrics"):
            parts.append(f"## Metrics\n{context['metrics']}")
        if context.get("context_hints"):
            parts.append(f"## Previous Context Hints\n{context['context_hints']}")
        if context.get("steer"):
            parts.append(f"## Human Steer\n{context['steer']}")
        parts.append(
            "Produce your strategic output in four sections:\n"
            "## STRATEGY\n## TASK_QUEUE\n## RISK\n## CONTEXT_HINTS"
        )
        return "\n\n".join(parts)


def _parse_sections(output: str) -> dict[str, str]:
    """Extract the four named output sections from a Coach LLM response."""
    result: dict[str, str] = {}
    pattern = re.compile(
        r"^##\s+(" + "|".join(_SECTIONS) + r")\s*$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(output))
    for i, match in enumerate(matches):
        section_name = match.group(1)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(output)
        chunk = output[start:end].strip()
        if section_name in result:
            result[section_name] = result[section_name] + "\n\n" + chunk
        else:
            result[section_name] = chunk
    return result


# Backward-compatible alias used by tests and external callers.
Coach = CoachPlayer
