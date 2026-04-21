"""Tests for tero2.providers.cli — CLIProvider._stream_events streaming logic.

Coverage:
- Valid JSON dict lines are yielded as parsed dicts
- Non-dict JSON (list, string, number) raises ProviderError
- Unparseable lines are yielded as {"type": "text", "text": ...}
- Empty/blank lines are silently skipped
- turn_end event is always yielded at the end
- Non-zero exit code raises ProviderError
- stdout exception cancels stderr task before drain (A46 bug behaviour)
- Multiple valid events yielded in order
- Mixed valid/invalid JSON lines handled correctly
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tero2.errors import ProviderError
from tero2.providers.cli import CLIProvider


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProc:
    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_data: bytes = b"",
        returncode: int = 0,
        *,
        raise_on_stdout_iter: bool = False,
    ) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr(stderr_data)
        self.returncode = returncode
        self._raise_on_stdout_iter = raise_on_stdout_iter
        self.waited = False

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


class _RaisingStdout:
    def __init__(self, lines_before_error: list[bytes]) -> None:
        self._lines = list(lines_before_error)
        self._raised = False

    def __aiter__(self) -> _RaisingStdout:
        return self

    async def __anext__(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if not self._raised:
            self._raised = True
            raise RuntimeError("stdout broke")
        raise StopAsyncIteration


class _RaisingStderr:
    def __init__(self) -> None:
        self.was_cancelled = False

    async def read(self) -> bytes:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        return b""


class _RaisingProc:
    """Process whose stdout raises mid-stream; stderr blocks indefinitely."""

    def __init__(self) -> None:
        self.stdout = _RaisingStdout([b'{"type": "text"}\n'])
        self.stderr = _RaisingStderr()
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode


def _provider() -> CLIProvider:
    return CLIProvider("claude")


async def _collect(gen: Any) -> list[dict]:
    result = []
    async for item in gen:
        result.append(item)
    return result


class TestValidJsonDict:
    async def test_single_dict_yielded(self) -> None:
        proc = _FakeProc([b'{"type": "text", "content": "hello"}\n'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert len(events) == 2
        assert events[0] == {"type": "text", "content": "hello"}

    async def test_multiple_dicts_in_order(self) -> None:
        lines = [
            b'{"type": "text", "content": "first"}\n',
            b'{"type": "tool_use", "name": "bash"}\n',
            b'{"type": "tool_result", "output": "ok"}\n',
        ]
        proc = _FakeProc(lines)
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["content"] == "first"
        assert events[1]["name"] == "bash"
        assert events[2]["output"] == "ok"

    async def test_dict_with_nested_json(self) -> None:
        data = {"type": "tool_use", "input": {"cmd": "ls -la"}}
        proc = _FakeProc([json.dumps(data).encode() + b"\n"])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["input"]["cmd"] == "ls -la"


class TestNonDictJson:
    async def test_json_array_raises_provider_error(self) -> None:
        proc = _FakeProc([b"[1, 2, 3]\n"])
        p = _provider()
        with pytest.raises(ProviderError, match="non-dict"):
            await _collect(p._stream_events(proc))

    async def test_json_string_raises_provider_error(self) -> None:
        proc = _FakeProc([b'"hello"\n'])
        p = _provider()
        with pytest.raises(ProviderError, match="non-dict"):
            await _collect(p._stream_events(proc))

    async def test_json_number_raises_provider_error(self) -> None:
        proc = _FakeProc([b"42\n"])
        p = _provider()
        with pytest.raises(ProviderError, match="non-dict"):
            await _collect(p._stream_events(proc))

    async def test_json_null_raises_provider_error(self) -> None:
        proc = _FakeProc([b"null\n"])
        p = _provider()
        with pytest.raises(ProviderError, match="non-dict"):
            await _collect(p._stream_events(proc))

    async def test_json_boolean_raises_provider_error(self) -> None:
        proc = _FakeProc([b"true\n"])
        p = _provider()
        with pytest.raises(ProviderError, match="non-dict"):
            await _collect(p._stream_events(proc))


class TestUnparseableLines:
    async def test_plain_text_yielded_as_text_event(self) -> None:
        proc = _FakeProc([b"this is not json\n"])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0] == {"type": "text", "text": "this is not json"}

    async def test_partial_json_yielded_as_text_event(self) -> None:
        proc = _FakeProc([b'{"broken": "json\n'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["type"] == "text"
        assert "broken" in events[0]["text"]

    async def test_mixed_valid_and_invalid(self) -> None:
        lines = [
            b'{"type": "text", "content": "valid"}\n',
            b"not json at all\n",
            b'{"type": "tool_use", "name": "bash"}\n',
        ]
        proc = _FakeProc(lines)
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0] == {"type": "text", "content": "valid"}
        assert events[1] == {"type": "text", "text": "not json at all"}
        assert events[2] == {"type": "tool_use", "name": "bash"}


class TestEmptyLines:
    async def test_empty_line_skipped(self) -> None:
        proc = _FakeProc([b"\n"])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert len(events) == 1
        assert events[0]["type"] == "turn_end"

    async def test_whitespace_only_line_skipped(self) -> None:
        proc = _FakeProc([b"   \n"])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert len(events) == 1
        assert events[0]["type"] == "turn_end"

    async def test_blank_lines_between_valid_events(self) -> None:
        lines = [
            b'{"type": "text", "content": "a"}\n',
            b"\n",
            b"   \n",
            b'{"type": "text", "content": "b"}\n',
        ]
        proc = _FakeProc(lines)
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert len(events) == 3
        assert events[0]["content"] == "a"
        assert events[1]["content"] == "b"
        assert events[2]["type"] == "turn_end"


class TestTurnEnd:
    async def test_turn_end_always_appended(self) -> None:
        proc = _FakeProc([b'{"type": "text", "content": "hi"}\n'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        last = events[-1]
        assert last["type"] == "turn_end"
        assert last["text"] == ""

    async def test_turn_end_on_empty_output(self) -> None:
        proc = _FakeProc([])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert len(events) == 1
        assert events[0]["type"] == "turn_end"

    async def test_turn_end_present_after_error_event(self) -> None:
        data = {"type": "error", "message": "rate limited"}
        proc = _FakeProc([json.dumps(data).encode() + b"\n"])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["type"] == "error"
        assert events[-1]["type"] == "turn_end"


class TestNonZeroExitCode:
    async def test_exit_code_1_raises(self) -> None:
        proc = _FakeProc(
            [b'{"type": "text"}\n'],
            stderr_data=b"something failed",
            returncode=1,
        )
        p = _provider()
        with pytest.raises(ProviderError, match="exited 1"):
            await _collect(p._stream_events(proc))

    async def test_exit_code_137_raises(self) -> None:
        proc = _FakeProc(
            [],
            stderr_data=b"OOM killed",
            returncode=137,
        )
        p = _provider()
        with pytest.raises(ProviderError, match="exited 137"):
            await _collect(p._stream_events(proc))

    async def test_error_message_includes_stderr(self) -> None:
        proc = _FakeProc(
            [],
            stderr_data=b"detailed error info",
            returncode=2,
        )
        p = _provider()
        with pytest.raises(ProviderError, match="detailed error info"):
            await _collect(p._stream_events(proc))

    async def test_zero_exit_code_succeeds(self) -> None:
        proc = _FakeProc([b'{"type": "text"}\n'], returncode=0)
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert len(events) == 2


class TestNoStderr:
    async def test_no_stderr_works(self) -> None:
        proc = _FakeProc([b'{"type": "text"}\n'])
        proc.stderr = None
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["type"] == "text"


class TestLineEndingVariations:
    async def test_no_trailing_newline(self) -> None:
        proc = _FakeProc([b'{"type": "text", "content": "no newline"}'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["content"] == "no newline"

    async def test_carriage_return_newline(self) -> None:
        proc = _FakeProc([b'{"type": "text", "content": "crlf"}\r\n'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["content"] == "crlf"

    async def test_multiple_newlines_stripped(self) -> None:
        proc = _FakeProc([b'{"type": "text", "content": "x"}\n\n'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["content"] == "x"


class TestUnicodeHandling:
    async def test_unicode_content_in_json(self) -> None:
        data = {"type": "text", "content": "Привет мир"}
        proc = _FakeProc([json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n"])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["content"] == "Привет мир"

    async def test_unicode_in_plain_text_line(self) -> None:
        proc = _FakeProc(["Ошибочный вывод\n".encode("utf-8")])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert "Ошибочный" in events[0]["text"]

    async def test_invalid_utf8_replaced(self) -> None:
        proc = _FakeProc([b'\xff\xfe{"type": "text"}\n'])
        p = _provider()
        events = await _collect(p._stream_events(proc))
        assert events[0]["type"] == "text"


class TestStdoutExceptionCancelsStderr:
    async def test_stdout_error_propagates(self) -> None:
        proc = _RaisingProc()
        p = _provider()
        with pytest.raises(RuntimeError, match="stdout broke"):
            await _collect(p._stream_events(proc))

    async def test_no_events_yielded_after_stdout_error(self) -> None:
        proc = _RaisingProc()
        p = _provider()
        events: list[dict] = []
        with pytest.raises(RuntimeError):
            async for ev in p._stream_events(proc):
                events.append(ev)
        assert events == []

    async def test_stderr_task_cancelled_on_stdout_error(self) -> None:
        proc = _RaisingProc()
        p = _provider()
        with pytest.raises(RuntimeError):
            await _collect(p._stream_events(proc))
        assert proc.stderr.was_cancelled
