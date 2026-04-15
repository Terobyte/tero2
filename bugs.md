# tero2 Bug Audit

## OPEN Bugs (5) — 9/9 tests RED, 0 false positive

| # | Bug | Severity | TDD Proof |
|---|-----|----------|-----------|
| 13 | ProviderError from CLI crash kills runner | CRITICAL | **RED** (2 tests) |
| 14 | config.retry.max_retries ignored (hardcoded constant) | HIGH | **RED** |
| 15 | No max_steps_per_task enforcement | HIGH | **RED** (2 tests) |
| 16 | lock.release() deletes other process's lock file | MEDIUM | **RED** (2 tests) |
| 17 | Telegram Markdown parse_mode fails on special chars | MEDIUM | **RED** (2 tests) |

Tests: `tests/test_bugs.py`

---

### 13. `ProviderError` from CLI crash kills the entire runner — **OPEN** (`chain.py:50-51`, `runner.py:198-202`)

When a CLI tool exits non-zero (segfault, OOM, bad args), `cli.py:194` raises `ProviderError`. But `_is_recoverable_error()` only recognizes `RateLimitError`, `ProviderTimeoutError`, `ProviderNotReadyError`. The chain re-raises immediately without trying the next provider, and the exception propagates unhandled through `_execute_plan` → `run()` → process crash.

**TDD proof:**
- `test_chain_tries_fallback_on_providererror` — chain with crasher + fallback re-raises `ProviderError` instead of trying fallback. **RED**: `ProviderError: cli segfaulted`.
- `test_run_agent_returns_false_on_providererror` — `_run_agent` should return `False` but re-raises `ProviderError`. **RED**: unhandled exception propagates.

**Fix:** Treat `ProviderError` as recoverable (retry with next provider / retry the chain), or catch it in `_run_agent` and return `False`.

### 14. `config.retry.max_retries` ignored — **OPEN** (`runner.py:98`)

```python
for attempt in range(MAX_TASK_RETRIES):  # hardcoded constant = 3
```

Should be `self.config.retry.max_retries`. User-configured `max_retries` in TOML is silently ignored.

**TDD proof:**
- `test_runner_uses_config_max_retries_not_constant` — source of `_execute_plan` contains `MAX_TASK_RETRIES`. **RED**: assertion fails, string found in source.

### 15. No `max_steps_per_task` enforcement — **OPEN** (`runner.py`, `checkpoint.py:87-89`)

`MAX_STEPS_PER_TASK=15` is defined and parsed into `config.retry.max_steps_per_task`, but nothing ever checks `state.steps_in_task >= limit`. A runaway agent loops forever with no step cap.

**TDD proof:**
- `test_increment_step_raises_at_limit` — calls `increment_step` past limit (15 → 16). **RED**: `DID NOT RAISE` — no enforcement exists.
- `test_increment_step_source_has_limit_check` — source of `increment_step` does not contain `max_steps`. **RED**: assertion fails.

### 16. `lock.release()` deletes another process's lock file — **OPEN** (`lock.py:46`)

```python
def release(self) -> None:
    if self._fd is not None:
        # ...funlock + close...
    self.lock_path.unlink(missing_ok=True)  # always runs
```

In `runner.py:77-85`, if `acquire()` raises `LockHeldError`, the `finally` block still calls `release()`. Since `_fd is None`, the flock code is skipped but the lock file is still unlinked — deleting another process's lock.

**TDD proof:**
- `test_release_without_acquire_preserves_lock_file` — creates lock file, sets `_fd=None`, calls `release()`. **RED**: file deleted, `exists()` returns `False`.
- `test_release_skips_unlink_when_fd_is_none` — same scenario. **RED**: file deleted.

**Fix:** Move `unlink` inside the `if self._fd is not None` block.

### 17. Telegram `parse_mode="Markdown"` fails on special characters — **OPEN** (`notifier.py:41`)

```python
data={"chat_id": ..., "text": text, "parse_mode": "Markdown"},
```

No escaping of `_`, `` ` ``, `[`, etc. Messages containing these characters (common in error messages like "step_1" or "`code`") get rejected by Telegram API with HTTP 400. The exception is caught and logged as "telegram send failed" — notifications silently disappear.

**TDD proof:**
- `test_send_does_not_pass_raw_markdown` — sends `"error in step_1: \`code\` failed [done]"` with `parse_mode="Markdown"` without escaping. **RED**: raw special chars + Markdown parse_mode.
- `test_send_source_escapes_or_drops_markdown` — source has `parse_mode="Markdown"` but no `escape`/`replace`. **RED**: assertion fails.
