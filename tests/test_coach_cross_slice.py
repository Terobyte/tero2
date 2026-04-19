"""Coach _gather_context should include summaries from ALL slices, not just current."""
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from tero2.players.coach import CoachPlayer
from tero2.disk_layer import DiskLayer


def _make_disk(tmp_path: Path) -> DiskLayer:
    disk = DiskLayer(tmp_path)
    disk.sora_dir.mkdir(parents=True, exist_ok=True)
    return disk


def test_cross_slice_summaries_included(tmp_path: Path) -> None:
    """Summaries from S01 and S02 are both included when Coach runs for S02."""
    disk = _make_disk(tmp_path)
    chain = MagicMock()

    # Write summaries for S01 and S02
    disk.write_file("milestones/M001/S01/T01-SUMMARY.md", "S01 T01 summary content")
    disk.write_file("milestones/M001/S02/T01-SUMMARY.md", "S02 T01 summary content")

    player = CoachPlayer(chain, disk)
    ctx = player._gather_context("milestones/M001", "S02")

    assert "S01 T01 summary content" in ctx["task_summaries"]
    assert "S02 T01 summary content" in ctx["task_summaries"]


def test_summaries_include_slice_prefix(tmp_path: Path) -> None:
    """Each summary is prefixed with slice/task id for LLM readability."""
    disk = _make_disk(tmp_path)
    chain = MagicMock()
    disk.write_file("milestones/M001/S01/T01-SUMMARY.md", "content here")

    player = CoachPlayer(chain, disk)
    ctx = player._gather_context("milestones/M001", "S01")

    assert "S01/T01" in ctx["task_summaries"]


def test_no_slice_dirs_gives_empty_summaries(tmp_path: Path) -> None:
    """If no slice dirs exist, task_summaries is empty."""
    disk = _make_disk(tmp_path)
    chain = MagicMock()
    (disk.sora_dir / "milestones" / "M001").mkdir(parents=True, exist_ok=True)

    player = CoachPlayer(chain, disk)
    ctx = player._gather_context("milestones/M001", "S01")

    assert ctx["task_summaries"] == ""
