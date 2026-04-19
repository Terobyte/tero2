"""Reviewer player -- plan hardening via convergence loop.

Wraps two LLM calls (review-pass and fix-pass) used by
:func:`~tero2.phases.harden_phase.run_harden`.

``mode="review"`` finds issues in the current plan.
``mode="fix"``   applies the findings to produce a corrected plan.
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
class ReviewerResult(PlayerResult):
    """Result from a single Reviewer pass."""

    verdict: str = ""
    fixed_plan: str = ""


class ReviewerPlayer(BasePlayer):
    """Run one review-or-fix pass of the Plan Hardening loop.

    Accepts two modes via the ``mode`` kwarg:

    * ``"review"`` — evaluate the plan and return a verdict string.
    * ``"fix"``    — rewrite the plan based on ``review_findings``.

    The convergence loop lives in :func:`~tero2.phases.harden_phase.run_harden`
    (not here); this player only executes a single pass.
    """

    role = "reviewer"

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        super().__init__(chain, disk, working_dir=working_dir)

    async def run(self, **kwargs: Any) -> ReviewerResult:
        mode: str = kwargs.get("mode", "review")
        prompt: str = kwargs.get("prompt", "")
        review_findings: str = kwargs.get("review_findings", "")

        if mode == "fix" and review_findings:
            prompt = prompt + f"\n\n## Reviewer Findings\n{review_findings}"

        try:
            output = await self._run_prompt(prompt)
            if mode == "review":
                return ReviewerResult(
                    success=True,
                    captured_output=output,
                    verdict=output,
                )
            else:
                return ReviewerResult(
                    success=True,
                    captured_output=output,
                    fixed_plan=output.strip(),
                )
        except Exception as exc:
            log.error("reviewer failed (mode=%s): %s", mode, exc)
            return ReviewerResult(success=False, error=str(exc))
