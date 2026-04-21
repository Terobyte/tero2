# tero2 Open Bugs

Audit 1: 2026-04-18 (4 agents). Audit 2: 2026-04-20 (5 agents).
14 open bugs. Fixed bugs removed. All open bugs are halal (negative tests fail when bug present).

Removed since last audit (fixed in code or false positive):
  12 — signal handler race: suppress(ValueError) in finally is functionally equivalent to the fix; handlers registered before disk.init()
  21 — shell subprocess: proc.terminate() already in except block
  22 — tui app NoMatches: query_one wrapped in try/except NoMatches
  24 — runner slice off-by-one: condition is `< max_slices - 1` (correct)
  27 — telegram file size: _MAX_FILE_SIZE check already at line 300-303
  28 — project_init empty name: ValueError raised when sanitized name is empty
  32 — plan_pick stat: _mtime has try/except OSError: return 0.0
  33 — project_pick DuplicateIds: action_manual_input guards with query_one + NoMatches
  34 — app BINDINGS: n/o bindings already present
  36 — project_pick d key: binding and action_delete_entry both present
  40 — stuck off-by-one: fixed b8c0aa3
  43 — app crash unmount: routing block wrapped in broad except Exception
  44 — config_writer lock: lock_path.unlink in finally block
  46 — escalation Level-2 skip: fixed b8c0aa3
  47 — escalation reset: FALSE POSITIVE — ctx.escalation_level resets to NONE on not-stuck (line 333)
  50 — model_pick IndexError: on_list_view_selected guards with 0 <= idx < len(self._filtered)
  53 — runner UTF-8: fixed b8c0aa3
  54 — reflexion UTF-8: fixed b8c0aa3
  57 — dead setattr: fixed b8c0aa3
  58 — circuit_breaker stuck: FALSE POSITIVE — with timeout=0, OPEN→HALF_OPEN always fires (0 >= 0)
  59 — persona crash: None guard already present
  60 — providers_pick id: fixed b8c0aa3

---

## CRITICAL (5)

### 4. lock.py race condition on acquire retry
**File:** `tero2/lock.py:27-31`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug4LockTOCTOU`
Between `_pid_alive()` check and recursive retry, process can die and new one acquire lock.
**Fix:** Don't retry after PID check — lock file is source of truth.

### 5. lock.py truncate+write not atomic
**File:** `tero2/lock.py:33-35`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug5LockNonAtomicWrite`
Concurrent reader sees empty file between `ftruncate()` and `write()`.
**Fix:** Write to tmp file, atomic rename.

### 41. shell provider: arbitrary command injection
**File:** `tero2/providers/shell.py:26-29`
**Test:** `tests/test_open_bugs_audit2.py::TestBug41ShellInjection`
`bash -c prompt` passes untrusted input to bash. Shell metacharacters execute freely.

### 42. events: memory leak on unsubscribe
**File:** `tero2/events.py:123-131`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug42EventsUnsubscribeNoDrain`
`unsubscribe()` removes queue but doesn't drain pending events. References in `data` dicts prevent GC over TUI lifecycle.
**Fix:** drain queue before removing from subscribers.

### 45. disk_layer: metrics contract violation without read_metrics
**File:** `tero2/disk_layer.py:74-99`
**Test:** `tests/test_open_bugs_audit2.py::TestBug45MetricsWithoutRead`
`write_metrics()` calculates delta against thread-local `last_read`. Without prior `read_metrics()`, delta is from 0 and silently added to whatever is on disk.
**Fix:** raise or auto-read if `last_read` not set.

---

## HIGH (6)

### 11. runner: retry wait is a monolithic sleep
**File:** `tero2/runner.py:335-341`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug11RetryMonolithicSleep`
`asyncio.sleep(wait)` can sleep up to 300 s in one call. STOP directive written during that window is missed.
**Fix:** Poll in 5-second intervals, check shutdown_event and _check_override() each tick.

### 19. usage_tracker: race condition on shared dict
**File:** `tero2/usage_tracker.py:106-126`
**Test:** `tests/test_open_bugs_audit2_part2.py::TestBug19UsageTrackerRace`
`record_step()` modifies `_providers` dict without any lock. Concurrent threads can interleave between the `not in` check and the dict assignment, losing increments.
**Fix:** Add threading.Lock.

### 48. runner: no shutdown check in slice loop
**File:** `tero2/runner.py:569-622`
**Test:** `tests/test_open_bugs_audit2_part2.py::TestBug48SliceLoopShutdownCheck`
`run_architect()` / `run_execute()` called without checking `shutdown_event` first. Long architect phase delays graceful shutdown.
**Fix:** Add `if shutdown_event and shutdown_event.is_set(): return` before each long call in the slice loop.

### 49. stream_bus: stale event loop after restart
**File:** `tero2/stream_bus.py:155-168`
**Test:** `tests/test_open_bugs_audit2.py::TestBug49StaleEventLoop`
Caches running loop on first call. Reuse across `asyncio.run()` restarts → stale loop → `RuntimeError: Event loop is closed` or silent drop.
**Fix:** In `publish()`, detect closed loop and update `_loop` to current loop.

### 51. shell provider: file descriptor leak
**File:** `tero2/providers/shell.py:24-43`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug51ShellFDLeak`
`PIPE` stdout/stderr not explicitly closed in exception path after `proc.terminate()`.
**Fix:** Close pipes explicitly in the except block after `proc.wait()`.

### 52. cli provider: stderr data loss on cancel
**File:** `tero2/providers/cli.py:149-173`
**Test:** `tests/test_open_bugs_audit2_part2.py::TestBug52StderrLossOnCancel`
Cancelled `stderr_task` → `result()` raises `CancelledError` → caught in broad except → returns `b""`, losing captured stderr.
**Fix:** Don't catch `CancelledError` in the stderr result section, or re-raise after saving bytes.

---

## MEDIUM (3)

### 29. escalation: inconsistent checkpointing across levels
**File:** `tero2/escalation.py:111-176`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug29EscalationInconsistentCheckpoint`
EVENT_JOURNAL / STUCK_REPORT written to disk before `checkpoint.save()`. If save fails, artifact exists but state is inconsistent — restart duplicates the entry.
**Fix:** Write disk artifacts only after checkpoint.save() succeeds.

### 55. state: touch() doesn't persist
**File:** `tero2/state.py:193`
**Test:** `tests/test_open_bugs_audit2.py::TestBug55TouchNoPersist`
`touch()` updates `updated_at` in memory without `save()`. Race with concurrent `save()` persists wrong timestamp.

### 56. checkpoint: mark_started() discards existing state
**File:** `tero2/checkpoint.py:44-50`
**Test:** `tests/test_open_bugs_audit2.py::TestBug56MarkStartedDiscardsState`
Always creates fresh `AgentState()`, losing `retry_count` and other context from previous `FAILED`/`PAUSED` run.
**Fix:** Load state via `restore()` and transition it instead of constructing fresh.

---

## Patterns

- **Locking issues** — lock.py TOCTOU, usage_tracker race
- **Resource leaks** — FDs not closed, event queues not drained
- **Silent failures** — metrics contract, stderr loss
- **State management** — mark_started discards retry context, touch doesn't persist
