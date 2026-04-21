"""Disk layer — CRUD for .sora/ directory structure."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tero2.state import AgentState


class DiskLayer:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.sora_dir = project_path / ".sora"
        self._metrics_lock = threading.Lock()
        # Per-instance, per-thread last-read tracking for delta-based write_metrics
        self._metrics_thread_local = threading.local()

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

    def _read_metrics_raw(self) -> dict:
        """Read metrics from disk without acquiring lock (caller holds lock)."""
        try:
            return json.loads(
                (self.sora_dir / "reports" / "metrics.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return {}

    def read_metrics(self) -> dict:
        with self._metrics_lock:
            data = self._read_metrics_raw()
            # Track what this thread last read (per-instance) for delta-based write_metrics
            self._metrics_thread_local.last_read = dict(data)
            return data

    def write_metrics(self, metrics: dict) -> None:
        if not hasattr(self._metrics_thread_local, "last_read"):
            raise ValueError(
                "write_metrics called without a prior read_metrics on this instance. "
                "Call read_metrics() first to establish a baseline."
            )
        last_read: dict = self._metrics_thread_local.last_read
        with self._metrics_lock:
            # Re-read current state under lock to prevent lost updates
            current = self._read_metrics_raw()
            # Apply delta: for numeric values, add (new - last_read) to current.
            # This makes the operation atomic: even if a thread read a stale value,
            # its actual increment contribution is correctly applied.
            for k, v in metrics.items():
                if isinstance(v, (int, float)):
                    delta = v - last_read.get(k, 0)
                    current[k] = current.get(k, 0) + delta
                else:
                    current[k] = v
            path = self.sora_dir / "reports" / "metrics.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(current, indent=2), encoding="utf-8")
            # Update thread-local so subsequent write_metrics in same thread use fresh baseline
            self._metrics_thread_local.last_read = dict(current)

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
        # Resolve symlinks and validate path stays within project_path
        try:
            resolved = path.resolve()
            project_resolved = self.project_path.resolve()
            if not str(resolved).startswith(str(project_resolved)):
                raise ValueError(
                    f"path traversal detected: {plan_file!r} resolves outside "
                    f"project directory ({project_resolved})"
                )
        except ValueError:
            raise
        except OSError:
            pass  # let the read below handle it
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return ""
