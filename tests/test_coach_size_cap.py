"""Coach _gather_context must cap total summary size at ~50KB."""
from pathlib import Path
from unittest.mock import MagicMock
from tero2.players.coach import CoachPlayer
from tero2.disk_layer import DiskLayer


def _make_disk(tmp_path: Path) -> DiskLayer:
    disk = DiskLayer(tmp_path)
    disk.sora_dir.mkdir(parents=True, exist_ok=True)
    return disk


def test_summary_size_cap_enforced(tmp_path: Path) -> None:
    """Total summaries in context must not exceed ~50KB (50_000 chars)."""
    disk = _make_disk(tmp_path)
    chain = MagicMock()

    # Write very large summaries across 2 slices
    big_content = "x" * 20_000
    for sid in ["S01", "S02", "S03"]:
        for i in range(1, 4):
            disk.write_file(
                f"milestones/M001/{sid}/T0{i}-SUMMARY.md", big_content
            )

    player = CoachPlayer(chain, disk)
    ctx = player._gather_context("milestones/M001", "S03")

    assert len(ctx["task_summaries"]) <= 50_200  # 50KB cap + minimal separator overhead
