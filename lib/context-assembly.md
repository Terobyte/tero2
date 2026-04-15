# Context Assembly — предзагрузка контекста

> Каждая задача стартует с чистого контекста (Iron Rule). Context Assembly — алгоритм динамической инжекции только нужной информации в промпт агента.

## Принцип

Builder/Debugger не помнит предыдущие задачи. Context Assembly **до запуска** собирает релевантный контекст:

```
pre-inlined prompt = system_prompt (роль)
                   + PLAN.md (текущая задача)
                   + SUMMARY (предыдущие задачи, lossy compression)
                   + CONTEXT_HINTS.md (от Coach)
                   + CONTEXT_MAP.md (от Scout, при наличии)
                   + code snippets (файлы из плана)
```

## Бюджет контекста

| Порог | Действие |
|-------|---------|
| **< 70%** | Нормальная работа. Цель: оставаться здесь |
| **70-80%** | Warning. Следующая задача может потребовать компрессии |
| **80-95%** | Обрезать: убрать старые SUMMARY, оставить PLAN + 2 последних |
| **> 95%** | Hard fail. НЕ запускать. Компрессия или эскалация |

```python
def check_budget(assembled_tokens: int, model_limit: int) -> str:
    ratio = assembled_tokens / model_limit
    if ratio > 0.95: return "HARD_FAIL"
    elif ratio > 0.80: return "COMPRESS"
    elif ratio > 0.70: return "WARNING"
    return "OK"
```

## Приоритет инжекции (что обрезается последним)

```
1. system_prompt (роль)           — НИКОГДА не обрезается
2. PLAN.md (текущая задача)       — НИКОГДА не обрезается
3. CONTEXT_HINTS.md (от Coach)    — обрезается при COMPRESS
4. code snippets (файлы из плана) — обрезаются при COMPRESS
5. CONTEXT_MAP.md (от Scout)      — обрезается первым
6. SUMMARY старых задач           — обрезается первым, от самого старого
```

При `COMPRESS`: убирать с низшего приоритета пока бюджет < 80%.

## Оптимальное окно контекста

**Ключевой инсайт** (Sweep, 7.7K★): качество решений пикает при **10-15K токенов**, НЕ при максимуме окна. **8-20K** релевантного контекста > 100K всего подряд.

## Алгоритм сборки

```python
def assemble_context(role, task_plan, summaries, context_hints,
                     context_map, code_snippets, model_limit=200_000):
    # Шаг 1: обязательные (не обрезаются)
    parts = [load_system_prompt(role), f"# Current Task\n{task_plan}"]
    # Шаг 2: необязательные (в порядке приоритета)
    optional = []
    if context_hints:
        optional.append(("hints", context_hints))
    for path, code in code_snippets.items():
        optional.append(("code", f"## {path}\n```\n{code}\n```"))
    if context_map:
        optional.append(("map", context_map))
    for summary in reversed(summaries):
        optional.append(("summary", summary))
    # Шаг 3: greedy — добавлять пока помещается
    for label, content in optional:
        candidate = "\n\n".join(parts) + f"\n\n{content}"
        if estimate_tokens(candidate) < model_limit * 0.70:
            parts.append(content)
        else:
            break
    final = "\n\n".join(parts)
    if check_budget(estimate_tokens(final), model_limit) == "HARD_FAIL":
        raise ContextWindowExceededError()
    return final
```

## PageRank для выбора code snippets

Продвинутый метод выбора файлов (Aider, 43K★):

```
1. tree-sitter → AST теги (имя, файл, строка, тип=def/ref)
2. Направленный граф: nodes=файлы, edges=cross-file references
3. NetworkX PageRank с personalization → файлы из плана
4. Файлы добавляются в PageRank order до исчерпания бюджета
```

> Эволюция Context Assembly (простой подход → PageRank) — см. `detailed-roadmap.md` §3.2.

## Context Assembly по ролям

| Роль | Что инжектируется | Целевой размер |
|------|-------------------|---------------|
| **Scout** | Структура директорий + git log | 5-10K |
| **Architect** | STRATEGY + CONTEXT_MAP + ROADMAP | 15-25K |
| **Builder** | PLAN + 2 последних SUMMARY + CONTEXT_HINTS + code | 8-20K |
| **Verifier** | PLAN (must-haves) + код от Builder | 10-15K |
| **Debugger** | Стектрейс + код + scratchpad (антипамять) | 10-20K |
| **Reviewer** | Diff + план + контекст файлов | 10-20K |
| **Designer** | Описание + скриншоты + существующие компоненты | 15-25K |
| **Coach** | Все SUMMARY + DECISIONS + EVENT_JOURNAL + metrics | 30-50K |

Coach — самый большой контекст (полная картина), но даже Coach <= 50K. Роли и модели конфигурируются через `.sora/config.toml`.

## Prompt Caching

Структура промпта для max cache hits:

```
[STATIC PREFIX]    system_prompt + ROADMAP.md + KNOWLEDGE.md
[SEMI-STATIC]      STRATEGY.md + CONTEXT_MAP.md
[DYNAMIC SUFFIX]   PLAN + SUMMARY + code snippets
```

Статический prefix максимизирован → максимум cache hits → экономия токенов.

## Связь с библиотекой

| Паттерн | Источник |
|---------|---------|
| Iron Rule (1 задача = 1 окно) | gsd2-architecture.md |
| 10-15K optimal window | github-findings.md (Sweep) |
| PageRank на графе кода | github-findings.md (Aider) |
| Context Fallback при переполнении | fault-tolerance.md |
| Context Assembly evolution (roadmap) | detailed-roadmap.md §3.2 |
