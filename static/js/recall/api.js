/* Recall — API layer. Thin wrappers over the existing JSON /api endpoints.
   Reuses everything; adds no backend coupling beyond URLs. */
(function () {
    'use strict';
    const A = {};

    // Default timeout for plain GET requests only (getJSON/A.get). NEVER
    // auto-applied to POST/PATCH/PUT/DELETE (send()) — many of those call
    // Claude (polish/summarize/translate/sentiment/research) and legitimately
    // run well past this window. Pass { timeoutMs: N } to opt a specific POST
    // in, or { timeoutMs: 0 } / { timeoutMs: false } to disable it for a GET.
    const DEFAULT_GET_TIMEOUT_MS = 15000;
    // Transient-error retry (GET only — idempotent). Mutating calls (send())
    // never auto-retry, to avoid double-firing e.g. a bulk_delete on a 503.
    const DEFAULT_GET_RETRIES = 2;
    const RETRY_BASE_MS = 300;

    function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

    // Merge N AbortSignals into one: aborts as soon as any input aborts.
    // Falls back to returning the single signal unchanged when there's only
    // one (or none), so callers that pass nothing keep getting `undefined`.
    function combineSignals(signals) {
        const valid = (signals || []).filter(Boolean);
        if (!valid.length) return undefined;
        if (valid.length === 1) return valid[0];
        const ctrl = new AbortController();
        for (const s of valid) {
            if (s.aborted) { ctrl.abort(s.reason); break; }
            s.addEventListener('abort', () => ctrl.abort(s.reason), { once: true });
        }
        return ctrl.signal;
    }

    async function fetchOnce(url, opts) {
        const res = await fetch(url, opts);
        if (!res.ok) {
            let msg = 'HTTP ' + res.status;
            try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (_) {}
            const e = new Error(msg); e.status = res.status; throw e;
        }
        return res.json();
    }

    // getJSON(url, opts) — opts is a normal fetch RequestInit PLUS two extra
    // (non-fetch) knobs consumed here and stripped before the real fetch():
    //   timeoutMs — overrides the default. `0`/`false` disables timeout entirely.
    //               Default: 15s for GET, OFF for everything else.
    //   retries   — overrides DEFAULT_GET_RETRIES (GET-only auto-retry on 502/503).
    // Network/timeout failures are normalized into an Error with .isTimeout /
    // .isNetworkError flags so callers (e.g. a future shell connection-health
    // indicator) can tell "server didn't answer" apart from a real HTTP error
    // (which still throws a plain Error with .status, as before).
    async function getJSON(url, opts) {
        opts = opts || {};
        const method = (opts.method || 'GET').toUpperCase();
        let timeoutMs = opts.timeoutMs;
        if (timeoutMs === undefined) timeoutMs = (method === 'GET') ? DEFAULT_GET_TIMEOUT_MS : 0;
        const maxRetries = opts.retries != null ? opts.retries : (method === 'GET' ? DEFAULT_GET_RETRIES : 0);

        const fetchOpts = Object.assign({ headers: { 'Accept': 'application/json' } }, opts);
        delete fetchOpts.timeoutMs;
        delete fetchOpts.retries;
        const externalSignal = opts.signal;

        let attempt = 0;
        while (true) {
            attempt++;
            const timeoutSig = timeoutMs ? AbortSignal.timeout(timeoutMs) : null;
            const signal = combineSignals([externalSignal, timeoutSig]);
            const attemptOpts = Object.assign({}, fetchOpts);
            if (signal) attemptOpts.signal = signal;
            try {
                return await fetchOnce(url, attemptOpts);
            } catch (err) {
                // HTTP-level error with a status (fetchOnce throws these) — maybe retry, else rethrow as-is.
                if (err && typeof err.status === 'number') {
                    const retryable = (err.status === 502 || err.status === 503) && attempt <= maxRetries;
                    if (retryable) { await sleep(RETRY_BASE_MS * Math.pow(2, attempt - 1)); continue; }
                    throw err;
                }
                // Caller-initiated abort — propagate untouched so existing
                // `err.name === 'AbortError'` checks keep working.
                if (externalSignal && externalSignal.aborted) throw err;
                // Otherwise: network failure or our own timeout fired.
                const timedOut = !!(timeoutSig && timeoutSig.aborted);
                const e = new Error(timedOut ? 'Час очікування відповіді вичерпано' : 'Немає з’єднання з сервером');
                e.isTimeout = timedOut; e.isNetworkError = true; e.cause = err;
                throw e;
            }
        }
    }

    // send(url, method, body, options) — options forwards straight to getJSON
    // (signal/timeoutMs/retries). Existing 3-arg callers are unaffected.
    async function send(url, method, body, options) {
        const opts = Object.assign({
            method,
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: body != null ? JSON.stringify(body) : undefined,
        }, options || {});
        return getJSON(url, opts);
    }
    // multipart/form-data POST (file upload, /api/transcribe) — same error contract.
    async function postForm(url, formData) {
        const res = await fetch(url, { method: 'POST', body: formData, headers: { 'Accept': 'application/json' } });
        if (!res.ok) {
            let msg = 'HTTP ' + res.status;
            try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (_) {}
            const e = new Error(msg); e.status = res.status; throw e;
        }
        return res.json();
    }
    A.get = getJSON;
    A.post = (u, b, o) => send(u, 'POST', b, o);
    A.patch = (u, b, o) => send(u, 'PATCH', b, o);
    A.put = (u, b, o) => send(u, 'PUT', b, o);
    A.del = (u, o) => send(u, 'DELETE', undefined, o);
    A.postForm = postForm;

    // Build a query string DROPPING null/undefined/'' — інакше URLSearchParams
    // серіалізує undefined у літерал "undefined" (напр. search=undefined → FTS
    // шукає слово "undefined" і нічого не знаходить → порожня Бібліотека).
    function qs(params) {
        const sp = new URLSearchParams();
        for (const k in (params || {})) {
            const v = params[k];
            if (v !== undefined && v !== null && v !== '') sp.set(k, v);
        }
        const s = sp.toString();
        return s ? '?' + s : '';
    }

    // — History / transcripts —
    A.history = (params) => getJSON('/api/history' + qs(params));
    A.transcript = (id) => getJSON('/api/history/' + id);
    A.audioUrl = (id) => '/api/transcription/' + id + '/audio';
    A.exportUrl = (fmt) => '/api/export/' + fmt;

    // — Ingest: YouTube → download → transcribe (lands in History/transcriptions) —
    A.models = () => getJSON('/api/models');
    A.youtubeInfo = (url) => A.post('/api/youtube/info', { url });
    A.youtubeDownload = (body) => A.post('/api/youtube/download', body);
    A.youtubeProgress = (id) => getJSON('/api/youtube/progress/' + encodeURIComponent(id));
    A.transcribe = (formData) => postForm('/api/transcribe', formData);
    // Активні бекенд-транскрипції, стартовані з Аудіотеки (library-шлях).
    // Ключ — audio_download_id. Бібліотека опитує для бейджа «транскрибується…».
    A.transcribeActive = () => getJSON('/api/transcribe/active');
    // Multipart upload via XHR so we get real upload progress (bodies can be
    // large — up to 20GB for video). onUploadProgress(pct) fires during the send;
    // onUploadDone() once bytes are all sent and the server starts its work
    // (response is then awaited). Resolves parsed JSON, rejects Error w/ .status.
    function uploadXHR(url, formData, onUploadProgress, onUploadDone) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url);
            xhr.setRequestHeader('Accept', 'application/json');
            if (xhr.upload) {
                xhr.upload.addEventListener('progress', (e) => {
                    if (e.lengthComputable && onUploadProgress) onUploadProgress(Math.round(e.loaded / e.total * 100));
                });
                xhr.upload.addEventListener('load', () => { if (onUploadDone) onUploadDone(); });
            }
            xhr.addEventListener('load', () => {
                let j = null;
                try { j = JSON.parse(xhr.responseText); } catch (_) {}
                if (xhr.status >= 200 && xhr.status < 300) { resolve(j); return; }
                const err = new Error((j && j.error) || ('HTTP ' + xhr.status));
                err.status = xhr.status; reject(err);
            });
            xhr.addEventListener('error', () => reject(new Error('Помилка зʼєднання з сервером')));
            xhr.addEventListener('abort', () => reject(new Error('Завантаження скасовано')));
            xhr.send(formData);
        });
    }
    A.uploadXHR = uploadXHR;
    A.transcribeUpload = (fd, p, d) => uploadXHR('/api/transcribe', fd, p, d);
    A.documentUpload = (fd, p, d) => uploadXHR('/api/documents/upload', fd, p, d);

    // — Audio Library (audio_downloads): downloaded audio + saved recordings —
    A.audioDownloads = (params) => getJSON('/api/audio/downloads' + qs(params));
    A.audioDelete = (id) => A.del('/api/audio/downloads/' + id);
    A.audioPlay = (id) => A.post('/api/audio/play/' + id);
    A.audioExplorer = (id) => A.post('/api/audio/open-explorer/' + id);

    // — Speakers (graph dimension): list / stats / timeline / merge / CRUD —
    A.speakers = () => getJSON('/api/speakers');
    A.speakersStats = () => getJSON('/api/speakers/stats');
    A.speakerTimeline = (id, days) => getJSON('/api/speakers/' + id + '/timeline' + qs({ days }));
    A.speakersMerge = (body) => A.post('/api/speakers/merge', body);
    A.speakerCreate = (name) => A.post('/api/speakers', { name });
    A.speakerRename = (id, name) => A.put('/api/speakers/' + id, { name });
    A.speakerDelete = (id) => A.del('/api/speakers/' + id);

    // — Saved searches (Library filters) —
    A.savedSearches = () => getJSON('/api/saved-searches');
    A.savedSearchCreate = (name, query) => A.post('/api/saved-searches', { name, query });
    A.savedSearchDelete = (id) => A.del('/api/saved-searches/' + id);
    A.savedSearchUse = (id) => A.post('/api/saved-searches/' + id + '/use');

    // — Bulk export (returns a file Response; caller handles the blob) —
    A.bulkExport = (ids, format) => fetch('/api/history/bulk_export', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, format }),
    });

    // — Segment bookmarks (transcript) —
    A.bookmarks = (tid) => getJSON('/api/transcription/' + tid + '/bookmarks');
    A.bookmarkCreate = (tid, segment_index, note) => A.post('/api/transcription/' + tid + '/bookmarks', { segment_index, note });
    A.bookmarkDelete = (id) => A.del('/api/bookmarks/' + id);
    A.bookmarkUpdate = (id, note) => A.patch('/api/bookmarks/' + id, { note });

    // — Коментарі власника (шар коментарів) —
    // counts іде POST'ом навмисно: Бібліотека вміє вибрати до записів за
    // фільтром, і такий список id у query-string не влазить.
    // qs() САМ додає '?' — літерал тут дав би '??', і Werkzeug прочитав би
    // перший ключ як '?target_type', тобто кожен запит падав би у 400.
    A.comments = (target_type, target_id) => getJSON('/api/comments' + qs({ target_type, target_id }));
    A.commentsRecent = (params) => getJSON('/api/comments/recent' + qs(params || {}));
    A.commentCounts = (target_type, ids) => A.post('/api/comments/counts', { target_type, ids });
    A.commentCreate = (payload) => A.post('/api/comments', payload);
    A.commentUpdate = (id, patch) => A.patch('/api/comments/' + id, patch);
    A.commentDelete = (id) => A.del('/api/comments/' + id);
    A.commentRestore = (id) => A.post('/api/comments/' + id + '/restore');
    A.commentMeta = () => getJSON('/api/comments/meta');
    // Без клієнтського тайм-ауту (дефолт POST і так 0): розбір платний, і
    // обірвати запит, за який уже заплачено, гірше, ніж почекати довше.
    A.commentAnalyze = (id, body) => A.post('/api/comments/' + id + '/analyze', body || {});
    A.commentDerived = (id) => getJSON('/api/comments/' + id + '/derived');

    // — Single-record напрямок: set + k-NN auto-suggest (✨) —
    A.setCategory = (tid, category_id) => A.patch('/api/memory/transcriptions/' + tid + '/category', { category_id });
    A.suggestCategory = (tid) => getJSON('/api/memory/transcriptions/' + tid + '/suggest-category');

    // — Memory / RAG / graph —
    A.stats = () => getJSON('/api/memory/stats');
    A.categories = () => getJSON('/api/memory/categories');
    // — Напрямки: керування (створення/редагування/видалення/обʼєднання) —
    A.categoryCreate = (body) => A.post('/api/memory/categories', body);
    A.categoryUpdate = (cid, body) => A.patch('/api/memory/categories/' + cid, body);
    A.categoryDelete = (cid) => A.del('/api/memory/categories/' + cid);
    A.categoryMerge = (cid, target_id, delete_source) =>
        A.post('/api/memory/categories/' + cid + '/merge', { target_id, delete_source });
    A.entities = (params) => getJSON('/api/memory/entities' + qs(params));
    A.entity = (id) => getJSON('/api/memory/entities/' + id);
    A.actionItems = (params) => getJSON('/api/memory/action-items' + qs(params));
    A.setActionStatus = (id, status) => A.patch('/api/memory/action-items/' + id, { status });

    // ============================================================
    // SSE layer: shared frame-parser + reliability helpers
    // ============================================================
    // Both postSseStream (POST, one-shot map-reduce — ask/research: server
    // ALWAYS ends with a `done` or `error` frame, see app/services/rag.py)
    // and sseStream (GET, often long-lived/continuous — e.g.
    // /api/recording/<sid>/stream has NO terminal frame by design, it just
    // streams until the client disconnects) share one frame-parsing loop.
    // That single ownership is what let askStream drift from researchSummary
    // before (dup'd ~30 lines, one got `signal`, the other didn't).

    // Reusable idle watchdog: fires onTimeout() if touch() isn't called again
    // within thresholdMs. touch() (re)arms the timer; cancel() disarms it.
    // Not SSE-specific — any view can use this for its own "nothing happened
    // in N seconds" detection.
    function createWatchdog(thresholdMs, onTimeout) {
        let timer = null;
        function arm() {
            if (timer) clearTimeout(timer);
            timer = setTimeout(() => { timer = null; onTimeout(); }, thresholdMs);
        }
        function cancel() { if (timer) { clearTimeout(timer); timer = null; } }
        arm();
        return { touch: arm, cancel };
    }
    A.createSseWatchdog = createWatchdog;

    // Shared frame-parsing loop over a fetch Response's SSE body.
    //   externalSignal     — the AbortSignal the CALLER passed in (if any). If
    //                        it's aborted, any break/throw is treated as an
    //                        intentional, clean stop — no synthetic `error`.
    //   internalAc          — an AbortController owned by postSseStream/
    //                        sseStream themselves, used only to trip the
    //                        idle watchdog (aborting it forces reader.read()
    //                        to reject deterministically, unlike reader.cancel()
    //                        whose settle behavior isn't reliably spec'd).
    //   idleTimeoutMs        — optional; if set, no read activity for this long
    //                        aborts the stream and reports a synthetic `error`.
    //   requireTerminalFrame — if true, reaching a clean end-of-stream WITHOUT
    //                        ever having seen a `done`/`error` event frame is
    //                        itself treated as an abrupt disconnect.
    // On an abrupt disconnect (network drop, idle timeout, or missing terminal
    // frame when required) this: (a) calls onEvent('error', {...}) once, so
    // existing consumers that already branch on event==='error' pick it up
    // for free, AND (b) rejects the returned promise, so callers using
    // try/catch around the await also see it. A clean, caller-initiated abort
    // (externalSignal.aborted) never does either — matches the old
    // "no auto-reconnect, silent stop" contract.
    async function readSseBody(res, onEvent, cfg) {
        cfg = cfg || {};
        const externalSignal = cfg.externalSignal;
        const internalAc = cfg.internalAc;
        const requireTerminalFrame = !!cfg.requireTerminalFrame;

        if (!res.ok || !res.body) {
            let msg = 'HTTP ' + res.status;
            try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (_) {}
            const e = new Error(msg); e.status = res.status; throw e;
        }
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        let sawTerminal = false;
        let watchdog = null;
        if (cfg.idleTimeoutMs && internalAc) {
            watchdog = createWatchdog(cfg.idleTimeoutMs, () => {
                try { internalAc.abort(new DOMException('SSE idle timeout', 'TimeoutError')); }
                catch (_) { try { internalAc.abort(); } catch (__) {} }
            });
        }

        function abruptFail(reason) {
            const err = new Error(reason); err.isStreamAbort = true;
            try { onEvent('error', { error: reason, code: 'sse_broken' }); } catch (_) {}
            return err;
        }

        try {
            while (true) {
                let chunk;
                try {
                    chunk = await reader.read();
                } catch (readErr) {
                    if (externalSignal && externalSignal.aborted) return;   // caller stopped it — clean
                    const idle = !!(internalAc && internalAc.signal.aborted);
                    throw abruptFail(idle ? 'Зʼєднання неактивне — сервер не відповідає' : 'Зʼєднання з сервером перервано');
                }
                if (chunk.done) break;
                if (watchdog) watchdog.touch();
                buf += dec.decode(chunk.value, { stream: true });
                let idx;
                while ((idx = buf.indexOf('\n\n')) >= 0) {
                    const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
                    let event = 'message', data = '';
                    for (const line of frame.split('\n')) {
                        if (line.startsWith('event:')) event = line.slice(6).trim();
                        else if (line.startsWith('data:')) data += line.slice(5).trim();
                    }
                    if (event === 'done' || event === 'error') sawTerminal = true;
                    let parsed = data;
                    try { parsed = JSON.parse(data); } catch (_) {}
                    onEvent(event, parsed);
                }
            }
        } finally {
            if (watchdog) watchdog.cancel();
        }

        if (requireTerminalFrame && !sawTerminal && !(externalSignal && externalSignal.aborted)) {
            throw abruptFail('З’єднання завершилося без відповіді сервера');
        }
    }

    // — Generic POST-SSE reader (fetch + ReadableStream, NOT EventSource).
    //   EventSource poisons werkzeug keep-alive (recall_sse_connection_gotcha)
    //   and can't POST a body anyway. Resolves when the stream closes cleanly
    //   with a terminal frame; rejects (and emits a synthetic `error` event
    //   first) on an abrupt disconnect. —
    // signature: postSseStream(url, payload, onEvent, signal?, opts?)
    //   opts.idleTimeoutMs        — see readSseBody
    //   opts.requireTerminalFrame — default TRUE here (one-shot request/response)
    async function postSseStream(url, payload, onEvent, signal, opts) {
        opts = opts || {};
        const internalAc = new AbortController();
        const combined = combineSignals([signal, internalAc.signal]);
        let res;
        try {
            res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
                body: JSON.stringify(payload),
                signal: combined,
            });
        } catch (err) {
            if (signal && signal.aborted) return;   // aborted before we even got a response
            throw err;
        }
        return readSseBody(res, onEvent, {
            externalSignal: signal,
            internalAc,
            idleTimeoutMs: opts.idleTimeoutMs,
            requireTerminalFrame: opts.requireTerminalFrame !== false,
        });
    }
    A.postSseStream = postSseStream;

    // — SSE helper: POST stream for RAG ask (thin wrapper over postSseStream).
    //   signature: askStream(payload, onEvent, signal?, opts?) — signal lets a
    //   caller-owned AbortController really cancel server-side generation
    //   (saves paid Claude tokens), matching the researchSummary pattern. —
    A.askStream = (payload, onEvent, signal, opts) => postSseStream('/api/memory/ask/stream', payload, onEvent, signal, opts);

    // — Research / big export (зібрати всі згадки бренду з усіх джерел) —
    A.researchPreview = (params) => getJSON('/api/research/preview' + qs(params));
    A.researchOriginals = (body) => A.post('/api/research/originals', body);
    A.researchSummary = (payload, onEvent, signal, opts) => postSseStream('/api/research/summary', payload, onEvent, signal, opts);

    // — Generic GET-SSE reader (fetch + ReadableStream, NOT EventSource) —
    //   EventSource poisons the werkzeug dev-server keep-alive connection (see
    //   recall_sse_connection_gotcha). This reader takes an AbortSignal so the
    //   caller can tear it down cleanly with controller.abort() — no
    //   auto-reconnect here (see A.sseReconnecting below for that).
    //   signature: sseStream(url, onEvent, signal?, opts?)
    //   opts.idleTimeoutMs        — see readSseBody
    //   opts.requireTerminalFrame — default FALSE here (many GET streams, e.g.
    //     /api/recording/<sid>/stream, are long-lived and never send done/error)
    A.sseStream = async function (url, onEvent, signal, opts) {
        opts = opts || {};
        const internalAc = new AbortController();
        const combined = combineSignals([signal, internalAc.signal]);
        let res;
        try {
            res = await fetch(url, { headers: { 'Accept': 'text/event-stream' }, signal: combined });
        } catch (err) {
            if (signal && signal.aborted) return;
            throw err;
        }
        return readSseBody(res, onEvent, {
            externalSignal: signal,
            internalAc,
            idleTimeoutMs: opts.idleTimeoutMs,
            requireTerminalFrame: !!opts.requireTerminalFrame,
        });
    };

    // — Reusable "SSE with auto-reconnect + backoff" helper, built on top of
    //   A.sseStream. NOT wired to any specific endpoint — a view (e.g.
    //   record.js, for /api/recordings/active) calls this instead of
    //   sseStream directly when it wants the connection to keep re-attaching
    //   itself across drops instead of just dying once.
    //
    //   A.sseReconnecting(url, onEvent, options) → { stop() }
    //   options:
    //     signal               — external AbortSignal; aborting it stops
    //                             reconnecting permanently (no more callbacks).
    //     baseDelayMs = 1000, maxDelayMs = 20000  — exponential backoff bounds.
    //     maxRetries  = Infinity                  — cap consecutive failures;
    //                             once exceeded, onPermanentFail() fires.
    //     idleTimeoutMs, requireTerminalFrame      — forwarded to each sseStream attempt.
    //     onReconnecting(attempt, delayMs)         — about to retry after a drop.
    //     onReconnected()                          — first event received after
    //                             >=1 prior failed attempt (i.e. we're back).
    //     onPermanentFail(err)                     — maxRetries exceeded, or a
    //                             non-retryable HTTP error (401/403/404).
    //   Returns { stop() } so the caller doesn't need to manage its own
    //   AbortController just to cancel this permanently.
    function sseReconnecting(url, onEvent, options) {
        options = options || {};
        const baseDelayMs = options.baseDelayMs || 1000;
        const maxDelayMs = options.maxDelayMs || 20000;
        const maxRetries = options.maxRetries != null ? options.maxRetries : Infinity;
        const ownAc = new AbortController();
        const outerSignal = combineSignals([options.signal, ownAc.signal]);
        let stopped = false;
        let reconnectAttempt = 0;

        function nonRetryable(err) { return !!(err && (err.status === 401 || err.status === 403 || err.status === 404)); }

        (async function loop() {
            while (!stopped && !(outerSignal && outerSignal.aborted)) {
                let firedThisAttempt = false;
                try {
                    await A.sseStream(url, (event, data) => {
                        if (reconnectAttempt > 0 && !firedThisAttempt && options.onReconnected) options.onReconnected();
                        firedThisAttempt = true;
                        reconnectAttempt = 0;
                        onEvent(event, data);
                    }, outerSignal, { idleTimeoutMs: options.idleTimeoutMs, requireTerminalFrame: options.requireTerminalFrame });
                } catch (err) {
                    if (outerSignal && outerSignal.aborted) return;
                    if (nonRetryable(err)) { if (options.onPermanentFail) options.onPermanentFail(err); return; }
                }
                if (stopped || (outerSignal && outerSignal.aborted)) return;
                reconnectAttempt++;
                if (reconnectAttempt > maxRetries) {
                    if (options.onPermanentFail) options.onPermanentFail(new Error('Досягнуто максимум спроб перепідключення'));
                    return;
                }
                const delay = Math.min(maxDelayMs, baseDelayMs * Math.pow(2, reconnectAttempt - 1));
                if (options.onReconnecting) options.onReconnecting(reconnectAttempt, delay);
                await sleep(delay);
            }
        })();

        return { stop() { stopped = true; try { ownAc.abort(); } catch (_) {} } };
    }
    A.sseReconnecting = sseReconnecting;

    window.Recall.api = A;
})();
