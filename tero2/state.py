"""Runtime state model for tero2."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from tero2.errors import StateTransitionError

log = logging.getLogger(__name__)


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


# Canonical phase execution order (excludes NONE which is a null-state).
# Import this instead of redeclaring the order in other modules.
SORA_PHASE_ORDER: list[SoraPhase] = [
    SoraPhase.HARDENING,
    SoraPhase.SCOUT,
    SoraPhase.COACH,
    SoraPhase.ARCHITECT,
    SoraPhase.EXECUTE,
    SoraPhase.SLICE_DONE,
]


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
    # SLICE_DONE → ARCHITECT: backward jump is safe here — re-plan for next slice
    # after the current one completes (multi-slice pipeline retry loop).
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
    tool_hash_updated: bool = False   # True once update_tool_hash has been called
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
        if name == "phase":
            target: Phase = value  # type: ignore[assignment]
            if "phase" in self.__dict__:
                # Subsequent assignment: validate the transition.
                if target not in _PHASE_VALID_NEXT.get(self.phase, frozenset()):
                    raise StateTransitionError(self.phase.value, target.value)
            else:
                # Initial assignment (dataclass __init__): only IDLE is a valid
                # starting phase.  COMPLETED is a terminal state that cannot be
                # reached without going through RUNNING first — coerce it so that
                # follow-on transition checks still fire correctly (A10).
                if target == Phase.COMPLETED:
                    value = Phase.RUNNING
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
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise ValueError(f"AgentState.from_json: corrupted data — {e}") from e

        if not isinstance(d, dict):
            raise ValueError(
                f"AgentState.from_json: expected dict, got {type(d).__name__}"
            )

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

        # Per-field enum coercion: a bad value in one field must not discard
        # all other valid fields by triggering a blanket except → cls().
        try:
            d["phase"] = Phase(d.get("phase", "idle"))
        except ValueError:
            d["phase"] = Phase.IDLE

        try:
            d["sora_phase"] = SoraPhase(d.get("sora_phase", "none"))
        except ValueError:
            d["sora_phase"] = SoraPhase.NONE

        try:
            # Use object.__new__ + object.__setattr__ to bypass the phase-transition
            # guard in __setattr__ — from_json must faithfully restore any terminal
            # state (e.g. Phase.COMPLETED) that was persisted to disk.
            import dataclasses
            instance = object.__new__(cls)
            for f in dataclasses.fields(cls):
                val = d.get(f.name, dataclasses.MISSING)
                if val is dataclasses.MISSING:
                    if f.default is not dataclasses.MISSING:
                        val = f.default
                    elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                        val = f.default_factory()  # type: ignore[misc]
                object.__setattr__(instance, f.name, val)
            return instance
        except (TypeError, KeyError, AttributeError) as e:
            raise ValueError(f"AgentState.from_json: invalid fields — {e}") from e

    @classmethod
    def from_file(cls, path: Path) -> AgentState:
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            log.warning("AgentState.from_file: cannot read %s — starting fresh", path)
            return cls()
        except ValueError as exc:
            # from_json raises ValueError on corrupted JSON or wrong shape.
            # Losing a STATE.json is bad, but crashing the runner on startup
            # is worse: prefer a fresh state with a loud warning so the next
            # save overwrites the bad file rather than leaving the agent
            # stuck in an import-time crash loop.
            log.error(
                "AgentState.from_file: corrupted state at %s (%s) — starting fresh",
                path,
                exc,
            )
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        try:
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        # Remember the last save path so touch() can persist without a path arg.
        # Uses object.__setattr__ to bypass the phase-guard __setattr__ and to
        # keep _last_path out of dataclasses.fields() → it won't appear in to_json().
        object.__setattr__(self, "_last_path", path)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
        last: Path | None = getattr(self, "_last_path", None)
        if last is not None:
            self.save(last)
