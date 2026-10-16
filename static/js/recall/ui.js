/* Recall — shared UI primitives: toast, skeleton, empty/error, provenance badge. */
(function () {
    'use strict';
    const U = window.Recall.util;
    const UI = {};
    const T = (k, v) => window.Recall.t(k, v);

    // — Toast — visible for TOAST_VISIBLE_MS, then fades out over TOAST_FADE_MS.
    const TOAST_VISIBLE_MS = 3200;
    const TOAST_FADE_MS = 300;
    UI.toast = function (msg, kind) {
        kind = kind || 'info';
        const wrap = document.getElementById('rcToasts');
        if (!wrap) return;
        const ico = kind === 'success' ? 'fa-circle-check'
            : kind === 'error' ? 'fa-triangle-exclamation' : 'fa-circle-info';
        const t = U.el('div', { class: 'rc-toast', dataset: { kind } },
            `<i class="rc-ico fa-solid ${ico}"></i><span>${U.esc(msg)}</span>`);
        wrap.appendChild(t);
        setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), TOAST_FADE_MS); }, TOAST_VISIBLE_MS);
    };

    // — State blocks —
    UI.skeletonList = function (n) {
        let h = '';
        for (let i = 0; i < (n || 6); i++) h += '<div class="rc-skel rc-skel-row"></div>';
        return h;
    };
    // opts.actions (T5.5) — optional CTA row so an empty state isn't a dead
    // end: [{ label, icon, href, primary, onClick }]. `href` renders as a
    // plain <a> (the router intercepts internal links itself — no wiring
    // needed); `onClick` renders a <button data-empty-act="i"> that the
    // caller must wire via UI.wireEmptyActions() after inserting the HTML.
    // Omitting actions reproduces the exact previous markup (back-compat).
    UI.empty = function (title, sub, icon, actions) {
        actions = actions || [];
        const btns = actions.length ? `<div class="rc-empty__actions">${actions.map((a, i) => a.href
            ? `<a class="rc-btn rc-btn--sm${a.primary ? ' rc-btn--primary' : ''}" href="${U.esc(a.href)}"><i class="fa-solid ${a.icon || 'fa-arrow-right'}"></i> ${U.esc(a.label)}</a>`
            : `<button type="button" class="rc-btn rc-btn--sm${a.primary ? ' rc-btn--primary' : ''}" data-empty-act="${i}"><i class="fa-solid ${a.icon || 'fa-arrow-right'}"></i> ${U.esc(a.label)}</button>`
        ).join('')}</div>` : '';
        return `<div class="rc-empty">
            <div class="rc-empty__ico"><i class="fa-solid ${icon || 'fa-inbox'}"></i></div>
            <div class="rc-empty__title">${U.esc(title)}</div>
            ${sub ? `<p>${U.esc(sub)}</p>` : ''}
            ${btns}
        </div>`;
    };
    // Wire the onClick actions of a UI.empty(..., actions) call after the
    // returned HTML has been inserted into `container`. href-actions are
    // plain links and need no wiring (router handles them).
    UI.wireEmptyActions = function (container, actions) {
        if (!container || !actions || !actions.length) return;
        container.querySelectorAll('[data-empty-act]').forEach((btn) => {
            const a = actions[Number(btn.dataset.emptyAct)];
            if (a && typeof a.onClick === 'function') btn.addEventListener('click', a.onClick);
        });
    };

    // opts.retry (T5.5) — when true, renders a "Повторити" button so a
    // failed load reads as "broken, retry" rather than a dead end. Wire it
    // with UI.bindErrorRetry() after inserting the HTML. Omitting opts
    // reproduces the exact previous markup (back-compat).
    UI.error = function (msg, opts) {
        opts = opts || {};
        const retryBtn = opts.retry
            ? `<button type="button" class="rc-btn rc-btn--sm rc-error__retry"><i class="fa-solid fa-rotate-right"></i> ${T('core.ui.retry')}</button>`
            : '';
        return `<div class="rc-error">
            <div class="rc-error__ico"><i class="fa-solid fa-triangle-exclamation"></i></div>
            <p>${U.esc(msg || T('core.ui.loadError'))}</p>
            ${retryBtn}
        </div>`;
    };
    // Wire the retry button of a UI.error(msg, {retry:true}) call after the
    // returned HTML has been inserted into `container`. No-op if the error
    // markup has no retry button.
    UI.bindErrorRetry = function (container, onRetry) {
        if (!container) return;
        const btn = container.querySelector('.rc-error__retry');
        if (btn) btn.addEventListener('click', onRetry);
    };

    // — Provenance source badge —
    UI.srcBadge = function (sourceType) {
        const st = sourceType || 'file';
        const label = U.SRC_LABEL[st] || st;
        const icon = U.SRC_ICON[st] || 'fa-solid fa-file';
        return `<span class="rc-srcbadge" data-src="${U.esc(st)}">
            <span class="rc-srcbadge__dot"></span>
            <i class="${icon}" style="font-size:.9em"></i>${U.esc(label)}
        </span>`;
    };

    // — Category resolution (cached) —
    UI.loadCategories = async function () {
        if (window.Recall.state.categories) return window.Recall.state.categories;
        try {
            const data = await window.Recall.api.categories();
            const list = data.categories || data || [];
            const map = {};
            list.forEach(c => { map[c.id] = c; });
            window.Recall.state.categories = { list, map };
        } catch (_) {
            window.Recall.state.categories = { list: [], map: {} };
        }
        return window.Recall.state.categories;
    };
    // Shared resolver of the "Category → Project" path from the cache (map: id -> node).
    // Unknown id or empty cache -> ''.
    function _pathFromCats(cats, id) {
        if (id == null || id === '' || !cats || !cats.map[id]) return '';
        const c = cats.map[id];
        if (c.parent_id != null && cats.map[c.parent_id]) return cats.map[c.parent_id].name + ' → ' + c.name;
        return c.name;
    }
    // "Category → Project" | "Category" | "" (unknown id). Reads
    // window.Recall.state.categories — call UI.loadCategories() beforehand.
    UI.catPath = function (id) {
        return _pathFromCats(window.Recall.state.categories, id);
    };
    function _normName(s) {
        return String(s == null ? '' : s).normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
    }
    // Chip/label (24): text = node name, or the full path if the tree has
    // ANOTHER node (of any level) with the same name (comparison is
    // case/Unicode-form insensitive, whitespace collapsed); title = always the full path.
    // Unknown id -> {text: '', title: ''}.
    UI.catLabel = function (id, cats) {
        cats = cats || window.Recall.state.categories;
        if (id == null || id === '' || !cats || !cats.map[id]) return { text: '', title: '' };
        const c = cats.map[id];
        const title = _pathFromCats(cats, id);
        const norm = _normName(c.name);
        const dupe = (cats.list || []).some((o) => o.id !== c.id && _normName(o.name) === norm);
        return { text: dupe ? title : c.name, title };
    };
    UI.catChip = function (catId, cats) {
        cats = cats || window.Recall.state.categories;
        if (!catId || !cats || !cats.map[catId]) return '';
        const c = cats.map[catId];
        const color = _cssColor(c.color);
        const label = UI.catLabel(catId, cats);
        return `<span class="rc-cat" title="${U.esc(label.title)}"><span class="rc-cat__dot" style="background:${U.esc(color)}"></span>${U.esc(label.text)}</span>`;
    };

    // — Inline "create a category right in the picker" (works in any select) —
    // Sentinel value of the option «＋ Новий напрямок…».
    UI.CAT_NEW = '__newcat__';

    // Build <option>s for the category select from the cache (window.Recall.state.categories,
    // must be loaded via UI.loadCategories). A two-level tree:
    // each category gets its own <optgroup label="Напрямок">, inside it an option of
    // the category itself + options of its projects («— Проєкт», indented via a
    // text prefix). opts.first: 'all' → «Усі напрямки», 'none' (default)
    // → «Без напрямку», 'hide' → no leading option. Always adds
    // «＋ Новий напрямок…» (omit: opts.newOpt === false).
    UI.catOptions = function (selected, opts) {
        opts = opts || {};
        const list = (window.Recall.state.categories || {}).list || [];
        const cur = selected == null ? '' : String(selected);
        const sel = (v) => (String(v) === cur ? ' selected' : '');
        let html = '';
        if (opts.first === 'all') html += `<option value=""${sel('')}>${T('core.ui.allCats')}</option>`;
        else if (opts.first !== 'hide') html += `<option value=""${sel('')}>${T('core.ui.noCat')}</option>`;
        let groupOpen = false;
        list.forEach((c) => {
            if (c.level === 'project') {
                html += `<option value="${c.id}"${sel(c.id)}>— ${U.esc(c.name)}</option>`;
                return;
            }
            if (groupOpen) html += `</optgroup>`;
            html += `<optgroup label="${U.esc(c.name)}"><option value="${c.id}"${sel(c.id)}>${U.esc(c.name)}</option>`;
            groupOpen = true;
        });
        if (groupOpen) html += `</optgroup>`;
        if (opts.newOpt !== false)
            html += `<option value="${UI.CAT_NEW}" class="rc-opt-new">${T('core.ui.newCatOpt')}</option>`;
        return html;
    };

    // Reset the category cache and reload (after create/rename/delete/merge).
    UI.bustCategories = function () {
        window.Recall.state.categories = null;
        return UI.loadCategories();
    };

    // — Tag dictionary (cached, same pattern as categories) —
    // An API error is NOT cached as "no tags": {list, map, error:true} —
    // an empty dictionary ≠ a load failure (minor 10 r0, 24).
    UI.loadTags = async function () {
        if (window.Recall.state.tags) return window.Recall.state.tags;
        try {
            const data = await window.Recall.api.getTags();
            const list = data.tags || data || [];
            const map = {};
            list.forEach((t) => { map[t.id] = t; });
            window.Recall.state.tags = { list, map };
        } catch (_) {
            window.Recall.state.tags = { list: [], map: {}, error: true };
        }
        return window.Recall.state.tags;
    };
    UI.bustTags = function () {
        window.Recall.state.tags = null;
        return UI.loadTags();
    };
    // Guard for a color from the DB (minor 12 r1; 23 closed it on write, 28 merged into one
    // helper — before that tagOptions had its own regex literal that
    // let through a 5- and 7-digit hex, which the server (`memory.py` _COLOR_RE)
    // rejects). Valid forms: #RGB/#RGBA/#RRGGBB/#RRGGBBAA — the same as
    // on the server. Used by tagChip, tagOptions and catChip.
    const _COLOR_RE = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
    function _isValidColor(color) {
        return typeof color === 'string' && _COLOR_RE.test(color);
    }
    function _cssColor(color) {
        return _isValidColor(color) ? color : 'var(--rc-ink-3)';
    }
    // Tag chip {id, name, color}. opts.onRemove(tag) — if given, draws «×»
    // and attaches the click itself. Returns an HTMLElement (not a string) — so onRemove can
    // be bound right away, without looking up the element after insertion into the DOM.
    UI.tagChip = function (tag, opts) {
        opts = opts || {};
        const color = _cssColor(tag.color);
        const removable = typeof opts.onRemove === 'function';
        const chip = U.el('span', { class: 'rc-tag' },
            `<span class="rc-tag__dot" style="background:${U.esc(color)}"></span>${U.esc(tag.name)}`
            + (removable ? `<button type="button" class="rc-tag__x" aria-label="${U.esc(T('core.ui.tag.removeAria', { name: tag.name }))}">&times;</button>` : ''));
        if (removable) chip.querySelector('.rc-tag__x').addEventListener('click', () => opts.onRemove(tag));
        return chip;
    };
    // Tag picker (21). Returns `<div class="rc-tagpick">` with a hidden
    // `<select multiple class="rc-tagsel">` (the cache must be loaded via
    // UI.loadTags; the same HTML fragment and signature as before — six
    // sync() screens and the Telegram row keep reading selectedOptions/change,
    // no edits in them). selectedNames — an array of names (option values are
    // names, not ids: PUT /api/memory/<id>/tags works with names). The visible layer
    // on top of the select is toggle chips (.rc-tagpick__chip), a click on them is caught by
    // the global delegated handler below. An empty dictionary — an explanatory
    // paragraph instead of a bare select rectangle.
    UI.tagOptions = function (selectedNames) {
        const names = (selectedNames || []).map(String);
        const sel = new Set(names);
        const tagsState = window.Recall.state.tags || {};
        const list = tagsState.list || [];
        if (tagsState.error) {
            // A UI.loadTags failure (24) — "no tags" would be false; a separate
            // state with "retry" (delegated click below: UI.bustTags() +
            // redraw of the current screen). Already selected names (28, minor
            // r1 item 11) stay as selected options of the hidden select.rc-tagsel
            // and as text nearby — otherwise the tag filter from the URL silently vanished
            // on the next render/save.
            const selOpts = names.map((n) => `<option value="${U.esc(n)}" selected>${U.esc(n)}</option>`).join('');
            const selText = names.length
                ? `<p class="rc-tagpick__error-selected">${T('core.ui.tags.selected', { names: names.map(U.esc).join(', ') })}</p>`
                : '';
            return `<div class="rc-tagpick"><select multiple class="rc-tagsel">${selOpts}</select>`
                + selText
                + `<div class="rc-tagpick__error"><span>${T('core.ui.tags.loadFail')}</span> `
                + `<button type="button" class="rc-btn rc-btn--sm rc-tagpick__retry">${T('core.ui.retry')}</button></div></div>`;
        }
        const selHtml = list.map((t) => `<option value="${U.esc(t.name)}"${sel.has(t.name) ? ' selected' : ''}>${U.esc(t.name)}</option>`).join('');
        if (!list.length) {
            return `<div class="rc-tagpick"><select multiple class="rc-tagsel"></select>`
                + `<p class="rc-tagpick__empty">${T('core.ui.tags.empty')}</p></div>`;
        }
        const chips = list.map((t) => {
            // Unlike UI.tagChip there is no unconditional inline background here —
            // an invalid value simply does NOT set --tag-color (CSS default
            // .rc-tagpick__dot { background: var(--tag-color, var(--rc-ink-3)) }).
            const validColor = _isValidColor(t.color);
            const style = validColor ? ` style="--tag-color:${U.esc(t.color)}"` : '';
            const on = sel.has(t.name);
            return `<button type="button" class="rc-chip rc-tagpick__chip${on ? ' is-active' : ''}" data-name="${U.esc(t.name)}"`
                + ` aria-pressed="${on ? 'true' : 'false'}"${style}>`
                + `<span class="rc-tagpick__dot"></span>${U.esc(t.name)}</button>`;
        }).join('');
        return `<div class="rc-tagpick"><select multiple class="rc-tagsel">${selHtml}</select>`
            + `<div class="rc-tagpick__chips" role="group" aria-label="${T('core.ui.tags.filterAria')}">${chips}</div></div>`;
    };
    // Redraws the tag picker chips (21, 24) from the current state of select.rc-tagsel
    // (option.selected -> aria-pressed + class is-active). Call after
    // setting option.selected directly in code (applySaved in library.js,
    // rollback in telegram.js) — a click on a chip raises change itself, which is caught by the
    // delegated listener below (a double call is idempotent).
    UI.syncTagPicker = function (selectEl) {
        if (!selectEl) return;
        const pick = selectEl.closest('.rc-tagpick');
        if (!pick) return;
        const selNames = new Set(Array.from(selectEl.selectedOptions).map((o) => o.value));
        pick.querySelectorAll('.rc-tagpick__chip').forEach((btn) => {
            const on = selNames.has(btn.dataset.name);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            btn.classList.toggle('is-active', on);
        });
    };
    // A collapsed "What is this?" hint next to a new filter/control. Returns an
    // HTMLElement, inserted next to the control.
    UI.hint = function (html) {
        return U.el('details', { class: 'rc-hint' }, `<summary>${T('core.ui.hint.what')}</summary>${html}`);
    };

    // Rebuild options in all .rc-catsel on the page (keeping the current choice)
    // after the category list changes.
    UI.refreshCatSelects = function (root) {
        (root || document).querySelectorAll('select.rc-catsel').forEach(s => {
            const cur = s.value === UI.CAT_NEW ? '' : s.value;
            s.innerHTML = UI.catOptions(cur, {
                first: s.dataset.catFirst || 'none',
                newOpt: s.dataset.catNew !== '0',
            });
            s.value = cur;
            s._rcPrev = s.value;
        });
    };

    // Modal for creating a category OR a project inside a category (select
    // «Напрямок»: empty = create a new category, chosen = create
    // a project under it). Resolve → {id, name, color, parent_id} or null (cancelled).
    UI.createCategoryModal = function (prefillName) {
        return new Promise((resolve) => {
            const ov = U.el('div', { class: 'rc-modal-ov' });
            const directions = ((window.Recall.state.categories || {}).list || []).filter((c) => c.level !== 'project');
            const parentOpts = `<option value="">${T('core.ui.newCat.parentNone')}</option>`
                + directions.map((d) => `<option value="${d.id}">${U.esc(d.name)}</option>`).join('');
            ov.innerHTML = `
                <div class="rc-modal" role="dialog" aria-modal="true" aria-label="${T('core.ui.newCat.title')}">
                    <div class="rc-modal__h"><i class="fa-solid fa-folder-plus"></i> ${T('core.ui.newCat.title')}</div>
                    <label class="rc-field__label" for="rcNewCatName">${T('core.ui.name')}</label>
                    <input class="rc-input" id="rcNewCatName" autocomplete="off" maxlength="60" placeholder="${T('core.ui.newCat.namePh')}">
                    <label class="rc-field__label" for="rcNewCatParent" style="margin-top:var(--rc-3)">${T('core.ui.newCat.category')}</label>
                    <select class="rc-select" id="rcNewCatParent">${parentOpts}</select>
                    <div class="rc-modal__row">
                        <label class="rc-field__label" for="rcNewCatColor" style="margin:0">${T('core.ui.newCat.color')}</label>
                        <input type="color" id="rcNewCatColor" value="#6c757d">
                    </div>
                    <div class="rc-modal__err" id="rcNewCatErr"></div>
                    <div class="rc-modal__actions">
                        <button class="rc-btn rc-btn--sm" id="rcNewCatCancel">${T('core.cancel')}</button>
                        <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcNewCatSave">${T('core.create')}</button>
                    </div>
                </div>`;
            document.body.appendChild(ov);
            const nameI = ov.querySelector('#rcNewCatName');
            const parentI = ov.querySelector('#rcNewCatParent');
            const colorI = ov.querySelector('#rcNewCatColor');
            const errEl = ov.querySelector('#rcNewCatErr');
            const saveB = ov.querySelector('#rcNewCatSave');
            if (prefillName) nameI.value = prefillName;
            setTimeout(() => nameI.focus(), 30);
            let done = false;
            const close = (val) => {
                if (done) return; done = true;
                document.removeEventListener('keydown', onKey, true);
                ov.remove(); resolve(val);
            };
            const save = async () => {
                const name = nameI.value.trim();
                if (!name) { nameI.focus(); return; }
                saveB.disabled = true; errEl.textContent = '';
                const parent_id = parentI.value ? Number(parentI.value) : null;
                try {
                    const r = await window.Recall.api.categoryCreate({ name, color: colorI.value, parent_id });
                    await UI.bustCategories();
                    close({ id: r.id, name: r.name || name, color: r.color || colorI.value, parent_id: r.parent_id != null ? r.parent_id : parent_id });
                } catch (e) {
                    errEl.textContent = (e && e.message) || T('core.ui.newCat.fail');
                    saveB.disabled = false; nameI.focus(); nameI.select();
                }
            };
            const onKey = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); }
                else if (e.key === 'Enter' && e.target === nameI) { e.preventDefault(); save(); }
            };
            document.addEventListener('keydown', onKey, true);
            ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(null); });
            saveB.addEventListener('click', save);
            ov.querySelector('#rcNewCatCancel').addEventListener('click', () => close(null));
        });
    };

    // — Generic branded dialog builder (T4.6). Resolves with the value of
    // whichever action button was clicked, or `null` on Escape / backdrop /
    // ×. Reuses the same rc-modal-ov/rc-modal markup as createCategoryModal
    // above so every dialog in the app looks the same.
    //   opts.title, opts.icon (fa-* name, optional)
    //   opts.bodyHTML — raw HTML for the dialog body
    //   opts.actions — [{ label, value, primary, danger, getValue(ov), validate(ov) }]
    //     - value: static resolve value (default action)
    //     - getValue(ov): computed at click-time (e.g. current input value) — wins over `value`
    //     - validate(ov): if it returns false, the click is ignored (dialog stays open, input refocused)
    //   opts.autofocus — selector for the element to focus on open (default: primary button)
    //   opts.onMount(ov) — called after the dialog is in the DOM, before autofocus
    //   opts.closeOnBackdrop (default true), opts.noEscape (default false)
    UI.modal = function (opts) {
        opts = opts || {};
        const actions = opts.actions || [];
        return new Promise((resolve) => {
            const ov = U.el('div', { class: 'rc-modal-ov' });
            const btnsHTML = actions.map((a, i) =>
                `<button class="rc-btn rc-btn--sm${a.primary ? ' rc-btn--primary' : ''}${a.danger ? ' rc-btn--danger' : ''}" data-modal-act="${i}">${U.esc(a.label)}</button>`
            ).join('');
            ov.innerHTML = `
                <div class="rc-modal" role="dialog" aria-modal="true" aria-label="${U.esc(opts.title || '')}">
                    <div class="rc-modal__h">${opts.icon ? `<i class="fa-solid ${opts.icon}"></i> ` : ''}${U.esc(opts.title || '')}</div>
                    <div class="rc-modal__body">${opts.bodyHTML || ''}</div>
                    <div class="rc-modal__err" id="rcModalErr"></div>
                    <div class="rc-modal__actions">${btnsHTML}</div>
                </div>`;
            document.body.appendChild(ov);
            let done = false;
            const close = (val) => {
                if (done) return; done = true;
                document.removeEventListener('keydown', onKey, true);
                ov.remove(); resolve(val);
            };
            const onKey = (e) => {
                if (e.key === 'Escape' && opts.noEscape !== true) { e.preventDefault(); e.stopPropagation(); close(null); }
            };
            document.addEventListener('keydown', onKey, true);
            if (opts.closeOnBackdrop !== false)
                ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(null); });
            ov.querySelectorAll('[data-modal-act]').forEach((b, i) => {
                b.addEventListener('click', () => {
                    const a = actions[i];
                    if (typeof a.validate === 'function' && !a.validate(ov)) {
                        const inp = ov.querySelector('input');
                        if (inp) inp.focus();
                        return;
                    }
                    const val = typeof a.getValue === 'function' ? a.getValue(ov) : a.value;
                    close(val);
                });
            });
            if (typeof opts.onMount === 'function') opts.onMount(ov, close);
            const af = opts.autofocus ? ov.querySelector(opts.autofocus) : ov.querySelector('.rc-btn--primary');
            if (af) setTimeout(() => af.focus(), 30);
        });
    };

    // Branded replacement for window.confirm(). Resolves true/false.
    //   opts.title, opts.message, opts.confirmLabel, opts.cancelLabel
    //   opts.danger (default true — confirm button styled as destructive)
    //   opts.icon (default fa-triangle-exclamation)
    UI.confirmModal = function (opts) {
        opts = opts || {};
        return UI.modal({
            title: opts.title || T('core.ui.confirm.title'),
            icon: opts.icon || 'fa-triangle-exclamation',
            bodyHTML: `<p>${U.esc(opts.message || '')}</p>`,
            actions: [
                { label: opts.cancelLabel || T('core.cancel'), value: false },
                { label: opts.confirmLabel || T('core.ui.confirm.ok'), value: true, primary: true, danger: opts.danger !== false },
            ],
        }).then((v) => !!v);
    };

    // Branded replacement for window.prompt(). Resolves the trimmed string,
    // or null if cancelled. opts.required blocks the confirm click (and
    // Enter) while the field is empty, instead of silently closing.
    UI.promptModal = function (opts) {
        opts = opts || {};
        return UI.modal({
            title: opts.title || T('core.ui.prompt.title'),
            icon: opts.icon || 'fa-pen',
            bodyHTML: `
                ${opts.label ? `<label class="rc-field__label" for="rcPromptInput">${U.esc(opts.label)}</label>` : ''}
                <input class="rc-input" id="rcPromptInput" autocomplete="off" maxlength="${opts.maxlength || 120}"
                       placeholder="${U.esc(opts.placeholder || '')}" value="${U.esc(opts.defaultValue || '')}">`,
            actions: [
                { label: opts.cancelLabel || T('core.cancel'), value: null },
                {
                    label: opts.confirmLabel || 'OK', primary: true,
                    getValue: (ov) => ov.querySelector('#rcPromptInput').value.trim(),
                    validate: (ov) => !opts.required || ov.querySelector('#rcPromptInput').value.trim().length > 0,
                },
            ],
            autofocus: '#rcPromptInput',
            onMount: (ov) => {
                const input = ov.querySelector('#rcPromptInput');
                const okBtn = ov.querySelector('[data-modal-act="1"]');
                input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); okBtn.click(); } });
            },
        });
    };

    // Edit-metadata modal (title + multiline description) — shared by the
    // record page pencil, Library row action and Audio card action (C6).
    //   opts.title, opts.description — current values (prefilled)
    //   opts.titleRequired — block save while title is empty (default false;
    //     records fall back to source_name server-side, so it's optional there)
    //   opts.save(values) — async fn performing the actual PATCH
    //   opts.suggest — optional async () => {titles, descriptions, basis}
    //     (R.api.suggestMeta): adds «Запропонувати» — the local model's variants
    //     appear under each field, a click puts one into the field for editing.
    //   opts.autoSuggest — ask for variants right on open (needs opts.suggest).
    //     (R.api.updateRecord/updateAudio). Thrown Error.message is shown in
    //     .rc-modal__err and the modal stays open; on success the modal closes
    //     and resolves {title, description}.
    // Resolves null on Escape/backdrop/Cancel.
    UI.editMetaModal = function (opts) {
        opts = opts || {};
        return new Promise((resolve) => {
            const ov = U.el('div', { class: 'rc-modal-ov' });
            const canSuggest = typeof opts.suggest === 'function';
            ov.innerHTML = `
                <div class="rc-modal${canSuggest ? ' rc-modal--wide' : ''}" role="dialog" aria-modal="true" aria-label="${T('core.ui.meta.title')}">
                    <div class="rc-modal__h"><i class="fa-solid fa-pen"></i> ${T('core.ui.meta.title')}
                        ${canSuggest ? `<button class="rc-btn rc-btn--sm rc-meta__suggest" id="rcMetaSuggest" type="button"><i class="fa-solid fa-wand-magic-sparkles"></i> ${T('core.ui.meta.suggest')}</button>` : ''}
                    </div>
                    ${canSuggest ? `<div class="rc-meta__status" id="rcMetaStatus" aria-live="polite"></div>` : ''}
                    <label class="rc-field__label" for="rcMetaTitle">${T('core.ui.name')}</label>
                    <input class="rc-input" id="rcMetaTitle" autocomplete="off" maxlength="200"
                           placeholder="${T('core.ui.meta.namePh')}" value="${U.esc(opts.title || '')}">
                    ${canSuggest ? `<div class="rc-meta__variants" id="rcMetaTitleVars"></div>` : ''}
                    <label class="rc-field__label" for="rcMetaDesc" style="margin-top:var(--rc-3)">${T('core.ui.meta.desc')}</label>
                    <textarea class="rc-textarea" id="rcMetaDesc" placeholder="${T('core.ui.meta.descPh')}">${U.esc(opts.description || '')}</textarea>
                    ${canSuggest ? `<div class="rc-meta__variants rc-meta__variants--desc" id="rcMetaDescVars"></div>` : ''}
                    <div class="rc-modal__err" id="rcMetaErr"></div>
                    <div class="rc-modal__actions">
                        <button class="rc-btn rc-btn--sm" id="rcMetaCancel">${T('core.cancel')}</button>
                        <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcMetaSave">${T('core.save')}</button>
                    </div>
                </div>`;
            document.body.appendChild(ov);
            const titleI = ov.querySelector('#rcMetaTitle');
            const descI = ov.querySelector('#rcMetaDesc');
            const errEl = ov.querySelector('#rcMetaErr');
            const saveB = ov.querySelector('#rcMetaSave');
            setTimeout(() => titleI.focus(), 30);
            let done = false;
            const close = (val) => {
                if (done) return; done = true;
                document.removeEventListener('keydown', onKey, true);
                ov.remove(); resolve(val);
            };
            const save = async () => {
                const title = titleI.value.trim();
                if (opts.titleRequired && !title) { titleI.focus(); return; }
                const values = { title, description: descI.value.trim() };
                saveB.disabled = true; errEl.textContent = '';
                try {
                    if (typeof opts.save === 'function') await opts.save(values);
                    close(values);
                } catch (e) {
                    errEl.textContent = (e && e.message) || T('core.ui.meta.saveFail');
                    saveB.disabled = false; titleI.focus();
                }
            };
            const onKey = (e) => {
                if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(null); }
            };
            document.addEventListener('keydown', onKey, true);
            ov.addEventListener('mousedown', (e) => { if (e.target === ov) close(null); });
            saveB.addEventListener('click', save);
            ov.querySelector('#rcMetaCancel').addEventListener('click', () => close(null));

            if (canSuggest) {
                const sugB = ov.querySelector('#rcMetaSuggest');
                const statusEl = ov.querySelector('#rcMetaStatus');
                const fill = (box, values, input) => {
                    box.innerHTML = '';
                    (values || []).forEach(v => {
                        const b = U.el('button', { class: 'rc-meta__variant', type: 'button' });
                        b.textContent = v;
                        b.addEventListener('click', () => {
                            input.value = v; input.focus();
                            box.querySelectorAll('.rc-meta__variant').forEach(x => x.classList.toggle('is-picked', x === b));
                        });
                        box.appendChild(b);
                    });
                };
                const ask = async () => {
                    sugB.disabled = true; errEl.textContent = '';
                    statusEl.className = 'rc-meta__status';
                    statusEl.textContent = T('core.ui.meta.thinking');
                    try {
                        const r = await opts.suggest();
                        if (done) return;
                        fill(ov.querySelector('#rcMetaTitleVars'), r.titles, titleI);
                        fill(ov.querySelector('#rcMetaDescVars'), r.descriptions, descI);
                        statusEl.textContent = r.basis === 'excerpts'
                            ? T('core.ui.meta.basisExcerpts')
                            : T('core.ui.meta.basisPick');
                        sugB.innerHTML = '<i class="fa-solid fa-rotate"></i> ' + T('core.ui.meta.more');
                    } catch (e) {
                        if (done) return;
                        statusEl.className = 'rc-meta__status is-err';
                        statusEl.textContent = (e && e.message) || T('core.ui.meta.suggestFail');
                    } finally {
                        if (!done) sugB.disabled = false;
                    }
                };
                sugB.addEventListener('click', ask);
                if (opts.autoSuggest) ask();
            }
        });
    };

    // A record without its own title: the title is still the session auto-name («Запис 2026-10-14 08:54»
    // or the fallback «Запис rec_…»). We look at the title the person sees
    // (title, otherwise source_name): transcription from the Media library copies the auto-name
    // into transcriptions.title, so "title non-empty" does not yet mean its own title.
    const AUTO_NAME_RE = /^Запис (\d{4}-\d{2}-\d{2} \d{2}:\d{2}|rec_[0-9a-f]+)$/;
    UI.isUnnamedRecording = function (r) {
        if (!r || r.source_type !== 'recording') return false;
        const shown = (r.title || '').trim() || (r.source_name || '').trim();
        return AUTO_NAME_RE.test(shown);
    };

    // Sticky action-toast for undo affordances (T4.6). Unlike UI.toast, it
    // carries an explicit action button and does not vanish after the short
    // 3.2s info-toast window — it lives for opts.duration (default 7s) or
    // until dismissed/actioned. If it silently expires with no click, the
    // caller does nothing further: the backend purges soft-deleted rows on
    // its own grace period regardless, so a missed toast is safe.
    UI.actionToast = function (msg, actionLabel, onAction, opts) {
        opts = opts || {};
        const wrap = document.getElementById('rcToasts');
        if (!wrap) return null;
        const t = U.el('div', { class: 'rc-toast rc-toast--action' },
            `<i class="rc-ico fa-solid fa-clock-rotate-left"></i><span></span>
             <button type="button" class="rc-toast__action"></button>
             <button type="button" class="rc-toast__x" aria-label="${T('core.close')}">&times;</button>`);
        t.querySelector('span').textContent = msg;
        t.querySelector('.rc-toast__action').textContent = actionLabel;
        wrap.appendChild(t);
        let done = false;
        const remove = () => { if (done) return; done = true; clearTimeout(timer); t.remove(); };
        t.querySelector('.rc-toast__action').addEventListener('click', () => {
            remove();
            try { if (onAction) onAction(); } catch (_) {}
        });
        t.querySelector('.rc-toast__x').addEventListener('click', remove);
        const timer = setTimeout(remove, opts.duration || 7000);
        return { close: remove };
    };

    // Snapshot of the current value before opening the list (for revert on cancel).
    document.addEventListener('focusin', function (e) {
        const s = e.target;
        if (s && s.tagName === 'SELECT' && s.classList.contains('rc-catsel') && s.value !== UI.CAT_NEW)
            s._rcPrev = s.value;
    }, true);

    // Global interceptor of «＋ Новий напрямок…» in any .rc-catsel.
    // Capture phase + stopPropagation: the view's own change handler does NOT see the
    // sentinel; after creation we set the real id and re-dispatch an ordinary
    // change, so the view reacts as if a new category had been chosen.
    document.addEventListener('change', function (e) {
        const s = e.target;
        if (!s || s.tagName !== 'SELECT' || !s.classList.contains('rc-catsel')) return;
        if (s.value !== UI.CAT_NEW) { s._rcPrev = s.value; return; }
        e.stopPropagation();
        const prev = s._rcPrev != null ? s._rcPrev : '';
        UI.createCategoryModal().then((cat) => {
            if (!cat) { s.value = prev; s._rcPrev = prev; return; }
            UI.refreshCatSelects();
            s.value = String(cat.id);
            s._rcPrev = s.value;
            s.dispatchEvent(new Event('change', { bubbles: true }));
        });
    }, true);

    // Delegated change on the hidden select.rc-tagsel (24): any change of
    // option.selected — from a chip click below or set directly in a view
    // (applySaved, Telegram rollback) — redraws the chips from a single source of
    // truth (BLOCK r5: chips are no longer read from aria-pressed directly).
    document.addEventListener('change', function (e) {
        const s = e.target;
        if (!s || s.tagName !== 'SELECT' || !s.classList.contains('rc-tagsel')) return;
        UI.syncTagPicker(s);
    });

    // Global interceptor of a click on a tag picker chip (.rc-tagpick__chip,
    // 21): the chip is only a visible layer, the source of truth stays the hidden
    // select.rc-tagsel nearby (same pattern as the capture handler of .rc-catsel
    // above). A click toggles the corresponding <option> and raises change on the select —
    // the listener above redraws the chips, the sync() of six screens and the Telegram row
    // work without edits (C9).
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.rc-tagpick__chip');
        if (!btn) return;
        const pick = btn.closest('.rc-tagpick');
        const sel = pick && pick.querySelector('select.rc-tagsel');
        if (!sel) return;
        const opt = Array.from(sel.options).find((o) => o.value === btn.dataset.name);
        if (!opt) return;
        opt.selected = !opt.selected;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
    });

    // "Retry" in .rc-tagpick__error (24): reset the tag cache and redraw
    // the current screen via the router — a synthetic popstate instead of
    // router.navigate(current path) (navigation to the same path is a no-op),
    // router.js is not edited.
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.rc-tagpick__retry');
        if (!btn) return;
        UI.bustTags().then(() => { window.dispatchEvent(new PopStateEvent('popstate')); });
    });

    // The link "Створіть перший у Налаштуваннях → Теги" from the empty state of
    // the picker (24): navigation via the router + scroll and flash of the "Теги" section —
    // the same pattern as onboarding.js goTo (router.js/onboarding.js themselves
    // are not edited, goTo stays a private function of that module).
    document.addEventListener('click', function (e) {
        const a = e.target.closest('.rc-tagpick__empty a[href^="/settings#"]');
        if (!a) return;
        e.preventDefault();
        const hash = (a.getAttribute('href') || '').split('#')[1];
        window.Recall.router.navigate('/settings');
        requestAnimationFrame(() => {
            const el = hash && document.getElementById(hash);
            if (!el) return;
            el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            const card = el.closest('.rc-set') || el;
            card.classList.add('rc-onb-flash');
            setTimeout(() => card.classList.remove('rc-onb-flash'), 1600);
        });
    });

    window.Recall.ui = UI;
})();
