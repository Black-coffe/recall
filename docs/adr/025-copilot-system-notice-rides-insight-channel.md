# ADR-025: Copilot system-level notices travel through the insight/log_event channel, never through `copilot_status`

- Status: proposed
- Date: 2026-09-22
- Spec: docs/specs/directions-projects-tags

## Context

Хвиля 7 цієї спеки полагодила `copilot/worker._scope_entity_ids`, щоб неоднозначний скоуп
дзвінка (тезка напрямку) не ковтався мовчки як «не знайдено». Це — розширення спеки поза всіма
попередніми `## Files` (сам файл не був у жодній історії 01–21), і рішення записане явно:

> **Рішення 4 — копілот полагодити (мажор 3 р0 / мінор 11 р1), не приймати ризиком:**
> `copilot/worker.py` не був у `## Files` жодної історії 01–21 — це **розширення спеки**,
> позначене явно (C10, Assumptions)… Якщо власник не хоче правки копілота в цій спеці, 25
> знімається одним словом і ризик іде в `## Descoped` як прийнятий — але тоді «ніколи мовчки» на
> живому дзвінку не виконується.

Перша редакція (історія 25) показувала попередження через `copilot_status`. Ревʼю раунду 6
знайшло, що цей канал не доходить до оператора (стан копілота перемальовується наступним тіком і
попередження зникає, не лишаючи сліду в історії сесії). Хвиля 8 замінила канал:

> **Рішення 3 — копілот через наявний шлях інсайту (27).** Картка + `log_event` замість
> `copilot_status`: і рендериться віджетом, і лежить у потоці сесії (MCP `get_live_copilot`
> бачить — памʼятка `live-mcp-agent-gets-raw-data`), і не перемальовує стан. `record.js` — у
> `## Files` (у 25 його не було, звідси критичне). Відкинуто: банер через `applyCopilotStatus`
> (ховається першим же `setCpStatus`, не потрапляє в події).

Контракт C10 (жовтень плану) підтверджує форму: подія `log_event` + `copilot_insight` зі своїм
`kind`, картка рендериться `record.js`, лежить у потоці сесії (`get_state`/`get_live_copilot`/
таймлайн), поза `_card_gate` і `cards_shown`; `copilot_status` зі `state:"quiet"` для цього
випадку не публікується.

## Options

1. **Обрано:** системне попередження — подія `log_event` + картка `copilot_insight` зі своїм
   `kind`, той самий шлях рендерингу, що інсайти (`record.js`), явно поза бюджетом карток
   (`_card_gate`, `cards_shown`) і поза лічильником інсайтів.
2. Відкинуто: банер через `copilot_status`/`applyCopilotStatus` — стан перемальовується першим же
   наступним тіком `setCpStatus` і ніколи не потрапляє в постійні події сесії (не бачить
   `get_live_copilot`, не лишається в таймлайні/експорті).
3. Відкинуто (розглянуто як альтернатива хвилі 7): прийняти діру як ризик і не чинити копілот у
   цій спеці — суперечить прямій вимозі власника «ніколи мовчки».

## Decision

Будь-яке **системне** повідомлення копілота оператору (не аналітичний інсайт від моделі) —
неоднозначний скоуп, майбутні класи попереджень такого роду — іде через ту саму подієву модель,
що інсайти: `log_event` + запис у `copilot_insight` зі своїм `kind`, рендериться наявним шляхом
картки (`record.js`), і тому лишається в `get_state`/`get_live_copilot`/таймлайні/експорті.
Явно виключається з бюджету карток (`_card_gate`, `cards_shown` це не рахує) і з лічильника
показаних інсайтів. Канал `copilot_status` для системних повідомлень не використовується — він
живе лише одним тіком і не персистить.

## Consequences

Легше: системні попередження видимі оператору і агенту (MCP) без нової таблиці/міграції —
переюзано наявну модель подій `copilot_sessions/topics/events` (v19). Важче: не записано — делта
не називає, чого це коштує сьогоднішньому коду поза `worker.py`/`record.js`.

## Invariants created

- A copilot notice that is not a model-generated insight (a system-level warning, e.g. an
  ambiguous call scope) is delivered as a `log_event` + `copilot_insight` entry with its own
  `kind`, rendered through the existing insight card path, and is exempt from the card budget
  (`_card_gate`, `cards_shown`) and the insight counter.
- `copilot_status` is never the channel for a notice that must persist — it is overwritten by the
  next status tick and is invisible to `get_live_copilot`/session history/export.

## Revisit when

Не записано — делта не називає конкретний тригер для перегляду.
