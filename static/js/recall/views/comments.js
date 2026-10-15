/* Recall — «Коментарі»: усе, що власник сам написав про свій архів.

   Це не ще один список записів. Бібліотека показує, ЩО потрапило в архів;
   ця сторінка — що власник про нього ДУМАЄ: де запис бреше, що вирішено,
   що лишилось питанням. Тому головна вісь тут не дата й не джерело, а ТИП
   коментаря: «виправлення» — це перелік місць, де архів вводить в оману, і
   такий зріз коштує дорожче за будь-який інший.

   Фільтри й сторінка живуть в URL (deep-linkable), як у Бібліотеці. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const T = (k, v) => R.t(k, v);
    const PER_PAGE = 30;

    // Порядок = вага в ранжуванні (див. /api/comments/meta). «Усі» першим,
    // далі від найважчого типу до найлегшого — щоб перший осмислений зріз
    // під рукою був саме той, який найдорожчий.
    const KINDS = [
        { k: '', label: T('knowledge.cmv.kind.all') },
        { k: 'correction', label: T('knowledge.cmv.kind.correction') },
        { k: 'decision', label: T('knowledge.cmv.kind.decision') },
        { k: 'note', label: T('knowledge.cmv.kind.note') },
        { k: 'context', label: T('knowledge.cmv.kind.context') },
        { k: 'question', label: T('knowledge.cmv.kind.question') },
    ];
    const TARGETS = [
        { k: '', label: T('knowledge.cmv.target.any') },
        { k: 'transcription', label: T('knowledge.cmv.target.transcription') },
        { k: 'audio_download', label: T('knowledge.cmv.target.audio') },
        { k: 'recording_session', label: T('knowledge.cmv.target.session') },
        { k: 'action_item', label: T('knowledge.cmv.target.task') },
        { k: 'entity', label: T('knowledge.cmv.target.entity') },
    ];
    const PERIODS = [
        { k: '', label: T('knowledge.cmv.period.all') },
        { k: '7', label: T('knowledge.cmv.period.week') },
        { k: '30', label: T('knowledge.cmv.period.month') },
        { k: '90', label: T('knowledge.cmv.period.quarter') },
    ];

    let listEl = null, state = null, last = null;

    async function render(ctx) {
        state = {
            kind: ctx.query.kind || '',
            target_type: ctx.query.target_type || '',
            days: ctx.query.days || '',
            search: ctx.query.search || '',
            page: parseInt(ctx.query.page, 10) || 1,
        };

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-regular fa-comment-dots"></i> ${T('knowledge.cmv.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${T('knowledge.cmv.title')}</h1>
                <p class="rc-pagehead__lede">${T('knowledge.cmv.lede')}</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcCmSearch" type="search" placeholder="${U.esc(T('knowledge.cmv.searchPh'))}" value="${U.esc(state.search)}">
                </div>
                <select class="rc-select" id="rcCmTarget">${TARGETS.map(t =>
                    `<option value="${t.k}"${t.k === state.target_type ? ' selected' : ''}>${t.label}</option>`).join('')}</select>
                <select class="rc-select" id="rcCmDays">${PERIODS.map(p =>
                    `<option value="${p.k}"${p.k === state.days ? ' selected' : ''}>${p.label}</option>`).join('')}</select>
            </div>
            <div class="rc-filterbar" id="rcCmKinds"></div>
            <div class="rc-list" id="rcCmList"></div>
            <div id="rcCmPager"></div>`;

        listEl = ctx.mount.querySelector('#rcCmList');

        ctx.mount.querySelector('#rcCmKinds').addEventListener('click', (e) => {
            const b = e.target.closest('.rc-chip'); if (!b) return;
            state.kind = b.dataset.kind; state.page = 1; sync(); load(ctx);
        });
        ctx.mount.querySelector('#rcCmTarget').addEventListener('change', (e) => {
            state.target_type = e.target.value; state.page = 1; sync(); load(ctx);
        });
        ctx.mount.querySelector('#rcCmDays').addEventListener('change', (e) => {
            state.days = e.target.value; state.page = 1; sync(); load(ctx);
        });
        ctx.mount.querySelector('#rcCmSearch').addEventListener('input', U.debounce((e) => {
            state.search = e.target.value.trim(); state.page = 1; sync(); load(ctx);
        }, 280));

        renderKinds();
        load(ctx);
    }

    function sync() {
        const p = new URLSearchParams();
        if (state.kind) p.set('kind', state.kind);
        if (state.target_type) p.set('target_type', state.target_type);
        if (state.days) p.set('days', state.days);
        if (state.search) p.set('search', state.search);
        if (state.page > 1) p.set('page', state.page);
        const q = p.toString();
        R.router.replace('/comments' + (q ? '?' + q : ''));
    }

    // Лічильники на чипах рахуються БЕЗ фільтра за типом (так їх віддає
    // сервер) — інакше, обравши «виправлення», ви бачили б «решта 0» і не
    // могли б оцінити, куди перемикатись.
    function renderKinds() {
        const box = document.getElementById('rcCmKinds');
        if (!box) return;
        const counts = (last && last.by_kind) || {};
        const total = Object.values(counts).reduce((a, b) => a + b, 0);
        box.innerHTML = KINDS.map(k => {
            const n = k.k ? (counts[k.k] || 0) : total;
            return `<button class="rc-chip${k.k === state.kind ? ' is-active' : ''}" data-kind="${k.k}">${k.label}${
                last ? ` <span class="rc-chip__n">${n}</span>` : ''}</button>`;
        }).join('');
    }

    // ISO-дата «N днів тому» — сервер фільтрує по created_at >= since.
    function sinceParam() {
        if (!state.days) return undefined;
        const d = new Date(Date.now() - parseInt(state.days, 10) * 86400000);
        return d.toISOString().slice(0, 19).replace('T', ' ');
    }

    async function load(ctx) {
        if (!listEl) return;
        listEl.innerHTML = UI.skeletonList(6);
        const pager = ctx.mount.querySelector('#rcCmPager');
        if (pager) pager.innerHTML = '';
        try {
            const d = await R.api.commentsRecent({
                limit: PER_PAGE,
                offset: (state.page - 1) * PER_PAGE,
                kind: state.kind || undefined,
                target_type: state.target_type || undefined,
                since: sinceParam(),
                search: state.search || undefined,
            });
            if (!ctx.isCurrent()) return;
            last = d;
            renderKinds();
            const rows = d.comments || [];
            if (!rows.length) {
                listEl.innerHTML = UI.empty(
                    state.kind || state.search || state.target_type || state.days
                        ? T('knowledge.cmv.nothing')
                        : T('knowledge.cmv.none'),
                    state.kind || state.search || state.target_type || state.days
                        ? T('knowledge.cmv.tryFilter')
                        : T('knowledge.cmv.noneHint'),
                    'fa-comment-dots');
                return;
            }
            listEl.innerHTML = rows.map(rowHTML).join('');
            bind(ctx, rows);
            if (pager) {
                pager.innerHTML = pagerHTML(d);
                pager.querySelectorAll('[data-page]').forEach(b =>
                    b.addEventListener('click', () => {
                        state.page = parseInt(b.dataset.page, 10);
                        sync(); load(ctx); window.scrollTo(0, 0);
                    }));
            }
        } catch (err) {
            if (ctx.isCurrent()) listEl.innerHTML = UI.error(err && err.message);
        }
    }

    const KIND_LABEL = (R.comments && R.comments.KIND_LABEL) || {};

    // Куди веде клік. Коментар на сесії запису, яку ще не транскрибували,
    // відкривати нікуди — така картка живе лише на сторінці запису, поки
    // дзвінок іде. Показуємо, але не робимо клікабельним: мертве посилання
    // гірше за його відсутність.
    function hrefFor(c) {
        if (c.target_type === 'transcription') return '/transcript/' + U.slug(c.target_ref, c.target_name);
        if (c.target_type === 'audio_download') return '/audio';
        if (c.target_type === 'action_item') return '/tasks';
        if (c.target_type === 'entity') return '/entities/' + c.target_ref;
        return '';
    }

    function rowHTML(c) {
        const href = hrefFor(c);
        const ts = c.anchor_time != null ? U.fmtDuration(c.anchor_time) : '';
        const meta = [
            `<span class="rc-cmrow__kind rc-cmrow__kind--${U.esc(c.kind)}">${U.esc(KIND_LABEL[c.kind] || c.kind)}</span>`,
            c.pinned ? '<span class="rc-cmrow__pin"><i class="fa-solid fa-thumbtack"></i></span>' : '',
            c.source === 'live' ? `<span class="rc-cmrow__live">${T('knowledge.cmv.live')}</span>` : '',
            ts ? `<span class="rc-mono">${ts}</span>` : '',
            `<span>${U.esc(U.fmtDate(c.created_at))}</span>`,
            c.indexed ? '' : `<span class="rc-rec__warn">${T('knowledge.cmv.notIndexed')}</span>`,
        ].filter(Boolean).join('<span class="sep">·</span>');
        return `<div class="rc-cmrow" data-id="${c.id}"${href ? ` data-href="${U.esc(href)}"` : ''}>
            <div class="rc-cmrow__body">
                <div class="rc-cmrow__text">${U.esc(c.body).replace(/\n/g, '<br>')}</div>
                <div class="rc-cmrow__meta">${meta}</div>
            </div>
            <div class="rc-cmrow__target">
                ${href ? `<span class="rc-cmrow__to">${U.esc(c.target_name)}</span>`
                       : `<span class="rc-cmrow__to is-dead">${U.esc(c.target_name)}</span>`}
                <span class="rc-rec__id rc-mono">#${c.id}</span>
            </div>
        </div>`;
    }

    function bind(ctx, rows) {
        listEl.querySelectorAll('.rc-cmrow').forEach(el => {
            const href = el.dataset.href;
            if (!href) return;
            el.classList.add('is-link');
            el.addEventListener('click', () => R.router.navigate(href));
        });
    }

    function pagerHTML(d) {
        const total = d.total || 0;
        const pages = Math.max(1, Math.ceil(total / (d.limit || PER_PAGE)));
        const page = state.page;
        if (pages <= 1) return `<div class="rc-pager"><span class="rc-pager__info">${T('knowledge.cmt.count', { count: total })}</span></div>`;
        const prev = page > 1 ? `<button class="rc-btn rc-btn--sm" data-page="${page - 1}"><i class="fa-solid fa-chevron-left"></i></button>` : '';
        const next = page < pages ? `<button class="rc-btn rc-btn--sm" data-page="${page + 1}"><i class="fa-solid fa-chevron-right"></i></button>` : '';
        return `<div class="rc-pager">${prev}<span class="rc-pager__info">${T('knowledge.cmv.page', { page: page, pages: pages })} · ${T('knowledge.cmt.count', { count: total })}</span>${next}</div>`;
    }

    R.views.comments = {
        render,
        destroy() { listEl = null; state = null; last = null; },
    };
})();
