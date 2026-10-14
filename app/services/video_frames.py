"""Адаптивний відбір ключових кадрів відеозапису (spec video-visual-timeline, історія 01).

Задача: мінімальний набір кадрів, що покриває кожну зміну картинки з точністю до
1 с. Два методи (обидва заміряні, цифри — docs/video-timeline.md):

* ``thumbs`` (дефолт) — ОДИН прохід декодування 1 fps у мініатюри 160×90 grayscale
  через ffmpeg rawvideo-пайп. Кожна мініатюра порівнюється з ЯКОРЕМ поточного
  відрізка (першою мініатюрою відрізка), а не лише з попередньою — так ловиться і
  різка зміна, і повільний дрейф (прокрутка), і A→B→A коротше кроку.
* ``bisect`` — опорні кадри кожні ``step`` с через seek (``-ss`` перед ``-i``),
  інтервал із різними кінцями ділиться навпіл до ``min_res``. Дешевший на дуже
  статичному відео, але пропускає A→B→A, коротше ``step`` (концi інтервалу
  збігаються) — це задокументована межа методу.

Порівняння ``diff_score(a, b, mask)``: частка пікселів маски, що змінились більше ніж
на ``PIX_DELTA`` (з 255). Маска = ``ignore_regions`` (частки кадру) мінус авто-маска
пікселів, що мерехтять постійно (годинник у треї, плитки вебкамер, курсор).

Зображення кадрів НЕ зберігаються: ``grab_frame`` ріже кадр з відео на запит.
Модуль не знає про БД — це робить ``video_analysis``.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, asdict
from io import BytesIO
from typing import Callable, Optional, Sequence

import numpy as np

from app.utils.proc import NO_WINDOW

logger = logging.getLogger(__name__)

ALGO_VERSION = 1

THUMB_W, THUMB_H = 160, 90
PIX_DELTA = 12                 # |Δ| яскравості, з якої піксель вважається зміненим
DEFAULT_THRESHOLD = 0.02       # частка змінених пікселів маски (замір на живих даних, docs/video-timeline.md)
DEFAULT_STEP = 10.0
DEFAULT_MIN_RES = 1.0
DEFAULT_MAX_FRAMES = 1500
DEFAULT_METHOD = 'thumbs'
DEFAULT_SETTLE = 2.0           # серія змін частіше ніж раз на 2 с (набір, прокрутка) — один відрізок
FLICKER_SHARE = 0.4            # піксель, змінений у >40 % пар, — шум, не зміст
REP_OFFSET = 0.7               # представник відрізка: +0.7 с від початку
BLANK_STD = 4.0                # std мініатюри нижче — порожній кадр


class NoVideoError(Exception):
    """Файл не має відеодоріжки (або не читається як відео)."""


@dataclass
class Segment:
    start: float
    end: float
    frame_time: float
    change_score: float          # оцінка зміни, що ВІДКРИЛА відрізок (0 для першого)
    std: float = 0.0             # контраст мініатюри представника (для типу blank)
    dhash: str = ''              # 64-біт dHash представника (hex) — для повторів

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def _ffmpeg(ffmpeg: Optional[str] = None) -> str:
    if ffmpeg:
        return ffmpeg
    try:
        from app.services.recording.video_probe import ffmpeg_path
        return ffmpeg_path()
    except Exception:
        logger.debug("ffmpeg_path() недоступний, беру ffmpeg з PATH", exc_info=True)
        return 'ffmpeg'


def _ffprobe(ff: str) -> str:
    cand = os.path.join(os.path.dirname(ff), 'ffprobe.exe')
    return cand if os.path.dirname(ff) and os.path.isfile(cand) else 'ffprobe'


def probe(video_path: str, ffmpeg: Optional[str] = None) -> dict:
    """Тривалість, розмір і fps першої відеодоріжки. Нема відео → NoVideoError."""
    if not os.path.isfile(video_path):
        raise NoVideoError(f'файл не знайдено: {video_path}')
    ff = _ffmpeg(ffmpeg)
    try:
        r = subprocess.run(
            [_ffprobe(ff), '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height,avg_frame_rate:format=duration',
             '-of', 'json', video_path],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW,
        )
        data = json.loads(r.stdout or '{}')
    except Exception as e:
        raise NoVideoError(f'ffprobe не прочитав файл: {e}') from e
    streams = data.get('streams') or []
    if not streams:
        raise NoVideoError('у файлі немає відеодоріжки')
    s = streams[0]
    try:
        num, den = (s.get('avg_frame_rate') or '0/1').split('/')
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        logger.debug("avg_frame_rate не розібрано: %r", s.get('avg_frame_rate'), exc_info=True)
        fps = 0.0
    duration = float((data.get('format') or {}).get('duration') or 0.0)
    if duration <= 0:
        raise NoVideoError('тривалість відео невідома або нульова')
    return {'duration': duration, 'width': int(s.get('width') or 0),
            'height': int(s.get('height') or 0), 'fps': fps}


def _run_raw(cmd: list, timeout: float) -> bytes:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    if r.returncode != 0 and not r.stdout:
        raise RuntimeError((r.stderr or b'')[-300:].decode('utf-8', 'replace'))
    return r.stdout


def grab_small(video_path: str, t: float, *, w: int = THUMB_W, h: int = THUMB_H,
               ffmpeg: Optional[str] = None) -> Optional[np.ndarray]:
    """Один кадр у момент ``t`` як grayscale-масив h×w (швидкий seek). None — кадру нема."""
    ff = _ffmpeg(ffmpeg)
    raw = _run_raw([ff, '-v', 'error', '-ss', f'{max(t, 0):.3f}', '-i', video_path,
                    '-frames:v', '1', '-vf', f'scale={w}:{h}:flags=area,format=gray',
                    '-f', 'rawvideo', '-'], timeout=60)
    if len(raw) < w * h:
        return None
    return np.frombuffer(raw[:w * h], dtype=np.uint8).reshape(h, w)


def grab_frame(video_path: str, t: float, *, max_width: Optional[int] = None,
               ffmpeg: Optional[str] = None):
    """Кадр у момент ``t`` як PIL.Image (RGB). Нічого не пишеться на диск."""
    from PIL import Image
    ff = _ffmpeg(ffmpeg)
    vf = f"scale='min({int(max_width)},iw)':-2" if max_width else 'null'
    raw = _run_raw([ff, '-v', 'error', '-ss', f'{max(t, 0):.3f}', '-i', video_path,
                    '-frames:v', '1', '-vf', vf, '-f', 'image2pipe', '-vcodec', 'png', '-'],
                   timeout=60)
    if not raw:
        raise NoVideoError(f'кадр на {t:.1f} с не декодувався')
    img = Image.open(BytesIO(raw))
    img.load()
    return img.convert('RGB')


def thumbs_pass(video_path: str, duration: float, *, fps: float = 1.0,
                ffmpeg: Optional[str] = None,
                progress: Optional[Callable[[float], None]] = None) -> np.ndarray:
    """Один прохід декодування: мініатюри (N, THUMB_H, THUMB_W) uint8, мініатюра i = момент i/fps."""
    ff = _ffmpeg(ffmpeg)
    frame_bytes = THUMB_W * THUMB_H
    cmd = [ff, '-v', 'error', '-i', video_path,
           '-vf', f'fps={fps},scale={THUMB_W}:{THUMB_H}:flags=area,format=gray',
           '-f', 'rawvideo', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=NO_WINDOW)
    chunks: list[bytes] = []
    expected = max(1, int(duration * fps))
    got = 0
    try:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes * 64)
            if not buf:
                break
            chunks.append(buf)
            got += len(buf) // frame_bytes
            if progress:
                progress(min(1.0, got / expected))
    finally:
        proc.wait(timeout=60)
    data = b''.join(chunks)
    n = len(data) // frame_bytes
    if n == 0:
        raise NoVideoError('ffmpeg не віддав жодного кадру')
    return np.frombuffer(data[:n * frame_bytes], dtype=np.uint8).reshape(n, THUMB_H, THUMB_W)


# ---------------------------------------------------------------------------
# порівняння і маски
# ---------------------------------------------------------------------------

def region_mask(ignore_regions: Optional[Sequence[Sequence[float]]],
                h: int = THUMB_H, w: int = THUMB_W) -> np.ndarray:
    """True = піксель бере участь у порівнянні. Регіони — частки кадру [x, y, w, h]."""
    mask = np.ones((h, w), dtype=bool)
    for reg in ignore_regions or ():
        x, y, rw, rh = (float(v) for v in reg)
        x0, y0 = int(round(x * w)), int(round(y * h))
        x1, y1 = int(round((x + rw) * w)), int(round((y + rh) * h))
        mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = False
    return mask


def flicker_mask(thumbs: np.ndarray, share: float = FLICKER_SHARE) -> np.ndarray:
    """False для пікселів, що змінюються в більш ніж ``share`` сусідніх пар (шум, не зміст)."""
    if len(thumbs) < 3:
        return np.ones(thumbs.shape[1:], dtype=bool)
    changed = np.zeros(thumbs.shape[1:], dtype=np.int32)
    for i in range(1, len(thumbs)):
        changed += np.abs(thumbs[i].astype(np.int16) - thumbs[i - 1]) > PIX_DELTA
    return changed < share * (len(thumbs) - 1)


def diff_score(a: np.ndarray, b: np.ndarray, mask: Optional[np.ndarray] = None) -> float:
    """Частка пікселів маски, змінених більше ніж на PIX_DELTA."""
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIX_DELTA
    if mask is not None:
        n = int(mask.sum())
        return float((d & mask).sum()) / n if n else 0.0
    return float(d.mean())


def same(a: np.ndarray, b: np.ndarray, mask: Optional[np.ndarray] = None,
         threshold: float = DEFAULT_THRESHOLD) -> bool:
    return diff_score(a, b, mask) <= threshold


def dhash(img: np.ndarray) -> str:
    """64-бітний dHash (9×8) — для повторів за змістом між несусідніми відрізками."""
    from PIL import Image
    small = np.asarray(Image.fromarray(img).resize((9, 8), Image.BILINEAR), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return f'{int("".join("1" if b else "0" for b in bits), 2):016x}'


def hamming(h1: str, h2: str) -> int:
    return bin(int(h1, 16) ^ int(h2, 16)).count('1')


def sharpness(img: np.ndarray) -> float:
    """Дисперсія дискретного Лапласіана — більша = різкіше."""
    f = img.astype(np.float32)
    lap = (f[1:-1, :-2] + f[1:-1, 2:] + f[:-2, 1:-1] + f[2:, 1:-1] - 4 * f[1:-1, 1:-1])
    return float(lap.var())


# ---------------------------------------------------------------------------
# відбір
# ---------------------------------------------------------------------------

def _settle(bounds: list[tuple[float, float]], settle: float) -> list[tuple[float, float, float]]:
    """Серія змін, що йдуть частіше ніж раз на ``settle`` с (набір тексту, прокрутка),
    — один відрізок. → [(початок, оцінка, момент, коли серія вляглась)]."""
    out: list[tuple[float, float, float]] = []
    for t, score in bounds:
        if out and settle > 0 and score > 0 and t - out[-1][2] < settle:
            start, sc, _ = out[-1]
            out[-1] = (start, max(sc, score), t)
        else:
            out.append((t, score, t))
    return out


def _segments_from_boundaries(bounds: list[tuple[float, float]], duration: float,
                              settle: float = 0.0) -> list[Segment]:
    """bounds — [(момент зміни, оцінка)], перший (0, 0). → відрізки до наступної межі.

    Межа відрізка — момент ПЕРШОЇ зміни серії (точність ``min_res``), представник —
    кадр після ОСТАННЬОЇ зміни серії, тобто вже усталений екран."""
    runs = _settle(bounds, settle)
    segs: list[Segment] = []
    for i, (t, score, settled) in enumerate(runs):
        end = runs[i + 1][0] if i + 1 < len(runs) else duration
        if end - t <= 0:
            continue
        segs.append(Segment(start=round(t, 3), end=round(end, 3),
                            frame_time=round(min(settled + REP_OFFSET, (settled + end) / 2), 3),
                            change_score=round(score, 5)))
    return segs


def select_thumbs(thumbs: np.ndarray, duration: float, *, fps: float = 1.0,
                  threshold: float = DEFAULT_THRESHOLD,
                  mask: Optional[np.ndarray] = None,
                  settle: float = DEFAULT_SETTLE) -> list[Segment]:
    """Відрізки за мініатюрами: нова межа, коли кадр відрізняється від ЯКОРЯ відрізка."""
    bounds: list[tuple[float, float]] = [(0.0, 0.0)]
    anchor = thumbs[0]
    for i in range(1, len(thumbs)):
        score = diff_score(thumbs[i], anchor, mask)
        if score > threshold:
            bounds.append((i / fps, score))
            anchor = thumbs[i]
    segs = _segments_from_boundaries(bounds, duration, settle)
    for s in segs:
        idx = min(len(thumbs) - 1, int(s.frame_time * fps))
        s.std = round(float(thumbs[idx].std()), 2)
        s.dhash = dhash(thumbs[idx])
    return segs


def select_bisect(video_path: str, duration: float, *, step: float = DEFAULT_STEP,
                  min_res: float = DEFAULT_MIN_RES, threshold: float = DEFAULT_THRESHOLD,
                  mask: Optional[np.ndarray] = None, ffmpeg: Optional[str] = None,
                  progress: Optional[Callable[[float], None]] = None,
                  settle: float = DEFAULT_SETTLE) -> list[Segment]:
    """Бісекція: опори кожні ``step`` с, різні кінці → ділимо навпіл до ``min_res``."""
    cache: dict[float, np.ndarray] = {}

    def frame(t: float) -> np.ndarray:
        t = round(min(max(t, 0.0), max(duration - 0.05, 0.0)), 3)
        if t not in cache:
            img = grab_small(video_path, t, ffmpeg=ffmpeg)
            if img is None:
                img = cache[max(cache)] if cache else np.zeros((THUMB_H, THUMB_W), np.uint8)
            cache[t] = img
        return cache[t]

    bounds: list[tuple[float, float]] = [(0.0, 0.0)]

    def split(t1: float, t2: float) -> None:
        score = diff_score(frame(t1), frame(t2), mask)
        if score <= threshold:
            return
        if t2 - t1 <= min_res:
            bounds.append((t2, score))
            return
        tm = (t1 + t2) / 2
        split(t1, tm)
        split(tm, t2)

    anchors = list(np.arange(0.0, duration, step)) + [max(duration - 0.05, 0.0)]
    for i in range(1, len(anchors)):
        split(float(anchors[i - 1]), float(anchors[i]))
        if progress:
            progress(i / (len(anchors) - 1))
    bounds.sort()
    segs = _segments_from_boundaries(bounds, duration, settle)
    for s in segs:
        img = frame(s.frame_time)
        s.std = round(float(img.std()), 2)
        s.dhash = dhash(img)
    return segs


def cap_segments(segs: list[Segment], max_frames: int) -> tuple[list[Segment], bool]:
    """Понад ``max_frames`` — зливаємо найкоротший відрізок із сусідом (захист від «мигаючого» відео)."""
    if len(segs) <= max_frames:
        return segs, False
    segs = list(segs)
    while len(segs) > max_frames:
        i = min(range(len(segs)), key=lambda k: segs[k].end - segs[k].start)
        j = i - 1 if i > 0 else i + 1
        a, b = sorted((i, j))
        merged = Segment(start=segs[a].start, end=segs[b].end, frame_time=segs[a].frame_time,
                         change_score=segs[a].change_score, std=segs[a].std, dhash=segs[a].dhash)
        segs[a:b + 1] = [merged]
    return segs, True


def mark_duplicates(segs: list[Segment], max_bits: int = 4) -> list[Optional[int]]:
    """Для кожного відрізка — індекс першого НЕсусіднього відрізка з тим самим екраном, або None."""
    out: list[Optional[int]] = [None] * len(segs)
    for i, s in enumerate(segs):
        if not s.dhash:
            continue
        for j in range(0, i - 1):
            if out[j] is None and segs[j].dhash and hamming(s.dhash, segs[j].dhash) <= max_bits:
                out[i] = j
                break
    return out


def pick_sharpest(video_path: str, seg: Segment, *, ffmpeg: Optional[str] = None) -> float:
    """Відрізок ≥ 3 с: найрізкіший з 3 кандидатів (+0.7 с, +1.5 с, середина)."""
    if seg.end - seg.start < 3.0:
        return seg.frame_time
    cands = [seg.start + REP_OFFSET, seg.start + 1.5, (seg.start + seg.end) / 2]
    best, best_v = seg.frame_time, -1.0
    for t in cands:
        img = grab_small(video_path, t, w=320, h=180, ffmpeg=ffmpeg)
        if img is None:
            continue
        v = sharpness(img)
        if v > best_v:
            best, best_v = t, v
    return round(best, 3)


def select_keyframes(video_path: str, *, method: str = DEFAULT_METHOD,
                     step: float = DEFAULT_STEP, min_res: float = DEFAULT_MIN_RES,
                     threshold: float = DEFAULT_THRESHOLD,
                     ignore_regions: Optional[Sequence[Sequence[float]]] = None,
                     max_frames: int = DEFAULT_MAX_FRAMES,
                     settle: float = DEFAULT_SETTLE,
                     cache_dir: Optional[str] = None,
                     ffmpeg: Optional[str] = None,
                     progress: Optional[Callable[[float], None]] = None) -> dict:
    """Головний вхід. Повертає {'segments': [Segment], 'duration', 'capped', 'method'}.

    ``cache_dir`` — тека для кешу мініатюр (``thumbs.npz``): повторний виклик із
    іншим порогом не декодує відео вдруге."""
    info = probe(video_path, ffmpeg)
    duration = info['duration']
    rmask = region_mask(ignore_regions)
    if method == 'bisect':
        segs = select_bisect(video_path, duration, step=step, min_res=min_res,
                             threshold=threshold, mask=rmask, ffmpeg=ffmpeg, progress=progress,
                             settle=settle)
    elif method == 'thumbs':
        thumbs = None
        cache = os.path.join(cache_dir, 'thumbs.npz') if cache_dir else None
        if cache and os.path.isfile(cache):
            try:
                with np.load(cache) as z:
                    # кеш дійсний лише для тієї самої тривалості І частоти декодування
                    if (float(z['duration']) == duration and 'fps' in z.files
                            and float(z['fps']) == 1.0 / min_res):
                        thumbs = z['thumbs']
            except Exception:
                logger.warning("кеш мініатюр %s пошкоджений — декодую заново", cache, exc_info=True)
        if thumbs is None:
            thumbs = thumbs_pass(video_path, duration, fps=1.0 / min_res, ffmpeg=ffmpeg,
                                 progress=progress)
            if cache:
                try:
                    os.makedirs(cache_dir, exist_ok=True)
                    np.savez_compressed(cache, thumbs=thumbs, duration=duration, fps=1.0 / min_res)
                except Exception:
                    logger.warning("кеш мініатюр не записано: %s", cache, exc_info=True)
        elif progress:
            progress(1.0)
        mask = rmask & flicker_mask(thumbs)
        segs = select_thumbs(thumbs, duration, fps=1.0 / min_res, threshold=threshold, mask=mask,
                             settle=settle)
    else:
        raise ValueError(f'невідомий метод: {method}')
    segs, capped = cap_segments(segs, max_frames)
    return {'segments': segs, 'duration': duration, 'capped': capped, 'method': method,
            'width': info['width'], 'height': info['height']}


def classify(seg: Segment, *, ocr_chars: int, ocr_lines: int, mean_word_h: float,
             frame_h: int, min_res: float = DEFAULT_MIN_RES, ocr_ran: bool = True) -> str:
    """Евристичний тип кадру (без моделі). Відомі похибки — docs/video-timeline.md.

    ``ocr_ran=False`` (OCR вимкнено або недоступний): тексту не знаємо, тож кадр не
    оголошується «лише камерою» — інакше стрічка за замовчуванням ховала б усе."""
    if seg.std < BLANK_STD and ocr_chars < 15:     # з текстом кадр не порожній
        return 'blank'
    if seg.end - seg.start < 2 * min_res and seg.change_score > 0:
        return 'transition'
    if not ocr_ran:
        return 'screen_share'
    if ocr_chars < 15:
        return 'camera_only'
    if ocr_lines <= 12 and frame_h and mean_word_h / frame_h >= 0.025:
        return 'slide'
    return 'screen_share'
