/* Recall — Tasks (action items) dashboard. Wraps /api/memory/action-items.

   Це НЕ список справ, а звід зобовʼязань із семимісячним хвостом: відкритих
   задач ~1.5k, дедлайн має лише кожна пʼята, протермінованих сотні. Тому екран
   побудований навколо ЧАСУ ДО ДІЇ, а не навколо порядку інджесту:

   · відра «Протерміновано / Найближчі 7 днів / Пізніше / Без дати» —
     взаємно виключні, лічильники приходять з бекенда (`windows`) і тому
     правдиві навіть коли сам список обрізаний лімітом;
   · фасет власника — 800+ задач привʼязані до графа, і «чиє це» звужує
     сильніше за будь-який інший фільтр;
   · статус-пілюля лише на ВИНЯТКАХ (протерміновано / застаріло / дубль):
     «open» на кожному з 1480 рядків — це шум, а не інформація.
*/
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    const STATUSES = [
        { k: 'open', label: 'Відкриті' },
        { k: 'done', label: 'Виконані' },
        // Трек 1: sweep_stale переводить давно протерміноване у 'stale'. Без
        // цього чипа тисячі таких задач недосяжні з UI взагалі.
        { k: 'stale', label: 'Застарілі' },
        { k: 'cancelled', label: 'Скасовані' },
        { k: 'all', label: 'Усі' },
    ];
    // Період — за датою запису-джерела. «all» = без ліміту по часу.
    const PERIODS = [
        { k: '24h', label: '24 години' },
        { k: '7d', label: '7 днів' },
        { k: 'month', label: 'Місяць' },
        { k: 'year', label: 'Рік' },
        { k: 'all', label: 'Усі' },
    ];
    // Порядок = порядок терміновості. `lead` — скільки рядків показуємо одразу;
    // «Без дати» згорнуте, бо це археологія, а не план на сьогодні.
    const BUCKETS = [
        { k: 'overdue', label: 'Протерміновано', tone: 'err', lead: 25 },
        { k: 'soon', label: 'Найближчі 7 днів', tone: 'accent', lead: 50 },
        { k: 'later', label: 'Пізніше', tone: 'plain', lead: 25 },
        { k: 'no_date', label: 'Без дати', tone: 'muted', lead: 0 },
    ];
    const PAGE = 50;    // скільки дотягуємо за один «показати ще»

    let listEl = null, cats = null, state = null;
    // Скільки рядків уже завантажено у кожному відрі + чи воно розгорнуте.
    let shown = {}, opened = {}, windows = {}, owners = [];

    async function render(ctx) {
        cats = await UI.loadCategories();
        state = {
            status: ctx.query.status || 'open',
            category_id: ctx.query.category_id || '',
            period: ctx.query.period || 'all',
            owner: ctx.query.owner || '',
        };
        if (!PERIODS.some(p => p.k === state.period)) state.period = 'all';
        if (!STATUSES.some(s => s.k === state.status)) state.status = 'open';
        shown = {}; opened = {}; windows = {}; owners = [];

        const catOpts = UI.catOptions(state.category_id, { first: 'all' });

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow">Дашборд</div>
                <h1 class="rc-pagehead__title">Задачі</h1>
                <p class="rc-pagehead__lede">Домовленості, що прозвучали у дзвінках. Згруповані за часом до дії — спершу протерміноване, далі найближчий тиждень.</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-filterbar rc-toolbar__grow" id="rcTaskFilters"></div>
                <select class="rc-select rc-catsel" id="rcTaskCat" data-cat-first="all">${catOpts}</select>
            </div>
            <div class="rc-periodrow">
                <span class="rc-periodrow__lbl rc-mono"><i class="fa-regular fa-clock"></i> Період</span>
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
        if (state.period && state.period !== 'all') p.set('period', state.period);
        if (state.owner) p.set('owner', state.owner);
        const qs = p.toString();
        R.router.replace('/tasks' + (qs ? '?' + qs : ''));
    }

    function params(extra) {
        return Object.assign({
            status: state.status,
            category_id: state.category_id || undefined,
            period: state.period && state.period !== 'all' ? state.period : undefined,
            owner: state.owner || undefined,
        }, extra);
    }

    // Одне відро = один запит. Так «Без дати» (тисяча з гаком рядків) не з'їдає
    // ліміт у протермінованих, і кожне відро гортається незалежно.
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
                    ? Promise.resolve({ action_items: [] })     // згорнуте не тягнемо
                    : R.api.actionItems(params({ window: b.k, limit: bucketLimit(b.k) }))
                        .catch(() => ({ action_items: [] }))));
            if (!ctx.isCurrent()) return;

            const data = { overdue: first.action_items || [] };
            BUCKETS.slice(1).forEach((b, i) => { data[b.k] = rest[i].action_items || []; });

            renderOwners(ctx);
            listEl.innerHTML = BUCKETS.map(b => bucketHTML(b, data[b.k])).filter(Boolean).join('')
                || UI.empty('Нічого не знайдено', 'Спробуйте інший статус, напрямок або період.', 'fa-clipboard-check');
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
        // Показуємо топ-8 — далі хвіст із одиничними задачами, який лише шумить.
        const top = owners.slice(0, 8);
        const active = state.owner && !top.some(o => o.owner === state.owner)
            ? [owners.find(o => o.owner === state.owner) || { owner: state.owner, n: 0 }] : [];
        box.innerHTML = `<span class="rc-ownerrow__lbl rc-mono"><i class="fa-regular fa-user"></i> Власник</span>`
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
            ? `<div class="rc-bucket__more"><button class="rc-btn rc-btn--sm" data-more="${b.k}">Показати ще ${Math.min(PAGE, n - rows.length)} з ${n - rows.length}</button></div>`
            : '';
        return `<section class="rc-bucket">${head}
            <div class="rc-list rc-bucket__list">${rows.map(a => taskHTML(a, b)).join('')}</div>${more}</section>`;
    }

    function taskHTML(a, bucket) {
        const owner = a.owner_canonical || a.owner_name;
        // Підпис джерела. Для дзвінка це назва запису, для переписки —
        // ЧАТ і автор репліки: source_name у TG склеєний з тексту самого
        // повідомлення, тож повторював би текст задачі замість того, щоб
        // сказати, куди написати.
        const label = (a.source_type === 'telegram' && a.tg_chat_title)
            ? a.tg_chat_title + (a.tg_sender ? ' · ' + a.tg_sender : '')
            : (a.source_name || ('#' + a.transcription_id));
        const src = a.transcription_id
            ? `<a class="rc-task__src" href="/transcript/${U.slug(a.transcription_id, a.source_name)}">${U.esc(label)}</a>`
            : '';
        // Дата: чим ближче/глибше прострочено, тим гучніше. Точність із Треку 1
        // показуємо лише коли дата НЕ денна — інакше «до 27.07» брехало б про
        // точність там, де в оригіналі було «десь у серпні».
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
            // Сира фраза без розпізнаної дати — краще показати як є, ніж мовчати.
            due = `<span class="rc-task__due" data-tone="muted"><span class="rc-task__due-rel">${U.esc(a.due)}</span></span>`;
        }
        // Пілюля лише на винятках. Дефолтний 'open' не показуємо взагалі.
        const flags = [
            a.status === 'stale' ? '<span class="rc-task__flag" data-s="stale">застаріло</span>' : '',
            a.status === 'done' ? '<span class="rc-task__flag" data-s="done">виконано</span>' : '',
            a.status === 'cancelled' ? '<span class="rc-task__flag" data-s="cancelled">скасовано</span>' : '',
            a.dup_of ? '<span class="rc-task__flag" data-s="dup">дубль</span>' : '',
        ].filter(Boolean).join('');

        // Дату зустрічі підписуємо явно: без підпису вона у тому ж форматі, що
        // й дедлайн у рейці зліва, і рядок читався як «дві дати, обидві дедлайн».
        const metaBits = [
            owner ? `<span class="rc-task__owner">${U.esc(owner)}</span>` : '',
            src,
            a.meeting_date ? `<span class="rc-mono">з ${U.esc(U.fmtDueDate(a.meeting_date))}</span>` : '',
        ].filter(Boolean).join('<span class="sep">·</span>');

        return `<div class="rc-task" data-id="${a.id}">
            ${due || '<span class="rc-task__due"></span>'}
            <div class="rc-task__body">
                <div class="rc-task__text">${U.esc(a.task)}${flags}</div>
                <div class="rc-rec__meta">${metaBits}</div>
            </div>
            <div class="rc-task__acts">
                ${a.status !== 'done' ? `<button class="rc-iconbtn" data-act="done" title="Виконано" aria-label="Позначити виконаним"><i class="fa-solid fa-check"></i></button>` : ''}
                ${a.status !== 'open' ? `<button class="rc-iconbtn" data-act="open" title="Повернути у роботу" aria-label="Повернути у роботу"><i class="fa-solid fa-rotate-left"></i></button>` : ''}
                ${a.status !== 'cancelled' ? `<button class="rc-iconbtn" data-act="cancelled" title="Знято з порядку денного" aria-label="Зняти задачу"><i class="fa-solid fa-xmark"></i></button>` : ''}
            </div>
        </div>`;
    }

    function wire(ctx) {
        listEl.querySelectorAll('.rc-bucket__head').forEach(h => {
            h.addEventListener('click', () => {
                const k = h.dataset.bucket;
                const b = BUCKETS.find(x => x.k === k);
                // Розгортання відра з lead>0 не має сенсу — воно вже відкрите.
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
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Завантаження…';
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
                        UI.toast('Статус оновлено', 'success');
                        load(ctx);
                        R.shell.loadCounts();
                    } catch (err) { UI.toast(err.message, 'error'); }
                });
            });
        });
    }

    // T5.5: три різні порожні стани замість одного повідомлення, яке маскує
    // «архів порожній» під «усе зроблено».
    //   1. звужений фільтр/статус → «розширте фільтр»
    //   2. дефолт + архів без записів → CTA додати перший запис
    //   3. дефолт + записи є → справді «все під контролем»
    async function renderEmpty(ctx) {
        const narrowed = !!(state.category_id || state.owner || (state.period && state.period !== 'all'));
        const nonDefaultStatus = state.status !== 'open';
        if (narrowed || nonDefaultStatus) {
            listEl.innerHTML = UI.empty('Задач немає',
                'Немає задач за цим фільтром — спробуйте розширити напрямок/період/власника або змінити статус.', 'fa-clipboard-check');
            return;
        }
        let hasRecords = true;
        try {
            const s = R.state.stats || await R.api.stats();
            if (ctx.isCurrent()) R.state.stats = s;
            hasRecords = !!(s && s.transcriptions && s.transcriptions.total > 0);
        } catch (_) { /* stats недоступні — не блокуємо, показуємо нейтральний стан "усе під контролем" */ }
        if (!ctx.isCurrent()) return;
        if (!hasRecords) {
            const actions = [
                { label: 'Завантажити аудіо', icon: 'fa-upload', href: '/upload', primary: true },
                { label: 'Записати дзвінок', icon: 'fa-microphone', href: '/record' },
            ];
            listEl.innerHTML = UI.empty('Архів ще порожній',
                'Задачі зʼявляться тут автоматично, коли Claude обробить перші записи.', 'fa-box-open', actions);
        } else {
            listEl.innerHTML = UI.empty('Усе під контролем',
                'Відкритих задач немає — усі виконані, зняті або застаріли.', 'fa-clipboard-check');
        }
    }

    R.views.tasks = {
        render,
        destroy() { listEl = null; cats = null; state = null; shown = {}; opened = {}; windows = {}; owners = []; },
    };
})();
