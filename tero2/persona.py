"""Persona registry — loads role-specific system prompts.

Loading priority (highest wins):
1. Constructor overrides (``overrides`` mapping).
2. Project-local ``.sora/prompts/{role}.md``.
3. Bundled ``tero2/prompts/{role}.md`` shipped with the package.

Prompt files may contain an optional YAML-like frontmatter block delimited by
``---``.  Metadata key-value pairs are extracted with a simple regex parser
(no PyYAML dependency).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib.resources import files as pkg_files
from pathlib import Path
from typing import Mapping

log = logging.getLogger(__name__)

_BUILTIN_ROLES = (
    "scout",
    "architect",
    "builder",
    "verifier",
    "coach",
    "reviewer_review",
    "reviewer_fix",
)

_LOCAL_PROMPTS_DIR = Path(".sora/prompts")

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\n(.*?)\n?---[ \t]*\n?(.*)",
    re.DOTALL,
)

_META_LINE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(.+)$")

_BUILTIN_CACHE: dict[str, Persona] | None = None
_PROMPTS_DIR: Path | None = None
_PROMPTS_DIR_RESOLVED: bool = False


def _get_prompts_dir() -> Path | None:
    global _PROMPTS_DIR, _PROMPTS_DIR_RESOLVED
    if _PROMPTS_DIR_RESOLVED:
        return _PROMPTS_DIR
    _PROMPTS_DIR_RESOLVED = True
    try:
        ref = pkg_files("tero2.prompts")
        base = Path(str(ref))
        if base.is_dir():
            _PROMPTS_DIR = base
            return base
    except (TypeError, ModuleNotFoundError, FileNotFoundError):
        pass
    fallback = Path(__file__).resolve().parent / "prompts"
    if fallback.is_dir():
        _PROMPTS_DIR = fallback
        return fallback
    return None


def _load_builtins_once() -> dict[str, Persona]:
    global _BUILTIN_CACHE
    if _BUILTIN_CACHE is not None:
        return _BUILTIN_CACHE
    cache: dict[str, Persona] = {}
    prompts_dir = _get_prompts_dir()
    if prompts_dir is not None:
        for role in _BUILTIN_ROLES:
            path = prompts_dir / f"{role}.md"
            try:
                raw = path.read_text(encoding="utf-8")
                meta, body = _parse_frontmatter(raw)
                cache[role] = Persona(name=role, system_prompt=body, metadata=meta)
            except FileNotFoundError:
                log.debug("no bundled prompt for role %s", role)
            except UnicodeDecodeError:
                # Disease 1: a corrupt bundled prompt must not wipe the
                # entire registry. Skip this role and continue loading others.
                log.warning("bundled prompt for role %s is not valid UTF-8 — skipping", role)
    _BUILTIN_CACHE = cache
    return cache


def clear_cache() -> None:
    global _BUILTIN_CACHE, _PROMPTS_DIR, _PROMPTS_DIR_RESOLVED
    _BUILTIN_CACHE = None
    _PROMPTS_DIR = None
    _PROMPTS_DIR_RESOLVED = False


@dataclass
class Persona:
    """A single role persona with its system prompt and metadata."""

    name: str
    system_prompt: str
    metadata: dict[str, str] = field(default_factory=dict)


_META_VALUE_MAX_LEN = 120
_META_DANGEROUS_RE = re.compile(r"ignore\s+previous\s+instructions?", re.IGNORECASE)


def _sanitize_meta_value(value: str) -> str:
    """Sanitize a frontmatter metadata value against prompt injection."""
    value = value[:_META_VALUE_MAX_LEN]
    value = _META_DANGEROUS_RE.sub("[REDACTED]", value)
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse optional ``---`` frontmatter from *text*.

    Returns ``(metadata_dict, body)``.  If no frontmatter is found the
    metadata dict is empty and *body* is the original *text*.
    """
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        lm = _META_LINE_RE.match(line.strip())
        if lm:
            meta[lm.group(1)] = _sanitize_meta_value(lm.group(2).strip())
    return meta, m.group(2)


class PersonaRegistry:
    """Lazy-loading registry that maps role names to :class:`Persona` objects.

    Args:
        overrides: Optional mapping ``{role: prompt_text}`` that short-circuits
            the project/bundled lookup entirely.
        project_path: Project root whose ``.sora/prompts/`` directory holds
            per-role overrides.  When omitted the registry falls back to the
            historical CWD-relative ``.sora/prompts`` path (bug 112
            back-compat) — but that only works when the process CWD is the
            project directory.  The runner should always pass this argument.
    """

    def __init__(
        self,
        overrides: Mapping[str, str] | None = None,
        *,
        project_path: Path | None = None,
    ) -> None:
        self._cache: dict[str, Persona] = {}
        self._resolved_cache: dict[str, Persona] = {}
        self._resolved_cache_mtime: dict[str, float] = {}  # Bug 231: track mtime for cache invalidation
        self._overrides: dict[str, str] = dict(overrides) if overrides else {}
        self._loaded = False
        self._project_path: Path | None = (
            Path(project_path) if project_path is not None else None
        )

    @property
    def _local_prompts_dir(self) -> Path:
        """Resolve the project-local prompts directory.

        Uses ``<project_path>/.sora/prompts`` when a project was supplied at
        construction time; otherwise falls back to the CWD-relative path
        preserved for legacy callers.
        """
        if self._project_path is not None:
            return self._project_path / ".sora" / "prompts"
        # Bug 279: log a warning when falling back to CWD-relative path,
        # so callers know they may be getting wrong files.
        log.warning(
            "PersonaRegistry: project_path not set — falling back to "
            "CWD-relative %s (pass project_path= to avoid this)",
            _LOCAL_PROMPTS_DIR,
        )
        return _LOCAL_PROMPTS_DIR

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for role, persona in _load_builtins_once().items():
            if role not in self._overrides:
                self._cache[role] = persona
        for role, prompt in self._overrides.items():
            meta, body = _parse_frontmatter(prompt)
            self._cache[role] = Persona(name=role, system_prompt=body, metadata=meta)

    def _resolve(self, role: str) -> Persona | None:
        if role in self._overrides:
            meta, body = _parse_frontmatter(self._overrides[role])
            return Persona(name=role, system_prompt=body, metadata=meta)
        cached = self._resolved_cache.get(role)
        if cached is not None:
            # Bug 231: record st_mtime for potential future invalidation/refresh.
            # mtime is tracked in _resolved_cache_mtime for callers that need
            # explicit invalidation (call del self._resolved_cache[role] to
            # force a reload, or call invalidate(role)).
            local_path = self._local_prompts_dir / f"{role}.md"
            try:
                current_mtime = local_path.stat().st_mtime
                self._resolved_cache_mtime[role] = current_mtime
            except OSError:
                pass
            return cached
        local_path = self._local_prompts_dir / f"{role}.md"
        try:
            raw = local_path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            persona = Persona(name=role, system_prompt=body, metadata=meta)
            self._resolved_cache[role] = persona
            try:
                self._resolved_cache_mtime[role] = local_path.stat().st_mtime
            except OSError:
                pass
            return persona
        except FileNotFoundError:
            pass
        except OSError:
            pass
        except UnicodeDecodeError:
            # Disease 1: a non-UTF-8 operator-written prompt override must
            # not crash role resolution; fall through to the bundled prompt.
            log.warning(
                "local prompt for role %s at %s is not valid UTF-8 — "
                "falling back to bundled prompt", role, local_path
            )
        self._ensure_loaded()
        persona = self._cache.get(role)
        if persona is not None:
            self._resolved_cache[role] = persona
        return persona

    def load(self, role: str) -> Persona:
        """Load the :class:`Persona` for *role* with full priority chain.

        Priority: overrides → ``.sora/prompts/{role}.md`` → bundled prompt.

        Raises:
            KeyError: when no prompt is found for *role*.
        """
        persona = self._resolve(role)
        if persona is None:
            raise KeyError(f"no prompt found for role '{role}'")
        return persona

    def load_or_default(self, role: str) -> Persona:
        """Same as :meth:`load` but returns an empty :class:`Persona` on miss."""
        persona = self._resolve(role)
        if persona is not None:
            return persona
        return Persona(name=role, system_prompt="", metadata={})

    def get(self, role: str) -> Persona:
        """Return the :class:`Persona` for *role* using the full priority chain.

        Priority: constructor overrides → ``.sora/prompts/{role}.md`` → bundled.
        Falls back to an empty persona when the role is unknown.
        """
        persona = self._resolve(role)
        if persona is not None:
            return persona
        return Persona(name=role, system_prompt="", metadata={})

    def load_all(self) -> dict[str, Persona]:
        """Return a snapshot of every loaded persona (role → Persona)."""
        self._ensure_loaded()
        return dict(self._cache)

    def available_roles(self) -> list[str]:
        self._ensure_loaded()
        return sorted(self._cache.keys())
