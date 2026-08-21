# 🗺️ memory/map — карта коду Recall

> Запозичено з [VULYK](https://github.com/Black-coffe/vulyk) (layer 4 — `memory/map/`),
> адаптовано під наш репозиторій. Це **підказка для навігації**, а не істина:
> завжди звіряй із реальним кодом перед правкою (наприклад, перед великою зміною
> в `app.py`). Якщо карта розійшлась із кодом — онови карту (делегуй дешевому агенту:
> `Agent(model: haiku)` або `Agent(effort: low)`).

**Навіщо:** `app.py` (817 рядків), `whisper_manager_new.py` (867), `transcription.py`
blueprint (~1600), `recall.css` (1397) — не вміщуються в один погляд. Карта дає
точку входу: куди йти за фічею, не перечитуючи піврепо.

## Файли карти

| Файл | Що описує |
|---|---|
| [overview.md](overview.md) | Архітектура, 3 процеси (Flask + telegram_listener + mcp_server), boot/shutdown, SSE/логи/rate-limit/безпека |
| [root.md](root.md) | Кореневі entry points: `app.py`, `config.py`, `whisper_manager_new.py`, `telegram_*`, `mcp_server.py`, `app/state.py` |
| [blueprints.md](blueprints.md) | HTTP-шар: 14 blueprints, ~135 endpoints, де що крутиться |
| [services.md](services.md) | `app/services/*` — транскрипція, RAG (embeddings/retrieval/rag), enrichment, документи, dba, інфра |
| [copilot.md](copilot.md) | `app/services/copilot/*` — живий ко-пілот, local-first cascade (Qwen→Claude) |
| [recording.md](recording.md) | `app/services/recording/*` — WASAPI-запис, FSM, finalize, recovery |
| [frontend.md](frontend.md) | SPA `static/js/recall/*` — роутер, views, SSE-через-fetch, service worker |

## Конвенції карти

Кожен модуль описано як:
- **Призначення** — 1-2 речення.
- **Entry points** — публічні функції/класи/маршрути, які викликають ззовні.
- **Типи/стан** — важливі глобали, dataclass-и, singleton-и.
- **Gotchas** — Windows-специфіка, порядок ініціалізації, GPU-локи, бюджет, неочевидне.

Стиль роботи з картою — у `CLAUDE.md`, секція «🐝 Routing-матриця моделей».

*Згенеровано 2026-06-23 з реального коду (4 паралельні Explore-агенти). При
суттєвих змінах архітектури — перегенеруй відповідний файл.*
