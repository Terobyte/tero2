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
    """Lazy-loading registry that maps role names to :class:`Persona` objects."""

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        self._cache: dict[str, Persona] = {}
        self._resolved_cache: dict[str, Persona] = {}
        self._overrides: dict[str, str] = dict(overrides) if overrides else {}
        self._loaded = False

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
            return cached
        local_path = _LOCAL_PROMPTS_DIR / f"{role}.md"
        try:
            raw = local_path.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            persona = Persona(name=role, system_prompt=body, metadata=meta)
            self._resolved_cache[role] = persona
            return persona
        except FileNotFoundError:
            pass
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
