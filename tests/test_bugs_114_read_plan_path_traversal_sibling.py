"""Bug 114: ``DiskLayer.read_plan`` path-traversal guard accepts sibling
directories whose name shares a prefix with the project root.

The guard is::

    resolved = path.resolve()
    project_resolved = self.project_path.resolve()
    if not str(resolved).startswith(str(project_resolved)):
        raise ValueError(...)

``str.startswith`` on a resolved path does not respect directory boundaries.
Given ``project_resolved = /tmp/myproject`` and a symlink or plan_file that
resolves to ``/tmp/myproject-evil/hack.md``, the check returns True because
``/tmp/myproject-evil/...`` starts with ``/tmp/myproject``. The unrelated
sibling directory leaks through.

Impact: a malicious plan or symlink inside a project — or a plan_file
argument crafted by a caller who does not fully control the path — can cause
``read_plan`` to return content from a sibling of the project directory. The
user's stated guarantee ("path stays within project_path") is violated.

Fix: use ``Path.is_relative_to`` instead of string ``startswith``.
``is_relative_to`` is path-segment aware — it checks each component, not the
raw bytes, so ``/tmp/myproject-evil`` is correctly NOT relative to
``/tmp/myproject``.

Per feedback_tdd_order.md: these tests are written before the fix. They are
expected to fail on the current (broken) code.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tero2.disk_layer import DiskLayer


class TestSiblingDirectoryRejected:
    """A path that resolves into a sibling directory (shared name prefix)
    must not be accepted as being "within" the project."""

    def test_sibling_via_symlink_raises(self, tmp_path: Path) -> None:
        """plan_file points to a symlink inside the project that resolves to
        a sibling directory. The sibling shares a name-prefix with the project."""
        project = tmp_path / "proj"
        project.mkdir()

        sibling = tmp_path / "proj-evil"
        sibling.mkdir()
        secret = sibling / "SECRET.md"
        secret.write_text("top secret sibling content", encoding="utf-8")

        # Symlink inside the project that points outward to the sibling.
        link_in_project = project / "inbound.md"
        os.symlink(secret, link_in_project)

        disk = DiskLayer(project)

        # The guard must reject this — the resolved target is in a different
        # directory, even though the string representation shares a prefix.
        with pytest.raises(ValueError, match="traversal"):
            disk.read_plan("inbound.md")

    def test_sibling_via_absolute_path_raises(self, tmp_path: Path) -> None:
        """plan_file is an absolute path into a sibling directory. Since
        sibling's absolute path shares the prefix, the broken startswith
        guard lets it through."""
        project = tmp_path / "proj"
        project.mkdir()

        sibling = tmp_path / "proj-evil"
        sibling.mkdir()
        secret = sibling / "secret-plan.md"
        secret.write_text("leaked from sibling", encoding="utf-8")

        disk = DiskLayer(project)

        with pytest.raises(ValueError, match="traversal"):
            disk.read_plan(str(secret))


class TestInsideProjectStillAllowed:
    """The fix must not regress legitimate reads inside the project."""

    def test_plain_relative_plan_inside_project(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        plan = project / "plan.md"
        plan.write_text("# plan\n", encoding="utf-8")

        disk = DiskLayer(project)
        assert disk.read_plan("plan.md") == "# plan\n"

    def test_absolute_path_inside_project(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        plan = project / "subdir" / "plan.md"
        plan.parent.mkdir()
        plan.write_text("# absolute plan\n", encoding="utf-8")

        disk = DiskLayer(project)
        assert disk.read_plan(str(plan)) == "# absolute plan\n"

    def test_nested_subdir_plan(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        deep = project / "a" / "b" / "c"
        deep.mkdir(parents=True)
        plan = deep / "plan.md"
        plan.write_text("nested", encoding="utf-8")

        disk = DiskLayer(project)
        assert disk.read_plan("a/b/c/plan.md") == "nested"

    def test_project_named_same_as_prefix(self, tmp_path: Path) -> None:
        """A project literally named ``proj`` must still work — the fix must
        not over-reject."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / "plan.md").write_text("ok", encoding="utf-8")
        disk = DiskLayer(project)
        assert disk.read_plan("plan.md") == "ok"


class TestParentDirTraversalStillRejected:
    """Regression guard: ``..``-style escape is still rejected (this was
    covered by the old startswith check too, so the fix must not regress)."""

    def test_dotdot_relative_escape(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        project.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("outside", encoding="utf-8")

        disk = DiskLayer(project)
        with pytest.raises(ValueError, match="traversal"):
            disk.read_plan("../outside.md")
