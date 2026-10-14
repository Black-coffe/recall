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


# C (analyze_video gate) знято 13.10.2026: конвеєр замінено (spec video-visual-timeline),
# його покривають tests/test_video_frames.py, test_screen_secrets.py, test_video_timeline.py.

# ===================================================================== підсумок
print()
if _fail:
    print(f"❌ FAILED: {_fail} перевірок не пройшло")
    sys.exit(1)
print("✅ ALL PASSED")
