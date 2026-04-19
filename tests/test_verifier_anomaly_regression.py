"""Regression: word 'anomaly' in output must NOT trigger ANOMALY verdict."""
from tero2.players.verifier import _parse_verdict, Verdict

def test_word_anomaly_in_output_is_pass():
    output = "test_anomaly_detection.py::test_foo PASSED\n# check for anomaly cases"
    assert _parse_verdict(output, [0, 0]) == Verdict.PASS

def test_word_anomaly_with_failures_is_fail():
    output = "test_anomaly_detection.py::test_foo FAILED"
    assert _parse_verdict(output, [0, 1]) == Verdict.FAIL

def test_negative_rc_is_anomaly():
    assert _parse_verdict("command timed out", [-1]) == Verdict.ANOMALY

def test_negative_rc_in_multi_command_is_anomaly():
    assert _parse_verdict("ok\ntimed out", [0, -1]) == Verdict.ANOMALY
