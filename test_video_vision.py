"""Офлайн-тест Phase 23B: vision-опис кадрів відео → RAG.

Без Ollama / Anthropic API / GPU / реального відео — усі зовнішні залежності
(local_llm, anthropic-клієнт, OCR, embeddings, resolve/extract) підставні.
Перевіряє контракти:

  A. video_vision: вибір бекенда ('local'/'claude'/'off'), availability з
     людською причиною, graceful-degrade (виняток у бекенді → '').
  B. local_llm.generate приймає kwarg `images` (vision-розширення).
  C. analyze_video: гейт чанка зм'якшено — кадр БЕЗ OCR, але з vision-описом
     тепер дає шукабельний чанк; текст чанка містить OCR + '[опис кадру] …';
     кадр без обох — рядок є, чанку нема; vision-колонки заповнюються.

Запуск:  .venv/Scripts/python.exe test_video_vision.py
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np

_fail = 0


def check(cond, msg):
    global _fail
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        _fail += 1


# ===================================================================== A. video_vision
print("\n[A] video_vision — backend dispatch / availability / degrade")

from app.services import video_vision as vv
from app.services import local_llm
from app.services import text_polishing

# Capture the real generate() signature BEFORE [A] monkeypatches it (for [B]).
_ORIG_GEN_PARAMS = set(inspect.signature(local_llm.generate).parameters)

# Tiny on-disk image (bytes never validated — base64 only).
_tmpdir = tempfile.mkdtemp(prefix="vvtest_")
_img = os.path.join(_tmpdir, "frame.png")
with open(_img, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n fake image bytes")

# -- off --
os.environ.pop("ANTHROPIC_API_KEY", None)
ok, reason = vv.availability("off")
check(not ok and "вимкнено" in reason, "availability('off') → False + причина")
check(vv.describe_frame(_img, backend="off") == "", "describe_frame('off') → ''")

# -- claude: no key --
ok, reason = vv.availability("claude")
check(not ok and "ANTHROPIC_API_KEY" in reason, "availability('claude') без ключа → False")

# -- local: model present --
local_llm.availability = lambda *a, **k: (True, "ok")          # type: ignore
local_llm.list_models = lambda: ["qwen2.5vl:7b", "qwen2.5:14b"]  # type: ignore
ok, reason = vv.availability("local")
check(ok, "availability('local') з присутньою VL-моделлю → True")

# -- local: model missing --
local_llm.list_models = lambda: ["llama3:8b"]                   # type: ignore
ok, reason = vv.availability("local")
check(not ok and "ollama pull" in reason, "availability('local') без моделі → підказка pull")

# -- local describe: success --
local_llm.list_models = lambda: ["qwen2.5vl:7b"]               # type: ignore
local_llm.generate = lambda *a, **k: {"response": "  таблиця з\nцифрами  "}  # type: ignore
out = vv.describe_frame(_img, backend="local")
check(out == "таблиця з цифрами", "describe_frame('local') чистить/повертає опис")
# images передаються у generate
_captured = {}
def _gen_capture(prompt, **k):
    _captured.update(k)
    return {"response": "опис"}
local_llm.generate = _gen_capture                               # type: ignore
vv.describe_frame(_img, backend="local")
check(_captured.get("images") and len(_captured["images"]) == 1,
      "describe_frame('local') шле images=[base64] у local_llm.generate")

# -- local describe: backend raises → '' --
def _boom(*a, **k):
    raise RuntimeError("ollama down")
local_llm.generate = _boom                                      # type: ignore
check(vv.describe_frame(_img, backend="local") == "", "describe_frame degrades to '' on error")

# -- claude describe via fake client (no anthropic import needed) --
class _FakeBlock:
    type = "text"
    def __init__(self, t): self.text = t
class _FakeMsg:
    def __init__(self, t): self.content = [_FakeBlock(t)]
class _FakeMessages:
    def create(self, **kw): return _FakeMsg("кадр: термінал з логами")
class _FakeClient:
    messages = _FakeMessages()
    def with_options(self, **kw): return self
text_polishing._get_client = lambda: _FakeClient()             # type: ignore
out = vv.describe_frame(_img, backend="claude")
check(out == "кадр: термінал з логами", "describe_frame('claude') повертає текст з vision-блоку")

# missing file → ''
check(vv.describe_frame(os.path.join(_tmpdir, "nope.png"), backend="local") == "",
      "describe_frame на неіснуючому файлі → ''")


# ===================================================================== B. local_llm.images
print("\n[B] local_llm.generate — kwarg images")
check("images" in _ORIG_GEN_PARAMS, "local_llm.generate має параметр images")


# ===================================================================== C. analyze_video gate
print("\n[C] analyze_video — vision relaxes chunk gate + combined text")

from app.services import video_analysis as va
from app.services import embeddings as emb
from app.services import document_parser as dp
from app.services import video_vision as vv2

# Temp DB with the minimal v24+v25 schema this path touches.
_db = os.path.join(_tmpdir, "t.db")
_c = sqlite3.connect(_db)
_c.executescript("""
CREATE TABLE transcriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT,
    video_analysis_at TIMESTAMP,
    video_keyframes_count INTEGER DEFAULT 0
);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcription_id INTEGER, chunk_index INTEGER,
    start_time REAL, end_time REAL,
    speaker TEXT, text TEXT, embedding BLOB, section TEXT
);
CREATE TABLE video_keyframes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transcription_id INTEGER NOT NULL,
    ts_offset_sec REAL NOT NULL,
    image_path TEXT, ocr_text TEXT, scene_score REAL,
    source TEXT DEFAULT 'scene',
    vision_text TEXT, vision_model TEXT, vision_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO transcriptions (id, file_path) VALUES (1, 'rec.mp3');
""")
_c.commit()
_c.close()

# Three fake keyframes on disk.
_frames = []
for i, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
    p = os.path.join(_tmpdir, name)
    with open(p, "wb") as f:
        f.write(b"jpg")
    _frames.append((float(i + 1), p))   # ts = 1.0, 2.0, 3.0

# Vision: frame a → table, frame b → '' (empty), frame c → diagram.
_vis_map = {_frames[0][1]: "таблиця бюджету проєкту X",
            _frames[1][1]: "",
            _frames[2][1]: "діаграма архітектури сервісів"}

va.resolve_recording_video = lambda db, tid: {                  # type: ignore
    "recording_session_id": "s1", "primary_video_path": _img,
    "session_dir": _tmpdir, "start_offset_sec": 0.0}
va.extract_scene_keyframes = lambda *a, **k: list(_frames)      # type: ignore

dp.ocr_available = lambda: False                                # type: ignore  (OCR off — isolate vision gate)
emb.is_available = lambda: True                                 # type: ignore
emb.embed_texts = lambda texts: np.zeros((len(texts), 1024), dtype=np.float32)  # type: ignore

vv2.availability = lambda backend=None: (True, "fake-vl ready")  # type: ignore
vv2.active_model = lambda backend=None: "qwen2.5vl:7b"          # type: ignore
vv2.describe_frame = lambda path, backend=None: _vis_map.get(path, "")  # type: ignore

res = va.analyze_video(_db, 1, force=True)

check(res.get("keyframes") == 3, f"3 кадри збережено (got {res.get('keyframes')})")
check(res.get("vision_descriptions") == 3, f"3 vision-виклики (got {res.get('vision_descriptions')})")
check(res.get("chunks_added") == 2,
      f"2 чанки (vision-only кадри шукабельні, порожній — ні; got {res.get('chunks_added')})")

_v = sqlite3.connect(_db)
_v.row_factory = sqlite3.Row
kf = {r["ts_offset_sec"]: r for r in _v.execute("SELECT * FROM video_keyframes").fetchall()}
check(kf[1.0]["vision_text"] == "таблиця бюджету проєкту X" and kf[1.0]["vision_model"] == "qwen2.5vl:7b",
      "vision_text + vision_model записані для кадру з описом")
check(kf[1.0]["vision_at"], "vision_at заповнено коли є опис")
check(kf[2.0]["vision_text"] is None and kf[2.0]["vision_model"] is None,
      "порожній опис → vision_text/vision_model NULL")

chunks = _v.execute("SELECT * FROM chunks ORDER BY start_time").fetchall()
check(all(c["speaker"] == "екран" for c in chunks), "усі чанки speaker='екран'")
check(chunks[0]["text"] == "[опис кадру] таблиця бюджету проєкту X",
      "текст чанка (OCR off) = '[опис кадру] …'")
check(chunks[0]["section"] == "екран 00:01", "section 'екран MM:SS'")
check(len(chunks[0]["embedding"]) == 1024 * 4, "embedding = 1024 float32")
ts_in_chunks = {c["start_time"] for c in chunks}
check(2.0 not in ts_in_chunks, "кадр без OCR і без vision → чанку нема")
_v.close()


# ===================================================================== підсумок
print()
if _fail:
    print(f"❌ FAILED: {_fail} перевірок не пройшло")
    sys.exit(1)
print("✅ ALL PASSED")
