# frontend — SPA (`static/js/recall/`)

Клієнтський роутер + shell, vanilla JS (без фреймворку), глобальний `window.Recall`.
**Дизайн:** editorial-каталог, тепле паперове полотно, тільки світла тема; класи з префіксом `rc-*`.

## Архітектура
- **namespace.js** (20) — `window.Recall` реєстр: `.routes/.views/.api/.util/.ui/.shell/.cmdk/.router` +
  `.state` (categories, stats). Чисті глобали.
- **router.js** (151) — History API роутер. Патерни `/transcript/:slug` → regex. Делеговане
  перехоплення `<a href="/...">`. `_seq`-лічильник проти out-of-order рендерів (`ctx.isCurrent()`). Deep-link нативно.
- **shell.js** (93) — активна навігація, бургер, ⌘K-тригер, банер оновлення моделі, live-лічильники (library/entities/tasks).
- **api.js** (230) — JSON-обгортки (get/post/patch/put/del), multipart-аплоад через XHR з progress.
  **SSE через `fetch`+`ReadableStream.getReader()`, НЕ EventSource** (`postSseStream()`/`sseStream()`,
  буфер по `\n\n`, парс `event:`/`data:`, abort через AbortController).
- **ui.js** (198) — toast, skeleton, empty/error, source-badge, категорії (модалка + глобальний
  capture-перехоплювач `.rc-catsel` на сентинелі `UI.CAT_NEW`), кеш `Recall.state.categories` (`bustCategories()`).
- **util.js** (110) — escape(XSS), кирилиця→ASCII слаг, parseId, формат дат/тривалості/байтів, `el()`, debounce.
- **cmdk.js** (119) — ⌘K палітра: nav-роути + debounce-пошук транскриптів (220ms) + «Запитати архів».
- **research_export.js** (84) — спільний SSE-драйвер для /research та картки сутності (originals 0-ток + AI-summary).

## Views (`views/*.js`) — кожен `{render(ctx), destroy()}`
| Route | View | Екран |
|---|---|---|
| `/` | home | Дашборд: плитки stats, ask-box, останні записи |
| `/library` | library | Історія: пошук + фасети (джерело/категорія/період), пагінація, bulk-категорія/видалення. **Рядок рендериться ПО ТИПУ джерела** (`rowParts()`): TG — чат+відправник у підписі й повідомлення в тілі; дзвінок — тривалість/спікери/задачі; документ — файл/сторінки |
| `/audio` | audio | Аудіотека (YouTube+записи): play/explore/transcribe/delete, бейдж активної транскрипції, фільтр **«Без транскрипта»** (`?transcribed=0`) |
| `/transcript/:slug` | transcript | Запис: 3 view (plain/segments/polished), плеєр+seek по сегменту, закладки, категорія, **таймлайн ко-пілота**, word cloud |
| `/ask` | ask | RAG-чат: `api.askStream()` (fetch+SSE), цитати-посилання |
| `/research` | research | Дослідження бренду: preview → originals(.md) / AI-summary |
| `/entities`, `/entities/:id` | entities | Граф: значущі сутності (≥2 згадки) + картка (co-mentions, timeline, export) |
| `/tasks` | tasks | Звід зобовʼязань: **відра терміновості** (протерміновано / найближчі 7 днів / пізніше / без дати — по запиту на відро, лічильники з `windows`) + фасет власника + статус (вкл. `stale`) + період + категорія |
| `/speakers` | speakers | Спікери: stats, rename/delete, bulk-merge, timeline |
| `/youtube` | youtube | URL → preview → download+transcribe (polling, без live-сегментів) → /transcript |
| `/upload` | upload | Drag-drop файл → XHR-progress → /transcript |
| `/documents` | documents | PDF/DOCX/...→ multipart → дедуп по хешу → /transcript |
| `/telegram` | telegram | Панель слухача: dialogs, toggle-моніторинг, категорія, backfill |
| `/settings` | settings | System info, каталог моделей, **ко-пілот config**, категорії CRUD/merge, спікери |
| `/record` | record | Рекордер: SSE level+сегменти (fetch), setup→active→stop-name→transcribe, мега-панель ко-пілота |

## Service worker — `static/sw.js` (202)
**Поточна версія кешу: `recall-v58`.** Стратегії: shell precache; `/static/` stale-while-revalidate;
CDN cache-first; API-GET network-first+fallback; SSE/POST не кешуються. На `activate` старі `recall-vN` чистяться.

## Gotchas
1. **SSE через fetch, не EventSource** — EventSource труїть werkzeug keep-alive (`recall_sse_connection_gotcha`).
2. **Бамп `VERSION` у sw.js** при будь-якій зміні JS/CSS/HTML, інакше старий код із кешу.
3. **CSS-колізії** — грепни `\.rc-<name>\b` перед новим класом (короткі імена перевикористані; `recall_css_class_collision`).
4. **Глобальний catsel-перехоплювач** — `.rc-catsel` на `UI.CAT_NEW` ловиться на capture-фазі; view не бачить.
5. **Render-seq guard** — асинх-рендер після навігації відкидається, якщо `!ctx.isCurrent()`.
6. **URL-driven state** — library/audio/entities/tasks/research тримають фільтри в query-params (deep-link).
7. **`[hidden]` і власний `display`** — глобальне `.rc-app [hidden]{display:none!important}` тепер є в
   `recall.css`. До нього кожен компонент із власним `display:flex` перебивав браузерний дефолт і
   лишався видимим під `hidden` (порожня `.rc-bulkbar` висіла внизу Бібліотеки завжди).
8. **Мета-рядки збирати `.filter(Boolean).join(sep)`**, НЕ хардкодити `<span class="sep">·</span>`
   між полями: у телеграма немає ні мови, ні моделі, і в рядку лишались висячі «· ·».

**CSS:** `static/css/recall.css` (1397) — токени (`--rc-canvas/ink/accent/src-*`), Fraunces/Inter/IBM Plex Mono, скоуп `.rc-app`.
**Порядок завантаження:** namespace → util → api → ui → research_export → views → shell → cmdk → router.
