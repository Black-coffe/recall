/* Recall — Library / History list. A real, deep-linkable list: filters,
   search and pagination all live in the URL. Selection mode adds bulk напрямок
   assignment (Phase 15D — seeds the k-NN auto-suggest) and bulk delete. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    const PER_PAGE = 20;

    const SOURCES = [
        { k: '', label: 'Усі' },
        { k: 'youtube', label: 'YouTube' },
        { k: 'recording', label: 'Запис' },
        { k: 'document', label: 'Документи' },
        { k: 'file', label: 'Файли' },
        { k: 'telegram', label: 'Telegram' },
    ];

    // Контекстні фасет-поля — зʼявляються лише під обране джерело й фільтрують
    // у реальному часі (від 2 символів) по метаданих, яких немає в FTS-пошуку
    // по тексту (відправник, назва чату, автор каналу).
    const FACETS = {
        telegram: [
            { key: 'tg_sender', icon: 'fa-user', ph: 'Відправник…' },
            { key: 'tg_chat', icon: 'fa-comments', ph: 'Група / канал…' },
        ],
        youtube: [
            { key: 'yt_author', icon: 'fa-youtube fa-brands', ph: 'Автор / канал…' },
        ],
    };
    const FACET_KEYS = ['tg_sender', 'tg_chat', 'yt_author'];

    let listEl = null, cats = null, state = null;
    let selecting = false, selected = new Set(), curRows = [], lastData = null;
    // T5.5: cap for "select all N filtered" — beyond this a filter should be
    // narrowed first rather than firing dozens of paginated fetches.
    const SELECT_ALL_CAP = 3000;

    async function render(ctx) {
        cats = await UI.loadCategories();
        selecting = false; selected = new Set(); curRows = []; lastData = null;
        state = {
            search: ctx.query.search || '',
            source_type: ctx.query.source_type || '',
            category_id: ctx.query.category_id || '',
            page: parseInt(ctx.query.page, 10) || 1,
            tg_sender: ctx.query.tg_sender || '',
            tg_chat: ctx.query.tg_chat || '',
            yt_author: ctx.query.yt_author || '',
        };

        const catOpts = UI.catOptions(state.category_id, { first: 'all' });

        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-pagehead__row">
                    <div>
                        <div class="rc-eyebrow">Архів</div>
                        <h1 class="rc-pagehead__title">Бібліотека</h1>
                    </div>
                </div>
                <p class="rc-pagehead__lede">Усі записи архіву — дзвінки, відео, документи, Telegram. Натисніть запис, щоб відкрити повну сторінку з провенансом і звʼязками.</p>
            </div>
            <div class="rc-toolbar">
                <div class="rc-search rc-toolbar__grow">
                    <i class="rc-ico fa-solid fa-magnifying-glass"></i>
                    <input id="rcLibSearch" type="search" placeholder="Пошук по тексту записів…" value="${U.esc(state.search)}">
                </div>
                <select class="rc-select rc-catsel" id="rcLibCat" data-cat-first="all">${catOpts}</select>
                <button class="rc-btn" id="rcLibSaveSearch" title="Зберегти поточний фільтр як пошук"><i class="fa-regular fa-bookmark"></i></button>
                <button class="rc-btn" id="rcLibSelect"><i class="fa-solid fa-list-check"></i> Вибрати</button>
            </div>
            <div class="rc-filterbar" id="rcLibSources"></div>
            <div class="rc-facets" id="rcLibFacets" hidden></div>
            <div class="rc-savedrow" id="rcLibSaved"></div>
            <div class="rc-selectbar" id="rcLibSelectBar" hidden></div>
            <div class="rc-list" id="rcLibList"></div>
            <div id="rcLibPager"></div>
            <div class="rc-bulkbar" id="rcLibBulk" hidden></div>`;

        listEl = ctx.mount.querySelector('#rcLibList');

        const sb = ctx.mount.querySelector('#rcLibSources');
        sb.innerHTML = SOURCES.map(s =>
            `<button class="rc-chip${s.k === state.source_type ? ' is-active' : ''}" data-src="${s.k}">${s.label}</button>`
        ).join('');
        sb.addEventListener('click', (e) => {
            const b = e.target.closest('.rc-chip'); if (!b) return;
            state.source_type = b.dataset.src; state.page = 1;
            // Зміна джерела скидає контекстні фасети (вони specific до джерела).
            FACET_KEYS.forEach(k => { state[k] = ''; });
            selected.clear(); // T4.5: фільтр змінився — старий bulk-вибір міг стати невидимим
            sb.querySelectorAll('.rc-chip').forEach(c => c.classList.toggle('is-active', c === b));
            renderFacets(ctx);
            sync(); load(ctx);
        });

        renderFacets(ctx);

        const catSel = ctx.mount.querySelector('#rcLibCat');
        catSel.value = state.category_id;
        catSel.addEventListener('change', () => { state.category_id = catSel.value; state.page = 1; selected.clear(); sync(); load(ctx); });

        const search = ctx.mount.querySelector('#rcLibSearch');
        search.addEventListener('input', U.debounce(() => { state.search = search.value.trim(); state.page = 1; selected.clear(); sync(); load(ctx); }, 280));

        ctx.mount.querySelector('#rcLibSelect').addEventListener('click', () => toggleSelectMode(ctx));
        ctx.mount.querySelector('#rcLibSaveSearch').addEventListener('click', () => doSaveSearch(ctx));

        loadSaved(ctx);
        load(ctx);
    }

    // ---- contextual facets (per-source live filters) -----------------------
    function renderFacets(ctx) {
        const box = ctx.mount.querySelector('#rcLibFacets');
        if (!box) return;
        const defs = FACETS[state.source_type] || [];
        if (!defs.length) { box.hidden = true; box.innerHTML = ''; return; }
        box.hidden = false;
        box.innerHTML = defs.map(f => `
            <div class="rc-facet">
                <i class="rc-facet__ic fa-solid ${f.icon}"></i>
                <input type="search" class="rc-facet__in" data-facet="${f.key}"
                       placeholder="${U.esc(f.ph)}" value="${U.esc(state[f.key] || '')}"
                       autocomplete="off" spellcheck="false">
            </div>`).join('');
        box.querySelectorAll('.rc-facet__in').forEach(inp => {
            inp.addEventListener('input', U.debounce(() => {
                state[inp.dataset.facet] = inp.value.trim();
                state.page = 1; selected.clear(); sync(); load(ctx);
            }, 240));
        });
    }

    // ---- saved searches ----------------------------------------------------
    function curQuery() {
        const q = {};
        if (state.search) q.search = state.search;
        if (state.source_type) q.source_type = state.source_type;
        if (state.category_id) q.category_id = state.category_id;
        FACET_KEYS.forEach(k => { if (state[k]) q[k] = state[k]; });
        return q;
    }
    function queryDesc(q) {
        const parts = [];
        if (q.search) parts.push('«' + q.search + '»');
        if (q.source_type) { const s = SOURCES.find(x => x.k === q.source_type); parts.push(s ? s.label : q.source_type); }
        if (q.category_id) { const c = (cats.list || []).find(x => String(x.id) === String(q.category_id)); if (c) parts.push(c.name); }
        return parts.join(' · ');
    }
    async function loadSaved(ctx) {
        const box = ctx.mount.querySelector('#rcLibSaved');
        if (!box) return;
        let searches = [];
        try { const d = await R.api.savedSearches(); searches = d.searches || []; }
        catch (_) { box.innerHTML = ''; return; }
        if (!ctx.isCurrent()) return;
        if (!searches.length) { box.innerHTML = ''; return; }
        box.innerHTML = `<span class="rc-savedrow__lbl rc-mono">Збережені:</span>` + searches.map(s =>
            `<span class="rc-saved" data-id="${s.id}" title="${U.esc(queryDesc(s.query) || 'усі записи')}">
                <button class="rc-saved__go" data-go="${s.id}">${U.esc(s.name)}</button>
                <button class="rc-saved__del" data-del="${s.id}" title="Видалити">&times;</button>
            </span>`).join('');
        box._searches = searches;
        box.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => applySaved(ctx, searches.find(x => x.id === Number(b.dataset.go)))));
        box.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async (e) => {
            e.stopPropagation();
            try { await R.api.savedSearchDelete(Number(b.dataset.del)); loadSaved(ctx); }
            catch (err) { UI.toast(err && err.message, 'error'); }
        }));
    }
    function applySaved(ctx, s) {
        if (!s) return;
        const q = s.query || {};
        state.search = q.search || '';
        state.source_type = q.source_type || '';
        state.category_id = q.category_id ? String(q.category_id) : '';
        FACET_KEYS.forEach(k => { state[k] = q[k] || ''; });
        state.page = 1;
        selected.clear(); // T4.5: застосування збереженого пошуку теж міняє фільтр
        // reflect into controls
        const si = ctx.mount.querySelector('#rcLibSearch'); if (si) si.value = state.search;
        const cs = ctx.mount.querySelector('#rcLibCat'); if (cs) cs.value = state.category_id;
        ctx.mount.querySelectorAll('#rcLibSources .rc-chip').forEach(c => c.classList.toggle('is-active', c.dataset.src === state.source_type));
        renderFacets(ctx);
        sync(); load(ctx);
        R.api.savedSearchUse(s.id).catch(() => {});
    }
    async function doSaveSearch(ctx) {
        const q = curQuery();
        if (!Object.keys(q).length) { UI.toast('Спершу задайте пошук або фільтр', 'info'); return; }
        const name = await UI.promptModal({
            title: 'Зберегти пошук',
            label: 'Назва збереженого пошуку',
            defaultValue: queryDesc(q),
            confirmLabel: 'Зберегти',
            required: true,
        });
        if (!name) return;
        try { await R.api.savedSearchCreate(name, q); UI.toast('Пошук збережено', 'success'); loadSaved(ctx); }
        catch (err) { UI.toast(err && err.message || 'Не вдалося зберегти', 'error'); }
    }

    function sync() {
        const p = new URLSearchParams();
        if (state.search) p.set('search', state.search);
        if (state.source_type) p.set('source_type', state.source_type);
        if (state.category_id) p.set('category_id', state.category_id);
        FACET_KEYS.forEach(k => { if (state[k]) p.set(k, state[k]); });
        if (state.page > 1) p.set('page', state.page);
        const qs = p.toString();
        R.router.replace('/library' + (qs ? '?' + qs : ''));
    }

    async function load(ctx) {
        if (!listEl) return;
        listEl.innerHTML = UI.skeletonList(7);
        const pagerEl = ctx.mount.querySelector('#rcLibPager');
        pagerEl.innerHTML = '';
        try {
            const data = await R.api.history({
                page: state.page, per_page: PER_PAGE,
                search: state.search || undefined,
                source_type: state.source_type || undefined,
                category_id: state.category_id || undefined,
                // Фасети застосовуємо від 2 символів (як просив користувач).
                tg_sender: state.tg_sender.length >= 2 ? state.tg_sender : undefined,
                tg_chat: state.tg_chat.length >= 2 ? state.tg_chat : undefined,
                yt_author: state.yt_author.length >= 2 ? state.yt_author : undefined,
            });
            if (!ctx.isCurrent()) return;
            curRows = data.transcriptions || [];
            lastData = data;
            renderRows(ctx);
        } catch (err) {
            if (ctx.isCurrent()) listEl.innerHTML = UI.error(err && err.message);
        }
    }

    function renderRows(ctx) {
        if (!listEl) return;
        listEl.classList.toggle('is-selecting', selecting);
        const pagerEl = ctx.mount.querySelector('#rcLibPager');
        renderSelectBar(ctx);
        if (!curRows.length) {
            listEl.innerHTML = UI.empty('Нічого не знайдено',
                state.search || state.source_type || state.category_id || state.tg_sender || state.tg_chat || state.yt_author
                    ? 'Спробуйте змінити фільтри або пошуковий запит.'
                    : 'Архів поки порожній — додайте перший запис.', 'fa-box-open');
            if (pagerEl) pagerEl.innerHTML = '';
            return;
        }
        listEl.innerHTML = curRows.map(rowHTML).join('');
        listEl.querySelectorAll('.rc-rec').forEach(el => {
            el.addEventListener('click', (e) => {
                // Зовнішнє посилання (напр. «відкрити у Telegram») не має ще й
                // відкривати сторінку транскрипту під ним.
                if (e.target.closest('.rc-rec__ext')) return;
                if (selecting) toggleRow(ctx, el);
                else R.router.navigate(el.dataset.href);
            });
        });
        listEl.querySelectorAll('[data-act="delete"]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = parseInt(btn.dataset.id, 10);
                const row = curRows.find(r => r.id === id);
                doSingleDelete(ctx, id, row && row.source_name);
            });
        });
        // Коментарі: лічильники одним запитом ПІСЛЯ рендеру рядків. Не блокуємо
        // список — картка без бейджа лишається повністю робочою, а бейдж
        // доїжджає окремо (лічильники не варті затримки всієї Бібліотеки).
        if (R.comments && !selecting) {
            R.comments.decorate('transcription',
                curRows.map(r => ({ id: r.id, el: listEl.querySelector(`.rc-rec[data-id="${r.id}"]`) }))
                       .filter(x => x.el),
                { slot: '.rc-rec__acts', position: 'afterbegin' });
        }
        if (pagerEl && lastData) {
            pagerEl.innerHTML = pagerHTML(lastData);
            pagerEl.querySelectorAll('[data-page]').forEach(b =>
                b.addEventListener('click', () => { state.page = parseInt(b.dataset.page, 10); sync(); load(ctx); window.scrollTo(0, 0); }));
        }
        renderBulkBar(ctx);
    }

    // Мета-рядок збираємо з масиву й фільтруємо порожнє. Хардкодити роздільники
    // між <span> не можна: у телеграм-записів немає ні мови, ні моделі, і в
    // рядку лишались висячі «· ·» (той самий баг, якого немає в audio.js).
    function meta(parts) {
        const kept = parts.filter(Boolean);
        return kept.join('<span class="sep">·</span>');
    }

    const DOC_TYPE_LABEL = {
        photo: 'фото', video: 'відео', voice: 'голосове', audio: 'аудіо',
        sticker: 'стікер', document: 'файл',
    };

    // Назва чату: власне поле, інакше — з префікса «[TG] Чат: текст…», яким
    // склеєний source_name у інджесті.
    function tgChat(t) {
        if (t.tg_chat_title) return t.tg_chat_title;
        const m = /^\[TG\]\s*([^:]+):/.exec(t.source_name || '');
        return m ? m[1].trim() : 'Telegram';
    }

    // Три форми рядка під три різні типи обʼєкта. Спільний каркас
    // (заголовок / мета / тіло), різний СЕНС кожного слота:
    //  · телеграм — заголовок це провенанс (чат+відправник), читається тіло;
    //  · дзвінок/відео — заголовок це назва запису, він і читається;
    //  · документ — заголовок це імʼя файлу.
    function rowParts(t) {
        const src = t.source_type;
        const dt = DOC_TYPE_LABEL[t.doc_type];
        const tasks = t.open_tasks > 0
            ? `<span class="rc-rec__sig">${t.open_tasks} ${U.plural(t.open_tasks, ['задача', 'задачі', 'задач'])}</span>` : '';
        // «Не збагачено» — виняток (для дзвінків/файлів це меншість), тож це
        // сигнал «Claude ще не розбирав», а не декоративний бейдж на всьому.
        const raw = (src !== 'telegram' && !t.enriched_at)
            ? `<span class="rc-rec__warn">не збагачено</span>` : '';

        if (src === 'telegram') {
            return {
                title: `<span class="rc-rec__chat">${U.esc(tgChat(t))}</span>${
                    t.tg_sender ? `<span class="sep">·</span><span class="rc-rec__who">${U.esc(t.tg_sender)}</span>` : ''}`,
                titleClass: 'rc-rec__title rc-rec__title--label',
                meta: meta([UI.srcBadge(src), dt ? `<span>${dt}</span>` : '',
                            `<span>${U.fmtDate(t.created_at)}</span>`, tasks]),
                bodyLead: true,      // тіло — головний текст, а не хвіст картки
            };
        }
        if (src === 'document') {
            return {
                title: U.esc(t.original_filename || t.source_name || ('Документ #' + t.id)),
                titleClass: 'rc-rec__title',
                meta: meta([UI.srcBadge(src), dt ? `<span>${dt}</span>` : '',
                            t.page_count ? `<span>${t.page_count} ${U.plural(t.page_count, ['стор.', 'стор.', 'стор.'])}</span>` : '',
                            tasks, raw, `<span>${U.fmtDate(t.created_at)}</span>`]),
            };
        }
        const dur = t.youtube_duration;
        return {
            title: U.esc(t.source_name || ('Запис #' + t.id)),
            titleClass: 'rc-rec__title',
            meta: meta([
                UI.srcBadge(src),
                dur ? `<span class="rc-rec__sig">${U.fmtDuration(dur)}</span>` : '',
                t.speaker_count > 0 ? `<span>${t.speaker_count} ${U.plural(t.speaker_count, ['спікер', 'спікери', 'спікерів'])}</span>` : '',
                tasks, raw,
                t.youtube_author ? `<span>${U.esc(t.youtube_author)}</span>` : '',
                `<span>${U.fmtDate(t.created_at)}</span>`,
            ]),
        };
    }

    function rowHTML(t) {
        const href = '/transcript/' + U.slug(t.id, t.source_name);
        const p = rowParts(t);
        const rail = t.youtube_thumbnail
            ? `<img class="rc-rec__thumb" src="${U.esc(t.youtube_thumbnail)}" alt="" loading="lazy">`
            : `<i class="rc-rec__glyph ${U.SRC_ICON[t.source_type] || 'fa-solid fa-file'}" aria-hidden="true"></i>`;
        const preview = U.esc(t.transcript_preview || '')
            .replace(/«MARK»/g, '<mark>').replace(/«\/MARK»/g, '</mark>');
        // Напрямок ховаємо, коли фільтр по ньому вже активний — тоді чип
        // однаковий на всіх рядках і не несе інформації.
        const cat = state && state.category_id ? '' : UI.catChip(t.category_id, cats);
        const tgLink = !selecting && t.tg_link
            ? `<a class="rc-iconbtn rc-rec__ext" href="${U.esc(t.tg_link)}" target="_blank" rel="noopener"
                  title="Відкрити у Telegram" aria-label="Відкрити у Telegram"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : '';
        const check = selecting ? `<span class="rc-rec__check"><input type="checkbox" ${selected.has(t.id) ? 'checked' : ''} tabindex="-1"></span>` : '';
        const del = !selecting ? `<button class="rc-iconbtn rc-rec__del" data-act="delete" data-id="${t.id}" title="Видалити запис" aria-label="Видалити запис"><i class="fa-solid fa-trash"></i></button>` : '';
        return `<div class="rc-rec${selecting ? ' is-select' : ''}${selected.has(t.id) ? ' is-checked' : ''}" data-id="${t.id}" data-href="${U.esc(href)}">
            ${check}<span class="rc-rec__rail">${rail}</span>
            <div class="rc-rec__body">
                <div class="${p.titleClass}">${p.title}</div>
                ${p.bodyLead
                    ? (preview ? `<div class="rc-rec__preview rc-rec__preview--lead">${preview}</div>` : '') + `<div class="rc-rec__meta">${p.meta}</div>`
                    : `<div class="rc-rec__meta">${p.meta}</div>` + (preview ? `<div class="rc-rec__preview">${preview}</div>` : '')}
            </div>
            <div class="rc-rec__aside">${cat}<span class="rc-rec__acts">${tgLink}${del}</span><span class="rc-rec__id rc-mono">#${t.id}</span></div>
        </div>`;
    }

    // ---- selection mode ----------------------------------------------------
    function toggleSelectMode(ctx) {
        selecting = !selecting;
        if (!selecting) selected.clear();
        const btn = ctx.mount.querySelector('#rcLibSelect');
        if (btn) btn.innerHTML = selecting ? '<i class="fa-solid fa-xmark"></i> Готово' : '<i class="fa-solid fa-list-check"></i> Вибрати';
        renderRows(ctx);
    }

    function toggleRow(ctx, el) {
        const id = parseInt(el.dataset.id, 10);
        if (selected.has(id)) selected.delete(id); else selected.add(id);
        el.classList.toggle('is-checked', selected.has(id));
        const chk = el.querySelector('input[type="checkbox"]');
        if (chk) chk.checked = selected.has(id);
        renderBulkBar(ctx);
        renderSelectBar(ctx);
    }

    // T5.5: "select all" header — page-scope (instant, from curRows already
    // in memory) and filter-scope (paginated fetch of every matching id).
    // Always operates within the *current* filter, same as T4.5's
    // clear-on-filter-change — selecting "all N" never reaches outside what's
    // on screen/filtered right now.
    function renderSelectBar(ctx) {
        const bar = ctx.mount.querySelector('#rcLibSelectBar');
        if (!bar) return;
        if (!selecting || !curRows.length) { bar.hidden = true; bar.innerHTML = ''; return; }
        const pageIds = curRows.map(r => r.id);
        const allPageSelected = pageIds.every(id => selected.has(id));
        const total = lastData ? (lastData.total || 0) : pageIds.length;
        bar.hidden = false;
        bar.innerHTML = `
            <label><input type="checkbox" id="rcLibSelAllPage" ${allPageSelected ? 'checked' : ''}> Вибрати всі на сторінці (${pageIds.length})</label>
            ${total > pageIds.length ? `<button class="rc-btn rc-btn--ghost rc-btn--sm" id="rcLibSelAllFiltered">${selected.size >= total ? `Вибрано всі ${total}` : `Вибрати всі ${total} за фільтром`}</button>` : ''}
            ${selected.size ? `<button class="rc-btn rc-btn--ghost rc-btn--sm" id="rcLibSelClear">Зняти вибір (${selected.size})</button>` : ''}`;
        const cb = bar.querySelector('#rcLibSelAllPage');
        if (cb) cb.addEventListener('change', () => {
            if (cb.checked) pageIds.forEach(id => selected.add(id));
            else pageIds.forEach(id => selected.delete(id));
            renderRows(ctx);
        });
        const allBtn = bar.querySelector('#rcLibSelAllFiltered');
        if (allBtn) allBtn.addEventListener('click', () => selectAllFiltered(ctx));
        const clearBtn = bar.querySelector('#rcLibSelClear');
        if (clearBtn) clearBtn.addEventListener('click', () => { selected.clear(); renderRows(ctx); });
    }

    async function selectAllFiltered(ctx) {
        if (!lastData) return;
        const total = lastData.total || 0;
        if (total > SELECT_ALL_CAP) {
            UI.toast(`Забагато записів (${total}) для вибору одразу — звузьте фільтр (до ${SELECT_ALL_CAP}).`, 'error');
            return;
        }
        const btn = ctx.mount.querySelector('#rcLibSelAllFiltered');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Завантаження…'; }
        try {
            const perPage = 100;
            const pages = Math.max(1, Math.ceil(total / perPage));
            const ids = [];
            for (let p = 1; p <= pages; p++) {
                const d = await R.api.history({
                    page: p, per_page: perPage,
                    search: state.search || undefined,
                    source_type: state.source_type || undefined,
                    category_id: state.category_id || undefined,
                    tg_sender: state.tg_sender.length >= 2 ? state.tg_sender : undefined,
                    tg_chat: state.tg_chat.length >= 2 ? state.tg_chat : undefined,
                    yt_author: state.yt_author.length >= 2 ? state.yt_author : undefined,
                });
                (d.transcriptions || []).forEach(r => ids.push(r.id));
            }
            if (!ctx.isCurrent()) return;
            ids.forEach(id => selected.add(id));
            UI.toast(`Вибрано ${selected.size} записів за фільтром`, 'success');
            renderRows(ctx);
        } catch (err) {
            UI.toast((err && err.message) || 'Не вдалося вибрати всі записи', 'error');
            if (btn) btn.disabled = false;
        }
    }

    function renderBulkBar(ctx) {
        const bar = ctx.mount.querySelector('#rcLibBulk');
        if (!bar) return;
        if (!selecting || selected.size === 0) { bar.hidden = true; bar.innerHTML = ''; return; }
        const catO = UI.catOptions('', { first: 'none' });
        bar.hidden = false;
        bar.innerHTML = `
            <span class="rc-bulkbar__n">${selected.size} вибрано</span>
            <select class="rc-select rc-select--sm rc-catsel" id="rcBulkCat" data-cat-first="none">${catO}</select>
            <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcBulkApply">Призначити напрямок</button>
            <div class="rc-yt__spacer"></div>
            <select class="rc-select rc-select--sm" id="rcBulkFmt">
                <option value="md">Markdown</option>
                <option value="txt">Текст</option>
                <option value="docx">Word</option>
                <option value="json">JSON</option>
            </select>
            <button class="rc-btn rc-btn--sm" id="rcBulkExport"><i class="fa-solid fa-file-export"></i> Експорт</button>
            <button class="rc-btn rc-btn--sm" id="rcBulkDelete"><i class="fa-solid fa-trash"></i> Видалити</button>`;
        bar.querySelector('#rcBulkApply').addEventListener('click', () => doBulkCategory(ctx));
        bar.querySelector('#rcBulkExport').addEventListener('click', () => doBulkExport(ctx));
        bar.querySelector('#rcBulkDelete').addEventListener('click', () => doBulkDelete(ctx));
    }

    async function doBulkExport(ctx) {
        const ids = [...selected];
        const fmt = (ctx.mount.querySelector('#rcBulkFmt') || {}).value || 'md';
        const btn = ctx.mount.querySelector('#rcBulkExport');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Експорт…'; }
        try {
            const res = await R.api.bulkExport(ids, fmt);
            if (!res.ok) { let m = 'HTTP ' + res.status; try { const j = await res.json(); if (j.error) m = j.error; } catch (_) {} throw new Error(m); }
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = `recall-export-${ids.length}.${fmt}`;
            document.body.appendChild(a); a.click();
            setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
            UI.toast(`Експортовано ${ids.length} записів (${fmt.toUpperCase()})`, 'success');
        } catch (err) { UI.toast(err && err.message || 'Помилка експорту', 'error'); }
        finally { if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-file-export"></i> Експорт'; } }
    }

    async function doBulkCategory(ctx) {
        const sel = ctx.mount.querySelector('#rcBulkCat');
        const cid = sel && sel.value ? Number(sel.value) : null;
        const ids = [...selected];
        const apply = ctx.mount.querySelector('#rcBulkApply');
        if (apply) apply.disabled = true;
        try {
            const r = await R.api.post('/api/memory/transcriptions/bulk-category', { ids, category_id: cid });
            UI.toast(`Напрямок ${cid ? 'призначено' : 'знято'} для ${r.updated} запис(ів)`, 'success');
            selecting = false; selected.clear();
            const btn = ctx.mount.querySelector('#rcLibSelect');
            if (btn) btn.innerHTML = '<i class="fa-solid fa-list-check"></i> Вибрати';
            load(ctx);
        } catch (err) { UI.toast(err && err.message, 'error'); if (apply) apply.disabled = false; }
    }

    // Одиничне видалення (soft-delete + undo-toast). Оптимістично прибирає
    // рядок зі списку; якщо DELETE впаде — повертає рядок назад.
    async function doSingleDelete(ctx, id, title) {
        const ok = await UI.confirmModal({
            title: 'Видалити запис',
            message: `Видалити «${title || ('Запис #' + id)}» з архіву? Файл одразу приховається зі списків; протягом короткого часу його можна відновити (сповіщення знизу).`,
            confirmLabel: 'Видалити',
        });
        if (!ok) return;
        const idx = curRows.findIndex(r => r.id === id);
        const removedRow = idx >= 0 ? curRows[idx] : null;
        if (idx >= 0) curRows.splice(idx, 1);
        selected.delete(id);
        renderRows(ctx);
        try {
            await R.api.del('/api/history/' + id);
            if (R.shell && R.shell.loadCounts) R.shell.loadCounts();
            UI.actionToast('Запис видалено', 'Скасувати', async () => {
                try {
                    await R.api.post('/api/history/' + id + '/restore');
                    UI.toast('Запис відновлено', 'success');
                    load(ctx);
                    if (R.shell && R.shell.loadCounts) R.shell.loadCounts();
                } catch (e) { UI.toast((e && e.message) || 'Не вдалося відновити', 'error'); }
            });
        } catch (err) {
            if (removedRow && ctx.isCurrent()) { curRows.splice(idx, 0, removedRow); renderRows(ctx); }
            UI.toast((err && err.message) || 'Не вдалося видалити', 'error');
        }
    }

    async function doBulkDelete(ctx) {
        const ids = [...selected];
        const BIG_N = 10; // від такої кількості вимагаємо усвідомленого type-to-confirm
        const needsTypeConfirm = ids.length > BIG_N;
        const ok = await UI.modal({
            title: 'Видалити записи',
            icon: 'fa-trash',
            bodyHTML: `
                <p>Видалити <b>${ids.length}</b> запис(ів) з архіву? Це фізичне видалення — файли й дані стираються одразу, скасувати цю дію НЕ можна.</p>
                ${needsTypeConfirm ? `
                    <label class="rc-field__label" for="rcBulkDelConfirm">Введіть <b>${ids.length}</b>, щоб підтвердити</label>
                    <input class="rc-input" id="rcBulkDelConfirm" autocomplete="off" placeholder="${ids.length}">
                ` : ''}`,
            actions: [
                { label: 'Скасувати', value: false },
                {
                    label: `Видалити ${ids.length} записів`, value: true, primary: true, danger: true,
                    validate: (ov) => {
                        if (!needsTypeConfirm) return true;
                        const inp = ov.querySelector('#rcBulkDelConfirm');
                        return !!inp && inp.value.trim() === String(ids.length);
                    },
                },
            ],
            autofocus: needsTypeConfirm ? '#rcBulkDelConfirm' : '.rc-btn--primary',
        });
        if (!ok) return;
        try {
            await R.api.post('/api/history/bulk_delete', { ids });
            UI.toast(`Видалено ${ids.length} записів`, 'success');
            selecting = false; selected.clear();
            const btn = ctx.mount.querySelector('#rcLibSelect');
            if (btn) btn.innerHTML = '<i class="fa-solid fa-list-check"></i> Вибрати';
            if (R.shell && R.shell.loadCounts) R.shell.loadCounts();
            load(ctx);
        } catch (err) { UI.toast(err && err.message, 'error'); }
    }

    function pagerHTML(d) {
        const total = d.total_pages || 1, page = d.page || 1;
        if (total <= 1) return `<div class="rc-pager"><span class="rc-pager__info">${d.total || 0} записів</span></div>`;
        const prev = page > 1 ? `<button class="rc-btn rc-btn--sm" data-page="${page - 1}"><i class="fa-solid fa-chevron-left"></i></button>` : '';
        const next = page < total ? `<button class="rc-btn rc-btn--sm" data-page="${page + 1}"><i class="fa-solid fa-chevron-right"></i></button>` : '';
        return `<div class="rc-pager">${prev}<span class="rc-pager__info">стор. ${page} / ${total} · ${d.total} записів</span>${next}</div>`;
    }

    R.views.library = { render, destroy() { listEl = null; state = null; selecting = false; selected = new Set(); curRows = []; } };
})();
