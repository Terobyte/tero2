"""Tests for tero2.project_init — project creation and initialization."""

from __future__ import annotations

from pathlib import Path

import pytest

from tero2.config import Config
from tero2.project_init import _extract_project_name, _sanitize_name, init_project


class TestSanitizedName:
    def test_spaces_to_hyphens(self):
        assert _sanitize_name("My Cool Project") == "my-cool-project"

    def test_special_chars_removed(self):
        assert _sanitize_name("Hello! @World#") == "hello-world"

    def test_multiple_hyphens_collapsed(self):
        assert _sanitize_name("a---b   c") == "a-b-c"

    def test_leading_trailing_hyphens_stripped(self):
        assert _sanitize_name("--hello--") == "hello"


class TestExtractProjectName:
    def test_first_heading(self):
        plan = "# Build Auth\nImplement JWT."
        assert _extract_project_name(plan) == "Build Auth"

    def test_no_heading_uses_first_line(self):
        plan = "Build the auth system\n- JWT tokens"
        assert _extract_project_name(plan) == "Build the auth system"

    def test_bullet_stripped(self):
        plan = "- Build auth\n- Implement JWT"
        assert _extract_project_name(plan) == "Build auth"

    def test_empty_plan_returns_untitled(self):
        assert _extract_project_name("") == "untitled-project"
        assert _extract_project_name("\n\n") == "untitled-project"


class TestInitProject:
    def test_creates_project_directory(self, tmp_path: Path):
        config = Config()
        config.projects_dir = str(tmp_path / "projects")
        result = init_project("test-proj", "# Test Plan", config)
        assert result.is_dir()
        assert result.name == "test-proj"

    def test_creates_sora_structure(self, tmp_path: Path):
        config = Config()
        config.projects_dir = str(tmp_path / "projects")
        result = init_project("test-proj", "# Test Plan", config)
        assert (result / ".sora").is_dir()
        assert (result / ".sora" / "runtime").is_dir()
        assert (result / ".sora" / "milestones").is_dir()

    def test_writes_plan_file(self, tmp_path: Path):
        config = Config()
        config.projects_dir = str(tmp_path / "projects")
        plan = "# Build Auth\nImplement JWT."
        result = init_project("test-proj", plan, config)
        plan_path = result / ".sora" / "milestones" / "M001" / "ROADMAP.md"
        assert plan_path.is_file()
        assert plan_path.read_text() == plan

    def test_raises_on_duplicate(self, tmp_path: Path):
        config = Config()
        config.projects_dir = str(tmp_path / "projects")
        init_project("test-proj", "# Plan", config)
        with pytest.raises(FileExistsError):
            init_project("test-proj", "# Plan Again", config)

    def test_name_sanitized(self, tmp_path: Path):
        config = Config()
        config.projects_dir = str(tmp_path / "projects")
        result = init_project("My Cool Project!", "# Plan", config)
        assert result.name == "my-cool-project"

    def test_git_initialized(self, tmp_path: Path):
        config = Config()
        config.projects_dir = str(tmp_path / "projects")
        result = init_project("test-proj", "# Plan", config)
        assert (result / ".git").is_dir()
