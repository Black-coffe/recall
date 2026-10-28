/* Recall service worker — full offline app shell + read-only API cache.
 *
 * Strategies:
 *   - HTML/navigation:    network-first, fall back to cache, final fall back to '/'.
 *   - /static/* (local):  stale-while-revalidate (fast from cache + background update).
 *   - CDN (origin !=):    cache-first (versioned URLs Inter/FontAwesome/Tippy — stable).
 *   - /api/* GET:         network-first with a cache fallback (data may be stale, but better than nothing).
 *   - SSE (Accept: text/event-stream or path /stream): not cached (browser default).
 *   - POST/PATCH/DELETE:  not cached.
 *
 * What does NOT work offline (deliberately): transcription, recording, ask/stream, polish, backup.
 * What works: opening the page, viewing history, entities, tasks (from cache).
 */

// Bump on any app shell change (JS/CSS/HTML), so the SW drops the old cache and serves
// fresh assets. v2 — Phase 16 (Documents tab, new formats, folder import).
// v3 — Phase 17 (Telegram tab + the «Новий напрямок» button).
// v4 — fix: the checkbox/category selector in TG listened to change via data-change.
// v5 — TG: filter chips + showing the saved category (merge with /chats).
// v6 — Design standard: dark theme removed, labels rebranded (Recall/records).
// v10 — Recall Phase 2: native YouTube page (/youtube) in the new shell.
// v11 — Recall Phase 2: native file upload page (/upload).
// v12 — Recall Phase 2: native documents page (/documents).
// v13 — Recall Phase 2: native Telegram page (/telegram).
// v14 — Recall Phase 2: native Settings page (/settings).
// v15 — Recall Phase 2: native Record page (/record). Phase 2 complete.
// v16 — cleanup: APP_SHELL = the new shell (recall/*), not the old app.js/index.html.
// v17 — native Audio library (/audio).
// v18 — transcript post-processing: translation/topics/word cloud/emotions.
// v19 — bulk category labeling + bulk deletion in the Library.
// v20 — dynamic model catalog + faster-whisper update check.
// v21 — default transcription model large-v3-turbo (instead of medium).
// v22 — new section «Дослідження»: big export of brand mentions (originals/summaries).
// v23 — export buttons on the entity card (the same research_export.js engine).
// v24 — legacy port: YouTube trim + the «Спікери» page (statistics/merging/timeline).
// v25 — speaker editor on the record page: name/reassign diarized voices.
// v26 — click-on-segment→seek+highlight during playback; bookmarks; ✨ auto-category; saved searches; bulk-export.
// v27 — /legacy removed: all features ported to the new shell (index.html/app.js/old CSS dropped).
// v28 — Co-pilot Step 1: co-pilot mega-panel at recording start (vector/mode/importance/budget/local-only).
// v29 — Co-pilot Step 2: topic strip in the recording (topic shift/return) via copilot_topic SSE.
// v30 — Co-pilot Step 3: local dispatcher LLM (triage+RAG+insights) → draft copilot_insight cards.
// v31 — Co-pilot Step 4: a full operator widget (2 columns, cards with actions dismiss/pin/👍👎/dig deeper, status, pause, anti-noise, collapse).
// v32 — Co-pilot Step 5: escalation to Claude (Sonnet verifies → badge «перевірено», $ counter, budget/safety-sweep, manual «копнути глибше»).
// v33 — Co-pilot Step 6: mode×importance matrix (multi-voice verify, Opus on the hard ones), cost-governor (warn), changing mode/importance on the fly.
// v34 — Co-pilot Step 7: session timeline on the transcript page (parallel track of events + seek by insight), session→transcript link.
// v35 — Co-pilot Step 8: session export (md/json: dialogue+notes) + re-ingest of hints into RAG (source_type=copilot).
// v36 — Co-pilot Step 9: hardening — GPU queue, degradation banner, «Ко-пілот» section in settings, smoke tests.
// v37 — category filter on /audio + /entities, category filter + period buttons (24h/7d/month/year/all) on /tasks.
// v38 — reworked the period control on /tasks: segment control (max-content+nowrap, does not break), shorter labels.
// v39 — class collision fix: period control .rc-seg→.rc-period (.rc-seg is already taken by the audio segments of /transcript as a grid).
// v40 — contextual facet filters in the Library per source: Telegram (sender+chat), YouTube (author) — live from 2 characters.
// v41 — categories: management in Settings (CRUD+move/merge), inline «＋ Новий напрямок…» in any select, casefold uniqueness (Cyrillic duplicates are impossible).
// v42 — recording UX: a recording appears in the Audio library right after save (non-blocking save→background transcription via the library path), «транскрибується…» status in the card (polling /api/transcribe/active), the recording's own category (audio_downloads.category_id, the filter works before transcription).
// v43 — screen video capture toggle + monitor select + video status chips (Phase 22, S7).
// v44 — Audio library→Media library + 📹 Відео badge + UTC→local date fix (Phase 22).
// v45 — Camtasia-style screen region selection UI: per-monitor Повний/Область toggle, drag-rect modal, numeric inputs, region chip (Phase 22 R-D).
// v46 — Phase 23 B-C: synced muted screen-video player, keyframe strip, «Розібрати відео» button on /transcript.
// v47 — Phase 23B: vision description of frames (Qwen2.5-VL / Claude) → into RAG + frame tooltips + Claude description toggle.
// v48 — REC indicator: live file size + growth rate (MB/min) + free disk space with a forecast.
// v49 — REC video chip: size of the selected area (◳ W×H) + video tracks in get_state (chips survive reload).
// v50 — REC video chip: track_id reconciliation with state after start (canonical label/status/fps + region, no stale chip).
// v51 — fix fps/MB on the video chip: video_stats sends {stats:{...}} nested, the front read it flat → unpacking.
// v52 — Wave 2 T4.4: the unconditional skipWaiting() removed (now only on a
//       SKIP_WAITING message from the client, see shell.js promptSwUpdate);
//       APP_SHELL is no longer duplicated by hand — install parses the real
//       <script src>/<link href> from the response to '/' (deriveAppShellUrls),
//       only FALLBACK_SHELL remains for what shell.html does not have as a
//       tag (manifest/icons) — see the bump checklist below.
// v53 — Wave 3 T5.3: first-run onboarding wizard (static/js/recall/
//       onboarding.js, a new <script> tag in shell.html — picked up by
//       parsing; localStorage recall_onboarding_done). shell.js calls
//       maybeShowFirstRun() + a graduation-cap topbar icon for reopening;
//       minimal CSS (.rc-modal--wide, .rc-onb__*).
// v58 — list UI/UX: /library renders a row BY source type (TG —
//       chat+sender in the caption, message in the body; call — duration/
//       speakers/tasks), /audio got a «Без транскрипта» filter, /tasks
//       was rebuilt on urgency buckets (overdue / 7 days / later
//       / no date) + an owner facet. The --rc-ink-3 and --rc-ok tokens were raised to
//       WCAG AA; the global `.rc-app [hidden]{display:none}` removed the phantom
//       bulk-action panel that always hung at the bottom of the Library.
// v62 — owner comments layer (Waves 0-6): a counter badge and an inline panel
//       on /library and /audio cards, a permanent panel on /transcript with an
//       anchor to the player timecode, a live comment field on /record
//       (Ctrl+Shift+K), a new /comments page — a notebook over the whole
//       archive with facets by type. New files: js/recall/comments.js
//       (shared component) and js/recall/views/comments.js — both have
//       tags in shell.html, so install picks them up by itself.
// v66 — UNASKED(2) of round 4: tag picker with chips (.rc-tagpick over
//       the hidden select.rc-tagsel, the same C9 contract) instead of a bare
//       <select multiple>; an empty tag dictionary — an explanatory paragraph with a
//       link to Settings→Tags instead of an empty rectangle.
// v68 — wave 8: one color guard in ui.js (tagChip/tagOptions/catChip —
//       #RGB/#RGBA/#RRGGBB/#RRGGBBAA, the same contract as the server
//       _COLOR_RE); the tag picker error state keeps already chosen tags as
//       selected options (the URL filter is no longer lost on retry);
//       also covers record.js of story 27 (one bump per wave).
// v69 — parallel recordings: /record shows each live recording as a tab (its own
//       SSE, tick, stop), a system sound switch «весь / тільки ці /
//       усе крім цих», a question about sounding applications on a second recording,
//       a banner returning the sound to the neighbour, co-pilot transfer
//       (.rc-rectabs/.rc-recsrc/.rc-recwarn in recall.css).
// v70 — /telegram: the chat row shows who it is (title, type, @nick, phone),
//       tag chips removed from the row (they pushed the title to zero); search by
//       name (Cyrillic/Latin), @nick and phone.
// v71 — made-up examples in hints, onboarding and settings («Агенція» →
//       «Маяк», «Орбіта», Polaris, Олена) instead of real categories, projects and people.
// v81 — /mail: Gmail ingest screen (views/mail.js, i18n/mail.js, .rc-mail* styles).
// v88 — library and media library: "Re-transcribe" action with an installed-model picker and job polling.
// v91 — default transcription model large-v3 in every select (whisper-best-default).
// v92 — /record: the live co-pilot is opt-in per call (checkbox, unchecked by default).
// v93 — Settings: Calendars row (k6-calendar-ics, connect via a one-time local page).
// v94 — /tasks: proposal statuses, Yes / No with undo; transcript marks proposals (task-approval).
// v95 — recording calls: REC badge + recovered banner + "Recording started" toast in the shell;
//       /record gain sliders, honest interruption, pause/resume resync, participants picker.
// v96 — bookmark clip button on the transcript page; /record consent line and "What did I miss";
//       /copilot review screen (views/copilot.js, i18n/copilot.js, .rc-cpr* styles).
// v97 — Settings → Calendars: step-by-step Google guide (.rc-calguide*, /static/img/calendar-guide/).
//
// CHECKLIST for a version bump (minimum manual work, no build step):
//   1. An edit in JS/CSS under /static/ → just increase VERSION below + add a
//      line to the changelog. The file list does NOT need touching — install
//      rereads the <script>/<link> tags from shell.html itself.
//   2. A new JS/CSS file needed for the first render (view module, css)
//      → add a <script>/<link> tag to it in templates/shell.html — and that's it:
//      sw.js will pick it up automatically on the next install.
//   3. A new asset with NO tag in shell.html (an icon from
//      manifest.webmanifest, another file that only JS references
//      by a string) → add it by hand to FALLBACK_SHELL below.
const VERSION = 'recall-v97';
const STATIC_CACHE = `recall-static-${VERSION}`;
const RUNTIME_CACHE = `recall-runtime-${VERSION}`;

// Minimal manual list: only what is NOT discovered by parsing
// shell.html (has no <script src>/<link href> of its own — the manifest references
// the icons by strings in JSON, not by HTML tags).
const FALLBACK_SHELL = [
    '/',
    '/static/favicon.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/manifest.webmanifest',
];

// Derives the full app shell from the fresh '/' markup — so APP_SHELL is no longer
// duplicated by hand against the <script>/<link> tags in shell.html (that was the
// third place of manual version sync, besides VERSION and the
// changelog itself). If fetch/parsing fails — we degrade to FALLBACK_SHELL,
// so that install() at worst caches at least '/' and the critical icons.
async function deriveAppShellUrls() {
    try {
        const res = await fetch('/', { cache: 'no-store' });
        if (!res.ok) return FALLBACK_SHELL;
        const html = await res.text();
        const urls = new Set(FALLBACK_SHELL);
        const re = /<(?:script[^>]+src|link[^>]+href)=["']([^"']+)["']/gi;
        let m;
        while ((m = re.exec(html))) {
            const u = m[1];
            // Only local /static/* — the CDN (fonts/FontAwesome) is deliberately NOT
            // precached here, it goes through cache-first in the fetch handler.
            if (u.startsWith('/static/')) urls.add(u);
        }
        return Array.from(urls);
    } catch (err) {
        console.warn('[sw] deriveAppShellUrls failed, using fallback', err.message);
        return FALLBACK_SHELL;
    }
}

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(STATIC_CACHE);
        const shellUrls = await deriveAppShellUrls();
        // Each asset separately, so that 1 failure does not bury the whole install.
        await Promise.all(shellUrls.map((url) =>
            cache.add(url).catch((err) => console.warn('[sw] precache skip', url, err.message))
        ));
        // We do NOT call self.skipWaiting() here — the new worker stays
        // "waiting" until the client explicitly asks (the SKIP_WAITING
        // message from shell.js, after a click on «Оновити» in the toast). This way
        // an open tab never switches to new modules in the middle of a
        // session without the user's knowledge.
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        // Delete old cache versions (the previous VERSION).
        const keys = await caches.keys();
        await Promise.all(keys
            .filter((k) => k.startsWith('recall-') && k !== STATIC_CACHE && k !== RUNTIME_CACHE)
            .map((k) => caches.delete(k)));
        // Take control of all tabs at once (without reload).
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', (event) => {
    const req = event.request;

    // Non-GET: network only. POST/PATCH/DELETE always go to the server.
    if (req.method !== 'GET') return;

    const url = new URL(req.url);
    const accept = req.headers.get('Accept') || '';

    // SSE: never cache (a stream).
    if (accept.includes('text/event-stream') || url.pathname.includes('/stream')) return;

    // CDN (another origin) — cache-first. Versioned URLs are stable.
    if (url.origin !== self.location.origin) {
        event.respondWith(cacheFirst(req, RUNTIME_CACHE));
        return;
    }

    // Local static — stale-while-revalidate.
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(staleWhileRevalidate(req, STATIC_CACHE));
        return;
    }

    // One-time secret pages: never cached, never served from cache.
    if (url.pathname.startsWith('/connect/')) return;

    // HTML navigation (the root or a direct entry into /something) — network-first with a fallback to '/'.
    if (req.mode === 'navigate' || accept.includes('text/html')) {
        event.respondWith(networkFirstFallback(req, STATIC_CACHE, '/'));
        return;
    }

    // /api/* GET — network-first with a cache fallback (we see stale data offline).
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(networkFirstFallback(req, RUNTIME_CACHE, null));
        return;
    }

    // Everything else — network passthrough.
});

// ============ helpers ============

async function cacheFirst(req, cacheName) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(req);
    if (cached) return cached;
    try {
        const resp = await fetch(req);
        if (resp.ok) cache.put(req, resp.clone());
        return resp;
    } catch (_) {
        return new Response('offline', { status: 503, statusText: 'Offline' });
    }
}

async function staleWhileRevalidate(req, cacheName) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(req);
    const networkPromise = fetch(req).then((resp) => {
        if (resp && resp.ok) cache.put(req, resp.clone());
        return resp;
    }).catch(() => null);
    return cached || (await networkPromise) || new Response('offline', { status: 503 });
}

async function networkFirstFallback(req, cacheName, fallbackUrl) {
    const cache = await caches.open(cacheName);
    try {
        const resp = await fetch(req);
        // Do not cache error responses (4xx/5xx).
        if (resp && resp.ok) cache.put(req, resp.clone());
        return resp;
    } catch (_) {
        const cached = await cache.match(req);
        if (cached) return cached;
        if (fallbackUrl) {
            const fb = await cache.match(fallbackUrl);
            if (fb) return fb;
        }
        return new Response('Ви офлайн і ця сторінка ще не кешована',
            { status: 503, statusText: 'Offline', headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
    }
}

// Lets the page ask a new SW version to activate instantly.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
