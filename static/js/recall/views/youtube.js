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
        { v: 'uk', l: 'Українська' },
        { v: 'en', l: 'English' },
        { v: 'ru', l: 'Русский' },
        { v: 'auto', l: 'Авто-визначення' },
    ];
    const DEFAULT_MODEL = 'large-v3-turbo';

    let st = null;        // { url, info, opts, busy }
    let _poll = null;     // download-progress poll timer
    let _models = null;   // cached /api/models result
    // T5.6: bgJob — фонова транскрипція, запущена ЦИМ view (модульний scope —
    // переживає render()/destroy() у межах вкладки, скидається лише повним
    // перезавантаженням сторінки). Поки вона не завершиться (успіх чи помилка),
    // повторний захід у /youtube показує статус-бейдж замість форми — це і є
    // guard проти дублю задачі (аудит: повторний клік/захід дублює роботу).
    let bgJob = null;      // { label }

    function cleanup() {
        if (_poll) { clearTimeout(_poll); _poll = null; }
    }

    function bgNoticeHTML(label) {
        return `<div class="rc-note">
            <div class="rc-note__title"><i class="fa-solid fa-cloud-arrow-up"></i> Транскрибую «${U.esc(label)}» у фоні</div>
            <p>Можна закрити цю вкладку або перейти в інший розділ — запис з'явиться в Історії, коли транскрипція завершиться.</p>
            <div class="rc-note__actions">
                <button class="rc-btn rc-btn--primary" id="rcYtGoHistory">Перейти в Історію</button>
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
        if (!Number.isFinite(from) || !Number.isFinite(to)) throw new Error('Вкажіть час початку і кінця (мм:сс)');
        if (to <= from) throw new Error('Кінець має бути пізніше за початок');
        if (dur && to > dur + 1) throw new Error('Кінець виходить за межі відео (' + U.fmtDuration(dur) + ')');
        return { start: from, end: to };
    }

    function updateTrimNote() {
        const note = document.getElementById('rcYtTrimNote');
        const chk = document.getElementById('rcYtTrim');
        if (!note || !chk) return;
        const fromRaw = ((document.getElementById('rcYtFrom') || {}).value || '').trim();
        const toRaw = ((document.getElementById('rcYtTo') || {}).value || '').trim();
        if (!fromRaw && !toRaw) {
            note.innerHTML = 'Формат: <b>мм:сс</b> або <b>год:хв:сс</b> (чи секунди).';
            note.classList.remove('is-err');
            return;
        }
        try {
            const t = readTrim();
            note.innerHTML = `Фрагмент: <b>${U.fmtDuration(t.start)}</b> – <b>${U.fmtDuration(t.end)}</b> · тривалість <b>${U.fmtDuration(t.end - t.start)}</b>`;
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
                <div class="rc-eyebrow"><i class="fa-brands fa-youtube" style="color:var(--rc-src-youtube)"></i> Додати у архів · YouTube</div>
                <h1 class="rc-pagehead__title">З YouTube в архів</h1>
                <p class="rc-pagehead__lede">Вставте посилання — Recall завантажить аудіо, транскрибує його й збереже у Бібліотеку з повним провенансом. Готовий запис відкриється як сторінка в Історії.</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-brands fa-youtube"></i>
                    <input id="rcYtUrl" type="url" inputmode="url" autocomplete="off" spellcheck="false"
                           placeholder="https://youtube.com/watch?v=…" value="${U.esc(st.url)}">
                </div>
                <button class="rc-btn rc-btn--primary" id="rcYtFetch"><i class="fa-solid fa-arrow-right"></i> Отримати відео</button>
            </div>
            <div id="rcYtBody"></div>`;

        const urlInput = ctx.mount.querySelector('#rcYtUrl');
        const fetchBtn = ctx.mount.querySelector('#rcYtFetch');
        const go = () => fetchInfo(ctx, urlInput.value);
        fetchBtn.addEventListener('click', go);
        urlInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });

        // T5.6: фонова транскрипція з попереднього заходу ще триває — не даємо
        // почати другу (для того самого чи іншого відео), поки не завершиться.
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
        if (!/youtu\.?be/i.test(url)) UI.toast('Це не схоже на YouTube-посилання', 'error');

        st.url = url; st.info = null; st.busy = false;
        body.innerHTML = `<div class="rc-yt__loading rc-mono"><i class="fa-solid fa-spinner fa-spin"></i> Отримую дані відео…</div>`;
        // reflect into the URL for deep-linking / refresh, without a re-render
        R.router.replace('/youtube?url=' + encodeURIComponent(url));

        try {
            const info = await R.api.youtubeInfo(url);
            if (!ctx.isCurrent()) return;
            if (!info || info.success === false) {
                body.innerHTML = UI.error((info && info.error) || 'Не вдалося отримати відео');
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
        const langOpts = LANGS.map(l => `<option value="${l.v}"${l.v === 'uk' ? ' selected' : ''}>${l.l}</option>`).join('');
        const catOpts = UI.catOptions('', { first: 'none' });

        const thumb = info.thumbnail
            ? `<img class="rc-yt__thumb" src="${U.esc(info.thumbnail)}" alt="" loading="lazy">`
            : `<div class="rc-yt__thumb rc-yt__thumb--ph"><i class="fa-brands fa-youtube"></i></div>`;

        body.innerHTML = `
            <div class="rc-yt">
                <div class="rc-yt__preview">
                    ${thumb}
                    <div class="rc-yt__meta">
                        <div class="rc-eyebrow">Відео</div>
                        <div class="rc-yt__title">${U.esc(info.title || 'Без назви')}</div>
                        <div class="rc-yt__sub rc-mono">${U.esc(info.author || '')}${info.duration ? ' · ' + U.fmtDuration(info.duration) : ''}</div>
                        ${info.description ? `<p class="rc-yt__desc">${U.esc(info.description)}</p>` : ''}
                    </div>
                </div>
                <div class="rc-yt__settings">
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtModel">Модель розпізнавання</label>
                        <select class="rc-select" id="rcYtModel">${modelOpts}</select>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtLang">Мова</label>
                        <select class="rc-select" id="rcYtLang">${langOpts}</select>
                    </div>
                    <div class="rc-field">
                        <label class="rc-field__label" for="rcYtCat">Напрямок</label>
                        <select class="rc-select rc-catsel" id="rcYtCat" data-cat-first="none">${catOpts}</select>
                    </div>
                    <label class="rc-check" for="rcYtDiar">
                        <input type="checkbox" id="rcYtDiar"> <span>Розрізняти спікерів (діаризація)</span>
                    </label>
                    <label class="rc-check" for="rcYtTrim">
                        <input type="checkbox" id="rcYtTrim"> <span>Обрізати: транскрибувати лише фрагмент</span>
                    </label>
                    <div class="rc-yt__trim" id="rcYtTrimFields" hidden>
                        <div class="rc-field">
                            <label class="rc-field__label" for="rcYtFrom">Початок</label>
                            <input class="rc-input rc-input--sm" id="rcYtFrom" inputmode="numeric" autocomplete="off" placeholder="0:00">
                        </div>
                        <div class="rc-field">
                            <label class="rc-field__label" for="rcYtTo">Кінець</label>
                            <input class="rc-input rc-input--sm" id="rcYtTo" inputmode="numeric" autocomplete="off" placeholder="${info.duration ? U.fmtDuration(info.duration) : 'мм:сс'}">
                        </div>
                        <div class="rc-yt__trimnote rc-mono" id="rcYtTrimNote">Формат: <b>мм:сс</b> або <b>год:хв:сс</b> (чи секунди).</div>
                    </div>
                </div>
                <div class="rc-yt__actions">
                    <button class="rc-btn" id="rcYtBack"><i class="fa-solid fa-arrow-left"></i> Інше відео</button>
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcYtStart"><i class="fa-solid fa-wand-magic-sparkles"></i> Транскрибувати та зберегти</button>
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
        ctx.mount.querySelectorAll('#rcYtModel,#rcYtLang,#rcYtCat,#rcYtDiar,#rcYtTrim,#rcYtFrom,#rcYtTo,#rcYtStart,#rcYtBack')
            .forEach(e => { e.disabled = disabled; });
    }

    // ---- step 2: download --------------------------------------------------
    async function start(ctx) {
        if (!st || !st.info || st.busy) return;
        const model = (ctx.mount.querySelector('#rcYtModel') || {}).value || DEFAULT_MODEL;
        const language = (ctx.mount.querySelector('#rcYtLang') || {}).value || 'uk';
        const category_id = (ctx.mount.querySelector('#rcYtCat') || {}).value || '';
        const diarize = !!(ctx.mount.querySelector('#rcYtDiar') || {}).checked;
        let trim;
        try { trim = readTrim(); }
        catch (e) { UI.toast(e.message, 'error'); return; }
        st.opts = { model, language, category_id, diarize, trim };
        st.busy = true;
        setSettingsDisabled(ctx, true);

        const run = ctx.mount.querySelector('#rcYtRun');
        if (run) run.innerHTML = runHTML();

        try {
            const dl = { url: st.url };
            if (trim) { dl.start_time = trim.start; dl.end_time = trim.end; }
            const r = await R.api.youtubeDownload(dl);
            if (!ctx.isCurrent()) return;
            if (!r || !r.download_id) { fail(ctx, (r && r.error) || 'Не вдалося запустити завантаження'); return; }
            pollDownload(ctx, r.download_id);
        } catch (err) {
            if (ctx.isCurrent()) fail(ctx, err && err.message);
        }
    }

    function runHTML() {
        return `<div class="rc-run">
            <div class="rc-run__head">
                <span class="rc-run__stage" id="rcRunStage">Підготовка…</span>
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
                setStage('Завантаження аудіо…');
                setProgress(Math.round(p.percent || 0), false);
                setHint([p.speed, p.eta && ('ETA ' + p.eta)].filter(Boolean).join('  ·  '));
                _poll = setTimeout(() => pollDownload(ctx, id), 600);
            } else if (status === 'processing') {
                setStage('Обробка аудіо…'); setProgress(100, false); setHint('Конвертація у MP3');
                _poll = setTimeout(() => pollDownload(ctx, id), 600);
            } else if (status === 'starting') {
                setStage('Підготовка…'); setProgress(0, false); setHint('Запуск завантаження');
                _poll = setTimeout(() => pollDownload(ctx, id), 600);
            } else if (status === 'completed') {
                transcribe(ctx, id);
            } else if (status === 'error') {
                fail(ctx, p.error || 'Помилка завантаження');
            } else {
                _poll = setTimeout(() => pollDownload(ctx, id), 1000);
            }
        } catch (err) {
            fail(ctx, err && err.message);
        }
    }

    // ---- step 3: transcribe (fire-and-forget; result lands in History) ----
    //   T5.6: раніше тут був `await R.api.transcribe(fd)`, що тримало вкладку
    //   «заблокованою» на весь час транскрипції (до 20GB файлів) без жодного
    //   способу дізнатись, що можна спокійно піти — а якщо вкладку закривали,
    //   користувач не знав, чи задача взагалі збереглась. Сервер виконує
    //   /api/transcribe синхронно в потоці запиту незалежно від клієнтського
    //   зʼєднання (той самий факт, на якому будується 504-фолбек у record.js
    //   save()) — тож розрив зʼєднання НЕ втрачає роботу, тільки видимість.
    //   Тому тепер: не чекаємо проміс для UI, одразу показуємо статус-бейдж
    //   «можна закрити вкладку», а `.then()/.catch()` довершують UI, ЛИШЕ якщо
    //   view ще активний (bgJob — module-scope guard від подвійного запуску).
    //
    //   NOTE: ми свідомо НЕ відкриваємо EventSource на /api/events/<id> для
    //   live-сегментів. Werkzeug dev-сервер лишає keep-alive зʼєднання в
    //   зламаному стані після GET-SSE стріму, що стопорить наступний запит на
    //   отруєному зʼєднанні — а саме /api/history/<id> сторінки транскрипту,
    //   лишаючи її «висіти» на скелетоні. Live word-streaming — це nice-to-have,
    //   коректність важливіша. Майбутнє відродження має використати
    //   fetch+ReadableStream reader з AbortController (див. api.askStream), а
    //   не EventSource.
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

        bgJob = { label };
        const run = ctx.mount.querySelector('#rcYtRun');
        if (run) { run.innerHTML = bgNoticeHTML(label); wireBgNotice(run); }
        UI.toast('Завантаження готове — транскрибую «' + label + '» у фоні. Можна закрити вкладку: запис зʼявиться в Історії.', 'info');

        R.api.transcribe(fd).then((result) => {
            bgJob = null;
            UI.toast('Готово — «' + label + '» збережено у Бібліотеці', 'success');
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
            UI.toast('Транскрипція «' + label + '» не вдалась: ' + ((err && err.message) || 'помилка'), 'error');
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
            run.innerHTML = UI.error(msg || 'Помилка')
                + `<div class="rc-yt__retry"><button class="rc-btn" id="rcYtRetry"><i class="fa-solid fa-rotate-right"></i> Спробувати ще раз</button></div>`;
            const retry = run.querySelector('#rcYtRetry');
            if (retry) retry.addEventListener('click', () => start(ctx));
        }
        const m = String(msg || '');
        if (/cookies/i.test(m)) UI.toast('Потрібен свіжий cookies.txt у корені проєкту', 'error');
        else if (/ffmpeg/i.test(m)) UI.toast('FFmpeg не знайдено в PATH', 'error');
    }

    R.views.youtube = { render, destroy() { cleanup(); st = null; } };
})();
