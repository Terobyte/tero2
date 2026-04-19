"""Verifier returns ANOMALY when subprocess times out or command not found."""
import subprocess
from unittest.mock import patch
from tero2.players.verifier import _run_subprocess, _parse_verdict, Verdict

def test_timeout_returns_negative_rc():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=120)):
        rc, out, err = _run_subprocess(["pytest", "-x"], cwd=".")
    assert rc == -1

def test_file_not_found_returns_negative_rc():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        rc, out, err = _run_subprocess(["nonexistent_command"], cwd=".")
    assert rc == -1

def test_parse_verdict_negative_rc_is_anomaly():
    assert _parse_verdict("", [-1]) == Verdict.ANOMALY
