/* Recall — "Додати у архів · Файл". Native upload page.

   Drag-drop (or pick) an audio/video file → choose transcription options →
   POST /api/transcribe (source_type=file) with real upload progress (XHR) →
   blocking transcribe → navigate to the native /transcript page. Video files
   have their audio extracted server-side. Result lands in History.

   Like the YouTube view, this deliberately avoids EventSource live-segments —
   see the recall_sse_connection_gotcha lesson. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const EXT = ['mp3', 'mp4', 'mpeg', 'mpga', 'm4a', 'wav', 'webm', 'ogg', 'flac', 'avi', 'mov', 'mkv', 'wmv'];
    const VIDEO_EXT = ['mp4', 'avi', 'mov', 'mkv', 'wmv', 'webm', 'mpeg'];
    const LANGS = [
        { v: 'uk', l: 'capture.lang.uk' },
        { v: 'en', l: 'capture.lang.en' },
        { v: 'ru', l: 'capture.lang.ru' },
        { v: 'auto', l: 'capture.lang.auto' },
    ];
    const DEFAULT_MODEL = 'large-v3-turbo';

    let st = null;        // { file, opts, busy }
    let _models = null;
    // T5.6: bgJob — фонова транскрипція завантаженого файлу, запущена ЦИМ view
    // (модульний scope — переживає render()/destroy() у межах вкладки).
    // Guard проти дублю: поки не завершиться (успіх чи помилка), повторний
    // захід у /upload показує статус-бейдж замість дропзони.
    let bgJob = null;      // { label }

    async function loadModels() {
        if (_models) return _models;
        try { const d = await R.api.models(); _models = Array.isArray(d) ? d : (d.models || []); }
        catch (_) { _models = []; }
        return _models;
    }

    function bgNoticeHTML(label) {
        return `<div class="rc-note">
            <div class="rc-note__title"><i class="fa-solid fa-cloud-arrow-up"></i> ${U.esc(R.t('capture.common.bgTitle', { label }))}</div>
            <p>${R.t('capture.common.bgText')}</p>
            <div class="rc-note__actions">
                <button class="rc-btn rc-btn--primary" id="rcUpGoHistory">${R.t('capture.common.goHistory')}</button>
            </div>
        </div>`;
    }
    function wireBgNotice(root) {
        if (!root) return;
        const btn = root.querySelector('#rcUpGoHistory');
        if (btn) btn.addEventListener('click', () => R.router.navigate('/library'));
    }

    function isVideo(name) { return VIDEO_EXT.includes((name.split('.').pop() || '').toLowerCase()); }
    function extOk(name) { return EXT.includes((name.split('.').pop() || '').toLowerCase()); }

    // ---- render: dropzone --------------------------------------------------
    async function render(ctx) {
        st = { file: null, opts: null, busy: false };
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-arrow-up-from-bracket"></i> ${R.t('capture.up.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('capture.up.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('capture.up.lede')}</p>
            </div>
            <div class="rc-drop" id="rcDrop" tabindex="0" role="button" aria-label="${R.t('capture.up.dropAria')}">
                <input type="file" id="rcFile" accept="${EXT.map(e => '.' + e).join(',')},audio/*,video/*" hidden>
                <div class="rc-drop__ico"><i class="fa-solid fa-cloud-arrow-up"></i></div>
                <div class="rc-drop__title">${R.t('capture.up.dropTitle')}</div>
                <div class="rc-drop__hint rc-mono">${EXT.join(' · ')}</div>
            </div>
            <div id="rcUpBody"></div>`;

        const drop = ctx.mount.querySelector('#rcDrop');
        const input = ctx.mount.querySelector('#rcFile');
        const body = ctx.mount.querySelector('#rcUpBody');

        // T5.6: фонова транскрипція з попереднього заходу ще триває — не даємо
        // почати другу, поки не завершиться.
        if (bgJob) {
            drop.hidden = true;
            body.innerHTML = bgNoticeHTML(bgJob.label);
            wireBgNotice(body);
            return;
        }

        loadModels();
        drop.addEventListener('click', () => input.click());
        drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } });
        input.addEventListener('change', () => { if (input.files && input.files[0]) onFile(ctx, input.files[0]); });

        drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('is-over'); });
        drop.addEventListener('dragleave', () => drop.classList.remove('is-over'));
        drop.addEventListener('drop', (e) => {
            e.preventDefault(); drop.classList.remove('is-over');
            const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (f) onFile(ctx, f);
        });
    }

    // ---- a file was chosen: show card + options ---------------------------
    async function onFile(ctx, file) {
        if (!extOk(file.name)) { UI.toast(R.t('capture.up.badType'), 'error'); return; }
        st.file = file; st.busy = false;
        await renderForm(ctx);
    }

    async function renderForm(ctx) {
        await loadModels();
        const cats = await UI.loadCategories();
        if (!ctx.isCurrent()) return;
        const body = ctx.mount.querySelector('#rcUpBody');
        if (!body || !st.file) return;

        const modelOpts = (_models.length ? _models : [{ name: DEFAULT_MODEL, info: {} }]).map(m => {
            const inf = m.info || {};
            const hint = [inf.size, inf.speed].filter(Boolean).join(', ');
            return `<option value="${U.esc(m.name)}"${m.name === DEFAULT_MODEL ? ' selected' : ''}>${U.esc(m.name)}${hint ? ' — ' + U.esc(hint) : ''}</option>`;
        }).join('');
        const langOpts = LANGS.map(l => `<option value="${l.v}"${l.v === 'uk' ? ' selected' : ''}>${R.t(l.l)}</option>`).join('');
        const catOpts = UI.catOptions('', { first: 'none' });

        const vid = isVideo(st.file.name);
        body.innerHTML = `
            <div class="rc-yt">
                <div class="rc-up__file">
                    <div class="rc-up__fico"><i class="fa-solid ${vid ? 'fa-file-video' : 'fa-file-audio'}"></i></div>
                    <div class="rc-up__fmeta">
                        <div class="rc-up__fname">${U.esc(st.file.name)}</div>
                        <div class="rc-up__fsub rc-mono">${U.fmtBytes(st.file.size)}${vid ? ' · ' + R.t('capture.up.videoNote') : ''}</div>
                    </div>
                    <button class="rc-iconbtn" id="rcUpClear" title="${R.t('capture.up.remove')}"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="rc-yt__settings">
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcUpTitle">${R.t('capture.common.titleOpt')}</label>
                        <input class="rc-input" id="rcUpTitle" autocomplete="off" maxlength="200" placeholder="${U.esc(st.file.name)}">
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcUpDesc">${R.t('capture.common.descOpt')}</label>
                        <textarea class="rc-textarea" id="rcUpDesc" maxlength="4000" placeholder="${R.t('capture.common.descPh')}"></textarea>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcUpModel">${R.t('capture.common.model')}</label>
                        <select class="rc-select" id="rcUpModel">${modelOpts}</select>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcUpLang">${R.t('capture.common.language')}</label>
                        <select class="rc-select" id="rcUpLang">${langOpts}</select>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcUpCat">${R.t('capture.common.category')}</label>
                        <select class="rc-select rc-catsel" id="rcUpCat" data-cat-first="none">${catOpts}</select>
                    </div>
                    <label class="rc-check" for="rcUpDiar">
                        <input type="checkbox" id="rcUpDiar"> <span>${R.t('capture.common.diarize')}</span>
                    </label>
                </div>
                <div class="rc-yt__actions">
                    <button class="rc-btn" id="rcUpBack"><i class="fa-solid fa-arrow-left"></i> ${R.t('capture.up.another')}</button>
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcUpStart"><i class="fa-solid fa-wand-magic-sparkles"></i> ${R.t('capture.common.transcribeSave')}</button>
                </div>
            </div>
            <div id="rcUpRun"></div>`;

        const reset = () => { st.file = null; body.innerHTML = ''; const i = ctx.mount.querySelector('#rcFile'); if (i) i.value = ''; };
        body.querySelector('#rcUpStart').addEventListener('click', () => start(ctx));
        body.querySelector('#rcUpBack').addEventListener('click', reset);
        body.querySelector('#rcUpClear').addEventListener('click', reset);
    }

    function setFormDisabled(ctx, disabled) {
        ctx.mount.querySelectorAll('#rcUpTitle,#rcUpDesc,#rcUpModel,#rcUpLang,#rcUpCat,#rcUpDiar,#rcUpStart,#rcUpBack,#rcUpClear')
            .forEach(e => { e.disabled = disabled; });
    }

    // ---- transcribe (XHR upload progress, fire-and-forget after upload) ---
    //   T5.6: XHR-запит лишається один (api.js не займаємо) — реальний прогрес
    //   байтів під час завантаження чесний і залишений як є. Але раніше УВЕСЬ
    //   виклик, включно з транскрипцією, чекався (`await`), тож єдиним сигналом
    //   був статичний індетермінований спіннер без жодної підказки, що можна
    //   спокійно закрити вкладку. Щойно байти дійшли до сервера
    //   (onUploadDone) — подальша обробка йде синхронно в потоці запиту
    //   незалежно від клієнтського зʼєднання, тож розрив після цього моменту
    //   НЕ втрачає роботу. Тому з onUploadDone одразу показуємо статус-бейдж
    //   «можна закрити вкладку», а .then()/.catch() довершують UI лише якщо
    //   view ще активний.
    function start(ctx) {
        if (!st || !st.file || st.busy || bgJob) return;
        const o = {
            model: (ctx.mount.querySelector('#rcUpModel') || {}).value || DEFAULT_MODEL,
            language: (ctx.mount.querySelector('#rcUpLang') || {}).value || 'uk',
            category_id: (ctx.mount.querySelector('#rcUpCat') || {}).value || '',
            diarize: !!(ctx.mount.querySelector('#rcUpDiar') || {}).checked,
            title: ((ctx.mount.querySelector('#rcUpTitle') || {}).value || '').trim(),
            description: ((ctx.mount.querySelector('#rcUpDesc') || {}).value || '').trim(),
        };
        st.opts = o; st.busy = true;
        setFormDisabled(ctx, true);

        const label = st.file.name;
        const run = ctx.mount.querySelector('#rcUpRun');
        if (run) run.innerHTML = `<div class="rc-run">
            <div class="rc-run__head"><span class="rc-run__stage" id="rcUpStage">${R.t('capture.up.uploading')}</span><span class="rc-run__pct rc-mono" id="rcUpPct">0%</span></div>
            <div class="rc-progress"><div class="rc-progress__fill" id="rcUpFill"></div></div>
            <div class="rc-run__hint rc-mono" id="rcUpHint">${U.esc(st.file.name)} · ${U.fmtBytes(st.file.size)}</div>
        </div>`;

        const fd = new FormData();
        fd.append('source_type', 'file');
        fd.append('audio', st.file);
        fd.append('model', o.model);
        fd.append('language', o.language);
        if (o.diarize) fd.append('diarize', 'true');
        if (o.category_id) fd.append('category_id', o.category_id);
        if (o.title) fd.append('title', o.title);
        if (o.description) fd.append('description', o.description);

        bgJob = { label };
        R.api.transcribeUpload(
            fd,
            (pct) => setProgress(pct, false),
            () => {
                // байти дійшли — далі безпечно навіть без цієї вкладки
                const run2 = ctx.mount.querySelector('#rcUpRun');
                if (run2) { run2.innerHTML = bgNoticeHTML(label); wireBgNotice(run2); }
                UI.toast(R.t('capture.up.uploaded', { label }), 'info');
            }
        ).then((result) => {
            bgJob = null;
            UI.toast(R.t('capture.common.done', { label }), 'success');
            if (!ctx.isCurrent()) return;     // saved server-side regardless
            st.busy = false;
            const tid = result && result.transcription_id;
            const name = (result && result.source_name) || label;
            if (tid) R.router.navigate('/transcript/' + U.slug(tid, name));
            else R.router.navigate('/library');
        }).catch((err) => {
            bgJob = null;
            UI.toast(R.t('capture.common.failed', { label, error: (err && err.message) || R.t('capture.common.errorWord') }), 'error');
            if (!ctx.isCurrent()) return;
            fail(ctx, err && err.message);
        });
    }

    function setProgress(pct, indeterminate) {
        const p = document.getElementById('rcUpPct'), f = document.getElementById('rcUpFill');
        if (f) {
            f.classList.toggle('is-indeterminate', !!indeterminate);
            if (!indeterminate && typeof pct === 'number') f.style.width = Math.max(0, Math.min(100, pct)) + '%';
        }
        if (p) p.textContent = indeterminate ? '' : (typeof pct === 'number' ? Math.round(pct) + '%' : '');
    }
    function setStage(t) { const s = document.getElementById('rcUpStage'); if (s && t) s.textContent = t; }
    function setHint(t) { const h = document.getElementById('rcUpHint'); if (h) h.textContent = t || ''; }

    function fail(ctx, msg) {
        st.busy = false;
        setFormDisabled(ctx, false);
        const run = ctx.mount.querySelector('#rcUpRun');
        if (run) {
            run.innerHTML = UI.error(msg || R.t('capture.common.error'))
                + `<div class="rc-yt__retry"><button class="rc-btn" id="rcUpRetry"><i class="fa-solid fa-rotate-right"></i> ${R.t('capture.common.retry')}</button></div>`;
            const retry = run.querySelector('#rcUpRetry');
            if (retry) retry.addEventListener('click', () => start(ctx));
        }
    }

    R.views.upload = { render, destroy() { st = null; } };
})();
