# Night Loop for tero2 — Autonomous Bug-Hunting & Fix Session

**Date:** 2026-04-21 (night run into 2026-04-22)
**Worktree:** `.claude/worktrees/nifty-hermann-d3480c`
**Branch:** `claude/nifty-hermann-d3480c`
**Operator:** Claude Opus 4.7 (self-paced loop)

## Goal

Exercise `tero2` end-to-end against `tero2-testbed/easy-three.md`, observe real runtime failures, diagnose root causes, commit fixes, and continue iterating through the night. The testbed plan is intentionally trivial (3 string utilities + tests), so any failure is a tero2 defect, not a plan defect.

## Mode Progression

1. **Phase B (headless)** — `tero2 run` without TUI, observe via stdout/stderr, `.sora/` state, `stream_bus` events, testbed git log, pytest output.
2. **Phase A (visual/TUI)** — triggered only when Phase B produces a fully green run (3 commits in testbed, all tests passing, no leaked subprocesses). Switch to `tero2 go`, screenshot + observe TUI layout/responsiveness.

TUI is known-broken per operator; Phase A will therefore generate the bulk of the bug backlog. Phase B validates the orchestration core first.

## Fix Scope

**In scope (may modify):**
- `tero2/` core modules: runner, phases, players, providers, stream_bus, state, task_supervisor, escalation, stuck_detection
- `tero2/tui/` — any TUI layer
- Provider config under `tero2/providers/`

**Out of scope (do not modify without explicit approval):**
- `tero2/persona.py`
- `daemon/` (launchd plist)
- Any file on `main` branch — work stays on `claude/nifty-hermann-d3480c`
- `tero2-testbed/` — only observed, not modified (except `stringy/` artifacts produced by tero2 itself)

**Forbidden actions:** `git push --force`, `git reset --hard main`, `rm -rf`, `gh pr merge`, any modification of `.git/config` or shell profile.

## Loop Architecture

```
iteration N:
  1. run tero2 headless on tero2-testbed/easy-three.md (5-min timeout)
  2. collect evidence:
     - stdout, stderr, exit code
     - .sora/ state snapshot
     - stream_bus event log
     - testbed git log (did commits happen?)
     - pytest -v output in testbed
  3. classify outcome:
     - GREEN (3 commits, all tests pass, clean exit)
       → mark Phase B complete, switch to TUI next iteration
     - PROVIDER_LIMIT (429 / rate-limit signal from one provider)
       → edit providers/ routing, switch default to alternate
       → commit as "switch default provider from X to Y (rate-limited)"
     - TERO2_BUG (crash, stuck, wrong behavior)
       → invoke systematic-debugging skill
       → locate root cause in tero2/ code
       → fix + halal negative test
       → commit as "fix bug N: <summary>" (continue bug numbering from 98+)
     - AMBIGUOUS
       → save evidence to docs/superpowers/night-run-2026-04-22/evidence-N.md
       → continue; review in morning
  4. update .tero2-night-state.json with iteration result
  5. sleep:
     - active fix in progress: ScheduleWakeup 270s (cache warm)
     - waiting on provider reset: ScheduleWakeup 1800s
     - my own Claude limit hit: ScheduleWakeup 3600s, then retry
```

## Rate Limit Handling

**Two distinct failure modes:**

- **My Claude API limit** — I cannot make any tool calls. Recovery: ScheduleWakeup 3600s (max), on wake attempt a trivial tool call; if still limited, sleep again. Max 4 hops (~4h waiting). If still limited after that, loop terminates gracefully with state saved.

- **tero2 provider limit** (Kilo / OpenCode / Codex / zai / Claude-via-tero2) — edit provider config or routing map to switch to alternate provider. Commit the switch. Continue loop.

## State Persistence

File: `.tero2-night-state.json` at worktree root.

```json
{
  "started_at": "2026-04-21T23:00:00+03:00",
  "iteration": 12,
  "phase": "B",
  "bugs_found": [
    {"n": 98, "summary": "stream_bus drops events on rapid fire", "fixed": true, "commit": "abc123"},
    {"n": 99, "summary": "runner timeout doesn't kill grandchildren", "fixed": false}
  ],
  "provider_switches": [
    {"from": "kilo", "to": "opencode", "reason": "rate_limit", "at": "2026-04-22T02:15:00+03:00"}
  ],
  "last_green_run": null,
  "phase_B_complete": false,
  "last_sleep_reason": "active_fix"
}
```

If loop dies mid-run, state file lets morning-me (or operator) resume.

## Stopping Conditions

- **Time:** stop at 08:00 MSK on 2026-04-22 (~9h budget)
- **Stuck:** 5 consecutive iterations with same failure + no progress
- **Exhausted:** all providers rate-limited AND fixes not helping
- **Operator interrupt:** any message from user terminates loop

## Morning Deliverable

- `docs/superpowers/night-run-2026-04-22/report.md` — summary: bugs found/fixed/open, provider switches, unresolved evidence pointers
- Commit series `fix bug N: ...` on `claude/nifty-hermann-d3480c`
- Updated `bugs.md` with any unresolved issues
- `.tero2-night-state.json` final snapshot

## Out-of-Scope for This Session (Deferred)

- PR creation — operator reviews commits and opens PR manually
- Main branch merge
- Documentation updates beyond bug tracking
- Performance tuning (only correctness)
- Any work on other testbed plans (`plans/`)

## Safety Checklist (enforced every iteration)

- [ ] Branch is `claude/nifty-hermann-d3480c`, not `main`
- [ ] Worktree is `.claude/worktrees/nifty-hermann-d3480c`
- [ ] No `--force`, no `reset --hard`, no `rm -rf`
- [ ] Each fix has a negative test (halal)
- [ ] Commits follow lowercase human-style format (no AI attribution)
