"""Спільний модуль для Flask-блюпринта і слухача Telegram (Phase 17C).

Shared-secret для двостороннього localhost-зв'язку:
  • Flask → listener control-API (/dialogs, /status, /backfill)
  • listener → Flask /api/telegram/ingest

Localhost-IP перевірка спуфабельна і не захищає від інших ЛОКАЛЬНИХ процесів,
тому обидва кінці вимагають заголовок X-Telegram-Token. Токен спільний:
генерується раз і персиститься у telegram_control.token (gitignore). Обидва
процеси читають той самий файл → автоматично однаковий токен, без ручних кроків.
Можна перекрити через env TELEGRAM_CONTROL_TOKEN.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

CONTROL_TOKEN_HEADER = "X-Telegram-Token"
_TOKEN_FILE = Path(__file__).parent / "telegram_control.token"


def control_token() -> str:
    """Спільний секрет Flask↔listener. env override → файл → згенерувати атомарно."""
    env = os.environ.get("TELEGRAM_CONTROL_TOKEN")
    if env and env.strip():
        return env.strip()

    token = secrets.token_hex(32)
    try:
        # O_CREAT|O_EXCL — атомарно: лише перший процес створює, решта читають.
        fd = os.open(_TOKEN_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, token.encode("ascii"))
        finally:
            os.close(fd)
        return token
    except FileExistsError:
        return _TOKEN_FILE.read_text(encoding="ascii").strip()
    except OSError:
        # Не змогли записати (права/ФС) — токен лише в пам'яті цього процесу.
        # Другий процес згенерує свій → зв'язок не пройде; але це деградація,
        # а не креш. Лог піде на рівні викликача.
        return token
