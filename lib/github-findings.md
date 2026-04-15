# Инженерные паттерны — что tero2 берёт из open-source

> Отфильтровано из 16+ проектов. Только паттерны, напрямую используемые в tero2.

---

## 1. "Less is More" — главный инсайт

**Mini-SWE-agent** (Princeton) — 100 строк Python достигает 74% на SWE-bench, matching агентов с 10K+ строк.

**Agentless** (UIUC) — 3-фазный pipeline: localize → edit → validate. $0.34/issue vs $3+ у агентных подходов.

**Вывод**: простые агенты с хорошими инструментами бьют сложных агентов с плохими интерфейсами.

> Используется в: архитектура tero2 — минимальный scaffolding, максимум в промптах.

---

## 2. PageRank на графе зависимостей (Aider, 43K★)

```
1. tree-sitter → AST Tags (name, file, line, kind=def/ref)
2. Directed graph: nodes=files, edges=cross-file refs
3. NetworkX PageRank с personalization → релевантные файлы
4. Token budget → файлы добавляются в PageRank order до исчерпания
```

Disk-cached tag index в SQLite с mtime validation. 130+ языков.

> Используется в: MVP7, модуль context_assembly — ранжирование файлов для инжекции в контекст.

---

## 3. Optimal Window 10-15K (Sweep, 7.7K★)

Качество решений пикает при 10-15K токенов контекста, НЕ при максимуме окна. Больше контекста = хуже решения.

> Используется в: MVP7, context_assembly — лимит инжектируемого контекста.

---

## 4. Boomerang Pattern — context isolation (Roo Code, 23K★)

Оркестратор **намеренно лишён** возможности читать/писать файлы.

```
Orchestrator (no file access)
  ├── new_task → Code Mode (full tools) → completion(result)
  ├── new_task → Architect Mode (read-only) → completion(result)
  └── new_task → Debug Mode (exec tools) → completion(result)
```

"Context poisoning" — давать LLM больше контекста часто ухудшает результат. Изоляция вынуждает делегировать.

> Используется в: SORA — роли (Scout, Builder, Verifier) с изолированными контекстами и инструментами.

---

## 5. Anti-Loop механизмы

| Метод | Как работает |
|-------|-------------|
| **State fingerprinting** | Hash состояния (file hashes + test results). 3+ повтора = loop |
| **Edit-distance monitoring** | Levenshtein между выводами. Падает ниже threshold = loop |
| **Action entropy** | Low entropy = loop. High entropy после многих ходов = lost |
| **Token budget circuit breaker** | 80% бюджета → status report. 100% → kill + escalate |
| **Intention tracking** | Стек интенций. Действие не совпадает с top = drift warning |

> Используется в: MVP4, модуль stuck_detector — state fingerprinting + circuit breaker.

---

## 6. Lint-on-Edit (SWE-agent, 19K★)

| Решение | Эффект |
|---------|--------|
| Валидация ДО исполнения | Каскадные ошибки невозможны |
| Edit показывает before/after diff | -40% ошибок редактирования |
| Пустой output → явное подтверждение | LLM не путается от тишины |

> Используется в: Builder + Verifier — lint/typecheck после каждого edit.

---

## 7. Prompt Caching Exploitation

Структурировать промпт: **статический prefix максимизирован** (system + repo map + task), переменный suffix минимизирован → max cache hits.

> Используется в: MVP3, модуль llm_client — кеширование system prompt + context prefix.

## 8. State Machine с фазами

```
UNDERSTAND → PLAN → IMPLEMENT → TEST → DEBUG → REVIEW
```

- В каждой фазе **разный набор инструментов**
- **Checkpoint после каждой фазы** (git commit / state snapshot)
- При провале → rollback к checkpoint и retry с другим подходом

Sub-state machine для DEBUG: `REPRODUCE → HYPOTHESIZE → INSTRUMENT → TEST → FIX → VERIFY`

> Используется в: Dispatcher (GSD-2 state machine) + роль Debugger.

## 9. Adversarial Self-Review

Второй агент с целью **найти баги** в коде первого. Adversarial dynamic ловит ошибки, которые self-review пропускает.

> Используется в: Iterative Plan Hardening (adversarial review фаза).

## Ключевые числа

| Метрика | Значение | Источник |
|---------|----------|---------|
| Optimal context window | **10-15K tokens** | Sweep |
| Mini-agent vs full agent | **100 LOC = 10K+ LOC** по качеству | Mini-SWE-agent |
| Cost: pipeline vs agent | **$0.34 vs $3+** per issue | Agentless |
| Lint-on-edit error reduction | **-40%** ошибок | SWE-agent |
| Opus vs Haiku cost | **60x** разница | API pricing |
