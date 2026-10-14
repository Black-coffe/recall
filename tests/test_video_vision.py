"""Локальна vision-модель не тримає VRAM 30 хв після пачки кадрів
(docs/defects/local-llm-during-recording.md)."""
from __future__ import annotations

from PIL import Image

from app.services import local_llm, video_vision


def test_local_describe_passes_short_keep_alive(tmp_path, monkeypatch):
    img = tmp_path / "кадр.jpg"
    Image.new("RGB", (64, 36), (200, 200, 200)).save(img, "JPEG")
    sent = {}
    monkeypatch.setattr(local_llm, "generate",
                        lambda prompt, **kw: sent.update(kw) or {"response": "Таблиця 1С"})
    assert video_vision._describe_local(str(img), timeout=None) == "Таблиця 1С"
    assert sent["keep_alive"] == video_vision.VISION_KEEPALIVE == "60s"
    assert sent["model"] == video_vision.VISION_MODEL_LOCAL


def test_module_passes_keep_alive_check():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "scripts/check_llm_keepalive.py", "app/services/video_vision.py"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
