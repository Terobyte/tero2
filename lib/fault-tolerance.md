# Отказоустойчивость и защита от тупиков — применение к tero2

> Только то, чего нет в GSD-2/SORA и что критично для tero2: loop detection, 3 уровня эскалации, circuit breaker, reflexion, context fallback, soft landing.

## Проблема

SORA/GSD-2 уже решают: disk-as-truth, crash recovery, retry на 429, Verifier блокировка.

**Что не решено:** семантические петли не детектируются; при тупике нет обхода/эскалации; падение провайдера обрушивает весь run.

---

## 1. Loop Detection

### Структурный счётчик — первый рубеж

```
steps_in_current_task: int   # инкрементируется каждым узлом
retry_count: int             # инкрементируется при каждом retry
last_tool_call: str          # hash последнего вызова инструмента
```

Правила выхода (детерминированные, без LLM):
- `retry_count >= 3` → soft escalation
- `steps_in_current_task >= 15` → принудительный fallback
- `last_tool_call == prev_tool_call` дважды подряд → immediate deadlock

Счётчики **не доступны LLM** — только Dispatcher читает/пишет.

### Семантическая петля — второй рубеж

```
1. На каждом шаге Builder'а → векторизовать мысль (text-embedding)
2. Cosine similarity с последними 3 шагами
3. similarity > 0.90 → семантическая петля обнаружена
```

Порог 0.90 — консервативный старт, калибровать до 0.85 по метрикам. Семантическая петля → сначала Уровень 1 (diversification), не сразу backtrack.

---

## 2. Три уровня реакции на тупик

```
Тупик обнаружен
    └── Уровень 1: Diversification (авто)
        ├── Температура: min(current + 0.3, 1.0)
        ├── Inject: "Предыдущий путь — тупик. Выбери другой метод."
        └── Не помогло за 2 шага → Уровень 2
    └── Уровень 2: Backtrack + Replan (авто)
        ├── Откат к последнему checkpoint
        ├── EVENT_JOURNAL.md ← причина тупика
        ├── Передать Coach
        └── Coach не разрешил → Уровень 3
    └── Уровень 3: Эскалация к человеку
        ├── .sora/human/STUCK_REPORT.md
        ├── Telegram-уведомление
        ├── ПРИОСТАНОВИТЬ (не завершать!)
        └── Ждать STEER.md или OVERRIDE.md
```

### STUCK_REPORT.md — формат

```markdown
# Stuck Report — {timestamp}
**Задача:** {task_id} — {task_name}
**Тупик:** {semantic_loop | retry_exhausted | tool_repeat}
**Шагов:** {steps_in_current_task}
**Последние действия:** {action_1}, {action_2}, {action_3}
**Что пробовали:** Diversification, Backtrack к {checkpoint_id}
**Нужно:** HUMAN_CONTEXT.md / переформулировать / OVERRIDE.md / STEER.md
```

### Telegram-уведомление при тупике

```python
# BOT_TOKEN и CHAT_ID из .sora/config.toml [telegram]
import toml, requests
from library.tts_fish_audio import tts_fish_audio_simple

config = toml.load(".sora/config.toml")
tg = config["telegram"]
message = f"tero2 застрял\nЗадача: {task_name}\nПричина: {deadlock_reason}"
audio = tts_fish_audio_simple(message)
with open(audio, 'rb') as f:
    requests.post(f"https://api.telegram.org/bot{tg['bot_token']}/sendVoice",
        data={"chat_id": tg["chat_id"]}, files={"voice": f})
```

---

## 3. Circuit Breaker для провайдеров

| Состояние | Поведение | Переход |
|-----------|-----------|---------|
| **CLOSED** | Нормальная работа | → OPEN при 3 подряд ошибках |
| **OPEN** | Fast-fail 0ms, немедленный fallback | → HALF-OPEN через 60 сек |
| **HALF-OPEN** | 1 probe (max_tokens=1) | → CLOSED (ok) / OPEN (fail) |

```python
class CircuitBreaker:
    def call(self, provider, request):
        if self.state == OPEN:
            raise FastFailError()
        try:
            result = provider.call(request)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            if self.failures >= 3: self.open()
            raise
```

### Retry с Jitter

```python
def retry_with_backoff(fn, max_attempts=3):
    for attempt in range(max_attempts):
        try: return fn()
        except RateLimitError:
            base = 2 ** attempt
            time.sleep(base + random.uniform(0, base * 0.3))
    raise MaxRetriesExceeded()
```

---

## 4. Context Fallback

```
ContextWindowExceededError
    → Шаг 1: убрать старые SUMMARY, оставить PLAN + 2 последних
    → Шаг 2: переключить на модель с лучшим качеством при плотном контексте
    → Шаг 3: эскалация (Уровень 3)
```

Проверка **до** запуска задачи:
```
if estimated_tokens > 0.95 * model_limit:
    compress_oldest_summaries()
    if still_too_large: switch_to_higher_quality_model()
```

---

## 5. Soft Landing вместо hard crash

```python
try:
    run_agent_step()
except DeadlockDetected:
    write_stuck_report(); notify_telegram(); pause_and_wait()
except ContextWindowExceeded:
    compress_context(); retry_with_smaller_context()
except ProviderUnavailable:
    circuit_breaker.open(); switch_to_next_provider()
except Exception as e:
    save_checkpoint(current_state); write_event_journal(error=e); trigger_coach()
```

---

## 6. Reflexion для Builder

```
Builder получает FAIL от Verifier
    └── Inject в следующий контекст:
        "Провалилось потому что: {verifier_feedback}
         Не сработало: {failed_tests}
         Избегай: {pattern}"
```

**Max reflexion cycles**: 2 (после → Coach, не бесконечный retry).

---

## Ключевые числа

| Параметр | Значение |
|----------|----------|
| retry_count лимит | 3 |
| steps_in_current_task лимит | 15 (~2x нормы) |
| Cosine similarity порог | 0.90 (старт → 0.85) |
| Circuit Breaker порог | 3 ошибки подряд |
| Backoff | 2^attempt + jitter |
| Reflexion cycles max | 2 |
| Температура diversification | min(current + 0.3, 1.0) |

> Эволюция Fault Tolerance (health metrics, калибровка) — см. `detailed-roadmap.md` §3.1.
