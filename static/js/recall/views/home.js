/* Recall — Home / Memory Hub. Archive stats + an ask box + recent records. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    async function render(ctx) {
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">Recall · архів памʼяті</div>
                <h1 class="rc-pagehead__title" style="font-size:var(--rc-t-display)">Памʼять усього.</h1>
                <p class="rc-pagehead__lede">Семантичний архів — дзвінки, файли, документи, Telegram. Питай природною мовою, знаходь людей і проєкти, відстежуй домовленості.</p>
            </div>
            <div class="rc-ask__bar" style="margin-bottom:32px">
                <textarea id="rcHomeAsk" placeholder="Спитай архів: «що вирішили по фонду?», «які задачі на Юлю?»…"></textarea>
                <button class="rc-btn rc-btn--primary" id="rcHomeAskGo"><i class="fa-solid fa-paper-plane"></i></button>
            </div>
            <div class="rc-stats" id="rcHomeStats"></div>
            <div class="rc-pagehead__row" style="margin-bottom:12px">
                <h2 style="font-size:var(--rc-t-h2)">Останні записи</h2>
                <a class="rc-btn rc-btn--ghost rc-btn--sm" href="/library">Уся бібліотека <i class="fa-solid fa-arrow-right"></i></a>
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
                { n: s.transcriptions ? s.transcriptions.total : 0, l: 'Записів', href: '/library' },
                { n: sum(s.entities_significant), l: 'Сутностей', href: '/entities' },
                { n: s.action_items ? s.action_items.open : 0, l: 'Відкритих задач', href: '/tasks' },
                { n: s.chunks || 0, l: 'Фрагментів у пошуку', href: '/ask' },
            ];
            box.innerHTML = tiles.map(t =>
                `<div class="rc-stat" data-href="${t.href}"><div class="rc-stat__n">${(t.n || 0).toLocaleString('uk')}</div><div class="rc-stat__l">${t.l}</div></div>`
            ).join('');
            box.querySelectorAll('.rc-stat').forEach(el => el.addEventListener('click', () => R.router.navigate(el.dataset.href)));
        } catch (err) {
            if (!ctx.isCurrent()) return;
            // T5.5: a failed stats load must read as "broken, retry" — not
            // silently blank, which a user can't tell apart from "archive is
            // genuinely empty" (zero-value tiles already render fine above).
            box.innerHTML = UI.error((err && err.message) || 'Не вдалося завантажити статистику', { retry: true });
            UI.bindErrorRetry(box, () => renderStats(ctx));
        }
    }

    async function renderRecent(ctx) {
        const box = ctx.mount.querySelector('#rcHomeRecent');
        box.innerHTML = UI.skeletonList(5);
        try {
            const cats = await UI.loadCategories();
            const data = await R.api.history({ per_page: 6 });
            if (!ctx.isCurrent()) return;
            const rows = data.transcriptions || [];
            if (!rows.length) {
                // T5.5: empty archive is a dead end without a direct way forward.
                const actions = [
                    { label: 'Завантажити аудіо', icon: 'fa-upload', href: '/upload', primary: true },
                    { label: 'Записати дзвінок', icon: 'fa-microphone', href: '/record' },
                ];
                box.innerHTML = UI.empty('Архів порожній', 'Додайте перший запис — завантажте аудіо чи файл, або запишіть дзвінок просто зараз.', 'fa-box-open', actions);
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
                    <div class="rc-rec__aside">${UI.catChip(t.category_id, cats)}</div></div>`;
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
