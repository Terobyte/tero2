# tero2 TUI UX Redesign — Design Spec

**Статус:** design, approved by user, ready for implementation planning
**Дата:** 2026-04-18
**Worktree:** `.worktrees/phase4-tui`
**Baseline branch:** `phase4-tui` (реализация [2026-04-15-mvp2.6-tui-dashboard-design.md](2026-04-15-mvp2.6-tui-dashboard-design.md))
**Автор:** Claude Code (brainstorming session с пользователем)

---

## Prerequisites / Baseline

**Этот spec надстраивается поверх ветки `phase4-tui`**, которая реализует [2026-04-15-mvp2.6-tui-dashboard-design.md](2026-04-15-mvp2.6-tui-dashboard-design.md) (TUI Dashboard). Все ссылки в spec'е на файлы `tero2/tui/*`, `tero2/cli.py` c `cmd_go`, `tero2/events.py`, `tero2/state.py.SoraPhase` — **валидны на ветке `phase4-tui`**, но **отсутствуют в `main`** на момент написания (2026-04-18).

**Что конкретно в `phase4-tui` есть и используется как baseline:**
- `tero2/cli.py` с subcommand `go` (`cmd_go`, `project_path` позиционный).
- `tero2/tui/` полный слой: `app.py` (`DashboardApp`, BINDINGS), `styles.tcss`, `widgets/pipeline.py|log_view.py|usage.py|controls.py`, `screens/role_swap.py|steer.py`.
- `tero2/events.py` с `Command` / `EventDispatcher` / `command_queue`.
- `tero2/state.py` с `SoraPhase` / `AgentState`.

**Статус baseline (2026-04-18):** ✓ `phase4-tui` **merged в `main`** через ff-merge (HEAD = `51e4f80`). Backup-ветка: `main-wip-backup-20260418` (содержит промежуточное состояние main WT, безопасно удалится после верификации). Spec применяется напрямую к main — disclaimer про phase4-tui сохранён как исторический контекст.

**15 open bugs** в `bugs.md` на момент merge — не блокеры для этого redesign'а, но должны быть закрыты параллельно (best-effort). Redesign-правки **не должны** вводить регрессии по уже известным багам.

**Этот spec НЕ дублирует mvp2.6** — он описывает только **addition/modification** поверх baseline. Новые файлы создаются с нуля; существующие (по состоянию `phase4-tui`) правятся точечно.

---

## Summary

Переработка UX терминального интерфейса tero2: добавить запуск без аргументов (стартап-визард), поиск и выбор моделей внутри провайдеров (CLI → модель), глобальные настройки, runtime командную палитру и project-level history. Сохранить все существующие BINDINGS и не ломать текущий Runner/Dashboard pipeline.

**Scope:** редизайн TUI-слоя (`tero2/tui/*`), минимальные правки CLI (`cli.py`), небольшие расширения `Config` (новое поле `TelegramConfig.enabled`). Новые модули: `history.py`, `providers/catalog.py`, `providers/zai.py`, `tui/commands.py`, 5 новых экранов.

**Out of scope (отдельные spec'и):**
- Gemma как local-provider + Telegram-чат с рычагами — записан в [2026-04-18-gemma-telegram-chat-idea.md](2026-04-18-gemma-telegram-chat-idea.md).
- Native OpenRouter provider (сейчас доступ через `opencode`).
- Двусторонний Telegram-chat с Runner'ом.

---

## Problem Statement

Текущее состояние ([tero2/tui/app.py](../../tero2/tui/app.py)):
- `tero2 go` требует `project_path` как позиционный аргумент — без него печатает help и выходит с ошибкой.
- Нет способа выбрать проект, план, провайдеров из TUI — всё через `.sora/config.toml` вручную.
- В `RoleSwapScreen` выбирается только CLI (`claude/codex/opencode/kilo`), но не модель внутри CLI. Нельзя, например, выбрать «opencode с zai/glm-5.1» из UI.
- Нет глобальных настроек — каждый проект конфигурируется отдельно.
- `ControlsPanel` рендерит хоткеи мелким текстом внизу — низкая discoverability.
- Нет project history — после закрытия tero2 пользователь должен помнить путь.
- `zai` как native provider отсутствует (есть в tero, не портирован).

**Боль пользователя (дословно):** «tero2 go без аргументов — ошибка в терминале», «не понятно как двигаться», «меню скудное», «я когда opencode выбираю я не могу выбрать именно zai».

---

## Decisions Log

Ключевые решения, принятые в брейнсторм-сессии (для audit trail):

1. **Глобальный конфиг:** оставляем существующий `~/.tero2/config.toml` со схемой `[roles.<name>]` ([config.py:145](../../tero2/config.py:145)). Не вводим отдельный `providers.toml`. Миграция не нужна.
2. **Навигация:** Command Palette (Ctrl+P, нативная Textual-фича) + замена `ControlsPanel` на нативный `Footer`. Sidebar отклонён.
3. **Provider picker:** двухуровневый — CLI → модель. `model` хранится в `RoleConfig.model` ([config.py:56](../../tero2/config.py:56)). Новые поля в схеме не вводим.
4. **codex reasoning:** через model IDs (`""=medium`, `"gpt-5.4"=high`), как в tero. Отдельное поле `RoleConfig.reasoning` **не** добавляем (YAGNI).
5. **Zai провайдер — ДВА пути доступа**, оба должны быть в UI:
   - **Native:** отдельный `ZaiProvider` через `claude-agent-sdk` с `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic` (порт из tero).
   - **Через opencode:** модель `zai/glm-5.1` в списке opencode — уже работает из коробки.
   - Пользователь выбирает любой путь через ModelPickScreen. Оба видны одновременно.
6. **Меню-навигация:** ↑↓ + Enter через `ListView.Selected`; Esc/q — отмена; Tab — между полями.
7. **Model catalog:** **dynamic fetch** через `opencode models` и `kilo models --refresh`. Hardcoded только для `claude`/`codex`/`zai`/`gemma` (placeholder).
8. **OpenRouter:** через `opencode` (ключ в `~/.config/opencode/opencode.json`). Native OpenRouter-провайдер — не в этот spec.
9. **Telegram:** в настройках только вкл/выкл + минимум полей. Бот остаётся **однонаправленным input'ом** ([telegram_input.py](../../tero2/telegram_input.py) — это plan-launcher, не chat).
10. **Переименования в UI:** «стир» → «указание», «scout» → «разведчик», «builder» → «строитель», «architect» → «архитектор», «verifier» → «проверяющий». Внутренние идентификаторы в коде (`Command("steer", ...)`, `role="scout"`) не меняем.
11. **Scope:** spec покрывает полный redesign, реализация разбивается на 3 milestone'а.

---

## Section 1 — Architecture

### Entry-point flow

```
tero2 go [project_path?]
         │
         ├─ есть project_path? ──► DashboardApp (+Header +Footer +CommandPalette)
         │
         └─ нет → StartupWizard
                     ├─ ProjectPickScreen (history + ручной ввод)
                     ├─ PlanPickScreen (browser .md + idle mode)
                     └─ ProvidersPickScreen (только если нет .sora/config.toml)
                            │
                            ▼
                     DashboardApp (тот же путь что cmd_go)
```

### Новые Screens

- `StartupWizard` — родительский Screen со state (текущий/пройденный/следующий шаг).
- `ProjectPickScreen` — шаг 1 wizard'а.
- `PlanPickScreen` — шаг 2 wizard'а.
- `ProvidersPickScreen` — шаг 3 (опциональный).
- `ModelPickScreen` — shared компонент, переиспользуется в RoleSwapScreen, SettingsScreen, ProvidersPickScreen.
- `SettingsScreen` — хоткей `[o]` из DashboardApp, 3 вкладки.

### Новые не-TUI модули

- `tero2/history.py` — загрузка/запись `~/.tero2/history.json`.
- `tero2/providers/catalog.py` — dynamic model catalog + static fallback.
- `tero2/providers/zai.py` — порт из tero.
- `tero2/providers/registry.py` — unified dispatcher (routes by name к `CLIProvider` либо `ZaiProvider`). **Новый файл** — на `phase4-tui` его нет.

### Новые TUI модули

- `tero2/tui/commands.py` — `CommandProvider` для Textual Command Palette (Ctrl+P).

### Изменения в существующих

- `tero2/cli.py` — `project_path` → `nargs="?"` в `go`-subparser.
- `tero2/tui/app.py` — `+Header`, `+Footer`, `-ControlsPanel`, `+action_new_project`, `+action_change_plan`, `+action_settings`, `COMMANDS` для Command Palette.
- `tero2/tui/screens/role_swap.py` — step 2 → step 2+3 (CLI → модель).
- `tero2/config.py` — `TelegramConfig.enabled: bool` + legacy fallback.

### Удаляется

- `tero2/tui/widgets/controls.py` — заменяется нативным `Footer`.

### Инварианты

- Textual импорты — лениво внутри функций (CLAUDE.md).
- `ClassVar[str]` для DEFAULT_CSS и BINDINGS.
- Try/except вокруг `query_one()` в watcher-методах.
- Wizard не переписывает `config.toml` без явного действия пользователя.
- SORA-invariant (`builder ⇒ architect + verifier`, [config.py:160-166](../../tero2/config.py:160)) проверяется перед запуском Runner'а.
- Существующие BINDINGS (`r/s/p/k/q/1-5`) сохраняются, добавляются только новые (`l/n/o`, `Ctrl+P`).

---

## Section 2 — Startup Wizard

### Screen 1: ProjectPickScreen

```
┌─ tero2 — выбор проекта ─────────────────────────────────────┐
│  ↑↓ выбор  Enter запуск  n ручной ввод  q выход             │
│                                                             │
│  ▶ OpenVerb              ~/Desktop/Projects/Active/OpenVerb │
│    (последний запуск: сегодня 14:23, plan: milestone-8.md)  │
│    CareerBot             ~/Desktop/Projects/Active/CareerBot│
│    (2 дня назад, plan: refactor-api.md)                     │
│    ─────────────────────────────────────────────────────    │
│    [n] Ввести путь вручную                                  │
└─────────────────────────────────────────────────────────────┘
```

**Источник:** `~/.tero2/history.json`. Сортировка `last_run DESC`, лимит 10.

**Действия:**
- `↑↓ + Enter` → PlanPickScreen с выбранной папкой.
- `n` → inline Input для ручного пути; Enter валидирует через `Path.is_dir()`, inline error при неудаче.
- `q` / `Esc` → выход.
- Пустая история → сразу режим ручного ввода.

**Валидация:** `Path(p).expanduser().resolve().is_dir()` обязательна. Если `.sora/` нет — inline warning «проект не инициализирован, будет создан автоматически».

**Missing-file handling:** `is_dir()` false → entry серым, пометка `⚠ папка не найдена`, Enter предлагает удалить из истории.

### Screen 2: PlanPickScreen

```
┌─ tero2 — план для OpenVerb ────────────────────────────────┐
│  ↑↓ выбор  Enter  b назад  i idle-режим  q выход            │
│                                                            │
│  ▶ milestone-8-client.md   ~/Desktop/.../OpenVerb/         │
│    plan.md                 ~/Desktop/.../OpenVerb/         │
│    PLAN.md                 ~/Desktop/.../OpenVerb/docs/    │
│    bug-fixing-plan.md      ~/Desktop/.../OpenVerb/plans/   │
│    ─────────────────────────────────────────────────────   │
│    [i] Запустить без плана (idle mode — жду команд)        │
│    [b] Назад                                               │
└────────────────────────────────────────────────────────────┘
```

**Источник:** `Path(project).rglob("*.md")`, фильтр по `_SKIP_DIRS` — **общий набор** из `tero2/constants.py` (новая константа). Порт из [tero2/players/scout.py:177-188](../../tero2/players/scout.py:177): `.git`, `.venv`, `node_modules`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `dist`. Дополнительно PlanPickScreen исключает `.sora/` полностью (runtime artefacts в `.sora/runtime/` + config/logs — не планы). Лимит 30, сортировка по `mtime DESC`.

**M2 code change:** `_SKIP_DIRS` выносится из `scout.py` в `tero2/constants.py` как `PROJECT_SCAN_SKIP_DIRS`; `scout.py` начинает импортировать оттуда (небольшой рефакторинг, без изменения поведения).

**Действия:**
- `↑↓ + Enter` — выбор плана → если `.sora/config.toml` есть, сразу в Dashboard; иначе в ProvidersPickScreen.
- `i` — idle mode (`plan_file=None`).
- `b` — назад.
- Если 0 `.md` файлов — автоматически idle mode с сообщением.

### Screen 3: ProvidersPickScreen (опциональный)

Показывается **только если** `.sora/config.toml` отсутствует в проекте.

```
┌─ tero2 — провайдеры для нового проекта ────────────────────┐
│  ↑↓ роль  Enter изменить  s сохранить и запустить  b назад │
│                                                            │
│  Строитель   claude       (sonnet)          ◀ default      │
│  Архитектор  claude       (opus)            ◀ default      │
│  Разведчик   codex        (по умолчанию)    ◀ default      │
│  Проверяющий claude       (sonnet)          ◀ default      │
│  Коуч        claude       (opus)            ◀ default      │
│  ─────────────────────────────────────────────────────     │
│  [ ] Сохранить эти значения как глобальный default         │
└────────────────────────────────────────────────────────────┘
```

**Источник дефолтов:** глобальный `~/.tero2/config.toml` если есть, иначе hardcoded (`builder=claude/sonnet`, `architect=claude/opus`, `scout=codex/""`, `verifier=claude/sonnet`, `coach=claude/opus`).

**Действия:**
- `Enter` на роли → `ModelPickScreen` (CLI → модель, возврат).
- `s` → записать `[roles.*]` в `project/.sora/config.toml` атомарно (tmp + `Path.replace()`). Если checkbox отмечен — ещё и в `~/.tero2/config.toml`.
- `b` → назад.

**Валидация SORA:** запрет сохранения при отсутствии `architect` или `verifier`, если есть `builder` — inline error.

---

## Section 3 — Dashboard (Settings, Command Palette, new BINDINGS)

### Новый layout

```
┌─ tero2 · OpenVerb · Phase: EXECUTE · slice 3/12 ───────────┐  ← Header
│  PipelinePanel (SORA-фазы, таймер, роли)                   │
├────────────────────────────────┬───────────────────────────┤
│  LogView                       │  UsagePanel               │
├────────────────────────────────┴───────────────────────────┤
│ [r]оли  [s] указание  [p]ауза  [k]пропустить  [l]план      │  ← native Footer
│ [n]овый  [o]настройки  [q]выход  [^P] палитра              │
└────────────────────────────────────────────────────────────┘
```

### Обновлённые BINDINGS ([app.py:28](../../tero2/tui/app.py:28))

- Существующие (show=True в Footer): `r` роли, `s` указание, `p` пауза, `q` выход, `k` пропустить.
- Новые: `l` план (смена на лету), `n` новый проект, `o` настройки.
- Stuck-options (`1`–`5`) — `show=False` в BINDINGS, чтобы не шумели в Footer.

### Новые actions

- `action_new_project` — ставит текущий runner на паузу через `command_queue`, открывает `StartupWizard` через `push_screen` с callback.
- `action_change_plan` — открывает `PlanPickScreen(self._runner.project_path)` с callback, который шлёт `Command("load_plan", ...)` в `command_queue`.
- `action_settings` — `push_screen(SettingsScreen())`.

**Runner-side handling `Command("load_plan")`:** сохранить checkpoint, загрузить новый план, применить к pipeline. Детали — в M2-плане при анализе [runner.py](../../tero2/runner.py).

### Command Palette

Новый модуль `tero2/tui/commands.py` с `Tero2CommandProvider(CommandProvider)`:

- `search(query)` ищет по набору пар `(label, fn)`:
  - «Открыть новый проект» → `action_new_project`
  - «Сменить план» → `action_change_plan`
  - «Сменить провайдера роли» → `action_roles`
  - «Отправить указание агенту» → `action_steer`
  - «Настройки (глобальные)» → `action_settings`
  - «Пауза / возобновить» → `action_pause`
  - «Пропустить задачу» → `action_skip`
  - «Выход» → `action_quit`
- Fuzzy matching через `self.matcher(query)`, `Hit(score, highlight, fn)`.

Регистрация в `DashboardApp`: `COMMANDS: ClassVar[set] = {Tero2CommandProvider}`. Ctrl+P работает нативно.

### SettingsScreen (хоткей `[o]`)

```
┌─ tero2 — настройки (глобальные ~/.tero2/config.toml) ──────┐
│  Tab: [Провайдеры]  [Telegram]  [Поведение]    b назад     │
│  ↑↓ выбор  Enter изменить  s сохранить         q выход     │
│                                                            │
│  ▶ Строитель     claude      (sonnet)                      │
│    Архитектор    claude      (opus)                        │
│    Разведчик     codex       (gpt-5.4)                     │
│    Проверяющий   claude      (sonnet)                      │
│    Коуч          claude      (opus)                        │
│    ─────────────────────────────────────────────           │
│    [+] Добавить роль                                       │
└────────────────────────────────────────────────────────────┘
```

**Tabs** (Textual `TabbedContent`, переключение shift+←/→):

| Tab | Содержимое | Пишет в `~/.tero2/config.toml` |
|-----|-----------|-------------------------------|
| Провайдеры | Список ролей, Enter → ModelPickScreen | `[roles.<name>]` |
| Telegram | `enabled` toggle; если вкл — bot_token, allowed_chat_ids, voice_on_done | `[telegram]` |
| Поведение | max_slices, idle_timeout_s, stuck_detection | `[sora]`, `[stuck_detection]` |

**Telegram tab UI-текст** (важно — не соврать пользователю):

```
Telegram-бот:  [  выкл  ]

Bot token:                 ••••••••1234
Разрешённые chat_id:       614473938
Голосовое уведомление:     [  вкл  ]

Что делает бот:
  • принимает .md-планы и текстовые сообщения
  • создаёт проект и запускает runner
  • НЕ чат с агентом — только вход
```

**Сохранение:** `s` — атомарная запись через `Path.replace()`. **Не применяется** к текущему запущенному Runner'у — глобальный конфиг подтянется следующим запуском. Runtime-override провайдеров — отдельно через `[r]` (RoleSwapScreen → `switch_provider` Command).

**Граница обязанностей:**
- `[o]` SettingsScreen → **будущие** запуски.
- `[r]` RoleSwapScreen → **текущий** запуск.

Разные BINDINGS → разная семантика.

---

## Section 4 — Data models & on-disk files

### 4.1 `~/.tero2/history.json`

```json
{
  "version": 1,
  "entries": [
    {
      "path": "/Users/terobyte/Desktop/Projects/Active/OpenVerb",
      "name": "OpenVerb",
      "last_run": "2026-04-18T14:23:11Z",
      "last_plan": "milestone-8-client.md",
      "run_count": 17
    }
  ]
}
```

**Модуль `tero2/history.py`:**
- `@dataclass HistoryEntry` с полями `path, name, last_run, last_plan, run_count`.
- `load_history() -> list[HistoryEntry]` — читает файл, fallback на пустой список.
- `record_run(project_path, plan_file)` — апдейтит или создаёт entry, атомарная запись.
- `trim_history(max_entries=20)` — удаляет самые старые, сортирует по `last_run`.

**Когда пишется:** при успешном старте DashboardApp'а (в `cmd_go` или по завершении wizard'а). Атомарная запись через `Path.replace()`.

### 4.2 `Config` / `RoleConfig` — минимальные правки

```python
@dataclass
class TelegramConfig:
    enabled: bool = False        # NEW
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True
    allowed_chat_ids: list[str] = field(default_factory=list)
```

**Legacy fallback в `_parse_config`** ([config.py:168](../../tero2/config.py:168)):
если `enabled` отсутствует в TOML, но `bot_token` непустой — считаем `enabled = True` (сохраняем поведение tero2 < redesign).

`RoleConfig.model` уже существует ([config.py:58](../../tero2/config.py:58)) — никаких новых полей.

### 4.3 `tero2/providers/catalog.py` — dynamic + static

**Константы:**
- `DEFAULT_PROVIDERS: list[str] = ["claude", "codex", "opencode", "kilo", "zai", "gemma"]` — 6 CLI. `gemma` — placeholder (заполнится в spec'е Gemma local provider).
- `STATIC_CATALOG: dict[str, list[ModelEntry]]` — hardcoded списки для провайдеров без live-команды `models`:
  - `claude`: `sonnet / opus / haiku`.
  - `codex`: `""` (medium), `gpt-5.4` (high).
  - `zai` (native): `glm-5.1`.
  - `gemma`: `[]` (placeholder).

**Datatype:** `@dataclass(frozen=True) ModelEntry(id: str, label: str)` — `id` передаётся в CLI (`-m` / `--model`), `label` — UI-friendly.

**Dynamic fetch для `opencode` и `kilo`:**
- `async fetch_cli_models(cli_name, provider_filter=None, free_only=False, refresh=False)` — запускает `{cli_name} models [provider] [--refresh]` через `asyncio.create_subprocess_exec`, парсит stdout построчно.
- Если `free_only=True` — оставляет только строки с `":free"` в id.
- Возвращает `list[ModelEntry]` с `label` = `_humanize(id)` (отрезает префикс `openrouter/`, capitalize).
- Exception (code ≠ 0, `FileNotFoundError` если CLI не установлен) — logged warning + fallback на `STATIC_CATALOG.get(cli, [])`.

**Caching:**
- `~/.tero2/cache/<cli>_models.json` с полями `{"fetched_at": iso8601, "entries": [...]}`.
- TTL 1 час; истёкший → refetch; `F5` в UI → force refetch с `refresh=True`.
- **Cache write failure** (read-only FS, диск заполнен) — logged warning, swallowed; следующий запуск снова пойдёт в fetch. Не блокирует UI.

**Unified API:**
- `async get_models(cli, free_only=False) -> list[ModelEntry]` — роутит в dynamic или static по `cli`.

### 4.4 `ModelPickScreen` — filter-input для 500+ моделей

```
┌─ Выбор модели для Builder (opencode) ─────────────────────┐
│  Поиск: [claude opus 4.7                        ]         │
│  ↑↓ выбор  Enter  /  очистить  F5 обновить  Esc отмена    │
│  [x] только бесплатные                                    │
│                                                           │
│  ▶ openrouter/anthropic/claude-opus-4.7                   │
│    openrouter/anthropic/claude-opus-4.6                   │
│    openrouter/anthropic/claude-opus-4.5                   │
│                                                           │
│  🔄 кеш обновлён: 2 мин назад                              │
│  Всего: 12 (фильтр) / 547 (всего)                         │
└───────────────────────────────────────────────────────────┘
```

Textual `Input` сверху + `watch_value` пересчитывает `ListView`. Checkbox под input'ом для `free_only`. При отсутствии данных (fetch failed, cache пуст) — fallback на STATIC_CATALOG и warning.

### 4.5 ZAI Provider — порт из tero (оба пути в catalog)

**Два пути доступа пользователя к zai:**

1. **Native `ZaiProvider`** — через `claude-agent-sdk` с `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic`. Источник: [tero/src/providers/zai.py](../../../tero/src/providers/zai.py). В UI отображается как CLI `zai` с моделью `glm-5.1`.
2. **Через opencode** — модель `zai/glm-5.1` внутри opencode (уже работает). В UI отображается как CLI `opencode` с моделью `zai/glm-5.1`.

Оба варианта **видны одновременно** в ModelPickScreen. Пользователь выбирает любой. Разные сценарии:
- Native zai: меньше слоёв, полный контроль; требует `ZAI_API_KEY` env или `~/.claude-zai/settings.json`.
- Через opencode: один key в opencode.json, единое логирование opencode, возможность A/B с другими моделями внутри opencode.

**Действия по порту:**
1. Скопировать [tero/src/providers/zai.py](../../../tero/src/providers/zai.py) → `tero2/providers/zai.py`.
2. Правки импортов: `src.config` → `tero2.config`, `src.constants` → `tero2.constants`.
3. Зарегистрировать в маппинге CLI-name → provider class (параллельно `CLIProvider`, т.к. zai — native SDK, не subprocess). **Связано с Open Question 4 ниже** — при имплементации M2 нужен unified `ProviderRegistry`, чтобы эти два пути регистрации не дублировались.
4. Добавить `claude-agent-sdk` в `pyproject.toml` dependencies если отсутствует.
5. Ключ читается из `ZAI_API_KEY` env или `~/.claude-zai/settings.json` — как в tero, копирование секрета не требуется.

**Важно:** zai-native параллелен `CLIProvider`, не наследник. Интерфейс (`async def run(self, **kwargs)`, returning AsyncIterator) тот же, что `BaseProvider`. `ProviderChain` работает без изменений.

### 4.6 Project-level `.sora/config.toml` — без изменений схемы

Wizard пишет те же `[roles.*]` блоки, что уже читает [config.py:145-154](../../tero2/config.py:145). Опциональные поля (`fallback`, `timeout_s`, `context_window`) wizard не трогает — дефолты. Их можно редактировать в SettingsScreen → advanced-режим (Tab раскрывает).

### 4.7 File layout after redesign

**Новые файлы:**
```
tero2/
├── history.py
├── providers/
│   ├── catalog.py
│   └── zai.py
└── tui/
    ├── commands.py
    └── screens/
        ├── startup_wizard.py
        ├── project_pick.py
        ├── plan_pick.py
        ├── providers_pick.py
        ├── model_pick.py
        └── settings.py
```

**Изменённые:**
```
tero2/
├── cli.py                      # project_path → nargs="?"
├── config.py                   # TelegramConfig.enabled
├── providers/
│   ├── cli.py                  # zai env-block пометить как deprecated, не используется
│   └── registry.py             # регистрация ZaiProvider рядом с CLIProvider
└── tui/
    ├── app.py                  # +Header +Footer +actions +COMMANDS
    ├── styles.tcss             # стили для новых screens
    └── screens/
        └── role_swap.py        # step 3 (model), переиспользует model_pick
```

**Удалён:**
```
tero2/tui/widgets/controls.py
```

---

## Section 5 — Testing, risks, milestones

### 5.1 Testing strategy

| Слой | Что | Инструменты |
|------|-----|-------------|
| Unit | `history.py`, `catalog.py` (мок subprocess), `_parse_config` с `TelegramConfig.enabled` + legacy fallback | pytest, tmp_path, `unittest.mock` |
| Screen logic | Wizard state-machine, валидация путей, SORA-invariant | pytest + Textual Pilot |
| Snapshot | ProjectPick / PlanPick / ModelPick / Settings — рендер на fake data | `pytest-textual-snapshot` (SVG) |
| Integration | e2e: `tero2 go` без args → wizard → DashboardApp → 1 step runner'а | pytest + `App.run_test()`, мок `ProviderChain` |
| Smoke (manual) | Все BINDINGS, Command Palette, смена плана на лету | Checklist в PR |

**Новые тестовые файлы:**
- `tests/test_history.py`
- `tests/test_catalog.py`
- `tests/test_startup_wizard.py`
- `tests/test_model_pick.py`
- `tests/test_settings_screen.py`
- `tests/test_zai_provider.py` (адаптация из tero)
- `tests/tui/snapshots/*.svg`

**Не пишем:**
- Live network-тесты (flaky).
- Golden-file для содержимого TOML (порядок ключей не гарантирован).

### 5.2 Risks

| Риск | P | I | Mitigation |
|------|---|---|-----------|
| `opencode models` медленный (3-5с) → UI фризится | H | M | Async subprocess, spinner, TTL-кеш |
| Breaking change в выводе `opencode models` | L | H | Tolerant parser; fallback на static catalog |
| Атомарная запись config.toml ломается на crash | L | H | `Path.replace()` на той же FS; test на partial write |
| Textual version mismatch | M | M | Pin `textual>=1.0,<2.0` в pyproject.toml; smoke-test |
| Существующие RoleSwapScreen тесты ломаются после step 3 | M | L | Negative test фиксирует `step in {1,2,3}` |
| Wizard → Runner ломает `cmd_go` | M | H | Shared helper функция; integration test для обоих путей |
| Путаница `[o]` vs `[r]` | H | L | Явные заголовки: «Глобальные настройки» vs «Сменить провайдера (текущий запуск)» |
| Проект из history удалён/переименован | M | L | `is_dir()` check при загрузке, серое отображение, опция удалить |
| Ключ `ZAI_API_KEY` отсутствует | M | M | `check_ready()` возвращает `(False, "..."")`; UI показывает disabled zai (native) в ModelPickScreen с tooltip; `zai/glm-5.1` через opencode остаётся доступным |

### 5.3 Milestones (для writing-plans)

**M1 — MVP wizard** (закрывает главную боль):
- `cli.py`: `project_path` → `nargs="?"`
- `history.py` + `~/.tero2/history.json`
- `StartupWizard` + `ProjectPickScreen` + `PlanPickScreen` (без шага 3)
- `app.py`: `Header` + нативный `Footer`, удаление `ControlsPanel`
- Добавление BINDINGS `[n]`, `[l]`. В M1 Runner-side handler для `Command("load_plan")` — **стаб**: логирует warning `"load_plan not yet implemented (M2)"` в LogView и игнорирует команду. Хоткей виден в Footer и Command Palette, но не ломает запущенного Runner'а. Полная реализация — в M2.
- Переименование «стир» → «указание», роли → русские
- Tests: history, wizard navigation, 2 snapshot-файла
- **Выход:** `tero2 go` без args запускает wizard → DashboardApp. Runner работает как раньше.

**M2 — Catalog + runtime provider picker:**
- `providers/catalog.py` с dynamic fetch + cache + static fallback
- `ModelPickScreen` с filter-input
- `role_swap.py`: step 2 → step 2+3 (CLI → модель)
- `tui/commands.py` + `COMMANDS` в App (Ctrl+P)
- Runner-side handling `Command("load_plan", ...)` для смены плана на лету
- `providers/zai.py` порт из tero (native path)
- Tests: catalog mock, model pick fuzzy filter, zai provider, snapshots
- **Выход:** можно выбрать и zai (native), и zai/glm-5.1 через opencode из UI. Все модели opencode — live-список.

**M3 — Settings + project-level wizard:**
- `SettingsScreen` 3 вкладки
- `TelegramConfig.enabled` + legacy fallback
- `ProvidersPickScreen` (step 3 wizard'а) + атомарная запись `.sora/config.toml`
- SORA-invariant в wizard
- Tests: settings, atomic write, SORA validation, snapshots
- **Выход:** полный scope — глобальные настройки через UI, новые проекты получают `.sora/config.toml` из wizard'а.

Каждая M → отдельная PR → работающее состояние.

### 5.4 Success metrics (verification)

- `tero2 go` без args открывает wizard, не падает.
- `tero2 go <path>` работает как раньше (regression).
- Выбор из history + Enter запускает Runner ≤ 2 секунды.
- Динамический список opencode-моделей возвращает непустой список, `:free` фильтр работает (сам список зависит от аккаунта и версии opencode, фиксированного числа не требуем).
- Смена плана `[l]` на лету сохраняет checkpoint.
- SettingsScreen `s` → валидный TOML в `~/.tero2/config.toml`.
- Все существующие тесты проходят.
- В ModelPickScreen одновременно видны **и** native `zai/glm-5.1`, **и** opencode-маршрутизированный `zai/glm-5.1`.
- При отсутствии `ZAI_API_KEY` — native zai показан disabled с tooltip; opencode-путь остаётся активным.

---

## Open Questions

1. **Runner-side изменения для `load_plan` Command** — какой API, где сохраняется текущий checkpoint? Ответить в M2-плане при анализе [runner.py](../../tero2/runner.py).
2. **Добавление роли через Settings** (кнопка `[+] Добавить роль` в tab «Провайдеры») — свободная строка или preset (`builder`/`architect`/...)? Предложение для M3: preset из known SORA-ролей + опция «custom».
3. **Локализация `DashboardApp` Header** — `self.title` сейчас смешанный русский/латиница. Унифицировать в M1.
4. **Unified provider registry:** сейчас `CLIProvider` захардкожен под 4 CLI ([providers/cli.py:17](../../tero2/providers/cli.py:17)). Нужен диспатчер `ProviderRegistry` (как в tero), умеющий выбирать между `CLIProvider` и `ZaiProvider` по имени. Разобраться в M2.

---

## References

- Источник live model lists: `opencode models` / `kilo models` (оба проверены, работают локально у пользователя).
- Deprecated hardcoded карта моделей (для справки): [tero/src/menu.py:17-33](../../../tero/src/menu.py:17).
- ZAI native provider source: [tero/src/providers/zai.py:1-183](../../../tero/src/providers/zai.py:1).
- Existing RoleSwap two-step pattern: [tero2/tui/screens/role_swap.py:29-71](../../tero2/tui/screens/role_swap.py:29).
- Current BINDINGS reference: [tero2/tui/app.py:28-39](../../tero2/tui/app.py:28).
- SORA validation invariant: [tero2/config.py:160-166](../../tero2/config.py:160).
- Scout role description (для UI-label «Разведчик»): [tero2/players/scout.py:1-9](../../tero2/players/scout.py:1).
- TelegramInputBot как plan-launcher (не chat): [tero2/telegram_input.py:28-46](../../tero2/telegram_input.py:28).
- Companion future spec: [2026-04-18-gemma-telegram-chat-idea.md](2026-04-18-gemma-telegram-chat-idea.md).

---

**Next step:** spec-document-reviewer loop, затем user review, затем writing-plans для M1.
