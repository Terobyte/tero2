# Telegram Agent — архитектура Concierge

> Агент живёт в Telegram как демон. Принимает текст, голосовые, звонки. Выполняет задачи, отвечает, уведомляет проактивно.

---

## Архитектура: Concierge + Specialists

```
┌─────────────────────────────────────────────┐
│  CONCIERGE (всегда запущен)                 │
│  Telegram Bot API (long polling / webhook)  │
│  - текст, голосовые, файлы, фото, звонки    │
│  - STT: Deepgram Nova-3 / Whisper           │
│  - TTS: Fish Audio (JLM4.7)                 │
│  - РАЗГОВАРИВАЕТ с тобой, не диспетчерит    │
│  - проактивные уведомления в чат            │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  SPECIALISTS (просыпаются по запросу)       │
│  Architect, Designer, Builder (OpenCode),   │
│  Debugger, Reviewer (Codex)                 │
│  Каждый = CLI-агент со своим промптом       │
│  и worktree. Роли конфигурируемы.           │
└─────────────────────────────────────────────┘
```

**Concierge** — собеседник. Слушает, уточняет, формулирует. Сам НЕ кодит. Когда мысль оформилась — передаёт специалисту.
**Specialists** — делают работу. Каждый с чистым контекстом (Iron Rule).

---

## Поток: разговор → решение → специалист

```
Ты: [звонишь / пишешь / голосовое]
  → Concierge: слушает, уточняет
  → "задача для дизайнера, соединяю"
  → Designer: [worktree → результат → Concierge → тебе]
```

**Ты управляешь**. Concierge предлагает, ты подтверждаешь.

---

## Каналы

**Ввод**: текст, голосовое (.ogg → STT), звонок (real-time), фото/скрин (Claude Vision), файл
**Вывод**: текст (markdown), голосовое (Fish Audio TTS), файлы (диффы, логи)

---

## Проактивные уведомления

Агент сам пишет: тесты прошли, CI упал, деплой готов, worker застрял, задача завершена.
**Триггеры**: задача done/fail, CI/CD событие, watchdog проблема, нужен human input.

---

## Два режима голоса

### Режим 1: Асинхронные голосовые (MVP4)

```
Голосовое → Bot API → STT → LLM → TTS → sendVoice
```
"Рация" — нет barge-in. Fish Audio TTS уже настроен.

### Режим 2: Real-time звонок (MVP6)

```
Звонок → MTProto (Pyrogram) → pytgcalls → WebSocket → Retell → TTS обратно
```
UserBot архитектура. **Barge-in**: сброс TTS буфера + обновить LLM историю + "слушаю" (< 100ms).

---

## Daemon — macOS LaunchAgent

```xml
<!-- ~/Library/LaunchAgents/com.tero.agent.plist -->
<dict>
    <key>Label</key>       <string>com.tero.agent</string>
    <key>RunAtLoad</key>   <true/>
    <key>KeepAlive</key>   <true/>
</dict>
```

`KeepAlive: true` — автоматический перезапуск после sleep/reboot. Reconnect loop внутри агента для сетевых разрывов.

---

## Безопасность

- **Только твой chat_id** — бот игнорирует все остальные
- **Whitelist команд** — push, deploy, delete требуют подтверждения
- **Audit log** — `.tero/telegram/history.jsonl`
- **Worker timeout** + **no secrets in chat**

---

## Связь с проектом

- **Async голос**: `detailed-roadmap.md` → MVP4 (Telegram Concierge)
- **Real-time звонки**: `detailed-roadmap.md` → MVP6 (Voice Calls)
- **Промпт Concierge**: `system-prompts.md` → Concierge prompt

| Паттерн | Как используется |
|---|---|
| **Heartbeat** | Watchdog: cron → check → notify в Telegram |
| **Disk as Truth** | Состояние задачи на диске, crash-safe |
| **Provider Chain** | Worker = OpenCode/Z.AI / Codex с fallback |
| **Human Steering** | Telegram = STEER.md в реальном времени |
| **Iron Rule** | Каждая задача Worker = свежий контекст |
| **Франкенштейн** | Сложные баг-задачи → полный автофикс цикл |
