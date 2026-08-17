/* Recall — "Дослідження бренду / великий експорт". Збирає з УСІХ джерел
   (аудіо, документи, Telegram, YouTube, записи) кожну згадку бренду/теми
   і віддає у двох формах:
     • Оригінали (.md) — дослівні фрагменти за датою, 0 токенів, без AI.
     • Саммарі (.md)   — структурований звіт дешевою моделлю (Haiku) ПО
                          фрагментах (не по цілих стенограмах) → мінімум токенів.

   Пошук лексичний: варіанти написання через кому (Horeca, хорека, HoReCa).
   Стан (запит/напрямок) живе в URL — deep-linkable. SSE саммарі — через
   api.researchSummary (fetch-reader, НЕ EventSource — recall_sse_connection_gotcha). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const MODELS = [
        { v: 'haiku', l: 'Haiku 4.5 — дешева, швидка (рекомендовано)' },
        { v: 'sonnet', l: 'Sonnet 4.6 — якісніша, дорожча' },
    ];

    let state = null, abortCtl = null, lastPreview = null;

    async function render(ctx) {
        state = {
            q: ctx.query.q || '',
            category_id: ctx.query.category_id || '',
            model: ctx.query.model || 'haiku',
        };
        lastPreview = null;

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-magnifying-glass-chart"></i> Архів · Дослідження</div>
                <h1 class="rc-pagehead__title">Дослідження бренду</h1>
                <p class="rc-pagehead__lede">Зібрати з УСІХ джерел (дзвінки, документи, Telegram, YouTube, записи) кожну згадку бренду чи теми — і вивантажити у великий <b>.md</b>: дослівні оригінали за датою або стисле AI-саммарі.</p>
            </div>

            <section class="rc-research__form">
                <label class="rc-research__lbl">Бренд / тема <span class="rc-research__hint">варіанти написання через кому</span></label>
                <input class="rc-input" id="rcResQ" type="text" autocomplete="off"
                    placeholder="Horeca, хорека, HoReCa" value="${U.esc(state.q)}">

                <div class="rc-research__row">
                    <div class="rc-research__field">
                        <label class="rc-research__lbl">Напрямок</label>
                        <select class="rc-select rc-catsel" id="rcResCat" data-cat-first="all"><option value="">Усі напрямки</option></select>
                    </div>
                    <div class="rc-research__field">
                        <label class="rc-research__lbl">Модель саммарі</label>
                        <select class="rc-select" id="rcResModel">
                            ${MODELS.map(m => `<option value="${m.v}"${m.v === state.model ? ' selected' : ''}>${m.l}</option>`).join('')}
                        </select>
                    </div>
                    <button class="rc-btn rc-btn--primary" id="rcResFind"><i class="fa-solid fa-magnifying-glass"></i> Знайти згадки</button>
                </div>
            </section>

            <div id="rcResOut"></div>`;

        loadCategories(ctx);
        const qEl = ctx.mount.querySelector('#rcResQ');
        const find = ctx.mount.querySelector('#rcResFind');
        find.addEventListener('click', () => preview(ctx));
        qEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); preview(ctx); } });
        ctx.mount.querySelector('#rcResModel').addEventListener('change', (e) => { state.model = e.target.value; sync(); });
        ctx.mount.querySelector('#rcResCat').addEventListener('change', (e) => { state.category_id = e.target.value; sync(); });
        qEl.focus();

        if (state.q.trim()) preview(ctx);
    }

    async function loadCategories(ctx) {
        const sel = ctx.mount.querySelector('#rcResCat');
        if (!sel) return;
        try {
            const d = await R.api.categories();
            if (!ctx.isCurrent()) return;
            const cats = d.categories || [];
            sel.innerHTML = `<option value="">Усі напрямки</option>`
                + cats.map(c => `<option value="${c.id}">${U.esc(c.name)}${c.count != null ? ` (${c.count})` : ''}</option>`).join('')
                + (d.uncategorized ? `<option value="none">Без напрямку (${d.uncategorized})</option>` : '')
                + `<option value="${UI.CAT_NEW}" class="rc-opt-new">＋ Новий напрямок…</option>`;
            sel.value = state.category_id || '';
        } catch (_) { /* лишаємо тільки «Усі напрямки» */ }
    }

    function sync() {
        const p = new URLSearchParams();
        if (state.q) p.set('q', state.q);
        if (state.category_id) p.set('category_id', state.category_id);
        if (state.model && state.model !== 'haiku') p.set('model', state.model);
        const s = p.toString();
        R.router.replace('/research' + (s ? '?' + s : ''));
    }

    async function preview(ctx) {
        const qEl = ctx.mount.querySelector('#rcResQ');
        state.q = (qEl.value || '').trim();
        sync();
        const out = ctx.mount.querySelector('#rcResOut');
        if (!state.q) { out.innerHTML = UI.empty('Введіть бренд або тему', 'Напр.: Horeca, хорека, HoReCa', 'fa-magnifying-glass'); return; }
        out.innerHTML = `<div class="rc-research__status"><i class="fa-solid fa-spinner fa-spin"></i> Шукаю згадки по всьому архіву…</div>`;
        try {
            const data = await R.api.researchPreview({ q: state.q, category_id: state.category_id || undefined });
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
            out.innerHTML = UI.empty('Згадок не знайдено',
                `За «${U.esc((data.terms || []).join(', '))}» нічого. Спробуйте інші варіанти написання або зніміть фільтр напрямку.`, 'fa-ghost');
            return;
        }
        const terms = (data.terms || []).join(', ');
        out.innerHTML = `
            <div class="rc-research__summary">
                <div class="rc-research__big"><b>${st.mentions}</b> згадок у <b>${st.records}</b> записах
                    <span class="rc-research__terms">«${U.esc(terms)}»</span></div>
                <div class="rc-filterbar">${srcChips(st.by_source)}</div>
            </div>

            <div class="rc-research__actions">
                <button class="rc-research__exp" data-exp="originals">
                    <div class="rc-research__exp-ic"><i class="fa-solid fa-file-arrow-down"></i></div>
                    <div>
                        <div class="rc-research__exp-t">Експорт оригіналів</div>
                        <div class="rc-research__exp-s">Дослівні фрагменти за датою (свіже згори) + посилання на оригінали. <b>0 токенів, без AI.</b></div>
                    </div>
                </button>
                <button class="rc-research__exp" data-exp="summary">
                    <div class="rc-research__exp-ic"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
                    <div>
                        <div class="rc-research__exp-t">AI-саммарі</div>
                        <div class="rc-research__exp-s">Структурований звіт по всіх джерелах. Дешева модель, тільки по знайдених фрагментах.</div>
                    </div>
                </button>
            </div>

            <div id="rcResExport"></div>

            <details class="rc-research__details"${data.sample && data.sample.length ? '' : ' hidden'}>
                <summary>Знайдені записи (${st.records}${data.truncated ? ', показано перші 50' : ''})</summary>
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
        const badge = r.title_only ? `<span class="rc-research__badge">у назві</span>` : `<span class="rc-research__badge">${r.mentions}×</span>`;
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
                { q: state.q, category_id: state.category_id || undefined }, box, ctx);
        } catch (_) { /* помилка вже відрендерена у box */ }
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
                { q: state.q, category_id: state.category_id || undefined, model: state.model },
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
