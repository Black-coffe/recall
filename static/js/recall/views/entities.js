/* Recall — Entities. List (/entities) + detail (/entities/:id).
   Two view objects sharing one file. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const TYPES = [
        { k: '', label: 'Усі' },
        { k: 'person', label: 'Люди' },
        { k: 'project', label: 'Проєкти' },
        { k: 'org', label: 'Організації' },
    ];
    const TYPE_LABEL = { person: 'людина', project: 'проєкт', org: 'організація', other: 'інше' };

    // ---------- LIST ----------
    let listEl = null, lstate = null, cats = null;
    // T4.7: entities are paginated (backend gives total/limit/offset) — track
    // the accumulated page(s) so "Завантажити ще" can append instead of
    // silently capping the list at ENT_PAGE rows.
    const ENT_PAGE = 200;
    let entRows = [], entTotal = 0;

    async function renderList(ctx) {
        cats = await UI.loadCategories();
        lstate = { type: ctx.query.type || '', q: ctx.query.q || '', category_id: ctx.query.category_id || '' };

        const catOpts = UI.catOptions(lstate.category_id, { first: 'all' });

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">Граф архіву</div>
                <h1 class="rc-pagehead__title">Сутності</h1>
                <p class="rc-pagehead__lede">Люди, проєкти й організації, що зустрічаються в архіві. Показані значущі (згадані у ≥2 записах); пошук розкриває й рідкісні.</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcEntSearch" type="search" placeholder="Пошук сутності за іменем…" value="${U.esc(lstate.q)}">
                </div>
                <select class="rc-select rc-catsel" id="rcEntCat" data-cat-first="all">${catOpts}</select>
            </div>
            <div class="rc-filterbar" id="rcEntTypes"></div>
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
        const search = ctx.mount.querySelector('#rcEntSearch');
        search.addEventListener('input', U.debounce(() => { lstate.q = search.value.trim(); syncList(); loadList(ctx); }, 280));
        loadList(ctx);
    }
    function syncList() {
        const p = new URLSearchParams();
        if (lstate.type) p.set('type', lstate.type);
        if (lstate.q) p.set('q', lstate.q);
        if (lstate.category_id) p.set('category_id', lstate.category_id);
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
            const filtered = !!(lstate.category_id || lstate.q || lstate.type);
            if (filtered) {
                listEl.innerHTML = UI.empty('Сутностей не знайдено',
                    'У цьому напрямку/фільтрі немає сутностей — спробуйте розширити пошук.', 'fa-diagram-project');
            } else {
                const actions = [{ label: 'Запустити збагачення', icon: 'fa-wand-magic-sparkles', primary: true, onClick: () => runEnrichment(ctx) }];
                listEl.innerHTML = UI.empty('Сутностей не знайдено',
                    'Збагачення (enrichment) ще не виконано — Claude ще не витягнув людей, проєкти й організації з записів.',
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
                            <span class="sep">·</span><span>${e.mention_count || 0} згадок</span>
                            <span class="sep">·</span><span>${e.meeting_count || 0} записів</span>
                        </div>
                    </div>
                </div>`).join('');
        // T4.7: honestly show truncation (same pattern as research.js's `truncated`)
        // instead of silently capping the list at ENT_PAGE rows.
        const more = entTotal > entRows.length
            ? `<div class="rc-note"><p>Показано ${entRows.length} з ${entTotal} сутностей.</p>
                <div class="rc-note__actions"><button class="rc-btn rc-btn--sm" id="rcEntMore">Завантажити ще</button></div></div>`
            : '';
        listEl.innerHTML = rowsHTML + more;
        listEl.querySelectorAll('.rc-rec').forEach(el => el.addEventListener('click', () => R.router.navigate(el.dataset.href)));
        const moreBtn = listEl.querySelector('#rcEntMore');
        if (moreBtn) moreBtn.addEventListener('click', () => {
            moreBtn.disabled = true; moreBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Завантаження…';
            fetchEntities(ctx, false);
        });
    }

    async function runEnrichment(ctx) {
        try {
            await R.api.post('/api/memory/backfill', {});
            UI.toast('Збагачення запущено у фоні — сутності зʼявляться протягом кількох хвилин. Оновіть сторінку пізніше.', 'success');
        } catch (err) {
            UI.toast((err && err.message) || 'Не вдалося запустити збагачення', 'error');
        }
    }

    // ---------- DETAIL ----------
    async function renderDetail(ctx) {
        const id = U.parseId(ctx.params.id);
        if (!Number.isInteger(id)) { ctx.mount.innerHTML = UI.error('Некоректне посилання.'); return; }
        ctx.mount.innerHTML = `<div class="rc-skel" style="height:120px"></div>`;
        let d;
        try { d = await R.api.entity(id); }
        catch (err) { if (ctx.isCurrent()) ctx.mount.innerHTML = UI.error(err.status === 404 ? 'Сутність не знайдено.' : err.message); return; }
        if (!ctx.isCurrent()) return;
        const e = d.entity;
        const meetings = d.meetings || [], actions = d.action_items || [], aliases = d.aliases || [];

        ctx.mount.innerHTML = `
            <a class="rc-btn rc-btn--ghost rc-btn--sm" href="/entities" style="margin-bottom:16px"><i class="fa-solid fa-arrow-left"></i> Сутності</a>
            <div class="rc-record">
                <div class="rc-record__head">
                    <div class="rc-record__accession"><span>RECALL · СУТНІСТЬ <span class="rc-id">#${e.id}</span></span><span>·</span><span class="rc-mono">${U.esc(TYPE_LABEL[e.type] || e.type)}</span></div>
                    <h1 class="rc-record__title">${U.esc(e.canonical_name)}</h1>
                    <div class="rc-prov">
                        ${e.role ? `<div class="rc-prov__cell"><div class="rc-prov__k">Роль</div><div class="rc-prov__v">${U.esc(e.role)}</div></div>` : ''}
                        <div class="rc-prov__cell"><div class="rc-prov__k">Згадок</div><div class="rc-prov__v">${e.mention_count || 0}</div></div>
                        <div class="rc-prov__cell"><div class="rc-prov__k">Записів</div><div class="rc-prov__v">${e.meeting_count || 0}</div></div>
                        ${aliases.length ? `<div class="rc-prov__cell"><div class="rc-prov__k">Псевдоніми</div><div class="rc-prov__v">${aliases.map(a => U.esc(a)).join(', ')}</div></div>` : ''}
                    </div>
                    ${e.description ? `<p style="margin-top:16px;color:var(--rc-ink-2)">${U.esc(e.description)}</p>` : ''}
                </div>
                <div class="rc-record__body">
                    <div class="rc-rel">
                        ${actions.length ? `<div class="rc-rel__block"><div class="rc-rel__h">Задачі <span class="rc-mono">${actions.length}</span></div>
                            <div>${actions.map(a => `<div class="rc-task"><span class="rc-task__status" data-s="${U.esc(a.status)}">${U.esc(U.taskStatus(a.status))}</span>
                                <div class="rc-task__body"><a href="/transcript/${U.slug(a.transcription_id, a.source_name)}" style="color:inherit">${U.esc(a.task)}</a></div></div>`).join('')}</div></div>` : ''}
                        <div class="rc-rel__block"><div class="rc-rel__h">Зʼявляється у записах <span class="rc-mono">${meetings.length}</span></div>
                            <div class="rc-tags">${meetings.map(m => `<a class="rc-entity" href="/transcript/${U.slug(m.id, m.source_name)}">
                                ${UI.srcBadge(m.source_type)} ${U.esc(m.source_name || ('#' + m.id))}
                                <span class="rc-entity__type">${U.esc(m.meeting_date || '')}</span></a>`).join('')}</div></div>

                        <div class="rc-rel__block rc-entexp">
                            <div class="rc-rel__h">Експорт за весь час <span class="rc-mono">${e.mention_count || 0} згадок</span></div>
                            <p class="rc-entexp__lede">Зібрати кожну згадку «${U.esc(e.canonical_name)}»${aliases.length ? ` (та псевдоніми: ${aliases.map(a => U.esc(a)).join(', ')})` : ''} з УСІХ джерел у великий <b>.md</b>.</p>
                            <div class="rc-research__actions">
                                <button class="rc-research__exp" data-eexp="originals">
                                    <div class="rc-research__exp-ic"><i class="fa-solid fa-file-arrow-down"></i></div>
                                    <div><div class="rc-research__exp-t">Повний експорт (оригінали)</div>
                                        <div class="rc-research__exp-s">Усі дослівні фрагменти за датою (свіже згори) + посилання на оригінали. <b>0 токенів, без AI.</b></div></div>
                                </button>
                                <button class="rc-research__exp" data-eexp="summary">
                                    <div class="rc-research__exp-ic"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
                                    <div><div class="rc-research__exp-t">AI-саммарі за весь час</div>
                                        <div class="rc-research__exp-s">Стислий хронологічний звіт по всіх згадках. Дешева модель, тільки по фрагментах.</div></div>
                                </button>
                            </div>
                            <div class="rc-entexp__opts">
                                <label class="rc-mono">Модель саммарі</label>
                                <select class="rc-select rc-select--sm" id="rcEntExpModel">
                                    <option value="haiku">Haiku 4.5 — дешева (рекомендовано)</option>
                                    <option value="sonnet">Sonnet 4.6 — якісніша</option>
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
