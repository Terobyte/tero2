# tero2 Bug Audit — 2026-04-18

Full codebase audit via 4 parallel agents. 32 unique bugs after deduplication.

**Fixed:** 1, 2, 6, 7, 8, 9, 10, 14, 15, 16, 17, 20, 23, 35, 38, 39 (2026-04-18)

## CRITICAL (9)

### 1. state.from_json silent failure
**File:** `tero2/state.py:114-118`
`from_json` returns default `cls()` on corrupted JSON. Agent loses all progress silently.
**Fix:** Raise exception or log error; let caller decide.

### 2. telegram_input task_done before re-queue
**File:** `tero2/telegram_input.py:200-206`
`task_done()` called before `queue.put()` on pause. `join()` completes prematurely, plans may process twice.
**Fix:** Call `task_done()` after re-queue, or check pause before `get()`.

### 3. events.py _unfinished_tasks manual bookkeeping
**File:** `tero2/events.py:163-176`
Direct deque manipulation with manual `_unfinished_tasks` decrement. Exception between lines 168-175 corrupts count.
**Fix:** Use try/finally or proper queue operations.
**Fixed:** Removed redundant `_unfinished_tasks -= 1 / += 1` pair in the swap path. A del+append swap is net-zero; only the overflow path (all-priority queue) legitimately increments the counter. Tests: `TestBug3UnfinishedTasksInvariant` (5 tests).

### 4. lock.py race condition on acquire retry
**File:** `tero2/lock.py:27-31`
Between `_pid_alive()` check and recursive retry, process can die and new one acquire lock.
**Fix:** Don't retry after PID check — lock file is source of truth.

### 5. lock.py truncate+write not atomic
**File:** `tero2/lock.py:33-35`
Concurrent reader sees garbage between `ftruncate()` and `write()`.
**Fix:** Write to tmp file, atomic rename.

### 6. ProviderChain unbounded message accumulation
**File:** `tero2/providers/chain.py:90-96`
`messages` list grows across all provider retries. All-fail scenario: memory unbounded.
**Fix:** Clear `messages` at start of each provider attempt.

### 7. CLIProvider stdin not closed on error
**File:** `tero2/providers/cli.py:176-186`
If `write()`/`drain()` raises (not BrokenPipe), stdin stays open → process hangs.
**Fix:** Wrap stdin ops in try/finally with `stdin.close()`.

### 8. VerifierPlayer stderr task never cleaned up
**File:** `tero2/players/verifier.py:189-196`
`stderr_task` created but never awaited/cancelled. Resource leak.
**Fix:** Await or cancel stderr_task after process completes.

### 9. context_assembly division by zero
**File:** `tero2/context_assembly.py:52-59`
`target_ratio=0.0` (corrupt config) → `ZeroDivisionError`.
**Fix:** Check `target_ratio <= 0` with `<` instead of `<=`, or add explicit zero guard.

---

## HIGH (13)

### 10. runner: no plan_file validation before mark_started
**File:** `tero2/runner.py:106-111`
Non-existent `plan_file` gets saved to state. On restart, tries to read missing file.
**Fix:** Check `plan_file.is_file()` before `mark_started()`.

### 11. runner: TOCTOU in override checking
**File:** `tero2/runner.py:309-333`
60-second sleep misses STOP directive added during wait.
**Fix:** Poll in 5-second intervals, check for STOP inside sleep loop.

### 12. runner: signal handler race condition
**File:** `tero2/runner.py:88-95`
SIGTERM/SIGINT can arrive between event creation and handler setup.
**Fix:** Track `handlers_added` flag; only remove if actually added.

### 13. state: __setattr__ validation bypass on from_json
**File:** `tero2/state.py:93-102`
First assignment skips validation. Loading JSON with invalid phases accepted.
**Fix:** Add post-load validation in `from_json()`.
**Fixed (by design):** The bypass is intentional for crash recovery — any valid saved Phase/SoraPhase must be restorable from disk. Invalid strings are coerced to IDLE/NONE via per-field enum coercion (lines 143–151). Tests: `TestBug13SetAttrBypassIsIntentional` (3 tests).

### 14. architect: disk write outside try/except
**File:** `tero2/players/architect.py:116`
`disk.write_file()` after try/except block. Write failure propagates instead of `success=False`.

### 15. coach: disk writes outside try/except
**File:** `tero2/players/coach.py:76-83`
4 `write_file()` calls unprotected. Same issue as #14.

### 16. scout: disk write outside try/except
**File:** `tero2/players/scout.py:72`
Same issue as #14.

### 17. builder: disk write outside try/except
**File:** `tero2/players/builder.py:85`
Same issue as #14.

### 18. circuit_breaker: HALF_OPEN stuck forever
**File:** `tero2/circuit_breaker.py:28-37`
`check()` returns None in HALF_OPEN. Without `record_success()`, stays HALF_OPEN indefinitely.
**Fix:** Allow one trial request; block subsequent until success/failure recorded.
**Fixed:** `_trial_in_progress` flag added. First `check()` in HALF_OPEN sets it and returns; subsequent calls raise `CircuitOpenError` until `record_success()` or `record_failure()` clears it. Tests: `TestBug18HalfOpenOneTrial` (3 tests).

### 19. usage_tracker: race condition on shared dict
**File:** `tero2/usage_tracker.py:106-126`
`record_step()` modifies `_providers` dict without locking.
**Fix:** Add asyncio.Lock or threading.Lock.

### 20. reflexion: UTF-8 truncation breaks characters
**File:** `tero2/reflexion.py:76-77`
`output[:MAX]` slices mid-multibyte → invalid UTF-8 on disk/network.
**Fix:** `output.encode('utf-8')[:MAX].decode('utf-8', errors='ignore')`.

### 21. shell provider: subprocess not cleaned on exception
**File:** `tero2/providers/shell.py:26-38`
`communicate()` exception leaves zombie process.
**Fix:** try/finally with `proc.terminate()`.

### 22. tui/app: query_one without try/except in event consumer
**File:** `tero2/tui/app.py:86-90`
Screen transition during event processing → `NoMatches` crash.
**Fix:** Wrap widget queries in try/except.

---

## MEDIUM (10)

### 23. runner: exponential backoff overflow
**File:** `tero2/runner.py:289-292`
Large `attempt` → float overflow. Cap at `min(attempt-1, 10)`.

### 24. runner: off-by-one in slice loop
**File:** `tero2/runner.py:483-527`
`while extra_slices_done < max_slices` runs max_slices+1 total (includes S01).

### 25. disk_layer: can't distinguish empty vs missing vs permission denied
**File:** `tero2/disk_layer.py:42-94`
`read_file` returns `""` for all error cases.
**Fixed:** Separate exception handlers: `FileNotFoundError → None`, other `OSError → ""`. Missing file is now distinguishable from an empty file (`None` vs `""`). Tests: `TestBug25DiskLayerErrorTypes` (3 tests).

### 26. context_assembly: O(n²) optional section processing
**File:** `tero2/context_assembly.py:144-154`
Rebuilds entire string for each optional section check.
**Fixed:** O(n) incremental token accumulation. Instead of rebuilding the full candidate string per iteration, a running `running_tokens` total is updated when a section is accepted. Also restored `raise ConfigError` for `target_ratio <= 0` (was accidentally changed to silent `HARD_FAIL`). Tests: `TestBug26ContextAssemblyPriorityOrder` (4 tests).

### 27. telegram_input: no file size check on download
**File:** `tero2/telegram_input.py:277-298`
Malicious large file → memory/disk exhaustion.

### 28. project_init: sanitization can produce empty string
**File:** `tero2/project_init.py:44-50`
Name "!!!" → empty string → project in root dir.

### 29. escalation: inconsistent checkpointing across levels
**File:** `tero2/escalation.py:111-176`
State partially updated if checkpoint fails mid-level.

### 30. stuck_detection: mutates state in-place
**File:** `tero2/stuck_detection.py:84-101`
Caller doesn't expect `state.last_tool_hash` to change as side effect.
**Fixed:** `update_tool_hash` uses `dataclasses.replace()` and returns a new `AgentState`; original is never mutated. Tests: `TestBug30NoStateMutation` (3 tests).

### 31. ProviderChain: index not updated on circuit breaker skip
**File:** `tero2/providers/chain.py:72-73`
`_current_provider_index` stale when provider skipped via open circuit breaker.
**Fixed:** Index is set AFTER the `cb.is_available` check, so skipped providers never update it. A skipped provider leaves the index at its previous value; the next available provider sets it correctly. Also fixed `yield from messages` (invalid in async generators) → `for msg in messages: yield msg`. Tests: `TestBug31ProviderChainIndexUpdate` (1 test).

### 32. TUI screens: stat() in sort without try/except
**File:** `tero2/tui/screens/project_pick.py:38-46`, `tero2/tui/screens/plan_pick.py:38-50`
`p.stat().st_mtime` in sort key → OSError crashes scan.

---

## Patterns

- **Disk writes unprotected** — all 4 players do `write_file()` outside try/except (systemic)
- **Silent failures** — `from_json`, `read_file`, bare `except: pass` hide real errors
- **TOCTOU in async+disk** — file checks then long sleeps; file changes missed mid-operation
- **Resource leaks** — subprocess stdin, stderr tasks, file descriptors not cleaned on error paths

---

## M1 TUI Redesign Bugs (33–39)

Found 2026-04-18 during post-implementation audit. All in code introduced by the TUI redesign (Task 4–8 of requirements.md).

---

### 33. project_pick: `n` key crashes with DuplicateIds on any press

**Severity:** HIGH
**File:** `tero2/tui/screens/project_pick.py:70–71`

```python
def action_manual_input(self) -> None:
    self.mount(Input(placeholder="Путь к проекту…", id="path-input"))
```

Two crash paths: (A) when history is empty, `compose()` already yields `Input(id="path-input")`; first press of `n` tries to mount a second with the same id → `DuplicateIds`. (B) when history is present, second press of `n` → same crash. `self._manual_mode = False` in `__init__` was clearly intended as a guard but is never read.

**Fix:** Guard with `try: self.query_one("#path-input") except NoMatches: self.mount(...)`.

---

### 34. app.py: BINDINGS drift — `n`/`o` keys dead, stuck labels blank

**Severity:** MEDIUM
**File:** `tero2/tui/app.py:29–41`

`action_new_project` and `action_settings` exist but are not in `BINDINGS` → `n` and `o` do nothing in the TUI. Stuck option labels are empty strings `""` instead of `"1 retry"`, `"2 switch"`, etc. — the Footer is useless during stuck state. Label `"Стир"` should be `"Указание"`, `"Изменить план"` should be `"Смена плана"`.

**Fix:** Add missing entries; copy BINDINGS from requirements.md Task 4 spec.

---

### 35. plan_pick / startup_wizard: skip filter uses absolute path parts

**Severity:** MEDIUM
**File:** `tero2/tui/screens/plan_pick.py:44`, `tero2/tui/screens/startup_wizard.py:73`

```python
if any(part in _SKIP for part in p.parts):   # p.parts is absolute
```

If any **ancestor** of `_project_path` matches a skip-dir name (e.g. project in `~/dist/myproject/`, `~/node_modules/myproject/`), all `.md` files are excluded. The screen shows "no plans" and auto-dismisses with `None`.

**Fix:** Use `p.relative_to(self._project_path).parts` to limit the check to project-internal path components.

---

### 36. project_pick: "press d to delete" notification references unimplemented key

**Severity:** LOW
**File:** `tero2/tui/screens/project_pick.py:61`

```python
self.notify("Папка не найдена — удалить из истории? (d)", severity="warning")
```

No `d` binding or handler exists. Pressing `d` has no effect. Stale entries accumulate in `~/.tero2/history.json` with no way to remove them from the wizard UI.

---

### 37. history.py: `last_plan` stores bare filename, losing subdirectory context

**Severity:** LOW
**File:** `tero2/history.py:36`

```python
plan_str = plan_file.name if plan_file else None  # "plan.md" — ambiguous
```

Projects with multiple `plan.md` files in different subdirs produce identical `last_plan` values. Any future feature that auto-selects the last plan cannot identify the correct file.

**Fix:** Store `str(plan_file.relative_to(project_path))` — needs `project_path` threaded into `record_run`.
**Fixed:** `record_run` already uses `plan_file.relative_to(project_path)` with fallback to `plan_file.name` for out-of-project paths. Tests: `TestBug37HistoryRelativePath` (2 tests).

---

### 38. cli.py: wizard-path `project_path` not re-validated before DashboardApp launch

**Severity:** LOW
**File:** `tero2/cli.py:136–140`

The direct-path branch validates `project_path.is_dir()` before launch; the wizard branch does not. `ProjectPickScreen` checks `is_dir()` at selection time, but a TOCTOU window exists (network drive unmounted, directory deleted). `Runner.__init__` will raise `FileNotFoundError` without a user-friendly message.

**Fix:** Add `if not project_path.is_dir(): print(...); sys.exit(1)` after the wizard branch, same as the direct-path branch.

---

### 39. cli.py: `_WizardApp` has no `CSS_PATH` — wizard renders unstyled

**Severity:** LOW
**File:** `tero2/cli.py:107–113`

`_WizardApp(App)` has no `CSS_PATH`. `ProjectPickScreen` and `PlanPickScreen` use classes `"screen-title"`, `"path-label"`, `"entry-warning"`, `"plan-name"` — all defined in `tero2/tui/styles.tcss`. Since `_WizardApp` doesn't load this file, the wizard renders with no styling when invoked via `tero2 go` (no args).

**Fix:** Add `CSS_PATH = Path(__file__).parent / "tui" / "styles.tcss"` to `_WizardApp`.
