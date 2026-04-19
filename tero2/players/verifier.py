"""Verifier player -- checks Builder output for correctness.

Runs project-configured commands (from ``[verifier].commands`` in config, or
extracted from the Task's must-haves) to determine PASS/FAIL/ANOMALY.
Falls back to ``ruff check . && pytest -x`` for Python projects when no
commands are provided.
"""

from __future__ import annotations

import logging
import re
import shlex
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


def _run_shell(cmd_str: str, cwd: str) -> tuple[int, str, str]:
    """Run a shell command string (supports &&, ||, cd, pipes)."""
    try:
        proc = subprocess.run(
            cmd_str,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            shell=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"command timed out: {cmd_str}"


_SHELL_OPS = re.compile(r"&&|\|\||[|;]|\bcd\b")


def _run_command(cmd_str: str, cwd: str) -> tuple[int, str, str]:
    """Dispatch to shell or subprocess based on command complexity."""
    if _SHELL_OPS.search(cmd_str):
        return _run_shell(cmd_str, cwd)
    try:
        parts = shlex.split(cmd_str)
    except ValueError:
        return _run_shell(cmd_str, cwd)
    return _run_subprocess(parts, cwd)


class VerifierPlayer(BasePlayer):
    """Verify Builder output by running project-configured test commands."""

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
        verify_commands: list[str] = kwargs.get("verify_commands", [])

        cwd = self.working_dir or "."
        try:
            if not verify_commands:
                # Python project fallback
                verify_commands = ["ruff check .", "pytest -x"]

            all_output: list[str] = []
            all_rc: list[int] = []
            for cmd_str in verify_commands:
                log.debug("verifier: running %r in %s", cmd_str, cwd)
                rc, out, err = _run_command(cmd_str, cwd)
                all_output.append(out + err)
                all_rc.append(rc)

            combined = "\n".join(all_output)
            verdict = _parse_verdict(combined, all_rc)
            report = verdict + "\n" + combined
            failed_tests = _extract_list(combined, "FAILED")
            must_haves_failed = _extract_list(builder_output, "must-haves")

            # Preserve ruff/pytest fields when using default Python commands.
            ruff_output = all_output[0] if len(all_output) > 0 else ""
            pytest_output = all_output[1] if len(all_output) > 1 else ""

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


def _parse_verdict(output: str, return_codes: list[int]) -> str:
    if re.search(r"\bANOMALY\b", output, re.IGNORECASE):
        return Verdict.ANOMALY
    if any(rc != 0 for rc in return_codes):
        return Verdict.FAIL
    return Verdict.PASS


def _extract_list(output: str, label: str) -> list[str]:
    pattern = re.compile(rf"{re.escape(label)}\s*(.+)$", re.MULTILINE | re.IGNORECASE)
    matches = pattern.findall(output)
    items: list[str] = []
    for m in matches:
        items.append(m.split(" - ")[0].strip())
    return items
