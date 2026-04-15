# SORA — Strategic Orchestration with Role-based Agents

> Гибридная архитектура автономных AI-агентов, объединяющая надёжность GSD-2 и адаптивный интеллект G3 Coach-Player.
> Это архитектурный справочник. План реализации — в `detailed-roadmap.md`.

---

## Проблема

| GSD-2 | G3 Coach-Player |
|---|---|
| Надёжный, crash-safe, детерминированный | Адаптивный, стратегически гибкий |
| Все агенты одинаковые — нет специализации | Coach деградирует от накопления контекста |
| Reassessment только в конце Slice | Coach блокирует hot path |
| Нет реального стратегического мышления | Нет crash recovery |

**SORA** берёт лучшее из обоих: детерминированный dispatcher из GSD-2 + эпизодический асинхронный Coach из G3.

**tero2 — инструмент для архитектора (пользователя).** Не "опиши приложение и оно построится", а инструмент который усиливает решения пользователя через специализированных агентов.

---

## Ключевая идея: Async Coach

Coach — это НЕ постоянно работающий агент. Он просыпается по триггерам, читает диск, пишет стратегию, умирает. Каждый вызов — чистый 200k контекст, никакой деградации.

```
Dispatcher (Python) — hot path, всегда работает
    └── вызывает Coach только при триггерах:
        • конец Slice
        • anomaly detected
        • budget >= 60%
        • stuck pattern
        • unexpected error от Builder

Coach (Codex) — эпизодически, НЕ в hot path
    читает:  все SUMMARY.md + DECISIONS.md + EVENT_JOURNAL.md + ROADMAP.md
    пишет:   STRATEGY.md + TASK_QUEUE.md + RISK.md + CONTEXT_HINTS.md
    умирает: контекст освобождён, нет деградации
```

---

## Архитектура по слоям

### Слой 1 — Deterministic Dispatcher

Python state machine. Никакого LLM в цикле принятия решений о том, что запустить следующим.

**Что делает:**
- Читает `STATE.md` с диска
- Решает: какой Player нужен, когда
- Вызывает Async Coach при триггерах
- Пишет `auto.lock` на время задачи
- Обрабатывает crashes, timeouts, retries

**Отличие от GSD-2:** Dispatcher может переорганизовать очередь задач на основе `STRATEGY.md`, который Coach записал на диск. Адаптивность без LLM в hot path.

---

### Слой 2 — Async Strategic Coach

Главная инновация SORA.

**Провайдер:** Codex (лучший в coaching, бесплатный). Configurable через `.sora/config.toml`.

**Читает при пробуждении:**
- Все `T0X-SUMMARY.md` задач за текущий Slice
- `DECISIONS.md` — история архитектурных решений
- `EVENT_JOURNAL.md` — аномалии от Verifier
- `ROADMAP.md` — общая цель
- `metrics.json` — cost/token статистика

**Пишет перед смертью:**
- `STRATEGY.md` — пересмотренные приоритеты и фокус
- `TASK_QUEUE.md` — переранжированная очередь задач
- `RISK.md` — что может пойти не так в следующем Slice
- `CONTEXT_HINTS.md` — подсказки для Players (domain knowledge)
- `DECISIONS.md` — append новых ADR

**Почему это решает проблему G3:**
Coach в G3 деградирует потому что держит весь контекст одновременно с работой Players. Здесь Coach стартует с чистым контекстом, получает компрессированный срез через SUMMARY-файлы — и его контекст после записи больше не нужен.

---

### Слой 3 — Specialized Players

Роли с разными системными промптами и разными моделями. Идея из G3, надёжность исполнения из GSD-2.

**Все role-provider маппинги конфигурируемы через `.sora/config.toml`.** Указанные модели — defaults.

#### Scout — быстрый разведчик
- **Модель:** Configurable (default: Kilo, `kilo/xiaomi/mimo-v2-pro:free`)
- **Запускается:** перед каждым Slice
- **Задача:** обход кодовой базы, понимание текущего состояния
- **Пишет:** `CONTEXT_MAP.md` — сжатая карта релевантных файлов

#### Architect — проектировщик
- **Модель:** Configurable (default: Claude Opus)
- **Запускается:** один раз на Slice, после Scout
- **Задача:** декомпозиция Slice на Tasks, проектирование интерфейсов
- **Читает:** `STRATEGY.md` от Coach + `CONTEXT_MAP.md` от Scout
- **Пишет:** `S0X-PLAN.md` с must-haves для каждой Task

#### Builder — исполнитель
- **Модель:** Configurable (default: OpenCode/Z.AI, `z.ai/glm-5.1`) → Codex → Kilo (fallback)
- **Запускается:** на каждую Task (N раз за Slice)
- **Задача:** написание кода
- **Получает:** pre-inlined контекст (PLAN + SUMMARY предыдущих Tasks + CONTEXT_HINTS)
- **Пишет:** `T0X-SUMMARY.md` + git commit

#### Verifier — контролёр
- **Модель:** Configurable (default: Kilo) → OpenCode (fallback)
- **Запускается:** после каждого Builder
- **Задача:** запуск тестов, линтеров, сравнение с must-haves
- **Пишет:** `EVENT_JOURNAL.md` при аномалиях (триггерит Coach)
- **Блокирует:** переход к следующей Task при critical failures

---

### Слой 4 — Disk Layer

Диск — единственная шина коммуникации. Это гарантирует crash-safety и human steering.

```
.sora/
├── config.toml        # role→provider mapping, все роли конфигурируемы
├── runtime/           # ephemeral
│   ├── STATE.md
│   ├── auto.lock
│   └── completed-units.json
│
├── strategic/         # Coach пишет, Dispatcher читает
│   ├── STRATEGY.md
│   ├── TASK_QUEUE.md
│   ├── RISK.md
│   └── CONTEXT_HINTS.md
│
├── persistent/        # всегда, не удаляется
│   ├── PROJECT.md
│   ├── DECISIONS.md
│   ├── KNOWLEDGE.md
│   └── EVENT_JOURNAL.md
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
│       └── CONTEXT_MAP.md    # Scout пишет
│
├── human/             # human steering
│   ├── STEER.md       # положи сюда — подхватится на следующей границе
│   ├── OVERRIDE.md    # экстренное изменение курса
│   └── HUMAN_CONTEXT.md
│
└── reports/           # observability
    ├── metrics.json
    ├── activity.jsonl
    └── M001-report.html
```

---

## Цикл выполнения (полный loop)

```
[Human кладёт ROADMAP.md и запускает: sora auto]

1.  Dispatcher читает STATE.md
    └── нет активного Slice → запустить Scout

2.  Scout (Kilo, свежий контекст)
    └── пишет CONTEXT_MAP.md → умирает

3.  Dispatcher проверяет STRATEGY.md
    └── есть? → передать Architect
    └── нет?  → вызвать Coach первый раз

4.  [Если нужен Coach]
    Coach (Codex, свежий контекст)
    └── читает ROADMAP + CONTEXT_MAP
    └── пишет STRATEGY.md + TASK_QUEUE.md → умирает

5.  Architect (Claude Opus, свежий контекст)
    └── читает STRATEGY.md + CONTEXT_MAP.md
    └── пишет S01-PLAN.md с N Tasks → умирает

6.  Для каждой Task:
    a. Builder (OpenCode/Z.AI, свежий контекст)
       └── получает pre-inlined: PLAN + предыдущие SUMMARY + CONTEXT_HINTS
       └── пишет код + T0X-SUMMARY.md → git commit → умирает
    b. Verifier (Kilo, свежий контекст)
       └── тесты, линтеры, must-haves check
       └── OK → следующая Task
       └── FAIL → Builder retry (max 2)
       └── ANOMALY → пишет EVENT_JOURNAL.md

7.  Конец Slice → ТРИГГЕР для Coach
    Coach (Codex) читает все T-SUMMARY + EVENT_JOURNAL
    └── пишет обновлённый STRATEGY.md → умирает
    Dispatcher читает новый STRATEGY.md
    └── продолжает или переставляет приоритеты

8.  Повтор по всем Slices
    └── Milestone validation
    └── Squash merge → main
    └── HTML report → .sora/reports/

[Human возвращается — всё готово]
```

---

## Human steering без остановки

Система работает пока тебя нет. Когда хочешь изменить курс:

```bash
# Мягкое изменение — подхватится на следующей фазовой границе
echo "Переориентируй на мобильную версию, десктоп вторичен" > .sora/human/STEER.md

# Экстренное изменение — Dispatcher проверяет после каждой Task
echo "СТОП: не трогай auth модуль, я его переписываю" > .sora/human/OVERRIDE.md

# Добавить контекст который агенты не знают
echo "Клиент требует GDPR compliance для EU данных" >> .sora/human/HUMAN_CONTEXT.md
```

Dispatcher читает `human/` директорию на каждой фазовой границе. Ничего прерывать не нужно.

---

## Конфигурируемость ролей

Все role-provider маппинги задаются в `.sora/config.toml`. Пользователь может:

- Переназначить любую роль на другого провайдера
- Изменить модель для конкретной роли
- Настроить fallback-цепочки
- Менять конфигурацию между запусками без изменения кода

Примеры: использовать Claude Sonnet вместо Opus для Architect (экономия), или Codex вместо OpenCode для Builder (другое предпочтение).

> Формат config.toml: `providers.md`

---

## Сравнение с исходными системами

| Характеристика | GSD-2 | G3 Coach-Player | **SORA (tero2)** |
|---|---|---|---|
| Оркестратор | TypeScript state machine | LLM Coach | Python + async Coach (Codex) |
| Контекст | Свежий per task | Накапливается в Coach | Свежий везде, включая Coach |
| Специализация | Нет (все одинаковые) | Да (роли) | Да (9 ролей, все configurable) |
| Builder | Одна модель | Одна модель | OpenCode/Z.AI (main), Codex fallback |
| Coach | Нет | LLM в hot path | Codex, эпизодический |
| Стратегическая адаптация | End-of-slice reassessment | Реальное время | Триггерная, без hot path |
| Crash recovery | Встроенная | Нет | Встроенная (паттерны GSD-2) |
| CircuitBreaker | Нет | Нет | Да (новое в tero2) |
| Human steering | STEER.md | Ручное | STEER.md + OVERRIDE.md + Telegram |
| Cost tracking | Да | Нет | Да + per-role breakdown |
| Деградация качества | Нет | Да (Coach) | Нет |
| Role config | Хардкод | Хардкод | `.sora/config.toml` |

---

## Почему автономность работает

Система не требует человека потому что:

- **Coach думает стратегически** между Slices — без него агенты могут уйти в сторону
- **Verifier закрывает петлю качества** — Builder не продвигается дальше пока тесты не зелёные
- **Disk — единственная истина** — крэш в любой момент не теряет прогресс
- **EVENT_JOURNAL триггерит Coach** при аномалиях — система самокорректируется
- **STEER.md** — если захочешь поменять направление не прерывая работу
- **CircuitBreaker** — автоматически обходит падающих провайдеров, снижает latency

> Ключевой принцип: каждый компонент может упасть, перезапуститься и продолжить с того же места. Никакого in-memory состояния которое нельзя восстановить с диска.

> План реализации: `detailed-roadmap.md`. Отказоустойчивость: `fault-tolerance.md`.
