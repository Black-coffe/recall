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
                <div class="rc-eyebrow"><i class="fa-solid fa-sliders"></i> ${R.t('settings.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('settings.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('settings.lede')}</p>
            </div>
            <section class="rc-set">
                <h2 class="rc-set__h">${R.t('settings.lang.title')}</h2>
                <div id="rcSetLang" class="rc-newcat"></div>
            </section>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:8px">
                    <h2 class="rc-set__h" style="margin:0">${R.t('settings.sys.title')}</h2>
                    <button class="rc-btn rc-btn--sm" id="rcDiagExport" title="${R.t('settings.diag.hint')}">
                        <i class="fa-solid fa-file-zipper"></i> ${R.t('settings.diag.export')}
                    </button>
                </div>
                <div id="rcSetSys">${UI.skeletonList(2)}</div>
            </section>
            <section class="rc-set">
                <h2 class="rc-set__h">AI · Claude</h2>
                <div id="rcSetAiKey">${UI.skeletonList(1)}</div>
            </section>
            <section class="rc-set">
                <h2 class="rc-set__h">${R.t('settings.models.title')}</h2>
                <div id="rcSetUpd" class="rc-updrow"></div>
                <div id="rcSetModels">${UI.skeletonList(3)}</div>
            </section>
            <section class="rc-set">
                <h2 class="rc-set__h">${R.t('settings.cp.title')}</h2>
                <div id="rcSetCopilot">${UI.skeletonList(2)}</div>
            </section>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:8px">
                    <h2 class="rc-set__h" style="margin:0">${R.t('settings.cats.title')}</h2>
                    <button class="rc-btn rc-btn--sm" id="rcCatAdd"><i class="fa-solid fa-plus"></i> ${R.t('settings.cats.new')}</button>
                </div>
                <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 4px;font-size:var(--rc-t-xs)">${R.t('settings.cats.lede')}</p>
                ${UI.hint('<p>' + R.t('settings.cats.hint') + '</p>').outerHTML}
                <div id="rcSetCats">${UI.skeletonList(3)}</div>
            </section>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:8px">
                    <h2 class="rc-set__h" style="margin:0">${R.t('settings.tags.title')}</h2>
                    <button class="rc-btn rc-btn--sm" id="rcTagAdd"><i class="fa-solid fa-plus"></i> ${R.t('settings.tags.new')}</button>
                </div>
                <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 12px;font-size:var(--rc-t-xs)">${R.t('settings.tags.lede')}</p>
                <div id="rcSetTags">${UI.skeletonList(2)}</div>
            </section>
            <section class="rc-set">
                <div class="rc-pagehead__row" style="margin-bottom:12px">
                    <h2 class="rc-set__h" style="margin:0">${R.t('settings.spk.title')}</h2>
                    <div style="display:flex;gap:8px">
                        <a class="rc-btn rc-btn--sm" href="/speakers"><i class="fa-solid fa-chart-simple"></i> ${R.t('settings.spk.statsLink')}</a>
                        <button class="rc-btn rc-btn--sm" id="rcSpkAdd"><i class="fa-solid fa-plus"></i> ${R.t('settings.spk.add')}</button>
                    </div>
                </div>
                <div id="rcSpkBar"></div>
                <div id="rcSetSpeakers">${UI.skeletonList(3)}</div>
            </section>`;

        ctx.mount.querySelector('#rcSpkAdd').addEventListener('click', () => toggleAddSpeaker(ctx));
        ctx.mount.querySelector('#rcCatAdd').addEventListener('click', () => addCategory(ctx));
        ctx.mount.querySelector('#rcTagAdd').addEventListener('click', () => addTag(ctx));
        ctx.mount.querySelector('#rcDiagExport').addEventListener('click', () => exportDiagnosticPackage(ctx));
        loadLang(ctx);
        loadSystem(ctx);
        loadAiKey(ctx);
        loadUpdateStatus(ctx);
        loadModels(ctx);
        loadCopilot(ctx);
        loadCategories(ctx);
        loadTags(ctx);
        loadSpeakers(ctx);
    }

    // Extract a toast message from an API error: 409 has_projects carries
    // a short code in .message (err.body.error === 'has_projects'), and
    // the human text with the list of projects is in err.body.message; the other codes
    // (two_levels/self_parent/bad_parent/duplicate) are already human in .message.
    function apiErrMsg(e, fallback) {
        return (e && e.body && e.body.message) || (e && e.message) || fallback || R.t('settings.err.generic');
    }

    // ---- interface language ------------------------------------------------------
    // GET/POST /api/settings/ui-lang: auto|uk|en in .env; after saving — reload,
    // so the shell gets the new data-ui-lang. «Українська»/«English» — not translated.
    async function loadLang(ctx) {
        const box = ctx.mount.querySelector('#rcSetLang');
        if (!box) return;
        let cur = 'auto';
        try {
            const r = await R.api.get('/api/settings/ui-lang');
            if (r && r.value) cur = r.value;
        } catch (err) { box.innerHTML = UI.error(err && err.message); return; }
        if (!ctx.isCurrent()) return;
        const detected = R.i18n.lang === 'uk' ? 'Українська' : 'English';
        const opts = [
            ['auto', R.t('settings.lang.auto', { lang: detected })],
            ['uk', 'Українська'],
            ['en', 'English'],
        ];
        box.innerHTML = `<select class="rc-select" id="rcLangSel" aria-label="${R.t('settings.lang.title')}" style="max-width:320px">`
            + opts.map(([v, label]) => `<option value="${v}"${v === cur ? ' selected' : ''}>${U.esc(label)}</option>`).join('')
            + `</select>`;
        const sel = box.querySelector('#rcLangSel');
        sel.addEventListener('change', async () => {
            const next = sel.value;
            sel.disabled = true;
            try {
                await R.api.post('/api/settings/ui-lang', { value: next });
                location.reload();
            } catch (e) {
                UI.toast(apiErrMsg(e), 'error');
                sel.value = cur; sel.disabled = false;
            }
        });
    }

    // ---- AI / Claude API key (T5.1) ---------------------------------------
    // Onboarding wall: previously the only way to set ANTHROPIC_API_KEY was
    // manual editing of .env, whose existence nobody told the user
    // about. Now — a masked input + Save + Check connection (a light
    // Haiku call) + a status dot. The key is never requested back from the
    // backend (only {configured: bool}) and is not logged on the front.
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
                <div class="rc-prov__cell"><div class="rc-prov__k">${R.t('settings.ai.key')}</div>
                    <div class="rc-prov__v" id="rcAiKeyStatus">${dot(configured)} ${configured ? R.t('settings.ai.on') : R.t('settings.ai.off')}</div></div>
            </div>
            <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 10px;font-size:var(--rc-t-xs)">
                ${R.t('settings.ai.lede')}
                ${R.t('settings.ai.getKey')} <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com</a> → API Keys.
            </p>
            <div class="rc-newcat">
                <input class="rc-input rc-mono" id="rcAiKeyInput" type="password" autocomplete="off" maxlength="300"
                       placeholder="${configured ? R.t('settings.ai.ph') : 'sk-ant-...'}" style="max-width:420px">
                <button class="rc-iconbtn" id="rcAiKeyToggle" type="button" title="${R.t('settings.ai.toggle')}"><i class="fa-solid fa-eye"></i></button>
                <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcAiKeySave"><i class="fa-solid fa-floppy-disk"></i> ${R.t('settings.save')}</button>
                <button class="rc-btn rc-btn--sm" id="rcAiKeyTest"${configured ? '' : ' disabled'}><i class="fa-solid fa-plug"></i> ${R.t('settings.ai.test')}</button>
                ${configured ? `<button class="rc-btn rc-btn--sm" id="rcAiKeyClear" title="${R.t('settings.ai.clearTitle')}"><i class="fa-solid fa-trash"></i></button>` : ''}
            </div>
            <div class="rc-mono" id="rcAiKeyResult" style="margin-top:8px;font-size:var(--rc-t-xs)"></div>`;

        const input = box.querySelector('#rcAiKeyInput');
        const result = box.querySelector('#rcAiKeyResult');
        box.querySelector('#rcAiKeyToggle').addEventListener('click', () => {
            input.type = input.type === 'password' ? 'text' : 'password';
        });
        box.querySelector('#rcAiKeySave').addEventListener('click', async () => {
            const key = input.value.trim();
            if (!key) { UI.toast(R.t('settings.ai.enter'), 'error'); return; }
            const btn = box.querySelector('#rcAiKeySave');
            btn.disabled = true;
            try {
                await R.api.post('/api/settings/anthropic-key', { api_key: key });
                UI.toast(R.t('settings.ai.saved'), 'success');
                loadAiKey(ctx);
            } catch (e) { UI.toast(e.message || R.t('settings.ai.saveFail'), 'error'); }
            finally { btn.disabled = false; }
        });
        const testBtn = box.querySelector('#rcAiKeyTest');
        if (testBtn) testBtn.addEventListener('click', async () => {
            testBtn.disabled = true; const old = testBtn.innerHTML;
            testBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + R.t('settings.ai.testing');
            result.textContent = '';
            try {
                const r = await R.api.post('/api/settings/anthropic-key/test', {});
                if (r.ok) { result.innerHTML = `${dot(true)} ${R.t('settings.ai.ok')}`; UI.toast(R.t('settings.ai.okToast'), 'success'); }
                else { result.innerHTML = `${dot(false)} ${U.esc(r.error || R.t('settings.ai.connFail'))}`; UI.toast(r.error || R.t('settings.ai.connFailToast'), 'error'); }
            } catch (e) { result.innerHTML = `${dot(false)} ${U.esc(e.message || R.t('settings.err.error'))}`; UI.toast(e.message || R.t('settings.ai.checkErr'), 'error'); }
            finally { testBtn.disabled = false; testBtn.innerHTML = old; }
        });
        const clearBtn = box.querySelector('#rcAiKeyClear');
        if (clearBtn) clearBtn.addEventListener('click', async () => {
            const ok = await UI.confirmModal({
                title: R.t('settings.ai.clearQ'),
                message: R.t('settings.ai.clearMsg'),
                confirmLabel: R.t('settings.ai.clearBtn'),
            });
            if (!ok) return;
            try { await R.api.del('/api/settings/anthropic-key'); UI.toast(R.t('settings.ai.cleared'), 'info'); loadAiKey(ctx); }
            catch (e) { UI.toast(e.message || R.t('settings.err.generic'), 'error'); }
        });
    }

    // ---- categories: management (tree, CRUD + reparent + move/merge) ----
    let _cats = [];
    async function loadCategories(ctx) {
        const box = ctx.mount.querySelector('#rcSetCats');
        if (!box) return;
        try {
            const d = await R.api.categories();
            if (!ctx.isCurrent()) return;
            _cats = d.categories || [];
            if (!_cats.length) { box.innerHTML = UI.empty(R.t('settings.cats.empty'), R.t('settings.cats.emptyHint'), 'fa-folder-tree'); return; }
            box.innerHTML = `<div class="rc-tree" id="rcCatTree">` + _cats.map(treeNodeHTML).join('')
                + (d.uncategorized ? `<div class="rc-mono" style="color:var(--rc-ink-3);margin-top:10px;font-size:var(--rc-t-xs)">${R.t('settings.cats.none', { n: d.uncategorized })}</div>` : '')
                + `</div>`;
            bindCategories(ctx, box);
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    function treeNodeHTML(c) {
        const color = c.color || '#6c757d';
        const isProject = c.level === 'project';
        const count = isProject ? R.t('settings.cnt', { n: c.items || 0 })
            : R.t('settings.cnt', { n: c.items_total != null ? c.items_total : (c.count || 0) });
        return `<div class="rc-tree__node${isProject ? ' rc-tree__node--project' : ''}" data-id="${c.id}">
            <input type="color" class="rc-catrow__color" value="${U.esc(color)}" title="${R.t('settings.color')}">
            <input class="rc-input rc-catrow__name" value="${U.esc(c.name)}" maxlength="60" aria-label="${R.t('settings.name')}">
            <span class="rc-catrow__count rc-mono">${count}</span>
            <select class="rc-select rc-select--sm" data-reparent="${c.id}" aria-label="${R.t('settings.cats.one')}">${reparentOptions(c)}</select>
            <div class="rc-catrow__actions">
                <button class="rc-iconbtn" data-act="merge" title="${R.t('settings.cats.mergeTitle')}"><i class="fa-solid fa-code-merge"></i></button>
                <button class="rc-iconbtn" data-act="delete" title="${R.t('settings.delete')}"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>`;
    }

    // Options of the reparent select of one node: all OTHER categories + «без напрямку»
    // (= it becomes/stays a category itself). Projects are not offered as targets —
    // there is no third level in the tree (the server would reject it anyway, this is only so as
    // not to show a knowingly wrong option).
    function reparentOptions(c) {
        const cur = c.parent_id == null ? '' : String(c.parent_id);
        const directions = _cats.filter((x) => x.level !== 'project' && x.id !== c.id);
        return `<option value=""${cur === '' ? ' selected' : ''}>${R.t('settings.cats.noneOpt')}</option>`
            + directions.map((d) => `<option value="${d.id}"${String(d.id) === cur ? ' selected' : ''}>${U.esc(d.name)}</option>`).join('');
    }

    function bindCategories(ctx, box) {
        box.querySelectorAll('.rc-tree__node').forEach(row => {
            const id = Number(row.dataset.id);
            const c = _cats.find(x => x.id === id) || {};
            const nameEl = row.querySelector('.rc-catrow__name');
            const colorEl = row.querySelector('.rc-catrow__color');
            const reparentEl = row.querySelector('[data-reparent]');

            const rename = async () => {
                const next = nameEl.value.trim();
                if (!next || next === c.name) { nameEl.value = c.name; return; }
                try {
                    await R.api.updateCategory(id, { name: next });
                    c.name = next; await UI.bustCategories();
                    UI.toast(R.t('settings.cats.renamed'), 'success');
                } catch (e) { UI.toast(apiErrMsg(e), 'error'); nameEl.value = c.name; }
            };
            nameEl.addEventListener('blur', rename);
            nameEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); } else if (e.key === 'Escape') { nameEl.value = c.name; nameEl.blur(); } });
            colorEl.addEventListener('change', async () => {
                try { await R.api.updateCategory(id, { color: colorEl.value }); c.color = colorEl.value; await UI.bustCategories(); UI.toast(R.t('settings.colorChanged'), 'success'); }
                catch (e) { UI.toast(apiErrMsg(e), 'error'); }
            });
            reparentEl.addEventListener('change', async () => {
                const val = reparentEl.value;
                const parent_id = val ? Number(val) : null;
                reparentEl.disabled = true;
                try {
                    await R.api.updateCategory(id, { parent_id });
                    await UI.bustCategories();
                    UI.toast(R.t('settings.cats.moved'), 'success');
                    loadCategories(ctx);
                } catch (e) {
                    UI.toast(apiErrMsg(e), 'error');
                    reparentEl.innerHTML = reparentOptions(c); reparentEl.disabled = false;
                }
            });
            row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
                const ok = await UI.confirmModal({
                    title: R.t('settings.cats.delQ'),
                    message: R.t('settings.cats.delMsg', { name: c.name, what: c.count ? R.t('settings.cats.delN', { n: c.count }) : R.t('settings.cats.delRecs') }),
                    confirmLabel: R.t('settings.delete'),
                });
                if (!ok) return;
                try { await R.api.categoryDelete(id); await UI.bustCategories(); UI.toast(R.t('settings.cats.deleted'), 'success'); loadCategories(ctx); }
                catch (e) { UI.toast(apiErrMsg(e), 'error'); }
            });
            row.querySelector('[data-act="merge"]').addEventListener('click', () => mergeCategory(ctx, c));
        });
    }

    async function addCategory(ctx) {
        const cat = await UI.createCategoryModal();
        if (cat) { UI.toast(R.t('settings.cats.created', { name: cat.name }), 'success'); loadCategories(ctx); }
    }

    // Move/merge a category into another (with the option to delete the source).
    function mergeCategory(ctx, src) {
        const others = _cats.filter(x => x.id !== src.id);
        if (!others.length) { UI.toast(R.t('settings.cats.noOther'), 'info'); return; }
        const ov = U.el('div', { class: 'rc-modal-ov' });
        const opts = `<option value="">${R.t('settings.cats.noneOpt')}</option>`
            + others.map(o => `<option value="${o.id}">${U.esc(o.name)}</option>`).join('');
        ov.innerHTML = `
            <div class="rc-modal" role="dialog" aria-modal="true" aria-label="${R.t('settings.cats.moveAria')}">
                <div class="rc-modal__h"><i class="fa-solid fa-code-merge"></i> ${R.t('settings.cats.moveH', { name: U.esc(src.name) })}</div>
                <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 10px;font-size:var(--rc-t-xs)">${R.t('settings.cats.moveLede', { n: src.count || 0, name: U.esc(src.name) })}</p>
                <label class="rc-field__label" for="rcMergeTarget">${R.t('settings.cats.moveTo')}</label>
                <select class="rc-select" id="rcMergeTarget">${opts}</select>
                <label class="rc-check" style="margin-top:12px"><input type="checkbox" id="rcMergeDel" checked> <span>${R.t('settings.cats.moveDel', { name: U.esc(src.name) })}</span></label>
                <div class="rc-modal__err" id="rcMergeErr"></div>
                <div class="rc-modal__actions">
                    <button class="rc-btn rc-btn--sm" id="rcMergeCancel">${R.t('settings.cancel')}</button>
                    <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcMergeGo">${R.t('settings.cats.moveGo')}</button>
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
                UI.toast(R.t('settings.cats.moved2', { n: r.moved }) + (del ? R.t('settings.cats.moved2Merged') : ''), 'success');
                loadCategories(ctx);
            } catch (e) {
                const errEl = ov.querySelector('#rcMergeErr'); errEl.textContent = apiErrMsg(e);
                go.disabled = false;
            }
        });
    }

    // ---- tags: dictionary (CRUD + merge) -------------------------------------
    let _tags = [];
    async function loadTags(ctx) {
        const box = ctx.mount.querySelector('#rcSetTags');
        if (!box) return;
        try {
            const d = await R.api.getTags();
            if (!ctx.isCurrent()) return;
            _tags = d.tags || [];
            if (!_tags.length) { box.innerHTML = UI.empty(R.t('settings.tags.empty'), R.t('settings.tags.emptyHint'), 'fa-tags'); return; }
            box.innerHTML = `<div class="rc-list">` + _tags.map(tagRowHTML).join('') + `</div>`;
            bindTags(ctx, box);
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    function tagRowHTML(t) {
        const color = t.color || '#6c757d';
        return `<div class="rc-catrow" data-id="${t.id}">
            <input type="color" class="rc-catrow__color" value="${U.esc(color)}" title="${R.t('settings.tags.color')}">
            <input class="rc-input rc-catrow__name" value="${U.esc(t.name)}" maxlength="40" aria-label="${R.t('settings.tags.name')}">
            <span class="rc-catrow__count rc-mono">${R.t('settings.cnt', { n: t.items || 0 })}</span>
            <div class="rc-catrow__actions">
                <button class="rc-iconbtn" data-act="merge" title="${R.t('settings.tags.mergeTitle')}"><i class="fa-solid fa-code-merge"></i></button>
                <button class="rc-iconbtn" data-act="delete" title="${R.t('settings.tags.delTitle')}"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>`;
    }

    function bindTags(ctx, box) {
        box.querySelectorAll('.rc-catrow').forEach(row => {
            const id = Number(row.dataset.id);
            const t = _tags.find(x => x.id === id) || {};
            const nameEl = row.querySelector('.rc-catrow__name');
            const colorEl = row.querySelector('.rc-catrow__color');

            const rename = async () => {
                const next = nameEl.value.trim();
                if (!next || next === t.name) { nameEl.value = t.name; return; }
                try {
                    await R.api.updateTag(id, { name: next });
                    t.name = next; await UI.bustTags();
                    UI.toast(R.t('settings.tags.renamed'), 'success');
                } catch (e) { UI.toast(apiErrMsg(e), 'error'); nameEl.value = t.name; }
            };
            nameEl.addEventListener('blur', rename);
            nameEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); } else if (e.key === 'Escape') { nameEl.value = t.name; nameEl.blur(); } });
            colorEl.addEventListener('change', async () => {
                try { await R.api.updateTag(id, { color: colorEl.value }); t.color = colorEl.value; await UI.bustTags(); UI.toast(R.t('settings.colorChanged'), 'success'); }
                catch (e) { UI.toast(apiErrMsg(e), 'error'); }
            });
            row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
                const ok = await UI.confirmModal({
                    title: R.t('settings.tags.delQ'),
                    message: R.t('settings.tags.delMsg', { name: t.name, n: t.items || 0 }),
                    confirmLabel: R.t('settings.delete'),
                });
                if (!ok) return;
                try { await R.api.deleteTag(id); await UI.bustTags(); UI.toast(R.t('settings.tags.deleted'), 'success'); loadTags(ctx); }
                catch (e) { UI.toast(apiErrMsg(e), 'error'); }
            });
            row.querySelector('[data-act="merge"]').addEventListener('click', () => mergeTag(ctx, t));
        });
    }

    async function addTag(ctx) {
        const name = await UI.promptModal({ title: R.t('settings.tags.new'), label: R.t('settings.tags.name'), required: true, maxlength: 40 });
        if (name == null || !name) return;
        try { await R.api.createTag({ name }); await UI.bustTags(); UI.toast(R.t('settings.tags.created', { name }), 'success'); loadTags(ctx); }
        catch (e) { UI.toast(apiErrMsg(e), 'error'); }
    }

    // Merge a tag with another (all records of src move to keep, src disappears).
    function mergeTag(ctx, src) {
        const others = _tags.filter(x => x.id !== src.id);
        if (!others.length) { UI.toast(R.t('settings.tags.noOther'), 'info'); return; }
        const ov = U.el('div', { class: 'rc-modal-ov' });
        const opts = others.map(o => `<option value="${o.id}">${U.esc(o.name)}</option>`).join('');
        ov.innerHTML = `
            <div class="rc-modal" role="dialog" aria-modal="true" aria-label="${R.t('settings.tags.mergeAria')}">
                <div class="rc-modal__h"><i class="fa-solid fa-code-merge"></i> ${R.t('settings.tags.mergeH', { name: U.esc(src.name) })}</div>
                <p class="rc-mono" style="color:var(--rc-ink-3);margin:0 0 10px;font-size:var(--rc-t-xs)">${R.t('settings.tags.mergeLede', { n: src.items || 0, name: U.esc(src.name) })}</p>
                <label class="rc-field__label" for="rcTagMergeTarget">${R.t('settings.tags.mergeWith')}</label>
                <select class="rc-select" id="rcTagMergeTarget">${opts}</select>
                <div class="rc-modal__err" id="rcTagMergeErr"></div>
                <div class="rc-modal__actions">
                    <button class="rc-btn rc-btn--sm" id="rcTagMergeCancel">${R.t('settings.cancel')}</button>
                    <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcTagMergeGo">${R.t('settings.tags.mergeGo')}</button>
                </div>
            </div>`;
        document.body.appendChild(ov);
        const close = () => ov.remove();
        ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(); });
        ov.querySelector('#rcTagMergeCancel').addEventListener('click', close);
        ov.querySelector('#rcTagMergeGo').addEventListener('click', async () => {
            const keep_id = Number(ov.querySelector('#rcTagMergeTarget').value);
            const go = ov.querySelector('#rcTagMergeGo'); go.disabled = true;
            try {
                await R.api.mergeTags(keep_id, src.id);
                await UI.bustTags(); close();
                UI.toast(R.t('settings.tags.merged'), 'success');
                loadTags(ctx);
            } catch (e) {
                const errEl = ov.querySelector('#rcTagMergeErr'); errEl.textContent = apiErrMsg(e);
                go.disabled = false;
            }
        });
    }

    // ---- co-pilot status (Phase 19, Step 9) --------------------------------
    const cpMode = (m) => ({ light: R.t('settings.cp.mode.light'), medium: R.t('settings.cp.mode.medium'), hard: R.t('settings.cp.mode.hard') })[m];
    const cpImp = (m) => ({ low: R.t('settings.cp.imp.low'), medium: R.t('settings.cp.imp.medium'), high: R.t('settings.cp.imp.high') })[m];
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
            [R.t('settings.cp.state'), `<span id="rcCpStateTxt">${dot(s.enabled)} ${s.enabled ? R.t('settings.cp.enabled') : R.t('settings.cp.disabled')}</span>
                <button class="rc-btn rc-btn--sm" id="rcCpToggleBtn" type="button" style="margin-left:10px" data-next="${s.enabled ? '0' : '1'}">${s.enabled ? R.t('settings.cp.disable') : R.t('settings.cp.enable')}</button>
                <span class="rc-mono" id="rcCpToggleHint" style="color:var(--rc-ink-3);margin-left:8px;font-size:var(--rc-t-xs)"></span>`],
            [R.t('settings.cp.local'), s.local_llm_available
                ? `${dot(true)} <span class="rc-mono">${U.esc(s.local_llm_model || '')}</span>`
                : `${dot(false)} <span class="rc-mono" style="color:var(--rc-ink-3)">${U.esc(s.local_llm_reason || R.t('settings.cp.unavailable'))}</span>`],
            ['Claude API', s.api_available
                ? `${dot(true)} ${R.t('settings.cp.apiOk')}`
                : `${dot(false)} <span class="rc-mono" style="color:var(--rc-ink-3)">${R.t('settings.cp.apiNo')}</span>`],
            [R.t('settings.cp.defaults'), R.t('settings.cp.defaultsVal', { mode: cpMode(d.mode) || '—', imp: cpImp(d.importance) || '—', budget: Number(d.budget_usd || 0).toFixed(2) }) + (d.model_api ? ` · ${R.t('settings.cp.verifier')} <span class="rc-mono">${U.esc(d.model_api)}</span>` : '')],
        ];
        if (fb) rows.push([R.t('settings.cp.feedback'),
            `<span class="rc-mono">👍 ${fb.thumbs_up || 0} · 👎 ${fb.thumbs_down || 0} · 📌 ${fb.pin || 0} · ✕ ${fb.dismiss || 0} · ${R.t('settings.cp.sessions')} ${fb.sessions || 0}</span>`]);
        box.className = 'rc-prov';
        box.innerHTML = rows.map(([k, v]) =>
            `<div class="rc-prov__cell"><div class="rc-prov__k">${U.esc(k)}</div><div class="rc-prov__v">${v}</div></div>`).join('');
        const toggleBtn = box.querySelector('#rcCpToggleBtn');
        if (toggleBtn) toggleBtn.addEventListener('click', () => onCopilotToggle(ctx, toggleBtn));
    }

    // A toggle instead of the developer instruction «додайте COPILOT_ENABLED=1 у .env
    // і перезапустіть» (T5.2). copilot_service is created ONCE at
    // process start (app.py) from cfg.COPILOT_ENABLED — there is no runtime path
    // to retroactively raise/drop it, so we honestly report
    // restart_required instead of lying that it turned on at once.
    async function onCopilotToggle(ctx, btn) {
        const next = btn.dataset.next === '1';
        btn.disabled = true;
        try {
            const r = await R.api.post('/api/settings/copilot/toggle', { enabled: next });
            const hint = document.getElementById('rcCpToggleHint');
            if (r.restart_required) {
                if (hint) hint.textContent = R.t('settings.cp.restartHint');
                UI.toast(R.t('settings.cp.restartToast'), 'info');
            } else {
                if (hint) hint.textContent = R.t('settings.cp.applied');
                UI.toast(R.t('settings.cp.done'), 'success');
            }
            btn.dataset.next = next ? '0' : '1';
            btn.textContent = next ? R.t('settings.cp.disable') : R.t('settings.cp.enable');
        } catch (e) { UI.toast(e.message || R.t('settings.err.generic'), 'error'); }
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
                    faster-whisper ${vsafe(s.installed_version)}${upd ? ` → ${R.t('settings.upd.available')} ${vsafe(s.latest_version)}` : ` (${R.t('settings.upd.current')})`}
                </span>
                ${upd ? `<code class="rc-mono">${vsafe(s.upgrade_command)}</code>` : ''}
                <button class="rc-btn rc-btn--sm" id="rcUpdCheck"><i class="fa-solid fa-rotate"></i> ${R.t('settings.upd.check')}</button>
                ${s.last_checked ? `<span class="rc-mono" style="color:var(--rc-ink-3);font-size:var(--rc-t-xs)">${R.t('settings.upd.last')}: ${vsafe(s.last_checked)}</span>` : ''}`;
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
                [R.t('settings.sys.backend'), s.backend],
                [R.t('settings.sys.device'), (s.device || '').toUpperCase()],
                ['GPU', s.cuda_device ? `${s.cuda_device}${s.cuda_memory ? ' · ' + s.cuda_memory : ''}` : '—'],
                ['CUDA', s.cuda_version || '—'],
                ['PyTorch', s.pytorch_version || '—'],
                [R.t('settings.sys.threads'), s.cpu_threads],
                [R.t('settings.sys.memory'), s.system_memory ? `${s.system_memory}${s.memory_percent ? ' · ' + R.t('settings.sys.used') + ' ' + s.memory_percent : ''}` : '—'],
                [R.t('settings.sys.loaded'), (s.loaded_models && s.loaded_models.length) ? s.loaded_models.join(', ') : '—'],
            ];
            box.className = 'rc-prov';
            box.innerHTML = rows.map(([k, v]) =>
                `<div class="rc-prov__cell"><div class="rc-prov__k">${U.esc(k)}</div><div class="rc-prov__v">${U.esc(v == null ? '—' : String(v))}</div></div>`).join('');
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    // ---- diagnostic package (T8.1) -----------------------------------------
    // Tails of the file logs of three processes (redacted on the backend) + system_info +
    // version/commit in a zip — so that "send the log" becomes one button. Without
    // transcripts/DB/.env (a backend guarantee, not the front's).
    async function exportDiagnosticPackage(ctx) {
        const btn = ctx.mount.querySelector('#rcDiagExport');
        if (!btn) return;
        const old = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + R.t('settings.diag.busy');
        try {
            const res = await fetch('/api/system/diagnostic-package', { headers: R.i18n.headers() });
            if (!res.ok) { let m = 'HTTP ' + res.status; try { const j = await res.json(); if (j.error) m = j.error; } catch (_) {} throw new Error(m); }
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `recall-diagnostic-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.zip`;
            document.body.appendChild(a); a.click();
            setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
            UI.toast(R.t('settings.diag.done'), 'success');
        } catch (err) { UI.toast(err && err.message || R.t('settings.diag.fail'), 'error'); }
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
                const meta = [inf.size, inf.params && (inf.params + ' ' + R.t('settings.models.params')), inf.speed].filter(Boolean).join(' · ');
                const badge = m.downloaded
                    ? `<span class="rc-srcbadge" style="color:var(--rc-ok)"><i class="fa-solid fa-circle-check"></i> ${R.t('settings.models.downloaded')}</span>`
                    : `<button class="rc-btn rc-btn--sm" data-dl="${U.esc(m.name)}"><i class="fa-solid fa-download"></i> ${R.t('settings.models.download')}</button>`;
                const tags = [
                    inf.recommended ? `<span class="rc-mono" style="color:var(--rc-accent-deep)">★ ${R.t('settings.models.recommended')}</span>` : '',
                    m.name === DEFAULT_MODEL ? `<span class="rc-mono" style="color:var(--rc-ink-3)">· ${R.t('settings.models.default')}</span>` : '',
                    inf.multilingual === false ? `<span class="rc-mono" style="color:var(--rc-ink-3)">· ${R.t('settings.models.enOnly')}</span>` : '',
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
        btn.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${R.t('settings.models.downloading')}`;
        try {
            await R.api.post('/api/download_model', { model_name: name });
            UI.toast(R.t('settings.models.done', { name }), 'success');
            loadModels(ctx);
        } catch (err) {
            UI.toast(err && err.message || R.t('settings.models.fail'), 'error');
            btn.disabled = false; btn.innerHTML = `<i class="fa-solid fa-download"></i> ${R.t('settings.models.download')}`;
        }
    }

    // ---- speakers ----------------------------------------------------------
    async function loadSpeakers(ctx) {
        const box = ctx.mount.querySelector('#rcSetSpeakers');
        try {
            const data = await R.api.get('/api/speakers');
            if (!ctx.isCurrent()) return;
            const sp = data.speakers || [];
            if (!sp.length) { box.innerHTML = UI.empty(R.t('settings.spk.empty'), R.t('settings.spk.emptyHint'), 'fa-users'); return; }
            box.innerHTML = `<div class="rc-list">` + sp.map(speakerHTML).join('') + `</div>`;
            bindSpeakers(ctx, box);
        } catch (err) { box.innerHTML = UI.error(err && err.message); }
    }

    function speakerHTML(s) {
        const color = s.color || 'var(--rc-ink-3)';
        return `<div class="rc-speaker" data-id="${s.id}">
            <span class="rc-speaker__dot" style="background:${U.esc(color)}"></span>
            <span class="rc-speaker__name">${U.esc(s.name)}</span>
            ${s.is_self ? `<span class="rc-tgrow__badge">${R.t('settings.spk.you')}</span>` : ''}
            <span class="rc-speaker__use rc-mono">${s.usage_count || 0}×</span>
            <div class="rc-speaker__actions">
                <button class="rc-iconbtn" data-act="rename" title="${R.t('settings.spk.rename')}"><i class="fa-solid fa-pen"></i></button>
                <button class="rc-iconbtn" data-act="delete" title="${R.t('settings.delete')}"><i class="fa-solid fa-trash"></i></button>
            </div>
        </div>`;
    }

    function bindSpeakers(ctx, box) {
        box.querySelectorAll('.rc-speaker').forEach(row => {
            const id = Number(row.dataset.id);
            const nameEl = row.querySelector('.rc-speaker__name');
            row.querySelector('[data-act="rename"]').addEventListener('click', async () => {
                const cur = nameEl.textContent;
                const next = await UI.promptModal({ title: R.t('settings.spk.renameTitle'), label: R.t('settings.spk.newName'), defaultValue: cur, required: true, maxlength: 100 });
                if (next == null || !next || next === cur) return;
                try { await R.api.put('/api/speakers/' + id, { name: next }); UI.toast(R.t('settings.spk.renamed'), 'success'); loadSpeakers(ctx); }
                catch (e) { UI.toast(e.message, 'error'); }
            });
            row.querySelector('[data-act="delete"]').addEventListener('click', async () => {
                const ok = await UI.confirmModal({
                    title: R.t('settings.spk.delQ'),
                    message: R.t('settings.spk.delMsg', { name: nameEl.textContent }),
                    confirmLabel: R.t('settings.delete'),
                });
                if (!ok) return;
                try { await R.api.del('/api/speakers/' + id); UI.toast(R.t('settings.spk.deleted'), 'success'); loadSpeakers(ctx); }
                catch (e) { UI.toast(e.message, 'error'); }
            });
        });
    }

    function toggleAddSpeaker(ctx) {
        const bar = ctx.mount.querySelector('#rcSpkBar');
        if (!bar) return;
        if (bar.innerHTML) { bar.innerHTML = ''; return; }
        bar.innerHTML = `<div class="rc-newcat">
            <input class="rc-input" id="rcSpkName" placeholder="${R.t('settings.spk.namePh')}" autocomplete="off">
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcSpkSave">${R.t('settings.spk.addGo')}</button>
            <button class="rc-btn rc-btn--sm" id="rcSpkCancel">${R.t('settings.cancel')}</button>
        </div>`;
        const input = bar.querySelector('#rcSpkName');
        input.focus();
        const close = () => { bar.innerHTML = ''; };
        const save = async () => {
            const name = input.value.trim();
            if (!name) return;
            try { await R.api.post('/api/speakers', { name }); close(); loadSpeakers(ctx); UI.toast(R.t('settings.spk.added', { name }), 'success'); }
            catch (e) { UI.toast(e.message || R.t('settings.spk.addFail'), 'error'); }
        };
        bar.querySelector('#rcSpkSave').addEventListener('click', save);
        bar.querySelector('#rcSpkCancel').addEventListener('click', close);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } else if (e.key === 'Escape') close(); });
    }

    R.views.settings = { render, destroy() {} };
})();
