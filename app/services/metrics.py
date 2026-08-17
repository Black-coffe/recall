"""Простой in-memory лічильник для /api/metrics (Phase 6.3).

Не залежимо від prometheus_client — щоб не тягнути зайву залежність.
Формат вихідного тексту повністю Prometheus-сумісний (text/plain;
version=0.0.4), тож scrape через Prometheus / Grafana працює.

Метрики:
- whisper_transcriptions_total{model, source} (counter)
- whisper_transcription_duration_seconds{model} (histogram-lite — sum + count)
- whisper_youtube_downloads_total (counter)
- whisper_polish_total{model} (counter)
- whisper_polish_tokens_total{model, kind} (counter; kind=input|output|cache_read)
- whisper_active_jobs (gauge — підтягується з job_queue)
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict, Tuple


class MetricsRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[Tuple[str, Tuple], float] = defaultdict(float)
        self._duration_sum: Dict[Tuple[str, Tuple], float] = defaultdict(float)
        self._duration_count: Dict[Tuple[str, Tuple], int] = defaultdict(int)

    @staticmethod
    def _label_tuple(labels: dict) -> Tuple:
        return tuple(sorted((labels or {}).items()))

    def inc(self, name: str, value: float = 1.0, **labels):
        key = (name, self._label_tuple(labels))
        with self._lock:
            self._counters[key] += value

    def observe_duration(self, name: str, seconds: float, **labels):
        key = (name, self._label_tuple(labels))
        with self._lock:
            self._duration_sum[key] += seconds
            self._duration_count[key] += 1

    def render(self, gauges: Dict[str, float] = None) -> str:
        """Render у Prometheus exposition format."""
        lines = []
        with self._lock:
            counters = dict(self._counters)
            dur_sum = dict(self._duration_sum)
            dur_count = dict(self._duration_count)

        # Counters
        seen_help: set = set()
        for (name, labels), val in sorted(counters.items()):
            if name not in seen_help:
                lines.append(f"# HELP {name} Counter (Whisper UI)")
                lines.append(f"# TYPE {name} counter")
                seen_help.add(name)
            label_str = self._render_labels(labels)
            lines.append(f"{name}{label_str} {val}")

        # Duration sum/count (histogram-lite)
        for (name, labels), val in sorted(dur_sum.items()):
            base = name.replace("_seconds", "")
            label_str = self._render_labels(labels)
            cnt = dur_count.get((name, labels), 0)
            if name not in seen_help:
                lines.append(f"# HELP {base}_seconds Total time (Whisper UI)")
                lines.append(f"# TYPE {base}_seconds summary")
                seen_help.add(name)
            lines.append(f"{base}_seconds_sum{label_str} {val}")
            lines.append(f"{base}_seconds_count{label_str} {cnt}")

        # Gauges (з зовнішнього коду — job_queue, system_monitor)
        if gauges:
            for name, val in sorted(gauges.items()):
                lines.append(f"# HELP {name} Gauge (Whisper UI)")
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {val}")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_labels(labels_tuple: Tuple) -> str:
        if not labels_tuple:
            return ""
        parts = []
        for k, v in labels_tuple:
            v_escaped = str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
            parts.append(f'{k}="{v_escaped}"')
        return "{" + ",".join(parts) + "}"


# Глобальний реєстр на процес
metrics = MetricsRegistry()
