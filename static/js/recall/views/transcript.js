/* Recall — Transcript detail. The "catalog record": full provenance header
   branched per source_type, the transcript itself (text / timecodes /
   polished), and a connections rail (entities, tasks, same category). */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;
    let cats = null, data = null, mode = 'text', audioReq = null, ppOpen = null, speakersOpen = false;
    let bookmarks = {}, bookmarksOpen = false;   // {segment_index: {id, note}}
    let _onTime = null;   // audio timeupdate handler (segment ↔ playback sync)
    let copilotTL = null, copilotOpen = false, cpFilter = 'all';   // co-pilot timeline (Phase 19, Крок 7)
    let videoInfo = null, videoPolling = null;   // screen-video panel (Phase 23, Story B-C)

    // word-cloud stopwords (UA + RU + EN) — client-side, no endpoint
    const STOPWORDS = new Set(('і та а але чи що як це то ще же ну от тільки тут там не ні був була було будуть буде я ти він вона воно ми ви вони мене тебе його її нас вас їх собі свій своя своє свої один одна одно до від за на у в з зі під над при без для через між перед після коли де куди чому хто який яка яке які щось хтось такий така таке такі лише уже вже теж також цей ця ці той те ті б би ж '
        + 'и но или то вот только нет что как это так ты есть если может мне все всё сейчас уже да ну же бы чтобы был была было будут будет он она оно мы вы они меня тебя его её их себе свой своя своё свои одна одно от с со под над при без для через между перед после когда где куда почему кто какой какая какое какие такой такая такое такие лишь еще тоже также этот эта эти тот те ли '
        + 'a an the and or but if of to in on at by for from with is was were are be been being do does did have has had will would can could should i you he she it we they this that these those my your his her its our their what where when who how why so as than then there here no not yes').split(/\s+/).filter(Boolean));

    async function render(ctx) {
        const id = U.parseId(ctx.params.slug);
        if (!Number.isInteger(id)) { ctx.mount.innerHTML = UI.error('Некоректне посилання на запис.'); return; }

        ctx.mount.innerHTML = `<div class="rc-record"><div class="rc-record__head">
            <div class="rc-skel" style="height:14px;width:180px;margin-bottom:16px"></div>
            <div class="rc-skel" style="height:34px;width:70%;margin-bottom:20px"></div>
            <div class="rc-skel" style="height:80px;width:100%"></div></div></div>`;

        cats = await UI.loadCategories();
        let t;
        try { t = await R.api.transcript(id); }
        catch (err) {
            if (ctx.isCurrent()) ctx.mount.innerHTML = UI.error(err.status === 404 ? 'Запис не знайдено.' : err.message);
            return;
        }
        if (!ctx.isCurrent()) return;
        data = t; ppOpen = null; speakersOpen = false;
        // honor ?view=segments always; ?view=polished only if polished text exists
        mode = ctx.query.view === 'segments' ? 'segments'
            : (ctx.query.view === 'polished' && t.polished_text) ? 'polished' : 'text';

        // load segment bookmarks (diarized/segmented records only)
        bookmarks = {}; bookmarksOpen = false;
        if ((t.segments || []).length) {
            try {
                const bd = await R.api.bookmarks(id);
                (bd.bookmarks || []).forEach(b => { bookmarks[b.segment_index] = { id: b.id, note: b.note || '' }; });
            } catch (_) {}
            if (!ctx.isCurrent()) return;
        }

        // canonical slug fix
        const canonical = '/transcript/' + U.slug(id, t.source_name);
        if (location.pathname !== canonical) R.router.replace(canonical + location.search);

        paint(ctx, id, t);
    }

    function paint(ctx, id, t) {
        speakersOpen = false; bookmarksOpen = false; _onTime = null;   // panels/audio rebuilt on every (re)paint
        copilotTL = null; copilotOpen = false; cpFilter = 'all';
        const segs = t.segments || [];
        const hasSpeakers = segs.some(s => s && s.speaker);
        const hasPolished = !!(t.polished_text && t.polished_text.trim());

        ctx.mount.innerHTML = `
            <a class="rc-btn rc-btn--ghost rc-btn--sm" href="/library" style="margin-bottom:16px"><i class="fa-solid fa-arrow-left"></i> Бібліотека</a>
            <div class="rc-record">
                <div class="rc-record__head">
                    ${t.youtube_thumbnail ? `<img class="rc-record__thumb" src="${U.esc(t.youtube_thumbnail)}" alt="">` : ''}
                    <div class="rc-record__accession">
                        <span>RECALL · ЗАПИС <span class="rc-id">#${id}</span></span>
                        <span>·</span>${UI.srcBadge(t.source_type)}
                    </div>
                    <h1 class="rc-record__title">${U.esc(t.source_name || ('Запис #' + id))}</h1>
                    ${provenanceHTML(t)}
                </div>
                <div class="rc-record__actions">
                    <div class="rc-seg-toggle" id="rcViewToggle">
                        <button data-mode="text" class="${mode==='text'?'is-active':''}">Текст</button>
                        <button data-mode="segments" class="${mode==='segments'?'is-active':''}">Таймкоди</button>
                        ${hasPolished ? `<button data-mode="polished" class="${mode==='polished'?'is-active':''}">✦ Поліпшений</button>` : ''}
                    </div>
                    ${hasSpeakers ? `<button class="rc-btn rc-btn--sm" id="rcSpeakersBtn"><i class="fa-solid fa-user-pen"></i> Спікери</button>` : ''}
                    ${segs.length ? `<button class="rc-btn rc-btn--sm" id="rcBookmarksBtn"><i class="fa-solid fa-bookmark"></i> Закладки<span id="rcBmCount"></span></button>` : ''}
                    <button class="rc-btn rc-btn--sm" id="rcCommentsBtn"><i class="fa-regular fa-comment-dots"></i> Коментарі<span id="rcCmCount"></span></button>
                    <button class="rc-btn rc-btn--sm" id="rcCopy"><i class="fa-solid fa-copy"></i> Копіювати</button>
                    <div class="rc-spacer"></div>
                    <button class="rc-btn rc-btn--sm" data-export="txt">TXT</button>
                    <button class="rc-btn rc-btn--sm" data-export="srt">SRT</button>
                    <button class="rc-btn rc-btn--sm" data-export="json">JSON</button>
                    <button class="rc-btn rc-btn--sm" id="rcPolish"><i class="fa-solid fa-wand-magic-sparkles"></i> Покращити</button>
                    <button class="rc-btn rc-btn--sm" id="rcSummary"><i class="fa-solid fa-list-check"></i> Резюме</button>
                    <button class="rc-btn rc-btn--sm rc-btn--video-hidden" id="rcVideoBtn" style="display:none"><i class="fa-solid fa-film"></i> <span id="rcVideoBtnLbl">Розібрати відео</span></button>
                    <label id="rcVisionClaudeWrap" style="display:none;align-items:center;gap:4px;font-size:.82em;color:#777;cursor:pointer" title="Опис кадрів через Claude vision (платно, точніше). Без галочки — локальна модель ($0)."><input type="checkbox" id="rcVisionClaude" style="margin:0">Claude-опис</label>
                    <button class="rc-btn rc-btn--sm" data-pp="translate"><i class="fa-solid fa-language"></i> Переклад</button>
                    <button class="rc-btn rc-btn--sm" data-pp="topics"><i class="fa-solid fa-tags"></i> Теми</button>
                    <button class="rc-btn rc-btn--sm" data-pp="wordcloud"><i class="fa-solid fa-cloud"></i> Хмара слів</button>
                    <button class="rc-btn rc-btn--sm" data-pp="sentiment"><i class="fa-solid fa-face-smile"></i> Емоції</button>
                </div>
                <div class="rc-mono" id="rcAiKeyChip" style="margin:-8px 0 12px;font-size:var(--rc-t-xs)"></div>
                <div class="rc-record__body">
                    <audio class="rc-audio" id="rcAudio" controls preload="none"></audio>
                    <div id="rcVideoPanel"></div>
                    <div id="rcSpeakersPanel"></div>
                    <div id="rcBookmarksPanel"></div>
                    <div id="rcCommentsPanel" hidden></div>
                    <div id="rcSummaryPanel"></div>
                    <div id="rcCopilotPanel"></div>
                    <div id="rcPostproc"></div>
                    <div id="rcTranscriptBody"></div>
                </div>
            </div>
            <div class="rc-rel" id="rcRel"></div>`;

        // audio (lazy)
        const audio = ctx.mount.querySelector('#rcAudio');
        audio.src = R.api.audioUrl(id);
        audio.addEventListener('error', () => { audio.style.display = 'none'; }, { once: true });

        // view toggle
        ctx.mount.querySelector('#rcViewToggle').addEventListener('click', (e) => {
            const b = e.target.closest('button[data-mode]'); if (!b) return;
            mode = b.dataset.mode;
            ctx.mount.querySelectorAll('#rcViewToggle button').forEach(x => x.classList.toggle('is-active', x === b));
            R.router.replace(location.pathname + (mode !== 'text' ? '?view=' + mode : ''));
            renderBody(ctx, t);
        });

        // copy
        ctx.mount.querySelector('#rcCopy').addEventListener('click', () => {
            const txt = (mode === 'polished' && hasPolished) ? t.polished_text : t.transcript_text || '';
            navigator.clipboard.writeText(txt).then(
                () => UI.toast('Скопійовано', 'success'),
                () => UI.toast('Не вдалося скопіювати', 'error'));
        });

        // export
        ctx.mount.querySelectorAll('[data-export]').forEach(b =>
            b.addEventListener('click', () => doExport(b.dataset.export, t)));

        // per-record speaker naming / reassignment (diarized records)
        const spkBtn = ctx.mount.querySelector('#rcSpeakersBtn');
        if (spkBtn) spkBtn.addEventListener('click', () => openSpeakers(ctx, id, t));

        // bookmarks (segmented records) + editable напрямок (✨ suggest)
        const bmBtn = ctx.mount.querySelector('#rcBookmarksBtn');
        if (bmBtn) bmBtn.addEventListener('click', () => openBookmarks(ctx, t));
        updateBmCount(ctx);

        // Коментарі власника. Якір за таймкодом береться з позиції плеєра: якщо
        // аудіо грає, коментар прикріплюється до моменту, який слухали, — це
        // єдиний спосіб сказати «ось ТУТ насправді було інакше», не рахуючи
        // хвилини руками.
        const cmBtn = ctx.mount.querySelector('#rcCommentsBtn');
        if (cmBtn) cmBtn.addEventListener('click', () => toggleComments(ctx, id));
        updateCmCount(ctx, id);
        bindCategoryEditor(ctx, id, t);

        // polish + summarize (existing endpoints, native)
        ctx.mount.querySelector('#rcPolish').addEventListener('click', () => doPolish(ctx, id));
        ctx.mount.querySelector('#rcSummary').addEventListener('click', () => doSummary(ctx, id));
        // post-processing: translate / topics / wordcloud / sentiment
        ctx.mount.querySelectorAll('[data-pp]').forEach(b =>
            b.addEventListener('click', () => pp(ctx, id, t, b.dataset.pp)));

        // wire video button (wired after paint; panel loaded async below)
        const videoBtn = ctx.mount.querySelector('#rcVideoBtn');
        if (videoBtn) videoBtn.addEventListener('click', () => {
            if (videoInfo && !videoInfo.video_analysis_at) doAnalyzeVideo(ctx, id);
            else if (videoInfo && videoInfo.video_analysis_at) toggleVideoPanel(ctx, id);
        });

        renderBody(ctx, t);
        renderRelated(ctx, id, t);
        // show cached summary if present
        if (t.summary_json) { try { renderSummary(ctx, JSON.parse(t.summary_json)); } catch (_) {} }
        // co-pilot timeline tab — lazy: inject button only if this record had a session
        loadCopilotTab(ctx, id);
        // screen-video panel — lazy, best-effort (Phase 23, Story B-C)
        loadVideoPanel(ctx, id);
        // T5.1: постійний статус-чип «ключ налаштований/ні» поруч із Claude-
        // фічами (Покращити/Резюме/Переклад/...) — щоб polish/enrich не падали
        // тихо в незрозумілу помилку, коли ключ ще не заданий у Settings.
        loadAiKeyChip(ctx);
    }

    // ---- Claude API-ключ: статус-чип (T5.1) --------------------------------
    async function loadAiKeyChip(ctx) {
        const chip = ctx.mount.querySelector('#rcAiKeyChip');
        if (!chip) return;
        try {
            const s = await R.api.get('/api/settings/anthropic-key/status');
            if (!ctx.isCurrent()) return;
            if (s && s.configured) {
                chip.innerHTML = `<i class="fa-solid fa-circle" style="font-size:7px;color:var(--rc-ok)"></i> Claude-ключ налаштовано`;
            } else {
                chip.innerHTML = `<a href="/settings" style="color:var(--rc-warn,#b07a16)"><i class="fa-solid fa-triangle-exclamation"></i> Покращити/Резюме/Переклад потребують Claude-ключа — додайте в Налаштуваннях</a>`;
            }
        } catch (_) { chip.innerHTML = ''; }   // best-effort — не критично для перегляду транскрипту
    }

    // ---- Co-pilot timeline (Phase 19, Крок 7) ----
    // Паралельна доріжка під діалогом: усе, що ко-пілот робив у кожен момент;
    // клік по події → seek аудіо на той таймкод (механіка з Phase 18).
    const CP_EVENT = {
        topic_shift:      { icon: 'fa-diagram-project', cls: 'is-topic', lbl: 'нова тема' },
        topic_return:     { icon: 'fa-rotate-left',     cls: 'is-topic', lbl: 'повернення до теми' },
        retrieval:        { icon: 'fa-magnifying-glass', cls: 'is-rag', lbl: 'пошук в архіві' },
        insight_local:    { icon: 'fa-microchip',       cls: 'is-local', lbl: 'локальна підказка' },
        insight_verified: { icon: 'fa-circle-check',    cls: 'is-verified', lbl: 'перевірено Claude' },
        operator_action:  { icon: 'fa-user',            cls: 'is-op', lbl: 'дія оператора' },
    };
    const CP_KIND_LBL = { contradiction: 'протиріччя', question: 'питання', clarification: 'уточнення', fact: 'факт' };
    const CP_OPACT_LBL = { pin: 'закріпив', unpin: 'відкріпив', dismiss: 'відхилив', thumbs_up: '👍 корисно', thumbs_down: '👎 не корисно', escalate: 'копнути глибше' };

    async function loadCopilotTab(ctx, id) {
        let r = null;
        try { r = await R.api.get('/api/copilot/by-transcription/' + id); } catch (_) { return; }
        if (!ctx.isCurrent() || !r || !r.found || !r.timeline) return;
        copilotTL = r.timeline;
        // inject button into the actions bar (before the spacer)
        const bar = ctx.mount.querySelector('.rc-record__actions');
        const spacer = bar && bar.querySelector('.rc-spacer');
        if (!bar || ctx.mount.querySelector('#rcCopilotBtn')) return;
        const n = (copilotTL.aggregates || {});
        const cnt = (n.insight_local || 0) + (n.insight_verified || 0);
        const btn = U.el('button', { class: 'rc-btn rc-btn--sm', id: 'rcCopilotBtn' },
            `<i class="fa-solid fa-wand-magic-sparkles"></i> Ко-пілот${cnt ? `<span class="rc-mono"> ${cnt}</span>` : ''}`);
        bar.insertBefore(btn, spacer || null);
        btn.addEventListener('click', () => toggleCopilot(ctx));
    }

    function toggleCopilot(ctx) {
        const panel = ctx.mount.querySelector('#rcCopilotPanel');
        const btn = ctx.mount.querySelector('#rcCopilotBtn');
        if (!panel) return;
        if (copilotOpen) { copilotOpen = false; panel.innerHTML = ''; if (btn) btn.classList.remove('is-active'); return; }
        copilotOpen = true; if (btn) btn.classList.add('is-active');
        renderCopilotPanel(ctx);
    }

    function renderCopilotPanel(ctx) {
        const panel = ctx.mount.querySelector('#rcCopilotPanel');
        if (!panel || !copilotTL) return;
        const s = copilotTL.session || {};
        const agg = copilotTL.aggregates || {};
        const cfg = s.config || {};
        const modeLbl = { light: 'лайт', medium: 'середній', hard: 'жорсткий' }[cfg.mode || s.mode] || '';
        const cost = (s.cost_estimate != null && s.cost_estimate > 0) ? `$${Number(s.cost_estimate).toFixed(2)}` : '$0';
        const FILTERS = [['all', 'усе'], ['contradiction', 'протиріччя'], ['question', 'питання'],
            ['clarification', 'уточнення'], ['fact', 'факти']];
        panel.innerHTML = `<div class="rc-cptl">
            <div class="rc-cptl__head">
                <span class="rc-cptl__title"><i class="fa-solid fa-wand-magic-sparkles"></i> Ко-пілот сесії</span>
                <span class="rc-cptl__meta rc-mono">${modeLbl ? 'режим: ' + modeLbl + ' · ' : ''}${(agg.insight_local || 0) + (agg.insight_verified || 0)} підказок · ${agg.insight_verified || 0} перевірено · ${cost}</span>
                <div class="rc-cptl__exp">
                    <button class="rc-btn rc-btn--sm" data-exp="md" title="Експорт у Markdown">MD</button>
                    <button class="rc-btn rc-btn--sm" data-exp="json" title="Експорт у JSON">JSON</button>
                    <button class="rc-btn rc-btn--sm" data-exp="archive" title="Зберегти підказки в архів (шукабельно у RAG)"><i class="fa-solid fa-database"></i> В архів</button>
                </div>
                <button class="rc-cptl__close" id="rcCpTlClose" title="Закрити"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div class="rc-cptl__filters">${FILTERS.map(([k, l]) =>
                `<button class="rc-cptl__filt rc-mono${cpFilter === k ? ' is-active' : ''}" data-filt="${k}">${l}</button>`).join('')}</div>
            <div class="rc-cptl__list" id="rcCpTlList">${renderTimelineRows()}</div>
        </div>`;
        panel.querySelector('#rcCpTlClose').addEventListener('click', () => toggleCopilot(ctx));
        panel.querySelectorAll('[data-exp]').forEach(b => b.addEventListener('click', () => {
            if (b.dataset.exp === 'archive') reingestCopilot(ctx);
            else downloadCopilotExport(b.dataset.exp);
        }));
        panel.querySelectorAll('[data-filt]').forEach(b => b.addEventListener('click', () => {
            cpFilter = b.dataset.filt;
            panel.querySelectorAll('[data-filt]').forEach(x => x.classList.toggle('is-active', x === b));
            const list = panel.querySelector('#rcCpTlList');
            if (list) list.innerHTML = renderTimelineRows();
            wireTimelineSeek(ctx, panel);
        }));
        wireTimelineSeek(ctx, panel);
    }

    function eventMatchesFilter(e) {
        if (cpFilter === 'all') return true;
        // показуємо лише інсайти вибраного типу (теми/пошук/дії ховаються у фільтрі)
        if (e.kind !== 'insight_local' && e.kind !== 'insight_verified') return false;
        return (e.payload && e.payload.kind) === cpFilter;
    }
    function renderTimelineRows() {
        const events = (copilotTL.events || []).filter(eventMatchesFilter);
        if (!events.length) return `<div class="rc-cptl__empty rc-mono">Нічого за цим фільтром.</div>`;
        return events.map(e => {
            const m = CP_EVENT[e.kind] || { icon: 'fa-circle', cls: '', lbl: e.kind };
            const t = (e.ts_offset_sec != null) ? U.fmtDuration(e.ts_offset_sec) : '—';
            const p = e.payload || {};
            let body = '';
            if (e.kind === 'topic_shift' || e.kind === 'topic_return') {
                body = `<b>${U.esc(p.label || 'тема')}</b>`;
            } else if (e.kind === 'retrieval') {
                body = `«${U.esc(p.query || '')}» <span class="rc-mono" style="color:var(--rc-ink-3)">${p.n || 0} фрагм.</span>`;
            } else if (e.kind === 'operator_action') {
                body = CP_OPACT_LBL[e.operator_action] || U.esc(e.operator_action || '');
            } else { // insight_local | insight_verified
                const kindLbl = CP_KIND_LBL[p.kind] || '';
                const conf = e.confidence != null ? ` · ${Math.round(e.confidence * 100)}%` : '';
                const ev = (p.evidence || []).map(x => {
                    const name = x.source_name || ('#' + x.transcription_id);
                    return `<a class="rc-cptl__ev rc-mono" href="/transcript/${U.slug(x.transcription_id, x.source_name || 'Джерело')}">[${U.esc(name)}]</a>`;
                }).join(' ');
                body = `<span class="rc-cptl__k rc-mono">${kindLbl}${conf}</span> ${U.esc(p.text || '')}${ev ? ' ' + ev : ''}`;
            }
            const seekable = e.ts_offset_sec != null;
            return `<div class="rc-cptl__row ${m.cls}${seekable ? ' is-seek' : ''}" ${seekable ? `data-seek="${e.ts_offset_sec}"` : ''}>
                <span class="rc-cptl__t rc-mono">${t}</span>
                <i class="fa-solid ${m.icon} rc-cptl__ico" title="${m.lbl}"></i>
                <span class="rc-cptl__body">${body}</span>
            </div>`;
        }).join('');
    }
    async function downloadCopilotExport(fmt) {
        const sid = (copilotTL.session || {}).id;
        if (!sid) return;
        try {
            const res = await fetch('/api/copilot/' + sid + '/export?format=' + fmt);
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'copilot-session-' + sid + '.' + fmt;
            document.body.appendChild(a); a.click();
            URL.revokeObjectURL(a.href); a.remove();
            UI.toast('Експортовано ' + fmt.toUpperCase(), 'success');
        } catch (_) { UI.toast('Помилка експорту', 'error'); }
    }
    async function reingestCopilot(ctx) {
        const sid = (copilotTL.session || {}).id;
        if (!sid) return;
        try {
            const r = await R.api.post('/api/copilot/' + sid + '/reingest', {});
            if (r && r.transcription_id) {
                UI.toast('Збережено в архів — індексується для пошуку', 'success');
                R.router.navigate('/transcript/' + U.slug(r.transcription_id, r.source_name || 'Ко-пілот'));
            }
        } catch (err) { UI.toast('Не вдалось зберегти: ' + (err && err.message), 'error'); }
    }

    function wireTimelineSeek(ctx, panel) {
        const audio = ctx.mount.querySelector('#rcAudio');
        panel.querySelectorAll('.rc-cptl__row.is-seek').forEach(row => {
            row.addEventListener('click', (e) => {
                if (e.target.closest('a')) return;   // evidence link — let it navigate
                const at = parseFloat(row.dataset.seek) || 0;
                if (audio && audio.src) { try { audio.currentTime = at; audio.play().catch(() => {}); } catch (_) {} }
                syncVideoNow(ctx, at);
            });
        });
    }

    // ---- Screen-video panel (Phase 23, Story B-C) ----
    // Architecture: audio = sound source (#rcAudio); video = muted screen recording
    // synced via timeupdate. startOffset = recording-info.start_offset_sec.

    async function loadVideoPanel(ctx, id) {
        videoInfo = null;
        let info;
        try { info = await R.api.get('/api/transcription/' + id + '/recording-info'); }
        catch (_) { return; }   // endpoint not present or no video — silently skip
        if (!ctx.isCurrent()) return;
        if (!info || !info.has_video) return;

        videoInfo = info;
        const btn = ctx.mount.querySelector('#rcVideoBtn');
        const lbl = ctx.mount.querySelector('#rcVideoBtnLbl');
        if (btn) btn.style.display = '';   // reveal button
        const claudeWrap = ctx.mount.querySelector('#rcVisionClaudeWrap');
        if (claudeWrap) claudeWrap.style.display = 'inline-flex';   // reveal Claude-опис toggle
        updateVideoBtnLabel(ctx);

        // If already analysed, render full panel immediately
        if (info.primary_video_available) {
            renderVideoElement(ctx, id, info);
        }
        if (info.keyframes_count > 0) {
            loadKeyframes(ctx, id);
        }
    }

    function updateVideoBtnLabel(ctx) {
        const lbl = ctx.mount.querySelector('#rcVideoBtnLbl');
        if (!lbl || !videoInfo) return;
        if (videoInfo.video_analysis_at && videoInfo.keyframes_count > 0) {
            lbl.textContent = 'Перерозібрати (' + videoInfo.keyframes_count + ' кадрів)';
        } else if (videoInfo.video_analysis_at) {
            lbl.textContent = 'Перерозібрати відео';
        } else {
            lbl.textContent = 'Розібрати відео';
        }
    }

    function toggleVideoPanel(ctx, id) {
        const panel = ctx.mount.querySelector('#rcVideoPanel');
        if (!panel) return;
        if (panel.querySelector('.rc-tvideo')) {
            // already showing — collapse
            panel.innerHTML = '';
        } else {
            renderVideoElement(ctx, id, videoInfo);
            if (videoInfo && videoInfo.keyframes_count > 0) loadKeyframes(ctx, id);
        }
    }

    function renderVideoElement(ctx, id, info) {
        const panel = ctx.mount.querySelector('#rcVideoPanel');
        if (!panel || panel.querySelector('.rc-tvideo')) return;   // already rendered

        const videoSrc = '/api/transcription/' + id + '/video';
        panel.innerHTML = `<div class="rc-tvideo">
            <div class="rc-tvideo__head">
                <i class="fa-solid fa-display rc-tvideo__ico"></i>
                <span class="rc-tvideo__title">Запис екрану</span>
                <button class="rc-tvideo__close rc-iconbtn" id="rcVideoClose" title="Згорнути"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <video class="rc-tvideo__el" id="rcVideo" muted playsinline preload="metadata"
                   src="${U.esc(videoSrc)}"></video>
            <div class="rc-kfstrip" id="rcKfStrip"><span class="rc-mono rc-kfstrip__empty">Кадри завантажуються…</span></div>
        </div>`;

        const video = panel.querySelector('#rcVideo');
        if (video) setupVideoSync(ctx, video, info.start_offset_sec || 0);

        panel.querySelector('#rcVideoClose').addEventListener('click', () => {
            panel.innerHTML = '';
        });
    }

    function setupVideoSync(ctx, video, startOffset) {
        const audio = ctx.mount.querySelector('#rcAudio');
        if (!audio || !video) return;

        // Sync on audio timeupdate
        audio.addEventListener('timeupdate', function onTU() {
            if (!video.isConnected) { audio.removeEventListener('timeupdate', onTU); return; }
            const want = Math.max(0, audio.currentTime - startOffset);
            if (Math.abs(video.currentTime - want) > 0.3) {
                try { video.currentTime = want; } catch (_) {}
            }
        });

        // Mirror play/pause
        audio.addEventListener('play', function onPlay() {
            if (!video.isConnected) { audio.removeEventListener('play', onPlay); return; }
            video.play().catch(() => {});
        });
        audio.addEventListener('pause', function onPause() {
            if (!video.isConnected) { audio.removeEventListener('pause', onPause); return; }
            video.pause();
        });

        // Sync current position immediately (for when audio is already mid-play)
        const want = Math.max(0, audio.currentTime - startOffset);
        try { video.currentTime = want; } catch (_) {}
        if (!audio.paused) video.play().catch(() => {});
    }

    // Seek the video immediately (called from segment/timeline click handlers)
    function syncVideoNow(ctx, audioTime) {
        if (!videoInfo) return;
        const video = ctx.mount.querySelector('#rcVideo');
        if (!video) return;
        const want = Math.max(0, audioTime - (videoInfo.start_offset_sec || 0));
        try { video.currentTime = want; } catch (_) {}
    }

    async function loadKeyframes(ctx, id) {
        let data;
        try { data = await R.api.get('/api/transcription/' + id + '/keyframes'); }
        catch (_) { return; }
        if (!ctx.isCurrent()) return;
        const kfs = (data && data.keyframes) || [];
        const strip = ctx.mount.querySelector('#rcKfStrip');
        if (!strip) return;
        if (!kfs.length) { strip.innerHTML = `<span class="rc-mono rc-kfstrip__empty">Кадри відсутні.</span>`; return; }

        strip.innerHTML = kfs.map(kf => {
            const ts = U.fmtDuration(kf.ts_offset_sec || 0);
            const vis = kf.vision_excerpt ? kf.vision_excerpt.slice(0, 200) : '';
            const ocr = kf.ocr_excerpt ? kf.ocr_excerpt.slice(0, 120) : '';
            const title = U.esc((vis && ocr) ? (vis + '\nНа екрані: ' + ocr) : (vis || ocr));
            return `<div class="rc-kf" data-ts="${U.esc(String(kf.ts_offset_sec || 0))}" title="${title}">
                <img class="rc-kf__img" src="/api/transcription/${id}/keyframe/${U.esc(String(kf.id))}" loading="lazy" alt="">
                <span class="rc-kf__ts rc-mono">${ts}</span>
            </div>`;
        }).join('');

        const audio = ctx.mount.querySelector('#rcAudio');
        strip.querySelectorAll('.rc-kf').forEach(el => {
            el.addEventListener('click', () => {
                const at = parseFloat(el.dataset.ts) || 0;
                if (audio && audio.src) {
                    try { audio.currentTime = at; audio.play().catch(() => {}); } catch (_) {}
                }
                syncVideoNow(ctx, at);
            });
        });
    }

    async function doAnalyzeVideo(ctx, id) {
        const btn = ctx.mount.querySelector('#rcVideoBtn');
        const lbl = ctx.mount.querySelector('#rcVideoBtnLbl');
        if (btn) { btn.disabled = true; }
        if (lbl) lbl.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Розбираю відео…';

        const claudeCb = ctx.mount.querySelector('#rcVisionClaude');
        const body = (claudeCb && claudeCb.checked) ? { vision_backend: 'claude' } : {};
        let result;
        try { result = await R.api.post('/api/transcription/' + id + '/analyze-video', body); }
        catch (err) {
            UI.toast((err && err.message) || 'Не вдалося запустити аналіз відео', 'error');
            if (btn) { btn.disabled = false; updateVideoBtnLabel(ctx); }
            return;
        }
        if (!result || !result.success) {
            const reason = (result && result.reason) || 'no video';
            UI.toast(reason === 'no video' ? 'Відеозапис відсутній' : ('Помилка: ' + reason), 'error');
            if (btn) { btn.disabled = false; updateVideoBtnLabel(ctx); }
            return;
        }

        // Poll until video_analysis_at is set (max ~3 min, every 2s)
        const MAX_POLLS = 90;
        let polls = 0;
        if (videoPolling) clearInterval(videoPolling);
        videoPolling = setInterval(async () => {
            polls++;
            if (polls > MAX_POLLS) {
                clearInterval(videoPolling); videoPolling = null;
                UI.toast('Час очікування аналізу вичерпано', 'error');
                if (btn) { btn.disabled = false; updateVideoBtnLabel(ctx); }
                return;
            }
            let info;
            try { info = await R.api.get('/api/transcription/' + id + '/recording-info'); }
            catch (_) { return; }
            if (!ctx.isCurrent()) { clearInterval(videoPolling); videoPolling = null; return; }
            if (info && info.video_analysis_at) {
                clearInterval(videoPolling); videoPolling = null;
                videoInfo = info;
                if (btn) { btn.disabled = false; }
                updateVideoBtnLabel(ctx);
                UI.toast('Відео розібрано — ' + (info.keyframes_count || 0) + ' кадрів', 'success');
                // Ensure video element is shown, then load keyframes
                renderVideoElement(ctx, id, info);
                loadKeyframes(ctx, id);
            } else {
                // Update progress label with known keyframe count if polling info has it
                const kn = parseInt((info && info.keyframes_count) || 0, 10) || 0;
                if (lbl) lbl.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Розбираю відео…${kn ? ' (' + kn + ' кадрів)' : ''}`;
            }
        }, 2000);
    }

    // ---- provenance grid (branches per source_type) ----
    function cell(k, v) { return v == null || v === '' ? '' : `<div class="rc-prov__cell"><div class="rc-prov__k">${U.esc(k)}</div><div class="rc-prov__v">${v}</div></div>`; }
    function ext(url, label) { return `<a href="${U.esc(url)}" target="_blank" rel="noopener" data-native>${U.esc(label)} <i class="fa-solid fa-arrow-up-right-from-square" style="font-size:.8em"></i></a>`; }

    function provenanceHTML(t) {
        const c = [];
        // universal
        c.push(cell('Тип', U.SRC_LABEL[t.source_type] || t.source_type));
        c.push(cell('Дата', U.fmtDate(t.created_at)));
        c.push(cell('Мова', U.langLabel(t.language)));
        if (t.model_used) c.push(cell('Модель', `<span class="rc-mono">${U.esc((t.model_used||'').toUpperCase())}</span>`));
        if (t.processing_time) c.push(cell('Обробка', `${Number(t.processing_time).toFixed(1)} с`));
        // Напрямок — editable (select + ✨ k-NN suggest); filled by bindCategoryEditor.
        c.push(`<div class="rc-prov__cell"><div class="rc-prov__k">Напрямок</div><div class="rc-prov__v" id="rcCatEdit"></div></div>`);

        // per-source provenance
        if (t.source_type === 'youtube') {
            c.push(cell('Канал', U.esc(t.youtube_author)));
            if (t.youtube_duration) c.push(cell('Тривалість', U.fmtDuration(t.youtube_duration)));
            if (t.source_url) c.push(cell('Джерело', ext(t.source_url, 'Відкрити на YouTube')));
            if (t.youtube_id) c.push(cell('Video ID', `<span class="rc-mono">${U.esc(t.youtube_id)}</span>`));
        } else if (t.source_type === 'telegram') {
            c.push(cell('Чат', U.esc(t.tg_chat_title)));
            c.push(cell('Від', U.esc(t.tg_sender)));
            if (t.tg_date) c.push(cell('Надіслано', U.fmtDate(t.tg_date)));
            if (t.tg_link) c.push(cell('Джерело', ext(t.tg_link, 'Відкрити в Telegram')));
        } else if (t.source_type === 'document') {
            if (t.doc_type) c.push(cell('Формат', `<span class="rc-mono">${U.esc((t.doc_type||'').toUpperCase())}</span>`));
            c.push(cell('Файл', U.esc(t.original_filename || t.source_name)));
            if (t.page_count) c.push(cell('Сторінок', t.page_count));
            if (t.byte_size) c.push(cell('Розмір', U.fmtBytes(t.byte_size)));
        } else if (t.source_type === 'file') {
            c.push(cell('Файл', U.esc(t.original_filename || t.source_name)));
        } else if (t.source_type === 'recording') {
            const sp = (t.speakers || []).filter(s => s.name).map(s => s.name);
            if (sp.length) c.push(cell('Спікери', U.esc(sp.join(', '))));
        }
        return `<div class="rc-prov">${c.join('')}</div>`;
    }

    // ---- transcript body ----
    function renderBody(ctx, t) {
        const box = ctx.mount.querySelector('#rcTranscriptBody');
        if (!box) return;
        if (mode === 'polished' && t.polished_text) {
            box.innerHTML = `<div class="rc-transcript">${U.esc(t.polished_text)}</div>`;
        } else if (mode === 'segments') {
            const segs = t.segments || [];
            if (!segs.length) { box.innerHTML = UI.empty('Сегментів немає', '', 'fa-clock'); return; }
            const spMap = {}; (t.speakers || []).forEach(s => { if (s.name) spMap[s.raw_label] = s.name; });
            box.innerHTML = `<div class="rc-seghint rc-mono"><i class="fa-solid fa-circle-play"></i> Клік по репліці — відтворити аудіо з цього місця (упізнати голос). Прапорець — закладка.</div>
            <div class="rc-segments">${segs.map((s, i) => {
                const sp = s.speaker ? (spMap[s.speaker] || labelOf(s.speaker)) : '';
                const bm = bookmarks[i];
                const end = s.end != null ? s.end : (segs[i + 1] ? segs[i + 1].start : (s.start || 0) + 5);
                return `<div class="rc-seg${bm ? ' is-bookmarked' : ''}" data-seg="${i}" data-start="${s.start || 0}" data-end="${end}">
                    <div class="rc-seg__t">${U.fmtDuration(s.start)}
                        <button class="rc-seg__bm" data-bm="${i}" title="${bm ? 'Прибрати закладку' : 'Додати закладку'}"><i class="${bm ? 'fa-solid' : 'fa-regular'} fa-bookmark"></i></button>
                    </div>
                    <div class="rc-seg__txt">${sp ? `<span class="rc-seg__sp">${U.esc(sp)}</span>` : ''}${U.esc(s.text || '')}
                        ${bm && bm.note ? `<div class="rc-seg__note"><i class="fa-solid fa-note-sticky"></i> ${U.esc(bm.note)}</div>` : ''}
                    </div>
                </div>`;
            }).join('')}</div>`;
            setupSegmentSync(ctx, t, segs);
        } else {
            box.innerHTML = `<div class="rc-transcript">${U.esc(t.transcript_text || 'Текст відсутній')}</div>`;
        }
    }
    function labelOf(raw) {
        if (raw === 'self') return 'Ви';
        const m = /^SPEAKER_(\d+)$/.exec(raw);
        return m ? 'Спікер ' + (parseInt(m[1], 10) + 1) : raw;
    }

    // ---- per-record speaker editor (name new speakers / fix misidentified) ----
    // Backend: PATCH /api/transcriptions/<id>/speakers {mapping:{raw_label:name|null}}
    // — find-or-creates a global speaker by name and relinks this record's labels.
    async function openSpeakers(ctx, id, t) {
        const panel = ctx.mount.querySelector('#rcSpeakersPanel');
        if (!panel) return;
        const btn = ctx.mount.querySelector('#rcSpeakersBtn');
        if (speakersOpen) { speakersOpen = false; panel.innerHTML = ''; if (btn) btn.classList.remove('is-active'); return; }
        speakersOpen = true; if (btn) btn.classList.add('is-active');

        // distinct raw labels in first-appearance order + a sample line for each
        const segs = t.segments || [];
        const order = [], seen = new Set(), sampleOf = {};
        for (const s of segs) {
            const r = s.speaker; if (!r) continue;
            if (!seen.has(r)) { seen.add(r); order.push(r); }
            if (!sampleOf[r] && s.text && s.text.trim()) sampleOf[r] = s.text.trim();
        }
        (t.speakers || []).forEach(s => { if (s.raw_label && !seen.has(s.raw_label)) { seen.add(s.raw_label); order.push(s.raw_label); } });
        if (!order.length) {
            panel.innerHTML = `<div class="rc-spkedit"><p class="rc-mono" style="color:var(--rc-ink-3)">У цьому записі немає розрізнення спікерів (діаризація не виконувалась).</p></div>`;
            return;
        }
        const nameOf = {}; (t.speakers || []).forEach(s => { if (s.name) nameOf[s.raw_label] = s.name; });

        // existing speaker names → datalist (autocomplete + consistency)
        let known = [];
        try { const d = await R.api.speakers(); known = (d.speakers || []).map(s => s.name); } catch (_) {}
        if (!ctx.isCurrent()) return;

        panel.innerHTML = `<div class="rc-spkedit">
            <div class="rc-spkedit__head"><i class="fa-solid fa-user-pen"></i> Імена спікерів у цьому записі</div>
            <p class="rc-spkedit__hint">Назвіть нерозпізнаних, виправте помилкові авто-збіги. Підказка під кожним — перша репліка спікера. Імена зберігаються глобально й підставляться наступного разу.</p>
            <datalist id="rcSpkKnown">${known.map(n => `<option value="${U.esc(n)}"></option>`).join('')}</datalist>
            <div class="rc-spkedit__rows">${order.map(raw => {
                const sample = sampleOf[raw] ? (sampleOf[raw].slice(0, 110) + (sampleOf[raw].length > 110 ? '…' : '')) : '';
                return `<div class="rc-spkedit__row" data-raw="${U.esc(raw)}">
                    <div class="rc-spkedit__lbl">
                        <span class="rc-spkedit__auto">${U.esc(labelOf(raw))}</span>
                        ${sample ? `<span class="rc-spkedit__sample">«${U.esc(sample)}»</span>` : ''}
                    </div>
                    <input class="rc-input rc-input--sm rc-spkedit__in" list="rcSpkKnown" autocomplete="off"
                           value="${U.esc(nameOf[raw] || '')}" placeholder="Імʼя спікера…">
                </div>`;
            }).join('')}</div>
            <div class="rc-spkedit__actions">
                <button class="rc-btn rc-btn--primary rc-btn--sm" id="rcSpkSave"><i class="fa-solid fa-check"></i> Зберегти імена</button>
                <button class="rc-btn rc-btn--sm" id="rcSpkClose">Закрити</button>
            </div>
        </div>`;
        panel.querySelector('#rcSpkSave').addEventListener('click', () => saveSpeakers(ctx, id, t, panel));
        panel.querySelector('#rcSpkClose').addEventListener('click', () => openSpeakers(ctx, id, t));
        const first = panel.querySelector('.rc-spkedit__in'); if (first) first.focus();
    }

    async function saveSpeakers(ctx, id, t, panel) {
        const mapping = {};
        panel.querySelectorAll('.rc-spkedit__row').forEach(row => {
            mapping[row.dataset.raw] = (row.querySelector('.rc-spkedit__in').value || '').trim() || null;
        });
        const btn = panel.querySelector('#rcSpkSave');
        btn.disabled = true; const old = btn.innerHTML; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Зберігаю…';
        try {
            const r = await R.api.patch('/api/transcriptions/' + id + '/speakers', { mapping });
            data.speakers = r.speakers || data.speakers;
            UI.toast('Імена спікерів збережено', 'success');
            speakersOpen = false;
            paint(ctx, id, data);   // re-render header + transcript with new names
        } catch (err) {
            UI.toast((err && err.message) || 'Не вдалося зберегти', 'error');
            btn.disabled = false; btn.innerHTML = old;
        }
    }

    // ---- segment ↔ audio sync: click a line to seek+play, live-highlight active ----
    function setupSegmentSync(ctx, t, segs) {
        const audio = ctx.mount.querySelector('#rcAudio');
        const box = ctx.mount.querySelector('#rcTranscriptBody');
        if (!box) return;
        const segEls = [...box.querySelectorAll('.rc-seg')];

        segEls.forEach(el => el.addEventListener('click', (e) => {
            if (e.target.closest('[data-bm]')) return;             // bookmark star — handled below
            const start = parseFloat(el.dataset.start) || 0;
            if (audio && audio.src) { try { audio.currentTime = start; audio.play().catch(() => {}); } catch (_) {} }
            syncVideoNow(ctx, start);
        }));
        box.querySelectorAll('[data-bm]').forEach(b => b.addEventListener('click', (e) => {
            e.stopPropagation(); toggleBookmark(ctx, t, parseInt(b.dataset.bm, 10));
        }));

        if (!audio) return;
        if (_onTime) { audio.removeEventListener('timeupdate', _onTime); _onTime = null; }
        let last = -1;
        _onTime = function () {
            const cur = audio.currentTime;
            let idx = -1;
            for (let i = 0; i < segEls.length; i++) {
                const st = parseFloat(segEls[i].dataset.start) || 0;
                const en = parseFloat(segEls[i].dataset.end) || (st + 5);
                if (cur >= st && cur < en) { idx = i; break; }
            }
            if (idx === last) return;
            if (last >= 0 && segEls[last]) segEls[last].classList.remove('is-playing');
            last = idx;
            if (idx >= 0 && segEls[idx]) {
                segEls[idx].classList.add('is-playing');
                const r = segEls[idx].getBoundingClientRect();
                if (r.bottom < 64 || r.top > window.innerHeight - 64) segEls[idx].scrollIntoView({ block: 'center', behavior: 'smooth' });
            }
        };
        audio.addEventListener('timeupdate', _onTime);
    }

    // ---- segment bookmarks ----
    function updateBmCount(ctx) {
        const el = ctx.mount.querySelector('#rcBmCount');
        const n = Object.keys(bookmarks).length;
        if (el) el.textContent = n ? ' ' + n : '';
    }
    // ---- коментарі власника ------------------------------------------------
    // Панель постійна (не розгортається під рядком, як у списках): на сторінці
    // запису коментар — повноцінна частина документа, а не приписка збоку.
    async function updateCmCount(ctx, id) {
        const el = ctx.mount.querySelector('#rcCmCount');
        if (!el || !R.comments) return;
        try {
            const d = await R.api.commentCounts('transcription', [id]);
            const c = (d.counts || {})[id] || (d.counts || {})[String(id)] || {};
            el.textContent = c.n ? ' ' + c.n : '';
        } catch (_) { /* лічильник — прикраса; сторінка працює без нього */ }
    }

    async function toggleComments(ctx, id) {
        const panel = ctx.mount.querySelector('#rcCommentsPanel');
        if (!panel || !R.comments) return;
        if (!panel.hidden) { panel.hidden = true; panel.innerHTML = ''; return; }
        panel.hidden = false;
        panel.className = 'rc-cmpanel';
        panel.innerHTML = '<div class="rc-cmpanel__loading rc-mono">Завантаження…</div>';
        const audio = ctx.mount.querySelector('#rcAudio');
        // Якір ставимо лише коли плеєр реально зрушив з нуля: інакше кожен
        // коментар отримав би фальшивий «~00:00», що гірше за жоден якір.
        const at = audio && audio.currentTime > 0.5 ? Math.floor(audio.currentTime) : null;
        await R.comments.mount(panel, 'transcription', id, {
            anchorTime: at,
            onChange: () => updateCmCount(ctx, id),
        });
    }

    async function toggleBookmark(ctx, t, idx) {
        if (bookmarks[idx]) {
            try { await R.api.bookmarkDelete(bookmarks[idx].id); delete bookmarks[idx]; UI.toast('Закладку прибрано', 'info'); afterBookmarkChange(ctx, t); }
            catch (e) { UI.toast(e.message, 'error'); }
        } else {
            const note = (await UI.promptModal({ title: 'Закладка', label: 'Нотатка до закладки (необовʼязково)', maxlength: 300 }) || '').trim();
            try { const r = await R.api.bookmarkCreate(data.id, idx, note || undefined); bookmarks[idx] = { id: r.id, note: note || '' }; UI.toast('Закладку додано', 'success'); afterBookmarkChange(ctx, t); }
            catch (e) { UI.toast(e.message, 'error'); }
        }
    }
    function afterBookmarkChange(ctx, t) {
        updateBmCount(ctx);
        if (mode === 'segments') renderBody(ctx, t);
        if (bookmarksOpen) renderBookmarksPanel(ctx, t);
    }
    function openBookmarks(ctx, t) {
        const panel = ctx.mount.querySelector('#rcBookmarksPanel');
        const btn = ctx.mount.querySelector('#rcBookmarksBtn');
        if (!panel) return;
        if (bookmarksOpen) { bookmarksOpen = false; panel.innerHTML = ''; if (btn) btn.classList.remove('is-active'); return; }
        bookmarksOpen = true; if (btn) btn.classList.add('is-active');
        renderBookmarksPanel(ctx, t);
    }
    function renderBookmarksPanel(ctx, t) {
        const panel = ctx.mount.querySelector('#rcBookmarksPanel');
        if (!panel || !bookmarksOpen) return;
        const segs = t.segments || [];
        const items = Object.keys(bookmarks).map(Number).sort((a, b) => a - b);
        if (!items.length) {
            panel.innerHTML = `<div class="rc-bmpanel"><div class="rc-bmpanel__h"><i class="fa-solid fa-bookmark"></i> Закладки</div><p class="rc-mono" style="color:var(--rc-ink-3)">Ще немає. У вкладці «Таймкоди» натисніть прапорець біля моменту.</p></div>`;
            return;
        }
        panel.innerHTML = `<div class="rc-bmpanel"><div class="rc-bmpanel__h"><i class="fa-solid fa-bookmark"></i> Закладки <span class="rc-mono">${items.length}</span></div>
            ${items.map(i => {
                const s = segs[i] || {}, bm = bookmarks[i];
                return `<div class="rc-bm" data-jump="${i}">
                    <span class="rc-bm__t rc-mono">${U.fmtDuration(s.start || 0)}</span>
                    <span class="rc-bm__note">${bm.note ? U.esc(bm.note) : '<span class="rc-bm__empty">без нотатки</span>'}</span>
                    <button class="rc-iconbtn" data-edit="${i}" title="Нотатка"><i class="fa-solid fa-pen"></i></button>
                    <button class="rc-iconbtn" data-del="${i}" title="Прибрати"><i class="fa-solid fa-trash"></i></button>
                </div>`;
            }).join('')}</div>`;
        panel.querySelectorAll('[data-jump]').forEach(el => el.addEventListener('click', (e) => {
            if (e.target.closest('[data-edit],[data-del]')) return;
            jumpToSegment(ctx, t, Number(el.dataset.jump));
        }));
        panel.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => editBookmarkNote(ctx, t, Number(b.dataset.edit))));
        panel.querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', () => toggleBookmark(ctx, t, Number(b.dataset.del))));
    }
    function jumpToSegment(ctx, t, idx) {
        if (mode !== 'segments') {
            mode = 'segments';
            ctx.mount.querySelectorAll('#rcViewToggle button').forEach(x => x.classList.toggle('is-active', x.dataset.mode === 'segments'));
            R.router.replace(location.pathname + '?view=segments');
            renderBody(ctx, t);
        }
        const seg = (t.segments || [])[idx];
        const audio = ctx.mount.querySelector('#rcAudio');
        if (audio && audio.src && seg) { try { audio.currentTime = seg.start || 0; audio.play().catch(() => {}); } catch (_) {} }
        const el = ctx.mount.querySelector(`.rc-seg[data-seg="${idx}"]`);
        if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.add('is-flash'); setTimeout(() => el.classList.remove('is-flash'), 1200); }
    }
    async function editBookmarkNote(ctx, t, idx) {
        const bm = bookmarks[idx]; if (!bm) return;
        const raw = await UI.promptModal({ title: 'Нотатка до закладки', label: 'Нотатка', defaultValue: bm.note || '', maxlength: 300 });
        if (raw == null) return;   // скасовано (Escape/backdrop/×) — на відміну від window.prompt, тепер розрізняємо це від "зберегти порожньою"
        const note = raw.trim();
        if (note === (bm.note || '')) return;
        R.api.bookmarkUpdate(bm.id, note).then(() => { bm.note = note; afterBookmarkChange(ctx, t); UI.toast('Нотатку збережено', 'success'); }).catch(e => UI.toast(e.message, 'error'));
    }

    // ---- editable напрямок + ✨ k-NN suggest ----
    function bindCategoryEditor(ctx, id, t) {
        const box = ctx.mount.querySelector('#rcCatEdit');
        if (!box) return;
        const opts = UI.catOptions(t.category_id, { first: 'none' });
        box.innerHTML = `<select class="rc-select rc-select--sm rc-catsel" id="rcCatSel" data-cat-first="none">${opts}</select>
            <button class="rc-iconbtn" id="rcCatSuggest" title="Підказати напрямок за схожими записами (✨)"><i class="fa-solid fa-wand-magic-sparkles"></i></button>`;
        box.querySelector('#rcCatSel').addEventListener('change', (e) => applyCategory(ctx, id, t, e.target.value ? Number(e.target.value) : null));
        box.querySelector('#rcCatSuggest').addEventListener('click', () => suggestCat(ctx, id, t));
    }
    async function applyCategory(ctx, id, t, cid) {
        try { await R.api.setCategory(id, cid); t.category_id = cid; data.category_id = cid; UI.toast(cid ? 'Напрямок змінено' : 'Напрямок знято', 'success'); }
        catch (e) { UI.toast(e.message || 'Не вдалося', 'error'); }
    }
    const _CAT_REASON = {
        insufficient_labels: 'Замало розмічених напрямків для підказки — розмітьте кілька записів вручну.',
        not_embedded: 'Запис ще не проіндексовано (embeddings) — підказка недоступна.',
        embeddings_unavailable: 'Локальні embeddings недоступні.',
        no_labeled_data: 'Немає розмічених записів для порівняння.',
        dim_mismatch: 'Несумісні embeddings — підказка недоступна.',
    };
    async function suggestCat(ctx, id, t) {
        const btn = ctx.mount.querySelector('#rcCatSuggest');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }
        try {
            const r = await R.api.suggestCategory(id);
            if (!r.suggestion) { UI.toast(_CAT_REASON[r.reason] || 'Підказати не вдалося.', 'info'); return; }
            const sel = ctx.mount.querySelector('#rcCatSel');
            if (sel) sel.value = String(r.suggestion.category_id);
            await applyCategory(ctx, id, t, r.suggestion.category_id);
            UI.toast(`За схожими записами: «${r.suggestion.name}» (${Math.round((r.suggestion.confidence || 0) * 100)}%)`, 'success');
        } catch (e) { UI.toast(e.message || 'Помилка підказки', 'error'); }
        finally { if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i>'; } }
    }

    // ---- related / connections ----
    async function renderRelated(ctx, id, t) {
        const rel = ctx.mount.querySelector('#rcRel');
        const blocks = [];
        const ents = t.entities || [];
        const tasks = t.action_items || [];

        if (ents.length) {
            blocks.push(`<div class="rc-rel__block">
                <div class="rc-rel__h">Сутності <span class="rc-mono">${ents.length}</span></div>
                <div class="rc-tags">${ents.map(e =>
                    `<a class="rc-entity" href="/entities/${e.id}"><span class="rc-entity__type">${U.esc(e.type||'')}</span>${U.esc(e.canonical_name)}</a>`
                ).join('')}</div></div>`);
        }
        if (tasks.length) {
            blocks.push(`<div class="rc-rel__block">
                <div class="rc-rel__h">Задачі <span class="rc-mono">${tasks.length}</span></div>
                <div>${tasks.map(a =>
                    `<div class="rc-task"><span class="rc-task__status" data-s="${U.esc(a.status)}">${U.esc(U.taskStatus(a.status))}</span>
                    <div class="rc-task__body">${U.esc(a.task)}${a.owner_name ? ` <span class="rc-task__owner">· ${U.esc(a.owner_name)}</span>` : ''}</div></div>`
                ).join('')}</div></div>`);
        }
        rel.innerHTML = blocks.join('');

        // same-category (separate fetch, appended)
        if (t.category_id) {
            try {
                const d = await R.api.history({ category_id: t.category_id, per_page: 6 });
                if (!ctx.isCurrent()) return;
                const others = (d.transcriptions || []).filter(x => x.id !== id).slice(0, 5);
                if (others.length) {
                    const cat = (cats.map[t.category_id] || {}).name || 'напрямку';
                    const block = U.el('div', { class: 'rc-rel__block' });
                    block.innerHTML = `<div class="rc-rel__h">Той самий напрямок <span class="rc-mono">${U.esc(cat)}</span></div>
                        <div class="rc-tags">${others.map(o =>
                            `<a class="rc-entity" href="/transcript/${U.slug(o.id, o.source_name)}">${U.esc(o.source_name || ('#'+o.id))}</a>`
                        ).join('')}</div>`;
                    rel.appendChild(block);
                }
            } catch (_) {}
        }
    }

    // ---- Claude actions (existing endpoints) ----
    async function doPolish(ctx, id) {
        const btn = ctx.mount.querySelector('#rcPolish');
        btn.disabled = true; const old = btn.innerHTML; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Покращую…';
        try {
            const r = await R.api.post('/api/transcription/' + id + '/polish', {});
            data.polished_text = r.polished_text;
            UI.toast(r.cached ? 'Готовий поліпшений текст' : 'Поліпшено ✦', 'success');
            paint(ctx, id, data); mode = 'polished'; renderBody(ctx, data);
            ctx.mount.querySelectorAll('#rcViewToggle button').forEach(x => x.classList.toggle('is-active', x.dataset.mode === 'polished'));
        } catch (err) { UI.toast(err.message, 'error'); btn.disabled = false; btn.innerHTML = old; }
    }
    async function doSummary(ctx, id) {
        const btn = ctx.mount.querySelector('#rcSummary');
        btn.disabled = true; const old = btn.innerHTML; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Резюмую…';
        try {
            const r = await R.api.post('/api/transcription/' + id + '/summarize', {});
            renderSummary(ctx, r); UI.toast(r.cached ? 'Резюме готове' : 'Резюме згенеровано', 'success');
        } catch (err) { UI.toast(err.message, 'error'); }
        finally { btn.disabled = false; btn.innerHTML = old; }
    }
    function renderSummary(ctx, r) {
        const panel = ctx.mount.querySelector('#rcSummaryPanel'); if (!panel) return;
        const kp = (r.key_points || []).map(p => `<li>${U.esc(p)}</li>`).join('');
        const ai = (r.action_items || []).map(a => `<li>${U.esc(typeof a === 'string' ? a : (a.task || ''))}</li>`).join('');
        panel.innerHTML = `<div class="rc-ask__answer" style="margin-bottom:24px">
            ${r.summary ? `<p>${U.esc(r.summary)}</p>` : ''}
            ${kp ? `<div class="rc-eyebrow" style="margin-top:12px">Ключові тези</div><ul>${kp}</ul>` : ''}
            ${ai ? `<div class="rc-eyebrow" style="margin-top:12px">Задачі</div><ul>${ai}</ul>` : ''}
        </div>`;
    }

    // ---- post-processing (translate / topics / wordcloud / sentiment) ----
    function pp(ctx, id, t, kind) {
        const panel = ctx.mount.querySelector('#rcPostproc');
        if (!panel) return;
        ctx.mount.querySelectorAll('[data-pp]').forEach(b => b.classList.toggle('is-active', b.dataset.pp === kind && ppOpen !== kind));
        if (ppOpen === kind) { ppOpen = null; panel.innerHTML = ''; return; }
        ppOpen = kind;
        if (kind === 'topics') doTopics(ctx, id, t, panel);
        else if (kind === 'translate') doTranslate(ctx, id, t, panel);
        else if (kind === 'wordcloud') doWordcloud(ctx, t, panel);
        else if (kind === 'sentiment') doSentiment(ctx, id, panel);
    }
    const ppHead = (title, ico) => `<div class="rc-pp__head"><i class="fa-solid ${ico}"></i> ${U.esc(title)}</div>`;
    const ppSpin = () => `<span class="rc-mono" style="color:var(--rc-ink-3)"><i class="fa-solid fa-spinner fa-spin"></i> Обробка…</span>`;
    const topicLabel = (tp) => typeof tp === 'string' ? tp : (tp.topic || tp.label || tp.name || '');

    async function doTopics(ctx, id, t, panel) {
        panel.innerHTML = `<div class="rc-pp">${ppHead('Теми', 'fa-tags')}<div class="rc-pp__body" id="rcPPb">${ppSpin()}</div></div>`;
        const body = panel.querySelector('#rcPPb');
        let topics = null;
        if (t.topics_json) { try { topics = JSON.parse(t.topics_json).topics; } catch (_) {} }
        try {
            if (!topics) { const r = await R.api.post('/api/transcription/' + id + '/topics', {}); topics = r.topics || []; t.topics_json = JSON.stringify({ topics }); }
            if (!ctx.isCurrent()) return;
            const labels = topics.map(topicLabel).filter(Boolean);
            if (!labels.length) { body.innerHTML = `<p class="rc-mono" style="color:var(--rc-ink-3)">Тем не знайдено.</p>`; return; }
            body.innerHTML = `<div class="rc-tags">${labels.map(l =>
                `<a class="rc-entity" href="/library?search=${encodeURIComponent(l)}">${U.esc(l)}</a>`).join('')}</div>`;
        } catch (err) { if (ctx.isCurrent()) body.innerHTML = UI.error(err && err.message); }
    }

    function doTranslate(ctx, id, t, panel) {
        const ALL = [{ v: 'uk', l: 'Українською' }, { v: 'en', l: 'English' }, { v: 'ru', l: 'Русською' }];
        const src = (t.language || '').toLowerCase();
        const opts = ALL.filter(l => l.v !== src);
        let cached = {};
        if (t.translations_json) { try { cached = JSON.parse(t.translations_json) || {}; } catch (_) {} }
        panel.innerHTML = `<div class="rc-pp">${ppHead('Переклад', 'fa-language')}<div class="rc-pp__body">
            <div class="rc-pp__row">${opts.map(l => `<button class="rc-btn rc-btn--sm" data-lang="${l.v}">${U.esc(l.l)}${cached[l.v] ? ' ✓' : ''}</button>`).join('')}</div>
            <div id="rcTrOut"></div></div></div>`;
        const out = panel.querySelector('#rcTrOut');
        const show = (txt) => { out.innerHTML = `<div class="rc-transcript" style="margin-top:16px">${U.esc(txt)}</div>`; };
        panel.querySelectorAll('[data-lang]').forEach(b => b.addEventListener('click', async () => {
            const lang = b.dataset.lang;
            if (cached[lang] && cached[lang].text) { show(cached[lang].text); return; }
            b.disabled = true; const old = b.innerHTML; b.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i>`;
            try {
                const r = await R.api.post('/api/transcription/' + id + '/translate', { target_lang: lang });
                cached[lang] = { text: r.translated_text }; t.translations_json = JSON.stringify(cached);
                show(r.translated_text); b.innerHTML = old + ' ✓';
            } catch (err) { UI.toast(err && err.message, 'error'); b.innerHTML = old; }
            finally { b.disabled = false; }
        }));
    }

    function countWords(text) {
        const map = new Map();
        const tokens = (text || '').toLowerCase().match(/[\p{L}'’-]+/gu) || [];
        for (const tk of tokens) {
            const w = tk.replace(/^[-'’]+|[-'’]+$/g, '');
            if (w.length < 3 || STOPWORDS.has(w) || /^\d+$/.test(w)) continue;
            map.set(w, (map.get(w) || 0) + 1);
        }
        return [...map.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30);
    }
    function doWordcloud(ctx, t, panel) {
        const text = (mode === 'polished' && t.polished_text) ? t.polished_text : (t.transcript_text || '');
        const counts = countWords(text);
        if (!counts.length) { panel.innerHTML = `<div class="rc-pp">${ppHead('Хмара слів', 'fa-cloud')}<div class="rc-pp__body"><p class="rc-mono" style="color:var(--rc-ink-3)">Замало слів для хмари.</p></div></div>`; return; }
        const max = Math.log(counts[0][1]), min = Math.log(counts[counts.length - 1][1]);
        const size = (c) => max === min ? 22 : Math.round(14 + (Math.log(c) - min) / (max - min) * 28);
        panel.innerHTML = `<div class="rc-pp">${ppHead('Хмара слів', 'fa-cloud')}<div class="rc-pp__body"><div class="rc-cloud">${
            counts.map(([w, c]) => `<span class="rc-cloud__w" data-w="${U.esc(w)}" style="font-size:${size(c)}px" title="${c}×">${U.esc(w)}</span>`).join('')
        }</div></div></div>`;
        panel.querySelectorAll('.rc-cloud__w').forEach(el =>
            el.addEventListener('click', () => R.router.navigate('/library?search=' + encodeURIComponent(el.dataset.w))));
    }

    async function doSentiment(ctx, id, panel) {
        panel.innerHTML = `<div class="rc-pp">${ppHead('Емоції за спікерами', 'fa-face-smile')}<div class="rc-pp__body" id="rcPPb">${ppSpin()}</div></div>`;
        const body = panel.querySelector('#rcPPb');
        try {
            const r = await R.api.post('/api/transcription/' + id + '/sentiment', {});
            if (!ctx.isCurrent()) return;
            const sp = r.speakers || {};
            const names = Object.keys(sp);
            if (!names.length) { body.innerHTML = `<p class="rc-mono" style="color:var(--rc-ink-3)">Немає даних.</p>`; return; }
            body.innerHTML = `<div class="rc-senti">${names.map(n => sentiCard(n, sp[n] || {})).join('')}</div>`;
        } catch (err) { if (ctx.isCurrent()) body.innerHTML = UI.error(err && err.message); }
    }
    function sentiCard(name, d) {
        const score = Math.max(-1, Math.min(1, Number(d.score) || 0));
        const pct = Math.round((score + 1) / 2 * 100);
        const color = score > 0.15 ? 'var(--rc-ok)' : score < -0.15 ? 'var(--rc-err)' : 'var(--rc-ink-3)';
        return `<div class="rc-senti__card">
            <div class="rc-senti__head"><span class="rc-senti__name">${U.esc(name)}</span><span class="rc-mono" style="color:var(--rc-ink-3)">${U.esc(d.tone || '')} · ${score.toFixed(2)}</span></div>
            <div class="rc-senti__bar"><div class="rc-senti__fill" style="width:${pct}%;background:${color}"></div></div>
            ${d.summary ? `<p class="rc-senti__sum">${U.esc(d.summary)}</p>` : ''}
        </div>`;
    }

    async function doExport(fmt, t) {
        try {
            const res = await fetch(R.api.exportUrl(fmt), {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(t),
            });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const blob = await res.blob();
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = U.slug(t.id, t.source_name) + '.' + fmt;
            document.body.appendChild(a); a.click();
            URL.revokeObjectURL(a.href); a.remove();
            UI.toast('Експортовано ' + fmt.toUpperCase(), 'success');
        } catch (err) { UI.toast('Помилка експорту', 'error'); }
    }

    R.views.transcript = {
        render,
        destroy() {
            const a = document.getElementById('rcAudio');
            if (a) { try { if (_onTime) a.removeEventListener('timeupdate', _onTime); a.pause(); a.src = ''; } catch (_) {} }
            _onTime = null; bookmarks = {}; bookmarksOpen = false; speakersOpen = false;
            copilotTL = null; copilotOpen = false; cpFilter = 'all';
            if (videoPolling) { clearInterval(videoPolling); videoPolling = null; }
            videoInfo = null;
            data = null; mode = 'text';
        },
    };
})();
