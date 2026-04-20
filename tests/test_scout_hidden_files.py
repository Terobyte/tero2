"""Scout _count_files must not count hidden files like .DS_Store, .env."""
import os
from pathlib import Path
from tero2.players.scout import _count_files


def test_hidden_files_not_counted(tmp_path: Path) -> None:
    """Hidden files (.DS_Store, .env) must not inflate file_count."""
    (tmp_path / ".DS_Store").write_text("binary junk")
    (tmp_path / ".env").write_text("SECRET=xyz")
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "utils.py").write_text("def f(): pass")

    assert _count_files(str(tmp_path)) == 2  # only main.py and utils.py


def test_visible_files_all_counted(tmp_path: Path) -> None:
    """Regular files are still counted correctly after the fix."""
    for name in ["a.py", "b.rs", "c.ts"]:
        (tmp_path / name).write_text("")
    assert _count_files(str(tmp_path)) == 3
