/* Recall — "Додати у архів · Telegram" (Phase 17). Management panel, not an
   ingest-and-navigate flow: shows listener status, the account's dialogs (live
   listener) or saved monitored chats (offline), and lets you toggle monitoring,
   assign a category (напрямок) and backfill history per chat.

   Reads: GET /api/telegram/status, /dialogs (live only), /chats (DB, always).
   Writes: POST /chats (enable + category upsert), /backfill, /api/memory/categories. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const TYPE_BADGE = { user: 'telegram.type.user', group: 'telegram.type.group', channel: 'telegram.type.channel', chat: 'telegram.type.chat' };
    const FILTERS = [
        { key: 'all', label: 'telegram.filter.all' },
        { key: 'tracked', label: 'telegram.filter.tracked' },
        { key: 'marked', label: 'telegram.filter.marked' },
        { key: 'unmarked', label: 'telegram.filter.unmarked' },
    ];

    // [{ id, title, type, username, first_name, last_name, phone, bot, deleted,
    //    monitored, category_id }] — first_name…deleted is provided only by the live listener
    // of the new version; the old listener and the offline list (from the DB) do not have them.
    let dialogs = [];
    let filter = 'all';
    let query = null;      // parseQuery(...) or null when the search is empty
    let live = false;      // the list came from the live listener (not from the DB)
    let cats = null;

    async function render(ctx) {
        cats = await UI.loadCategories();
        dialogs = []; filter = 'all'; query = null; live = false;
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-brands fa-telegram" style="color:var(--rc-src-telegram)"></i> ${R.t('telegram.eyebrow')}</div>
                <div class="rc-pagehead__row">
                    <h1 class="rc-pagehead__title">Telegram</h1>
                    <div style="display:flex;gap:8px">
                        <button class="rc-btn rc-btn--sm" id="rcTgNewCat"><i class="fa-solid fa-plus"></i> ${R.t('telegram.newCategory')}</button>
                        <button class="rc-btn rc-btn--sm" id="rcTgReload"><i class="fa-solid fa-rotate"></i> ${R.t('telegram.reload')}</button>
                    </div>
                </div>
                <p class="rc-pagehead__lede">${R.t('telegram.lede')}</p>
            </div>
            <div id="rcTgStatus" class="rc-tgstatus"></div>
            <div class="rc-tginfo" style="display:flex;gap:10px;align-items:flex-start;border:1px solid var(--rc-hairline);border-radius:var(--rc-r-2);padding:var(--rc-3) var(--rc-4);margin-bottom:var(--rc-4);font-size:var(--rc-t-sm);line-height:1.55;background:var(--rc-surface-2)">
                <i class="fa-solid fa-circle-info" style="color:var(--rc-warn);margin-top:2px;flex:none"></i>
                <div>
                    ${R.t('telegram.info.what')}
                    <br>
                    ${R.t('telegram.info.risk')}
                </div>
            </div>
            <div id="rcTgNewCatBar"></div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcTgSearch" type="search" autocomplete="off"
                        placeholder="${R.t('telegram.search.placeholder')}" aria-label="${R.t('telegram.search.aria')}">
                </div>
            </div>
            <div class="rc-filterbar" id="rcTgFilters"></div>
            <div id="rcTgList"></div>`;

        ctx.mount.querySelector('#rcTgSearch').addEventListener('input',
            U.debounce((e) => { query = parseQuery(e.target.value); paintList(ctx); }, 200));
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
        live = dl !== null;
        if (!ctx.isCurrent()) return;
        paintList(ctx);
    }

    function renderStatus(ctx, s) {
        const box = ctx.mount.querySelector('#rcTgStatus');
        if (!box) return;
        if (s && s.alive) {
            const who = s.me ? U.esc(s.me.name || '') : '';
            box.className = 'rc-tgstatus is-online';
            box.innerHTML = `<i class="fa-solid fa-circle-check"></i> ${R.t('telegram.status.online', { who: who ? ' — ' + who : '', count: (s.monitored || []).length })}`;
        } else {
            box.className = 'rc-tgstatus is-offline';
            box.innerHTML = `<div style="display:flex;flex-direction:column;gap:6px;width:100%">
                <div><i class="fa-solid fa-circle-xmark"></i> ${R.t('telegram.status.offline')}</div>
                <details style="font-size:var(--rc-t-xs);color:var(--rc-ink-2)">
                    <summary style="cursor:pointer;color:var(--rc-accent-deep)">${R.t('telegram.setup.title')}</summary>
                    <ol style="margin:6px 0 0 18px;padding:0;line-height:1.7">
                        <li>${R.t('telegram.setup.step1')}</li>
                        <li>${R.t('telegram.setup.step2')}</li>
                        <li>${R.t('telegram.setup.step3')}</li>
                    </ol>
                </details>
            </div>`;
        }
    }

    // ---- Search: name / @nick / phone ---------------------------------------
    // Names in Telegram are written however people like: the same person can be «Olena
    // Koval» and «Олена Коваль». So besides a direct substring match, the name
    // is also compared by a "skeleton": Latin after transliteration, where j/y→i,
    // g→h, kh→h and doubled letters are merged (Олена = Olena = Elena).
    const CYR = {
        а: 'a', б: 'b', в: 'v', г: 'h', ґ: 'g', д: 'd', е: 'e', є: 'ie', ё: 'e', ж: 'zh', з: 'z',
        и: 'y', і: 'i', ї: 'i', й: 'i', к: 'k', л: 'l', м: 'm', н: 'n', о: 'o', п: 'p', р: 'r',
        с: 's', т: 't', у: 'u', ф: 'f', х: 'kh', ц: 'ts', ч: 'ch', ш: 'sh', щ: 'shch', ъ: '',
        ы: 'y', ь: '', э: 'e', ю: 'iu', я: 'ia',
    };
    // toLowerCase in JS folds Cyrillic correctly (unlike SQLite LOWER);
    // we strip apostrophes (', ’, ʼ) so that «Мар'яна» = «Марʼяна».
    function fold(s) {
        return String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/['’ʼ`]/g, '')
            .replace(/\s+/g, ' ').trim();
    }
    function skeleton(s) {
        let out = '';
        for (const ch of fold(s)) out += CYR[ch] !== undefined ? CYR[ch] : ch;
        return out.replace(/x/g, 'ks').replace(/w/g, 'v').replace(/kh/g, 'h').replace(/g/g, 'h')
            .replace(/[jy]/g, 'i').replace(/[^a-z0-9 ]/g, '').replace(/([a-z])\1+/g, '$1');
    }
    const digits = (s) => String(s == null ? '' : s).replace(/\D/g, '');

    // A phone from Telethon — digits without "+" (380671234567). We compare the query by
    // digits: «+380 67 123-45-67», «(067) 123 45 67», «0671234567» — one number.
    // A local number with a trunk 0/8 matches the tail of the international one.
    function phoneHit(phone, qd) {
        if (phone.includes(qd)) return true;
        return qd.length >= 9 && /^[08]/.test(qd) && phone.endsWith(qd.slice(1));
    }
    function fmtPhone(p) {
        const s = digits(p);
        if (!s) return '';
        const m = /^380(\d{2})(\d{3})(\d{2})(\d{2})$/.exec(s);
        return m ? `+380 ${m[1]} ${m[2]} ${m[3]} ${m[4]}` : '+' + s;
    }

    function parseQuery(raw) {
        const q = String(raw || '').trim();
        if (!q) return null;
        const atNick = q.startsWith('@');
        const text = fold(atNick ? q.slice(1) : q);
        const compact = q.replace(/\s/g, '');
        const skel = skeleton(text).split(' ').filter(Boolean);
        return {
            raw: q, atNick,
            tokens: text.split(' ').filter(Boolean),
            // a skeleton of 1–2 letters («й», «y») matches almost everything — just noise
            skel: skel.join('').length >= 3 ? skel : [],
            phone: /^[+\d\s\-().]+$/.test(q) && digits(q).length >= 3 ? digits(q) : '',
            id: /^-?\d+$/.test(compact) ? compact : '',
        };
    }

    function searchIndex(d) {
        const names = [d.title, d.first_name, d.last_name].filter(Boolean).join(' ');
        return {
            name: fold(names),
            nick: fold(d.username),
            skel: skeleton(names + ' ' + (d.username || '')),
            phone: digits(d.phone),
        };
    }

    // → null (does not fit) or { rank, by }. rank orders the results (lower —
    // higher), by — which field matched: we highlight it in the row, because a match by
    // @nick or phone is not visible from the title.
    function matchDialog(d, q) {
        const ix = d._ix || (d._ix = searchIndex(d));
        if (q.id && String(d.id) === q.id) return { rank: 0, by: 'id' };
        if (q.phone && ix.phone && phoneHit(ix.phone, q.phone)) return { rank: 1, by: 'phone' };
        if (q.atNick) {
            const n = q.tokens.join('');
            if (!ix.nick || !ix.nick.includes(n)) return null;
            return { rank: ix.nick.startsWith(n) ? 0 : 1, by: 'nick' };
        }
        if (!q.tokens.length) return null;
        if (q.tokens.every(t => ix.name.includes(t))) {
            return { rank: ix.name.startsWith(q.tokens[0]) || ix.name.includes(' ' + q.tokens[0]) ? 0 : 1, by: 'name' };
        }
        if (q.tokens.every(t => ix.name.includes(t) || ix.nick.includes(t))) return { rank: 2, by: 'nick' };
        if (q.skel.length && q.skel.every(t => ix.skel.includes(t))) return { rank: 3, by: 'name' };
        return null;
    }

    // The search narrows the list BEFORE the filters: the counters on the chips show how many
    // were found in each filter, so «у Відстежуваних 0» is visible at once.
    function searched() {
        if (!query) return dialogs.map(d => ({ d, hit: null }));
        const out = [];
        for (const d of dialogs) { const hit = matchDialog(d, query); if (hit) out.push({ d, hit }); }
        return out;
    }

    function counts(items) {
        return {
            all: items.length,
            tracked: items.filter(x => x.d.monitored).length,
            marked: items.filter(x => x.d.monitored && x.d.category_id != null).length,
            unmarked: items.filter(x => !x.d.monitored || x.d.category_id == null).length,
        };
    }
    function matchFilter(d) {
        if (filter === 'tracked') return !!d.monitored;
        if (filter === 'marked') return !!d.monitored && d.category_id != null;
        if (filter === 'unmarked') return !d.monitored || d.category_id == null;
        return true;
    }

    function paintList(ctx) {
        const items = searched();
        const fb = ctx.mount.querySelector('#rcTgFilters');
        const c = counts(items);
        if (fb) {
            fb.innerHTML = FILTERS.map(f =>
                `<button class="rc-chip${filter === f.key ? ' is-active' : ''}" data-filter="${f.key}">${R.t(f.label)} <span class="rc-chip__n">${c[f.key]}</span></button>`).join('');
            fb.querySelectorAll('.rc-chip').forEach(b => b.addEventListener('click', () => { filter = b.dataset.filter; paintList(ctx); }));
        }
        const listEl = ctx.mount.querySelector('#rcTgList');
        if (!listEl) return;
        const shown = items.filter(x => matchFilter(x.d));
        shown.sort((a, b) => ((a.hit ? a.hit.rank : 0) - (b.hit ? b.hit.rank : 0))
            || (b.d.monitored - a.d.monitored)
            || R.i18n.compare(displayTitle(a.d), displayTitle(b.d)));
        if (!shown.length) { listEl.innerHTML = emptyHTML(); return; }
        listEl.innerHTML = `<div class="rc-tglist">` + shown.map(x => rowHTML(x.d, x.hit)).join('') + `</div>`;
        bindRows(ctx, listEl);
    }

    function emptyHTML() {
        if (!query) return UI.empty(R.t('telegram.empty.title'), R.t('telegram.empty.sub'), 'fa-comment-slash');
        let sub = R.t('telegram.empty.searchHint');
        if (query.phone && !dialogs.some(d => d.phone !== undefined)) {
            sub = live
                ? R.t('telegram.empty.noPhonesLive')
                : R.t('telegram.empty.noPhonesOffline');
        }
        return UI.empty(R.t('telegram.empty.notFound', { query: query.raw }), sub, 'fa-magnifying-glass');
    }

    function catOptions(sel) {
        return UI.catOptions(sel, { first: 'none' });
    }

    function displayTitle(d) {
        if (d.title) return d.title;
        if (d.deleted) return R.t('telegram.deletedAccount');
        if (d.username) return '@' + d.username;
        return R.t('telegram.untitled');
    }
    function typeLabel(d) {
        if (d.bot) return R.t('telegram.type.bot');
        // a deleted account comes without a name, and _chat_type_of sees "chat" in it
        if (d.deleted) return R.t('telegram.type.user');
        return TYPE_BADGE[d.type] ? R.t(TYPE_BADGE[d.type]) : (d.type || '');
    }

    function rowHTML(d, hit) {
        const by = hit ? hit.by : '';
        const cls = (k) => (by === k ? ' is-hit' : '');
        const title = displayTitle(d);
        const badge = typeLabel(d);
        const meta = [
            badge ? `<span class="rc-tgrow__badge">${U.esc(badge)}</span>` : '',
            d.username ? `<span class="rc-tgrow__nick${cls('nick')}">@${U.esc(d.username)}</span>` : '',
            d.phone ? `<span class="rc-tgrow__phone${cls('phone')}">${U.esc(fmtPhone(d.phone))}</span>` : '',
            // without a title and nick the only thing that distinguishes the row is the id
            (!d.title && !d.username) || by === 'id' ? `<span class="rc-tgrow__id${cls('id')}">id ${U.esc(String(d.id))}</span>` : '',
        ].join('');
        return `<div class="rc-tgrow-wrap" data-wrap="${d.id}">
            <div class="rc-tgrow" data-id="${d.id}">
                <label class="rc-tgrow__check">
                    <input type="checkbox" ${d.monitored ? 'checked' : ''} data-act="toggle" aria-label="${U.esc(R.t('telegram.row.listen', { title }))}">
                    <span class="rc-tgrow__who">
                        <span class="rc-tgrow__title${d.title ? '' : ' is-untitled'}" title="${U.esc(title)}">${U.esc(title)}</span>
                        <span class="rc-tgrow__meta">${meta}</span>
                    </span>
                </label>
                <div class="rc-tgrow__ctrl">
                    <select class="rc-select rc-select--sm rc-catsel" data-cat-first="none" data-act="category" aria-label="${R.t('telegram.row.category')}">${catOptions(d.category_id)}</select>
                    <button class="rc-iconbtn" data-act="backfill" title="${R.t('telegram.row.backfill')}" aria-label="${R.t('telegram.row.backfill')}"><i class="fa-solid fa-clock-rotate-left"></i></button>
                </div>
            </div>
            <div class="rc-tgbf" data-bf="${d.id}" style="display:none"></div>
        </div>`;
    }

    // The backfill volume is chosen by the user (instead of a silently fixed limit=200).
    // Fire-and-forget on the backend (Phase 17D) has no progress API — so we honestly
    // show: how many were requested, that this is a background process, and a link to
    // the Archive (filtered by this chat), where new records are visible as they appear.
    function backfillPanelHTML(d) {
        return `<div style="display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:8px 0;font-size:var(--rc-t-xs);color:var(--rc-ink-2)">
            <label style="display:flex;align-items:center;gap:6px">${R.t('telegram.bf.volume')}
                <input type="number" class="rc-input rc-select--sm" style="width:90px" min="20" max="2000" step="20" value="200" data-bf-limit>
                ${R.t('telegram.bf.messages')}
            </label>
            <button class="rc-btn rc-btn--sm rc-btn--primary" data-bf-start>${R.t('telegram.bf.start')}</button>
            <button class="rc-btn rc-btn--sm" data-bf-cancel>${R.t('core.cancel')}</button>
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
            statusEl.textContent = R.t('telegram.bf.starting');
            try {
                await R.api.post('/api/telegram/backfill', { chat_id: d.id, limit });
                const libHref = `/library?source_type=telegram&tg_chat=${encodeURIComponent(d.title || '')}`;
                panel.innerHTML = `<div style="padding:4px 0;line-height:1.6">
                    <i class="fa-solid fa-circle-check" style="color:var(--rc-ok)"></i>
                    ${R.t('telegram.bf.started', { limit })} <a href="${libHref}">${R.t('telegram.bf.checkLibrary')}</a>.
                </div>`;
                UI.toast(R.t('telegram.bf.toast'), 'success');
            } catch (e) {
                startBtn.disabled = false;
                statusEl.textContent = '';
                UI.toast(e.message || R.t('telegram.bf.unavailable'), 'info');
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
                    UI.toast(chk.checked ? R.t('telegram.toast.enabled') : R.t('telegram.toast.disabled'), 'success');
                    paintCounts(ctx);
                } catch (e) { UI.toast(e.message, 'error'); chk.checked = !chk.checked; }
            });
            sel.addEventListener('change', async () => {
                try {
                    await R.api.post('/api/telegram/chats', { chat_id: id, category_id: sel.value === '' ? null : Number(sel.value) });
                    d.category_id = sel.value === '' ? null : Number(sel.value);
                    UI.toast(R.t('telegram.toast.categorySaved'), 'success');
                    paintCounts(ctx);
                } catch (e) { UI.toast(e.message, 'error'); }
            });
            bf.addEventListener('click', () => toggleBackfillPanel(ctx, wrap, d));
        });
    }

    function paintCounts(ctx) {
        const fb = ctx.mount.querySelector('#rcTgFilters');
        if (!fb) return;
        const c = counts(searched());
        FILTERS.forEach(f => { const el = fb.querySelector(`[data-filter="${f.key}"] .rc-chip__n`); if (el) el.textContent = c[f.key]; });
    }

    function toggleNewCat(ctx) {
        const bar = ctx.mount.querySelector('#rcTgNewCatBar');
        if (!bar) return;
        if (bar.innerHTML) { bar.innerHTML = ''; return; }
        bar.innerHTML = `<div class="rc-newcat">
            <input class="rc-input" id="rcTgCatName" placeholder="${R.t('telegram.newcat.placeholder')}" autocomplete="off">
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcTgCatSave">${R.t('core.create')}</button>
            <button class="rc-btn rc-btn--sm" id="rcTgCatCancel">${R.t('core.cancel')}</button>
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
                UI.toast(R.t('telegram.newcat.created', { name }), 'success');
            } catch (e) { UI.toast(e.message || R.t('telegram.newcat.failed'), 'error'); }
        };
        bar.querySelector('#rcTgCatSave').addEventListener('click', save);
        bar.querySelector('#rcTgCatCancel').addEventListener('click', close);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') { e.preventDefault(); save(); }
            else if (e.key === 'Escape') { close(); }
        });
    }

    R.views.telegram = { render, destroy() { dialogs = []; filter = 'all'; query = null; live = false; } };
})();
