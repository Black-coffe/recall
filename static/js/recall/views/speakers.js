/* Recall — "Спікери" (граф архіву). Розширене керування спікерами діаризації:
   список зі статистикою (скільки часу/записів/слів), додавання, перейменування,
   видалення, ОБʼЄДНАННЯ дублікатів (pyannote часто плодить SPEAKER_NN, які
   насправді одна людина) та ТАЙМЛАЙН активності по днях.

   Бекенд: /api/speakers/stats, /api/speakers/<id>/timeline, /api/speakers/merge
   (раніше доступні лише у /legacy — Phase 18 порт). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    let state = null;

    async function render(ctx) {
        state = { merge: false, selected: new Set(), speakers: null };
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-users"></i> Граф архіву</div>
                <h1 class="rc-pagehead__title">Спікери</h1>
                <p class="rc-pagehead__lede">Хто говорить у ваших записах: скільки часу, у скількох записах, скільки слів. Перейменовуйте, обʼєднуйте дублікати діаризації та дивіться активність по днях.</p>
            </div>
            <div class="rc-toolbar">
                <button class="rc-btn rc-btn--sm" id="rcSpkAdd"><i class="fa-solid fa-plus"></i> Додати спікера</button>
                <button class="rc-btn rc-btn--sm" id="rcSpkMerge"><i class="fa-solid fa-code-merge"></i> Обʼєднати</button>
            </div>
            <div id="rcSpkAddBar"></div>
            <div id="rcSpkMergeBar"></div>
            <div class="rc-list" id="rcSpkList">${UI.skeletonList(5)}</div>`;

        ctx.mount.querySelector('#rcSpkAdd').addEventListener('click', () => toggleAdd(ctx));
        ctx.mount.querySelector('#rcSpkMerge').addEventListener('click', () => toggleMerge(ctx));
        load(ctx);
    }

    async function load(ctx) {
        const box = ctx.mount.querySelector('#rcSpkList');
        try {
            const data = await R.api.speakersStats();
            if (!ctx.isCurrent()) return;
            state.speakers = data.speakers || [];
            renderList(ctx);
        } catch (err) {
            if (ctx.isCurrent()) box.innerHTML = UI.error(err && err.message);
        }
    }

    function renderList(ctx) {
        const box = ctx.mount.querySelector('#rcSpkList');
        const sp = state.speakers || [];
        if (!sp.length) {
            box.innerHTML = UI.empty('Спікерів ще немає', 'Зʼявляться після діаризації записів або додайте вручну.', 'fa-users');
            updateMergeBar(ctx);
            return;
        }
        box.innerHTML = sp.map(rowHTML).join('');
        sp.forEach(s => bindRow(ctx, s));
        updateMergeBar(ctx);
    }

    function rowHTML(s) {
        const color = s.color || 'var(--rc-ink-3)';
        const checkbox = state.merge
            ? `<input type="checkbox" class="rc-spk__chk" data-chk="${s.id}"${s.is_self ? ' disabled title="«Ви» не можна обʼєднувати"' : ''}${state.selected.has(s.id) ? ' checked' : ''}>`
            : '';
        const stats = [
            `${s.transcripts_count || 0} зап.`,
            s.total_seconds ? U.fmtDuration(s.total_seconds) + ' розмови' : null,
            s.words_count ? `${s.words_count} слів` : null,
        ].filter(Boolean).join(' · ');
        return `<div class="rc-spk${s.is_self ? ' is-self' : ''}" data-id="${s.id}">
            <div class="rc-spk__main">
                ${checkbox}
                <span class="rc-spk__dot" style="background:${U.esc(color)}"></span>
                <div class="rc-spk__body">
                    <div class="rc-spk__name">${U.esc(s.name)}${s.is_self ? ' <span class="rc-tgrow__badge">Ви</span>' : ''}</div>
                    <div class="rc-spk__stats rc-mono">${stats || 'ще не у записах'}</div>
                </div>
                <div class="rc-spk__actions">
                    <button class="rc-iconbtn" data-act="timeline" title="Активність по днях"><i class="fa-solid fa-chart-simple"></i></button>
                    <button class="rc-iconbtn" data-act="rename" title="Перейменувати"><i class="fa-solid fa-pen"></i></button>
                    <button class="rc-iconbtn" data-act="delete" title="${s.is_self ? '«Ви» видалити не можна' : 'Видалити'}"${s.is_self ? ' disabled' : ''}><i class="fa-solid fa-trash"></i></button>
                </div>
            </div>
            <div class="rc-spk__timeline" hidden></div>`
            + `</div>`;
    }

    function bindRow(ctx, s) {
        const row = ctx.mount.querySelector(`.rc-spk[data-id="${s.id}"]`);
        if (!row) return;
        const nameEl = row.querySelector('.rc-spk__name');

        const chk = row.querySelector('[data-chk]');
        if (chk) chk.addEventListener('change', () => {
            if (chk.checked) state.selected.add(s.id); else state.selected.delete(s.id);
            updateMergeBar(ctx);
        });

        row.querySelector('[data-act="timeline"]').addEventListener('click', () => toggleTimeline(ctx, s, row));
        row.querySelector('[data-act="rename"]').addEventListener('click', async () => {
            const cur = s.name;
            const next = await UI.promptModal({
                title: 'Перейменувати спікера',
                label: 'Нове імʼя',
                defaultValue: cur,
                confirmLabel: 'Зберегти',
                required: true,
            });
            if (!next || next === cur) return;
            try { await R.api.speakerRename(s.id, next); UI.toast('Перейменовано', 'success'); load(ctx); }
            catch (e) { UI.toast(e.message, 'error'); }
        });
        const del = row.querySelector('[data-act="delete"]');
        if (del && !s.is_self) del.addEventListener('click', async () => {
            const ok = await UI.confirmModal({
                title: 'Видалити спікера',
                message: `Видалити спікера «${s.name}»? Привʼязки в записах скинуться (стануть «Спікер N»).`,
                confirmLabel: 'Видалити',
            });
            if (!ok) return;
            try { await R.api.speakerDelete(s.id); UI.toast('Спікера видалено', 'success'); state.selected.delete(s.id); load(ctx); }
            catch (e) { UI.toast(e.message, 'error'); }
        });
    }

    // ---- timeline (activity by day) ----
    async function toggleTimeline(ctx, s, row) {
        const tl = row.querySelector('.rc-spk__timeline');
        if (!tl.hidden) { tl.hidden = true; tl.innerHTML = ''; return; }
        tl.hidden = false;
        tl.innerHTML = `<div class="rc-mono" style="color:var(--rc-ink-3)"><i class="fa-solid fa-spinner fa-spin"></i> Завантажую активність…</div>`;
        try {
            const d = await R.api.speakerTimeline(s.id, 30);
            if (!ctx.isCurrent()) return;
            tl.innerHTML = timelineHTML(d);
        } catch (e) { tl.innerHTML = UI.error(e.message); }
    }

    function timelineHTML(d) {
        const tl = d.timeline || [], sum = d.summary || {};
        if (!tl.length) return `<div class="rc-mono" style="color:var(--rc-ink-3)">За останні ${d.days} днів активності немає.</div>`;
        const max = Math.max.apply(null, tl.map(x => x.total_seconds || 0).concat([1]));
        const bars = tl.map(x => {
            const h = Math.max(4, Math.round((x.total_seconds || 0) / max * 100));
            const tip = `${x.date} · ${U.fmtDuration(x.total_seconds)} · ${x.words_count} слів · ${x.transcripts_count} зап.`;
            return `<div class="rc-tl__bar" style="height:${h}%" title="${U.esc(tip)}"></div>`;
        }).join('');
        return `<div class="rc-spk__tlsum rc-mono">За ${d.days} днів: <b>${U.fmtDuration(sum.total_seconds || 0)}</b> розмови · ${sum.total_words || 0} слів · ${sum.total_transcripts || 0} записів · активних днів ${sum.active_days || 0}</div>
            <div class="rc-tl">${bars}</div>
            <div class="rc-tl__axis rc-mono"><span>${U.esc(tl[0].date)}</span><span>${U.esc(tl[tl.length - 1].date)}</span></div>`;
    }

    // ---- add speaker ----
    function toggleAdd(ctx) {
        const bar = ctx.mount.querySelector('#rcSpkAddBar');
        if (bar.innerHTML) { bar.innerHTML = ''; return; }
        bar.innerHTML = `<div class="rc-newcat">
            <input class="rc-input" id="rcSpkNew" placeholder="Імʼя спікера…" autocomplete="off">
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcSpkNewSave">Додати</button>
            <button class="rc-btn rc-btn--sm" id="rcSpkNewCancel">Скасувати</button>
        </div>`;
        const input = bar.querySelector('#rcSpkNew');
        input.focus();
        const close = () => { bar.innerHTML = ''; };
        const save = async () => {
            const name = input.value.trim();
            if (!name) return;
            try { await R.api.speakerCreate(name); close(); load(ctx); UI.toast(`Спікера «${name}» додано`, 'success'); }
            catch (e) { UI.toast(e.message || 'Не вдалося додати', 'error'); }
        };
        bar.querySelector('#rcSpkNewSave').addEventListener('click', save);
        bar.querySelector('#rcSpkNewCancel').addEventListener('click', close);
        input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } else if (e.key === 'Escape') close(); });
    }

    // ---- merge mode ----
    function toggleMerge(ctx) {
        state.merge = !state.merge;
        state.selected.clear();
        ctx.mount.querySelector('#rcSpkMerge').classList.toggle('is-active', state.merge);
        renderList(ctx);
    }

    function updateMergeBar(ctx) {
        const bar = ctx.mount.querySelector('#rcSpkMergeBar');
        if (!bar) return;
        if (!state.merge) { bar.innerHTML = ''; return; }
        const sel = (state.speakers || []).filter(s => state.selected.has(s.id));
        if (sel.length < 2) {
            bar.innerHTML = `<div class="rc-mergebar"><i class="fa-solid fa-circle-info"></i> Позначте <b>2+ спікери</b> (дублікати), щоб обʼєднати їх в одного.</div>`;
            return;
        }
        const keep = sel.slice().sort((a, b) => (b.usage_count || 0) - (a.usage_count || 0))[0];
        bar.innerHTML = `<div class="rc-mergebar">
            <span>Обʼєднати <b>${sel.length}</b> в одного, залишити:</span>
            <select class="rc-select rc-select--sm" id="rcMergeKeep">${sel.map(s => `<option value="${s.id}"${s.id === keep.id ? ' selected' : ''}>${U.esc(s.name)}</option>`).join('')}</select>
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcMergeGo"><i class="fa-solid fa-code-merge"></i> Обʼєднати</button>
            <button class="rc-btn rc-btn--sm" id="rcMergeCancel">Очистити</button>
        </div>`;
        bar.querySelector('#rcMergeGo').addEventListener('click', () => doMerge(ctx));
        bar.querySelector('#rcMergeCancel').addEventListener('click', () => { state.selected.clear(); renderList(ctx); });
    }

    // Приклади фраз спікера для preview перед обʼєднанням (щоб було видно,
    // кого саме зливаємо — різні люди можуть мати схожі голоси діаризації).
    async function fetchSamples(id) {
        try { return await R.api.get(`/api/speakers/${id}/samples?limit=3`); }
        catch (_) { return null; }
    }

    function mergePreviewHTML(speakers, keepId, mergeIds, samplesById) {
        const byId = {}; (speakers || []).forEach(s => { byId[s.id] = s; });
        const allIds = [keepId, ...mergeIds];
        const blocks = allIds.map(id => {
            const s = byId[id] || {};
            const data = samplesById[id];
            const items = (data && data.samples) || [];
            const roleLabel = id === keepId ? ' — залишиться' : ' — буде обʼєднано сюди';
            const list = items.length
                ? items.map(x => {
                    const text = (x.text || '').trim();
                    const short = text.length > 160 ? text.slice(0, 160) + '…' : text;
                    return `<li>${U.esc(short)}</li>`;
                }).join('')
                : '<li><i>Прикладів фраз не знайдено</i></li>';
            return `<div style="margin-top:var(--rc-3)">
                <div><b>${U.esc(s.name || ('Спікер ' + id))}</b>${U.esc(roleLabel)}</div>
                <ul style="margin:.35em 0 0 1.2em;padding:0">${list}</ul>
            </div>`;
        }).join('');
        return `<p>Перевірте, кого саме обʼєднуєте — різні люди можуть мати схожі голоси діаризації.</p>${blocks}`;
    }

    async function doMerge(ctx) {
        const keepId = parseInt(ctx.mount.querySelector('#rcMergeKeep').value, 10);
        const mergeIds = [...state.selected].filter(id => id !== keepId);
        if (!mergeIds.length) return;
        const keep = (state.speakers || []).find(s => s.id === keepId);
        const goBtn = ctx.mount.querySelector('#rcMergeGo');
        if (goBtn) { goBtn.disabled = true; goBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Завантажую приклади…'; }

        const allIds = [keepId, ...mergeIds];
        const results = await Promise.all(allIds.map(fetchSamples));
        const samplesById = {};
        allIds.forEach((id, i) => { samplesById[id] = results[i]; });

        if (goBtn) { goBtn.disabled = false; goBtn.innerHTML = '<i class="fa-solid fa-code-merge"></i> Обʼєднати'; }

        const ok = await UI.modal({
            title: 'Обʼєднати спікерів',
            icon: 'fa-code-merge',
            bodyHTML: mergePreviewHTML(state.speakers, keepId, mergeIds, samplesById),
            actions: [
                { label: 'Скасувати', value: false },
                { label: `Обʼєднати ${mergeIds.length} у «${keep.name}»`, value: true, primary: true, danger: true },
            ],
        });
        if (!ok) return;
        try {
            const res = await R.api.speakersMerge({ keep_id: keepId, merge_ids: mergeIds });
            UI.toast(`Обʼєднано у «${keep.name}»`, 'success');
            state.selected.clear();
            load(ctx);
            const mergeId = res && res.merge_id;
            if (mergeId) {
                UI.actionToast('Обʼєднано. Скасувати?', 'Скасувати', async () => {
                    try {
                        await R.api.post(`/api/speakers/unmerge/${mergeId}`);
                        UI.toast('Обʼєднання скасовано', 'success');
                        load(ctx);
                    } catch (e) {
                        if (e && e.status === 409) UI.toast('Це обʼєднання вже було скасовано раніше', 'info');
                        else if (e && e.status === 404) UI.toast('Не вдалося скасувати — запис обʼєднання не знайдено', 'error');
                        else UI.toast((e && e.message) || 'Не вдалося скасувати обʼєднання', 'error');
                    }
                });
            }
        } catch (e) { UI.toast(e.message || 'Не вдалося обʼєднати', 'error'); }
    }

    R.views.speakers = { render, destroy() { state = null; } };
})();
