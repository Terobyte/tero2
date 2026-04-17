"""Tests for context_assembly: prompt validity, budget states, truncation."""

import pytest

from tero2.config import Config, ContextConfig, RoleConfig
from tero2.context_assembly import (
    AssembledPrompt,
    BudgetState,
    ContextAssembler,
    estimate_tokens,
)
from tero2.errors import ContextWindowExceededError


def _make_config(
    *,
    context_window: int = 128_000,
    target_ratio: float = 0.70,
    warning_ratio: float = 0.80,
    hard_fail_ratio: float = 0.95,
    roles: dict[str, RoleConfig] | None = None,
) -> Config:
    cfg = Config()
    cfg.context = ContextConfig(
        target_ratio=target_ratio,
        warning_ratio=warning_ratio,
        hard_fail_ratio=hard_fail_ratio,
    )
    if roles:
        cfg.roles = roles
    else:
        cfg.roles = {
            "scout": RoleConfig(provider="openai", context_window=context_window),
            "architect": RoleConfig(provider="openai", context_window=context_window),
            "builder": RoleConfig(provider="openai", context_window=context_window),
            "verifier": RoleConfig(provider="openai", context_window=context_window),
            "coach": RoleConfig(provider="openai", context_window=context_window),
            "reviewer": RoleConfig(provider="openai", context_window=context_window),
        }
    return cfg


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_approximate_ratio(self):
        assert estimate_tokens("a" * 400) == 100


class TestAssembledPromptShape:
    def test_fields(self):
        ap = AssembledPrompt(
            system_prompt="sys",
            user_prompt="usr",
            budget_state=BudgetState.OK,
            estimated_tokens=10,
        )
        assert ap.system_prompt == "sys"
        assert ap.user_prompt == "usr"
        assert ap.budget_state == BudgetState.OK
        assert ap.estimated_tokens == 10


class TestBudgetStateTransitions:
    def test_ok_when_small(self):
        cfg = _make_config(context_window=1000, target_ratio=0.70)
        asm = ContextAssembler(cfg, system_prompts={"builder": "sys"})
        result = asm.assemble("builder", "sys", "small task")
        assert result.budget_state == BudgetState.OK

    def test_warning_above_target(self):
        cfg = _make_config(
            context_window=100, target_ratio=0.01, warning_ratio=0.02, hard_fail_ratio=0.99
        )
        asm = ContextAssembler(cfg)
        result = asm.assemble("builder", "s", "t" * 100)
        assert result.budget_state in (
            BudgetState.WARNING,
            BudgetState.COMPRESS,
            BudgetState.HARD_FAIL,
        )

    def test_hard_fail_raises(self):
        cfg = _make_config(context_window=50, hard_fail_ratio=0.01)
        asm = ContextAssembler(cfg)
        with pytest.raises(ContextWindowExceededError):
            asm.assemble("builder", "s" * 100, "t" * 100)

    def test_exact_hard_fail_ratio_triggers_hard_fail(self):
        cfg = _make_config(
            context_window=1000, target_ratio=0.70, warning_ratio=0.80, hard_fail_ratio=0.50
        )
        asm = ContextAssembler(cfg)
        text_500_tokens = "x" * 2000
        with pytest.raises(ContextWindowExceededError):
            asm.assemble("builder", text_500_tokens, "y")


class TestRoleAwareBudgeting:
    def test_budget_comes_from_role_context_window(self):
        cfg = Config()
        cfg.roles = {
            "scout": RoleConfig(provider="openai", context_window=1000),
            "builder": RoleConfig(provider="openai", context_window=50_000),
        }
        cfg.context = ContextConfig(target_ratio=0.70)
        asm = ContextAssembler(cfg, system_prompts={"scout": "sys", "builder": "sys"})

        small = asm.assemble("scout", "sys", "task")
        big = asm.assemble("builder", "sys", "task")

        assert small.estimated_tokens == big.estimated_tokens
        assert small.budget_state in (BudgetState.OK, BudgetState.WARNING)

    def test_missing_role_uses_default(self):
        cfg = Config()
        cfg.roles = {}
        asm = ContextAssembler(cfg)
        result = asm.assemble("unknown_role", "sys", "task")
        assert result.budget_state == BudgetState.OK


class TestTruncation:
    def test_summaries_truncated_when_over_budget(self):
        cfg = _make_config(
            context_window=200, target_ratio=0.10, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        big_summary = "x" * 3000
        result = asm.assemble("builder", "sys", "task", summaries=[big_summary, big_summary])
        assert result.budget_state in (BudgetState.WARNING, BudgetState.COMPRESS, BudgetState.OK)

    def test_summaries_dropped_before_context_hints(self):
        cfg = _make_config(
            context_window=500, target_ratio=0.10, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        big_summary = "s" * 2000
        result = asm.assemble(
            "builder",
            "sys",
            "task",
            summaries=[big_summary],
            context_hints="h" * 800,
        )
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "Summary" not in result.user_prompt

    def test_context_map_supported(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg)
        result = asm.assemble("builder", "sys", "task", context_map="project overview data")
        assert "CONTEXT_MAP" in result.user_prompt
        assert "project overview data" in result.user_prompt

    def test_code_snippets_supported(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg)
        result = asm.assemble("builder", "sys", "task", code_snippets="def foo(): pass")
        assert "Code Snippets" in result.user_prompt
        assert "def foo(): pass" in result.user_prompt

    def test_truncation_priority_order(self):
        cfg = _make_config(
            context_window=200, target_ratio=0.05, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        medium = "m" * 100
        result = asm.assemble(
            "builder",
            "sys",
            "task",
            summaries=[medium],
            context_map=medium,
            code_snippets=medium,
            context_hints=medium,
        )
        assert "## Task" in result.user_prompt
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "Code Snippets" in result.user_prompt
        assert "CONTEXT_MAP" in result.user_prompt
        assert "Summary" not in result.user_prompt

    def test_regression_mutually_exclusive_priority(self):
        cfg = _make_config(
            context_window=200, target_ratio=0.05, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        section = "x" * 300
        result = asm.assemble(
            "builder",
            "sys",
            "task",
            context_map=section,
            context_hints=section,
        )
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "CONTEXT_MAP" not in result.user_prompt

    def test_regression_summary_fits_hints_fit_both_exceed(self):
        cfg = _make_config(
            context_window=200, target_ratio=0.05, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        big_summary = "s" * 400
        hints = "h" * 300
        result = asm.assemble(
            "builder",
            "sys",
            "task",
            summaries=[big_summary],
            context_hints=hints,
        )
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "Summary" not in result.user_prompt

    def test_higher_priority_evicts_lower_priority(self):
        cfg = _make_config(
            context_window=200, target_ratio=0.05, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        section = "x" * 300
        result = asm.assemble(
            "builder",
            "sys",
            "task",
            context_map=section,
            context_hints=section,
        )
        assert "## Task" in result.user_prompt
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "CONTEXT_MAP" not in result.user_prompt

    def test_context_hints_kept_over_code_snippets_and_map(self):
        cfg = _make_config(
            context_window=200, target_ratio=0.05, warning_ratio=0.50, hard_fail_ratio=0.95
        )
        asm = ContextAssembler(cfg)
        section = "y" * 300
        result = asm.assemble(
            "builder",
            "sys",
            "task",
            context_map=section,
            code_snippets=section,
            context_hints=section,
        )
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "CONTEXT_MAP" not in result.user_prompt
        assert "Code Snippets" not in result.user_prompt


class TestRoleMethods:
    def test_assemble_scout(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"scout": "scout sys"})
        result = asm.assemble_scout()
        assert result.system_prompt == "scout sys"
        assert isinstance(result, AssembledPrompt)

    def test_assemble_architect(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"architect": "arch sys"})
        result = asm.assemble_architect("S01")
        assert result.system_prompt == "arch sys"
        assert "S01" in result.user_prompt

    def test_assemble_builder(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"builder": "builder sys"})
        result = asm.assemble_builder(
            "implement X", reflexion_prompt="refl", context_hints="hints"
        )
        assert result.system_prompt == "builder sys"
        assert "implement X" in result.user_prompt
        assert "CONTEXT_HINTS" in result.user_prompt

    def test_assemble_verifier(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"verifier": "ver sys"})
        result = asm.assemble_verifier("verify X")
        assert result.system_prompt == "ver sys"
        assert "verify X" in result.user_prompt

    def test_assemble_coach(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"coach": "coach sys"})
        result = asm.assemble_coach()
        assert result.system_prompt == "coach sys"
        assert isinstance(result, AssembledPrompt)

    def test_assemble_reviewer_review_mode(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"reviewer_review": "rev sys"})
        result = asm.assemble_reviewer("plan content", mode="review")
        assert result.system_prompt == "rev sys"
        assert "plan content" in result.user_prompt

    def test_assemble_reviewer_fix_mode(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg, system_prompts={"reviewer_fix": "fix sys"})
        result = asm.assemble_reviewer("plan content", mode="fix")
        assert result.system_prompt == "fix sys"
        assert "plan content" in result.user_prompt


class TestPromptValidity:
    def test_system_and_user_separated(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg)
        result = asm.assemble("builder", "SYSTEM", "TASK")
        assert result.system_prompt == "SYSTEM"
        assert "SYSTEM" not in result.user_prompt
        assert "TASK" in result.user_prompt

    def test_estimated_tokens_reasonable(self):
        cfg = _make_config()
        asm = ContextAssembler(cfg)
        result = asm.assemble("builder", "sys prompt", "task plan")
        combined = result.system_prompt + result.user_prompt
        assert result.estimated_tokens == estimate_tokens(combined)
