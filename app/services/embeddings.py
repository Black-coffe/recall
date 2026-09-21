"""Локальні embeddings + чанкінг (Phase 13B).

Семантичний шар RAG-архіву. Транскрипт ріжеться на чанки (вікна по ходах
спікера з перекриттям), кожен кодується локальною мультимовною моделлю
(дефолт — multilingual-e5-large на RTX 3090, 1024-dim; конфігурується через
EMBED_MODEL/EMBED_VERSION, стиль префіксів запиту/пасажу — за родиною моделі,
див. _style_for()) у float32-вектор → зберігається BLOB у таблиці chunks.
(Чому НЕ bge-m3 — див. коментар біля EMBED_MODEL нижче.)

Чому локально, а не хмара: безкоштовно на тисячах чанків, приватно (нічого не
йде назовні), відмінно для UA/RU, GPU вже є. Модель — lazy singleton, тягнеться
з HF при першому використанні (~2.2GB у ~/.cache/huggingface).

Все опціонально: якщо sentence-transformers/torch недоступні — is_available()
=False, embeddings просто вимикаються, FTS5-пошук далі працює.

Волна B (історія 05): кожен чанк несе ще й КОНТЕКСТНИЙ ПРЕФІКС
(build_context_prefix) — тип/назва/дата/спікер/напрямок|нитка|сторінка + рядок
сводки одиниці сенсу. Префікс іде у вектор і в chunks_fts (колонка
chunks.context_prefix, міграція v43), але НЕ в chunks.text — цитати, експорт і
нитки далі бачать лише сказане.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from typing import Optional

import numpy as np

from app.db.connection import get_db_connection


logger = logging.getLogger(__name__)

# Модель (override через env). multilingual-e5-large: 1024-dim, мультимовна,
# відмінна для UA/RU. Чому НЕ bge-m3: bge-m3 має лише pytorch_model.bin (без
# safetensors), а transformers>=4.56 блокує torch.load на torch<2.6 (CVE-2025-32434),
# а наш torch запінено на 2.5.1 (стабільність faster-whisper/ctranslate2). e5-large
# має model.safetensors → вантажиться без проблем. Ціна: ліміт 512 токенів + e5
# потребує query:/passage: префіксів (обробляється нижче через _style_for()).
#
# production-rag-wave-b, історія 04: перехід на Qwen/Qwen3-Embedding-0.6B —
# пара `EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B` + `EMBED_VERSION=3` (обидва в
# .env), відкат — тими ж двома. Дефолти коду НЕ мінялись (Non-goals) — це
# опційний перемикач, не новий дефолт.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-large")
EMBED_DIM = 1024  # дефолт до lazy-завантаження моделі; _apply_model_dim() підміняє на факт
# T6.8: EMBED_VERSION тепер СТРУКТУРНО підключено до idempotency-check у
# chunk_and_embed_transcription() — версія зберігається поряд з embedding_model
# у transcriptions.embedding_version (міграція v28) і звіряється при рішенні
# "чи потрібен re-embed". Раніше константа була декларативною (ніде не
# читалась) — зміна ЛОГІКИ чанкінгу без зміни назви моделі НЕ детектувалась.
#
# v2 (T6.5, 14.08.2026): аудіо-сегменти ріжуться по природних межах (пауза /
# зміна спікера), а не лише по переповненню буфера — нарізка змінилась, отже
# наявні вектори застаріли. Бамп означає: список роботи індексера
# (enrichment.list_unenriched_ids) починає віддавати ВЕСЬ архів на re-embed
# (локально, без Claude — картка при цьому skip'ається за enrichment_version).
#
# EMBED_VERSION тепер конфігурується через env (реєстр app/core/settings.py,
# дефолт лишається 2) — v3 зарезервована для пари з Qwen3-Embedding вище.
EMBED_VERSION = int(os.environ.get("EMBED_VERSION", "2"))

# Стеля довжини послідовності. Потрібна тому, що НЕ кожна модель привозить її
# сама: e5 має sentence_bert_config.json з max_seq_length=512, а
# Qwen3-Embedding-0.6B цього файла не має взагалі — і sentence-transformers
# тоді бере min(config.max_position_embeddings, tokenizer.model_max_length)
# (Transformer.py:98), тобто 32768. Наслідок заміряно 17.09.2026 на re-embed
# знімка: чанки-монстри (у боєвому архіві max 73k символів ≈ 18k токенів —
# документи й TG, ліміт _MAX_CHARS=1000 діє лише на аудіо-шляху) проходили
# всі 28 шарів уваги на повну довжину, кешуючий аллокатор torch тримав пік
# 17.3 ГБ VRAM і не віддавав його, відеопамʼять переповнювалась, WDDM починав
# вивантажувати її в RAM — швидкість впала в 30 разів (45 → 1.5 записи/хв).
#
# 1024, а не 512 «як в e5»: на нашій кирилиці токенізатор Qwen3 дає 2.02
# символи/токен проти 3.57 в e5 (замір на 300 бойових чанках), тож звичайний
# чанк — p50=285, p95=471, max=551 токенів. На 512 обрізало б уже нормальні
# чанки. На 1024 звичайний чанк входить цілком, а монстри ріжуться на ~2070
# символах проти ~1830 в e5 — тобто СИМВОЛІВ обидві моделі читають майже
# порівну, і різниця в eval лишається різницею моделей, а не глибини зрізу.
# Застосовується лише як СТЕЛЯ: модель зі своїм меншим лімітом (e5 — 512)
# не чіпається.
EMBED_MAX_SEQ = int(os.environ.get("EMBED_MAX_SEQ", "1024"))


def _style_for(model_name: str) -> str:
    """Стиль префіксів запиту/пасажу за РОДИНОЮ моделі (визначається з назви
    EMBED_MODEL — окремої env-змінної для стилю немає навмисно, родина й
    назва моделі це одне й те саме рішення).

    - ``e5`` (``intfloat/multilingual-e5-*``): query:/passage: префікси.
    - ``qwen3`` (``Qwen/Qwen3-Embedding-*``): інструкція для запиту за карткою
      моделі на HF (``Instruct: {task}\\nQuery: {q}``), пасаж БЕЗ префікса.
    - інше (``plain``): без префіксів — звичайний encode.
    """
    name = (model_name or "").lower()
    if "qwen3" in name:
        return "qwen3"
    if "e5" in name:
        return "e5"
    return "plain"


_STYLE = _style_for(EMBED_MODEL)

# Задача для Qwen3-інструкції запиту (картка моделі на HF) — узагальнений
# retrieval, той самий текст для будь-якого питання по архіву.
_QWEN3_QUERY_TASK = "Given a search query, retrieve relevant passages that answer the query"

# Чанкінг. _MAX_CHARS тримаємо нижче 512-токенного ліміту e5. Замір на живому
# архіві (чанків аудіо-шляху, токенізатор multilingual-e5-large): p50=304,
# p99=407 токенів, >512 лише 1 чанк із 3000 — тобто на нашій кирилиці виходить
# ~3.5 символи/токен, а не 2, і запас до обрізання ще є. Ліміт лишаємо 1000:
# він тут не про токени, а про гранулярність видачі.
_MAX_CHARS = 1000
_TEXT_OVERLAP = 150        # перекриття для plain-text чанкінгу

# --- T6.5: природні межі чанку для аудіо-сегментів ---
# Раніше межа була одна — переповнення буфера на _MAX_CHARS. Через це думку
# рвало посеред фрази, а спікер чанку («домінантний за символами») міг бути
# тим, хто в цьому чанку в меншості: замір по 12.8k чанків аудіо-шляху дав
# частина чанків зі слабкою атрибуцією (домінант < 70% символів) і лише 54%
# чанків з ОДНИМ спікером. Для питань «хто що пообіцяв» це підпис-брехня.
# Тепер первинний сигнал — «думка скінчилась» (пауза або зміна спікера),
# а _MAX_CHARS лишається страховкою зверху.
_MIN_CHARS = 350           # нижче цього не ріжемо навіть на природній межі:
                           # медіанний сегмент — 56 символів, різати на кожній
                           # репліці означало б покришити діалог на уламки
_PAUSE_BOUNDARY_SEC = 1.5  # пауза між сегментами, яку читаємо як кінець думки.
                           # Замір розподілу пауз: p50=0.0, p90=0.88, p95=1.86;
                           # >=1.5s — 5.8% стиків (≈19 на транскрипт), тобто
                           # сигнал рідкісний і межі чанків не подрібнює
_SEG_OVERLAP_CHARS = 150   # перекриття — ЛИШЕ коли чанк обрізав ліміт (думку
                           # таки розірвано, треба дати сусіду контекст). На
                           # природній межі перекриття шкідливе: воно тягне в
                           # новий чанк думку, яка вже закінчилась

_model = None
_model_lock = threading.Lock()
_unavailable_reason: Optional[str] = None

# --- T6.6: моніторинг масштабу vector search (лише спостереження, НЕ ANN) ---
# retrieval._vector_search вантажить УСІ embedded-чанки (BLOB'и) в памʼять на
# кожен запит (brute-force numpy, без ANN/кешу). Поки чанків мало — це <100мс
# і незначна памʼять, але зростає лінійно з архівом (1024-dim float32 = 4KB/
# чанк). Щоб дізнатись про проблему ЗАЗДАЛЕГІДЬ (а не зі скарги на latency/RAM),
# рахуємо к-сть embedded-чанків через дешевий COUNT(*) (БЕЗ завантаження BLOB'ів)
# і логуємо WARNING, коли перевищено поріг — щоб міграцію на sqlite-vec (ANN)
# планували заздалегідь.
_VECTOR_WARN_THRESHOLD = int(os.environ.get("RECALL_VECTOR_WARN_THRESHOLD", "50000"))
_scale_warned = False  # process-level: не спамити лог на кожен виклик, поки поріг не «відпустить»


def check_vector_scale(db_path: str) -> dict:
    """Порахувати embedded-чанки (COUNT(*), БЕЗ завантаження BLOB'ів у памʼять)
    і за потреби залогувати WARNING про наближення до межі brute-force numpy
    vector search (retrieval._vector_search вантажить УСІ вектори на кожен
    запит). Дешево — можна кликати при старті застосунку і після кожного
    ембедингу (chunk_and_embed_transcription), без побудови numpy-матриці.

    Returns {"chunk_count": int, "threshold": int, "over_threshold": bool}.
    """
    global _scale_warned
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedding IS NOT NULL"
        ).fetchone()
    n = int(row["n"]) if row is not None else 0
    over = n > _VECTOR_WARN_THRESHOLD
    if over and not _scale_warned:
        logger.warning(
            "[embeddings] Embedded-чанків у архіві: %d — перевищено поріг %d "
            "(RECALL_VECTOR_WARN_THRESHOLD). retrieval._vector_search вантажить "
            "УСІ вектори в памʼять на кожен запит (brute-force numpy, без ANN) — "
            "плануйте міграцію на sqlite-vec заздалегідь, поки це не деградувало "
            "latency/RAM.", n, _VECTOR_WARN_THRESHOLD,
        )
        _scale_warned = True
    elif not over:
        # дозволяємо повторний warning, якщо к-сть знову переросте поріг
        # (напр. після ручного чищення й повторного росту архіву)
        _scale_warned = False
    return {"chunk_count": n, "threshold": _VECTOR_WARN_THRESHOLD, "over_threshold": over}


# ============================================================
# Модель
# ============================================================

def is_available() -> bool:
    """Чи можна рахувати embeddings (sentence-transformers + torch імпортуються)."""
    global _unavailable_reason
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception as e:  # pragma: no cover
        _unavailable_reason = str(e)
        return False


def unavailability_reason() -> str:
    return _unavailable_reason or "sentence-transformers/torch недоступні"


def _apply_model_dim(model) -> int:
    """Синхронізує EMBED_DIM із фактичним виміром завантаженої моделі.

    До цього виклику EMBED_DIM лишається дефолтом 1024 — так
    retrieval._vector_search бачить актуальний вимір одразу після lazy-
    завантаження моделі, без рестарту процесу. Винесено окремою функцією,
    щоб покрити тестом без реального завантаження (torch/sentence-transformers
    у юніт-тестах не піднімаються, memory mcp-stdio-no-heavy-models)."""
    global EMBED_DIM
    EMBED_DIM = model.get_sentence_embedding_dimension()
    return EMBED_DIM


def _apply_max_seq(model) -> int:
    """Опускає max_seq_length моделі до EMBED_MAX_SEQ, якщо той менший.

    Тільки вниз: модель зі своїм меншим лімітом (e5 — 512) лишається як є,
    інакше ми б їй цей ліміт ПІДНІМАЛИ. Окремою функцією — з тієї ж причини,
    що й _apply_model_dim: тест не піднімає torch."""
    current = getattr(model, "max_seq_length", None)
    if current is None or EMBED_MAX_SEQ <= 0:
        return current
    if current > EMBED_MAX_SEQ:
        logger.info("[embeddings] max_seq_length %d → %d (стеля EMBED_MAX_SEQ)",
                    current, EMBED_MAX_SEQ)
        model.max_seq_length = EMBED_MAX_SEQ
    return model.max_seq_length


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        import torch
        from sentence_transformers import SentenceTransformer
        device = os.environ.get("EMBED_DEVICE") or ("cuda" if torch.cuda.is_available() else "cpu")
        logger.info("[embeddings] Завантаження %s на %s …", EMBED_MODEL, device)
        t0 = datetime.now()
        # use_safetensors=True — гарантує що НЕ впадемо у torch.load(.bin), який
        # transformers блокує на torch<2.6 (див. коментар біля EMBED_MODEL).
        _model = SentenceTransformer(
            EMBED_MODEL, device=device, model_kwargs={"use_safetensors": True},
        )
        _apply_model_dim(_model)
        _apply_max_seq(_model)
        logger.info("[embeddings] Модель готова за %.1fs (dim=%d, max_seq=%s)",
                    (datetime.now() - t0).total_seconds(), EMBED_DIM,
                    getattr(_model, "max_seq_length", "?"))
        return _model


def _encode(texts: list[str], batch_size: int = 32) -> np.ndarray:
    model = _get_model()
    vecs = model.encode(
        texts, batch_size=batch_size, normalize_embeddings=True,
        show_progress_bar=False, convert_to_numpy=True,
    )
    return np.asarray(vecs, dtype=np.float32)


def embed_texts(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """Закодувати чанки-passages → np.float32 [N, dim], L2-нормалізовані.

    Префікс залежить від родини моделі (_style_for): e5 — 'passage: ', qwen3
    і plain — без префікса (картка Qwen3-Embedding префіксує лише запит)."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    if _STYLE == "e5":
        texts = [f"passage: {t}" for t in texts]
    return _encode(texts, batch_size)


def embed_query(text: str) -> np.ndarray:
    """Вектор одного запиту (1D float32, нормалізований).

    Префікс залежить від родини моделі (_style_for): e5 — 'query: ', qwen3 —
    інструкція за карткою моделі на HF, plain — без префікса."""
    t = text or ""
    if _STYLE == "e5":
        t = f"query: {t}"
    elif _STYLE == "qwen3":
        t = f"Instruct: {_QWEN3_QUERY_TASK}\nQuery: {t}"
    v = _encode([t])
    return v[0] if len(v) else np.zeros(EMBED_DIM, dtype=np.float32)


def blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ============================================================
# Чанкінг
# ============================================================

def _resolve_speaker(raw: Optional[str], speaker_map: dict[str, str]) -> Optional[str]:
    if not raw:
        return None
    if raw in speaker_map and speaker_map[raw]:
        return speaker_map[raw]
    if raw == "self":
        return "Ви"
    if raw == "SPEAKER_UNKNOWN":
        return None
    m = re.match(r"^SPEAKER_(\d+)$", raw)
    if m:
        return f"Спікер {int(m.group(1)) + 1}"
    return raw


def _is_turn_change(prev_speaker: Optional[str], speaker: Optional[str]) -> bool:
    """Чи змінився мовець між сусідніми сегментами.

    SPEAKER_UNKNOWN — це діра діаризації, а не окремий учасник: вхід у неї і
    вихід з неї не є зміною черги (на живих даних — 465 сегментів у 5
    транскриптах, і кожен дав би зайвий розріз). Сегменти без мітки взагалі
    (недіаризований транскрипт — 104k із 120k сегментів) сигналу не дають."""
    if not prev_speaker or not speaker:
        return False
    if "SPEAKER_UNKNOWN" in (prev_speaker, speaker):
        return False
    return prev_speaker != speaker


def _chunk_from_segments(segments: list[dict], speaker_map: dict[str, str]) -> list[dict]:
    """Вікна по сегментах із природними межами (T6.5). Зберігає таймкоди
    (start/end) і домінантного спікера чанку.

    Порядок сигналів межі:
      1) зміна спікера або пауза >= _PAUSE_BOUNDARY_SEC — «думка скінчилась»,
         ріжемо БЕЗ перекриття (але не раніше _MIN_CHARS, інакше діалог
         покришиться на репліки по 50 символів);
      2) _MAX_CHARS — страховка: думку розірвано штучно, тож новий чанк
         починається з хвоста попереднього (_SEG_OVERLAP_CHARS).
    """
    chunks: list[dict] = []
    cur: list[dict] = []   # {"text","speaker","start","end","len"}
    cur_len = 0            # усі символи в буфері (разом із перенесеним хвостом)
    fresh_len = 0          # символи, додані ПІСЛЯ останнього flush
    carried = 0            # скільки сегментів у cur — це хвіст попереднього чанку
    idx = 0

    def _emit():
        nonlocal idx
        sp_chars: dict[str, int] = {}
        for s in cur:
            if s["speaker"]:
                sp_chars[s["speaker"]] = sp_chars.get(s["speaker"], 0) + s["len"]
        dominant = max(sp_chars, key=sp_chars.get) if sp_chars else None
        starts = [s["start"] for s in cur if s["start"] is not None]
        ends = [s["end"] for s in cur if s["end"] is not None]
        chunks.append({
            "chunk_index": idx,
            "start_time": starts[0] if starts else None,
            "end_time": ends[-1] if ends else None,
            "speaker": _resolve_speaker(dominant, speaker_map),
            "text": " ".join(s["text"] for s in cur).strip(),
        })
        idx += 1

    def _flush(overlap: bool):
        """Закрити чанк. overlap=True — лишити хвіст на ~_SEG_OVERLAP_CHARS
        символів як початок наступного (тільки для розриву за лімітом)."""
        nonlocal cur, cur_len, fresh_len, carried
        if not cur:
            return
        _emit()
        fresh_len = 0
        if not overlap or _SEG_OVERLAP_CHARS <= 0:
            cur, cur_len, carried = [], 0, 0
            return
        # У хвіст беремо лише те, що ВЛАЗИТЬ у бюджет перекриття — включно з
        # першим сегментом. Правило «хоч один сегмент завжди» (успадковане від
        # старого чанкера) тягнуло в наступний чанк цілі 900-символьні монологи:
        # текст дублювався в індексі і перебивав голосування за спікера. Якщо
        # останній сегмент сам більший за бюджет — перекриття не потрібне:
        # чанк і так закінчився на цілому сегменті, думку ніхто не обрізав.
        tail: list[dict] = []
        tail_len = 0
        for s in reversed(cur):
            if tail_len + s["len"] > _SEG_OVERLAP_CHARS:
                break
            tail.insert(0, s)
            tail_len += s["len"]
        cur, cur_len, carried = tail, tail_len, len(tail)

    prev_end: Optional[float] = None
    prev_speaker: Optional[str] = None
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = seg.get("start"), seg.get("end")
        speaker = seg.get("speaker")

        # Поріг рахуємо по СВОЄМУ тексту (fresh_len), не разом із перенесеним
        # хвостом: інакше чанк, який щойно отримав 150 символів перекриття, вже
        # «майже дозрів», і природна межа спрацювала б на власному вмісті у
        # 200 символів. len(cur) > carried — у буфері має бути хоч один свій
        # сегмент, інакше межа випустила б чанк із самого лише хвоста (дубль).
        if fresh_len >= _MIN_CHARS and len(cur) > carried:
            long_pause = (
                isinstance(prev_end, (int, float)) and isinstance(start, (int, float))
                and (start - prev_end) >= _PAUSE_BOUNDARY_SEC
            )
            if long_pause or _is_turn_change(prev_speaker, speaker):
                _flush(overlap=False)

        cur.append({"text": text, "speaker": speaker, "start": start,
                    "end": end, "len": len(text)})
        cur_len += len(text)
        fresh_len += len(text)
        if isinstance(end, (int, float)):
            prev_end = end
        prev_speaker = speaker

        if cur_len >= _MAX_CHARS:
            _flush(overlap=True)
    # len(cur) > carried: якщо ліміт спрацював на ОСТАННЬОМУ сегменті, у буфері
    # лишився самий хвіст — випустити його означало б додати чанк, повністю
    # вкладений у попередній (зайвий вектор і зайвий рядок chunks_fts).
    if len(cur) > carried:
        _flush(overlap=False)
    return chunks


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n{2,}", text)
    return [p.strip() for p in parts if p and p.strip()]


def _overlap_tail(text: str, limit: int) -> str:
    """Хвіст тексту (~limit символів) для перекриття — зрізаний по межі речення,
    а на крайняк по межі слова.

    T6.5: раніше хвіст брався сирим зрізом `text[-limit:]`, тобто ніж падав
    посеред слова — на живому архіві 1607 із 1873 не-перших чанків текстового
    шляху починались з обрубка. Такий початок псує і вектор, і цитату в чаті."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    m = re.search(r"(?<=[.!?…])\s+(\S)", tail)
    if m:
        return tail[m.start(1):]
    space = tail.find(" ")
    return tail[space + 1:] if space != -1 else tail


def _window_text(text: str) -> list[str]:
    """Sentence-aware ковзне вікно → список текстових шматків ≤ _MAX_CHARS
    з перекриттям ~_TEXT_OVERLAP. Спільне ядро для plain-text і блок-чанкінгу."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= _MAX_CHARS:
        return [text]
    sentences = _split_sentences(text)
    pieces: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        cur.append(s)
        cur_len += len(s) + 1
        if cur_len >= _MAX_CHARS:
            joined = " ".join(cur).strip()
            pieces.append(joined)
            tail = _overlap_tail(joined, _TEXT_OVERLAP)
            cur = [tail] if tail else []
            cur_len = len(tail)
    if cur:
        joined = " ".join(cur).strip()
        if joined:
            pieces.append(joined)
    return pieces


def _chunk_from_text(text: str) -> list[dict]:
    """Sentence-aware ковзне вікно для plain-text (без таймкодів/провенансу)."""
    return [{"chunk_index": i, "start_time": None, "end_time": None,
             "speaker": None, "text": p}
            for i, p in enumerate(_window_text(text))]


def _chunk_from_blocks(blocks: list[dict]) -> list[dict]:
    """Чанкінг документів з провенансом (Phase 16B). Вікна НЕ перетинають межі
    блоку (сторінки/слайда) — кожен чанк зберігає page+section свого блоку.
    Великий блок ріжеться sentence-aware на під-чанки (та сама page/section)."""
    chunks: list[dict] = []
    idx = 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btext = (block.get("text") or "").strip()
        if not btext:
            continue
        page = block.get("page")
        section = block.get("section")
        for piece in _window_text(btext):
            chunks.append({
                "chunk_index": idx,
                "start_time": None, "end_time": None, "speaker": None,
                "page": page, "section": section,
                "text": piece,
            })
            idx += 1
    return chunks


def build_chunks(segments_json: Optional[str], transcript_text: str,
                 speaker_map: dict[str, str],
                 structure_json: Optional[str] = None) -> list[dict]:
    """Обрати стратегію чанкінгу:
      1) документ зі структурою (structure_json) → блочний чанкер з провенансом;
      2) аудіо із сегментами (таймкоди) → сегментний чанкер;
      3) інакше → плоский текстовий чанкер.
    """
    import json
    # 1) Провенанс документів (Phase 16B): PDF-сторінки / PPTX-слайди.
    if structure_json:
        try:
            blocks = json.loads(structure_json)
        except (ValueError, TypeError):
            blocks = None
        if blocks and any(isinstance(b, dict) and (b.get("text") or "").strip() for b in blocks):
            return _chunk_from_blocks(blocks)
    # 2) Аудіо-сегменти з таймкодами.
    segments = None
    if segments_json:
        try:
            segments = json.loads(segments_json)
        except (ValueError, TypeError):
            segments = None
    if segments and any(isinstance(s, dict) and (s.get("text") or "").strip() for s in segments):
        return _chunk_from_segments(segments, speaker_map)
    # 3) Плоский текст.
    return _chunk_from_text(transcript_text)


# ============================================================
# Контекстний префікс чанка (Волна B, історія 05)
# ============================================================
#
# Чанк був самодостатнім лише для того, хто вже знає, звідки він: у вектор і в
# BM25 йшов голий текст репліки. Назва чату, дата, автор, напрямок і тема нитки
# лежали в сусідніх колонках і в пошуку не брали участі взагалі.
#
# Префікс — це ПОЛЯ БД, а не переказ тексту моделлю: рядок 1 — провенанс,
# рядок 2 — один рядок сводки одиниці сенсу (`summaries.unit_summary_line`,
# контракт C1). Він іде в ембедер і в `chunks_fts`, але НЕ в `chunks.text` —
# цитати, експорт і нитки далі показують лише те, що людина справді сказала.

#: Порядок полів у рядку 1 фіксований (контракт C4 плану) — префікс має бути
#: детермінованим: той самий запис дає той самий рядок, інакше кожен re-embed
#: змінював би вектори «сам по собі».
_PREFIX_SEP = " · "

_KIND_LABELS = {"telegram": "переписка", "document": "документ"}


def _prefix_date(value: Optional[str]) -> Optional[str]:
    """`YYYY-MM-DD` з дати запису. meeting_date і created_at обидва починаються
    з ISO-дати, тож беремо перші 10 символів і перевіряємо форму — сміття
    («невідомо», порожнє) у префікс не пускаємо."""
    s = (str(value) if value is not None else "").strip()
    if not s:
        return None
    head = s[:10]
    return head if re.match(r"^\d{4}-\d{2}-\d{2}$", head) else None


#: Опис запису (`transcriptions.description`) може бути до 4000 символів —
#: у префікс іде лише голова: префікс повторюється в КОЖНОМУ чанку запису, і
#: довгий опис витіснив би з вікна ембедера сам текст.
_PREFIX_DESCRIPTION_MAX = 300


def _prefix_description(value) -> Optional[str]:
    """Опис одним рядком: переноси → пробіли, обрізано до 300 символів.
    Порожнє/не-рядок → None (рядка «опис:» у префіксі просто не буде)."""
    if not isinstance(value, str):
        return None
    flat = " ".join(value.split())
    if not flat:
        return None
    return flat[:_PREFIX_DESCRIPTION_MAX]


def build_context_prefix(meta: dict, chunk: dict) -> str:
    """Контекстний заголовок чанка (контракт C4): 1–2 рядки.

    Рядок 1: ``[тип] назва|чат · YYYY-MM-DD · спікер|автор · хвіст``, де хвіст
    залежить від типу — напрямок (дзвінок), ``нитка: label`` (переписка),
    ``стор. N`` або секція (документ). Порожні поля просто пропускаються;
    рядка «None» не буває ніколи.
    Рядок «опис: …» (якщо власник його написав) і рядок 2:
    `unit_summary_line` одиниці сенсу, якщо вона є.

    `meta` — поля ЗАПИСУ (`_load_prefix_meta`), `chunk` — поля самого чанка
    (`speaker`, `page`, `section`). Функція чиста й детермінована.
    """
    source_type = (meta.get("source_type") or "").strip()
    kind = _KIND_LABELS.get(source_type, "дзвінок")

    parts: list[str] = [f"[{kind}]"]

    def _add(value) -> None:
        s = str(value).strip() if value is not None else ""
        if s:
            parts.append(s)

    _add(meta.get("title"))
    _add(_prefix_date(meta.get("date")))
    _add(chunk.get("speaker"))

    if source_type == "telegram":
        label = (meta.get("thread_label") or "").strip() if meta.get("thread_label") else ""
        if label:
            _add(f"нитка: {label}")
    elif source_type == "document":
        page = chunk.get("page")
        if page is not None and str(page).strip():
            _add(f"стор. {page}")
        else:
            _add(chunk.get("section"))
    else:
        _add(meta.get("category"))

    # Перший елемент — мітка типу в дужках, вона приклеюється до назви пробілом,
    # решта — через роздільник: "[дзвінок] Назва · 2026-09-17 · Андрій · Фонд".
    if len(parts) == 1:
        line1 = parts[0]
    else:
        line1 = parts[0] + " " + _PREFIX_SEP.join(parts[1:])

    lines = [line1]
    description = _prefix_description(meta.get("description"))
    if description:
        lines.append(f"опис: {description}")

    summary_line = (meta.get("summary_line") or "").strip() if meta.get("summary_line") else ""
    if summary_line:
        lines.append(summary_line)
    return "\n".join(lines)


def _load_prefix_meta(conn, transcription_id: int) -> dict:
    """Поля запису для префікса одним доджойном: категорія (напрямок) і мітка
    нитки лежать у сусідніх таблицях, а не в `transcriptions`.

    Сводка береться через `summaries.unit_summary_line` (лише SQL — модуль
    навмисно без torch/anthropic на рівні імпорту, memory
    `mcp-stdio-no-heavy-models`)."""
    row = conn.execute(
        "SELECT t.source_type, t.source_name, t.title, t.description, "
        "       t.original_filename, t.tg_chat_title, "
        "       t.meeting_date, t.created_at, t.tg_date, "
        "       cat.name AS category_name, th.label AS thread_label "
        "FROM transcriptions t "
        "LEFT JOIN categories cat ON cat.id = t.category_id "
        "LEFT JOIN tg_threads th ON th.id = t.tg_thread_id "
        "WHERE t.id = ?",
        (transcription_id,),
    ).fetchone()
    if not row:
        return {}

    source_type = row["source_type"]
    if source_type == "telegram":
        fallback = row["tg_chat_title"] or row["source_name"]
    elif source_type == "document":
        fallback = row["source_name"] or row["original_filename"]
    else:
        fallback = row["source_name"]

    # Власна назва (`transcriptions.title`, контракт C2) б'є провенанс; якщо її
    # нема — лишається та сама логіка, що була до історії 03 (для TG це чат).
    # Порожні обидва поля лишаються порожнім місцем у префіксі (як було), а не
    # штучним «Запис #id», який display_name віддає для UI.
    from app.services.record_meta import display_name as _display_name
    title = (_display_name({"title": row["title"], "source_name": fallback})
             if (row["title"] or fallback) else None)

    try:
        from app.services.summaries import unit_summary_line
        summary_line = unit_summary_line(conn, transcription_id)
    except Exception:  # noqa: BLE001 — префікс без сводки кращий за зірваний ембединг
        logger.debug("[embeddings] unit_summary_line не вдався для tx=%s",
                     transcription_id, exc_info=True)
        summary_line = None

    return {
        "source_type": source_type,
        "title": title,
        "description": row["description"],
        "date": row["meeting_date"] or row["tg_date"] or row["created_at"],
        "category": row["category_name"],
        "thread_label": row["thread_label"],
        "summary_line": summary_line,
    }


# ============================================================
# Збереження
# ============================================================

#: Заглушки, які інжест ставить, коли розпізнавати нічого: «[фото без тексту]»,
#: «[відео без розпізнаного тексту]», «[голосове — очікує розпізнавання]» тощо.
#: Це не текст, а службова позначка — вектор із неї шкідливий (152 однакові
#: точки на живих даних), а фраза «фото» в ній ще й ловиться на запити про фото.
_TG_CONTENTLESS = re.compile(r"^\s*\[[^\]]{0,60}\]\s*$")


def _is_contentless_tg(text: str) -> bool:
    return bool(_TG_CONTENTLESS.match(text or ""))


def _mark_embedded_without_chunks(db_path: str, transcription_id: int) -> None:
    """Запис не дає жодного чанку: прибрати старі і позначити обробленим.

    DELETE обовʼязковий: у звичайному шляху чанки перезаписуються (DELETE+INSERT
    нижче), а тут ми виходимо раніше — без нього ПЕРЕІНДЕКСАЦІЯ лишала старі
    сміттєві вектори живими, і фільтр заглушок нічого б не змінив для наявного
    архіву (спіймано на живих даних: 188 «[фото без тексту]» пережили прохід).
    Позначка потрібна, інакше індексер вічно вважатиме запис роботою."""
    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM chunks WHERE transcription_id = ?", (transcription_id,))
        conn.execute(
            "UPDATE transcriptions SET embedded_at = CURRENT_TIMESTAMP, "
            "embedding_model = ?, embedding_version = ?, chunk_count = 0 WHERE id = ?",
            (EMBED_MODEL, EMBED_VERSION, transcription_id))
        conn.commit()


def chunk_and_embed_transcription(db_path: str, transcription_id: int,
                                  force: bool = False) -> dict:
    """Порізати транскрипт на чанки + закодувати + зберегти. Idempotent через
    embedded_at. Перезаписує чанки при re-run (force або зміна моделі)."""
    if not is_available():
        return {"status": "unavailable", "reason": unavailability_reason()}

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT id, transcript_text, polished_text, segments, structure_json, "
            "embedded_at, embedding_model, embedding_version, source_type, tg_sender "
            "FROM transcriptions WHERE id = ?",
            (transcription_id,),
        ).fetchone()
        if not row:
            return {"status": "not_found", "transcription_id": transcription_id}

        # T6.8: idempotency звіряє МОДЕЛЬ і ВЕРСІЮ.
        # T6.5 (14.08.2026): NULL більше НЕ означає «сумісний». Раніше так було
        # навмисно — щоб бамп v28 не перерахував архів заднім числом. Тепер
        # нарізка справді змінилась, а NULL — це «ембеджено ще до того, як
        # версію взагалі почали писати» (записів на живому архіві), тобто
        # завідомо стара нарізка. Лишити їх «сумісними» означало б тиху діру:
        # частина архіву назавжди з іншим чанкінгом. Умова тут ОБОВʼЯЗКОВО
        # дзеркалить enrichment.list_unenriched_ids — інакше індексер віддавав
        # би записи, які ця функція мовчки skip'ає, і робота б не закінчувалась.
        row_version = row["embedding_version"] if "embedding_version" in row.keys() else None
        version_matches = row_version == EMBED_VERSION
        already = (bool(row["embedded_at"])
                  and row["embedding_model"] == EMBED_MODEL
                  and version_matches)
        if already and not force:
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "already_embedded"}

        body = row["polished_text"] or row["transcript_text"] or ""
        if not body.strip():
            return {"status": "empty", "transcription_id": transcription_id}

        sp_rows = c.execute(
            "SELECT m.raw_label, s.name FROM transcription_speaker_map m "
            "LEFT JOIN speakers s ON s.id = m.speaker_id WHERE m.transcription_id = ?",
            (transcription_id,),
        ).fetchall()
        speaker_map = {r["raw_label"]: r["name"] for r in sp_rows if r["name"]}
        segments_json = row["segments"]
        structure_json = row["structure_json"] if "structure_json" in row.keys() else None
        is_telegram = row["source_type"] == "telegram"
        tg_sender = row["tg_sender"] if is_telegram else None
        # Волна B: поля для контекстного префікса — тим самим зʼєднанням, поки
        # воно відкрите (назва/чат, дата, напрямок, мітка нитки, рядок сводки).
        prefix_meta = _load_prefix_meta(conn, transcription_id)

    chunks = build_chunks(segments_json, body, speaker_map, structure_json)

    if is_telegram:
        # Автор повідомлення. У TG немає segments (діаризувати нічого), тож
        # build_chunks лишав speaker=None — на живих даних порожній у ВСІХ 5857
        # чанках. Через це RAG не міг сказати, ХТО щось пообіцяв, хоча імʼя
        # лежало в сусідній колонці.
        if tg_sender:
            for ch in chunks:
                if not ch.get("speaker"):
                    ch["speaker"] = tg_sender
        # Заглушки без вмісту не мають бути векторами: 152 однакових
        # «[фото без тексту]» — це 152 однакові точки, які конкурують за місце
        # у видачі і нічого не означають.
        chunks = [ch for ch in chunks if not _is_contentless_tg(ch["text"])]

    if not chunks:
        if is_telegram:
            _mark_embedded_without_chunks(db_path, transcription_id)
            return {"status": "skipped", "transcription_id": transcription_id,
                    "reason": "tg_contentless"}
        return {"status": "empty", "transcription_id": transcription_id}

    # Волна B: контекстний префікс рахуємо ПІСЛЯ нарізки й TG-постпроцесу —
    # у ньому бере участь спікер чанку (для TG його щойно проставили) і
    # сторінка/секція документа.
    for ch in chunks:
        ch["context_prefix"] = build_context_prefix(prefix_meta, ch)

    # Кодуємо поза БД-зʼєднанням (GPU-операція). У вектор іде префікс+текст —
    # сам текст лишається в `chunks.text` недоторканим (цитати, експорт, нитки).
    vecs = embed_texts([
        (ch["context_prefix"] + "\n" + ch["text"]) if ch["context_prefix"] else ch["text"]
        for ch in chunks
    ])

    with get_db_connection(db_path) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM chunks WHERE transcription_id = ?", (transcription_id,))
        for ch, vec in zip(chunks, vecs):
            c.execute(
                "INSERT INTO chunks (transcription_id, chunk_index, start_time, "
                "end_time, speaker, text, embedding, token_estimate, page, section, "
                "context_prefix) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (transcription_id, ch["chunk_index"], ch["start_time"], ch["end_time"],
                 ch["speaker"], ch["text"], vec.astype(np.float32).tobytes(),
                 len(ch["text"]) // 4, ch.get("page"), ch.get("section"),
                 ch.get("context_prefix") or None),
            )
        c.execute(
            "UPDATE transcriptions SET embedded_at = CURRENT_TIMESTAMP, "
            "embedding_model = ?, embedding_version = ?, chunk_count = ? WHERE id = ?",
            (EMBED_MODEL, EMBED_VERSION, len(chunks), transcription_id),
        )
        conn.commit()

    logger.info("[embeddings] tx=%s: %d чанків закодовано (%s)",
                transcription_id, len(chunks), EMBED_MODEL)

    # T6.6: періодична (на кожен ембединг — тобто на кожен ріст архіву) перевірка
    # масштабу. Дешево: лише COUNT(*), без завантаження BLOB'ів.
    try:
        check_vector_scale(db_path)
    except Exception:  # pragma: no cover — моніторинг не має валити основний потік
        logger.debug("[embeddings] check_vector_scale не вдався", exc_info=True)

    return {"status": "embedded", "transcription_id": transcription_id,
            "chunks": len(chunks), "model": EMBED_MODEL}
