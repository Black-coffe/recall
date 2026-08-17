/* Recall — T5.3: перший запуск (guided onboarding).
   Легкий чек-лист (не тур): модель Whisper → API-ключ Claude → mic-дозвіл →
   Telegram opt-in. Кожен крок веде у вже наявне місце (Налаштування/Telegram)
   і сам перевіряє, чи вже виконаний — нічого не дублює з T5.1/T5.2/T5.7.

   Персист: localStorage (НЕ sessionStorage) під ключем recall_onboarding_done —
   переживає перезапуск браузера. Ставиться на будь-яке явне закриття
   (Пропустити / × / Escape / клік по підкладці / дія кроку — «веди й закрий»),
   бо неявний авто-показ при кожному навігейті між кроками був би нав'язливим.
   Реюз завжди доступний — іконка у топбарі (shell.html #rcOnbTrigger) відкриває
   вікно незалежно від прапорця (force-режим).

   Detection per step (жива перевірка при кожному відкритті, не кешується
   довше одного показу):
     (a) модель:    GET /api/models         → є m.downloaded === true
     (b) API-ключ:  GET /api/settings/anthropic-key/status → {configured}
     (c) mic:       navigator.permissions.query({name:'microphone'}) якщо є,
                     інакше статус "невідомо" доки не натиснуть дію
     (d) telegram:  GET /api/telegram/status → alive && monitored.length>0
*/
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const LS_KEY = 'recall_onboarding_done';

    const onb = {};
    let overlayEl = null;

    function isDone() {
        try { return localStorage.getItem(LS_KEY) === '1'; } catch (_) { return false; }
    }
    function markDone() {
        try { localStorage.setItem(LS_KEY, '1'); } catch (_) { /* private mode / quota — non-fatal, just may re-show */ }
    }

    // Called once from shell.init() on every page load. No-op if the user
    // already finished or skipped (localStorage), or if an overlay is
    // somehow already open (defensive — shouldn't happen this early).
    onb.maybeShowFirstRun = function () {
        if (isDone() || overlayEl) return;
        onb.open();
    };

    // Re-entry point — called from the topbar "graduation cap" icon
    // (#rcOnbTrigger, wired in shell.js) regardless of the done-flag.
    onb.open = function () {
        if (overlayEl) return;
        renderOverlay();
    };

    function close(markAsDone) {
        if (!overlayEl) return;
        document.removeEventListener('keydown', onKeydown, true);
        overlayEl.remove();
        overlayEl = null;
        if (markAsDone) markDone();
    }
    function onKeydown(e) {
        if (e.key === 'Escape') { e.preventDefault(); close(true); }
    }

    // Navigate to an existing screen, optionally scroll a target section
    // into view once it has (synchronously) mounted, then dismiss the
    // overlay. Settings/Telegram render their skeleton markup synchronously
    // inside view.render() before their async loaders resolve, so the
    // target ids already exist right after navigate() returns — a rAF is
    // enough to wait for layout.
    function goTo(path, sectionId) {
        R.router.navigate(path);
        requestAnimationFrame(() => {
            const el = sectionId && document.getElementById(sectionId);
            if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                const card = el.closest('.rc-set') || el;
                card.classList.add('rc-onb-flash');
                setTimeout(() => card.classList.remove('rc-onb-flash'), 1600);
            }
        });
        close(true);
    }

    async function renderOverlay() {
        overlayEl = U.el('div', { class: 'rc-modal-ov' });
        overlayEl.innerHTML = `
            <div class="rc-modal rc-modal--wide rc-onb" role="dialog" aria-modal="true" aria-label="Початок роботи з Recall">
                <div class="rc-modal__h">
                    <i class="fa-solid fa-compass"></i> Початок роботи з Recall
                    <button class="rc-iconbtn rc-onb__x" id="rcOnbClose" type="button" aria-label="Закрити"><i class="fa-solid fa-xmark"></i></button>
                </div>
                <p class="rc-onb__lede">Чотири кроки — і архів готовий до першого дзвінка. Усе опційне й повертається пізніше: іконка <i class="fa-solid fa-graduation-cap"></i> у шапці відкриває це вікно знову.</p>
                <div class="rc-onb__list" id="rcOnbList">${UI.skeletonList(4)}</div>
                <div class="rc-modal__actions">
                    <button class="rc-btn rc-btn--sm" id="rcOnbSkip">Пропустити</button>
                </div>
            </div>`;
        document.body.appendChild(overlayEl);
        document.addEventListener('keydown', onKeydown, true);
        overlayEl.addEventListener('mousedown', (e) => { if (e.target === overlayEl) close(true); });
        overlayEl.querySelector('#rcOnbClose').addEventListener('click', () => close(true));
        overlayEl.querySelector('#rcOnbSkip').addEventListener('click', () => close(true));
        await loadSteps();
    }

    function stepRow(cfg) {
        const s = cfg.status; // 'done' | 'pending' | 'unknown'
        const dot = s === 'done'
            ? '<i class="fa-solid fa-circle-check"></i> Готово'
            : s === 'unknown'
                ? '<i class="fa-regular fa-circle"></i> Невідомо'
                : '<i class="fa-regular fa-circle"></i> Ще ні';
        return `<div class="rc-onb__step" data-step="${cfg.id}">
            <div class="rc-onb__ico"><i class="${cfg.icon}"></i></div>
            <div class="rc-onb__body">
                <div class="rc-onb__title">${U.esc(cfg.title)}${cfg.optional ? ' <span class="rc-onb__opt">· опційно</span>' : ''}</div>
                <div class="rc-onb__desc">${cfg.descHTML}</div>
                <div class="rc-onb__status" data-s="${s}">${dot}</div>
            </div>
            <div class="rc-onb__actions">${cfg.actionsHTML || ''}</div>
        </div>`;
    }

    async function loadSteps() {
        const list = overlayEl && overlayEl.querySelector('#rcOnbList');
        if (!list) return;

        // Kick off all three status checks in parallel — independent, each
        // degrades to "unknown/pending" on its own failure so one dead
        // endpoint doesn't blank the whole checklist.
        const [modelDone, keyDone, tgDone] = await Promise.all([
            checkModelDownloaded(), checkKeyConfigured(), checkTelegramOptedIn(),
        ]);
        if (!overlayEl) return; // closed while awaiting
        const micStatus = await checkMicPermission(); // best-effort, permissions API optional

        // Neutral rc-btn--sm throughout (no accent/primary per row) — this is
        // a parallel checklist, not a linear wizard with one "next" action,
        // and stacking 3-4 amber primary buttons reads as an AI-tell in the
        // editorial-catalog design language. Status (done/pending) is
        // communicated by the ✓ line, not button color.
        list.innerHTML = [
            stepRow({
                id: 'model', icon: 'fa-solid fa-download', title: 'Модель розпізнавання (Whisper)',
                descHTML: 'Обов’язково для транскрибації. Оберіть і завантажте модель у Налаштуваннях — типова <span class="rc-mono">large-v3-turbo</span>.',
                status: modelDone ? 'done' : 'pending',
                actionsHTML: `<button class="rc-btn rc-btn--sm" data-onb-go="model">Перейти до моделей</button>`,
            }),
            stepRow({
                id: 'key', icon: 'fa-solid fa-key', title: 'API-ключ Claude', optional: true,
                descHTML: 'Для покращення тексту, резюме, RAG-чату та інших Claude-фіч. Без ключа архів і транскрибація й далі працюють локально.',
                status: keyDone ? 'done' : 'pending',
                actionsHTML: `<button class="rc-btn rc-btn--sm" data-onb-go="key">${keyDone ? 'Відкрити' : 'Додати ключ'}</button>`,
            }),
            stepRow({
                id: 'mic', icon: 'fa-solid fa-microphone', title: 'Дозвіл на мікрофон',
                descHTML: 'Потрібен для запису дзвінків прямо в браузері. Можна дозволити зараз або пізніше зі сторінки «Запис».',
                status: micStatus,
                actionsHTML: `<button class="rc-btn rc-btn--sm" id="rcOnbMic"${micStatus === 'done' ? ' disabled' : ''}>${micStatus === 'done' ? 'Дозволено' : 'Дати дозвіл'}</button>`,
            }),
            stepRow({
                id: 'telegram', icon: 'fa-brands fa-telegram', title: 'Telegram', optional: true,
                descHTML: 'Опційне підключення власного акаунта (не бот) — повідомлення обраних чатів підуть у той самий архів.',
                status: tgDone ? 'done' : 'pending',
                actionsHTML: `<button class="rc-btn rc-btn--sm" data-onb-go="telegram">${tgDone ? 'Керувати' : 'Підключити'}</button>`,
            }),
        ].join('');

        list.querySelectorAll('[data-onb-go]').forEach(btn => {
            btn.addEventListener('click', () => {
                const step = btn.dataset.onbGo;
                if (step === 'model') goTo('/settings', 'rcSetModels');
                else if (step === 'key') goTo('/settings', 'rcSetAiKey');
                else if (step === 'telegram') goTo('/telegram', 'rcTgStatus');
            });
        });
        const micBtn = list.querySelector('#rcOnbMic');
        if (micBtn) micBtn.addEventListener('click', () => requestMic(micBtn));
    }

    async function checkModelDownloaded() {
        try {
            const data = await R.api.models();
            const list = Array.isArray(data) ? data : (data.models || []);
            return list.some(m => m.downloaded);
        } catch (_) { return false; }
    }
    async function checkKeyConfigured() {
        try { const s = await R.api.get('/api/settings/anthropic-key/status'); return !!(s && s.configured); }
        catch (_) { return false; }
    }
    async function checkTelegramOptedIn() {
        try {
            const s = await R.api.get('/api/telegram/status');
            return !!(s && s.alive && Array.isArray(s.monitored) && s.monitored.length > 0);
        } catch (_) { return false; }
    }
    // Permissions API support for 'microphone' is inconsistent (Firefox
    // rejects the query outright) — degrade to 'unknown' rather than
    // claiming "not granted", since that's not something we can assert.
    async function checkMicPermission() {
        try {
            if (!navigator.permissions || !navigator.permissions.query) return 'unknown';
            const st = await navigator.permissions.query({ name: 'microphone' });
            return st.state === 'granted' ? 'done' : (st.state === 'denied' ? 'pending' : 'pending');
        } catch (_) { return 'unknown'; }
    }

    // Trigger the browser's native mic-permission prompt. We only need the
    // prompt, not an open stream — stop all tracks immediately on success so
    // no mic indicator lingers in the OS/browser chrome.
    async function requestMic(btn) {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            UI.toast('Браузер не підтримує доступ до мікрофона', 'error');
            return;
        }
        btn.disabled = true;
        const old = btn.innerHTML;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Питаю…';
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            stream.getTracks().forEach(t => t.stop());
            btn.textContent = 'Дозволено';
            btn.disabled = true; // stays disabled — granted, nothing more to do
            const row = btn.closest('.rc-onb__step');
            const statusEl = row && row.querySelector('.rc-onb__status');
            if (statusEl) { statusEl.dataset.s = 'done'; statusEl.innerHTML = '<i class="fa-solid fa-circle-check"></i> Готово'; }
            UI.toast('Доступ до мікрофона надано', 'success');
        } catch (err) {
            // Gracefully handle refusal/no-device — never throw to the console
            // as an uncaught rejection, and let the user retry (a re-click
            // re-prompts unless the browser itself remembers the block).
            btn.disabled = false; btn.innerHTML = old;
            const denied = err && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError');
            UI.toast(denied ? 'Доступ до мікрофона відхилено — можна дозволити пізніше в налаштуваннях браузера' : 'Не вдалося отримати доступ до мікрофона', 'error');
        }
    }

    R.onboarding = onb;
})();
