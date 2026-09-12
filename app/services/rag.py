"""RAG «Запитай архів» (Phase 13C).

Питання користувача → гібридний retrieval по чанках усіх мітингів → Claude
формує відповідь СУВОРО з знайдених фрагментів, з цитатами [n] на конкретні
мітинги (дата + спікер + таймкод). Це і є досвід «запустив Claude Code в
meeting_archive і спитав», але вбудований у Whisper.

Антигалюцинація: модель відповідає тільки з наданого контексту; якщо даних
бракує — каже про це. Кожен source повертається клієнту для клікабельних цитат.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterator, Optional

from app.services import retrieval, text_polishing
from app.services import claude_retry
from app.services.claude_retry import is_retryable_error
from app.services.models import get_default_model, supports_adaptive_thinking


logger = logging.getLogger(__name__)

# T6.4: опційний локальний cross-encoder rerank (app/services/retrieval.py
# rerank=True → app/services/reranker.py). ЗА ЗАМОВЧУВАННЯМ OFF — вмикається
# точково лише тут (RAG-чат «Запитай архів», найвища ціна помилки top-k), а
# НЕ у retrieval.search() глобально — copilot/categorize/MCP-пошук лишаються
# без rerank і без зміни латентності. Читаємо env напряму (як
# COPILOT_ENABLED/embeddings.py), щоб модуль лишався standalone-тестованим.
_RERANK_ENABLED = os.environ.get("RECALL_RERANK_ENABLED", "0").strip() in ("1", "true", "True")

# Скільки фрагментів іде у контекст відповіді. 12, а не 8 — за замірами
# eval-харнеса на golden-set (14.08.2026, після T6.5): recall@8 67.9% проти
# recall@12 82.1%, source_name_hit 78.6% проти 85.7%. Тобто на k=8 частина
# правильних джерел стоїть одразу ЗА межею зрізу (характерний випадок —
# джерело на 9-й позиції). Ціна: +4 фрагменти у промпті ≈ +1.5k вхідних
# токенів на питання; вихідні не змінюються. Копілот сюди НЕ входить — у
# нього власні бюджети уваги за режимами (5/7/9, Трек 3).
_DEFAULT_TOP_K = int(os.environ.get("RAG_TOP_K", "12"))


_RAG_SYSTEM_PROMPT = """Ти — асистент по архіву робочих зустрічей/дзвінків компанії.

Тобі дають ПИТАННЯ і пронумеровані ФРАГМЕНТИ транскриптів різних мітингів
(кожен з номером [n], назвою мітингу, датою, спікером). Твоя задача — відповісти
на питання, спираючись ВИКЛЮЧНО на ці фрагменти.

Окремо тобі можуть дати КОМЕНТАРІ ВЛАСНИКА АРХІВУ — це не репліки з розмови, а
речення, які власник свідомо написав ПРО запис уже після нього: уточнення,
виправлення, акцент. Вони мають ВИЩИЙ пріоритет за сирий транскрипт:

- Якщо коментар СУПЕРЕЧИТЬ тому, що сказано у фрагменті транскрипту — вір
  коментарю, відповідай за ним і прямо скажи, що в записі було інакше.
- Коментар типу «виправлення» скасовує відповідне місце транскрипту, навіть
  якщо транскрипт звучить упевнено.
- Посилайся на коментарі так само, як на фрагменти — за їхнім номером [n].
- Не вигадуй коментарів і не приписуй власнику того, чого він не писав.

Правила:
- Відповідай мовою питання (зазвичай українською).
- Використовуй ТІЛЬКИ інформацію з фрагментів. НЕ вигадуй, не додавай знань ззовні.
- Після кожного твердження став посилання на джерело у форматі [n] (можна кілька: [1][3]).
- Якщо фрагментів недостатньо для відповіді — чесно скажи, що в архіві бракує
  інформації, і вкажи що саме знайшлося дотичного (якщо є).
- Будь конкретним: імена, цифри, дати, рішення, домовленості — як у фрагментах.
- Структуруй відповідь (абзаци / короткі пункти), якщо це доречно. Без води.

Не повторюй саме питання. Не додавай преамбул типу «Ось відповідь»."""


def _fmt_time(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


_COMMENT_KIND_LABEL = {
    "correction": "ВИПРАВЛЕННЯ",
    "decision": "РІШЕННЯ",
    "note": "НОТАТКА",
    "context": "КОНТЕКСТ",
    "question": "ПИТАННЯ",
}


def _fmt_comment(i: int, kind: str, label: str, date, text: str,
                 anchor_time=None, pinned: bool = False) -> str:
    """Заголовок коментаря у контексті.

    Слово «КОМЕНТАР ВЛАСНИКА» стоїть першим навмисно: без явної позначки
    провенансу модель читала б уточнення як ще одну репліку з дзвінка і
    зважувала б його нарівні з нею — тобто весь сенс шару зникав би саме там,
    де формулюється відповідь.
    """
    head = f"[{i}] КОМЕНТАР ВЛАСНИКА ({_COMMENT_KIND_LABEL.get(kind, 'НОТАТКА')})"
    if pinned:
        head += ", закріплений"
    head += f" до «{label}»"
    if date:
        head += f" ({date})"
    ts = _fmt_time(anchor_time)
    if ts:
        head += f", до моменту ~{ts}"
    return f"{head}\n{text}"


def order_citables(chunks: list[dict],
                   attached_comments: Optional[list[dict]] = None) -> list[dict]:
    """Єдиний порядок джерел — і для нумерації [n], і для масиву `sources`.

    ЧОМУ ОДНА ФУНКЦІЯ, А НЕ ДВА СПИСКИ. Контекст ставить коментарі першими
    (правило пріоритету в system prompt працює лише тоді, коли модель
    прочитала уточнення ДО транскрипту, який воно виправляє), а `sources`
    раніше віддавався у порядку видачі. Номери роз'їжджались: на chunks
    [t1, t2] плюс одне підшите виправлення контекст давав [1]=коментар,
    [2]=t1, [3]=t2, а клієнт відкривав `sources[n-1]` — тобто [2] вело на t2,
    а [3] взагалі виходило за межі масиву і лишалось голим текстом.
    Розходження зачіпало саме той випадок, заради якого шар зроблено:
    будь-який pinned/correction коментар на знайденому записі зсував УСІ
    посилання на одиницю.

    Підшиті коментарі теж потрапляють у список: вони пронумеровані в
    контексті, отже клієнт мусить уміти їх відкрити (у них є
    `transcription_id`).

    `why` на кожному елементі. Знайдені `chunks` уже несуть `why` від
    `retrieval.search` (контракт C2). Підшиті коментарі — інший шлях: вони
    прийшли з `attach_comments` (SQL-запит на pinned/correction по картках
    видачі), який ніколи не звертався до `retrieval.search`, тож `why` у них
    нема органічно. Мовчазна відсутність ключа ловить `KeyError` у
    споживача — ставимо явний маркер провенансу замість скорингу.
    Маркер несе ТОЙ САМИЙ обовʼязковий набір ключів C2 (`src`, `rrf`, `rec`,
    `by`, `top`), з нульовими внесками (`rrf`/`rec` = 0.0, `by` = []) і
    пʼятим легальним значенням `top` — `"attached"` (план D2): ключ, який
    іноді є, а іноді нема, — той самий `KeyError` з відстрочкою. Перелік
    ключів не переписується літералом тут — береться з
    `retrieval.build_placeholder_why` (одне джерело істини, історія 13, план D3).
    """
    comment_chunks = [c for c in chunks if c.get("source_type") == "comment"]
    attached = [dict(ac, source_type="comment", attached=True,
                     why=ac.get("why") or retrieval.build_placeholder_why(
                         "comment", "attached",
                         note="підшито без пошуку (pinned/correction)"))
                for ac in (attached_comments or [])]
    rest = [c for c in chunks if c.get("source_type") != "comment"]
    return comment_chunks + attached + rest


def _build_context(chunks: list[dict], attached_comments: Optional[list[dict]] = None) -> str:
    """Контекст для Claude. Порядок і нумерація — з `order_citables`."""
    parts = []
    for i, ch in enumerate(order_citables(chunks, attached_comments), 1):
        source_type = ch.get("source_type")
        if source_type == "comment":
            # Підшитий коментар приходить із `attach_comments` (ключі kind/
            # date/anchor_time), самостійна знахідка — з retrieval
            # (comment_kind/meeting_date/start_time). Читаємо обидві форми.
            parts.append(_fmt_comment(
                i,
                ch.get("comment_kind") or ch.get("kind"),
                ch.get("target_label") or ch.get("source_name"),
                ch.get("meeting_date") or ch.get("date"),
                ch["text"],
                ch.get("start_time") if ch.get("start_time") is not None
                else ch.get("anchor_time"),
                ch.get("pinned")))
            continue
        if source_type == "telegram":
            # Переписка — НЕ мітинг. Раніше сюди йшло «Мітинг «[TG] чат: сніпет»»,
            # тобто переважна більшість корпусу модель переказувала як «на зустрічі ви вирішили»,
            # ще й не знаючи автора. Чат виносимо окремо (а не як частину
            # source_name), автора — теж: у TG це tg_sender, який тепер доїжджає
            # у chunks.speaker.
            chat = ch.get("tg_chat_title") or ch.get("source_name")
            head = f"[{i}] Telegram, чат «{chat}»"
            if ch.get("meeting_date"):
                head += f" ({ch['meeting_date']})"
            if ch.get("speaker"):
                head += f", від: {ch['speaker']}"
            if ch.get("tg_reply_to"):
                head += f", відповідь на повідомлення {ch['tg_reply_to']}"
            # Нитка (Волна 4.5): у переписці знахідка часто є ПИТАННЯМ, а
            # відповідь — наступною реплікою, у якої немає спільних слів із
            # запитом, тож окремо вона не знаходиться ніколи. Віддаємо розмову
            # шматком і явно позначаємо, що саме знайшлось, — інакше модель не
            # відрізнить знахідку від контексту і цитуватиме сусіда.
            thread = ch.get("thread")
            if thread and thread.get("messages"):
                label = thread.get("label")
                head += f"\nНитка «{label}»" if label else "\nНитка розмови"
                if thread.get("total_messages"):
                    head += f" ({len(thread['messages'])} з {thread['total_messages']} повідомлень)"
                lines = []
                for m in thread["messages"]:
                    mark = "→ " if m.get("is_hit") else "  "
                    when = (m.get("date") or "")[:16].replace("T", " ")
                    who = m.get("sender") or "невідомо"
                    lines.append(f"{mark}[{when} {who}] {m.get('text') or ''}")
                parts.append(f"{head}\n" + "\n".join(lines))
                continue
            parts.append(f"{head}\n{ch['text']}")
            continue

        kind = "Документ" if source_type == "document" else "Мітинг"
        head = f"[{i}] {kind} «{ch['source_name']}»"
        if ch.get("meeting_date"):
            head += f" ({ch['meeting_date']})"
        if ch.get("speaker"):
            head += f", спікер: {ch['speaker']}"
        # Провенанс документів — сторінка / слайд / лист (+ заголовок секції)
        page = ch.get("page")
        if page:
            dt = ch.get("doc_type")
            label = "слайд" if dt == "pptx" else ("лист" if dt in ("xlsx", "csv") else "стор.")
            head += f", {label} {page}"
            if ch.get("section"):
                head += f" «{ch['section']}»"
        ts = _fmt_time(ch.get("start_time"))
        if ts:
            head += f", ~{ts}"
        parts.append(f"{head}\n{ch['text']}")
    return "\n\n".join(parts)


def _build_request_kwargs(question: str, chunks: list[dict], model: Optional[str],
                          attached_comments: Optional[list[dict]] = None) -> tuple[dict, str]:
    """Спільна побудова запиту до Claude (для sync і stream). Returns (kwargs, model)."""
    model = model or get_default_model()
    user_message = (
        f"ПИТАННЯ: {question}\n\n"
        f"ФРАГМЕНТИ АРХІВУ:\n\n{_build_context(chunks, attached_comments)}"
    )
    kwargs = dict(
        model=model,
        max_tokens=4096,
        system=[{
            "type": "text",
            "text": _RAG_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_message}],
    )
    if supports_adaptive_thinking(model):
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "medium"}
    return kwargs, model


def _resolve_project(db_path: str, project: Optional[str]) -> Optional[list[int]]:
    """Назви проєктів/людей → id сутностей для звуження пошуку (Трек 2).

    Другий шар скоупу: категорії недостатньо, бо «Робота» — більша частина корпусу.
    Порожній результат (назви не знайдено) НЕ звужує пошук до нуля — повертаємо
    None, тобто «шукай скрізь»: краще ширша відповідь, ніж мовчання через друкарську
    помилку в назві проєкту.
    """
    if not project:
        return None
    from app.services import scope
    ids = scope.scope_filter_ids(db_path, project)
    if not ids:
        logger.info("rag: проєкт/людину «%s» не знайдено у графі — шукаю без звуження", project)
        return None
    return ids


def answer_question(
    db_path: str,
    question: str,
    top_k: int = _DEFAULT_TOP_K,
    model: Optional[str] = None,
    timeout: float = 180.0,
    category_id: Optional[int] = None,
    project: Optional[str] = None,
    explain: bool = False,
) -> dict:
    """Відповісти на питання по архіву з цитатами. category_id — обмежити напрямком.
    explain — Історія 05: прокидається у `retrieval.search`, `why` кожного
    чанка виживає до `sources` крізь `order_citables`/`attach_thread_context`.

    Returns {"answer", "sources": [chunks], "model", "found", token usage}.
    """
    question = (question or "").strip()
    if not question:
        return {"answer": "", "sources": [], "found": 0, "model": ""}

    scope_tids = _resolve_project(db_path, project)
    res = retrieval.search(db_path, question, top_k=top_k, category_id=category_id,
                           scope_tids=scope_tids,
                           rerank=_RERANK_ENABLED,
                           explain=explain)
    chunks = retrieval.attach_thread_context(db_path, res["chunks"])
    if not chunks:
        return {
            "answer": "У архіві не знайдено релевантної інформації за цим запитом.",
            "sources": [], "found": 0, "model": "",
            "vector_available": res.get("vector_available", False),
        }

    # Шар 2 пріоритету: виправлення й закріплені коментарі тих записів, що
    # потрапили у видачу, — навіть якщо самі вони із запитом не збіглися.
    attached = retrieval.attach_comments(db_path, chunks)
    # `sources` мусить бути ТИМ САМИМ списком, який пронумеровано в контексті —
    # інакше [n] у відповіді вказує не на те джерело (див. order_citables).
    sources = order_citables(chunks, attached)

    client = text_polishing._get_client()
    request_kwargs, model = _build_request_kwargs(question, chunks, model, attached)

    logger.info("[rag] ask: model=%s, chunks=%d, comments=%d, q=%r",
                model, len(chunks), len(attached), question[:80])
    # Retry на транзиентних помилках (429/5xx/timeout) — T6.3. Увесь виклик
    # заново на кожній спробі, безпечно тут: get_final_message() нічого не
    # віддає споживачу до повного завершення.
    result = text_polishing._stream_with_retry(client, timeout, request_kwargs, what="rag-ask")

    answer = "".join(b.text for b in result.content if b.type == "text").strip()
    usage = result.usage
    return {
        "answer": answer,
        "sources": sources,
        "found": len(sources),
        "model": result.model,
        "vector_available": res.get("vector_available", False),
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def _sse(event: str, data: dict) -> str:
    """Сформувати один SSE-кадр. data JSON-кодуємо (newlines зберігаються як \\n)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def answer_question_stream(
    db_path: str,
    question: str,
    top_k: int = _DEFAULT_TOP_K,
    model: Optional[str] = None,
    timeout: float = 180.0,
    category_id: Optional[int] = None,
    project: Optional[str] = None,
    explain: bool = False,
) -> Iterator[str]:
    """Стрім-версія answer_question. category_id — обмежити напрямком. explain —
    Історія 05, те саме, що в answer_question (не міняє формат SSE-подій).
    Yields SSE-кадри:
      event: sources — {sources, found, vector_available} (одразу після retrieval)
      event: delta   — {text} (токени відповіді по мірі надходження)
      event: done    — {model, *_tokens}
      event: error   — {error}
    """
    question = (question or "").strip()
    if not question:
        yield _sse("error", {"error": "Порожнє питання"})
        return

    scope_tids = _resolve_project(db_path, project)
    res = retrieval.search(db_path, question, top_k=top_k, category_id=category_id,
                           scope_tids=scope_tids,
                           rerank=_RERANK_ENABLED,
                           explain=explain)
    chunks = retrieval.attach_thread_context(db_path, res["chunks"])
    attached = retrieval.attach_comments(db_path, chunks)
    # Той самий порядок, що й у контексті: клієнт резолвить [n] як sources[n-1],
    # тож підшиті коментарі мусять бути ТУТ, а не окремим полем — вони
    # пронумеровані нарівні з рештою (див. order_citables).
    sources = order_citables(chunks, attached)
    yield _sse("sources", {
        "sources": sources, "found": len(sources),
        "retrieved": len(chunks),      # скільки реально знайшов пошук
        "attached": len(attached),     # скільки підшито без збігу
        "vector_available": res.get("vector_available", False),
    })

    if not chunks:
        yield _sse("delta", {"text": "У архіві не знайдено релевантної інформації за цим запитом."})
        yield _sse("done", {"found": 0, "model": ""})
        return

    try:
        client = text_polishing._get_client()
        request_kwargs, model = _build_request_kwargs(question, chunks, model, attached)
        logger.info("[rag] ask-stream: model=%s, chunks=%d, comments=%d, q=%r",
                    model, len(chunks), len(attached), question[:80])

        # Retry на транзиентних помилках (429/5xx/timeout) — T6.3. Повторюємо
        # ВЕСЬ виклик заново, АЛЕ лише поки в цій спробі ще жоден delta не
        # був відданий споживачу (SSE-клієнту) — після першого delta повтор
        # небезпечний (дубльований/пошкоджений вивід), тому далі помилка
        # прокидається як є в зовнішній except.
        attempt = 0
        final = None
        while True:
            yielded_any = False
            try:
                with client.with_options(timeout=timeout).messages.stream(**request_kwargs) as stream:
                    for text in stream.text_stream:
                        if text:
                            yielded_any = True
                            yield _sse("delta", {"text": text})
                    final = stream.get_final_message()
                break
            except Exception as exc:
                if yielded_any or attempt >= claude_retry.DEFAULT_MAX_RETRIES or not is_retryable_error(exc):
                    raise
                delay = claude_retry.backoff_delay(attempt)
                logger.warning(
                    "[rag] ask-stream: транзиентна помилка (спроба %d/%d): %s — повтор через %.1fс",
                    attempt + 1, claude_retry.DEFAULT_MAX_RETRIES + 1, exc, delay,
                )
                time.sleep(delay)
                attempt += 1

        usage = final.usage
        yield _sse("done", {
            "model": final.model,
            "found": len(chunks),
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        })
    except Exception as e:
        logger.error("[rag] ask-stream failed: %s", e, exc_info=True)
        yield _sse("error", {"error": "Помилка RAG. Перевірте логи."})
