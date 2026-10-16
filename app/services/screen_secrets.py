"""Secrets from the screen: passwords, tokens, keys in OCR of video frames (spec video-visual-timeline, story 02).

The protection boundary is "a secret does not get into the chat through Recall tools":
  * a detector without a model over OCR words with boxes; an error goes toward hiding;
  * what is found is replaced with ``[🔒 секрет]`` in ``video_keyframes.ocr_text``,
    ``vision_text``, in word boxes and in the RAG chunks ``екран`` — that is, BEFORE search,
    ask_archive, MCP and Claude;
  * raw values go to ``screen_secrets``, which no HTTP endpoint and
    no MCP tool reads;
  * a frame image goes out only with painted-over boxes (``redact``);
  * raw values can be obtained ONLY with this module from an interactive terminal:
    ``python -m app.services.screen_secrets reveal <recording id> [--out file]`` — it
    asks for a confirmation, writes the values to a file, and to the console — only the path and
    the count. The agent's Bash has no TTY, so it gets a refusal.

Localhost in Recall is trusted (``app/core/auth.py``): the browser, MCP and the agent's ``curl``
are indistinguishable, so there is no "show password" button in the UI and cannot be. Code run
as the same Windows user will read the DB directly — that is beyond the boundary.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MASK = '[🔒 секрет]'
BOX_MASK = '🔒'

# The label after which the value follows on the same line. "pass" only as a whole word.
_LABEL = re.compile(
    r'^(?:пароль|пароля|password|passwd|passphrase|pass|pwd|pin|пін|пин|token|токен|secret|'
    r'секрет|api[-_ ]?key|apikey|access[-_]?key|secret[-_]?key|ключ|client[-_]?secret)'
    r'\s*[:=]?\s*(?P<rest>.*)$', re.IGNORECASE)

_TOKEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ('anthropic_key', re.compile(r'sk-ant-[A-Za-z0-9_\-]{10,}')),
    ('openai_key', re.compile(r'sk-[A-Za-z0-9_\-]{16,}')),
    ('github_token', re.compile(r'(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}')),
    ('slack_token', re.compile(r'xox[abposr]-[A-Za-z0-9\-]{10,}')),
    ('aws_key', re.compile(r'AKIA[0-9A-Z]{16}')),
    ('google_key', re.compile(r'AIza[0-9A-Za-z_\-]{30,}')),
    ('jwt', re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}')),
    ('telegram_bot_token', re.compile(r'\d{8,10}:[A-Za-z0-9_\-]{35}')),
    ('private_key', re.compile(r'-----BEGIN[A-Z ]*PRIVATE KEY-----')),
    ('url_credentials', re.compile(r'[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@')),
]

_PATHLIKE = re.compile(r'^[\w.\-]+(?:[\\/][\w.\-]+)+[\\/]?$')


@dataclass
class Secret:
    kind: str
    value: str
    word_index: Optional[int] = None      # index in the OCR word list (for the box)


def _is_masked(v: str) -> bool:
    """A value that is already a mask (or its fragment after OCR splits it into words) is
    not a secret. Without this, repeated masking would grow: «[🔒 секрет] секрет] …»."""
    v = (v or '').strip()
    return (not v or '🔒' in v or v in ('секрет]', 'секрет')
            or set(v) <= set('[]'))              # a fragment of the mask «]» after splitting into words


def _high_entropy(w: str) -> bool:
    """A long string without spaces with uppercase, lowercase and digits — looks like a token."""
    if len(w) < 24 or ' ' in w:
        return False
    if '\\' in w or w.lower().startswith(('http://', 'https://', 'www.')) or _PATHLIKE.match(w):
        return False
    classes = sum(bool(re.search(p, w)) for p in (r'[a-z]', r'[A-Z]', r'\d'))
    return classes >= 3


def _token_kind(w: str) -> Optional[str]:
    for kind, pat in _TOKEN_PATTERNS:
        if pat.search(w):
            return kind
    if _high_entropy(w):
        return 'high_entropy'
    return None


def _same_line(a: list, b: list) -> bool:
    """OCR words [x, y, w, h, text] on one line: y centres closer than half a height."""
    ca, cb = a[1] + a[3] / 2, b[1] + b[3] / 2
    return abs(ca - cb) <= max(a[3], b[3]) / 2


def scan(words: list) -> list[Secret]:
    """Secrets among OCR words ([x, y, w, h, text] in reading order)."""
    found: dict[int, Secret] = {}
    for i, box in enumerate(words):
        w = str(box[4]).strip()
        kind = _token_kind(w)
        if kind:
            found[i] = Secret(kind, w, i)
            continue
        m = _LABEL.match(w)
        if not m:
            continue
        rest = m.group('rest').strip(' :=')
        if rest and _is_masked(rest):
            continue
        if rest:                                    # «password=hunter2» as one word
            found[i] = Secret('labeled', rest, i)
            continue
        # the value is the next word on the same line (skip lone «:»/«=»)
        j = i + 1
        while j < len(words) and str(words[j][4]).strip() in (':', '=', '-', '—'):
            j += 1
        if j < len(words) and _same_line(box, words[j]):
            val = str(words[j][4]).strip()
            if val and j not in found and not _is_masked(val):
                found[j] = Secret('labeled', val, j)
    return [found[k] for k in sorted(found)]


def scan_text(text: str) -> list[Secret]:
    """The same for free text (a frame description): tokens + «label: value» in a line."""
    out: list[Secret] = []
    for line in (text or '').splitlines():
        for m in re.finditer(r'(?i)(пароль|password|passwd|pwd|pin|token|токен|secret|api[-_ ]?key|ключ)'
                             r'\s*[:=]\s*(\S+)', line):
            val = m.group(2).strip('.,;')
            if not _is_masked(val):
                out.append(Secret('labeled', val))
        for tok in line.split():
            tok = tok.strip('.,;()[]«»"\'')
            kind = _token_kind(tok)
            if kind:
                out.append(Secret(kind, tok))
    return out


def scan_all(text: str, words: list) -> list[Secret]:
    """Frame secrets: by words (with boxes) + by text lines; for what is found in text
    a box is looked up — the word containing the value — so it is painted over too."""
    found = scan(words)
    have = {x.value for x in found}
    for sec in scan_text(text):
        if sec.value in have:
            continue
        for i, box in enumerate(words):
            w = str(box[4])
            if sec.value in w or (len(w) >= 4 and w in sec.value):
                sec.word_index = i
                break
        found.append(sec)
        have.add(sec.value)
    return found


def mask_text(text: Optional[str], secrets: list[Secret]) -> Optional[str]:
    """Replace every secret value with MASK (longer ones first)."""
    if not text:
        return text
    for v in sorted({s.value for s in secrets if s.value}, key=len, reverse=True):
        text = text.replace(v, MASK)
    return text


def mask_words(words: list, secrets: list[Secret]) -> list:
    """Word boxes with the secrets' text hidden and the mark ``"s": 1`` (the 6th element)."""
    idx = {s.word_index for s in secrets if s.word_index is not None}
    out = []
    for i, box in enumerate(words):
        if i in idx:
            out.append([box[0], box[1], box[2], box[3], BOX_MASK, 1])
        else:
            out.append(list(box[:5]))
    return out


def secret_boxes(boxes_json: Optional[str]) -> tuple[list, int, int]:
    """From ``video_keyframes.ocr_boxes_json`` → (secret boxes, width, height of the OCR frame)."""
    if not boxes_json:
        return [], 0, 0
    try:
        data = json.loads(boxes_json)
    except ValueError:
        logger.warning("ocr_boxes_json is not JSON — the frame is painted over entirely")
        return [[0, 0, 10 ** 6, 10 ** 6]], 1, 1
    words = data.get('words') or []
    boxes = [w[:4] for w in words if len(w) > 5 and w[5]]
    return boxes, int(data.get('w') or 0), int(data.get('h') or 0)


def redact(img, boxes: list, src_w: int = 0, src_h: int = 0, pad: int = 4):
    """Paint over boxes (OCR frame coordinates src_w×src_h) on a copy of ``img``."""
    if not boxes:
        return img
    from PIL import ImageDraw
    out = img.copy()
    sx = out.width / src_w if src_w else 1.0
    sy = out.height / src_h if src_h else 1.0
    d = ImageDraw.Draw(out)
    for x, y, w, h in boxes:
        d.rectangle([max(0, (x - pad) * sx), max(0, (y - pad) * sy),
                     min(out.width, (x + w + pad) * sx), min(out.height, (y + h + pad) * sy)],
                    fill=(0, 0, 0))
    return out


def store(conn, keyframe_id: int, secrets: list[Secret], words: list) -> None:
    """Raw values go to a separate table (read only by ``reveal``)."""
    for s in secrets:
        bbox = words[s.word_index][:4] if s.word_index is not None else None
        conn.execute("INSERT INTO screen_secrets (keyframe_id, kind, value, bbox_json) "
                     "VALUES (?, ?, ?, ?)",
                     (keyframe_id, s.kind, s.value, json.dumps(bbox) if bbox else None))


# ---------------------------------------------------------------------------
# reveal — interactive terminal only, values go to a file
# ---------------------------------------------------------------------------

def _default_out(tid: int) -> str:
    base = os.path.join(os.environ.get('USERPROFILE') or os.path.expanduser('~'), 'RecallSecrets')
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    return os.path.join(base, f'secrets_{tid}_{stamp}.txt')


def reveal(db_path: str, tid: int, out_path: Optional[str] = None, *,
           stdin=None, stdout=None, stderr=None) -> int:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if not (stdin.isatty() and stdout.isatty()):
        stderr.write('Refused: reveal works only in a human\'s interactive terminal. '
                     'Run the command in your own terminal, not through the agent.\n')
        return 2
    stdout.write(f'Raw secrets from the frames of recording #{tid} will be written to a file.\n'
                 f'To confirm, type: показати {tid}\n> ')
    stdout.flush()
    if stdin.readline().strip() != f'показати {tid}':
        stderr.write('Not confirmed — nothing written.\n')
        return 3
    import sqlite3
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            "SELECT k.ts_offset_sec, s.kind, s.value FROM screen_secrets s "
            "JOIN video_keyframes k ON k.id = s.keyframe_id "
            "WHERE k.transcription_id = ? ORDER BY k.ts_offset_sec", (tid,)).fetchall()
    finally:
        conn.close()
    path = out_path or _default_out(tid)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'# Secrets from the screen, recording #{tid}. Do not paste this file into a chat.\n')
        for t, kind, value in rows:
            f.write(f'{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}\t{kind}\t{value}\n')
    stdout.write(f'Wrote {len(rows)} values to file: {path}\n')
    return 0


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(prog='python -m app.services.screen_secrets',
                                description='Secrets from video frames (local only, to a file only).')
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('reveal', help='Write a recording\'s raw secrets to a file (TTY required)')
    r.add_argument('tid', type=int)
    r.add_argument('--out', default=None)
    r.add_argument('--db', default='whisper_history.db')
    args = p.parse_args(argv)
    return reveal(args.db, args.tid, args.out)


if __name__ == '__main__':
    sys.exit(main())
