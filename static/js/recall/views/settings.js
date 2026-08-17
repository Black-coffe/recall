/* Recall — "Налаштування". System info, Whisper models, and the global
   speaker address book (used by diarization). Read-mostly; speakers support
   add / rename / delete. Advanced speaker ops (stats, merge, timeline) live
   on the dedicated /speakers page (linked from here). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const DEFAULT_MODEL = 'large-v3-turbo';

    async function render(ctx) {
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-sliders"></i> Сервіс</div>
                <h1 class="rc-pagehead__title">Налаштування</h1>
                <p class="rc-pagehead__lede">Система, моделі розпізнавання та адресна книга спікерів для діаризації.</p>
            </div>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:8px">
                    <h2 class="rc-set__h" style="margin:0">Система</h2>
                    <button class="rc-btn rc-btn--sm" id="rcDiagExport" title="Хвости логів (без секретів/транскриптів) + system_info у zip — для ручної відправки при жалобі">
                        <i class="fa-solid fa-file-zipper"></i> Експортувати діагностичний пакет
                    </button>
                </div>
                <div id="rcSetSys">${UI.skeletonList(2)}</div>
            </section>
            <section class="rc-set">
                <h2 class="rc-set__h">AI · Claude</h2>
                <div id="rcSetAiKey">${UI.skeletonList(1)}</div>
            </section>
            <section class="rc-set">
                <h2 class="rc-set__h">Моделі Whisper</h2>
                <div id="rcSetUpd" class="rc-updrow"></div>
                <div id="rcSetModels">${UI.skeletonList(3)}</div>
            </section>
            <section class="rc-set">
                <h2 class="rc-set__h">Ко-пілот дзвінка</h2>
                <div id="rcSetCopilot">${UI.skeletonList(2)}</div>
            </section>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:8px">
                    <h2 class="rc-set__h" style="margin:0">Напрямки</h2>
                    <button class="rc-btn rc-btn--sm" id="rcCatAdd"><i class="fa-solid fa-plus"></i> Новий напрямок</button>
                </div>
                <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 12px;font-size:var(--rc-t-xs)">Напрямки звужують RAG, ко-пілота й пошук під окремі проєкти. Унікальність регістронезалежна (кирилиця теж) — дублі неможливі. Колір і назву можна змінювати; «перенести/обʼєднати» перекидає всі записи напрямку в інший.</p>
                <div id="rcSetCats">${UI.skeletonList(3)}</div>
            </section>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:12px">
                    <h2 class="rc-set__h" style="margin:0">Спікери</h2>
                    <div style="display:flex;gap:8px">
                        <a class="rc-btn rc-btn--sm" href="/speakers"><i class="fa-solid fa-chart-simple"></i> Статистика, обʼєднання, таймлайн</a>
                        <button class="rc-btn rc-btn--sm" id="rcSpkAdd"><i class="fa-solid fa-plus"></i> Додати спікера</button>
                    </div>
                </div>
                <div id="rcSpkBar"></div>
                <div id="rcSetSpeakers">${UI.skeletonList(3)}</div>
            </section>`;

        ctx.mount.querySelector('#rcSpkAdd').addEventListener('click', () => toggleAddSpeaker(ctx));
        ctx.mount.querySelector('#rcCatAdd').addEventListener('click', () => addCategory(ctx));
        ctx.mount.querySelector('#rcDiagExport').addEventListener('click', () => exportDiagnosticPackage(ctx));
        loadSystem(ctx);
        loadAiKey(ctx);
        loadUpdateStatus(ctx);
        loadModels(ctx);
        loadCopilot(ctx);
        loadCategories(ctx);
        loadSpeakers(ctx);
    }

    // ---- AI / Claude API-ключ (T5.1) ---------------------------------------
    // Онбординг-стіна: раніше єдиний спосіб задати ANTHROPIC_API_KEY був
    // ручним редагуванням .env, про існування якого користувачу ніхто не
    // казав. Тепер — masked-інпут + Зберегти + Перевірити зʼєднання (лёгкий
    // Haiku-виклик) + статус-крапка. Ключ ніколи не запитується назад із
    // бекенду (лише {configured: bool}) і не логується на фронті.
    async function loadAiKey(ctx) {
        const box = ctx.mount.querySelector('#rcSetAiKey');
        if (!box) return;
        let s;
        try { s = await R.api.get('/api/settings/anthropic-key/status'); }
        catch (err) { box.innerHTML = UI.error(err && err.message); return; }
        if (!ctx.isCurrent()) return;
        renderAiKey(ctx, box, !!s.configured);
    }

    function renderAiKey(ctx, box, configured) {
        const dot = (ok) => `<i class="fa-solid fa-circle" style="font-size:8px;color:${ok ? 'var(--rc-ok)' : 'var(--rc-err)'}"></i>`;
        box.innerHTML = `
            <div class="rc-prov" style="margin-bottom:12px">
                <div class="rc-prov__cell"><div class="rc-prov__k">Ключ</div>
                    <div class="rc-prov__v" id="rcAiKeyStatus">${dot(configured)} ${configured ? 'Налаштовано' : 'Не налаштовано'}</div></div>
            </div>
            <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 10px;font-size:var(--rc-t-xs)">
                Потрібен для покращення тексту, резюме, перекладу, RAG-чату та інших Claude-фіч.
                Отримати ключ: <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com</a> → API Keys.
            </p>
            <div class="rc-newcat">
                <input class="rc-input rc-mono" id="rcAiKeyInput" type="password" autocomplete="off" maxlength="300"
                       placeholder="${configured ? '•••••••••• (уведіть новий, щоб замінити)' : 'sk-ant-...'}" style="max-width:420px">
                <button class="rc-iconbtn" id="rcAiKeyToggle" type="button" title="Показати/сховати ключ"><i class="fa-solid fa-eye"></i></button>
                <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcAiKeySave"><i class="fa-solid fa-floppy-disk"></i> Зберегти</button>
                <button class="rc-btn rc-btn--sm" id="rcAiKeyTest"${configured ? '' : ' disabled'}><i class="fa-solid fa-plug"></i> Перевірити з'єднання</button>
                ${configured ? `<button class="rc-btn rc-btn--sm" id="rcAiKeyClear" title="Прибрати ключ"><i class="fa-solid fa-trash"></i></button>` : ''}
            </div>
            <div class="rc-mono" id="rcAiKeyResult" style="margin-top:8px;font-size:var(--rc-t-xs)"></div>`;

        const input = box.querySelector('#rcAiKeyInput');
        const result = box.querySelector('#rcAiKeyResult');
        box.querySelector('#rcAiKeyToggle').addEventListener('click', () => {
            input.type = input.type === 'password' ? 'text' : 'password';
        });
        box.querySelector('#rcAiKeySave').addEventListener('click', async () => {
            const key = input.value.trim();
            if (!key) { UI.toast('Уведіть ключ', 'error'); return; }
            const btn = box.querySelector('#rcAiKeySave');
            btn.disabled = true;
            try {
                await R.api.post('/api/settings/anthropic-key', { api_key: key });
                UI.toast('Ключ збережено', 'success');
                loadAiKey(ctx);
            } catch (e) { UI.toast(e.message || 'Не вдалося зберегти', 'error'); }
            finally { btn.disabled = false; }
        });
        const testBtn = box.querySelector('#rcAiKeyTest');
        if (testBtn) testBtn.addEventListener('click', async () => {
            testBtn.disabled = true; const old = testBtn.innerHTML;
            testBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Перевіряю…';
            result.textContent = '';
            try {
                const r = await R.api.post('/api/settings/anthropic-key/test', {});
                if (r.ok) { result.innerHTML = `${dot(true)} З'єднання успішне`; UI.toast("З'єднання з Claude API успішне", 'success'); }
                else { result.innerHTML = `${dot(false)} ${U.esc(r.error || 'Не вдалося з’єднатись')}`; UI.toast(r.error || "З'єднання не вдалося", 'error'); }
            } catch (e) { result.innerHTML = `${dot(false)} ${U.esc(e.message || 'Помилка')}`; UI.toast(e.message || 'Помилка перевірки', 'error'); }
            finally { testBtn.disabled = false; testBtn.innerHTML = old; }
        });
        const clearBtn = box.querySelector('#rcAiKeyClear');
        if (clearBtn) clearBtn.addEventListener('click', async () => {
            const ok = await UI.confirmModal({
                title: 'Прибрати ключ?',
                message: 'Claude-фічі (покращення тексту, резюме, RAG-чат) стануть недоступні, доки ви не додасте ключ знову.',
                confirmLabel: 'Прибрати',
            });
            if (!ok) return;
            try { await R.api.del('/api/settings/anthropic-key'); UI.toast('Ключ прибрано', 'info'); loadAiKey(ctx); }
            catch (e) { UI.toast(e.message || 'Не вдалося', 'error'); }
        });
    }

    // ---- напрямки: керування (CRUD + перенесення/обʼєднання) ----------------
    let _cats = [];
    async function loadCategories(ctx) {
        const box = ctx.mount.querySelector('#rcSetCats');
        if (!box) return;
        try {
            const d = await R.api.categories();
            if (!ctx.isCurrent()) return;
            _cats = d.categories || [];
            if (!_cats.length) { box.innerHTML = UI.empty('Напрямків ще немає', 'Створіть перший — наприклад «Фонд» чи «Проєкт Альфа».', 'fa-folder-tree'); return; }
            box.innerHTML = `<div class="rc-list">` + _cats.map(catRowHTML).join('')
                + (d.uncategorized ? `<div class="rc-mono" style="color:var(--rc-ink-3);margin-top:10px;font-size:var(--rc-t-xs)">Без напрямку: ${d.uncategorized} зап.</div>` : '')
                + `</div>`;
            bindCategories(ctx, box);
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    function catRowHTML(c) {
        const color = c.color || '#6c757d';
        return `<div class="rc-catrow" data-id="${c.id}">
            <input type="color" class="rc-catrow__color" value="${U.esc(color)}" title="Колір напрямку">
            <input class="rc-input rc-catrow__name" value="${U.esc(c.name)}" maxlength="60" aria-label="Назва напрямку">
            <span class="rc-catrow__count rc-mono">${c.count || 0} зап.</span>
            <div class="rc-catrow__actions">
                <button class="rc-iconbtn" data-act="merge" title="Перенести / обʼєднати в інший напрямок"><i class="fa-solid fa-code-merge"></i></button>
                <button class="rc-iconbtn" data-act="delete" title="Видалити напрямок"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>`;
    }

    function bindCategories(ctx, box) {
        box.querySelectorAll('.rc-catrow').forEach(row => {
            const id = Number(row.dataset.id);
            const c = _cats.find(x => x.id === id) || {};
            const nameEl = row.querySelector('.rc-catrow__name');
            const colorEl = row.querySelector('.rc-catrow__color');

            const rename = async () => {
                const next = nameEl.value.trim();
                if (!next || next === c.name) { nameEl.value = c.name; return; }
                try {
                    await R.api.categoryUpdate(id, { name: next });
                    c.name = next; await UI.bustCategories();
                    UI.toast('Напрямок перейменовано', 'success');
                } catch (e) { UI.toast(e.message || 'Не вдалося', 'error'); nameEl.value = c.name; }
            };
            nameEl.addEventListener('blur', rename);
            nameEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); } else if (e.key === 'Escape') { nameEl.value = c.name; nameEl.blur(); } });
            colorEl.addEventListener('change', async () => {
                try { await R.api.categoryUpdate(id, { color: colorEl.value }); c.color = colorEl.value; await UI.bustCategories(); UI.toast('Колір змінено', 'success'); }
                catch (e) { UI.toast(e.message || 'Не вдалося', 'error'); }
            });
            row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
                const ok = await UI.confirmModal({
                    title: 'Видалити напрямок?',
                    message: `Видалити напрямок «${c.name}»? ${c.count ? c.count + ' записів' : 'Записи'} стануть «без напрямку» (самі записи НЕ видаляються).`,
                    confirmLabel: 'Видалити',
                });
                if (!ok) return;
                try { await R.api.categoryDelete(id); await UI.bustCategories(); UI.toast('Напрямок видалено', 'success'); loadCategories(ctx); }
                catch (e) { UI.toast(e.message || 'Не вдалося', 'error'); }
            });
            row.querySelector('[data-act="merge"]').addEventListener('click', () => mergeCategory(ctx, c));
        });
    }

    async function addCategory(ctx) {
        const cat = await UI.createCategoryModal();
        if (cat) { UI.toast(`Напрямок «${cat.name}» створено`, 'success'); loadCategories(ctx); }
    }

    // Перенести/обʼєднати напрямок у інший (з опцією видалити джерело).
    function mergeCategory(ctx, src) {
        const others = _cats.filter(x => x.id !== src.id);
        if (!others.length) { UI.toast('Немає іншого напрямку для перенесення', 'info'); return; }
        const ov = U.el('div', { class: 'rc-modal-ov' });
        const opts = `<option value="">— без напрямку —</option>`
            + others.map(o => `<option value="${o.id}">${U.esc(o.name)}</option>`).join('');
        ov.innerHTML = `
            <div class="rc-modal" role="dialog" aria-modal="true" aria-label="Перенести напрямок">
                <div class="rc-modal__h"><i class="fa-solid fa-code-merge"></i> Перенести «${U.esc(src.name)}»</div>
                <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 10px;font-size:var(--rc-t-xs)">Усі ${src.count || 0} записів напрямку «${U.esc(src.name)}» перейдуть у вибраний напрямок.</p>
                <label class="rc-field__label" for="rcMergeTarget">Перенести записи в</label>
                <select class="rc-select" id="rcMergeTarget">${opts}</select>
                <label class="rc-check" style="margin-top:12px"><input type="checkbox" id="rcMergeDel" checked> <span>Видалити напрямок «${U.esc(src.name)}» після перенесення (обʼєднати)</span></label>
                <div class="rc-modal__err" id="rcMergeErr"></div>
                <div class="rc-modal__actions">
                    <button class="rc-btn rc-btn--sm" id="rcMergeCancel">Скасувати</button>
                    <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcMergeGo">Перенести</button>
                </div>
            </div>`;
        document.body.appendChild(ov);
        const close = () => ov.remove();
        ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
        ov.querySelector('#rcMergeCancel').addEventListener('click', close);
        ov.querySelector('#rcMergeGo').addEventListener('click', async () => {
            const tv = ov.querySelector('#rcMergeTarget').value;
            const target_id = tv ? Number(tv) : null;
            const del = ov.querySelector('#rcMergeDel').checked;
            const go = ov.querySelector('#rcMergeGo'); go.disabled = true;
            try {
                const r = await R.api.categoryMerge(src.id, target_id, del);
                await UI.bustCategories(); close();
                UI.toast(`Перенесено записів: ${r.moved}${del ? ' · напрямок обʼєднано' : ''}`, 'success');
                loadCategories(ctx);
            } catch (e) {
                const errEl = ov.querySelector('#rcMergeErr'); errEl.textContent = e.message || 'Не вдалося';
                go.disabled = false;
            }
        });
    }

    // ---- co-pilot status (Phase 19, Крок 9) --------------------------------
    const CP_MODE = { light: 'лайт', medium: 'середній', hard: 'жорсткий' };
    const CP_IMP = { low: 'низька', medium: 'середня', high: 'висока' };
    async function loadCopilot(ctx) {
        const box = ctx.mount.querySelector('#rcSetCopilot');
        if (!box) return;
        let s;
        try { s = await R.api.get('/api/copilot/availability'); }
        catch (err) { box.innerHTML = UI.error(err && err.message); return; }
        if (!ctx.isCurrent()) return;
        const dot = (ok) => `<i class="fa-solid fa-circle" style="font-size:8px;color:${ok ? 'var(--rc-ok)' : 'var(--rc-err)'}"></i>`;
        const d = s.defaults || {};
        const fb = s.feedback;
        const rows = [
            ['Стан', `<span id="rcCpStateTxt">${dot(s.enabled)} ${s.enabled ? 'Увімкнено' : 'Вимкнено'}</span>
                <button class="rc-btn rc-btn--sm" id="rcCpToggleBtn" type="button" style="margin-left:10px" data-next="${s.enabled ? '0' : '1'}">${s.enabled ? 'Вимкнути' : 'Увімкнути'}</button>
                <span class="rc-mono" id="rcCpToggleHint" style="color:var(--rc-ink-3);margin-left:8px;font-size:var(--rc-t-xs)"></span>`],
            ['Локальна модель', s.local_llm_available
                ? `${dot(true)} <span class="rc-mono">${U.esc(s.local_llm_model || '')}</span>`
                : `${dot(false)} <span class="rc-mono" style="color:var(--rc-ink-3)">${U.esc(s.local_llm_reason || 'недоступна')}</span>`],
            ['Claude API', s.api_available
                ? `${dot(true)} доступний (верифікація)`
                : `${dot(false)} <span class="rc-mono" style="color:var(--rc-ink-3)">ключ не налаштовано — лише локальний шар</span>`],
            ['Дефолти', `режим <b>${CP_MODE[d.mode] || '—'}</b> · важливість <b>${CP_IMP[d.importance] || '—'}</b> · бюджет <b>$${Number(d.budget_usd || 0).toFixed(2)}</b>${d.model_api ? ` · верифікатор <span class="rc-mono">${U.esc(d.model_api)}</span>` : ''}`],
        ];
        if (fb) rows.push(['Фідбек оператора',
            `<span class="rc-mono">👍 ${fb.thumbs_up || 0} · 👎 ${fb.thumbs_down || 0} · 📌 ${fb.pin || 0} · ✕ ${fb.dismiss || 0} · сесій ${fb.sessions || 0}</span>`]);
        box.className = 'rc-prov';
        box.innerHTML = rows.map(([k, v]) =>
            `<div class="rc-prov__cell"><div class="rc-prov__k">${U.esc(k)}</div><div class="rc-prov__v">${v}</div></div>`).join('');
        const toggleBtn = box.querySelector('#rcCpToggleBtn');
        if (toggleBtn) toggleBtn.addEventListener('click', () => onCopilotToggle(ctx, toggleBtn));
    }

    // Тумблер замість developer-інструкції «додайте COPILOT_ENABLED=1 у .env
    // і перезапустіть» (T5.2). copilot_service створюється ОДИН раз при
    // старті процесу (app.py) з cfg.COPILOT_ENABLED — рантайм-шляху
    // ретроактивно піднятий/погасити його нема, тому чесно повідомляємо
    // restart_required замість того, щоб брехати, що ввімкнулось одразу.
    async function onCopilotToggle(ctx, btn) {
        const next = btn.dataset.next === '1';
        btn.disabled = true;
        try {
            const r = await R.api.post('/api/settings/copilot/toggle', { enabled: next });
            const hint = document.getElementById('rcCpToggleHint');
            if (r.restart_required) {
                if (hint) hint.textContent = 'Записано. Потрібен перезапуск сервера, щоб застосувати.';
                UI.toast('Налаштування збережено — перезапустіть сервер, щоб застосувати', 'info');
            } else {
                if (hint) hint.textContent = 'Застосовано.';
                UI.toast('Готово', 'success');
            }
            btn.dataset.next = next ? '0' : '1';
            btn.textContent = next ? 'Вимкнути' : 'Увімкнути';
        } catch (e) { UI.toast(e.message || 'Не вдалося', 'error'); }
        finally { btn.disabled = false; }
    }

    function vsafe(v) { return U.esc(String(v == null ? '—' : v)); }
    async function loadUpdateStatus(ctx, force) {
        const box = ctx.mount.querySelector('#rcSetUpd');
        if (!box) return;
        box.innerHTML = `<span class="rc-mono" style="color:var(--rc-ink-3)"><i class="fa-solid fa-spinner fa-spin"></i> faster-whisper…</span>`;
        try {
            const s = force
                ? await R.api.post('/api/models/check-updates', {})
                : await R.api.get('/api/models/update-status');
            if (!ctx.isCurrent()) return;
            const upd = s.update_available;
            box.innerHTML = `
                <span class="rc-mono" style="color:${upd ? 'var(--rc-accent-deep)' : 'var(--rc-ink-3)'}">
                    ${upd ? '<i class="fa-solid fa-circle-arrow-up"></i> ' : '<i class="fa-solid fa-circle-check"></i> '}
                    faster-whisper ${vsafe(s.installed_version)}${upd ? ` → доступна ${vsafe(s.latest_version)}` : ' (актуальна)'}
                </span>
                ${upd ? `<code class="rc-mono">${vsafe(s.upgrade_command)}</code>` : ''}
                <button class="rc-btn rc-btn--sm" id="rcUpdCheck"><i class="fa-solid fa-rotate"></i> Перевірити</button>
                ${s.last_checked ? `<span class="rc-mono" style="color:var(--rc-ink-3);font-size:var(--rc-t-xs)">останній чек: ${vsafe(s.last_checked)}</span>` : ''}`;
            const btn = box.querySelector('#rcUpdCheck');
            if (btn) btn.addEventListener('click', () => { btn.disabled = true; loadUpdateStatus(ctx, true); });
        } catch (_) { box.innerHTML = ''; }
    }

    // ---- system info -------------------------------------------------------
    async function loadSystem(ctx) {
        const box = ctx.mount.querySelector('#rcSetSys');
        try {
            const s = await R.api.get('/api/system_info');
            if (!ctx.isCurrent()) return;
            const rows = [
                ['Бекенд', s.backend],
                ['Пристрій', (s.device || '').toUpperCase()],
                ['GPU', s.cuda_device ? `${s.cuda_device}${s.cuda_memory ? ' · ' + s.cuda_memory : ''}` : '—'],
                ['CUDA', s.cuda_version || '—'],
                ['PyTorch', s.pytorch_version || '—'],
                ['CPU потоки', s.cpu_threads],
                ['Памʼять', s.system_memory ? `${s.system_memory}${s.memory_percent ? ' · зайнято ' + s.memory_percent : ''}` : '—'],
                ['Завантажені моделі', (s.loaded_models && s.loaded_models.length) ? s.loaded_models.join(', ') : '—'],
            ];
            box.className = 'rc-prov';
            box.innerHTML = rows.map(([k, v]) =>
                `<div class="rc-prov__cell"><div class="rc-prov__k">${U.esc(k)}</div><div class="rc-prov__v">${U.esc(v == null ? '—' : String(v))}</div></div>`).join('');
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    // ---- діагностичний пакет (T8.1) -----------------------------------------
    // Хвости файлових логів трьох процесів (redacted на бекенді) + system_info +
    // версія/коміт у zip — щоб «пришлите лог» стало однією кнопкою. Без
    // транскриптів/БД/.env (гарантія бекенда, не фронта).
    async function exportDiagnosticPackage(ctx) {
        const btn = ctx.mount.querySelector('#rcDiagExport');
        if (!btn) return;
        const old = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Збираю пакет…';
        try {
            const res = await fetch('/api/system/diagnostic-package');
            if (!res.ok) { let m = 'HTTP ' + res.status; try { const j = await res.json(); if (j.error) m = j.error; } catch (_) {} throw new Error(m); }
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `recall-diagnostic-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.zip`;
            document.body.appendChild(a); a.click();
            setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
            UI.toast('Діагностичний пакет завантажено', 'success');
        } catch (err) { UI.toast(err && err.message || 'Не вдалося зібрати діагностичний пакет', 'error'); }
        finally { btn.disabled = false; btn.innerHTML = old; }
    }

    // ---- whisper models ----------------------------------------------------
    async function loadModels(ctx) {
        const box = ctx.mount.querySelector('#rcSetModels');
        try {
            const data = await R.api.models();
            if (!ctx.isCurrent()) return;
            const list = Array.isArray(data) ? data : (data.models || []);
            box.innerHTML = `<div class="rc-list">` + list.map(m => {
                const inf = m.info || {};
                const meta = [inf.size, inf.params && (inf.params + ' пар.'), inf.speed].filter(Boolean).join(' · ');
                const badge = m.downloaded
                    ? `<span class="rc-srcbadge" style="color:var(--rc-ok)"><i class="fa-solid fa-circle-check"></i> завантажено</span>`
                    : `<button class="rc-btn rc-btn--sm" data-dl="${U.esc(m.name)}"><i class="fa-solid fa-download"></i> Завантажити</button>`;
                const tags = [
                    inf.recommended ? '<span class="rc-mono" style="color:var(--rc-accent-deep)">★ рекомендована</span>' : '',
                    m.name === DEFAULT_MODEL ? '<span class="rc-mono" style="color:var(--rc-ink-3)">· типова</span>' : '',
                    inf.multilingual === false ? '<span class="rc-mono" style="color:var(--rc-ink-3)">· тільки англ.</span>' : '',
                ].filter(Boolean).join(' ');
                return `<div class="rc-model">
                    <div><div class="rc-model__name">${U.esc(m.name)} ${tags}</div>
                    <div class="rc-model__meta rc-mono">${U.esc(meta)}</div></div>
                    <div>${badge}</div>
                </div>`;
            }).join('') + `</div>`;
            box.querySelectorAll('[data-dl]').forEach(b => b.addEventListener('click', () => downloadModel(ctx, b)));
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    async function downloadModel(ctx, btn) {
        const name = btn.dataset.dl;
        btn.disabled = true;
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Завантаження…`;
        try {
            await R.api.post('/api/download_model', { model_name: name });
            UI.toast(`Модель ${name} завантажено`, 'success');
            loadModels(ctx);
        } catch (err) {
            UI.toast(err && err.message || 'Помилка завантаження', 'error');
            btn.disabled = false; btn.innerHTML = `<i class="fa-solid fa-download"></i> Завантажити`;
        }
    }

    // ---- speakers ----------------------------------------------------------
    async function loadSpeakers(ctx) {
        const box = ctx.mount.querySelector('#rcSetSpeakers');
        try {
            const data = await R.api.get('/api/speakers');
            if (!ctx.isCurrent()) return;
            const sp = data.speakers || [];
            if (!sp.length) { box.innerHTML = UI.empty('Спікерів ще немає', 'Зʼявляться після діаризації або додайте вручну.', 'fa-users'); return; }
            box.innerHTML = `<div class="rc-list">` + sp.map(speakerHTML).join('') + `</div>`;
            bindSpeakers(ctx, box);
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    function speakerHTML(s) {
        const color = s.color || 'var(--rc-ink-3)';
        return `<div class="rc-speaker" data-id="${s.id}">
            <span class="rc-speaker__dot" style="background:${U.esc(color)}"></span>
            <span class="rc-speaker__name">${U.esc(s.name)}</span>
            ${s.is_self ? `<span class="rc-tgrow__badge">Ви</span>` : ''}
            <span class="rc-speaker__use rc-mono">${s.usage_count || 0}×</span>
            <div class="rc-speaker__actions">
                <button class="rc-iconbtn" data-act="rename" title="Перейменувати"><i class="fa-solid fa-pen"></i></button>
                <button class="rc-iconbtn" data-act="delete" title="Видалити"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>`;
    }

    function bindSpeakers(ctx, box) {
        box.querySelectorAll('.rc-speaker').forEach(row => {
            const id = Number(row.dataset.id);
            const nameEl = row.querySelector('.rc-speaker__name');
            row.querySelector('[data-act="rename"]').addEventListener('click', async () => {
                const cur = nameEl.textContent;
                const next = await UI.promptModal({ title: 'Перейменувати спікера', label: 'Нове імʼя спікера', defaultValue: cur, required: true, maxlength: 100 });
                if (next == null || !next || next === cur) return;
                try { await R.api.put('/api/speakers/' + id, { name: next }); UI.toast('Перейменовано', 'success'); loadSpeakers(ctx); }
                catch (e) { UI.toast(e.message, 'error'); }
            });
            row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
                const ok = await UI.confirmModal({
                    title: 'Видалити спікера?',
                    message: `Видалити спікера «${nameEl.textContent}»? Привʼязки в записах скинуться.`,
                    confirmLabel: 'Видалити',
                });
                if (!ok) return;
                try { await R.api.del('/api/speakers/' + id); UI.toast('Спікера видалено', 'success'); loadSpeakers(ctx); }
                catch (e) { UI.toast(e.message, 'error'); }
            });
        });
    }

    function toggleAddSpeaker(ctx) {
        const bar = ctx.mount.querySelector('#rcSpkBar');
        if (!bar) return;
        if (bar.innerHTML) { bar.innerHTML = ''; return; }
        bar.innerHTML = `<div class="rc-newcat">
            <input class="rc-input" id="rcSpkName" placeholder="Імʼя спікера…" autocomplete="off">
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcSpkSave">Додати</button>
            <button class="rc-btn rc-btn--sm" id="rcSpkCancel">Скасувати</button>
        </div>`;
        const input = bar.querySelector('#rcSpkName');
        input.focus();
        const close = () => { bar.innerHTML = ''; };
        const save = async () => {
            const name = input.value.trim();
            if (!name) return;
            try { await R.api.post('/api/speakers', { name }); close(); loadSpeakers(ctx); UI.toast(`Спікера «${name}» додано`, 'success'); }
            catch (e) { UI.toast(e.message || 'Не вдалося додати', 'error'); }
        };
        bar.querySelector('#rcSpkSave').addEventListener('click', save);
        bar.querySelector('#rcSpkCancel').addEventListener('click', close);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } else if (e.key === 'Escape') close(); });
    }

    R.views.settings = { render, destroy() {} };
})();
