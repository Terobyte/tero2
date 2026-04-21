"""Tests for player ↔ chain streaming integration.

Verifies that:
- BasePlayer._run_prompt calls chain.run_prompt_collected
- Players correctly build prompts and hand them to the chain
- Players return well-formed PlayerResult on success and on failure
- Players that accept stream chunks handle them correctly
- Player role attribute is set on each concrete class
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.disk_layer import DiskLayer
from tero2.players.base import BasePlayer, PlayerResult
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain


# ── helpers ───────────────────────────────────────────────────────────────────


class _StubProvider(BaseProvider):
    """Provider that yields a configurable reply."""

    def __init__(self, reply: str = "stub reply") -> None:
        self._reply = reply
        self.run_kwargs: list[dict] = []

    @property
    def display_name(self) -> str:
        return "stub"

    async def run(self, **kwargs: Any):
        self.run_kwargs.append(dict(kwargs))
        yield self._reply


def _make_chain(reply: str = "chain reply") -> ProviderChain:
    """Return a ProviderChain whose run_prompt_collected is an AsyncMock."""
    chain = ProviderChain([_StubProvider(reply)])
    chain.run_prompt_collected = AsyncMock(return_value=reply)  # type: ignore[attr-defined]
    return chain


def _make_disk(tmp_path: Path) -> DiskLayer:
    return DiskLayer(tmp_path)


# ── BasePlayer._run_prompt ────────────────────────────────────────────────────


class TestBasePlayerRunPrompt:
    async def test_run_prompt_calls_chain_run_prompt_collected(self, tmp_path: Path) -> None:
        """`_run_prompt` must delegate to `chain.run_prompt_collected`."""
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain("the answer")
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))

        result = await player._run_prompt("my prompt")

        chain.run_prompt_collected.assert_awaited_once_with("my prompt")  # type: ignore[attr-defined]
        assert result == "the answer"

    async def test_run_prompt_returns_str(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain("hello")
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player._run_prompt("q")
        assert isinstance(result, str)

    async def test_run_prompt_propagates_chain_exception(self, tmp_path: Path) -> None:
        """Exceptions from run_prompt_collected must bubble up unmodified."""
        from tero2.players.scout import ScoutPlayer
        from tero2.errors import RateLimitError

        chain = _make_chain()
        chain.run_prompt_collected = AsyncMock(side_effect=RateLimitError("all providers exhausted"))  # type: ignore[attr-defined]
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))

        with pytest.raises(RateLimitError):
            await player._run_prompt("x")


# ── PlayerResult ──────────────────────────────────────────────────────────────


class TestPlayerResultStructure:
    def test_success_true(self) -> None:
        r = PlayerResult(success=True, output_file="out.md", captured_output="text", error="")
        assert r.success is True
        assert r.output_file == "out.md"
        assert r.captured_output == "text"
        assert r.error == ""

    def test_success_false_with_error(self) -> None:
        r = PlayerResult(success=False, error="something went wrong")
        assert not r.success
        assert r.error == "something went wrong"

    def test_default_fields_are_empty_strings(self) -> None:
        r = PlayerResult(success=True)
        assert r.output_file == ""
        assert r.captured_output == ""
        assert r.error == ""


# ── ScoutPlayer integration ───────────────────────────────────────────────────


class TestScoutPlayerIntegration:
    async def test_success_result_has_correct_fields(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain("context map content")
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player.run(milestone_path="milestones/M001")

        assert result.success is True
        assert result.captured_output == "context map content"
        assert "CONTEXT_MAP.md" in result.output_file

    async def test_failure_result_on_chain_error(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain()
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("chain failed"))  # type: ignore[attr-defined]
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player.run(milestone_path="milestones/M001")

        assert result.success is False
        assert "chain failed" in result.error

    async def test_run_passes_prompt_to_chain(self, tmp_path: Path) -> None:
        """ScoutPlayer.run() must call chain.run_prompt_collected with a non-empty prompt."""
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain("anything")
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        await player.run(milestone_path="milestones/M001")

        call_args = chain.run_prompt_collected.call_args  # type: ignore[attr-defined]
        assert call_args is not None
        prompt_sent = call_args[0][0]
        assert isinstance(prompt_sent, str)
        assert len(prompt_sent) > 0


# ── BuilderPlayer integration ─────────────────────────────────────────────────


class TestBuilderPlayerIntegration:
    async def test_success_result(self, tmp_path: Path) -> None:
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain("# T0X-SUMMARY\n\nDone")
        player = BuilderPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player.run(
            task_id="T01",
            task_description="implement feature",
            milestone_path="milestones/M001",
        )
        assert result.success is True

    async def test_failure_result_on_chain_error(self, tmp_path: Path) -> None:
        from tero2.players.builder import BuilderPlayer
        from tero2.errors import ProviderError

        chain = _make_chain()
        chain.run_prompt_collected = AsyncMock(side_effect=ProviderError("provider died"))  # type: ignore[attr-defined]
        player = BuilderPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player.run(
            task_id="T01",
            task_description="something",
            milestone_path="milestones/M001",
        )
        assert result.success is False

    async def test_role_attribute(self, tmp_path: Path) -> None:
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain()
        player = BuilderPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        assert player.role == "builder"


# ── ArchitectPlayer integration ───────────────────────────────────────────────


class TestArchitectPlayerIntegration:
    async def test_role_attribute(self, tmp_path: Path) -> None:
        from tero2.players.architect import ArchitectPlayer

        chain = _make_chain()
        player = ArchitectPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        assert player.role == "architect"

    async def test_success_result_has_output_file(self, tmp_path: Path) -> None:
        from tero2.players.architect import ArchitectPlayer

        # Plan must have correct task headers (## T01:) and must-haves
        response = (
            "## T01: Implement the feature\n\n"
            "Must-haves:\n"
            "- Feature works correctly\n"
            "- Tests pass\n"
        )
        chain = _make_chain(response)
        player = ArchitectPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player.run(milestone_path="milestones/M001", context_map="")
        assert result.success is True
        assert result.output_file != ""

    async def test_failure_on_chain_error(self, tmp_path: Path) -> None:
        from tero2.players.architect import ArchitectPlayer

        chain = _make_chain()
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[attr-defined]
        player = ArchitectPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player.run(milestone_path="milestones/M001", context_map="")
        assert result.success is False


# ── chain stored on player instance ──────────────────────────────────────────


class TestPlayerChainAttribute:
    def test_chain_attribute_is_set(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain()
        player = ScoutPlayer(chain, _make_disk(tmp_path))
        assert player.chain is chain

    def test_disk_attribute_is_set(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        disk = _make_disk(tmp_path)
        chain = _make_chain()
        player = ScoutPlayer(chain, disk)
        assert player.disk is disk

    def test_working_dir_attribute_is_set(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain()
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir="/some/path")
        assert player.working_dir == "/some/path"

    def test_working_dir_defaults_to_empty_string(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain()
        player = ScoutPlayer(chain, _make_disk(tmp_path))
        assert player.working_dir == ""


# ── chain stream passthrough (no mock) ───────────────────────────────────────


class TestChainStreamPassthrough:
    async def test_player_receives_all_chunks_via_real_chain(self, tmp_path: Path) -> None:
        """Without mocking run_prompt_collected, the player collects all stream chunks."""
        from tero2.players.scout import ScoutPlayer

        class _MultiChunkProvider(BaseProvider):
            @property
            def display_name(self) -> str:
                return "multi"

            async def run(self, **kwargs: Any):
                yield "chunk1"
                yield "chunk2"
                yield "chunk3"

        chain = ProviderChain([_MultiChunkProvider()])
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        text = await player._run_prompt("test")
        # run_prompt_collected joins with "\n"
        assert "chunk1" in text
        assert "chunk2" in text
        assert "chunk3" in text

    async def test_mid_stream_error_dict_does_not_leak_into_output(self, tmp_path: Path) -> None:
        """An error-dict event from the provider must NOT appear in the collected output.

        The chain converts stream error-dicts into ProviderError and falls back
        to the next provider. The caller (player) must never see the raw error dict
        as content — it gets a clean string from the fallback provider instead.
        """
        from tero2.players.scout import ScoutPlayer

        class _ErrorDictProvider(BaseProvider):
            @property
            def display_name(self) -> str:
                return "bad"

            async def run(self, **kwargs: Any):
                yield {"type": "error", "error": {"message": "upstream broke"}}

        class _GoodProvider(BaseProvider):
            @property
            def display_name(self) -> str:
                return "good"

            async def run(self, **kwargs: Any):
                yield "clean answer"

        chain = ProviderChain(
            [_ErrorDictProvider(), _GoodProvider()],
            rate_limit_max_retries=0,
        )
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        text = await player._run_prompt("anything")

        assert "clean answer" in text
        assert "upstream broke" not in text
        assert "error" not in text.lower()

    async def test_run_prompt_collected_joins_chunks_with_newline(self, tmp_path: Path) -> None:
        """run_prompt_collected must join multiple string chunks with newlines."""
        from tero2.players.scout import ScoutPlayer

        class _TwoLineProvider(BaseProvider):
            @property
            def display_name(self) -> str:
                return "twoline"

            async def run(self, **kwargs: Any):
                yield "line_a"
                yield "line_b"

        chain = ProviderChain([_TwoLineProvider()])
        player = ScoutPlayer(chain, _make_disk(tmp_path), working_dir=str(tmp_path))
        result = await player._run_prompt("q")
        assert result == "line_a\nline_b"

    async def test_run_prompt_streaming_yields_individual_chunks(self, tmp_path: Path) -> None:
        """chain.run_prompt must yield individual chunks (not wait for full collection)."""

        class _ChunkProvider(BaseProvider):
            @property
            def display_name(self) -> str:
                return "chunks"

            async def run(self, **kwargs: Any):
                for i in range(4):
                    yield f"tok{i}"

        chain = ProviderChain([_ChunkProvider()])
        received: list[Any] = []
        async for chunk in chain.run_prompt("x"):
            received.append(chunk)

        assert received == ["tok0", "tok1", "tok2", "tok3"]
