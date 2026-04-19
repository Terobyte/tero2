from pathlib import Path
import pytest
from tero2.config_writer import write_global_config_section


def test_write_creates_file(tmp_path):
    target = tmp_path / "config.toml"
    write_global_config_section(target, "telegram", {"enabled": True, "bot_token": "tok"})
    assert target.exists()
    content = target.read_text()
    assert "[telegram]" in content
    assert "enabled = true" in content


def test_write_is_atomic(tmp_path):
    """No .tmp file left behind after write."""
    target = tmp_path / "config.toml"
    write_global_config_section(target, "roles.builder", {"provider": "claude", "model": "sonnet"})
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_write_preserves_other_sections(tmp_path):
    """Writing one section doesn't wipe other sections."""
    target = tmp_path / "config.toml"
    target.write_text('[sora]\nmax_slices = 10\n')
    write_global_config_section(target, "telegram", {"enabled": False})
    content = target.read_text()
    assert "[sora]" in content
    assert "max_slices" in content
    assert "[telegram]" in content


def test_write_nested_table_roundtrips(tmp_path):
    """Regression: nested sections must render as [a.b], not [b] — and re-reads
    cleanly after 5 consecutive writes (the ProvidersPickScreen case)."""
    import tomllib
    target = tmp_path / "config.toml"
    for role in ("builder", "architect", "scout", "verifier", "coach"):
        write_global_config_section(
            target, f"roles.{role}", {"provider": "claude", "model": "sonnet"}
        )
    parsed = tomllib.loads(target.read_text())
    assert set(parsed["roles"].keys()) == {
        "builder", "architect", "scout", "verifier", "coach"
    }
    assert parsed["roles"]["builder"]["provider"] == "claude"
