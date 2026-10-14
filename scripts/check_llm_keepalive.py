"""Карточка docs/defects/local-llm-during-recording.md: виклик локальної моделі з явною
`model=` без `keep_alive=` — модель тримає VRAM ще LOCAL_LLM_KEEPALIVE (30 хв) після
відповіді, і дзвінок, що почнеться в цей час, лишає копілот і транскрипцію без місця.

Використання: check_llm_keepalive.py <file.py | dir> — exit 1, якщо є такий виклик.
Модель копілота (без `model=`) не чіпаємо: вона й так живе у VRAM.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

CALLS = {"generate", "generate_json"}


def offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name not in CALLS:
            continue
        kws = {k.arg for k in node.keywords if k.arg}
        if "model" in kws and "keep_alive" not in kws:
            out.append(f"{path}:{node.lineno}: {name}(model=…) без keep_alive=")
    return out


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path("app")
    files = [target] if target.is_file() else sorted(target.rglob("*.py"))
    found = [line for f in files for line in offenders(f)]
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
