#!/usr/bin/env python3
"""Микро-бенчмарк WhisperManager: сравнение faster vs openai backend.

Запуск:
    cd E:\\Projects\\Whisper
    .venv\\Scripts\\python.exe tools\\benchmark_whisper.py [audio_path] [model]

Параметры (опц.):
    audio_path  путь к аудиофайлу (по умолчанию /tmp/bench_60s.mp3)
    model       имя модели (по умолчанию base)
"""
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from whisper_manager_new import ModernWhisperManager


def bench(backend_name, audio_path, model_name, language="uk"):
    print(f"\n=== {backend_name} ===")
    os.environ["WHISPER_BACKEND"] = backend_name
    m = ModernWhisperManager(backend=backend_name)

    # Загрузка модели (не учитываем во времени)
    t0 = time.time()
    m.load_model(model_name)
    load_t = time.time() - t0
    print(f"load: {load_t:.2f}s")

    # Транскрипция
    t0 = time.time()
    result = m.transcribe_with_progress(audio_path, model_name, language)
    elapsed = time.time() - t0

    if "error" in result:
        print(f"ERROR: {result['error']}")
        return None

    duration = m.get_audio_duration(audio_path)
    rtf = duration / max(elapsed, 0.001)
    text_preview = result["text"][:120].replace("\n", " ")
    # Windows console может не уметь в UTF-8: фолбэк на ASCII
    safe_preview = text_preview.encode("ascii", errors="replace").decode("ascii")
    print(f"transcribe: {elapsed:.2f}s  ({rtf:.1f}x realtime)")
    print(f"segments: {len(result['segments'])}, lang: {result['language']}")
    print(f"text[:120]: {safe_preview}...")
    return {"backend": backend_name, "elapsed": elapsed, "rtf": rtf, "load": load_t}


def main():
    audio = sys.argv[1] if len(sys.argv) > 1 else "/tmp/bench_60s.mp3"
    model = sys.argv[2] if len(sys.argv) > 2 else "base"

    if not os.path.exists(audio):
        print(f"audio not found: {audio}")
        sys.exit(1)

    print(f"audio: {audio}")
    print(f"model: {model}")

    results = []
    for backend in ["openai", "faster"]:
        try:
            r = bench(backend, audio, model)
            if r:
                results.append(r)
        except Exception as e:
            print(f"{backend}: failed -> {e}")

    if len(results) == 2:
        speedup = results[0]["elapsed"] / results[1]["elapsed"]
        print(f"\n=== summary ===")
        print(f"openai:  {results[0]['elapsed']:.2f}s ({results[0]['rtf']:.1f}x rt)")
        print(f"faster:  {results[1]['elapsed']:.2f}s ({results[1]['rtf']:.1f}x rt)")
        print(f"speedup: {speedup:.2f}x faster")


if __name__ == "__main__":
    main()
