# frontend — SPA (`static/js/recall/`)

Client-side router + shell, vanilla JS (no framework), global `window.Recall`.
last-verified: 2026-10-20 (retranscribe-versions 4.25.0: api.js, compare.js, library/audio/transcript views, sw.js v89 re-read); 2026-10-16 (english-i18n 4.16.0, english-code 4.17.0, gmail-ingest 4.18.0, sw.js v81; 2026-10-18 mcp-mail-connect 4.19.0, sw.js v82; 2026-10-18 mcp-foundations-a 4.20.0, api.js UI token; 2026-10-19 mcp-foundations-b 4.22.0, UI.gated, sw.js v84; 2026-10-19 recording-autosave 4.23.0, record.js/audio.js, sw.js v85; 4.23.1-4.23.2 save-screen defaults, sw.js v87)
**Design:** editorial catalog, a warm paper canvas, light theme only; classes with the `rc-*` prefix.

## Architecture
- **namespace.js** — the `window.Recall` registry: `.routes/.views/.api/.util/.ui/.shell/.cmdk/.router` + `.state`.
- **i18n.js** (98) — `R.t(key, vars)` + `R.i18n = {lang, setting, add, t, num, date, compare, headers, apply}`.
  Language: `data-ui-lang` on `<html>` = uk|en → that; `auto` → `navigator.languages[0]` on `uk` → uk, otherwise en (:10-18);
  sets `<html lang>`. `t`: the language key → en fallback → the key itself + `console.warn` (:53-62). Plural — a value object,
  the form by `Intl.PluralRules` from `vars.count`. `apply(root)` (:80) translates `data-i18n`/`-title`/`-placeholder`/`-aria-label`
  (called from `shell.js:18`). Dictionaries — `i18n/<area>.js` (core, capture, transcript, settings, library, knowledge,
  telegram, mail), each a single `R.i18n.add('<area>', {uk, en})`; keys carry the area prefix.
- **router.js** — a History API router, `/transcript/:slug` → regex; delegated interception of `<a href="/...">`;
  `_seq` against out-of-order (`ctx.isCurrent()`).
- **shell.js** — navigation, burger, ⌘K, model banner, live counters (`R.i18n.num`). K2 `startCallPrompts()` polls
  `GET /api/recording/autoprompt` every 5 s (visible tab), the `.rc-toast--call` plate in `#rcToasts` (buttons = navigation
  to a one-time URL; "Not needed"/× on "Record it?" → `POST …/dismiss`; × on "Stop recording?" only hides).
- **api.js** (~230) — JSON wrappers, multipart via XHR, `A.updateRecord`→`PATCH /api/history/<id>`,
  `A.updateAudio`→`PATCH /api/audio/downloads/<id>`. **SSE via `fetch`+`ReadableStream`, NOT EventSource**
  (`postSseStream()`/`sseStream()`, buffer split on `\n\n`, abort). Adds `X-Recall-Lang` and (4.20.0) the per-boot `X-Recall-UI`
  token through `UH()` (:9; reads `<meta name="recall-ui-token">` from `shell.html` :6, empty when the shell was not trusted) on EVERY request
  it sends (wrappers :43, multipart :119, XHR :175, SSE :413/:459); exported as `A.uiHeaders` (:127) for raw `fetch` sites
  (`views/transcript.js:1194` export POST). Nothing server-side enforces the token yet; a page loaded before an app.py restart
  carries a stale one (HTML is network-first in the service worker, `sw.js:216`). The server now reads it (approval gate: `ui` vs `unknown`).
- **api.js** 4.25.0 (:158-162): `A.retranscribe(tid, {model,...})`, `A.versions(tid)`, `A.version(tid, vid)`, `A.activateVersion(tid, vid)`.
- **compare.js** (4.25.0, pure, no DOM; `window.Recall.compare` and `module.exports` for node tests; loaded before `views/transcript.js`, `shell.html:135`) —
  `align(segsA, segsB)` clusters segments by chained time overlap into rows `{a, b}`; `diffWords(x, y)` word-level LCS diff.
- **ui.js** — toast, skeleton, empty/error, source-badge, categories (modal + the capture interceptor `.rc-catsel` on
  `UI.CAT_NEW`, `bustCategories()`). `UI.editMetaModal({title, description, titleRequired}) -> {title, description}|null`.
  Directions/projects (4.6.0): `UI.catOptions`, `UI.catPath/catLabel(id) -> {text,title}` (take a direction label from here,
  not `c.name`), `UI.catChip`. Tags: `UI.loadTags/bustTags`, `UI.tagChip`, `UI.tagOptions` (`.rc-tagpick`: a hidden
  `select.rc-tagsel` = the source of truth + chips), `UI.syncTagPicker(selectEl)`; `UI.hint(html)` = `<details class="rc-hint">`.
- **`UI.gated(btn, run, opts)`** (ui.js:465, 4.22.0) — wraps a call to a destructive endpoint: disables the button with
  `core.gate.waiting`, awaits `run()`, and turns `APPROVAL_DENIED` (reason `timeout` vs `no`), `RATE_CEILING` (`retry_after_sec`),
  `APPROVAL_UNAVAILABLE`/`APPROVAL_PENDING` (`GATE_KEYS` :461) into a toast; any other error is rethrown; restores the button; returns
  `undefined` after a toast (callers check `if (!r) return`). Call sites (count): audio 1, entities 1, library 3, mail 1,
  settings 6, speakers 2, telegram 1. A button for a `_D_ROUTES` endpoint needs it, or the denial surfaces as a raw error. Strings `core.gate.*` in `i18n/core.js`.
- **util.js** — escape, Cyrillic→ASCII slug, parseId, dates/duration/bytes (via `R.i18n`), `el()`, debounce.
- **cmdk.js** — the ⌘K palette: routes + debounced search (220 ms) + "Ask the archive".
- **research_export.js** — the shared SSE driver of /research and the entity card. **comments.js** — the shared comments UI.

## Views (`views/*.js`) — each `{render(ctx), destroy()}`
| Route | View | Screen |
|---|---|---|
| `/` | home | Dashboard: stats tiles, ask-box, recent records |
| `/library` | library | History: search+facets, pagination, bulk. A row BY source type (`rowParts()`: TG/call/document); a duplicate → a `duplicate_of` badge. 4.25.0 re-transcribe: row button `data-act="retranscribe"` only when `t.has_media === true` (:508); `doRetranscribe` (:657) picks among `downloaded` models (`R.api.models()`), `R.api.retranscribe`, polls `R.api.pollJob`, spinner via `rtRunning`; errors map via `RT_ERR` on `error_code` |
| `/audio` | audio | Audio library: play/explore/transcribe/delete, 4.25.0 re-transcribe button on a transcribed item (`d.transcription_id`, `runRetranscribe` :544; `audio_library.py` unchanged, no new list field), the "No transcript" filter (`?transcribed=0`); 4.23.0 banner of recordings without a direction from `GET /api/recordings/uncategorized` (:143) with one-click k-NN candidates |
| `/transcript/:slug` | transcript | Record: view plain/segments/polished, player+seek, bookmarks, category, copilot timeline, pencil → `UI.editMetaModal` (renaming re-slugs the URL). 4.25.0 versions bar (`transcript.js:670-840`; `R.api.versions` best-effort, `.rc-versions*`): rows model/date/original/main tags, view a version read-only (banner), make main (`activateVersion` :781), two-column compare (`openCompare` :793, `paintCompare` :810, `R.compare.align/diffWords`); shows a spinner while `running_job_id`. `?task=<id>` → `focusTask(rel,id)` (`.is-target`, 4 s); a foreign id — nothing |
| `/ask` | ask | RAG chat: `api.askStream()`, citations, 👍/👎+note (`R.api.rateAsk` → `POST /api/memory/ask/<id>/rate`) |
| `/research` | research | Brand research: preview → originals(.md) / AI-summary |
| `/entities`, `/entities/:id` | entities | Graph: entities (≥2 mentions) + card |
| `/tasks` | tasks | Urgency buckets (one request per bucket) + owner + status (incl. `stale`) + period + category |
| `/comments` | comments | Comments list |
| `/speakers` | speakers | Speakers: stats, rename/delete, bulk-merge, timeline |
| `/youtube`, `/upload`, `/documents` | youtube, upload, documents | URL/file → progress → /transcript (documents: dedup by hash) |
| `/telegram` | telegram | Listener panel: dialogs, monitoring, category, backfill; row `.rc-tgrow*`, search `#rcTgSearch` (`searchIndex`, `phoneHit`) |
| `/mail` | mail | Gmail ingest (4.18.0, `views/mail.js`, nav `data-route="mail"` in `shell.html:73`): add-account form (address + app password field, never prefilled/stored), create/revoke/safety hints, per-account label list (enable, root direction, Claude toggle, system folders collapsed), refresh labels, delete, "load deeper" (`POST /api/mail/sync {months}`). Reads `GET /api/mail/status`; error codes map to `mail.err.*` via a literal `ERR_KEYS`; styles `.rc-mail*` (16 rules in `recall.css`). Emails in the Library/transcript use the generic row (title = subject) |
| `/settings` | settings | System info, models, copilot, categories, speakers, **language selector** (`GET/POST /api/settings/ui-lang` → reload, :96-121) |
| `/record` | record | Recorder: tabs of parallel sessions (`/api/recordings/active`, SSE for each), system audio choice, retarget `/system-source`, 409 `SESSION_LIMIT`, copilot move; `?app=&mic=` → `presetFrom(q)`. 4.23.0 save screen for ANY stop: `?stopped=<sid>` (regex `rec_\w{1,64}`) → `attachStopped()` (:164), `applyAutosave()` (:1915) counts down to `state.autosave.at` (client fallback `AUTOSAVE_FALLBACK_SEC=60`, lead 3 s before the server deadline); typing/`change`/stopname dialog → `pauseAutosave()` (:1972) → `POST /api/recording/<sid>/autosave/hold` with the typed fields (best-effort); polls the state every 15 s; discard 409 `RECORDING_KEPT` handled (:2167); its save defaults match the server autosave: language `langOpts('auto')` (4.23.1, the live-preview picker keeps `uk`), model `DEFAULT_MODEL='large-v3'` (:24, 4.23.2) |

## Service worker — `static/sw.js`
**Cache version: `recall-v92`** (sw.js:122, 4.29.0: `/record` live co-pilot is opt-in per call — `#rcCpOn` unchecked on every render, panel fields in `<fieldset id="rcCpFields" disabled>`, `collectCopilot` returns null without it, so `/api/recording/start` gets no `copilot`; guarded by `tests/test_copilot_opt_in.py`; v91 4.28.0: every model select defaults to `large-v3`, `DEFAULT_MODEL` in upload/youtube/audio/record/settings views, pinned by `tests/test_whisper_defaults.py`; before that v90, 4.26.0: agent badge + confirm button on comments — `comments.js:106-107`, `views/comments.js:191-214`, `A.confirmComment` api.js:263 -> `POST /api/comments/<id>/confirm`, keys `knowledge.comments.badge.agent|confirm`; earlier: v89 ( v88/v89 not traced to a spec, v89 is current after 4.25.0 re-transcribe/compare; v87 = save-screen model large-v3, v86 = save-screen auto language, v85 = recording autosave; v81 = /mail screen, v82 = `/connect/` passthrough; current value read from the tree). The precache list is derived from the `<script>/<link>` tags of `shell.html` — a new view only needs its tag. shell precache; `/static/` stale-while-revalidate; CDN cache-first;
API-GET network-first; SSE/POST are not cached. `/connect/*` (one-time secret pages, `blueprints.md` § connect.py) returns early
in the fetch handler (sw.js:213): never cached, never served from cache, never falls back to the SPA shell. They are server-rendered
(`templates/connect.html`), not an SPA view. On `activate` old `recall-vN` are cleaned up.

## Gotchas
1. **SSE via fetch, not EventSource** — EventSource poisons werkzeug keep-alive.
2. **Bump `VERSION` in sw.js** on any JS/CSS/HTML change, otherwise the old code comes from the cache.
3. **CSS collisions** — grep `\.rc-<name>\b` before a new class.
4. **catsel interceptor** — `.rc-catsel` on `UI.CAT_NEW` is caught in the capture phase; the view does not see it.
5. **Render-seq guard** — an async render after navigation is dropped if `!ctx.isCurrent()`.
6. **URL-driven state** — library/audio/entities/tasks/research/ask keep filters in the query (`category_id`, `tags`,
   `since`/`until`), every filter is accompanied by `UI.hint()`.
7. **`[hidden]` and a custom `display`** — the global `.rc-app [hidden]{display:none!important}` in `recall.css`.
8. **Meta rows** `.filter(Boolean).join(sep)`, do not hardcode `<span class="sep">`.
9. **`display_name`** instead of `source_name`: `t.display_name || t.source_name`; the exception is the TG row in the Library.
10. **i18n (4.16.0):** every new UI string goes through a dictionary key `R.t('area.key', …)` (a literal as the first argument;
    dynamic keys only via a map of literals); there is no Cyrillic in `.js` outside `i18n/` — guard
    `tests/test_i18n_coverage.py` (allowlist `JS_ALLOW` in the same place: slugs, language endonyms, stop words). A direct `fetch` outside
    `api.js` must carry `R.i18n.headers()` (`transcript.js:358`). Text in `shell.html` — `data-i18n*`. uk/en parity
    and `{vars}` — `tests/test_i18n_js.py`. Dates/numbers — `R.i18n.num/date`, sorting — `R.i18n.compare`.
    JS comments are English too (4.17.0, `tests/test_code_english.py` covers `.py` only); server-originated texts arrive already
    in the request language (`X-Recall-Lang`), including the service-layer catalog `svc_*`.
11. **`data-i18n-content`** sits on `<meta name=description>` in `shell.html:7`, but `apply()` does not handle it
    (:82-87) — the static uk text stays.

**CSS:** `static/css/recall.css` — tokens (`--rc-canvas/ink/accent/src-*`), Fraunces/Inter/IBM Plex Mono, scope `.rc-app`.
**Load order (`shell.html:119-153`):** namespace → i18n → i18n/{core,capture,transcript,settings,library,knowledge,telegram,mail}
→ util → api → ui → research_export → comments → compare (4.25.0) → views → shell → onboarding → cmdk → router.
