/* Recall — ⌘K command palette. Jump to sections, ask the archive,
   and live-search transcripts by name/id. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util;
    const cmdk = {};
    let overlay = null, input = null, list = null, items = [], active = 0;
    let searchTimer = null;
    const SEARCH_DEBOUNCE_MS = 220;

    const NAV = [
        { group: 'Розділи', icon: 'fa-house', label: 'Головна', path: '/' },
        { group: 'Розділи', icon: 'fa-layer-group', label: 'Бібліотека', path: '/library' },
        { group: 'Розділи', icon: 'fa-compact-disc', label: 'Медіатека', path: '/audio' },
        { group: 'Розділи', icon: 'fa-comments', label: 'Запитай архів', path: '/ask' },
        { group: 'Розділи', icon: 'fa-magnifying-glass-chart', label: 'Дослідження бренду', path: '/research' },
        { group: 'Розділи', icon: 'fa-diagram-project', label: 'Сутності', path: '/entities' },
        { group: 'Розділи', icon: 'fa-list-check', label: 'Задачі', path: '/tasks' },
        { group: 'Розділи', icon: 'fa-users', label: 'Спікери', path: '/speakers' },
        { group: 'Додати', icon: 'fa-arrow-up-from-bracket', label: 'Завантажити файл', path: '/upload' },
        { group: 'Додати', icon: 'fa-youtube', label: 'YouTube', path: '/youtube' },
        { group: 'Додати', icon: 'fa-file-lines', label: 'Документ', path: '/documents' },
        { group: 'Додати', icon: 'fa-telegram', label: 'Telegram', path: '/telegram' },
        { group: 'Додати', icon: 'fa-microphone', label: 'Запис', path: '/record' },
        { group: 'Сервіс', icon: 'fa-sliders', label: 'Налаштування', path: '/settings' },
    ];

    cmdk.open = function () {
        if (overlay) return;
        overlay = U.el('div', { class: 'rc-cmdk-overlay' });
        overlay.innerHTML = `
            <div class="rc-cmdk" role="dialog" aria-label="Команди">
                <input class="rc-cmdk__input" type="text" placeholder="Спитати архів, перейти, знайти запис…" autocomplete="off" spellcheck="false">
                <div class="rc-cmdk__list"></div>
            </div>`;
        document.body.appendChild(overlay);
        input = overlay.querySelector('.rc-cmdk__input');
        list = overlay.querySelector('.rc-cmdk__list');
        overlay.addEventListener('click', (e) => { if (e.target === overlay) cmdk.close(); });
        input.addEventListener('input', onInput);
        input.addEventListener('keydown', onKey);
        document.addEventListener('keydown', onEsc, true);
        renderItems(NAV, '');
        input.focus();
    };

    cmdk.close = function () {
        if (!overlay) return;
        document.removeEventListener('keydown', onEsc, true);
        overlay.remove(); overlay = input = list = null; items = []; active = 0;
    };

    function onEsc(e) { if (e.key === 'Escape') { e.stopPropagation(); cmdk.close(); } }

    function onInput() {
        const q = input.value.trim();
        clearTimeout(searchTimer);
        if (!q) { renderItems(NAV, ''); return; }
        // local nav filter immediately
        const navMatches = NAV.filter(n => n.label.toLowerCase().includes(q.toLowerCase()));
        renderItems(navMatches, q, true);
        // debounced transcript search
        searchTimer = setTimeout(async () => {
            try {
                const data = await R.api.history({ search: q, per_page: 6 });
                const recs = (data.transcriptions || []).map(t => ({
                    group: 'Записи', icon: U.SRC_ICON[t.source_type] || 'fa-file',
                    label: t.display_name || t.source_name || ('Запис #' + t.id),
                    badge: '#' + t.id,
                    path: '/transcript/' + U.slug(t.id, t.display_name || t.source_name),
                }));
                // also offer "ask the archive: q"
                const ask = { group: 'Дія', icon: 'fa-comments', label: 'Запитати архів: «' + q + '»',
                    path: '/ask?q=' + encodeURIComponent(q) };
                renderItems(navMatches.concat([ask], recs), q, true);
            } catch (_) {}
        }, SEARCH_DEBOUNCE_MS);
    }

    function renderItems(arr, q, keepFocus) {
        items = arr; active = 0;
        if (!arr.length) { list.innerHTML = `<div class="rc-cmdk__group">Нічого не знайдено</div>`; return; }
        let html = '', lastGroup = null;
        arr.forEach((it, i) => {
            if (it.group !== lastGroup) { html += `<div class="rc-cmdk__group">${U.esc(it.group)}</div>`; lastGroup = it.group; }
            const fa = it.icon.startsWith('fa-brands') ? it.icon : (it.icon.indexOf('fa-') === 0 ? 'fa-solid ' + it.icon : it.icon);
            html += `<div class="rc-cmdk__item${i === 0 ? ' is-active' : ''}" data-i="${i}">
                <i class="rc-ico ${fa.includes('youtube') || fa.includes('telegram') ? 'fa-brands ' + it.icon : fa}"></i>
                <span>${U.esc(it.label)}</span>
                ${it.badge ? `<span class="rc-mono">${U.esc(it.badge)}</span>` : ''}
            </div>`;
        });
        list.innerHTML = html;
        list.querySelectorAll('.rc-cmdk__item').forEach(el => {
            el.addEventListener('click', () => choose(parseInt(el.dataset.i, 10)));
        });
    }

    function onKey(e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
        else if (e.key === 'Enter') { e.preventDefault(); choose(active); }
    }
    function move(d) {
        const els = list.querySelectorAll('.rc-cmdk__item');
        if (!els.length) return;
        els[active]?.classList.remove('is-active');
        active = (active + d + els.length) % els.length;
        els[active].classList.add('is-active');
        els[active].scrollIntoView({ block: 'nearest' });
    }
    function choose(i) {
        const it = items[i]; if (!it) return;
        cmdk.close();
        if (it.native) { location.href = it.path; }
        else { R.router.navigate(it.path); }
    }

    R.cmdk = cmdk;
})();
