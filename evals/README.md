# evals/ — recall@k / citation-rate харнес для retrieval і RAG

**Навіщо.** Пороги гібридного retrieval (`app/services/retrieval.py`:
`RAG_RECENCY_WEIGHT`, `RAG_MAX_PER_MEETING`, RRF) і промпт RAG-чату
(`app/services/rag.py`) підбирались за ручними прогонами. Без відтворюваного
харнесу будь-яка зміна промпта, чанкінгу, моделі чи ваг — і ви дізнаєтесь про
регрес зі скарг користувача, а не з числа. Цей каталог — не заміна повноцінних
evals-платформ, а мінімальний скрипт «прогнати N питань → порівняти до/після».

## Структура

```
evals/
  __init__.py
  metrics.py              — ДЕТЕРМІНОВАНА математика метрик (recall@k, citation-rate).
                             Без мережі/GPU/БД — тому тестується офлайн
                             (tests/test_eval_metrics.py).
  run_eval.py              — CLI: ганяє golden-set проти реального архіву
                             (retrieval.search + rag.answer_question) і друкує таблицю.
  golden_set.example.json  — ШАБЛОН golden-set із синтетичними записами
                             (НЕ реальні транскрипти) — скопіюйте й адаптуйте.
  README.md                — цей файл.
```

## Формат golden-set

JSON-об'єкт з ключем `"items"` (список) або просто список записів. Кожен
запис:

```json
{
  "id": "budget-decision-01",
  "question": "Яке рішення ухвалили щодо бюджету на маркетинг у 2 кварталі?",
  "category_id": null,
  "expected_transcription_ids": [101],
  "expected_source_name_contains": ["Синк з маркетингом"],
  "expected_facts": [
    "бюджет на маркетинг Q2 збільшили до 50 000 грн",
    "відповідальна за виконання — Олена"
  ],
  "notes": "людський коментар — чому це хороший test-case"
}
```

Усі поля крім `id`/`question` — опційні; кожна метрика в агрегації рахується
лише по записах, де відповідний критерій заданий (порожній список = критерій
пропускається, а не провал).

| Поле | Навіщо |
|---|---|
| `expected_transcription_ids` | Точні ID транскриптів-джерел → recall@k. **Крихкі**: змінюються при переінджесті архіву. |
| `expected_source_name_contains` | Стійкіша альтернатива — підрядок `source_name`. Не залежить від ID. |
| `expected_facts` | Факти, які МАЮТЬ бути у відповіді. Автоматично НЕ перевіряються (без LLM-judge) — призначені для ручного eyeball-огляду `--json-out` і для промпту LLM-judge (`--llm-judge`). |
| `category_id` | Обмежити пошук напрямком (int / `"none"` / `null`) — як параметр `retrieval.search`. |

Негативні кейси (свідомо немає релевантних джерел в архіві) — лишіть
`expected_transcription_ids`/`expected_source_name_contains` порожніми
списками; вони випадуть з recall@k-агрегації, але `--json-out` дозволить
вручну перевірити, що модель чесно каже «не знайдено», а не галюцинує.

Дивіться `golden_set.example.json` — три синтетичні приклади (звичайний
кейс з одним джерелом, кейс із кількома очікуваними джерелами, негативний
кейс).

## Як зібрати ВЛАСНИЙ golden-set (по своєму архіву)

1. Скопіюйте `golden_set.example.json` → `golden_set.local.json` (або будь-яку
   назву поза git — див. «Приватність» нижче).
2. Візьміть 15-30 реальних питань, які ви (чи команда) справді задавали б
   архіву — включно з кількома, де свідомо НЕМАЄ відповіді (негативні кейси
   ловлять галюцинації).
3. Для кожного питання вручну знайдіть у своєму архіві (UI «Запитай архів»
   або SQL) правильний(і) `transcription_id` і/або назву джерела.
4. Опційно — випишіть 1-3 факти, які відповідь МАЄ містити (`expected_facts`)
   для LLM-judge/ручного огляду.
5. Прогоняйте `run_eval.py` на цьому файлі — див. нижче.

**Приватність.** `golden_set.local.json` (чи будь-яка ваша копія з реальними
питаннями/ID з вашого архіву) містить дані про ваші реальні мітинги —
**НЕ комітьте її**. У репозиторії лишається лише синтетичний
`golden_set.example.json`. Додайте свій файл у `.gitignore` (наприклад
`evals/*.local.json`), якщо плануєте тримати кілька версій.

## Коли ганяти

- **Перед релізом** — regression-перевірка, що останні зміни не погіршили
  recall@k / citation-rate відносно попереднього прогону.
- **При зміні** `app/services/retrieval.py` (ваги RRF, recency, диверсифікація),
  `app/services/rag.py` (системний промпт, чанкінг), або дефолтної моделі
  (`app/services/models.py`) — прогнати ДО і ПІСЛЯ, порівняти `--json-out`.
- **НЕ на кожен коміт у CI** — `--retrieval-only` безкоштовний і не потребує
  GPU/ключа (окрім e5-large embeddings, якщо доступні локально — інакше
  автоматичний FTS-фолбек), але повний прогін з відповідями і особливо
  `--llm-judge` коштують реальних токенів Claude. Ганяйте вручну/за потреби,
  не вбудовуйте у обов'язковий pre-commit чи CI-гейт.

## Запуск

```bash
# 1) Тільки retrieval — БЕЗКОШТОВНО, без ANTHROPIC_API_KEY.
#    Перевіряє лише recall@k / source_name_hit_rate (без citation-rate,
#    бо немає відповіді).
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json \
    --db whisper_history.db \
    --retrieval-only

# 2) Повний прогін: retrieval + генерація відповіді через rag.answer_question
#    (реальний виклик Claude API — ПЛАТНО, потребує ANTHROPIC_API_KEY у .env).
#    Додає citation_rate / citations_valid_rate.
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json \
    --db whisper_history.db \
    --json-out evals/last_run.json

# 3) + LLM-judge релевантності — ДОДАТКОВИЙ виклик Claude на КОЖНЕ питання
#    (ще платніше). Вимкнено за замовчуванням, вмикається явно прапорцем.
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json \
    --llm-judge

# 4) T6.4: до/після локального cross-encoder rerank (bge-reranker-v2-m3,
#    БЕЗКОШТОВНО, GPU/CPU — lazy-load моделі при першому виклику). Прогнати
#    ОДИН І ТОЙ САМИЙ golden-set ДВІЧІ (без і з --rerank), звірити
#    recall@k/source_name_hit_rate/citation_rate:
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json --db whisper_history.db \
    --retrieval-only --json-out evals/before_rerank.json
.venv/Scripts/python.exe -m evals.run_eval \
    --golden evals/golden_set.local.json --db whisper_history.db \
    --retrieval-only --rerank --json-out evals/after_rerank.json
```

Прапорці: `--top-k` (default 8, як `retrieval.search`), `--model` (override
дефолтної моделі з `app/services/models.py`), `--category-id` немає окремим
прапорцем — задається ПО ЗАПИСУ golden-set (`"category_id"`), бо різні
питання можуть стосуватись різних напрямків. `--rerank` (T6.4) — увімкнути
локальний cross-encoder rerank для цього прогону: з `--retrieval-only`
передається напряму у `retrieval.search(rerank=True)`; без нього виставляє
`RECALL_RERANK_ENABLED=1` перед імпортом `app.services.rag`, тож і повний
прогін (`rag.answer_question`) реранжує — той самий шлях, що продакшн
RAG-чат (`app/services/rag.py`).

## Інтерпретація метрик

| Метрика | Що означає | Джерело |
|---|---|---|
| `recall@k` | Середня частка `expected_transcription_ids`, що знайшлися у top-k retrieved (по записах, де це поле задане) | `retrieval.search` |
| `source_name_hit_rate` | Частка записів, де серед top-k знайшлось джерело з очікуваною назвою (підрядок) | `retrieval.search` |
| `citation_rate` | Частка відповідей, що містять хоча б одне цитування `[n]` (вимагається системним промптом RAG) | `rag.answer_question` |
| `citations_valid_rate` | Частка відповідей, де ВСІ цитати `[n]` посилаються на реально надані фрагменти (1..found) — ловить "галюцинований" номер джерела | `rag.answer_question` |
| `llm_judge_score` (опційно) | Середня оцінка 1-5 від Claude-судді (релевантність + покриття `expected_facts`) | окремий виклик Claude, `--llm-judge` |

Порівнюйте ДО/ПІСЛЯ через `--json-out` (два файли, diff вручну або власним
скриптом) — цей харнес навмисно не робить автоматичний regression-gate
(поріг "X% просідання = fail"), бо на малому golden-set (15-30 питань) такий
поріг легко хибно спрацьовує. Дивіться таблицю по кожному запису (`per_item`
у JSON) — там, де `recall_at_k` впав до 0 або `citations_valid` стало
`false`, там і копайте.

## Детермінована частина без API/GPU

`evals/metrics.py` — чисті функції (`recall_at_k`, `source_name_hit`,
`has_citation`, `citations_in_range`, `evaluate_item`, `aggregate`), без
жодного мережевого виклику чи звернення до БД. Покрито
`tests/test_eval_metrics.py` на синтетичних даних (списки словників-чанків,
рядки-відповіді) — жодного реального `ANTHROPIC_API_KEY` чи GPU не потрібно.
Це та частина, яку ЛЕГКО й ПОТРІБНО тримати в звичайному `pytest`-прогоні;
`run_eval.py` (реальні виклики retrieval/RAG) — окремо, ручний запуск.
