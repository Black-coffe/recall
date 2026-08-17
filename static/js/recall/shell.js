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
            <span>Доступна новіша версія <b>faster-whisper ${safeVer(s.latest_version)}</b> (у вас ${safeVer(s.installed_version)}) — можливо, зʼявилися нові моделі.</span>
            <code class="rc-mono">${safeVer(s.upgrade_command || 'pip install -U faster-whisper')}</code>
            <a href="/settings" data-route="settings" class="rc-updbar__link">Моделі →</a>
            <button class="rc-updbar__x" aria-label="Сховати">&times;</button>`;
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
            el.textContent = typeof n === 'number' ? n.toLocaleString('uk') : (n || '');
        });
    }
    function setCountError(key) {
        document.querySelectorAll(`[data-count="${key}"]`).forEach(el => {
            el.classList.add('rc-nav__count--err');
            el.textContent = '–'; // en dash — visually distinct from "0"
            el.title = 'Не вдалося завантажити';
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
        return state === 'offline' ? 'Офлайн' : state === 'degraded' ? 'Сервер не відповідає' : 'Онлайн';
    }
    function connTitle(state) {
        return state === 'offline' ? 'Немає мережевого з’єднання'
            : state === 'degraded' ? 'Сервер не відповідає вчасно — можливо, перевантажений або завис'
            : 'З’єднання із сервером у нормі';
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
            R.ui.toast(state === 'offline' ? 'Немає мережевого з’єднання' : 'Сервер не відповідає', 'error');
            connDegradedToastShown = true;
        } else if (state === 'online' && connDegradedToastShown) {
            R.ui.toast('З’єднання відновлено', 'success');
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
        showActionToast('Доступна нова версія Recall', 'Оновити', () => {
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
            '<button type="button" class="rc-toast__x" aria-label="Закрити">&times;</button>';
        t.querySelector('span').textContent = msg;
        t.querySelector('.rc-toast__action').textContent = actionLabel;
        wrap.appendChild(t);
        t.querySelector('.rc-toast__action').addEventListener('click', () => { onAction(); t.remove(); });
        t.querySelector('.rc-toast__x').addEventListener('click', () => t.remove());
    }

    R.shell = shell;
})();
