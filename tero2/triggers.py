"""Coach trigger detection.

The Coach is invoked episodically, not continuously.
This module checks trigger conditions after each phase boundary.

FIRST_RUN and END_OF_SLICE are NOT conditional — they are fixed pipeline
steps called directly by the runner. check_triggers() only evaluates
conditional triggers in priority order: STUCK > ANOMALY > HUMAN_STEER > BUDGET_60.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from tero2.config import Config
from tero2.disk_layer import DiskLayer
from tero2.state import AgentState


class CoachTrigger(str, Enum):
    FIRST_RUN = "first_run"
    END_OF_SLICE = "end_of_slice"
    ANOMALY = "anomaly"
    BUDGET_60 = "budget_60"
    STUCK = "stuck"
    HUMAN_STEER = "human_steer"


# Alias kept for compatibility with modules that import TriggerEvent.
TriggerEvent = CoachTrigger


@dataclass
class TriggerResult:
    should_fire: bool
    trigger: CoachTrigger | None = None
    reason: str = ""


def check_triggers(state: AgentState, disk: DiskLayer, config: Config) -> TriggerResult:
    """Check all conditional trigger conditions, return highest priority.

    Priority: STUCK > ANOMALY > HUMAN_STEER > BUDGET_60

    FIRST_RUN and END_OF_SLICE are NOT checked here — they are fixed
    pipeline steps called directly by the runner.
    """
    if _check_stuck(state, config):
        return TriggerResult(
            should_fire=True,
            trigger=CoachTrigger.STUCK,
            reason=f"escalation_level={state.escalation_level} >= 2",
        )

    if _check_anomaly(disk):
        return TriggerResult(
            should_fire=True,
            trigger=CoachTrigger.ANOMALY,
            reason="EVENT_JOURNAL.md contains ANOMALY entry",
        )

    if _check_human_steer(disk):
        return TriggerResult(
            should_fire=True,
            trigger=CoachTrigger.HUMAN_STEER,
            reason="human/STEER.md is non-empty",
        )

    if _check_budget(disk):
        return TriggerResult(
            should_fire=True,
            trigger=CoachTrigger.BUDGET_60,
            reason="budget usage >= 60%",
        )

    return TriggerResult(should_fire=False)


def _check_stuck(state: AgentState, config: Config) -> bool:
    """True if escalation has reached Level 2 (backtrack)."""
    return state.escalation_level >= 2


_ANOMALY_RE = re.compile(r"(?:^|##\s*)ANOMALY", re.MULTILINE)


def _check_anomaly(disk: DiskLayer) -> bool:
    """True if EVENT_JOURNAL.md contains a structured ANOMALY marker.

    Matches '## ANOMALY ...' (structured event header) or 'ANOMALY ...' at
    the start of a line.  Does NOT match 'ANOMALY' embedded in prose sentences.
    """
    journal = disk.read_file("persistent/EVENT_JOURNAL.md")
    if not journal:
        return False
    return bool(_ANOMALY_RE.search(journal))


def _check_human_steer(disk: DiskLayer) -> bool:
    """True if STEER.md exists and is non-empty."""
    return disk.read_steer() != ""


def _check_budget(disk: DiskLayer) -> bool:
    """True if budget usage >= 60% (from metrics.json)."""
    metrics = disk.read_metrics()
    if not metrics:
        return False
    total_cost = metrics.get("total_cost", 0)
    budget_limit = metrics.get("budget_limit", 0)
    if budget_limit <= 0:
        return False
    return (total_cost / budget_limit) >= 0.60
