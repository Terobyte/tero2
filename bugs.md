# tero2 Bug Audit

## OPEN Bugs (0)

---

## FIXED Bugs (5)

### Bug 6 — `_execute_plan` ignores existing `retry_count` after crash recovery

**Severity:** High
**Status:** ✅ Fixed — `range(state.retry_count, self.config.retry.max_retries)`
**Location:** `runner.py` — `_execute_plan` retry loop
**Proof:** `test_retry_count_respects_max_after_crash_recovery`, `test_execute_plan_source_accounts_for_retry_count`

`_execute_plan` uses `range(self.config.retry.max_retries)` which always starts from 0, ignoring the `retry_count` already stored in the restored state. After crash recovery with `retry_count=2` and `max_retries=3`, the loop runs 3 more times for a total of 5 attempts — exceeding the configured maximum.

**Impact:** Runner silently exceeds the retry budget. A task that should fail after 3 attempts instead gets 3+ extra attempts after each crash, potentially spending hours on a doomed task.

---

### Bug 7 — `max_steps_per_task` exceeded causes infinite crash loop

**Severity:** Critical
**Status:** ✅ Fixed — `increment_step` no longer raises; STEP_LIMIT detected mid-attempt in `_run_agent`
**Location:** `checkpoint.py:89-93` — `increment_step`
**Proof:** `test_max_steps_exceeded_marks_failed_not_crash`, `test_increment_step_does_not_raise_on_limit`

`increment_step` increments `steps_in_task`, checks against max, and raises `RuntimeError` **before** calling `self.save()`. The state on disk is stale. `RuntimeError` is not in `_is_recoverable_error`, so it propagates through `_run_agent` → `_execute_plan` → `run()` without any catch-all. State stays RUNNING on disk. On restart, the runner restores the stale state, hits the same check, and crashes again — infinite loop.

**Impact:** Once a task hits the step limit, the runner enters an unrecoverable crash loop. The only escape is manual intervention (deleting STATE.json).

---

### Bug 8 — `ProviderChain` yields duplicate messages on internal retry

**Severity:** High
**Status:** ✅ Fixed — messages buffered in chain; only forwarded to consumer on success
**Location:** `chain.py:66-74` — `ProviderChain.run` retry loop
**Proof:** `test_no_duplicate_messages_on_retry`, `test_runner_receives_duplicate_tool_results_via_chain`

When a provider yields some messages (e.g., `tool_result`) and then raises a recoverable error (e.g., `RateLimitError`), the chain catches it and retries the same provider. The consumer receives **all** messages from **all** attempts — including duplicates. With `rate_limit_max_retries=1`, a provider that yields 2 messages produces 4 total (2 original + 2 duplicate).

**Impact:** The runner calls `increment_step` for each duplicate message, inflating the step count. With 3 retries in `_execute_plan`, a single failing provider causes 12 step increments instead of 2. This can prematurely exhaust `max_steps_per_task` (see Bug 7) and cause the runner to process the same tool results multiple times (duplicate file edits, duplicate commands, etc.).

---

### Bug 9 — Non-recoverable exceptions leave state as RUNNING (no FAILED mark)

**Severity:** High
**Status:** ✅ Fixed — `except Exception` catch-all in `run()` marks FAILED before re-raising
**Location:** `runner.py:64-87` — `Runner.run`
**Proof:** `test_config_error_marks_state_failed`, `test_runtime_error_marks_state_failed`, `test_run_source_has_catch_all_for_failed_state`

`Runner.run()` only catches `LockHeldError`. Any other exception that propagates out of `_execute_plan` (e.g., `ConfigError`, `RuntimeError`) exits without saving FAILED state. The `finally` block only releases the lock. On next run, the state is RUNNING with stale data, and `_execute_plan` starts a fresh retry loop (see Bug 6).

**Impact:** After any unhandled crash, the runner restores a RUNNING state on next start. Combined with Bug 6, this can give the runner a full extra set of retry attempts. The FAILED state is never persisted, making it impossible to detect failure from the CLI (`tero2 status` shows "running").

---

### Bug 10 — `CLIProvider` yields stdout before checking exit code

**Severity:** Medium
**Status:** ✅ Fixed — stdout buffered; exit code checked before yielding
**Location:** `cli.py:185-194` — `CLIProvider.run`
**Proof:** `test_cli_provider_source_checks_exit_code_before_yield`, `test_cli_provider_does_not_yield_on_nonzero_exit`

`CLIProvider.run()` yields all stdout lines via `async for line in proc.stdout`, then checks `proc.returncode`. If the process exits non-zero, all output has already been delivered to the consumer. The consumer processes it as valid tool results, then `ProviderError` is raised.

**Impact:** Output from a failed command is treated as successful tool results. The runner may execute actions based on corrupt/incomplete output. Combined with Bug 8, the chain then retries, executing the same prompt again — all previous actions are duplicated.
