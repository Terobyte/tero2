"""Tests for tero2.history — project run history persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import tero2.history as history_mod
from tero2.history import (
    HistoryEntry,
    _write,
    load_history,
    record_run,
    trim_history,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _make_entry(path: str, name: str, last_run: str, run_count: int = 1) -> HistoryEntry:
    return HistoryEntry(
        path=path,
        name=name,
        last_run=last_run,
        last_plan=None,
        run_count=run_count,
    )


def _write_raw(hist_file: Path, entries: list[dict]) -> None:
    hist_file.parent.mkdir(parents=True, exist_ok=True)
    hist_file.write_text(json.dumps({"version": 1, "entries": entries}))


# ── load_history ───────────────────────────────────────────────────────────


class TestLoadHistory:
    def test_returns_empty_on_missing_file(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(history_mod, "HISTORY_FILE", tmp_path / "missing.json")
        assert load_history() == []

    def test_returns_empty_on_invalid_json(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        hist_file.write_text("not-json{{{")
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        assert load_history() == []

    def test_returns_empty_on_empty_entries_key(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        _write_raw(hist_file, [])
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        assert load_history() == []

    def test_deserializes_single_entry(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        entry = {
            "path": "/foo/bar",
            "name": "bar",
            "last_run": "2024-01-01T00:00:00+00:00",
            "last_plan": "plan.md",
            "run_count": 3,
        }
        _write_raw(hist_file, [entry])
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        result = load_history()

        assert len(result) == 1
        assert result[0].path == "/foo/bar"
        assert result[0].name == "bar"
        assert result[0].run_count == 3
        assert result[0].last_plan == "plan.md"

    def test_deserializes_multiple_entries(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        entries = [
            {
                "path": f"/proj/{i}",
                "name": f"proj{i}",
                "last_run": f"2024-0{i + 1}-01T00:00:00+00:00",
                "last_plan": None,
                "run_count": i + 1,
            }
            for i in range(3)
        ]
        _write_raw(hist_file, entries)
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        result = load_history()

        assert len(result) == 3
        assert result[0].name == "proj0"
        assert result[2].run_count == 3

    def test_returns_empty_when_entries_key_missing(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        hist_file.write_text(json.dumps({"version": 1}))  # no "entries" key
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        assert load_history() == []

    def test_returns_empty_on_type_error_in_entries(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        # entries contains a non-dict item → HistoryEntry(**item) will TypeError
        hist_file.write_text(json.dumps({"version": 1, "entries": ["bad", "data"]}))
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        assert load_history() == []


# ── record_run ─────────────────────────────────────────────────────────────


class TestRecordRun:
    def test_creates_new_entry_for_unknown_project(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        project = tmp_path / "myproject"
        project.mkdir()
        record_run(project, plan_file=None)

        entries = load_history()
        assert len(entries) == 1
        assert entries[0].name == "myproject"
        assert entries[0].run_count == 1
        assert entries[0].last_plan is None

    def test_new_entry_has_run_count_one(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        project = tmp_path / "alpha"
        project.mkdir()

        record_run(project, plan_file=None)

        assert load_history()[0].run_count == 1

    def test_updates_existing_entry_increments_run_count(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        project = tmp_path / "beta"
        project.mkdir()

        record_run(project, plan_file=None)
        record_run(project, plan_file=None)
        record_run(project, plan_file=None)

        entries = load_history()
        assert len(entries) == 1
        assert entries[0].run_count == 3

    def test_records_plan_file_name(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        project = tmp_path / "gamma"
        project.mkdir()
        plan = tmp_path / "ROADMAP.md"
        plan.touch()

        record_run(project, plan_file=plan)

        assert load_history()[0].last_plan == "ROADMAP.md"

    def test_last_plan_is_none_when_not_provided(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        project = tmp_path / "delta"
        project.mkdir()

        record_run(project, plan_file=None)

        assert load_history()[0].last_plan is None

    def test_updates_last_run_timestamp(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        project = tmp_path / "epsilon"
        project.mkdir()

        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2024, 6, 1, tzinfo=timezone.utc)

        with patch("tero2.history.datetime") as mock_dt:
            mock_dt.now.return_value = t1
            record_run(project, plan_file=None)

        with patch("tero2.history.datetime") as mock_dt:
            mock_dt.now.return_value = t2
            record_run(project, plan_file=None)

        entry = load_history()[0]
        assert entry.last_run == t2.isoformat()

    def test_entries_sorted_by_last_run_descending(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        proj_a = tmp_path / "a_project"
        proj_a.mkdir()
        proj_b = tmp_path / "b_project"
        proj_b.mkdir()

        t_old = datetime(2024, 1, 1, tzinfo=timezone.utc)
        t_new = datetime(2024, 6, 1, tzinfo=timezone.utc)

        with patch("tero2.history.datetime") as mock_dt:
            mock_dt.now.return_value = t_old
            record_run(proj_a, plan_file=None)

        with patch("tero2.history.datetime") as mock_dt:
            mock_dt.now.return_value = t_new
            record_run(proj_b, plan_file=None)

        entries = load_history()
        assert entries[0].name == "b_project"
        assert entries[1].name == "a_project"

    def test_limits_to_20_entries(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        # pre-populate with 20 entries via _write so record_run sees them
        old_entries = [
            _make_entry(f"/old/{i}", f"old{i}", f"2023-0{(i % 9) + 1}-01T00:00:00+00:00")
            for i in range(20)
        ]
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        _write(old_entries)

        # add a 21st project
        proj_new = tmp_path / "newest"
        proj_new.mkdir()
        t_new = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with patch("tero2.history.datetime") as mock_dt:
            mock_dt.now.return_value = t_new
            record_run(proj_new, plan_file=None)

        assert len(load_history()) == 20

    def test_most_recent_entry_is_at_index_zero(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        for day in [1, 2, 3]:
            proj = tmp_path / f"day{day}"
            proj.mkdir(exist_ok=True)
            t = datetime(2024, 1, day, tzinfo=timezone.utc)
            with patch("tero2.history.datetime") as mock_dt:
                mock_dt.now.return_value = t
                record_run(proj, plan_file=None)

        assert load_history()[0].name == "day3"

    def test_path_stored_as_resolved_absolute(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        project = tmp_path / "myproj"
        project.mkdir()

        record_run(project, plan_file=None)

        entry = load_history()[0]
        assert Path(entry.path).is_absolute()
        assert entry.path == str(project.expanduser().resolve())


# ── trim_history ───────────────────────────────────────────────────────────


class TestTrimHistory:
    def test_no_change_when_at_or_below_max(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        entries = [_make_entry(f"/p/{i}", f"p{i}", f"2024-01-0{i + 1}T00:00:00+00:00") for i in range(5)]
        _write(entries)

        trim_history(max_entries=10)

        assert len(load_history()) == 5

    def test_trims_to_max_entries(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        entries = [_make_entry(f"/p/{i}", f"p{i}", f"2024-01-{i + 1:02d}T00:00:00+00:00") for i in range(15)]
        _write(entries)

        trim_history(max_entries=5)

        assert len(load_history()) == 5

    def test_keeps_newest_entries_after_trim(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        # Write in newest-first order, mirroring what record_run always produces.
        # p9 = Jan 10 (newest) … p0 = Jan 1 (oldest).
        entries = [
            _make_entry(f"/p/{i}", f"p{i}", f"2024-01-{i + 1:02d}T00:00:00+00:00")
            for i in range(9, -1, -1)
        ]
        _write(entries)

        trim_history(max_entries=3)

        remaining = load_history()
        # trim slices the first N from the newest-first file, so the 3 newest survive
        assert remaining[0].name == "p9"   # Jan 10 — newest
        assert remaining[2].name == "p7"   # Jan 8 — third-newest
        # oldest entries are gone
        assert all(e.name not in {"p0", "p1", "p2", "p3", "p4", "p5", "p6"} for e in remaining)

    def test_does_nothing_on_empty_history(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)

        trim_history(max_entries=5)  # no file yet, should not crash

        assert load_history() == []


# ── _write (internal) ──────────────────────────────────────────────────────


class TestWrite:
    def test_creates_parent_directory(self, tmp_path: Path, monkeypatch) -> None:
        nested = tmp_path / "deep" / "dir" / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", nested)
        _write([])
        assert nested.parent.is_dir()

    def test_written_file_is_valid_json(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        entry = _make_entry("/a/b", "b", "2024-01-01T00:00:00+00:00")
        _write([entry])
        data = json.loads(hist_file.read_text())
        assert "version" in data
        assert "entries" in data

    def test_written_file_contains_correct_entries(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        entry = _make_entry("/x/y", "y", "2024-05-01T00:00:00+00:00", run_count=7)
        _write([entry])
        data = json.loads(hist_file.read_text())
        assert len(data["entries"]) == 1
        assert data["entries"][0]["run_count"] == 7
        assert data["entries"][0]["name"] == "y"

    def test_atomic_write_leaves_no_tmp_file(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        _write([])
        tmp_file = hist_file.with_suffix(".tmp")
        assert not tmp_file.exists()

    def test_overwrites_existing_file(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        entry1 = _make_entry("/a", "a", "2024-01-01T00:00:00+00:00")
        _write([entry1])
        entry2 = _make_entry("/b", "b", "2024-06-01T00:00:00+00:00")
        _write([entry2])
        result = load_history()
        assert len(result) == 1
        assert result[0].name == "b"

    def test_writes_version_field(self, tmp_path: Path, monkeypatch) -> None:
        hist_file = tmp_path / "history.json"
        monkeypatch.setattr(history_mod, "HISTORY_FILE", hist_file)
        _write([])
        data = json.loads(hist_file.read_text())
        assert data["version"] == 1
