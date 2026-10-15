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
                <div class="rc-eyebrow"><i class="fa-solid fa-users"></i> ${R.t('settings.sv.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${R.t('settings.sv.title')}</h1>
                <p class="rc-pagehead__lede">${R.t('settings.sv.lede')}</p>
            </div>
            <div class="rc-toolbar">
                <button class="rc-btn rc-btn--sm" id="rcSpkAdd"><i class="fa-solid fa-plus"></i> ${R.t('settings.sv.add')}</button>
                <button class="rc-btn rc-btn--sm" id="rcSpkMerge"><i class="fa-solid fa-code-merge"></i> ${R.t('settings.sv.merge')}</button>
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
            box.innerHTML = UI.empty(R.t('settings.sv.empty'), R.t('settings.sv.emptyHint'), 'fa-users');
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
            ? `<input type="checkbox" class="rc-spk__chk" data-chk="${s.id}"${s.is_self ? ` disabled title="${R.t('settings.sv.selfNoMerge')}"` : ''}${state.selected.has(s.id) ? ' checked' : ''}>`
            : '';
        const stats = [
            R.t('settings.sv.recs', { n: s.transcripts_count || 0 }),
            s.total_seconds ? U.fmtDuration(s.total_seconds) + ' ' + R.t('settings.sv.ofTalk') : null,
            s.words_count ? R.t('settings.sv.words', { n: s.words_count }) : null,
        ].filter(Boolean).join(' · ');
        return `<div class="rc-spk${s.is_self ? ' is-self' : ''}" data-id="${s.id}">
            <div class="rc-spk__main">
                ${checkbox}
                <span class="rc-spk__dot" style="background:${U.esc(color)}"></span>
                <div class="rc-spk__body">
                    <div class="rc-spk__name">${U.esc(s.name)}${s.is_self ? ` <span class="rc-tgrow__badge">${R.t('settings.sv.you')}</span>` : ''}</div>
                    <div class="rc-spk__stats rc-mono">${stats || R.t('settings.sv.noRecs')}</div>
                </div>
                <div class="rc-spk__actions">
                    <button class="rc-iconbtn" data-act="timeline" title="${R.t('settings.sv.activity')}"><i class="fa-solid fa-chart-simple"></i></button>
                    <button class="rc-iconbtn" data-act="rename" title="${R.t('settings.sv.rename')}"><i class="fa-solid fa-pen"></i></button>
                    <button class="rc-iconbtn" data-act="delete" title="${s.is_self ? R.t('settings.sv.selfNoDelete') : R.t('settings.sv.delete')}"${s.is_self ? ' disabled' : ''}><i class="fa-solid fa-trash"></i></button>
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
                title: R.t('settings.sv.renameTitle'),
                label: R.t('settings.sv.newName'),
                defaultValue: cur,
                confirmLabel: R.t('settings.sv.save'),
                required: true,
            });
            if (!next || next === cur) return;
            try { await R.api.speakerRename(s.id, next); UI.toast(R.t('settings.sv.renamed'), 'success'); load(ctx); }
            catch (e) { UI.toast(e.message, 'error'); }
        });
        const del = row.querySelector('[data-act="delete"]');
        if (del && !s.is_self) del.addEventListener('click', async () => {
            const ok = await UI.confirmModal({
                title: R.t('settings.sv.delQ'),
                message: R.t('settings.sv.delMsg', { name: s.name }),
                confirmLabel: R.t('settings.sv.delete'),
            });
            if (!ok) return;
            try { await R.api.speakerDelete(s.id); UI.toast(R.t('settings.sv.deleted'), 'success'); state.selected.delete(s.id); load(ctx); }
            catch (e) { UI.toast(e.message, 'error'); }
        });
    }

    // ---- timeline (activity by day) ----
    async function toggleTimeline(ctx, s, row) {
        const tl = row.querySelector('.rc-spk__timeline');
        if (!tl.hidden) { tl.hidden = true; tl.innerHTML = ''; return; }
        tl.hidden = false;
        tl.innerHTML = `<div class="rc-mono" style="color:var(--rc-ink-3)"><i class="fa-solid fa-spinner fa-spin"></i> ${R.t('settings.sv.loadingTl')}</div>`;
        try {
            const d = await R.api.speakerTimeline(s.id, 30);
            if (!ctx.isCurrent()) return;
            tl.innerHTML = timelineHTML(d);
        } catch (e) { tl.innerHTML = UI.error(e.message); }
    }

    function timelineHTML(d) {
        const tl = d.timeline || [], sum = d.summary || {};
        if (!tl.length) return `<div class="rc-mono" style="color:var(--rc-ink-3)">${R.t('settings.sv.tlNone', { days: d.days })}</div>`;
        const max = Math.max.apply(null, tl.map(x => x.total_seconds || 0).concat([1]));
        const bars = tl.map(x => {
            const h = Math.max(4, Math.round((x.total_seconds || 0) / max * 100));
            const tip = `${x.date} · ${U.fmtDuration(x.total_seconds)} · ${R.t('settings.sv.words', { n: x.words_count })} · ${R.t('settings.sv.recs', { n: x.transcripts_count })}`;
            return `<div class="rc-tl__bar" style="height:${h}%" title="${U.esc(tip)}"></div>`;
        }).join('');
        return `<div class="rc-spk__tlsum rc-mono">${R.t('settings.sv.tlSum', { days: d.days, dur: '<b>' + U.fmtDuration(sum.total_seconds || 0) + '</b>', words: sum.total_words || 0, recs: sum.total_transcripts || 0, active: sum.active_days || 0 })}</div>
            <div class="rc-tl">${bars}</div>
            <div class="rc-tl__axis rc-mono"><span>${U.esc(tl[0].date)}</span><span>${U.esc(tl[tl.length - 1].date)}</span></div>`;
    }

    // ---- add speaker ----
    function toggleAdd(ctx) {
        const bar = ctx.mount.querySelector('#rcSpkAddBar');
        if (bar.innerHTML) { bar.innerHTML = ''; return; }
        bar.innerHTML = `<div class="rc-newcat">
            <input class="rc-input" id="rcSpkNew" placeholder="${R.t('settings.sv.namePh')}" autocomplete="off">
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcSpkNewSave">${R.t('settings.sv.addGo')}</button>
            <button class="rc-btn rc-btn--sm" id="rcSpkNewCancel">${R.t('settings.sv.cancel')}</button>
        </div>`;
        const input = bar.querySelector('#rcSpkNew');
        input.focus();
        const close = () => { bar.innerHTML = ''; };
        const save = async () => {
            const name = input.value.trim();
            if (!name) return;
            try { await R.api.speakerCreate(name); close(); load(ctx); UI.toast(R.t('settings.sv.added', { name }), 'success'); }
            catch (e) { UI.toast(e.message || R.t('settings.sv.addFail'), 'error'); }
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
            bar.innerHTML = `<div class="rc-mergebar"><i class="fa-solid fa-circle-info"></i> ${R.t('settings.sv.mergeHint')}</div>`;
            return;
        }
        const keep = sel.slice().sort((a, b) => (b.usage_count || 0) - (a.usage_count || 0))[0];
        bar.innerHTML = `<div class="rc-mergebar">
            <span>${R.t('settings.sv.mergeKeep', { n: '<b>' + sel.length + '</b>' })}</span>
            <select class="rc-select rc-select--sm" id="rcMergeKeep">${sel.map(s => `<option value="${s.id}"${s.id === keep.id ? ' selected' : ''}>${U.esc(s.name)}</option>`).join('')}</select>
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcMergeGo"><i class="fa-solid fa-code-merge"></i> ${R.t('settings.sv.merge')}</button>
            <button class="rc-btn rc-btn--sm" id="rcMergeCancel">${R.t('settings.sv.clear')}</button>
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
            const roleLabel = id === keepId ? R.t('settings.sv.roleKeep') : R.t('settings.sv.roleMerged');
            const list = items.length
                ? items.map(x => {
                    const text = (x.text || '').trim();
                    const short = text.length > 160 ? text.slice(0, 160) + '…' : text;
                    return `<li>${U.esc(short)}</li>`;
                }).join('')
                : '<li><i>' + R.t('settings.sv.noSamples') + '</i></li>';
            return `<div style="margin-top:var(--rc-3)">
                <div><b>${U.esc(s.name || R.t('settings.sv.fallbackName', { id }))}</b>${U.esc(roleLabel)}</div>
                <ul style="margin:.35em 0 0 1.2em;padding:0">${list}</ul>
            </div>`;
        }).join('');
        return `<p>${R.t('settings.sv.previewNote')}</p>${blocks}`;
    }

    async function doMerge(ctx) {
        const keepId = parseInt(ctx.mount.querySelector('#rcMergeKeep').value, 10);
        const mergeIds = [...state.selected].filter(id => id !== keepId);
        if (!mergeIds.length) return;
        const keep = (state.speakers || []).find(s => s.id === keepId);
        const goBtn = ctx.mount.querySelector('#rcMergeGo');
        if (goBtn) { goBtn.disabled = true; goBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + R.t('settings.sv.loadingSamples'); }

        const allIds = [keepId, ...mergeIds];
        const results = await Promise.all(allIds.map(fetchSamples));
        const samplesById = {};
        allIds.forEach((id, i) => { samplesById[id] = results[i]; });

        if (goBtn) { goBtn.disabled = false; goBtn.innerHTML = '<i class="fa-solid fa-code-merge"></i> ' + R.t('settings.sv.merge'); }

        const ok = await UI.modal({
            title: R.t('settings.sv.mergeTitle'),
            icon: 'fa-code-merge',
            bodyHTML: mergePreviewHTML(state.speakers, keepId, mergeIds, samplesById),
            actions: [
                { label: R.t('settings.sv.cancel'), value: false },
                { label: R.t('settings.sv.mergeInto', { n: mergeIds.length, name: keep.name }), value: true, primary: true, danger: true },
            ],
        });
        if (!ok) return;
        try {
            const res = await R.api.speakersMerge({ keep_id: keepId, merge_ids: mergeIds });
            UI.toast(R.t('settings.sv.mergedInto', { name: keep.name }), 'success');
            state.selected.clear();
            load(ctx);
            const mergeId = res && res.merge_id;
            if (mergeId) {
                UI.actionToast(R.t('settings.sv.mergedUndo'), R.t('settings.sv.cancel'), async () => {
                    try {
                        await R.api.post(`/api/speakers/unmerge/${mergeId}`);
                        UI.toast(R.t('settings.sv.unmerged'), 'success');
                        load(ctx);
                    } catch (e) {
                        if (e && e.status === 409) UI.toast(R.t('settings.sv.alreadyUnmerged'), 'info');
                        else if (e && e.status === 404) UI.toast(R.t('settings.sv.unmergeNotFound'), 'error');
                        else UI.toast((e && e.message) || R.t('settings.sv.unmergeFail'), 'error');
                    }
                });
            }
        } catch (e) { UI.toast(e.message || R.t('settings.sv.mergeFail'), 'error'); }
    }

    R.views.speakers = { render, destroy() { state = null; } };
})();
