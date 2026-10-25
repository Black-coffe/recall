/* Recall — "Медіатека". Browser over audio_downloads: downloaded YouTube audio
   and saved server recordings (the /record "save only" path lands here). Per
   row: play in the system player, reveal in Explorer, transcribe (→ History /
   /transcript) or open the existing transcript, delete. Video recordings show
   a 📹 Відео badge (has_video = 1).

   Filters / search / page live in the URL (deep-linkable), like /library. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const T = (k, v) => R.t(k, v);
    const PER_PAGE = 20;
    const DEFAULT_MODEL = 'large-v3';

    const SOURCES = [
        { k: '', label: T('library.aud.src.all'), c: 'all' },
        { k: 'youtube', label: 'YouTube', c: 'youtube' },
        { k: 'recording', label: T('library.aud.src.recording'), c: 'recording' },
        { k: 'file', label: T('library.aud.src.file'), c: 'file' },
    ];
    const LANGS = [
        { v: 'auto', l: T('library.aud.lang.auto') }, { v: 'uk', l: T('library.aud.lang.uk') },
        { v: 'en', l: T('library.aud.lang.en') }, { v: 'ru', l: T('library.aud.lang.ru') },
    ];

    // C9: a hint shared by the screen + explanation of an empty result (the same
    // text as in library.js — duplicated on purpose, we do not build a shared filter-bar
    // component outside 09).
    const FILTER_HINT = T('library.filterHint');

    function filterExplain(st) {
        if (!st.category_id && !st.tags.length && !st.since && !st.until) return null;
        const bits = [];
        if (st.category_id && UI.catPath) { const p = UI.catPath(Number(st.category_id)); if (p) bits.push(T('library.lib.explain.in', { path: p })); }
        if (st.tags.length) bits.push(T('library.lib.explain.tags', { tags: st.tags.join(', ') }));
        if (st.since || st.until) bits.push(T('library.lib.explain.period', { from: st.since || '…', to: st.until || '…' }));
        if (!bits.length) return null;
        return T('library.aud.explain.none', { bits: bits.join(' ') });
    }

    let listEl = null, state = null, _models = null, cats = null;
    // Phase 21: background transcription status. activeIds — ids of records that are being
    // transcribed right now (from /api/transcribe/active); lastActive — the last snapshot
    // (to mark freshly rendered rows right after load).
    let pollTimer = null, activeIds = new Set(), lastActive = [];
    // T5.6: pendingIds — ids for which we ourselves JUST sent /api/transcribe
    // (fire-and-forget) and the server has not yet had time to show them in /api/transcribe/active
    // (polling once per 2.5s). Gives an instant pill without delay and stops a repeated
    // click on the same row from starting a second transcription at the same moment.
    let pendingIds = new Set();
    // Re-transcribe runs started here: audio download id -> model; stops of their job polls.
    let rtRunning = new Map(), rtStops = [];
    const RT_ERR = {
        NO_MEDIA: 'library.rt.err.NO_MEDIA', MODEL_NOT_INSTALLED: 'library.rt.err.MODEL_NOT_INSTALLED',
        RECORDING_ACTIVE: 'library.rt.err.RECORDING_ACTIVE', ALREADY_RUNNING: 'library.rt.err.ALREADY_RUNNING',
    };

    async function loadModels() {
        if (_models) return _models;
        try { const d = await R.api.models(); _models = Array.isArray(d) ? d : (d.models || []); }
        catch (_) { _models = []; }
        return _models;
    }

    async function render(ctx) {
        cats = await UI.loadCategories();
        if (UI.loadTags) await UI.loadTags();
        state = {
            search: ctx.query.search || '',
            source_type: ctx.query.source_type || '',
            category_id: ctx.query.category_id || '',
            tags: (ctx.query.tags || '').split(',').map(s => s.trim()).filter(Boolean),
            since: ctx.query.since || '',
            until: ctx.query.until || '',
            // '0' — only untranscribed. The main job on this page:
            // find what is not yet turned into text.
            transcribed: ctx.query.transcribed === '0' ? '0' : '',
            page: parseInt(ctx.query.page, 10) || 1,
        };
        loadModels();

        const catOpts = UI.catOptions(state.category_id, { first: 'all' });
        const tagSel = UI.tagOptions ? UI.tagOptions(state.tags) : '<select multiple class="rc-select rc-select--sm" disabled></select>';

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-compact-disc"></i> ${T('library.aud.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${T('library.aud.title')}</h1>
                <p class="rc-pagehead__lede">${T('library.aud.lede')}</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcAudSearch" type="search" placeholder="${U.esc(T('library.aud.searchPh'))}" value="${U.esc(state.search)}">
                </div>
                <select class="rc-select rc-catsel" id="rcAudCat" data-cat-first="all">${catOpts}</select>
            </div>
            <div class="rc-filterbar" id="rcAudSources"></div>
            <div class="rc-toolbar" id="rcAudTagPeriod">
                <div id="rcAudTagsWrap">${tagSel}</div>
                <input type="date" class="rc-input rc-select--sm" id="rcAudSince" value="${U.esc(state.since)}" title="${U.esc(T('library.lib.sinceTitle'))}">
                <input type="date" class="rc-input rc-select--sm" id="rcAudUntil" value="${U.esc(state.until)}" title="${U.esc(T('library.lib.untilTitle'))}">
            </div>
            <div id="rcAudAsk"></div>
            <div id="rcAudHint"></div>
            <div class="rc-list" id="rcAudList"></div>
            <div id="rcAudPager"></div>`;

        listEl = ctx.mount.querySelector('#rcAudList');
        const sb = ctx.mount.querySelector('#rcAudSources');
        sb.addEventListener('click', (e) => {
            const b = e.target.closest('.rc-chip'); if (!b) return;
            state.page = 1;
            if (b.dataset.tr !== undefined) state.transcribed = state.transcribed === '0' ? '' : '0';
            else state.source_type = b.dataset.src;
            sync(); load(ctx);
        });
        const catSel = ctx.mount.querySelector('#rcAudCat');
        catSel.value = state.category_id;
        catSel.addEventListener('change', () => { state.category_id = catSel.value; state.page = 1; sync(); load(ctx); });

        const tagSelEl = ctx.mount.querySelector('#rcAudTagsWrap select');
        if (tagSelEl) { tagSelEl.setAttribute('aria-label', T('library.lib.tagsAria')); tagSelEl.title = T('library.lib.tagsTitle'); }
        if (tagSelEl) tagSelEl.addEventListener('change', () => {
            state.tags = Array.from(tagSelEl.selectedOptions).map(o => o.value).filter(Boolean);
            state.page = 1; sync(); load(ctx);
        });
        const sinceEl = ctx.mount.querySelector('#rcAudSince');
        const untilEl = ctx.mount.querySelector('#rcAudUntil');
        sinceEl.addEventListener('change', () => { state.since = sinceEl.value; state.page = 1; sync(); load(ctx); });
        untilEl.addEventListener('change', () => { state.until = untilEl.value; state.page = 1; sync(); load(ctx); });

        const hintBox = ctx.mount.querySelector('#rcAudHint');
        if (hintBox && UI.hint) hintBox.appendChild(UI.hint(FILTER_HINT));

        const search = ctx.mount.querySelector('#rcAudSearch');
        search.addEventListener('input', U.debounce(() => { state.search = search.value.trim(); state.page = 1; sync(); load(ctx); }, 280));

        load(ctx);
        loadAsk(ctx);
    }

    // recording-autosave: recordings the autosave could not file ask for a direction here,
    // one click on a k-NN candidate or a pick from the full list.
    async function loadAsk(ctx) {
        const box = ctx.mount.querySelector('#rcAudAsk');
        if (!box) return;
        let items = [];
        try { const r = await R.api.get('/api/recordings/uncategorized'); items = (r && r.items) || []; }
        catch (_) { items = []; }
        if (!ctx.isCurrent()) return;
        box.innerHTML = items.map(it => `
            <div class="rc-recwarn rc-recwarn--ask" data-ask="${it.transcription_id}">
                <span><i class="fa-solid fa-folder-open"></i> ${U.esc(T('library.aud.ask.text', { title: it.title || '' }))}</span>
                <span class="rc-yt__spacer"></span>
                ${(it.candidates || []).map(c => `<button type="button" class="rc-chip" data-cat="${c.category_id}">${U.esc(c.name)}</button>`).join('')}
                <select class="rc-select rc-select--sm" data-ask-sel aria-label="${U.esc(T('library.aud.ask.pick'))}"><option value="">${U.esc(T('library.aud.ask.pick'))}</option>${UI.catOptions(null, { first: 'hide', newOpt: false })}</select>
            </div>`).join('');
        box.querySelectorAll('[data-ask]').forEach(row => {
            const tid = Number(row.dataset.ask);
            const pick = async (cid) => {
                if (!cid) return;
                try { await R.api.setCategory(tid, Number(cid)); }
                catch (err) { UI.toast((err && err.message) || T('library.aud.ask.failed'), 'error'); return; }
                row.remove();
                UI.toast(T('library.aud.ask.done'), 'success');
                load(ctx);
            };
            row.querySelectorAll('[data-cat]').forEach(b => b.addEventListener('click', () => pick(b.dataset.cat)));
            const sel = row.querySelector('[data-ask-sel]');
            sel.addEventListener('change', () => pick(sel.value));
        });
    }

    function sync() {
        const p = new URLSearchParams();
        if (state.search) p.set('search', state.search);
        if (state.source_type) p.set('source_type', state.source_type);
        if (state.category_id) p.set('category_id', state.category_id);
        if (state.tags.length) p.set('tags', state.tags.join(','));
        if (state.since) p.set('since', state.since);
        if (state.until) p.set('until', state.until);
        if (state.transcribed === '0') p.set('transcribed', '0');
        if (state.page > 1) p.set('page', state.page);
        const q = p.toString();
        R.router.replace('/audio' + (q ? '?' + q : ''));
    }

    function renderChips(counts) {
        const sb = document.getElementById('rcAudSources');
        if (!sb) return;
        const src = SOURCES.map(s => {
            const n = counts ? (counts[s.c] || 0) : null;
            return `<button class="rc-chip${s.k === state.source_type ? ' is-active' : ''}" data-src="${s.k}">${s.label}${n != null ? ` <span class="rc-chip__n">${n}</span>` : ''}</button>`;
        }).join('');
        // The "remaining work" slice — separate from the sources, because it is a different filter axis
        // (not "where it came from" but "whether it is already processed").
        const un = counts ? (counts.untranscribed || 0) : 0;
        const trChip = un || state.transcribed === '0'
            ? `<span class="rc-filterbar__sep" aria-hidden="true"></span>
               <button class="rc-chip${state.transcribed === '0' ? ' is-active' : ''}" data-tr="1"
                       title="${U.esc(T('library.aud.noTranscriptTitle'))}">${T('library.aud.noTranscript')} <span class="rc-chip__n">${un}</span></button>`
            : '';
        sb.innerHTML = src + trChip;
    }

    async function load(ctx) {
        if (!listEl) return;
        listEl.innerHTML = UI.skeletonList(6);
        const pagerEl = ctx.mount.querySelector('#rcAudPager');
        if (pagerEl) pagerEl.innerHTML = '';
        try {
            const data = await R.api.audioDownloads({
                page: state.page, per_page: PER_PAGE,
                search: state.search || undefined,
                source_type: state.source_type || undefined,
                category_id: state.category_id || undefined,
                tags: state.tags.length ? state.tags.join(',') : undefined,
                since: state.since || undefined,
                until: state.until || undefined,
                transcribed: state.transcribed === '0' ? '0' : undefined,
            });
            if (!ctx.isCurrent()) return;
            renderChips(data.counts);
            const rows = data.downloads || [];
            if (!rows.length) {
                const explain = filterExplain(state);
                if (state.transcribed === '0') {
                    listEl.innerHTML = UI.empty(T('library.aud.empty.doneTitle'),
                        explain ? T('library.aud.empty.doneBody') + ' ' + explain
                                : T('library.aud.empty.doneBody'), 'fa-circle-check');
                } else {
                    listEl.innerHTML = UI.empty(T('library.aud.empty.title'),
                        explain || (state.search || state.source_type ? T('library.aud.empty.change') : T('library.aud.empty.add')),
                        'fa-compact-disc');
                }
                return;
            }
            listEl.innerHTML = rows.map(rowHTML).join('');
            rows.forEach(d => bindRow(ctx, d));
            // A comment on a Media library file lives on the file itself, not on its
            // transcript: the transcript may not exist yet (this is the main slice of this
            // page), and it is often exactly then that one needs to say something about the record.
            if (R.comments) {
                R.comments.decorate('audio_download',
                    rows.map(d => ({ id: d.id, el: listEl.querySelector(`.rc-aud[data-id="${d.id}"]`) }))
                        .filter(x => x.el),
                    { slot: '.rc-aud__actions', position: 'beforeend' });
            }
            applyMarks();           // mark rows that are being transcribed right away
            startPolling(ctx);      // and keep the status alive
            if (pagerEl) {
                pagerEl.innerHTML = pagerHTML(data);
                pagerEl.querySelectorAll('[data-page]').forEach(b =>
                    b.addEventListener('click', () => { state.page = parseInt(b.dataset.page, 10); sync(); load(ctx); window.scrollTo(0, 0); }));
            }
        } catch (err) {
            if (ctx.isCurrent()) listEl.innerHTML = UI.error(err && err.message);
        }
    }

    // ---- background transcription status (poll /api/transcribe/active) --------
    function startPolling(ctx) {
        if (pollTimer) return;  // already running
        pollTimer = setInterval(() => pollActive(ctx), 2500);
    }
    function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }
    async function pollActive(ctx) {
        if (!ctx.isCurrent || !ctx.isCurrent()) { stopPolling(); return; }
        let data;
        try { data = await R.api.transcribeActive(); }
        catch (_) { return; }
        if (!ctx.isCurrent()) { stopPolling(); return; }
        const active = (data && data.active) || [];
        const nowIds = new Set(active.map(a => Number(a.audio_download_id)));
        // completion: was active, now not → reload (the
        // «Транскрипт» button + the category from the transcript will appear).
        let completed = false;
        activeIds.forEach(id => { if (!nowIds.has(id)) completed = true; });
        activeIds = nowIds;
        lastActive = active;
        applyMarks();
        if (completed) load(ctx);
    }
    function applyMarks() {
        if (!listEl) return;
        lastActive.forEach(a => markRowTranscribing(Number(a.audio_download_id), a));
    }
    function markRowTranscribing(id, entry) {
        const row = listEl && listEl.querySelector(`.rc-aud[data-id="${id}"]`);
        if (!row) return;
        const actions = row.querySelector('.rc-aud__actions');
        if (!actions) return;
        const stage = entry && entry.stage === 'diarizing' ? T('library.aud.stage.diarizing') : T('library.aud.stage.transcribing');
        let pill = actions.querySelector('[data-act="tr-status"]');
        if (pill) { pill.querySelector('.rc-tr-stage').textContent = stage; return; }
        // we replace «Транскрибувати» / «Транскрипт» with a static status pill
        const btn = actions.querySelector('[data-act="transcribe"]') || actions.querySelector('[data-act="open-tr"]');
        const pillHTML = `<span class="rc-btn rc-btn--sm is-transcribing" data-act="tr-status" title="${U.esc(T('library.aud.statusTitle'))}"><i class="fa-solid fa-spinner fa-spin"></i> <span class="rc-tr-stage">${stage}</span></span>`;
        if (btn) btn.outerHTML = pillHTML;
        else actions.insertAdjacentHTML('afterbegin', pillHTML);
    }

    function rowHTML(d) {
        const st = d.source_type || 'youtube';
        const thumb = d.thumbnail_url
            ? `<img class="rc-rec__thumb" src="${U.esc(d.thumbnail_url)}" alt="" loading="lazy">`
            : `<i class="rc-rec__glyph ${U.SRC_ICON[st] || 'fa-solid fa-compact-disc'}" aria-hidden="true"></i>`;
        const dur = d.duration || d.recording_duration_sec;
        // Order by usefulness for the "listen / transcribe" decision:
        // duration → author → date. File size is NOT shown here — it is
        // needed once a year when cleaning the disk, and weighed as much as
        // duration. The source label is also removed: «Локальний запис» next to the
        // «ЗАПИС» badge is a verbatim repeat with no information.
        const meta = [
            UI.srcBadge(st),
            d.has_video ? `<span class="rc-vidbadge">&#x1F4F9; ${T('library.aud.video')}</span>` : '',
            dur ? `<span class="rc-rec__sig">${U.fmtDuration(dur)}</span>` : '',
            // `author` is meaningful only for YouTube (the channel). For local
            // records the DB holds the literal «Локальний запис» — a verbatim repeat of the
            // «ЗАПИС» badge next to it, i.e. zero information on every row.
            (st === 'youtube' && d.author) ? `<span>${U.esc(d.author)}</span>` : '',
            `<span>${U.fmtDate(d.created_at)}</span>`,
            d.transcription_id ? '' : `<span class="rc-rec__warn">${T('library.aud.noTranscriptBadge')}</span>`,
            state && state.category_id ? '' : UI.catChip(d.category_id),
        ].filter(Boolean).join('<span class="sep">·</span>');
        // d.tags — taxonomy tags (C4, from the linked transcript), NOT to be
        // confused with d.youtube_tags (the video's own YouTube keywords).
        const tags = d.tags || [];
        const tagsHTML = tags.length
            ? `<div class="rc-tags" style="margin-top:6px">${tags.map(tg => `<span class="rc-tag"${tg.color ? ` style="border-color:${U.esc(tg.color)}"` : ''}>${U.esc(tg.name)}</span>`).join('')}</div>`
            : '';
        const rtModel = rtRunning.get(d.id);
        const tr = rtModel
            ? `<span class="rc-btn rc-btn--sm is-transcribing" data-act="rt-status"><i class="fa-solid fa-spinner fa-spin"></i> ${U.esc(T('library.rt.running', { model: rtModel }))}</span>`
            : d.transcription_id
            ? `<button class="rc-btn rc-btn--sm" data-act="open-tr"><i class="fa-solid fa-file-lines"></i> ${T('library.aud.transcript')}</button>`
            : `<button class="rc-btn rc-btn--sm rc-btn--primary" data-act="transcribe"><i class="fa-solid fa-wand-magic-sparkles"></i> ${T('library.aud.transcribe')}</button>`;
        // A record without its own title but with a transcript — the local model proposes
        // variants. Without a transcript the proposal arrives by itself after transcription.
        const name = d.transcription_id && UI.isUnnamedRecording(d)
            ? `<button class="rc-btn rc-btn--sm rc-rec__name" data-act="suggest" title="${U.esc(T('library.aud.suggestTitle'))}"><i class="fa-solid fa-wand-magic-sparkles"></i> ${T('library.aud.suggest')}</button>`
            : '';
        return `<div class="rc-aud" data-id="${d.id}">
            <span class="rc-rec__rail">${thumb}</span>
            <div class="rc-rec__body">
                <div class="rc-rec__title">${U.esc(d.title || T('library.aud.fallback', { id: d.id }))}</div>
                <div class="rc-rec__meta">${meta}</div>
                ${tagsHTML}
                <div class="rc-aud__run" hidden></div>
            </div>
            <div class="rc-aud__actions">
                ${name}${tr}${d.transcription_id && !rtModel ? `<button class="rc-btn rc-btn--sm" data-act="retranscribe"><i class="fa-solid fa-rotate"></i> ${T('library.rt.action')}</button>` : ''}
                <button class="rc-iconbtn" data-act="play" title="${U.esc(T('library.aud.play'))}" aria-label="${U.esc(T('library.aud.playAria'))}"><i class="fa-solid fa-play"></i></button>
                <button class="rc-iconbtn" data-act="explorer" title="${U.esc(T('library.aud.explorer'))}" aria-label="${U.esc(T('library.aud.explorer'))}"><i class="fa-solid fa-folder-open"></i></button>
                <button class="rc-iconbtn" data-act="edit" title="${U.esc(T('library.lib.editMeta'))}" aria-label="${U.esc(T('library.lib.editMeta'))}"><i class="fa-solid fa-pen"></i></button>
                <button class="rc-iconbtn rc-rec__del" data-act="delete" title="${U.esc(T('library.aud.deleteFile'))}" aria-label="${U.esc(T('library.aud.deleteFile'))}"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>`;
    }

    function bindRow(ctx, d) {
        const row = listEl.querySelector(`.rc-aud[data-id="${d.id}"]`);
        if (!row) return;
        const act = (sel) => row.querySelector(`[data-act="${sel}"]`);

        const openTr = act('open-tr');
        if (openTr) openTr.addEventListener('click', () => R.router.navigate('/transcript/' + U.slug(d.transcription_id, d.title)));

        const rtBtn = act('retranscribe');
        if (rtBtn) rtBtn.addEventListener('click', () => openTranscribePanel(ctx, row, d, true));

        const trBtn = act('transcribe');
        if (trBtn) trBtn.addEventListener('click', () => openTranscribePanel(ctx, row, d));

        act('play').addEventListener('click', async () => {
            try { await R.api.audioPlay(d.id); UI.toast(T('library.aud.playing'), 'info'); }
            catch (e) { UI.toast(e.message || T('library.aud.playFail'), 'error'); }
        });
        act('explorer').addEventListener('click', async () => {
            try { await R.api.audioExplorer(d.id); }
            catch (e) { UI.toast(e.message || T('library.aud.explorerFail'), 'error'); }
        });
        act('edit').addEventListener('click', () => openAudioMeta(ctx, d, { autoSuggest: false }));
        const sugBtn = act('suggest');
        if (sugBtn) sugBtn.addEventListener('click', () => openAudioMeta(ctx, d, { autoSuggest: true }));
        act('delete').addEventListener('click', async () => {
            const delBtn = act('delete');
            row.style.opacity = '0.4';
            row.querySelectorAll('button').forEach(b => b.disabled = true);
            try {
                // The backend now returns {success, id} (previously {status, message}) — soft-delete, the file is not erased.
                const res = await UI.gated(delBtn, () => R.api.audioDelete(d.id).then((x) => x || {}));
                if (res === undefined) {
                    row.style.opacity = '';
                    row.querySelectorAll('button').forEach(b => b.disabled = false);
                    return;
                }
                const delId = (res && res.id) || d.id;
                row.remove();
                UI.actionToast(T('library.aud.del.done'), T('library.aud.del.undo'), async () => {
                    try {
                        await R.api.post(`/api/audio/downloads/${delId}/restore`);
                        UI.toast(T('library.aud.del.restored'), 'success');
                        load(ctx);
                    } catch (e) { UI.toast((e && e.message) || T('library.aud.del.restoreFail'), 'error'); }
                });
            } catch (e) {
                row.style.opacity = '';
                row.querySelectorAll('button').forEach(b => b.disabled = false);
                UI.toast(e.message || T('library.aud.del.fail'), 'error');
            }
        });
    }

    // The «Назва й опис» window of a Media library card. Proposals — only when there is
    // a transcript (the model writes from it); `first` — already ready variants
    // (auto-proposal after transcription), so as not to ask the model a second time.
    async function openAudioMeta(ctx, d, o) {
        o = o || {};
        const tid = o.transcriptionId || d.transcription_id;
        let first = o.first || null;
        const res = await UI.editMetaModal({
            title: d.title || '',
            description: d.description || '',
            titleRequired: true,
            suggest: tid ? () => {
                if (first) { const r = first; first = null; return Promise.resolve(r); }
                return R.api.suggestMeta(tid);
            } : null,
            autoSuggest: !!(tid && (o.autoSuggest || o.first)),
            save: async (values) => {
                const r = await R.api.updateAudio(d.id, values);
                Object.assign(d, r.item || {});
            },
        });
        if (!res) return;
        if (ctx.isCurrent()) load(ctx);
        UI.toast(T('library.aud.saved'), 'success');
    }

    // After transcribing a record without a title: we wait for the Claude summary (enrichment
    // appends it in ~1–2 min, input based on it is several times more accurate), ceiling ~3 min — then
    // variants from transcript fragments. While recording is going on, the server answers
    // 409 RECORDING_ACTIVE (the model belongs to the copilot) — we wait for the end of the recording.
    // The window opens already with ready variants and only if the owner is still
    // on the Media library; for one card — once.
    const SUMMARY_POLL_MS = 10000, SUMMARY_WAIT_MS = 180000, RECORDING_POLL_MS = 30000;
    const autoNamed = new Set();
    async function autoSuggestAfterTranscribe(ctx, d, tid) {
        if (!tid || autoNamed.has(d.id)) return;
        autoNamed.add(d.id);
        const deadline = Date.now() + SUMMARY_WAIT_MS;
        while (ctx.isCurrent() && Date.now() < deadline) {
            try {
                const rec = await R.api.get('/api/history/' + tid);
                if (rec && rec.summary_json) break;
            } catch (e) { console.debug('[audio] summary poll', e); }
            await new Promise(r => setTimeout(r, SUMMARY_POLL_MS));
        }
        while (ctx.isCurrent()) {
            try {
                const first = await R.api.suggestMeta(tid);
                if (!ctx.isCurrent()) return;
                openAudioMeta(ctx, d, { transcriptionId: tid, first });
                return;
            } catch (e) {
                const code = e && e.body && e.body.error_code;
                if (code !== 'RECORDING_ACTIVE') {
                    // The model is unavailable etc.: open the window for manual input with the reason.
                    if (ctx.isCurrent()) openAudioMeta(ctx, d, { transcriptionId: tid, autoSuggest: true });
                    return;
                }
            }
            await new Promise(r => setTimeout(r, RECORDING_POLL_MS));
        }
    }

    function openTranscribePanel(ctx, row, d, again) {
        if (activeIds.has(d.id) || pendingIds.has(d.id) || rtRunning.has(d.id)) {
            UI.toast(T('library.aud.alreadyRunning'), 'info');
            return;
        }
        const run = row.querySelector('.rc-aud__run');
        if (!run) return;
        if (!run.hidden) { run.hidden = true; run.innerHTML = ''; return; }
        const installed = again ? (_models || []).filter(m => m.downloaded === true) : null;
        if (again && !installed.length) { UI.toast(T('library.rt.noModels'), 'error'); return; }
        const modelOpts = (again ? installed : (_models && _models.length ? _models : [{ name: DEFAULT_MODEL, info: {} }])).map(m =>
            `<option value="${U.esc(m.name)}"${m.name === (again ? installed[0].name : DEFAULT_MODEL) ? ' selected' : ''}>${U.esc(m.name)}</option>`).join('');
        const langO = LANGS.map(l => `<option value="${l.v}"${l.v === 'auto' ? ' selected' : ''}>${l.l}</option>`).join('');
        run.hidden = false;
        run.innerHTML = `<div class="rc-aud__tr">
            <select class="rc-select rc-select--sm" data-tr="model">${modelOpts}</select>
            ${again ? '' : `<select class="rc-select rc-select--sm" data-tr="lang">${langO}</select>`}
            <button class="rc-btn rc-btn--primary rc-btn--sm" data-tr="go">${again ? T('library.rt.action') : T('library.aud.transcribe')}</button>
            <button class="rc-btn rc-btn--sm" data-tr="cancel">${T('library.aud.cancel')}</button>
        </div>`;
        run.querySelector('[data-tr="cancel"]').addEventListener('click', () => { run.hidden = true; run.innerHTML = ''; });
        run.querySelector('[data-tr="go"]').addEventListener('click', () => {
            const model = run.querySelector('[data-tr="model"]').value || DEFAULT_MODEL;
            if (again) { runRetranscribe(ctx, run, d, model); return; }
            const language = run.querySelector('[data-tr="lang"]').value || 'auto';
            runTranscribe(ctx, row, run, d, model, language);
        });
    }

    // T5.6: fire-and-forget — we do not wait for /api/transcribe (files up to 20GB, the task
    // may take long). We hide the inline form right away and show the same pill
    // as for tasks found by polling (markRowTranscribing) — the real stage
    // (diarizing/…) will be picked up by startPolling/pollActive, so there is no double UI.
    // Completion (success or error) is also picked up by polling (completed → load()),
    // so there is no navigation to the transcript page here anymore — this reconciles
    // a self-started transcription with those that polling finds on its own.
    function runTranscribe(ctx, row, run, d, model, language) {
        if (activeIds.has(d.id) || pendingIds.has(d.id)) return;   // guard: do not duplicate the task
        pendingIds.add(d.id);
        run.hidden = true; run.innerHTML = '';
        markRowTranscribing(d.id, null);   // instant pill; polling will give the real stage
        UI.toast(T('library.aud.bgToast', { title: d.title || '' }), 'info');

        const fd = new FormData();
        fd.append('source_type', 'library');
        fd.append('audio_download_id', d.id);
        fd.append('model', model);
        fd.append('language', language);

        const wantName = UI.isUnnamedRecording(d);
        R.api.transcribe(fd).then((res) => {
            UI.toast(T('library.aud.trSaved', { title: d.title || '' }), 'success');
            if (wantName) autoSuggestAfterTranscribe(ctx, d, res && res.transcription_id);
        }).catch((err) => {
            UI.toast((err && err.message) || T('library.aud.trFail', { title: d.title || '' }), 'error');
        }).finally(() => {
            pendingIds.delete(d.id);
            // if the server never picked up the task (activeIds did not see it) —
            // we remove the stuck pill and restore the row's normal buttons.
            if (ctx.isCurrent()) load(ctx);
        });
    }

    async function runRetranscribe(ctx, run, d, model) {
        run.hidden = true; run.innerHTML = '';
        let res;
        try { res = await R.api.retranscribe(d.transcription_id, { model }); }
        catch (err) {
            const code = err && err.body && err.body.error_code;
            UI.toast(RT_ERR[code] ? T(RT_ERR[code]) : T('library.rt.err.generic'), 'error');
            return;
        }
        rtRunning.set(d.id, model);
        UI.toast(T('library.rt.started', { model }), 'info');
        if (ctx.isCurrent()) load(ctx);
        rtStops.push(R.api.pollJob(res.job_id, (job) => {
            if (job.state !== 'completed' && job.state !== 'failed') return false;
            rtRunning.delete(d.id);
            if (job.state === 'completed') UI.toast(T('library.rt.done'), 'success');
            else UI.toast(job.error || T('library.rt.failed'), 'error');
            if (ctx.isCurrent()) load(ctx);
            return true;
        }));
    }

    function pagerHTML(d) {
        const total = d.total_pages || 1, page = d.page || 1;
        if (total <= 1) return `<div class="rc-pager"><span class="rc-pager__info">${T('library.aud.pager.total', { count: d.total || 0, num: R.i18n.num(d.total || 0) })}</span></div>`;
        const prev = page > 1 ? `<button class="rc-btn rc-btn--sm" data-page="${page - 1}"><i class="fa-solid fa-chevron-left"></i></button>` : '';
        const next = page < total ? `<button class="rc-btn rc-btn--sm" data-page="${page + 1}"><i class="fa-solid fa-chevron-right"></i></button>` : '';
        return `<div class="rc-pager">${prev}<span class="rc-pager__info">${T('library.aud.pager.page', { page: R.i18n.num(page), pages: R.i18n.num(total), total: T('library.aud.pager.total', { count: d.total, num: R.i18n.num(d.total) }) })}</span>${next}</div>`;
    }

    R.views.audio = {
        render,
        destroy() {
            stopPolling();
            rtStops.forEach(f => f()); rtStops = []; rtRunning = new Map();
            activeIds = new Set(); lastActive = [];
            listEl = null; state = null; cats = null;
        }
    };
})();
