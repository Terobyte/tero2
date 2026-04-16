# tero2 Bug Audit

## OPEN Bugs (15)

## Bug 11 — Off-by-one in TOOL_REPEAT threshold check

**Severity:** High
**Status:** 🔴 Open
**Location:** `stuck_detection.py:65`

```python
if state.tool_repeat_count > 0 and state.tool_repeat_count >= config.tool_repeat_threshold - 1:
```

`tool_repeat_count` is incremented in `update_tool_hash` — it equals the number of **consecutive repeats after the first** (0=first call, 1=second identical call, 2=third identical call, etc.). The `>= threshold - 1` comparison is off-by-one:

With `tool_repeat_threshold=2` (default):
- After 2 identical calls: `tool_repeat_count=1`. Buggy check: `1 >= 1` → **triggers immediately** ❌
- Should trigger after 2 repeats: `1 >= 2` → `False` ✅

The test `test_signal_at_threshold` passes because it manually sets `tool_repeat_count=2` (bypassing the increment logic), but with normal operation the signal fires one step too early.

**Impact:** TOOL_REPEAT is detected after just **1 repeat** when threshold=2, or **2 repeats** when threshold=3 — runners abort prematurely, wasting progress.

---

## Bug 12 — `_current_state` not updated after `_handle_override` PAUSE

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `runner.py:378`

```python
def _handle_override(self, content: str, state: AgentState) -> None:
    if self._RE_STOP.search(content):
        self.checkpoint.mark_failed(state, "STOP directive in OVERRIDE.md")
        return
    if self._RE_PAUSE.search(content) and state.phase != Phase.PAUSED:
        self.checkpoint.mark_paused(state, "PAUSE directive in OVERRIDE.md")
        # ← BUG: returned AgentState is NOT assigned back to self._current_state
        return
```

`mark_paused()` returns an updated `AgentState` with `phase=PAUSED`, but the caller (`_execute_plan`) discards it. Every other state-mutating call in the runner correctly chains the result (e.g., `state = self.checkpoint.mark_failed(...)` at line 110, `state = self.checkpoint.mark_running(...)` at line 177). The PAUSE path is the only outlier.

**Impact:** After PAUSE via OVERRIDE.md, `self._current_state.phase` still reads `RUNNING` in memory while disk has `PAUSED`. If an exception fires after the return (e.g., a signal), the exception handler at `runner.py:94` checks `self._current_state.phase == Phase.RUNNING` — which is **False** (it's PAUSED) — so `mark_failed` is skipped. The stale RUNNING-in-memory state leaks into the exception context.

---

## Bug 13 — `escalation_level` persists across `_execute_plan` calls (cross-plan state bleed)

**Severity:** High
**Status:** 🔴 Open
**Location:** `runner.py:59` + `runner.py:86`

`Runner` instance attributes `_escalation_level` and `_div_steps` are initialized once in `__init__` and never reset between `_execute_plan` calls. A runner that processes **multiple plans sequentially** (e.g., via Telegram bot queue) carries over escalation state:

1. Plan A exhausts retries → escalates to Level 3 → `self._escalation_level = EscalationLevel.HUMAN`
2. Runner processes Plan B (new `_execute_plan` call):
   - `if current_level >= EscalationLevel.BACKTRACK_COACH: return EscalationAction(level=HUMAN, should_pause=True)`
   - Plan B is **immediately paused** without ever running

The same applies to `_escalation_history` — escalation history from previous plans pollutes new plans.

**Impact:** Telegram queue consumers that reuse the same `Runner` instance will have every plan after the first stuck in PAUSED state. Only the first plan gets executed.

---

## Bug 14 — `disk.write_state` vs `checkpoint.save` inconsistency in `execute_escalation`

**Severity:** Low
**Status:** 🔴 Open
**Location:** `escalation.py:135` and `escalation.py:157`

```python
# Level 1 (line 135):
disk.write_state(state)          # ← writes state, but last_checkpoint NOT updated

# Level 2 (line 157):
disk.write_state(state)          # ← writes state, but last_checkpoint NOT updated

# Level 3 (line 171):
state = checkpoint.mark_paused(...)  # ← uses CheckpointManager.save() → last_checkpoint updated
```

LEVEL_1 and LEVEL_2 use `disk.write_state(state)` directly, which calls `AgentState.save()` — this does **not** update `state.last_checkpoint`. LEVEL_3 correctly uses `checkpoint.mark_paused()` which calls `CheckpointManager.save()`, which does set `last_checkpoint`.

The inconsistency means `last_checkpoint` is updated for LEVEL_3 but not for LEVEL_1/2, making it unreliable for backtracking.

---

## Bug 15 — `update_tool_hash` hashes stringified dict for dict messages (unstable hash)

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `runner.py:313`

```python
state, _ = update_tool_hash(state, str(message))  # str(dict) → "{'type': 'tool_result', ...}"
```

`_run_agent` correctly handles dict, str, and object messages and extracts `text_content`. But `update_tool_hash` receives the **raw `message`** before wrapping. For dict messages:

- `_run_agent` extracts: `text_content = message.get("text") or message.get("content") or ""`
- `update_tool_hash` receives: `str(message)` → `"{'type': 'tool_result', 'content': '...'}`

Problems:
1. **Dict key order**: `str(dict)` output order is insertion-order-dependent. Python 3.7+ guarantees dict order, but the format `{'key': 'value'}` (single quotes) vs `{"key": "value"}` (double quotes) depends on how the dict was built. If the same message is reconstructed differently, the string differs.
2. **False negatives**: Two tool calls with identical semantics but different dict representation produce different hashes.
3. **Content pollution**: If a provider yields the same tool call twice with different `content` fields (e.g., different output), the hashes differ and TOOL_REPEAT never fires.

The fix: extract the semantically meaningful tool identifier before hashing:
```python
tool_call_str = getattr(message, "content", None) or getattr(message, "text", None) or str(message)
state, _ = update_tool_hash(state, tool_call_str)
```

---

## Bug 16 — `tool_repeat_count` and `last_tool_hash` not reset in `increment_retry`

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `checkpoint.py:81-87`

```python
def increment_retry(self, state: AgentState) -> AgentState:
    state.retry_count += 1
    state.steps_in_task = 0        # ✓ reset
    state.provider_index = 0       # ✓ reset
    # ✗ tool_repeat_count NOT reset
    # ✗ last_tool_hash NOT reset
    state.touch()
    self.save(state)
    return state
```

A new retry attempt starts with a fresh provider (thanks to `provider_index=0`) but inherits `tool_repeat_count` and `last_tool_hash` from the failed attempt. If the previous attempt triggered TOOL_REPEAT near the threshold, the new attempt starts **already marked** as potentially stuck. With default `tool_repeat_threshold=2`, if the previous attempt had `tool_repeat_count=1`, one more repeat in the new attempt immediately fires TOOL_REPEAT.

Contrast with Level 2 backtrack escalation (`escalation.py:151-154`) which correctly resets all three:
```python
state.steps_in_task = 0
state.retry_count = 0
state.tool_repeat_count = 0    # ✓ reset here
state.last_tool_hash = ""       # ✓ reset here
```

**Impact:** TOOL_REPEAT can fire prematurely on retry attempts if the previous attempt made repeated tool calls, even if the new attempt is genuinely different. Combined with Bug 11, this compounds the off-by-one problem.

---

## Bug 17 — `_override_contains_pause` ignores STOP — runner resumes on STOP directive

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `runner.py:166-178`

```python
if state.phase == Phase.PAUSED:
    ...
    while await self._override_contains_pause():
        if shutdown_event and shutdown_event.is_set():
            ...
        await asyncio.sleep(60)
    # ← PAUSE removed, but STOP not checked!
    state = self.checkpoint.mark_running(state)
    self._current_state = state
```

The PAUSE wait loop only checks for the `PAUSE` keyword. If the user removes `PAUSE` and writes `STOP` in OVERRIDE.md, the loop exits (no more PAUSE) and the runner calls `mark_running` — resuming execution and completely ignoring the STOP directive. The STOP check only happens at the top of each retry iteration (`_handle_override`), not after the PAUSE wait loop exits.

**Impact:** User writes STOP expecting the runner to halt, but it resumes instead. The task continues executing, potentially wasting time on a task the user wanted cancelled. The STOP directive is silently ignored when combined with PAUSE removal.

---

## Bug 18 — `mark_started` doesn't use `self.save()` — `last_checkpoint` empty on task start

**Severity:** Low
**Status:** 🔴 Open
**Location:** `checkpoint.py:45-52`

```python
def mark_started(self, plan_file: str) -> AgentState:
    state = AgentState()
    state = self._transition(state, Phase.RUNNING)
    state.plan_file = str(plan_file)
    state.started_at = datetime.now(timezone.utc).isoformat()
    state.touch()
    self.disk.write_state(state)   # ← last_checkpoint NOT set
    return state
```

Same class of inconsistency as Bug 14. `mark_started` uses `self.disk.write_state(state)` instead of `self.save(state)`. The `save()` method sets `state.last_checkpoint` before writing; `write_state()` does not. After `mark_started`, `last_checkpoint` is an empty string.

Every other state-mutating method (`mark_completed`, `mark_failed`, `mark_paused`, `mark_running`, `increment_retry`, `increment_step`) uses `self.save()`. `mark_started` is the only one that bypasses it.

**Impact:** `last_checkpoint` is unreliable for the first checkpoint in a task's lifecycle. Any code that depends on `last_checkpoint` (e.g., backtracking to last checkpoint in Level 2 escalation) sees an empty value at the start.

---

## Bug 19 — `_escalation_level` not reset on OVERRIDE.md PAUSE resume

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `runner.py:177`

```python
state = self.checkpoint.mark_running(state)
self._current_state = state
# ← self._escalation_level NOT reset!
# ← self._div_steps NOT reset!
# ← self._escalation_history NOT reset!
```

When the runner resumes from OVERRIDE.md PAUSE (user removes PAUSE from file), `mark_running` transitions the state back to RUNNING, but the instance-level escalation state (`_escalation_level`, `_div_steps`, `_escalation_history`) is NOT reset.

Related to Bug 13 (cross-plan state bleed), but this also happens **within a single plan**. If escalation reached Level 3 (HUMAN) before the PAUSE, or even Level 2 (BACKTRACK_COACH), the next stuck detection within the same plan will immediately escalate again:

- `current_level >= EscalationLevel.BACKTRACK_COACH` → immediately returns `HUMAN, should_pause=True`
- The runner is re-paused without ever getting a chance to try the user's new direction from STEER.md

**Impact:** After OVERRIDE.md PAUSE resume, the runner is stuck at the escalation level it had before pausing. If it was at Level 2 or 3, the first stuck signal re-triggers HUMAN escalation immediately. The user's STEER.md input is wasted.

---

## Bug 20 — `_launch_runner` fire-and-forget — subprocess failures silently lost

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `telegram_input.py:231-246`

```python
async def _launch_runner(self, project_path: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tero2.cli", "run", str(project_path),
        "--plan", str(plan_path),
    )
    log.info(f"launched runner (PID {proc.pid}) for {project_path.name}")
    # Fire and forget — runner handles its own lifecycle
```

The subprocess is launched but never awaited or monitored. If it fails immediately (invalid Python path, missing module, import error), the error is silently lost. The user already received "project created — starting runner" in Telegram, but the runner never actually starts.

No stderr capture, no exit code check, no notification on failure.

**Impact:** User submits a plan via Telegram, gets "starting runner" confirmation, but the runner crashes silently. The project sits in IDLE/RUNNING state forever with no feedback. User has no way to know it failed without manually checking logs.

---

## Bug 21 — `_pid_alive(0)` returns True — empty lock file causes permanent lock

**Severity:** Medium
**Status:** 🔴 Open
**Location:** `lock.py:61-69`

```python
@staticmethod
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
```

`_read_pid()` returns `0` when the lock file is empty (e.g., file was created with `O_CREAT` but the process crashed before writing PID). Then `_pid_alive(0)` is called:

- `os.kill(0, 0)` sends signal 0 to **all processes in the current process group**
- This always succeeds (returns without error)
- So `_pid_alive(0) = True`

The retry logic in `acquire`:
```python
pid = self._read_pid()          # pid = 0
if pid and self._pid_alive(pid): # 0 is falsy, so this branch is skipped
```

Wait — `0` is falsy in Python, so `if pid` is `False`. The code falls through to `if _retried: raise ... return self.acquire(_retried=True)`. On retry, the stale file is still there, `flock` succeeds (no one holds it), and the process acquires the lock normally. **This is actually safe.**

However, there's a subtler scenario: if `_read_pid()` returns a **positive but recycled PID** (the original process died and the PID was reassigned to a new, alive process), `_pid_alive` returns `True` for the wrong process. The lock is considered held by a process that never acquired it. The retry is skipped, and `LockHeldError` is raised permanently.

**Impact:** PID recycling (common on long-running systems) causes false `LockHeldError`. The runner cannot acquire the lock even though the original holder is dead. Requires manual deletion of the lock file.

---

## Bug 22 — `FileLock.release` unlink race condition — two processes hold lock simultaneously

**Severity:** High
**Status:** 🔴 Open
**Location:** `lock.py:38-46`

```python
def release(self) -> None:
    if self._fd is not None:
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)   # (1) release flock
            os.close(self._fd)                       # (2) close fd
        except OSError:
            pass
        self._fd = None
        self.lock_path.unlink(missing_ok=True)       # (3) delete file
```

Between step (1) and (3), a race window exists:

1. Process A calls `flock(UN)` — lock released
2. Process B opens the same file, calls `flock(LOCK_EX)` — succeeds
3. Process A calls `unlink` — removes directory entry
4. Process B writes its PID to the now-unlinked file (inode still alive because B has it open)
5. Process C opens the path — creates a **new file** (new inode), calls `flock(LOCK_EX)` — succeeds (no conflict, different inode)
6. **Both B and C believe they hold the exclusive lock**

Fix: don't unlink the lock file on release. Just release the flock and close the fd. The next acquirer will overwrite the PID via `ftruncate + write`.

**Impact:** Two tero2 runners on the same project execute simultaneously, corrupting STATE.json and producing duplicate tool calls. The single-writer guarantee is broken.

---

## Bug 23 — Unknown CLI providers pass prompt as CLI argument — OS arg length limit

**Severity:** Low
**Status:** 🔴 Open
**Location:** `cli.py:152-155`

```python
if builder is None:
    cmd = [self._name]
    if self._default_model:
        cmd.extend(["--model", self._default_model])
    cmd.append(prompt)  # ← prompt as CLI argument, not stdin
```

For providers without a registered builder (not claude/codex/opencode/kilo), the prompt is appended as a command-line argument. Known builders pipe via stdin. But unknown providers:

1. **OS arg limit**: Linux `ARG_MAX` ~2MB, macOS ~256KB. Long plans (common for complex tasks) exceed this → `OSError: [Errno 7] Argument list too long`.
2. **No stdin pipe**: `stdin_data` is `None`, so stdin is set to `DEVNULL`. The subprocess cannot read from stdin.
3. **Shell injection risk**: While `create_subprocess_exec` avoids shell injection, the prompt could contain strings that look like flags (e.g., `--verbose`, `--help`) that confuse the CLI tool.

**Impact:** Unknown providers fail silently on long prompts. The runner retries, wasting time, and eventually exhausts retries without a meaningful error message.

---

## Bug 24 — `_generate_tts` modifies `sys.path` — not thread-safe

**Severity:** Low
**Status:** 🔴 Open
**Location:** `notifier.py:89-107`

```python
@staticmethod
def _generate_tts(text: str) -> Path | None:
    target = str(TTS_SCRIPT.parent.parent)
    inserted = target not in sys.path
    if inserted:
        sys.path.insert(0, target)    # ← global mutation
    try:
        from library.tts_fish_audio import tts_fish_audio_simple
        result = tts_fish_audio_simple(text)
    finally:
        if inserted and target in sys.path:
            sys.path.remove(target)    # ← global mutation
    return Path(result)
```

`_generate_tts` is called via `asyncio.to_thread` (thread pool executor). It modifies `sys.path` — a shared global list — without any synchronization. If two TTS requests run concurrently:

1. Thread A: `sys.path.insert(0, target)` 
2. Thread B: `sys.path.insert(0, target)` — duplicate insertion (inserted flag was False because A hasn't finished yet)
3. Thread A: `sys.path.remove(target)` — removes one copy
4. Thread B: `from library.tts_fish_audio import ...` — import fails because A removed the path
5. Or worse: `sys.path.remove(target)` raises `ValueError` because the list state is unexpected

**Impact:** Concurrent notifications (e.g., STUCK + DONE in rapid succession) can cause TTS import failures or `ValueError` in `sys.path.remove`. The `except Exception` catches it and returns `None`, so the voice message is silently dropped.

---

## Bug 25 — `ProviderChain.run` sets `_current_provider_index` before circuit breaker skip

**Severity:** Trivial
**Status:** 🔴 Open
**Location:** `chain.py:70-74`

```python
for idx, provider in enumerate(self.providers):
    self._current_provider_index = idx    # ← set for skipped provider
    cb = self.cb_registry.get(provider.display_name)
    if not cb.is_available:
        continue                          # ← skip, but index already set
```

The provider index is set before checking if the circuit breaker is open. When a CB-open provider is skipped, `_current_provider_index` points to the skipped provider. The runner reads `chain.current_provider_index` during message processing:

```python
state.provider_index = base_provider_index + chain.current_provider_index
```

This is saved to STATE.json. On crash recovery, the runner tries to resume from the CB-open provider, which gets skipped again (CB is still open), wasting a retry attempt.

**Impact:** Minor — wastes one retry attempt after crash recovery when resuming from a CB-open provider. The runner self-corrects on the next iteration.
