# tero2 — Детализированный Roadmap

> Инструмент для архитектора: ты думаешь и проектируешь, tero2 — разведывает, строит, проверяет, чинит, отчитывается.
> Каждый MVP — самостоятельный рабочий продукт И шаг к суперпрограмме.
> Стек: Python 3.11+. Платформа: macOS. Проекты: `/Users/terobyte/Desktop/Projects/Active/`

---

## Часть 1: Фундамент (Core)

Модули которые появляются в MVP0 и живут во всех последующих MVP почти без изменений.

---

### 1.1 Providers (из tero v1)

**Источник:** `/Users/terobyte/Desktop/Projects/Active/tero/src/providers/` (~2044 строки)

**Берём as-is:**

| Модуль | Строк | Что делает |
|--------|-------|-----------|
| `base.py` | 67 | `AgentProvider` Protocol + `AgentResult` dataclass |
| `subprocess_runner.py` | 136 | Async JSONL subprocess runner, chunk-based stdout, concurrent stderr drain |
| `message_adapter.py` | 308 | Унифицированный `AdaptedMessage` формат, нормализация между CLI и SDK |
| `chain.py` | 136 | `ProviderChain` — автофолбек при rate limit / transient errors, буферизация output |
| `claude_native.py` | 150 | Claude Code CLI (Pro/Max подписка), `--system-prompt`, `--output-format stream-json` |
| `codex.py` | 524 | Codex CLI (`codex exec --json`), sandbox режимы, large prompt через temp file |
| `opencode.py` | 245 | OpenCode CLI (MIMO, Kimi, MiniMax, Z.AI), `<SYSTEM INSTRUCTIONS>` тег |
| `zai.py` | 182 | Z.AI через claude-agent-sdk, изолированный CLAUDE_CONFIG_DIR |
| `registry.py` | 165 | `ProviderRegistry` — фабрика, lazy instantiation с кешем |
| `__init__.py` | 131 | Public API + `create_provider()` |

**Что добавляется в tero2 поверх v1:**

- **CircuitBreaker** — три состояния (CLOSED → OPEN → HALF-OPEN), fast-fail для мёртвых провайдеров (сейчас отсутствует в chain.py)
- **RoleProviderConfig** — конфигурируемый маппинг роль→провайдер (YAML/TOML), не хардкод

```python
# Пример конфига (config.toml или .sora/config.toml)
[roles.scout]
provider = "kilo"
model = "kilo/xiaomi/mimo-v2-pro:free"
fallback = ["opencode"]

[roles.architect]
provider = "claude"
model = "opus"
fallback = []

[roles.builder]
provider = "opencode"
model = "z.ai/glm-5.1"
fallback = ["codex", "kilo"]

[roles.coach]
provider = "codex"
model = ""  # из ~/.codex/config.toml
fallback = []

[roles.verifier]
provider = "kilo"
model = "kilo/xiaomi/mimo-v2-pro:free"
fallback = ["opencode"]
```

**Зависимости:**
- Нет внешних зависимостей кроме `asyncio`, `json`, `dataclasses`
- `src.constants` и `src.errors` нужно перенести/создать

---

### 1.2 Disk Layer

**Принцип:** Диск — единственная шина коммуникации. Crash в любой момент не теряет прогресс. Каждый агент читает с диска, пишет на диск, умирает.

**Структура `.sora/`:**

```
.sora/
├── config.toml             # роль→провайдер маппинг, параметры
│
├── runtime/                # ephemeral (удаляется при restart milestone)
│   ├── STATE.md            # текущее состояние: фаза, задача, retry_count
│   ├── auto.lock           # PID + timestamp, OS-level lock
│   └── completed-units.json
│
├── strategic/              # Coach пишет, Dispatcher читает
│   ├── STRATEGY.md
│   ├── TASK_QUEUE.md
│   ├── RISK.md
│   └── CONTEXT_HINTS.md
│
├── persistent/             # живёт всегда
│   ├── PROJECT.md          # описание проекта, стек
│   ├── DECISIONS.md        # архитектурные решения (append-only)
│   ├── KNOWLEDGE.md        # межсессионные правила
│   └── EVENT_JOURNAL.md    # аномалии от Verifier
│
├── milestones/
│   └── M001/
│       ├── ROADMAP.md
│       ├── CONTEXT.md
│       ├── S01/
│       │   ├── PLAN.md
│       │   ├── T01-PLAN.md
│       │   ├── T01-SUMMARY.md
│       │   └── UAT.md
│       └── CONTEXT_MAP.md
│
├── human/                  # human steering
│   ├── STEER.md            # мягкое — подхватится на границе фазы
│   ├── OVERRIDE.md         # экстренное — проверяется после каждой Task
│   └── HUMAN_CONTEXT.md    # дополнительный контекст от человека
│
├── prompts/                # системные промпты ролей (.md файлы)
│   ├── scout.md
│   ├── architect.md
│   ├── builder.md
│   ├── verifier.md
│   ├── coach.md
│   ├── concierge.md
│   ├── debugger.md
│   ├── reviewer.md
│   └── designer.md
│
└── reports/                # observability
    ├── metrics.json        # cost/tokens per role
    ├── activity.jsonl      # structured event log
    └── M001-report.html    # итоговый отчёт по milestone
```

**Ключевые модули:**

| Модуль | Что делает |
|--------|-----------|
| `disk_layer.py` | CRUD для `.sora/` файлов: read_state, write_state, append_journal, read_strategy |
| `state.py` | `AgentState` dataclass: phase, current_task, retry_count, steps, last_tool_hash |
| `lock.py` | `auto.lock` — OS-level file lock (fcntl.flock на macOS), PID + timestamp |
| `persona.py` | `PersonaRegistry` — загрузка промптов из `.md` файлов (из v1, адаптировать) |

**Open Questions:**
- Формат STATE.md: Markdown для human-readability или JSON для machine parsing? → **Решение: JSON с pretty-print. Причина: STATE.md читается программой (Dispatcher), не человеком. Человек читает reports/.**
- Lock strategy: fcntl.flock (advisory) или lockfile с PID check? → **Решение: fcntl.flock + PID в файле. flock — OS-level, PID — для обнаружения stale locks после crash.**

---

### 1.3 Notifier (Telegram Out)

**Что делает:** Отправляет уведомления в Telegram — текст и голос (TTS).

**Модули:**

| Модуль | Что делает |
|--------|-----------|
| `notifier.py` | Unified API: `notify(text, voice=False)` → Telegram text/voice |
| Зависимость: `library/tts_fish_audio.py` | Fish Audio TTS (JLM4.7 voice) — уже существует |

**Типы уведомлений:**

```python
class NotifyLevel(Enum):
    HEARTBEAT = "heartbeat"   # "работаю, Task 3/7"
    PROGRESS = "progress"     # "Slice 2 готов, начинаю Slice 3"
    STUCK = "stuck"           # "застрял на Task 3, жду тебя"
    DONE = "done"             # "готово, 47/47 тестов green"
    ERROR = "error"           # "провайдер claude недоступен, fallback на codex"
```

**Heartbeat:**
- Периодичность: каждые N минут (конфигурируемо, default 15 мин)
- Содержание: текущая фаза, задача, % прогресса
- Без TTS (экономия), только текст

**Config:**
```toml
[telegram]
bot_token = "..."
chat_id = "614473938"
heartbeat_interval_min = 15
voice_on_done = true       # голосовое при завершении
voice_on_stuck = true      # голосовое при тупике
```

---

### 1.4 Runner (Process Lifecycle)

**Что делает:** Основной цикл — запуск агентов, watch за ними, restart при crash.

**Модули:**

| Модуль | Что делает |
|--------|-----------|
| `runner.py` | Главный цикл: spawn agent → watch → checkpoint → restart |
| `checkpoint.py` | Save/restore state на диск: текущая фаза, задача, retry count |

**Жизненный цикл:**

```
runner.py запускается
  → читает STATE.md (или создаёт новый)
  → определяет текущую фазу (Plan / Execute / Complete / Reassess)
  → спавнит нужного Player через ProviderRegistry
  → наблюдает за ним (async stream)
  → Player завершился:
      OK → checkpoint → следующая фаза/задача
      CRASH → checkpoint → retry (max 3)
      TIMEOUT → checkpoint → escalate
  → повтор
```

**Checkpoint:** после каждой успешной Task. При crash — restart с последнего checkpoint, не с начала.

**Daemon:**
- macOS LaunchAgent (`com.tero.agent.plist`)
- `KeepAlive: true` — auto-restart после crash/sleep
- Логи: `/tmp/tero-agent.log`, `/tmp/tero-agent.err`
- Альтернатива для разработки: `screen` / `tmux`

---

### 1.5 Config (Runtime Configuration)

**Что делает:** Единая точка конфигурации системы.

**Модуль:** `config.py`

**Формат:** TOML (`.sora/config.toml` per project + `~/.tero2/config.toml` global)

**Структура:**

```toml
# Глобальные настройки (default, переопределяются per project)

[general]
projects_dir = "~/Desktop/Projects/Active"
log_level = "INFO"

[roles]
# роль→провайдер маппинг (см. 1.1 Providers)

[retry]
max_retries = 3
backoff_base = 2              # 2^attempt + jitter
max_steps_per_task = 15

[context]
target_ratio = 0.70           # целевое заполнение контекста
warning_ratio = 0.80
hard_fail_ratio = 0.95

[telegram]
# см. 1.3 Notifier

[plan_hardening]
max_rounds = 5
stop_on_cosmetic_only = true
```

**Приоритет:** project `.sora/config.toml` > global `~/.tero2/config.toml` > defaults

---

## Часть 2: MVP Дельты

---

### MVP0 — "Бессмертный раннер"

**Проблема:** tero v1 крашится, слепота, rate limit убивает.

**Что ты получаешь:** запустил `tero2 run ~/project` → уехал → получил в Telegram "готово" или "застрял, жду".

**Модули (все новые, кроме providers):**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | Constants + Errors | `constants.py`, `errors.py` | — | Константы и типизированные исключения |
| 2 | Config | `config.py` | — | Чтение TOML конфига, defaults, merge |
| 3 | Disk Layer | `disk_layer.py`, `state.py`, `lock.py` | constants | CRUD для `.sora/`, STATE, locks |
| 4 | Providers (из v1) | `providers/` | constants, errors | Перенос + адаптация импортов |
| 5 | Circuit Breaker | `circuit_breaker.py` | — | CLOSED/OPEN/HALF-OPEN для провайдеров |
| 6 | Notifier | `notifier.py` | config | Telegram text/voice уведомления |
| 7 | Checkpoint | `checkpoint.py` | disk_layer, state | Save/restore state при crash |
| 8 | Runner | `runner.py` | все выше | Основной цикл: spawn → watch → restart |
| 9 | CLI Entry | `cli.py` | runner, config | `tero2 run <path>` + `tero2 status` |
| 10 | Daemon config | `daemon/com.tero.agent.plist` | — | LaunchAgent для macOS |

**Граф зависимостей модулей:**

```
cli.py
  └── runner.py
        ├── checkpoint.py
        │     ├── disk_layer.py
        │     │     ├── state.py
        │     │     └── lock.py
        │     └── state.py
        ├── notifier.py
        │     └── config.py
        ├── providers/
        │     ├── chain.py
        │     │     └── circuit_breaker.py
        │     ├── registry.py
        │     └── (claude, codex, opencode, kilo, zai)
        └── config.py
              └── constants.py
```

**Порядок реализации (критический путь):**

```
1. constants.py + errors.py        — нулевые зависимости
2. config.py                       — нулевые зависимости
3. state.py + lock.py              — constants
4. disk_layer.py                   — state, lock
5. providers/ (перенос из v1)      — constants, errors
6. circuit_breaker.py              — нулевые зависимости
7. интеграция CB в chain.py        — circuit_breaker
8. checkpoint.py                   — disk_layer, state
9. notifier.py                     — config
10. runner.py                      — checkpoint, notifier, providers, config
11. cli.py                         — runner, config
12. daemon plist                   — cli
```

Шаги 1-4 и 5-6 и 9 могут идти параллельно.

**Open Questions:**
- CLI framework: `argparse` (stdlib) или `click`/`typer`? → **Решение: argparse. Минимум зависимостей для MVP0. Переехать на typer позже если нужно.**
- Как runner узнаёт КАКОГО агента спавнить? В MVP0 нет ролей (Scout, Architect, Builder) — это MVP2. → **Решение: MVP0 runner берёт markdown-план из файла и отдаёт единственному агенту (Builder) через ProviderChain. Роль одна — "executor". Роли появляются в MVP1-2.**

**Критерий "готово":**
- [ ] `tero2 run ~/project --plan plan.md` запускает агента
- [ ] Agent crash → auto-restart с checkpoint
- [ ] Rate limit 429 → retry с backoff + jitter
- [ ] Provider dead → fallback на следующий (ProviderChain + CircuitBreaker)
- [ ] Telegram: "начал", heartbeat каждые 15 мин, "готово"/"ошибка"
- [ ] `tero2 status` показывает текущее состояние
- [ ] LaunchAgent переживает sleep Mac'а

---

### MVP1 — "Полируй и строй"

**Проблема:** 30-70% completion, баги из-за плохого плана.

**Что ты получаешь:** даёшь план (файл или Telegram) → tero2 полирует план (Plan Hardening) → строит с проверкой (Builder + Verifier).

**Дельта поверх MVP0:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | Persona Registry | `persona.py` | disk_layer | Загрузка промптов ролей из `.md` |
| 2 | Context Assembly | `context_assembly.py` | disk_layer, config | Сборка промпта с бюджетным контролем |
| 3 | Plan Hardening | `plan_hardening.py` | providers, persona, context_assembly | N проходов ревью плана свежими контекстами |
| 4 | Builder Player | `players/builder.py` | providers, persona, context_assembly | Пишет код по плану задачи |
| 5 | Verifier Player | `players/verifier.py` | providers, persona | Тесты, линтер, must-haves check |
| 6 | Reflexion | `reflexion.py` | disk_layer | Inject причины FAIL в контекст следующей попытки |
| 7 | Dispatcher (v1) | обновление `runner.py` | все выше | Plan → Hardening → Execute(Builder→Verifier) → Complete |
| 8 | Telegram Input | `telegram_input.py` | config, disk_layer | Приём markdown-планов, создание проектов |
| 9 | Project Init | `project_init.py` | disk_layer | Создание `Projects/Active/{name}/` + git init + `.sora/` |

**Новый цикл Dispatcher (поверх MVP0 runner):**

```
Plan получен (файл или Telegram)
  → Project Init (если новый проект)
  → .sora/ инициализация
  → Plan Hardening (3-5 проходов, Reviewer роль)
  → Architect: декомпозиция плана на Tasks (в MVP1 может быть ручная — план уже содержит задачи)
  → Для каждой Task:
      Builder (OpenCode/Z.AI) → пишет код + T0X-SUMMARY.md
      Verifier (Kilo) → тесты, линтер, must-haves
        PASS → следующая Task
        FAIL → Reflexion → Builder retry (max 2)
        FAIL × 3 → ANOMALY → notify Telegram
  → Все Tasks пройдены → DONE → notify Telegram
```

**Telegram Input (polling бот):**

```python
# Минимальный бот: принимает markdown, создаёт проект
async def handle_message(message):
    if message.chat.id != ALLOWED_CHAT_ID:
        return  # игнорировать чужих

    if message.document:  # файл .md
        plan = await download_file(message.document)
    elif message.text:
        plan = message.text

    project_name = extract_project_name(plan)
    project_path = create_project(project_name, plan)
    await notify(f"проект {project_name} создан, начинаю hardening")
    # Dispatcher подхватывает
```

**Библиотека:** `python-telegram-bot` (async, поддержка Bot API)

**Open Questions:**
- Plan Hardening: Вариант A (последовательный) или C (convergence loop)? → **Решение: C (convergence loop). Автоматическая остановка когда проблемы кончились. Max 5 раундов.**
- Кто делает декомпозицию? В MVP1 это может быть часть плана (пользователь пишет Tasks). Architect появляется в MVP2. → **Решение: MVP1 ожидает что план уже содержит задачи. Если нет — одна большая Task. Architect как автодекомпозитор — MVP2.**
- Verifier: запускать реальные тесты (pytest) или только линтер (ruff)? → **Решение: оба. ruff check + pytest -x. Если pytest не настроен — только ruff.**

**Критерий "готово":**
- [ ] Принял markdown-план из Telegram → создал проект
- [ ] Plan Hardening: 3-5 проходов → количество найденных проблем падает до 0
- [ ] Builder построил код по плану
- [ ] Verifier прогнал тесты/линтер → PASS или FAIL с описанием
- [ ] При FAIL → Reflexion → Builder retry с контекстом ошибки
- [ ] Telegram: "план закалён, начинаю строить" → "готово, N/N тестов green"
- [ ] `.sora/` структура создана и заполнена: PLAN, SUMMARY, STATE

---

### MVP2 — "Стратег"

**Проблема:** ты сам декомпозируешь задачи, агент не думает.

**Дельта поверх MVP1:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | Scout Player | `players/scout.py` | providers, persona | Быстрая разведка кодовой базы → CONTEXT_MAP.md |
| 2 | Architect Player | `players/architect.py` | providers, persona, context_assembly | Декомпозиция Slice на Tasks → S0X-PLAN.md |
| 3 | Coach Runner | `coach_runner.py` | providers, persona, disk_layer | Запуск Coach по триггерам → STRATEGY.md |
| 4 | Trigger Detection | `triggers.py` | state, disk_layer | Условия для вызова Coach: end-of-slice, anomaly, budget, stuck |
| 5 | Context Assembly v2 | обновление `context_assembly.py` | disk_layer | + CONTEXT_MAP, + CONTEXT_HINTS, + STRATEGY injection |
| 6 | Stuck Detection (basic) | `stuck_detection.py` | state | Структурный: retry_count, steps, tool_call hash |
| 7 | Escalation | `escalation.py` | notifier, disk_layer | 3 уровня: diversification → backtrack+coach → human |
| 8 | Dispatcher v2 | обновление `runner.py` | все выше | Полный SORA цикл: Scout → Architect → Builder → Verifier |

**Новый цикл Dispatcher (SORA):**

```
1. Dispatcher читает STATE.md
   └── нет активного Slice → запустить Scout

2. Scout (Kilo, бесплатно)
   └── пишет CONTEXT_MAP.md → умирает

3. Dispatcher проверяет STRATEGY.md
   └── есть → передать Architect
   └── нет → вызвать Coach первый раз

4. Coach (Codex) при триггере
   └── читает ROADMAP + CONTEXT_MAP + summaries
   └── пишет STRATEGY.md + TASK_QUEUE.md → умирает

5. Architect (Claude Opus/Sonnet)
   └── читает STRATEGY + CONTEXT_MAP
   └── пишет S0X-PLAN.md с N Tasks → умирает

6. Для каждой Task:
   a. Builder (OpenCode/Z.AI)
   b. Verifier (Kilo)
   c. FAIL → Reflexion → retry
   d. ANOMALY → EVENT_JOURNAL → триггер Coach

7. Конец Slice → Coach
   └── пишет обновлённый STRATEGY.md
   └── Dispatcher использует для следующего Slice

8. Повтор по Slices → Milestone validation
```

**Триггеры Coach'а:**

```python
class CoachTrigger(Enum):
    END_OF_SLICE = "end_of_slice"       # штатный — конец каждого Slice
    ANOMALY = "anomaly"                  # Verifier написал ANOMALY в EVENT_JOURNAL
    BUDGET_60 = "budget_60"              # потрачено 60%+ бюджета
    STUCK = "stuck"                      # stuck_detection сработал
    HUMAN_STEER = "human_steer"          # STEER.md появился/изменился
```

**Open Questions:**
- Architect модель когда Claude уйдёт с Max? → Не вопрос — Pro остаётся. Opus на Pro или Sonnet. Конфигурируемо.
- Scout — нужен ли вообще для маленьких проектов? → **Решение: конфигурируемо. `skip_scout_if_files_lt = 20`. Маленький проект — Builder сам справится без карты.**
- Milestone vs Slice vs Task иерархия — всегда 3 уровня или иногда достаточно 2? → **Решение: Milestone опционален. Простая задача = 1 Slice с N Tasks. Milestone для крупных проектов (10+ Slices).**

**Критерий "готово":**
- [ ] "сделай CLI-тул для X" → Scout разведал → Architect разбил на 5 Tasks
- [ ] Coach проснулся после Slice 1 → переставил приоритеты
- [ ] Builder + Verifier выполнили все Tasks
- [ ] При stuck → escalation → Telegram с описанием тупика
- [ ] Telegram: итоговый отчёт с метриками

---

### MVP3 — "Франкенштейн"

**Проблема:** баги остаются, агент не умеет дебажить глубоко.

**Дельта поверх MVP2:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | Debugger Player | `players/debugger.py` | providers, persona | Frankenstein: reproduce → trace → fix → verify |
| 2 | Антипамять | `scratchpad.py` | disk_layer | Blacklist неудачных решений per debug session |
| 3 | Мультипатч | `multipatch.py` | providers | 3 патча-кандидата → тесты выбирают лучший |
| 4 | Effort Classifier | `effort_classifier.py` | — | EASY/MEDIUM/HARD → определяет глубину debug loop |
| 5 | Stuck Detection v2 | обновление `stuck_detection.py` | — | + семантическая петля (cosine similarity) |
| 6 | Loop Detection (semantic) | `semantic_loop.py` | — | Embedding + cosine similarity > 0.90 |

**Цикл Debugger'а:**

```
Баг получен (из Verifier ANOMALY, или через Concierge "найди баги")
  → Effort Classifier: EASY / MEDIUM / HARD

  EASY (single-shot, max 5 шагов):
    → reproduce → CoT (NL-first) → 1 патч → verify

  MEDIUM (мультипатч, max 5 итераций):
    → reproduce → RED test
    → CoT → 3 патча-кандидата → тесты как арбитр
    → антипамять фильтрует пробованное
    → GREEN = done

  HARD (self-evolving, max 20 итераций):
    → всё из MEDIUM + scratchpad + loop detection
    → если петля → diversification → backtrack
```

**Semantic Loop Detection:**

```python
# Rolling buffer последних 3 "мыслей" Builder'а
# Embedding через lightweight модель (local) или text-embedding-3-small
# cosine_similarity > 0.90 → петля обнаружена

def check_semantic_loop(current_thought: str, history: list[np.ndarray]) -> bool:
    current_vec = embed(current_thought)
    for prev_vec in history[-3:]:
        if cosine_similarity(current_vec, prev_vec) > 0.90:
            return True
    return False
```

**Open Questions:**
- Embedding для semantic loop: локальная модель (sentence-transformers) или API? → **Решение: локальная (sentence-transformers, all-MiniLM-L6-v2). ~80MB, быстро, бесплатно. Fallback: text-embedding-3-small если нужна точность.**
- Effort Classifier: ML или эвристика? → **Решение: эвристика (кол-во файлов, размер diff, тип ошибки). ML оверкилл для MVP3.**

**Критерий "готово":**
- [ ] "найди баги в auth модуле" → RED test → 3 патча → выбрал лучший → GREEN
- [ ] Антипамять: 2-я попытка знает что 1-я не сработала
- [ ] Effort level автоматически определяется
- [ ] Semantic loop detection ловит повторяющиеся мысли

---

### MVP4 — "Голос"

**Проблема:** печатать markdown с телефона неудобно.

**Дельта поверх MVP3:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | STT | `stt.py` | — | Whisper/Deepgram: .ogg → текст |
| 2 | Concierge Player | `players/concierge.py` | providers, persona, telegram | Собеседник в Telegram: понимает, уточняет, делегирует |
| 3 | Telegram Bot v2 | обновление `telegram_input.py` | stt, notifier | + голосовые, + фото, + файлы |
| 4 | Vision | `vision.py` | — | Фото/скрин → Claude Vision → текст-описание |
| 5 | Specialist Router | `specialist_router.py` | providers, persona | Concierge → нужный специалист (Architect/Builder/Debugger/...) |

**Поток:**

```
Голосовое в Telegram
  → STT (Deepgram Nova-3) → текст
  → Concierge (Claude Sonnet): понимает, уточняет
  → "понял, задача для Builder"
  → Specialist Router → Builder
  → Builder работает
  → Результат → Concierge → ответ (текст или TTS)
```

**STT выбор:**
- Deepgram Nova-3: лучший для русского, ~$0.0043/мин
- Whisper (local): бесплатно, но медленнее

**Open Questions:**
- Concierge — отдельный long-running процесс или polling? → **Решение: polling бот (python-telegram-bot), long-running процесс. LaunchAgent daemon.**
- Concierge должен помнить контекст разговора? → **Решение: да, in-memory history для текущего разговора. Сбрасывается при перезапуске. Долгосрочная память — KNOWLEDGE.md.**
- Как Concierge передаёт контекст специалисту? → **Решение: пишет сформулированную задачу в `.sora/human/TASK_REQUEST.md`, Dispatcher подхватывает.**

**Критерий "готово":**
- [ ] Голосовое "сделай лендинг с тёмной темой" → STT → текст
- [ ] Concierge уточнил 2 вопроса (голос→голос или текст→текст)
- [ ] Concierge передал задачу Builder'у
- [ ] Builder построил → Telegram: "готово" (текст + TTS)
- [ ] Фото/скрин → Vision → контекст для задачи

---

### MVP5 — "Параллелизм"

**Проблема:** одна задача за раз = медленно.

**Дельта поверх MVP4:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | Worktree Manager | `worktree.py` | — | Git worktree: создание, удаление, merge |
| 2 | Eligibility Check | `eligibility.py` | disk_layer | Проверка: Tasks не пересекаются по файлам |
| 3 | Conflict Detection | `conflict_detection.py` | worktree | Real-time: git diff между worktrees |
| 4 | Parallel Dispatcher | обновление `runner.py` | worktree, eligibility | 2-3 Builder'а одновременно |
| 5 | Merge Strategy | `merge.py` | worktree | Per-slice squash, auto/confirm/manual |
| 6 | Per-role Cost Tracking | обновление `metrics` | — | Cost breakdown по ролям |

**Принцип:**
- Каждый параллельный Builder работает в своём git worktree (физически разные директории)
- Eligibility check перед запуском: Tasks не должны трогать одни и те же файлы
- Conflict detection: если обнаружен конфликт — приостановить один из Builder'ов
- Merge: после завершения всех параллельных Tasks — squash merge в основную ветку

**Open Questions:**
- Max параллельных Builder'ов? → **Решение: конфигурируемо, default 2-3. Ограничение — CPU/RAM Mac'а + rate limits провайдеров.**
- Merge conflicts: автоматический resolve или manual? → **Решение: auto attempt → если конфликт → notify Telegram → ждать human.**

**Критерий "готово":**
- [ ] 10 Tasks → 3 Builder'а параллельно в worktrees
- [ ] Eligibility check: Tasks 1,2,3 параллельны, Task 4 ждёт Task 1
- [ ] Squash merge → main
- [ ] Telegram: "готово за 2 часа вместо 6"
- [ ] Cost report per role

---

### MVP6 — "Real-time Voice"

**Проблема:** голосовые = рация, хочется живой разговор.

**Дельта поверх MVP5:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | MTProto Client | `mtproto.py` | — | Pyrogram userbot для перехвата звонков |
| 2 | Audio Bridge | `audio_bridge.py` | mtproto | pytgcalls → WebSocket → Voice AI → обратно |
| 3 | Barge-In Handler | `barge_in.py` | audio_bridge | Перебил → сброс TTS буфера → режим "слушаю" |
| 4 | Voice AI Connector | `voice_ai.py` | — | WebSocket к Deepgram Voice Agent / Retell |

**Архитектура:**

```
Telegram звонок (MTProto, UserBot)
  → pytgcalls перехватывает WebRTC аудио
  → raw PCM → WebSocket → Voice AI платформа
    └── STT (Deepgram) → LLM (Claude) → TTS (Fish Audio)
  → аудио обратно через pytgcalls
  → barge-in: UserStartedSpeaking → сброс буфера
```

**Риски:**
- UserBot архитектура хрупкая (Telegram может заблокировать)
- pytgcalls не всегда стабильно
- Latency: 300-600ms (зависит от платформы)

**Open Questions:**
- Telegram UserBot или перейти на WhatsApp Business Calling API? → **Решение: начать с Telegram (экосистема уже есть). WhatsApp — альтернатива если Telegram нестабилен.**
- Voice AI: Deepgram (контроль) или Retell (простота)? → **Решение: Retell для быстрого старта. Deepgram для максимального контроля позже.**

**Критерий "готово":**
- [ ] Звонишь → агент слышит → отвечает голосом
- [ ] Barge-in: перебил → замолчал → слушает
- [ ] Full-duplex: < 600ms latency
- [ ] Разговор на русском языке

---

### MVP7 — "Autoresearch"

**Проблема:** агент не учится на ошибках, каждый запуск — с нуля.

**Дельта поверх MVP6:**

| # | Модуль | Файл | Зависит от | Что делает |
|---|--------|------|-----------|-----------|
| 1 | Evaluator | `autoresearch/evaluator.py` | — | composite_score (неизменяемый harness) |
| 2 | Mutator | `autoresearch/mutator.py` | providers | Мутация промптов и параметров |
| 3 | Ratchet | `autoresearch/ratchet.py` | evaluator, mutator | Цикл: мутация → бенчмарк → commit/revert |
| 4 | Task Generator | `autoresearch/task_generator.py` | providers | Генерация синтетических микро-задач |
| 5 | Idle Mode | `autoresearch/idle_mode.py` | ratchet, task_generator | Нет работы → тренируйся |
| 6 | Pattern Extractor | `autoresearch/pattern_extractor.py` | disk_layer | Извлечение паттернов из реальных проектов |

**4 уровня самоулучшения:**

```
Level 1   — промпты (каждая итерация)
Level 1.5 — параметры (каждые 5 итераций L1)
Level 2   — паттерны и память (каждые 2 × L1.5)
Level 3   — кодовая самомодификация (каждые 5 × L2, human review)
```

**Защита:**
- Harness (evaluator + seeds + тесты) — read-only для агента
- Docker sandbox для Level 3
- Human review queue для кодовых мутаций
- Kill switch: `OVERRIDE.md` с "СТОП autoresearch"
- Минимальный score threshold: `composite_score < 0.3` → rollback

**Open Questions:**
- Docker для sandbox обязателен или можно macOS sandbox-exec? → **Решение: Docker. Полная изоляция, воспроизводимость. sandbox-exec слишком ограничен.**
- Seed-проекты: делать самому или взять SWE-bench? → **Решение: начать с 5 самописных seed-проектов (контроль). SWE-bench как валидация позже.**

**Критерий "готово":**
- [ ] Оставил на ночь → утром Telegram: "47 экспериментов, score 0.58 → 0.71"
- [ ] Лучшие мутации: "переписал промпт Builder (retry -40%)"
- [ ] Seed-проекты: стабильный рост на 5+ проектах
- [ ] Level 3: сгенерированный код в review queue

---

## Часть 3: Сквозные Concerns

---

### 3.1 Fault Tolerance

**Эволюция по MVP:**

| MVP | Что добавляется |
|-----|----------------|
| MVP0 | Retry с backoff+jitter, ProviderChain fallback, CircuitBreaker, checkpoint |
| MVP1 | Reflexion (inject причину FAIL в retry), Builder retry max 2, Verifier ANOMALY |
| MVP2 | Stuck Detection (структурный), 3-уровневая эскалация, Coach как Уровень 2 |
| MVP3 | Semantic Loop Detection (cosine similarity), антипамять, effort-based retry |
| MVP4 | Reconnect при sleep Mac'а (Telegram бот), graceful degradation STT |
| MVP5 | Worktree conflict detection, merge conflict resolution |
| MVP6 | Audio reconnect, barge-in recovery |
| MVP7 | Catastrophic score drop → rollback, kill switch |

**Ключевые числа (стартовые, калибруются):**

| Параметр | Значение |
|----------|----------|
| retry_count лимит | 3 |
| steps_per_task лимит | 15 |
| cosine similarity порог | 0.90 |
| Circuit Breaker порог | 3 ошибки подряд |
| CB half-open timeout | 60 сек |
| backoff base | 2^attempt + jitter |
| reflexion cycles max | 2 |
| diversification temp | min(current + 0.3, 1.0) |

---

### 3.2 Context Assembly

**Эволюция по MVP:**

| MVP | Подход |
|-----|--------|
| MVP0 | Нет (просто передаём план + промпт целиком) |
| MVP1 | Базовый: system_prompt + PLAN + SUMMARY предыдущих Tasks. Бюджетный контроль 70/80/95% |
| MVP2 | + CONTEXT_MAP (от Scout), + CONTEXT_HINTS (от Coach), + STRATEGY injection |
| MVP3 | + scratchpad (антипамять) для Debugger, + стектрейсы |
| MVP5 | Prompt Caching: статический prefix максимизирован (system + roadmap + knowledge) |
| MVP7 | PageRank на коде (tree-sitter → AST → NetworkX) для выбора файлов |

**Приоритет инжекции (что обрезается последним):**

```
1. system_prompt     — НИКОГДА не обрезается
2. PLAN текущей Task — НИКОГДА не обрезается
3. CONTEXT_HINTS     — обрезается при COMPRESS (MVP2+)
4. code snippets     — обрезаются при COMPRESS
5. CONTEXT_MAP       — обрезается первым (MVP2+)
6. старые SUMMARY    — обрезаются первым, от самого старого
```

**Оптимальное окно:** 8-20K токенов для Builder (Sweep finding). Больше ≠ лучше.

---

### 3.3 Cost Tracking

**Эволюция по MVP:**

| MVP | Что отслеживается |
|-----|-------------------|
| MVP0 | Общие token/cost за run |
| MVP1 | Per-task: tokens in/out, cost, duration |
| MVP2 | Per-role: сколько стоит Scout vs Builder vs Coach |
| MVP5 | Per-worker: параллельные Builder'ы |
| MVP7 | Per-experiment: autoresearch costs |

**Budget Guards:**
- 50% budget → tier shift suggestion
- 75% budget → warning в Telegram
- 90% budget → auto downgrade model tier
- 100% budget → graceful stop + report

**Формат metrics.json:**

```json
{
  "run_id": "2026-04-14T09:00:00",
  "total_cost_usd": 0.42,
  "total_tokens": {"input": 150000, "output": 45000},
  "by_role": {
    "scout": {"calls": 2, "tokens": 5000, "cost": 0.00},
    "architect": {"calls": 1, "tokens": 25000, "cost": 0.15},
    "builder": {"calls": 7, "tokens": 80000, "cost": 0.05},
    "verifier": {"calls": 7, "tokens": 20000, "cost": 0.00},
    "coach": {"calls": 2, "tokens": 20000, "cost": 0.22}
  },
  "retries": 3,
  "duration_s": 7200
}
```

---

### 3.4 Human Steering

**Механизм:** Dispatcher проверяет `.sora/human/` на каждой фазовой границе.

| Файл | Когда проверяется | Что делает |
|------|------------------|-----------|
| `STEER.md` | Между Slices, между фазами | Мягкое изменение направления |
| `OVERRIDE.md` | После каждой Task | Экстренное: СТОП, пропустить, изменить |
| `HUMAN_CONTEXT.md` | При Context Assembly | Дополнительный контекст, всегда инжектируется |

**Telegram как STEER (MVP1+):**
- Сообщение в бот → Concierge интерпретирует → пишет в STEER.md / OVERRIDE.md
- "стоп" → OVERRIDE.md: "PAUSE"
- "продолжай" → OVERRIDE.md: удалить
- "переориентируйся на мобильную версию" → STEER.md

---

## Часть 4: Dependency Graph

---

### 4.1 MVP зависимости

```
MVP0 (Бессмертный раннер)
  │
  ├──► MVP1 (Полируй и строй)
  │      │
  │      ├──► MVP2 (Стратег)
  │      │      │
  │      │      ├──► MVP3 (Франкенштейн)
  │      │      │      │
  │      │      │      └──► MVP4 (Голос) *
  │      │      │             │
  │      │      │             └──► MVP5 (Параллелизм) *
  │      │      │                    │
  │      │      │                    ├──► MVP6 (Real-time Voice)
  │      │      │                    │
  │      │      │                    └──► MVP7 (Autoresearch)
  │      │      │
  │      │      └──► MVP5 (Параллелизм) — можно без MVP3/MVP4
  │      │
  │      └──► MVP4 (Голос) — можно без MVP2 (Concierge просто делегирует Builder'у)
  │
  └──► MVP4 (Голос) — минимальный: STT + TTS без ролей

* MVP4 и MVP5 не зависят друг от друга — можно в любом порядке
```

**Жёсткие зависимости:**
- MVP1 → MVP0 (нужен runner)
- MVP2 → MVP1 (нужен Builder + Verifier)
- MVP7 → MVP2 (нужны все роли для оптимизации промптов)

**Мягкие зависимости (можно в другом порядке):**
- MVP3 ↔ MVP4 ↔ MVP5 — независимы друг от друга, все требуют MVP2
- MVP6 → MVP4 (нужен STT/TTS стек)

### 4.2 Модульные зависимости (MVP0+MVP1)

```
                    ┌──────────┐
                    │  cli.py  │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
              ┌─────┤ runner.py ├─────┐
              │     └────┬─────┘     │
              │          │           │
     ┌────────▼──┐  ┌────▼─────┐  ┌─▼──────────┐
     │notifier.py│  │checkpoint │  │ providers/ │
     └────┬──────┘  │  .py     │  ├────────────┤
          │         └────┬─────┘  │ chain.py   │
     ┌────▼──────┐  ┌────▼─────┐  │ + CB       │
     │ config.py │  │disk_layer│  │ registry   │
     └───────────┘  │  .py     │  │ claude     │
                    ├──────────┤  │ codex      │
                    │ state.py │  │ opencode   │
                    │ lock.py  │  │ kilo/zai   │
                    └──────────┘  └────────────┘

MVP1 добавляет:
     ┌─────────────┐  ┌────────────────┐  ┌──────────┐
     │ persona.py  │  │context_assembly│  │plan_hard.│
     └──────┬──────┘  │     .py        │  │  .py     │
            │         └───────┬────────┘  └────┬─────┘
     ┌──────▼──────┐          │                │
     │ .sora/      │    ┌─────▼──────┐   ┌─────▼─────┐
     │ prompts/*.md│    │ Builder    │   │ Verifier  │
     └─────────────┘    │ (player)   │   │ (player)  │
                        └────────────┘   └───────────┘
```

### 4.3 Критический путь для MVP0

```
Шаг 1 ─── constants.py + errors.py ─────────────┐
Шаг 2 ─── config.py ────────────────────────────┤
                                                  ├── Шаг 5: providers/ перенос
Шаг 3 ─── state.py + lock.py ──┐                │
                                 ├── Шаг 4:      │
                                 │   disk_layer   │
                                 │                │
Шаг 6 ─── circuit_breaker.py ──┤                │
                                 ├── Шаг 7: CB + chain.py
                                 │
Шаг 8 ─── checkpoint.py ───────┤
                                 │
Шаг 9 ─── notifier.py ─────────┤
                                 ├── Шаг 10: runner.py
                                 │
                                 ├── Шаг 11: cli.py
                                 │
                                 └── Шаг 12: daemon plist
```

**Параллельные треки:**
- Track A: constants → state/lock → disk_layer → checkpoint
- Track B: config → notifier
- Track C: providers перенос → circuit_breaker → CB интеграция в chain
- Merge: runner.py (ждёт A + B + C) → cli.py → daemon

---

## Часть 5: Сводная таблица

| MVP | Название | Новые модули | Ключевая ценность | Зависит от |
|-----|----------|-------------|-------------------|-----------|
| 0 | Бессмертный раннер | runner, checkpoint, notifier, CB, disk_layer, config, cli | Не крашится, уведомляет | — |
| 1 | Полируй и строй | persona, context_assembly, plan_hardening, builder, verifier, reflexion, telegram_input | Полирует план, строит с проверкой | MVP0 |
| 2 | Стратег | scout, architect, coach_runner, triggers, stuck_detection, escalation | Сам декомпозирует и адаптируется | MVP1 |
| 3 | Франкенштейн | debugger, scratchpad, multipatch, effort_classifier, semantic_loop | Автономный баг-хантер | MVP2 |
| 4 | Голос | stt, concierge, vision, specialist_router | Голосовое управление | MVP1 (мин) / MVP2 (полный) |
| 5 | Параллелизм | worktree, eligibility, conflict_detection, merge | 2-3x скорость | MVP2 |
| 6 | Real-time Voice | mtproto, audio_bridge, barge_in, voice_ai | Живой разговор | MVP4 |
| 7 | Autoresearch | evaluator, mutator, ratchet, task_generator, idle_mode, pattern_extractor | Самоулучшение | MVP2 |
