"""Builder player -- executes a single Task.

Runs after Architect decomposition. Writes code + T0X-SUMMARY.md.
On failure, reflexion context is injected for retry.

When a ``RunnerContext`` (``ctx``) is provided, the player delegates to
``ctx.run_agent(chain, prompt)`` — which runs the full agentic streaming
loop with stuck detection, step tracking, and heartbeat.  When ``ctx``
is absent (e.g. in unit tests) it falls back to
``chain.run_prompt_collected(prompt)``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tero2.disk_layer import DiskLayer
from tero2.players.base import BasePlayer, PlayerResult
from tero2.providers.chain import ProviderChain

log = logging.getLogger(__name__)


@dataclass
class BuilderResult(PlayerResult):
    """Result from Builder execution."""

    summary: str = ""
    task_id: str = ""


class BuilderPlayer(BasePlayer):
    """Execute a single Task and produce a summary.

    Accepts an optional ``ctx`` kwarg (a ``RunnerContext`` instance).
    When present, execution is delegated to ``ctx.run_agent(chain, prompt)``
    which provides full agentic execution semantics.  Without ``ctx`` the
    player falls back to a simple ``_run_prompt`` call (used in tests and
    standalone invocations).
    """

    role = "builder"

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        super().__init__(chain, disk, working_dir=working_dir)

    async def run(self, **kwargs: Any) -> BuilderResult:
        task_plan: str = kwargs.get("task_plan", "")
        persona_prompt: str = kwargs.get("persona_prompt", "")
        reflexion_context: str = kwargs.get("reflexion_context", "")
        context_hints: str = kwargs.get("context_hints", "")
        task_id: str = kwargs.get("task_id", "T01")
        slice_id: str = kwargs.get("slice_id", "S01")
        milestone_path: str = kwargs.get("milestone_path", "milestones/M001")
        ctx: Any = kwargs.get("ctx", None)  # optional RunnerContext

        prompt = self._build_prompt(persona_prompt, task_plan, reflexion_context, context_hints)
        try:
            if ctx is not None and hasattr(ctx, "run_agent"):
                # Full agentic execution: stuck detection, step tracking, heartbeat.
                # run_agent returns (success: bool, captured_output: str).
                success, output = await ctx.run_agent(self.chain, prompt)
                if not success:
                    log.error("builder ctx.run_agent did not succeed for %s", task_id)
                    return BuilderResult(
                        success=False,
                        captured_output=output,
                        error="agent run did not succeed",
                        task_id=task_id,
                    )
            else:
                # Fallback: simple single-response collection (tests / standalone).
                output = await self._run_prompt(prompt)

            summary = output.strip()
            output_path = f"{milestone_path}/{slice_id}/{task_id}-SUMMARY.md"
            self.disk.write_file(output_path, summary)
            return BuilderResult(
                success=True,
                output_file=output_path,
                captured_output=output,
                summary=summary,
                task_id=task_id,
            )
        except Exception as exc:
            log.error("builder failed for %s: %s", task_id, exc)
            return BuilderResult(
                success=False,
                error=str(exc),
                task_id=task_id,
            )

    @staticmethod
    def _build_prompt(
        persona_prompt: str,
        task_plan: str,
        reflexion_context: str,
        context_hints: str,
    ) -> str:
        parts: list[str] = []
        if persona_prompt:
            parts.append(persona_prompt)
            parts.append("---")
        if reflexion_context:
            parts.append(reflexion_context)
        if context_hints:
            parts.append(f"## Context Hints\n{context_hints}")
        parts.append(f"## Task\n{task_plan}")
        return "\n\n".join(parts)
