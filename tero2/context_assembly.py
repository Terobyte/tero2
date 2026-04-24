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
    if not text:
        return 0
    return max(1, len(text) // 4)


def _check_budget(tokens: int, budget: int, cfg: ContextConfig) -> BudgetState:
    if budget <= 0:
        return BudgetState.HARD_FAIL
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
        # Bug 193: validate that context ratios are in the correct order.
        # Ratios must satisfy: target_ratio < warning_ratio < hard_fail_ratio.
        # Out-of-order ratios produce incorrect budget decisions.
        # Validation is enforced at _parse_config time; warn here if misconfigured.
        import logging as _logging
        _log193 = _logging.getLogger(__name__)
        _ctx = config.context
        if not (_ctx.target_ratio < _ctx.warning_ratio < _ctx.hard_fail_ratio):
            _log193.warning(
                "context ratios out of order: target_ratio=%s warning_ratio=%s "
                "hard_fail_ratio=%s — raise ConfigError at parse time to fix",
                _ctx.target_ratio, _ctx.warning_ratio, _ctx.hard_fail_ratio,
            )
        self._config = config
        self._system_prompts = system_prompts or {}

    def _get_system_prompt(self, role: str) -> str:
        return self._system_prompts.get(role, "")

    def _role_limit(self, role: str) -> int:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        role_cfg = self._config.roles.get(role)
        if role_cfg is None:
            # Bug 266: log warning when falling back to default context window
            _log.warning(
                "role %r not configured — using default context window (%d)",
                role, _DEFAULT_CONTEXT_WINDOW,
            )
        raw = role_cfg.context_window if role_cfg else _DEFAULT_CONTEXT_WINDOW
        if raw <= 0:
            raw = _DEFAULT_CONTEXT_WINDOW
        raw = min(raw, 1_000_000)
        return max(1, int(raw * self._config.context.target_ratio))

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

        user_parts: list[str] = [_section("Task", task_plan)] if task_plan else []
        mandatory_user = "\n\n".join(user_parts)
        mandatory_tokens = estimate_tokens(mandatory_user)

        # A13: check mandatory_user against the budget — the user portion
        # must fit even when we strip system prompts from the count.
        status = _check_budget(mandatory_tokens, budget, cfg)
        if status == BudgetState.HARD_FAIL:
            raise ContextWindowExceededError(mandatory_tokens, budget)

        # Bug L2: also gate on system_prompt independently. A persona that
        # alone blows past the hard-fail threshold would otherwise pass
        # this check (user fits) and then silently emit a HARD_FAIL prompt
        # downstream. Raise early with both token counts attributed.
        system_tokens = estimate_tokens(system_prompt)
        system_status = _check_budget(system_tokens, budget, cfg)
        if system_status == BudgetState.HARD_FAIL:
            raise ContextWindowExceededError(system_tokens + mandatory_tokens, budget)

        if summaries is None:
            summaries = []

        # Build list of optional sections with keep priority.
        # Higher keep_priority = retained longer (dropped last).
        optional: list[tuple[str, str, int]] = []

        for i, summary in enumerate(summaries):
            idx = i + 1
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
        # O(n): accumulate token count incrementally instead of rebuilding
        # the full candidate string on each iteration.
        included_indices: set[int] = set()
        # Bug 265: include system_prompt in running token count from the start
        # so mid-assembly inclusion checks account for the full prompt size.
        running_tokens = estimate_tokens(system_prompt + mandatory_user)
        for idx, (_tag, body, _pri) in sorted(enumerate(optional), key=lambda x: -x[1][2]):
            section_tokens = estimate_tokens("\n\n" + _section(_tag, body))
            candidate_tokens = running_tokens + section_tokens
            c_status = _check_budget(candidate_tokens, budget, cfg)
            if c_status in (BudgetState.OK, BudgetState.WARNING):
                included_indices.add(idx)
                running_tokens = candidate_tokens

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
