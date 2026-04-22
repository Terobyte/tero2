"""Bug 121: ``DiskLayer.read_file`` catches ``FileNotFoundError`` and
``OSError`` but not ``UnicodeDecodeError``. The latter is a
``ValueError`` subclass, not an ``OSError``, so a file containing bytes
invalid as UTF-8 propagates the exception out of ``read_file`` and
crashes any caller that expects a safe string-or-None contract.

This matters because several ``read_file`` targets are operator-written
— ``human/STEER.md``, ``human/OVERRIDE.md``, ``persistent/PROJECT.md``
— and a user pasting text from an editor defaulting to Windows-1252
(or Latin-1) can produce non-UTF-8 bytes. A long-running tero2 run
should not fall over because the operator saved a file with the wrong
encoding.

Fix: add ``except UnicodeDecodeError`` that returns ``""`` (mirroring
the existing ``except OSError`` branch's "degrade silently" contract).
Return value consistency lets ``read_steer``/``read_override``
continue to return ``""`` as documented and keeps the runtime cascade
alive.
"""

from __future__ import annotations

from pathlib import Path


def test_read_file_does_not_raise_on_non_utf8_bytes(tmp_path: Path) -> None:
    """The bug: a non-UTF-8 file crashes read_file instead of being
    treated as an unreadable file."""
    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(tmp_path)
    disk.init()
    # Windows-1252 bytes that are not valid UTF-8 (0x93/0x94 are smart
    # quotes in cp1252, invalid lead bytes in UTF-8).
    target = disk.sora_dir / "human" / "STEER.md"
    target.write_bytes(b"\x93steer directive\x94")

    # Broken code raises UnicodeDecodeError here.
    # Fixed code returns "" (same contract as other unreadable files).
    result = disk.read_file("human/STEER.md")

    assert result == "", (
        "bug 121: read_file must degrade to '' on non-UTF-8 content, "
        "mirroring the existing OSError branch. An operator saving "
        "STEER.md in cp1252 should not crash the runner. "
        f"got {result!r}"
    )


def test_read_steer_on_non_utf8_returns_empty(tmp_path: Path) -> None:
    """The observable caller contract: ``read_steer`` returns '' when
    STEER.md is present but unreadable as UTF-8. execute_phase and
    Coach both rely on this — they must not blow up because an
    operator saved in the wrong encoding."""
    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(tmp_path)
    disk.init()
    (disk.sora_dir / "human" / "STEER.md").write_bytes(b"\xff\xfe\xfdbad bytes")

    result = disk.read_steer()

    assert result == "", (
        "bug 121: read_steer must return '' on non-UTF-8 STEER.md — "
        "a runtime cascade depends on it. "
        f"got {result!r}"
    )


def test_read_override_on_non_utf8_returns_empty(tmp_path: Path) -> None:
    """Same as above for OVERRIDE.md — another operator-written file."""
    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(tmp_path)
    disk.init()
    (disk.sora_dir / "human" / "OVERRIDE.md").write_bytes(b"\x80\x81bad")

    result = disk.read_override()
    assert result == ""


def test_read_file_valid_utf8_still_works(tmp_path: Path) -> None:
    """Regression guard: normal UTF-8 content returns decoded string."""
    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(tmp_path)
    disk.init()
    (disk.sora_dir / "human" / "STEER.md").write_text(
        "steer directive with UTF-8: ü ö ä ё 日", encoding="utf-8"
    )

    result = disk.read_file("human/STEER.md")
    assert result == "steer directive with UTF-8: ü ö ä ё 日"


def test_read_file_missing_still_returns_none(tmp_path: Path) -> None:
    """Regression guard: FileNotFoundError contract (returns None) is
    preserved — only UnicodeDecodeError is added to the catch list."""
    from tero2.disk_layer import DiskLayer

    disk = DiskLayer(tmp_path)
    disk.init()
    assert disk.read_file("human/STEER.md") is None
