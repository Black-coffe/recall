/* Recall service worker — повний офлайн app shell + кеш read-only API.
 *
 * Стратегії:
 *   - HTML/navigation:    network-first, fallback на cache, фінальний fallback на '/'.
 *   - /static/* (local):  stale-while-revalidate (швидко з кешу + оновлення у фоні).
 *   - CDN (origin !=):    cache-first (versioned URLs Inter/FontAwesome/Tippy — стабільні).
 *   - /api/* GET:         network-first з fallback'ом на cache (дані можуть бути stale, але краще ніж нічого).
 *   - SSE (Accept: text/event-stream або шлях /stream): не кешується (browser default).
 *   - POST/PATCH/DELETE:  не кешується.
 *
 * Що НЕ працює офлайн (свідомо): транскрипція, запис, ask/stream, polish, backup.
 * Що працює: відкрити сторінку, переглянути історію, сутності, задачі (з кешу).
 */

// Bump при зміні app shell (JS/CSS/HTML), щоб SW скинув старий кеш і віддав
// свіжі ассети. v2 — Phase 16 (вкладка Документи, нові формати, імпорт папки).
// v3 — Phase 17 (вкладка Telegram + кнопка «Новий напрямок»).
// v4 — фікс: чекбокс/селектор напрямку у TG слухали change через data-change.
// v5 — TG: фільтри-чипси + показ збереженого напрямку (merge з /chats).
// v6 — Дизайн-стандарт: видалено темну тему, ребренд підписів (Recall/записів).
// v10 — Recall Phase 2: нативна сторінка YouTube (/youtube) у новому каркасі.
// v11 — Recall Phase 2: нативна сторінка завантаження файлу (/upload).
// v12 — Recall Phase 2: нативна сторінка документів (/documents).
// v13 — Recall Phase 2: нативна сторінка Telegram (/telegram).
// v14 — Recall Phase 2: нативна сторінка Налаштувань (/settings).
// v15 — Recall Phase 2: нативна сторінка Запису (/record). Phase 2 завершено.
// v16 — cleanup: APP_SHELL = новий каркас (recall/*), а не старий app.js/index.html.
// v17 — нативна Аудіотека (/audio).
// v18 — пост-обробка транскрипту: переклад/теми/хмара слів/емоції.
// v19 — bulk-розмітка напрямку + bulk-видалення у Бібліотеці.
// v20 — динамічний каталог моделей + перевірка оновлень faster-whisper.
// v21 — дефолтна модель транскрибації large-v3-turbo (замість medium).
// v22 — новий розділ «Дослідження»: великий експорт згадок бренду (оригінали/саммарі).
// v23 — кнопки експорту на картці сутності (той самий рушій research_export.js).
// v24 — порт legacy: YouTube-обрізка (trim) + сторінка «Спікери» (статистика/обʼєднання/таймлайн).
// v25 — редактор спікерів на сторінці запису: назвати/переназначити діаризовані голоси.
// v26 — клік-по-сегменту→seek+підсвітка під час відтворення; закладки; ✨ авто-напрямок; saved searches; bulk-export.
// v27 — видалено /legacy: усі фічі портовані в новий каркас (index.html/app.js/старі CSS прибрано).
// v28 — Co-pilot Крок 1: мега-панель ко-пілота на старті запису (вектор/режим/важливість/бюджет/лише-локально).
// v29 — Co-pilot Крок 2: смужка тем у записі (зсув/повернення теми) через copilot_topic SSE.
// v30 — Co-pilot Крок 3: локальний диспетчер-LLM (триаж+RAG+інсайти) → чорнові картки copilot_insight.
// v31 — Co-pilot Крок 4: повноцінний віджет оператора (2-колонки, картки з діями dismiss/pin/👍👎/копнути, статус, пауза, антишум, згортання).
// v32 — Co-pilot Крок 5: ескалація в Claude (Sonnet верифікує → бейдж «перевірено», лічильник $, бюджет/safety-sweep, ручна «копнути глибше»).
// v33 — Co-pilot Крок 6: матриця режим×важливість (мульти-голос verify, Opus на гнарлі), cost-governor (warn), зміна режиму/важливості на льоту.
// v34 — Co-pilot Крок 7: таймлайн сесії на сторінці транскрипту (паралельна доріжка подій + seek по інсайту), лінк сесія→транскрипт.
// v35 — Co-pilot Крок 8: експорт сесії (md/json: діалог+нотатки) + реінджест підказок у RAG (source_type=copilot).
// v36 — Co-pilot Крок 9: hardening — GPU-черга, банер деградації, розділ «Ко-пілот» у налаштуваннях, smoke-тести.
// v37 — фільтр напрямків на /audio + /entities, фільтр напрямків + кнопки періоду (24год/7д/місяць/рік/усі) на /tasks.
// v38 — переробка контролу періоду на /tasks: сегмент-контрол (max-content+nowrap, не ламається), коротші підписи.
// v39 — фікс колізії класів: контрол періоду .rc-seg→.rc-period (.rc-seg вже зайнятий аудіо-сегментами /transcript як grid).
// v40 — контекстні фасет-фільтри в Бібліотеці під джерело: Telegram (відправник+чат), YouTube (автор) — live від 2 символів.
// v41 — напрямки: керування в Налаштуваннях (CRUD+перенесення/обʼєднання), inline «＋ Новий напрямок…» у будь-якому селекті, casefold-унікальність (кирилиця-дублі неможливі).
// v42 — recording UX: запис зʼявляється в Аудіотеці одразу після save (неблокуючий save→фонова транскрипція library-шляхом), статус «транскрибується…» у картці (polling /api/transcribe/active), власний напрямок запису (audio_downloads.category_id, фільтр працює до транскрипції).
// v43 — screen video capture toggle + monitor select + video status chips (Phase 22, S7).
// v44 — Аудіотека→Медіатека + 📹 Відео badge + UTC→local date fix (Phase 22).
// v45 — Camtasia-style screen region selection UI: per-monitor Повний/Область toggle, drag-rect modal, numeric inputs, region chip (Phase 22 R-D).
// v46 — Phase 23 B-C: synced muted screen-video player, keyframe strip, «Розібрати відео» button on /transcript.
// v47 — Phase 23B: vision-опис кадрів (Qwen2.5-VL / Claude) → у RAG + тултипи кадрів + Claude-опис toggle.
// v48 — REC-індикатор: live розмір файлу + швидкість росту (МБ/хв) + вільне місце на диску з прогнозом.
// v49 — REC-відеочіп: розмір виділеної області (◳ W×H) + видеотреки в get_state (чіпи переживають reload).
// v50 — REC-відеочіп: звірка track_id зі state після старту (канонічна мітка/статус/fps + region, без stale-чипа).
// v51 — фікс fps/МБ на відеочіпі: video_stats шле {stats:{...}} вкладено, фронт читав плоско → розпаковка.
// v52 — Волна 2 T4.4: прибрано безумовний skipWaiting() (тепер тільки за
//       SKIP_WAITING-повідомленням від клієнта, див. shell.js promptSwUpdate);
//       APP_SHELL більше не дублюється вручну — install парсить реальні
//       <script src>/<link href> з відповіді на '/' (deriveAppShellUrls),
//       лишається лиш FALLBACK_SHELL для того, чого в shell.html немає
//       тегом (manifest/icons) — див. чекліст bump'а нижче.
// v53 — Волна 3 T5.3: онбординг-візард першого запуску (static/js/recall/
//       onboarding.js, новий <script> тег у shell.html — підхоплюється
//       парсингом; localStorage recall_onboarding_done). shell.js викликає
//       maybeShowFirstRun() + топбар-іконка graduation-cap для повторного
//       відкриття; мінімальний CSS (.rc-modal--wide, .rc-onb__*).
// v58 — UI/UX списків: /library рендерить рядок ПО ТИПУ джерела (TG —
//       чат+відправник у підписі, повідомлення в тілі; дзвінок — тривалість/
//       спікери/задачі), /audio отримав фільтр «Без транскрипта», /tasks
//       перебудовано на відра терміновості (протерміновано / 7 днів / пізніше
//       / без дати) + фасет власника. Токени --rc-ink-3 і --rc-ok підняті до
//       WCAG AA; глобальне `.rc-app [hidden]{display:none}` прибрало фантомну
//       панель bulk-дій, що висіла внизу Бібліотеки завжди.
// v62 — шар коментарів власника (Волни 0-6): бейдж-лічильник і інлайн-панель
//       на картках /library та /audio, постійна панель на /transcript із
//       якорем на таймкод плеєра, поле живого коментаря на /record
//       (Ctrl+Shift+K), нова сторінка /comments — записник поверх усього
//       архіву з фасетами за типом. Нові файли: js/recall/comments.js
//       (спільний компонент) і js/recall/views/comments.js — обидва мають
//       теги в shell.html, тож install підхопить їх сам.
//
// ЧЕКЛІСТ bump'а версії (мінімум ручної роботи, без build-step):
//   1. Правка в JS/CSS під /static/ → просто збільш VERSION нижче + додай
//      рядок у changelog. Список файлів чіпати НЕ треба — install сам
//      перечитає <script>/<link> теги з shell.html.
//   2. Новий JS/CSS-файл, потрібен для першого рендеру (view-модуль, css)
//      → додай йому <script>/<link> тег у templates/shell.html — і все:
//      sw.js підхопить його автоматично при наступному install.
//   3. Новий асет, на який НЕМАЄ тегу в shell.html (іконка з
//      manifest.webmanifest, інший файл, на який лише JS посилається
//      рядком) → додай вручну в FALLBACK_SHELL нижче.
const VERSION = 'recall-v64';
const STATIC_CACHE = `recall-static-${VERSION}`;
const RUNTIME_CACHE = `recall-runtime-${VERSION}`;

// Мінімальний ручний список: лише те, що НЕ виявляється парсингом
// shell.html (не має свого <script src>/<link href> — manifest посилається
// на іконки рядками в JSON, а не HTML-тегами).
const FALLBACK_SHELL = [
    '/',
    '/static/favicon.svg',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/manifest.webmanifest',
];

// Виводить повний app-shell зі свіжої розмітки '/' — так APP_SHELL більше
// НЕ дублюється вручну проти <script>/<link> тегів у shell.html (це було
// третє місце ручної синхронізації версій, окрім VERSION і самого
// changelog). Якщо фетч/парсинг не вдався — деградуємо на FALLBACK_SHELL,
// щоб install() у гіршому разі закешував хоча б '/' і критичні іконки.
async function deriveAppShellUrls() {
    try {
        const res = await fetch('/', { cache: 'no-store' });
        if (!res.ok) return FALLBACK_SHELL;
        const html = await res.text();
        const urls = new Set(FALLBACK_SHELL);
        const re = /<(?:script[^>]+src|link[^>]+href)=["']([^"']+)["']/gi;
        let m;
        while ((m = re.exec(html))) {
            const u = m[1];
            // Лише локальні /static/* — CDN (fonts/FontAwesome) свідомо НЕ
            // precache'иться тут, вони йдуть через cache-first у fetch-хендлері.
            if (u.startsWith('/static/')) urls.add(u);
        }
        return Array.from(urls);
    } catch (err) {
        console.warn('[sw] deriveAppShellUrls failed, using fallback', err.message);
        return FALLBACK_SHELL;
    }
}

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(STATIC_CACHE);
        const shellUrls = await deriveAppShellUrls();
        // Кожен ассет окремо, щоб 1 невдача не похоронила весь install.
        await Promise.all(shellUrls.map((url) =>
            cache.add(url).catch((err) => console.warn('[sw] precache skip', url, err.message))
        ));
        // НЕ викликаємо self.skipWaiting() тут — новий воркер лишається
        // "waiting", доки клієнт explicit не попросить (SKIP_WAITING
        // повідомлення з shell.js, після кліку «Оновити» у toast'і). Так
        // відкрита вкладка ніколи не переключається на нові модулі посеред
        // сесії без відома користувача.
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        // Видалити старі версії кешу (попередні VERSION).
        const keys = await caches.keys();
        await Promise.all(keys
            .filter((k) => k.startsWith('recall-') && k !== STATIC_CACHE && k !== RUNTIME_CACHE)
            .map((k) => caches.delete(k)));
        // Взяти контроль над усіма tab'ами одразу (без reload).
        await self.clients.claim();
    })());
});

self.addEventListener('fetch', (event) => {
    const req = event.request;

    // Не-GET: лиш network. POST/PATCH/DELETE завжди йдуть на сервер.
    if (req.method !== 'GET') return;

    const url = new URL(req.url);
    const accept = req.headers.get('Accept') || '';

    // SSE: ніколи не кешуємо (стрім).
    if (accept.includes('text/event-stream') || url.pathname.includes('/stream')) return;

    // CDN (інший origin) — cache-first. Версіоновані URL'и стабільні.
    if (url.origin !== self.location.origin) {
        event.respondWith(cacheFirst(req, RUNTIME_CACHE));
        return;
    }

    // Локальна статика — stale-while-revalidate.
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(staleWhileRevalidate(req, STATIC_CACHE));
        return;
    }

    // HTML-навігація (корінь чи прямий вхід у /something) — network-first з fallback'ом на '/'.
    if (req.mode === 'navigate' || accept.includes('text/html')) {
        event.respondWith(networkFirstFallback(req, STATIC_CACHE, '/'));
        return;
    }

    // /api/* GET — network-first з кеш-fallback'ом (бачимо stale дані офлайн).
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(networkFirstFallback(req, RUNTIME_CACHE, null));
        return;
    }

    // Все інше — network passthrough.
});

// ============ helpers ============

async function cacheFirst(req, cacheName) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(req);
    if (cached) return cached;
    try {
        const resp = await fetch(req);
        if (resp.ok) cache.put(req, resp.clone());
        return resp;
    } catch (_) {
        return new Response('offline', { status: 503, statusText: 'Offline' });
    }
}

async function staleWhileRevalidate(req, cacheName) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(req);
    const networkPromise = fetch(req).then((resp) => {
        if (resp && resp.ok) cache.put(req, resp.clone());
        return resp;
    }).catch(() => null);
    return cached || (await networkPromise) || new Response('offline', { status: 503 });
}

async function networkFirstFallback(req, cacheName, fallbackUrl) {
    const cache = await caches.open(cacheName);
    try {
        const resp = await fetch(req);
        // Не кешуємо помилкові відповіді (4xx/5xx).
        if (resp && resp.ok) cache.put(req, resp.clone());
        return resp;
    } catch (_) {
        const cached = await cache.match(req);
        if (cached) return cached;
        if (fallbackUrl) {
            const fb = await cache.match(fallbackUrl);
            if (fb) return fb;
        }
        return new Response('Ви офлайн і ця сторінка ще не кешована',
            { status: 503, statusText: 'Offline', headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
    }
}

// Дозволяє сторінці попросити нову версію SW активуватись миттєво.
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});
