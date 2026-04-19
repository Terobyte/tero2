import json
from pathlib import Path

import pytest

from tero2.state import AgentState, Phase, SoraPhase


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
        with pytest.raises(ValueError, match="corrupted data"):
            AgentState.from_file(p)

    @pytest.mark.parametrize("payload", [b"[]", b"[1, 2, 3]", b'"x"', b"123", b"null", b"true"])
    def test_wrong_top_level_type(self, tmp_path: Path, payload: bytes) -> None:
        p = tmp_path / "state.json"
        _write(p, payload)
        with pytest.raises(ValueError, match="expected dict"):
            AgentState.from_file(p)

    def test_valid_state_round_trips(self, tmp_path: Path) -> None:
        s = AgentState(phase=Phase.RUNNING, current_task="do stuff", steps_in_task=3)
        p = tmp_path / "state.json"
        s.save(p)
        loaded = AgentState.from_file(p)
        assert loaded.phase == Phase.RUNNING
        assert loaded.current_task == "do stuff"
        assert loaded.steps_in_task == 3


class TestSoraPhase:
    def test_old_json_without_sora_phase_defaults_to_none(self) -> None:
        """STATE.json from before SoraPhase existed must deserialize cleanly."""
        old_json = json.dumps({"phase": "running", "current_task": "x"})
        state = AgentState.from_json(old_json)
        assert state.sora_phase == SoraPhase.NONE
        assert state.current_slice == ""
        assert state.current_task_index == 0

    @pytest.mark.parametrize("phase", list(SoraPhase))
    def test_round_trip_all_sora_phases(self, phase: SoraPhase) -> None:
        """Every SoraPhase value must survive a to_json → from_json cycle."""
        s = AgentState(sora_phase=phase, current_slice="S01", current_task_index=2)
        restored = AgentState.from_json(s.to_json())
        assert restored.sora_phase == phase
        assert restored.current_slice == "S01"
        assert restored.current_task_index == 2

    def test_sora_phase_is_str_enum(self) -> None:
        """SoraPhase members must compare equal to their string values."""
        assert SoraPhase.SCOUT == "scout"
        assert SoraPhase.EXECUTE.value == "execute"
        assert isinstance(SoraPhase.HARDENING, str)

    def test_sora_phase_serialized_as_string(self) -> None:
        """to_json must write sora_phase as a plain string, not an enum repr."""
        s = AgentState(sora_phase=SoraPhase.ARCHITECT)
        d = json.loads(s.to_json())
        assert d["sora_phase"] == "architect"
        assert isinstance(d["sora_phase"], str)
