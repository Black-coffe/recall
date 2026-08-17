"""Спільна утиліта захисту від path traversal (T1.5, Волна 1).

Раніше `os.path.abspath(...).startswith(...)` / `os.path.realpath(...)`-перевірка
писалась вручну і по-різному в кожному blueprint'і (documents.py, transcription.py,
telegram.py) — де паттерн забули скопіювати, захисту не було. Тепер одна точка
правди: `safe_path_within` (одна база) і `safe_path_within_any` (декілька
дозволених коренів, напр. uploads + youtube_downloads + recordings).

Контракт:
- Резолвить і `base`, і `candidate` через `os.path.realpath` — розкриває
  симлінки, нормалізує `..`/`.`, коректно працює з Windows-літерами дисків
  та UNC-шляхами (`\\\\server\\share\\...`).
- Порівняння регістронезалежне на Windows (`os.path.normcase`) — NTFS
  case-insensitive за замовчуванням; на POSIX лишається case-sensitive
  (`normcase` там — no-op).
- Межу перевіряє по роздільнику шляху (`base + os.sep`), а не голим
  `startswith`, щоб `C:\\foo` не пропускав `C:\\foobar\\secret.txt`.
- НЕ перевіряє існування `candidate` — придатна і для шляхів, які ще будуть
  створені (наприклад, ціль завантаження файлу), і для вже існуючих.
- Повертає резолвлений абсолютний шлях `candidate`, якщо він дорівнює `base`
  або лежить строго всередині нього; інакше `None` (traversal/symlink-escape/
  шлях поза межами).

НЕ використовується у `mcp_server.py`: той — окремий stdio-процес, і навмисно
тримає власну легку копію цього ж патерну, щоб не тягнути важкий імпорт
пакету `app` (Flask/DB-ініціалізація) у процес, де це критично для часу
старту (див. memory/mcp-stdio-no-heavy-models.md — «нічого важкого в
mcp_server.py»). Якщо колись знадобиться синхронізувати логіку — синхронізуй
вручну, не імпортуй звідси.
"""
from __future__ import annotations

import os


def safe_path_within(base: str | os.PathLike, candidate: str | os.PathLike) -> str | None:
    """Повертає резолвлений `candidate`, якщо він у межах `base`, інакше None.

    Приклад:
        docs_dir = "documents"
        target = safe_path_within(docs_dir, os.path.join(docs_dir, filename))
        if target is None:
            raise ValueError("path traversal")
    """
    if not base or not candidate:
        return None
    base_real = os.path.realpath(str(base))
    cand_real = os.path.realpath(str(candidate))
    base_cmp = os.path.normcase(base_real)
    cand_cmp = os.path.normcase(cand_real)
    if cand_cmp == base_cmp or cand_cmp.startswith(base_cmp + os.sep):
        return cand_real
    return None


def safe_path_within_any(
    bases: "list[str | os.PathLike] | tuple[str | os.PathLike, ...]",
    candidate: str | os.PathLike,
) -> str | None:
    """Те саме, що `safe_path_within`, але з кількома дозволеними коренями
    (напр. uploads + youtube_downloads + recordings). Повертає резолвлений
    шлях при першому збігу, інакше None. Порожні/falsy елементи `bases`
    пропускаються.
    """
    for base in bases:
        if not base:
            continue
        result = safe_path_within(base, candidate)
        if result is not None:
            return result
    return None
