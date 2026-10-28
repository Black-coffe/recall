# frontend — SPA (`static/js/recall/`)

Client-side router + shell, vanilla JS (no framework), global `window.Recall`.
last-verified: 2026-10-28 (calendar-connect-guide 4.38.0: settings guide, sw.js v97; 4.37.0 copilot view, record/shell/transcript additions); earlier passes condensed, see git history
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
  to a one-time URL; "Not needed"/× on "Record it?" → `POST …/dismiss`; × on "Stop recording?" only hides). 4.36.0: `kind === 'started'` (auto-record running) shows Stop (one-time URL) / Keep (hides, no request) (`showCallPrompt` :237-241); global recording badge `startRecBadge` :291 polls `GET /api/recordings/active` every `REC_POLL_MS` on a visible tab (hidden on `/record`); `checkRecovered` :318 reads `/api/recordings/recovered?ack=0`, renders `renderRecoveredBanner` :329 (link to `/audio`), only then `?ack=1`. Keys `core.call.recording_started|keep`, `core.rec.badge|recovered` in `i18n/core.js`.
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
| `/transcript/:slug` | transcript | Record: view plain/segments/polished, player+seek, bookmarks, category, copilot timeline, pencil → `UI.editMetaModal` (renaming re-slugs the URL). 4.25.0 versions bar (`transcript.js:670-840`; `R.api.versions` best-effort, `.rc-versions*`): rows model/date/original/main tags, view a version read-only (banner), make main (`activateVersion` :781), two-column compare (`openCompare` :793, `paintCompare` :810, `R.compare.align/diffWords`); shows a spinner while `running_job_id`. `?task=<id>` → `focusTask(rel,id)` (`.is-target`, 4 s); a foreign id — nothing; 4.35.0 the task list hides `legacy` rows and marks `proposed`/`expired`/`rejected` (`PROPOSAL_STATUS` :9, `.is-proposal`), so a proposal card link lands on the row |
| `/ask` | ask | RAG chat: `api.askStream()`, citations, 👍/👎+note (`R.api.rateAsk` → `POST /api/memory/ask/<id>/rate`) |
| `/research` | research | Brand research: preview → originals(.md) / AI-summary |
| `/entities`, `/entities/:id` | entities | Graph: entities (≥2 mentions) + card |
| `/tasks` | tasks | Urgency buckets (one request per bucket) + owner + status chips for every `commitments.STATUSES` value (4.35.0, ADR-056; `legacy` only via its own chip) + period + category. Proposal rows carry flags and a Yes/No pair (`data-decide`, `APPROVE_FROM`/`REJECT_FROM` :36-37) via `A.taskDecide(id, action)` (api.js:290) and a one-step undo `A.undoActionEvent` (:291); the `proposed` listing starts the 3-day clock server-side |
| `/comments` | comments | Comments list |
| `/speakers` | speakers | Speakers: stats, rename/delete, bulk-merge, timeline |
| `/youtube`, `/upload`, `/documents` | youtube, upload, documents | URL/file → progress → /transcript (documents: dedup by hash) |
| `/telegram` | telegram | Listener panel: dialogs, monitoring, category, backfill; row `.rc-tgrow*`, search `#rcTgSearch` (`searchIndex`, `phoneHit`) |
| `/mail` | mail | Gmail ingest (4.18.0, `views/mail.js`, nav `data-route="mail"` in `shell.html:73`): add-account form (address + app password field, never prefilled/stored), create/revoke/safety hints, per-account label list (enable, root direction, Claude toggle, system folders collapsed), refresh labels, delete, "load deeper" (`POST /api/mail/sync {months}`). Reads `GET /api/mail/status`; error codes map to `mail.err.*` via a literal `ERR_KEYS`; styles `.rc-mail*` (16 rules in `recall.css`). Emails in the Library/transcript use the generic row (title = subject) |
| `/settings` | settings | System info, models, copilot, categories, speakers, **language selector** (`GET/POST /api/settings/ui-lang` → reload, :96-121); 4.34.0 **Calendars** section (`loadCalendars` :211 / `renderCalendars` :221, called from :83): list from `GET /api/settings/calendars`, label field -> `POST .../connect-link` (opens the one-time `/connect/calendar/` page; the ICS address is never typed in the SPA), per-source sync `POST .../<id>/sync`, disconnect `UI.gated(... R.api.del('/api/settings/calendars/<id>'))`; keys `settings.cal.*` in `i18n/settings.js`; 4.38.0 `calendarGuideHTML()` (:222, rendered in `renderCalendars` :259): collapsed `<details class="rc-hint rc-calguide">` with 6 numbered steps, captions `settings.cal.guide.s1..s6|title|workspace` (UI language), screenshots are English-only static PNGs `static/img/calendar-guide/{1-settings,2-integrate,3-recall-calendars,4-connect-page,5-connected,6-recall-connected}.png` (`loading="lazy"`); styles `.rc-calguide*` in `recall.css` |
| `/record` | record | Recorder: tabs of parallel sessions (`/api/recordings/active`, SSE for each), system audio choice, retarget `/system-source`, 409 `SESSION_LIMIT`, copilot move; `?app=&mic=` → `presetFrom(q)`. 4.23.0 save screen for ANY stop: `?stopped=<sid>` (regex `rec_\w{1,64}`) → `attachStopped()` (:164), `applyAutosave()` (:1915) counts down to `state.autosave.at` (client fallback `AUTOSAVE_FALLBACK_SEC=60`, lead 3 s before the server deadline); typing/`change`/stopname dialog → `pauseAutosave()` (:1972) → `POST /api/recording/<sid>/autosave/hold` with the typed fields (best-effort); polls the state every 15 s; discard 409 `RECORDING_KEPT` handled (:2167); its save defaults match the server autosave: language `langOpts('auto')` (4.23.1, the live-preview picker keeps `uk`), model `DEFAULT_MODEL='large-v3'` (:24, 4.23.2). 4.36.0: start-form gain sliders (sent as `gain` only when off 0) and live `POST /api/recording/<sid>/gain` (:756); participants chips `G.participants` prefilled from `GET /api/recording/participants`, search `?q=` (:765-822), `/start` gets `participants` ids; `ERR_TXT.DEVICE_ACCESS_DENIED` + per-stream `DENIED_TXT` (:89-96, 403 `stream`); `resyncState` (:1529) re-reads `/state` on SSE reconnect/permanent failure and on a failed pause/resume, a gone or `crashed` session sets `S.interrupted` and banner `capture.rec.interrupted`; `/start` `video.enabled === false` -> toast `capture.rec.video.without_screen` (:1022) |

| `/copilot` | copilot | 4.37.0 read-only review (`views/copilot.js`, dictionary `i18n/copilot.js` `copilot.*`, nav `data-route="copilot"` `shell.html:80`, route `router.js:32`): sessions list from `GET /api/copilot/sessions` (shown/muted/thumbs counts), per-session timeline `GET /api/copilot/<id>/timeline` with muted cards greyed, export links `/export?format=md|json`. No mutation |

**4.37.0 additions to existing views:** `record.js` consent line `paintConsent` (:1262-1276; `S.consent` from `/start` and `/state` `consent_reminder`, dismiss in memory per session, key `capture.rec.consent`) and «What did I miss» box (:1279-1325; window picker since_self|2m|5m|10m|whole, `POST /api/copilot/live-missed`, hidden for non-owner starters or on 403 `AGENT_SESSION`, 429 shows the cap, 503 unavailable);
`shell.js:252` consent line on the `started` plate (`core.call.consent`); `transcript.js:1041` bookmark clip link `a[data-clip]` -> `/api/bookmarks/<id>/clip` (`transcript.bm.clip`).

## Service worker — `static/sw.js`
**Cache version: `recall-v97`** (sw.js:129, 4.38.0: Google calendar guide in settings; v96 = 4.37.0: clip button, /record consent + missed, /copilot view; v95 = 4.36.0: recorder gain/participants/interrupted, shell badge + banner; v94 = 4.35.0: task approval UI; v93 = 4.34.0: Calendars section in settings; v92 = 4.29.0: `/record` live co-pilot is opt-in per call — `#rcCpOn` unchecked on every render, panel fields in `<fieldset id="rcCpFields" disabled>`, `collectCopilot` returns null without it, so `/api/recording/start` gets no `copilot`; guarded by `tests/test_copilot_opt_in.py`; v91 4.28.0: every model select defaults to `large-v3`, `DEFAULT_MODEL` in upload/youtube/audio/record/settings views, pinned by `tests/test_whisper_defaults.py`; before that v90, 4.26.0: agent badge + confirm button on comments — `comments.js:106-107`, `views/comments.js:191-214`, `A.confirmComment` api.js:263 -> `POST /api/comments/<id>/confirm`, keys `knowledge.comments.badge.agent|confirm`; earlier: v89 ( v88/v89 not traced to a spec, v89 is current after 4.25.0 re-transcribe/compare; v87 = save-screen model large-v3, v86 = save-screen auto language, v85 = recording autosave; v81 = /mail screen, v82 = `/connect/` passthrough; current value read from the tree). The precache list is derived from the `<script>/<link>` tags of `shell.html` — a new view only needs its tag. shell precache; `/static/` stale-while-revalidate; CDN cache-first;
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
**Load order (`shell.html:119-153`):** namespace → i18n → i18n/{core,capture,transcript,settings,library,knowledge,telegram,mail,copilot}
→ util → api → ui → research_export → comments → compare (4.25.0) → views → shell → onboarding → cmdk → router.
