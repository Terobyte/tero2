"""Verifier player -- checks Builder output for correctness.

Runs ruff check and pytest as subprocesses to determine PASS/FAIL/ANOMALY.
Uses LLM only to analyze must-have coverage, not for the verdict itself.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any

from tero2.disk_layer import DiskLayer
from tero2.players.base import BasePlayer, PlayerResult
from tero2.providers.chain import ProviderChain

log = logging.getLogger(__name__)


class Verdict:
    PASS = "PASS"
    FAIL = "FAIL"
    ANOMALY = "ANOMALY"


@dataclass
class VerifierResult(PlayerResult):
    """Result from Verifier check."""

    verdict: str = ""
    ruff_output: str = ""
    pytest_output: str = ""
    failed_tests: list[str] = None  # type: ignore[assignment]
    must_haves_failed: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failed_tests is None:
            self.failed_tests = []
        if self.must_haves_failed is None:
            self.must_haves_failed = []


def _run_subprocess(cmd: list[str], cwd: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", f"command timed out: {' '.join(cmd)}"


class VerifierPlayer(BasePlayer):
    """Verify Builder output by running ruff and pytest."""

    role = "verifier"

    def __init__(
        self,
        chain: ProviderChain,
        disk: DiskLayer,
        *,
        working_dir: str = "",
    ) -> None:
        super().__init__(chain, disk, working_dir=working_dir)

    async def run(self, **kwargs: Any) -> VerifierResult:
        builder_output: str = kwargs.get("builder_output", "")
        task_id: str = kwargs.get("task_id", "T01")

        cwd = self.working_dir or "."
        try:
            ruff_rc, ruff_out, ruff_err = _run_subprocess(
                ["ruff", "check", "."],
                cwd,
            )
            pytest_rc, pytest_out, pytest_err = _run_subprocess(
                ["pytest", "-x"],
                cwd,
            )
            ruff_output = ruff_out + ruff_err
            pytest_output = pytest_out + pytest_err

            combined = ruff_output + "\n" + pytest_output
            verdict = _parse_verdict(combined, ruff_rc, pytest_rc)
            report = verdict + "\n" + combined
            failed_tests = _extract_list(pytest_output, "FAILED")
            must_haves_failed = _extract_list(builder_output, "must-haves")

            return VerifierResult(
                success=(verdict == Verdict.PASS),
                captured_output=report,
                verdict=verdict,
                ruff_output=ruff_output,
                pytest_output=pytest_output,
                failed_tests=failed_tests,
                must_haves_failed=must_haves_failed,
            )
        except Exception as exc:
            log.error("verifier failed for %s: %s", task_id, exc)
            return VerifierResult(
                success=False,
                verdict=Verdict.FAIL,
                error=str(exc),
            )


def _parse_verdict(output: str, ruff_rc: int, pytest_rc: int) -> str:
    if re.search(r"\bANOMALY\b", output, re.IGNORECASE):
        return Verdict.ANOMALY
    if ruff_rc != 0 or pytest_rc != 0:
        return Verdict.FAIL
    return Verdict.PASS


def _extract_list(output: str, label: str) -> list[str]:
    import re

    pattern = re.compile(rf"{re.escape(label)}\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    matches = pattern.findall(output)
    items: list[str] = []
    for m in matches:
        items.append(m.split(" - ")[0].strip())
    return items
