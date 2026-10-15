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
    const DEFAULT_MODEL = 'large-v3-turbo';

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

    // C9: спільна для екрана підказка + пояснення порожньої видачі (той самий
    // текст, що й у library.js — дублюємо навмисно, спільний компонент фільтр-
    // бару поза 09 не збираємо).
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
    // Phase 21: фоновий статус транскрипції. activeIds — id записів, що зараз
    // транскрибуються (з /api/transcribe/active); lastActive — останній снапшот
    // (щоб одразу маркувати свіжо-відрендерені рядки після load).
    let pollTimer = null, activeIds = new Set(), lastActive = [];
    // T5.6: pendingIds — id, для яких ми ЩОЙНО самі відправили /api/transcribe
    // (fire-and-forget) і сервер ще не встиг зʼявити їх у /api/transcribe/active
    // (поллінг раз на 2.5с). Дає миттєвий pill без затримки і не дає повторному
    // кліку по тому самому рядку запустити другу транскрипцію тієї ж миті.
    let pendingIds = new Set();

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
            // '0' — лише нетранскрибовані. Головна робота на цій сторінці:
            // знайти те, що ще не перетворене на текст.
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
        // Зріз «залишок роботи» — окремо від джерел, бо це інша вісь фільтра
        // (не «звідки прийшло», а «чи вже оброблено»).
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
            // Коментар на файлі Медіатеки живе на самому файлі, а не на його
            // транскрипті: транскрипта може ще не бути (це головний зріз цієї
            // сторінки), а сказати про запис часто треба саме тоді.
            if (R.comments) {
                R.comments.decorate('audio_download',
                    rows.map(d => ({ id: d.id, el: listEl.querySelector(`.rc-aud[data-id="${d.id}"]`) }))
                        .filter(x => x.el),
                    { slot: '.rc-aud__actions', position: 'beforeend' });
            }
            applyMarks();           // одразу позначити рядки, що транскрибуються
            startPolling(ctx);      // і тримати статус живим
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
        if (pollTimer) return;  // вже працює
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
        // завершення: був активним, тепер ні → перезавантажити (зʼявиться
        // кнопка «Транскрипт» + напрямок із транскрипту).
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
        // замінюємо «Транскрибувати» / «Транскрипт» на нерухомий статус-pill
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
        // Порядок за корисністю для рішення «слухати / транскрибувати»:
        // тривалість → автор → дата. Розмір файлу тут НЕ показуємо — він
        // потрібен раз на рік при чистці диска, а важив стільки ж, скільки
        // тривалість. Підпис джерела теж прибрано: «Локальний запис» поруч із
        // бейджем ЗАПИС — дослівний повтор без жодної інформації.
        const meta = [
            UI.srcBadge(st),
            d.has_video ? `<span class="rc-vidbadge">&#x1F4F9; ${T('library.aud.video')}</span>` : '',
            dur ? `<span class="rc-rec__sig">${U.fmtDuration(dur)}</span>` : '',
            // `author` осмислений лише для YouTube (канал). Для локальних
            // записів у БД лежить літерал «Локальний запис» — дослівний повтор
            // бейджа ЗАПИС поруч, тобто нуль інформації на кожному рядку.
            (st === 'youtube' && d.author) ? `<span>${U.esc(d.author)}</span>` : '',
            `<span>${U.fmtDate(d.created_at)}</span>`,
            d.transcription_id ? '' : `<span class="rc-rec__warn">${T('library.aud.noTranscriptBadge')}</span>`,
            state && state.category_id ? '' : UI.catChip(d.category_id),
        ].filter(Boolean).join('<span class="sep">·</span>');
        // d.tags — таксономічні теги (C4, з привʼязаного транскрипту), НЕ
        // плутати з d.youtube_tags (власні ключові слова відео на YouTube).
        const tags = d.tags || [];
        const tagsHTML = tags.length
            ? `<div class="rc-tags" style="margin-top:6px">${tags.map(tg => `<span class="rc-tag"${tg.color ? ` style="border-color:${U.esc(tg.color)}"` : ''}>${U.esc(tg.name)}</span>`).join('')}</div>`
            : '';
        const tr = d.transcription_id
            ? `<button class="rc-btn rc-btn--sm" data-act="open-tr"><i class="fa-solid fa-file-lines"></i> ${T('library.aud.transcript')}</button>`
            : `<button class="rc-btn rc-btn--sm rc-btn--primary" data-act="transcribe"><i class="fa-solid fa-wand-magic-sparkles"></i> ${T('library.aud.transcribe')}</button>`;
        // Запис без власної назви, але з транскриптом — локальна модель пропонує
        // варіанти. Без транскрипту пропозиція прийде сама після транскрибації.
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
                ${name}${tr}
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
            const ok = await UI.confirmModal({
                title: T('library.aud.del.title'),
                message: T('library.aud.del.message', { title: d.title || ('#' + d.id) }),
                confirmLabel: T('library.aud.del.confirm'),
            });
            if (!ok) return;
            row.style.opacity = '0.4';
            row.querySelectorAll('button').forEach(b => b.disabled = true);
            try {
                // Бекенд тепер повертає {success, id} (раніше {status, message}) — soft-delete, файл не стирається.
                const res = await R.api.audioDelete(d.id);
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

    // Вікно «Назва й опис» картки Медіатеки. Пропозиції — лише коли є
    // транскрипт (модель пише за ним); `first` — уже готові варіанти
    // (автопропозиція після транскрибації), щоб не питати модель вдруге.
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

    // Після транскрибації запису без назви: чекаємо саммарі Claude (enrichment
    // дописує його за ~1–2 хв, вхід за ним у рази точніший), стеля ~3 хв — далі
    // варіанти з фрагментів транскрипту. Поки йде запис, сервер відповідає
    // 409 RECORDING_ACTIVE (модель належить копілоту) — чекаємо кінця запису.
    // Вікно відкривається вже з готовими варіантами і лише якщо власник досі
    // на Медіатеці; для однієї картки — один раз.
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
                    // Модель недоступна тощо: відкриваємо вікно для ручного вводу з причиною.
                    if (ctx.isCurrent()) openAudioMeta(ctx, d, { transcriptionId: tid, autoSuggest: true });
                    return;
                }
            }
            await new Promise(r => setTimeout(r, RECORDING_POLL_MS));
        }
    }

    function openTranscribePanel(ctx, row, d) {
        if (activeIds.has(d.id) || pendingIds.has(d.id)) {
            UI.toast(T('library.aud.alreadyRunning'), 'info');
            return;
        }
        const run = row.querySelector('.rc-aud__run');
        if (!run) return;
        if (!run.hidden) { run.hidden = true; run.innerHTML = ''; return; }
        const modelOpts = (_models && _models.length ? _models : [{ name: DEFAULT_MODEL, info: {} }]).map(m =>
            `<option value="${U.esc(m.name)}"${m.name === DEFAULT_MODEL ? ' selected' : ''}>${U.esc(m.name)}</option>`).join('');
        const langO = LANGS.map(l => `<option value="${l.v}"${l.v === 'auto' ? ' selected' : ''}>${l.l}</option>`).join('');
        run.hidden = false;
        run.innerHTML = `<div class="rc-aud__tr">
            <select class="rc-select rc-select--sm" data-tr="model">${modelOpts}</select>
            <select class="rc-select rc-select--sm" data-tr="lang">${langO}</select>
            <button class="rc-btn rc-btn--primary rc-btn--sm" data-tr="go">${T('library.aud.transcribe')}</button>
            <button class="rc-btn rc-btn--sm" data-tr="cancel">${T('library.aud.cancel')}</button>
        </div>`;
        run.querySelector('[data-tr="cancel"]').addEventListener('click', () => { run.hidden = true; run.innerHTML = ''; });
        run.querySelector('[data-tr="go"]').addEventListener('click', () => {
            const model = run.querySelector('[data-tr="model"]').value || DEFAULT_MODEL;
            const language = run.querySelector('[data-tr="lang"]').value || 'auto';
            runTranscribe(ctx, row, run, d, model, language);
        });
    }

    // T5.6: fire-and-forget — не чекаємо /api/transcribe (файли до 20GB, задача
    // може йти довго). Одразу ховаємо інлайн-форму й показуємо той самий pill,
    // що й для задач, знайдених поллінгом (markRowTranscribing) — реальну стадію
    // (diarizing/…) підхопить startPolling/pollActive, тож подвійного UI немає.
    // Завершення (успіх чи помилка) теж підхоплює поллінг (completed → load()),
    // тож переходу на сторінку транскрипту тут більше немає — це узгоджує
    // самостійно запущену транскрипцію з тими, що поллінг знаходить сам.
    function runTranscribe(ctx, row, run, d, model, language) {
        if (activeIds.has(d.id) || pendingIds.has(d.id)) return;   // guard: не дублюємо задачу
        pendingIds.add(d.id);
        run.hidden = true; run.innerHTML = '';
        markRowTranscribing(d.id, null);   // миттєвий pill; реальну стадію дасть поллінг
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
            // якщо сервер так і не підхопив задачу (activeIds її не бачив) —
            // прибираємо застряглий pill і повертаємо звичайні кнопки рядка.
            if (ctx.isCurrent()) load(ctx);
        });
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
            activeIds = new Set(); lastActive = [];
            listEl = null; state = null; cats = null;
        }
    };
})();
