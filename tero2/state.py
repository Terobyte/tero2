"""Runtime state model for tero2."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


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

    def to_json(self) -> str:
        d = asdict(self)
        d["phase"] = self.phase.value
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, data: str) -> AgentState:
        try:
            d = json.loads(data)
            d["phase"] = Phase(d.get("phase", "idle"))
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
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
