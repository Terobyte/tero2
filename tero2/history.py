"""Project run history — reads/writes ~/.tero2/history.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

HISTORY_FILE = Path.home() / ".tero2" / "history.json"
_VERSION = 1


@dataclass
class HistoryEntry:
    path: str
    name: str
    last_run: str  # ISO-8601 UTC
    last_plan: str | None
    run_count: int


def load_history() -> list[HistoryEntry]:
    try:
        raw = json.loads(HISTORY_FILE.read_text())
        return [HistoryEntry(**e) for e in raw.get("entries", [])]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def record_run(project_path: Path, plan_file: Path | None) -> None:
    entries = load_history()
    path_str = str(project_path.expanduser().resolve())
    name = project_path.name
    now = datetime.now(timezone.utc).isoformat()
    if plan_file:
        try:
            plan_str: str | None = str(plan_file.relative_to(project_path))
        except ValueError:
            plan_str = plan_file.name
    else:
        plan_str = None

    for entry in entries:
        if entry.path == path_str:
            entry.last_run = now
            entry.last_plan = plan_str
            entry.run_count += 1
            break
    else:
        entries.insert(0, HistoryEntry(
            path=path_str,
            name=name,
            last_run=now,
            last_plan=plan_str,
            run_count=1,
        ))

    entries.sort(key=lambda e: e.last_run, reverse=True)
    _write(entries[:20])


def trim_history(max_entries: int = 20) -> None:
    entries = load_history()
    if len(entries) > max_entries:
        _write(entries[:max_entries])


def _write(entries: list[HistoryEntry]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": _VERSION, "entries": [asdict(e) for e in entries]}
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(HISTORY_FILE)
