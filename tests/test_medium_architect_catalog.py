"""
Failing tests demonstrating 4 medium bugs from bugs.md.

  A32 — architect.py lines 156–158: catch-all except Exception swallows
         RateLimitError / ProviderError instead of letting them propagate
         to the caller's retry logic.
  A31 — architect.py lines 106–120: after recovery, errors is blindly
         cleared (``errors = []``) without re-validating the recovered
         plan — corrupted/invalid recovered plan silently accepted.
  A14 — providers/catalog.py lines 124–134: free_only=True call writes
         filtered (subset) entries to cache; subsequent free_only=False
         call reads the poisoned cache and returns only the subset.
  A27 — providers/catalog.py lines 100–101: datetime.fromisoformat may
         return a naive datetime; subtracting from datetime.now(timezone.utc)
         raises TypeError — cache staleness check silently broken.

Each test FAILs against current code and would pass once the bug is fixed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.errors import ProviderError, RateLimitError


# ─────────────────────────────────────────────────────────────────────────────
# A32 — ArchitectPlayer.run(): catch-all except swallows RateLimitError
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a32_rate_limit_error_propagates_from_architect_run(tmp_path):
    """A32 — RateLimitError must propagate out of ArchitectPlayer.run(), not be swallowed.

    Current code (architect.py lines 156–158)::

        except Exception as exc:
            log.error("architect failed: %s", exc)
            return ArchitectResult(success=False, error=str(exc))

    Bug: the bare ``except Exception`` catches everything, including
    ``RateLimitError`` and ``ProviderError``.  These errors carry retry
    semantics: the caller (the execute phase / runner) uses them to decide
    whether to attempt a different provider or back off.  By converting them
    to ``ArchitectResult(success=False)`` the information is lost and the
    runner has no way to distinguish a hard failure from a retryable
    rate-limit condition.

    This test makes ``chain.run_prompt_collected`` raise ``RateLimitError``,
    then calls ``ArchitectPlayer.run()``.  Correct behaviour: the exception
    propagates to the caller.  Buggy behaviour: swallowed into
    ``ArchitectResult(success=False)`` with no exception raised.
    """
    from tero2.players.architect import ArchitectPlayer
    from tero2.providers.chain import ProviderChain
    from tero2.disk_layer import DiskLayer

    chain = MagicMock(spec=ProviderChain)
    chain.run_prompt_collected = AsyncMock(side_effect=RateLimitError("all providers exhausted"))

    disk = MagicMock(spec=DiskLayer)
    disk.read_file.return_value = ""
    disk.write_file.return_value = None

    player = ArchitectPlayer(chain, disk, working_dir=str(tmp_path))

    with pytest.raises(RateLimitError):
        await player.run(slice_id="S01", milestone_path="milestones/M001")

    # BUG: the except Exception block catches RateLimitError and returns
    # ArchitectResult(success=False) instead of re-raising.  pytest.raises
    # never fires because no exception escapes — the test fails with:
    # "DID NOT RAISE <class 'tero2.errors.RateLimitError'>"


# ─────────────────────────────────────────────────────────────────────────────
# A31 — ArchitectPlayer.run(): errors blindly cleared after recovery
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a31_recovery_not_triggered_for_non_empty_tasks_errors(tmp_path):
    """A31 — _recover_plan_from_disk must NOT be called when the plan has tasks but
    fails for a non-structural reason (e.g. invalid dependency reference).

    Current code (architect.py lines 106–112)::

        errors = validate_plan(plan)
        if errors:                         # ← triggers recovery for ANY error
            recovered = self._recover_plan_from_disk(slice_id, milestone_path)
            if recovered is not None:
                plan = recovered[1]
                errors = []   # older versions; current may re-validate, but
                              # recovery is still triggered unnecessarily

    Bug: disk recovery is triggered for ANY validation error, not just when
    the plan has no tasks (which is the only case where an agent-CLI may have
    written the plan to a file instead of returning it as text).  When the
    LLM returns a plan that HAS tasks but fails for another reason (e.g.
    referencing a non-existent dependency), the code searches disk for a
    recovered file.  If a stale or unrelated plan file happens to exist on
    disk (from a prior run), it is loaded and may pass validation even though
    it corresponds to a completely different task decomposition.

    Correct behaviour: recovery should only be attempted when the plan has
    zero tasks (``task_count == 0``), not for every validation error.

    This test provides:
    - An LLM output that has tasks (task_count > 0) but FAILS validate_plan
      due to an invalid dependency reference.
    - A recovery plan on disk that PASSES validate_plan.

    Correct code: returns success=False (the LLM plan failed validation and
    recovery should NOT be triggered because tasks are present).
    Buggy code: recovery IS triggered even with tasks present, the valid disk
    plan is loaded, and success=True is returned with the wrong plan.
    """
    from tero2.players.architect import ArchitectPlayer, validate_plan
    from tero2.providers.chain import ProviderChain
    from tero2.disk_layer import DiskLayer

    # LLM output: has tasks (task_count > 0) but fails validate_plan
    # because it references a dependency T99 that doesn't exist.
    llm_output = (
        "## T01: Implement feature\n\n"
        "Must-haves: unit tests pass, code review approved.\n"
        "Dependencies: T99\n"
    )
    errors_llm = validate_plan(llm_output)
    assert errors_llm != [], (
        f"Test setup: llm_output must fail validate_plan; errors={errors_llm!r}"
    )
    # Confirm it HAS tasks — this is not a zero-tasks case
    import re
    task_re = re.compile(r"^##\s+T\d{2}[:\s]", re.MULTILINE)
    assert len(task_re.findall(llm_output)) > 0, (
        "Test setup: llm_output must contain at least one task"
    )

    # A valid plan that WOULD be found on disk if recovery is triggered
    disk_valid_plan = (
        "## T01: Completely different task\n\n"
        "Must-haves: nothing relevant to current slice.\n"
    )
    assert validate_plan(disk_valid_plan) == [], (
        f"Test setup: disk_valid_plan must pass validate_plan"
    )

    chain = MagicMock(spec=ProviderChain)
    chain.run_prompt_collected = AsyncMock(return_value=llm_output)

    disk = MagicMock(spec=DiskLayer)
    disk.read_file.return_value = ""
    disk.write_file.return_value = None

    player = ArchitectPlayer(chain, disk, working_dir=str(tmp_path))

    recover_calls: list[tuple[str, str]] = []

    def mock_recover(slice_id: str, milestone_path: str):
        recover_calls.append((slice_id, milestone_path))
        return (str(tmp_path / "S01-PLAN.md"), disk_valid_plan)

    with patch.object(player, "_recover_plan_from_disk", side_effect=mock_recover):
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

    # CORRECT: recovery should NOT be triggered when tasks are present but
    # validation fails for a non-structural reason (invalid dep ref).
    # The LLM plan errors should propagate → success=False.
    #
    # BUG A31: recovery IS triggered for any errors, so mock_recover is called,
    # the valid disk plan replaces the LLM output, and success=True is returned
    # with a completely unrelated plan.
    assert not result.success, (
        f"BUG A31: ArchitectPlayer.run() returned success=True by loading a "
        f"disk recovery plan even though the LLM output had tasks (task_count > 0) "
        f"and only failed for an invalid dependency reference. "
        f"Recovery (``_recover_plan_from_disk``) should only be triggered when "
        f"task_count == 0 (agent wrote plan to file instead of returning it). "
        f"Current code triggers recovery for ANY validation error (``if errors:``), "
        f"which allows a stale or unrelated disk plan to silently replace the "
        f"LLM output. _recover_plan_from_disk was called: {len(recover_calls)} time(s).\n"
        f"result={result!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A14 — catalog.py: free_only=True call poisons the cache for free_only=False
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a14_free_only_call_does_not_poison_cache(tmp_path):
    """A14 — get_models(free_only=True) must NOT write a filtered subset to cache.

    Current code (catalog.py lines 124–134)::

        async def get_models(cli: str, free_only: bool = False) -> list[ModelEntry]:
            if cli not in _DYNAMIC_PROVIDERS:
                return STATIC_CATALOG.get(cli, [])
            cached = _load_cache(cli)
            if cached is not None:
                if free_only:
                    return [m for m in cached if ":free" in m.id]
                return cached
            entries = await fetch_cli_models(cli, free_only=free_only)
            _save_cache(cli, entries)   # ← saves only the :free subset!
            return entries

    Bug: when the cache is cold and ``free_only=True``, ``fetch_cli_models``
    is called with ``free_only=True`` and returns only free models.  That
    filtered subset is then written to the cache via ``_save_cache``.  The
    next call with ``free_only=False`` reads the poisoned cache and returns
    only the free subset instead of all models.

    This test calls ``get_models("opencode", free_only=True)`` with a cold
    cache, then calls ``get_models("opencode", free_only=False)``.  Correct
    behaviour: the second call returns all models (more than the first).
    Buggy behaviour: the second call returns the same subset written to cache
    by the first call.
    """
    import tero2.providers.catalog as catalog_mod
    from tero2.providers.catalog import ModelEntry

    # Two full model entries — one free, one paid
    all_models = [
        ModelEntry(id="opencode/fast:free", label="Fast (free)"),
        ModelEntry(id="opencode/pro", label="Pro (paid)"),
    ]
    free_models = [m for m in all_models if ":free" in m.id]

    assert len(free_models) == 1
    assert len(all_models) == 2

    # Use a per-test cache directory so we don't pollute the real cache
    test_cache_dir = tmp_path / "cache"
    test_cache_dir.mkdir()

    async def mock_fetch(cli_name, provider_filter=None, free_only=False, refresh=False):
        if free_only:
            return free_models
        return all_models

    with (
        patch.object(catalog_mod, "_CACHE_DIR", test_cache_dir),
        patch.object(catalog_mod, "fetch_cli_models", side_effect=mock_fetch),
    ):
        # First call: free_only=True — cold cache, fetches free models, writes cache
        result_free = await catalog_mod.get_models("opencode", free_only=True)

        # Second call: free_only=False — cache is now warm (but poisoned by first call)
        result_all = await catalog_mod.get_models("opencode", free_only=False)

    # CORRECT: second call should return all models (len 2 > len 1)
    # BUG A14: second call returns cached subset (len 1 == len 1) because the
    #          first call wrote only the free subset to cache
    assert len(result_all) > len(result_free), (
        f"BUG A14: get_models(free_only=False) returned {len(result_all)} model(s) "
        f"but get_models(free_only=True) also returned {len(result_free)} model(s). "
        f"A free_only=True fetch wrote only the filtered subset to cache. "
        f"The subsequent free_only=False call read the poisoned cache and returned "
        f"only the free subset instead of all models. "
        f"result_free={result_free!r}, result_all={result_all!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A27 — catalog.py: naive datetime subtraction raises TypeError
# ─────────────────────────────────────────────────────────────────────────────


def test_a27_naive_fetched_at_raises_type_error(tmp_path):
    """A27 — _load_cache must handle a timezone-naive fetched_at without raising TypeError.

    Current code (catalog.py lines 100–101)::

        fetched_at = datetime.fromisoformat(raw["fetched_at"])
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()

    Bug: ``datetime.fromisoformat("2026-04-20T12:00:00")`` returns a
    timezone-naive datetime object.  Subtracting it from
    ``datetime.now(timezone.utc)`` (which is timezone-aware) raises:
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``.

    This TypeError is NOT caught by the ``except`` clause in ``_load_cache``
    (which only catches FileNotFoundError, KeyError, json.JSONDecodeError,
    ValueError) — so the exception propagates up to the caller and crashes
    the models fetch.

    This test writes a cache file with a naive ISO datetime string and calls
    ``_load_cache``.  Correct behaviour: no TypeError — the function either
    treats a naive timestamp as UTC or catches the error and returns None.
    Buggy behaviour: TypeError propagates out of _load_cache.
    """
    import tero2.providers.catalog as catalog_mod
    from tero2.providers.catalog import ModelEntry

    test_cache_dir = tmp_path / "cache"
    test_cache_dir.mkdir()

    # Write a cache file with a NAIVE (no timezone offset) ISO datetime
    cache_data = {
        "fetched_at": "2026-04-20T12:00:00",   # naive — no +00:00 suffix
        "entries": [{"id": "opencode/fast:free", "label": "Fast (free)"}],
    }
    cache_file = test_cache_dir / "opencode_models.json"
    cache_file.write_text(json.dumps(cache_data), encoding="utf-8")

    with patch.object(catalog_mod, "_CACHE_DIR", test_cache_dir):
        # BUG A27: raises TypeError because naive datetime cannot be subtracted
        # from aware datetime.  Correct code handles both naive and aware.
        try:
            result = catalog_mod._load_cache("opencode")
        except TypeError as exc:
            pytest.fail(
                f"BUG A27: _load_cache raised TypeError when fetched_at is a "
                f"timezone-naive ISO string ('2026-04-20T12:00:00'). "
                f"catalog.py line 101: ``age = datetime.now(timezone.utc) - "
                f"datetime.fromisoformat(raw['fetched_at'])`` raises "
                f"TypeError: can't subtract offset-naive and offset-aware datetimes. "
                f"The TypeError is not caught by the except clause "
                f"(which only catches FileNotFoundError, KeyError, "
                f"json.JSONDecodeError, ValueError). "
                f"Fix: parse with .replace(tzinfo=timezone.utc) when tzinfo is None, "
                f"or add TypeError to the except tuple. "
                f"Original error: {exc}"
            )
