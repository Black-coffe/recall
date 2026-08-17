"""Co-pilot — топік-стейт-машина, керована LLM-міткою (Phase 19, Крок 2 → доробка).

Раніше теми визначались косинусною схожістю ембеддингів вікна до центроїда. На
реальних дзвінках це виявилось крихким: e5 має високу «підлогу» схожості (косинус
сусідніх вікон ~0.91), тож фіксований поріг або не дробив зовсім (1 тема за 47 хв),
або пересегментував. Поріг на ембеддингах *міток* теж не працює — їхні розподіли
«одна тема» (0.83–0.89) і «різні теми» (0.77–0.87) перетинаються.

Тому рішення про спадкоємність теми приймає САМА LLM у триажі: бачачи список уже
відомих тем сесії + поточну, вона повертає ``topic_status`` (continue/shift/return)
і, за потреби, ``return_to_index``. Цей трекер — лише детермінований реєстр тем, що
застосовує рішення LLM (без I/O, без ембеддингів). Семантику «що є нова тема»
розв'язує модель, а не магічний поріг.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional


def _norm_label(text: str, max_chars: int = 80) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return (t[:max_chars].rstrip() + "…") if len(t) > max_chars else t


class TopicTracker:
    """Реєстр тем сесії, керований рішенням LLM. Один інстанс на копілот-сесію.

    ``embed_fn`` лишено в сигнатурі для зворотної сумісності виклику (воркер/тести),
    але НЕ використовується — теми більше не залежать від ембеддингів.
    """

    def __init__(self, embed_fn: Optional[Callable[..., Any]] = None, **_ignored):
        # [{index, label, first_ts, last_ts}]
        self.topics: list[dict] = []
        self.current: Optional[int] = None

    # ------------------------------------------------------------- public API

    def topic_list(self) -> list[dict]:
        """Компактний список тем для промпта триажу: [{index, label}]."""
        return [{"index": t["index"], "label": t["label"]} for t in self.topics]

    def apply(self, *, status: Optional[str], label: Optional[str],
              return_to: Optional[int], ts: float) -> dict:
        """Застосувати рішення LLM про тему. Повертає подію:
          {"action": "shift"|"return"|"continue", "topic_index", "label",
           "returned_from"?, "relabeled"?, "similarity": None, "ts"}.

        Логіка:
          * нема тем → завжди створюємо першу (shift), хай що сказала модель;
          * return з валідним return_to (інший наявний індекс) → повертаємось;
          * shift → нова тема з міткою;
          * інакше continue → лишаємось, за потреби перейменовуємо поточну.
        Невалідні значення (return на неіснуючий/поточний індекс, порожня мітка на
        shift) деградують у continue — захист від галюцинацій малого кванта.
        """
        label = _norm_label(label or "")

        if not self.topics:
            return self._new_topic(label, ts, returned_from=None)

        if status == "return":
            ri = self._as_index(return_to)
            if ri is not None and ri != self.current:
                prev = self.current
                self.current = ri
                self.topics[ri]["last_ts"] = ts
                if label:
                    self.topics[ri]["label"] = label
                return {"action": "return", "topic_index": ri,
                        "label": self.topics[ri]["label"], "returned_from": prev,
                        "similarity": None, "ts": ts}
            # невалідний return → трактуємо як continue
            status = "continue"

        if status == "shift" and label:
            return self._new_topic(label, ts, returned_from=self.current)

        # continue (або деградований shift/return)
        cur = self.topics[self.current]
        cur["last_ts"] = ts
        relabeled = bool(label and label != cur["label"])
        if relabeled:
            cur["label"] = label
        return {"action": "continue", "topic_index": self.current,
                "label": cur["label"], "relabeled": relabeled,
                "similarity": None, "ts": ts}

    # ------------------------------------------------------------- internals

    def _new_topic(self, label: str, ts: float, *, returned_from: Optional[int]) -> dict:
        idx = len(self.topics)
        self.topics.append({
            "index": idx, "label": label or f"Тема {idx + 1}",
            "first_ts": ts, "last_ts": ts,
        })
        self.current = idx
        return {"action": "shift", "topic_index": idx,
                "label": self.topics[idx]["label"], "returned_from": returned_from,
                "similarity": None, "ts": ts}

    def _as_index(self, v: Any) -> Optional[int]:
        try:
            i = int(v)
        except (TypeError, ValueError):
            return None
        return i if 0 <= i < len(self.topics) else None
