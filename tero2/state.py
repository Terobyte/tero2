"""Runtime state model for tero2."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from tero2.errors import StateTransitionError


class Phase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class SoraPhase(str, Enum):
    NONE = "none"
    HARDENING = "hardening"
    SCOUT = "scout"
    COACH = "coach"
    ARCHITECT = "architect"
    EXECUTE = "execute"
    SLICE_DONE = "slice_done"


# Valid next-states for Phase transitions.
# COMPLETED is terminal; no exits allowed.
_PHASE_VALID_NEXT: dict[Phase, frozenset[Phase]] = {
    Phase.IDLE: frozenset({Phase.RUNNING}),
    Phase.RUNNING: frozenset({Phase.COMPLETED, Phase.FAILED, Phase.PAUSED}),
    Phase.PAUSED: frozenset({Phase.RUNNING, Phase.FAILED}),
    Phase.FAILED: frozenset({Phase.RUNNING}),
    Phase.COMPLETED: frozenset(),
}

# Valid next-states for SoraPhase transitions.
# Self-transitions are allowed (crash recovery re-sets the current phase).
# Forward order: NONE → HARDENING → SCOUT → COACH → ARCHITECT → EXECUTE → SLICE_DONE.
_SORA_VALID_NEXT: dict[SoraPhase, frozenset[SoraPhase]] = {
    SoraPhase.NONE: frozenset({
        SoraPhase.HARDENING, SoraPhase.SCOUT, SoraPhase.COACH, SoraPhase.ARCHITECT
    }),
    SoraPhase.HARDENING: frozenset({
        SoraPhase.HARDENING, SoraPhase.SCOUT, SoraPhase.COACH, SoraPhase.ARCHITECT
    }),
    SoraPhase.SCOUT: frozenset({SoraPhase.SCOUT, SoraPhase.COACH, SoraPhase.ARCHITECT}),
    SoraPhase.COACH: frozenset({SoraPhase.COACH, SoraPhase.ARCHITECT}),
    SoraPhase.ARCHITECT: frozenset({SoraPhase.ARCHITECT, SoraPhase.EXECUTE}),
    SoraPhase.EXECUTE: frozenset({SoraPhase.EXECUTE, SoraPhase.SLICE_DONE}),
    SoraPhase.SLICE_DONE: frozenset({SoraPhase.SLICE_DONE, SoraPhase.ARCHITECT}),
}


@dataclass
class AgentState:
    phase: Phase = Phase.IDLE
    current_task: str = ""
    retry_count: int = 0
    steps_in_task: int = 0
    last_tool_hash: str = ""
    tool_repeat_count: int = 0        # consecutive same-hash count (stuck detection)
    last_checkpoint: str = ""
    provider_index: int = 0
    started_at: str = ""
    updated_at: str = ""
    error_message: str = ""
    plan_file: str = ""
    escalation_level: int = 0         # current escalation level (0-3)
    sora_phase: SoraPhase = SoraPhase.NONE
    current_slice: str = ""
    current_task_index: int = 0
    task_in_progress: bool = False  # True between "before-task" and "after-task" checkpoint saves

    def __setattr__(self, name: str, value: object) -> None:
        if name == "phase" and "phase" in self.__dict__:
            target: Phase = value  # type: ignore[assignment]
            if target not in _PHASE_VALID_NEXT.get(self.phase, frozenset()):
                raise StateTransitionError(self.phase.value, target.value)
        elif name == "sora_phase" and "sora_phase" in self.__dict__:
            sp: SoraPhase = value  # type: ignore[assignment]
            if sp not in _SORA_VALID_NEXT.get(self.sora_phase, frozenset()):
                raise StateTransitionError(self.sora_phase.value, sp.value)
        object.__setattr__(self, name, value)

    def to_json(self) -> str:
        d = asdict(self)
        d["phase"] = self.phase.value
        d["sora_phase"] = self.sora_phase.value
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, data: str) -> AgentState:
        try:
            d = json.loads(data)

            # Fuzzy-correct typo'd field names before enum conversion.
            # Example: "soraphase" → "sora_phase" (underscore stripped comparison).
            known = set(cls.__dataclass_fields__)
            unknown = [k for k in d if k not in known]
            if unknown:
                _norm = {f.replace("_", ""): f for f in known}
                for uk in unknown:
                    canonical = _norm.get(uk.replace("_", ""))
                    # Only apply correction when the canonical key isn't already present.
                    if canonical is not None and canonical not in d:
                        d[canonical] = d.pop(uk)

            d["phase"] = Phase(d.get("phase", "idle"))
            d["sora_phase"] = SoraPhase(d.get("sora_phase", "none"))
            return cls(**{k: v for k, v in d.items() if k in known})
        except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
            return cls()

    @classmethod
    def from_file(cls, path: Path) -> AgentState:
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
