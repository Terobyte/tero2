# Telegram Dispatcher Stage 2 — Architect Mode + Deepgram Voice Input Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two capabilities on top of the Stage 1 dispatcher: (1) **Architect mode** — `/architect` opens a persistent Claude Code session (`claude --resume <session_id>`) tunneled through Telegram so the user can chat with Opus about the project from their phone; (2) **Voice input** — Telegram voice messages are transcribed via the user's existing **Deepgram STT engine** (`/Users/terobyte/Desktop/Projects/Active/tts/deepgram/`) and routed through the same dispatcher pipeline as text.

**Architecture:** Architect mode stores `state.architect_session_id` + `state.architect_proc` on `ChatState`; every user message in `ARCHITECT` mode spawns `claude --resume <id> -p <text>` as a subprocess (argv list, no shell), streams stdout line-by-line into Telegram with 3000-char chunking, and is reaped on bot shutdown. Voice input downloads the `.ogg` from Telegram, POSTs it to Deepgram's pre-recorded endpoint (`POST /v1/listen`), and feeds the transcript into the text pipeline. The existing Node/TS engine at `tts/deepgram/` is the reference implementation — tero2 calls Deepgram's HTTP API directly from Python (single-file transcription, no WebSocket streaming needed for complete Telegram voice messages).

**Tech Stack:** Python asyncio, `httpx` (Deepgram REST), `asyncio.create_subprocess_exec` (Claude), existing `tero2/dispatcher/` from Stage 1. No new SDK dependencies.

**Spec reference:** `docs/superpowers/specs/2026-04-20-telegram-dispatcher-design.md` §2.1–§2.5. This plan overrides the spec on **one point**: voice STT is **Deepgram REST** (`model=nova-3` or `nova-2`), not Gemini native audio. The `Brain.transcribe()` method on `KimiBrain` stays `NotImplementedError`; transcription lives in a dedicated `tero2/dispatcher/stt.py` module so it's brain-agnostic.

---

## File Structure

```
tero2/dispatcher/
├── stt.py                              # NEW — DeepgramSTT (file → transcript)
└── brains/
    ├── claude_code.py                  # REPLACED — was Stage 1 stub; now session_id validator + send_to_architect

tero2/telegram_input.py                 # MODIFIED — voice handler, /architect, /done, _handle_architect_message
tero2/dispatcher/state_machine.py       # MODIFIED — architect_proc field (typed as asyncio.subprocess.Process | None)
tero2/config.py                         # MODIFIED — Deepgram env + existing Stage 2 fields (architect_enabled, claude_binary, output_flush_chars)

tests/dispatcher/
├── test_stt_deepgram.py                # NEW — mocked httpx → deepgram happy path, 4xx, timeout, empty transcript
├── test_architect_session.py           # NEW — mocked subprocess → session_id extraction, resume, /done cleanup
└── test_voice_handler.py               # NEW — mocked STT + routing: voice → transcript → dispatcher
```

---

## Decision Deltas vs Spec

1. **STT = Deepgram REST API** (not Gemini native audio). The canonical reference is the user's Node engine at `/Users/terobyte/Desktop/Projects/Active/tts/deepgram/` — same API family (`wss://api.deepgram.com/v1/listen` for streaming, `https://api.deepgram.com/v1/listen` for pre-recorded). For Telegram voice (complete file, seconds-long), we use the **pre-recorded REST endpoint**, not WebSocket.
2. **STT lives in `stt.py`, not on the Brain.** Rationale: brain-agnostic — Stage 3 local Gemma takes audio directly (see Stage 3 plan), so we want the dispatcher to call `stt.transcribe_file(path)` in Stage 2 and skip that call entirely in Stage 3 when the brain supports multimodal.
3. **Env var:** `DEEPGRAM_API_KEY`. Config knob: `cfg.dispatcher.stt_provider: str = "deepgram"` (adds one field to `DispatcherConfig`; defaulting to `"deepgram"` keeps Stage 2 turn-key).
4. **Architect output stays text-only** (spec §2.4 — no TTS for dispatcher free-text). Unchanged.

---

## Prereq Checks

- [ ] Stage 1 shipped and tagged `dispatcher-stage1`; `uv run pytest tests/dispatcher/ -q` green.
- [ ] `DEEPGRAM_API_KEY` env var set (same key the Node engine uses).
- [ ] `claude` binary on `PATH` in the environment where tero2 runs (`which claude`).
- [ ] A scratch Claude project to test `/architect` against.

---

## Task 1 — Config additions

**Files:**
- Modify: `tero2/config.py`
- Modify: `tests/dispatcher/test_config.py`

- [ ] **Step 1: Test** that `DispatcherConfig` now has `architect_enabled: bool = False`, `claude_binary: str = "claude"`, `output_flush_chars: int = 3000`, `stt_provider: str = "deepgram"`, `stt_api_key_env: str = "DEEPGRAM_API_KEY"`, `stt_model: str = "nova-3"`, `stt_language: str = "multi"`. (Some fields were declared in Stage 1 as forward-compat stubs; this task exercises them.)

- [ ] **Step 2:** Add missing fields; wire `[dispatcher.stt]` subsection to the loader so TOML `stt_model = "nova-2"` works if a user wants to downgrade.

- [ ] **Step 3:** PASS. `git commit -m "dispatcher: stage 2 config (deepgram + architect knobs)"`

---

## Task 2 — DeepgramSTT module

**Files:**
- Create: `tero2/dispatcher/stt.py`
- Create: `tests/dispatcher/test_stt_deepgram.py`

- [ ] **Step 1: Write tests** (all mocked with `respx`; no real network):
  1. Happy path — POST succeeds, JSON has `results.channels[0].alternatives[0].transcript`, returns the string.
  2. 401 response → `DeepgramAuthError` raised.
  3. 5xx → `DeepgramTransientError` raised after retry budget (try twice total).
  4. Timeout (>20s) → `asyncio.TimeoutError` surfaced as `DeepgramTimeoutError`.
  5. Empty transcript in the JSON → returns `""` (caller decides how to message the user).
  6. File path doesn't exist → `FileNotFoundError` raised before the HTTP call.

```python
# tests/dispatcher/test_stt_deepgram.py (fragment)
import pytest, respx, httpx
from pathlib import Path
from tero2.dispatcher.stt import DeepgramSTT, DeepgramAuthError

@respx.mock
@pytest.mark.asyncio
async def test_transcribe_happy(tmp_path):
    ogg = tmp_path / "v.ogg"
    ogg.write_bytes(b"OggS" + b"\x00" * 100)
    respx.post("https://api.deepgram.com/v1/listen").mock(
        return_value=httpx.Response(200, json={
            "results":{"channels":[{"alternatives":[{"transcript":"hello"}]}]}
        })
    )
    stt = DeepgramSTT(api_key="k", model="nova-3", language="multi")
    assert await stt.transcribe_file(ogg) == "hello"

@respx.mock
@pytest.mark.asyncio
async def test_transcribe_auth(tmp_path):
    ogg = tmp_path / "v.ogg"; ogg.write_bytes(b"OggS")
    respx.post("https://api.deepgram.com/v1/listen").mock(
        return_value=httpx.Response(401, json={"err_code":"INVALID_AUTH"})
    )
    stt = DeepgramSTT(api_key="bad", model="nova-3", language="multi")
    with pytest.raises(DeepgramAuthError):
        await stt.transcribe_file(ogg)
```

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `stt.py`.**

```python
# tero2/dispatcher/stt.py
import asyncio
import logging
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class DeepgramError(Exception): ...
class DeepgramAuthError(DeepgramError): ...
class DeepgramTransientError(DeepgramError): ...
class DeepgramTimeoutError(DeepgramError): ...


class DeepgramSTT:
    """Pre-recorded audio → transcript via Deepgram REST.

    Matches the API surface used by the Node engine at tts/deepgram/ but
    for single-file transcription (Telegram voice messages arrive complete,
    so we skip the streaming WebSocket path).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        language: str = "multi",
        base_url: str = "https://api.deepgram.com",
        timeout: float = 20.0,
        max_retries: int = 1,
    ):
        self._api_key = api_key
        self._model = model
        self._language = language
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries

    async def transcribe_file(self, audio_path: Path) -> str:
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        audio_bytes = await asyncio.to_thread(path.read_bytes)

        params = {
            "model": self._model,
            "language": self._language,
            "punctuate": "true",
            "smart_format": "true",
        }
        headers = {
            "Authorization": f"Token {self._api_key}",
            "Content-Type": self._guess_content_type(path),
        }
        url = f"{self._base_url}/v1/listen"

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(
                        url, params=params, headers=headers, content=audio_bytes
                    )
                if resp.status_code == 401:
                    raise DeepgramAuthError("invalid Deepgram API key")
                if 500 <= resp.status_code < 600:
                    last_exc = DeepgramTransientError(f"deepgram {resp.status_code}")
                    continue
                resp.raise_for_status()
                data = resp.json()
                alts = (
                    data.get("results", {})
                        .get("channels", [{}])[0]
                        .get("alternatives", [{}])
                )
                return (alts[0].get("transcript") or "").strip() if alts else ""
            except httpx.TimeoutException as exc:
                last_exc = DeepgramTimeoutError(str(exc))
                continue
            except httpx.HTTPError as exc:
                last_exc = DeepgramTransientError(str(exc))
                continue
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _guess_content_type(path: Path) -> str:
        ext = path.suffix.lower()
        return {
            ".ogg":  "audio/ogg",
            ".oga":  "audio/ogg",
            ".mp3":  "audio/mpeg",
            ".wav":  "audio/wav",
            ".flac": "audio/flac",
            ".m4a":  "audio/mp4",
        }.get(ext, "application/octet-stream")
```

- [ ] **Step 4:** `uv run pytest tests/dispatcher/test_stt_deepgram.py -v` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add tero2/dispatcher/stt.py tests/dispatcher/test_stt_deepgram.py
git commit -m "dispatcher: deepgram stt module"
```

---

## Task 3 — Architect subprocess bridge

**Files:**
- Replace: `tero2/dispatcher/brains/claude_code.py` (was Stage 1 stub)
- Create: `tests/dispatcher/test_architect_session.py`

- [ ] **Step 1: Tests** (subprocess mocked with an `asyncio.subprocess.Process`-shaped fake):
  1. `_validate_session_id("7b3e1c2e-8a9d-4f5c-9d8e-1a2b3c4d5e6f")` → OK.
  2. `_validate_session_id("../escape")` → `InvalidSessionID`.
  3. `_validate_session_id("7b3e1c2e--not-a-uuid")` → `InvalidSessionID`.
  4. Session bootstrap: run `claude -p "<starter>" --output-format json`, parse `session_id` from stdout, store on `ChatState`.
  5. `/done` terminates `state.architect_proc` within 3s, SIGKILLs if terminate stalls, clears both `architect_session_id` and `architect_proc`, sets mode to `IDLE`.
  6. Bot shutdown with active `architect_proc` → proc terminated within 3s; no orphan.

- [ ] **Step 2:** Implement `claude_code.py` per spec §2.2 verbatim (UUID4 validator, `send_to_architect` using `create_subprocess_exec` in argv-list form — **no shell interpolation**).

- [ ] **Step 3:** Implement session-bootstrap helper that runs `claude -p <starter> --output-format json` in the project's working directory and parses `session_id` from the JSON output.

- [ ] **Step 4:** Extend `TelegramInputBot.stop()` with the reaping logic (spec §2.2 — `terminate` then `wait_for(3s)` then `kill`).

- [ ] **Step 5:** PASS. `git commit -m "dispatcher: architect mode claude resume"`

---

## Task 4 — Voice handler in Telegram bot

**Files:**
- Modify: `tero2/telegram_input.py`
- Create: `tests/dispatcher/test_voice_handler.py`

- [ ] **Step 1: Tests** (mocked STT + mocked Telegram download):
  1. Voice message → `_download_voice` writes to NamedTemporaryFile → `stt.transcribe_file()` returns "retry please" → `_dispatch_as_answer("retry please")` is called → tmp file is unlinked even on exception.
  2. STT raises `DeepgramAuthError` → user gets `"⚠️ Couldn't transcribe voice. Please retype."`, tmp file still unlinked.
  3. STT returns `""` (empty transcript) → user gets the same retype message; dispatcher is NOT called.
  4. Voice received in `ARCHITECT` mode → transcript is fed to `_handle_architect_message`, not `_dispatch_as_answer`.

- [ ] **Step 2: Implement voice handling** per spec §2.3, with one swap: replace `self._brain.transcribe(...)` with `self._stt.transcribe_file(...)` where `self._stt = DeepgramSTT(api_key=os.environ[cfg.dispatcher.stt_api_key_env], model=cfg.dispatcher.stt_model, language=cfg.dispatcher.stt_language)` in `__init__`.

```python
# tero2/telegram_input.py (excerpt — inside _handle_update)
voice = message.get("voice")
if voice:
    with tempfile.NamedTemporaryFile(
        suffix=".ogg", prefix="tero2-voice-", delete=False
    ) as tmp:
        ogg_path = Path(tmp.name)
    try:
        await self._download_voice(voice["file_id"], ogg_path)
        transcript = await self._stt.transcribe_file(ogg_path)
    except Exception as exc:
        log.warning("voice transcribe failed: %s", exc)
        await self.notifier.notify("⚠️ Couldn't transcribe voice. Please retype.")
        return
    finally:
        with suppress(OSError):
            ogg_path.unlink(missing_ok=True)
    if not transcript:
        await self.notifier.notify("⚠️ Empty transcript. Please retype.")
        return
    message["text"] = transcript
    # fall through to text pipeline
```

- [ ] **Step 3:** PASS. `git commit -m "dispatcher: telegram voice → deepgram → pipeline"`

---

## Task 5 — `/architect` and `/done` commands

**Files:**
- Modify: `tero2/dispatcher/commands.py`
- Modify: `tero2/telegram_input.py`

- [ ] **Step 1: Tests** extending `test_commands.py`:
  - `/architect` → returns a sentinel `CommandIntent.ARCHITECT_START` (not a `ToolCall` — it's a mode switch).
  - `/done` → returns `CommandIntent.ARCHITECT_END`.
  - `/architect` while `state.mode == AWAITING_ESCALATION_ANSWER` → rejected (spec §1.11 regression test): bot replies `"⛔ Finish the current escalation first."`, waiter untouched.

- [ ] **Step 2:** Extend `parse_command` and the bot's message handler to route these intents: on `/architect` in `IDLE`, send welcome + bootstrap session + set mode `ARCHITECT`; on `/done`, reap proc + clear state + mode `IDLE`.

- [ ] **Step 3: Integration test** (`test_architect_session.py`): `/architect` → 3 turns with mocked `claude` subprocess (`stdout` streams `"Hello"\n"there"\n`) → `/done` → state cleaned up.

- [ ] **Step 4:** PASS. `git commit -m "dispatcher: /architect /done flow"`

---

## Task 6 — Output chunking

**Files:**
- Modify: `tero2/telegram_input.py` (architect output buffer)
- Add test to: `tests/dispatcher/test_architect_session.py`

- [ ] **Step 1: Test** — feed a 10k-char stdout stream → expect 4 messages delivered to Telegram, each `≤ 3000` chars, split on newline boundaries when possible.

- [ ] **Step 2:** Implement `_flush_buffer` helper using `cfg.dispatcher.output_flush_chars` as the threshold; flush immediately on newline at/after threshold, or when process exits.

- [ ] **Step 3:** PASS. `git commit -m "dispatcher: architect output chunking"`

---

## Task 7 — Manual E2E gate & ship

- [ ] **Step 1:** Set `dispatcher.architect_enabled = true` and `dispatcher.enabled = true` in the test project.
- [ ] **Step 2:** From phone: send `/architect` → get welcome → ask "what do tero2 phases do?" → receive Claude reply within 10s → `/done` → bot confirms end.
- [ ] **Step 3:** From phone during a real escalation: send a voice message `"swap scout to glm-4.6"` → bot transcribes via Deepgram → dispatcher invokes `swap_agent` → runner resumes.
- [ ] **Step 4:** Verify `bugs.md` for any new issues surfaced in the manual run; file if applicable.
- [ ] **Step 5: Tag.**

```bash
git tag dispatcher-stage2
git push --tags
```

---

## Acceptance (Stage 2 done when)

- [ ] `uv run pytest tests/dispatcher/ -q` green (all Stage 1 + new Stage 2 tests pass).
- [ ] Manual E2E: architect 3-turn conversation works; voice `/retry` works end-to-end with real Deepgram key.
- [ ] `DEEPGRAM_API_KEY` unset → clear `ConfigError` at startup when `dispatcher.enabled` is true.
- [ ] Bot shutdown during an active architect session: `claude` subprocess terminated within 3s; no zombies in `ps`.
- [ ] No TTS invocations in the architect output path (grep test from Stage 1 §1.11 C#19 still green).
