"""Autonomous bug-loop iteration 4.

Three real bugs in unexplored areas:

1. ChainCollectsClaudeFormatAsEmpty
   Where:  tero2/providers/chain.py, ``run_prompt_collected``.
   What:   ``run_prompt_collected`` iterates provider output messages and
           extracts text by probing ``msg["content"]`` or ``msg["text"]`` at
           the TOP level. Claude CLI (``--output-format stream-json``) yields
           messages shaped as
           ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``.
           No top-level ``content`` or ``text`` key exists, so
           ``run_prompt_collected`` returns an EMPTY STRING while the underlying
           model produced valid output. All downstream players
           (scout/architect/builder/verifier) then see empty output — scout
           writes an empty CONTEXT_MAP.md and returns success, builder
           reports success with no work done, etc.
   Fix:    Normalize the message shape before extracting text (walk into
           ``msg.message.content[i].text`` for assistant-shaped messages), or
           pipe the messages through the provider's stream normalizer first.

2. PersonaFrontmatterCRLFNotParsed
   Where:  tero2/persona.py, ``_FRONTMATTER_RE`` + ``_parse_frontmatter``.
   What:   The frontmatter regex requires LF line separators (``\\n``). A
           prompt file edited on Windows with CRLF line endings (``\\r\\n``)
           does NOT match the frontmatter pattern — the entire file including
           ``---`` delimiters is returned as the body, and the metadata dict
           is silently empty. The body then starts with ``---\\r\\nname:\\r\\n---\\r\\n``
           text that ends up in the LLM system prompt, confusing the model.
   Fix:    Either strip ``\\r`` from the input before matching, or adjust the
           regex to accept ``\\r?\\n`` as the line separator.

3. CircuitBreakerIsAvailableHasSideEffect
   Where:  tero2/circuit_breaker.py, ``CircuitBreaker.is_available``.
   What:   The ``is_available`` property calls ``check()`` which has side
           effects: when state is OPEN and the recovery timeout has elapsed,
           ``check()`` MUTATES the breaker — transitions state to HALF_OPEN
           and sets ``_trial_in_progress=True``. A caller that inspects
           ``is_available`` purely to log availability or decide routing (with
           no intent to actually issue a request) will consume the trial slot
           and cause the NEXT legitimate caller's check() to raise
           CircuitOpenError because ``_trial_in_progress`` is now True.
           ``is_available`` is named and documented as a query, but behaves
           as a state-mutating command.
   Fix:    ``is_available`` should peek without mutating — e.g. inspect
           state/last_failure_time directly, or pass a flag to ``check()`` to
           suppress the transition. Equivalent: a dedicated ``peek()`` method.
"""

from __future__ import annotations

import asyncio

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker
from tero2.errors import CircuitOpenError
from tero2.persona import _parse_frontmatter
from tero2.providers.chain import ProviderChain


# ── Bug A: Chain collects Claude-format as empty ─────────────────────────────


class _ClaudeMockProvider:
    """Mock provider that yields raw Claude stream-JSON events."""

    kind = "claude"
    display_name = "mock_claude"

    async def run(self, **kwargs):
        # Real Claude output shape per
        # tero2/providers/normalizers/claude.py header comment.
        yield {
            "type": "system",
            "tools": ["bash", "read", "edit"],
        }
        yield {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hello world from claude"},
                ]
            },
        }
        yield {"type": "result", "subtype": "success"}


class TestLoopIter4ChainCollectsClaudeFormatAsEmpty:
    """Bug A: run_prompt_collected returns '' for Claude-format messages."""

    def test_claude_format_text_is_captured_not_dropped(self):
        """The model emits 'hello world from claude' — run_prompt_collected
        must capture that string. Currently it returns ''."""

        async def _go() -> str:
            chain = ProviderChain(providers=[_ClaudeMockProvider()])
            return await chain.run_prompt_collected("irrelevant prompt")

        result = asyncio.run(_go())
        assert "hello world from claude" in result, (
            f"expected model output to be collected; got {result!r}. "
            "run_prompt_collected does not understand the Claude assistant-"
            "message shape (text nested at message.content[i].text)."
        )


# ── Bug B: Persona frontmatter CRLF not parsed ───────────────────────────────


class TestLoopIter4PersonaFrontmatterCRLFNotParsed:
    """Bug B: _parse_frontmatter fails on CRLF (Windows) line endings."""

    def test_crlf_frontmatter_parses_metadata_and_strips_delimiters(self):
        text = "---\r\nname: builder\r\nrole: engineer\r\n---\r\nBody begins here\r\n"
        meta, body = _parse_frontmatter(text)

        # Metadata must be extracted.
        assert meta == {"name": "builder", "role": "engineer"}, (
            f"CRLF frontmatter metadata was not parsed. meta={meta!r}. "
            "The frontmatter regex hard-codes '\\n' and fails on Windows line "
            "endings, silently dropping metadata keys."
        )

        # Body must not contain the frontmatter delimiters. If the regex fails
        # the whole file is returned as body, which would pollute the LLM
        # system prompt with yaml-looking text.
        assert "---" not in body, (
            f"CRLF frontmatter delimiters leaked into body. body={body!r}. "
            "A failed match returns the whole file as body."
        )
        assert "name: builder" not in body, (
            f"CRLF frontmatter lines leaked into body. body={body!r}."
        )


# ── Bug C: CircuitBreaker.is_available has side effects ──────────────────────


class TestLoopIter4CircuitBreakerIsAvailableHasSideEffect:
    """Bug C: is_available mutates state (OPEN -> HALF_OPEN + trial_in_progress)."""

    def test_is_available_does_not_mutate_state(self):
        """Query-named property must not alter state or block subsequent
        callers. Currently calling is_available on an OPEN breaker past its
        recovery window steals the trial slot."""
        cb = CircuitBreaker(
            name="provider_x",
            failure_threshold=1,
            recovery_timeout_s=0,  # immediate recovery for determinism
        )
        cb.record_failure()
        assert cb.state == CBState.OPEN

        # Inspect availability. Under current behaviour this transitions the
        # breaker to HALF_OPEN with _trial_in_progress=True.
        before_state = cb.state
        before_trial = cb._trial_in_progress
        _ = cb.is_available
        after_state = cb.state
        after_trial = cb._trial_in_progress

        assert (before_state, before_trial) == (after_state, after_trial), (
            "is_available mutated breaker: "
            f"before=(state={before_state}, trial={before_trial}) "
            f"after=(state={after_state}, trial={after_trial}). "
            "A query property must not alter internal state."
        )

    def test_is_available_does_not_starve_subsequent_caller(self):
        """If is_available mutates, the NEXT caller that actually wants to
        probe the provider via check() is blocked with CircuitOpenError
        because the trial slot was consumed."""
        cb = CircuitBreaker(
            name="provider_y",
            failure_threshold=1,
            recovery_timeout_s=0,
        )
        cb.record_failure()
        assert cb.state == CBState.OPEN

        # Caller #1: a logger, routing decision, telemetry probe, etc.
        assert cb.is_available is True

        # Caller #2: the actual code path that will run the provider.
        # This should succeed (the breaker is effectively available — we just
        # queried availability, no one used the trial yet).
        try:
            cb.check()
        except CircuitOpenError:
            pytest.fail(
                "is_available consumed the trial slot; the real caller's "
                "check() now raises CircuitOpenError even though no provider "
                "call has been made yet. is_available must be side-effect "
                "free so availability checks compose with real use."
            )
