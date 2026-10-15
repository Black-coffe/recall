/* Recall — Home / Memory Hub. Archive stats + an ask box + recent records. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const T = (k, v) => R.t(k, v);

    async function render(ctx) {
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">${T('library.home.eyebrow')}</div>
                <h1 class="rc-pagehead__title" style="font-size:var(--rc-t-display)">${T('library.home.title')}</h1>
                <p class="rc-pagehead__lede">${T('library.home.lede')}</p>
            </div>
            <div class="rc-ask__bar" style="margin-bottom:32px">
                <textarea id="rcHomeAsk" placeholder="${U.esc(T('library.home.askPh'))}"></textarea>
                <button class="rc-btn rc-btn--primary" id="rcHomeAskGo"><i class="fa-solid fa-paper-plane"></i></button>
            </div>
            <div class="rc-stats" id="rcHomeStats"></div>
            <div class="rc-pagehead__row" style="margin-bottom:12px">
                <h2 style="font-size:var(--rc-t-h2)">${T('library.home.recent')}</h2>
                <a class="rc-btn rc-btn--ghost rc-btn--sm" href="/library">${T('library.home.all')} <i class="fa-solid fa-arrow-right"></i></a>
            </div>
            <div class="rc-list" id="rcHomeRecent"></div>`;

        const ask = ctx.mount.querySelector('#rcHomeAsk');
        const fire = () => { const q = ask.value.trim(); R.router.navigate('/ask' + (q ? '?q=' + encodeURIComponent(q) : '')); };
        ctx.mount.querySelector('#rcHomeAskGo').addEventListener('click', fire);
        ask.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); fire(); } });

        renderStats(ctx);
        renderRecent(ctx);
    }

    async function renderStats(ctx) {
        const box = ctx.mount.querySelector('#rcHomeStats');
        try {
            const s = R.state.stats || await R.api.stats();
            if (!ctx.isCurrent()) return;
            const sum = (o) => o ? Object.values(o).reduce((a, b) => a + (b || 0), 0) : 0;
            const tiles = [
                { n: s.transcriptions ? s.transcriptions.total : 0, l: T('library.home.tile.records'), href: '/library' },
                { n: sum(s.entities_significant), l: T('library.home.tile.entities'), href: '/entities' },
                { n: s.action_items ? s.action_items.open : 0, l: T('library.home.tile.tasks'), href: '/tasks' },
                { n: s.chunks || 0, l: T('library.home.tile.chunks'), href: '/ask' },
            ];
            box.innerHTML = tiles.map(t =>
                `<div class="rc-stat" data-href="${t.href}"><div class="rc-stat__n">${R.i18n.num(t.n || 0)}</div><div class="rc-stat__l">${t.l}</div></div>`
            ).join('');
            box.querySelectorAll('.rc-stat').forEach(el => el.addEventListener('click', () => R.router.navigate(el.dataset.href)));
        } catch (err) {
            if (!ctx.isCurrent()) return;
            // T5.5: a failed stats load must read as "broken, retry" — not
            // silently blank, which a user can't tell apart from "archive is
            // genuinely empty" (zero-value tiles already render fine above).
            box.innerHTML = UI.error((err && err.message) || T('library.home.statsFail'), { retry: true });
            UI.bindErrorRetry(box, () => renderStats(ctx));
        }
    }

    async function renderRecent(ctx) {
        const box = ctx.mount.querySelector('#rcHomeRecent');
        box.innerHTML = UI.skeletonList(5);
        try {
            await UI.loadCategories();   // warm cache — UI.catChip(id) reads Recall.state.categories itself (C9)
            const data = await R.api.history({ per_page: 6 });
            if (!ctx.isCurrent()) return;
            const rows = data.transcriptions || [];
            if (!rows.length) {
                // T5.5: empty archive is a dead end without a direct way forward.
                const actions = [
                    { label: T('library.home.upload'), icon: 'fa-upload', href: '/upload', primary: true },
                    { label: T('library.home.record'), icon: 'fa-microphone', href: '/record' },
                ];
                box.innerHTML = UI.empty(T('library.home.empty.title'), T('library.home.empty.body'), 'fa-box-open', actions);
                return;
            }
            box.innerHTML = rows.map(t => {
                const href = '/transcript/' + U.slug(t.id, t.display_name || t.source_name);
                const thumb = t.youtube_thumbnail
                    ? `<img class="rc-rec__thumb" src="${U.esc(t.youtube_thumbnail)}" alt="" loading="lazy">`
                    : `<div class="rc-rec__icon"><i class="${U.SRC_ICON[t.source_type] || 'fa-solid fa-file'}"></i></div>`;
                return `<div class="rc-rec" data-href="${U.esc(href)}">${thumb}
                    <div class="rc-rec__body"><div class="rc-rec__title">${U.esc(t.display_name || t.source_name || ('#' + t.id))}</div>
                    <div class="rc-rec__meta">${UI.srcBadge(t.source_type)}<span>${U.fmtDate(t.created_at)}</span></div></div>
                    <div class="rc-rec__aside">${UI.catChip(t.category_id)}</div></div>`;
            }).join('');
            box.querySelectorAll('.rc-rec').forEach(el => el.addEventListener('click', () => R.router.navigate(el.dataset.href)));
        } catch (err) {
            if (!ctx.isCurrent()) return;
            box.innerHTML = UI.error(err.message, { retry: true });
            UI.bindErrorRetry(box, () => renderRecent(ctx));
        }
    }

    R.views.home = { render, destroy() {} };
})();
