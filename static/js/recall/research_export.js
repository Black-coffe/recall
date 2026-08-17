/* Recall — спільний рушій «великого експорту» (Phase 18).
   Використовується і сторінкою /research, і карткою сутності (/entities/:id).
   Рендерить статус/готово у переданий box-елемент; payload може бути
   {q,...} (вільний термін) або {entity_id} (сутність — бекенд сам резолвить
   ім'я+псевдоніми у терміни). SSE-саммарі — через api.researchSummary
   (fetch-reader, НЕ EventSource — recall_sse_connection_gotcha). */
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

    // Експорт оригіналів (0 токенів, без AI). payload: {q?|entity_id?, category_id?}.
    async function runOriginals(payload, box, ctx) {
        box.innerHTML = `<div class="rc-research__status"><i class="fa-solid fa-spinner fa-spin"></i> Збираю оригінали…</div>`;
        try {
            const data = await R.api.researchOriginals(payload);
            if (ctx && !ctx.isCurrent()) return;
            downloadMd(data.filename, data.markdown);
            const kb = Math.round((data.markdown || '').length / 1024);
            box.innerHTML = `<div class="rc-research__done">
                <i class="fa-solid fa-circle-check"></i> Завантажено <b>${U.esc(data.filename)}</b>
                — ${data.stats.records} записів, ~${kb} КБ.
                <button class="rc-btn rc-btn--sm" data-redl><i class="fa-solid fa-download"></i> Завантажити ще раз</button>
            </div>`;
            box.querySelector('[data-redl]').addEventListener('click', () => downloadMd(data.filename, data.markdown));
            UI.toast('Оригінали експортовано', 'success');
        } catch (err) {
            if (!ctx || ctx.isCurrent()) box.innerHTML = UI.error(err && err.message);
            throw err;
        }
    }

    // AI-саммарі (дешева модель, SSE map-reduce). payload: {q?|entity_id?, category_id?, model?}.
    async function runSummary(payload, box, ctx, signal, nameForFile) {
        box.innerHTML = `<div class="rc-research__status" data-st><i class="fa-solid fa-spinner fa-spin"></i> Готую саммарі…</div>`;
        const st = box.querySelector('[data-st]');
        try {
            await R.api.researchSummary(payload, (event, d) => {
                if (ctx && !ctx.isCurrent()) return;
                if (event === 'start') {
                    st.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Аналізую ${d.fragments} фрагментів з ${d.records} записів (${d.batches} ${d.batches === 1 ? 'батч' : 'батчі'}, ${U.esc(d.model || '')})…`;
                } else if (event === 'progress') {
                    st.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> ${U.esc(d.label || '')} <span class="rc-mono">(${d.step}/${d.total})</span>`;
                } else if (event === 'error') {
                    box.innerHTML = UI.error(d.error || 'Помилка саммарі');
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
                <i class="fa-solid fa-circle-check"></i> Саммарі готове.
                <span class="rc-mono rc-research__usage">${U.esc(d.model || '')} · ${d.input_tokens || 0}→${d.output_tokens || 0} ток. · ${cost}</span>
                <button class="rc-btn rc-btn--primary rc-btn--sm" data-dl><i class="fa-solid fa-download"></i> Завантажити .md</button>
            </div>
            <pre class="rc-research__md" data-md></pre>`;
        box.querySelector('[data-md]').textContent = d.markdown || '';
        box.querySelector('[data-dl]').addEventListener('click', () => downloadMd(fn, d.markdown || ''));
        downloadMd(fn, d.markdown || '');  // одразу віддаємо файл
        UI.toast('Саммарі згенеровано', 'success');
    }

    R.researchExport = { downloadMd, runOriginals, runSummary };
})();
