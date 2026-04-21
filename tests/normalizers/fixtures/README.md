# Normalizer Test Fixtures

Fixtures for `tests/normalizers/` — one `.jsonl` per provider.

**Capture date:** 2026-04-21

---

## Directory layout

```
fixtures/
  *.jsonl            — synthetic fixtures (hand-crafted, stable shapes)
  captures/          — real CLI output captured from actual runs
  README.md          — this file
```

---

## Synthetic fixtures (`*.jsonl`)

| File | Provider | Event types covered |
|------|----------|---------------------|
| `claude.jsonl` | Claude CLI `--output-format stream-json` | `system`, `assistant` (text, thinking, tool_use), `user` (tool_result), `result` |
| `claude_rate_limit.jsonl` | Claude CLI | `error` (rate-limit / API error) |
| `codex.jsonl` | Codex CLI `--json` | `text`, `tool`, `tool_output`, `done`, `error` |
| `codex_tool_error.jsonl` | Codex CLI | `text`, `tool`, `tool_output`, `done` (tool exit_code=1 variant) |
| `kilo.jsonl` | Kilo CLI `--format json` | `text`, `status`, `tool_use`, `tool_result`, `kilo_internal_checkpoint`, `error`, `turn_end` |
| `opencode.jsonl` | OpenCode CLI `--format json` | `message`, `tool_call`, `tool_result`, `end`, `error` |
| `opencode_unknown_model.jsonl` | OpenCode CLI | `error`, `end` (unknown model variant) |
| `zai.jsonl` | Zai (Claude Agent SDK via api.z.ai) | `text`, `thinking`, `tool_use`, `tool_result`, `status`, `error`, `turn_end` |

---

## Real captures (`captures/*.jsonl`)

| File | CLI version | Scenario |
|------|-------------|----------|
| `captures/claude.jsonl` | claude 2.1.114 | `read README.md and summarize` → system+thinking+tool_use+tool_result+text+result |
| `captures/claude_rate_limit.jsonl` | claude 2.1.114 | invalid API key → 401 auth_error via assistant+result(is_error) |
| `captures/codex.jsonl` | codex-cli 0.115.0 | `read README.md and summarize` → thread.started+item events |
| `captures/codex_tool_error.jsonl` | codex-cli 0.115.0 | read nonexistent file → item.completed(exit_code=1) |
| `captures/kilo.jsonl` | kilo (nvm) | `read README.md and summarize` → step_start+tool_use+step_finish |
| `captures/opencode.jsonl` | opencode 1.4.0 | `read README.md and summarize` → step_start+tool_use+text+step_finish |
| `captures/opencode_unknown_model.jsonl` | opencode 1.4.0 | unknown model configured → `{"type":"error",...}` |

---

## Collective event type coverage

The fixtures collectively cover all normalizer-required event shapes:

| Type | Present in |
|------|-----------|
| `text` | `kilo.jsonl`, `zai.jsonl`, `codex.jsonl`, `codex_tool_error.jsonl` |
| `tool_use` | `kilo.jsonl`, `zai.jsonl` |
| `tool_result` | `kilo.jsonl`, `opencode.jsonl`, `zai.jsonl` |
| `thinking` | `zai.jsonl` |
| `turn_end` | `kilo.jsonl`, `zai.jsonl` |
| `error` | `claude_rate_limit.jsonl`, `codex.jsonl`, `kilo.jsonl`, `opencode.jsonl`, `opencode_unknown_model.jsonl`, `zai.jsonl` |

---

## Format notes

- Each `.jsonl` starts with `//` comment lines describing provenance — skip these when parsing.
- All data lines are valid JSON objects (verified 2026-04-21).
- No secrets: session IDs and UUIDs are real but not sensitive; the one API key in a capture header is `invalid-key-for-testing` (synthetic).
