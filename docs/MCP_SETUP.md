# Recall MCP Server

Локальний MCP-сервер (`mcp_server.py`) — **read-first міст** до Recall для
Claude Code / Claude Desktop. Окремий процес, реюз сервісів `app.services.*` та
готових HTTP-ендпоінтів — без дублювання логіки. **29 read-only інструментів**
(жодного create/update/delete — свідоме рішення власника, 02.07.2026; write-тулзи
фізично видалені, нові не додаються).

## Архітектура (гібрид) — хто працює без `app.py`, а хто ні

Немає єдиного правила «усі READ незалежні» — це залежить від конкретного тула:
8 читають SQLite напряму (autonomos), 16 інших — HTTP-проксі на запущений
`app.py` (зокрема й флагманський `search_archive`).

**Direct-DB, автономні — працюють БЕЗ запущеного `app.py` (8):**
`get_transcript`, `get_original_file`, `list_recent`, `list_categories`,
`list_entities`, `get_entity`, `list_action_items`, `weekly_digest`,
`list_dropped_commitments`, `list_stale_topics`, `research_export`.
Ці тули відкривають SQLite напряму через `get_db_connection`/`app.services.research`
і не роблять жодного HTTP-виклику.

**HTTP-proxy, потребують запущеного `app.py` (16):**
`search_archive`, `ask_archive`, `suggest_category`, `research_preview`,
`research_summary`, `search_history`, `get_archive_stats`, `list_speakers`,
`get_speaker_stats`, `get_speaker_timeline`, `list_bookmarks`, `list_saved_searches`,
`get_job_status`, `get_active_recording`, `copilot_availability`, `telegram_status`.
Усі викликають `_api()`/`_sse_collect()` → `http://127.0.0.1:5050`. Причина —
свідома: `search_archive`/`ask_archive` не тягнуть e5/torch у stdio-процес (тепла
модель вже живе в пам'яті `app.py`), а `get_job_status`/`get_active_recording`/
`copilot_availability`/`telegram_status` читають стан живих підсистем, якого
просто немає в SQLite.

Якщо `app.py` не піднятий — proxy-інструменти повертають зрозумілу помилку
(`{"error": "Recall (app.py) не запущений…"}`), а не падають; direct-DB тули
працюють як завжди.

> **Тільки читання.** MCP не робить жодних create/update/delete ні напряму, ні
> через проксі — усі зміни стану робить UI Recall.

## Каталог інструментів (27)

- **Ядро архіву (direct-DB, 7):** `get_transcript`, `get_original_file`,
  `list_recent`, `list_categories`, `list_entities`, `get_entity`, `research_export`.
- **Зобовʼязання (direct-DB, 4 — Трек 1):** `weekly_digest(owner)` — понеділковий звід
  одним викликом; `list_action_items(window=this_week|next_week|overdue|soon|no_date|all,
  owner, category_id, status)`; `list_dropped_commitments(days)` — обіцянки, після яких
  тема більше не спливала; `list_stale_topics(days)` — напрями, що зникли з розмов.
  Задачі віддають і сиру фразу терміну (`due_raw`), і обчислену дату (`due_date` +
  `due_precision`) — див. `app/services/commitments.py`.
- **AI на вимогу (proxy, 4):** `ask_archive` (RAG-відповідь з цитатами),
  `suggest_category` (k-NN підказка), `research_preview`, `research_summary`.
- **Багатші читання, як в UI (proxy, 7):** `search_archive` (гібрид e5+FTS5),
  `search_history`, `get_archive_stats`, `list_speakers`, `get_speaker_stats`,
  `get_speaker_timeline`, `list_bookmarks`, `list_saved_searches`.
- **Live / фонові статуси (proxy, 4):** `get_job_status`, `get_active_recording`,
  `copilot_availability`, `telegram_status`.

**Resources:** `recall://about` (огляд + жива статистика + карта тулзів +
список автономні/proxy), `recall://transcript/{id}`, `recall://entity/{id}`.

**Довгі операції:** MCP — запит/відповідь без стріму. Опитуй стан фонової задачі
через `get_job_status(job_id)` (job_id видає UI Recall — стартового тула немає в
MCP). SSE-звіти (`research_summary`) дочитуються на боці проксі й вертаються
готовими.

## Транспорти й авторизація

| Режим | Команда | Авторизація | Кому |
|-------|---------|-------------|------|
| **stdio** (default) | `mcp_server.py` | не треба (клієнт сам спавнить процес) | Claude Code, Claude Desktop |
| **http** | `mcp_server.py --transport http` (:5060) | `Authorization: Bearer <RECALL_MCP_KEY>` | персистентний сервер; майбутній web |

У http-режимі без `RECALL_MCP_KEY` сервер **не стартує** (захист від випадкової
публічності). Слухає лише `127.0.0.1`.

## Підключення

### Claude Code — stdio, глобально на всі проєкти (рекомендовано)
```bash
claude mcp add recall -s user -- "E:\Projects\Recall\.venv\Scripts\python.exe" "E:\Projects\Recall\mcp_server.py"
```
`-s user` → доступно у Claude Code з будь-якого проєкту на цьому ПК (конфіг у
`C:\Users\<you>\.claude.json`). Транспорт за замовчуванням stdio — `--transport`
**не передавай** (трейлінг-аргументи після `--` Claude CLI відкидає; саме тому stdio
зроблено дефолтним у самому сервері). Після `add` **перезапусти Claude Code**, щоб
зʼявилась група `recall`. Перевірка: `claude mcp list` → `recall … ✔ Connected`.

### Claude Desktop — stdio
У `claude_desktop_config.json` (Settings → Developer → Edit Config):
```json
{
  "mcpServers": {
    "recall": {
      "command": "E:\\Projects\\Recall\\.venv\\Scripts\\python.exe",
      "args": ["E:\\Projects\\Recall\\mcp_server.py"]
    }
  }
}
```
Перезапустити Desktop.

### http (опційно, персистентний сервер)
```bash
# .env: RECALL_MCP_KEY=<secrets.token_urlsafe(32)>
.venv\Scripts\python.exe mcp_server.py --transport http
claude mcp add --transport http recall http://127.0.0.1:5060/mcp --header "Authorization: Bearer <RECALL_MCP_KEY>"
```

## Змінні середовища

| Env | Default | Призначення |
|-----|---------|-------------|
| `RECALL_API_URL` | `http://127.0.0.1:5050` | адреса запущеного `app.py` для проксі-тулзів |
| `RECALL_MCP_READONLY` | (вимк.) | захист-про-запас: усі write-тулзи вже фізично видалені з коду, тож зараз цей флаг ні на що не впливає — можна лишити для майбутнього |
| `RECALL_MCP_KEY` | — | Bearer-ключ, **лише** для http-режиму |
| `RECALL_MCP_DEBUG_LOG` | `1` (увімкнено з 24.07.2026) | лог кожного виклику тулзи у `logs/mcp_calls.log`: імʼя, аргументи, мс, ok/exc. До Треку 1 був вимкнений — 488 викликів пішли в нікуди, і будь-який тюнінг видачі був наосліп |

`.env` вантажиться з каталогу самого скрипта (а не cwd) — тому user-scope конектор
коректно працює при запуску з будь-якого проєкту.

## Безпека
- Слухає лише localhost; http без валідного `Bearer` → 401.
- Читання БД через `get_db_connection` (WAL, паралельно з Flask).
- Читання оригіналів файлів (`get_original_file`) обмежено білим списком папок
  (`documents/`, `uploads/`, `youtube_downloads/`, `telegram_media/`) — anti-traversal.
- Жодних записів через MCP немає — весь інструментарій read-only.
- НЕ комітити `.env` з ключем (він у `.gitignore`).

## Чому НЕ автозапуск з app.py
Stdio-сервер спавнить **сам клієнт** (Claude Code/Desktop) на вимогу і вбиває по
завершенні — тримати окремий процес із `app.py` не треба (і це створило б другий зайвий
сервер). Автозапуск доречний лише для http-режиму (майбутній web-доступ через
тунель+OAuth) — тоді за зразком `telegram_listener`.

## Перформанс / нюанси
- `mcp_server.py` сам НЕ імпортує torch/e5 — важка ML-ініціалізація живе лише в
  `app.py`; `search_archive`/`ask_archive` проксіюють запит на вже теплу модель
  там, а не вантажать її в stdio-процес.
- Proxy-тулзи (16 з 27, див. таблицю вище) потребують запущеного `app.py`
  (найзручніше — іконкою **Recall** на робочому столі: термінал з логами + сервер
  на :5050). Direct-DB тули (11) працюють і без нього.

## Roadmap
- **Web / claude.ai:** OAuth 2.1+PKCE (FastMCP `OAuthProxy`) + Cloudflare Tunnel для
  публічного HTTPS-хоста + автозапуск http-сервера з `app.py`.
