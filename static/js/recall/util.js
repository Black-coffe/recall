/* Recall — utilities: slug/parseId, escaping, dates, tiny DOM helpers. */
(function () {
    'use strict';
    const U = {};

    // — HTML escaping (XSS) —
    U.esc = function (s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    };

    // — Cyrillic → ASCII transliteration (UA + RU) for link-friendly slugs —
    const TRANSLIT = {
        'а':'a','б':'b','в':'v','г':'h','ґ':'g','д':'d','е':'e','є':'ie','ё':'e',
        'ж':'zh','з':'z','и':'y','і':'i','ї':'yi','й':'i','к':'k','л':'l','м':'m',
        'н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh',
        'ц':'ts','ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e',
        'ю':'iu','я':'ia',
    };
    function translit(str) {
        let out = '';
        for (const ch of str.toLowerCase()) {
            out += (ch in TRANSLIT) ? TRANSLIT[ch] : ch;
        }
        return out;
    }

    // slug(id, name) -> "123-build-your-own-ai-knowledge-base"
    const SLUG_MAX_LEN = 60;
    U.slug = function (id, name) {
        let base = translit(String(name || ''))
            .normalize('NFKD').replace(/[̀-ͯ]/g, '')
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '');
        if (base.length > SLUG_MAX_LEN) base = base.slice(0, SLUG_MAX_LEN).replace(/-+[^-]*$/, '');
        return base ? id + '-' + base : String(id);
    };

    // parseId("123-foo") -> 123 ; parseId("foo") -> NaN
    U.parseId = function (seg) {
        const n = parseInt(seg, 10);
        return Number.isInteger(n) ? n : NaN;
    };

    // — Dates / durations —
    // SQLite CURRENT_TIMESTAMP пише UTC-naive рядок ("YYYY-MM-DD HH:MM:SS").
    // Позначаємо його як UTC ('Z'), щоб браузер сконвертував у ЛОКАЛЬНИЙ час
    // (напр. Europe/Kyiv +3). Без 'Z' JS трактує рядок як локальний → час
    // показується на кілька годин позаду. 'Z' додаємо лише якщо TZ ще нема.
    function _toLocalDate(s) {
        let str = String(s).replace(' ', 'T');
        if (!/[zZ]$|[+\-]\d{2}:?\d{2}$/.test(str)) str += 'Z';
        return new Date(str);
    }
    U.fmtDate = function (s) {
        if (!s) return '';
        const d = _toLocalDate(s);
        if (isNaN(d)) return String(s);
        const dd = String(d.getDate()).padStart(2, '0');
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const hh = String(d.getHours()).padStart(2, '0');
        const mi = String(d.getMinutes()).padStart(2, '0');
        return `${dd}.${mm}.${d.getFullYear()} ${hh}:${mi}`;
    };
    U.fmtDateShort = function (s) {
        if (!s) return '';
        const d = _toLocalDate(s);
        if (isNaN(d)) return String(s);
        return `${String(d.getDate()).padStart(2,'0')}.${String(d.getMonth()+1).padStart(2,'0')}.${d.getFullYear()}`;
    };
    U.fmtDuration = function (sec) {
        sec = Number(sec) || 0;
        const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
        if (h) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
        return `${m}:${String(s).padStart(2,'0')}`;
    };
    // — Дедлайни (Трек 1). Працюють з ISO-датою 'YYYY-MM-DD' (ai.due_date), яка
    // вже нормалізована на бекенді. Порівнюємо по КАЛЕНДАРНИХ днях у локальній
    // зоні, не по мілісекундах: «завтра» має лишатись «завтра» і о 23:59.
    function _isoToLocalMidnight(iso) {
        const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
        return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
    }
    U.daysUntil = function (iso) {
        const d = _isoToLocalMidnight(iso);
        if (!d) return null;
        const now = new Date();
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        return Math.round((d - today) / 86400000);
    };
    const _PLURAL_DAY = ['день', 'дні', 'днів'];
    U.plural = function (n, forms) {
        n = Math.abs(Math.round(n));
        const n10 = n % 10, n100 = n % 100;
        if (n10 === 1 && n100 !== 11) return forms[0];
        if (n10 >= 2 && n10 <= 4 && (n100 < 10 || n100 >= 20)) return forms[1];
        return forms[2];
    };
    // «сьогодні» / «завтра» / «за 3 дні» / «3 дні тому» — людською мовою.
    U.fmtDue = function (iso) {
        const n = U.daysUntil(iso);
        if (n == null) return '';
        if (n === 0) return 'сьогодні';
        if (n === 1) return 'завтра';
        if (n === -1) return 'вчора';
        if (n > 0) return `за ${n} ${U.plural(n, _PLURAL_DAY)}`;
        return `${-n} ${U.plural(n, _PLURAL_DAY)} тому`;
    };
    // Коротка дата дедлайну: «пн 27.07», з роком якщо не поточний.
    const _WD = ['нд', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
    U.fmtDueDate = function (iso) {
        const d = _isoToLocalMidnight(iso);
        if (!d) return '';
        const base = `${_WD[d.getDay()]} ${String(d.getDate()).padStart(2, '0')}.${String(d.getMonth() + 1).padStart(2, '0')}`;
        return d.getFullYear() === new Date().getFullYear() ? base : `${base}.${d.getFullYear()}`;
    };
    // Клас точності дедлайну (Трек 1): наскільки буквально читати дату.
    U.DUE_PRECISION = {
        day: '', week: 'тиждень', month: 'місяць', quarter: 'квартал',
        soon: 'найближчим часом', event: 'за подією',
        next_meeting: 'до наступної зустрічі', recurring: 'регулярно',
    };

    U.fmtBytes = function (n) {
        n = Number(n) || 0;
        if (n < 1024) return n + ' B';
        if (n < 1048576) return (n / 1024).toFixed(0) + ' KB';
        if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
        return (n / 1073741824).toFixed(2) + ' GB';
    };

    // — Source-type display labels (UA) —
    U.SRC_LABEL = {
        youtube: 'YouTube', file: 'Файл', recording: 'Запис',
        document: 'Документ', telegram: 'Telegram', library: 'Медіатека',
        meeting_archive: 'Архів', copilot: 'Ко-пілот',
    };
    U.SRC_ICON = {
        youtube: 'fa-brands fa-youtube', file: 'fa-solid fa-file-audio',
        recording: 'fa-solid fa-microphone', document: 'fa-solid fa-file-lines',
        telegram: 'fa-brands fa-telegram', library: 'fa-solid fa-compact-disc',
        meeting_archive: 'fa-solid fa-box-archive', copilot: 'fa-solid fa-wand-magic-sparkles',
    };
    // Статуси задач. У БД вони англійські (open/done/cancelled/stale) — але в
    // UI показувались сирими, тобто єдиним англійським словом серед українського
    // інтерфейсу. Один словник на всі три екрани, де задача видима.
    U.TASK_STATUS = { open: 'відкрито', done: 'виконано', cancelled: 'скасовано', stale: 'застаріло' };
    U.taskStatus = function (s) { return U.TASK_STATUS[s] || s || ''; };

    U.LANG_LABEL = { uk: 'Українська', en: 'English', ru: 'Русский', auto: 'Авто' };
    U.langLabel = function (c) { return U.LANG_LABEL[c] || (c || '').toUpperCase(); };

    // — DOM helpers —
    U.el = function (tag, attrs, html) {
        const e = document.createElement(tag);
        if (attrs) for (const k in attrs) {
            if (k === 'class') e.className = attrs[k];
            else if (k === 'dataset') Object.assign(e.dataset, attrs[k]);
            else if (k in e) e[k] = attrs[k];
            else e.setAttribute(k, attrs[k]);
        }
        if (html != null) e.innerHTML = html;
        return e;
    };
    U.debounce = function (fn, ms) {
        let t; return function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), ms); };
    };

    window.Recall.util = U;
})();
