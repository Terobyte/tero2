"""Autonomous bug-loop iteration 6.

Real bugs in still-unexplored code paths:

1. HardenParseVerdictCriticalShadowsNoIssues
   Where:  tero2/phases/harden_phase.py, ``_parse_verdict``.
   What:   ``_parse_verdict`` gives unconditional priority to the word
           ``CRITICAL``. Whenever a Reviewer output contains both
           ``NO ISSUES FOUND`` and the word ``CRITICAL`` (e.g. as part of a
           natural-language justification such as
           ``"NO ISSUES FOUND. There are no CRITICAL defects."``), the
           function returns ``"critical"`` instead of ``"no_issues"``. The
           harden convergence loop then keeps running another reviewer cycle
           and wastes up to ``max_rounds`` LLM calls even though the model
           is clearly telling us the plan is clean. User-observable impact:
           much slower ``tero2 harden`` runs that burn quota on already-
           converged plans.
   Fix:    Flip the check ordering so ``NO ISSUES FOUND`` is evaluated
           FIRST and short-circuits the ``CRITICAL`` branch, e.g.:
               if _NO_ISSUES_RE.search(output):
                   return "no_issues"
               if _CRITICAL_RE.search(output):
                   return "critical"
               ...

2. CliHardenAcceptsNegativeRoundsAndNukesPlan
   Where:  tero2/cli.py, ``cmd_harden`` (the ``--rounds`` arg plumbing).
   What:   ``cmd_harden`` forwards ``--rounds`` straight into
           ``config.plan_hardening.max_rounds`` with no validation:
               if args.rounds is not None:
                   config.plan_hardening.max_rounds = args.rounds
           A negative value (e.g. ``--rounds -1``) is accepted, the harden
           loop's ``range(1, max_rounds + 1)`` collapses to an empty range,
           ``run_harden`` skips ALL reviewer passes, and the final
           ``ctx.disk.write_file("{milestone}/PLAN.md", current_plan)`` just
           writes the input plan unchanged as the "hardened" artifact. User-
           observable impact: a typo like ``--rounds -1`` silently does
           nothing and the command still reports success, so the user
           believes the plan is hardened when it is not.
   Fix:    Reject negative / non-positive ``--rounds`` at the CLI boundary,
           e.g. ``if args.rounds is not None and args.rounds < 1: error``.
           (Validating once here is simpler than hardening every
            downstream consumer of max_rounds.)

3. ProjectInitSanitizeNameSwallowsPunctuationOnly
   Where:  tero2/project_init.py, ``init_project`` + ``_sanitize_name``.
   What:   ``_sanitize_name`` ends with ``return result or "project"``: any
           name whose ``[^\\w\\s-]``-stripped form is empty (``"@@@"``,
           ``"???"``, ``"..."``) is silently coerced to the literal
           ``"project"``. The guard added for bug 194 in ``init_project``
               if not safe_name:
                   raise ValueError(...)
           therefore NEVER fires, because ``_sanitize_name`` cannot return
           an empty string. Two users who call ``init_project("???", …)``
           and ``init_project("!!!", …)`` both land in
           ``{projects_dir}/project`` — the second call raises
           ``FileExistsError`` from the mkdir, masking the real bug (the
           original name was unrecognisable and should have been rejected
           up-front). User-observable impact: unpredictable directory
           names, collisions between unrelated "nameless" projects, and a
           misleading error message that does not tell the user the
           project name was garbage.
   Fix:    Drop the ``or "project"`` fallback inside ``_sanitize_name``
           (or have it return ``""``) so that ``init_project``'s
           ``if not safe_name: raise ValueError(...)`` guard can do its
           job and reject punctuation-only / empty names with a clear
           error.
"""

from __future__ import annotations

import pytest


# ── Bug 1: harden_phase._parse_verdict prioritizes CRITICAL substring ────


class TestLoopIter6HardenParseVerdictCriticalShadowsNoIssues:
    """_parse_verdict mis-classifies 'NO ISSUES FOUND ... no CRITICAL ...' as critical."""

    def test_no_issues_with_critical_word_in_reasoning_is_no_issues(self):
        from tero2.phases.harden_phase import _parse_verdict

        # Realistic Reviewer output: the model has decided the plan is clean
        # AND explicitly references 'CRITICAL' in its reasoning.
        reviewer_output = (
            "After reviewing the plan, I conclude: NO ISSUES FOUND.\n"
            "There are no CRITICAL defects and no COSMETIC concerns either.\n"
        )
        verdict = _parse_verdict(reviewer_output)
        # BUG: returns 'critical' because _CRITICAL_RE.search runs first.
        # Expected: 'no_issues' — the reviewer is clearly reporting no issues.
        assert verdict == "no_issues", (
            f"_parse_verdict should see NO ISSUES FOUND wins over a mere "
            f"mention of the word CRITICAL in reasoning, got {verdict!r}"
        )

    def test_actual_critical_finding_still_flagged(self):
        """Sanity: real CRITICAL findings (no NO ISSUES FOUND marker) must still trigger fix pass."""
        from tero2.phases.harden_phase import _parse_verdict

        reviewer_output = (
            "Found 2 CRITICAL defects — missing tests for the failover path.\n"
            "Remediation required.\n"
        )
        assert _parse_verdict(reviewer_output) == "critical", (
            "actual CRITICAL findings without NO ISSUES FOUND marker must "
            "still return 'critical' so the fix pass runs"
        )

    def test_no_issues_without_critical_word_stays_no_issues(self):
        """Sanity: the classic clean output must still parse as no_issues."""
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("NO ISSUES FOUND") == "no_issues"


# ── Bug 2: cli.cmd_harden accepts --rounds < 1 and writes plan unchanged ──


class TestLoopIter6CliHardenAcceptsNegativeRoundsAndNukesPlan:
    """cmd_harden with --rounds=0/-1 skips the reviewer loop and silently succeeds."""

    def test_negative_rounds_should_be_rejected_by_cli(self, monkeypatch, tmp_path, capsys):
        """Running ``tero2 harden --rounds -1`` must fail fast, not no-op to success."""
        import sys

        # Hermetic HOME so this test doesn't depend on the runner's
        # ~/.tero2/config.toml happening to have a reviewer role configured.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        # Build a minimal project tree under tmp_path, with a project-local
        # .sora/config.toml that configures the reviewer role (required by
        # cmd_harden). This ensures the CLI path reaches the --rounds
        # handling regardless of the user's global config.
        project_path = tmp_path / "proj"
        project_path.mkdir()
        (project_path / "plan.md").write_text(
            "# Plan\n\n## T01: Task one\n- item\n",
            encoding="utf-8",
        )
        sora_dir = project_path / ".sora"
        sora_dir.mkdir()
        (sora_dir / "config.toml").write_text(
            "[roles.reviewer]\nprovider = \"claude\"\nmodel = \"sonnet\"\n",
            encoding="utf-8",
        )

        # Replace run_harden with a sentinel so we can detect whether the loop
        # was actually invoked with broken inputs. If the CLI validated rounds
        # at the boundary, run_harden would NEVER be called for rounds=-1.
        run_harden_calls: list[object] = []

        async def fake_run_harden(ctx):
            run_harden_calls.append(ctx.config.plan_hardening.max_rounds)
            # Emulate what run_harden does with a non-positive max_rounds:
            # range(1, max_rounds + 1) is empty, so the PLAN.md write just
            # passes current_plan straight through.
            from tero2.phases.context import PhaseResult
            return PhaseResult(success=True, data="pretend-hardened")

        monkeypatch.setattr(
            "tero2.phases.harden_phase.run_harden",
            fake_run_harden,
        )

        # Build fake argv for argparse.
        monkeypatch.setattr(
            sys, "argv",
            ["tero2", "harden", str(project_path),
             "--plan", str(project_path / "plan.md"),
             "--rounds", "-1"],
        )

        from tero2.cli import main

        try:
            main()
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
        else:
            exit_code = 0

        # BUG: CLI accepts --rounds -1 and reports success (exit 0) even
        # though run_harden's loop collapses to an empty range. The fake
        # run_harden captures the forwarded max_rounds so we can confirm
        # the value actually reached run_harden unchanged.
        # Expected: non-zero exit + an error message mentioning rounds.
        assert exit_code != 0 or run_harden_calls != [-1], (
            "tero2 harden --rounds -1 should fail with non-zero exit OR "
            f"sanitise the value before reaching run_harden; "
            f"got exit={exit_code}; run_harden_calls={run_harden_calls!r}"
        )


# ── Bug 3: project_init._sanitize_name always falls back to 'project' ────


class TestLoopIter6ProjectInitSanitizeNameSwallowsPunctuationOnly:
    """init_project accepts punctuation-only names by coercing to 'project' directory."""

    def test_punctuation_only_name_should_raise(self, tmp_path):
        """A name like '???' contains no usable characters — init_project must reject it."""
        from tero2.config import Config
        from tero2.project_init import init_project

        cfg = Config()
        cfg.projects_dir = str(tmp_path)

        plan_content = "# Plan\n\n## T01: Task one\n- item\n"

        # BUG: today, init_project silently creates {tmp_path}/project and
        # returns its path, then a second call with another punctuation-only
        # name collides on FileExistsError — hiding the real problem (the
        # input name was garbage).
        with pytest.raises(ValueError, match="(?i)empty|name|sanitiz"):
            init_project("???", plan_content, cfg)

    def test_empty_name_should_raise(self, tmp_path):
        from tero2.config import Config
        from tero2.project_init import init_project

        cfg = Config()
        cfg.projects_dir = str(tmp_path)

        with pytest.raises(ValueError, match="(?i)empty|name|sanitiz"):
            init_project("", "# Plan\n", cfg)

    def test_hyphens_only_name_should_raise(self, tmp_path):
        """Hyphens-only input produces empty string after strip('-')."""
        from tero2.config import Config
        from tero2.project_init import init_project

        cfg = Config()
        cfg.projects_dir = str(tmp_path)

        with pytest.raises(ValueError, match="(?i)empty|name|sanitiz"):
            init_project("----", "# Plan\n", cfg)

    def test_valid_name_still_creates_directory(self, tmp_path):
        """Sanity: a regular name with sanitisation still creates the expected dir."""
        from tero2.config import Config
        from tero2.project_init import init_project

        cfg = Config()
        cfg.projects_dir = str(tmp_path)

        project_path = init_project("My Cool Project!", "# Plan\n", cfg)
        assert project_path.name == "my-cool-project"
        assert project_path.is_dir()
