/* Recall — "Додати у архів · YouTube". Native ingest page.

   Flow (mirrors the legacy app.js YouTube pipeline, but as a real page):
     1. paste URL → POST /api/youtube/info → preview card + transcribe options
     2. POST /api/youtube/download → download_id, poll /api/youtube/progress
     3. on completed → POST /api/transcribe (blocking) with the chosen options
        while streaming live segments via SSE /api/events/<download_id>
     4. result.transcription_id → navigate to the native /transcript page.

   The finished record always lands in History/transcriptions — see the
   transcript_location_ux note: YouTube goes to Історія, not Аудіотека. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const LANGS = [
        { v: 'uk', l: 'capture.lang.uk' },
        { v: 'en', l: 'capture.lang.en' },
        { v: 'ru', l: 'capture.lang.ru' },
        { v: 'auto', l: 'capture.lang.auto' },
    ];
    const DEFAULT_MODEL = 'large-v3';

    let st = null;        // { url, info, opts, busy }
    let _poll = null;     // download-progress poll timer
    let _models = null;   // cached /api/models result
    // T5.6: bgJob — a background transcription started by THIS view (module scope —
    // survives render()/destroy() within the tab, reset only by a full
    // page reload). Until it finishes (success or error),
    // a repeated visit to /youtube shows a status badge instead of the form — this is the
    // guard against a duplicate task (audit: a repeated click/visit duplicates the work).
    let bgJob = null;      // { label }

    function cleanup() {
        if (_poll) { clearTimeout(_poll); _poll = null; }
    }

    function bgNoticeHTML(label) {
        return `<div class="rc-note">
            <div class="rc-note__title"><i class="fa-solid fa-cloud-arrow-up"></i> ${U.esc(R.t('capture.common.bgTitle', { label }))}</div>
            <p>${R.t('capture.common.bgText')}</p>
            <div class="rc-note__actions">
                <button class="rc-btn rc-btn--primary" id="rcYtGoHistory">${R.t('capture.common.goHistory')}</button>
            </div>
        </div>`;
    }
    function wireBgNotice(root) {
        if (!root) return;
        const btn = root.querySelector('#rcYtGoHistory');
        if (btn) btn.addEventListener('click', () => R.router.navigate('/library'));
    }

    async function loadModels() {
        if (_models) return _models;
        try {
            const data = await R.api.models();
            _models = Array.isArray(data) ? data : (data.models || []);
        } catch (_) { _models = []; }
        return _models;
    }

    // ---- trim helpers ------------------------------------------------------
    // Accepts "ss", "mm:ss", or "h:mm:ss" → seconds (NaN if malformed).
    function parseTime(s) {
        s = (s || '').trim();
        if (!s) return NaN;
        if (/^\d+$/.test(s)) return parseInt(s, 10);
        const parts = s.split(':').map(x => x.trim());
        if (parts.some(p => p === '' || !/^\d+$/.test(p))) return NaN;
        let sec = 0;
        for (const p of parts) sec = sec * 60 + parseInt(p, 10);
        return sec;
    }

    // {start,end} seconds if trim is on & valid; null if off; throws Error if invalid.
    function readTrim() {
        const chk = document.getElementById('rcYtTrim');
        if (!chk || !chk.checked) return null;
        const from = parseTime((document.getElementById('rcYtFrom') || {}).value);
        const to = parseTime((document.getElementById('rcYtTo') || {}).value);
        const dur = st && st.info && st.info.duration;
        if (!Number.isFinite(from) || !Number.isFinite(to)) throw new Error(R.t('capture.yt.errTimes'));
        if (to <= from) throw new Error(R.t('capture.yt.errOrder'));
        if (dur && to > dur + 1) throw new Error(R.t('capture.yt.errBeyond', { dur: U.fmtDuration(dur) }));
        return { start: from, end: to };
    }

    function updateTrimNote() {
        const note = document.getElementById('rcYtTrimNote');
        const chk = document.getElementById('rcYtTrim');
        if (!note || !chk) return;
        const fromRaw = ((document.getElementById('rcYtFrom') || {}).value || '').trim();
        const toRaw = ((document.getElementById('rcYtTo') || {}).value || '').trim();
        if (!fromRaw && !toRaw) {
            note.innerHTML = R.t('capture.yt.trimFormat');
            note.classList.remove('is-err');
            return;
        }
        try {
            const t = readTrim();
            note.innerHTML = R.t('capture.yt.trimFragment', { from: U.fmtDuration(t.start), to: U.fmtDuration(t.end), len: U.fmtDuration(t.end - t.start) });
            note.classList.remove('is-err');
        } catch (e) {
            note.textContent = e.message;
            note.classList.add('is-err');
        }
    }

    // ---- render: page shell + URL bar -------------------------------------
    async function render(ctx) {
        cleanup();
        st = { url: ctx.query.url || '', info: null, opts: null, busy: false };

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-brands fa-youtube" style="color:var(--rc-src-youtube)"></i> ${R.t('capture.yt.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('capture.yt.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('capture.yt.lede')}</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-brands fa-youtube"></i>
                    <input id="rcYtUrl" type="url" inputmode="url" autocomplete="off" spellcheck="false"
                           placeholder="https://youtube.com/watch?v=…" value="${U.esc(st.url)}">
                </div>
                <button class="rc-btn rc-btn--primary" id="rcYtFetch"><i class="fa-solid fa-arrow-right"></i> ${R.t('capture.yt.fetch')}</button>
            </div>
            <div id="rcYtBody"></div>`;

        const urlInput = ctx.mount.querySelector('#rcYtUrl');
        const fetchBtn = ctx.mount.querySelector('#rcYtFetch');
        const go = () => fetchInfo(ctx, urlInput.value);
        fetchBtn.addEventListener('click', go);
        urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });

        // T5.6: a background transcription from a previous visit is still going — we do not let
        // a second one start (for the same or another video) until it finishes.
        if (bgJob) {
            urlInput.disabled = true; fetchBtn.disabled = true;
            const body = ctx.mount.querySelector('#rcYtBody');
            body.innerHTML = bgNoticeHTML(bgJob.label);
            wireBgNotice(body);
            return;
        }

        urlInput.focus();
        loadModels();                       // warm cache; non-blocking
        if (st.url.trim()) fetchInfo(ctx, st.url);
    }

    // ---- step 1: metadata --------------------------------------------------
    async function fetchInfo(ctx, url) {
        url = (url || '').trim();
        const body = ctx.mount.querySelector('#rcYtBody');
        if (!body) return;
        if (!url) { body.innerHTML = ''; return; }
        if (!/youtu\.?be/i.test(url)) UI.toast(R.t('capture.yt.notYoutube'), 'error');

        st.url = url; st.info = null; st.busy = false;
        body.innerHTML = `<div class="rc-yt__loading rc-mono"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('capture.yt.loading')}</div>`;
        // reflect into the URL for deep-linking / refresh, without a re-render
        R.router.replace('/youtube?url=' + encodeURIComponent(url));

        try {
            const info = await R.api.youtubeInfo(url);
            if (!ctx.isCurrent()) return;
            if (!info || info.success === false) {
                body.innerHTML = UI.error((info && info.error) || R.t('capture.yt.fetchFailed'));
                return;
            }
            st.info = info;
            await renderInfo(ctx, info);
        } catch (err) {
            if (ctx.isCurrent()) body.innerHTML = UI.error(err && err.message);
        }
    }

    // ---- step 1b: preview card + transcription options --------------------
    async function renderInfo(ctx, info) {
        await loadModels();
        const cats = await UI.loadCategories();
        if (!ctx.isCurrent()) return;
        const body = ctx.mount.querySelector('#rcYtBody');
        if (!body) return;

        const modelOpts = (_models.length ? _models : [{ name: DEFAULT_MODEL, info: {} }]).map(m => {
            const inf = m.info || {};
            const hint = [inf.size, inf.speed].filter(Boolean).join(', ');
            const sel = m.name === DEFAULT_MODEL ? ' selected' : '';
            return `<option value="${U.esc(m.name)}"${sel}>${U.esc(m.name)}${hint ? ' — ' + U.esc(hint) : ''}</option>`;
        }).join('');
        const langOpts = LANGS.map(l => `<option value="${l.v}"${l.v === 'uk' ? ' selected' : ''}>${R.t(l.l)}</option>`).join('');
        const catOpts = UI.catOptions('', { first: 'none' });

        const thumb = info.thumbnail
            ? `<img class="rc-yt__thumb" src="${U.esc(info.thumbnail)}" alt="" loading="lazy">`
            : `<div class="rc-yt__thumb rc-yt__thumb--ph"><i class="fa-brands fa-youtube"></i></div>`;

        body.innerHTML = `
            <div class="rc-yt">
                <div class="rc-yt__preview">
                    ${thumb}
                    <div class="rc-yt__meta">
                        <div class="rc-eyebrow">${R.t('capture.yt.video')}</div>
                        <div class="rc-yt__title">${U.esc(info.title || R.t('capture.yt.noTitle'))}</div>
                        <div class="rc-yt__sub rc-mono">${U.esc(info.author || '')}${info.duration ? ' · ' + U.fmtDuration(info.duration) : ''}</div>
                        ${info.description ? `<p class="rc-yt__desc">${U.esc(info.description)}</p>` : ''}
                    </div>
                </div>
                <div class="rc-yt__settings">
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtTitle">${R.t('capture.common.titleOpt')}</label>
                        <input class="rc-input" id="rcYtTitle" autocomplete="off" maxlength="200" placeholder="${U.esc(info.title || R.t('capture.yt.asInVideo'))}">
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtDesc">${R.t('capture.common.descOpt')}</label>
                        <textarea class="rc-textarea" id="rcYtDesc" maxlength="4000" placeholder="${R.t('capture.common.descPh')}"></textarea>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtModel">${R.t('capture.common.model')}</label>
                        <select class="rc-select" id="rcYtModel">${modelOpts}</select>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtLang">${R.t('capture.common.language')}</label>
                        <select class="rc-select" id="rcYtLang">${langOpts}</select>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtCat">${R.t('capture.common.category')}</label>
                        <select class="rc-select rc-catsel" id="rcYtCat" data-cat-first="none">${catOpts}</select>
                    </div>
                    <label class="rc-check" for="rcYtDiar">
                        <input type="checkbox" id="rcYtDiar"> <span>${R.t('capture.common.diarize')}</span>
                    </label>
                    <label class="rc-check" for="rcYtTrim">
                        <input type="checkbox" id="rcYtTrim"> <span>${R.t('capture.yt.trim')}</span>
                    </label>
                    <div class="rc-yt__trim" id="rcYtTrimFields" hidden>
                        <div class="rc-field">
                            <label class="rc-field__label" for="rcYtFrom">${R.t('capture.yt.from')}</label>
                            <input class="rc-input rc-input--sm" id="rcYtFrom" inputmode="numeric" autocomplete="off" placeholder="0:00">
                        </div>
                        <div class="rc-field">
                            <label class="rc-field__label" for="rcYtTo">${R.t('capture.yt.to')}</label>
                            <input class="rc-input rc-input--sm" id="rcYtTo" inputmode="numeric" autocomplete="off" placeholder="${info.duration ? U.fmtDuration(info.duration) : R.t('capture.yt.timePh')}">
                        </div>
                        <div class="rc-yt__trimnote rc-mono" id="rcYtTrimNote">${R.t('capture.yt.trimFormat')}</div>
                    </div>
                </div>
                <div class="rc-yt__actions">
                    <button class="rc-btn" id="rcYtBack"><i class="fa-solid fa-arrow-left"></i> ${R.t('capture.yt.another')}</button>
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcYtStart"><i class="fa-solid fa-wand-magic-sparkles"></i> ${R.t('capture.common.transcribeSave')}</button>
                </div>
            </div>
            <div id="rcYtRun"></div>`;

        body.querySelector('#rcYtStart').addEventListener('click', () => start(ctx));

        // trim toggle + live validation
        const trimChk = body.querySelector('#rcYtTrim');
        const trimFields = body.querySelector('#rcYtTrimFields');
        const fromEl = body.querySelector('#rcYtFrom');
        const toEl = body.querySelector('#rcYtTo');
        trimChk.addEventListener('change', () => {
            trimFields.hidden = !trimChk.checked;
            if (trimChk.checked) fromEl.focus();
            updateTrimNote();
        });
        [fromEl, toEl].forEach(el => el.addEventListener('input', updateTrimNote));

        body.querySelector('#rcYtBack').addEventListener('click', () => {
            cleanup();
            st.info = null; body.innerHTML = '';
            const u = ctx.mount.querySelector('#rcYtUrl'); if (u) { u.value = ''; u.focus(); }
            R.router.replace('/youtube');
        });
    }

    function setSettingsDisabled(ctx, disabled) {
        ctx.mount.querySelectorAll('#rcYtTitle,#rcYtDesc,#rcYtModel,#rcYtLang,#rcYtCat,#rcYtDiar,#rcYtTrim,#rcYtFrom,#rcYtTo,#rcYtStart,#rcYtBack')
            .forEach(e => { e.disabled = disabled; });
    }

    // ---- step 2: download --------------------------------------------------
    async function start(ctx) {
        if (!st || !st.info || st.busy) return;
        const model = (ctx.mount.querySelector('#rcYtModel') || {}).value || DEFAULT_MODEL;
        const language = (ctx.mount.querySelector('#rcYtLang') || {}).value || 'uk';
        const category_id = (ctx.mount.querySelector('#rcYtCat') || {}).value || '';
        const diarize = !!(ctx.mount.querySelector('#rcYtDiar') || {}).checked;
        const title = ((ctx.mount.querySelector('#rcYtTitle') || {}).value || '').trim();
        const description = ((ctx.mount.querySelector('#rcYtDesc') || {}).value || '').trim();
        let trim;
        try { trim = readTrim(); }
        catch (e) { UI.toast(e.message, 'error'); return; }
        st.opts = { model, language, category_id, diarize, trim, title, description };
        st.busy = true;
        setSettingsDisabled(ctx, true);

        const run = ctx.mount.querySelector('#rcYtRun');
        if (run) run.innerHTML = runHTML();

        try {
            const dl = { url: st.url };
            if (trim) { dl.start_time = trim.start; dl.end_time = trim.end; }
            const r = await R.api.youtubeDownload(dl);
            if (!ctx.isCurrent()) return;
            if (!r || !r.download_id) { fail(ctx, (r && r.error) || R.t('capture.yt.startFailed')); return; }
            pollDownload(ctx, r.download_id);
        } catch (err) {
            if (ctx.isCurrent()) fail(ctx, err && err.message);
        }
    }

    function runHTML() {
        return `<div class="rc-run">
            <div class="rc-run__head">
                <span class="rc-run__stage" id="rcRunStage">${R.t('capture.yt.prepare')}</span>
                <span class="rc-run__pct rc-mono" id="rcRunPct">0%</span>
            </div>
            <div class="rc-progress"><div class="rc-progress__fill" id="rcRunFill"></div></div>
            <div class="rc-run__hint rc-mono" id="rcRunHint"></div>
        </div>`;
    }

    function setProgress(pct, indeterminate) {
        const p = document.getElementById('rcRunPct'), f = document.getElementById('rcRunFill');
        if (f) {
            f.classList.toggle('is-indeterminate', !!indeterminate);
            if (!indeterminate && typeof pct === 'number') f.style.width = Math.max(0, Math.min(100, pct)) + '%';
        }
        if (p) p.textContent = indeterminate ? '' : (typeof pct === 'number' ? Math.round(pct) + '%' : '');
    }
    function setStage(text) { const s = document.getElementById('rcRunStage'); if (s && text) s.textContent = text; }
    function setHint(text) { const h = document.getElementById('rcRunHint'); if (h) h.textContent = text || ''; }

    async function pollDownload(ctx, id) {
        try {
            const p = await R.api.youtubeProgress(id);
            if (!ctx.isCurrent()) return;
            const status = p.status;
            if (status === 'downloading') {
                setStage(R.t('capture.yt.downloadingAudio'));
                setProgress(Math.round(p.percent || 0), false);
                setHint([p.speed, p.eta && ('ETA ' + p.eta)].filter(Boolean).join('  ·  '));
                _poll = setTimeout(() => pollDownload(ctx, id), 600);
            } else if (status === 'processing') {
                setStage(R.t('capture.yt.processingAudio')); setProgress(100, false); setHint(R.t('capture.yt.convertMp3'));
                _poll = setTimeout(() => pollDownload(ctx, id), 600);
            } else if (status === 'starting') {
                setStage(R.t('capture.yt.prepare')); setProgress(0, false); setHint(R.t('capture.yt.startingDownload'));
                _poll = setTimeout(() => pollDownload(ctx, id), 600);
            } else if (status === 'completed') {
                transcribe(ctx, id);
            } else if (status === 'error') {
                fail(ctx, p.error || R.t('capture.yt.downloadFailed'));
            } else {
                _poll = setTimeout(() => pollDownload(ctx, id), 1000);
            }
        } catch (err) {
            fail(ctx, err && err.message);
        }
    }

    // ---- step 3: transcribe (fire-and-forget; result lands in History) ----
    //   T5.6: here there used to be `await R.api.transcribe(fd)`, which kept the tab
    //   "blocked" for the whole transcription (files up to 20GB) with no
    //   way to learn that it is fine to leave — and if the tab was closed,
    //   the user did not know whether the task was saved at all. The server runs
    //   /api/transcribe synchronously in the request thread regardless of the client
    //   connection (the same fact the 504 fallback in record.js save() is built on)
    //   — so a dropped connection does NOT lose the work, only the visibility.
    //   So now: we do not wait for the promise for the UI, we immediately show the status badge
    //   «можна закрити вкладку», and `.then()/.catch()` finish the UI ONLY if the
    //   view is still active (bgJob — a module-scope guard against a double start).
    //
    //   NOTE: we deliberately do NOT open an EventSource on /api/events/<id> for
    //   live segments. The Werkzeug dev server leaves the keep-alive connection in a
    //   broken state after a GET-SSE stream, which stalls the next request on the
    //   poisoned connection — namely the transcript page's /api/history/<id>,
    //   leaving it "hanging" on the skeleton. Live word-streaming is nice-to-have,
    //   correctness matters more. A future revival should use a
    //   fetch+ReadableStream reader with an AbortController (see api.askStream), and
    //   not EventSource.
    function transcribe(ctx, id) {
        const o = st.opts || {};
        const label = (st.info && st.info.title) || st.url;
        const fd = new FormData();
        fd.append('source_type', 'youtube');
        fd.append('download_id', id);
        fd.append('model', o.model || DEFAULT_MODEL);
        fd.append('language', o.language || 'uk');
        if (o.diarize) fd.append('diarize', 'true');
        if (o.category_id) fd.append('category_id', o.category_id);
        if (o.title) fd.append('title', o.title);
        if (o.description) fd.append('description', o.description);

        bgJob = { label };
        const run = ctx.mount.querySelector('#rcYtRun');
        if (run) { run.innerHTML = bgNoticeHTML(label); wireBgNotice(run); }
        UI.toast(R.t('capture.yt.downloadedBg', { label }), 'info');

        R.api.transcribe(fd).then((result) => {
            bgJob = null;
            UI.toast(R.t('capture.common.done', { label }), 'success');
            if (!ctx.isCurrent()) return;
            cleanup();
            st.busy = false;
            setSettingsDisabled(ctx, false);
            const tid = result.transcription_id;
            const name = result.source_name || label;
            if (tid) R.router.navigate('/transcript/' + U.slug(tid, name));
            else R.router.navigate('/library');
        }).catch((err) => {
            bgJob = null;
            UI.toast(R.t('capture.common.failed', { label, error: (err && err.message) || R.t('capture.common.errorWord') }), 'error');
            if (!ctx.isCurrent()) return;
            fail(ctx, err && err.message);
        });
    }

    function fail(ctx, msg) {
        cleanup();
        bgJob = null;
        if (!ctx.isCurrent()) return;
        st.busy = false;
        setSettingsDisabled(ctx, false);
        const run = ctx.mount.querySelector('#rcYtRun');
        if (run) {
            run.innerHTML = UI.error(msg || R.t('capture.common.error'))
                + `<div class="rc-yt__retry"><button class="rc-btn" id="rcYtRetry"><i class="fa-solid fa-rotate-right"></i> ${R.t('capture.common.retry')}</button></div>`;
            const retry = run.querySelector('#rcYtRetry');
            if (retry) retry.addEventListener('click', () => start(ctx));
        }
        const m = String(msg || '');
        if (/cookies/i.test(m)) UI.toast(R.t('capture.yt.needCookies'), 'error');
        else if (/ffmpeg/i.test(m)) UI.toast(R.t('capture.yt.noFfmpeg'), 'error');
    }

    R.views.youtube = { render, destroy() { cleanup(); st = null; } };
})();
