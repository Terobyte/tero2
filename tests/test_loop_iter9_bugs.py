"""Loop iter9 — candidate bugs.

Each class documents ONE candidate bug with:
- a negative test that fails on current (broken) code,
- a summary of observable impact,
- a fix hint in the docstring.
"""

from __future__ import annotations

from pathlib import Path


# ── Bug 1 ───────────────────────────────────────────────────────────────────
class TestLoopIter9ArchitectMustHaveLeadingDashes:
    """Bug: ``_parse_slice_plan`` mangles must-haves that begin with flag-like text.

    Location: tero2/players/architect.py:295

        must_haves.append(line.lstrip("- ").strip())

    ``str.lstrip("- ")`` treats the argument as a CHARACTER SET, not a prefix.
    It strips any combination of ``'-'`` and ``' '`` from the left, so a bullet
    containing a CLI flag loses its flag marker:

        ``- --verbose flag``  →  ``"verbose flag"``   (lost ``--``)
        ``- -t option``       →  ``"t option"``       (lost ``-``)
        ``- - option two``    →  ``"option two"``     (lost the inner dash!)

    Observable impact: the Builder receives the stripped string in its prompt
    via ``_format_task_plan`` (execute_phase.py line 699), so the LLM sees
    ``verbose flag`` instead of the intended ``--verbose flag`` — which is a
    different requirement. Must-haves that document CLI options silently lose
    their flag markers.

    Fix hint: use ``line[1:].lstrip()`` or a single-char strip: after
    ``line.startswith("-")``, take ``line[1:].lstrip(" ")``. A prefix-style
    strip avoids the character-set gotcha.
    """

    def test_must_have_with_double_dash_flag_preserved(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        plan = (
            "## T01: Build feature\n\n"
            "**Must-haves:**\n"
            "- --verbose flag supported\n"
            "- -t option documented\n"
        )
        sp = _parse_slice_plan(plan, "S01")
        assert len(sp.tasks) == 1
        must_haves = sp.tasks[0].must_haves
        assert "--verbose flag supported" in must_haves, (
            f"BUG: must-have '--verbose flag supported' was mangled; "
            f"parsed as {must_haves!r}. str.lstrip('- ') strips any combination "
            f"of '-' and ' ', losing leading dashes that are part of the value."
        )
        assert "-t option documented" in must_haves, (
            f"BUG: must-have '-t option documented' was mangled; "
            f"parsed as {must_haves!r}. A CLI short-flag would be lost entirely."
        )


# ── Bug 2 ───────────────────────────────────────────────────────────────────
class TestLoopIter9PersonaCacheInvalidation:
    """Bug: ``PersonaRegistry._resolve`` records mtime but never invalidates cache.

    Location: tero2/persona.py:197-213

    The comment at line 203 says:

        # Bug 231: record st_mtime for potential future invalidation/refresh.

    and lines 209-212 store ``current_mtime`` into ``_resolved_cache_mtime``,
    but the stored value is NEVER compared against the cached one. The cache
    is returned regardless of whether the on-disk file was modified since
    first read.

    Observable impact: a user editing ``.sora/prompts/builder.md`` while the
    runner is live sees no effect — the first-load content is reused forever
    for the life of the ``PersonaRegistry`` instance. This defeats the
    entire point of tracking mtime and breaks the operator's ability to
    hot-reload personas between slices.

    Fix hint: compare ``current_mtime`` against the stored
    ``self._resolved_cache_mtime.get(role)`` before returning the cache,
    re-read the file on mtime change.
    """

    def test_persona_cache_picks_up_on_disk_edits(self, tmp_path: Path) -> None:
        from tero2.persona import PersonaRegistry

        prompts_dir = tmp_path / ".sora" / "prompts"
        prompts_dir.mkdir(parents=True)
        prompt_file = prompts_dir / "builder.md"
        prompt_file.write_text("original prompt\n", encoding="utf-8")

        reg = PersonaRegistry(project_path=tmp_path)
        first = reg.load("builder")
        assert first.system_prompt.strip() == "original prompt"

        # Simulate an operator editing the prompt between slices. Bump mtime
        # well into the future so the FS cannot silently coalesce the writes.
        import os
        import time

        time.sleep(0.01)  # ensure mtime resolution differs
        prompt_file.write_text("edited prompt\n", encoding="utf-8")
        future = time.time() + 10
        os.utime(prompt_file, (future, future))

        second = reg.load("builder")
        assert second.system_prompt.strip() == "edited prompt", (
            f"BUG: PersonaRegistry returned stale cached prompt "
            f"{second.system_prompt!r} after the on-disk file was edited. "
            f"_resolved_cache_mtime is tracked but never compared — the "
            f"first-load value is reused for the life of the registry."
        )


# ── Bug 3 ───────────────────────────────────────────────────────────────────
class TestLoopIter9ReflexionAddAttemptMutatesInput:
    """Bug: ``add_attempt`` mutates the caller's ReflexionContext in place.

    Location: tero2/reflexion.py:100-126

        def add_attempt(context, ...):
            ...
            context.attempts.append(attempt)
            return context

    The function is sold as "Returns: Updated ReflexionContext", suggesting a
    functional, immutable update. In reality it mutates the input AND returns
    it, so two holders of the same ReflexionContext accidentally share state.

    Observable impact: the execute_phase retry loop treats the reflexion
    context as a sequence of snapshots (see ``reflexion_ctx = add_attempt(...)``
    at execute_phase.py:369 and :459). If a caller keeps an earlier reference
    for audit/replay (e.g. to compare "attempts after cycle 1" vs "attempts
    after cycle 2"), all references point to the same mutated list — the
    earlier snapshot now also shows later attempts.

    Fix hint: rebuild the dataclass with a fresh list:

        return ReflexionContext(attempts=[*context.attempts, attempt])

    or use ``dataclasses.replace`` with a copied list.
    """

    def test_add_attempt_does_not_mutate_input_snapshot(self) -> None:
        from tero2.reflexion import ReflexionContext, add_attempt

        ctx = ReflexionContext()
        snapshot_before = add_attempt(ctx, builder_output="out 1", verifier_feedback="fb 1")
        # Capture the length seen at "snapshot time" — the caller expects this
        # to stay stable as later attempts get appended.
        snapshot_len = len(snapshot_before.attempts)

        # Second call — supposed to produce a new context with 2 attempts.
        # The old snapshot_before reference MUST still see only 1 attempt.
        add_attempt(snapshot_before, builder_output="out 2", verifier_feedback="fb 2")

        assert len(snapshot_before.attempts) == snapshot_len, (
            f"BUG: add_attempt mutates the input context in place. The "
            f"snapshot reference captured after attempt 1 now shows "
            f"{len(snapshot_before.attempts)} attempts instead of "
            f"{snapshot_len}. Two independent callers holding the same "
            f"context reference corrupt each other's view of history."
        )
