"""Tests for tero2.persona — Persona dataclass, PersonaRegistry, frontmatter parser."""

from __future__ import annotations

import pytest

from tero2.persona import (
    Persona,
    PersonaRegistry,
    _BUILTIN_ROLES,
    _parse_frontmatter,
    clear_cache,
)


class TestPersonaDataclass:
    def test_fields(self):
        p = Persona(name="scout", system_prompt="hello", metadata={"tier": "1"})
        assert p.name == "scout"
        assert p.system_prompt == "hello"
        assert p.metadata == {"tier": "1"}

    def test_default_metadata(self):
        p = Persona(name="x", system_prompt="y")
        assert p.metadata == {}


class TestFrontmatterParser:
    def test_no_frontmatter(self):
        meta, body = _parse_frontmatter("just content\n")
        assert meta == {}
        assert body == "just content\n"

    def test_simple_frontmatter(self):
        raw = "---\nname: Scout\ntier: 1\n---\nYou are Scout."
        meta, body = _parse_frontmatter(raw)
        assert meta == {"name": "Scout", "tier": "1"}
        assert body == "You are Scout."

    def test_frontmatter_strips_trailing_newline_after_delimiter(self):
        raw = "---\nkey: val\n---\nbody line\n"
        meta, body = _parse_frontmatter(raw)
        assert meta == {"key": "val"}
        assert body == "body line\n"

    def test_empty_frontmatter_block(self):
        raw = "---\n---\njust body"
        meta, body = _parse_frontmatter(raw)
        assert meta == {}
        assert body == "just body"

    def test_ignores_non_key_lines(self):
        raw = "---\nnot a kv line\nname: Builder\n---\ncontent"
        meta, body = _parse_frontmatter(raw)
        assert meta == {"name": "Builder"}
        assert body == "content"

    def test_value_with_colons(self):
        raw = "---\ndesc: a: b: c\n---\nbody"
        meta, body = _parse_frontmatter(raw)
        assert meta == {"desc": "a: b: c"}

    def test_key_with_hyphens_and_underscores(self):
        raw = "---\nmy-key: v1\nmy_key: v2\n---\nbody"
        meta, body = _parse_frontmatter(raw)
        assert meta == {"my-key": "v1", "my_key": "v2"}


class TestPersonaRegistryInit:
    def test_empty_registry_loads_builtins(self):
        reg = PersonaRegistry()
        roles = reg.available_roles()
        assert all(r in roles for r in _BUILTIN_ROLES)

    def test_override_prepopulates(self):
        reg = PersonaRegistry(overrides={"scout": "be curious"})
        p = reg.load("scout")
        assert isinstance(p, Persona)
        assert p.system_prompt == "be curious"


class TestLoad:
    def test_override_beats_builtin(self):
        reg = PersonaRegistry(overrides={"builder": "custom builder prompt"})
        p = reg.load("builder")
        assert p.system_prompt == "custom builder prompt"

    def test_returns_persona_object(self):
        reg = PersonaRegistry()
        p = reg.load("scout")
        assert isinstance(p, Persona)
        assert p.name == "scout"
        assert len(p.system_prompt) > 0

    def test_unknown_role_raises_key_error(self):
        reg = PersonaRegistry()
        with pytest.raises(KeyError, match="nonexistent"):
            reg.load("nonexistent")

    def test_override_with_frontmatter(self):
        raw = "---\ntier: 2\n---\ncustom prompt"
        reg = PersonaRegistry(overrides={"builder": raw})
        p = reg.load("builder")
        assert p.metadata == {"tier": "2"}
        assert p.system_prompt == "custom prompt"


class TestLoadOrDefault:
    def test_returns_persona_for_known_override(self):
        reg = PersonaRegistry(overrides={"coach": "coach prompt"})
        p = reg.load_or_default("coach")
        assert p.system_prompt == "coach prompt"

    def test_returns_empty_persona_for_unknown(self):
        reg = PersonaRegistry()
        p = reg.load_or_default("nope")
        assert isinstance(p, Persona)
        assert p.system_prompt == ""
        assert p.name == "nope"


class TestGet:
    def test_returns_cached_builtin(self):
        reg = PersonaRegistry()
        p = reg.get("scout")
        assert isinstance(p, Persona)
        assert p.name == "scout"

    def test_returns_empty_persona_for_unknown(self):
        reg = PersonaRegistry()
        p = reg.get("totally_fake_role_xyz")
        assert p.system_prompt == ""


class TestLoadAll:
    def test_includes_overrides(self):
        reg = PersonaRegistry(overrides={"builder": "build it"})
        snapshot = reg.load_all()
        assert snapshot["builder"].system_prompt == "build it"

    def test_snapshot_is_copy(self):
        reg = PersonaRegistry(overrides={"scout": "look"})
        snap = reg.load_all()
        snap["scout"] = Persona(name="scout", system_prompt="mutated")
        assert reg.load("scout").system_prompt == "look"


class TestAvailableRoles:
    def test_includes_override_roles(self):
        reg = PersonaRegistry(overrides={"custom_role": "hi"})
        roles = reg.available_roles()
        assert "custom_role" in roles

    def test_sorted_order(self):
        reg = PersonaRegistry(overrides={"z_role": "z", "a_role": "a"})
        roles = reg.available_roles()
        assert roles == sorted(roles)


class TestBuiltinRoles:
    def test_builtin_roles_tuple_is_not_empty(self):
        assert len(_BUILTIN_ROLES) > 0

    def test_expected_roles_present(self):
        for role in ("scout", "architect", "builder", "verifier", "coach"):
            assert role in _BUILTIN_ROLES

    def test_builtin_personas_have_prompts(self):
        clear_cache()
        reg = PersonaRegistry()
        for role in _BUILTIN_ROLES:
            p = reg.load(role)
            assert len(p.system_prompt) > 0, f"role {role} has empty prompt"


class TestInMemoryCaching:
    def test_builtins_loaded_once_across_instances(self):
        clear_cache()
        reg1 = PersonaRegistry()
        p1 = reg1.load("scout")
        import tero2.persona as mod

        assert mod._BUILTIN_CACHE is not None
        reg2 = PersonaRegistry()
        p2 = reg2.load("scout")
        assert p1.system_prompt == p2.system_prompt
        assert p1 is p2

    def test_clear_cache_resets(self):
        clear_cache()
        reg = PersonaRegistry()
        reg.load("scout")
        import tero2.persona as mod

        assert mod._BUILTIN_CACHE is not None
        clear_cache()
        assert mod._BUILTIN_CACHE is None
        assert mod._PROMPTS_DIR_RESOLVED is False


class TestLoadFromDisk:
    def test_loads_local_prompt_over_bundled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "scout.md").write_text(
            "---\ntier: local\n---\nlocal scout prompt", encoding="utf-8"
        )
        reg = PersonaRegistry()
        p = reg.load("scout")
        assert p.system_prompt == "local scout prompt"
        assert p.metadata == {"tier": "local"}

    def test_local_prompt_with_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "builder.md").write_text(
            "---\nversion: 2\ndesc: custom\n---\nmy custom builder",
            encoding="utf-8",
        )
        reg = PersonaRegistry()
        p = reg.load("builder")
        assert p.metadata["version"] == "2"
        assert p.metadata["desc"] == "custom"
        assert p.system_prompt == "my custom builder"

    def test_local_no_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "coach.md").write_text("plain coach content", encoding="utf-8")
        reg = PersonaRegistry()
        p = reg.load("coach")
        assert p.system_prompt == "plain coach content"
        assert p.metadata == {}


class TestFallbackToBundled:
    def test_falls_back_to_bundled_when_no_local(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        assert not (tmp_path / ".sora" / "prompts" / "scout.md").exists()
        reg = PersonaRegistry()
        p = reg.load("scout")
        assert len(p.system_prompt) > 0
        assert "Scout" in p.system_prompt

    def test_fallback_for_role_not_in_local_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "architect.md").write_text("local architect", encoding="utf-8")
        reg = PersonaRegistry()
        p_arch = reg.load("architect")
        assert p_arch.system_prompt == "local architect"
        p_builder = reg.load("builder")
        assert "Builder" in p_builder.system_prompt


class TestPriorityChain:
    def test_override_wins_over_local_and_bundled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "scout.md").write_text("local scout", encoding="utf-8")
        reg = PersonaRegistry(overrides={"scout": "override scout"})
        p = reg.load("scout")
        assert p.system_prompt == "override scout"

    def test_local_wins_over_bundled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "scout.md").write_text("local scout", encoding="utf-8")
        reg = PersonaRegistry()
        p = reg.load("scout")
        assert p.system_prompt == "local scout"

    def test_bundled_used_when_no_override_and_no_local(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        reg = PersonaRegistry()
        p = reg.load("architect")
        assert "Architect" in p.system_prompt


class TestResolvedCaching:
    def test_second_load_uses_resolved_cache(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clear_cache()
        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "scout.md").write_text("cached scout", encoding="utf-8")
        reg = PersonaRegistry()
        p1 = reg.load("scout")
        assert p1.system_prompt == "cached scout"
        (local_dir / "scout.md").write_text("changed scout", encoding="utf-8")
        p2 = reg.load("scout")
        assert p2.system_prompt == "cached scout"
        assert p1.system_prompt == p2.system_prompt
