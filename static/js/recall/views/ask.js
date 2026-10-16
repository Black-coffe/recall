/* Recall — "Запитай архів" (RAG). Streams a grounded answer with citations
   from the existing POST /api/memory/ask/stream SSE endpoint. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    let abortCtl = null, busy = false, state = null;

    // C9: three different filter axes — the same explanation text as on
    // Library/Media library/Tasks/Graph/Research.
    const FILTER_HINT = () => R.t('knowledge.ask.filterHint');

    async function render(ctx) {
        if (abortCtl) { try { abortCtl.abort(); } catch (_) {} abortCtl = null; }
        busy = false;
        await UI.loadCategories();
        if (UI.loadTags) await UI.loadTags();
        const preset = ctx.query.q || '';
        state = {
            category_id: ctx.query.category_id || '',
            tags: (ctx.query.tags || '').split(',').map(x => x.trim()).filter(Boolean),
            since: ctx.query.since || '',
            until: ctx.query.until || '',
        };
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">${R.t('knowledge.ask.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('knowledge.ask.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('knowledge.ask.lede')}</p>
            </div>
            <div class="rc-ask__bar">
                <textarea id="rcAskInput" placeholder="${U.esc(R.t('knowledge.ask.placeholder'))}">${U.esc(preset)}</textarea>
                <button class="rc-btn rc-btn--primary" id="rcAskGo"><i class="fa-solid fa-paper-plane"></i> ${R.t('knowledge.ask.go')}</button>
            </div>
            <div class="rc-toolbar" id="rcAskFilters">
                <select class="rc-select rc-catsel" id="rcAskCat" data-cat-first="all">${UI.catOptions(state.category_id, { first: 'all' })}</select>
                <div id="rcAskTagsWrap">${UI.tagOptions ? UI.tagOptions(state.tags) : '<select multiple class="rc-tagsel"></select>'}</div>
                <input type="date" class="rc-input rc-select--sm" id="rcAskSince" value="${U.esc(state.since)}" title="${U.esc(R.t('knowledge.ask.sinceTitle'))}">
                <input type="date" class="rc-input rc-select--sm" id="rcAskUntil" value="${U.esc(state.until)}" title="${U.esc(R.t('knowledge.ask.untilTitle'))}">
            </div>
            <div id="rcAskHint"></div>
            <div id="rcAskOut"></div>`;

        const input = ctx.mount.querySelector('#rcAskInput');
        const go = ctx.mount.querySelector('#rcAskGo');
        const catSel = ctx.mount.querySelector('#rcAskCat');
        catSel.value = state.category_id;
        catSel.addEventListener('change', () => { state.category_id = catSel.value; sync(input.value); });
        const tagSel = ctx.mount.querySelector('#rcAskTagsWrap select');
        if (tagSel) { tagSel.setAttribute('aria-label', R.t('knowledge.ask.tagsAria')); tagSel.title = R.t('knowledge.ask.tagsTitle'); }
        if (tagSel) tagSel.addEventListener('change', () => {
            state.tags = Array.from(tagSel.selectedOptions).map(o => o.value).filter(Boolean);
            sync(input.value);
        });
        const sinceEl = ctx.mount.querySelector('#rcAskSince');
        const untilEl = ctx.mount.querySelector('#rcAskUntil');
        sinceEl.addEventListener('change', () => { state.since = sinceEl.value; sync(input.value); });
        untilEl.addEventListener('change', () => { state.until = untilEl.value; sync(input.value); });
        const hintBox = ctx.mount.querySelector('#rcAskHint');
        if (hintBox && UI.hint) hintBox.appendChild(UI.hint(FILTER_HINT()));
        go.addEventListener('click', () => ask(ctx, input.value));
        input.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); ask(ctx, input.value); }
        });
        input.focus();
        if (preset.trim()) ask(ctx, preset);
    }

    // Filters and the question live in the URL — a question with the same slice can be
    // sent as a link (the deep link restores both the controls and the query).
    function sync(question) {
        const p = new URLSearchParams();
        const q = (question || '').trim();
        if (q) p.set('q', q);
        if (state.category_id) p.set('category_id', state.category_id);
        if (state.tags.length) p.set('tags', state.tags.join(','));
        if (state.since) p.set('since', state.since);
        if (state.until) p.set('until', state.until);
        const qs = p.toString();
        R.router.replace('/ask' + (qs ? '?' + qs : ''));
    }

    // Explanation of the active slice — for "nothing found".
    function filterExplain() {
        const bits = [];
        if (state.category_id && UI.catPath) { const p = UI.catPath(Number(state.category_id)); if (p) bits.push(R.t('knowledge.ask.explainIn', { path: p })); }
        if (state.tags.length) bits.push(R.t('knowledge.ask.explainTags', { tags: state.tags.join(', ') }));
        if (state.since || state.until) bits.push(R.t('knowledge.ask.explainPeriod', { from: state.since || '…', to: state.until || '…' }));
        if (!bits.length) return null;
        return R.t('knowledge.ask.explain', { bits: bits.join(' ') });
    }

    function setBusy(ctx, flag) {
        const go = ctx.mount.querySelector('#rcAskGo');
        if (!go) return;
        go.disabled = flag;
        go.innerHTML = flag
            ? '<i class="fa-solid fa-spinner fa-spin"></i> ' + R.t('knowledge.ask.wait')
            : '<i class="fa-solid fa-paper-plane"></i> ' + R.t('knowledge.ask.go');
    }

    async function ask(ctx, q) {
        q = (q || '').trim();
        const out = ctx.mount.querySelector('#rcAskOut');
        if (!q || busy) return;   // block: a request is already in flight
        sync(q);

        if (abortCtl) { try { abortCtl.abort(); } catch (_) {} }   // defensive — see T4.1/T4.2
        const myCtl = new AbortController();
        abortCtl = myCtl;
        busy = true;
        setBusy(ctx, true);

        // request-local sources — NOT a shared module variable, so a late
        // callback from a previous/aborted question can never bleed its
        // citation numbering into this answer (T4.2 race fix).
        const sources = [];

        out.innerHTML = `<div class="rc-ask__answer" id="rcAnswer"><span class="rc-mono" style="color:var(--rc-ink-3)"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('knowledge.ask.searching')}</span></div>
            <div id="rcAskRate"></div>
            <div id="rcAskSources" style="margin-top:16px"></div>`;
        const answerEl = out.querySelector('#rcAnswer');
        let text = '';
        try {
            const payload = { question: q };
            if (state.category_id) payload.category_id = state.category_id;
            if (state.tags.length) payload.tags = state.tags.join(',');
            if (state.since) payload.since = state.since;
            if (state.until) payload.until = state.until;
            await R.api.askStream(payload, (event, data) => {
                if (myCtl.signal.aborted || !ctx.isCurrent()) return;
                if (event === 'sources') {
                    sources.push(...(data.sources || []));
                    renderSources(ctx, sources);
                } else if (event === 'delta') {
                    text += (data.text || '');
                    answerEl.textContent = text;  // textContent — safe; plain answer text
                } else if (event === 'error') {
                    answerEl.innerHTML = UI.error(
                        data.code === 'sse_broken'
                            ? R.t('knowledge.ask.connBroken')
                            : (data.error || R.t('knowledge.ask.ragError')));
                } else if (event === 'done') {
                    if (!text) answerEl.textContent = R.t('knowledge.ask.empty');
                    if (!sources.length) {
                        const why = filterExplain();
                        if (why) {
                            const note = document.createElement('div');
                            note.className = 'rc-mono';
                            note.style.cssText = 'margin-top:10px;font-size:var(--rc-t-xs);color:var(--rc-ink-3);line-height:1.6';
                            note.textContent = why;
                            answerEl.appendChild(note);
                        }
                    }
                    renderCitations(ctx, answerEl, text, sources);
                    renderRating(ctx, data.ask_id);
                }
            }, myCtl.signal, { idleTimeoutMs: 30000 });
        } catch (err) {
            // a real abrupt disconnect (network drop / idle watchdog / missing
            // terminal frame) rejects the promise — a caller-initiated abort
            // (myCtl.signal.aborted, e.g. leaving the page) resolves silently
            // and never lands here.
            if (ctx.isCurrent() && !myCtl.signal.aborted) {
                answerEl.innerHTML = UI.error((err && err.message) || R.t('knowledge.ask.connBroken'));
            }
        } finally {
            if (abortCtl === myCtl) { abortCtl = null; busy = false; setBusy(ctx, false); }
        }
    }

    // turn [1],[2] markers in the answer into clickable citation chips
    function renderCitations(ctx, answerEl, text, sources) {
        if (!sources.length) return;
        const safe = U.esc(text).replace(/\[(\d{1,2})\]/g, (m, n) => {
            const i = parseInt(n, 10) - 1;
            if (i < 0 || i >= sources.length) return m;
            return `<sup class="rc-ask__cite" data-i="${i}">[${n}]</sup>`;
        });
        answerEl.innerHTML = safe;
        answerEl.querySelectorAll('.rc-ask__cite').forEach(el => {
            el.addEventListener('click', () => {
                const src = sources[parseInt(el.dataset.i, 10)];
                if (src && src.transcription_id) R.router.navigate('/transcript/' + U.slug(src.transcription_id, src.display_name || src.source_name));
            });
        });
    }

    // 👍/👎 + a note under the answer. This is not a "like" but golden-set labeling:
    // once a week the owner moves a dozen questions from here into the set, and 👎
    // go there first. Without ask_id (the log was not written) we do not show the block —
    // a button that leads nowhere is worse than its absence.
    function renderRating(ctx, askId) {
        const box = ctx.mount.querySelector('#rcAskRate');
        if (!box || !askId) return;
        box.innerHTML = `<div class="rc-ask__rate">
                <span class="rc-ask__rate-lede">${R.t('knowledge.ask.rateLede')}</span>
                <button class="rc-btn rc-btn--ghost" data-rating="1" title="${U.esc(R.t('knowledge.ask.rateGood'))}">👍</button>
                <button class="rc-btn rc-btn--ghost" data-rating="-1" title="${U.esc(R.t('knowledge.ask.rateBad'))}">👎</button>
                <input type="text" class="rc-ask__rate-note" placeholder="${U.esc(R.t('knowledge.ask.ratePlaceholder'))}">
                <span class="rc-ask__rate-state"></span>
            </div>`;
        const stateEl = box.querySelector('.rc-ask__rate-state');
        const noteEl = box.querySelector('.rc-ask__rate-note');
        box.querySelectorAll('button[data-rating]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const rating = parseInt(btn.dataset.rating, 10);
                box.querySelectorAll('button[data-rating]').forEach(b => b.disabled = true);
                try {
                    await R.api.rateAsk(askId, rating, noteEl.value.trim());
                    box.querySelectorAll('button[data-rating]').forEach(b =>
                        b.classList.toggle('rc-btn--primary', b === btn));
                    stateEl.textContent = R.t('knowledge.ask.rated');
                } catch (err) {
                    stateEl.textContent = (err && err.message) || R.t('knowledge.ask.rateFail');
                } finally {
                    box.querySelectorAll('button[data-rating]').forEach(b => b.disabled = false);
                }
            });
        });
    }

    function renderSources(ctx, sources) {
        const box = ctx.mount.querySelector('#rcAskSources');
        if (!box || !sources.length) return;
        box.innerHTML = `<div class="rc-eyebrow" style="margin-bottom:8px">${R.t('knowledge.ask.sources')} · ${sources.length}</div>
            <div class="rc-tags">${sources.map((s, i) =>
                `<a class="rc-entity" href="/transcript/${U.slug(s.transcription_id, s.display_name || s.source_name)}">
                    <span class="rc-entity__type">[${i + 1}]</span>${U.esc(s.display_name || s.source_name || ('#' + s.transcription_id))}</a>`
            ).join('')}</div>`;
    }

    R.views.ask = {
        render,
        destroy() {
            if (abortCtl) { try { abortCtl.abort(); } catch (_) {} abortCtl = null; }
            busy = false; state = null;
        },
    };
})();
