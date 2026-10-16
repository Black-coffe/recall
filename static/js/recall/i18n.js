/* Recall — i18n: Recall.t(), per-area dictionaries, number/date formats.
Loaded right after namespace.js; the dictionaries (i18n/<area>.js) come after it.
Language: data-ui-lang on <html> = uk|en → that; auto → navigator.languages[0]
on "uk" → uk, otherwise en. No libraries and no fetch of dictionaries. */
(function () {
    'use strict';
    const R = window.Recall;
    const root = document.documentElement;

    const attr = (root.getAttribute('data-ui-lang') || 'auto').toLowerCase();
    const setting = (attr === 'uk' || attr === 'en') ? attr : 'auto';

    function detect() {
        if (setting !== 'auto') return setting;
        const nav = (navigator.languages && navigator.languages[0]) || navigator.language || '';
        return String(nav).toLowerCase().indexOf('uk') === 0 ? 'uk' : 'en';
    }
    const lang = detect();
    root.setAttribute('lang', lang);

    const dict = { uk: {}, en: {} };

    function add(area, bundle) {
        ['uk', 'en'].forEach(function (l) {
            Object.assign(dict[l], (bundle && bundle[l]) || {});
        });
    }

    const rules = {};
    function pluralForm(l, n) {
        try {
            rules[l] = rules[l] || new Intl.PluralRules(l);
            return rules[l].select(n);
        } catch (e) {
            return n === 1 ? 'one' : 'other';
        }
    }

    function pickForm(val, l, vars) {
        if (typeof val !== 'object' || val === null) return val;
        const n = Number(vars && vars.count);
        const f = pluralForm(l, isNaN(n) ? 0 : n);
        return val[f] != null ? val[f] : (val.other != null ? val.other : (val.many != null ? val.many : val.one));
    }

    function fill(str, vars) {
        if (str == null || !vars) return str;
        return String(str).replace(/\{(\w+)\}/g, function (m, k) {
            return vars[k] != null ? vars[k] : m;
        });
    }

    function t(key, vars) {
        let val = dict[lang][key];
        let l = lang;
        if (val == null && lang !== 'en') { val = dict.en[key]; l = 'en'; }
        if (val == null) {
            if (typeof console !== 'undefined') console.warn('[i18n] missing key: ' + key);
            return key;
        }
        return fill(pickForm(val, l, vars), vars);
    }

    function num(n, opts) {
        return Number(n).toLocaleString(lang, opts);
    }
    function date(d, opts) {
        const dt = (d instanceof Date) ? d : new Date(d);
        return new Intl.DateTimeFormat(lang, opts).format(dt);
    }
    let collator = null;
    function compare(a, b) {
        collator = collator || new Intl.Collator(lang);
        return collator.compare(a, b);
    }
    function headers() {
        return { 'X-Recall-Lang': lang };
    }

    function apply(scope) {
        const r = scope || document;
        const map = [
            ['data-i18n', function (el, v) { el.textContent = v; }],
            ['data-i18n-title', function (el, v) { el.setAttribute('title', v); }],
            ['data-i18n-placeholder', function (el, v) { el.setAttribute('placeholder', v); }],
            ['data-i18n-aria-label', function (el, v) { el.setAttribute('aria-label', v); }],
        ];
        map.forEach(function (m) {
            r.querySelectorAll('[' + m[0] + ']').forEach(function (el) {
                m[1](el, t(el.getAttribute(m[0])));
            });
        });
    }

    R.t = t;
    R.i18n = { lang: lang, setting: setting, add: add, t: t, num: num, date: date,
               compare: compare, headers: headers, apply: apply };
})();
