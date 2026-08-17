"""
app/services/recording/region_overlay.py — Phase 22 region overlay indicator.

A lightweight always-on-top, click-through, transparent native window that draws
a border + size label around a screen-capture REGION while recording, so the user
sees on the physical monitor exactly what area is being captured.

Key property: the overlay is EXCLUDED from screen capture via
SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) (Windows 10 2004+ / Win11) — the
user sees the frame, but it does NOT appear in the recorded video (like a Camtasia
guide). Pass --no-exclude to disable that (e.g. for verification screenshots).

Runs standalone (spawned by VideoCaptureSupervisor, killed on stop):
    python -m app.services.recording.region_overlay --x 200 --y 150 --w 1280 --h 720 --label "1280×720"

Coordinates are VIRTUAL-DESKTOP pixels (monitor.pos + region offset). Best-effort:
any failure exits quietly without affecting the recording.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import sys

# Win32 constants
_WDA_EXCLUDEFROMCAPTURE = 0x11
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_TRANSPARENT_KEY = "#010203"  # chroma-key colour unlikely to collide with the border


def _apply_win32_styles(hwnd: int, exclude: bool) -> None:
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    ex = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    ex |= _WS_EX_LAYERED | _WS_EX_TRANSPARENT | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE
    user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex)
    if exclude:
        # WDA_EXCLUDEFROMCAPTURE keeps the window on-screen but out of capture APIs
        # (Desktop Duplication / ddagrab, Graphics Capture, PrintWindow, BitBlt).
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        try:
            user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)
        except Exception:
            pass


def run(x: int, y: int, w: int, h: int, label: str, color: str, border: int, exclude: bool) -> int:
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", _TRANSPARENT_KEY)
    except tk.TclError:
        pass
    root.config(bg=_TRANSPARENT_KEY)
    root.geometry(f"{w}x{h}+{x}+{y}")

    cv = tk.Canvas(root, width=w, height=h, bg=_TRANSPARENT_KEY, highlightthickness=0, bd=0)
    cv.pack(fill="both", expand=True)

    # Border hugging the inside edge of the region (interior stays transparent).
    half = max(1, border // 2)
    cv.create_rectangle(half, half, w - half, h - half, outline=color, width=border)

    # Size label chip in the top-left corner.
    text = label or f"{w}×{h}"
    pad = 5
    t = cv.create_text(border + pad + 2, border + 11, anchor="w", text=text,
                       fill="white", font=("Segoe UI", 9, "bold"))
    bb = cv.bbox(t)
    if bb:
        chip = cv.create_rectangle(bb[0] - pad, bb[1] - 3, bb[2] + pad, bb[3] + 3,
                                   fill=color, outline="")
        cv.tag_lower(chip, t)

    root.update_idletasks()
    # Resolve the real top-level HWND for an overrideredirect Tk window.
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    try:
        _apply_win32_styles(hwnd, exclude)
    except Exception:
        pass

    root.mainloop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Screen-capture region overlay indicator")
    ap.add_argument("--x", type=int, required=True)
    ap.add_argument("--y", type=int, required=True)
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--h", type=int, required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--color", default="#e5484d")  # accent red
    ap.add_argument("--border", type=int, default=3)
    ap.add_argument("--no-exclude", action="store_true",
                    help="do NOT exclude from capture (overlay will appear in the recording)")
    a = ap.parse_args(argv)
    try:
        return run(a.x, a.y, a.w, a.h, a.label, a.color, a.border, exclude=not a.no_exclude)
    except Exception as e:  # best-effort: never crash loudly
        sys.stderr.write(f"region_overlay error: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
