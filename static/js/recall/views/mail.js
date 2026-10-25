/* Recall — "Add to archive · Gmail" (gmail-ingest). Management panel modelled on the
   Telegram screen: add an account (address + app password), per-account label list with
   enable / direction / Claude controls, refresh labels, "load deeper".

   Reads: GET /api/mail/status. Writes: POST /api/mail/accounts, DELETE /api/mail/accounts/<id>,
   GET /api/mail/accounts/<id>/labels?refresh=1, PATCH /api/mail/labels/<id>, POST /api/mail/sync;
   behind the Windows dialog: POST /api/mail/labels/<id>/enrich, POST /api/mail/backfill.
   The app password lives only in the form field: never stored, logged or prefilled. */
(function () {
    'use strict';
    const R = window.Recall, U = R.util, UI = R.ui;

    // Literal map: server error code -> dictionary key (no dynamic key strings).
    const ERR_KEYS = {
        AUTH_FAILED: 'mail.err.authFailed',
        CONNECT_FAILED: 'mail.err.connectFailed',
        FOLDER_MISSING: 'mail.err.folderMissing',
        PROTOCOL: 'mail.err.protocol',
    };
    const errText = (code) => R.t(ERR_KEYS[code] || 'mail.err.unknown');

    let accounts = [];
    let showSystem = {};   // account id -> system folders expanded

    const fmtWhen = (iso) => R.i18n.date(iso, { dateStyle: 'medium', timeStyle: 'short' });

    async function render(ctx) {
        await UI.loadCategories();
        accounts = []; showSystem = {};
        ctx.mount.innerHTML = `
            <div class="rc-pagehead">
                <div class="rc-eyebrow"><i class="fa-solid fa-envelope"></i> ${R.t('mail.eyebrow')}</div>
                <div class="rc-pagehead__row">
                    <h1 class="rc-pagehead__title">${R.t('mail.title')}</h1>
                    <button class="rc-btn rc-btn--sm" id="rcMailReload"><i class="fa-solid fa-rotate"></i> ${R.t('mail.reload')}</button>
                </div>
                <p class="rc-pagehead__lede">${R.t('mail.lede')}</p>
            </div>
            <form class="rc-mailform" id="rcMailForm" autocomplete="off">
                <h2 class="rc-mailform__title">${R.t('mail.add.title')}</h2>
                <div class="rc-mailform__row">
                    <input class="rc-input" type="email" name="address" autocomplete="off" required
                        placeholder="${U.esc(R.t('mail.add.address'))}" aria-label="${U.esc(R.t('mail.add.address'))}">
                    <input class="rc-input" type="password" name="app_password" autocomplete="off" required
                        placeholder="${U.esc(R.t('mail.add.password'))}" aria-label="${U.esc(R.t('mail.add.password'))}">
                    <button class="rc-btn rc-btn--primary" type="submit">${R.t('mail.add.submit')}</button>
                </div>
            </form>
            <div id="rcMailHint"></div>
            <div id="rcMailList"></div>`;

        ctx.mount.querySelector('#rcMailHint').appendChild(UI.hint(
            `<p>${R.t('mail.hint.create')}</p><p>${R.t('mail.hint.revoke')}</p><p>${R.t('mail.hint.safety')}</p>`));
        ctx.mount.querySelector('#rcMailReload').addEventListener('click', () => load(ctx));
        ctx.mount.querySelector('#rcMailForm').addEventListener('submit', (e) => { e.preventDefault(); addAccount(ctx, e.target); });
        load(ctx);
    }

    async function load(ctx) {
        let st = null;
        try { st = await R.api.get('/api/mail/status'); }
        catch (e) { UI.toast(e.message, 'error'); }
        if (!ctx.isCurrent()) return;
        accounts = (st && st.accounts) || [];
        paint(ctx);
    }

    async function addAccount(ctx, form) {
        const address = form.elements.address.value.trim();
        const pwd = form.elements.app_password;
        const btn = form.querySelector('button[type="submit"]');
        btn.disabled = true; btn.textContent = R.t('mail.add.connecting');
        try {
            await R.api.post('/api/mail/accounts', { address, app_password: pwd.value });
            form.elements.address.value = '';
            UI.toast(R.t('mail.add.done', { address }), 'success');
            await load(ctx);
        } catch (e) {
            UI.toast(e.message || R.t('mail.add.failed'), 'error');
        } finally {
            pwd.value = '';   // the password never stays in the DOM after a submit
            btn.disabled = false; btn.textContent = R.t('mail.add.submit');
        }
    }

    function paint(ctx) {
        const box = ctx.mount.querySelector('#rcMailList');
        if (!box) return;
        if (!accounts.length) { box.innerHTML = UI.empty(R.t('mail.accounts.empty'), R.t('mail.accounts.emptySub'), 'fa-envelope'); return; }
        box.innerHTML = accounts.map(accountHTML).join('');
        box.querySelectorAll('.rc-mailacc').forEach((el) => bindAccount(ctx, el));
    }

    function accountHTML(a) {
        const poll = a.last_poll_at ? R.t('mail.account.lastPoll', { when: fmtWhen(a.last_poll_at) }) : R.t('mail.account.neverPolled');
        const labels = a.labels || [];
        const sys = labels.filter((l) => l.system);
        const main = labels.filter((l) => !l.system);
        const open = !!showSystem[a.id];
        return `<section class="rc-mailacc" data-acc="${a.id}">
            <div class="rc-mailacc__head">
                <div class="rc-mailacc__who">
                    <strong>${U.esc(a.address)}</strong>
                    <span class="rc-mailacc__state${a.configured ? '' : ' is-bad'}">${R.t(a.configured ? 'mail.account.configured' : 'mail.account.notConfigured')}</span>
                    <span class="rc-mailacc__meta">${U.esc(poll)}</span>
                    ${a.last_error_code ? `<span class="rc-mailacc__err">${U.esc(errText(a.last_error_code))}</span>` : ''}
                </div>
                <div class="rc-mailacc__ctrl">
                    <button class="rc-btn rc-btn--sm" data-act="refresh"><i class="fa-solid fa-rotate"></i> ${R.t('mail.account.refreshLabels')}</button>
                    <button class="rc-btn rc-btn--sm" data-act="delete"><i class="fa-solid fa-trash"></i> ${R.t('mail.account.delete')}</button>
                </div>
            </div>
            <div class="rc-mailacc__deeper">
                <label>${R.t('mail.deeper.label')}
                    <input type="number" class="rc-input rc-select--sm" min="1" max="120" step="1" value="12" data-months></label>
                <button class="rc-btn rc-btn--sm" data-act="deeper">${R.t('mail.deeper.start')}</button>
            </div>
            <h3 class="rc-mailacc__sub">${R.t('mail.labels.title')}</h3>
            <div class="rc-mailacc__labels">
                ${main.length ? main.map(labelHTML).join('') : (sys.length ? '' : `<p class="rc-mailacc__meta">${R.t('mail.labels.none')}</p>`)}
                ${sys.length ? `<button class="rc-btn rc-btn--sm" data-act="sys">${open ? R.t('mail.labels.hideSystem') : R.t('mail.labels.showSystem', { count: sys.length })}</button>` : ''}
                ${open ? sys.map(labelHTML).join('') : ''}
            </div>
        </section>`;
    }

    function labelHTML(l) {
        const name = l.display_name || l.folder;
        const sync = l.last_sync_at ? R.t('mail.label.lastSync', { when: fmtWhen(l.last_sync_at) }) : R.t('mail.label.neverSynced');
        return `<div class="rc-tgrow rc-maillabel" data-label="${l.id}">
            <label class="rc-tgrow__check">
                <input type="checkbox" data-field="enabled" ${l.enabled ? 'checked' : ''} aria-label="${U.esc(R.t('mail.label.enable', { name }))}">
                <span class="rc-tgrow__who">
                    <span class="rc-tgrow__title" title="${U.esc(name)}">${U.esc(name)}</span>
                    <span class="rc-tgrow__meta">
                        <span>${U.esc(sync)}</span>
                        <span>${R.t('mail.label.records', { count: R.i18n.num(l.records || 0) })}</span>
                        ${l.last_error_code ? `<span class="rc-mailacc__err">${U.esc(errText(l.last_error_code))}</span>` : ''}
                    </span>
                </span>
            </label>
            <div class="rc-tgrow__ctrl">
                <select class="rc-select rc-select--sm rc-catsel" data-cat-first="none" data-field="category_id" aria-label="${U.esc(R.t('mail.label.direction'))}">${UI.catOptions(l.category_id, { first: 'none' })}</select>
                <label class="rc-maillabel__claude" title="${U.esc(R.t('mail.label.claudeHint'))}">
                    <input type="checkbox" data-field="enrich" ${l.enrich ? 'checked' : ''}> ${R.t('mail.label.claude')}
                </label>
            </div>
        </div>`;
    }

    function bindAccount(ctx, el) {
        const id = Number(el.dataset.acc);
        const acc = accounts.find((x) => x.id === id);
        if (!acc) return;

        el.querySelector('[data-act="refresh"]').addEventListener('click', async () => {
            try {
                const r = await R.api.get('/api/mail/accounts/' + id + '/labels?refresh=1');
                acc.labels = mergeLabels(acc.labels || [], r.labels || []);
                UI.toast(R.t('mail.account.labelsRefreshed'), 'success');
                paint(ctx);
            } catch (e) { UI.toast(e.message, 'error'); }
        });
        el.querySelector('[data-act="delete"]').addEventListener('click', async () => {
            try {
                const r = await UI.gated(el.querySelector('[data-act="delete"]'), () => R.api.del('/api/mail/accounts/' + id).then(() => true));
                if (!r) return;
                accounts = accounts.filter((x) => x.id !== id);
                UI.toast(R.t('mail.account.deleted'), 'success');
                paint(ctx);
            } catch (e) { UI.toast(e.message, 'error'); }
        });
        el.querySelector('[data-act="deeper"]').addEventListener('click', async () => {
            const months = Math.max(1, Math.min(120, Math.round(Number(el.querySelector('[data-months]').value) || 12)));
            try {
                // Deeper than 12 months is a backfill: the server asks the Windows dialog first.
                if (months > 12) {
                    const ok = await UI.gated(el.querySelector('[data-act="deeper"]'),
                        () => R.api.post('/api/mail/backfill', { account_id: id, months }).then(() => true));
                    if (!ok) return;
                } else {
                    await R.api.post('/api/mail/sync', { account_id: id, months });
                }
                UI.toast(R.t('mail.deeper.queued'), 'success');
            } catch (e) { UI.toast(e.message, 'error'); }
        });
        const sysBtn = el.querySelector('[data-act="sys"]');
        if (sysBtn) sysBtn.addEventListener('click', () => { showSystem[id] = !showSystem[id]; paint(ctx); });

        el.querySelectorAll('.rc-maillabel').forEach((row) => {
            const lid = Number(row.dataset.label);
            row.querySelectorAll('[data-field]').forEach((inp) => inp.addEventListener('change', async () => {
                const field = inp.dataset.field;
                const body = {};
                if (field === 'category_id') body[field] = inp.value === '' ? null : Number(inp.value);
                else body[field] = inp.checked;
                try {
                    // Turning paid Claude enrichment on goes through the Windows dialog.
                    const r = (field === 'enrich' && inp.checked)
                        ? await UI.gated(null, () => R.api.post('/api/mail/labels/' + lid + '/enrich', {}))
                        : await R.api.patch('/api/mail/labels/' + lid, body);
                    if (!r) { paint(ctx); return; }
                    const cur = (acc.labels || []).find((x) => x.id === lid);
                    if (cur && r.label) Object.assign(cur, r.label);
                    // Re-render only the account section that holds the row.
                    const fresh = U.el('div', {}, accountHTML(acc)).firstElementChild;
                    el.replaceWith(fresh);
                    bindAccount(ctx, fresh);
                    UI.toast(R.t('mail.label.saved'), 'success');
                } catch (e) { UI.toast(e.message, 'error'); paint(ctx); }
            }));
        });
    }

    // The labels endpoint returns raw rows; keep the status-only fields (records, ...) we already have.
    function mergeLabels(prev, next) {
        const byId = {};
        prev.forEach((l) => { byId[l.id] = l; });
        return next.map((l) => Object.assign({ records: 0 }, byId[l.id] || {}, l));
    }

    R.views.mail = { render, destroy() { accounts = []; showSystem = {}; } };
})();
