"""Disk layer — CRUD for .sora/ directory structure."""

from __future__ import annotations

import json
from pathlib import Path

from tero2.state import AgentState


class DiskLayer:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.sora_dir = project_path / ".sora"

    def init(self) -> None:
        dirs = [
            "runtime",
            "strategic",
            "persistent",
            "milestones",
            "human",
            "prompts",
            "reports",
        ]
        for d in dirs:
            (self.sora_dir / d).mkdir(parents=True, exist_ok=True)

    def is_initialized(self) -> bool:
        return (self.sora_dir / "runtime").is_dir()

    def read_state(self) -> AgentState:
        return AgentState.from_file(self.sora_dir / "runtime" / "STATE.json")

    def write_state(self, state: AgentState) -> None:
        state.save(self.sora_dir / "runtime" / "STATE.json")

    @property
    def lock_path(self) -> Path:
        return self.sora_dir / "runtime" / "auto.lock"

    def read_file(self, relative_path: str) -> str | None:
        try:
            return (self.sora_dir / relative_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return ""

    def write_file(self, relative_path: str, content: str) -> None:
        path = self.sora_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def append_file(self, relative_path: str, content: str) -> None:
        path = self.sora_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)

    def read_metrics(self) -> dict:
        try:
            return json.loads(
                (self.sora_dir / "reports" / "metrics.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}

    def write_metrics(self, metrics: dict) -> None:
        path = self.sora_dir / "reports" / "metrics.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    def append_activity(self, event: dict) -> None:
        path = self.sora_dir / "reports" / "activity.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def read_override(self) -> str:
        return self.read_file("human/OVERRIDE.md") or ""

    def read_steer(self) -> str:
        return self.read_file("human/STEER.md") or ""

    def clear_override(self) -> None:
        (self.sora_dir / "human" / "OVERRIDE.md").unlink(missing_ok=True)

    def read_plan(self, plan_file: str) -> str:
        path = Path(plan_file)
        if not path.is_absolute():
            path = self.project_path / path
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return ""
