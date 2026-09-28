/* Recall — shared UI primitives: toast, skeleton, empty/error, provenance badge. */
(function () {
    'use strict';
    const U = window.Recall.util;
    const UI = {};

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
            ? `<button type="button" class="rc-btn rc-btn--sm rc-error__retry"><i class="fa-solid fa-rotate-right"></i> Повторити</button>`
            : '';
        return `<div class="rc-error">
            <div class="rc-error__ico"><i class="fa-solid fa-triangle-exclamation"></i></div>
            <p>${U.esc(msg || 'Помилка завантаження')}</p>
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
    // Спільний резолвер шляху «Напрямок → Проєкт» з кешу (map: id -> node).
    // Невідомий id або порожній кеш -> ''.
    function _pathFromCats(cats, id) {
        if (id == null || id === '' || !cats || !cats.map[id]) return '';
        const c = cats.map[id];
        if (c.parent_id != null && cats.map[c.parent_id]) return cats.map[c.parent_id].name + ' → ' + c.name;
        return c.name;
    }
    // "Напрямок → Проєкт" | "Напрямок" | "" (невідомий id). Читає
    // window.Recall.state.categories — виклич UI.loadCategories() заздалегідь.
    UI.catPath = function (id) {
        return _pathFromCats(window.Recall.state.categories, id);
    };
    function _normName(s) {
        return String(s == null ? '' : s).normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim();
    }
    // Чіп/мітка (24): text = імʼя вузла, або повний шлях, якщо в дереві є
    // ІНШИЙ вузол (будь-якого рівня) з такою самою назвою (порівняння без
    // регістру/форми Unicode, пробіли стиснуті); title = повний шлях завжди.
    // Невідомий id -> {text: '', title: ''}.
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

    // — Inline «створити напрямок прямо у виборі» (працює в будь-якому селекті) —
    // Сентинел-значення опції «＋ Новий напрямок…».
    UI.CAT_NEW = '__newcat__';

    // Збудувати <option>-и для селекта напрямку з кешу (window.Recall.state.categories,
    // має бути завантажений через UI.loadCategories). Дерево двох рівнів:
    // кожен напрямок — свій <optgroup label="Напрямок">, усередині опція
    // самого напрямку + опції його проєктів («— Проєкт», з відступом через
    // текстовий префікс). opts.first: 'all' → «Усі напрямки», 'none' (деф.)
    // → «Без напрямку», 'hide' → без провідної опції. Завжди додає
    // «＋ Новий напрямок…» (опустити: opts.newOpt === false).
    UI.catOptions = function (selected, opts) {
        opts = opts || {};
        const list = (window.Recall.state.categories || {}).list || [];
        const cur = selected == null ? '' : String(selected);
        const sel = (v) => (String(v) === cur ? ' selected' : '');
        let html = '';
        if (opts.first === 'all') html += `<option value=""${sel('')}>Усі напрямки</option>`;
        else if (opts.first !== 'hide') html += `<option value=""${sel('')}>Без напрямку</option>`;
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
            html += `<option value="${UI.CAT_NEW}" class="rc-opt-new">＋ Новий напрямок…</option>`;
        return html;
    };

    // Скинути кеш напрямків і перезавантажити (після create/rename/delete/merge).
    UI.bustCategories = function () {
        window.Recall.state.categories = null;
        return UI.loadCategories();
    };

    // — Словник тегів (кешований, той самий патерн, що напрямки) —
    // Помилка API НЕ кешується як «тегів немає»: {list, map, error:true} —
    // порожній словник ≠ збій завантаження (мінор 10 р0, 24).
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
    // Гард кольору з БД (мінор 12 р1; 23 закрив на записі, 28 звів у один
    // хелпер — раніше tagOptions мала власний літерал регулярки, що
    // пропускав 5- і 7-значний hex, який сервер (`memory.py` _COLOR_RE)
    // відкидає). Валідні форми: #RGB/#RGBA/#RRGGBB/#RRGGBBAA — ті самі, що
    // й на сервері. Користуються tagChip, tagOptions і catChip.
    const _COLOR_RE = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
    function _isValidColor(color) {
        return typeof color === 'string' && _COLOR_RE.test(color);
    }
    function _cssColor(color) {
        return _isValidColor(color) ? color : 'var(--rc-ink-3)';
    }
    // Чіп тега {id, name, color}. opts.onRemove(tag) — якщо задано, малює «×»
    // і сам вішає клік. Повертає HTMLElement (не рядок) — щоб onRemove можна
    // було прив'язати одразу, без пошуку елемента після вставки в DOM.
    UI.tagChip = function (tag, opts) {
        opts = opts || {};
        const color = _cssColor(tag.color);
        const removable = typeof opts.onRemove === 'function';
        const chip = U.el('span', { class: 'rc-tag' },
            `<span class="rc-tag__dot" style="background:${U.esc(color)}"></span>${U.esc(tag.name)}`
            + (removable ? `<button type="button" class="rc-tag__x" aria-label="Прибрати тег ${U.esc(tag.name)}">&times;</button>` : ''));
        if (removable) chip.querySelector('.rc-tag__x').addEventListener('click', () => opts.onRemove(tag));
        return chip;
    };
    // Пікер тегів (21). Повертає `<div class="rc-tagpick">` з прихованим
    // `<select multiple class="rc-tagsel">` (кеш має бути завантажений через
    // UI.loadTags; той самий HTML-фрагмент і сигнатура, що й раніше — шість
    // sync()-екранів і рядок Telegram далі читають selectedOptions/change,
    // жодних правок у них). selectedNames — масив назв (значення опцій —
    // назви, не id: PUT /api/memory/<id>/tags працює з іменами). Видимий шар
    // поверх селекта — чіпи-перемикачі (.rc-tagpick__chip), клік по них ловить
    // глобальний делегований обробник нижче. Порожній словник — абзац-
    // пояснення замість голого прямокутника-селекта.
    UI.tagOptions = function (selectedNames) {
        const names = (selectedNames || []).map(String);
        const sel = new Set(names);
        const tagsState = window.Recall.state.tags || {};
        const list = tagsState.list || [];
        if (tagsState.error) {
            // Збій UI.loadTags (24) — «немає тегів» була б хибною; окремий
            // стан з «повторити» (делегований клік нижче: UI.bustTags() +
            // перемалювання поточного екрана). Уже вибрані назви (28, мінор
            // р1 п.11) лишаються selected-опціями прихованого select.rc-tagsel
            // і текстом поруч — інакше фільтр тегів із URL мовчки зникав
            // при наступному рендері/збереженні.
            const selOpts = names.map((n) => `<option value="${U.esc(n)}" selected>${U.esc(n)}</option>`).join('');
            const selText = names.length
                ? `<p class="rc-tagpick__error-selected">Вибрані теги: ${names.map(U.esc).join(', ')}</p>`
                : '';
            return `<div class="rc-tagpick"><select multiple class="rc-tagsel">${selOpts}</select>`
                + selText
                + `<div class="rc-tagpick__error"><span>Не вдалося завантажити теги.</span> `
                + `<button type="button" class="rc-btn rc-btn--sm rc-tagpick__retry">Повторити</button></div></div>`;
        }
        const selHtml = list.map((t) => `<option value="${U.esc(t.name)}"${sel.has(t.name) ? ' selected' : ''}>${U.esc(t.name)}</option>`).join('');
        if (!list.length) {
            return `<div class="rc-tagpick"><select multiple class="rc-tagsel"></select>`
                + `<p class="rc-tagpick__empty">Тегів ще немає. Тег — мітка впоперек напрямків і проєктів `
                + `(наприклад, AI); запис може мати кілька тегів. Створіть перший у `
                + `<a href="/settings#rcSetTags">Налаштуваннях → Теги</a> або на сторінці запису.</p></div>`;
        }
        const chips = list.map((t) => {
            // На відміну від UI.tagChip тут немає безумовного inline-фону —
            // невалідне значення просто НЕ виставляє --tag-color (CSS-дефолт
            // .rc-tagpick__dot { background: var(--tag-color, var(--rc-ink-3)) }).
            const validColor = _isValidColor(t.color);
            const style = validColor ? ` style="--tag-color:${U.esc(t.color)}"` : '';
            const on = sel.has(t.name);
            return `<button type="button" class="rc-chip rc-tagpick__chip${on ? ' is-active' : ''}" data-name="${U.esc(t.name)}"`
                + ` aria-pressed="${on ? 'true' : 'false'}"${style}>`
                + `<span class="rc-tagpick__dot"></span>${U.esc(t.name)}</button>`;
        }).join('');
        return `<div class="rc-tagpick"><select multiple class="rc-tagsel">${selHtml}</select>`
            + `<div class="rc-tagpick__chips" role="group" aria-label="Фільтр за тегами">${chips}</div></div>`;
    };
    // Перемальовує чіпи пікера тегів (21, 24) з поточного стану select.rc-tagsel
    // (option.selected -> aria-pressed + клас is-active). Викликати після
    // прямого виставлення option.selected у коді (applySaved у library.js,
    // відкат у telegram.js) — клік по чіпу сам піднімає change, який ловить
    // делегований слухач нижче (подвійний виклик ідемпотентний).
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
    // Згорнута підказка «Що це?» біля нового фільтра/контролу. Повертає
    // HTMLElement, вставляється поруч із контролом.
    UI.hint = function (html) {
        return U.el('details', { class: 'rc-hint' }, `<summary>Що це?</summary>${html}`);
    };

    // Перебудувати опції в усіх .rc-catsel на сторінці (зберігаючи поточний вибір)
    // після зміни списку напрямків.
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

    // Модалка створення напрямку АБО проєкту всередині напрямку (селект
    // «Напрямок»: порожньо = створюємо новий напрямок, обрано = створюємо
    // проєкт під ним). Resolve → {id, name, color, parent_id} або null (скасовано).
    UI.createCategoryModal = function (prefillName) {
        return new Promise((resolve) => {
            const ov = U.el('div', { class: 'rc-modal-ov' });
            const directions = ((window.Recall.state.categories || {}).list || []).filter((c) => c.level !== 'project');
            const parentOpts = `<option value="">— Це буде напрямок —</option>`
                + directions.map((d) => `<option value="${d.id}">${U.esc(d.name)}</option>`).join('');
            ov.innerHTML = `
                <div class="rc-modal" role="dialog" aria-modal="true" aria-label="Новий напрямок">
                    <div class="rc-modal__h"><i class="fa-solid fa-folder-plus"></i> Новий напрямок</div>
                    <label class="rc-field__label" for="rcNewCatName">Назва</label>
                    <input class="rc-input" id="rcNewCatName" autocomplete="off" maxlength="60" placeholder="Напр.: Проєкт Альфа">
                    <label class="rc-field__label" for="rcNewCatParent" style="margin-top:var(--rc-3)">Напрямок</label>
                    <select class="rc-select" id="rcNewCatParent">${parentOpts}</select>
                    <div class="rc-modal__row">
                        <label class="rc-field__label" for="rcNewCatColor" style="margin:0">Колір</label>
                        <input type="color" id="rcNewCatColor" value="#6c757d">
                    </div>
                    <div class="rc-modal__err" id="rcNewCatErr"></div>
                    <div class="rc-modal__actions">
                        <button class="rc-btn rc-btn--sm" id="rcNewCatCancel">Скасувати</button>
                        <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcNewCatSave">Створити</button>
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
                    errEl.textContent = (e && e.message) || 'Не вдалося створити напрямок';
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
            title: opts.title || 'Підтвердіть дію',
            icon: opts.icon || 'fa-triangle-exclamation',
            bodyHTML: `<p>${U.esc(opts.message || '')}</p>`,
            actions: [
                { label: opts.cancelLabel || 'Скасувати', value: false },
                { label: opts.confirmLabel || 'Підтвердити', value: true, primary: true, danger: opts.danger !== false },
            ],
        }).then((v) => !!v);
    };

    // Branded replacement for window.prompt(). Resolves the trimmed string,
    // or null if cancelled. opts.required blocks the confirm click (and
    // Enter) while the field is empty, instead of silently closing.
    UI.promptModal = function (opts) {
        opts = opts || {};
        return UI.modal({
            title: opts.title || 'Введіть значення',
            icon: opts.icon || 'fa-pen',
            bodyHTML: `
                ${opts.label ? `<label class="rc-field__label" for="rcPromptInput">${U.esc(opts.label)}</label>` : ''}
                <input class="rc-input" id="rcPromptInput" autocomplete="off" maxlength="${opts.maxlength || 120}"
                       placeholder="${U.esc(opts.placeholder || '')}" value="${U.esc(opts.defaultValue || '')}">`,
            actions: [
                { label: opts.cancelLabel || 'Скасувати', value: null },
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
    //     (R.api.updateRecord/updateAudio). Thrown Error.message is shown in
    //     .rc-modal__err and the modal stays open; on success the modal closes
    //     and resolves {title, description}.
    // Resolves null on Escape/backdrop/Cancel.
    UI.editMetaModal = function (opts) {
        opts = opts || {};
        return new Promise((resolve) => {
            const ov = U.el('div', { class: 'rc-modal-ov' });
            ov.innerHTML = `
                <div class="rc-modal" role="dialog" aria-modal="true" aria-label="Назва й опис">
                    <div class="rc-modal__h"><i class="fa-solid fa-pen"></i> Назва й опис</div>
                    <label class="rc-field__label" for="rcMetaTitle">Назва</label>
                    <input class="rc-input" id="rcMetaTitle" autocomplete="off" maxlength="200"
                           placeholder="Назва запису" value="${U.esc(opts.title || '')}">
                    <label class="rc-field__label" for="rcMetaDesc" style="margin-top:var(--rc-3)">Опис</label>
                    <textarea class="rc-textarea" id="rcMetaDesc" placeholder="Опис (необов'язково)">${U.esc(opts.description || '')}</textarea>
                    <div class="rc-modal__err" id="rcMetaErr"></div>
                    <div class="rc-modal__actions">
                        <button class="rc-btn rc-btn--sm" id="rcMetaCancel">Скасувати</button>
                        <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcMetaSave">Зберегти</button>
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
                    errEl.textContent = (e && e.message) || 'Не вдалося зберегти';
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
        });
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
             <button type="button" class="rc-toast__x" aria-label="Закрити">&times;</button>`);
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

    // Снапшот поточного значення перед відкриттям списку (для revert при скасуванні).
    document.addEventListener('focusin', function (e) {
        const s = e.target;
        if (s && s.tagName === 'SELECT' && s.classList.contains('rc-catsel') && s.value !== UI.CAT_NEW)
            s._rcPrev = s.value;
    }, true);

    // Глобальний перехоплювач «＋ Новий напрямок…» у будь-якому .rc-catsel.
    // Capture-фаза + stopPropagation: власний change-хендлер в'юхи НЕ бачить
    // сентинел; після створення ставимо реальний id і ре-діспатчимо звичайний
    // change, щоб в'юха зреагувала так, ніби обрали новий напрямок.
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

    // Делегований change на прихованому select.rc-tagsel (24): будь-яка зміна
    // option.selected — з кліку по чіпу нижче чи прямим виставленням у view
    // (applySaved, відкат Telegram) — перемальовує чіпи з єдиного джерела
    // істини (BLOCK р5: чіпи більше не читаються з aria-pressed напряму).
    document.addEventListener('change', function (e) {
        const s = e.target;
        if (!s || s.tagName !== 'SELECT' || !s.classList.contains('rc-tagsel')) return;
        UI.syncTagPicker(s);
    });

    // Глобальний перехоплювач кліку по чіпу пікера тегів (.rc-tagpick__chip,
    // 21): чіп — лише видимий шар, джерело істини лишається прихований
    // select.rc-tagsel поруч (той самий патерн, що capture-обробник .rc-catsel
    // вище). Клік перемикає відповідну <option> і піднімає change на селекті —
    // слухач вище перемальовує чіпи, sync() шести екранів і рядок Telegram
    // відпрацьовують без правок (C9).
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

    // «Повторити» в .rc-tagpick__error (24): скинути кеш тегів і перемалювати
    // поточний екран через роутер — синтетичний popstate замість
    // router.navigate(поточний шлях) (навігація на той самий path — no-op),
    // router.js не правиться.
    document.addEventListener('click', function (e) {
        const btn = e.target.closest('.rc-tagpick__retry');
        if (!btn) return;
        UI.bustTags().then(() => { window.dispatchEvent(new PopStateEvent('popstate')); });
    });

    // Посилання «Створіть перший у Налаштуваннях → Теги» з порожнього стану
    // пікера (24): перехід через роутер + докрутка й спалах секції «Теги» —
    // той самий патерн, що onboarding.js goTo (router.js/onboarding.js самі
    // не правляться, goTo лишається приватною функцією того модуля).
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
