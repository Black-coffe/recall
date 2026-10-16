/* Recall — "Дослідження бренду / великий експорт". Collects from ALL sources
   (audio, documents, Telegram, YouTube, recordings) every mention of a brand/topic
   and hands it over in two forms:
     • Originals (.md) — verbatim fragments by date, 0 tokens, no AI.
     • Summary (.md)   — a structured report by a cheap model (Haiku) OVER
                          fragments (not whole transcripts) → minimum tokens.

   The search is lexical: spelling variants separated by commas (Polaris, Поларіс, поларис).
   State (query/category) lives in the URL — deep-linkable. SSE summary — via
   api.researchSummary (fetch-reader, NOT EventSource — recall_sse_connection_gotcha). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const MODELS = [
        { v: 'haiku', l: () => R.t('knowledge.research.modelHaiku') },
        { v: 'sonnet', l: () => R.t('knowledge.research.modelSonnet') },
    ];

    let state = null, abortCtl = null, lastPreview = null;

    // C9: category/project/tag are three different axes; the same text as on the rest of
    // the screens (09 gives shared helpers, not a shared filter-bar component).
    const FILTER_HINT = () => R.t('knowledge.research.filterHint');

    async function render(ctx) {
        await UI.loadCategories();
        if (UI.loadTags) await UI.loadTags();
        state = {
            q: ctx.query.q || '',
            category_id: ctx.query.category_id || '',
            tags: (ctx.query.tags || '').split(',').map(x => x.trim()).filter(Boolean),
            since: ctx.query.since || '',
            until: ctx.query.until || '',
            model: ctx.query.model || 'haiku',
        };
        lastPreview = null;

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-magnifying-glass-chart"></i> ${R.t('knowledge.research.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('knowledge.research.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('knowledge.research.lede')}</p>
            </div>

            <section class="rc-research__form">
                <label class="rc-research__lbl">${R.t('knowledge.research.brand')} <span class="rc-research__hint">${R.t('knowledge.research.brandHint')}</span></label>
                <input class="rc-input" id="rcResQ" type="text" autocomplete="off"
                    placeholder="${R.t('knowledge.research.brandExample')}" value="${U.esc(state.q)}">

                <div class="rc-research__row">
                    <div class="rc-research__field">
                        <label class="rc-research__lbl">${R.t('knowledge.research.cat')}</label>
                        <select class="rc-select rc-catsel" id="rcResCat" data-cat-first="all">${UI.catOptions(state.category_id, { first: 'all' })}</select>
                    </div>
                    <div class="rc-research__field">
                        <label class="rc-research__lbl">${R.t('knowledge.research.tags')}</label>
                        ${UI.tagOptions ? UI.tagOptions(state.tags) : '<select multiple class="rc-tagsel"></select>'}
                    </div>
                    <div class="rc-research__field">
                        <label class="rc-research__lbl">${R.t('knowledge.research.period')}</label>
                        <input type="date" class="rc-input rc-select--sm" id="rcResSince" value="${U.esc(state.since)}" title="${U.esc(R.t('knowledge.ask.sinceTitle'))}">
                        <input type="date" class="rc-input rc-select--sm" id="rcResUntil" value="${U.esc(state.until)}" title="${U.esc(R.t('knowledge.ask.untilTitle'))}">
                    </div>
                    <div class="rc-research__field">
                        <label class="rc-research__lbl">${R.t('knowledge.research.model')}</label>
                        <select class="rc-select" id="rcResModel">
                            ${MODELS.map(m => `<option value="${m.v}"${m.v === state.model ? ' selected' : ''}>${m.l()}</option>`).join('')}
                        </select>
                    </div>
                    <button class="rc-btn rc-btn--primary" id="rcResFind"><i class="fa-solid fa-magnifying-glass"></i> ${R.t('knowledge.research.find')}</button>
                </div>
                <div id="rcResHint"></div>
            </section>

            <div id="rcResOut"></div>`;

        const hintBox = ctx.mount.querySelector('#rcResHint');
        if (hintBox && UI.hint) hintBox.appendChild(UI.hint(FILTER_HINT()));

        const qEl = ctx.mount.querySelector('#rcResQ');
        const find = ctx.mount.querySelector('#rcResFind');
        find.addEventListener('click', () => preview(ctx));
        qEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); preview(ctx); } });
        ctx.mount.querySelector('#rcResModel').addEventListener('change', (e) => { state.model = e.target.value; sync(); });
        const catSel = ctx.mount.querySelector('#rcResCat');
        catSel.value = state.category_id || '';
        catSel.addEventListener('change', (e) => { state.category_id = e.target.value; sync(); if (state.q.trim()) preview(ctx); });
        const tagSel = ctx.mount.querySelector('.rc-tagsel');
        if (tagSel) tagSel.addEventListener('change', () => {
            state.tags = Array.from(tagSel.selectedOptions).map(o => o.value).filter(Boolean);
            sync(); if (state.q.trim()) preview(ctx);
        });
        const sinceEl = ctx.mount.querySelector('#rcResSince');
        const untilEl = ctx.mount.querySelector('#rcResUntil');
        sinceEl.addEventListener('change', () => { state.since = sinceEl.value; sync(); if (state.q.trim()) preview(ctx); });
        untilEl.addEventListener('change', () => { state.until = untilEl.value; sync(); if (state.q.trim()) preview(ctx); });
        qEl.focus();

        if (state.q.trim()) preview(ctx);
    }

    function sync() {
        const p = new URLSearchParams();
        if (state.q) p.set('q', state.q);
        if (state.category_id) p.set('category_id', state.category_id);
        if (state.tags.length) p.set('tags', state.tags.join(','));
        if (state.since) p.set('since', state.since);
        if (state.until) p.set('until', state.until);
        if (state.model && state.model !== 'haiku') p.set('model', state.model);
        const s = p.toString();
        R.router.replace('/research' + (s ? '?' + s : ''));
    }

    // C4: shared filter axes for the preview and both exports — one list,
    // so a new axis is not lost in one of the three calls.
    function axes() {
        return {
            category_id: state.category_id || undefined,
            tags: state.tags.length ? state.tags.join(',') : undefined,
            since: state.since || undefined,
            until: state.until || undefined,
        };
    }
    // An empty result with active filters — explain WHAT exactly narrows it.
    function filterExplain() {
        const bits = [];
        if (state.category_id && UI.catPath) { const p = UI.catPath(Number(state.category_id)); if (p) bits.push(R.t('knowledge.ask.explainIn', { path: p })); }
        if (state.tags.length) bits.push(R.t('knowledge.ask.explainTags', { tags: state.tags.join(', ') }));
        if (state.since || state.until) bits.push(R.t('knowledge.ask.explainPeriod', { from: state.since || '…', to: state.until || '…' }));
        if (!bits.length) return null;
        return R.t('knowledge.research.explain', { bits: bits.join(' ') });
    }

    async function preview(ctx) {
        const qEl = ctx.mount.querySelector('#rcResQ');
        state.q = (qEl.value || '').trim();
        sync();
        const out = ctx.mount.querySelector('#rcResOut');
        if (!state.q) { out.innerHTML = UI.empty(R.t('knowledge.research.enterTitle'), R.t('knowledge.research.enterSub'), 'fa-magnifying-glass'); return; }
        out.innerHTML = `<div class="rc-research__status"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('knowledge.research.searching')}</div>`;
        try {
            const data = await R.api.researchPreview(Object.assign({ q: state.q }, axes()));
            if (!ctx.isCurrent()) return;
            lastPreview = data;
            renderResults(ctx, data);
        } catch (err) {
            if (ctx.isCurrent()) out.innerHTML = UI.error(err && err.message);
        }
    }

    function srcChips(by) {
        const LAB = U.SRC_LABEL;
        return Object.keys(by || {}).sort((a, b) => by[b] - by[a])
            .map(k => `<span class="rc-chip is-static">${U.esc(LAB[k] || k)} <span class="rc-chip__n">${by[k]}</span></span>`).join('');
    }

    function renderResults(ctx, data) {
        const out = ctx.mount.querySelector('#rcResOut');
        const st = data.stats || { records: 0, mentions: 0, by_source: {} };
        if (!st.records) {
            out.innerHTML = UI.empty(R.t('knowledge.research.noneTitle'),
                filterExplain()
                || R.t('knowledge.research.noneSub', { terms: U.esc((data.terms || []).join(', ')) }),
                'fa-ghost');
            return;
        }
        const terms = (data.terms || []).join(', ');
        out.innerHTML = `
            <div class="rc-research__summary">
                <div class="rc-research__big">${R.t('knowledge.research.mentions', { count: st.mentions })} ${R.t('knowledge.research.inRecords', { count: st.records })}
                    <span class="rc-research__terms">«${U.esc(terms)}»</span></div>
                <div class="rc-filterbar">${srcChips(st.by_source)}</div>
            </div>

            <div class="rc-research__actions">
                <button class="rc-research__exp" data-exp="originals">
                    <div class="rc-research__exp-ic"><i class="fa-solid fa-file-arrow-down"></i></div>
                    <div>
                        <div class="rc-research__exp-t">${R.t('knowledge.research.expOrig')}</div>
                        <div class="rc-research__exp-s">${R.t('knowledge.research.expOrigSub')}</div>
                    </div>
                </button>
                <button class="rc-research__exp" data-exp="summary">
                    <div class="rc-research__exp-ic"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
                    <div>
                        <div class="rc-research__exp-t">${R.t('knowledge.research.expSum')}</div>
                        <div class="rc-research__exp-s">${R.t('knowledge.research.expSumSub')}</div>
                    </div>
                </button>
            </div>

            <div id="rcResExport"></div>

            <details class="rc-research__details"${data.sample && data.sample.length ? '' : ' hidden'}>
                <summary>${R.t('knowledge.research.found')} (${st.records}${data.truncated ? R.t('knowledge.research.firstFifty') : ''})</summary>
                <div class="rc-list rc-research__list">${(data.sample || []).map(sampleRow).join('')}</div>
            </details>`;

        out.querySelector('[data-exp="originals"]').addEventListener('click', () => exportOriginals(ctx));
        out.querySelector('[data-exp="summary"]').addEventListener('click', () => exportSummary(ctx));
        out.querySelectorAll('[data-open]').forEach(a => {
            a.addEventListener('click', () => R.router.navigate('/transcript/' + a.dataset.open));
        });
    }

    function sampleRow(r) {
        const ic = U.SRC_ICON[r.source_type] || 'fa-solid fa-file-lines';
        const who = r.who ? ` · ${U.esc(r.who)}` : '';
        const badge = r.title_only ? `<span class="rc-research__badge">${R.t('knowledge.research.inTitle')}</span>` : `<span class="rc-research__badge">${r.mentions}×</span>`;
        return `<div class="rc-research__rowi" data-open="${r.id}">
            <i class="${ic} rc-research__rowic"></i>
            <div class="rc-research__rowbody">
                <div class="rc-research__rowt">${U.esc(r.title)}</div>
                <div class="rc-research__rowm rc-mono">${U.esc(r.date)} · ${U.esc(U.SRC_LABEL[r.source_type] || r.source_type)}${who}</div>
            </div>
            ${badge}
        </div>`;
    }

    // ---- exports delegate to the shared engine (R.researchExport) ----
    async function exportOriginals(ctx) {
        const box = ctx.mount.querySelector('#rcResExport');
        const btn = ctx.mount.querySelector('[data-exp="originals"]');
        if (btn) btn.disabled = true;
        try {
            await R.researchExport.runOriginals(
                Object.assign({ q: state.q }, axes()), box, ctx);
        } catch (_) { /* error already rendered in the box */ }
        finally { if (btn) btn.disabled = false; }
    }

    async function exportSummary(ctx) {
        const box = ctx.mount.querySelector('#rcResExport');
        const btn = ctx.mount.querySelector('[data-exp="summary"]');
        if (btn) btn.disabled = true;
        if (abortCtl) { try { abortCtl.abort(); } catch (_) {} }
        abortCtl = new AbortController();
        try {
            await R.researchExport.runSummary(
                Object.assign({ q: state.q, model: state.model }, axes()),
                box, ctx, abortCtl.signal, state.q);
        } finally { if (btn) btn.disabled = false; }
    }

    R.views.research = {
        render,
        destroy() {
            if (abortCtl) { try { abortCtl.abort(); } catch (_) {} abortCtl = null; }
            state = null; lastPreview = null;
        },
    };
})();
