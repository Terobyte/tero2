# Провайдеры — конкретная реализация

> Данные из реального кода tero v1 (`src/providers/`). Только то что проверено в бою.
> Маппинг ролей на провайдеров — **конфигурируемый** через `.sora/config.toml`.

---

## Обзор

| Провайдер | Команда | Бесплатно? | Система промпт | Роль в tero2 (default) |
|-----------|---------|-----------|----------------|------------------------|
| **Claude Code** | `claude` | Нет (Pro ~$20/мес) | `--system-prompt <text>` | Architect, Concierge, Designer |
| **Codex** | `codex exec --json` | Да | `CODEX_INSTRUCTIONS` env | Coach, Builder fallback, Debugger |
| **OpenCode** | `opencode run --format json` | Да | `<SYSTEM INSTRUCTIONS>` тег | Builder (основной, z.ai/glm-5.1) |
| **Kilo** | `kilo run --format json` | Да | `<SYSTEM INSTRUCTIONS>` тег | Scout, Verifier, Reviewer |

---

## Claude Code

**Тип:** Claude CLI (Pro/Max подписка)

```python
cmd = [
    "claude",
    "-p",
    "--verbose",
    "--model", "sonnet",          # sonnet | opus | haiku
    "--max-turns", "30",
    "--permission-mode", "bypassPermissions",
    "--output-format", "stream-json",
    "--settings", '{"autoCompactThreshold": 0.99}',  # важно! без этого ранний compact
    "--system-prompt", system_prompt,
    # промпт — через stdin
]
```

**Важно:** блокировать эти env vars перед запуском (иначе конфликт с Pro auth):
```python
_BLOCKED_ENV_VARS = [
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL", "ZAI_API_KEY",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
]
```
`CLAUDE_CONFIG_DIR` тоже убирать — ломает auth resolution на macOS.

**Проверка готовности:**
```bash
claude auth status  # должен вернуть 0
```

**Модели:**
- `sonnet` — claude-sonnet-4-x (баланс)
- `opus` — claude-opus-4-x (тяжёлая архитектура)
- `haiku` — claude-haiku-4-x (быстро/дёшево)

**Контекстное окно:** 1M токенов (промпт + код + история)

**Роль в tero2:** Architect (opus/sonnet, configurable), Concierge (sonnet), Designer (sonnet). Платный — только для задач требующих качества.

---

## Codex

**Тип:** OpenAI Codex CLI

```python
cmd = [
    "codex", "exec",
    "--json",                     # JSONL output в stdout
    "-m", model,                  # пусто = из ~/.codex/config.toml
    "-C", working_dir,
    "--dangerously-bypass-approvals-and-sandbox",  # для auto-режима
    "--ephemeral",                # нет persistent сессий — каждый раз чисто
    "-",                          # промпт из stdin
]
```

**Системный промпт** — через env var:
```python
env["CODEX_INSTRUCTIONS"] = system_prompt

# Если промпт > 64KB → записать во temp файл и передать путь:
env["CODEX_INSTRUCTIONS"] = (
    "IMPORTANT: Your complete system instructions are in the file "
    f"{tmp_path} — you MUST read that file in full before doing anything else."
)
```

**Sandbox режимы:** `read-only | workspace-write | danger-full-access`

**Проверка готовности:**
```bash
codex --version
# установка: npm i -g @openai/codex
```

**Роль в tero2:** Coach (основной — лучший в coaching), Builder fallback, Debugger. Бесплатный.

---

## OpenCode

**Тип:** OpenCode CLI (MIMO, Kimi, MiniMax, Z.AI и другие бесплатные модели)

```python
cmd = [
    "opencode", "run",
    "--format", "json",
    "--dir", working_dir,
    "-m", "z.ai/glm-5.1",           # дефолтная модель для Builder
    "-",                              # промпт из stdin
]
```

**Системный промпт** — НЕ env var, а тег внутри промпта:
```python
full_prompt = (
    f"<SYSTEM INSTRUCTIONS>\n{system_prompt}\n</SYSTEM INSTRUCTIONS>"
    f"\n\n{user_prompt}"
)
# Всё это идёт в stdin
```

**Доступные модели (бесплатные):**
```
z.ai/glm-5.1                   # Z.AI GLM — основная для Builder
opencode/mimo-v2-pro-free       # MIMO
opencode/kimi-k2-free           # Moonshot Kimi K2
opencode/minimax-text-01-free   # MiniMax 2.5
```

**Проверка готовности:**
```bash
which opencode
```

**Роль в tero2:** Builder (основной, через z.ai/glm-5.1). Scout fallback (MiniMax 2.5).

---

## Kilo

**Тип:** Тот же OpenCodeProvider, команда `kilo`

```python
cmd = [
    "kilo", "run",
    "--format", "json",
    "--dir", working_dir,
    "-m", "kilo/xiaomi/mimo-v2-pro:free",
    "-",
]
```

**Системный промпт** — тот же `<SYSTEM INSTRUCTIONS>` тег (идентично OpenCode).

**Проверка готовности:**
```bash
which kilo
```

**Роль в tero2:** Scout (быстро и бесплатно), Verifier, Reviewer.

---

## Маппинг ролей — config.toml

Все роли настраиваются через `.sora/config.toml`. Маппинг НЕ хардкодится в коде.

```toml
# .sora/config.toml — role→provider mapping

[roles.scout]
provider = "kilo"
model = "kilo/xiaomi/mimo-v2-pro:free"
fallback = ["opencode"]

[roles.architect]
provider = "claude"
model = "opus"                          # opus или sonnet — зависит от задачи
fallback = []                           # только платный, нет fallback

[roles.builder]
provider = "opencode"
model = "z.ai/glm-5.1"                 # основной — OpenCode/Z.AI
fallback = ["codex", "kilo"]            # Codex = fallback, не основной

[roles.verifier]
provider = "kilo"
model = "kilo/xiaomi/mimo-v2-pro:free"
fallback = ["opencode"]

[roles.coach]
provider = "codex"
model = ""                              # из ~/.codex/config.toml
fallback = []                           # coach = codex, нет fallback

[roles.concierge]
provider = "claude"
model = "sonnet"
fallback = []

[roles.debugger]
provider = "codex"
model = ""
fallback = ["opencode", "kilo"]

[roles.reviewer]
provider = "kilo"
model = "kilo/xiaomi/mimo-v2-pro:free"
fallback = ["opencode"]

[roles.designer]
provider = "claude"
model = "sonnet"
fallback = []
```

**Логика выбора (defaults):**
- Стратегия и coaching → Codex (лучший в coaching, бесплатный)
- Архитектура и дизайн → Claude Code (opus/sonnet, Pro подписка)
- Кодирование → OpenCode/Z.AI (бесплатно, основной builder)
- Дешёвые/быстрые задачи → Kilo (scout, verifier, reviewer)

---

## ProviderChain + CircuitBreaker

### ProviderChain — паттерн из tero v1

```python
class ProviderChain:
    """Автоматический fallback при ошибках провайдера."""

    # Recoverable errors (→ fallback):
    RECOVERABLE_KEYWORDS = [
        "429", "rate", "limit", "too many requests",
        "timeout", "timed out",
        "connection reset", "connection aborted", "connection refused",
        "network", "temporar", "unavailable", "overloaded",
        "gateway", "unexpected eof", "broken pipe",
    ]

    def __init__(self, providers: list, retry_wait_s=60, max_retries=3):
        ...
    # Буферизует вывод до успеха — нет частичного вывода из упавшего провайдера
```

**Ключевой паттерн:** output буферизуется до завершения провайдера. Если провайдер упал — буфер сбрасывается и пробуется следующий. Caller не видит мусор от упавшего.

### CircuitBreaker — новое в tero2

```python
class CircuitBreaker:
    """Предотвращает повторные вызовы к провайдеру который стабильно падает."""

    states: CLOSED → OPEN → HALF_OPEN

    # CLOSED: нормальная работа
    # OPEN: провайдер заблокирован (N ошибок подряд) — сразу fallback
    # HALF_OPEN: пробуем 1 запрос — если OK → CLOSED, если FAIL → OPEN

    failure_threshold = 3       # ошибок до OPEN
    recovery_timeout_s = 300    # через сколько попробовать HALF_OPEN
```

CircuitBreaker работает поверх ProviderChain: если провайдер в OPEN state, ProviderChain пропускает его и сразу идёт к fallback. Снижает latency при длительных outages.

> Подробнее: `fault-tolerance.md`, `detailed-roadmap.md`

---

## Как передаётся системный промпт — итог

| Провайдер | Механизм | Контекстное окно |
|-----------|----------|-----------------|
| Claude Code | `--system-prompt <text>` CLI аргумент | **1M токенов** |
| Codex | `CODEX_INSTRUCTIONS` env var → temp file если > 64KB | **1M токенов** |
| OpenCode | `<SYSTEM INSTRUCTIONS>` тег в начале stdin | зависит от модели |
| Kilo | `<SYSTEM INSTRUCTIONS>` тег в начале stdin | зависит от модели |

**OpenCode/Kilo — окно по модели:**
- `mimo-v2-pro-free` — ~32K
- `kimi-k2-free` — 128K
- `z.ai/glm-5.1` — 128K
- Проверяй актуальные лимиты в docs провайдера — они меняются

---

## Ссылки на код tero v1

```
src/providers/
├── chain.py          — ProviderChain (fallback + retry логика)
├── claude_native.py  — ClaudeNativeProvider
├── codex.py          — CodexProvider (1000+ строк, полная реализация)
├── opencode.py       — OpenCodeProvider (Kilo тоже через него)
├── registry.py       — ProviderRegistry (get/create по имени)
├── base.py           — AgentProvider interface
└── subprocess_runner.py — async JSONL subprocess runner
```

> Полная архитектура и план реализации: `detailed-roadmap.md`
