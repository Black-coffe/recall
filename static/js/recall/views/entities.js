/* Recall — Entities. List (/entities) + detail (/entities/:id).
   Two view objects sharing one file. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const T = (k, v) => R.t(k, v);

    const TYPES = [
        { k: '', label: T('knowledge.ent.type.all') },
        { k: 'person', label: T('knowledge.ent.type.people') },
        { k: 'project', label: T('knowledge.ent.type.projects') },
        { k: 'org', label: T('knowledge.ent.type.orgs') },
    ];
    const TYPE_LABEL = { person: T('knowledge.ent.kind.person'), project: T('knowledge.ent.kind.project'), org: T('knowledge.ent.kind.org'), other: T('knowledge.ent.kind.other') };

    // ---------- LIST ----------
    let listEl = null, lstate = null, cats = null;
    // T4.7: entities are paginated (backend gives total/limit/offset) — track
    // the accumulated page(s) so "Завантажити ще" can append instead of
    // silently capping the list at ENT_PAGE rows.
    const ENT_PAGE = 200;
    let entRows = [], entTotal = 0;

    const FILTER_HINT = T('knowledge.ent.filterHint');
    function filterExplain(st) {
        if (!st.category_id && !st.tags.length && !st.since && !st.until) return null;
        const bits = [];
        if (st.category_id && UI.catPath) { const p = UI.catPath(Number(st.category_id)); if (p) bits.push(T('knowledge.filter.inCat', { path: p })); }
        if (st.tags.length) bits.push(T('knowledge.filter.withTags', { tags: st.tags.join(', ') }));
        if (st.since || st.until) bits.push(T('knowledge.filter.period', { from: st.since || '…', to: st.until || '…' }));
        if (!bits.length) return null;
        return T('knowledge.ent.explain', { bits: bits.join(' ') });
    }

    async function renderList(ctx) {
        cats = await UI.loadCategories();
        if (UI.loadTags) await UI.loadTags();
        lstate = {
            type: ctx.query.type || '', q: ctx.query.q || '', category_id: ctx.query.category_id || '',
            tags: (ctx.query.tags || '').split(',').map(s => s.trim()).filter(Boolean),
            since: ctx.query.since || '', until: ctx.query.until || '',
        };

        const catOpts = UI.catOptions(lstate.category_id, { first: 'all' });
        const tagSel = UI.tagOptions ? UI.tagOptions(lstate.tags) : '<select multiple class="rc-select rc-select--sm" disabled></select>';

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">${T('knowledge.ent.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${T('knowledge.ent.title')}</h1>
                <p class="rc-pagehead__lede">${T('knowledge.ent.lede')}</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcEntSearch" type="search" placeholder="${U.esc(T('knowledge.ent.searchPh'))}" value="${U.esc(lstate.q)}">
                </div>
                <select class="rc-select rc-catsel" id="rcEntCat" data-cat-first="all">${catOpts}</select>
            </div>
            <div class="rc-filterbar" id="rcEntTypes"></div>
            <div class="rc-toolbar" id="rcEntTagPeriod">
                <div id="rcEntTagsWrap">${tagSel}</div>
                <input type="date" class="rc-input rc-select--sm" id="rcEntSince" value="${U.esc(lstate.since)}" title="${T('knowledge.filter.sinceTitle')}">
                <input type="date" class="rc-input rc-select--sm" id="rcEntUntil" value="${U.esc(lstate.until)}" title="${T('knowledge.filter.untilTitle')}">
            </div>
            <div id="rcEntHint"></div>
            <div class="rc-list" id="rcEntList"></div>`;

        listEl = ctx.mount.querySelector('#rcEntList');
        const tb = ctx.mount.querySelector('#rcEntTypes');
        tb.innerHTML = TYPES.map(t => `<button class="rc-chip${t.k === lstate.type ? ' is-active' : ''}" data-t="${t.k}">${t.label}</button>`).join('');
        tb.addEventListener('click', (e) => {
            const b = e.target.closest('.rc-chip'); if (!b) return;
            lstate.type = b.dataset.t;
            tb.querySelectorAll('.rc-chip').forEach(c => c.classList.toggle('is-active', c === b));
            syncList(); loadList(ctx);
        });
        const catSel = ctx.mount.querySelector('#rcEntCat');
        catSel.value = lstate.category_id;
        catSel.addEventListener('change', () => { lstate.category_id = catSel.value; syncList(); loadList(ctx); });
        const tagSelEl = ctx.mount.querySelector('#rcEntTagsWrap select');
        if (tagSelEl) { tagSelEl.setAttribute('aria-label', T('core.ui.tags.filterAria')); tagSelEl.title = T('knowledge.filter.tagsTitle'); }
        if (tagSelEl) tagSelEl.addEventListener('change', () => {
            lstate.tags = Array.from(tagSelEl.selectedOptions).map(o => o.value).filter(Boolean);
            syncList(); loadList(ctx);
        });
        const sinceEl = ctx.mount.querySelector('#rcEntSince');
        const untilEl = ctx.mount.querySelector('#rcEntUntil');
        sinceEl.addEventListener('change', () => { lstate.since = sinceEl.value; syncList(); loadList(ctx); });
        untilEl.addEventListener('change', () => { lstate.until = untilEl.value; syncList(); loadList(ctx); });
        const hintBox = ctx.mount.querySelector('#rcEntHint');
        if (hintBox && UI.hint) hintBox.appendChild(UI.hint(FILTER_HINT));
        const search = ctx.mount.querySelector('#rcEntSearch');
        search.addEventListener('input', U.debounce(() => { lstate.q = search.value.trim(); syncList(); loadList(ctx); }, 280));
        loadList(ctx);
    }
    function syncList() {
        const p = new URLSearchParams();
        if (lstate.type) p.set('type', lstate.type);
        if (lstate.q) p.set('q', lstate.q);
        if (lstate.category_id) p.set('category_id', lstate.category_id);
        if (lstate.tags.length) p.set('tags', lstate.tags.join(','));
        if (lstate.since) p.set('since', lstate.since);
        if (lstate.until) p.set('until', lstate.until);
        const qs = p.toString();
        R.router.replace('/entities' + (qs ? '?' + qs : ''));
    }
    // Fetch a fresh first page (filters changed) or the next page (load-more).
    async function loadList(ctx) {
        listEl.innerHTML = UI.skeletonList(6);
        entRows = []; entTotal = 0;
        await fetchEntities(ctx, true);
    }

    async function fetchEntities(ctx, replace) {
        try {
            const data = await R.api.entities({
                type: lstate.type || undefined, q: lstate.q || undefined, category_id: lstate.category_id || undefined,
                tags: lstate.tags.length ? lstate.tags.join(',') : undefined,
                since: lstate.since || undefined, until: lstate.until || undefined,
                limit: ENT_PAGE, offset: replace ? 0 : entRows.length,
            });
            if (!ctx.isCurrent()) return;
            entTotal = data.total || 0;
            const rows = data.entities || [];
            entRows = replace ? rows.slice() : entRows.concat(rows);
            renderEntities(ctx);
        } catch (err) {
            if (!ctx.isCurrent()) return;
            listEl.innerHTML = UI.error(err && err.message, { retry: true });
            UI.bindErrorRetry(listEl, () => fetchEntities(ctx, replace));
        }
    }

    function renderEntities(ctx) {
        if (!entRows.length) {
            // T5.5: "no filter narrows the result" (enrichment likely hasn't
            // run yet) needs a way forward, not just a diagnosis. A narrowed
            // filter/search gets the neutral "widen your filter" message.
            const filtered = !!(lstate.category_id || lstate.tags.length || lstate.since || lstate.until || lstate.q || lstate.type);
            if (filtered) {
                listEl.innerHTML = UI.empty(T('knowledge.ent.none'),
                    filterExplain(lstate) || T('knowledge.ent.noneFiltered'), 'fa-diagram-project');
            } else {
                const actions = [{ label: T('knowledge.ent.runEnrich'), icon: 'fa-wand-magic-sparkles', primary: true, onClick: () => runEnrichment(ctx) }];
                listEl.innerHTML = UI.empty(T('knowledge.ent.none'),
                    T('knowledge.ent.noEnrich'),
                    'fa-diagram-project', actions);
                UI.wireEmptyActions(listEl, actions);
            }
            return;
        }
        const rowsHTML = entRows.map(e => `
                <div class="rc-rec" data-href="/entities/${e.id}">
                    <div class="rc-rec__icon"><i class="fa-solid ${e.type === 'person' ? 'fa-user' : e.type === 'project' ? 'fa-diagram-project' : 'fa-building'}"></i></div>
                    <div class="rc-rec__body">
                        <div class="rc-rec__title">${U.esc(e.canonical_name)}</div>
                        <div class="rc-rec__meta">
                            <span class="rc-mono">${U.esc(TYPE_LABEL[e.type] || e.type)}</span>
                            ${e.role ? `<span class="sep">·</span><span>${U.esc(e.role)}</span>` : ''}
                            <span class="sep">·</span><span>${T('knowledge.ent.mentionsN', { count: e.mention_count || 0 })}</span>
                            <span class="sep">·</span><span>${T('knowledge.ent.recordsN', { count: e.meeting_count || 0 })}</span>
                        </div>
                    </div>
                </div>`).join('');
        // T4.7: honestly show truncation (same pattern as research.js's `truncated`)
        // instead of silently capping the list at ENT_PAGE rows.
        const more = entTotal > entRows.length
            ? `<div class="rc-note"><p>${T('knowledge.ent.shown', { shown: entRows.length, total: entTotal })}</p>
                <div class="rc-note__actions"><button class="rc-btn rc-btn--sm" id="rcEntMore">${T('knowledge.ent.loadMore')}</button></div></div>`
            : '';
        listEl.innerHTML = rowsHTML + more;
        listEl.querySelectorAll('.rc-rec').forEach(el => el.addEventListener('click', () => R.router.navigate(el.dataset.href)));
        const moreBtn = listEl.querySelector('#rcEntMore');
        if (moreBtn) moreBtn.addEventListener('click', () => {
            moreBtn.disabled = true; moreBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + T('knowledge.ent.loading');
            fetchEntities(ctx, false);
        });
    }

    async function runEnrichment(ctx) {
        try {
            await R.api.post('/api/memory/backfill', {});
            UI.toast(T('knowledge.ent.enrichStarted'), 'success');
        } catch (err) {
            UI.toast((err && err.message) || T('knowledge.ent.enrichFail'), 'error');
        }
    }

    // ---------- DETAIL ----------
    async function renderDetail(ctx) {
        const id = U.parseId(ctx.params.id);
        if (!Number.isInteger(id)) { ctx.mount.innerHTML = UI.error(T('knowledge.ent.badLink')); return; }
        ctx.mount.innerHTML = `<div class="rc-skel" style="height:120px"></div>`;
        let d;
        try { d = await R.api.entity(id); }
        catch (err) { if (ctx.isCurrent()) ctx.mount.innerHTML = UI.error(err.status === 404 ? T('knowledge.ent.notFound') : err.message); return; }
        if (!ctx.isCurrent()) return;
        const e = d.entity;
        const meetings = d.meetings || [], actions = d.action_items || [], aliases = d.aliases || [];

        ctx.mount.innerHTML = `
            <a class="rc-btn rc-btn--ghost rc-btn--sm" href="/entities" style="margin-bottom:16px"><i class="fa-solid fa-arrow-left"></i> ${T('knowledge.ent.title')}</a>
            <div class="rc-record">
                <div class="rc-record__head">
                    <div class="rc-record__accession"><span>${T('knowledge.ent.accession')} <span class="rc-id">#${e.id}</span></span><span>·</span><span class="rc-mono">${U.esc(TYPE_LABEL[e.type] || e.type)}</span></div>
                    <h1 class="rc-record__title">${U.esc(e.canonical_name)}</h1>
                    <div class="rc-prov">
                        ${e.role ? `<div class="rc-prov__cell"><div class="rc-prov__k">${T('knowledge.ent.role')}</div><div class="rc-prov__v">${U.esc(e.role)}</div></div>` : ''}
                        <div class="rc-prov__cell"><div class="rc-prov__k">${T('knowledge.ent.mentions')}</div><div class="rc-prov__v">${e.mention_count || 0}</div></div>
                        <div class="rc-prov__cell"><div class="rc-prov__k">${T('knowledge.ent.records')}</div><div class="rc-prov__v">${e.meeting_count || 0}</div></div>
                        ${aliases.length ? `<div class="rc-prov__cell"><div class="rc-prov__k">${T('knowledge.ent.aliases')}</div><div class="rc-prov__v">${aliases.map(a => U.esc(a)).join(', ')}</div></div>` : ''}
                    </div>
                    ${e.description ? `<p style="margin-top:16px;color:var(--rc-ink-2)">${U.esc(e.description)}</p>` : ''}
                </div>
                <div class="rc-record__body">
                    <div class="rc-rel">
                        ${actions.length ? `<div class="rc-rel__block"><div class="rc-rel__h">${T('knowledge.ent.tasks')} <span class="rc-mono">${actions.length}</span></div>
                            <div>${actions.map(a => `<div class="rc-task"><span class="rc-task__status" data-s="${U.esc(a.status)}">${U.esc(U.taskStatus(a.status))}</span>
                                <div class="rc-task__body"><a href="/transcript/${U.slug(a.transcription_id, a.source_name)}" style="color:inherit">${U.esc(a.task)}</a></div></div>`).join('')}</div></div>` : ''}
                        <div class="rc-rel__block"><div class="rc-rel__h">${T('knowledge.ent.appearsIn')} <span class="rc-mono">${meetings.length}</span></div>
                            <div class="rc-tags">${meetings.map(m => `<a class="rc-entity" href="/transcript/${U.slug(m.id, m.source_name)}">
                                ${UI.srcBadge(m.source_type)} ${U.esc(m.source_name || ('#' + m.id))}
                                <span class="rc-entity__type">${U.esc(m.meeting_date || '')}</span></a>`).join('')}</div></div>

                        <div class="rc-rel__block rc-entexp">
                            <div class="rc-rel__h">${T('knowledge.ent.exportAll')} <span class="rc-mono">${T('knowledge.ent.mentionsN', { count: e.mention_count || 0 })}</span></div>
                            <p class="rc-entexp__lede">${aliases.length
                                ? T('knowledge.ent.exportLedeAliases', { name: U.esc(e.canonical_name), aliases: aliases.map(a => U.esc(a)).join(', ') })
                                : T('knowledge.ent.exportLede', { name: U.esc(e.canonical_name) })}</p>
                            <div class="rc-research__actions">
                                <button class="rc-research__exp" data-eexp="originals">
                                    <div class="rc-research__exp-ic"><i class="fa-solid fa-file-arrow-down"></i></div>
                                    <div><div class="rc-research__exp-t">${T('knowledge.ent.expOrig.t')}</div>
                                        <div class="rc-research__exp-s">${T('knowledge.ent.expOrig.s')}</div></div>
                                </button>
                                <button class="rc-research__exp" data-eexp="summary">
                                    <div class="rc-research__exp-ic"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
                                    <div><div class="rc-research__exp-t">${T('knowledge.ent.expSum.t')}</div>
                                        <div class="rc-research__exp-s">${T('knowledge.ent.expSum.s')}</div></div>
                                </button>
                            </div>
                            <div class="rc-entexp__opts">
                                <label class="rc-mono">${T('knowledge.ent.model')}</label>
                                <select class="rc-select rc-select--sm" id="rcEntExpModel">
                                    <option value="haiku">${T('knowledge.ent.model.haiku')}</option>
                                    <option value="sonnet">${T('knowledge.ent.model.sonnet')}</option>
                                </select>
                            </div>
                            <div id="rcEntExpOut"></div>
                        </div>
                    </div>
                </div>
            </div>`;

        bindEntityExport(ctx, e);
    }

    function bindEntityExport(ctx, e) {
        const box = ctx.mount.querySelector('#rcEntExpOut');
        const oBtn = ctx.mount.querySelector('[data-eexp="originals"]');
        const sBtn = ctx.mount.querySelector('[data-eexp="summary"]');
        if (!box || !oBtn || !sBtn) return;
        oBtn.addEventListener('click', async () => {
            oBtn.disabled = true;
            try { await R.researchExport.runOriginals({ entity_id: e.id }, box, ctx); }
            catch (_) { /* помилка вже у box */ }
            finally { oBtn.disabled = false; }
        });
        sBtn.addEventListener('click', async () => {
            const model = ctx.mount.querySelector('#rcEntExpModel').value || 'haiku';
            sBtn.disabled = true;
            if (detailAbort) { try { detailAbort.abort(); } catch (_) {} }
            detailAbort = new AbortController();
            try {
                await R.researchExport.runSummary({ entity_id: e.id, model }, box, ctx,
                    detailAbort.signal, e.canonical_name);
            } finally { sBtn.disabled = false; }
        });
    }

    let detailAbort = null;
    R.views.entities = { render: renderList, destroy() { listEl = null; lstate = null; cats = null; entRows = []; entTotal = 0; } };
    R.views.entity = {
        render: renderDetail,
        destroy() { if (detailAbort) { try { detailAbort.abort(); } catch (_) {} detailAbort = null; } },
    };
})();
