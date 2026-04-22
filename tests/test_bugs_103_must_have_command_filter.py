"""Bug 103: _extract_must_have_commands treated every backtick span as a command.

The markdown plan format uses backticks for both runnable commands and
plain identifiers/paths/type-signatures. The original ``_CMD_RE``
captured every backtick span indiscriminately, so must-haves like::

    - [ ] `stringy/utils.py` exists and defines `def reverse_string(s: str) -> str`
    - [ ] `tests/test_reverse_string.py` exists with …
    - [ ] `pytest tests/test_reverse_string.py -v` reports 3 passed

produced the command list
``['stringy/utils.py', 'def reverse_string(s: str) -> str',
'tests/test_reverse_string.py', 'pytest tests/test_reverse_string.py -v']``.
The verifier then tried to ``subprocess.run("stringy/utils.py")`` and failed
with ``Permission denied`` — observed live in night-loop iter-7 where every
task's verification blew up on the first non-command "command".

Fix: filter extracted spans through a whitelist of known executable
prefixes (``pytest``, ``python``, ``ruff``, ``go``, …) or the ``./``
relative-path prefix. Only real commands survive.
"""

from __future__ import annotations

import pytest

from tero2.phases.execute_phase import _extract_must_have_commands, _looks_like_command
from tero2.players.architect import Task


class TestLooksLikeCommand:
    """The command classifier must separate executable invocations from identifiers."""

    @pytest.mark.parametrize(
        "text",
        [
            "pytest tests/foo.py -v",
            "python3 -m pytest",
            "ruff check .",
            "go test ./...",
            "cargo build",
            "npm run lint",
            "./scripts/run.sh",
            "git status",
        ],
    )
    def test_real_command_accepted(self, text: str) -> None:
        assert _looks_like_command(text), f"{text!r} should be accepted as a command"

    @pytest.mark.parametrize(
        "text",
        [
            "stringy/utils.py",
            "reverse_string",
            "def reverse_string(s: str) -> str",
            "tests/test_reverse_string.py",
            "stringy/__init__.py",
            "dict[str, int]",
            "MyClass.method",
            "",
            "   ",
        ],
    )
    def test_identifier_rejected(self, text: str) -> None:
        assert not _looks_like_command(text), (
            f"{text!r} should NOT be accepted as a command"
        )


class TestExtractMustHaveCommands:
    """Extraction against the exact plan shape that broke iter-7."""

    def test_iter7_plan_yields_only_pytest_command(self) -> None:
        task = Task(
            id="T01",
            description="reverse_string",
            must_haves=[
                "`stringy/utils.py` exists and defines `def reverse_string(s: str) -> str`",
                "`stringy/__init__.py` re-exports `reverse_string` at package root",
                "`tests/test_reverse_string.py` exists with basic/empty/unicode tests",
                "`pytest tests/test_reverse_string.py -v` reports 3 passed",
            ],
        )
        cmds = _extract_must_have_commands(task)
        assert cmds == ["pytest tests/test_reverse_string.py -v"], (
            f"expected only the pytest command, got: {cmds!r}"
        )

    def test_file_path_alone_not_a_command(self) -> None:
        """Regression for iter-7 Permission denied: file path must not be extracted."""
        task = Task(
            id="T02",
            description="x",
            must_haves=["`stringy/utils.py` defines reverse_string"],
        )
        cmds = _extract_must_have_commands(task)
        assert cmds == [], (
            f"bare file-path backtick must not be treated as command, got: {cmds!r}"
        )

    def test_type_signature_not_a_command(self) -> None:
        task = Task(
            id="T03",
            description="x",
            must_haves=["`def reverse_string(s: str) -> str` is exported"],
        )
        assert _extract_must_have_commands(task) == []

    def test_mixed_keeps_only_runnable(self) -> None:
        task = Task(
            id="T04",
            description="x",
            must_haves=[
                "`foo.py` exists",
                "`pytest -x` reports all passing",
                "`python3 -m mypy foo.py` reports no errors",
                "`def foo() -> None` is the signature",
            ],
        )
        cmds = _extract_must_have_commands(task)
        assert cmds == ["pytest -x", "python3 -m mypy foo.py"]

    def test_relative_path_executable_kept(self) -> None:
        """./script.sh is explicit executable-path syntax — must be kept."""
        task = Task(
            id="T05",
            description="x",
            must_haves=["`./scripts/check.sh` exits zero"],
        )
        assert _extract_must_have_commands(task) == ["./scripts/check.sh"]

    def test_duplicates_deduplicated(self) -> None:
        task = Task(
            id="T06",
            description="x",
            must_haves=[
                "`pytest -v` passes",
                "`pytest -v` passes again",
            ],
        )
        assert _extract_must_have_commands(task) == ["pytest -v"]

    def test_empty_must_haves_returns_empty(self) -> None:
        task = Task(id="T07", description="x", must_haves=[])
        assert _extract_must_have_commands(task) == []


class TestBulletBranchRegression:
    """The pre-existing non-backtick bullet branch (`- pytest foo`) must still work."""

    def test_bullet_pytest_line_extracted(self) -> None:
        task = Task(
            id="T08",
            description="x",
            must_haves=["pytest tests/all.py passes"],
        )
        cmds = _extract_must_have_commands(task)
        assert "pytest tests/all.py passes" in cmds or cmds, (
            f"bullet-style command must still be extracted: {cmds!r}"
        )

    def test_bullet_cd_line_extracted(self) -> None:
        task = Task(
            id="T09",
            description="x",
            must_haves=["cd build && make test"],
        )
        cmds = _extract_must_have_commands(task)
        assert cmds, f"cd-prefixed command must survive: {cmds!r}"
