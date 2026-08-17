"""Recording session storage (Phase 9.2).

Кожна сесія = окрема папка в ``RECORDING_DIR/<session_id>/`` з:

- ``manifest.json`` — state machine, оновлюється атомарно через tmp+rename.
- ``mic.pcm``, ``system.pcm`` — raw PCM, append-only, дописуються через
  :class:`ChunkedPcmWriter` (інший модуль).
- ``mic.wav``, ``system.wav``, ``final.mp3`` — створюються на finalize
  (Phase 9.4).

Manifest schema (version 1):

.. code-block:: json

    {
      "session_id": "rec_abc123",
      "version": 1,
      "status": "recording",
      "started_at": "2026-05-04T12:34:56+00:00",
      "sample_rate": 48000,
      "channels": 2,
      "bytes_per_sample": 2,
      "streams": {
        "mic":    {"device": "Microphone (...)", "bytes": 14400000, "error": null},
        "system": {"device": "Speakers Loopback", "bytes": 14400000, "error": null}
      },
      "segments": [
        {"index": 0, "start_ts": "...", "end_ts": "...", "duration_sec": 240.5,
         "mic_offset_bytes": 0, "system_offset_bytes": 0}
      ],
      "name": null,
      "auto_name": "Запис 2026-05-04 12:34"
    }

Crash-resilience: атомарний rename гарантує що manifest на диску
завжди валідний JSON. Якщо процес умре посеред write — тимчасовий
файл лишається .tmp і ігнорується; основний manifest актуальний на
момент попереднього успішного rename'у.

Thread-safety: усі мутації йдуть через :meth:`modify` під per-session
RLock. Концурентний read дозволений.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


MANIFEST_VERSION = 2
MANIFEST_FILENAME = 'manifest.json'

# Single source of truth для валідації session_id. Захист від path traversal:
# session_id використовується як ім'я підпапки в RECORDING_DIR і у URL-routes,
# тому будь-які '..', '/', '\' або інші spec-символи блокуємо. Формат:
# rec_ + 1..64 символів [a-f0-9] (hex з uuid4().hex[:16] = 16 hex chars,
# беремо ширше обмеження для майбутньої гнучкості). У тестах допускаємо
# алфавітні символи через підкреслювання — `^rec_[a-z0-9_]+$`.
_SESSION_ID_RE = re.compile(r'^rec_[a-z0-9_]{1,64}$')

# Допустимі статуси сесії. Recovery шукає перші три.
STATUS_RECORDING = 'recording'
STATUS_PAUSED = 'paused'
STATUS_STOPPING = 'stopping'
STATUS_FINALIZED = 'finalized'
STATUS_CRASHED = 'crashed'
STATUS_DISCARDED = 'discarded'

ACTIVE_STATUSES = frozenset({STATUS_RECORDING, STATUS_PAUSED, STATUS_STOPPING})

STREAM_MIC = 'mic'
STREAM_SYSTEM = 'system'


class SessionStoreError(RuntimeError):
    """Помилка операції над сесією."""


class SessionStore:
    """Файлове сховище для recording-сесій з atomic manifest.

    Не зберігає стан у пам'яті (окрім locks) — manifest читається з
    диска на кожен ``modify``. Це навмисно: робить store re-entrant
    і простішим для recovery.
    """

    def __init__(self, base_dir: Path | str):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # --------------------------------------------------------- public api

    def create(
        self,
        session_id: str,
        sample_rate: int,
        channels: int,
        mic_device_name: Optional[str] = None,
        system_device_name: Optional[str] = None,
        auto_name: Optional[str] = None,
    ) -> dict:
        """Створює нову сесію. Повертає створений manifest.

        Якщо session_id вже існує — кидає :class:`SessionStoreError`.
        Валідація session_id (path traversal protection) — у
        :meth:`session_dir`.
        """
        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            raise SessionStoreError(f"Сесія {session_id} вже існує")

        session_dir.mkdir(parents=True)
        now = _now_iso()
        manifest: dict[str, Any] = {
            'session_id': session_id,
            'version': MANIFEST_VERSION,
            'status': STATUS_RECORDING,
            'started_at': now,
            'sample_rate': int(sample_rate),
            'channels': int(channels),
            'bytes_per_sample': 2,
            'streams': {
                STREAM_MIC: {
                    'device': mic_device_name,
                    'bytes': 0,
                    'error': None,
                    'enabled': mic_device_name is not None,
                    # Per-stream native параметри. Заповнюються
                    # _open_stream після resolve_device_params, бо
                    # девайс може не підтримувати запитуваний rate
                    # (наприклад моно-mic 88200Hz при requested 48000/2).
                    'sample_rate': None,
                    'channels': None,
                },
                STREAM_SYSTEM: {
                    'device': system_device_name,
                    'bytes': 0,
                    'error': None,
                    'enabled': system_device_name is not None,
                    'sample_rate': None,
                    'channels': None,
                },
                'video': [],
            },
            'segments': [{
                'index': 0,
                'start_ts': now,
                'end_ts': None,
                'duration_sec': None,
                'mic_offset_bytes': 0,
                'system_offset_bytes': 0,
            }],
            'name': None,
            'auto_name': auto_name or _default_auto_name(),
        }
        self._write_manifest(session_dir, manifest)
        return manifest

    def session_dir(self, session_id: str) -> Path:
        """Шлях до папки сесії. Валідує session_id проти path traversal.

        Кидає :class:`SessionStoreError` якщо session_id містить
        небезпечні символи ('..', '/', '\\') або не відповідає
        патерну ``^rec_[a-z0-9_]{1,64}$``. Це гарантує що
        ``base_dir / session_id`` ніколи не виходить за межі base_dir.
        """
        if not _SESSION_ID_RE.fullmatch(session_id or ''):
            raise SessionStoreError(
                f"Невалідний session_id: {session_id!r} "
                "(мусить відповідати ^rec_[a-z0-9_]{1,64}$)"
            )
        return self.base_dir / session_id

    def pcm_path(self, session_id: str, stream: str) -> Path:
        """Шлях до raw PCM файлу для стріму ('mic' або 'system')."""
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Невідомий stream: {stream!r}")
        return self.session_dir(session_id) / f'{stream}.pcm'

    def read(self, session_id: str) -> dict:
        """Читає manifest з диска. Кидає :class:`SessionStoreError`
        якщо сесія/manifest відсутні чи зламані.

        Self-healing шляхів (03.07.2026): manifest зберігає АБСОЛЮТНІ
        шляхи (``final_mp3_path``, ``primary_video_path``,
        ``streams.video[].path``). Після переїзду/перейменування кореня
        проєкту ці шляхи стають мертвими — файл фізично лежить у тій же
        сесійній папці, просто за старим абсолютним префіксом. Якщо шлях
        не існує, а файл з тим самим basename знайдено в папці ЦІЄЇ
        сесії — підміняємо його У ПОВЕРНУТОМУ dict. Це read-only:
        на диск нічого не пишеться (лікування живе, лише поки живий цей
        dict; якщо потрібне persist — це зробить окремий modify()).
        """
        session_dir = self.session_dir(session_id)
        manifest = self._read_manifest(session_dir)
        healed = self._heal_paths(manifest, session_dir)
        if healed:
            logger.debug(
                "Manifest %s: self-healing підмінив %d шлях(ів) на актуальні "
                "(файл(и) знайдено в папці сесії за старим basename)",
                session_id, healed,
            )
        return manifest

    # --- self-healing (repath after project move)

    _REPATH_TOP_LEVEL_FIELDS = ('final_mp3_path', 'primary_video_path')

    @classmethod
    def _heal_paths(cls, manifest: dict, session_dir: Path) -> int:
        """Мутує ``manifest`` in-place, підміняючи мертві абсолютні шляхи
        на живі (той самий basename у ``session_dir``). Повертає кількість
        підмінених полів."""
        healed = 0
        for field in cls._REPATH_TOP_LEVEL_FIELDS:
            fixed = cls._heal_one_path(manifest.get(field), session_dir)
            if fixed is not None:
                manifest[field] = fixed
                healed += 1

        video_tracks = (manifest.get('streams') or {}).get('video') or []
        for track in video_tracks:
            fixed = cls._heal_one_path(track.get('path'), session_dir)
            if fixed is not None:
                track['path'] = fixed
                healed += 1

        return healed

    @staticmethod
    def _heal_one_path(path: Optional[str], session_dir: Path) -> Optional[str]:
        """``None`` якщо шлях не треба чіпати; інакше — новий актуальний шлях."""
        if not path or os.path.isfile(path):
            return None
        candidate = session_dir / os.path.basename(path)
        if candidate.is_file():
            return str(candidate)
        return None

    def exists(self, session_id: str) -> bool:
        manifest_path = self.session_dir(session_id) / MANIFEST_FILENAME
        return manifest_path.is_file()

    def modify(
        self,
        session_id: str,
        modifier: Callable[[dict], None],
    ) -> dict:
        """Атомарне read-modify-write під session lock.

        ``modifier(manifest)`` мутує manifest in-place. Повертає новий
        manifest після запису.
        """
        with self._lock_for(session_id):
            session_dir = self.session_dir(session_id)
            manifest = self._read_manifest(session_dir)
            modifier(manifest)
            self._write_manifest(session_dir, manifest)
            return manifest

    # --- shorthand mutators

    def update_status(self, session_id: str, status: str) -> dict:
        def _m(mf: dict) -> None:
            mf['status'] = status
        return self.modify(session_id, _m)

    def set_name(self, session_id: str, name: Optional[str]) -> dict:
        def _m(mf: dict) -> None:
            mf['name'] = name
        return self.modify(session_id, _m)

    def update_stream_bytes(
        self,
        session_id: str,
        stream: str,
        total_bytes: int,
    ) -> None:
        """Оновити лічильник total bytes для стріму. Викликається з
        ChunkedPcmWriter після успішного flush+fsync."""
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Невідомий stream: {stream!r}")
        def _m(mf: dict) -> None:
            mf['streams'][stream]['bytes'] = int(total_bytes)
        self.modify(session_id, _m)

    def set_stream_error(
        self,
        session_id: str,
        stream: str,
        error: Optional[str],
    ) -> None:
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Невідомий stream: {stream!r}")
        def _m(mf: dict) -> None:
            mf['streams'][stream]['error'] = error
        self.modify(session_id, _m)

    def set_stream_params(
        self,
        session_id: str,
        stream: str,
        sample_rate: int,
        channels: int,
    ) -> None:
        """Зафіксувати native (rate, channels) для стріму.

        Викликається з RecordingService._open_stream() після
        resolve_device_params() — гарантує що finalize читатиме
        правильні параметри для WAV-хедера, навіть якщо девайс
        не підтримував requested rate (моно-мікрофон з 88200Hz
        при requested 48000/2).
        """
        if stream not in (STREAM_MIC, STREAM_SYSTEM):
            raise ValueError(f"Невідомий stream: {stream!r}")
        def _m(mf: dict) -> None:
            mf['streams'][stream]['sample_rate'] = int(sample_rate)
            mf['streams'][stream]['channels'] = int(channels)
        self.modify(session_id, _m)

    def add_video_track(self, session_id: str, track: dict) -> None:
        """Додати запис відео-треку до manifest['streams']['video'].

        Tolerant of v1 manifests that lack the 'video' key: initialises it
        on-the-fly so old sessions don't crash.
        """
        def _m(mf: dict) -> None:
            vids = mf['streams'].setdefault('video', [])
            tid = track.get('track_id')
            # upsert по track_id: оновити наявний трек, а не плодити дублі
            # (кличеться і на start, і на stop супервізора).
            for i, t in enumerate(vids):
                if t.get('track_id') == tid:
                    vids[i] = {**t, **dict(track)}
                    return
            vids.append(dict(track))
        self.modify(session_id, _m)

    def update_video_track(self, session_id: str, track_id: str, **fields) -> None:
        """Оновити поля існуючого відео-треку за його track_id.

        Якщо track_id не знайдено — логується warning, маніфест не змінюється.
        Tolerant of v1 manifests that lack the 'video' key.
        """
        def _m(mf: dict) -> None:
            tracks = mf['streams'].get('video', [])
            for t in tracks:
                if t.get('track_id') == track_id:
                    t.update(fields)
                    return
            logger.warning(
                "update_video_track: track_id %r not found in session %s",
                track_id, session_id,
            )
        self.modify(session_id, _m)

    def open_segment(self, session_id: str) -> int:
        """Закриває попередній segment (якщо відкритий) і відкриває новий.
        Повертає index нового segment'у.

        Викликається на resume після pause. На create перший segment
        створюється автоматично — цю функцію викликати не потрібно.
        """
        def _m(mf: dict) -> None:
            now = _now_iso()
            segments = mf['segments']
            if segments and segments[-1].get('end_ts') is None:
                # Закриваємо відкритий segment
                last = segments[-1]
                last['end_ts'] = now
                last['duration_sec'] = _iso_diff_seconds(last['start_ts'], now)
            mic_total = mf['streams'][STREAM_MIC]['bytes']
            sys_total = mf['streams'][STREAM_SYSTEM]['bytes']
            segments.append({
                'index': len(segments),
                'start_ts': now,
                'end_ts': None,
                'duration_sec': None,
                'mic_offset_bytes': mic_total,
                'system_offset_bytes': sys_total,
            })
        manifest = self.modify(session_id, _m)
        return manifest['segments'][-1]['index']

    def close_segment(self, session_id: str) -> None:
        """Закриває останній відкритий segment (на pause/stop)."""
        def _m(mf: dict) -> None:
            segments = mf['segments']
            if segments and segments[-1].get('end_ts') is None:
                last = segments[-1]
                now = _now_iso()
                last['end_ts'] = now
                last['duration_sec'] = _iso_diff_seconds(last['start_ts'], now)
        self.modify(session_id, _m)

    # --- queries

    def list_all(self) -> list[dict]:
        """Список manifest'ів усіх сесій. Зламані manifest'и пропускає
        з warning'ом."""
        out: list[dict] = []
        if not self.base_dir.is_dir():
            return out
        for entry in self.base_dir.iterdir():
            if not entry.is_dir():
                continue
            manifest_path = entry / MANIFEST_FILENAME
            if not manifest_path.is_file():
                continue
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Зіпсований manifest у %s: %s", entry, e)
        return out

    def list_orphaned(self) -> list[dict]:
        """Сесії в активному статусі (recording/paused/stopping).
        Викликається при старті додатку для recovery."""
        return [m for m in self.list_all() if m.get('status') in ACTIVE_STATUSES]

    def list_unfinalized(self) -> list[dict]:
        """Сесії ``crashed``, які так і не отримали ``final.mp3``.

        Другий (після :meth:`list_orphaned`) вхід у recovery. Закриває діру,
        через яку запис міг зникнути НАЗАВЖДИ: перший рестарт переводив активну
        сесію в ``crashed`` і ставив finalize у чергу, а другий рестарт убивав
        уже сам job (``jobs.state='crashed'``). Після цього сесію не бачив ніхто —
        ``list_orphaned`` дивиться тільки на ACTIVE_STATUSES, ``reconcile`` —
        тільки на вже фіналізовані, ``/save`` для ``crashed`` віддає 500, а
        UI-банер ``/api/recordings/recovered`` одноразовий. PCM лежав на диску
        мертвим вантажем (реальний випадок 21.07.2026: 10-годинна сесія, 8.2 ГБ).

        Повертає сесії зі ``status == 'crashed'``, де ``final_mp3_path`` порожній
        або файл за ним не існує (переїзд проєкту / ручне видалення). Сесії з
        живим MP3 не чіпаємо — їх підбере ``reconcile_recordings``.
        """
        out: list[dict] = []
        for m in self.list_all():
            if m.get('status') != STATUS_CRASHED:
                continue
            final = m.get('final_mp3_path')
            if final and Path(final).is_file():
                continue
            out.append(m)
        return out

    def delete(self, session_id: str) -> None:
        """Видаляє папку сесії повністю. Idempotent."""
        import shutil
        session_dir = self.session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir, ignore_errors=True)
        with self._locks_guard:
            self._locks.pop(session_id, None)

    # ---------------------------------------------------------- internals

    def _lock_for(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
            return lock

    def _read_manifest(self, session_dir: Path) -> dict:
        manifest_path = session_dir / MANIFEST_FILENAME
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except FileNotFoundError as e:
            raise SessionStoreError(f"Manifest не знайдено: {manifest_path}") from e
        except json.JSONDecodeError as e:
            raise SessionStoreError(f"Manifest зламано ({manifest_path}): {e}") from e

        # Phase 9.10 hardening: попередження якщо schema-version не співпадає.
        # Зараз ми лише warn'аємо і повертаємо as-is — наступні версії
        # додадуть logic міграції старих manifest'ів. Це робить майбутні
        # schema changes безпечнішими: ми хоча б знаємо що manifest старий.
        version = manifest.get('version')
        if version is not None and version != MANIFEST_VERSION:
            logger.warning(
                "Manifest %s має version=%s, очікується %s — продовжую "
                "без міграції (поведінка може бути неочікувана)",
                manifest_path, version, MANIFEST_VERSION,
            )
        return manifest

    def _write_manifest(self, session_dir: Path, manifest: dict) -> None:
        """Atomic write: запис у tmp + fsync + os.replace.

        Інваріант: на диску завжди валідний JSON. Якщо процес умре
        посеред write — tmp файл залишається, основний нерушений.
        """
        manifest_path = session_dir / MANIFEST_FILENAME
        tmp_path = session_dir / (MANIFEST_FILENAME + '.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, manifest_path)


# ------------------------------------------------------------- helpers

def _now_iso() -> str:
    """ISO-8601 в UTC з timezone-suffix."""
    return datetime.now(tz=timezone.utc).isoformat(timespec='seconds')


def _iso_diff_seconds(start_iso: str, end_iso: str) -> float:
    """Кількість секунд між двома ISO-датами."""
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    return round((end - start).total_seconds(), 3)


def _default_auto_name() -> str:
    """Auto-suggested name типу 'Запис 2026-05-04 14:32' (локальний час)."""
    now = datetime.now()
    return f"Запис {now.strftime('%Y-%m-%d %H:%M')}"
