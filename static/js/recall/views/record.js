/* Recall — "Додати у архів · Запис". Server-side recorder (mic + system
   loopback). The recording runs on the server and survives SPA navigation /
   tab close — on entering this view we reattach to any active session via
   /api/recordings/active.

   Stages: setup → active → stopname → transcribe.
   Save writes the audio into the Audio Library; the optional transcribe
   (source_type=recording) lands a transcript in History → /transcript.

   Live levels + segments stream over /api/recording/<sid>/stream as SSE, read
   via fetch+ReadableStream with an AbortController — NOT EventSource (which
   poisons the werkzeug dev-server connection; see recall_sse_connection_gotcha).

   NOTE: cannot be exercised in automated/browser-MCP testing (needs real audio
   devices) — verified by the user with a live microphone. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const LANGS = [
        { v: 'uk', l: 'capture.lang.uk' }, { v: 'en', l: 'capture.lang.en' },
        { v: 'ru', l: 'capture.lang.ru' }, { v: 'auto', l: 'capture.lang.auto' },
    ];
    const DEFAULT_MODEL = 'large-v3-turbo';

    // Co-pilot (Phase 19): мега-налаштування на старті дзвінка.
    const CP_MODES = [{ v: 'light', l: 'capture.rec.cp.mode.light' }, { v: 'medium', l: 'capture.rec.cp.mode.medium' }, { v: 'hard', l: 'capture.rec.cp.mode.hard' }];
    const CP_IMP = [{ v: 'low', l: 'capture.rec.cp.imp.low' }, { v: 'medium', l: 'capture.rec.cp.imp.medium' }, { v: 'high', l: 'capture.rec.cp.imp.high' }];

    // Паралельні записи (C9): G — чернетка налаштувань нового запису; кожен живий
    // запис — свій S у `sessions` (власний SSE-читач і тік працюють у фоні).
    // `st` — той S, з яким зараз працюємо: вибраний таб, або (на час SSE-події /
    // тіку фонового запису) його власний S через withS(). DOM чіпаємо лише для
    // вибраного (`$id` повертає null для решти), тож хелпери лишились як були.
    let G = null;       // { devices, screens, videoAvailable, videoRegions, exclude, includeFor }
    const sessions = new Map();   // sid → S
    let selSid = null;  // null = таб «+ Новий запис» (setup)
    let st = null;
    let _letter = 0;    // A, B, C… — коротке імʼя запису в табах і питаннях
    let _limitHit = false;   // 409 SESSION_LIMIT → ховаємо «+ Новий запис», поки хтось не завершиться
    let _models = null;
    let _cats = null;
    let _cpAvail = null;   // { enabled, local_llm_available, local_llm_reason, ... }
    let _liveCmHotkey = null;   // Ctrl+Shift+K → фокус у поле живого коментаря (вибраного запису)
    let currentCtx = null;      // ctx поточного рендера — для обробників, що живуть поза render()

    function newS(sid) {
        const letter = String.fromCharCode(65 + (_letter++ % 26));
        return { sessionId: sid, letter, name: '', stage: 'active', status: 'recording', elapsedSec: 0, videoTracks: {}, audioBytes: {}, streamErr: {}, diskFree: null, diskTotal: null, abort: null, tick: null, copilotSessionId: null, copilotConfig: null, copilotActive: false, cpOffer: null, topics: [], curTopic: null, insights: [], cpStatus: 'analyzing', cpUnseen: 0, cpUsage: null, sseState: 'ok', systemSource: null, hasSystem: false, segs: [], stopInfo: null, restore: [] };
    }
    function haltS(S) {
        if (!S) return;
        if (S.abort) { try { S.abort.abort(); } catch (_) {} S.abort = null; }
        if (S.tick) { clearInterval(S.tick); S.tick = null; }
    }
    function reset() {
        for (const S of sessions.values()) haltS(S);
        sessions.clear();
        selSid = null; st = null; _letter = 0; _limitHit = false;
        G = { devices: [], screens: [], videoAvailable: false, videoRegions: {}, exclude: null, includeFor: {}, preset: null };
    }
    // K2: «Інші джерела» з підказки дзвінка → /record?app=<exe>&mic=<назва мікрофона>.
    // Лише передвибір на формі; невідомі значення мовчки ігноруються.
    function presetFrom(q) {
        const app = String((q && q.app) || '').trim().toLowerCase();
        if (!/^[\w .()-]{1,80}\.exe$/.test(app)) return null;
        const mic = String((q && q.mic) || '').trim();
        return { app, label: app.replace(/\.exe$/, ''), mic: mic || null };
    }
    function $id(id) { return (st && st.sessionId && st.sessionId === selSid) ? document.getElementById(id) : null; }
    function withS(S, fn) { const prev = st; st = S; try { return fn(); } finally { st = prev; } }
    function liveSessions() { return [...sessions.values()].filter(S => S.stage === 'active'); }
    function tabName(S) { return S.name ? S.name : R.t('capture.rec.tabName', { letter: S.letter }); }
    function errCode(err) { return (err && err.body && err.body.error_code) || null; }
    const ERR_TXT = {
        SESSION_LIMIT: 'capture.rec.err.SESSION_LIMIT',
        NOT_FOUND: 'capture.rec.err.NOT_FOUND',
        UNSUPPORTED_SOURCE_SPEC: 'capture.rec.err.UNSUPPORTED_SOURCE_SPEC',
        NO_SYSTEM_STREAM: 'capture.rec.err.NO_SYSTEM_STREAM',
        COPILOT_BUSY: 'capture.rec.err.COPILOT_BUSY',
        AMBIGUOUS_SESSION: 'capture.rec.err.AMBIGUOUS_SESSION',
    };
    function errToast(err, prefix) {
        const code = errCode(err);
        const txt = (code && ERR_TXT[code])
            ? R.t(ERR_TXT[code]) + ((err.body && err.body.error) ? ' (' + err.body.error + ')' : '')
            : ((err && err.message) || R.t('capture.rec.err.unknown'));
        UI.toast((prefix ? prefix + ': ' : '') + txt, 'error');
    }

    async function render(ctx) {
        reset();
        G.preset = presetFrom(ctx.query);
        currentCtx = ctx;
        _models = _models || (await loadModels());
        _cats = await UI.loadCategories();
        try { _cpAvail = await R.api.get('/api/copilot/availability'); } catch (_) { _cpAvail = null; }

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-microphone" style="color:var(--rc-src-recording)"></i> ${R.t('capture.rec.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('capture.rec.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('capture.rec.lede')}</p>
            </div>
            <div class="rc-rectabs" id="rcRecTabs" hidden></div>
            <div id="rcRecBody"></div>`;

        // device list (503 → recorder backend disabled)
        try {
            const d = await R.api.get('/api/recording/devices');
            G.devices = d.devices || [];
        } catch (err) {
            if (!ctx.isCurrent()) return;
            ctx.mount.querySelector('#rcRecBody').innerHTML = UI.empty(R.t('capture.rec.unavailable'),
                err.status === 503 ? R.t('capture.rec.recorderOff') : (err.message || ''), 'fa-microphone-slash');
            return;
        }

        // screen list — best-effort; failure is non-fatal (video section just shows as unavailable)
        try {
            const sv = await R.api.get('/api/recording/screens');
            G.videoAvailable = !!(sv && sv.video_available);
            G.screens = (sv && sv.screens) || [];
        } catch (_) {
            G.videoAvailable = false;
            G.screens = [];
        }

        // reattach to every active server-side session (C5: sessions[]; the old
        // single-session shape is still read so a half-upgraded backend works)
        let active = null;
        try { active = await R.api.get('/api/recordings/active'); } catch (_) {}
        if (!ctx.isCurrent()) return;
        let entries = [];
        if (active && Array.isArray(active.sessions)) entries = active.sessions;
        else if (active && active.active && active.session_id) entries = [{ session_id: active.session_id, state: active.state }];
        for (const e of entries) {
            if (!e || !e.session_id) continue;
            let snap = (e.state && typeof e.state === 'object') ? e.state
                : (entries.length === 1 && active.state && typeof active.state === 'object' ? active.state : null);
            if (!snap) {
                try {
                    const r = await R.api.get('/api/recording/' + e.session_id + '/state');
                    snap = (r && r.state) || null;
                } catch (err) {
                    if (err && err.status === 404) continue;   // застарілий sid → без табу
                    snap = { status: typeof e.state === 'string' ? e.state : 'recording' };
                }
                if (snap && snap.is_active === false) continue;
            }
            if (!ctx.isCurrent()) return;
            attachSession(e, snap || {});
        }
        if (!ctx.isCurrent()) return;
        const first = sessions.keys().next();
        for (const S of sessions.values()) { connectSSE(ctx, S); startTick(ctx, S); }
        // З передвибором дзвінка відкриваємо форму нового запису, а не живий таб.
        select(ctx, (!first.done && !G.preset) ? first.value : null);
    }

    function attachSession(e, snap) {
        const S = newS(e.session_id);
        S.status = (typeof snap.status === 'string' && snap.status) || 'recording';
        S.elapsedSec = snap.elapsed_seconds || e.elapsed_seconds || 0;
        S.name = e.name || snap.name || e.auto_name || snap.auto_name || '';
        // seed live size/disk so the REC meta line is populated before the first chunk_saved
        if (snap.streams) for (const k in snap.streams) {
            const v = snap.streams[k];
            if (!v || typeof v !== 'object') continue;
            S.audioBytes[k] = v.bytes || 0;
            if (v.error) S.streamErr[k] = v.error;
        }
        const sysSnap = snap.streams && snap.streams.system;
        S.hasSystem = !!((sysSnap && typeof sysSnap === 'object' && sysSnap.enabled) || (e.streams && e.streams.system === true));
        S.systemSource = snap.system_source || e.system_source || null;
        S.copilotActive = !!e.copilot_active;
        if (typeof snap.disk_free === 'number') S.diskFree = snap.disk_free;
        if (typeof snap.disk_total === 'number') S.diskTotal = snap.disk_total;
        sessions.set(S.sessionId, S);
        // seed video tracks (incl. region) from the manifest so chips survive a reload mid-recording
        withS(S, () => applyStateVideoTracks(snap.video_tracks));
        return S;
    }

    // ---- tabs: one per recording + «+ Новий запис» ------------------------
    // З одним записом табів немає — екран як до паралельних записів.
    function select(ctx, sid) {
        selSid = sid && sessions.has(sid) ? sid : null;
        st = selSid ? sessions.get(selSid) : null;
        renderTabs(ctx);
        if (!st) renderSetup(ctx);
        else if (st.stage === 'stopname') renderStopName(ctx);
        else renderActive(ctx);
    }
    function tabStateTxt(S) {
        if (S.stage === 'stopname') return R.t('capture.rec.tab.stopped');
        return S.status === 'paused' ? R.t('capture.rec.tab.paused') : 'REC';
    }
    function renderTabs(ctx) {
        const bar = ctx.mount.querySelector('#rcRecTabs');
        if (!bar) return;
        const show = sessions.size >= 2 || (sessions.size >= 1 && selSid === null);
        bar.hidden = !show;
        if (!show) { bar.innerHTML = ''; return; }
        const tabs = [...sessions.values()].map(S => `
            <button class="rc-rectabs__tab${S.sessionId === selSid ? ' is-active' : ''}${S.stage === 'active' && S.status !== 'paused' ? ' is-rec' : ''}" data-sid="${U.esc(S.sessionId)}">
                <span class="rc-rectabs__dot"></span>
                <span class="rc-rectabs__name">${U.esc(tabName(S))}</span>
                <span class="rc-rectabs__time rc-mono">${fmt(S.elapsedSec)}</span>
                <span class="rc-rectabs__state rc-mono">${tabStateTxt(S)}</span>
                ${(S.copilotSessionId || S.copilotActive) ? '<i class="fa-solid fa-wand-magic-sparkles" title="' + R.t('capture.rec.tab.copilot') + '"></i>' : ''}
            </button>`).join('');
        const plus = _limitHit ? '' : `<button class="rc-rectabs__tab rc-rectabs__tab--new${selSid === null ? ' is-active' : ''}" data-sid=""><i class="fa-solid fa-plus"></i> ${R.t('capture.rec.tab.new')}</button>`;
        bar.innerHTML = tabs + plus;
        bar.querySelectorAll('.rc-rectabs__tab').forEach(b => b.addEventListener('click', () => select(ctx, b.dataset.sid || null)));
    }
    function paintTab(S) {
        const bar = document.getElementById('rcRecTabs');
        if (!bar || bar.hidden) return;
        const b = [...bar.querySelectorAll('.rc-rectabs__tab')].find(x => x.dataset.sid === S.sessionId);
        if (!b) return;
        const t = b.querySelector('.rc-rectabs__time'); if (t) t.textContent = fmt(S.elapsedSec);
        const s = b.querySelector('.rc-rectabs__state'); if (s) s.textContent = tabStateTxt(S);
        b.classList.toggle('is-rec', S.stage === 'active' && S.status !== 'paused');
    }
    function dropSession(S) {
        haltS(S);
        sessions.delete(S.sessionId);
        _limitHit = false;
    }
    // Після save/discard: лишились інші записи → на перший із них; ні → як раніше.
    function afterDrop(ctx, fallback) {
        const first = sessions.keys().next();
        if (!first.done) select(ctx, first.value);
        else fallback();
    }

    async function loadModels() {
        try { const d = await R.api.models(); return Array.isArray(d) ? d : (d.models || []); }
        catch (_) { return []; }
    }
    function modelOpts() {
        return (_models && _models.length ? _models : [{ name: DEFAULT_MODEL, info: {} }]).map(m => {
            const inf = m.info || {};
            const hint = [inf.size, inf.speed].filter(Boolean).join(', ');
            return `<option value="${U.esc(m.name)}"${m.name === DEFAULT_MODEL ? ' selected' : ''}>${U.esc(m.name)}${hint ? ' — ' + U.esc(hint) : ''}</option>`;
        }).join('');
    }
    function langOpts() { return LANGS.map(l => `<option value="${l.v}"${l.v === 'uk' ? ' selected' : ''}>${R.t(l.l)}</option>`).join(''); }
    // Дерево напрямків: <optgroup «Напрямок»> + «— Проєкт» усередині. Під
    // селектом живий рядок шляху (`catPathLine`) — у закритому селекті видно
    // лише «— Проєкт», без напрямку-батька.
    function catOpts() {
        return UI.catOptions('', { first: 'none' });
    }
    // Рядок «Напрямок → Проєкт» під селектом напрямку. Вішається на будь-який
    // .rc-catsel рекордера: показує повний шлях обраного вузла (або нічого).
    function bindCatPath(sel) {
        if (!sel || !sel.parentNode) return;
        if (sel.parentNode.querySelector('[data-catpath]')) return;   // ідемпотентно: wireCopilot кличеться на кожен paint
        const line = document.createElement('div');
        line.setAttribute('data-catpath', '');
        line.className = 'rc-mono';
        line.style.cssText = 'font-size:var(--rc-t-xs);color:var(--rc-ink-3);margin-top:4px;min-height:1.1em';
        sel.parentNode.appendChild(line);
        const paint = () => {
            const p = sel.value ? UI.catPath(Number(sel.value)) : '';
            line.textContent = p ? p : (sel.value ? '' : R.t('capture.rec.noCategory'));
        };
        sel.addEventListener('change', paint);
        paint();
    }

    // ---- Co-pilot mega-settings (Phase 19, Крок 1) -------------------------
    function cpOpts(arr, def) {
        return arr.map(o => `<option value="${o.v}"${o.v === def ? ' selected' : ''}>${R.t(o.l)}</option>`).join('');
    }
    function copilotPanel() {
        if (!_cpAvail || !_cpAvail.enabled) return '';   // фіча вимкнена на бекенді
        const status = _cpAvail.local_llm_available
            ? `<span class="rc-mono" style="color:var(--rc-ok)"><i class="fa-solid fa-circle-check"></i> ${R.t('capture.rec.cp.localReady')}</span>`
            : `<span class="rc-mono" style="color:var(--rc-warn,#fb8c00)" title="${U.esc(_cpAvail.local_llm_reason || '')}"><i class="fa-solid fa-circle-exclamation"></i> ${R.t('capture.rec.cp.ollamaNotReady')}</span>`;
        // T5.2 (REMEDIATION_PLAN Волна 3): режим/важливість/бюджет — power-user
        // налаштування, сховані за вкладеним "Розширені" — новий користувач, що
        // розгортає "Ко-пілот наживо" на старті першого дзвінка, бачить лише
        // напрямок + просте "лише локально" перемикання + пояснення поточного
        // дефолту (medium/medium), а не 3 селекти й числове поле бюджету одразу.
        return `
            <details class="rc-rec-copilot" id="rcCopilot">
                <summary style="cursor:pointer;font-weight:600"><i class="fa-solid fa-wand-magic-sparkles"></i> ${R.t('capture.rec.cp.summary')} <span style="margin-left:8px;font-weight:400">${status}</span></summary>
                <div style="margin-top:12px">
                    <div class="rc-field"><label class="rc-field__label" for="rcCpVector">${R.t('capture.rec.cp.vector')}</label><select class="rc-select rc-catsel" id="rcCpVector" data-cat-first="none">${catOpts()}</select></div>
                    <div class="rc-field" style="margin-top:10px"><label class="rc-field__label" for="rcCpScope">${R.t('capture.rec.cp.scopeLabel')}</label><input class="rc-input" id="rcCpScope" type="text" placeholder="${R.t('capture.rec.cp.scopePh')}"><div class="rc-rec-hint rc-mono" style="margin-top:4px">${R.t('capture.rec.cp.scopeHint')}</div></div>
                    <label class="rc-check" style="margin-top:10px"><input type="checkbox" id="rcCpLocalOnly"> <span>${R.t('capture.rec.cp.localOnly')}</span></label>
                    <div class="rc-rec-hint rc-mono" id="rcCpHint" style="margin-top:10px"></div>
                    <details style="margin-top:12px">
                        <summary class="rc-mono" style="cursor:pointer;color:var(--rc-ink-3);font-size:var(--rc-t-xs)">${R.t('capture.rec.cp.advanced')}</summary>
                        <div style="margin-top:10px">
                            <div class="rc-yt__settings">
                                <div class="rc-field"><label class="rc-field__label" for="rcCpMode">${R.t('capture.rec.cp.mode')}</label><select class="rc-select" id="rcCpMode">${cpOpts(CP_MODES, 'medium')}</select></div>
                                <div class="rc-field"><label class="rc-field__label" for="rcCpImportance">${R.t('capture.rec.cp.importance')}</label><select class="rc-select" id="rcCpImportance">${cpOpts(CP_IMP, 'medium')}</select></div>
                            </div>
                            <div class="rc-field" style="max-width:220px;margin-top:10px"><label class="rc-field__label" for="rcCpBudget">${R.t('capture.rec.cp.budget')}</label><input class="rc-input" id="rcCpBudget" type="number" min="0" step="0.5" placeholder="${R.t('capture.rec.cp.budgetPh')}"></div>
                        </div>
                    </details>
                </div>
            </details>`;
    }
    function cpHintText(ctx) {
        const get = id => (ctx.mount.querySelector(id) || {}).value;
        const mode = get('#rcCpMode') || 'medium';
        const imp = get('#rcCpImportance') || 'medium';
        const localOnly = !!(ctx.mount.querySelector('#rcCpLocalOnly') || {}).checked;
        const where = (localOnly || imp === 'low')
            ? R.t('capture.rec.cp.whereLocal')
            : R.t('capture.rec.cp.whereHybrid');
        return R.t('capture.rec.cp.hintText', { mode: R.t((CP_MODES.find(m => m.v === mode) || {}).l), imp: R.t((CP_IMP.find(i => i.v === imp) || {}).l), where });
    }
    function wireCopilot(ctx) {
        if (!ctx.mount.querySelector('#rcCopilot')) return;
        bindCatPath(ctx.mount.querySelector('#rcCpVector'));
        const localOnly = ctx.mount.querySelector('#rcCpLocalOnly');
        const budget = ctx.mount.querySelector('#rcCpBudget');
        const hint = ctx.mount.querySelector('#rcCpHint');
        const sync = () => {
            if (budget) budget.disabled = !!(localOnly && localOnly.checked);
            if (hint) hint.textContent = cpHintText(ctx);
        };
        ['#rcCpMode', '#rcCpImportance', '#rcCpLocalOnly'].forEach(id => {
            const el = ctx.mount.querySelector(id);
            if (el) el.addEventListener('change', sync);
        });
        sync();
    }
    function collectCopilot(ctx) {
        if (!_cpAvail || !_cpAvail.enabled) return null;
        const get = id => (ctx.mount.querySelector(id) || {}).value;
        const localOnly = !!(ctx.mount.querySelector('#rcCpLocalOnly') || {}).checked;
        const cp = { mode: get('#rcCpMode') || 'medium', importance: get('#rcCpImportance') || 'medium' };
        const vec = get('#rcCpVector');
        if (vec) cp.category_id = vec;
        const scope = (get('#rcCpScope') || '').trim();
        if (scope) cp.scope_projects = scope;   // «Маяк, Олена» → зріз пошуку (Трек 2/3)
        if (localOnly) cp.api_enabled = false;   // інакше — дефолт за важливістю (бекенд)
        const b = get('#rcCpBudget');
        if (b && !localOnly) cp.budget_usd = parseFloat(b);
        return cp;
    }

    // ---- video section helpers -----------------------------------------------
    function videoSection() {
        const avail = G.videoAvailable;
        const disabledAttr = avail ? '' : ' disabled';
        const hint = avail ? '' : `<span class="rc-rec-hint rc-mono">${R.t('capture.rec.video.unavailable')}</span>`;
        const cards = G.screens.map(s => {
            const primaryBadge = s.is_primary ? `<span class="rc-screen-card__badge">PRIMARY</span>` : '';
            const thumb = s.thumbnail ? `<img class="rc-screen-card__thumb" src="${U.esc(s.thumbnail)}" alt="">` : '';
            const reg = G.videoRegions[String(s.monitor_index)];
            const regionChip = (reg && reg.mode === 'region' && reg.region)
                ? `<span class="rc-screen-region-chip rc-mono">${R.t('capture.rec.video.regionChip', { w: reg.region.w, h: reg.region.h })}</span>` : '';
            return `<div class="rc-screen-card-wrap" data-monitor="${s.monitor_index}">
                <label class="rc-screen-card${s.is_primary ? ' is-primary' : ''}">
                    <input type="checkbox" value="${s.monitor_index}"${s.is_primary ? ' checked' : ''}>
                    ${thumb}
                    <span class="rc-screen-card__info">
                        <span class="rc-screen-card__label">${U.esc(s.label || ('Monitor ' + s.monitor_index))}</span>
                        <span class="rc-screen-card__res rc-mono">${s.width}×${s.height}</span>
                        ${primaryBadge}
                    </span>
                </label>
                <div class="rc-screen-mode" id="rcScreenMode_${s.monitor_index}" hidden>
                    <div class="rc-screen-mode__toggle">
                        <button class="rc-screen-mode__btn is-active" data-monitor="${s.monitor_index}" data-mode="full">${R.t('capture.rec.video.full')}</button>
                        <button class="rc-screen-mode__btn" data-monitor="${s.monitor_index}" data-mode="region">${R.t('capture.rec.video.region')}</button>
                    </div>
                    <button class="rc-screen-region-pick" data-monitor="${s.monitor_index}" id="rcPickRegion_${s.monitor_index}" hidden>
                        <i class="fa-solid fa-crop-simple"></i> ${R.t('capture.rec.video.pick')}
                    </button>
                    ${regionChip}
                </div>
            </div>`;
        }).join('');
        return `
            <div class="rc-rec-dev">
                <label class="rc-check"><input type="checkbox" id="rcRecVideoEn"${disabledAttr}>
                    📺 <span>${R.t('capture.rec.video.record')}</span></label>
                ${hint}
            </div>
            <div class="rc-rec-screens" id="rcRecScreens" hidden>${cards}</div>`;
    }
    function wireVideoToggle(ctx) {
        const venc = ctx.mount.querySelector('#rcRecVideoEn');
        const screens = ctx.mount.querySelector('#rcRecScreens');
        if (!venc || !screens) return;
        venc.addEventListener('change', () => { screens.hidden = !venc.checked; });

        // wire per-card checkbox → show/hide mode controls
        screens.querySelectorAll('.rc-screen-card-wrap').forEach(wrap => {
            const idx = wrap.dataset.monitor;
            const cb = wrap.querySelector('input[type=checkbox]');
            const modeRow = wrap.querySelector('#rcScreenMode_' + idx);
            if (!cb || !modeRow) return;
            const syncMode = () => { modeRow.hidden = !cb.checked; };
            cb.addEventListener('change', syncMode);
            syncMode();
        });

        // wire mode toggle buttons (Повний / Область)
        screens.addEventListener('click', e => {
            const btn = e.target.closest('.rc-screen-mode__btn');
            if (!btn) return;
            const idx = btn.dataset.monitor;
            const mode = btn.dataset.mode;
            const wrap = screens.querySelector('.rc-screen-card-wrap[data-monitor="' + idx + '"]');
            if (!wrap) return;
            // update button active state
            wrap.querySelectorAll('.rc-screen-mode__btn').forEach(b => b.classList.toggle('is-active', b.dataset.mode === mode));
            // show/hide pick-region button
            const pickBtn = wrap.querySelector('#rcPickRegion_' + idx);
            if (pickBtn) pickBtn.hidden = (mode !== 'region');
            // update state
            if (mode === 'full') {
                G.videoRegions[String(idx)] = { mode: 'full', region: null };
                updateRegionChip(wrap, null);
            } else {
                // switching to region: keep existing region if any, just show pick button
                if (!G.videoRegions[String(idx)] || G.videoRegions[String(idx)].mode !== 'region') {
                    G.videoRegions[String(idx)] = { mode: 'region', region: null };
                }
                updateRegionChip(wrap, (G.videoRegions[String(idx)] || {}).region);
            }
        });

        // wire pick-region buttons
        screens.addEventListener('click', e => {
            const btn = e.target.closest('.rc-screen-region-pick');
            if (!btn) return;
            const idx = +btn.dataset.monitor;
            const screenInfo = G.screens.find(s => s.monitor_index === idx);
            if (screenInfo) openRegionModal(idx, screenInfo);
        });
    }

    // ---- region modal ---------------------------------------------------------
    function updateRegionChip(wrap, region) {
        let chip = wrap.querySelector('.rc-screen-region-chip');
        if (region) {
            if (!chip) {
                chip = document.createElement('span');
                chip.className = 'rc-screen-region-chip rc-mono';
                wrap.querySelector('.rc-screen-mode').appendChild(chip);
            }
            chip.textContent = R.t('capture.rec.region.chip', { w: region.w, h: region.h });
        } else {
            if (chip) chip.remove();
        }
    }

    function openRegionModal(monitorIdx, screenInfo) {
        // remove any existing modal
        const existing = document.querySelector('.rc-region-modal');
        if (existing) existing.remove();

        const modal = document.createElement('div');
        modal.className = 'rc-region-modal';
        modal.innerHTML = `
            <div class="rc-region-panel">
                <div class="rc-region-panel__head">
                    <span class="rc-region-panel__title"><i class="fa-solid fa-crop-simple"></i> ${U.esc(R.t('capture.rec.region.title', { label: screenInfo.label || ('Monitor ' + monitorIdx) }))}</span>
                    <button class="rc-region-close" id="rcRegionCancel" title="${R.t('capture.rec.cancel')}"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="rc-region-stage-wrap">
                    <div class="rc-region-stage" id="rcRegionStage">
                        <div class="rc-region-loading rc-mono" id="rcRegionLoading"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('capture.rec.region.loading')}</div>
                        <div class="rc-region-rect" id="rcRegionRect" hidden></div>
                    </div>
                </div>
                <div class="rc-region-size rc-mono" id="rcRegionSize"></div>
                <div class="rc-region-inputs">
                    <div class="rc-region-field"><label>X</label><input class="rc-input rc-mono" id="rcRiX" type="number" min="0" step="2" placeholder="0"></div>
                    <div class="rc-region-field"><label>Y</label><input class="rc-input rc-mono" id="rcRiY" type="number" min="0" step="2" placeholder="0"></div>
                    <div class="rc-region-field"><label>W</label><input class="rc-input rc-mono" id="rcRiW" type="number" min="2" step="2" placeholder="${screenInfo.width}"></div>
                    <div class="rc-region-field"><label>H</label><input class="rc-input rc-mono" id="rcRiH" type="number" min="2" step="2" placeholder="${screenInfo.height}"></div>
                </div>
                <div class="rc-region-panel__foot">
                    <button class="rc-btn" id="rcRegionCancelBtn"><i class="fa-solid fa-xmark"></i> ${R.t('capture.rec.cancel')}</button>
                    <div style="flex:1"></div>
                    <button class="rc-btn rc-btn--primary" id="rcRegionDone"><i class="fa-solid fa-check"></i> ${R.t('capture.rec.region.done')}</button>
                </div>
            </div>`;
        document.body.appendChild(modal);

        // current saved region (if any) to pre-populate
        const saved = (G.videoRegions[String(monitorIdx)] || {}).region || null;

        // fetch preview image
        const stage = modal.querySelector('#rcRegionStage');
        const loading = modal.querySelector('#rcRegionLoading');
        const rectEl = modal.querySelector('#rcRegionRect');
        const sizeEl = modal.querySelector('#rcRegionSize');
        let stageImg = null;
        let dragState = null;    // { startX, startY } in stage-relative pixels
        let currentRect = null;  // { x, y, w, h } in display pixels (stage-relative)

        R.api.get('/api/recording/screens/' + monitorIdx + '/preview?w=1000').then(data => {
            if (!data || !data.image) throw new Error(R.t('capture.rec.region.noImage'));
            loading.remove();
            const img = document.createElement('img');
            img.className = 'rc-region-img';
            img.src = data.image;
            img.draggable = false;
            stage.appendChild(img);
            stageImg = img;

            // if there's a saved region, pre-draw it
            if (saved) {
                const scale = data.monitor_width / img.clientWidth;
                const dispX = saved.x / scale;
                const dispY = saved.y / scale;
                const dispW = saved.w / scale;
                const dispH = saved.h / scale;
                currentRect = { x: dispX, y: dispY, w: dispW, h: dispH };
                applyRect();
                populateInputs(saved.x, saved.y, saved.w, saved.h);
                updateSizeLabel(saved.w, saved.h);
            }
        }).catch(err => {
            loading.innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="color:var(--rc-err)"></i> ${U.esc(R.t('capture.rec.region.previewFailed', { error: err && err.message || '' }))}`;
        });

        function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
        function evenFloor(v) { return Math.floor(v / 2) * 2; }

        function applyRect() {
            if (!currentRect) { rectEl.hidden = true; return; }
            rectEl.hidden = false;
            rectEl.style.left   = currentRect.x + 'px';
            rectEl.style.top    = currentRect.y + 'px';
            rectEl.style.width  = currentRect.w + 'px';
            rectEl.style.height = currentRect.h + 'px';
        }

        function getRealRegion() {
            if (!stageImg || !currentRect) return null;
            const scale = (screenInfo.width / stageImg.clientWidth) || 1;
            const x = evenFloor(clamp(Math.round(currentRect.x * scale), 0, screenInfo.width));
            const y = evenFloor(clamp(Math.round(currentRect.y * scale), 0, screenInfo.height));
            const w = evenFloor(clamp(Math.round(currentRect.w * scale), 2, screenInfo.width - x));
            const h = evenFloor(clamp(Math.round(currentRect.h * scale), 2, screenInfo.height - y));
            return { x, y, w, h };
        }

        function updateSizeLabel(w, h) {
            sizeEl.textContent = (w && h) ? `${w} × ${h} px` : '';
        }

        function populateInputs(x, y, w, h) {
            const set = (id, v) => { const el = modal.querySelector(id); if (el) el.value = String(v); };
            set('#rcRiX', x); set('#rcRiY', y); set('#rcRiW', w); set('#rcRiH', h);
        }

        // pointer drag on stage
        stage.addEventListener('pointerdown', e => {
            if (!stageImg) return;
            e.preventDefault();
            stage.setPointerCapture(e.pointerId);
            const r = stageImg.getBoundingClientRect();
            const sx = clamp(e.clientX - r.left, 0, stageImg.clientWidth);
            const sy = clamp(e.clientY - r.top, 0, stageImg.clientHeight);
            dragState = { startX: sx, startY: sy };
            currentRect = { x: sx, y: sy, w: 0, h: 0 };
            applyRect();
        });
        stage.addEventListener('pointermove', e => {
            if (!dragState || !stageImg) return;
            const r = stageImg.getBoundingClientRect();
            const cx = clamp(e.clientX - r.left, 0, stageImg.clientWidth);
            const cy = clamp(e.clientY - r.top, 0, stageImg.clientHeight);
            currentRect = {
                x: Math.min(dragState.startX, cx),
                y: Math.min(dragState.startY, cy),
                w: Math.abs(cx - dragState.startX),
                h: Math.abs(cy - dragState.startY),
            };
            applyRect();
            const real = getRealRegion();
            if (real) { updateSizeLabel(real.w, real.h); populateInputs(real.x, real.y, real.w, real.h); }
        });
        stage.addEventListener('pointerup', e => {
            if (!dragState) return;
            dragState = null;
            stage.releasePointerCapture(e.pointerId);
            const real = getRealRegion();
            if (real) { updateSizeLabel(real.w, real.h); populateInputs(real.x, real.y, real.w, real.h); }
        });

        // numeric input sync → rect
        ['#rcRiX', '#rcRiY', '#rcRiW', '#rcRiH'].forEach(id => {
            const el = modal.querySelector(id);
            if (!el) return;
            el.addEventListener('input', () => {
                const getVal = (qid) => { const e2 = modal.querySelector(qid); return e2 && e2.value !== '' ? parseInt(e2.value, 10) : null; };
                const rx = getVal('#rcRiX'), ry = getVal('#rcRiY'), rw = getVal('#rcRiW'), rh = getVal('#rcRiH');
                if (rx == null || ry == null || rw == null || rh == null || rw <= 0 || rh <= 0) return;
                if (!stageImg) return;
                const scale = (screenInfo.width / stageImg.clientWidth) || 1;
                currentRect = { x: rx / scale, y: ry / scale, w: rw / scale, h: rh / scale };
                applyRect();
                updateSizeLabel(rw, rh);
            });
        });

        // cancel
        const cancelFn = () => { modal.remove(); };
        modal.querySelector('#rcRegionCancel').addEventListener('click', cancelFn);
        modal.querySelector('#rcRegionCancelBtn').addEventListener('click', cancelFn);
        modal.addEventListener('click', e => { if (e.target === modal) cancelFn(); });

        // done
        modal.querySelector('#rcRegionDone').addEventListener('click', () => {
            const real = getRealRegion();
            if (!real || real.w < 2 || real.h < 2) {
                // allow saving with numeric-only input even without image
                const getVal = (qid) => { const el = modal.querySelector(qid); return el && el.value !== '' ? parseInt(el.value, 10) : null; };
                const rx = getVal('#rcRiX') || 0, ry = getVal('#rcRiY') || 0;
                const rw = getVal('#rcRiW'), rh = getVal('#rcRiH');
                if (!rw || !rh || rw < 2 || rh < 2) {
                    UI.toast(R.t('capture.rec.region.needRegion'), 'error');
                    return;
                }
                const r2 = { x: evenFloor(rx), y: evenFloor(ry), w: evenFloor(rw), h: evenFloor(rh) };
                G.videoRegions[String(monitorIdx)] = { mode: 'region', region: r2 };
                refreshCardChip(monitorIdx, r2);
                modal.remove();
                return;
            }
            G.videoRegions[String(monitorIdx)] = { mode: 'region', region: real };
            refreshCardChip(monitorIdx, real);
            modal.remove();
        });
    }

    function refreshCardChip(monitorIdx, region) {
        const wrap = document.querySelector('.rc-screen-card-wrap[data-monitor="' + monitorIdx + '"]');
        if (wrap) updateRegionChip(wrap, region);
    }

    // ---- stage: setup ------------------------------------------------------
    function renderSetup(ctx) {
        const mics = G.devices.filter(d => d.kind === 'mic');
        const lbs = G.devices.filter(d => d.kind === 'loopback');
        const pre = G.preset;
        const preMic = pre && pre.mic && mics.some(d => d.name === pre.mic) ? pre.mic : null;
        const isSel = d => (d.kind === 'mic' && preMic) ? d.name === preMic : d.is_default;
        const devOpts = (arr, empty) => arr.length
            ? arr.map(d => `<option value="${d.index}" data-name="${U.esc(d.name)}"${isSel(d) ? ' selected' : ''}>${U.esc(d.name)}${d.is_default ? R.t('capture.rec.dev.default') : ''}</option>`).join('')
            : `<option value="">${empty}</option>`;
        const presetBlock = pre ? `
                <div class="rc-recsrc rc-recsrc--prompt" id="rcRecPreset">
                    <div class="rc-recsrc__head"><i class="fa-solid fa-phone"></i> ${R.t('capture.rec.preset.head')}</div>
                    <label class="rc-check"><input type="checkbox" id="rcRecPresetOnly" checked> <span>${U.esc(R.t('capture.rec.preset.only', { label: pre.label }))}</span></label>
                </div>` : '';
        const second = liveSessions().length > 0;

        const body = ctx.mount.querySelector('#rcRecBody');
        body.innerHTML = `
            <div class="rc-rec-setup">
                <div class="rc-rec-dev">
                    <label class="rc-check"><input type="checkbox" id="rcRecMicEn" ${mics.length ? 'checked' : ''} ${mics.length ? '' : 'disabled'}>
                        <i class="fa-solid fa-microphone"></i> <span>${R.t('capture.rec.mic')}</span></label>
                    <select class="rc-select" id="rcRecMic" ${mics.length ? '' : 'disabled'}>${devOpts(mics, R.t('capture.rec.noMics'))}</select>
                </div>
                <div class="rc-rec-dev">
                    <label class="rc-check"><input type="checkbox" id="rcRecSysEn" ${lbs.length ? 'checked' : ''} ${lbs.length ? '' : 'disabled'}>
                        <i class="fa-solid fa-volume-high"></i> <span>${R.t('capture.rec.sys')}</span></label>
                    <select class="rc-select" id="rcRecSys" ${lbs.length ? '' : 'disabled'}>${devOpts(lbs, R.t('capture.rec.noLoopback'))}</select>
                </div>
                ${presetBlock}
                ${videoSection()}
                <div class="rc-field" style="max-width:260px">
                    <label class="rc-field__label" for="rcRecLang">${R.t('capture.rec.liveLang')}</label>
                    <select class="rc-select" id="rcRecLang">${langOpts()}</select>
                </div>
                ${copilotPanel()}
                ${second ? appsPromptShell() : ''}
                <div class="rc-rec-hint rc-mono"><i class="fa-solid fa-shield-halved"></i> ${R.t('capture.rec.permHint')}</div>
                <div class="rc-yt__actions">
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcRecStart"><i class="fa-solid fa-circle" style="color:var(--rc-err)"></i> ${R.t('capture.rec.start')}</button>
                </div>
            </div>`;
        body.querySelector('#rcRecStart').addEventListener('click', () => start(ctx));
        wireVideoToggle(ctx);
        wireCopilot(ctx);
        if (second) loadAppsPrompt(ctx);
    }

    // ---- другий запис: звучні застосунки + два питання (Ask 2) --------------
    // «Виключити X з цього запису» — одна галочка на весь список (exclude несе
    // один застосунок; друга галочка перемикає). «Лишити в A тільки X?» — для
    // застосунків, які вже пише інший запис; відповідь застосовується до A лише
    // ПІСЛЯ успішного старту нового запису.
    function appsPromptShell() {
        return `
            <div class="rc-recsrc rc-recsrc--prompt" id="rcRecApps">
                <div class="rc-recsrc__head"><i class="fa-solid fa-volume-high"></i> ${R.t('capture.rec.apps.head')}</div>
                <div id="rcRecAppsList" class="rc-mono rc-recsrc__muted"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('capture.rec.apps.searching')}</div>
            </div>
            <div class="rc-recwarn">
                <div><i class="fa-solid fa-headphones"></i> ${R.t('capture.rec.apps.tipHeadphones')}</div>
                <div><i class="fa-solid fa-window-restore"></i> ${R.t('capture.rec.apps.tipBrowser')}</div>
            </div>`;
    }
    async function loadAppsPrompt(ctx) {
        G.exclude = null; G.includeFor = {};
        let d = null;
        try { d = await R.api.get('/api/recording/apps'); } catch (_) { d = null; }
        if (!ctx.isCurrent() || selSid !== null) return;
        const box = ctx.mount.querySelector('#rcRecAppsList');
        if (!box) return;
        const apps = (d && Array.isArray(d.apps)) ? d.apps : [];
        if (!d || d.available === false || !apps.length) {
            box.textContent = (d && d.available === false)
                ? R.t('capture.rec.apps.unavailable', { reason: d.reason ? ' (' + d.reason + ')' : '' })
                : R.t('capture.rec.apps.noneSounding');
            return;
        }
        box.classList.remove('rc-mono', 'rc-recsrc__muted');
        box.innerHTML = apps.map(a => {
            const by = (a.recorded_by || []).map(sid => sessions.get(sid)).filter(S => S && S.stage === 'active');
            const badges = by.map(S => `<span class="rc-recsrc__badge rc-mono">${U.esc(R.t('capture.rec.apps.inRecording', { name: tabName(S) }))}</span>`).join('');
            const onlyQ = by.map(S => `
                <label class="rc-check"><input type="checkbox" data-incl="${U.esc(a.exe)}" data-sid="${U.esc(S.sessionId)}"> <span>${U.esc(R.t('capture.rec.apps.keepOnly', { name: tabName(S), app: a.label || a.exe }))}</span></label>`).join('');
            return `
                <div class="rc-recsrc__app">
                    <div class="rc-recsrc__appname"><b>${U.esc(a.label || a.exe)}</b> <span class="rc-mono rc-recsrc__muted">${a.state === 'active' ? R.t('capture.rec.apps.sounding') : R.t('capture.rec.apps.quiet')}</span> ${badges}</div>
                    <label class="rc-check"><input type="checkbox" data-excl="${U.esc(a.exe)}"> <span>${U.esc(R.t('capture.rec.apps.exclude', { app: a.label || a.exe }))}</span></label>
                    ${onlyQ}
                </div>`;
        }).join('');
        box.addEventListener('change', e => {
            const cb = e.target;
            if (!cb || cb.type !== 'checkbox') return;
            if (cb.dataset.excl != null) {
                box.querySelectorAll('input[data-excl]').forEach(x => { if (x !== cb) x.checked = false; });
                G.exclude = cb.checked ? cb.dataset.excl : null;
            } else if (cb.dataset.incl != null) {
                const sid = cb.dataset.sid;
                const set = G.includeFor[sid] || (G.includeFor[sid] = []);
                const i = set.indexOf(cb.dataset.incl);
                if (cb.checked && i < 0) {
                    if (set.length >= 4) { cb.checked = false; UI.toast(R.t('capture.rec.apps.max4'), 'error'); return; }
                    set.push(cb.dataset.incl);
                } else if (!cb.checked && i >= 0) set.splice(i, 1);
            }
        });
    }

    async function start(ctx) {
        const micEn = ctx.mount.querySelector('#rcRecMicEn');
        const sysEn = ctx.mount.querySelector('#rcRecSysEn');
        const micSel = ctx.mount.querySelector('#rcRecMic');
        const sysSel = ctx.mount.querySelector('#rcRecSys');
        const payload = {};
        if (micEn && micEn.checked && micSel && micSel.value) {
            payload.mic_device_index = parseInt(micSel.value, 10);
            const o = micSel.options[micSel.selectedIndex];
            payload.mic_device_name = o ? o.dataset.name : null;
        }
        if (sysEn && sysEn.checked && sysSel && sysSel.value) {
            payload.system_device_index = parseInt(sysSel.value, 10);
            const o = sysSel.options[sysSel.selectedIndex];
            payload.system_device_name = o ? o.dataset.name : null;
        }
        // K2: передвибір дзвінка — системний звук лише цього застосунку (include,
        // пристрій loopback тоді не потрібен).
        const preOnly = G.preset && ctx.mount.querySelector('#rcRecPresetOnly');
        if (preOnly && preOnly.checked && sysEn && sysEn.checked) {
            delete payload.system_device_index;
            delete payload.system_device_name;
            payload.system_source = { mode: 'include', apps: [G.preset.app] };
        }
        if (payload.mic_device_index == null && payload.system_device_index == null && !payload.system_source) {
            UI.toast(R.t('capture.rec.pickStream'), 'error'); return;
        }
        const langSel = ctx.mount.querySelector('#rcRecLang');
        if (langSel && langSel.value) payload.language = langSel.value;

        const cp = collectCopilot(ctx);
        if (cp) payload.copilot = cp;
        // Питання про звучні застосунки (лише коли вже йде інший запис).
        const others = liveSessions();
        if (others.length && payload.system_device_index != null && G.exclude) {
            payload.system_source = { mode: 'exclude', apps: [G.exclude] };
        }
        const narrow = others.length ? Object.assign({}, G.includeFor) : {};

        // video capture (S7 + R-D): only add the key when the checkbox is checked
        const venc = ctx.mount.querySelector('#rcRecVideoEn');
        if (venc && venc.checked) {
            const picked = [...ctx.mount.querySelectorAll('.rc-screen-card-wrap input[type=checkbox]:checked')]
                .map(cb => {
                    const idx = +cb.value;
                    const track = { monitor_index: idx, fps: 30, codec: 'h264_nvenc', quality: 'p5' };
                    const reg = G.videoRegions[String(idx)];
                    if (reg && reg.mode === 'region' && reg.region) {
                        track.mode = 'region';
                        track.region = reg.region;
                    } else {
                        track.mode = 'full';
                    }
                    return track;
                });
            if (picked.length) payload.video = { enabled: true, tracks: picked };
        }

        const btn = ctx.mount.querySelector('#rcRecStart');
        if (btn) btn.disabled = true;
        try {
            const r = await R.api.post('/api/recording/start', payload);
            if (!r || !r.session_id) throw new Error((r && r.error) || R.t('capture.rec.startFailed'));
            if (!ctx.isCurrent()) return;
            const S = newS(r.session_id);
            S.copilotConfig = cp || null;
            S.copilotSessionId = r.copilot_session_id || null;
            S.copilotActive = !!S.copilotSessionId;
            if (r.copilot && r.copilot.error_code === 'COPILOT_BUSY') S.cpOffer = r.copilot.owner_session_id || null;
            S.hasSystem = payload.system_device_index != null || !!payload.system_source;
            if (G.preset) { G.preset = null; history.replaceState(null, '', '/record'); }
            S.systemSource = r.system_source || (S.hasSystem ? (payload.system_source || { mode: 'all', apps: [] }) : null);
            // pre-populate videoTracks from the start response if video is enabled
            if (r.video && r.video.enabled && Array.isArray(r.video.tracks)) {
                for (const t of r.video.tracks) {
                    S.videoTracks[String(t.track_id != null ? t.track_id : t.monitor_index)] = {
                        track_id: t.track_id != null ? t.track_id : t.monitor_index,
                        monitor_index: t.monitor_index,
                        monitor_label: t.monitor_label || ('Monitor ' + t.monitor_index),
                        status: 'starting',
                        mode: t.mode || 'full',
                        region: t.region || null,
                        fps: null, dropped: null, bytes: null,
                    };
                }
            }
            sessions.set(S.sessionId, S);
            G.exclude = null; G.includeFor = {};
            connectSSE(ctx, S);
            startTick(ctx, S);
            select(ctx, S.sessionId);
            // start-відповідь несе лише monitor_index; справжні track_id ('mon0',…) знає
            // супервізор. Звіряємось зі state трохи згодом, щоб чипи стали канонічними
            // (правильна мітка/статус + region) і live video_stats почали потрапляти в ціль.
            if (r.video && r.video.enabled) reconcileVideoTracks(ctx, S);
            // «Лишити в A тільки X» — лише тепер, коли новий запис точно пішов.
            for (const sid in narrow) {
                if (narrow[sid] && narrow[sid].length && sessions.has(sid)) {
                    setSystemSource(ctx, sessions.get(sid), { mode: 'include', apps: narrow[sid].slice(), reason: 'user' });
                }
            }
        } catch (err) {
            if (errCode(err) === 'SESSION_LIMIT') {
                _limitHit = true;
                const lim = err.body && err.body.limit;
                UI.toast(ERR_TXT.SESSION_LIMIT + (lim ? ' (' + lim + ')' : ''), 'error');
                const first = sessions.keys().next();
                if (!first.done) { select(ctx, first.value); return; }
            } else {
                errToast(err, R.t('capture.rec.startFailed'));
            }
            if (btn) btn.disabled = false;
        }
    }

    // ---- системний звук запису: весь / тільки ці / усе крім цих (Ask 4) -----
    // Тіло — рівно C5: {mode, apps, reason}. Відповідь (або подія source_changed)
    // оновлює картку; коди помилок — тости, не консоль.
    async function setSystemSource(ctx, S, spec) {
        const body = { mode: spec.mode, apps: spec.mode === 'all' ? [] : (spec.apps || []), reason: spec.reason || 'user' };
        try {
            const r = await R.api.post('/api/recording/' + S.sessionId + '/system-source', body);
            if (r && r.source) S.systemSource = r.source;
            if (r && r.applied_on_resume) UI.toast(R.t('capture.rec.appliedOnResume', { name: tabName(S) }), 'info');
            if (ctx.isCurrent()) withS(S, paintSource);
            return true;
        } catch (err) {
            errToast(err, tabName(S));
            return false;
        }
    }
    const SRC_LBL = { all: 'capture.rec.src.all', include: 'capture.rec.src.include', exclude: 'capture.rec.src.exclude' };
    function sourceHTML() {
        if (!st.hasSystem) return '';
        return `<div class="rc-recsrc" id="rcRecSrc"></div>`;
    }
    function paintSource() {
        const box = $id('rcRecSrc');
        if (!box) return;
        const src = st.systemSource || { mode: 'all', apps: [] };
        const apps = (src.apps || []).join(', ');
        box.innerHTML = `
            <div class="rc-recsrc__row">
                <span class="rc-recsrc__lbl rc-mono">SYS</span>
                <div class="rc-seg-toggle">${['all', 'include', 'exclude'].map(m =>
                    `<button type="button" data-mode="${m}" class="${src.mode === m ? 'is-active' : ''}">${R.t(SRC_LBL[m])}</button>`).join('')}</div>
                ${apps && src.mode !== 'all' ? `<span class="rc-recsrc__muted rc-mono">${U.esc(apps)}</span>` : ''}
            </div>
            <div class="rc-recsrc__pick" id="rcRecSrcPick" hidden></div>`;
        const S = st;
        box.querySelectorAll('.rc-seg-toggle button').forEach(b => b.addEventListener('click', () => {
            const mode = b.dataset.mode;
            if (mode === 'all') { if (src.mode !== 'all') setSystemSource(currentCtx, S, { mode: 'all', reason: 'user' }); return; }
            openSourcePick(S, mode);
        }));
    }
    async function openSourcePick(S, mode) {
        const pick = withS(S, () => $id('rcRecSrcPick'));
        if (!pick) return;
        pick.hidden = false;
        pick.innerHTML = `<span class="rc-mono rc-recsrc__muted"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('capture.rec.apps.searching')}</span>`;
        let d = null;
        try { d = await R.api.get('/api/recording/apps'); } catch (_) { d = null; }
        if (!pick.isConnected) return;
        const cur = (S.systemSource && S.systemSource.mode === mode) ? (S.systemSource.apps || []) : [];
        const byExe = new Map();
        ((d && d.apps) || []).forEach(a => byExe.set(a.exe, a.label || a.exe));
        cur.forEach(x => { if (!byExe.has(x)) byExe.set(x, x); });   // застосунок зараз мовчить — лишаємо у списку
        if (!byExe.size) {
            pick.innerHTML = `<span class="rc-mono rc-recsrc__muted">${(d && d.available === false) ? R.t('capture.rec.apps.listUnavailable') : R.t('capture.rec.apps.noneSoundingShort')}.</span>`;
            return;
        }
        const multi = mode === 'include';
        pick.innerHTML = `
            <div class="rc-recsrc__muted rc-mono">${multi ? R.t('capture.rec.apps.recordOnly') : R.t('capture.rec.apps.recordExcept')}</div>
            ${[...byExe.entries()].map(([exe, label]) => `<label class="rc-check"><input type="${multi ? 'checkbox' : 'radio'}" name="rcSrcPick" value="${U.esc(exe)}"${cur.indexOf(exe) >= 0 ? ' checked' : ''}> <span>${U.esc(label)}</span></label>`).join('')}
            <div class="rc-recsrc__row">
                <button type="button" class="rc-btn rc-btn--primary" data-act="apply">${R.t('capture.rec.apply')}</button>
                <button type="button" class="rc-btn" data-act="cancel">${R.t('capture.rec.cancel')}</button>
            </div>`;
        pick.querySelector('[data-act="cancel"]').addEventListener('click', () => { pick.hidden = true; pick.innerHTML = ''; });
        pick.querySelector('[data-act="apply"]').addEventListener('click', async () => {
            const apps = [...pick.querySelectorAll('input[name="rcSrcPick"]:checked')].map(x => x.value);
            if (!apps.length) { UI.toast(R.t('capture.rec.apps.pickOne'), 'error'); return; }
            if (multi && apps.length > 4) { UI.toast(R.t('capture.rec.apps.max4short'), 'error'); return; }
            await setSystemSource(currentCtx, S, { mode, apps, reason: 'user' });
        });
    }

    // ---- ко-пілот: де працює, «перенести сюди» (Ask 8) ----------------------
    function copilotOwner() {
        for (const S of sessions.values()) if (S.stage === 'active' && (S.copilotSessionId || S.copilotActive)) return S;
        return null;
    }
    function cpWhereHTML() {
        if (!_cpAvail || !_cpAvail.enabled) return '';
        if (sessions.size < 2 && !st.cpOffer) return '';   // один запис — як раніше: віджет сам усе показує
        return `<div class="rc-rec-hint rc-mono" id="rcRecCpWhere" style="margin-bottom:var(--rc-3)"></div>`;
    }
    function paintCpWhere() {
        const el = $id('rcRecCpWhere');
        if (!el) return;
        // Власник: з відповіді COPILOT_BUSY (owner_session_id), інакше — хто з табів його тримає.
        const ownerS = st.cpOffer ? sessions.get(st.cpOffer) : copilotOwner();
        const ownerSid = st.cpOffer || (ownerS && ownerS.sessionId) || null;
        const ownerName = ownerS ? tabName(ownerS) : R.t('capture.rec.cpbar.otherRecording');
        const here = st.copilotSessionId || st.copilotActive;
        let html;
        if (here) html = '<i class="fa-solid fa-wand-magic-sparkles"></i> ' + R.t('capture.rec.cpbar.here');
        else if (ownerSid && ownerSid !== st.sessionId) {
            html = `<i class="fa-solid fa-wand-magic-sparkles"></i> ${st.cpOffer ? R.t('capture.rec.cpbar.busyPrefix') : R.t('capture.rec.cpbar.prefix')}${U.esc(R.t('capture.rec.cpbar.onRecording', { name: ownerName }))}
                <button type="button" class="rc-btn rc-btn--sm" data-cpmove="${U.esc(ownerSid)}">${st.cpOffer ? U.esc(R.t('capture.rec.cpbar.moveFrom', { name: ownerName })) : R.t('capture.rec.cpbar.moveHere')}</button>`;
        } else html = '<i class="fa-solid fa-wand-magic-sparkles"></i> ' + R.t('capture.rec.cpbar.idle');
        el.innerHTML = html;
        const b = el.querySelector('[data-cpmove]');
        const S = st;
        if (b) b.addEventListener('click', () => moveCopilot(currentCtx, b.dataset.cpmove, S));
    }
    async function moveCopilot(ctx, fromSid, toS) {
        let r;
        try {
            r = await R.api.post('/api/copilot/move', { from_session_id: fromSid, to_session_id: toS.sessionId });
        } catch (err) { errToast(err, R.t('capture.rec.cpbar.moveFailed')); return; }
        const from = sessions.get(fromSid);
        if (from) { from.copilotSessionId = null; from.copilotActive = false; }
        toS.cpOffer = null;
        if (r && r.success) {
            toS.copilotSessionId = r.copilot_session_id || null;
            toS.copilotActive = true;
            if (!toS.copilotConfig && from) toS.copilotConfig = from.copilotConfig;
            toS.topics = []; toS.curTopic = null; toS.insights = []; toS.cpStatus = 'analyzing'; toS.cpUnseen = 0; toS.cpUsage = null;
            UI.toast(R.t('capture.rec.cpMoved', { name: tabName(toS) }), 'success');
        } else {
            UI.toast(R.t('capture.rec.cpStoppedHere', { name: from ? tabName(from) : fromSid }) + (r && r.reason ? ': ' + r.reason : ''), 'error');
        }
        if (ctx.isCurrent()) select(ctx, selSid);
    }

    // ---- stage: active -----------------------------------------------------
    function renderActive(ctx) {
        const body = ctx.mount.querySelector('#rcRecBody');
        const cp = !!st.copilotSessionId;
        body.innerHTML = `
            <div class="rc-rec-active${cp ? ' rc-rec-active--cp' : ''}">
                <div class="rc-rec-main">
                    <div class="rc-rec-top">
                        <span class="rc-rec-pill${st.status === 'paused' ? ' is-paused' : ''}" id="rcRecPill"><span class="rc-rec-dot"></span><span id="rcRecStatusTxt">${st.status === 'paused' ? 'PAUSED' : 'REC'}</span></span>
                        <span class="rc-rec-timer" id="rcRecTimer">${fmt(st.elapsedSec)}</span>
                    </div>
                    ${cpWhereHTML()}
                    <div class="rc-rec-vid-banner" id="rcRecSseBanner" hidden></div>
                    <div class="rc-rec-meta rc-mono" id="rcRecMeta"></div>
                    <div class="rc-rec-vid-banner" id="rcRecAudioBanner" hidden></div>
                    <div class="rc-vu" id="rcVuMic"><span class="rc-vu__lbl rc-mono">MIC</span><div class="rc-vu__bar"><div class="rc-vu__fill"></div><div class="rc-vu__peak"></div></div><span class="rc-vu__val rc-mono"></span></div>
                    <div class="rc-vu" id="rcVuSys"><span class="rc-vu__lbl rc-mono">SYS</span><div class="rc-vu__bar"><div class="rc-vu__fill"></div><div class="rc-vu__peak"></div></div><span class="rc-vu__val rc-mono"></span></div>
                    ${sourceHTML()}
                    <div class="rc-rec-vid" id="rcRecVid"></div>
                    <div class="rc-rec-vid-banner" id="rcRecVidBanner" hidden></div>
                    <div class="rc-rec-live" id="rcRecLive" hidden></div>
                    ${liveCommentHTML()}
                    <div class="rc-yt__actions" style="margin-top:24px">
                        <button class="rc-btn" id="rcRecDiscard"><i class="fa-solid fa-trash"></i> ${R.t('capture.rec.discard')}</button>
                        <div class="rc-yt__spacer"></div>
                        ${(sessions.size < 2 && !_limitHit) ? '<button class="rc-btn" id="rcRecNew" title="' + R.t('capture.rec.parallelTitle') + '"><i class="fa-solid fa-plus"></i> ' + R.t('capture.rec.tab.new') + '</button>' : ''}
                        <button class="rc-btn" id="rcRecPause"><i class="fa-solid ${st.status === 'paused' ? 'fa-play' : 'fa-pause'}"></i> <span id="rcRecPauseLbl">${st.status === 'paused' ? R.t('capture.rec.resume') : R.t('capture.rec.pause')}</span></button>
                        <button class="rc-btn rc-btn--primary" id="rcRecStop"><i class="fa-solid fa-stop"></i> ${R.t('capture.rec.stop')}</button>
                    </div>
                </div>
                ${cp ? copilotWidget() : ''}
            </div>`;
        body.querySelector('#rcRecPause').addEventListener('click', () => pauseResume(ctx));
        body.querySelector('#rcRecStop').addEventListener('click', () => stop(ctx));
        body.querySelector('#rcRecDiscard').addEventListener('click', () => discard(ctx));
        const nb = body.querySelector('#rcRecNew');
        if (nb) nb.addEventListener('click', () => select(ctx, null));
        wireLiveComment(ctx);
        if (cp) wireWidget();
        renderVideoChips();
        updateRecMeta();
        markStreamStatus();
        refreshAudioBanner();
        refreshPill();
        if (st.sseState === 'reconnecting') showSseBanner('reconnecting');
        else if (st.sseState === 'error') showSseBanner('failed');
        paintSegs(st.segs);
        paintSource();
        paintCpWhere();
    }

    // ---- живий коментар оператора (Волна 4) --------------------------------
    // Під час дзвінка руки зайняті розмовою, а не мишею, тож поле одне, завжди
    // на видноті, і закривається одним Enter. Ctrl+Shift+K фокусує його з
    // будь-якого місця сторінки — щоб думку можна було зафіксувати, не шукаючи
    // курсором, поки співрозмовник говорить.
    //
    // Момент запису беремо з таймера (st.elapsedSec), а не з годинника: якір
    // потрібен ВІД ПОЧАТКУ ЗАПИСУ, бо після транскрибування коментар переїде
    // на транскрипт і має вказувати в те саме місце доріжки.
    function liveCommentHTML() {
        return `
            <div class="rc-livecm" id="rcLiveCm">
                <div class="rc-livecm__row">
                    <i class="rc-livecm__ic fa-regular fa-comment-dots" aria-hidden="true"></i>
                    <input class="rc-livecm__in" id="rcLiveCmIn" type="text" autocomplete="off"
                           placeholder="${R.t('capture.rec.live.placeholder')}">
                    <select class="rc-select rc-select--sm rc-livecm__kind" id="rcLiveCmKind">
                        <option value="note">${R.t('capture.rec.live.note')}</option>
                        <option value="correction">${R.t('capture.rec.live.correction')}</option>
                        <option value="decision">${R.t('capture.rec.live.decision')}</option>
                        <option value="question">${R.t('capture.rec.live.question')}</option>
                    </select>
                    <span class="rc-livecm__hint rc-mono">Ctrl+Shift+K</span>
                </div>
                <div class="rc-livecm__log" id="rcLiveCmLog"></div>
            </div>`;
    }

    function wireLiveComment(ctx) {
        const box = $id('rcLiveCm');
        const inp = $id('rcLiveCmIn');
        if (!box || !inp) return;
        inp.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            saveLiveComment(ctx);
        });
        if (!_liveCmHotkey) {
            _liveCmHotkey = (e) => {
                if (!(e.ctrlKey && e.shiftKey && (e.key === 'K' || e.key === 'k'))) return;
                const el = $id('rcLiveCmIn');
                if (!el) return;
                e.preventDefault();
                el.focus();
            };
            document.addEventListener('keydown', _liveCmHotkey);
        }
        refreshLiveComments();
    }

    async function saveLiveComment(ctx) {
        const inp = $id('rcLiveCmIn');
        const kindSel = $id('rcLiveCmKind');
        if (!inp || !st.sessionId) return;
        const body = (inp.value || '').trim();
        if (!body) return;
        // Поле звільняємо ОДРАЗУ, до відповіді сервера: оператор друкує далі,
        // а не чекає на HTTP. Помилку показуємо тостом і повертаємо текст.
        inp.value = '';
        const at = st.elapsedSec;
        try {
            await R.api.commentCreate({
                target_type: 'recording_session', target_id: st.sessionId,
                body, kind: (kindSel && kindSel.value) || 'note',
                anchor_time: at, source: 'live',
            });
            refreshLiveComments();
        } catch (err) {
            inp.value = body;
            UI.toast((err && err.message) || R.t('capture.rec.live.notSaved'), 'error');
        }
    }

    async function refreshLiveComments() {
        const log = $id('rcLiveCmLog');
        if (!log || !st.sessionId) return;
        let rows = [];
        try { rows = (await R.api.comments('recording_session', st.sessionId)).comments || []; }
        catch (_) { return; }
        if (!rows.length) { log.innerHTML = ''; return; }
        // Показуємо останні — під час дзвінка потрібен слід, а не архів.
        log.innerHTML = rows.slice(-4).map(c => `
            <div class="rc-livecm__item">
                <span class="rc-livecm__at rc-mono">${fmt(c.anchor_time || 0)}</span>
                <span class="rc-livecm__txt">${U.esc(c.body)}</span>
            </div>`).join('');
    }

    // ---- status pill: REC / PAUSED / RECONNECTING / ERROR (T4.8) -----------
    // sseState (live-view transport health) takes priority over the recording
    // pause state — a dropped SSE connection matters more than whether the
    // (server-side, unaffected) recording is paused, since the operator can no
    // longer tell paused-and-fine from paused-and-broken without it. No new CSS
    // classes: T4.8 is scoped to this file only, so RECONNECTING/ERROR looks
    // are applied as inline styles (reusing --rc-warn/--rc-err custom
    // properties already defined in recall.css) instead of adding rules there.
    function pillState() {
        if (st.sseState === 'error') return { kind: 'error', txt: 'ERROR' };
        if (st.sseState === 'reconnecting') return { kind: 'reconnecting', txt: 'RECONNECTING' };
        return st.status === 'paused' ? { kind: 'paused', txt: 'PAUSED' } : { kind: 'rec', txt: 'REC' };
    }
    function refreshPill() {
        const pill = $id('rcRecPill');
        const txt = $id('rcRecStatusTxt');
        if (!pill || !txt) return;
        const dot = pill.querySelector('.rc-rec-dot');
        const s = pillState();
        pill.classList.toggle('is-paused', s.kind === 'paused');
        txt.textContent = s.txt;
        if (s.kind === 'reconnecting') {
            pill.style.background = 'rgba(176, 122, 22, 0.14)'; pill.style.color = 'var(--rc-warn)';
            if (dot) { dot.style.background = 'var(--rc-warn)'; dot.style.animationDuration = '0.9s'; }
        } else if (s.kind === 'error') {
            pill.style.background = 'var(--rc-err-tint)'; pill.style.color = 'var(--rc-err)';
            if (dot) { dot.style.background = 'var(--rc-err)'; dot.style.animation = 'none'; }
        } else {
            pill.style.background = ''; pill.style.color = '';
            if (dot) { dot.style.background = ''; dot.style.animation = ''; dot.style.animationDuration = ''; }
        }
    }

    // ---- SSE live-view banner (T4.8): server-side recording is NOT affected —
    // only the live monitoring (VU meters, live segments, co-pilot feed) goes
    // stale. Permanent, not a toast, because a frozen-but-silent VU meter reads
    // as "still fine" — the operator needs to know monitoring itself died.
    // Reuses .rc-rec-vid-banner (the existing video-failure banner style) so no
    // new CSS is needed and both banners read as the same visual language. ----
    function showSseBanner(kind, attempt) {
        const b = $id('rcRecSseBanner');
        if (!b) return;
        b.hidden = false;
        b.innerHTML = '';
        const icon = document.createElement('i');
        icon.className = 'fa-solid ' + (kind === 'failed' ? 'fa-triangle-exclamation' : 'fa-rotate fa-spin');
        b.appendChild(icon);
        b.appendChild(document.createTextNode(kind === 'failed'
            ? R.t('capture.rec.sse.failed')
            : R.t('capture.rec.sse.retrying', { attempt: attempt ? R.t('capture.rec.sse.attempt', { n: attempt }) : '' })));
    }
    function hideSseBanner() {
        const b = $id('rcRecSseBanner');
        if (b) { b.hidden = true; b.innerHTML = ''; }
    }

    // ---- audio-stream failure banner (T4.8) — same permanent .rc-rec-vid-banner
    // treatment as the video-track failure banner (see updateVideoTrack below),
    // scaled UP in severity by wording: audio is the whole point of the
    // product, so a dead mic/system stream is worse than a dead screen capture.
    // Previously this only got an auto-dismissing toast + VU-row highlight
    // (markStreamStatus) — that stays for the in-context cue, this adds the
    // persistent one. -----------------------------------------------------
    const AUDIO_STREAM_LBL = { mic: 'capture.rec.mic', system: 'capture.rec.sys' };
    function refreshAudioBanner() {
        const b = $id('rcRecAudioBanner');
        if (!b) return;
        const failed = Object.keys(st.streamErr || {}).filter(k => st.streamErr[k]);
        if (!failed.length) { b.hidden = true; b.innerHTML = ''; return; }
        const label = failed.map(k => (AUDIO_STREAM_LBL[k] ? R.t(AUDIO_STREAM_LBL[k]) : k)).join(', ');
        const detail = failed.map(k => st.streamErr[k]).filter(Boolean).join('; ');
        b.hidden = false;
        b.innerHTML = '';
        const icon = document.createElement('i');
        icon.className = 'fa-solid fa-microphone-slash';
        b.appendChild(icon);
        b.appendChild(document.createTextNode(
            R.t('capture.rec.audioFailed', { label, detail: detail ? ' (' + detail + ')' : '' })
        ));
    }

    const CP_MODE_LBL = { light: 'capture.rec.cp.lbl.mode.light', medium: 'capture.rec.cp.lbl.mode.medium', hard: 'capture.rec.cp.lbl.mode.hard' };
    const CP_IMP_LBL = { low: 'capture.rec.cp.lbl.imp.low', medium: 'capture.rec.cp.lbl.imp.medium', high: 'capture.rec.cp.lbl.imp.high' };
    const CP_MODE_NEXT = { light: 'medium', medium: 'hard', hard: 'light' };
    const CP_IMP_NEXT = { low: 'medium', medium: 'high', high: 'low' };
    function copilotWidget() {
        const c = st.copilotConfig || {};
        const mode = c.mode || 'medium';
        const imp = c.importance || 'medium';
        return `
            <aside class="rc-cp" id="rcCp">
                <div class="rc-cp__head">
                    <span class="rc-cp__title"><i class="fa-solid fa-wand-magic-sparkles"></i> ${R.t('capture.rec.cp.widgetTitle')}</span>
                    <span class="rc-cp__status" id="rcCpStatus" data-state="analyzing"><span class="rc-cp__dot"></span><span id="rcCpStatusTxt">${R.t('capture.rec.cp.analyzing')}</span></span>
                    <span class="rc-cp__cost rc-mono" id="rcCpCost" hidden></span>
                    <span class="rc-cp__count rc-mono" id="rcCpCount" hidden>0</span>
                    <span class="rc-cp__count rc-mono" id="rcCpBudgetLbl" title="${R.t('capture.rec.cp.budgetTitle')}" hidden></span>
                    <button class="rc-cp__collapse" id="rcCpCollapse" title="${R.t('capture.rec.cp.collapse')}"><i class="fa-solid fa-chevron-right"></i></button>
                </div>
                <div class="rc-cp__ctrls">
                    <button class="rc-cp__chip rc-mono" id="rcCpMode" data-mode="${mode}" title="${R.t('capture.rec.cp.modeChipTitle')}"><i class="fa-solid fa-gauge-high"></i> ${R.t(CP_MODE_LBL[mode])}</button>
                    <button class="rc-cp__chip rc-mono" id="rcCpImp" data-imp="${imp}" title="${R.t('capture.rec.cp.impChipTitle')}"><i class="fa-solid fa-flag"></i> ${R.t(CP_IMP_LBL[imp])}</button>
                </div>
                <div class="rc-cp__banner" id="rcCpBanner" hidden></div>
                <div class="rc-cp-strip" id="rcCpStrip" hidden></div>
                <div class="rc-cp__feed" id="rcCpInsights"></div>
                <div class="rc-cp__empty" id="rcCpEmpty"><i class="fa-solid fa-ear-listen"></i> ${R.t('capture.rec.cp.listening')}</div>
            </aside>`;
    }

    function wireWidget() {
        const collapse = $id('rcCpCollapse');
        if (collapse) collapse.addEventListener('click', toggleWidget);
        const feed = $id('rcCpInsights');
        if (feed) feed.addEventListener('click', onInsightAction);
        const modeBtn = $id('rcCpMode');
        if (modeBtn) modeBtn.addEventListener('click', () => changeSetting('mode', CP_MODE_NEXT[modeBtn.dataset.mode] || 'medium'));
        const impBtn = $id('rcCpImp');
        if (impBtn) impBtn.addEventListener('click', () => changeSetting('importance', CP_IMP_NEXT[impBtn.dataset.imp] || 'medium'));
        renderTopicStrip();
        renderInsights();
        setCpStatus(st.cpStatus || 'analyzing');
        if (st.cpUsage) applyCopilotUsage(st.cpUsage);
    }

    // Зміна режиму/важливості на льоту під час дзвінка (Крок 6).
    async function changeSetting(field, value) {
        const S = st;
        if (!S || !S.copilotSessionId) return;
        try {
            const r = await R.api.post('/api/copilot/' + S.copilotSessionId + '/settings', { [field]: value });
            if (r && r.config) {
                S.copilotConfig = r.config;
                withS(S, refreshWidgetChips);
                const lbl = R.t(field === 'mode' ? CP_MODE_LBL[value] : CP_IMP_LBL[value]);
                UI.toast(R.t('capture.rec.cp.toastChanged', { label: lbl }), 'info');
            }
        } catch (err) { UI.toast(R.t('capture.rec.cp.changeFailed', { error: (err && err.message) || '' }), 'error'); }
    }
    function refreshWidgetChips() {
        const c = st.copilotConfig || {};
        const m = $id('rcCpMode');
        if (m && c.mode) { m.dataset.mode = c.mode; m.innerHTML = `<i class="fa-solid fa-gauge-high"></i> ${R.t(CP_MODE_LBL[c.mode])}`; }
        const ip = $id('rcCpImp');
        if (ip && c.importance) { ip.dataset.imp = c.importance; ip.innerHTML = `<i class="fa-solid fa-flag"></i> ${R.t(CP_IMP_LBL[c.importance])}`; }
    }

    function toggleWidget() {
        const cp = $id('rcCp');
        if (!cp) return;
        cp.classList.toggle('is-collapsed');
        const cb = $id('rcCpCollapse'); const ic = cb && cb.querySelector('i');
        if (ic) ic.className = 'fa-solid ' + (cp.classList.contains('is-collapsed') ? 'fa-chevron-left' : 'fa-chevron-right');
        if (!cp.classList.contains('is-collapsed')) { st.cpUnseen = 0; updateCount(); }
    }

    function applyPauseUi() {
        const lbl = $id('rcRecPauseLbl');
        const pauseBtn = $id('rcRecPause');
        const paused = st.status === 'paused';
        refreshPill();
        if (lbl) lbl.textContent = paused ? R.t('capture.rec.resume') : R.t('capture.rec.pause');
        if (pauseBtn) { const i = pauseBtn.querySelector('i'); if (i) i.className = 'fa-solid ' + (paused ? 'fa-play' : 'fa-pause'); }
        // ко-пілот: миттєво відобразити паузу/відновлення (SSE copilot_status прийде з лагом тіку)
        if (st.copilotSessionId) setCpStatus(paused ? 'paused' : 'analyzing');
    }

    async function pauseResume(ctx) {
        const S = st;
        if (!S || !S.sessionId) return;
        const action = S.status === 'paused' ? 'resume' : 'pause';
        try {
            const r = await R.api.post('/api/recording/' + S.sessionId + '/' + action);
            S.status = r.status || (action === 'pause' ? 'paused' : 'recording');
            withS(S, applyPauseUi);
            paintTab(S);
        } catch (err) { errToast(err, R.t('capture.rec.error')); }
    }

    // ---- SSE (fetch-reader + AbortController, auto-reconnect — T4.8) -------
    // /api/recording/<sid>/stream is a continuous live-view feed (VU levels,
    // live segments, co-pilot events, video stats) with no terminal frame by
    // design — it streams until the client disconnects. Using sseReconnecting
    // (requireTerminalFrame:false) instead of a bare sseStream means a dropped
    // connection (Wi-Fi blip, laptop sleep, proxy hiccup) auto-retries with
    // backoff instead of silently going dark: the server-side recording is
    // unaffected either way, but the operator needs to SEE that live
    // monitoring died instead of staring at a frozen-but-seemingly-fine VU
    // meter. onReconnecting/onReconnected/onPermanentFail drive the pill +
    // banner (see pillState/showSseBanner/hideSseBanner above).
    // Кожен запис тримає СВІЙ читач: фоновий таб оновлює лише свій S, DOM — тільки
    // вибраний (withS + $id), тож перемикання табів не рве жоден потік.
    function connectSSE(ctx, S) {
        haltSse(S);
        const ac = new AbortController();
        S.abort = ac;
        const sid = S.sessionId;
        S.sseState = 'ok';
        withS(S, hideSseBanner);
        R.api.sseReconnecting('/api/recording/' + sid + '/stream', (event, data) => {
            if (!ctx.isCurrent() || ac.signal.aborted || sessions.get(sid) !== S) return;
            withS(S, () => onSseEvent(S, event, data));
        }, {
            signal: ac.signal,
            requireTerminalFrame: false,
            onReconnecting: (attempt, delayMs) => {
                if (!ctx.isCurrent() || sessions.get(sid) !== S) return;
                withS(S, () => { S.sseState = 'reconnecting'; refreshPill(); showSseBanner('reconnecting', attempt); });
            },
            onReconnected: () => {
                if (!ctx.isCurrent() || sessions.get(sid) !== S) return;
                withS(S, () => { S.sseState = 'ok'; refreshPill(); hideSseBanner(); });
            },
            onPermanentFail: () => {
                if (!ctx.isCurrent() || sessions.get(sid) !== S) return;
                withS(S, () => { S.sseState = 'error'; refreshPill(); showSseBanner('failed'); });
            },
        });
    }
    function haltSse(S) { if (S && S.abort) { try { S.abort.abort(); } catch (_) {} S.abort = null; } }

    function onSseEvent(S, event, data) {
            if (event === 'level') {
                vu('rcVuMic', data.streams && data.streams.mic);
                vu('rcVuSys', data.streams && data.streams.system);
                if (typeof data.elapsed === 'number') { st.elapsedSec = data.elapsed; const t = $id('rcRecTimer'); if (t) t.textContent = fmt(st.elapsedSec); paintTab(S); }
            } else if (event === 'source_changed') {
                if (data && data.source && (!data.stream || data.stream === 'system')) {
                    st.systemSource = data.source;
                    paintSource();
                }
            } else if (event === 'chunk_saved') {
                if (data.streams) st.audioBytes = Object.assign(st.audioBytes || {}, data.streams);
                if (typeof data.disk_free === 'number') st.diskFree = data.disk_free;
                if (typeof data.disk_total === 'number') st.diskTotal = data.disk_total;
                updateRecMeta();
            } else if (event === 'status') {
                st.status = data.status;
                paintTab(S);
                if (data.status === 'paused' || data.status === 'recording') applyPauseUi();
                else if (data.status === 'finalized' || data.status === 'crashed') { /* external finalize */ }
            } else if (event === 'live_segment') {
                appendSegments(data.segments || []);
            } else if (event === 'copilot_topic') {
                applyCopilotTopic(data);
            } else if (event === 'copilot_insight') {
                applyCopilotInsight(data);
                updateCardBudget((st.insights || []).length);
            } else if (event === 'copilot_insight_update') {
                applyCopilotInsightUpdate(data);
            } else if (event === 'copilot_usage') {
                applyCopilotUsage(data);
            } else if (event === 'copilot_status') {
                applyCopilotStatus(data);
            } else if (event === 'video_status') {
                updateVideoTrack(data);
            } else if (event === 'video_stats') {
                updateVideoStats(data);
            } else if (event === 'error') {
                // Two distinct shapes land here (FW0): a synthetic transport-drop
                // frame from api.js's readSseBody — {error, code:'sse_broken'} —
                // fired on every abrupt disconnect BEFORE sseReconnecting retries;
                // and a real server-side recording error — {stream, message}. The
                // former is not user-actionable (the reconnect banner already
                // covers it) and toasting it would spam one popup per drop, so it
                // gets its own branch instead of falling into the generic toast.
                if (data && data.code === 'sse_broken') return;
                if (data && data.stream) {
                    st.streamErr[data.stream] = data.message || R.t('capture.rec.stream.error');
                    markStreamStatus();
                    refreshAudioBanner();
                }
                UI.toast((sessions.size > 1 ? tabName(S) + ': ' : '') + R.t('capture.rec.stream.errorToast', { message: (data && data.message) || R.t('capture.rec.stream.unknown') }), 'error');
            }
    }

    // ---- video track helpers (S7) ------------------------------------------
    function updateVideoTrack(data) {
        if (!data || data.track_id == null) return;
        const key = String(data.track_id);
        const existing = st.videoTracks[key] || {};
        st.videoTracks[key] = Object.assign({}, existing, {
            track_id: data.track_id,
            monitor_index: data.monitor_index != null ? data.monitor_index : existing.monitor_index,
            monitor_label: data.monitor_label || existing.monitor_label || ('Monitor ' + (data.monitor_index || '')),
            status: data.status || existing.status,
            error: data.error || null,
            mode: data.mode || existing.mode || 'full',
            region: data.region != null ? data.region : (existing.region || null),
        });
        renderVideoChips();
        // on failure: reveal the banner (audio recording continues unaffected)
        if (data.status === 'failed') {
            const banner = $id('rcRecVidBanner');
            if (banner) {
                banner.hidden = false;
                // use textContent for the safe plain-text part, build with DOM for icon
                banner.innerHTML = '';
                const icon = document.createElement('i');
                icon.className = 'fa-solid fa-triangle-exclamation';
                banner.appendChild(icon);
                banner.appendChild(document.createTextNode(
                    R.t('capture.rec.video.stopped', { label: data.monitor_label || ('Monitor ' + data.monitor_index) })
                ));
            }
        }
    }
    function updateVideoStats(data) {
        if (!data || data.track_id == null) return;
        const key = String(data.track_id);
        if (!st.videoTracks[key]) return;
        // бекенд шле {track_id, stats:{fps,dropped,bytes}} — розпаковуємо вкладене
        // (фолбек на плоске на випадок іншої форми події)
        const s = data.stats || data;
        st.videoTracks[key].fps = s.fps != null ? s.fps : st.videoTracks[key].fps;
        st.videoTracks[key].dropped = s.dropped != null ? s.dropped : st.videoTracks[key].dropped;
        st.videoTracks[key].bytes = s.bytes != null ? s.bytes : st.videoTracks[key].bytes;
        // update only the stats label within the existing chip (no full re-render to avoid flicker)
        const vrow = $id('rcRecVid'); const chip = vrow && vrow.querySelector('.rc-rec-vid-chip[data-track-id="' + key + '"]');
        if (chip) {
            const stats = chip.querySelector('.rc-rec-vid-chip__stats');
            if (stats) {
                const fps = s.fps != null ? s.fps.toFixed(0) + ' fps' : '';
                const mb = s.bytes != null ? (s.bytes / 1048576).toFixed(1) + ' MB' : '';
                stats.textContent = [fps, mb, regionLabel(st.videoTracks[key])].filter(Boolean).join(' · ');
            }
        }
        updateRecMeta();
    }
    // п.5: підпис розміру області (◳ W×H) для трека, що пише виділений регіон екрана
    function regionLabel(t) {
        return (t && t.mode === 'region' && t.region && t.region.w)
            ? '◳ ' + t.region.w + '×' + t.region.h : '';
    }
    // Канонічний пересбір відеотреків зі снапшоту state (get_state.video_tracks).
    // Усуває рассинхрон ключів: start-відповідь дає лише monitor_index, а live-події
    // (video_status/stats) і manifest — справжній track_id ('mon0'). Збираємо за
    // канонічним track_id, зберігаючи вже накопичені live-байти/fps якщо снапшот їх не має.
    function applyStateVideoTracks(arr) {
        if (!Array.isArray(arr)) return;
        const next = {};
        for (const t of arr) {
            const key = String(t.track_id != null ? t.track_id : t.monitor_index);
            const ls = t.last_stats || {};
            const prev = st.videoTracks[key] || {};
            next[key] = {
                track_id: t.track_id != null ? t.track_id : t.monitor_index,
                monitor_index: t.monitor_index,
                monitor_label: t.monitor_label || prev.monitor_label || ('Monitor ' + t.monitor_index),
                status: t.status || prev.status || 'recording',
                mode: t.mode || prev.mode || 'full',
                region: t.region || prev.region || null,
                fps: ls.fps != null ? ls.fps : (prev.fps != null ? prev.fps : null),
                dropped: ls.dropped != null ? ls.dropped : (prev.dropped != null ? prev.dropped : null),
                bytes: ls.bytes != null ? ls.bytes : (prev.bytes != null ? prev.bytes : null),
            };
        }
        st.videoTracks = next;
        renderVideoChips();
    }
    // Одноразова звірка чипів зі state після старту (track_id стає канонічним).
    function reconcileVideoTracks(ctx, S) {
        const sid = S.sessionId;
        setTimeout(async () => {
            if (!ctx.isCurrent() || sessions.get(sid) !== S) return;
            try {
                const s = await R.api.get('/api/recording/' + sid + '/state');
                if (s && s.success && s.state && Array.isArray(s.state.video_tracks) && s.state.video_tracks.length) {
                    withS(S, () => applyStateVideoTracks(s.state.video_tracks));
                }
            } catch (_) { /* лишаємо провіжн-чипи зі start-відповіді */ }
        }, 1800);
    }
    function renderVideoChips() {
        const row = $id('rcRecVid');
        if (!row) return;
        const tracks = Object.values(st.videoTracks);
        if (!tracks.length) { row.innerHTML = ''; return; }
        row.innerHTML = tracks.map(t => {
            const isRec = t.status === 'recording';
            const isFail = t.status === 'failed';
            const dot = isRec ? '<span class="rc-rec-vid-chip__dot"></span>' : '';
            const fps = t.fps != null ? t.fps.toFixed(0) + ' fps' : '';
            const mb = t.bytes != null ? (t.bytes / 1048576).toFixed(1) + ' MB' : '';
            const statsStr = [fps, mb, regionLabel(t)].filter(Boolean).join(' · ');
            const label = U.esc(t.monitor_label || ('Monitor ' + t.monitor_index));
            const cls = 'rc-rec-vid-chip rc-mono' + (isRec ? ' is-recording' : '') + (isFail ? ' is-failed' : '');
            return `<span class="${cls}" data-track-id="${U.esc(String(t.track_id))}">${dot}REC ${label}<span class="rc-rec-vid-chip__stats">${U.esc(statsStr)}</span></span>`;
        }).join('');
    }

    function vu(rowId, d) {
        if (!d) return;
        const row = $id(rowId);
        if (!row) return;
        const fill = row.querySelector('.rc-vu__fill');
        const peak = row.querySelector('.rc-vu__peak');
        const val = row.querySelector('.rc-vu__val');
        if (fill) fill.style.width = Math.min(100, (d.rms || 0) * 100) + '%';
        if (peak) peak.style.left = Math.min(100, (d.peak || 0) * 100) + '%';
        if (val) { const db = (d.peak > 0.0001) ? (20 * Math.log10(d.peak)).toFixed(0) + ' dB' : '-∞'; val.textContent = db; }
    }

    // Живі сегменти зберігаються в S (хвіст), щоб перемикання табів їх не губило.
    function appendSegments(segs) {
        if (!segs.length) return;
        st.segs.push(...segs);
        if (st.segs.length > 300) st.segs.splice(0, st.segs.length - 300);
        paintSegs(segs);
    }
    function paintSegs(segs) {
        if (!segs.length) return;
        const live = $id('rcRecLive');
        if (!live) return;
        live.hidden = false;
        for (const s of segs) {
            const text = (s.text || '').trim();
            if (!text) continue;
            const who = s.speaker === 'self' ? R.t('capture.rec.seg.you') : (s.speaker === 'other' ? R.t('capture.rec.seg.other') : '?');
            const div = U.el('div', { class: 'rc-rec-seg' },
                `<span class="rc-rec-seg__who rc-mono ${s.speaker === 'self' ? 'is-self' : 'is-other'}">${who}</span><span>${U.esc(text)}</span>`);
            live.appendChild(div);
        }
        const nearBottom = live.scrollHeight - live.scrollTop - live.clientHeight < 80;
        if (nearBottom) live.scrollTop = live.scrollHeight;
    }

    // ---- Co-pilot: топік-смужка (Крок 2) ----------------------------------
    function renderTopicStrip() {
        const el = $id('rcCpStrip');
        if (!el) return;
        if (!st.topics.length) { el.hidden = true; return; }
        el.hidden = false;
        el.innerHTML = '<span class="rc-cp-strip__lbl rc-mono">' + R.t('capture.rec.topics') + '</span>'
            + st.topics.map(t => {
                const on = t.index === st.curTopic;
                return `<span class="rc-cp-topic rc-mono${on ? ' is-active' : ''}">${U.esc(t.label || R.t('capture.rec.topic', { n: t.index + 1 }))}</span>`;
            }).join('');
    }
    function applyCopilotTopic(data) {
        if (!data || data.topic_index == null) return;
        const idx = data.topic_index;
        const found = st.topics.find(t => t.index === idx);
        if (!found) st.topics.push({ index: idx, label: data.label || R.t('capture.rec.topic', { n: idx + 1 }) });
        else if (data.label) found.label = data.label;
        st.curTopic = idx;
        renderTopicStrip();
        if (data.action === 'return') {
            UI.toast(R.t('capture.rec.topicReturn', { label: data.label || R.t('capture.rec.topic', { n: idx + 1 }) }), 'info');
        }
    }

    // ---- Co-pilot: індикатор стану (Крок 4) -------------------------------
    const CP_STATE = {
        analyzing: { txt: 'capture.rec.cp.state.analyzing', cls: 'is-analyzing' },
        paused:    { txt: 'capture.rec.cp.state.paused', cls: 'is-paused' },
        idle:      { txt: 'capture.rec.cp.state.idle', cls: 'is-idle' },
        degraded:  { txt: 'capture.rec.cp.state.degraded', cls: 'is-degraded' },
        quiet:     { txt: 'capture.rec.cp.state.quiet', cls: 'is-idle' },
    };
    function applyCopilotStatus(data) {
        if (!data || !data.state) return;
        setCpStatus(data.state, data.reason);
        // Трек 3: бюджет карток вичерпано. Тиша має бути ПОЯСНЕНОЮ — інакше
        // читається як «копілот зламався», а не «він навмисно мовчить».
        if (data.state === 'quiet' && data.reason === 'card_budget') {
            const n = data.suppressed || 0;
            showCpBanner(R.t('capture.rec.cp.budgetSpent') +
                (n ? R.t('capture.rec.cp.budgetSuppressed', { count: n }) : '.'));
        }
        if (data.cards_shown != null) updateCardBudget(data.cards_shown);
    }
    // «2/5» у шапці віджета: скільки уваги оператора вже витрачено.
    function updateCardBudget(shown) {
        const el = $id('rcCpBudgetLbl');
        if (!el) return;
        const limit = (st.copilotConfig || {}).max_cards;
        el.hidden = false;
        el.textContent = limit ? `${shown}/${limit}` : String(shown);
    }
    function showCpBanner(text) {
        const el = $id('rcCpBanner');
        if (!el) return;
        el.hidden = false;
        el.textContent = text;
    }
    // Лічильник вартості верифікації Claude (Крок 5): «$0.12 / $1.00».
    function applyCopilotUsage(data) {
        if (!data) return;
        st.cpUsage = data;
        const el = $id('rcCpCost');
        if (!el) return;
        const cost = (data.cost_estimate != null) ? '$' + Number(data.cost_estimate).toFixed(2) : '';
        const lim = (data.budget != null && data.budget > 0) ? ' / $' + Number(data.budget).toFixed(2) : '';
        el.hidden = !cost;
        el.textContent = cost + lim;
        const over = data.state === 'budget_exhausted';
        const warn = data.state === 'warn';
        el.classList.toggle('is-over', over);
        el.classList.toggle('is-warn', warn);
        const ic = over ? '<i class="fa-solid fa-ban"></i> ' : (warn ? '<i class="fa-solid fa-triangle-exclamation"></i> ' : '');
        el.innerHTML = ic + U.esc(cost + lim);
        el.title = over ? R.t('capture.rec.cp.budgetOver')
            : warn ? R.t('capture.rec.cp.budgetWarn')
            : R.t('capture.rec.cp.verification', { in: data.tokens_in || 0, out: data.tokens_out || 0 });
    }
    function setCpStatus(state, reason) {
        st.cpStatus = state;
        const el = $id('rcCpStatus');
        if (!el) return;
        const s = CP_STATE[state] || CP_STATE.analyzing;
        el.dataset.state = state;
        if (reason) el.title = reason;
        const t = $id('rcCpStatusTxt');
        if (t) t.textContent = R.t(s.txt);
        const cp = $id('rcCp');
        if (cp) cp.classList.toggle('is-paused', state === 'paused');
        // Деградація (Ollama впав) → банер; запис/STT працюють далі (Крок 9).
        const banner = $id('rcCpBanner');
        if (banner) {
            if (state === 'degraded') {
                banner.hidden = false;
                banner.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> ${R.t('capture.rec.cp.degraded')}`;
            } else { banner.hidden = true; banner.innerHTML = ''; }
        }
    }

    // ---- Co-pilot: картки-інсайти з діями (Крок 4) ------------------------
    const CP_KIND = {
        contradiction: { icon: 'fa-triangle-exclamation', lbl: 'capture.rec.cp.kind.contradiction' },
        question:      { icon: 'fa-circle-question',       lbl: 'capture.rec.cp.kind.question' },
        clarification: { icon: 'fa-lightbulb',             lbl: 'capture.rec.cp.kind.clarification' },
        fact:          { icon: 'fa-thumbtack',             lbl: 'capture.rec.cp.kind.fact' },
        // Історія 27: системне попередження «скоуп неоднозначний» — своя картка
        // (не інсайт диспетчера), тому без кнопок закріпити/оцінити/копнути глибше.
        scope_notice:  { icon: 'fa-circle-info',            lbl: 'capture.rec.cp.kind.scope_notice' },
    };
    function applyCopilotInsight(data) {
        if (!data || !data.text) return;
        // Антишум: дублікат (той самий тип+текст) не плодить картку — бампимо лічильник.
        const key = data.kind + '::' + data.text.trim().toLowerCase();
        const dup = st.insights.find(i => i.key === key && !i.dismissed);
        if (dup) {
            dup.count = (dup.count || 1) + 1;
            dup.ts_offset_sec = data.ts_offset_sec;
            renderInsights();
            return;
        }
        st.insights.push({
            id: data.id, key: key, kind: data.kind, text: data.text,
            candidates: data.candidates || [],
            evidence: data.evidence || [], confidence: data.confidence,
            source: data.source || 'local', verdict: null,
            ts_offset_sec: data.ts_offset_sec, count: 1,
            pinned: false, feedback: null, escalated: false, dismissed: false,
        });
        if (st.insights.length > 60) {
            // прибираємо найстарішу незакріплену
            const i = st.insights.findIndex(x => !x.pinned);
            if (i >= 0) st.insights.splice(i, 1);
        }
        // Системне попередження не перемальовує пілюлю стану/банер degraded —
        // це не сигнал «диспетчер щойно проаналізував», а окремий канал.
        if (data.kind !== 'scope_notice') setCpStatus('analyzing');
        const cp = $id('rcCp');
        if (cp && cp.classList.contains('is-collapsed')) { st.cpUnseen = (st.cpUnseen || 0) + 1; updateCount(); }
        renderInsights();
    }
    // Крок 5 надсилатиме copilot_insight_update (апгрейд «локально»→«перевірено»);
    // хендлер готовий тут, щоб віджет одразу вмів оновлювати бейдж картки.
    function applyCopilotInsightUpdate(data) {
        if (!data || data.id == null) return;
        const ins = st.insights.find(i => i.id === data.id);
        if (!ins) return;
        ins.source = data.source || 'api';
        if (data.verdict) ins.verdict = data.verdict;
        if (data.confidence != null) ins.confidence = data.confidence;
        if (data.text) ins.text = data.text;
        renderInsights();
    }

    function sortedInsights() {
        // закріплені зверху, далі найновіші; відхилені не показуємо
        const live = st.insights.filter(i => !i.dismissed);
        const pinned = live.filter(i => i.pinned).reverse();
        const rest = live.filter(i => !i.pinned).reverse();
        return pinned.concat(rest);
    }
    function renderInsights() {
        const el = $id('rcCpInsights');
        if (!el) return;
        const items = sortedInsights();
        const empty = $id('rcCpEmpty');
        if (empty) empty.hidden = items.length > 0;
        el.innerHTML = items.map(insightCard).join('');
        updateCount();
    }
    function insightCard(ins) {
        const k = CP_KIND[ins.kind] || CP_KIND.fact;
        const pct = ins.confidence != null ? Math.round(ins.confidence * 100) + '%' : '';
        const verified = ins.source === 'api';
        const refuted = ins.verdict === 'refuted';
        const badge = verified
            ? `<span class="rc-cp-card__badge is-verified" title="${R.t('capture.rec.cp.verifiedTitle')}"><i class="fa-solid fa-circle-check"></i> ${refuted ? R.t('capture.rec.cp.refuted') : R.t('capture.rec.cp.verified')}${pct ? ' · ' + pct : ''}</span>`
            : `<span class="rc-cp-card__badge is-local" title="${R.t('capture.rec.cp.localTitle')}"><i class="fa-solid fa-microchip"></i> ${R.t('capture.rec.cp.localBadge')}${pct ? ' · ' + pct : ''}</span>`;
        const time = (ins.ts_offset_sec != null) ? fmt(Math.round(ins.ts_offset_sec)) : '';
        const cnt = (ins.count > 1) ? ` <span class="rc-cp-card__cnt rc-mono">×${ins.count}</span>` : '';
        const ev = (ins.evidence || []).map(e => {
            const name = e.source_name || ('#' + e.transcription_id);
            const slug = U.slug(e.transcription_id, e.source_name || R.t('capture.rec.cp.source'));
            return `<a class="rc-cp-ev rc-mono" href="/transcript/${slug}" title="${U.esc((e.meeting_date || '') + ' · ' + name)}">[${U.esc(name)}]</a>`;
        }).join(' ');
        const act = (a, ic, title, on) =>
            `<button class="rc-cp-act${on ? ' is-on' : ''}" data-act="${a}" title="${title}"><i class="fa-solid ${ic}"></i></button>`;
        // Системне попередження про скоуп — не спостереження диспетчера: закріпити/
        // оцінити/копнути глибше для нього безглузді, лишається тільки «прибрати».
        const isNotice = ins.kind === 'scope_notice';
        const actions = isNotice
            ? `<span class="rc-yt__spacer"></span>${act('dismiss', 'fa-xmark', R.t('capture.rec.cp.act.dismiss'), false)}`
            : `${act('pin', 'fa-thumbtack', ins.pinned ? R.t('capture.rec.cp.act.unpin') : R.t('capture.rec.cp.act.pin'), ins.pinned)}
                ${act('thumbs_up', 'fa-thumbs-up', R.t('capture.rec.cp.act.up'), ins.feedback === 'up')}
                ${act('thumbs_down', 'fa-thumbs-down', R.t('capture.rec.cp.act.down'), ins.feedback === 'down')}
                ${act('escalate', 'fa-magnifying-glass-plus', R.t('capture.rec.cp.act.escalate'), ins.escalated)}
                <span class="rc-yt__spacer"></span>
                ${act('dismiss', 'fa-xmark', R.t('capture.rec.cp.act.reject'), false)}`;
        const paths = (isNotice && (ins.candidates || []).length)
            ? `<ul class="rc-cp-card__paths">${ins.candidates.map(p => `<li>${U.esc(p)}</li>`).join('')}</ul>`
            : '';
        return `<div class="rc-cp-card is-${ins.kind}${ins.pinned ? ' is-pinned' : ''}${refuted ? ' is-refuted' : ''}" data-id="${ins.id != null ? ins.id : ''}" data-key="${U.esc(ins.key)}">
            <div class="rc-cp-card__head">
                <i class="fa-solid ${k.icon} rc-cp-card__kico"></i>
                <span class="rc-cp-card__kind rc-mono">${R.t(k.lbl)}${cnt}</span>
                ${badge}
                ${time ? `<span class="rc-cp-card__time rc-mono">${time}</span>` : ''}
            </div>
            <div class="rc-cp-card__text">${U.esc(ins.text)}</div>
            ${paths}
            ${ev ? `<div class="rc-cp-card__ev">${ev}</div>` : ''}
            <div class="rc-cp-card__actions">
                ${actions}
            </div>
        </div>`;
    }
    function onInsightAction(e) {
        const btn = e.target.closest('.rc-cp-act');
        if (!btn) return;
        const card = btn.closest('.rc-cp-card');
        if (!card) return;
        const ins = st.insights.find(i => String(i.id) === card.dataset.id && card.dataset.id !== '')
            || st.insights.find(i => i.key === card.dataset.key);
        if (!ins) return;
        const a = btn.dataset.act;
        if (a === 'dismiss') { ins.dismissed = true; cpAction(ins, 'dismiss'); renderInsights(); return; }
        if (a === 'pin') { ins.pinned = !ins.pinned; cpAction(ins, ins.pinned ? 'pin' : 'unpin'); renderInsights(); return; }
        if (a === 'thumbs_up') { ins.feedback = ins.feedback === 'up' ? null : 'up'; if (ins.feedback) cpAction(ins, 'thumbs_up'); renderInsights(); return; }
        if (a === 'thumbs_down') { ins.feedback = ins.feedback === 'down' ? null : 'down'; if (ins.feedback) cpAction(ins, 'thumbs_down'); renderInsights(); return; }
        if (a === 'escalate') {
            ins.escalated = true; cpAction(ins, 'escalate');
            UI.toast(R.t('capture.rec.cp.escalated'), 'info');
            renderInsights();
        }
    }
    function cpAction(ins, action) {
        if (!st.copilotSessionId) return;
        R.api.post('/api/copilot/' + st.copilotSessionId + '/action', {
            action: action, event_id: ins.id, ts_offset_sec: ins.ts_offset_sec,
        }).catch(() => { /* дія — best-effort, не блокуємо UI */ });
    }
    function updateCount() {
        const cp = $id('rcCp');
        const badge = $id('rcCpCount');
        if (!cp || !badge) return;
        const collapsed = cp.classList.contains('is-collapsed');
        const n = collapsed ? (st.cpUnseen || 0) : sortedInsights().length;
        badge.hidden = !collapsed || n <= 0;
        badge.textContent = String(n);
    }

    function startTick(ctx, S) {
        if (S.tick) clearInterval(S.tick);
        S.tick = setInterval(() => {
            if (!ctx.isCurrent()) { clearInterval(S.tick); S.tick = null; return; }
            if (S.status === 'recording') withS(S, () => {
                S.elapsedSec += 1;
                const t = $id('rcRecTimer'); if (t) t.textContent = fmt(S.elapsedSec);
                updateRecMeta();
                paintTab(S);
            });
        }, 1000);
    }

    // ---- stop → name → transcribe ------------------------------------------
    async function stop(ctx) {
        const S = st;
        if (!S || !S.sessionId) return;
        const sid = S.sessionId;
        // close SSE + ticker synchronously before awaiting (recording has ended)
        haltS(S);
        try {
            await R.api.post('/api/recording/' + sid + '/stop', {});
            S.status = 'stopping';
        } catch (err) {
            errToast(err, R.t('capture.rec.stopFailed'));
            // запис живий далі — повертаємо йому читач і тік
            if (ctx.isCurrent() && sessions.get(sid) === S && errCode(err) !== 'NOT_FOUND') { connectSSE(ctx, S); startTick(ctx, S); }
            return;
        }
        S.stage = 'stopname';
        S.copilotSessionId = null; S.copilotActive = false;   // стоп запису завершує його ко-пілот (бекенд)
        // Ask 3: сусіди, яким звузили системний звук, — пропонуємо повернути весь
        // (за замовчуванням «так»: без відповіді застосується при збереженні/скасуванні).
        S.restore = liveSessions().filter(o => o !== S && o.systemSource && o.systemSource.mode && o.systemSource.mode !== 'all')
            .map(o => o.sessionId);
        _limitHit = false;

        // pull auto-name + segments count for the summary
        let autoName = '', segs = 1;
        try { const s = await R.api.get('/api/recording/' + sid + '/state'); if (s.success) { autoName = (s.state && s.state.auto_name) || ''; segs = (s.state && s.state.segments_count) || 1; } }
        catch (_) {}
        S.stopInfo = { autoName, segs };
        if (!ctx.isCurrent()) return;
        if (selSid === sid) select(ctx, sid);
        else renderTabs(ctx);
    }

    function restoreHTML() {
        const rows = (st.restore || []).map(sid => sessions.get(sid)).filter(o => o && o.stage === 'active');
        if (!rows.length) return '';
        return rows.map(o => `
            <div class="rc-recwarn rc-recwarn--ask" data-restore="${U.esc(o.sessionId)}">
                <span><i class="fa-solid fa-volume-high"></i> ${U.esc(R.t('capture.rec.restore.ask', { name: tabName(o) }))} <span class="rc-mono rc-recsrc__muted">${U.esc(R.t('capture.rec.restore.now', { mode: SRC_LBL[o.systemSource.mode] ? R.t(SRC_LBL[o.systemSource.mode]) : o.systemSource.mode }))}${(o.systemSource.apps || []).length ? ' — ' + U.esc(o.systemSource.apps.join(', ')) : ''}</span></span>
                <span class="rc-yt__spacer"></span>
                <button type="button" class="rc-btn rc-btn--primary rc-btn--sm" data-restore-yes>${R.t('capture.rec.yes')}</button>
                <button type="button" class="rc-btn rc-btn--sm" data-restore-no>${R.t('capture.rec.no')}</button>
            </div>`).join('');
    }
    function wireRestore(ctx, body) {
        const S = st;
        body.querySelectorAll('[data-restore]').forEach(row => {
            const sid = row.dataset.restore;
            const done = () => { S.restore = (S.restore || []).filter(x => x !== sid); row.remove(); };
            row.querySelector('[data-restore-yes]').addEventListener('click', async () => {
                const o = sessions.get(sid);
                if (o && o.stage === 'active') await setSystemSource(ctx, o, { mode: 'all', reason: 'auto_restore' });
                done();
            });
            row.querySelector('[data-restore-no]').addEventListener('click', done);
        });
    }
    // Відповідь за замовчуванням — «так»: невідповідені питання застосовуються,
    // коли зупинений запис зберігають або скасовують.
    function applyPendingRestore(ctx, S) {
        const pend = (S.restore || []).slice();
        S.restore = [];
        pend.forEach(sid => {
            const o = sessions.get(sid);
            if (o && o.stage === 'active' && o.systemSource && o.systemSource.mode !== 'all') {
                setSystemSource(ctx, o, { mode: 'all', reason: 'auto_restore' });
            }
        });
    }

    function renderStopName(ctx) {
        const autoName = (st.stopInfo && st.stopInfo.autoName) || '';
        const segs = (st.stopInfo && st.stopInfo.segs) || 1;
        const body = ctx.mount.querySelector('#rcRecBody');
        body.innerHTML = `
            <div class="rc-rec-stop">
                <div class="rc-rec-summary rc-mono"><i class="fa-solid fa-circle-check" style="color:var(--rc-ok)"></i> ${R.t('capture.rec.stopped')} · ${fmt(st.elapsedSec)} · ${R.t('capture.rec.segments', { count: segs })}</div>
                ${restoreHTML()}
                <div class="rc-field">
                    <label class="rc-field__label" for="rcRecName">${R.t('capture.rec.nameLabel')}</label>
                    <input class="rc-input" id="rcRecName" placeholder="${U.esc(autoName || R.t('capture.rec.namePh'))}" autocomplete="off" maxlength="200">
                </div>
                <div class="rc-field">
                    <label class="rc-field__label" for="rcRecDesc">${R.t('capture.common.descOpt')}</label>
                    <textarea class="rc-textarea" id="rcRecDesc" maxlength="4000" placeholder="${R.t('capture.common.descPh')}"></textarea>
                </div>
                <label class="rc-check" style="margin-top:8px"><input type="checkbox" id="rcRecWantTr" checked> <span>${R.t('capture.rec.wantTr')}</span></label>
                <div class="rc-rec-tropts" id="rcRecTrOpts">
                    <div class="rc-yt__settings">
                        <div class="rc-field"><label class="rc-field__label" for="rcRecModel">${R.t('capture.rec.model')}</label><select class="rc-select" id="rcRecModel">${modelOpts()}</select></div>
                        <div class="rc-field"><label class="rc-field__label" for="rcRecTrLang">${R.t('capture.common.language')}</label><select class="rc-select" id="rcRecTrLang">${langOpts()}</select></div>
                        <div class="rc-field"><label class="rc-field__label" for="rcRecCat">${R.t('capture.common.category')}</label><select class="rc-select rc-catsel" id="rcRecCat" data-cat-first="none">${catOpts()}</select></div>
                    </div>
                </div>
                <div class="rc-yt__actions" style="margin-top:20px">
                    <button class="rc-btn" id="rcRecCancel"><i class="fa-solid fa-trash"></i> ${R.t('capture.rec.cancelRecording')}</button>
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcRecSave"><i class="fa-solid fa-floppy-disk"></i> <span id="rcRecSaveLbl">${R.t('capture.rec.saveTr')}</span></button>
                </div>
                <div id="rcRecSaveRun"></div>
            </div>`;
        const want = body.querySelector('#rcRecWantTr');
        const opts = body.querySelector('#rcRecTrOpts');
        const lbl = body.querySelector('#rcRecSaveLbl');
        const syncWant = () => { opts.hidden = !want.checked; lbl.textContent = want.checked ? R.t('capture.rec.saveTr') : R.t('capture.rec.saveOnly'); };
        want.addEventListener('change', syncWant); syncWant();
        bindCatPath(body.querySelector('#rcRecCat'));
        wireRestore(ctx, body);
        body.querySelector('#rcRecName').focus();
        body.querySelector('#rcRecSave').addEventListener('click', () => save(ctx));
        body.querySelector('#rcRecCancel').addEventListener('click', () => discard(ctx));
    }

    async function save(ctx) {
        const S = st;
        const sid = S && S.sessionId;
        if (!sid) return;
        const name = (ctx.mount.querySelector('#rcRecName') || {}).value || '';
        const description = ((ctx.mount.querySelector('#rcRecDesc') || {}).value || '').trim();
        const want = !!(ctx.mount.querySelector('#rcRecWantTr') || {}).checked;
        const model = (ctx.mount.querySelector('#rcRecModel') || {}).value || DEFAULT_MODEL;
        const lang = (ctx.mount.querySelector('#rcRecTrLang') || {}).value || 'uk';
        const cat = (ctx.mount.querySelector('#rcRecCat') || {}).value;
        const btn = ctx.mount.querySelector('#rcRecSave');
        if (btn) btn.disabled = true;
        const run = ctx.mount.querySelector('#rcRecSaveRun');
        if (run) run.innerHTML = `<div class="rc-run__hint rc-mono" style="margin-top:12px"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('capture.rec.saving')}</div>`;
        applyPendingRestore(ctx, S);

        const savePayload = { name: name.trim() };
        if (description) savePayload.description = description;
        let sb;
        try {
            sb = await R.api.post('/api/recording/' + sid + '/save', savePayload);
        } catch (err) {
            // Finalize довший за дедлайн (504) → запис усе одно авто-реєструється
            // на сервері. Не блокуємо — ведемо в Аудіотеку, де він зʼявиться.
            if (err && err.status === 504) {
                UI.toast(R.t('capture.rec.savingBg'), 'info');
                dropSession(S);
                if (ctx.isCurrent()) afterDrop(ctx, () => { reset(); R.router.navigate('/audio'); });
                return;
            }
            UI.toast(R.t('capture.rec.errorWith', { error: (err && err.message) || '' }), 'error');
            if (btn) btn.disabled = false;
            if (run) run.innerHTML = '';
            return;
        }

        UI.toast(R.t('capture.rec.savedAs', { name: sb.name || R.t('capture.rec.untitled') }), 'success');

        // Транскрипція — у ФОНІ (неблокуюча). Йдемо library-шляхом по download_id,
        // щоб серверний active-tracker (/api/transcribe/active) позначив картку
        // «транскрибується…». Запит НЕ чекаємо: postForm без abort-сигналу
        // переживає навігацію, а сервер дорахує навіть якщо клієнт пішов. Юзер
        // одразу бачить запис у Аудіотеці зі статусом — нічого «не зникає».
        if (want && sb.download_id) {
            const fd = new FormData();
            fd.append('source_type', 'library');
            fd.append('audio_download_id', String(sb.download_id));
            fd.append('model', model);
            fd.append('language', lang);
            if (cat) fd.append('category_id', cat);
            R.api.transcribe(fd).catch(() => { /* фоново; статус видно в Аудіотеці */ });
            UI.toast(R.t('capture.rec.transcribingBg'), 'info');
        }

        dropSession(S);
        if (!ctx.isCurrent()) return;
        // Інші записи ще йдуть → лишаємось на екрані запису; інакше — як раніше.
        afterDrop(ctx, () => { reset(); R.router.navigate('/audio'); });
    }

    async function discard(ctx) {
        const S = st;
        if (!S || !S.sessionId) { renderSetup(ctx); return; }
        const ok = await UI.confirmModal({ title: R.t('capture.rec.discardTitle'), message: R.t('capture.rec.discardMsg'), confirmLabel: R.t('capture.rec.cancelRecording') });
        if (!ok) return;
        const sid = S.sessionId;
        haltS(S);
        if (S.stage === 'stopname') applyPendingRestore(ctx, S);
        try { await R.api.post('/api/recording/' + sid + '/discard'); } catch (_) {}
        UI.toast(R.t('capture.rec.discarded'), 'info');
        dropSession(S);
        if (!ctx.isCurrent()) return;
        afterDrop(ctx, () => select(ctx, null));
    }

    function fmt(sec) {
        const t = Math.max(0, Math.floor(sec));
        return String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
    }

    // ---- live REC meta: розмір файлу, швидкість росту, місце на диску --------
    const GIB = 1073741824, MIB = 1048576;
    function fmtBytes(b) {
        if (b == null) return '';
        const mb = b / MIB;
        if (mb < 1024) return R.t('capture.rec.unit.mb', { n: mb.toFixed(1) });
        return R.t('capture.rec.unit.gb', { n: (mb / 1024).toFixed(2) });
    }
    function fmtDur(sec) {
        sec = Math.max(0, Math.floor(sec));
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
        if (h > 0) return R.t('capture.rec.unit.hm', { h, m });
        if (m > 0) return R.t('capture.rec.unit.m', { m });
        return R.t('capture.rec.unit.lt1m');
    }
    function recTotalBytes() {
        let total = 0;
        if (st.audioBytes) for (const k in st.audioBytes) total += st.audioBytes[k] || 0;
        if (st.videoTracks) for (const k in st.videoTracks) total += (st.videoTracks[k].bytes || 0);
        return total;
    }
    function updateRecMeta() {
        const el = $id('rcRecMeta');
        if (!el) return;
        const total = recTotalBytes();
        // середній bitrate з початку запису — стабільніший за миттєвий (аудіо тікає раз на 5с)
        const rateBps = st.elapsedSec > 1 ? total / st.elapsedSec : 0;
        const parts = [];
        parts.push('<span><i class="fa-solid fa-database"></i> ' + fmtBytes(total) + '</span>');
        if (rateBps > 0) parts.push('<span>' + R.t('capture.rec.unit.mbPerMin', { n: (rateBps * 60 / MIB).toFixed(1) }) + '</span>');
        if (st.diskFree != null) {
            let txt = '<i class="fa-solid fa-hard-drive"></i> ' + R.t('capture.rec.diskFree', { size: fmtBytes(st.diskFree) });
            let warn = st.diskFree < 2 * GIB;
            if (rateBps > 0) {
                const left = st.diskFree / rateBps;        // секунд до заповнення
                txt += ' · ~' + fmtDur(left);
                if (left < 600) warn = true;               // < 10 хв запасу
            }
            parts.push('<span class="rc-rec-meta__disk' + (warn ? ' is-warn' : '') + '">' + txt + '</span>');
        }
        el.innerHTML = parts.join('<span class="rc-rec-meta__sep">·</span>');
    }
    // п.4: статус аудіо-дорожок — провалений потік підсвічує свій VU-рядок (не лише разовий toast)
    function markStreamStatus() {
        const map = { mic: 'rcVuMic', system: 'rcVuSys' };
        for (const name in map) {
            const row = $id(map[name]);
            if (!row) continue;
            const failed = !!(st.streamErr && st.streamErr[name]);
            row.classList.toggle('is-failed', failed);
            if (failed) row.title = st.streamErr[name]; else row.removeAttribute('title');
        }
    }

    R.views.record = {
        render,
        destroy() {
            // Навігація геть рве читачі й тіки всіх записів (самі записи живуть на сервері).
            for (const S of sessions.values()) haltS(S);
            // Хоткей живе на document, тож без явного зняття він пережив би
            // сторінку і ловив Ctrl+Shift+K у решті застосунку.
            if (_liveCmHotkey) {
                document.removeEventListener('keydown', _liveCmHotkey);
                _liveCmHotkey = null;
            }
        }
    };
})();
