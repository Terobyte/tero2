"""Context Assembly -- smart prompt builder with role-aware budget control.

Each agent starts with a clean context (Iron Rule).  This module assembles
the pre-inlined prompt: system prompt + plan + summaries + hints,
respecting the per-role context window budget derived from config.

Token budget per role:
    config.roles[role].context_window * config.context.target_ratio

Key insight (Sweep, 7.7K*): quality peaks at 10-15K tokens of context,
NOT at maximum window.  More context = worse results.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tero2.config import Config, ContextConfig
from tero2.errors import ConfigError, ContextWindowExceededError

__all__ = [
    "BudgetState",
    "AssembledPrompt",
    "ContextAssembler",
    "estimate_tokens",
    "ContextWindowExceededError",
]

_DEFAULT_CONTEXT_WINDOW = 128_000


class BudgetState(str, Enum):
    OK = "ok"
    WARNING = "warning"
    COMPRESS = "compress"
    HARD_FAIL = "hard_fail"


@dataclass
class AssembledPrompt:
    system_prompt: str
    user_prompt: str
    budget_state: BudgetState
    estimated_tokens: int


def estimate_tokens(text: str) -> int:
    return len(text) // 4


def _check_budget(tokens: int, budget: int, cfg: ContextConfig) -> BudgetState:
    if budget <= 0:
        raise ConfigError("context budget must be positive")
    if cfg.target_ratio <= 0:
        raise ConfigError("context target_ratio must be positive")
    ratio = tokens / budget
    hard_fail_threshold = cfg.hard_fail_ratio / cfg.target_ratio
    compress_threshold = cfg.warning_ratio / cfg.target_ratio
    if ratio >= hard_fail_threshold:
        return BudgetState.HARD_FAIL
    if ratio >= compress_threshold:
        return BudgetState.COMPRESS
    if ratio >= 1.0:
        return BudgetState.WARNING
    return BudgetState.OK


def _section(tag: str, body: str) -> str:
    return f"## {tag}\n{body}"


class ContextAssembler:
    """Assemble prompts for agent roles with per-role token budgets.

    System prompts are resolved internally via an optional override map or
    sensible defaults.  Callers only pass role-specific data.

    Truncation priority (first dropped first):
        old summaries -> CONTEXT_MAP -> code snippets -> CONTEXT_HINTS
        -> (task plan: never, system prompt: never)
    """

    def __init__(
        self,
        config: Config,
        system_prompts: dict[str, str] | None = None,
    ) -> None:
        self._config = config
        self._system_prompts = system_prompts or {}

    def _get_system_prompt(self, role: str) -> str:
        return self._system_prompts.get(role, "")

    def _role_limit(self, role: str) -> int:
        role_cfg = self._config.roles.get(role)
        raw = role_cfg.context_window if role_cfg else _DEFAULT_CONTEXT_WINDOW
        return int(raw * self._config.context.target_ratio)

    def assemble(
        self,
        role: str,
        system_prompt: str,
        task_plan: str,
        summaries: list[str] | None = None,
        context_map: str = "",
        code_snippets: str = "",
        context_hints: str = "",
    ) -> AssembledPrompt:
        cfg = self._config.context
        budget = self._role_limit(role)

        user_parts: list[str] = [_section("Task", task_plan)]
        mandatory_user = "\n\n".join(user_parts)
        mandatory_tokens = estimate_tokens(system_prompt + mandatory_user)

        status = _check_budget(mandatory_tokens, budget, cfg)
        if status == BudgetState.HARD_FAIL:
            raise ContextWindowExceededError(mandatory_tokens, budget)

        if summaries is None:
            summaries = []

        # Build list of optional sections with keep priority.
        # Higher keep_priority = retained longer (dropped last).
        optional: list[tuple[str, str, int]] = []

        for i, summary in enumerate(reversed(summaries)):
            idx = len(summaries) - i
            optional.append((f"Summary ({idx}/{len(summaries)})", summary, 0))

        if context_map:
            optional.append(("CONTEXT_MAP", context_map, 1))

        if code_snippets:
            optional.append(("Code Snippets", code_snippets, 2))

        if context_hints:
            optional.append(("CONTEXT_HINTS", context_hints, 3))

        # Determine which sections to include: process highest keep priority
        # first so higher-priority sections get first claim on the budget
        # and can evict lower-priority sections.
        included_indices: set[int] = set()
        for idx, (_tag, _body, _pri) in sorted(enumerate(optional), key=lambda x: -x[1][2]):
            parts = [mandatory_user]
            for i, (t, b, _p) in enumerate(optional):
                if i in included_indices or i == idx:
                    parts.append(_section(t, b))
            candidate = "\n\n".join(parts)
            candidate_tokens = estimate_tokens(system_prompt + candidate)
            c_status = _check_budget(candidate_tokens, budget, cfg)
            if c_status in (BudgetState.OK, BudgetState.WARNING):
                included_indices.add(idx)

        # Build final prompt in canonical display order
        current = mandatory_user
        for i, (tag, body, _pri) in enumerate(optional):
            if i in included_indices:
                current += "\n\n" + _section(tag, body)

        total = estimate_tokens(system_prompt + current)
        status = _check_budget(total, budget, cfg)
        return AssembledPrompt(
            system_prompt=system_prompt,
            user_prompt=current,
            budget_state=status,
            estimated_tokens=total,
        )

    # -- Role-specific assembly methods (Task 7 contract) --

    def assemble_scout(self) -> AssembledPrompt:
        return self.assemble("scout", self._get_system_prompt("scout"), "")

    def assemble_architect(self, slice_id: str) -> AssembledPrompt:
        task_plan = f"Create a plan for slice: {slice_id}"
        return self.assemble(
            "architect",
            self._get_system_prompt("architect"),
            task_plan,
        )

    def assemble_builder(
        self,
        task_plan: str,
        reflexion_prompt: str = "",
        context_hints: str = "",
    ) -> AssembledPrompt:
        summaries = [reflexion_prompt] if reflexion_prompt else []
        return self.assemble(
            "builder",
            self._get_system_prompt("builder"),
            task_plan,
            summaries=summaries,
            context_hints=context_hints,
        )

    def assemble_verifier(self, task_plan: str) -> AssembledPrompt:
        return self.assemble(
            "verifier",
            self._get_system_prompt("verifier"),
            task_plan,
        )

    def assemble_coach(self) -> AssembledPrompt:
        return self.assemble("coach", self._get_system_prompt("coach"), "")

    def assemble_reviewer(self, plan: str, mode: str = "review") -> AssembledPrompt:
        role_key = "reviewer_fix" if mode == "fix" else "reviewer_review"
        return self.assemble(
            role_key,
            self._get_system_prompt(role_key),
            plan,
        )
