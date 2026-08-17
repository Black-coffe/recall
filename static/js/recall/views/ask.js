/* Recall — "Запитай архів" (RAG). Streams a grounded answer with citations
   from the existing POST /api/memory/ask/stream SSE endpoint. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    let abortCtl = null, busy = false;

    async function render(ctx) {
        if (abortCtl) { try { abortCtl.abort(); } catch (_) {} abortCtl = null; }
        busy = false;
        const preset = ctx.query.q || '';
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">RAG · по всьому архіву</div>
                <h1 class="rc-pagehead__title">Запитай архів</h1>
                <p class="rc-pagehead__lede">Відповіді будуються СУВОРО з вашого архіву, з клікабельними цитатами на конкретні записи.</p>
            </div>
            <div class="rc-ask__bar">
                <textarea id="rcAskInput" placeholder="Спитай: «що вирішили по фонду?», «які задачі на Юлю?», «де про окупність Acmecorp?»…">${U.esc(preset)}</textarea>
                <button class="rc-btn rc-btn--primary" id="rcAskGo"><i class="fa-solid fa-paper-plane"></i> Спитати</button>
            </div>
            <div id="rcAskOut"></div>`;

        const input = ctx.mount.querySelector('#rcAskInput');
        const go = ctx.mount.querySelector('#rcAskGo');
        go.addEventListener('click', () => ask(ctx, input.value));
        input.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); ask(ctx, input.value); }
        });
        input.focus();
        if (preset.trim()) ask(ctx, preset);
    }

    function setBusy(ctx, flag) {
        const go = ctx.mount.querySelector('#rcAskGo');
        if (!go) return;
        go.disabled = flag;
        go.innerHTML = flag
            ? '<i class="fa-solid fa-spinner fa-spin"></i> Зачекайте…'
            : '<i class="fa-solid fa-paper-plane"></i> Спитати';
    }

    async function ask(ctx, q) {
        q = (q || '').trim();
        const out = ctx.mount.querySelector('#rcAskOut');
        if (!q || busy) return;   // block: a request is already in flight

        if (abortCtl) { try { abortCtl.abort(); } catch (_) {} }   // defensive — see T4.1/T4.2
        const myCtl = new AbortController();
        abortCtl = myCtl;
        busy = true;
        setBusy(ctx, true);

        // request-local sources — NOT a shared module variable, so a late
        // callback from a previous/aborted question can never bleed its
        // citation numbering into this answer (T4.2 race fix).
        const sources = [];

        out.innerHTML = `<div class="rc-ask__answer" id="rcAnswer"><span class="rc-mono" style="color:var(--rc-ink-3)"><i class="fa-solid fa-spinner fa-spin"></i> Шукаю в архіві…</span></div>
            <div id="rcAskSources" style="margin-top:16px"></div>`;
        const answerEl = out.querySelector('#rcAnswer');
        let text = '';
        try {
            await R.api.askStream({ question: q }, (event, data) => {
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
                            ? 'Зʼєднання перервано, спробуйте знову'
                            : (data.error || 'Помилка RAG'));
                } else if (event === 'done') {
                    if (!text) answerEl.textContent = 'Порожня відповідь.';
                    renderCitations(ctx, answerEl, text, sources);
                }
            }, myCtl.signal, { idleTimeoutMs: 30000 });
        } catch (err) {
            // a real abrupt disconnect (network drop / idle watchdog / missing
            // terminal frame) rejects the promise — a caller-initiated abort
            // (myCtl.signal.aborted, e.g. leaving the page) resolves silently
            // and never lands here.
            if (ctx.isCurrent() && !myCtl.signal.aborted) {
                answerEl.innerHTML = UI.error((err && err.message) || 'Зʼєднання перервано, спробуйте знову');
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
                if (src && src.transcription_id) R.router.navigate('/transcript/' + U.slug(src.transcription_id, src.source_name));
            });
        });
    }

    function renderSources(ctx, sources) {
        const box = ctx.mount.querySelector('#rcAskSources');
        if (!box || !sources.length) return;
        box.innerHTML = `<div class="rc-eyebrow" style="margin-bottom:8px">Джерела · ${sources.length}</div>
            <div class="rc-tags">${sources.map((s, i) =>
                `<a class="rc-entity" href="/transcript/${U.slug(s.transcription_id, s.source_name)}">
                    <span class="rc-entity__type">[${i + 1}]</span>${U.esc(s.source_name || ('#' + s.transcription_id))}</a>`
            ).join('')}</div>`;
    }

    R.views.ask = {
        render,
        destroy() {
            if (abortCtl) { try { abortCtl.abort(); } catch (_) {} abortCtl = null; }
            busy = false;
        },
    };
})();
