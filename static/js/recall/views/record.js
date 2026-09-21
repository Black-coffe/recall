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
        { v: 'uk', l: 'Українська' }, { v: 'en', l: 'English' },
        { v: 'ru', l: 'Русский' }, { v: 'auto', l: 'Авто-визначення' },
    ];
    const DEFAULT_MODEL = 'large-v3-turbo';

    // Co-pilot (Phase 19): мега-налаштування на старті дзвінка.
    const CP_MODES = [{ v: 'light', l: 'Лайтовий' }, { v: 'medium', l: 'Середній' }, { v: 'hard', l: 'Жорсткий' }];
    const CP_IMP = [{ v: 'low', l: 'Низька' }, { v: 'medium', l: 'Середня' }, { v: 'high', l: 'Висока' }];

    let st = null;      // { sessionId, status, elapsedSec, devices, screens, videoAvailable, videoTracks, videoRegions, abort, tick, copilotSessionId }
    let _models = null;
    let _cats = null;
    let _cpAvail = null;   // { enabled, local_llm_available, local_llm_reason, ... }
    let _liveCmHotkey = null;   // Ctrl+Shift+K → фокус у поле живого коментаря

    function reset() {
        if (st && st.abort) { try { st.abort.abort(); } catch (_) {} }
        if (st && st.tick) { clearInterval(st.tick); }
        st = { sessionId: null, status: 'idle', elapsedSec: 0, devices: [], screens: [], videoAvailable: false, videoTracks: {}, videoRegions: {}, audioBytes: {}, streamErr: {}, diskFree: null, diskTotal: null, abort: null, tick: null, copilotSessionId: null, copilotConfig: null, topics: [], curTopic: null, insights: [], cpStatus: 'analyzing', cpUnseen: 0, cpUsage: null, sseState: 'ok' };
    }

    async function render(ctx) {
        reset();
        _models = _models || (await loadModels());
        _cats = await UI.loadCategories();
        try { _cpAvail = await R.api.get('/api/copilot/availability'); } catch (_) { _cpAvail = null; }

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-microphone" style="color:var(--rc-src-recording)"></i> Додати у архів · Запис</div>
                <h1 class="rc-pagehead__title">Запис</h1>
                <p class="rc-pagehead__lede">Запис іде на сервері — переживе закриття вкладки й обрив інтернету. Кожні ~5 секунд чанк зберігається на диск. Можна писати мікрофон, системний звук або обидва.</p>
            </div>
            <div id="rcRecBody"></div>`;

        // device list (503 → recorder backend disabled)
        try {
            const d = await R.api.get('/api/recording/devices');
            st.devices = d.devices || [];
        } catch (err) {
            if (!ctx.isCurrent()) return;
            ctx.mount.querySelector('#rcRecBody').innerHTML = UI.empty('Запис недоступний',
                err.status === 503 ? 'Рекордер вимкнено на бекенді (немає аудіо-підсистеми).' : (err.message || ''), 'fa-microphone-slash');
            return;
        }

        // screen list — best-effort; failure is non-fatal (video section just shows as unavailable)
        try {
            const sv = await R.api.get('/api/recording/screens');
            st.videoAvailable = !!(sv && sv.video_available);
            st.screens = (sv && sv.screens) || [];
        } catch (_) {
            st.videoAvailable = false;
            st.screens = [];
        }

        // reattach to an active server-side session, if any
        let active = null;
        try { active = await R.api.get('/api/recordings/active'); } catch (_) {}
        if (!ctx.isCurrent()) return;
        if (active && active.active && active.session_id) {
            st.sessionId = active.session_id;
            const snap = active.state || {};
            st.status = snap.status || 'recording';
            st.elapsedSec = snap.elapsed_seconds || 0;
            // seed live size/disk so the REC meta line is populated before the first chunk_saved
            st.audioBytes = {};
            st.streamErr = {};
            if (snap.streams) for (const k in snap.streams) {
                st.audioBytes[k] = (snap.streams[k] && snap.streams[k].bytes) || 0;
                if (snap.streams[k] && snap.streams[k].error) st.streamErr[k] = snap.streams[k].error;
            }
            if (typeof snap.disk_free === 'number') st.diskFree = snap.disk_free;
            if (typeof snap.disk_total === 'number') st.diskTotal = snap.disk_total;
            // seed video tracks (incl. region) from the manifest so chips survive a reload mid-recording
            st.videoTracks = {};
            applyStateVideoTracks(snap.video_tracks);
            renderActive(ctx);
            connectSSE(ctx);
            startTick(ctx);
        } else {
            renderSetup(ctx);
        }
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
    function langOpts() { return LANGS.map(l => `<option value="${l.v}"${l.v === 'uk' ? ' selected' : ''}>${l.l}</option>`).join(''); }
    function catOpts() {
        return UI.catOptions('', { first: 'none' });
    }

    // ---- Co-pilot mega-settings (Phase 19, Крок 1) -------------------------
    function cpOpts(arr, def) {
        return arr.map(o => `<option value="${o.v}"${o.v === def ? ' selected' : ''}>${o.l}</option>`).join('');
    }
    function copilotPanel() {
        if (!_cpAvail || !_cpAvail.enabled) return '';   // фіча вимкнена на бекенді
        const status = _cpAvail.local_llm_available
            ? `<span class="rc-mono" style="color:var(--rc-ok)"><i class="fa-solid fa-circle-check"></i> локальна модель готова</span>`
            : `<span class="rc-mono" style="color:var(--rc-warn,#fb8c00)" title="${U.esc(_cpAvail.local_llm_reason || '')}"><i class="fa-solid fa-circle-exclamation"></i> Ollama не готовий — підказки запрацюють після запуску</span>`;
        // T5.2 (REMEDIATION_PLAN Волна 3): режим/важливість/бюджет — power-user
        // налаштування, сховані за вкладеним "Розширені" — новий користувач, що
        // розгортає "Ко-пілот наживо" на старті першого дзвінка, бачить лише
        // напрямок + просте "лише локально" перемикання + пояснення поточного
        // дефолту (medium/medium), а не 3 селекти й числове поле бюджету одразу.
        return `
            <details class="rc-rec-copilot" id="rcCopilot">
                <summary style="cursor:pointer;font-weight:600"><i class="fa-solid fa-wand-magic-sparkles"></i> Ко-пілот наживо <span style="margin-left:8px;font-weight:400">${status}</span></summary>
                <div style="margin-top:12px">
                    <div class="rc-field"><label class="rc-field__label" for="rcCpVector">Вектор (напрямок)</label><select class="rc-select rc-catsel" id="rcCpVector" data-cat-first="none">${catOpts()}</select></div>
                    <div class="rc-field" style="margin-top:10px"><label class="rc-field__label" for="rcCpScope">Про що дзвінок — проєкти та учасники</label><input class="rc-input" id="rcCpScope" type="text" placeholder="напр. Acmecorp, Адам — через кому (не обовʼязково)"><div class="rc-rec-hint rc-mono" style="margin-top:4px">Звужує пошук у архіві до цих проєктів/людей. Без цього копілот шукає по всьому напрямку.</div></div>
                    <label class="rc-check" style="margin-top:10px"><input type="checkbox" id="rcCpLocalOnly"> <span>Лише локально (без Claude API, $0)</span></label>
                    <div class="rc-rec-hint rc-mono" id="rcCpHint" style="margin-top:10px"></div>
                    <details style="margin-top:12px">
                        <summary class="rc-mono" style="cursor:pointer;color:var(--rc-ink-3);font-size:var(--rc-t-xs)">Розширені</summary>
                        <div style="margin-top:10px">
                            <div class="rc-yt__settings">
                                <div class="rc-field"><label class="rc-field__label" for="rcCpMode">Режим</label><select class="rc-select" id="rcCpMode">${cpOpts(CP_MODES, 'medium')}</select></div>
                                <div class="rc-field"><label class="rc-field__label" for="rcCpImportance">Важливість</label><select class="rc-select" id="rcCpImportance">${cpOpts(CP_IMP, 'medium')}</select></div>
                            </div>
                            <div class="rc-field" style="max-width:220px;margin-top:10px"><label class="rc-field__label" for="rcCpBudget">Бюджет Claude, $ за сесію</label><input class="rc-input" id="rcCpBudget" type="number" min="0" step="0.5" placeholder="авто (за важливістю)"></div>
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
            ? 'Лише локально — без зовнішніх викликів, $0.'
            : 'Гібрид: локальна модель + Claude-верифікація протиріч.';
        return `Режим «${(CP_MODES.find(m => m.v === mode) || {}).l}», важливість «${(CP_IMP.find(i => i.v === imp) || {}).l}». ${where} Низька важливість за замовч. = лише локально.`;
    }
    function wireCopilot(ctx) {
        if (!ctx.mount.querySelector('#rcCopilot')) return;
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
        if (scope) cp.scope_projects = scope;   // «Acmecorp, Адам» → зріз пошуку (Трек 2/3)
        if (localOnly) cp.api_enabled = false;   // інакше — дефолт за важливістю (бекенд)
        const b = get('#rcCpBudget');
        if (b && !localOnly) cp.budget_usd = parseFloat(b);
        return cp;
    }

    // ---- video section helpers -----------------------------------------------
    function videoSection() {
        const avail = st.videoAvailable;
        const disabledAttr = avail ? '' : ' disabled';
        const hint = avail ? '' : `<span class="rc-rec-hint rc-mono">Недоступно: потрібні NVENC + ddagrab</span>`;
        const cards = st.screens.map(s => {
            const primaryBadge = s.is_primary ? `<span class="rc-screen-card__badge">PRIMARY</span>` : '';
            const thumb = s.thumbnail ? `<img class="rc-screen-card__thumb" src="${U.esc(s.thumbnail)}" alt="">` : '';
            const reg = st.videoRegions[String(s.monitor_index)];
            const regionChip = (reg && reg.mode === 'region' && reg.region)
                ? `<span class="rc-screen-region-chip rc-mono">Область ${reg.region.w}×${reg.region.h}</span>` : '';
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
                        <button class="rc-screen-mode__btn is-active" data-monitor="${s.monitor_index}" data-mode="full">Повний</button>
                        <button class="rc-screen-mode__btn" data-monitor="${s.monitor_index}" data-mode="region">Область</button>
                    </div>
                    <button class="rc-screen-region-pick" data-monitor="${s.monitor_index}" id="rcPickRegion_${s.monitor_index}" hidden>
                        <i class="fa-solid fa-crop-simple"></i> Виділити область
                    </button>
                    ${regionChip}
                </div>
            </div>`;
        }).join('');
        return `
            <div class="rc-rec-dev">
                <label class="rc-check"><input type="checkbox" id="rcRecVideoEn"${disabledAttr}>
                    📺 <span>Записувати екран</span></label>
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
                st.videoRegions[String(idx)] = { mode: 'full', region: null };
                updateRegionChip(wrap, null);
            } else {
                // switching to region: keep existing region if any, just show pick button
                if (!st.videoRegions[String(idx)] || st.videoRegions[String(idx)].mode !== 'region') {
                    st.videoRegions[String(idx)] = { mode: 'region', region: null };
                }
                updateRegionChip(wrap, (st.videoRegions[String(idx)] || {}).region);
            }
        });

        // wire pick-region buttons
        screens.addEventListener('click', e => {
            const btn = e.target.closest('.rc-screen-region-pick');
            if (!btn) return;
            const idx = +btn.dataset.monitor;
            const screenInfo = st.screens.find(s => s.monitor_index === idx);
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
            chip.textContent = `Область ${region.w}×${region.h}`;
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
                    <span class="rc-region-panel__title"><i class="fa-solid fa-crop-simple"></i> Виділити область — ${U.esc(screenInfo.label || ('Monitor ' + monitorIdx))}</span>
                    <button class="rc-region-close" id="rcRegionCancel" title="Скасувати"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <div class="rc-region-stage-wrap">
                    <div class="rc-region-stage" id="rcRegionStage">
                        <div class="rc-region-loading rc-mono" id="rcRegionLoading"><i class="fa-solid fa-spinner fa-spin"></i> Завантаження превʼю…</div>
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
                    <button class="rc-btn" id="rcRegionCancelBtn"><i class="fa-solid fa-xmark"></i> Скасувати</button>
                    <div style="flex:1"></div>
                    <button class="rc-btn rc-btn--primary" id="rcRegionDone"><i class="fa-solid fa-check"></i> Готово</button>
                </div>
            </div>`;
        document.body.appendChild(modal);

        // current saved region (if any) to pre-populate
        const saved = (st.videoRegions[String(monitorIdx)] || {}).region || null;

        // fetch preview image
        const stage = modal.querySelector('#rcRegionStage');
        const loading = modal.querySelector('#rcRegionLoading');
        const rectEl = modal.querySelector('#rcRegionRect');
        const sizeEl = modal.querySelector('#rcRegionSize');
        let stageImg = null;
        let dragState = null;    // { startX, startY } in stage-relative pixels
        let currentRect = null;  // { x, y, w, h } in display pixels (stage-relative)

        R.api.get('/api/recording/screens/' + monitorIdx + '/preview?w=1000').then(data => {
            if (!data || !data.image) throw new Error('Немає зображення');
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
            loading.innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="color:var(--rc-err)"></i> Не вдалось завантажити превʼю: ${U.esc(err && err.message || '')}. Введіть координати вручну.`;
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
                    UI.toast('Виділіть область або введіть координати', 'error');
                    return;
                }
                const r2 = { x: evenFloor(rx), y: evenFloor(ry), w: evenFloor(rw), h: evenFloor(rh) };
                st.videoRegions[String(monitorIdx)] = { mode: 'region', region: r2 };
                refreshCardChip(monitorIdx, r2);
                modal.remove();
                return;
            }
            st.videoRegions[String(monitorIdx)] = { mode: 'region', region: real };
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
        const mics = st.devices.filter(d => d.kind === 'mic');
        const lbs = st.devices.filter(d => d.kind === 'loopback');
        const devOpts = (arr, empty) => arr.length
            ? arr.map(d => `<option value="${d.index}" data-name="${U.esc(d.name)}"${d.is_default ? ' selected' : ''}>${U.esc(d.name)}${d.is_default ? ' (типовий)' : ''}</option>`).join('')
            : `<option value="">${empty}</option>`;

        const body = ctx.mount.querySelector('#rcRecBody');
        body.innerHTML = `
            <div class="rc-rec-setup">
                <div class="rc-rec-dev">
                    <label class="rc-check"><input type="checkbox" id="rcRecMicEn" ${mics.length ? 'checked' : ''} ${mics.length ? '' : 'disabled'}>
                        <i class="fa-solid fa-microphone"></i> <span>Мікрофон</span></label>
                    <select class="rc-select" id="rcRecMic" ${mics.length ? '' : 'disabled'}>${devOpts(mics, 'Немає мікрофонів')}</select>
                </div>
                <div class="rc-rec-dev">
                    <label class="rc-check"><input type="checkbox" id="rcRecSysEn" ${lbs.length ? 'checked' : ''} ${lbs.length ? '' : 'disabled'}>
                        <i class="fa-solid fa-volume-high"></i> <span>Системний звук</span></label>
                    <select class="rc-select" id="rcRecSys" ${lbs.length ? '' : 'disabled'}>${devOpts(lbs, 'Немає loopback')}</select>
                </div>
                ${videoSection()}
                <div class="rc-field" style="max-width:260px">
                    <label class="rc-field__label" for="rcRecLang">Мова live-превʼю</label>
                    <select class="rc-select" id="rcRecLang">${langOpts()}</select>
                </div>
                ${copilotPanel()}
                <div class="rc-rec-hint rc-mono"><i class="fa-solid fa-shield-halved"></i> Якщо мікрофона немає у списку — дозвольте доступ у налаштуваннях ОС. Системний звук (loopback) дозволу не потребує.</div>
                <div class="rc-yt__actions">
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcRecStart"><i class="fa-solid fa-circle" style="color:var(--rc-err)"></i> Розпочати запис</button>
                </div>
            </div>`;
        body.querySelector('#rcRecStart').addEventListener('click', () => start(ctx));
        wireVideoToggle(ctx);
        wireCopilot(ctx);
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
        if (payload.mic_device_index == null && payload.system_device_index == null) {
            UI.toast('Оберіть хоча б один потік', 'error'); return;
        }
        const langSel = ctx.mount.querySelector('#rcRecLang');
        if (langSel && langSel.value) payload.language = langSel.value;

        const cp = collectCopilot(ctx);
        if (cp) payload.copilot = cp;
        st.copilotConfig = cp || null;

        // video capture (S7 + R-D): only add the key when the checkbox is checked
        const venc = ctx.mount.querySelector('#rcRecVideoEn');
        if (venc && venc.checked) {
            const picked = [...ctx.mount.querySelectorAll('.rc-screen-card-wrap input[type=checkbox]:checked')]
                .map(cb => {
                    const idx = +cb.value;
                    const track = { monitor_index: idx, fps: 30, codec: 'h264_nvenc', quality: 'p5' };
                    const reg = st.videoRegions[String(idx)];
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
            if (!r || !r.session_id) throw new Error((r && r.error) || 'Не вдалось розпочати');
            st.sessionId = r.session_id;
            st.status = 'recording';
            st.elapsedSec = 0;
            st.audioBytes = {};
            st.streamErr = {};
            st.diskFree = null;
            st.diskTotal = null;
            st.copilotSessionId = r.copilot_session_id || null;
            // pre-populate videoTracks from the start response if video is enabled
            st.videoTracks = {};
            if (r.video && r.video.enabled && Array.isArray(r.video.tracks)) {
                for (const t of r.video.tracks) {
                    st.videoTracks[String(t.track_id != null ? t.track_id : t.monitor_index)] = {
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
            renderActive(ctx);
            connectSSE(ctx);
            startTick(ctx);
            // start-відповідь несе лише monitor_index; справжні track_id ('mon0',…) знає
            // супервізор. Звіряємось зі state трохи згодом, щоб чипи стали канонічними
            // (правильна мітка/статус + region) і live video_stats почали потрапляти в ціль.
            if (r.video && r.video.enabled) reconcileVideoTracks(ctx, st.sessionId);
        } catch (err) {
            UI.toast('Не вдалось розпочати: ' + (err && err.message), 'error');
            if (btn) btn.disabled = false;
        }
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
                    <div class="rc-rec-vid-banner" id="rcRecSseBanner" hidden></div>
                    <div class="rc-rec-meta rc-mono" id="rcRecMeta"></div>
                    <div class="rc-rec-vid-banner" id="rcRecAudioBanner" hidden></div>
                    <div class="rc-vu" id="rcVuMic"><span class="rc-vu__lbl rc-mono">MIC</span><div class="rc-vu__bar"><div class="rc-vu__fill"></div><div class="rc-vu__peak"></div></div><span class="rc-vu__val rc-mono"></span></div>
                    <div class="rc-vu" id="rcVuSys"><span class="rc-vu__lbl rc-mono">SYS</span><div class="rc-vu__bar"><div class="rc-vu__fill"></div><div class="rc-vu__peak"></div></div><span class="rc-vu__val rc-mono"></span></div>
                    <div class="rc-rec-vid" id="rcRecVid"></div>
                    <div class="rc-rec-vid-banner" id="rcRecVidBanner" hidden></div>
                    <div class="rc-rec-live" id="rcRecLive" hidden></div>
                    ${liveCommentHTML()}
                    <div class="rc-yt__actions" style="margin-top:24px">
                        <button class="rc-btn" id="rcRecDiscard"><i class="fa-solid fa-trash"></i> Скасувати</button>
                        <div class="rc-yt__spacer"></div>
                        <button class="rc-btn" id="rcRecPause"><i class="fa-solid ${st.status === 'paused' ? 'fa-play' : 'fa-pause'}"></i> <span id="rcRecPauseLbl">${st.status === 'paused' ? 'Продовжити' : 'Пауза'}</span></button>
                        <button class="rc-btn rc-btn--primary" id="rcRecStop"><i class="fa-solid fa-stop"></i> Зупинити</button>
                    </div>
                </div>
                ${cp ? copilotWidget() : ''}
            </div>`;
        body.querySelector('#rcRecPause').addEventListener('click', () => pauseResume(ctx));
        body.querySelector('#rcRecStop').addEventListener('click', () => stop(ctx));
        body.querySelector('#rcRecDiscard').addEventListener('click', () => discard(ctx));
        wireLiveComment(ctx);
        if (cp) wireWidget();
        renderVideoChips();
        updateRecMeta();
        markStreamStatus();
        refreshAudioBanner();
        refreshPill();
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
                           placeholder="Коментар до цього моменту — Enter, щоб зберегти">
                    <select class="rc-select rc-select--sm rc-livecm__kind" id="rcLiveCmKind">
                        <option value="note">Нотатка</option>
                        <option value="correction">Виправлення</option>
                        <option value="decision">Рішення</option>
                        <option value="question">Питання</option>
                    </select>
                    <span class="rc-livecm__hint rc-mono">Ctrl+Shift+K</span>
                </div>
                <div class="rc-livecm__log" id="rcLiveCmLog"></div>
            </div>`;
    }

    function wireLiveComment(ctx) {
        const box = document.getElementById('rcLiveCm');
        const inp = document.getElementById('rcLiveCmIn');
        if (!box || !inp) return;
        inp.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            e.preventDefault();
            saveLiveComment(ctx);
        });
        if (!_liveCmHotkey) {
            _liveCmHotkey = (e) => {
                if (!(e.ctrlKey && e.shiftKey && (e.key === 'K' || e.key === 'k'))) return;
                const el = document.getElementById('rcLiveCmIn');
                if (!el) return;
                e.preventDefault();
                el.focus();
            };
            document.addEventListener('keydown', _liveCmHotkey);
        }
        refreshLiveComments();
    }

    async function saveLiveComment(ctx) {
        const inp = document.getElementById('rcLiveCmIn');
        const kindSel = document.getElementById('rcLiveCmKind');
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
            UI.toast((err && err.message) || 'Коментар не збережено', 'error');
        }
    }

    async function refreshLiveComments() {
        const log = document.getElementById('rcLiveCmLog');
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
        const pill = document.getElementById('rcRecPill');
        const txt = document.getElementById('rcRecStatusTxt');
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
        const b = document.getElementById('rcRecSseBanner');
        if (!b) return;
        b.hidden = false;
        b.innerHTML = '';
        const icon = document.createElement('i');
        icon.className = 'fa-solid ' + (kind === 'failed' ? 'fa-triangle-exclamation' : 'fa-rotate fa-spin');
        b.appendChild(icon);
        b.appendChild(document.createTextNode(kind === 'failed'
            ? ' Live-перегляд відключено — запис триває, перепідключитись не вдалось. Оновіть сторінку, щоб відновити моніторинг.'
            : ' Live-перегляд відключено — запис триває, перепідключення' + (attempt ? ' (спроба ' + attempt + ')' : '') + '…'));
    }
    function hideSseBanner() {
        const b = document.getElementById('rcRecSseBanner');
        if (b) { b.hidden = true; b.innerHTML = ''; }
    }

    // ---- audio-stream failure banner (T4.8) — same permanent .rc-rec-vid-banner
    // treatment as the video-track failure banner (see updateVideoTrack below),
    // scaled UP in severity by wording: audio is the whole point of the
    // product, so a dead mic/system stream is worse than a dead screen capture.
    // Previously this only got an auto-dismissing toast + VU-row highlight
    // (markStreamStatus) — that stays for the in-context cue, this adds the
    // persistent one. -----------------------------------------------------
    const AUDIO_STREAM_LBL = { mic: 'Мікрофон', system: 'Системний звук' };
    function refreshAudioBanner() {
        const b = document.getElementById('rcRecAudioBanner');
        if (!b) return;
        const failed = Object.keys(st.streamErr || {}).filter(k => st.streamErr[k]);
        if (!failed.length) { b.hidden = true; b.innerHTML = ''; return; }
        const label = failed.map(k => AUDIO_STREAM_LBL[k] || k).join(', ');
        const detail = failed.map(k => st.streamErr[k]).filter(Boolean).join('; ');
        b.hidden = false;
        b.innerHTML = '';
        const icon = document.createElement('i');
        icon.className = 'fa-solid fa-microphone-slash';
        b.appendChild(icon);
        b.appendChild(document.createTextNode(
            ' Аудіопотік «' + label + '» відвалився — запис без цього джерела триває' + (detail ? ' (' + detail + ')' : '') + '.'
        ));
    }

    const CP_MODE_LBL = { light: 'лайт', medium: 'середній', hard: 'жорсткий' };
    const CP_IMP_LBL = { low: 'важл. низька', medium: 'важл. середня', high: 'важл. висока' };
    const CP_MODE_NEXT = { light: 'medium', medium: 'hard', hard: 'light' };
    const CP_IMP_NEXT = { low: 'medium', medium: 'high', high: 'low' };
    function copilotWidget() {
        const c = st.copilotConfig || {};
        const mode = c.mode || 'medium';
        const imp = c.importance || 'medium';
        return `
            <aside class="rc-cp" id="rcCp">
                <div class="rc-cp__head">
                    <span class="rc-cp__title"><i class="fa-solid fa-wand-magic-sparkles"></i> Ко-пілот</span>
                    <span class="rc-cp__status" id="rcCpStatus" data-state="analyzing"><span class="rc-cp__dot"></span><span id="rcCpStatusTxt">аналіз…</span></span>
                    <span class="rc-cp__cost rc-mono" id="rcCpCost" hidden></span>
                    <span class="rc-cp__count rc-mono" id="rcCpCount" hidden>0</span>
                    <span class="rc-cp__count rc-mono" id="rcCpBudgetLbl" title="Показано карток за дзвінок (бюджет уваги)" hidden></span>
                    <button class="rc-cp__collapse" id="rcCpCollapse" title="Згорнути/розгорнути"><i class="fa-solid fa-chevron-right"></i></button>
                </div>
                <div class="rc-cp__ctrls">
                    <button class="rc-cp__chip rc-mono" id="rcCpMode" data-mode="${mode}" title="Режим аналізу — клік змінює (лайт/середній/жорсткий)"><i class="fa-solid fa-gauge-high"></i> ${CP_MODE_LBL[mode]}</button>
                    <button class="rc-cp__chip rc-mono" id="rcCpImp" data-imp="${imp}" title="Важливість дзвінка — клік змінює (масштабує бюджет/модель)"><i class="fa-solid fa-flag"></i> ${CP_IMP_LBL[imp]}</button>
                </div>
                <div class="rc-cp__banner" id="rcCpBanner" hidden></div>
                <div class="rc-cp-strip" id="rcCpStrip" hidden></div>
                <div class="rc-cp__feed" id="rcCpInsights"></div>
                <div class="rc-cp__empty" id="rcCpEmpty"><i class="fa-solid fa-ear-listen"></i> Слухаю розмову — підказки зʼявляться тут.</div>
            </aside>`;
    }

    function wireWidget() {
        const collapse = document.getElementById('rcCpCollapse');
        if (collapse) collapse.addEventListener('click', toggleWidget);
        const feed = document.getElementById('rcCpInsights');
        if (feed) feed.addEventListener('click', onInsightAction);
        const modeBtn = document.getElementById('rcCpMode');
        if (modeBtn) modeBtn.addEventListener('click', () => changeSetting('mode', CP_MODE_NEXT[modeBtn.dataset.mode] || 'medium'));
        const impBtn = document.getElementById('rcCpImp');
        if (impBtn) impBtn.addEventListener('click', () => changeSetting('importance', CP_IMP_NEXT[impBtn.dataset.imp] || 'medium'));
        renderTopicStrip();
        renderInsights();
        setCpStatus(st.cpStatus || 'analyzing');
        if (st.cpUsage) applyCopilotUsage(st.cpUsage);
    }

    // Зміна режиму/важливості на льоту під час дзвінка (Крок 6).
    async function changeSetting(field, value) {
        if (!st.copilotSessionId) return;
        try {
            const r = await R.api.post('/api/copilot/' + st.copilotSessionId + '/settings', { [field]: value });
            if (r && r.config) {
                st.copilotConfig = r.config;
                refreshWidgetChips();
                const lbl = field === 'mode' ? CP_MODE_LBL[value] : CP_IMP_LBL[value];
                UI.toast('Ко-пілот: ' + lbl, 'info');
            }
        } catch (err) { UI.toast('Не вдалось змінити: ' + (err && err.message), 'error'); }
    }
    function refreshWidgetChips() {
        const c = st.copilotConfig || {};
        const m = document.getElementById('rcCpMode');
        if (m && c.mode) { m.dataset.mode = c.mode; m.innerHTML = `<i class="fa-solid fa-gauge-high"></i> ${CP_MODE_LBL[c.mode]}`; }
        const ip = document.getElementById('rcCpImp');
        if (ip && c.importance) { ip.dataset.imp = c.importance; ip.innerHTML = `<i class="fa-solid fa-flag"></i> ${CP_IMP_LBL[c.importance]}`; }
    }

    function toggleWidget() {
        const cp = document.getElementById('rcCp');
        if (!cp) return;
        cp.classList.toggle('is-collapsed');
        const ic = document.querySelector('#rcCpCollapse i');
        if (ic) ic.className = 'fa-solid ' + (cp.classList.contains('is-collapsed') ? 'fa-chevron-left' : 'fa-chevron-right');
        if (!cp.classList.contains('is-collapsed')) { st.cpUnseen = 0; updateCount(); }
    }

    function applyPauseUi() {
        const lbl = document.getElementById('rcRecPauseLbl');
        const pauseBtn = document.getElementById('rcRecPause');
        const paused = st.status === 'paused';
        refreshPill();
        if (lbl) lbl.textContent = paused ? 'Продовжити' : 'Пауза';
        if (pauseBtn) { const i = pauseBtn.querySelector('i'); if (i) i.className = 'fa-solid ' + (paused ? 'fa-play' : 'fa-pause'); }
        // ко-пілот: миттєво відобразити паузу/відновлення (SSE copilot_status прийде з лагом тіку)
        if (st.copilotSessionId) setCpStatus(paused ? 'paused' : 'analyzing');
    }

    async function pauseResume(ctx) {
        if (!st.sessionId) return;
        const action = st.status === 'paused' ? 'resume' : 'pause';
        try {
            const r = await R.api.post('/api/recording/' + st.sessionId + '/' + action);
            st.status = r.status || (action === 'pause' ? 'paused' : 'recording');
            applyPauseUi();
        } catch (err) { UI.toast('Помилка: ' + (err && err.message), 'error'); }
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
    function connectSSE(ctx) {
        if (st.abort) { try { st.abort.abort(); } catch (_) {} }
        const ac = new AbortController();
        st.abort = ac;
        const sid = st.sessionId;
        st.sseState = 'ok';
        hideSseBanner();
        R.api.sseReconnecting('/api/recording/' + sid + '/stream', (event, data) => {
            if (!ctx.isCurrent() || st.sessionId !== sid) return;
            if (event === 'level') {
                vu('rcVuMic', data.streams && data.streams.mic);
                vu('rcVuSys', data.streams && data.streams.system);
                if (typeof data.elapsed === 'number') { st.elapsedSec = data.elapsed; const t = document.getElementById('rcRecTimer'); if (t) t.textContent = fmt(st.elapsedSec); }
            } else if (event === 'chunk_saved') {
                if (data.streams) st.audioBytes = Object.assign(st.audioBytes || {}, data.streams);
                if (typeof data.disk_free === 'number') st.diskFree = data.disk_free;
                if (typeof data.disk_total === 'number') st.diskTotal = data.disk_total;
                updateRecMeta();
            } else if (event === 'status') {
                st.status = data.status;
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
                    st.streamErr[data.stream] = data.message || 'помилка';
                    markStreamStatus();
                    refreshAudioBanner();
                }
                UI.toast('Помилка запису: ' + ((data && data.message) || 'невідома'), 'error');
            }
        }, {
            signal: ac.signal,
            requireTerminalFrame: false,
            onReconnecting: (attempt, delayMs) => {
                if (!ctx.isCurrent() || st.sessionId !== sid) return;
                st.sseState = 'reconnecting';
                refreshPill();
                showSseBanner('reconnecting', attempt);
            },
            onReconnected: () => {
                if (!ctx.isCurrent() || st.sessionId !== sid) return;
                st.sseState = 'ok';
                refreshPill();
                hideSseBanner();
            },
            onPermanentFail: () => {
                if (!ctx.isCurrent() || st.sessionId !== sid) return;
                st.sseState = 'error';
                refreshPill();
                showSseBanner('failed');
            },
        });
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
            const banner = document.getElementById('rcRecVidBanner');
            if (banner) {
                banner.hidden = false;
                // use textContent for the safe plain-text part, build with DOM for icon
                banner.innerHTML = '';
                const icon = document.createElement('i');
                icon.className = 'fa-solid fa-triangle-exclamation';
                banner.appendChild(icon);
                banner.appendChild(document.createTextNode(
                    ' Запис екрана зупинено (' + (data.monitor_label || ('Monitor ' + data.monitor_index)) + ') — аудіо триває'
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
        const chip = document.querySelector('.rc-rec-vid-chip[data-track-id="' + key + '"]');
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
    function reconcileVideoTracks(ctx, sid) {
        setTimeout(async () => {
            if (!ctx.isCurrent() || st.sessionId !== sid) return;
            try {
                const s = await R.api.get('/api/recording/' + sid + '/state');
                if (s && s.success && s.state && Array.isArray(s.state.video_tracks) && s.state.video_tracks.length) {
                    applyStateVideoTracks(s.state.video_tracks);
                }
            } catch (_) { /* лишаємо провіжн-чипи зі start-відповіді */ }
        }, 1800);
    }
    function renderVideoChips() {
        const row = document.getElementById('rcRecVid');
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
        const row = document.getElementById(rowId);
        if (!row) return;
        const fill = row.querySelector('.rc-vu__fill');
        const peak = row.querySelector('.rc-vu__peak');
        const val = row.querySelector('.rc-vu__val');
        if (fill) fill.style.width = Math.min(100, (d.rms || 0) * 100) + '%';
        if (peak) peak.style.left = Math.min(100, (d.peak || 0) * 100) + '%';
        if (val) { const db = (d.peak > 0.0001) ? (20 * Math.log10(d.peak)).toFixed(0) + ' dB' : '-∞'; val.textContent = db; }
    }

    function appendSegments(segs) {
        if (!segs.length) return;
        const live = document.getElementById('rcRecLive');
        if (!live) return;
        live.hidden = false;
        for (const s of segs) {
            const text = (s.text || '').trim();
            if (!text) continue;
            const who = s.speaker === 'self' ? 'Ви' : (s.speaker === 'other' ? 'Інший' : '?');
            const div = U.el('div', { class: 'rc-rec-seg' },
                `<span class="rc-rec-seg__who rc-mono ${s.speaker === 'self' ? 'is-self' : 'is-other'}">${who}</span><span>${U.esc(text)}</span>`);
            live.appendChild(div);
        }
        const nearBottom = live.scrollHeight - live.scrollTop - live.clientHeight < 80;
        if (nearBottom) live.scrollTop = live.scrollHeight;
    }

    // ---- Co-pilot: топік-смужка (Крок 2) ----------------------------------
    function renderTopicStrip() {
        const el = document.getElementById('rcCpStrip');
        if (!el) return;
        if (!st.topics.length) { el.hidden = true; return; }
        el.hidden = false;
        el.innerHTML = '<span class="rc-cp-strip__lbl rc-mono">ТЕМИ</span>'
            + st.topics.map(t => {
                const on = t.index === st.curTopic;
                return `<span class="rc-cp-topic rc-mono${on ? ' is-active' : ''}">${U.esc(t.label || ('Тема ' + (t.index + 1)))}</span>`;
            }).join('');
    }
    function applyCopilotTopic(data) {
        if (!data || data.topic_index == null) return;
        const idx = data.topic_index;
        const found = st.topics.find(t => t.index === idx);
        if (!found) st.topics.push({ index: idx, label: data.label || ('Тема ' + (idx + 1)) });
        else if (data.label) found.label = data.label;
        st.curTopic = idx;
        renderTopicStrip();
        if (data.action === 'return') {
            UI.toast('↩ Повернулись до теми: ' + (data.label || ('Тема ' + (idx + 1))), 'info');
        }
    }

    // ---- Co-pilot: індикатор стану (Крок 4) -------------------------------
    const CP_STATE = {
        analyzing: { txt: 'аналіз…',          cls: 'is-analyzing' },
        paused:    { txt: 'на паузі',          cls: 'is-paused' },
        idle:      { txt: 'очікую',            cls: 'is-idle' },
        degraded:  { txt: 'лок. модель офлайн', cls: 'is-degraded' },
        quiet:     { txt: 'тихий режим',       cls: 'is-idle' },
    };
    function applyCopilotStatus(data) {
        if (!data || !data.state) return;
        setCpStatus(data.state, data.reason);
        // Трек 3: бюджет карток вичерпано. Тиша має бути ПОЯСНЕНОЮ — інакше
        // читається як «копілот зламався», а не «він навмисно мовчить».
        if (data.state === 'quiet' && data.reason === 'card_budget') {
            const n = data.suppressed || 0;
            showCpBanner('Бюджет підказок за дзвінок вичерпано' +
                (n ? ` — ще ${n} знахідок збережено, дивись у розборі після дзвінка.` : '.'));
        }
        if (data.cards_shown != null) updateCardBudget(data.cards_shown);
    }
    // «2/5» у шапці віджета: скільки уваги оператора вже витрачено.
    function updateCardBudget(shown) {
        const el = document.getElementById('rcCpBudgetLbl');
        if (!el) return;
        const limit = (st.copilotConfig || {}).max_cards;
        el.hidden = false;
        el.textContent = limit ? `${shown}/${limit}` : String(shown);
    }
    function showCpBanner(text) {
        const el = document.getElementById('rcCpBanner');
        if (!el) return;
        el.hidden = false;
        el.textContent = text;
    }
    // Лічильник вартості верифікації Claude (Крок 5): «$0.12 / $1.00».
    function applyCopilotUsage(data) {
        if (!data) return;
        st.cpUsage = data;
        const el = document.getElementById('rcCpCost');
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
        el.title = over ? 'Бюджет вичерпано — лише локальний аналіз'
            : warn ? 'Бюджет майже вичерпано'
            : ('Верифікація Claude · ' + (data.tokens_in || 0) + '→' + (data.tokens_out || 0) + ' ток.');
    }
    function setCpStatus(state, reason) {
        st.cpStatus = state;
        const el = document.getElementById('rcCpStatus');
        if (!el) return;
        const s = CP_STATE[state] || CP_STATE.analyzing;
        el.dataset.state = state;
        if (reason) el.title = reason;
        const t = document.getElementById('rcCpStatusTxt');
        if (t) t.textContent = s.txt;
        const cp = document.getElementById('rcCp');
        if (cp) cp.classList.toggle('is-paused', state === 'paused');
        // Деградація (Ollama впав) → банер; запис/STT працюють далі (Крок 9).
        const banner = document.getElementById('rcCpBanner');
        if (banner) {
            if (state === 'degraded') {
                banner.hidden = false;
                banner.innerHTML = `<i class="fa-solid fa-triangle-exclamation"></i> Локальна модель недоступна — ко-пілот призупинено. Запис триває.`;
            } else { banner.hidden = true; banner.innerHTML = ''; }
        }
    }

    // ---- Co-pilot: картки-інсайти з діями (Крок 4) ------------------------
    const CP_KIND = {
        contradiction: { icon: 'fa-triangle-exclamation', lbl: 'протиріччя' },
        question:      { icon: 'fa-circle-question',       lbl: 'питання' },
        clarification: { icon: 'fa-lightbulb',             lbl: 'уточнення' },
        fact:          { icon: 'fa-thumbtack',             lbl: 'факт із архіву' },
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
        setCpStatus('analyzing');
        const cp = document.getElementById('rcCp');
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
        const el = document.getElementById('rcCpInsights');
        if (!el) return;
        const items = sortedInsights();
        const empty = document.getElementById('rcCpEmpty');
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
            ? `<span class="rc-cp-card__badge is-verified" title="перевірено Claude"><i class="fa-solid fa-circle-check"></i> ${refuted ? 'спростовано' : 'перевірено'}${pct ? ' · ' + pct : ''}</span>`
            : `<span class="rc-cp-card__badge is-local" title="локальна модель, не перевірено"><i class="fa-solid fa-microchip"></i> локально${pct ? ' · ' + pct : ''}</span>`;
        const time = (ins.ts_offset_sec != null) ? fmt(Math.round(ins.ts_offset_sec)) : '';
        const cnt = (ins.count > 1) ? ` <span class="rc-cp-card__cnt rc-mono">×${ins.count}</span>` : '';
        const ev = (ins.evidence || []).map(e => {
            const name = e.source_name || ('#' + e.transcription_id);
            const slug = U.slug(e.transcription_id, e.source_name || 'Джерело');
            return `<a class="rc-cp-ev rc-mono" href="/transcript/${slug}" title="${U.esc((e.meeting_date || '') + ' · ' + name)}">[${U.esc(name)}]</a>`;
        }).join(' ');
        const act = (a, ic, title, on) =>
            `<button class="rc-cp-act${on ? ' is-on' : ''}" data-act="${a}" title="${title}"><i class="fa-solid ${ic}"></i></button>`;
        return `<div class="rc-cp-card is-${ins.kind}${ins.pinned ? ' is-pinned' : ''}${refuted ? ' is-refuted' : ''}" data-id="${ins.id != null ? ins.id : ''}" data-key="${U.esc(ins.key)}">
            <div class="rc-cp-card__head">
                <i class="fa-solid ${k.icon} rc-cp-card__kico"></i>
                <span class="rc-cp-card__kind rc-mono">${k.lbl}${cnt}</span>
                ${badge}
                ${time ? `<span class="rc-cp-card__time rc-mono">${time}</span>` : ''}
            </div>
            <div class="rc-cp-card__text">${U.esc(ins.text)}</div>
            ${ev ? `<div class="rc-cp-card__ev">${ev}</div>` : ''}
            <div class="rc-cp-card__actions">
                ${act('pin', 'fa-thumbtack', ins.pinned ? 'Відкріпити' : 'Закріпити', ins.pinned)}
                ${act('thumbs_up', 'fa-thumbs-up', 'Корисно', ins.feedback === 'up')}
                ${act('thumbs_down', 'fa-thumbs-down', 'Не корисно', ins.feedback === 'down')}
                ${act('escalate', 'fa-magnifying-glass-plus', 'Копнути глибше (перевірка Claude)', ins.escalated)}
                <span class="rc-yt__spacer"></span>
                ${act('dismiss', 'fa-xmark', 'Відхилити', false)}
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
            UI.toast('Надіслано на глибшу перевірку (Claude — у наступному кроці)', 'info');
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
        const cp = document.getElementById('rcCp');
        const badge = document.getElementById('rcCpCount');
        if (!cp || !badge) return;
        const collapsed = cp.classList.contains('is-collapsed');
        const n = collapsed ? (st.cpUnseen || 0) : sortedInsights().length;
        badge.hidden = !collapsed || n <= 0;
        badge.textContent = String(n);
    }

    function startTick(ctx) {
        if (st.tick) clearInterval(st.tick);
        st.tick = setInterval(() => {
            if (!ctx.isCurrent()) { clearInterval(st.tick); st.tick = null; return; }
            if (st.status === 'recording') { st.elapsedSec += 1; const t = document.getElementById('rcRecTimer'); if (t) t.textContent = fmt(st.elapsedSec); updateRecMeta(); }
        }, 1000);
    }

    // ---- stop → name → transcribe ------------------------------------------
    async function stop(ctx) {
        if (!st.sessionId) return;
        const sid = st.sessionId;
        // close SSE + ticker synchronously before awaiting (recording has ended)
        if (st.abort) { try { st.abort.abort(); } catch (_) {} st.abort = null; }
        if (st.tick) { clearInterval(st.tick); st.tick = null; }
        try {
            await R.api.post('/api/recording/' + sid + '/stop', {});
            st.status = 'stopping';
        } catch (err) { UI.toast('Помилка зупинки: ' + (err && err.message), 'error'); return; }

        // pull auto-name + segments count for the summary
        let autoName = '', segs = 1;
        try { const s = await R.api.get('/api/recording/' + sid + '/state'); if (s.success) { autoName = (s.state && s.state.auto_name) || ''; segs = (s.state && s.state.segments_count) || 1; } }
        catch (_) {}
        if (!ctx.isCurrent()) return;
        renderStopName(ctx, autoName, segs);
    }

    function renderStopName(ctx, autoName, segs) {
        const body = ctx.mount.querySelector('#rcRecBody');
        body.innerHTML = `
            <div class="rc-rec-stop">
                <div class="rc-rec-summary rc-mono"><i class="fa-solid fa-circle-check" style="color:var(--rc-ok)"></i> Запис зупинено · ${fmt(st.elapsedSec)} · ${segs} ${segs === 1 ? 'сегмент' : 'сегментів'}</div>
                <div class="rc-field">
                    <label class="rc-field__label" for="rcRecName">Назва запису</label>
                    <input class="rc-input" id="rcRecName" placeholder="${U.esc(autoName || 'Запис…')}" autocomplete="off" maxlength="200">
                </div>
                <div class="rc-field">
                    <label class="rc-field__label" for="rcRecDesc">Опис (необовʼязково)</label>
                    <textarea class="rc-textarea" id="rcRecDesc" maxlength="4000" placeholder="Короткий опис запису…"></textarea>
                </div>
                <label class="rc-check" style="margin-top:8px"><input type="checkbox" id="rcRecWantTr" checked> <span>Одразу транскрибувати (інакше — лише зберегти аудіо)</span></label>
                <div class="rc-rec-tropts" id="rcRecTrOpts">
                    <div class="rc-yt__settings">
                        <div class="rc-field"><label class="rc-field__label" for="rcRecModel">Модель</label><select class="rc-select" id="rcRecModel">${modelOpts()}</select></div>
                        <div class="rc-field"><label class="rc-field__label" for="rcRecTrLang">Мова</label><select class="rc-select" id="rcRecTrLang">${langOpts()}</select></div>
                        <div class="rc-field"><label class="rc-field__label" for="rcRecCat">Напрямок</label><select class="rc-select rc-catsel" id="rcRecCat" data-cat-first="none">${catOpts()}</select></div>
                    </div>
                </div>
                <div class="rc-yt__actions" style="margin-top:20px">
                    <button class="rc-btn" id="rcRecCancel"><i class="fa-solid fa-trash"></i> Скасувати запис</button>
                    <div class="rc-yt__spacer"></div>
                    <button class="rc-btn rc-btn--primary" id="rcRecSave"><i class="fa-solid fa-floppy-disk"></i> <span id="rcRecSaveLbl">Зберегти та транскрибувати</span></button>
                </div>
                <div id="rcRecSaveRun"></div>
            </div>`;
        const want = body.querySelector('#rcRecWantTr');
        const opts = body.querySelector('#rcRecTrOpts');
        const lbl = body.querySelector('#rcRecSaveLbl');
        const syncWant = () => { opts.hidden = !want.checked; lbl.textContent = want.checked ? 'Зберегти та транскрибувати' : 'Тільки зберегти'; };
        want.addEventListener('change', syncWant); syncWant();
        body.querySelector('#rcRecName').focus();
        body.querySelector('#rcRecSave').addEventListener('click', () => save(ctx));
        body.querySelector('#rcRecCancel').addEventListener('click', () => discard(ctx));
    }

    async function save(ctx) {
        const sid = st.sessionId;
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
        if (run) run.innerHTML = `<div class="rc-run__hint rc-mono" style="margin-top:12px"><i class="fa-solid fa-spinner fa-spin"></i> Зводжу та зберігаю запис… (для довгих записів — до хвилини)</div>`;

        const savePayload = { name: name.trim() };
        if (description) savePayload.description = description;
        let sb;
        try {
            sb = await R.api.post('/api/recording/' + sid + '/save', savePayload);
        } catch (err) {
            // Finalize довший за дедлайн (504) → запис усе одно авто-реєструється
            // на сервері. Не блокуємо — ведемо в Аудіотеку, де він зʼявиться.
            if (err && err.status === 504) {
                UI.toast('Запис зводиться у фоні — зʼявиться в Аудіотеці за хвилину', 'info');
                if (ctx.isCurrent()) { reset(); R.router.navigate('/audio'); }
                return;
            }
            UI.toast('Помилка: ' + (err && err.message), 'error');
            if (btn) btn.disabled = false;
            if (run) run.innerHTML = '';
            return;
        }

        UI.toast('Запис «' + (sb.name || 'без назви') + '» збережено в Аудіотеці', 'success');

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
            UI.toast('Транскрибую у фоні — статус видно в Аудіотеці', 'info');
        }

        if (!ctx.isCurrent()) return;
        reset();
        R.router.navigate('/audio');
    }

    async function discard(ctx) {
        if (!st.sessionId) { reset(); renderSetup(ctx); return; }
        const ok = await UI.confirmModal({ title: 'Скасувати запис?', message: 'Дані буде стерто.', confirmLabel: 'Скасувати запис' });
        if (!ok) return;
        const sid = st.sessionId;
        if (st.abort) { try { st.abort.abort(); } catch (_) {} st.abort = null; }
        if (st.tick) { clearInterval(st.tick); st.tick = null; }
        try { await R.api.post('/api/recording/' + sid + '/discard'); } catch (_) {}
        UI.toast('Запис скасовано', 'info');
        reset(); renderSetup(ctx);
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
        if (mb < 1024) return mb.toFixed(1) + ' МБ';
        return (mb / 1024).toFixed(2) + ' ГБ';
    }
    function fmtDur(sec) {
        sec = Math.max(0, Math.floor(sec));
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
        if (h > 0) return h + ' год ' + m + ' хв';
        if (m > 0) return m + ' хв';
        return '< 1 хв';
    }
    function recTotalBytes() {
        let total = 0;
        if (st.audioBytes) for (const k in st.audioBytes) total += st.audioBytes[k] || 0;
        if (st.videoTracks) for (const k in st.videoTracks) total += (st.videoTracks[k].bytes || 0);
        return total;
    }
    function updateRecMeta() {
        const el = document.getElementById('rcRecMeta');
        if (!el) return;
        const total = recTotalBytes();
        // середній bitrate з початку запису — стабільніший за миттєвий (аудіо тікає раз на 5с)
        const rateBps = st.elapsedSec > 1 ? total / st.elapsedSec : 0;
        const parts = [];
        parts.push('<span><i class="fa-solid fa-database"></i> ' + fmtBytes(total) + '</span>');
        if (rateBps > 0) parts.push('<span>' + (rateBps * 60 / MIB).toFixed(1) + ' МБ/хв</span>');
        if (st.diskFree != null) {
            let txt = '<i class="fa-solid fa-hard-drive"></i> ' + fmtBytes(st.diskFree) + ' вільно';
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
            const row = document.getElementById(map[name]);
            if (!row) continue;
            const failed = !!(st.streamErr && st.streamErr[name]);
            row.classList.toggle('is-failed', failed);
            if (failed) row.title = st.streamErr[name]; else row.removeAttribute('title');
        }
    }

    R.views.record = {
        render,
        destroy() {
            if (st && st.abort) { try { st.abort.abort(); } catch (_) {} }
            if (st && st.tick) clearInterval(st.tick);
            // Хоткей живе на document, тож без явного зняття він пережив би
            // сторінку і ловив Ctrl+Shift+K у решті застосунку.
            if (_liveCmHotkey) {
                document.removeEventListener('keydown', _liveCmHotkey);
                _liveCmHotkey = null;
            }
        }
    };
})();
