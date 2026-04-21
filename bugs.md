# tero2 Open Bugs

Audit 1: 2026-04-18 (4 agents). Audit 2: 2026-04-20 (5 agents).
36 open bugs total. Fixed bugs removed from tracking.

---

## CRITICAL (5)

### 4. lock.py race condition on acquire retry
**File:** `tero2/lock.py:27-31`
Between `_pid_alive()` check and recursive retry, process can die and new one acquire lock.
**Fix:** Don't retry after PID check — lock file is source of truth.

### 5. lock.py truncate+write not atomic
**File:** `tero2/lock.py:33-35`
Concurrent reader sees garbage between `ftruncate()` and `write()`.
**Fix:** Write to tmp file, atomic rename.

### 40. stuck_detection: off-by-one in tool_repeat threshold
**File:** `tero2/stuck_detection.py:67`
`state.tool_repeat_count >= config.tool_repeat_threshold - 1` triggers one step early. Threshold 3 fires at count 2 instead of 3.
**Fix:** `>= config.tool_repeat_threshold`

### 41. shell provider: arbitrary command injection
**File:** `tero2/providers/shell.py:26-29`
`bash -c prompt` passes untrusted input to bash. Shell metacharacters execute freely.

### 42. events: memory leak on unsubscribe
**File:** `tero2/events.py:123-131`
`unsubscribe()` removes queue but doesn't drain pending events. References in `data` dicts prevent GC over TUI lifecycle.
**Fix:** drain queue before removing from subscribers.

### 43. app: crash during unmount widget queries
**File:** `tero2/tui/app.py:105-110`
`_consume_events()` catches `NoMatches` on queries but event routing runs outside try. Screen transitions during processing crash on subsequent widget method calls.
**Fix:** move entire routing block inside try.

### 44. config_writer: lock file leak on success
**File:** `tero2/config_writer.py:79-110`
`.sora/config.toml.lock` only unlinked in exception path. Every successful write leaves stale lock on disk.
**Fix:** always unlink in finally.

### 45. disk_layer: metrics contract violation without read_metrics
**File:** `tero2/disk_layer.py:74-99`
`write_metrics()` calculates delta against thread-local `last_read`. Without prior `read_metrics()`, values treated as absolute not incremental.
**Fix:** raise or auto-read if `last_read` not set.

---

## HIGH (12)

### 11. runner: TOCTOU in override checking
**File:** `tero2/runner.py:309-333`
60-second sleep misses STOP directive added during wait.
**Fix:** Poll in 5-second intervals, check for STOP inside sleep loop.

### 12. runner: signal handler race condition
**File:** `tero2/runner.py:88-95`
SIGTERM/SIGINT can arrive between event creation and handler setup.
**Fix:** Track `handlers_added` flag; only remove if actually added.

### 19. usage_tracker: race condition on shared dict
**File:** `tero2/usage_tracker.py:106-126`
`record_step()` modifies `_providers` dict without locking.
**Fix:** Add asyncio.Lock or threading.Lock.

### 21. shell provider: subprocess not cleaned on exception
**File:** `tero2/providers/shell.py:26-38`
`communicate()` exception leaves zombie process.
**Fix:** try/finally with `proc.terminate()`.

### 22. tui/app: query_one without try/except in event consumer
**File:** `tero2/tui/app.py:86-90`
Screen transition during event processing → `NoMatches` crash.
**Fix:** Wrap widget queries in try/except.

### 33. project_pick: `n` key crashes with DuplicateIds on any press
**File:** `tero2/tui/screens/project_pick.py:70–71`
```python
def action_manual_input(self) -> None:
    self.mount(Input(placeholder="Путь к проекту…", id="path-input"))
```
Two crash paths: (A) when history is empty, `compose()` already yields `Input(id="path-input")`; first press of `n` tries to mount a second with the same id → `DuplicateIds`. (B) when history is present, second press of `n` → same crash. `self._manual_mode = False` in `__init__` was clearly intended as a guard but is never read.
**Fix:** Guard with `try: self.query_one("#path-input") except NoMatches: self.mount(...)`.

### 46. escalation: Level 2 retry skipped
**File:** `tero2/escalation.py:86-87`
`current_level >= EscalationLevel.BACKTRACK_COACH` includes Level 2 itself. After one attempt, any stuck jumps straight to Level 3.
**Fix:** `> EscalationLevel.BACKTRACK_COACH`.

### 47. runner: escalation level never resets after diversification
**File:** `tero2/runner.py:304-331`
`ctx.escalation_level` stays at `DIVERSIFICATION` forever. Only resets when `stuck.signal == NONE`, but persists through successful attempts.

### 48. runner: no shutdown check in slice loop
**File:** `tero2/runner.py:569-622`
`run_architect()` / `run_execute()` calls don't check `shutdown_event` before invocation. Long architect phase delays graceful shutdown.

### 49. stream_bus: event loop capture race condition
**File:** `tero2/stream_bus.py:155-168`
Caches `asyncio.get_running_loop()` on first call. Reuse across async contexts (tests, restarts) → stale loop → events lost silently.

### 50. model_pick: index out-of-bounds on fast filter
**File:** `tero2/tui/screens/model_pick.py:76-80`
Search filter shrinks `self._filtered` but `ListView.index` stays at old position. Enter press → `IndexError`.
**Fix:** `idx = min(idx, len(self._filtered) - 1)`.

### 51. shell provider: file descriptor leak
**File:** `tero2/providers/shell.py:24-43`
`PIPE` for stdout/stderr never closed after `communicate()`. Accumulates FDs in long-running processes.

### 52. cli provider: stderr data loss on cancel
**File:** `tero2/providers/cli.py:149-173`
Cancelled `stderr_task` → `result()` raises `CancelledError` → caught → returns `b""`, losing captured stderr.

---

## MEDIUM (12)

### 24. runner: off-by-one in slice loop
**File:** `tero2/runner.py:483-527`
`while extra_slices_done < max_slices` runs max_slices+1 total (includes S01).

### 27. telegram_input: no file size check on download
**File:** `tero2/telegram_input.py:277-298`
Malicious large file → memory/disk exhaustion.

### 28. project_init: sanitization can produce empty string
**File:** `tero2/project_init.py:44-50`
Name "!!!" → empty string → project in root dir.

### 29. escalation: inconsistent checkpointing across levels
**File:** `tero2/escalation.py:111-176`
State partially updated if checkpoint fails mid-level.

### 32. TUI screens: stat() in sort without try/except
**File:** `tero2/tui/screens/project_pick.py:38-46`, `tero2/tui/screens/plan_pick.py:38-50`
`p.stat().st_mtime` in sort key → OSError crashes scan.

### 34. app.py: BINDINGS drift — `n`/`o` keys dead, stuck labels blank
**File:** `tero2/tui/app.py:29–41`
`action_new_project` and `action_settings` exist but are not in `BINDINGS` → `n` and `o` do nothing in the TUI. Stuck option labels are empty strings.
**Fix:** Add missing entries; copy BINDINGS from requirements.md Task 4 spec.

### 36. project_pick: "press d to delete" notification references unimplemented key
**File:** `tero2/tui/screens/project_pick.py:61`
No `d` binding or handler exists. Pressing `d` has no effect. Stale entries accumulate in `~/.tero2/history.json`.

### 53. runner: UTF-8 truncation corrupts multi-byte characters
**File:** `tero2/runner.py:427-432`
Byte-based slicing `encode("utf-8")[:2000].decode("utf-8", errors="ignore")` splits Cyrillic/emoji. Characters at boundary silently dropped.
**Fix:** character-based: `output[:2000]`.

### 54. reflexion: same UTF-8 truncation bug (audit 2)
**File:** `tero2/reflexion.py:79`
Same pattern as #53 with `errors="replace"`. Still corrupts output.

### 55. state: touch() doesn't persist
**File:** `tero2/state.py:193`
`touch()` updates `updated_at` in memory without `save()`. Race with concurrent `save()` persists wrong timestamp.

### 56. checkpoint: mark_started() discards existing state
**File:** `tero2/checkpoint.py:44-50`
Always creates fresh `AgentState()`, losing context from previous `FAILED`/`PAUSED` state.

### 57. runner: dead code with object.__setattr__ bypass
**File:** `tero2/runner.py:751-761`
`object.__setattr__(state, "phase", ...)` modifies local `state` immediately discarded. No runtime harm but confusing.

### 58. circuit_breaker: HALF_OPEN stuck forever with timeout=0
**File:** `tero2/circuit_breaker.py:29-53`
`recovery_timeout_s == 0` → condition at line 44 always False → no new trial after failed HALF_OPEN probe → permanent break.

### 59. persona: crash on missing prompts dir
**File:** `tero2/persona.py:68-84`
`_get_prompts_dir()` returns `None` → `None / "architect.md"` raises `TypeError`. Not caught by `FileNotFoundError` handler.
**Fix:** `if prompts_dir is None: return {}`.

### 60. providers_pick: queries non-existent #pp-title widget
**File:** `tero2/tui/screens/providers_pick.py:131`
`_enter_step2()` queries `#pp-title` but compose() creates no widget with that id. `NoMatches` silently fails provider list update.

---

## Patterns

- **Locking issues** — lock.py TOCTOU, config_writer lock leak, usage_tracker race
- **Resource leaks** — subprocess zombies, FDs not closed, event queues not drained
- **Silent failures** — metrics contract, stderr loss, widget query misses
- **UTF-8 corruption** — byte-based truncation in runner and reflexion
- **TUI fragility** — unguarded widget queries, stale indices, missing bindings
