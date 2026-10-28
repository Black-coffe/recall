/* Recall — shell behavior: active nav, sidebar counts, burger, ⌘K trigger.
   The chrome markup lives statically in shell.html (resilient); this only
   adds behavior and live data. */
(function () {
    'use strict';
    const R = window.Recall;
    const shell = {};
    let _inited = false;

    shell.setActive = function (routeId) {
        document.querySelectorAll('.rc-nav__item[data-route]').forEach(a => {
            a.classList.toggle('is-active', a.getAttribute('data-route') === routeId);
        });
    };

    shell.init = function () {
        if (_inited) return; _inited = true;
        R.i18n.apply(document);

        // Burger (mobile)
        const burger = document.getElementById('rcBurger');
        const app = document.getElementById('rcApp');
        if (burger && app) {
            burger.addEventListener('click', () => app.classList.toggle('rc-sidebar-open'));
            // close sidebar after navigating on mobile
            document.getElementById('rcSidebar')?.addEventListener('click', (e) => {
                if (e.target.closest('a[data-route]')) app.classList.remove('rc-sidebar-open');
            });
        }

        // ⌘K trigger + shortcut
        const trigger = document.getElementById('rcCmdkTrigger');
        if (trigger) trigger.addEventListener('click', () => R.cmdk.open());
        document.addEventListener('keydown', (e) => {
            if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
                e.preventDefault(); R.cmdk.open();
            }
        });

        // First-run onboarding (T5.3) + its permanent re-open entry point.
        const onbTrigger = document.getElementById('rcOnbTrigger');
        if (onbTrigger && R.onboarding) onbTrigger.addEventListener('click', () => R.onboarding.open());
        if (R.onboarding) R.onboarding.maybeShowFirstRun();

        shell.loadCounts();
        shell.checkModelUpdates();
        startConnWatch();
        startCallPrompts();
        startRecBadge();
        checkRecovered();
        initServiceWorker();
    };

    // Header notice when a newer faster-whisper is published (= maybe new models).
    // Status is filled by the app's staleness-gated startup check; we only read it.
    shell.checkModelUpdates = async function () {
        try {
            const s = await R.api.get('/api/models/update-status');
            shell.reportRequestOk();
            if (!s || !s.update_available) return;
            if (sessionStorage.getItem('rcDismissModelUpd') === String(s.latest_version)) return;
            renderUpdateBanner(s);
        } catch (err) {
            // Not shown to the user (this banner is a nice-to-have, not
            // critical) — but no longer swallowed: feed the shared
            // connection-health signal and leave a trace for debugging.
            shell.reportRequestError(err);
            console.warn('[shell] checkModelUpdates failed:', err && err.message);
        }
    };

    function safeVer(v) { return String(v == null ? '' : v).replace(/[^0-9A-Za-z.\- ]/g, ''); }
    function renderUpdateBanner(s) {
        const main = document.querySelector('.rc-main');
        const content = document.getElementById('rcContent');
        if (!main || !content || document.getElementById('rcUpdBanner')) return;
        const bar = document.createElement('div');
        bar.id = 'rcUpdBanner';
        bar.className = 'rc-updbar';
        bar.innerHTML = `<i class="fa-solid fa-circle-arrow-up"></i>
            <span>${R.t('core.shell.upd.text', { latest: safeVer(s.latest_version), installed: safeVer(s.installed_version) })}</span>
            <code class="rc-mono">${safeVer(s.upgrade_command || 'pip install -U faster-whisper')}</code>
            <a href="/settings" data-route="settings" class="rc-updbar__link">${R.t('core.shell.upd.models')}</a>
            <button class="rc-updbar__x" aria-label="${R.t('core.shell.hide')}">&times;</button>`;
        main.insertBefore(bar, content);
        bar.querySelector('.rc-updbar__x').addEventListener('click', () => {
            try { sessionStorage.setItem('rcDismissModelUpd', String(s.latest_version || '1')); } catch (_) {}
            bar.remove();
        });
    }

    shell.loadCounts = async function () {
        try {
            const s = await R.api.stats();
            R.state.stats = s;
            shell.reportRequestOk();
            const sumVals = (o) => o ? Object.values(o).reduce((a, b) => a + (b || 0), 0) : 0;
            setCount('library', s.transcriptions ? s.transcriptions.total : 0);
            setCount('entities', sumVals(s.entities_significant));
            setCount('tasks', s.action_items ? s.action_items.open : 0);
        } catch (err) {
            // Was a bare silent catch — counts stayed blank forever with no
            // way to tell "0 items" apart from "couldn't load". Now: feed
            // the connection-health signal and mark the badges as errored
            // (dash + tooltip) instead of leaving them empty.
            shell.reportRequestError(err);
            console.warn('[shell] loadCounts failed:', err && err.message);
            setCountError('library'); setCountError('entities'); setCountError('tasks');
        }
    };

    function setCount(key, n) {
        document.querySelectorAll(`[data-count="${key}"]`).forEach(el => {
            el.classList.remove('rc-nav__count--err');
            el.title = '';
            el.textContent = typeof n === 'number' ? R.i18n.num(n) : (n || '');
        });
    }
    function setCountError(key) {
        document.querySelectorAll(`[data-count="${key}"]`).forEach(el => {
            el.classList.add('rc-nav__count--err');
            el.textContent = '–'; // en dash — visually distinct from "0"
            el.title = R.t('core.shell.countFail');
        });
    }

    // ============================================================
    // Connection-health indicator (topbar #rcConn): online / degraded /
    // offline. Signal sources:
    //   - typed errors from api.js getJSON (err.isTimeout / err.isNetworkError
    //     vs a plain HTTP error with err.status) — see api.js ~50.
    //   - a lightweight periodic /api/health ping (open, no-auth endpoint).
    //   - browser online/offline events for the "offline" case specifically.
    // NOT wired into SSE/long POST flows (transcription, ask/stream, polish,
    // research) — those are not getJSON-timeout calls, so a normal
    // long-running Claude call never flips this to "degraded".
    // ============================================================
    const CONN_PING_INTERVAL_MS = 25000;
    const CONN_PING_TIMEOUT_MS = 5000;
    let connState = 'online';
    let connDegradedToastShown = false;

    function connLabel(state) {
        return state === 'offline' ? R.t('core.conn.offline') : state === 'degraded' ? R.t('core.conn.degraded') : R.t('core.conn.online');
    }
    function connTitle(state) {
        return state === 'offline' ? R.t('core.conn.offlineTitle')
            : state === 'degraded' ? R.t('core.conn.degradedTitle')
            : R.t('core.conn.onlineTitle');
    }
    function setConnState(state) {
        const el = document.getElementById('rcConn');
        const changed = state !== connState;
        const prev = connState;
        connState = state;
        if (el) {
            el.setAttribute('data-state', state);
            el.title = connTitle(state);
            const label = el.querySelector('.rc-conn__label');
            if (label) label.textContent = connLabel(state);
        }
        if (!changed) return;
        // Toast only on meaningful transitions — never spam on every ping.
        if (state !== 'online' && prev === 'online') {
            R.ui.toast(state === 'offline' ? R.t('core.conn.offlineTitle') : R.t('core.conn.degraded'), 'error');
            connDegradedToastShown = true;
        } else if (state === 'online' && connDegradedToastShown) {
            R.ui.toast(R.t('core.conn.restored'), 'success');
            connDegradedToastShown = false;
        }
    }

    // Any shell-level request using the api.js typed-error contract calls
    // this on failure instead of swallowing it — see checkModelUpdates /
    // loadCounts above and pingHealth below.
    shell.reportRequestError = function (err) {
        if (!err) return;
        if (typeof err.status === 'number') return; // real HTTP error from a reachable server — not a connectivity problem
        if (err.isTimeout || err.isNetworkError) {
            setConnState(navigator.onLine === false ? 'offline' : 'degraded');
        }
    };
    shell.reportRequestOk = function () { setConnState('online'); };

    async function pingHealth() {
        if (document.hidden) return; // don't burn requests on background tabs
        try {
            await R.api.get('/api/health', { timeoutMs: CONN_PING_TIMEOUT_MS, retries: 0 });
            shell.reportRequestOk();
        } catch (err) {
            shell.reportRequestError(err);
        }
    }

    function startConnWatch() {
        window.addEventListener('online', () => pingHealth());
        window.addEventListener('offline', () => setConnState('offline'));
        document.addEventListener('visibilitychange', () => { if (!document.hidden) pingHealth(); });
        pingHealth();
        setInterval(pingHealth, CONN_PING_INTERVAL_MS);
    }

    // ============================================================
    // K2: call prompts ("Record?" / "Stop?") as a banner in the tab.
    // The detector lives on the server (call_watch.py) and sends Windows notifications; here we only
    // show the same thing GET /api/recording/autoprompt returns, with the same
    // buttons. A button = a jump to a one-time action URL: the server performs the action and
    // returns to /record. Polling happens only while the tab is visible.
    const CALL_POLL_MS = 5000;
    const callShown = new Map();   // prompt id → element (null = closed here, do not show again)

    function startCallPrompts() {
        document.addEventListener('visibilitychange', () => { if (!document.hidden) pollCallPrompts(); });
        pollCallPrompts();
        setInterval(pollCallPrompts, CALL_POLL_MS);
    }

    async function pollCallPrompts() {
        if (document.hidden) return;
        let d = null;
        try { d = await R.api.get('/api/recording/autoprompt', { retries: 0 }); } catch (_) { return; }
        const prompts = (d && d.enabled && Array.isArray(d.prompts)) ? d.prompts : [];
        const live = new Set(prompts.map(p => p.id));
        for (const [id, el] of callShown) {
            if (!live.has(id)) { if (el) el.remove(); callShown.delete(id); }
        }
        for (const p of prompts) {
            if (!callShown.has(p.id)) callShown.set(p.id, showCallPrompt(p));
        }
    }

    function goAction(url) {
        const u = new URL(url, window.location.origin);   // same origin, however Recall was opened
        window.location.href = u.pathname + u.search;
    }

    function showCallPrompt(p) {
        const wrap = document.getElementById('rcToasts');
        if (!wrap) return null;
        const isRec = p.kind === 'record';
        const isStarted = p.kind === 'started';   // auto-record already running: Stop or Keep (hide, no request)
        const hide = () => { t.remove(); callShown.set(p.id, null); };
        const t = document.createElement('div');
        t.className = 'rc-toast rc-toast--action rc-toast--call';
        t.innerHTML = '<i class="rc-ico fa-solid ' + (isRec ? 'fa-phone' : isStarted ? 'fa-circle-dot' : 'fa-circle-stop') + '"></i><span></span>' +
            '<div class="rc-toast__actions"></div>' +
            '<button type="button" class="rc-toast__x" aria-label="' + R.t('core.close') + '">&times;</button>';
        t.querySelector('span').textContent = isRec
            ? R.t('core.call.started', { label: p.label })
            : isStarted ? R.t('core.call.recStarted')
            : R.t('core.call.ended', { label: p.label });
        if (isStarted && p.consent_reminder === true) {
            // wave4-client-calls-07: the consent line under "Recording started"; it blocks nothing.
            const c = document.createElement('div');
            c.style.cssText = 'margin-top:4px;font-size:var(--rc-t-xs)';
            c.textContent = R.t('core.call.consent');
            t.querySelector('span').appendChild(c);
        }
        const acts = t.querySelector('.rc-toast__actions');
        const add = (label, fn) => {
            const b = document.createElement('button');
            b.type = 'button'; b.className = 'rc-toast__action'; b.textContent = label;
            b.addEventListener('click', fn);
            acts.appendChild(b);
        };
        const dismiss = async () => {
            t.remove(); callShown.set(p.id, null);
            try { await R.api.post('/api/recording/autoprompt/' + encodeURIComponent(p.id) + '/dismiss', {}); }
            catch (err) { console.warn('[shell] call prompt dismiss failed:', err && err.message); }
        };
        if (isRec) {
            add(R.t('core.call.record'), () => goAction(p.actions.record));
            add(R.t('core.call.sources'), () => goAction(p.actions.sources));
            add(R.t('core.call.dismiss'), dismiss);
        } else if (isStarted) {
            add(R.t('core.call.stop'), () => goAction(p.actions.stop));
            add(R.t('core.call.keep'), hide);
        } else {
            add(R.t('core.call.stop'), () => goAction(p.actions.stop));
            add(R.t('core.call.keep'), () => goAction(p.actions.keep));
        }
        // × on "Record?" = "Not needed"; on "Stop?" it only hides the banner here
        // (auto-stop after silence stays — the server controls it).
        t.querySelector('.rc-toast__x').addEventListener('click', isRec ? dismiss : hide);
        wrap.appendChild(t);
        return t;
    }

    // ============================================================
    // REC badge (topbar #rcRecBadge) + recovered-recordings banner.
    // The badge polls GET /api/recordings/active like the call prompts (visible tab only).
    // The banner reads /api/recordings/recovered with ack=0; the endpoint clears its log
    // on read, so the ack=1 request is sent only after the banner is on screen.
    // ============================================================
    const REC_POLL_MS = 5000;
    let recCount = 0;

    function startRecBadge() {
        document.addEventListener('visibilitychange', () => { if (!document.hidden) pollRecBadge(); });
        // The router swaps the route without a poll: re-check after any click (the router
        // handles it first, the timeout runs after) and after back/forward.
        document.addEventListener('click', () => setTimeout(applyRecBadge, 0));
        window.addEventListener('popstate', applyRecBadge);
        pollRecBadge();
        setInterval(pollRecBadge, REC_POLL_MS);
    }

    async function pollRecBadge() {
        if (document.hidden) return;
        let d = null;
        try { d = await R.api.get('/api/recordings/active', { retries: 0 }); }
        catch (err) { console.debug('[shell] rec badge poll failed:', err && err.message); return; }
        recCount = (d && Array.isArray(d.sessions)) ? d.sessions.length : 0;
        applyRecBadge();
    }

    function applyRecBadge() {
        const el = document.getElementById('rcRecBadge');
        if (!el) return;
        el.hidden = window.location.pathname === '/record' || recCount === 0;
        const n = document.getElementById('rcRecCount');
        if (n) n.textContent = recCount > 1 ? R.i18n.num(recCount) : '';
    }

    async function checkRecovered() {
        let d = null;
        try { d = await R.api.get('/api/recordings/recovered?ack=0', { retries: 0 }); }
        catch (err) { console.debug('[shell] recovered check failed:', err && err.message); return; }
        const n = (d && d.count) ? d.count : 0;
        if (!n) return;
        if (!renderRecoveredBanner(n)) return;
        try { await R.api.get('/api/recordings/recovered?ack=1', { retries: 0 }); }
        catch (err) { console.warn('[shell] recovered ack failed:', err && err.message); }
    }

    function renderRecoveredBanner(n) {
        const main = document.querySelector('.rc-main');
        const content = document.getElementById('rcContent');
        if (!main || !content || document.getElementById('rcRecBanner')) return false;
        const bar = document.createElement('div');
        bar.id = 'rcRecBanner';
        bar.className = 'rc-updbar';
        bar.innerHTML = `<i class="fa-solid fa-circle-dot"></i>
            <span>${R.t('core.rec.recovered', { count: n })}</span>
            <a href="/audio" data-route="audio" class="rc-updbar__link">${R.t('core.rec.openAudio')}</a>
            <button class="rc-updbar__x" aria-label="${R.t('core.shell.hide')}">&times;</button>`;
        main.insertBefore(bar, content);
        bar.querySelector('.rc-updbar__x').addEventListener('click', () => bar.remove());
        return true;
    }

    // ============================================================
    // Service worker: registration + "new version available" update flow.
    // sw.js no longer calls self.skipWaiting() unconditionally on install —
    // a newly installed worker stays "waiting" until the user explicitly
    // asks for it here (button click → SKIP_WAITING message), so an open
    // tab is never silently switched to new module code mid-session.
    // ============================================================
    let swUpdateToastShown = false;
    let swUpdateRequested = false;
    let swReloadOnce = false;

    function initServiceWorker() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.register('/sw.js').then((reg) => {
            // A worker may already be waiting from before this page load
            // (tab was open when a new version installed).
            if (reg.waiting && navigator.serviceWorker.controller) promptSwUpdate(reg);
            const trackInstalling = (installing) => {
                if (!installing) return;
                installing.addEventListener('statechange', () => {
                    // "installed" + an existing controller = this is an
                    // update, not the very first install on this page.
                    if (installing.state === 'installed' && navigator.serviceWorker.controller) {
                        promptSwUpdate(reg);
                    }
                });
            };
            // Cover the race where install was already in progress by the
            // time .then() fired (updatefound would've fired before we
            // could listen for it) — check reg.installing directly too.
            trackInstalling(reg.installing);
            reg.addEventListener('updatefound', () => trackInstalling(reg.installing));
        }).catch((err) => console.warn('[shell] SW register failed:', err && err.message));

        // Reload exactly once, and only after WE asked for the update (via
        // the toast button below) — guards against any reload loop and
        // against reloading on the very first-ever SW claim of a fresh tab.
        navigator.serviceWorker.addEventListener('controllerchange', () => {
            if (!swUpdateRequested || swReloadOnce) return;
            swReloadOnce = true;
            window.location.reload();
        });
    }

    function promptSwUpdate(reg) {
        if (swUpdateToastShown) return;
        swUpdateToastShown = true;
        showActionToast(R.t('core.shell.newVersion'), R.t('core.shell.update'), () => {
            swUpdateRequested = true;
            if (reg.waiting) reg.waiting.postMessage({ type: 'SKIP_WAITING' });
        });
    }

    // A toast with an action button that does NOT auto-dismiss (unlike
    // R.ui.toast) — reserved for things the user must consciously act on.
    function showActionToast(msg, actionLabel, onAction) {
        const wrap = document.getElementById('rcToasts');
        if (!wrap) return;
        const t = document.createElement('div');
        t.className = 'rc-toast rc-toast--action';
        t.innerHTML = '<i class="rc-ico fa-solid fa-circle-arrow-up"></i><span></span>' +
            '<button type="button" class="rc-toast__action"></button>' +
            '<button type="button" class="rc-toast__x" aria-label="' + R.t('core.close') + '">&times;</button>';
        t.querySelector('span').textContent = msg;
        t.querySelector('.rc-toast__action').textContent = actionLabel;
        wrap.appendChild(t);
        t.querySelector('.rc-toast__action').addEventListener('click', () => { onAction(); t.remove(); });
        t.querySelector('.rc-toast__x').addEventListener('click', () => t.remove());
    }

    R.shell = shell;
})();
