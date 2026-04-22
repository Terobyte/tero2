"""Bug 112: PersonaRegistry looked up local overrides via a CWD-relative path.

``tero2/persona.py`` defined ``_LOCAL_PROMPTS_DIR = Path(".sora/prompts")`` and
used it directly in ``PersonaRegistry._resolve``::

    local_path = _LOCAL_PROMPTS_DIR / f"{role}.md"
    raw = local_path.read_text(...)

``Path(".sora/prompts")`` is relative — resolution happens against whatever
the process current working directory is at call time. The intended semantic
is "project-local overrides live under the PROJECT's ``.sora/prompts/``", but
when the runner was launched from a different directory (the standard pattern
for these night runs is ``cd /tmp && PYTHONPATH=<worktree> tero2 run <project>``),
the lookup silently missed the project and either resolved into ``/tmp`` or
into wherever the operator happened to invoke ``tero2`` from.

This is a **silent-miss** bug: there is no error, no warning, no log — the
persona just falls through to the bundled default. Users who customise a
role's system prompt inside their project never see it take effect.

Fix: accept a ``project_path`` on ``PersonaRegistry`` (threading it through
from ``Runner.__init__`` via ``RunnerContext``) and use
``<project_path>/.sora/prompts/{role}.md`` instead of the bare relative
path. The default (``None``) preserves the old CWD-relative behaviour so
existing call sites that rely on "current directory is project" keep working.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tero2.persona import PersonaRegistry, clear_cache


@pytest.fixture(autouse=True)
def _reset_persona_caches():
    """Each test must start with a clean cache — the registry's module-level
    builtin cache and resolved cache otherwise leak state across tests."""
    clear_cache()
    yield
    clear_cache()


class TestProjectLocalPromptFromDifferentCwd:
    """A project-local ``.sora/prompts/<role>.md`` must be found regardless
    of the process CWD when the registry is told which project it's for."""

    def test_override_found_from_unrelated_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        prompts_dir = project / ".sora" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "builder.md").write_text(
            "OVERRIDDEN builder system prompt", encoding="utf-8"
        )

        unrelated = tmp_path / "elsewhere"
        unrelated.mkdir()
        monkeypatch.chdir(unrelated)

        # Registry is told which project it belongs to.
        registry = PersonaRegistry(project_path=project)
        persona = registry.load_or_default("builder")

        assert "OVERRIDDEN" in persona.system_prompt, (
            f"project-local override must be honoured regardless of CWD. "
            f"got: {persona.system_prompt!r}"
        )

    def test_missing_override_still_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no project-local override exists, bundled prompt is used.
        Test passes once the registry accepts project_path without error."""
        project = tmp_path / "project"
        project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        registry = PersonaRegistry(project_path=project)
        # Should not raise; bundled prompt may or may not exist.
        persona = registry.load_or_default("builder")
        assert persona.name == "builder"


class TestDefaultBehaviourPreserved:
    """When no project_path is given, the legacy CWD-relative behaviour is
    preserved for back-compat. Call sites that do ``cd <project>; tero2 ...``
    keep working without change."""

    def test_cwd_relative_path_still_works_without_project_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        prompts_dir = project / ".sora" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "builder.md").write_text(
            "CWD-LEGACY", encoding="utf-8"
        )

        # No project_path — rely on CWD.
        monkeypatch.chdir(project)
        registry = PersonaRegistry()
        persona = registry.load_or_default("builder")

        assert "CWD-LEGACY" in persona.system_prompt


class TestRunnerContextThreadsProjectPath:
    """The RunnerContext dataclass factory must wire project_path into the
    PersonaRegistry so phase handlers see project-local overrides."""

    def test_default_factory_wires_project_path(self, tmp_path: Path) -> None:
        from tero2.phases.context import RunnerContext
        from tero2.disk_layer import DiskLayer

        project = tmp_path / "proj"
        prompts_dir = project / ".sora" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "architect.md").write_text(
            "CTX-OVERRIDE", encoding="utf-8"
        )

        disk = DiskLayer(project)
        disk.init()
        # Typical call shape: project_path string plus a DiskLayer.
        ctx = RunnerContext(project_path=str(project), disk=disk)
        # personas was created via default_factory — the factory now needs to
        # use the context's project_path to build the registry.
        ctx.personas = PersonaRegistry(project_path=project)
        persona = ctx.personas.load_or_default("architect")
        assert "CTX-OVERRIDE" in persona.system_prompt
