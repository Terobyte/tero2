# tero2 Open Bugs

Audit 1: 2026-04-18 (4 agents). Audit 2: 2026-04-20 (5 agents). Audit 3: 2026-04-21 (5 agents). Audit 4: 2026-04-21 (10 agents).
17 open bugs (2 critical, 9 high, 6 medium). Fixed bugs removed. All open bugs are halal (negative tests fail when bug present).

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
  62 — cli deadlock: FALSE POSITIVE — stderr_task drains stderr in background, deadlock can't happen
  63 — heartbeat_task leak: FALSE POSITIVE — no heartbeat pattern in runner.py
  66 — escalation counter order: DUPLICATE of #29 — counters reset BEFORE escalation level (lines 148→155)
  68 — chain generator: FIXED — aclosing() already used (chain.py:95)
  69 — notifier sys.path: FIXED — uses importlib.util, not sys.path manipulation
  74 — pipeline timer: FALSE POSITIVE — Textual auto-cleans set_interval on unmount
  75 — role_swap worker: FALSE POSITIVE — Textual auto-cancels run_worker on screen dismiss
  76 — providers_pick worker: FALSE POSITIVE — same, Textual handles cleanup
  81 — state from_json: FIXED — per-field error handling replaces blanket catch
  84 — project_init TOCTOU: MITIGATED — mkdir(exist_ok=False) catches the race
  4  — lock.py TOCTOU retry: FIXED 49baf8f — acquire() no longer recurses; raises LockHeldError immediately
  11 — runner monolithic sleep: FIXED 68144b8 — polled 5-second loop with shutdown check
  29 — escalation checkpoint order: FIXED 68144b8 — checkpoint.save() called before disk writes
  41 — shell injection: FIXED — shlex.split + create_subprocess_exec, metacharacters not interpreted
  42 — events unsubscribe no drain: FIXED — unsubscribe() drains queue before removing from subscribers
  45 — disk_layer metrics without read: FIXED — write_metrics() raises ValueError if last_read not set
  48 — runner slice loop no shutdown: FIXED 68144b8 — shutdown checks added before architect and execute calls
  51 — shell FD leak: FALSE POSITIVE — asyncio StreamReaders have no .close(); event loop closes _PipeReadTransport automatically on process exit
  61 — architect inverted logic: FALSE POSITIVE — `if not validate_plan(content)` is correct: empty list (no errors) is falsy, so this returns the valid plan
  70 — scout depth check: FALSE POSITIVE — without symlinks creating shortcuts, no real_path is reachable at both valid and invalid depth from the same root
  72 — coach truncation no marker: FIXED — `[TRUNCATED — context limit reached]` appended on cap hit
  73 — verifier no type check: FIXED — isinstance(verify_commands, list) guard with TypeError
  78 — startup_wizard None plan: FIXED — early return on plan_file is None
  79 — project_pick stale _pending_delete: FIXED — reset in on_list_view_highlighted
  55 — state touch no persist: FIXED — touch() already calls self.save(last)
  65 — lock.py fd leak on write error: FIXED — try/except around lseek/write/truncate calls os.close(fd) on any exception
  82 — state tmp file left on failure: FIXED — try/except around os.replace calls tmp.unlink(missing_ok=True) on failure
  83 — disk_layer write_file OSError: FIXED — wrapped in try/except OSError, returns bool (True=success, False=failure)
  85 — architect empty description passes validation: FIXED — validate_plan checks description is non-empty
  19 — usage_tracker race: FIXED — threading.Lock wraps _providers dict access
  49 — stream_bus stale loop: FIXED — is_closed() detection with _loop update on restart
  56 — checkpoint mark_started discards state: FIXED — restore() loads prior state, transitions instead of fresh AgentState()

---

## CRITICAL (2)

### 5. lock.py truncate+write not atomic
**File:** `tero2/lock.py:29-32`
**Test:** `tests/test_open_bugs_audit2_part3.py::TestBug5LockNonAtomicWrite`
`os.write()` then `os.truncate()` is not atomic. Concurrent reader can see empty/partial file between the two calls.
**Fix:** Write PID to a tmp file, atomic rename over the lock file.

### 64. coach: duplicate section headers silently overwritten
**File:** `tero2/players/coach.py:190-194`
**Test:** `tests/test_open_bugs_audit3.py::TestBug64CoachDuplicateSections`
`_parse_sections()` uses `result[section_name] = ...`, so duplicate headers (e.g. two `## STRATEGY`) silently lose the first section's content.
**Fix:** Concatenate content from duplicate sections or raise on duplicates.

---

## HIGH (9)

### 52. cli provider: stderr data loss on cancel
**File:** `tero2/providers/cli.py:149-173`
**Test:** `tests/test_open_bugs_audit2_part2.py::TestBug52StderrLossOnCancel`
Cancelled `stderr_task` → `result()` raises `CancelledError` → caught in broad except → returns `b""`, losing captured stderr.
**Fix:** Don't catch `CancelledError` in the stderr result section, or re-raise after saving bytes.

### 67. cli provider: process leak on stdin exception
**File:** `tero2/providers/cli.py:229-237`
**Test:** `tests/test_audit3_halal_bugs.py::TestBug67CLIProcessLeakOnBrokenPipe`
If `proc.stdin.drain()` raises BrokenPipeError, ProviderError propagates but `proc` is never awaited/killed. Subprocess becomes zombie.
**Fix:** Add try/finally around proc to call proc.kill()/proc.wait() on error.

### 71. architect: malformed task headers silently dropped
**File:** `tero2/players/architect.py:267-270`
**Test:** `tests/test_audit3_halal_bugs.py::TestBug71ArchitectMalformedHeadersDropped`
Headers not matching `_TASK_ID_RE` (e.g. `## Task: Something`) are logged and skipped. Entire task content lost silently — caller gets incomplete SlicePlan with no indication.
**Fix:** Add `dropped_headers: list[str]` to SlicePlan and populate it on each skip.

### 77. config: thread-unsafe load_config
**File:** `tero2/config.py:87-96`
**Test:** `tests/test_audit3_halal_bugs.py::TestBug77ConfigThreadUnsafe`
`load_config()` has no synchronization. Multiple threads can race on TOML parsing, getting inconsistent config views.
**Fix:** Add module-level threading.Lock around config loading.

### 86. catalog.py: subprocess leak on TimeoutError
**File:** `tero2/providers/catalog.py:71-88`
`fetch_cli_models()` creates a subprocess at line 71, but when `asyncio.wait_for()` raises `TimeoutError` at line 76, the except on line 88 returns a static fallback without ever calling `proc.kill()`. The subprocess continues running as a zombie, leaking a PID and file descriptors until the parent exits.
**Fix:** Kill and await the process in the except block before returning the fallback.

### 87. cli provider: generator early exit leaks subprocess
**File:** `tero2/providers/cli.py:254-256`
`run()` is an async generator that yields events from `_stream_events(proc)`. If the consumer stops iterating early (cancellation, exception, break), there is no try/finally to clean up `proc`. The subprocess becomes a zombie.
**Fix:** Wrap the `async for` in try/finally that calls `proc.kill(); await proc.wait()` on generator close.

### 88. events.py: subscribe/unsubscribe race with emit iteration
**File:** `tero2/events.py:113-137 vs 154`
`emit()` iterates `_subscribers` under `_emit_lock`, but `subscribe()` and `unsubscribe()` modify `_subscribers` without acquiring the lock. A concurrent subscribe/unsubscribe during emit can cause `RuntimeError: list changed size during iteration` or silently dropped events.
**Fix:** Acquire `_emit_lock` in `subscribe()` and `unsubscribe()`.

### 89. stream_bus.py: subscribe/unsubscribe race with publish
**File:** `tero2/stream_bus.py:120-137 vs 178-194`
Same pattern as bug 88. `_publish_impl()` iterates `_subscribers` while `subscribe()`/`unsubscribe()` modify it without any lock. No synchronization primitive protects the subscriber list.
**Fix:** Add a threading.Lock to protect `_subscribers` in all three methods.

### 90. execute_phase: div_steps not reset on BACKTRACK_COACH
**File:** `tero2/phases/execute_phase.py:260-261`
Only `DIVERSIFICATION` increments `ctx.div_steps`, but `BACKTRACK_COACH` does not reset it to 0. Compare with `runner.py:329-330` which does `ctx.div_steps = 0` on `BACKTRACK_COACH`. This inconsistency means escalation decisions in execute_phase use a stale div_steps counter, potentially causing premature escalation to HUMAN level.
**Fix:** Add `elif esc_action.level == EscalationLevel.BACKTRACK_COACH: ctx.div_steps = 0` after line 261.

---

## MEDIUM (6)

### 80. tui/model_pick: full list rebuild on every keystroke
**File:** `tero2/tui/screens/model_pick.py:53-74`
**Test:** `tests/test_audit3_halal_bugs.py::TestBug80ModelPickRebuildOnKeystroke`
`on_input_changed()` rebuilds entire ListView on each keystroke. O(n) per event, laggy for large model lists.
**Fix:** Debounce with `self.set_timer(0.15, self._rebuild_list)`.

### 91. telegram_input: HTTP error responses not checked
**File:** `tero2/telegram_input.py:86-101`
`_poll_once()` calls `resp.json()` without checking `resp.status_code`. When Telegram returns 429 Rate Limit or 500 Internal Error with valid JSON, the code processes the error response as if it were a normal response. When the response body is not JSON (HTML error page), the exception is caught at line 95-97 and updates are skipped — but the offset is never advanced, causing the same failed poll to repeat indefinitely.
**Fix:** Check `resp.status_code != 200` before calling `resp.json()`, and advance offset on known error responses.

### 92. telegram_input: subprocess stderr not drained on timeout
**File:** `tero2/telegram_input.py:262-277`
`_watch_runner()` calls `asyncio.wait_for(proc.wait(), timeout=30)`. On TimeoutError (line 276), it just `pass`es — the process is still running, but its stderr pipe is never read again. When the subprocess eventually fills its stderr buffer, it blocks on write, causing a silent hang. No one drains the pipe after the watcher returns.
**Fix:** Start a background task to drain stderr after timeout, or close stderr to let the subprocess handle SIGPIPE.

### 93. shell provider: proc.terminate() without kill fallback
**File:** `tero2/providers/shell.py:37-39`
On exception, `proc.terminate()` (SIGTERM) is sent, then `await proc.wait()` blocks until the child exits. If the child ignores or blocks SIGTERM, `proc.wait()` hangs indefinitely. No timeout or SIGKILL fallback exists.
**Fix:** `await asyncio.wait_for(proc.wait(), timeout=5)` then `proc.kill()` on TimeoutError.

### 94. state.py: bare except catches SystemExit and KeyboardInterrupt
**File:** `tero2/state.py:193`
`save()` uses `except:` (bare except) around `os.replace()`. This catches `KeyboardInterrupt` and `SystemExit`, preventing the user from interrupting the process during a failed save. Should be `except OSError` since `os.replace` only raises OS-level errors.
**Fix:** Change `except:` to `except OSError:`.

### 95. history.py: TOCTOU race on concurrent writes
**File:** `tero2/history.py:31-64`
`record_run()` uses read-modify-write without locking. Two concurrent tero2 instances both read the same `history.json`, update locally, and write back. Last writer wins; the other run record is silently lost.
**Fix:** Use `fcntl.flock()` around the read-modify-write cycle, or append-only format.

### 96. notifier: TTS audio files never cleaned up
**File:** `tero2/notifier.py:49-71`
`send_voice()` generates a TTS audio file via `_generate_tts()`, uploads it, but never deletes the temporary file. Each voice notification leaks ~50-200KB on disk. Over time, accumulated files waste disk space.
**Fix:** Add `finally: audio_path.unlink(missing_ok=True)` after upload.

### 97. providers_pick: dead tmp_path code (misleading atomic write intent)
**File:** `tero2/tui/screens/providers_pick.py:105-114`
`_write_project_config()` creates `tmp_path` at line 105 but never writes to it — writes go directly to `config_path` via `write_global_config_section()`. The `finally` block at line 113-114 cleans up a file that was never created. The code misleadingly suggests atomic write semantics that aren't actually implemented.
**Fix:** Either remove the dead tmp_path code, or implement actual atomic write (write to tmp, then rename).

---

## Patterns

- **Locking issues** — lock.py atomic write, config thread-unsafe, events/stream_bus subscriber races
- **Resource leaks** — subprocess zombies (catalog, cli generator, shell terminate, telegram stderr)
- **Silent failures** — stderr loss, coach duplicate sections, malformed tasks dropped, telegram HTTP errors
- **Inconsistent state** — div_steps reset differs between runner.py and execute_phase.py
