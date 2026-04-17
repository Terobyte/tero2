"""Tests for tero2.triggers — Coach trigger detection."""

from __future__ import annotations

from unittest.mock import MagicMock

from tero2.config import Config
from tero2.disk_layer import DiskLayer
from tero2.state import AgentState
from tero2.triggers import CoachTrigger, TriggerResult, check_triggers


def _make_disk(
    *,
    journal: str = "",
    steer: str = "",
    metrics: dict | None = None,
) -> MagicMock:
    disk = MagicMock(spec=DiskLayer)
    disk.read_file.side_effect = lambda p: journal if "EVENT_JOURNAL" in p else ""
    disk.read_steer.return_value = steer
    disk.read_metrics.return_value = metrics if metrics is not None else {}
    return disk


def _make_state(*, escalation_level: int = 0) -> AgentState:
    return AgentState(escalation_level=escalation_level)


class TestNoTriggersFire:
    def test_default_state_no_triggers(self) -> None:
        state = _make_state()
        disk = _make_disk()
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False
        assert result.trigger is None

    def test_zero_escalation_no_journal_no_steer_no_budget(self) -> None:
        state = _make_state(escalation_level=0)
        disk = _make_disk(
            journal="normal entry", steer="", metrics={"total_cost": 0, "budget_limit": 100}
        )
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False


class TestStuckTrigger:
    def test_stuck_fires_at_level_2(self) -> None:
        state = _make_state(escalation_level=2)
        disk = _make_disk()
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is True
        assert result.trigger == CoachTrigger.STUCK
        assert "escalation_level=2" in result.reason

    def test_stuck_fires_at_level_3(self) -> None:
        state = _make_state(escalation_level=3)
        disk = _make_disk()
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is True
        assert result.trigger == CoachTrigger.STUCK

    def test_stuck_does_not_fire_at_level_1(self) -> None:
        state = _make_state(escalation_level=1)
        disk = _make_disk()
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False


class TestAnomalyTrigger:
    def test_anomaly_fires_when_journal_contains_anomaly(self) -> None:
        state = _make_state()
        disk = _make_disk(journal="## Entry\nANOMALY detected\n")
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is True
        assert result.trigger == CoachTrigger.ANOMALY
        assert "ANOMALY" in result.reason

    def test_anomaly_does_not_fire_without_keyword(self) -> None:
        state = _make_state()
        disk = _make_disk(journal="## Entry\nAll good\n")
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False

    def test_anomaly_does_not_fire_on_empty_journal(self) -> None:
        state = _make_state()
        disk = _make_disk(journal="")
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False

    def test_anomaly_is_case_sensitive(self) -> None:
        state = _make_state()
        disk = _make_disk(journal="anomaly lower case")
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False


class TestHumanSteerTrigger:
    def test_steer_fires_when_non_empty(self) -> None:
        state = _make_state()
        disk = _make_disk(steer="Change direction to X")
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is True
        assert result.trigger == CoachTrigger.HUMAN_STEER
        assert "STEER" in result.reason

    def test_steer_does_not_fire_when_empty(self) -> None:
        state = _make_state()
        disk = _make_disk(steer="")
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False


class TestBudgetTrigger:
    def test_budget_fires_at_60_percent(self) -> None:
        state = _make_state()
        disk = _make_disk(metrics={"total_cost": 60, "budget_limit": 100})
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is True
        assert result.trigger == CoachTrigger.BUDGET_60

    def test_budget_fires_above_60_percent(self) -> None:
        state = _make_state()
        disk = _make_disk(metrics={"total_cost": 90, "budget_limit": 100})
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is True
        assert result.trigger == CoachTrigger.BUDGET_60

    def test_budget_does_not_fire_below_60_percent(self) -> None:
        state = _make_state()
        disk = _make_disk(metrics={"total_cost": 59, "budget_limit": 100})
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False

    def test_budget_does_not_fire_with_zero_limit(self) -> None:
        state = _make_state()
        disk = _make_disk(metrics={"total_cost": 100, "budget_limit": 0})
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False

    def test_budget_does_not_fire_with_empty_metrics(self) -> None:
        state = _make_state()
        disk = _make_disk(metrics={})
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False

    def test_budget_does_not_fire_without_metrics_keys(self) -> None:
        state = _make_state()
        disk = _make_disk(metrics={"other_key": 42})
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.should_fire is False


class TestPriorityOrder:
    def test_stuck_beats_anomaly(self) -> None:
        state = _make_state(escalation_level=2)
        disk = _make_disk(
            journal="ANOMALY here",
            steer="steer me",
            metrics={"total_cost": 80, "budget_limit": 100},
        )
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.trigger == CoachTrigger.STUCK

    def test_anomaly_beats_steer(self) -> None:
        state = _make_state()
        disk = _make_disk(
            journal="ANOMALY here",
            steer="steer me",
            metrics={"total_cost": 80, "budget_limit": 100},
        )
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.trigger == CoachTrigger.ANOMALY

    def test_steer_beats_budget(self) -> None:
        state = _make_state()
        disk = _make_disk(
            journal="", steer="steer me", metrics={"total_cost": 80, "budget_limit": 100}
        )
        config = Config()
        result = check_triggers(state, disk, config)
        assert result.trigger == CoachTrigger.HUMAN_STEER


class TestTriggerResultDefaults:
    def test_no_fire_result(self) -> None:
        r = TriggerResult(should_fire=False)
        assert r.trigger is None
        assert r.reason == ""

    def test_fire_result_fields(self) -> None:
        r = TriggerResult(should_fire=True, trigger=CoachTrigger.STUCK, reason="x")
        assert r.should_fire is True
        assert r.trigger == CoachTrigger.STUCK
        assert r.reason == "x"


class TestCoachTriggerEnum:
    def test_all_members_exist(self) -> None:
        expected = {"first_run", "end_of_slice", "anomaly", "budget_60", "stuck", "human_steer"}
        assert set(m.value for m in CoachTrigger) == expected

    def test_str_enum_comparison(self) -> None:
        assert CoachTrigger.STUCK == "stuck"
        assert CoachTrigger.ANOMALY == "anomaly"
