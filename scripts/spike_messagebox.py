"""Spike: can a MessageBoxW raised from a non-main thread be found and closed by title?

Run (owner present, NEVER during a recording; shows one real dialog for up to 8 s):
    .venv/Scripts/pythonw.exe scripts/spike_messagebox.py
Writes logs/spike_messagebox.json with the keys: shown, return_value, found_by_title,
closed_by_post, elapsed_s, station_visible, python_exe. Stdlib only; not imported by tests.
"""
import ctypes
import json
import os
import sys
import threading
import time

MB_FLAGS = 0x4 | 0x30 | 0x100 | 0x1000 | 0x10000 | 0x40000
WM_COMMAND, IDNO = 0x0111, 7
DEADLINE_S = 8.0


def main() -> dict:
    result = {"shown": False, "return_value": None, "found_by_title": False,
              "closed_by_post": False, "elapsed_s": 0.0, "station_visible": False,
              "python_exe": sys.executable}
    user32 = ctypes.windll.user32

    class Flags(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int), ("b", ctypes.c_int), ("flags", ctypes.c_uint32)]

    flags, needed = Flags(), ctypes.c_uint32(0)
    ok = user32.GetUserObjectInformationW(user32.GetProcessWindowStation(), 1,
                                          ctypes.byref(flags), ctypes.sizeof(flags),
                                          ctypes.byref(needed))
    result["station_visible"] = bool(ok) and bool(flags.flags & 1)
    title = "Recall spike %d" % (int(time.time()) % 10000)
    box = {}
    done = threading.Event()

    def run():
        box["value"] = user32.MessageBoxW(None, "Spike: close me or wait 8 s", title, MB_FLAGS)
        done.set()

    start = time.monotonic()
    threading.Thread(target=run, daemon=True).start()
    if not done.wait(DEADLINE_S):
        result["shown"] = True
        hwnd = user32.FindWindowW(None, title)
        result["found_by_title"] = bool(hwnd)
        if hwnd:
            result["closed_by_post"] = bool(user32.PostMessageW(hwnd, WM_COMMAND, IDNO, 0))
            done.wait(2.0)
    else:
        result["shown"] = True
    result["return_value"] = box.get("value")
    result["elapsed_s"] = round(time.monotonic() - start, 2)
    return result


if __name__ == "__main__":
    res = main()
    os.makedirs("logs", exist_ok=True)
    with open(os.path.join("logs", "spike_messagebox.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)
    print(json.dumps(res))
