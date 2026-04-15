# Парадигма Autoresearch — архитектура и применение к tero2

> Цикл Карпаты (autoresearch, март 2026, ~54K stars за 19 дней). ~630 строк кода → универсальный алгоритм для научных открытий и оптимизации.

## Ключевая идея

**Karpathy Loop**: изолированная среда + один редактируемый файл + скалярная метрика + временной бюджет. Агент предлагает изменения, оценивает результат, сохраняет только улучшения через `git keep/revert`.

**Формула**: если задача = (редактируемый артефакт) + (фиксированные ограничения) + (скалярная метрика) — она автоматизируема.

## Архитектура трёх файлов

| Файл | Роль | Доступ агента |
|------|------|---------------|
| `prepare.py` | Неизменная инфраструктура: данные, оценка | Запрещён |
| `train.py` | Экспериментальная песочница | Полный |
| `program.md` | Конституция: гипотезы, рамки, критерии | Редактируется человеком |

Барьер между `prepare.py` и `train.py` — единственная гарантия от reward hacking.

## Операционный цикл (Ratchet)

```
1. Читает program.md и текущий train.py
2. Гипотеза → правки в код
3. Эксперимент: строго 5 минут wall clock
4. val_bpb улучшился → git commit | ухудшился → git revert
5. Повтор: ~12 экспериментов/час, ~80-100 за ночь
```

## Ловушки и как их избегать

| Ловушка | Решение |
|---------|---------|
| Переобучение на тестовой выборке | Expanding Time Windows — обучать на прошлом, тестировать на будущем |
| Reward Hacking | `prepare.py` запрещён для агента |
| Agentic Trust Gap (прирост = открытие или шум?) | Journal-Observer Protocol — доказать каузальность до merge |
| Локальные оптимумы | Level 2: Tabu Search + Orthogonal Exploration |

## Ключевые числа

| Параметр | Значение |
|----------|----------|
| Экспериментов в час / за ночь | ~12 / 80-100 |
| Bilevel прирост (Level 2 vs 1) | 5x |
| Shopify QMD: 0.8B обошла 1.6B на | 19% |

---
---

# Autoresearch для tero2 — самоулучшение на ходу

> Karpathy Loop для самого tero2. Агент оптимизирует **собственные процессы, промпты, параметры и паттерны**. Без проектов — тренируется на бенчмарках.

## Формула

```
Артефакт     = промпты, параметры, стратегии, паттерны tero2
Ограничения  = harness (тесты, seed-проекты, SWE-bench) — неизменяем
Метрика      = composite_score
```

## Четыре уровня самоулучшения

### Level 1 — Промпты и стратегии

Оптимизируется: системные промпты ролей, промпт Coach'а, шаблоны Plan Hardening.

```
Текущие промпты → бенчмарк → мутация → повторный бенчмарк → commit/revert
```

Хранение: `.sora/autoresearch/prompts/`

### Level 1.5 — Параметры и процессы

Оптимизируется: hardening проходы, контекстный бюджет, retry стратегии, Coach триггеры, температура per role.

Цикл: каждые 5 итераций Level 1. Хранение: `.sora/autoresearch/params.json`

### Level 2 — Паттерны и память

Оптимизируется: база паттернов по типам задач, антипаттерны (blacklist), domain knowledge.

```
Завершённый проект → анализ retry/success/bugs → паттерны → KNOWLEDGE.md
→ инжекция в CONTEXT_HINTS → бенчмарк с/без → Δscore
```

Хранение: `.sora/autoresearch/patterns/`

### Level 3 — Кодовая самомодификация

Оптимизируется: стратегии поиска, эвристики stuck detection, новые модули-плагины.

```
Логи → генерация Python-кода → бенчмарк → commit/revert
```

**Защита**: Docker, AST-валидация, sandbox, human review queue. Хранение: `.sora/autoresearch/mutations/`

---

## Idle Mode — тренировка без проектов

```
1. [ДЁШЕВО]  Синтетические микро-задачи   — генерирует сам, быстрый feedback
2. [СРЕДНЕ]  Seed-проекты                 — шаблонные проекты с готовыми тестами
3. [ДОРОГО]  SWE-bench задачи             — реальные issue из GitHub
```

### Синтетические микро-задачи

Генерирует задачу + тест-сьют → Builder строит → Verifier прогоняет тесты → метрика.
Типы: CLI, REST API, парсеры, рефакторинг, баг-фиксинг (ротация).

### Seed-проекты

| Seed | Тип | Тесты |
|------|-----|-------|
| `seed-cli-todo` | CLI приложение | 15 |
| `seed-api-rest` | REST API | 20 |
| `seed-parser` | Data parser | 12 |
| `seed-refactor` | Грязный код → чистый | 18 |
| `seed-bugfix-10` | 10 известных багов | 10 |
| `seed-fullstack` | Frontend + API | 25 |

Каждый seed = git-репо с неизменяемыми тестами (аналог `prepare.py`).

---

## composite_score

```python
def composite_score(run_results):
    """Одно число 0.0 → 1.0."""
    return (
        (tests_passed / tests_total)                    * 0.45  # качество
      + (1 - total_retries / max_possible_retries)      * 0.25  # эффективность
      + (1 - total_cost / budget_cap)                   * 0.15  # экономия
      + (1 - wall_time / time_budget)                   * 0.15  # скорость
    )
```

Формула зафиксирована в harness. Seeds и тесты в read-only репо. Docker изоляция.

---

## Ratchet-цикл

```
while idle:
    1. Выбрать задачу (синтетика → seed → SWE-bench)
    2. Snapshot (git tag)
    3. composite_score (baseline)
    4. Мутация (Level 1/1.5/2/3 по расписанию)
    5. composite_score (candidate)
    6. candidate > baseline + ε (0.01) → commit | else → revert
    7. Логирование → EXPERIMENT_LOG.md
    Прерывание: реальная задача → сохранить стейт → переключиться
```

**Расписание:** каждая итерация → L1, каждые 5 → L1.5, каждые 2 L1.5 → L2, каждые 5 L2 → L3 (human review).

---

## Disk-структура

```
.sora/autoresearch/
├── config.json, EXPERIMENT_LOG.md, INSIGHTS.md
├── prompts/          (scout, architect, builder, verifier, coach + history/)
├── patterns/         (by-type/, blacklist.md)
├── mutations/        (pending/, approved/)
├── seeds/            (git submodules)
├── benchmarks/       (runs/, trends.json)
└── harness/          (evaluator.py, task_generator.py, sandbox.Dockerfile)
```

---

## Защитные механизмы

- **Reward Hacking**: harness read-only, Docker изоляция, ротация seeds
- **Катастрофическое ухудшение**: score < 0.3 → rollback, ежедневный snapshot, Telegram алерт при падении 20%
- **Переобучение на seeds**: expanding набор, SWE-bench валидация, cross-validation 70/30
- **Level 3 Safety**: AST-валидация, песочница, human review, kill switch через OVERRIDE.md

> Autoresearch — **MVP7** в `detailed-roadmap.md`. Запускается последним: чем больше компонентов к моменту запуска, тем больше поверхность для оптимизации.
