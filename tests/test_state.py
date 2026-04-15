from pathlib import Path

import pytest

from tero2.state import AgentState, Phase


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestFromFileCorruption:
    def test_nonexistent_file_returns_default(self, tmp_path: Path) -> None:
        result = AgentState.from_file(tmp_path / "nope.json")
        assert result == AgentState()

    def test_invalid_utf8(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        _write(p, b"\x80\x81\xfe\xff")
        assert AgentState.from_file(p) == AgentState()

    def test_malformed_json(self, tmp_path: Path) -> None:
        p = tmp_path / "state.json"
        _write(p, b"{bad json!!!")
        assert AgentState.from_file(p) == AgentState()

    @pytest.mark.parametrize("payload", [b"[]", b"[1, 2, 3]", b'"x"', b"123", b"null", b"true"])
    def test_wrong_top_level_type(self, tmp_path: Path, payload: bytes) -> None:
        p = tmp_path / "state.json"
        _write(p, payload)
        assert AgentState.from_file(p) == AgentState()

    def test_valid_state_round_trips(self, tmp_path: Path) -> None:
        s = AgentState(phase=Phase.RUNNING, current_task="do stuff", steps_in_task=3)
        p = tmp_path / "state.json"
        s.save(p)
        loaded = AgentState.from_file(p)
        assert loaded.phase == Phase.RUNNING
        assert loaded.current_task == "do stuff"
        assert loaded.steps_in_task == 3
