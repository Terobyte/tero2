"""Tests for constants.PROJECT_SCAN_SKIP_DIRS."""

from tero2.constants import PROJECT_SCAN_SKIP_DIRS


def test_skip_dirs_is_frozenset():
    assert isinstance(PROJECT_SCAN_SKIP_DIRS, frozenset)


def test_skip_dirs_contains_expected():
    for d in (".git", ".venv", "node_modules", "__pycache__", "dist"):
        assert d in PROJECT_SCAN_SKIP_DIRS


def test_skip_dirs_scout_alias_is_same_object():
    """Scout imports PROJECT_SCAN_SKIP_DIRS as _SKIP_DIRS — must be same object."""
    from tero2.players.scout import _SKIP_DIRS
    assert _SKIP_DIRS is PROJECT_SCAN_SKIP_DIRS
