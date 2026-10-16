/* Recall — Tasks (action items) dashboard. Wraps /api/memory/action-items.

   This is NOT a to-do list but a digest of commitments with a seven-month tail: open
   tasks ~1.5k, only every fifth has a deadline, overdue ones number in the hundreds. So the screen
   is built around TIME TO ACTION, not around ingest order:

   · buckets «Протерміновано / Найближчі 7 днів / Пізніше / Без дати» —
  mutually exclusive, the counters come from the backend (`windows`) and are therefore
  truthful even when the list itself is cut by the limit;
   · the owner facet — 800+ tasks are linked to the graph, and "whose is this" narrows
  more strongly than any other filter;
   · the status pill only on EXCEPTIONS (overdue / stale / duplicate):
  "open" on each of 1480 rows is noise, not information.
*/
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const T = (k, v) => R.t(k, v);

    const STATUSES = [
        { k: 'open', label: T('knowledge.tasks.status.open') },
        { k: 'done', label: T('knowledge.tasks.status.done') },
        // Track 1: sweep_stale moves long-overdue items to 'stale'. Without
        // this chip thousands of such tasks are unreachable from the UI altogether.
        { k: 'stale', label: T('knowledge.tasks.status.stale') },
        { k: 'cancelled', label: T('knowledge.tasks.status.cancelled') },
        { k: 'all', label: T('knowledge.tasks.status.all') },
    ];
    // Period — by the date of the source record. "all" = no time limit.
    const PERIODS = [
        { k: '24h', label: T('knowledge.tasks.period.24h') },
        { k: '7d', label: T('knowledge.tasks.period.7d') },
        { k: 'month', label: T('knowledge.tasks.period.month') },
        { k: 'year', label: T('knowledge.tasks.period.year') },
        { k: 'all', label: T('knowledge.tasks.period.all') },
    ];
    // Order = order of urgency. `lead` — how many rows we show right away;
    // «Без дати» is collapsed because it is archaeology, not a plan for today.
    const BUCKETS = [
        { k: 'overdue', label: T('knowledge.tasks.bucket.overdue'), tone: 'err', lead: 25 },
        { k: 'soon', label: T('knowledge.tasks.bucket.soon'), tone: 'accent', lead: 50 },
        { k: 'later', label: T('knowledge.tasks.bucket.later'), tone: 'plain', lead: 25 },
        { k: 'no_date', label: T('knowledge.tasks.bucket.noDate'), tone: 'muted', lead: 0 },
    ];
    const PAGE = 50;    // how many we fetch per one "show more"

    let listEl = null, cats = null, state = null;
    // How many rows are already loaded in each bucket + whether it is expanded.
    let shown = {}, opened = {}, windows = {}, owners = [];

    // C9: the same filter bar as on Library/Media library/Graph, duplicated
    // on purpose (09 — only shared helpers, not a shared filter-bar component).
    const FILTER_HINT = T('knowledge.tasks.filterHint');

    async function render(ctx) {
        cats = await UI.loadCategories();
        if (UI.loadTags) await UI.loadTags();
        state = {
            status: ctx.query.status || 'open',
            category_id: ctx.query.category_id || '',
            tags: (ctx.query.tags || '').split(',').map(s => s.trim()).filter(Boolean),
            since: ctx.query.since || '',
            until: ctx.query.until || '',
            period: ctx.query.period || 'all',
            owner: ctx.query.owner || '',
        };
        if (!PERIODS.some(p => p.k === state.period)) state.period = 'all';
        if (!STATUSES.some(s => s.k === state.status)) state.status = 'open';
        shown = {}; opened = {}; windows = {}; owners = [];

        const catOpts = UI.catOptions(state.category_id, { first: 'all' });
        const tagSel = UI.tagOptions ? UI.tagOptions(state.tags) : '<select multiple class="rc-select rc-select--sm" disabled></select>';

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">${T('knowledge.tasks.eyebrow')}</div>
                <h1 class="rc-pagehead__title">${T('knowledge.tasks.title')}</h1>
                <p class="rc-pagehead__lede">${T('knowledge.tasks.lede')}</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-filterbar rc-toolbar__grow" id="rcTaskFilters"></div>
                <select class="rc-select rc-catsel" id="rcTaskCat" data-cat-first="all">${catOpts}</select>
            </div>
            <div class="rc-toolbar" id="rcTaskTagPeriod">
                <div id="rcTaskTagsWrap">${tagSel}</div>
                <input type="date" class="rc-input rc-select--sm" id="rcTaskSince" value="${U.esc(state.since)}" title="${T('knowledge.filter.sinceTitle')}">
                <input type="date" class="rc-input rc-select--sm" id="rcTaskUntil" value="${U.esc(state.until)}" title="${T('knowledge.filter.untilTitle')}">
            </div>
            <div id="rcTaskHint"></div>
            <div class="rc-periodrow">
                <span class="rc-periodrow__lbl rc-mono"><i class="fa-regular fa-clock"></i> ${T('knowledge.tasks.period')}</span>
                <div class="rc-period" id="rcTaskPeriod"></div>
            </div>
            <div class="rc-ownerrow" id="rcTaskOwners"></div>
            <div id="rcTaskList"></div>`;
        listEl = ctx.mount.querySelector('#rcTaskList');

        const fb = ctx.mount.querySelector('#rcTaskFilters');
        fb.innerHTML = STATUSES.map(s => `<button class="rc-chip${s.k === state.status ? ' is-active' : ''}" data-s="${s.k}">${s.label}</button>`).join('');
        fb.addEventListener('click', (e) => {
            const b = e.target.closest('.rc-chip'); if (!b) return;
            state.status = b.dataset.s;
            fb.querySelectorAll('.rc-chip').forEach(c => c.classList.toggle('is-active', c === b));
            reset(); sync(); load(ctx);
        });

        const pb = ctx.mount.querySelector('#rcTaskPeriod');
        pb.innerHTML = PERIODS.map(p => `<button class="rc-period__btn${p.k === state.period ? ' is-active' : ''}" data-p="${p.k}">${p.label}</button>`).join('');
        pb.addEventListener('click', (e) => {
            const b = e.target.closest('.rc-period__btn'); if (!b) return;
            state.period = b.dataset.p;
            pb.querySelectorAll('.rc-period__btn').forEach(c => c.classList.toggle('is-active', c === b));
            reset(); sync(); load(ctx);
        });

        const catSel = ctx.mount.querySelector('#rcTaskCat');
        catSel.value = state.category_id;
        catSel.addEventListener('change', () => { state.category_id = catSel.value; reset(); sync(); load(ctx); });

        const tagSelEl = ctx.mount.querySelector('#rcTaskTagsWrap select');
        if (tagSelEl) { tagSelEl.setAttribute('aria-label', T('core.ui.tags.filterAria')); tagSelEl.title = T('knowledge.filter.tagsTitle'); }
        if (tagSelEl) tagSelEl.addEventListener('change', () => {
            state.tags = Array.from(tagSelEl.selectedOptions).map(o => o.value).filter(Boolean);
            reset(); sync(); load(ctx);
        });
        const sinceEl = ctx.mount.querySelector('#rcTaskSince');
        const untilEl = ctx.mount.querySelector('#rcTaskUntil');
        sinceEl.addEventListener('change', () => { state.since = sinceEl.value; reset(); sync(); load(ctx); });
        untilEl.addEventListener('change', () => { state.until = untilEl.value; reset(); sync(); load(ctx); });

        const hintBox = ctx.mount.querySelector('#rcTaskHint');
        if (hintBox && UI.hint) hintBox.appendChild(UI.hint(FILTER_HINT));

        ctx.mount.querySelector('#rcTaskOwners').addEventListener('click', (e) => {
            const b = e.target.closest('[data-owner]'); if (!b) return;
            const v = b.dataset.owner;
            state.owner = state.owner === v ? '' : v;
            reset(); sync(); load(ctx);
        });

        load(ctx);
    }

    function reset() { shown = {}; opened = {}; }

    function sync() {
        const p = new URLSearchParams();
        if (state.status !== 'open') p.set('status', state.status);
        if (state.category_id) p.set('category_id', state.category_id);
        if (state.tags.length) p.set('tags', state.tags.join(','));
        if (state.since) p.set('since', state.since);
        if (state.until) p.set('until', state.until);
        if (state.period && state.period !== 'all') p.set('period', state.period);
        if (state.owner) p.set('owner', state.owner);
        const qs = p.toString();
        R.router.replace('/tasks' + (qs ? '?' + qs : ''));
    }

    function params(extra) {
        return Object.assign({
            status: state.status,
            category_id: state.category_id || undefined,
            tags: state.tags.length ? state.tags.join(',') : undefined,
            since: state.since || undefined,
            until: state.until || undefined,
            period: state.period && state.period !== 'all' ? state.period : undefined,
            owner: state.owner || undefined,
        }, extra);
    }

    // One bucket = one request. This way «Без дати» (a thousand-plus rows) does not eat
    // the limit of the overdue ones, and every bucket scrolls independently.
    async function load(ctx) {
        listEl.innerHTML = UI.skeletonList(6);
        try {
            const first = await R.api.actionItems(params({ window: 'overdue', limit: bucketLimit('overdue') }));
            if (!ctx.isCurrent()) return;
            windows = first.windows || {};
            owners = first.owners || [];
            const total = windows.all || 0;
            if (!total) { renderOwners(ctx); await renderEmpty(ctx); return; }

            const rest = await Promise.all(BUCKETS.slice(1).map(b =>
                (b.lead === 0 && !opened[b.k])
                    ? Promise.resolve({ action_items: [] })     // a collapsed one is not fetched
                    : R.api.actionItems(params({ window: b.k, limit: bucketLimit(b.k) }))
                        .catch(() => ({ action_items: [] }))));
            if (!ctx.isCurrent()) return;

            const data = { overdue: first.action_items || [] };
            BUCKETS.slice(1).forEach((b, i) => { data[b.k] = rest[i].action_items || []; });

            renderOwners(ctx);
            listEl.innerHTML = BUCKETS.map(b => bucketHTML(b, data[b.k])).filter(Boolean).join('')
                || UI.empty(T('knowledge.tasks.nothing'), T('knowledge.tasks.nothingHint'), 'fa-clipboard-check');
            wire(ctx);
        } catch (err) {
            if (!ctx.isCurrent()) return;
            listEl.innerHTML = UI.error(err.message, { retry: true });
            UI.bindErrorRetry(listEl, () => load(ctx));
        }
    }

    function bucketLimit(k) {
        const b = BUCKETS.find(x => x.k === k);
        return shown[k] || Math.max(b ? b.lead : PAGE, 1);
    }

    function renderOwners(ctx) {
        const box = ctx.mount.querySelector('#rcTaskOwners');
        if (!box) return;
        if (!owners.length) { box.innerHTML = ''; return; }
        // We show the top 8 — beyond that is a tail of single tasks that only adds noise.
        const top = owners.slice(0, 8);
        const active = state.owner && !top.some(o => o.owner === state.owner)
            ? [owners.find(o => o.owner === state.owner) || { owner: state.owner, n: 0 }] : [];
        box.innerHTML = `<span class="rc-ownerrow__lbl rc-mono"><i class="fa-regular fa-user"></i> ${T('knowledge.tasks.owner')}</span>`
            + top.concat(active).map(o =>
                `<button class="rc-chip${state.owner === o.owner ? ' is-active' : ''}" data-owner="${U.esc(o.owner)}">${U.esc(o.owner)} <span class="rc-chip__n">${o.n}</span></button>`
            ).join('');
    }

    function bucketHTML(b, rows) {
        const n = windows[b.k] || 0;
        if (!n) return '';
        const isOpen = b.lead > 0 || opened[b.k];
        const head = `<div class="rc-bucket__head" data-bucket="${b.k}">
                <span class="rc-bucket__caret"><i class="fa-solid fa-chevron-${isOpen ? 'down' : 'right'}"></i></span>
                <span class="rc-bucket__name" data-tone="${b.tone}">${b.label}</span>
                <span class="rc-bucket__n rc-mono">${n}</span>
            </div>`;
        if (!isOpen) return `<section class="rc-bucket is-collapsed">${head}</section>`;
        const more = rows.length < n
            ? `<div class="rc-bucket__more"><button class="rc-btn rc-btn--sm" data-more="${b.k}">${T('knowledge.tasks.showMore', { count: Math.min(PAGE, n - rows.length), left: n - rows.length })}</button></div>`
            : '';
        return `<section class="rc-bucket">${head}
            <div class="rc-list rc-bucket__list">${rows.map(a => taskHTML(a, b)).join('')}</div>${more}</section>`;
    }

    function taskHTML(a, bucket) {
        const owner = a.owner_canonical || a.owner_name;
        // Source label. For a call it is the record title, for a conversation —
        // the CHAT and the author of the line: source_name in TG is glued from the text of the
        // message itself, so it would repeat the task text instead of
        // saying where to write.
        const label = (a.source_type === 'telegram' && a.tg_chat_title)
            ? a.tg_chat_title + (a.tg_sender ? ' · ' + a.tg_sender : '')
            : (a.source_name || ('#' + a.transcription_id));
        const src = a.transcription_id
            ? `<a class="rc-task__src" href="/transcript/${U.slug(a.transcription_id, a.source_name)}">${U.esc(label)}</a>`
            : '';
        // Date: the closer/deeper overdue, the louder. The precision from Track 1
        // is shown only when the date is NOT a day — otherwise "до 27.07" would lie about
        // precision where the original said "somewhere in August".
        const prec = U.DUE_PRECISION[a.due_precision];
        let due = '';
        if (a.due_date) {
            const rel = U.fmtDue(a.due_date);
            due = `<span class="rc-task__due" data-tone="${bucket.tone}" title="${U.esc(a.due || '')}">
                     <span class="rc-task__due-rel">${U.esc(rel)}</span>
                     <span class="rc-task__due-abs rc-mono">${U.esc(U.fmtDueDate(a.due_date))}</span>
                     ${prec ? `<span class="rc-task__due-prec">≈ ${U.esc(prec)}</span>` : ''}
                   </span>`;
        } else if (a.due) {
            // A raw phrase without a recognized date — better to show as is than stay silent.
            due = `<span class="rc-task__due" data-tone="muted"><span class="rc-task__due-rel">${U.esc(a.due)}</span></span>`;
        }
        // The pill only on exceptions. The default 'open' is not shown at all.
        const flags = [
            a.status === 'stale' ? '<span class="rc-task__flag" data-s="stale">' + T('knowledge.tasks.flag.stale') + '</span>' : '',
            a.status === 'done' ? '<span class="rc-task__flag" data-s="done">' + T('knowledge.tasks.flag.done') + '</span>' : '',
            a.status === 'cancelled' ? '<span class="rc-task__flag" data-s="cancelled">' + T('knowledge.tasks.flag.cancelled') + '</span>' : '',
            a.dup_of ? '<span class="rc-task__flag" data-s="dup">' + T('knowledge.tasks.flag.dup') + '</span>' : '',
        ].filter(Boolean).join('');

        // We label the meeting date explicitly: without a label it is in the same format
        // as the deadline in the rail on the left, and the row read as "two dates, both deadlines".
        const metaBits = [
            owner ? `<span class="rc-task__owner">${U.esc(owner)}</span>` : '',
            src,
            a.meeting_date ? `<span class="rc-mono">${T('knowledge.tasks.fromDate', { date: U.esc(U.fmtDueDate(a.meeting_date)) })}</span>` : '',
        ].filter(Boolean).join('<span class="sep">·</span>');

        return `<div class="rc-task" data-id="${a.id}">
            ${due || '<span class="rc-task__due"></span>'}
            <div class="rc-task__body">
                <div class="rc-task__text">${U.esc(a.task)}${flags}</div>
                <div class="rc-rec__meta">${metaBits}</div>
            </div>
            <div class="rc-task__acts">
                ${a.status !== 'done' ? `<button class="rc-iconbtn" data-act="done" title="${T('knowledge.tasks.act.done')}" aria-label="${T('knowledge.tasks.act.doneAria')}"><i class="fa-solid fa-check"></i></button>` : ''}
                ${a.status !== 'open' ? `<button class="rc-iconbtn" data-act="open" title="${T('knowledge.tasks.act.reopen')}" aria-label="${T('knowledge.tasks.act.reopen')}"><i class="fa-solid fa-rotate-left"></i></button>` : ''}
                ${a.status !== 'cancelled' ? `<button class="rc-iconbtn" data-act="cancelled" title="${T('knowledge.tasks.act.cancel')}" aria-label="${T('knowledge.tasks.act.cancelAria')}"><i class="fa-solid fa-xmark"></i></button>` : ''}
            </div>
        </div>`;
    }

    function wire(ctx) {
        listEl.querySelectorAll('.rc-bucket__head').forEach(h => {
            h.addEventListener('click', () => {
                const k = h.dataset.bucket;
                const b = BUCKETS.find(x => x.k === k);
                // Expanding a bucket with lead>0 makes no sense — it is already open.
                if (b && b.lead > 0) return;
                opened[k] = !opened[k];
                if (opened[k] && !shown[k]) shown[k] = PAGE;
                load(ctx);
            });
        });
        listEl.querySelectorAll('[data-more]').forEach(btn => {
            btn.addEventListener('click', () => {
                const k = btn.dataset.more;
                shown[k] = bucketLimit(k) + PAGE;
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + T('knowledge.tasks.loading');
                load(ctx);
            });
        });
        listEl.querySelectorAll('.rc-task[data-id]').forEach(row => {
            row.querySelectorAll('[data-act]').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const id = row.dataset.id, newStatus = btn.dataset.act;
                    try {
                        await R.api.setActionStatus(id, newStatus);
                        UI.toast(T('knowledge.tasks.statusUpdated'), 'success');
                        load(ctx);
                        R.shell.loadCounts();
                    } catch (err) { UI.toast(err.message, 'error'); }
                });
            });
        });
    }

    // T5.5: three different empty states instead of one message that masks
    // "the archive is empty" as "everything is done".
    //   1. narrowed filter/status → "widen the filter"
    //   2. default + an archive without records → CTA to add the first record
    //   3. default + records exist → truly "everything under control"
    function filterExplain() {
        const bits = [];
        if (state.category_id && UI.catPath) { const p = UI.catPath(Number(state.category_id)); if (p) bits.push(T('knowledge.filter.inCat', { path: p })); }
        if (state.tags.length) bits.push(T('knowledge.filter.withTags', { tags: state.tags.join(', ') }));
        if (state.since || state.until) bits.push(T('knowledge.filter.period', { from: state.since || '…', to: state.until || '…' }));
        if (!bits.length) return null;
        return T('knowledge.tasks.explain', { bits: bits.join(' ') });
    }

    async function renderEmpty(ctx) {
        const narrowed = !!(state.category_id || state.tags.length || state.since || state.until || state.owner || (state.period && state.period !== 'all'));
        const nonDefaultStatus = state.status !== 'open';
        if (narrowed || nonDefaultStatus) {
            listEl.innerHTML = UI.empty(T('knowledge.tasks.none'),
                filterExplain() || T('knowledge.tasks.noneFiltered'), 'fa-clipboard-check');
            return;
        }
        let hasRecords = true;
        try {
            const s = R.state.stats || await R.api.stats();
            if (ctx.isCurrent()) R.state.stats = s;
            hasRecords = !!(s && s.transcriptions && s.transcriptions.total > 0);
        } catch (_) { /* stats unavailable — do not block, show the neutral state "усе під контролем" */ }
        if (!ctx.isCurrent()) return;
        if (!hasRecords) {
            const actions = [
                { label: T('knowledge.tasks.uploadAudio'), icon: 'fa-upload', href: '/upload', primary: true },
                { label: T('knowledge.tasks.recordCall'), icon: 'fa-microphone', href: '/record' },
            ];
            listEl.innerHTML = UI.empty(T('knowledge.tasks.emptyArchive'),
                T('knowledge.tasks.emptyArchiveHint'), 'fa-box-open', actions);
        } else {
            listEl.innerHTML = UI.empty(T('knowledge.tasks.allClear'),
                T('knowledge.tasks.allClearHint'), 'fa-clipboard-check');
        }
    }

    R.views.tasks = {
        render,
        destroy() { listEl = null; cats = null; state = null; shown = {}; opened = {}; windows = {}; owners = []; },
    };
})();
