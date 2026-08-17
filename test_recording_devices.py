"""Smoke test для Phase 9.1: WASAPI recorder primitive.

Запуск:
    .venv/Scripts/python.exe test_recording_devices.py

Що перевіряє:
1. Імпорт pyaudiowpatch працює.
2. list_input_devices() повертає mic-и та loopback'и.
3. resolve_device_params() для default mic та default loopback повертає
   валідні (rate, channels).
4. WasapiRecorder можна відкрити, стартувати, отримати кадри ~2 сек з
   default loopback'у і коректно закрити.
5. LevelMeter повертає peak/rms (значення 0..1, можуть бути нулями
   якщо тиша).

Якщо PyAudioWPatch не встановлений → script завершиться з
exit-code 2 і відповідним повідомленням.
"""
from __future__ import annotations

import sys
import time

# Windows console default cp1252 ламається на укр. символах. Флешимо stdout
# у UTF-8 — гарантує читабельний вивід незалежно від chcp.
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

try:
    from app.services.recording import (
        WasapiRecorder,
        list_input_devices,
        resolve_device_params,
    )
    from app.services.recording.recorder import _HAS_PYAUDIO  # type: ignore
except Exception as e:
    print(f"[FAIL] Імпорт модулю recording: {e}")
    sys.exit(1)


def section(title: str) -> None:
    print()
    print('=' * 70)
    print(f'  {title}')
    print('=' * 70)


def main() -> int:
    if not _HAS_PYAUDIO:
        print("[SKIP] PyAudioWPatch не встановлений — recording disabled")
        return 2

    section("1. list_input_devices()")
    devices = list_input_devices()
    if not devices:
        print("[FAIL] Жодного WASAPI девайсу не знайдено")
        return 1

    mics = [d for d in devices if d.kind == 'mic']
    loopbacks = [d for d in devices if d.kind == 'loopback']
    print(f"  знайдено: {len(mics)} мікрофон(и), {len(loopbacks)} loopback")
    print()
    print(f"  {'#':>3}  {'kind':<8} {'def':<3} {'ch':>2} {'rate':>6}  name")
    print(f"  {'-'*3}  {'-'*8} {'-'*3} {'-'*2} {'-'*6}  {'-'*40}")
    for d in devices:
        marker = ' * ' if d.is_default else '   '
        print(
            f"  {d.index:>3}  {d.kind:<8} {marker} {d.channels:>2} "
            f"{d.default_sample_rate:>6}  {d.name[:55]}"
        )

    default_mic = next((d for d in mics if d.is_default), mics[0] if mics else None)
    default_lb = next((d for d in loopbacks if d.is_default), loopbacks[0] if loopbacks else None)
    print()
    print(f"  default mic:      {default_mic.name if default_mic else 'НЕМА'}")
    print(f"  default loopback: {default_lb.name if default_lb else 'НЕМА'}")

    section("2. resolve_device_params() для mic та loopback")
    if default_mic:
        rate, ch = resolve_device_params(default_mic.index, 48000, 2)
        print(f"  mic:      requested 48000/2 → resolved {rate}/{ch}")
    if default_lb:
        rate, ch = resolve_device_params(default_lb.index, 48000, 2)
        print(f"  loopback: requested 48000/2 → resolved {rate}/{ch}")

    target = default_lb or default_mic
    if target is None:
        print("[FAIL] Немає жодного придатного девайсу для запису")
        return 1

    section(f"3. WasapiRecorder smoke (~2s з {target.kind}: {target.name[:40]})")
    rate, ch = resolve_device_params(target.index, 48000, 2)
    try:
        with WasapiRecorder(
            device_index=target.index,
            sample_rate=rate,
            channels=ch,
            chunk_frames=1024,
        ) as rec:
            rec.start()
            captured_bytes = 0
            captured_frames = 0
            t_start = time.time()
            # Збираємо ~2 сек з періодичним polling (10Hz)
            while time.time() - t_start < 2.0:
                time.sleep(0.1)
                frames = rec.drain()
                for fr in frames:
                    captured_bytes += len(fr.data)
                    captured_frames += 1
                snap = rec.level.snapshot()
                bar = _bar(snap['rms'], width=30)
                print(
                    f"\r  rms={snap['rms']:.3f} peak={snap['peak']:.3f} "
                    f"|{bar}|  bytes={captured_bytes:>8}",
                    end='', flush=True,
                )

            print()  # newline
            err = rec.get_callback_error()
            if err is not None:
                print(f"  [WARN] callback error: {err}")
            print(f"  всього кадрів: {captured_frames}")
            print(f"  всього байт:   {captured_bytes}")
            print(f"  expected ~{int(rate * ch * 2 * 2)} байт за 2с (rate*ch*2*2)")
            print(f"  dropped:       {rec.dropped_frames}")
    except Exception as e:
        print(f"[FAIL] WasapiRecorder error: {e}")
        return 1

    if captured_bytes < 1000:
        print("[FAIL] Захоплено замало даних — щось не так зі стрімом")
        return 1

    expected_min_bytes = int(rate * ch * 2 * 1.0)  # 1 секунда мінімум
    if captured_bytes < expected_min_bytes:
        print(f"[WARN] Захоплено {captured_bytes} байт за 2с — менше ніж очікувалось")

    print()
    print("[OK] Phase 9.1 smoke passed")
    return 0


def _bar(value: float, width: int = 30) -> str:
    filled = int(min(1.0, max(0.0, value)) * width)
    return '#' * filled + '-' * (width - filled)


if __name__ == '__main__':
    sys.exit(main())
