# Telegram Dispatcher Stage 3 — Local Gemma 4 E2B + Direct Audio Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Swap the cloud brain for a **local Gemma 4 E2B** model served via **`llama-cpp-python`** on Metal, reusing the GGUF file OpenVerb already has on disk. Stage 3 ships a **text-only** local brain — voice input continues to go through Deepgram (Stage 2 path) because `llama-cpp-python` does not yet expose the Gemma 4 audio conformer encoder that landed in upstream `llama.cpp` in April 2026 (`mtmd` + conformer PRs are C++-only for now). A follow-up Stage 3.1 will wire direct audio when the Python bindings catch up. If the local path misses its latency/RSS targets, users flip `brain_provider` back to `kimi` and keep shipping.

**Architecture:** One new file — `tero2/dispatcher/brains/llama_cpp.py` — implementing `Brain` via `llama_cpp.Llama` with `chat_format="gemma"`. Lazy model loading on first `interpret()` (10–15s mmap + Metal init on the thread pool, not the event loop). Idle-unload watchdog reclaims Metal buffers after 30 min idle. Tool calls parsed from Gemma's `<|tool_call>...<tool_call|>` content markers with regex (Gemma 4 has native function-calling but llama-cpp-python's chat-template handler for Gemma tool-calling is still maturing; regex fallback is defensive and works for both formats). No mmproj is loaded in Stage 3 — mmproj is llama.cpp's **image** projector, not an audio encoder.

**Tech Stack:** `llama-cpp-python >= 0.3.0` as an **optional** extra (`[llama_cpp]`), Metal backend on Apple Silicon (`CMAKE_ARGS="-DGGML_METAL=on"`), asyncio, existing dispatcher from Stages 1–2.

**Spec reference:** `docs/superpowers/specs/2026-04-20-telegram-dispatcher-design.md` §3.1–§3.7. This plan **overrides** the spec on three points: (1) `mmproj_path` is dropped from the hot path (it's for images, not audio, and this plan doesn't add image support); (2) `Brain.transcribe()` is deleted from the ABC — STT lives in `tero2/dispatcher/stt.py` from Stage 2; (3) direct-audio-into-Gemma is explicitly deferred to Stage 3.1 pending `llama-cpp-python` Gemma-audio support.

---

## File Structure

```
tero2/dispatcher/brains/
└── llama_cpp.py                        # NEW — LlamaCppBrain (text only in Stage 3)

tero2/dispatcher/brains/__init__.py     # MODIFIED — make_brain supports "llama_cpp"; validate gguf_path
tero2/config.py                         # MODIFIED — gguf_path, n_ctx, n_threads, idle_unload_s
                                        # NOTE: mmproj_path stays declared for Stage 3.1, unused in Stage 3

pyproject.toml                          # MODIFIED — [project.optional-dependencies] llama_cpp

tests/dispatcher/
├── test_llama_cpp_parser.py            # NEW — pure tests on tool_call regex (no llama-cpp import)
└── test_llama_cpp_live.py               # NEW — env-gated (RUN_LOCAL_LLM_TESTS=1); loads real GGUF
```

**Not in Stage 3** (deferred):
- `tero2/dispatcher/brain.py` `supports_audio()` method → comes in Stage 3.1
- `test_voice_direct_audio.py` → comes in Stage 3.1
- Any direct-audio code path → Stage 3.1

---

## Decision Deltas vs Spec

1. **No direct audio in Stage 3.** The spec (and my first draft of this plan) implied `LlamaCppBrain` would feed `.ogg` straight into Gemma. Reality check (web search, April 2026):
   - Gemma 4 E2B **does** support audio input up to 30s via an audio conformer encoder.
   - The conformer encoder landed in upstream `llama.cpp` C++ (`mtmd` stack, PR by stephencox-ict, April 2026).
   - `llama-cpp-python` bindings do **NOT** yet expose this path — "currently llama-cpp will not parse audio with Gemma" per the official discussion thread.
   - **Consequence:** Stage 3 ships text-only. Voice in Stage 3 keeps using Deepgram from Stage 2. A follow-up **Stage 3.1** wires direct audio when `llama-cpp-python` catches up (or we shell out to `llama-mtmd-cli`, TBD).

2. **`mmproj_path` is NOT loaded in Stage 3.** It's llama.cpp's multimodal projector for **images**, not a Gemma audio encoder. We keep the field declared in `DispatcherConfig` as a forward-compat stub for Stage 3.1 (image input) but the Stage 3 factory ignores it.

3. **`Brain.transcribe()` is deleted** from the ABC in Stage 3. Stage 2 made voice brain-agnostic via `stt.py`, so this method is dead code — remove it on this stage.

4. **Tool-call format.** Gemma 4 has native function-calling, and llama-cpp-python has a Gemma chat handler (PR #1989 by kossum, merged). Prefer the native OpenAI-style `tool_calls` response path; keep the regex parser on `<|tool_call>…<tool_call|>` markers as a defensive fallback when the handler misses.

5. **Perf fallback is explicit** — if tool-call replies exceed 5s wall time or RSS climbs past 3 GB, log a warning and document observed numbers in `docs/dispatcher-performance.md`. Flip `brain_provider = "kimi"` to restore Stage 1/2 behavior with zero code changes.

---

## Prereq Checks

- [ ] Stages 1 & 2 shipped and tagged.
- [ ] `ls -lh ~/Library/Application\ Support/OpenVerb/models/` shows `gemma-4-E2B-it-Q4_K_M.gguf` (~1.5 GB). (An mmproj file may also exist from OpenVerb — Stage 3 ignores it; Stage 3.1 will use it only if image input is added.)
- [ ] Xcode command-line tools installed: `xcode-select -p` prints a path.
- [ ] `uv` Python 3.12 venv active for tero2.
- [ ] Upstream sanity check: on `https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF`, confirm the exact quant file name; paths in this plan assume `gemma-4-E2B-it-Q4_K_M.gguf`.

---

## Task 1 — Optional dependency + build

- [ ] **Step 1:** Append to `pyproject.toml`:

```toml
[project.optional-dependencies]
llama_cpp = ["llama-cpp-python>=0.3.0"]
```

- [ ] **Step 2:** Install with Metal:

```bash
CMAKE_ARGS="-DGGML_METAL=on" uv pip install '.[llama_cpp]'
```

- [ ] **Step 3:** Smoke: `uv run python -c "import llama_cpp; print(llama_cpp.__version__)"` → prints a version ≥ `0.3.0`.

- [ ] **Step 4:** `git commit -m "deps: llama-cpp-python optional extra"`

---

## Task 2 — Clean the `Brain` ABC

**Files:**
- Modify: `tero2/dispatcher/brain.py`
- Modify: `tero2/dispatcher/brains/kimi.py`

- [ ] **Step 1:** Delete `transcribe()` from the `Brain` ABC. Delete the stub from `KimiBrain`. Nothing in Stage 1/2 code paths calls `brain.transcribe` — voice goes through `tero2/dispatcher/stt.py` (Stage 2).

- [ ] **Step 2:** `uv run pytest -q` — full suite green (no regressions — `KimiBrain.interpret` signature unchanged).

- [ ] **Step 3:** `git commit -m "dispatcher: drop dead brain.transcribe()"`

---

## Task 3 — LlamaCppBrain parser tests (pure)

**Files:**
- Create: `tests/dispatcher/test_llama_cpp_parser.py`

- [ ] **Step 1: Write tests** — `LlamaCppBrain._parse_reply` is a `@staticmethod`, so we exercise it without loading a model. Fixtures (copy from spec §3.6):
  1. `<|tool_call>{"name":"swap_agent","arguments":{"role":"scout","model":"glm-4.6"}}<tool_call|>` → `BrainReply(tool_call=ToolCall("swap_agent", {...}))`.
  2. Plain text "Which role to swap?" → `BrainReply(text="Which role to swap?")`.
  3. `<|tool_call>{"name":"x","arguments":` (malformed JSON) → `BrainReply(text=None)`  → caller substitutes fallback.
  4. Tool call + trailing text → tool_call wins.
  5. Two tool_call blocks → first wins (we log a warning on the second).
  6. Empty string → `BrainReply(text=None)`  → caller substitutes fallback.
  7. Partial marker `<|tool_call>` without closing → treated as plain text.

- [ ] **Step 2: Run — FAIL** (class doesn't exist).

- [ ] **Step 3:** Implement `LlamaCppBrain` per spec §3.3, adapted for text-only Stage 3. Key points:
  - `__init__` does **NOT** load the model (lazy) — only captures config.
  - `_ensure_loaded` uses `asyncio.Lock` + `asyncio.to_thread(Llama, …)` (10-15s disk + Metal setup off the event loop).
  - Constructor kwargs: `chat_format="gemma"` (the upstream chat handler landed via PR #1989, merged in `llama-cpp-python` 0.3.x).
  - `clip_model_path` / mmproj args are **NOT set** in Stage 3 — mmproj is for images, which Stage 3 doesn't use.
  - `_idle_watchdog` task unloads after `idle_unload_s`.
  - `unload()` calls `llm.close()` if available (llama-cpp-python ≥ 0.3 exposes it), else drops the reference with a warning.
  - `aclose()` cancels the watchdog and unloads — called from bot shutdown.
  - `interpret(user_text, tools, context, history)` — text-only signature; no `audio_path` kwarg in Stage 3.

- [ ] **Step 4:** `uv run pytest tests/dispatcher/test_llama_cpp_parser.py -v` → PASS (pure parser tests — no llama-cpp-python actually loaded).

- [ ] **Step 5:** `git commit -m "dispatcher: llama-cpp brain parser + lazy load"`

---

## Task 4 — Factory wiring

**Files:**
- Modify: `tero2/dispatcher/brains/__init__.py`

- [ ] **Step 1: Tests** — extend `tests/dispatcher/test_config.py` (or add `test_factory.py`):
  - `make_brain(cfg)` with `brain_provider="llama_cpp"` and `gguf_path=""` → `ConfigError("gguf_path required")`.
  - `gguf_path="/does/not/exist"` → `ConfigError("does not exist")`.
  - Valid `gguf_path` but `llama-cpp-python` not installed → `ConfigError` with the install hint.
  - `mmproj_path` set to a nonexistent path → **not** an error in Stage 3 (field is forward-compat; the factory ignores it). Verify with a test.

- [ ] **Step 2:** Implement per spec §3.5, adapted:
  - Validate `gguf_path` only; do NOT require or validate `mmproj_path`.
  - Translate `ImportError` on `llama_cpp` import into a `ConfigError` with the Metal-install hint.

- [ ] **Step 3:** PASS. `git commit -m "dispatcher: llama_cpp brain factory"`

---

## Task 5 — Live local model tests

**Files:**
- Create: `tests/dispatcher/test_llama_cpp_live.py`

- [ ] **Step 1:** Gate with `@pytest.mark.skipif(not os.environ.get("RUN_LOCAL_LLM_TESTS"), reason="live local model")`.

- [ ] **Step 2:** Three canonical prompts:
  1. `"retry please"` → at least one `tool_call` in the reply (`retry_phase`).
  2. `"swap scout to glm-4.6"` → tool_call with `name="swap_agent"` and `args={"role":"scout","model":"glm-4.6"}`.
  3. `"what's going on?"` → plain text reply (`BrainReply.text` non-empty, no tool_call).

- [ ] **Step 3:** Verify `chat_format="gemma"` against llama-cpp-python's changelog. PR #1989 (kossum) added the Gemma3 chat handler; 0.3.x versions include Gemma 4 support once the handler name is updated upstream. If the exact handler name differs (`"gemma-4"`, `"gemma"`, or custom), patch `LlamaCppBrain.__init__` accordingly and add a note in `docs/dispatcher-performance.md`.

- [ ] **Step 4:** Run manually: `RUN_LOCAL_LLM_TESTS=1 uv run pytest tests/dispatcher/test_llama_cpp_live.py -v`. Record cold-start + warm-reply wall times.

- [ ] **Step 5:** If cold-start > 15s or warm reply > 3s, document in `docs/dispatcher-performance.md` and open a follow-up issue — do NOT block the ship on it; the fallback is `brain_provider = "kimi"`.

- [ ] **Step 6:** `git commit -m "dispatcher: live local gemma tests (env-gated)"`

---

## Task 6 — Config flip & ship

- [ ] **Step 1:** Update `~/.tero2/config.toml`:

```toml
[dispatcher]
enabled = true
brain_provider = "llama_cpp"
gguf_path = "/Users/terobyte/Library/Application Support/OpenVerb/models/gemma-4-E2B-it-Q4_K_M.gguf"
# mmproj_path not used in Stage 3; will be wired in Stage 3.1 for image/audio
n_ctx = 4096
n_threads = 8
idle_unload_s = 1800
```

- [ ] **Step 2:** Force a real Level 3 escalation. Reply from phone with a **text** message. Verify: Gemma processes locally (check logs for "LlamaCppBrain"), tool_call fires, runner resumes.

- [ ] **Step 2b:** Force another Level 3 escalation, reply with a **voice** message. Verify: Deepgram IS called (Stage 2 path), its transcript flows into Gemma, tool_call fires. Voice → Deepgram → Gemma is the Stage 3 contract.

- [ ] **Step 3:** Let the bot sit idle for 35 minutes. Check `ps -o rss= -p <pid>` — RSS should drop by the model size (~1.5 GB) after the watchdog unloads.

- [ ] **Step 4:** Tag.

```bash
git tag dispatcher-stage3
git push --tags
```

- [ ] **Step 5:** Flip back to `brain_provider = "kimi"` if perf targets missed; document observed numbers.

---

## Acceptance (Stage 3 done when)

- [ ] `uv run pytest tests/dispatcher/ -q` green (parser tests pass without llama-cpp import).
- [ ] `RUN_LOCAL_LLM_TESTS=1 uv run pytest tests/dispatcher/test_llama_cpp_live.py -v` green on Apple Silicon.
- [ ] Manual E2E (text): real escalation → text reply → Gemma tool_call → runner resumes within 5s.
- [ ] Manual E2E (voice): real escalation → voice reply → Deepgram transcribes → Gemma tool_calls on the transcript → runner resumes.
- [ ] Idle watchdog verified: RSS drops after `idle_unload_s`; next call reloads cleanly.
- [ ] Bot shutdown: `LlamaCppBrain.aclose()` cancels the watchdog AND unloads; no Metal leak warnings in logs.
- [ ] Fallback documented: `brain_provider = "kimi"` flip restores Stage 1/2 behavior with zero code changes.

---

## Stage 3.1 — Follow-up (not in this plan)

When `llama-cpp-python` exposes Gemma 4 audio (via `mtmd` + conformer encoder bindings), wire direct-audio:
1. Add `Brain.supports_audio() -> bool` to the ABC (default False; `LlamaCppBrain` returns True when the audio path is available).
2. Telegram voice handler branches: `supports_audio()` True → feed audio bytes straight into `brain.interpret`; False → Stage 2 Deepgram path.
3. Drop Deepgram for voice in this project entirely (optional — Deepgram may stay as a backup).

Track upstream: https://github.com/abetlen/llama-cpp-python/issues (search "gemma audio") and https://github.com/ggml-org/llama.cpp/pull/21421.
