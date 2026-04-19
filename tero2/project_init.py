"""Project initialization -- create project + .sora/ + git.

Creates the project under the configured projects_dir,
initializes git, and creates the .sora/ directory structure.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tero2.config import Config
from tero2.disk_layer import DiskLayer


def init_project(
    project_name: str,
    plan_content: str,
    config: Config,
) -> Path:
    """Create a new project and initialize .sora/.

    1. Sanitize project name (lowercase, replace spaces with hyphens)
    2. Create directory under config.projects_dir
    3. git init
    4. Create .sora/ structure via DiskLayer.init()
    5. Write plan to .sora/milestones/M001/ROADMAP.md

    NOTE: .sora/prompts/ is NOT created here -- persona/prompt system is
    deferred. Do not call copy_default_prompts() in MVP1.

    Args:
        project_name: Name for the project (from plan heading or user input).
        plan_content: The markdown plan to write.
        config: tero2 config (for projects_dir path).

    Returns:
        Path to the created project directory.

    Raises:
        FileExistsError: If project directory already exists.
    """
    safe_name = _sanitize_name(project_name)
    if not safe_name:
        raise ValueError(f"Project name {project_name!r} produces an empty directory name after sanitization")
    projects_dir = Path(config.projects_dir).expanduser().resolve()
    project_path = projects_dir / safe_name

    if project_path.exists():
        raise FileExistsError(f"Project directory already exists: {project_path}")

    project_path.mkdir(parents=True, exist_ok=False)

    # git init
    try:
        subprocess.run(
            ["git", "init"],
            cwd=str(project_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass  # non-fatal -- git init is optional

    # Create .sora/ structure
    disk = DiskLayer(project_path)
    disk.init()

    # Write plan to .sora/milestones/M001/ROADMAP.md
    plan_dir = project_path / ".sora" / "milestones" / "M001"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "ROADMAP.md"
    plan_path.write_text(plan_content, encoding="utf-8")

    return project_path


def _sanitize_name(name: str) -> str:
    """Convert project name to directory-safe format.

    "My Cool Project" -> "my-cool-project"
    """
    # Replace non-alphanumeric chars (except spaces and hyphens) with nothing
    cleaned = re.sub(r"[^\w\s-]", "", name)
    # Replace whitespace with hyphens, lowercase
    result = re.sub(r"[-\s]+", "-", cleaned).strip("-").lower()
    return result or "project"


def _extract_project_name(plan: str) -> str:
    """Extract project name from plan content.

    Uses first heading (# Title) or first non-empty line.
    """
    # Try first markdown heading
    heading_match = re.search(r"^#\s+(.+)$", plan, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()

    # Fall back to first non-empty line
    for line in plan.splitlines():
        stripped = line.strip()
        if stripped:
            # Remove markdown list/bullet prefixes
            cleaned = re.sub(r"^[-*]\s+", "", stripped)
            return cleaned

    return "untitled-project"
