"""Smoke test for Phase 9.1: the WASAPI recorder primitive.

Run:
    .venv/Scripts/python.exe test_recording_devices.py

What it checks:
1. The pyaudiowpatch import works.
2. list_input_devices() returns mics and loopbacks.
3. resolve_device_params() for the default mic and default loopback returns
   valid (rate, channels).
4. A WasapiRecorder can be opened, started, receive frames for ~2 sec from the
   default loopback and be closed correctly.
5. LevelMeter returns peak/rms (values 0..1, may be zeros
   if silent).

If PyAudioWPatch is not installed → the script exits with
exit code 2 and a matching message.
"""
from __future__ import annotations

import sys
import time

# The Windows console default cp1252 breaks on Ukrainian characters. We flush stdout
# as UTF-8 — guarantees readable output regardless of chcp.
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
    print(f"[FAIL] Import of the recording module: {e}")
    sys.exit(1)


def section(title: str) -> None:
    print()
    print('=' * 70)
    print(f'  {title}')
    print('=' * 70)


def main() -> int:
    if not _HAS_PYAUDIO:
        print("[SKIP] PyAudioWPatch is not installed — recording disabled")
        return 2

    section("1. list_input_devices()")
    devices = list_input_devices()
    if not devices:
        print("[FAIL] No WASAPI device found")
        return 1

    mics = [d for d in devices if d.kind == 'mic']
    loopbacks = [d for d in devices if d.kind == 'loopback']
    print(f"  found: {len(mics)} microphone(s), {len(loopbacks)} loopback")
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
    print(f"  default mic:      {default_mic.name if default_mic else 'NONE'}")
    print(f"  default loopback: {default_lb.name if default_lb else 'NONE'}")

    section("2. resolve_device_params() for mic and loopback")
    if default_mic:
        rate, ch = resolve_device_params(default_mic.index, 48000, 2)
        print(f"  mic:      requested 48000/2 → resolved {rate}/{ch}")
    if default_lb:
        rate, ch = resolve_device_params(default_lb.index, 48000, 2)
        print(f"  loopback: requested 48000/2 → resolved {rate}/{ch}")

    target = default_lb or default_mic
    if target is None:
        print("[FAIL] No suitable device to record from")
        return 1

    section(f"3. WasapiRecorder smoke (~2s from {target.kind}: {target.name[:40]})")
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
            # Collect ~2 sec with periodic polling (10Hz)
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
            print(f"  total frames: {captured_frames}")
            print(f"  total bytes:  {captured_bytes}")
            print(f"  expected ~{int(rate * ch * 2 * 2)} bytes per 2s (rate*ch*2*2)")
            print(f"  dropped:       {rec.dropped_frames}")
    except Exception as e:
        print(f"[FAIL] WasapiRecorder error: {e}")
        return 1

    if captured_bytes < 1000:
        print("[FAIL] Too little data captured — something is wrong with the stream")
        return 1

    expected_min_bytes = int(rate * ch * 2 * 1.0)  # 1 second minimum
    if captured_bytes < expected_min_bytes:
        print(f"[WARN] Captured {captured_bytes} bytes in 2s — less than expected")

    print()
    print("[OK] Phase 9.1 smoke passed")
    return 0


def _bar(value: float, width: int = 30) -> str:
    filled = int(min(1.0, max(0.0, value)) * width)
    return '#' * filled + '-' * (width - filled)


if __name__ == '__main__':
    sys.exit(main())
