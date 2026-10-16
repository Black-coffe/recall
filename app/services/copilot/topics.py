"""Co-pilot — topic state machine driven by an LLM label (Phase 19, Step 2 → rework).

Topics used to be detected by the cosine similarity of the window embeddings to a centroid. On
real calls this proved brittle: e5 has a high similarity "floor" (cosine of
neighbouring windows ~0.91), so a fixed threshold either did not split at all (1 topic in 47 min)
or over-segmented. A threshold on the *label* embeddings does not work either — their distributions
"same topic" (0.83–0.89) and "different topics" (0.77–0.87) overlap.

So the topic-continuity decision is made by the LLM itself in triage: seeing the list of
topics already known to the session + the current one, it returns ``topic_status`` (continue/shift/return)
and, if needed, ``return_to_index``. This tracker is only a deterministic topic registry that
applies the LLM decision (no I/O, no embeddings). The semantics of "what is a new topic"
is resolved by the model, not by a magic threshold.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional


def _norm_label(text: str, max_chars: int = 80) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return (t[:max_chars].rstrip() + "…") if len(t) > max_chars else t


class TopicTracker:
    """Session topic registry driven by the LLM decision. One instance per co-pilot session.

    ``embed_fn`` is kept in the signature for call backward compatibility (worker/tests),
    but is NOT used — topics no longer depend on embeddings.
    """

    def __init__(self, embed_fn: Optional[Callable[..., Any]] = None, **_ignored):
        # [{index, label, first_ts, last_ts}]
        self.topics: list[dict] = []
        self.current: Optional[int] = None

    # ------------------------------------------------------------- public API

    def topic_list(self) -> list[dict]:
        """Compact topic list for the triage prompt: [{index, label}]."""
        return [{"index": t["index"], "label": t["label"]} for t in self.topics]

    def apply(self, *, status: Optional[str], label: Optional[str],
              return_to: Optional[int], ts: float) -> dict:
        """Apply the LLM topic decision. Returns an event:
          {"action": "shift"|"return"|"continue", "topic_index", "label",
           "returned_from"?, "relabeled"?, "similarity": None, "ts"}.

        Logic:
          * no topics → always create the first one (shift), whatever the model said;
          * return with a valid return_to (another existing index) → go back;
          * shift → new topic with the label;
          * otherwise continue → stay, rename the current one if needed.
        Invalid values (return to a non-existent/current index, empty label on
        shift) degrade to continue — protection against small-quant hallucinations.
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
            # invalid return → treat as continue
            status = "continue"

        if status == "shift" and label:
            return self._new_topic(label, ts, returned_from=self.current)

        # continue (or degraded shift/return)
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
