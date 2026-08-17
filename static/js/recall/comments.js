/* Recall — шар коментарів (Волна 1, UI).

   Один компонент на всі поверхні: Бібліотека, Медіатека, сторінка транскрипту,
   далі — задачі й сутності. Тримати його спільним обовʼязково: коментар має
   виглядати й поводитись однаково скрізь, інакше «написати уточнення» стане
   різною дією залежно від екрана.

   Патерн — той самий, що вже є в Медіатеці (`.rc-aud__run`): клік по
   іконці-лічильнику розгортає панель ПІД карткою, не забираючи зі сторінки.
   Навігацію по картці це не ламає — панель зупиняє спливання кліку.

   Мова вводу не обмежується нічим: e5-large багатомовна, а `body` — звичайний
   TEXT. Ніяких спелчекерів і транслітерацій. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    // Довідник типів приходить із сервера (/api/comments/meta), а не дублюється
    // тут: ваги задають ранжування, і розʼїзд константи між фронтом і
    // ретрівалом означав би, що UI обіцяє пріоритет, якого пошук не дає.
    let _meta = null;
    async function meta() {
        if (_meta) return _meta;
        try { _meta = await R.api.commentMeta(); }
        catch (_) {
            _meta = { kinds: [{ key: 'note', weight: 0.6 }], default_kind: 'note',
                      max_body_chars: 20000 };
        }
        return _meta;
    }

    const KIND_LABEL = {
        correction: 'Виправлення',
        decision: 'Рішення',
        note: 'Нотатка',
        context: 'Контекст',
        question: 'Питання',
    };
    const KIND_HINT = {
        correction: 'Скасовує сказане в записі — найвища вага у пошуку й відповідях',
        decision: 'Підсумок або ухвалене рішення',
        note: 'Звичайна замітка',
        context: 'Передісторія, тло',
        question: 'Відкрите питання до себе',
    };

    // ---- бейдж на картці ---------------------------------------------------

    /* Три стани навмисно різні, бо означають різне:
       0        — приглушений, це запрошення, а не сигнал;
       >0       — акцент: на картці є що прочитати;
       є correction — окремий (теплий) акцент: у записі щось СПРОСТОВАНО,
                  і це найважливіше, що картка може про себе сказати. */
    function badgeHTML(count, hasCorrection) {
        const n = count || 0;
        const cls = 'rc-cmbtn' + (n ? ' is-has' : '') + (hasCorrection ? ' is-fix' : '');
        const title = n
            ? `${n} ${U.plural(n, ['коментар', 'коментарі', 'коментарів'])}${hasCorrection ? ' · є виправлення' : ''}`
            : 'Додати коментар';
        return `<button class="${cls}" data-act="comments" title="${U.esc(title)}" aria-label="${U.esc(title)}">
            <i class="fa-regular fa-comment-dots"></i>${n ? `<span class="rc-cmbtn__n">${n}</span>` : ''}
        </button>`;
    }

    /* Розставити лічильники по вже відрендереному списку одним запитом.
       rows: [{id, el}] — el це вузол, у який вставити бейдж (або сам рядок). */
    async function decorate(targetType, items, opts) {
        const o = opts || {};
        const ids = items.map(it => it.id).filter(v => v != null);
        if (!ids.length) return;
        let counts = {};
        try { counts = (await R.api.commentCounts(targetType, ids)).counts || {}; }
        catch (_) { return; }   // лічильники — прикраса; без них картки цілі
        items.forEach(it => {
            const host = o.slot ? it.el.querySelector(o.slot) : it.el;
            if (!host) return;
            // Сервер віддає {n, corrections} — третій стан бейджа («у записі
            // щось спростовано») інакше був би недосяжним.
            const c = counts[it.id] || counts[String(it.id)] || {};
            const n = c.n || 0;
            const old = host.querySelector('[data-act="comments"]');
            const html = badgeHTML(n, (c.corrections || 0) > 0);
            if (old) old.outerHTML = html;
            else host.insertAdjacentHTML(o.position || 'afterbegin', html);
            const btn = host.querySelector('[data-act="comments"]');
            if (btn) btn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggle(it.el, targetType, it.id, { onChange: o.onChange });
            });
        });
    }

    // ---- панель ------------------------------------------------------------

    function fmtWhen(s) {
        return U.fmtDate ? U.fmtDate(s) : (s || '');
    }

    function itemHTML(c) {
        const label = KIND_LABEL[c.kind] || c.kind;
        const cls = 'rc-cm rc-cm--' + U.esc(c.kind);
        return `<div class="${cls}" data-cid="${c.id}" data-pinned="${c.pinned ? '1' : '0'}" data-analyzed="${c.analyzed ? '1' : '0'}">
            <div class="rc-cm__head">
                <span class="rc-cm__kind">${U.esc(label)}</span>
                ${c.pinned ? '<span class="rc-cm__pin" title="Закріплено — підшивається до відповідей завжди"><i class="fa-solid fa-thumbtack"></i></span>' : ''}
                ${c.anchor_time != null ? `<span class="rc-cm__at rc-mono">${U.fmtDuration ? U.fmtDuration(c.anchor_time) : ''}</span>` : ''}
                <span class="rc-cm__when">${U.esc(fmtWhen(c.created_at))}</span>
                ${c.indexed ? '' : '<span class="rc-cm__pending" title="Ще не потрапив у векторний пошук — зʼявиться після індексації">в черзі</span>'}
                <span class="rc-cm__acts">
                    <button class="rc-iconbtn rc-iconbtn--xs" data-cm="analyze"
                            title="${c.analyzed ? 'Розібрати ще раз (платний виклик Claude)' : 'Витягти задачі й звʼязки (платний виклик Claude)'}"><i class="fa-solid fa-wand-magic-sparkles"></i></button>
                    <button class="rc-iconbtn rc-iconbtn--xs" data-cm="pin" title="${c.pinned ? 'Відкріпити' : 'Закріпити'}"><i class="fa-solid fa-thumbtack"></i></button>
                    <button class="rc-iconbtn rc-iconbtn--xs" data-cm="edit" title="Редагувати"><i class="fa-solid fa-pen"></i></button>
                    <button class="rc-iconbtn rc-iconbtn--xs" data-cm="del" title="Видалити"><i class="fa-solid fa-trash"></i></button>
                </span>
            </div>
            <div class="rc-cm__body">${U.esc(c.body).replace(/\n/g, '<br>')}</div>
            <div class="rc-cm__derived" data-derived="${c.id}" ${c.analyzed ? '' : 'hidden'}></div>
        </div>`;
    }

    /* Похідні коментаря — задачі, які з нього витягнув Claude. Довантажуємо
       окремо і лише для розібраних: список картки читається на кожен клік по
       бейджу, а це майже завжди порожньо. */
    async function loadDerived(el, cid) {
        const box = el.querySelector(`[data-derived="${cid}"]`);
        if (!box) return;
        let tasks = [];
        try { tasks = (await R.api.commentDerived(cid)).action_items || []; }
        catch (_) { return; }
        if (!tasks.length) {
            box.hidden = false;
            box.innerHTML = `<span class="rc-cm__none">розібрано · задач немає</span>`;
            return;
        }
        box.hidden = false;
        box.innerHTML = `<div class="rc-cm__tasks">` + tasks.map(t => `
            <div class="rc-cm__task">
                <i class="fa-regular fa-square-check"></i>
                <span>${U.esc(t.task)}</span>
                ${t.owner_name ? `<span class="rc-cm__owner">${U.esc(t.owner_name)}</span>` : ''}
                ${t.due_date ? `<span class="rc-cm__due rc-mono">${U.esc(t.due_date)}</span>`
                             : (t.due ? `<span class="rc-cm__due">${U.esc(t.due)}</span>` : '')}
            </div>`).join('') + `</div>`;
    }

    async function composerHTML(anchorTime) {
        const m = await meta();
        const opts = (m.kinds || []).map(k =>
            `<option value="${U.esc(k.key)}"${k.key === m.default_kind ? ' selected' : ''}
                     title="${U.esc(KIND_HINT[k.key] || '')}">${U.esc(KIND_LABEL[k.key] || k.key)}</option>`
        ).join('');
        return `<div class="rc-cmform">
            <textarea class="rc-cmform__ta" data-cm="body" rows="2" maxlength="${m.max_body_chars || 20000}"
                      placeholder="Уточнення, виправлення, акцент — будь-якою мовою…"></textarea>
            <div class="rc-cmform__row">
                <select class="rc-select rc-select--sm" data-cm="kind">${opts}</select>
                <label class="rc-cmform__pin"><input type="checkbox" data-cm="pinned"> Закріпити</label>
                ${anchorTime != null ? `<span class="rc-cmform__at rc-mono" title="Коментар прикріплений до моменту запису">${U.fmtDuration ? U.fmtDuration(anchorTime) : ''}</span>` : ''}
                <span class="rc-cmform__grow"></span>
                <span class="rc-cmform__hint rc-mono">Ctrl+Enter</span>
                <button class="rc-btn rc-btn--primary rc-btn--sm" data-cm="save">Додати</button>
            </div>
        </div>`;
    }

    /* Розгорнути/згорнути панель під переданим елементом. host — рядок картки;
       панель вставляється одразу після нього окремим вузлом, щоб не воювати з
       layout самої картки. */
    async function toggle(host, targetType, targetId, opts) {
        const o = opts || {};
        const existing = host.nextElementSibling;
        if (existing && existing.classList.contains('rc-cmpanel')
            && existing.dataset.for === `${targetType}:${targetId}`) {
            existing.remove();
            return null;
        }
        // Одна панель на список: інакше екран швидко перетворюється на
        // гармошку з десяти відкритих карток.
        const list = host.parentElement;
        if (list) list.querySelectorAll(':scope > .rc-cmpanel').forEach(p => p.remove());

        const panel = document.createElement('div');
        panel.className = 'rc-cmpanel';
        panel.dataset.for = `${targetType}:${targetId}`;
        panel.innerHTML = '<div class="rc-cmpanel__loading rc-mono">Завантаження…</div>';
        host.insertAdjacentElement('afterend', panel);
        await mount(panel, targetType, targetId, o);
        const ta = panel.querySelector('[data-cm="body"]');
        if (ta) ta.focus();
        return panel;
    }

    /* Відрендерити панель у ГОТОВИЙ контейнер — для сторінки транскрипту, де
       вона живе постійно, а не розгортається під рядком. */
    async function mount(panel, targetType, targetId, opts) {
        const o = opts || {};
        let items = [];
        try { items = (await R.api.comments(targetType, targetId)).comments || []; }
        catch (err) {
            panel.innerHTML = UI.error((err && err.message) || 'Не вдалося завантажити коментарі');
            return;
        }
        panel.innerHTML =
            (items.length
                ? `<div class="rc-cmlist">${items.map(itemHTML).join('')}</div>`
                : `<div class="rc-cmpanel__empty">Коментарів ще немає. Написане тут має вищу вагу в пошуку й відповідях, ніж сам транскрипт.</div>`)
            + (await composerHTML(o.anchorTime));
        bind(panel, targetType, targetId, o);
    }

    function bind(panel, targetType, targetId, o) {
        const reload = async () => {
            await mount(panel, targetType, targetId, o);
            if (o.onChange) o.onChange();
        };

        const save = panel.querySelector('[data-cm="save"]');
        const ta = panel.querySelector('[data-cm="body"]');
        const doSave = async () => {
            const body = (ta.value || '').trim();
            if (!body) { ta.focus(); return; }
            save.disabled = true;
            try {
                await R.api.commentCreate({
                    target_type: targetType, target_id: targetId, body,
                    kind: panel.querySelector('[data-cm="kind"]').value,
                    pinned: panel.querySelector('[data-cm="pinned"]').checked,
                    anchor_time: o.anchorTime != null ? o.anchorTime : undefined,
                    source: o.source || 'ui',
                });
                UI.toast('Коментар додано — він уже впливає на пошук', 'success');
                await reload();
            } catch (err) {
                UI.toast((err && err.message) || 'Не вдалося зберегти', 'error');
                save.disabled = false;
            }
        };
        if (save) save.addEventListener('click', doSave);
        if (ta) ta.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); doSave(); }
        });

        panel.querySelectorAll('.rc-cm').forEach(el => {
            const cid = Number(el.dataset.cid);
            const act = (n) => el.querySelector(`[data-cm="${n}"]`);
            act('pin').addEventListener('click', async () => {
                const pinned = el.dataset.pinned !== '1';
                try { await R.api.commentUpdate(cid, { pinned }); await reload(); }
                catch (err) { UI.toast((err && err.message) || 'Не вдалося', 'error'); }
            });
            act('edit').addEventListener('click', async () => {
                const cur = el.querySelector('.rc-cm__body');
                const text = await UI.promptModal({
                    title: 'Редагувати коментар',
                    label: 'Текст коментаря',
                    defaultValue: cur ? cur.innerText : '',
                    confirmLabel: 'Зберегти',
                    required: true,
                });
                if (!text) return;
                try { await R.api.commentUpdate(cid, { body: text }); await reload(); }
                catch (err) { UI.toast((err && err.message) || 'Не вдалося', 'error'); }
            });
            // Розбір через Claude — платна дія, тож підтвердження обовʼязкове,
            // а повторний розбір уже розібраного питаємо окремо: інакше
            // випадковий другий клік коштує грошей мовчки.
            act('analyze').addEventListener('click', async () => {
                const done = el.dataset.analyzed === '1';
                const ok = await UI.confirmModal({
                    title: done ? 'Розібрати ще раз' : 'Розібрати коментар',
                    message: done
                        ? 'Коментар уже розбирали. Повторний розбір — ще один платний виклик Claude; попередні задачі з цього коментаря буде замінено.'
                        : 'Claude витягне з коментаря задачі та згадані імена й проєкти. Це платний виклик до API.',
                    confirmLabel: 'Розібрати',
                });
                if (!ok) return;
                const btn = act('analyze');
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
                try {
                    const r = await R.api.commentAnalyze(cid, done ? { force: true } : {});
                    const n = (r.result && r.result.counts) || {};
                    UI.toast(n.action_items
                        ? `Витягнуто задач: ${n.action_items}`
                        : 'Задач у коментарі не знайшлось', 'success');
                    await reload();
                } catch (err) {
                    UI.toast((err && err.message) || 'Розбір не вдався', 'error');
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i>';
                }
            });
            if (el.dataset.analyzed === '1') loadDerived(el, cid);
            act('del').addEventListener('click', async () => {
                try {
                    await R.api.commentDelete(cid);
                    await reload();
                    // Soft-delete — тож пропонуємо повернути, як усюди в Recall.
                    UI.actionToast('Коментар видалено', 'Скасувати', async () => {
                        try { await R.api.commentRestore(cid); await reload(); }
                        catch (e) { UI.toast((e && e.message) || 'Не вдалося відновити', 'error'); }
                    });
                } catch (err) { UI.toast((err && err.message) || 'Не вдалося видалити', 'error'); }
            });
        });
    }

    R.comments = { badgeHTML, decorate, toggle, mount, meta, KIND_LABEL };
})();
