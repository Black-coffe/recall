/* Recall — "Додати у архів · Документ". Native document ingestion (Phase 16).

   Drag-drop (or pick) a PDF/DOCX/PPTX/XLSX/CSV/MD/TXT/image → POST
   /api/documents/upload (parses synchronously, dedups by content hash, stores
   as a transcriptions row with source_type='document', kicks off enrichment) →
   navigate to the native /transcript page. Duplicates are surfaced with the
   option to open the existing record or re-ingest with force.

   No EventSource here either (see recall_sse_connection_gotcha). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const EXT = ['pdf', 'docx', 'pptx', 'xlsx', 'csv', 'md', 'markdown', 'txt',
                 'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp'];
    const ICON = {
        pdf: 'fa-file-pdf', docx: 'fa-file-word', pptx: 'fa-file-powerpoint',
        xlsx: 'fa-file-excel', csv: 'fa-file-csv',
        md: 'fa-file-lines', markdown: 'fa-file-lines', txt: 'fa-file-lines',
        png: 'fa-file-image', jpg: 'fa-file-image', jpeg: 'fa-file-image',
        tif: 'fa-file-image', tiff: 'fa-file-image', bmp: 'fa-file-image', webp: 'fa-file-image',
    };

    let st = null;   // { file, category_id, busy }
    // T5.6: bgJob — фонове додавання документа, запущене ЦИМ view (модульний
    // scope — переживає render()/destroy() у межах вкладки). Guard проти
    // дублю: поки не завершиться, повторний захід у /documents показує
    // статус-бейдж замість дропзони.
    let bgJob = null;      // { label }

    const extOf = (n) => (n.split('.').pop() || '').toLowerCase();
    const extOk = (n) => EXT.includes(extOf(n));

    function bgNoticeHTML(label) {
        return `<div class="rc-note">
            <div class="rc-note__title"><i class="fa-solid fa-cloud-arrow-up"></i> Додаю «${U.esc(label)}» у фоні</div>
            <p>Можна закрити цю вкладку або перейти в інший розділ — запис з'явиться в Історії, коли обробка завершиться.</p>
            <div class="rc-note__actions">
                <button class="rc-btn rc-btn--primary" id="rcDocGoHistory">Перейти в Історію</button>
            </div>
        </div>`;
    }
    function wireBgNotice(root) {
        if (!root) return;
        const btn = root.querySelector('#rcDocGoHistory');
        if (btn) btn.addEventListener('click', () => R.router.navigate('/library'));
    }

    async function render(ctx) {
        st = { file: null, category_id: '', busy: false };
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-file-lines"></i> Додати у архів · Документ</div>
                <h1 class="rc-pagehead__title">Завантажити документ</h1>
                <p class="rc-pagehead__lede">PDF, Word, PowerPoint, Excel, CSV, Markdown, текст або зображення — Recall розбере текст (з OCR для сканів) і додасть у семантичний пошук. Готовий запис відкриється як сторінка в Історії.</p>
            </div>
            <div class="rc-drop" id="rcDrop" tabindex="0" role="button" aria-label="Обрати документ або перетягнути сюди">
                <input type="file" id="rcDocFile" accept="${EXT.map(e => '.' + e).join(',')}" hidden>
                <div class="rc-drop__ico"><i class="fa-solid fa-cloud-arrow-up"></i></div>
                <div class="rc-drop__title">Перетягніть документ сюди або натисніть, щоб обрати</div>
                <div class="rc-drop__hint rc-mono">${EXT.join(' · ')}</div>
            </div>
            <div id="rcDocBody"></div>`;

        const drop = ctx.mount.querySelector('#rcDrop');
        const input = ctx.mount.querySelector('#rcDocFile');
        const body = ctx.mount.querySelector('#rcDocBody');

        // T5.6: фонове додавання з попереднього заходу ще триває — не даємо
        // почати друге, поки не завершиться.
        if (bgJob) {
            drop.hidden = true;
            body.innerHTML = bgNoticeHTML(bgJob.label);
            wireBgNotice(body);
            return;
        }

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

    async function onFile(ctx, file) {
        if (!extOk(file.name)) { UI.toast('Непідтримуваний формат документа', 'error'); return; }
        st.file = file; st.busy = false;
        await renderForm(ctx);
    }

    async function renderForm(ctx) {
        const cats = await UI.loadCategories();
        if (!ctx.isCurrent()) return;
        const body = ctx.mount.querySelector('#rcDocBody');
        if (!body || !st.file) return;

        const catOpts = UI.catOptions('', { first: 'none' });
        const ico = ICON[extOf(st.file.name)] || 'fa-file';

        body.innerHTML = `
            <div class="rc-yt">
                <div class="rc-up__file">
                    <div class="rc-up__fico"><i class="fa-solid ${ico}"></i></div>
                    <div class="rc-up__fmeta">
                        <div class="rc-up__fname">${U.esc(st.file.name)}</div>
                        <div class="rc-up__fsub rc-mono">${U.fmtBytes(st.file.size)} · ${U.esc(extOf(st.file.name).toUpperCase())}</div>
                    </div>
                    <button class="rc-iconbtn" id="rcDocClear" title="Прибрати"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="rc-yt__settings">
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcDocCat">Напрямок</label>
                        <select class="rc-select rc-catsel" id="rcDocCat" data-cat-first="none">${catOpts}</select>
                    </div>
                </div>
                <div class="rc-yt__actions">
                    <button class="rc-btn" id="rcDocBack"><i class="fa-solid fa-arrow-left"></i> Інший документ</button>
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcDocStart"><i class="fa-solid fa-file-import"></i> Додати в архів</button>
                </div>
            </div>
            <div id="rcDocRun"></div>`;

        const reset = () => { st.file = null; body.innerHTML = ''; const i = ctx.mount.querySelector('#rcDocFile'); if (i) i.value = ''; };
        body.querySelector('#rcDocStart').addEventListener('click', () => start(ctx, false));
        body.querySelector('#rcDocBack').addEventListener('click', reset);
        body.querySelector('#rcDocClear').addEventListener('click', reset);
    }

    function setFormDisabled(ctx, disabled) {
        ctx.mount.querySelectorAll('#rcDocCat,#rcDocStart,#rcDocBack,#rcDocClear').forEach(e => { e.disabled = disabled; });
    }

    // T5.6: XHR лишається один виклик (api.js не займаємо) — реальний прогрес
    // байтів під час завантаження чесний і лишений як є. Розбір документа
    // (парсинг/OCR/дедуп по хешу) йде синхронно на бекенді в тому ж потоці
    // запиту, незалежно від клієнтського зʼєднання — тож щойно байти дійшли
    // (onUploadDone), розрив зʼєднання більше не втрачає роботу. З цього
    // моменту показуємо статус-бейдж «можна закрити вкладку»; .then()/.catch()
    // довершують UI (включно з showDuplicate) лише якщо view ще активний.
    function start(ctx, force) {
        if (!st || !st.file || st.busy || bgJob) return;
        st.category_id = (ctx.mount.querySelector('#rcDocCat') || {}).value || st.category_id || '';
        st.busy = true;
        setFormDisabled(ctx, true);

        const label = st.file.name;
        const run = ctx.mount.querySelector('#rcDocRun');
        if (run) run.innerHTML = `<div class="rc-run">
            <div class="rc-run__head"><span class="rc-run__stage" id="rcDocStage">Завантаження…</span><span class="rc-run__pct rc-mono" id="rcDocPct">0%</span></div>
            <div class="rc-progress"><div class="rc-progress__fill" id="rcDocFill"></div></div>
            <div class="rc-run__hint rc-mono" id="rcDocHint">${U.esc(st.file.name)} · ${U.fmtBytes(st.file.size)}</div>
        </div>`;

        const fd = new FormData();
        fd.append('document', st.file);
        if (st.category_id) fd.append('category_id', st.category_id);
        if (force) fd.append('force', 'true');

        bgJob = { label };
        R.api.documentUpload(
            fd,
            (pct) => setProgress(pct, false),
            () => {
                const run2 = ctx.mount.querySelector('#rcDocRun');
                if (run2) { run2.innerHTML = bgNoticeHTML(label); wireBgNotice(run2); }
                UI.toast('Файл завантажено — розбираю «' + label + '» у фоні. Можна закрити вкладку.', 'info');
            }
        ).then((result) => {
            bgJob = null;
            if (!ctx.isCurrent()) {
                UI.toast('«' + label + '» додано в архів', 'success');
                return;
            }
            st.busy = false;
            const tid = result && result.transcription_id;
            const name = (result && result.source_name) || label;
            if (result && result.duplicate) { showDuplicate(ctx, tid, name); return; }
            UI.toast('Документ додано в архів', 'success');
            if (tid) R.router.navigate('/transcript/' + U.slug(tid, name));
            else R.router.navigate('/library');
        }).catch((err) => {
            bgJob = null;
            UI.toast('Не вдалось додати «' + label + '»: ' + ((err && err.message) || 'помилка'), 'error');
            if (!ctx.isCurrent()) return;
            fail(ctx, err && err.message);
        });
    }

    function showDuplicate(ctx, tid, name) {
        st.busy = false;
        const run = ctx.mount.querySelector('#rcDocRun');
        if (!run) return;
        run.innerHTML = `<div class="rc-note">
            <div class="rc-note__title"><i class="fa-solid fa-circle-info"></i> Такий документ уже є в архіві</div>
            <p>Контент збігається з наявним записом. Можна відкрити його або додати копію примусово.</p>
            <div class="rc-note__actions">
                <button class="rc-btn rc-btn--primary" id="rcDocOpen">Відкрити наявний</button>
                <button class="rc-btn" id="rcDocForce">Додати все одно</button>
            </div></div>`;
        const open = run.querySelector('#rcDocOpen');
        if (open) open.addEventListener('click', () => { if (tid) R.router.navigate('/transcript/' + U.slug(tid, name)); });
        const force = run.querySelector('#rcDocForce');
        if (force) force.addEventListener('click', () => start(ctx, true));
    }

    function isImg(n) { return ['png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'webp'].includes(extOf(n)); }
    function setProgress(pct, indeterminate) {
        const p = document.getElementById('rcDocPct'), f = document.getElementById('rcDocFill');
        if (f) {
            f.classList.toggle('is-indeterminate', !!indeterminate);
            if (!indeterminate && typeof pct === 'number') f.style.width = Math.max(0, Math.min(100, pct)) + '%';
        }
        if (p) p.textContent = indeterminate ? '' : (typeof pct === 'number' ? Math.round(pct) + '%' : '');
    }
    function setStage(t) { const s = document.getElementById('rcDocStage'); if (s && t) s.textContent = t; }
    function setHint(t) { const h = document.getElementById('rcDocHint'); if (h) h.textContent = t || ''; }

    function fail(ctx, msg) {
        st.busy = false;
        setFormDisabled(ctx, false);
        const run = ctx.mount.querySelector('#rcDocRun');
        if (run) {
            run.innerHTML = UI.error(msg || 'Помилка')
                + `<div class="rc-yt__retry"><button class="rc-btn" id="rcDocRetry"><i class="fa-solid fa-rotate-right"></i> Спробувати ще раз</button></div>`;
            const retry = run.querySelector('#rcDocRetry');
            if (retry) retry.addEventListener('click', () => start(ctx, false));
        }
    }

    R.views.documents = { render, destroy() { st = null; } };
})();
