"""Tests for run_prompt_collected() and get_model_context_limit() in chain.py."""

from __future__ import annotations

from typing import Any

import pytest

from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain, get_model_context_limit


# ── helpers ──────────────────────────────────────────────────────────


class _YieldsStr(BaseProvider):
    """Yields raw string messages."""

    @property
    def display_name(self) -> str:
        return "yields-str"

    async def run(self, **kwargs: Any):
        yield "hello"
        yield " world"


class _YieldsDict(BaseProvider):
    """Yields dict messages with a 'content' key."""

    @property
    def display_name(self) -> str:
        return "yields-dict"

    async def run(self, **kwargs: Any):
        yield {"content": "from dict"}


class _YieldsDictText(BaseProvider):
    """Yields dict messages with a 'text' key (no 'content')."""

    @property
    def display_name(self) -> str:
        return "yields-dict-text"

    async def run(self, **kwargs: Any):
        yield {"text": "from text key"}


class _YieldsObj(BaseProvider):
    """Yields object messages with a .content attribute."""

    @property
    def display_name(self) -> str:
        return "yields-obj"

    async def run(self, **kwargs: Any):
        class _Msg:
            content = "from object"

        yield _Msg()


class _YieldsObjText(BaseProvider):
    """Yields object messages with a .text attribute (no .content)."""

    @property
    def display_name(self) -> str:
        return "yields-obj-text"

    async def run(self, **kwargs: Any):
        class _Msg:
            text = "from obj text"

        yield _Msg()


class _YieldsEmpty(BaseProvider):
    """Yields a dict with empty content — should produce no parts."""

    @property
    def display_name(self) -> str:
        return "yields-empty"

    async def run(self, **kwargs: Any):
        yield {"content": ""}
        yield {"text": ""}


# ── run_prompt_collected() ────────────────────────────────────────────


class TestRunPromptCollected:
    async def test_collects_str_messages(self):
        chain = ProviderChain([_YieldsStr()])
        result = await chain.run_prompt_collected("ping")
        assert result == "hello\n world"

    async def test_collects_dict_content(self):
        chain = ProviderChain([_YieldsDict()])
        result = await chain.run_prompt_collected("ping")
        assert result == "from dict"

    async def test_collects_dict_text_fallback(self):
        chain = ProviderChain([_YieldsDictText()])
        result = await chain.run_prompt_collected("ping")
        assert result == "from text key"

    async def test_collects_obj_content_attr(self):
        chain = ProviderChain([_YieldsObj()])
        result = await chain.run_prompt_collected("ping")
        assert result == "from object"

    async def test_collects_obj_text_attr(self):
        chain = ProviderChain([_YieldsObjText()])
        result = await chain.run_prompt_collected("ping")
        assert result == "from obj text"

    async def test_empty_content_skipped(self):
        chain = ProviderChain([_YieldsEmpty()])
        result = await chain.run_prompt_collected("ping")
        assert result == ""

    async def test_returns_str_type(self):
        chain = ProviderChain([_YieldsStr()])
        result = await chain.run_prompt_collected("any")
        assert isinstance(result, str)


# ── get_model_context_limit() ─────────────────────────────────────────


class TestGetModelContextLimit:
    @pytest.mark.parametrize(
        "model, expected",
        [
            ("claude-3-opus", 200_000),
            ("claude-3-5-sonnet-20241022", 200_000),
            ("gemini-1.5-pro", 1_000_000),
            ("gemini-2.0-flash", 1_000_000),
            ("gpt-4o", 128_000),
            ("gpt-4-turbo", 128_000),
            ("deepseek-v3", 128_000),
            ("qwen2.5-coder", 128_000),
            ("glm-4", 128_000),
            ("mimo-7b", 128_000),
        ],
    )
    def test_known_models(self, model: str, expected: int):
        assert get_model_context_limit(model) == expected

    def test_unknown_model_returns_default(self):
        assert get_model_context_limit("totally-unknown-model-xyz") == 128_000

    def test_case_insensitive(self):
        assert get_model_context_limit("CLAUDE-3") == 200_000
        assert get_model_context_limit("Gemini-Pro") == 1_000_000

    def test_returns_int(self):
        assert isinstance(get_model_context_limit("claude"), int)
