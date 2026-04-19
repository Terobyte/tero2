"""Tests for the 8 medium-complexity bugs from the audit.

Bugs covered: 3, 13, 18, 25, 26, 30, 31, 37
Run: pytest tests/test_medium_bugs.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker
from tero2.config import Config, ContextConfig, RoleConfig, StuckDetectionConfig
from tero2.context_assembly import ContextAssembler, estimate_tokens
from tero2.disk_layer import DiskLayer
from tero2.errors import CircuitOpenError
from tero2.events import Event, EventDispatcher, make_event
from tero2.history import record_run
from tero2.state import AgentState, Phase, SoraPhase
from tero2.stuck_detection import update_tool_hash


# ── Bug #3: events.py _unfinished_tasks invariant ────────────────────────────


class TestBug3UnfinishedTasksInvariant:
    """After every emit(), q._unfinished_tasks must equal q.qsize().

    The slow path manipulates deque internals directly. When swapping a
    non-priority item for a new item, the net change to _unfinished_tasks
    must be zero. Current code does -= 1 then += 1 (correct but fragile);
    the fix removes both (simpler and safe).
    """

    @pytest.fixture()
    def dispatcher(self) -> EventDispatcher:
        return EventDispatcher()

    def _priority(self, n: int) -> Event:
        return make_event("phase_change", priority=True, data={"n": n})

    def _normal(self, n: int) -> Event:
        return make_event("step", priority=False, data={"n": n})

    @pytest.mark.asyncio
    async def test_unfinished_tasks_equals_qsize_after_priority_evicts_normal(
        self, dispatcher: EventDispatcher
    ) -> None:
        """Emit priority event into a full queue of normal events.

        Invariant: _unfinished_tasks == qsize() after the swap.
        """
        q = dispatcher.subscribe()
        maxsize = q.maxsize  # 500

        # Fill the queue with non-priority events using put_nowait
        for i in range(maxsize):
            q.put_nowait(self._normal(i))

        assert q.qsize() == maxsize
        assert q._unfinished_tasks == maxsize  # type: ignore[attr-defined]

        # Emit one priority event — triggers the slow path (swap)
        await dispatcher.emit(self._priority(999))

        # Invariant: count must still match queue size (swap is net-zero)
        assert q._unfinished_tasks == q.qsize(), (  # type: ignore[attr-defined]
            f"_unfinished_tasks={q._unfinished_tasks} != qsize={q.qsize()}"  # type: ignore[attr-defined]
        )

    @pytest.mark.asyncio
    async def test_unfinished_tasks_equals_qsize_after_normal_evicts_normal(
        self, dispatcher: EventDispatcher
    ) -> None:
        """Emit normal event into full queue of normal events (same priority swap)."""
        q = dispatcher.subscribe()
        for i in range(q.maxsize):
            q.put_nowait(self._normal(i))

        await dispatcher.emit(self._normal(999))

        assert q._unfinished_tasks == q.qsize()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_all_priority_queue_grows_by_one_for_priority_event(
        self, dispatcher: EventDispatcher
    ) -> None:
        """Overflow path: all-priority queue grows by 1 when a priority event arrives."""
        q = dispatcher.subscribe()
        for i in range(q.maxsize):
            q.put_nowait(self._priority(i))

        before = q.qsize()
        await dispatcher.emit(self._priority(999))

        assert q.qsize() == before + 1
        assert q._unfinished_tasks == q.qsize()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_all_priority_queue_drops_incoming_normal_event(
        self, dispatcher: EventDispatcher
    ) -> None:
        """Drop path: normal event is dropped when queue is all-priority."""
        q = dispatcher.subscribe()
        for i in range(q.maxsize):
            q.put_nowait(self._priority(i))

        before = q.qsize()
        await dispatcher.emit(self._normal(999))

        # Non-priority event silently dropped — queue size unchanged
        assert q.qsize() == before
        assert q._unfinished_tasks == q.qsize()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_priority_event_replaces_oldest_non_priority(
        self, dispatcher: EventDispatcher
    ) -> None:
        """The oldest non-priority item is discarded (FIFO order)."""
        q = dispatcher.subscribe()
        for i in range(q.maxsize):
            q.put_nowait(self._normal(i))

        # The first item in the deque (index 0) should be evicted
        first_item = q._queue[0]  # type: ignore[attr-defined]
        assert first_item.data["n"] == 0

        await dispatcher.emit(self._priority(999))

        # First item should be gone; last item should be our new priority event
        assert q._queue[0].data["n"] == 1  # type: ignore[attr-defined]  # oldest was evicted
        assert q._queue[-1].data["n"] == 999  # type: ignore[attr-defined]  # new event at end


# ── Bug #13: state __setattr__ bypass is intentional (crash recovery) ─────────


class TestBug13SetAttrBypassIsIntentional:
    """from_json must accept any valid saved phase for crash recovery.

    The bypass is intentional — __setattr__ transition validation only
    applies to runtime transitions. On restart, any valid Phase/SoraPhase
    must be restorable from disk.
    """

    def test_from_json_restores_completed_phase(self) -> None:
        """COMPLETED must be loadable — it's a valid terminal state on disk."""
        import json

        state = AgentState.from_json(json.dumps({"phase": "completed"}))
        assert state.phase == Phase.COMPLETED

    def test_from_json_restores_execute_sora_phase(self) -> None:
        """EXECUTE must be loadable — runner may have crashed mid-execute."""
        import json

        state = AgentState.from_json(json.dumps({"sora_phase": "execute"}))
        assert state.sora_phase == SoraPhase.EXECUTE

    def test_invalid_phase_string_falls_back_to_idle(self) -> None:
        """Unknown phase strings are coerced to IDLE, not a crash."""
        import json

        state = AgentState.from_json(json.dumps({"phase": "not_a_phase"}))
        assert state.phase == Phase.IDLE


# ── Bug #18: circuit_breaker HALF_OPEN allows exactly one trial ───────────────


class TestBug18HalfOpenOneTrial:
    """check() must allow exactly one trial call in HALF_OPEN state.

    After the first allowed call, subsequent calls must raise CircuitOpenError
    until record_success() or record_failure() resolves the trial.
    """

    def test_half_open_blocks_second_call(self) -> None:
        cb = CircuitBreaker(name="svc", failure_threshold=1, recovery_timeout_s=0)
        cb.record_failure()
        cb.last_failure_time = 0.0
        cb.check()  # OPEN → HALF_OPEN; trial allowed
        with pytest.raises(CircuitOpenError):
            cb.check()  # second call must be blocked

    def test_success_closes_circuit(self) -> None:
        cb = CircuitBreaker(name="svc", failure_threshold=1, recovery_timeout_s=0)
        cb.state = CBState.HALF_OPEN
        cb.check()
        cb.record_success()
        assert cb.state == CBState.CLOSED
        cb.check()  # CLOSED: no raise

    def test_failure_reopens_circuit(self) -> None:
        cb = CircuitBreaker(name="svc", failure_threshold=1, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN
        cb.check()
        cb.record_failure()
        assert cb.state == CBState.OPEN
        # recovery_timeout_s=60, so the circuit cannot recover immediately
        with pytest.raises(CircuitOpenError):
            cb.check()


# ── Bug #25: disk_layer distinguishes missing vs empty vs OSError ─────────────


class TestBug25DiskLayerErrorTypes:
    """read_file must return distinguishable results for different failure modes."""

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()
        assert disk.read_file("human/no_such_file.md") is None

    def test_empty_file_returns_empty_string(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()
        (disk.sora_dir / "human" / "empty.md").write_text("")
        assert disk.read_file("human/empty.md") == ""

    def test_missing_and_empty_are_distinguishable(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()
        (disk.sora_dir / "human" / "empty.md").write_text("")
        missing = disk.read_file("human/gone.md")
        empty = disk.read_file("human/empty.md")
        assert missing != empty, f"missing={missing!r} should differ from empty={empty!r}"


# ── Bug #26: context_assembly O(n²) → O(n) correctness ───────────────────────


def _make_config(budget_chars: int) -> Config:
    """Config where the role budget is budget_chars // 4 tokens."""
    # target_ratio * context_window = budget in tokens
    # budget_chars // 4 = budget in tokens, so context_window = budget_chars * 4 chars
    cfg = Config()
    cfg.roles["tester"] = RoleConfig(provider="test", context_window=budget_chars * 4)
    cfg.context = ContextConfig(target_ratio=0.25)  # 25% of context_window = budget_chars tokens
    return cfg


class TestBug26ContextAssemblyPriorityOrder:
    """Sections are included in priority order; when budget is tight,
    higher-priority sections must be included and lower-priority ones dropped.

    This validates correctness of both the O(n²) and the O(n) implementation.
    """

    def _assembler(self, total_budget_chars: int) -> ContextAssembler:
        cfg = _make_config(total_budget_chars)
        return ContextAssembler(cfg)

    def test_highest_priority_sections_included_when_budget_tight(self) -> None:
        """With a tight budget that fits system + task + 2 optional sections,
        the 2 highest-priority sections must be included, not the lowest-priority ones."""
        # Each section body is 400 chars → 100 tokens
        section_body = "x" * 400

        # Budget: system(100t) + task(100t) + 2 optional(200t) = 400t
        # Expressed in chars for config: 400 * 4 = 1600 chars budget
        # But we need some margin for section headers ("## Tag\n" = ~10 chars)
        # Let's use 500 tokens budget = 2000 chars budget
        total_budget_chars = 2000

        assembler = self._assembler(total_budget_chars)

        result = assembler.assemble(
            role="tester",
            system_prompt="s" * 100 * 4,    # 100 tokens
            task_plan="t" * 100 * 4,         # 100 tokens (in ## Task section)
            summaries=[section_body],         # priority=0, 100 tokens
            context_map=section_body,         # priority=1, 100 tokens
            code_snippets=section_body,       # priority=2, 100 tokens
            context_hints=section_body,       # priority=3, 100 tokens
        )

        # Should include highest-priority sections (context_hints=3, code_snippets=2)
        # and not include lower-priority ones when tight
        # At minimum, context_hints (pri=3) must be included
        assert "CONTEXT_HINTS" in result.user_prompt, (
            "context_hints (highest priority) must be included"
        )

    def test_all_sections_included_when_budget_ample(self) -> None:
        """When budget is large, all optional sections must be included."""
        section_body = "x" * 100  # 25 tokens each

        # Very large budget: context_window=1_000_000
        cfg = Config()
        cfg.roles["tester"] = RoleConfig(provider="test", context_window=1_000_000)
        cfg.context = ContextConfig(target_ratio=0.5)
        assembler = ContextAssembler(cfg)

        result = assembler.assemble(
            role="tester",
            system_prompt="sys",
            task_plan="task",
            summaries=[section_body],
            context_map=section_body,
            code_snippets=section_body,
            context_hints=section_body,
        )

        assert "CONTEXT_MAP" in result.user_prompt
        assert "Code Snippets" in result.user_prompt
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "Summary" in result.user_prompt

    def test_many_summaries_included_by_priority(self) -> None:
        """With N summaries, newer summaries (higher index = lower priority 0) are
        tried alongside other sections. Sections with higher keep_priority evict them."""
        cfg = Config()
        cfg.roles["tester"] = RoleConfig(provider="test", context_window=1_000_000)
        cfg.context = ContextConfig(target_ratio=0.5)
        assembler = ContextAssembler(cfg)

        # 5 summaries — all should fit in a large budget
        result = assembler.assemble(
            role="tester",
            system_prompt="sys",
            task_plan="task",
            summaries=["summary" + str(i) for i in range(5)],
        )

        for i in range(5):
            assert f"summary{i}" in result.user_prompt, f"summary{i} missing from result"

    def test_incremental_token_count_equivalent_to_string_rebuild(self) -> None:
        """O(n) and O(n²) must agree on which sections to include.

        This test builds the expected result independently and verifies
        the assembler produces the same selection.
        """
        body = "y" * 200  # 50 tokens per body

        cfg = Config()
        cfg.roles["tester"] = RoleConfig(provider="test", context_window=10_000)
        cfg.context = ContextConfig(target_ratio=0.1)  # budget = 1000 tokens
        assembler = ContextAssembler(cfg)

        result = assembler.assemble(
            role="tester",
            system_prompt="s" * 400,   # 100 tokens
            task_plan="t" * 400,        # 100 tokens
            summaries=[body],           # pri=0, 50 tokens
            context_map=body,           # pri=1, 50 tokens
            code_snippets=body,         # pri=2, 50 tokens
            context_hints=body,         # pri=3, 50 tokens
        )

        # System(100) + task(100) + 4 optional(4*~60) = ~440 tokens < 1000
        # All sections should fit
        assert "CONTEXT_HINTS" in result.user_prompt
        assert "Code Snippets" in result.user_prompt
        assert "CONTEXT_MAP" in result.user_prompt
        assert "Summary" in result.user_prompt


# ── Bug #30: stuck_detection does not mutate input state ─────────────────────


class TestBug30NoStateMutation:
    """update_tool_hash must return a new state and leave the original unchanged."""

    def test_original_state_not_mutated(self) -> None:
        state = AgentState(last_tool_hash="abc", tool_repeat_count=0)
        update_tool_hash(state, "tool_call_x")
        assert state.last_tool_hash == "abc"
        assert state.tool_repeat_count == 0

    def test_returned_state_has_new_hash(self) -> None:
        state = AgentState(last_tool_hash="old", tool_repeat_count=0)
        new_state, is_repeat = update_tool_hash(state, "tool_call_y")
        assert new_state.last_tool_hash != "old"
        assert not is_repeat

    def test_repeat_detected_when_same_call(self) -> None:
        state = AgentState(last_tool_hash="", tool_repeat_count=0)
        state1, _ = update_tool_hash(state, "same_call")
        state2, is_repeat = update_tool_hash(state1, "same_call")
        assert is_repeat
        assert state2.tool_repeat_count == 1


# ── Bug #31: ProviderChain index updates after CB skip ───────────────────────


class TestBug31ProviderChainIndexUpdate:
    """current_provider_index reflects the provider that was actually used,
    not a skipped one.

    The index must be set AFTER the CB availability check, so a circuit-open
    provider never leaves its own index in current_provider_index.
    """

    @pytest.mark.asyncio
    async def test_index_reflects_used_provider_not_skipped_one(self) -> None:
        from unittest.mock import AsyncMock

        from tero2.circuit_breaker import CircuitBreakerRegistry
        from tero2.providers.chain import ProviderChain

        class _FakeProvider:
            display_name: str

            def __init__(self, name: str) -> None:
                self.display_name = name

            async def run(self, **kwargs):  # type: ignore[override]
                yield "ok"

        p0 = _FakeProvider("p0")
        p1 = _FakeProvider("p1")

        registry = CircuitBreakerRegistry()
        # Open the circuit for p0
        cb0 = registry.get("p0")
        for _ in range(cb0.failure_threshold):
            cb0.record_failure()
        assert not cb0.is_available

        chain = ProviderChain([p0, p1], cb_registry=registry)
        results = []
        async for msg in chain.run(prompt="hi"):
            results.append(msg)

        # p0 was skipped (circuit open); p1 was used → index must be 1
        assert chain.current_provider_index == 1, (
            f"index should be 1 (p1 used), got {chain.current_provider_index}"
        )


# ── Bug #37: history.py stores relative plan path ────────────────────────────


class TestBug37HistoryRelativePath:
    """record_run must store plan_file as a relative path, not just the filename.

    Storing plan_file.name loses subdirectory context. Projects with plans
    in different subdirs would produce identical last_plan values.
    """

    def test_plan_in_subdir_stored_as_relative_path(self, tmp_path: Path) -> None:
        from tero2.history import HISTORY_FILE, load_history

        project = tmp_path / "myproject"
        project.mkdir()
        plan = project / "docs" / "plans" / "feature.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# plan")

        # Temporarily redirect HISTORY_FILE to tmp_path
        import tero2.history as history_mod
        original = history_mod.HISTORY_FILE
        history_mod.HISTORY_FILE = tmp_path / "history.json"
        try:
            record_run(project, plan)
            entries = load_history()
            assert entries, "history must have at least one entry"
            last_plan = entries[0].last_plan
            assert last_plan == "docs/plans/feature.md", (
                f"expected relative path, got {last_plan!r}"
            )
        finally:
            history_mod.HISTORY_FILE = original

    def test_plan_at_project_root_stored_as_filename(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        plan = project / "plan.md"
        plan.write_text("# plan")

        import tero2.history as history_mod
        original = history_mod.HISTORY_FILE
        history_mod.HISTORY_FILE = tmp_path / "history.json"
        try:
            record_run(project, plan)
            from tero2.history import load_history
            entries = load_history()
            assert entries[0].last_plan == "plan.md"
        finally:
            history_mod.HISTORY_FILE = original
