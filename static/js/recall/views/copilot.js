/* Recall — /copilot: post-call copilot review (read-only).

   Reads: GET /api/copilot/sessions?limit&offset (list with shown/suppressed/thumbs counts),
   GET /api/copilot/<id>/timeline (cards grouped by topic), GET /api/copilot/<id>/export?format=md|json.
   Ratings are displayed, never changed here: they stay on the transcript panel. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const LIMIT = 20;
    const INSIGHT = { insight_local: 1, insight_verified: 1 };
    const MODE_KEYS = { light: 'copilot.mode.light', medium: 'copilot.mode.medium', hard: 'copilot.mode.hard' };

    let offset = 0;
    let filter = 'all';
    let timeline = null;

    const modeLabel = (m) => (MODE_KEYS[m] ? R.t(MODE_KEYS[m]) : (m || ''));
    const fmtCost = (c) => '$' + R.i18n.num(Number(c || 0), { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const isMuted = (e) => !!(e.payload && e.payload.shown === false);
    const isOwner = (e) => { const a = e.payload && e.payload.actor; return !a || a === 'ui'; };

    async function render(ctx) {
        offset = 0; filter = 'all'; timeline = null;
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-wand-magic-sparkles"></i> ${R.t('copilot.eyebrow')}</div>
                <div class="rc-pagehead__row">
                    <h1 class="rc-pagehead__title">${R.t('copilot.title')}</h1>
                    <button class="rc-btn rc-btn--sm" id="rcCprReload"><i class="fa-solid fa-rotate"></i> ${R.t('copilot.reload')}</button>
                </div>
                <p class="rc-pagehead__lede">${R.t('copilot.lede')}</p>
            </div>
            <div class="rc-cpr__list" id="rcCprList">${UI.skeletonList(4)}</div>
            <div class="rc-cpr__detail" id="rcCprDetail"></div>`;
        ctx.mount.querySelector('#rcCprReload').addEventListener('click', () => loadList(ctx));
        await loadList(ctx);
    }

    async function loadList(ctx) {
        const box = ctx.mount.querySelector('#rcCprList');
        let r;
        try {
            r = await R.api.get('/api/copilot/sessions?limit=' + LIMIT + '&offset=' + offset);
        } catch (_) {
            if (ctx.isCurrent() && box) box.innerHTML = UI.error(R.t('copilot.loadFailed'));
            return;
        }
        if (!ctx.isCurrent() || !box) return;
        const rows = (r && r.sessions) || [];
        if (!rows.length && offset === 0) {
            box.innerHTML = UI.empty(R.t('copilot.empty'), R.t('copilot.emptySub'), 'fa-wand-magic-sparkles');
            return;
        }
        box.innerHTML = `<table class="rc-cpr__table">
            <thead><tr>
                <th>${R.t('copilot.col.name')}</th><th>${R.t('copilot.col.date')}</th>
                <th>${R.t('copilot.col.mode')}</th><th>${R.t('copilot.col.cost')}</th>
                <th>${R.t('copilot.col.cards')}</th><th>${R.t('copilot.col.ratings')}</th>
            </tr></thead>
            <tbody>${rows.map(s => `<tr class="rc-cpr__row" data-sid="${Number(s.id)}">
                <td>${U.esc(s.transcript_name || R.t('copilot.noTranscript', { id: s.id }))}</td>
                <td class="rc-mono">${U.esc(s.started_at ? U.fmtDate(s.started_at) : '')}</td>
                <td>${U.esc(modeLabel(s.mode))}</td>
                <td class="rc-mono">${fmtCost(s.cost_estimate)}</td>
                <td class="rc-mono">${Number(s.shown || 0)} / ${Number(s.suppressed || 0)}</td>
                <td class="rc-mono"><i class="fa-solid fa-thumbs-up"></i> ${Number(s.thumbs_up || 0)}
                    <i class="fa-solid fa-thumbs-down"></i> ${Number(s.thumbs_down || 0)}</td>
            </tr>`).join('')}</tbody>
        </table>
        <div class="rc-cpr__pager">
            <button class="rc-btn rc-btn--sm" data-page="prev"${offset === 0 ? ' disabled' : ''}>${R.t('copilot.prev')}</button>
            <span class="rc-mono">${R.t('copilot.page', { page: Math.floor(offset / LIMIT) + 1 })}</span>
            <button class="rc-btn rc-btn--sm" data-page="next"${rows.length < LIMIT ? ' disabled' : ''}>${R.t('copilot.next')}</button>
        </div>`;
        box.querySelectorAll('[data-sid]').forEach(tr => tr.addEventListener('click', () => openSession(ctx, Number(tr.dataset.sid))));
        box.querySelectorAll('[data-page]').forEach(b => b.addEventListener('click', () => {
            offset = Math.max(0, offset + (b.dataset.page === 'next' ? LIMIT : -LIMIT));
            loadList(ctx);
        }));
    }

    async function openSession(ctx, sid) {
        const box = ctx.mount.querySelector('#rcCprDetail');
        if (!box) return;
        box.innerHTML = UI.skeletonList(3);
        let tl;
        try {
            const r = await R.api.get('/api/copilot/' + sid + '/timeline');
            tl = r && r.timeline;
        } catch (_) { tl = null; }
        if (!ctx.isCurrent()) return;
        if (!tl || !tl.session) { box.innerHTML = UI.error(R.t('copilot.detail.loadFailed')); return; }
        timeline = tl; filter = 'all';
        paintDetail(ctx);
    }

    // Owner ratings per card: the last owner thumbs event referencing the card wins.
    function ratings(events) {
        const out = {};
        events.forEach(e => {
            if (e.kind !== 'operator_action' || !isOwner(e)) return;
            if (e.operator_action !== 'thumbs_up' && e.operator_action !== 'thumbs_down') return;
            const ref = e.payload && e.payload.ref_event_id;
            if (ref != null) out[ref] = e.operator_action;
        });
        return out;
    }

    // Cards grouped by the topic in force when they were raised (topic_shift/topic_return events).
    function groups(events) {
        const out = [];
        let cur = { label: R.t('copilot.topic.none'), cards: [] };
        events.forEach(e => {
            if (e.kind === 'topic_shift' || e.kind === 'topic_return') {
                if (cur.cards.length) out.push(cur);
                cur = { label: (e.payload && e.payload.label) || R.t('copilot.topic.fallback'), cards: [] };
            } else if (INSIGHT[e.kind]) {
                cur.cards.push(e);
            }
        });
        if (cur.cards.length) out.push(cur);
        return out;
    }

    function passes(e) {
        if (filter === 'shown') return !isMuted(e);
        if (filter === 'muted') return isMuted(e);
        return true;
    }

    function renderCards() {
        const events = timeline.events || [];
        const rate = ratings(events);
        const gs = groups(events).map(g => ({ label: g.label, cards: g.cards.filter(passes) })).filter(g => g.cards.length);
        if (!gs.length) return `<div class="rc-cpr__empty rc-mono">${R.t('copilot.cards.empty')}</div>`;
        return gs.map(g => `<section class="rc-cpr__topic">
            <h3 class="rc-cpr__topic-title">${U.esc(g.label)}</h3>
            ${g.cards.map(e => {
                const p = e.payload || {};
                const muted = isMuted(e);
                const r = rate[e.id];
                const t = e.ts_offset_sec != null ? U.fmtDuration(e.ts_offset_sec) : '';
                const conf = e.confidence != null ? Math.round(e.confidence * 100) + '%' : '';
                const ev = (p.evidence || []).map(x => {
                    const name = x.source_name || ('#' + x.transcription_id);
                    return `<a class="rc-cpr__ev rc-mono" href="/transcript/${U.slug(x.transcription_id, x.source_name || R.t('copilot.evidence'))}">[${U.esc(name)}]</a>`;
                }).join(' ');
                return `<div class="rc-cpr__card${muted ? ' is-muted' : ''}" data-card="${Number(e.id)}">
                    <div class="rc-cpr__card-meta rc-mono">
                        <span>${t}</span>
                        ${p.kind ? `<span>${U.esc(p.kind)}</span>` : ''}
                        ${conf ? `<span>${conf}</span>` : ''}
                        ${e.kind === 'insight_verified' ? `<span class="rc-cpr__badge">${R.t('copilot.verified')}</span>` : ''}
                        ${muted ? `<span class="rc-cpr__badge rc-cpr__badge--muted">${R.t('copilot.muted')}</span>` : ''}
                        ${r === 'thumbs_up' ? `<span class="rc-cpr__rating" title="${R.t('copilot.rating.up')}"><i class="fa-solid fa-thumbs-up"></i></span>` : ''}
                        ${r === 'thumbs_down' ? `<span class="rc-cpr__rating" title="${R.t('copilot.rating.down')}"><i class="fa-solid fa-thumbs-down"></i></span>` : ''}
                    </div>
                    <div class="rc-cpr__card-text">${U.esc(p.text || '')}${ev ? ' ' + ev : ''}</div>
                </div>`;
            }).join('')}
        </section>`).join('');
    }

    function paintDetail(ctx) {
        const box = ctx.mount.querySelector('#rcCprDetail');
        if (!box || !timeline) return;
        const s = timeline.session || {};
        const sid = Number(s.id);
        const FILTERS = [['all', R.t('copilot.filter.all')], ['shown', R.t('copilot.filter.shown')], ['muted', R.t('copilot.filter.muted')]];
        box.innerHTML = `<div class="rc-cpr__panel">
            <div class="rc-cpr__head">
                <span class="rc-cpr__title">${R.t('copilot.detail.title', { id: sid })}</span>
                <span class="rc-mono">${U.esc(modeLabel(s.mode))} · ${fmtCost(s.cost_estimate)}</span>
                <span class="rc-cpr__exp">
                    <a class="rc-btn rc-btn--sm" data-exp="md" href="/api/copilot/${sid}/export?format=md" title="${R.t('copilot.exp.md')}">MD</a>
                    <a class="rc-btn rc-btn--sm" data-exp="json" href="/api/copilot/${sid}/export?format=json" title="${R.t('copilot.exp.json')}">JSON</a>
                </span>
                <button class="rc-btn rc-btn--sm" id="rcCprClose" title="${R.t('copilot.detail.close')}"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div class="rc-cpr__filters">${FILTERS.map(([k, l]) =>
                `<button class="rc-cpr__filt rc-mono${filter === k ? ' is-active' : ''}" data-filt="${k}">${l}</button>`).join('')}</div>
            <div class="rc-cpr__cards" id="rcCprCards">${renderCards()}</div>
        </div>`;
        box.querySelector('#rcCprClose').addEventListener('click', () => { timeline = null; box.innerHTML = ''; });
        box.querySelectorAll('[data-exp]').forEach(a => a.addEventListener('click', (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            downloadExport(sid, a.dataset.exp, a.getAttribute('href'));
        }));
        box.querySelectorAll('[data-filt]').forEach(b => b.addEventListener('click', () => {
            filter = b.dataset.filt;
            box.querySelectorAll('[data-filt]').forEach(x => x.classList.toggle('is-active', x === b));
            const cards = box.querySelector('#rcCprCards');
            if (cards) cards.innerHTML = renderCards();
        }));
    }

    async function downloadExport(sid, fmt, url) {
        try {
            const res = await fetch(url, { headers: R.i18n.headers() });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'copilot-session-' + sid + '.' + fmt;
            document.body.appendChild(a); a.click();
            URL.revokeObjectURL(a.href); a.remove();
            UI.toast(R.t('copilot.exported', { fmt: fmt.toUpperCase() }), 'success');
        } catch (_) { UI.toast(R.t('copilot.exportFailed'), 'error'); }
    }

    R.views.copilot = { render, destroy() { timeline = null; offset = 0; filter = 'all'; } };
})();
