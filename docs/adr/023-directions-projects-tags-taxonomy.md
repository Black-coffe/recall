# ADR-023: Двошарова таксономія (напрямок → проєкт) і теги — один резолвер, одна таблиця, точковий re-embed через версію ембедингів

- Status: proposed
- Date: 2026-09-22
- Spec: docs/specs/directions-projects-tags

## Context

Рішення власника з грилю (brief.md `## Answers`) вже зафіксовані і тут не переглядаються:
проєкт — другий рівень тієї самої таблиці `categories` через `parent_id`; теги — вручну +
підказка Claude при збагаченні + успадкування від TG-чату; розбір фрази робить агент через
MCP; зміна напрямку/проєкту/тегів → точковий re-embed, префікс чанка отримує
«напрямок → проєкт → теги» для всіх типів записів.

Цей ADR закриває **як саме**. Сили, що тиснуть (перевірено по коду 22.09.2026):

- Фільтр за `category_id` живе у ~15 місцях без спільного хелпера: `retrieval.py:227-232,
  281-287` (два рази — vector і FTS), `research.py:136-140`, `commitments.py:927-929`,
  `audio_library.py:160-162` (через COALESCE-підзапит `cat_subq`), `/api/history`,
  `/api/memory/entities`, `/api/memory/action-items`, `/api/telegram/chats`, а в
  `mcp_server.py` — прямий SQL у `list_recent` (`:603-604`), `list_categories` (`:663`),
  `search_history`. HTTP-гілка і direct-DB тулзи MCP не ділять код. Парсер `_parse_category`
  скопійований двічі (`memory.py:72`, `research.py:96`).
- `retrieval.search` приймає одне `category_id`; `scope_tids` — «другий шар», який
  навмисно не знає про граф (`retrieval.py:196-199`).
- Нормалізація імен: `memory.py:_norm_cat` (casefold + стиск пробілів) і
  `scope.py:_norm_name` (NFKC + casefold) — два різні ключі до однієї колонки `name_norm`.
- Хук re-embed (`record_meta.after_meta_update` → `reembed.schedule_record_reembed`)
  викликається лише з PATCH назви/опису. Зміна `category_id` — одиночна (`memory.py:245`),
  bulk (`:277/:280`), merge (`:59-66`), `scope.apply_chat_categories` (`:172-176`, один
  UPDATE на тисячі рядків), `scope.label_uncategorized` (`:340`) — вектори не оновлює.
  `_null_category_refs`/`_reassign_category_refs` не чіпають `audio_downloads.category_id`.
- Префікс чанка (`embeddings.build_context_prefix:630-641`) кладе назву напрямку **лише
  для дзвінків**; TG отримує нитку, документ — сторінку. `_load_prefix_meta:668-678` —
  один JOIN на `categories`, тегів немає.
- Масовий re-embed кришить FTS5 (`chunks_fts optimize` наприкінці — вже в `reembed.run`);
  ADR-022 — точковий re-embed відкладається на час живого запису.
- `ask_archive(project=…)` і `scope.scope_filter_ids` вже означають «сутність графа /
  вільний текст», у графі 1092 сутності типу `project`. Новий рівень ієрархії теж зветься
  «проєкт».
- Golden-set (`evals/golden_io.py:60-72`) знає лише `category_id`; гейт (`gate.py:228-237`)
  передає його у `retrieval.search` як є.
- Живі числа: 29 категорій, «Фонд Робота» записів (≈більша частина архіву), решта — хвіст.

## Options

### A. Розкриття «напрямок = він сам + його проєкти»

1. Рекурсивний CTE / `IN (SELECT …)` руками в кожному місці — нуль нових модулів, але
   15 копій одного підзапиту у двох процесах, що не діляться кодом; розʼїдуться на першій
   правці (та сама пастка, що з `lower()` у кириличному grep).
2. **Один SQL-only модуль `app/services/categories.py` з функцією-фрагментом, яку кличе
   кожне місце фільтра** — ті самі 15 місць правляться один раз (заміна `col = ?` на виклик),
   далі семантика живе в одній функції; імпортується і з `mcp_server.py` (без torch).
3. Денормалізована `root_category_id` на 4 таблицях — швидкий фільтр, але кожен reparent/
   merge/переїзд мусить синхронізувати колонку в 4 таблицях; дорога скасувати, а виграшу у
   швидкості на SQLite з 6 тис. записів немає.

### B. Схема тегів

1. Тег як рядок у `entities` типу `topic` — таблиця вже є, але там 2663 шумних тем, ручного
   контролю словника немає, а `meeting_entities` існує лише для збагачених (~16 %) записів.
2. **Окремі таблиці `tags` + `record_tags` + `tg_chat_tags`; кріплення лише до
   `transcriptions`; підказки Claude — окрема колонка `suggested_tags_json`, у `record_tags`
   тільки підтверджене** — контрольований словник, фільтри без умов на статус.
3. JSON-масив тегів у колонці `transcriptions.tags_json` — найменше таблиць, але фільтр
   «тег AI» стає `LIKE`/`json_each` у 15 місцях, а перейменування тегу — прохід по всіх рядках.

### C. Зіткнення назв

1. Новий параметр `project_id` поруч із наявним `project` — два параметри в одне слово, агент
   плутатиме гарантовано.
2. **`category_id` приймає id будь-якого вузла (напрямок або проєкт), нового параметра немає;
   `project` лишається сутністю графа/вільним текстом** — жоден наявний виклик не ламається.
3. Перейменувати `project` → `entity` зі зворотним аліасом — чистіше, але це правка контракту
   9 тулзів заради слова, і `ask_log`/golden-set уже містять `project`.

### D. Re-embed і префікс

1. Точковий хук на кожен запис при кожному UPDATE — на переїзді 4051 TG-записів це 4051
   постановка в чергу і 4051 DELETE+INSERT по FTS без `optimize`.
2. **Хук лише для «поточної пари» ембедингів + батч-задача для масових змін + сама зміна
   формату префікса ходить як нова епоха `EMBED_VERSION` (номер — у `.env`, строго вище
   бойової пари) через наявний `reembed.run`** — масову
   перебудову робить механізм, який уже вміє optimize в кінці та знімок→гейт→бойова.
3. Спеціальний прапорець «міграція, хуки вимкнені» — зайвий стан, який хтось забуде зняти.

## Decision

### A. Один резолвер — `app/services/categories.py`

Новий модуль **без torch/anthropic на рівні імпорту** (памʼятка `mcp-stdio-no-heavy-models`),
куди переїжджають і наявні `_norm_cat`/`_norm_name`, `_parse_category`,
`_null_category_refs`/`_reassign_category_refs`:

```python
def parse_filter(raw) -> int | str | None            # '5'→5, 'none'→'none', ''/'all'/None→None
def filter_sql(column: str, value) -> tuple[str, list]
    # None  → ("", [])
    # 'none'→ (f" AND {column} IS NULL", [])
    # int   → (f" AND {column} IN (SELECT id FROM categories WHERE id = ? OR parent_id = ?)", [v, v])
def norm_name(name: str) -> str                      # NFKC + strip + стиск пробілів + casefold
def subtree_ids(conn, category_id: int) -> list[int]  # [self, *children] — для UI-лічильників і тестів
```

Дерево має **рівно два рівні**: `parent_id` може вказувати лише на вузол із `parent_id IS
NULL` (перевірка в PATCH/POST категорії, 400 інакше). Тому `id = ? OR parent_id = ?` — це
повне розкриття без CTE; для проєкта (листка) той самий фрагмент повертає лише його. `column`
— будь-який вираз, у т.ч. `cat_subq` з `audio_library.py`.

Всі 15 місць фільтра (список у Context) замінюють свою гілку `= ? / IS NULL` на
`filter_sql(...)`. `retrieval.search` **зберігає сигнатуру** `category_id` — семантика
«вузол + його проєкти» реалізується всередині `_vector_search`/`_fts_search` тим самим
хелпером, тож HTTP-проксі, `gate.py`, `copilot/dispatcher` і MCP-проксі не правляться.
`scope.scope_filter_ids:438-441` (збіг назви з категорією) шукає вузол за `name_norm` і далі
теж кличе `filter_sql`. Обидва старі нормалізатори стають імпортом `categories.norm_name`
(єдина різниця — NFKC + стиск пробілів разом; для наявних 29 імен ключ не змінюється,
`name_norm` не перераховуємо).

Відкинуто: (1) — 15 копій у двох процесах; (3) — синхронізація 4 таблиць при кожному
reparent, дороге скасування.

### B. Теги

Міграція **v45** (аддитивна, `PRAGMA table_info` перед кожним кроком):

```sql
ALTER TABLE categories ADD COLUMN parent_id INTEGER;            -- NULL = напрямок
CREATE INDEX idx_categories_parent ON categories(parent_id);
CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                   name_norm TEXT NOT NULL UNIQUE, color TEXT,
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE record_tags (transcription_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
                          source TEXT NOT NULL DEFAULT 'manual',   -- manual | claude | chat
                          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                          PRIMARY KEY (transcription_id, tag_id));
CREATE INDEX idx_record_tags_tag ON record_tags(tag_id);
CREATE TABLE tg_chat_tags (chat_id INTEGER NOT NULL, tag_id INTEGER NOT NULL,
                           PRIMARY KEY (chat_id, tag_id));
ALTER TABLE transcriptions ADD COLUMN suggested_tags_json TEXT;   -- підказки Claude, ще не прийняті
```

- **Кріплення — лише `transcriptions`.** Аудіо в Медіатеці показує теги свого останнього
  транскрипту тим самим COALESCE-шляхом, що й напрямок (`audio_library.py:132-136`);
  нетранскрибований запис тегів не має. Копілот-сесія тегів не має.
- **Підказки живуть окремо від істини.** При збагаченні (`enrichment._persist_card`) Claude
  отримує наявний словник тегів (як `_ask_claude_category` отримує список напрямків) і
  повертає ≤5 назв → `suggested_tags_json`. Прийняття одним кліком переносить у
  `record_tags` із `source='claude'`; фільтри читають **тільки** `record_tags`, тому жодне
  місце фільтра не знає про статус підказки.
- **`source` потрібен для відвʼязування.** Зняття тега з чату видаляє лише рядки
  `source='chat'` цього чату; ручні лишаються (те саме правило «один слот — один
  власник», що в `entity-graph-provenance`). Привʼязка тега до чату — ретроактивно, як
  `apply_chat_categories`; новий інжест TG копіює `tg_chat_tags` чату у `record_tags`
  поруч із успадкуванням `category_id`.
- Кілька тегів у фільтрі — **OR** (будь-який із); перетин із `category_id` — AND. Це «обʼєднати
  тегами» з брифу; перетин тегів не просили.
- **Колір є, опису/іконки немає.** Колір — бо чіп `.rc-chip` + `.rc-cat__dot` уже так
  малюється; опис — четверте вільне поле, яке ніхто не читатиме.
- **Теги ортогональні дереву.** Видалення/злиття напрямку `record_tags` не чіпає взагалі.
  Видалення тега каскадує в app-коді (`record_tags`, `tg_chat_tags`); злиття тегів — `INSERT OR
  IGNORE` + DELETE, як у категорій.

Відкинуто: (1) `entities.topic` — шумний словник і покриття 16 %; (3) JSON-колонка —
`LIKE` у 15 місцях.

### C. Іменування

- **`category_id` — id будь-якого вузла** (напрямок або проєкт) у HTTP і MCP. Нового
  параметра `project_id` немає. `list_categories` (HTTP і MCP) віддає дерево: `id, name,
  slug, color, icon, parent_id, level ('direction'|'project'), items` (власні) і
  `items_total` (піддерево); MCP-версія перестає губити color/icon.
- **`project` не перейменовуємо** і не змінюємо семантику: сутність графа ∪ текстова згадка ∪
  однойменний вузол дерева (з проєктами). Раз назва проєкта тепер ще й вузол — `project="NOVA"`
  чесно захоплює і піддерево, це задокументована частина обʼєднання.
- Нові параметри там, де вже є `category_id` (`search_archive`, `list_recent`,
  `list_action_items`, `ask_archive`, `research_*`, `search_history`, `grep_archive` — цьому
  ще й `category_id`): `tags: Optional[str]` (назви через кому, як `project`), `since`/`until`
  (ISO-дата, по `meeting_date`). Розбір «за тиждень» — робить агент, Recall дат не парсить.
- **Текст для docstring кожного тулза з обома параметрами (дослівно):**

  > `category_id` — id **напрямку або проєкту** з `list_categories`; напрямок автоматично
  > включає всі свої проєкти, проєкт — лише себе. Це рівень ієрархії архіву.
  > `project` — **не** проєкт ієрархії, а назва сутності графа або вільний текст («Ковальчука»,
  > «Адам», бренд): звужує до записів, де ця назва згадана. Щоб звузити до проєкту як розділу
  > архіву — передай його id у `category_id`, не назву у `project`.
  > `tags` — назви тегів через кому; кілька = будь-який із них. Поєднання з `category_id` —
  > перетин («тег AI лише в цьому напрямку»).

Відкинуто: (1) `project_id` — плутанина гарантована; (3) перейменування `project` — правка
9 контрактів і `ask_log` заради слова.

### D. Re-embed і префікс

- **Формат префікса змінюється для всіх типів → це нова епоха ембедингів.** Хвіст рядка 1
  стає `Напрямок → Проєкт` (або лише напрямок) **для всіх типів**, далі типоспецифічне
  (`нитка: …` / `стор. N`), далі `теги: a, b, c` (≤5 тегів, ≤60 символів — стеля, як у
  `_PREFIX_DESCRIPTION_MAX`). `_load_prefix_meta` — self-JOIN `categories parent`
  + `GROUP_CONCAT` по `record_tags`. Масову перебудову архіву робить **наявний**
  `reembed.run` (знімок на старій епосі → знімок на новій → `evals.gate` → `evals.compare`
  → бойова з `--yes-live`), він уже кличе `optimize_chunk_index` один раз наприкінці.
  Нічого нового для масового шляху не пишемо.
- **Номер епохи — акт конфігурації, не правка коду.** Читається з БД, ставиться в `.env`:
  `SELECT embedding_model, embedding_version, COUNT(*) FROM transcriptions GROUP BY 1, 2`
  — станом на 22.09.2026 бойова тримає `(intfloat/multilingual-e5-large, 4)` на живих даних (+7 на старій 2), тож **бойова 4 → 5 у `.env`** (крок 1 рунбука нижче). Номер
  беруть **строго вище** будь-якої пари, що вже є в БД. **ADR-016 дотримано: дефолт коду не
  змінюється** — `embeddings.EMBED_VERSION` і реєстр `app/core/settings.py` лишаються на `2`,
  епохи вище бойова проходила лише рядком `.env`. **ADR-017: пін розділяє формати лише за
  нового номера** — під номером, який у БД уже є, новий префікс ляже в стару епоху,
  `reembed.run --dry-run` покаже 0 stale, а точкові хуки тихо змішають два формати в одній
  вибірці `retrieval.search`.
- **Точковий хук фільтрує за парою.** `reembed.schedule_records_reembed(ids, *, db_path,
  reason)` — новий батч-варіант поверх `schedule_record_reembed`: пропускає id, чия пара
  `(embedding_model, embedding_version)` ≠ поточна (їх і так підбере `run`); ≤20 id → по
  одному через наявний точковий шлях (з відкладенням ADR-022); >20 → одна задача
  `reembed_batch`, яка ітерує id, знімає їх із `_pending_ids` на старті кожного, і кличе
  `optimize_chunk_index` один раз наприкінці; відкладення під час запису — те саме
  `_recording_active()` + таймер.
- **Єдині письменники таксономії.** `categories.set_category(conn, ids, cid) -> list[int]`,
  `categories.set_tags(conn, tid, add, remove, source) -> bool`, `categories.reparent(...)`,
  `apply_chat_tags(...)` — повертають id записів, чий префікс змінився; кожен HTTP/CLI-виклик
  після `commit` передає їх у `schedule_records_reembed`. `memory.py:245/277/280`, merge,
  `scope.apply_chat_categories:172-176`, `scope.label_uncategorized:340` переводяться на ці
  функції. Reparent/перейменування вузла змінює префікс усього піддерева — це теж
  `schedule_records_reembed(subtree records)`; у день розкладки всі вони — no-op (пара
  стара), і архів перебудовує один `run`.
- **Гард формату, а не ручний порядок кроків.** Код оголошує `embeddings.PREFIX_FORMAT`
  (факт коду, не налаштування), а БД памʼятає формат КОЖНОЇ епохи —
  `embedding_epochs(model, version, prefix_format)`, міграція **v46**, бекфіл стемплює все,
  що існувало до неї, форматом 1. Збіг номера епохи зі старішим форматом = **відмова
  писати** (ERROR у лозі, запис лишається без чанків і його підбере `run`; `reembed.run`
  відмовляється стартувати, включно з `--dry-run`), а не тихе змішування двох форматів в
  одній вибірці `retrieval.search` (ADR-017). Дефолт `EMBED_VERSION` при цьому не
  рухається — ADR-016 чинний.
- Порядок дня міграції: `EMBED_VERSION=5` в `.env` → **`python -m app.services.reembed
  epoch-check` → exit 0** (`--db` за замовчуванням бойова, режим `mode=ro`; CLI читає
  ОТОЧЕННЯ ПРОЦЕСУ, а не `.env`, тож у тій самій сесії спершу `$env:EMBED_VERSION="5"`) →
  рестарт (накочуються v45+v46) → власник розкладає 29 імен у Налаштуваннях →
  `scope apply-chats --dry-run` → apply → `label-rest --dry-run` →
  apply → `reembed.run` (знімок → гейт → бойова). Забутий `.env` більше не тихий: перший же
  новий транскрипт дасть ERROR і лишиться без чанків, а `epoch-check` — exit 1
  `format_mismatch`. Вікно деградації векторної гілки до FTS —
  тривалість `run` (≈2–3 год на 6 тис. записів при 45 зап/хв); для особистого інструмента
  прийнятно, робиться за один вечір.

Відкинуто: (1) — 4051 постановка в чергу і FTS без optimize; (3) — прапорець, який забудуть.

### E. Міграція і безпека

- v45 — тільки DDL, **жодного переносу даних**; розкладка — руками власника через
  `PATCH /api/memory/categories/<id>` (нове поле `parent_id`) і через `chat_categories.local.json`,
  де рядок чату отримує опційне `"parent": "Фонд Робота"` — `_ensure_categories` створює
  відсутній вузол під ним. Назва вузла унікальна **по всій таблиці** (наявний UNIQUE
  `name_norm`): проєкт «Маркетинг» не може існувати у двох напрямках — свідоме спрощення.

  > **Замінено 2026-09-22 (v47, хвиля 6).** Спрощення не пережило першої ж розкладки живого
  > архіву: «Маркетинг» потрібен і під «Фонд Робота», і під «Фріланс». Унікальність тепер
  > **серед братів** — напрямки між собою, проєкти одного батька між собою; напрямок і чужий
  > проєкт можуть збігатись. Міграція v47 перебудовує `categories` (inline `UNIQUE COLLATE
  > NOCASE` з v15 не знімається ALTER'ом), ставить `idx_categories_parent_name` UNIQUE на
  > `(COALESCE(parent_id, 0), name_norm)` і лишає `slug UNIQUE`; `PRAGMA foreign_keys=OFF` на
  > час перебудови обовʼязковий — з FK увімкненими `DROP TABLE categories` спрацьовує як
  > DELETE і мовчки зануляє `tg_monitored_chats.category_id` (`ON DELETE SET NULL`, v18).
  > Дубль ловить Python-перевірка серед братів ДО запису (`categories.
  > _assert_free_sibling_name`), тож 409 називає напрямок, у якому зайнято, і несе
  > `conflict {id, path}`; індекс лишається останнім рубежем, а не основним контролем.
  >
  > **Канонічна текстова назва вузла — повний шлях «Напрямок → Проєкт»** (роздільник
  > `categories.PATH_SEP`, той самий `→`, що в контекстному префіксі чанка). Його несе
  > поле `path` кожного елемента `list_tree` — HTTP `GET /api/memory/categories` і MCP
  > `list_categories`. Гола назва лишається дозволеним входом, але розвʼязує її ОДИН
  > резолвер — `categories.resolve_node(conn, text)`: один збіг → він, нуль → `None`,
  > кілька → `CategoryError('ambiguous')` з `.candidates` (перелік шляхів).
  >
  > Неоднозначність ніколи не тиха: HTTP віддає 409 `{"error":"ambiguous","candidates":[…]}`
  > (`/api/memory/categories`, `/api/memory/ask`, `/api/memory/ask/stream` — останній
  > розвʼязує `project` ДО генератора, бо зі стріма 409 уже не повернути), MCP
  > `ask_archive` — ноту з тими самими шляхами, карта чатів (`_ensure_categories`) і арбітр
  > (`_ask_claude_category`) пропускають рядок і друкують шляхи у звіті, `_title_matchers`
  > не робить із такої назви сигналу. Ніхто не бере «перший збіг», не обʼєднує піддерева
  > тезок і не віддає порожньо.
  >
  > Рядок карти чатів із `parent` більше не reparent-ить чужий однойменний вузол — створює
  > власний під указаним напрямком. І навпаки: **reparent ставить усе піддерево в точковий
  > re-embed так само, як перейменування** — шлях стоїть у префіксі чанка, тож після
  > переїзду вектори тезок розходяться саме по ньому.
  >
  > **Доповнено 2026-09-22 (хвиля 7, історія 22)** — чотири уточнення, кожне за знахідкою
  > ради раунду 5:
  >
  > 1. **Старшинство резолву.** `resolve_node` спершу шукає ТОЧНИЙ канонічний шлях, і лише
  >    потім голу назву по всіх вузлах. Канонічний шлях напрямку — його гола назва, тож
  >    напрямок-тезка («Маркетинг» поруч із чужим «Фріланс → Маркетинг») досяжний своїм
  >    імʼям, а не `ambiguous` назавжди. Інваріант: `resolve_node(node["path"]) == node["id"]`
  >    для КОЖНОГО елемента `list_tree`, і кожен рядок `.candidates` резолвиться назад у свій
  >    вузол — перелік, який сам себе повертає в `ambiguous`, це глухий кут.
  > 2. **`→` у назві вузла заборонений** (`CategoryError('bad_name')` ДО запису, 400).
  >    Вузол «Фонд → Клієнти» ламає шлях з обох боків: сам не резолвиться за власною назвою,
  >    а канонічний шлях його дитини стає тричастинним (`split_path` рахує рівно дві частини).
  > 3. **Карта чатів резолвить рядок ПАРОЮ `(parent, category)`**, не голою назвою: два рядки
  >    «Маркетинг» під різними напрямками — це два вузли і дві привʼязки. `--dry-run` перелічує
  >    ВСІ вузли, які створить реальний прохід (план рахується з того самого резолву, не
  >    звіркою назв із усією таблицею «до»), кожен зі шляхом і нотою «тезка існує: …».
  > 4. **FK-гард v47 — захисний.** Виміряно: міграцію ганяє сирий `sqlite3.connect` в
  >    `init_database`, де `PRAGMA foreign_keys` = 0; `foreign_keys=ON` вмикає інше зʼєднання
  >    (`app/db/connection.py:40`). Тому `PRAGMA foreign_keys=OFF` у v47 нічого не несе на
  >    чинному шляху і тримається на випадок виклику на FK-ON зʼєднанні; сама пастка доведена
  >    тестом на зібраному руками FK-ON зʼєднанні. Слово «обовʼязковий» вище читати як
  >    «обовʼязковий для будь-якого зʼєднання, де FK увімкнені».
  >
  > **Доповнено 2026-09-22 (хвиля 8, історія 26)** — за радою раунду 6:
  >
  > 1. **`--dry-run` карти чатів — той самий писальний шлях із відкотом**, а не окремо
  >    виведений план. Три раунди поспіль окрема гілка розходилась із реальним проходом
  >    (нота `namesake`, порядок рядків). Письменники `create_category`, `set_category`,
  >    `apply_chat_tags`, `ensure_tag` мають ключ `commit=False`; `apply_chat_categories`
  >    виконує їх на одному зʼєднанні й закінчує `commit()` або `rollback()` (re-embed — лише
  >    в реальному). `categories_created` — журнал фактичних створень проходу. Відкинуто:
  >    проксі-зʼєднання з no-op `commit` (прихована магія) і копію БД `VACUUM INTO` на кожне
  >    превʼю. Резолв детермінований: спершу рядки з `parent`, потім голі — по дереву, що вже
  >    містить ці створення. `ambiguous` ключується парою рядка: рядок із `parent`
  >    неоднозначним не буває.
  > 2. **Правило `→` безумовне** — `update_category` перевіряє його і при reparent без
  >    перейменування (звичайний reparent лишається дозволеним).
  > 3. **Два входи — два правила, свідомо.** *Адресація* — людина чи тулз передає скоуп
  >    (`resolve_node`, `scope_filter_ids`, карта чатів): точний канонічний шлях спершу, тож
  >    голе імʼя напрямку = цей напрямок; це «один збіг», а не «напрямок завжди виграє».
  >    *Висновок із вільного тексту* — `_title_matchers`, згадка в заголовку, яку автор не
  >    адресував: назви з тезками пропускаються зовсім.
- `_null_category_refs`/`_reassign_category_refs` переїжджають у `categories.py` і
  **закривають дірку** з `audio_downloads.category_id` (той самий цикл по таблицях).
- **Видалення/злиття вузла з дітьми — відмова (409)**, поки проєкти не перенесені або не
  видалені окремо. Відкинуто каскад (несподіване зникнення проєктів) і тихий підйом дітей у
  корінь (ховає помилку).
- **Приори k-NN не агрегуємо.** `categorize.suggest_category` голосує по `category_id` сусіда
  як є — вузол може бути напрямком чи проєктом, і саме проєкт власник хоче бачити в підказці;
  `_prior_correct` рахує частки по вузлах як зараз — після переїзду записів у проєкти «Фонд»
  зменшиться, і перекос гіганта, який цей коефіцієнт душить, зменшиться разом із ним.
  Змінюються лише два входи: `_title_matchers` уже читає всі вузли (проєкти потрапляють
  автоматично), а арбітр `_ask_claude_category` отримує список у вигляді шляхів
  «Напрямок → Проєкт», щоб обирати проєкт, а не лише корінь.
- `_prime_system_prompt` MCP і `catOptions()` фронта отримують те саме дерево з
  `list_categories` (optgroup напрямок → проєкти з відступом); `research.js` переходить на
  `catOptions` замість ручної збірки.

### F. Мірило

Golden-set отримує два опційні поля: `tags: list[str]` і `expected_leak: 0` (константа,
пояснення нижче). `gate._search` передає `tags=` у `retrieval.search`. Гейт для кожного
пункту з `category_id` або `tags` додатково рахує **`filter_leak`** — кількість чанків у
видачі, чий запис не лежить у піддереві `category_id` або не має жодного з `tags`; будь-який
пункт із `filter_leak > 0` — FAIL незалежно від recall. Це доводить, що фільтр не пропускає
чуже, без жодної ручної розмітки; `recall_at_k` на тих самих пунктах доводить, що він не
ховає своє. Мінімальний набір у `golden_set.local.json`: ≥3 пункти, де очікуваний запис
лежить у **проєкті**, а `category_id` — його **напрямок** (доказ розкриття); ≥3 пункти з
`tags`; ≥2 з `tags` + `category_id`. `slice` не розширюємо; `compare.py` не чіпаємо.

## Consequences

Легше: один модуль тримає семантику фільтра для HTTP і MCP; `retrieval.search`, гейт,
копілот і проксі-тулзи не змінюють сигнатур; масова перебудова векторів — уже перевірений
шлях Волни B. Важче: 15 механічних правок у двох процесах (рутинна, але має бути повною —
пропущене місце виглядатиме як «проєкт зник із фільтра»); вікно деградації до FTS на час
`run`; unique-назва вузла по всій таблиці; вимога «два рівні» перевіряється в app-коді, а не
схемою. Прийнятий борг: `grep_archive` і `archive_grep` досі не фільтрують `duplicate_of`
(відома дірка з wiki) — додаючи туди `category_id`/`tags`, дублі не чіпаємо.

## Invariants created

- **The category tree is exactly two levels deep.** `categories.parent_id` may only point at
  a row whose own `parent_id IS NULL`; the API rejects anything else (400). A node name is
  unique across the whole table (`name_norm`), not per parent.

  > **Замінено 2026-09-22 (v47, хвиля 6).** A node name is unique **among its siblings**
  > (`idx_categories_parent_name` UNIQUE on `(COALESCE(parent_id, 0), name_norm)`), not across
  > the table; `slug` stays globally unique. The two-level rule itself is unchanged. The
  > canonical textual name of a node is its full path «Напрямок → Проєкт» (`path` in
  > `list_tree`); every reader that used to resolve a node by its bare name goes through
  > `categories.resolve_node`, which raises `ambiguous` with the list of paths instead of
  > picking «the first match», merging namesakes or answering empty.
- **Every category filter goes through `app.services.categories.filter_sql`.** A direction
  id means the direction plus all its projects; a project id means the project alone. No
  reader (HTTP, MCP direct-DB, retrieval, research, commitments, copilot) writes its own
  `category_id = ?`; `parse_filter` and `norm_name` are likewise single-sourced there. The
  module imports no torch/anthropic at module level so `mcp_server.py` can import it.
- **`category_id` is the only hierarchy parameter; `project` is never the hierarchy.**
  `project` in `ask_archive`/`scope_filter_ids` stays "graph entity or free text" and is not
  reused, renamed, or shadowed by a `project_id`.
- **Tags attach to `transcriptions` only; `record_tags` holds confirmed tags only.** Claude
  suggestions live in `transcriptions.suggested_tags_json` until accepted. `record_tags.source`
  records who set the tag (`manual|claude|chat`); unbinding a tag from a chat removes only
  rows with `source='chat'` for that chat. Deleting or merging a category never touches
  `record_tags`.
- **Every writer of `transcriptions.category_id` or `record_tags` returns the changed ids and
  schedules `reembed.schedule_records_reembed` after commit.** No raw
  `UPDATE transcriptions SET category_id` outside `app.services.categories`.
- **Point re-embed only touches records on the current `(embedding_model, embedding_version)`
  pair.** Stale records belong to `reembed.run`, which re-embeds them with current meta anyway;
  a batch of more than 20 ids runs as one job and calls `optimize_chunk_index` once at the end.
- **The context prefix carries `Напрямок → Проєкт` for every source type, then the
  type-specific tail, then `теги: …` (≤5 tags, ≤60 chars).** A change to the prefix format is
  a new `EMBED_VERSION`, rebuilt through `reembed.run` (snapshot → gate → live), never through
  per-record hooks.
- **Deleting or merging a category that still has projects is refused (409).** Reassign or
  delete the projects first; `audio_downloads.category_id` is nulled/moved together with the
  other three tables.
- **A filtered eval item with `filter_leak > 0` fails regardless of recall.** Any golden item
  carrying `category_id` or `tags` proves both that the filter hides nothing of its own
  (recall) and lets nothing foreign through (leak).

## Revisit when

- A third level is asked for (a project needs sub-projects) — `id = ? OR parent_id = ?`
  stops being a full expansion and the two-level check becomes a recursive CTE.
- The same project name is needed under two directions — the table-wide `name_norm` UNIQUE
  must become `(parent_id, name_norm)`.

  > **Настало 2026-09-22 (v47, хвиля 6).** Зроблено саме так:
  > `idx_categories_parent_name` UNIQUE на `(COALESCE(parent_id, 0), name_norm)`
  > (`COALESCE`, бо в UNIQUE-індексі NULL-и не конфліктують — напрямки інакше лишились би
  > без унікальності між собою). Наступний перегляд цього пункту — коли знадобиться третій
  > рівень (перший пункт списку).
- Tag filtering needs AND semantics ("AI and інвестори") or tag counts exceed a few hundred —
  the OR-only `tags` parameter and the flat `tags` table without hierarchy get reopened.
- `reembed.run` over the archive takes longer than an evening (archive ≫ 6k records) — the
  degradation window during the `EMBED_VERSION` bump needs a shadow-column strategy instead.
