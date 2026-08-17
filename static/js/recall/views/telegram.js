/* Recall — "Додати у архів · Telegram" (Phase 17). Management panel, not an
   ingest-and-navigate flow: shows listener status, the account's dialogs (live
   listener) or saved monitored chats (offline), and lets you toggle monitoring,
   assign a напрямок (category) and backfill history per chat.

   Reads: GET /api/telegram/status, /dialogs (live only), /chats (DB, always).
   Writes: POST /chats (enable + category upsert), /backfill, /api/memory/categories. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const TYPE_BADGE = { user: 'Особистий', group: 'Група', channel: 'Канал', chat: 'Чат' };
    const FILTERS = [
        { key: 'all', label: 'Усі' },
        { key: 'tracked', label: 'Відстежувані' },
        { key: 'marked', label: 'З напрямком' },
        { key: 'unmarked', label: 'Без розмітки' },
    ];

    let dialogs = [];      // [{ id, title, type, username, monitored, category_id }]
    let filter = 'all';
    let search = '';
    let cats = null;

    async function render(ctx) {
        cats = await UI.loadCategories();
        dialogs = []; filter = 'all'; search = '';
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-brands fa-telegram" style="color:var(--rc-src-telegram)"></i> Додати у архів · Telegram</div>
                <div class="rc-pagehead__row">
                    <h1 class="rc-pagehead__title">Telegram</h1>
                    <div style="display:flex;gap:8px">
                        <button class="rc-btn rc-btn--sm" id="rcTgNewCat"><i class="fa-solid fa-plus"></i> Новий напрямок</button>
                        <button class="rc-btn rc-btn--sm" id="rcTgReload"><i class="fa-solid fa-rotate"></i> Оновити список</button>
                    </div>
                </div>
                <p class="rc-pagehead__lede">Оберіть чати для архівації — текст, голосові, фото, відео й документи підуть у той самий семантичний пошук.</p>
            </div>
            <div id="rcTgStatus" class="rc-tgstatus"></div>
            <div class="rc-tginfo" style="display:flex;gap:10px;align-items:flex-start;border:1px solid var(--rc-hairline);border-radius:var(--rc-r-2);padding:var(--rc-3) var(--rc-4);margin-bottom:var(--rc-4);font-size:var(--rc-t-sm);line-height:1.55;background:var(--rc-surface-2)">
                <i class="fa-solid fa-circle-info" style="color:var(--rc-warn);margin-top:2px;flex:none"></i>
                <div>
                    <strong>Що це технічно означає.</strong> Recall підключається до вашого <u>реального</u> Telegram-акаунта
                    протоколом MTProto (бібліотека Telethon) — <strong>не через бота</strong>. Слухач бачить усе, що бачите ви
                    у вибраних нижче чатах (текст, голосові, фото, відео, документи), і надсилає це у локальний архів.
                    <br>
                    <strong>Ризик.</strong> Telegram офіційно не підтримує автоматизовані клієнти на звичайному акаунті;
                    надто активне читання історії (backfill) великими партіями теоретично може призвести до тимчасового
                    обмеження акаунта. Recall притримує швидкість між повідомленнями, але ризик не нульовий — почніть
                    з невеликого обсягу backfill і некритичного чату.
                </div>
            </div>
            <div id="rcTgNewCatBar"></div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcTgSearch" type="search" placeholder="Пошук чату за назвою…">
                </div>
            </div>
            <div class="rc-filterbar" id="rcTgFilters"></div>
            <div id="rcTgList"></div>`;

        ctx.mount.querySelector('#rcTgSearch').addEventListener('input',
            U.debounce((e) => { search = e.target.value.trim().toLowerCase(); paintList(ctx); }, 200));
        ctx.mount.querySelector('#rcTgReload').addEventListener('click', () => load(ctx));
        ctx.mount.querySelector('#rcTgNewCat').addEventListener('click', () => toggleNewCat(ctx));

        load(ctx);
    }

    async function load(ctx) {
        const listEl = ctx.mount.querySelector('#rcTgList');
        if (listEl) listEl.innerHTML = UI.skeletonList(6);

        let status = null;
        try { status = await R.api.get('/api/telegram/status'); } catch (_) {}
        if (!ctx.isCurrent()) return;
        renderStatus(ctx, status);
        const alive = !!(status && status.alive);

        let dl = null;
        if (alive) {
            try { const d = await R.api.get('/api/telegram/dialogs'); if (d && d.success) dl = d.dialogs || []; }
            catch (_) { /* listener busy/offline mid-call */ }
        }
        let saved = {};
        try { const cd = await R.api.get('/api/telegram/chats'); for (const c of (cd.chats || [])) saved[c.chat_id] = c; }
        catch (_) {}

        if (dl === null) {
            dialogs = Object.values(saved).map(c => ({
                id: c.chat_id, title: c.title, type: c.chat_type,
                username: c.username, monitored: !!c.enabled, category_id: c.category_id,
            }));
        } else {
            for (const d of dl) { const s = saved[d.id]; if (s) { d.category_id = s.category_id; d.monitored = !!s.enabled; } }
            dialogs = dl;
        }
        if (!ctx.isCurrent()) return;
        paintList(ctx);
    }

    function renderStatus(ctx, s) {
        const box = ctx.mount.querySelector('#rcTgStatus');
        if (!box) return;
        if (s && s.alive) {
            const who = s.me ? U.esc(s.me.name || '') : '';
            box.className = 'rc-tgstatus is-online';
            box.innerHTML = `<i class="fa-solid fa-circle-check"></i> Слухач активний${who ? ' — ' + who : ''} · моніторю ${(s.monitored || []).length} чат(ів)`;
        } else {
            box.className = 'rc-tgstatus is-offline';
            box.innerHTML = `<div style="display:flex;flex-direction:column;gap:6px;width:100%">
                <div><i class="fa-solid fa-circle-xmark"></i> Слухач вимкнений — показано лише збережені чати.</div>
                <details style="font-size:var(--rc-t-xs);color:var(--rc-ink-2)">
                    <summary style="cursor:pointer;color:var(--rc-accent-deep)">Як підключити слухач Telegram</summary>
                    <ol style="margin:6px 0 0 18px;padding:0;line-height:1.7">
                        <li>Отримайте <code>api_id</code> і <code>api_hash</code> на
                            <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a>
                            (розділ «API development tools») і додайте їх у <code>.env</code> як
                            <code>TELEGRAM_API_ID</code> / <code>TELEGRAM_API_HASH</code>.</li>
                        <li>Один раз виконайте в консолі проєкту
                            <code>.venv/Scripts/python.exe telegram_login.py</code> — введіть номер телефону і код
                            підтвердження з Telegram; це створить файл сесії.</li>
                        <li>Перезапустіть застосунок (<code>app.py</code>) — він сам підніме слухача автоматично, і
                            тут з'явиться повний список діалогів.</li>
                    </ol>
                </details>
            </div>`;
        }
    }

    function counts() {
        return {
            all: dialogs.length,
            tracked: dialogs.filter(d => d.monitored).length,
            marked: dialogs.filter(d => d.monitored && d.category_id != null).length,
            unmarked: dialogs.filter(d => !d.monitored || d.category_id == null).length,
        };
    }
    function matchFilter(d) {
        if (filter === 'tracked') return !!d.monitored;
        if (filter === 'marked') return !!d.monitored && d.category_id != null;
        if (filter === 'unmarked') return !d.monitored || d.category_id == null;
        return true;
    }

    function paintList(ctx) {
        const fb = ctx.mount.querySelector('#rcTgFilters');
        const c = counts();
        if (fb) {
            fb.innerHTML = FILTERS.map(f =>
                `<button class="rc-chip${filter === f.key ? ' is-active' : ''}" data-filter="${f.key}">${f.label} <span class="rc-chip__n">${c[f.key]}</span></button>`).join('');
            fb.querySelectorAll('.rc-chip').forEach(b => b.addEventListener('click', () => { filter = b.dataset.filter; paintList(ctx); }));
        }
        const listEl = ctx.mount.querySelector('#rcTgList');
        if (!listEl) return;
        let shown = dialogs.filter(matchFilter);
        if (search) shown = shown.filter(d => (d.title || '').toLowerCase().includes(search) || String(d.id).includes(search));
        shown.sort((a, b) => (b.monitored - a.monitored) || (a.title || '').localeCompare(b.title || ''));
        if (!shown.length) { listEl.innerHTML = UI.empty('Немає чатів', 'Спробуйте інший фільтр або оновіть список.', 'fa-comment-slash'); return; }
        listEl.innerHTML = `<div class="rc-tglist">` + shown.map(rowHTML).join('') + `</div>`;
        bindRows(ctx, listEl);
    }

    function catOptions(sel) {
        return UI.catOptions(sel, { first: 'none' });
    }

    function rowHTML(d) {
        const badge = TYPE_BADGE[d.type] || d.type || '';
        return `<div class="rc-tgrow-wrap" data-wrap="${d.id}">
            <div class="rc-tgrow" data-id="${d.id}">
                <label class="rc-tgrow__check">
                    <input type="checkbox" ${d.monitored ? 'checked' : ''} data-act="toggle">
                    <span class="rc-tgrow__title">${U.esc(d.title || ('chat ' + d.id))}</span>
                    ${badge ? `<span class="rc-tgrow__badge">${U.esc(badge)}</span>` : ''}
                </label>
                <div class="rc-tgrow__ctrl">
                    <select class="rc-select rc-select--sm rc-catsel" data-cat-first="none" data-act="category" aria-label="Напрямок чату">${catOptions(d.category_id)}</select>
                    <button class="rc-iconbtn" data-act="backfill" title="Догрузити історію чату"><i class="fa-solid fa-clock-rotate-left"></i></button>
                </div>
            </div>
            <div class="rc-tgbf" data-bf="${d.id}" style="display:none"></div>
        </div>`;
    }

    // Обсяг backfill'у обирає користувач (замість фіксованого limit=200 мовчки).
    // Fire-and-forget на бекенді (Phase 17D) не має progress-API — тож чесно
    // показуємо: скільки запросили, що це фоновий процес, і посилання на
    // Архів (відфільтрований по цьому чату), де видно нові записи по мірі появи.
    function backfillPanelHTML(d) {
        return `<div style="display:flex;align-items:center;gap:10px;padding:8px 0;font-size:var(--rc-t-xs);color:var(--rc-ink-2)">
            <label style="display:flex;align-items:center;gap:6px">Обсяг:
                <input type="number" class="rc-input rc-select--sm" style="width:90px" min="20" max="2000" step="20" value="200" data-bf-limit>
                повідомлень
            </label>
            <button class="rc-btn rc-btn--sm rc-btn--primary" data-bf-start>Почати догрузку</button>
            <button class="rc-btn rc-btn--sm" data-bf-cancel>Скасувати</button>
            <span data-bf-status></span>
        </div>`;
    }

    function toggleBackfillPanel(ctx, wrap, d) {
        const panel = wrap.querySelector('[data-bf]');
        if (!panel) return;
        if (panel.style.display !== 'none') { panel.style.display = 'none'; panel.innerHTML = ''; return; }
        panel.style.display = 'block';
        panel.innerHTML = backfillPanelHTML(d);
        panel.querySelector('[data-bf-cancel]').addEventListener('click', () => { panel.style.display = 'none'; panel.innerHTML = ''; });
        panel.querySelector('[data-bf-start]').addEventListener('click', async () => {
            const limitInput = panel.querySelector('[data-bf-limit]');
            const limit = Math.max(20, Math.min(2000, Number(limitInput.value) || 200));
            const startBtn = panel.querySelector('[data-bf-start]');
            const statusEl = panel.querySelector('[data-bf-status]');
            startBtn.disabled = true;
            statusEl.textContent = 'Запускаю…';
            try {
                await R.api.post('/api/telegram/backfill', { chat_id: d.id, limit });
                const libHref = `/library?source_type=telegram&tg_chat=${encodeURIComponent(d.title || '')}`;
                panel.innerHTML = `<div style="padding:4px 0;line-height:1.6">
                    <i class="fa-solid fa-circle-check" style="color:var(--rc-ok)"></i>
                    Запущено — до ${limit} повідомлень. Це фоновий процес, може тривати кілька хвилин; повідомлення
                    з'являтимуться в архіві поступово. <a href="${libHref}">Перевірити прогрес у Бібліотеці</a>.
                </div>`;
                UI.toast('Догрузку історії запущено', 'success');
            } catch (e) {
                startBtn.disabled = false;
                statusEl.textContent = '';
                UI.toast(e.message || 'Догрузка недоступна (потрібен слухач)', 'info');
            }
        });
    }

    function bindRows(ctx, listEl) {
        listEl.querySelectorAll('.rc-tgrow-wrap').forEach(wrap => {
            const row = wrap.querySelector('.rc-tgrow');
            const id = Number(row.dataset.id);
            const d = dialogs.find(x => x.id === id);
            if (!d) return;
            const chk = row.querySelector('[data-act="toggle"]');
            const sel = row.querySelector('[data-act="category"]');
            const bf = row.querySelector('[data-act="backfill"]');

            chk.addEventListener('change', async () => {
                try {
                    await R.api.post('/api/telegram/chats', {
                        chat_id: id, enabled: chk.checked,
                        title: d.title || null, username: d.username || null, chat_type: d.type || null,
                    });
                    d.monitored = chk.checked;
                    UI.toast(chk.checked ? 'Чат увімкнено для слухання' : 'Чат вимкнено', 'success');
                    paintCounts(ctx);
                } catch (e) { UI.toast(e.message, 'error'); chk.checked = !chk.checked; }
            });
            sel.addEventListener('change', async () => {
                try {
                    await R.api.post('/api/telegram/chats', { chat_id: id, category_id: sel.value === '' ? null : Number(sel.value) });
                    d.category_id = sel.value === '' ? null : Number(sel.value);
                    UI.toast('Напрямок чату збережено', 'success');
                    paintCounts(ctx);
                } catch (e) { UI.toast(e.message, 'error'); }
            });
            bf.addEventListener('click', () => toggleBackfillPanel(ctx, wrap, d));
        });
    }

    function paintCounts(ctx) {
        const fb = ctx.mount.querySelector('#rcTgFilters');
        if (!fb) return;
        const c = counts();
        FILTERS.forEach(f => { const el = fb.querySelector(`[data-filter="${f.key}"] .rc-chip__n`); if (el) el.textContent = c[f.key]; });
    }

    function toggleNewCat(ctx) {
        const bar = ctx.mount.querySelector('#rcTgNewCatBar');
        if (!bar) return;
        if (bar.innerHTML) { bar.innerHTML = ''; return; }
        bar.innerHTML = `<div class="rc-newcat">
            <input class="rc-input" id="rcTgCatName" placeholder="Назва нового напрямку…" autocomplete="off">
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcTgCatSave">Створити</button>
            <button class="rc-btn rc-btn--sm" id="rcTgCatCancel">Скасувати</button>
        </div>`;
        const input = bar.querySelector('#rcTgCatName');
        input.focus();
        const close = () => { bar.innerHTML = ''; };
        const save = async () => {
            const name = input.value.trim();
            if (!name) return;
            try {
                await R.api.post('/api/memory/categories', { name });
                R.state.categories = null;            // bust shared cache
                cats = await UI.loadCategories();
                close(); paintList(ctx);
                UI.toast(`Напрямок «${name}» створено`, 'success');
            } catch (e) { UI.toast(e.message || 'Не вдалось створити напрямок', 'error'); }
        };
        bar.querySelector('#rcTgCatSave').addEventListener('click', save);
        bar.querySelector('#rcTgCatCancel').addEventListener('click', close);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); save(); }
            else if (e.key === 'Escape') { close(); }
        });
    }

    R.views.telegram = { render, destroy() { dialogs = []; filter = 'all'; search = ''; } };
})();
