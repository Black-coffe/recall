/* Recall — shared "big export" engine (Phase 18).
   Used by both the /research page and the entity card (/entities/:id).
   Renders status/done into the passed box element; payload can be
   {q,...} (free term) or {entity_id} (entity — the backend resolves
   name+aliases into terms itself). SSE summary — via api.researchSummary
   (fetch-reader, NOT EventSource — recall_sse_connection_gotcha). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    function downloadMd(filename, text) {
        const blob = new Blob([text], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1500);
    }

    function slugName(s) {
        return (s || 'research').replace(/[^\wа-яіїєґ]+/gi, '-').replace(/^-+|-+$/g, '').toLowerCase() || 'research';
    }

    // Export of originals (0 tokens, no AI). payload: {q?|entity_id?, category_id?}.
    async function runOriginals(payload, box, ctx) {
        box.innerHTML = `<div class="rc-research__status"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('knowledge.export.collecting')}</div>`;
        try {
            const data = await R.api.researchOriginals(payload);
            if (ctx && !ctx.isCurrent()) return;
            downloadMd(data.filename, data.markdown);
            const kb = Math.round((data.markdown || '').length / 1024);
            box.innerHTML = `<div class="rc-research__done">
                <i class="fa-solid fa-circle-check"></i> ${R.t('knowledge.export.downloaded', { file: U.esc(data.filename), records: R.t('knowledge.export.records', { count: data.stats.records }), kb: kb })}
                <button class="rc-btn rc-btn--sm" data-redl><i class="fa-solid fa-download"></i> ${R.t('knowledge.export.again')}</button>
            </div>`;
            box.querySelector('[data-redl]').addEventListener('click', () => downloadMd(data.filename, data.markdown));
            UI.toast(R.t('knowledge.export.originalsDone'), 'success');
        } catch (err) {
            if (!ctx || ctx.isCurrent()) box.innerHTML = UI.error(err && err.message);
            throw err;
        }
    }

    // AI summary (cheap model, SSE map-reduce). payload: {q?|entity_id?, category_id?, model?}.
    async function runSummary(payload, box, ctx, signal, nameForFile) {
        box.innerHTML = `<div class="rc-research__status" data-st><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('knowledge.export.preparing')}</div>`;
        const st = box.querySelector('[data-st]');
        try {
            await R.api.researchSummary(payload, (event, d) => {
                if (ctx && !ctx.isCurrent()) return;
                if (event === 'start') {
                    st.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${R.t('knowledge.export.analyzing', { fragments: d.fragments, records: d.records, batches: R.t('knowledge.export.batches', { count: d.batches }), model: U.esc(d.model || '') })}`;
                } else if (event === 'progress') {
                    st.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${U.esc(d.label || '')} <span class="rc-mono">(${d.step}/${d.total})</span>`;
                } else if (event === 'error') {
                    box.innerHTML = UI.error(d.error || R.t('knowledge.export.summaryError'));
                } else if (event === 'done') {
                    summaryDone(box, d, nameForFile);
                }
            }, signal);
        } catch (err) {
            if (!ctx || ctx.isCurrent()) box.innerHTML = UI.error(err && err.message);
        }
    }

    function summaryDone(box, d, nameForFile) {
        const cost = (d.cost != null) ? `≈ $${Number(d.cost).toFixed(d.cost < 0.01 ? 4 : 2)}` : '';
        const fn = `recall-${slugName(nameForFile)}-summary.md`;
        box.innerHTML = `
            <div class="rc-research__done">
                <i class="fa-solid fa-circle-check"></i> ${R.t('knowledge.export.summaryReady')}
                <span class="rc-mono rc-research__usage">${U.esc(d.model || '')} · ${d.input_tokens || 0}→${d.output_tokens || 0} ${R.t('knowledge.export.tokens')} · ${cost}</span>
                <button class="rc-btn rc-btn--primary rc-btn--sm" data-dl><i class="fa-solid fa-download"></i> ${R.t('knowledge.export.downloadMd')}</button>
            </div>
            <pre class="rc-research__md" data-md></pre>`;
        box.querySelector('[data-md]').textContent = d.markdown || '';
        box.querySelector('[data-dl]').addEventListener('click', () => downloadMd(fn, d.markdown || ''));
        downloadMd(fn, d.markdown || '');  // hand the file over right away
        UI.toast(R.t('knowledge.export.summaryToast'), 'success');
    }

    R.researchExport = { downloadMd, runOriginals, runSummary };
})();
