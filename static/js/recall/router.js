/* Recall — client-side router. Plain history API, no framework.
   Routes render into #view; the shell (sidebar/topbar) stays mounted.
   Internal links are ordinary <a href="/..."> intercepted via delegation,
   so deep-links cold-start correctly even if JS is slow/disabled. */
(function () {
    'use strict';
    const R = window.Recall;
    const router = {};
    let _current = null;   // { route, view } currently mounted
    let _seq = 0;          // guards against out-of-order async renders

    // Route table: pattern with :params -> regex. Order = priority.
    const ROUTES = [
        { id: 'home',       pattern: '/',                 view: 'home' },
        { id: 'library',    pattern: '/library',          view: 'library' },
        { id: 'audio',      pattern: '/audio',            view: 'audio' },
        { id: 'transcript', pattern: '/transcript/:slug', view: 'transcript' },
        { id: 'ask',        pattern: '/ask',              view: 'ask' },
        { id: 'research',   pattern: '/research',         view: 'research' },
        { id: 'entity',     pattern: '/entities/:id',     view: 'entity' },
        { id: 'entities',   pattern: '/entities',         view: 'entities' },
        { id: 'tasks',      pattern: '/tasks',            view: 'tasks' },
        { id: 'comments',   pattern: '/comments',         view: 'comments' },
        { id: 'speakers',   pattern: '/speakers',         view: 'speakers' },
        { id: 'youtube',    pattern: '/youtube',          view: 'youtube' },
        { id: 'upload',     pattern: '/upload',           view: 'upload' },
        { id: 'documents',  pattern: '/documents',        view: 'documents' },
        { id: 'telegram',   pattern: '/telegram',         view: 'telegram' },
        { id: 'settings',   pattern: '/settings',         view: 'settings' },
        { id: 'record',     pattern: '/record',           view: 'record' },
    ];
    ROUTES.forEach(r => {
        const keys = [];
        const rx = r.pattern.replace(/:[^/]+/g, (m) => { keys.push(m.slice(1)); return '([^/]+)'; });
        r.regex = new RegExp('^' + rx + '/?$');
        r.keys = keys;
    });
    R.routes = ROUTES;

    function match(pathname) {
        for (const r of ROUTES) {
            const m = r.regex.exec(pathname);
            if (m) {
                const params = {};
                r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
                return { route: r, params };
            }
        }
        return null;
    }
    router.match = match;

    function mountEl() { return document.getElementById('view'); }

    async function render(path) {
        const url = new URL(path, location.origin);
        const found = match(url.pathname);
        const mount = mountEl();
        if (!mount) return;

        // teardown previous view
        if (_current && _current.view && typeof _current.view.destroy === 'function') {
            try { _current.view.destroy(); } catch (_) {}
        }

        if (!found) {
            mount.innerHTML = R.ui.empty('Сторінку не знайдено', 'Можливо, посилання застаріле.', 'fa-compass');
            R.shell.setActive(null);
            _current = null;
            return;
        }

        const view = R.views[found.route.view];
        R.shell.setActive(found.route.id === 'entity' ? 'entities'
            : found.route.id === 'home' ? 'home' : found.route.id);

        if (!view) {
            mount.innerHTML = R.ui.error('Модуль «' + found.route.view + '» не завантажено.');
            _current = null;
            return;
        }

        const ctx = {
            params: found.params,
            query: Object.fromEntries(url.searchParams.entries()),
            searchParams: url.searchParams,
            mount,
            navigate: router.navigate,
            replace: router.replace,
            seq: ++_seq,
            isCurrent() { return this.seq === _seq; },
        };
        _current = { route: found.route, view };
        mount.scrollTop = 0;
        const content = document.getElementById('rcContent');
        if (content) content.scrollTop = 0;
        try {
            await view.render(ctx);
        } catch (err) {
            if (ctx.isCurrent()) mount.innerHTML = R.ui.error(err && err.message);
        }
    }

    // — Navigation API —
    router.navigate = function (path, opts) {
        opts = opts || {};
        if (path === location.pathname + location.search) { return; }
        if (opts.replace) history.replaceState({ path }, '', path);
        else history.pushState({ path }, '', path);
        render(path);
    };
    // canonical-slug fix without a new history entry / re-render
    router.replace = function (path) {
        if (path !== location.pathname + location.search) {
            history.replaceState({ path }, '', path);
        }
    };

    // — popstate (back/forward) —
    window.addEventListener('popstate', function () {
        render(location.pathname + location.search);
    });

    // — Delegated link interception —
    document.addEventListener('click', function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        const a = e.target.closest('a[href]');
        if (!a) return;
        const href = a.getAttribute('href');
        if (!href || a.target === '_blank' || a.hasAttribute('data-native')) return;
        // only same-origin absolute paths; let everything else (external,
        // /api, /static, hash, mailto) navigate natively.
        if (!href.startsWith('/')) return;
        if (href.startsWith('/api/') || href.startsWith('/static/')) return;
        if (!match(new URL(href, location.origin).pathname)) return; // unknown → native
        e.preventDefault();
        router.navigate(href);
    });

    router.start = function () {
        render(location.pathname + location.search);
    };

    R.router = router;

    // boot once DOM + all modules are parsed (scripts are at body end)
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => { R.shell.init(); router.start(); });
    } else {
        R.shell.init(); router.start();
    }
})();
