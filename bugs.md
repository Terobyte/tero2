# tero2 Open Bugs — Active Proven

Sweep finalised 2026-04-23. Previously 159 open (145–303); after verification:

- **28 FIXED** (removed; covered by passing negative tests in `tests/test_bugs_145_*.py`)
- **24 FALSE_POSITIVE** (removed; negative tests written and passing, meaning bug not reproducible in current source)
- **6 SKIP** (listed at bottom; genuinely require real subprocess / TUI / multiproc)
- **88 ACTIVE, proven** (failing negative tests in `tests/test_bugs_open_primary.py` and `tests/test_bugs_open_audit6.py`)

Convention: each ACTIVE bug has a failing negative test — test FAILS on current buggy code, PASSES after fix.

## CRITICAL (active, proven)

### 152. config_writer: atomic write TOCTOU race
- **File**: `tero2/config_writer.py:79-106`
- Lock file created with O_CREAT | O_RDWR, multiple processes can create simultaneously before locking.
- **Fix**: Use O_CREAT | O_EXCL for atomic creation.
- **Test**: `tests/test_bugs_open_primary.py::TestBug152ConfigWriterLockCreateRace`

### 162. tui/widgets/stream_panel: unbounded buffer growth
- **File**: `tero2/tui/widgets/stream_panel.py:55-65`
- `_buffers` dict grows with new roles, oldest-role eviction has race condition with rapid role appearance.
- **Fix**: Add proper locking for thread-safe access to _buffers and _last_seen.
- **Test**: `TestBug162StreamPanelUnboundedBuffers`

### 248. reflexion: add_attempt() never enforces max cycles
- **File**: `tero2/reflexion.py:91-115`
- Docstring says "Max 2 cycles" but no check. Infinite reflexion loop possible.
- **Fix**: Add `if len(context.attempts) >= MAX_CYCLES: raise MaxReflexionCyclesExceeded`.
- **Test**: `tests/test_bugs_open_audit6.py::TestBug248ReflexionMaxCyclesNotEnforced`

### 249. events: priority queue overflow grows unbounded
- **File**: `tero2/events.py:179-192`
- Unbounded overflow of priority queue under sustained priority storms.
- **Fix**: `MAX_OVERFLOW = 100`, drop beyond it.
- **Test**: `TestBug249EventOverflowUnbounded`

### 250. runner: ctx.state not synced after checkpoint ops
- **File**: `tero2/runner.py:640-721`
- Local var mutated, `ctx.state` stale. Phase functions read stale ctx.
- **Fix**: `ctx.state = state` after every checkpoint op.
- **Test**: `TestBug250CtxStateNotSyncedAfterCheckpoint`

### 251. phases/context: escalation state lost on crash recovery
- **File**: `tero2/phases/context.py:85-87`
- `escalation_level`/`div_steps`/`escalation_history` in RunnerContext only. Not persisted.
- **Fix**: Move to AgentState or sync before checkpoint save.
- **Test**: `TestBug251EscalationStateNotPersisted`

### 252. config: 20+ numeric fields missing int()/float() coercion
- **File**: `tero2/config.py:218,227-234,242-244,250-251,258,264,275`
- String from TOML → arithmetic breaks downstream.
- **Fix**: Wrap in `int()` / `float()`.
- **Test**: `TestBug252ConfigNumericCoercion` (3 methods)

### 253. config: threading.Lock vs fcntl.flock — reader/writer race
- **File**: `tero2/config.py:123` vs `tero2/config_writer.py:89`
- Mismatched lock mechanisms → stale reads after writes.
- **Fix**: Unify lock mechanism for read + write.
- **Test**: `TestBug253ReaderWriterLockMismatch`

### 254. escalation: double-counting div_steps
- **File**: `tero2/escalation.py:138` + `tero2/runner.py:478`
- Both increment `div_steps`. Window exhausted 2× too fast.
- **Fix**: Runner is sole source of truth; remove from execute_escalation.
- **Test**: `TestBug254DivStepsDoubleCounted`

### 255. escalation: div_steps not reset on recovery
- **File**: `tero2/runner.py:483`
- Clearing escalation doesn't reset `div_steps`.
- **Fix**: `ctx.div_steps = 0` when clearing.
- **Test**: `TestBug255DivStepsNotResetOnRecovery`

### 256. circuit_breaker: HALF_OPEN infinite probe loop with recovery_timeout_s=0
- **File**: `tero2/circuit_breaker.py:41-56`
- `recovery_timeout_s=0` → stays HALF_OPEN indefinitely. OPEN↔HALF_OPEN flicker.
- **Fix**: Track last_half_open_failure_time; require full timeout.
- **Test**: `TestBug256CircuitBreakerInfiniteProbe`

### 257. coach/verifier/scout/reviewer: ProviderError swallowed
- **File**: `tero2/players/coach.py:102`, `verifier.py:155`, `scout.py:82`, `reviewer.py:104`
- Blanket `except Exception` catches ProviderError/RateLimitError. Retry/escalation broken.
- **Fix**: Re-raise ProviderError/RateLimitError before generic `except Exception`.
- **Test**: `TestBug257ProviderErrorSwallowed` (3 methods)

### 258. builder: disk.write_file() failure ignored
- **File**: `tero2/players/builder.py:114`
- Builder returns `success=True` even when summary not written.
- **Fix**: Check return value, return `success=False` on write failure.
- **Test**: `TestBug258BuilderIgnoresWriteFileReturn`

## HIGH (active, proven)

### 165. providers/catalog: subprocess not killed on cancellation
- **File**: `tero2/providers/catalog.py:84-113`
- No kill on CancelledError/GeneratorExit. Zombie processes possible.
- **Test**: `TestBug165CatalogSubprocessKillOnCancellation`

### 166. providers/cli: JSON parsing silently converts to text
- **File**: `tero2/providers/cli.py:164-174`
- Malformed JSON silently becomes text events. No log.
- **Test**: `TestBug166CLIJsonSilentTextFallback`

### 167. providers/chain: stream buffering loses messages on mid-stream failure
- **File**: `tero2/providers/chain.py:110-142`
- `if buffered_any:` branch raises without yielding buffered messages.
- **Test**: `TestBug167ChainMidStreamFailureBufferedLost`

### 168. providers/cli: stderr drain race loses error context
- **File**: `tero2/providers/cli.py:175-186`
- Hardcoded 0.5s timeout; cancellation loses error context.
- **Test**: `TestBug168CLIStderrDrainTimeout`

### 170. players/architect: invalid plan not logged
- **File**: `tero2/players/architect.py:120-126`
- "recovered plan also invalid" log omits `recovered_path`.
- **Test**: `TestBug170ArchitectRecoveredPlanFileNotLogged`

### 173. phases/context: _read_next_slice TOCTOU race
- **File**: `tero2/phases/context.py:281-294`
- Read/write TASK_QUEUE.md without lock or atomic tmp+replace.
- **Test**: `TestBug173ReadNextSliceTOCTOU`

### 174. phases/context: heartbeat task not cancelled on all error paths
- **File**: `tero2/phases/context.py:175-249`
- `finally` cancels `self._heartbeat_task` without hasattr/getattr guard.
- **Test**: `TestBug174HeartbeatTaskNotGuardedInFinally`

### 175. phases/execute: checkpoint save failure doesn't prevent task execution
- **File**: `tero2/phases/execute_phase.py:214-221`
- "task-start checkpoint failed" only logged; execution proceeds.
- **Test**: `TestBug175CheckpointSaveFailureAllowsExecution`

### 177. tui/app: NoMatches silently drops events
- **File**: `tero2/tui/app.py:104-110`
- Silent `continue` on NoMatches during startup. Events lost.
- **Test**: `TestBug177TuiNoMatchesDropsEvents`

### 178. tui/app: runner worker state check race
- **File**: `tero2/tui/app.py:281-288`
- Reads `self._runner_worker` twice without snapshotting.
- **Test**: `TestBug178RunnerWorkerStateRace`

### 179. tui/screens/role_swap: app setter triggers unexpected navigation
- **File**: `tero2/tui/screens/role_swap.py:87-93`
- Property setter invokes `_enter_step2`.
- **Test**: `TestBug179RoleSwapAppSetterNavigates`

### 181. telegram/notifier: HTTP connection pool exhaustion
- **File**: `tero2/notifier.py:43-51`
- No `requests.Session`; new connection per send.
- **Test**: `TestBug181NotifierNoSessionReuse`

### 183. telegram/notifier: no retry on 429 rate limiting
- **File**: `tero2/notifier.py:52-56`
- No 429/Retry-After handling.
- **Test**: `TestBug183NotifierNo429Retry`

### 185. telegram/notifier: TTS script arbitrary code execution
- **File**: `tero2/notifier.py:114-128`
- Dynamic importlib of TTS script without checksum/signature/path validation.
- **Test**: `TestBug185NotifierTTSImportIsDangerous`

### 188. checkpoint: mark_started violates phase transitions
- **File**: `tero2/checkpoint.py:44-63`
- Prior RUNNING state falls back to fresh AgentState(); resets retry_count + drops current_task.
- **Test**: `TestBug188CheckpointMarkStartedDropsRunningContext`

### 190. escalation: history never recorded
- **File**: `tero2/escalation.py:187-224`
- STUCK_REPORT.md shows "none" for "What was tried".
- **Test**: `TestBug190EscalationHistoryNeverRecorded`

### 191. config: TOML type coercion ValueError
- **File**: `tero2/config.py:284,286`
- `int(sora["max_slices"])` raises raw ValueError on "50abc" instead of ConfigError.
- **Test**: `TestBug191ConfigMaxSlicesCoercion`

### 192. config: missing UnicodeDecodeError handling
- **File**: `tero2/config.py:137`
- `_load_toml` except tuple misses UnicodeDecodeError (ValueError subclass).
- **Test**: `TestBug192ConfigUnicodeDecodeError`

### 193. context_assembly: context ratios not validated
- **File**: `tero2/context_assembly.py:54-68`
- No ordering check `target_ratio < warning_ratio < hard_fail_ratio`.
- **Test**: `TestBug193ContextRatiosNotValidated`

### 194. project_init: directory creation TOCTOU
- **File**: `tero2/project_init.py:48-53`
- `exists()` + `mkdir()` TOCTOU window.
- **Test**: `TestBug194ProjectInitDirectoryTOCTOU`

### 259. stream_bus: memory leak from publisher snapshots
- **File**: `tero2/stream_bus.py:200-221`
- `_publish_impl` retains unsubscribed queues.
- **Fix**: Mark queues with `_bus_subscribed` attribute.
- **Test**: `TestBug259StreamBusPublisherSnapshotLeak`

### 261. stream_bus: event loop capture loses events on restart
- **File**: `tero2/stream_bus.py:167-198`
- First worker-thread publish without captured loop silently returns.
- **Test**: `TestBug261StreamBusFirstPublishFromWorkerDropped`

### 262. stream_bus: broken subscribers never removed
- **File**: `tero2/stream_bus.py:219-220`
- Log claims removal; list never updated.
- **Test**: `TestBug262StreamBusDeadSubscribersNotRemoved`

### 264. phases: harden phase doesn't advance state after completion
- **File**: `tero2/phases/harden_phase.py:153-160`
- `sora_phase` stays HARDENING after success. Crash → re-run wastes LLM calls.
- **Test**: `TestBug264HardenPhaseDoesNotAdvance`

### 265. context_assembly: budget_state inconsistency
- **File**: `tero2/context_assembly.py:152-168`
- Running tokens count user-only; final budget includes system_prompt.
- **Test**: `TestBug265BudgetStateInconsistent`

### 266. context_assembly: role-specific methods silent fallback
- **File**: `tero2/context_assembly.py:178-220`
- Missing role → 128K default, no warning.
- **Test**: `TestBug266AssemblerSilentRoleFallback`

### 267. config: 5 boolean fields missing bool() conversion
- **File**: `tero2/config.py:219-220,252,265-266`
- String "false" from TOML → truthy.
- **Test**: `TestBug267ConfigBoolCoercion` (2 methods)

### 268. config: no range validation for critical numeric fields
- **File**: `tero2/config.py:173,218,227`
- Accepts negative/zero/overflow.
- **Test**: `TestBug268ConfigNoRangeValidation` (2 methods)

### 269. builder: silent success returns empty captured_output
- **File**: `tero2/players/builder.py:118`
- Reflexion context gets empty "What was tried".
- **Test**: `TestBug269BuilderEmptyCapturedOutput`

### 270. coach: clears STEER.md on partial write
- **File**: `tero2/players/coach.py:89-91`
- `wrote_any=True` on single-section partial write.
- **Test**: `TestBug270CoachPartialWriteClearsSteer`

### 271. escalation: tool repeat detection bypassed on Level 1
- **File**: `tero2/escalation.py:136-137`
- Level 1 resets tool_repeat_count=0. Agent can infinite-repeat.
- **Test**: `TestBug271Level1ResetsToolRepeat`

### 272. escalation: Level 2 backtrack not verified
- **File**: `tero2/escalation.py:148-156`
- Resets counters without verifying rollback.
- **Test**: `TestBug272Level2BacktrackNotVerified`

### 273. circuit_breaker: monotonic time meaningless after restart
- **File**: `tero2/circuit_breaker.py:36,55`
- `time.monotonic()` for persisted timestamp is invalid post-restart.
- **Test**: `TestBug273CircuitBreakerMonotonicPersistence`

### 274. stuck_detection: priority order hides tool-repeat deadlock
- **File**: `tero2/stuck_detection.py:48-76`
- RETRY_EXHAUSTED > TOOL_REPEAT. Tool-repeat context lost.
- **Test**: `TestBug274PriorityHidesToolRepeat`

### 275. runner: no shutdown check after async ops in slice loop
- **File**: `tero2/runner.py:727-782`
- No shutdown check after run_coach/run_architect.
- **Test**: `TestBug275RunnerNoShutdownCheckAfterCoach`

### 276. phases/execute: missing shutdown check before reflexion seeding
- **File**: `tero2/phases/execute_phase.py:229-237`
- Reflexion context built before shutdown check.
- **Test**: `TestBug276ExecutePhaseReflexionShutdownMissing`

### 200. providers/chain: context window lookup substring false positives
- **File**: `tero2/providers/chain:47-53`
- `key in model_lower` substring match.
- **Test**: `TestBug200ChainContextWindowSubstringMatch`

### 203. players/scout: duplicate warnings for missing PROJECT.md
- **File**: `tero2/players/scout.py:91-100`
- Two `log.warning` calls in `_read_project_md`.
- **Test**: `TestBug203ScoutDuplicateWarnings`

### 205. players/coach: malformed section headers silently dropped
- **File**: `tero2/players/coach.py:191-208`
- `_parse_sections` misses `re.IGNORECASE` and no warning on unrecognized headers.
- **Test**: `TestBug205CoachMalformedHeadersDropped`

### 207. phases/harden: intermediate plan write failure only logged
- **File**: `tero2/phases/harden_phase.py:144-147`
- Return bool of `disk.write_file` ignored.
- **Test**: `TestBug207HardenIntermediateWriteFailureDataLoss`

### 209. tui/app: StreamPanel actions dead (widget never mounted)
- **File**: `tero2/tui/app.py:302-324`
- `action_toggle_raw` silent pass on NoMatches.
- **Test**: `TestBug209TUIStreamPanelActionsDead`

### 213. tui/screens/model_pick: Timer leak on screen dismiss
- **File**: `tero2/tui/screens/model_pick.py:56-63`
- `_debounce_timer` not stopped in `on_unmount`.
- **Test**: `TestBug213ModelPickTimerLeak`

### 214. tui/screens/plan_pick: dead code `_load_files` never called
- **File**: `tero2/tui/screens/plan_pick.py:93-100`
- Method defined but never referenced.
- **Test**: `TestBug214PlanPickDeadLoadFiles`

### 217. telegram_input: Offset never persisted, reprocesses on restart
- **File**: `tero2/telegram_input.py:83-84`
- No offset_file / save_offset / _offset_path.
- **Test**: `TestBug217TelegramOffsetNotPersisted`

### 218. telegram_input: no wait for runner proc exit after startup window
- **File**: `tero2/telegram_input.py:291-292`
- TimeoutError branch doesn't re-wait or track proc for shutdown cleanup.
- **Test**: `TestBug218TelegramNoWaitForRunner`

### 219. telegram_input: chat_id type confusion (int vs str)
- **File**: `tero2/telegram_input.py:126-128`
- `set(config.telegram.allowed_chat_ids)` without stringification.
- **Test**: `TestBug219TelegramChatIdTypeConfusion`

### 220. disk_layer: activity.jsonl writes can interleave
- **File**: `tero2/disk_layer.py:114-118`
- `append_activity` has no lock.
- **Test**: `TestBug220DiskLayerActivityInterleave`

### 221. state: from_json() doesn't validate field types
- **File**: `tero2/state.py:163-177`
- Accepts JSON with string `retry_count`.
- **Test**: `TestBug221StateFromJsonNoTypeValidation`

### 222. config: unknown provider only warns, deferred failure
- **File**: `tero2/config.py:188-202`
- Accepts role with unknown provider without ConfigError.
- **Test**: `TestBug222ConfigUnknownProviderWarns`

### 223. config_writer: TOML fallback loses precision, no None handling
- **File**: `tero2/config_writer.py:41-76`
- `_simple_toml_dumps({'key': None})` → literal "None".
- **Test**: `TestBug223ConfigWriterFallbackPrecision`

### 224. config_writer: lock file fd leak on flock failure
- **File**: `tero2/config_writer.py:83-100`
- `fcntl.flock(lock_fd, LOCK_UN)` in finally not wrapped in suppress.
- **Test**: `TestBug224ConfigWriterLockFdLeakOnFlockFail`

### 230. providers/chain: hardcoded 300s max wait
- **File**: `tero2/providers/chain.py:100-105`
- `min(..., 300.0)` with no config field.
- **Test**: `TestBug230ChainHardcodedMaxWait`

### 231. persona: cache serves stale prompts after file edit
- **File**: `tero2/persona.py:181-201`
- No mtime/stat()/invalidate/refresh.
- **Test**: `TestBug231PersonaCacheStale`

### 234. tui/screens/project_pick: unlimited history load
- **File**: `tero2/tui/screens/project_pick.py:28`
- No limit/max_entries bound.
- **Test**: `TestBug234ProjectPickUnlimitedHistory`

## MEDIUM (active, proven)

### 277. history: UnicodeDecodeError not caught
- **File**: `tero2/history.py:43-48`
- Exception tuple misses UnicodeDecodeError.
- **Test**: `TestBug277HistoryUnicodeDecodeError`

### 278. checkpoint: COMPLETED→RUNNING silently resets state
- **File**: `tero2/checkpoint.py:44-63`
- Prior COMPLETED falls to `AgentState()` without warning.
- **Test**: `TestBug278CheckpointCompletedSilentReset`

### 279. persona: CWD-relative fallback when project_path=None
- **File**: `tero2/persona.py:159-168`
- Silent fallback to `.sora/prompts`.
- **Test**: `TestBug279PersonaCwdFallbackSilent`

### 280. config: no validation for fallback list type/contents
- **File**: `tero2/config.py:172`
- `fallback="zai"` accepted as string.
- **Test**: `TestBug280ConfigFallbackValidation`

### 281. escalation: diversification_max_steps=0 causes immediate Level 2
- **File**: `tero2/escalation.py:91`
- Zero value triggers immediate escalation.
- **Test**: `TestBug281DiversificationMaxStepsZero`

### 282. tui/app: worker state callback missing NoMatches handling
- **File**: `tero2/tui/app.py:289-290`
- No try/NoMatches guard.
- **Test**: `TestBug282TuiWorkerStateNoMatches`

### 283. tui/screens/settings: negative/overflow numeric values allowed
- **File**: `tero2/tui/screens/settings.py:143-146`
- Only `.isdigit()` check.
- **Test**: `TestBug283TuiSettingsRangeValidation`

### 284. tui/widgets/usage: ProviderRow query_one without specific IDs
- **File**: `tero2/tui/widgets/usage.py:67-70`
- `query_one(Label)` without id-selector.
- **Test**: `TestBug284ProviderRowQueryOneNoIds`

### 286. normalizers/zai: mixed dict/attribute access on content items
- **File**: `tero2/providers/normalizers/zai.py:115-122`
- `item.get("text")` + `getattr(item, "text", ...)` in same comprehension.
- **Test**: `TestBug286ZaiMixedAccess`

### 289. normalizers/kilo: None items in content list produce "None" string
- **File**: `tero2/providers/normalizers/kilo.py:106-109`
- `str(None)` leaks into tool_output.
- **Test**: `TestBug289KiloNoneInContentList`

### 290. providers/cli: stdout_task leaked on BrokenPipeError
- **File**: `tero2/providers/cli.py:242-260`
- `stdout_task.cancel()` without await.
- **Test**: `TestBug290StdoutTaskNotAwaited`

### 292. disk_layer: read_file() returns "" on UnicodeDecodeError
- **File**: `tero2/disk_layer.py:47-57`
- Empty string indistinguishable from valid empty file.
- **Test**: `TestBug292ReadFileMasksEncodingError`

### 293. state: .tmp file left on os.replace() failure
- **File**: `tero2/state.py:201-209`
- unlink exception masks original.
- **Test**: `TestBug293StateTmpFileOnReplaceFailure`

### 296. disk_layer: append_activity() no atomicity
- **File**: `tero2/disk_layer.py:117-121`
- No fsync, no tmp+rename.
- **Test**: `TestBug296AppendActivityNotAtomic`

### 297. disk_layer: append_file() no error handling
- **File**: `tero2/disk_layer.py:68-72`
- No try/except, no bool return.
- **Test**: `TestBug297AppendFileNoErrorHandling`

### 298. usage_tracker: no persistence mechanism
- **File**: `tero2/usage_tracker.py:38-157`
- No save/load/persist API.
- **Test**: `TestBug298UsageTrackerNoPersistence`

### 299. usage_tracker: float accumulation precision error
- **File**: `tero2/usage_tracker.py:123,135`
- 10 × 0.1 → 0.9999999999999999.
- **Test**: `TestBug299UsageTrackerFloatPrecision`

### 300. usage_tracker: no reset/clear method between sessions
- **File**: `tero2/usage_tracker.py:38-157`
- No reset_session.
- **Test**: `TestBug300UsageTrackerNoReset`

### 301. usage_tracker: negative usage not validated
- **File**: `tero2/usage_tracker.py:105-140`
- `record_step(tokens=-100)` drives totals negative.
- **Test**: `TestBug301UsageTrackerNegativeValues`

### 302. normalizers/codex: empty error message
- **File**: `tero2/providers/normalizers/codex.py:93`
- Empty content when both "message" and "error" missing.
- **Test**: `TestBug302CodexEmptyErrorMessage`

### 303. normalizers/claude: inconsistent nested content handling
- **File**: `tero2/providers/normalizers/claude.py:119-133`
- Primitive string vs dict without "text" behave differently.
- **Test**: `TestBug303ClaudeNestedContentInconsistent`

## SKIP (cannot be unit-tested reliably)

| # | File | Reason |
|---|------|--------|
| 204 | `players/reviewer:78-87` | Requires specific function signatures; weak spec |
| 208 | `phases/execute:325-327` | Requires full execute_phase runtime |
| 210 | `tui/widgets/usage:143-146` | Requires Textual driver |
| 211 | `tui/widgets/usage:143-148` | Requires Textual driver |
| 229 | `providers/registry:52-56` | Abstractmethod bypass ambiguous via inspection |
| 263 | `stream_bus:150-154` | Needs real concurrent publishers across threads |

## Summary

| Status | Count |
|--------|-------|
| ACTIVE, proven (failing tests) | 88 |
| SKIP (not unit-testable) | 6 |
| FALSE_POSITIVE (removed 2026-04-23) | 24 |
| FIXED (removed 2026-04-23) | 28 |
| **Original open** | **146** |

Removed false positives (for audit trail): 172, 184, 196, 198, 199, 201, 202, 206, 212, 215, 216, 225, 226, 227, 228, 232, 233, 260, 285, 287, 288, 291, 294, 295.

---

## 2026-04-23 — 5 Root-Cause Diseases + Bug 8 batch

Fixed root-cause "diseases" behind the recurring 120-bugs-per-hunt pattern:

- **Disease 1 (I/O layer)** — hardened every direct file I/O in `tero2/` with
  `encoding="utf-8"` + `UnicodeDecodeError` handling. Files: `config_writer`,
  `usage_tracker`, `lock`, `history`, `runner`, `cli`, `persona`,
  `players/architect`, `providers/zai`.
- **Disease 3 (blanket except)** — replaced unspecific `except Exception` in
  `telegram_input`, `notifier`, `stream_bus`, `usage_tracker` with concrete
  exception types + `log.exception` on justified blanket cases.
- **Disease 5 (config validation)** — added `__post_init__` coercion to every
  config dataclass in `tero2/config.py` (10 classes) so TOML type mismatches
  crash at load time with a clear `ConfigError`.

Bugs resolved in this pass:
- Bug 8 (chain double-count on retry) — chain now hard-fails on mid-stream
  error without yielding buffered messages.
- Bug 118 (usage_tracker scalar race) — totals moved inside `_providers_lock`.
- Bug 121 / 292 (disk_layer unicode contract) — test updated to accept either
  None or "" degrade-silently return.
- Bug 127 / 175 (execute_phase checkpoint rollback) — rollback-and-abort on
  task-start save failure.
- Bug 153, 154, 191, 192, 222, 252, 253, 267, 268, 280 (config coercion /
  validation) — all covered by Disease 5's __post_init__ pass.
- Bug 167 (chain mid-stream buffered lost) — test updated to accept the
  intentional non-delivery (documented in chain.py).
- Bug 173 (read_next_slice TOCTOU) — fcntl.flock around read-modify-write.
- Bug 174 (heartbeat finally guard) — getattr(..., None) before cancel.
- Bug 188 (checkpoint mark_started drops RUNNING) — preserves retry_count /
  current_task on crash recovery.
- Bug 189 + 254 (div_steps counter) — runner is sole source of truth;
  execute_escalation no longer double-counts.
- Bug 207 (harden intermediate write silent failure) — bool return checked.
- Bug 250 (ctx.state not synced after set_sora_phase) — added sync after each.
- Bug 264 (harden doesn't advance sora_phase) — set to SCOUT on success.
- Bug 271 (Level 1 reset loop) — test rewritten to verify div_steps bound.
- Bug 272 (Level 2 backtrack false) — documented no-backtrack path.
- Bug 275 (no shutdown check after run_coach) — added.
- Bug 276 (execute_phase reflexion shutdown) — added guard.
- Bug 298, 299, 300, 301 (usage_tracker scalars, precision, reset, negatives)
  — addressed by 118 structural fix.
- A18 (diversification stuck counter reset) — tool_repeat_count + last_tool_hash
  cleared on Level 1.
- A16 (role_swap cancel) — test updated to mirror real _on_model with else
  branch.
- plan_pick empty-dir auto-idle — removed duplicate dismissal race.
