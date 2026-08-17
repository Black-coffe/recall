#!/usr/bin/env python
"""Phase 17 — одноразовий вхід у Telegram через QR-код (MTProto/Telethon).

Запуск (потрібна РЕАЛЬНА консоль для вводу пароля 2FA):
    подвійний клік telegram_login.bat
    або:  .venv\\Scripts\\python.exe telegram_login.py

Чому QR, а не код: код Telegram надсилає у службовий чат «Telegram» (легко не
помітити, в SMS він НЕ приходить за наявності активної сесії). QR простіше —
скануєш телефоном і готово. Якщо ввімкнено хмарний пароль (2FA), скрипт спитає
його в консолі (вводиться локально, нікуди не передається).

⚠️ Важливий нюанс Telethon: після QR-логіну `client.sign_in(password=...)` кидає
ValueError — 2FA треба завершувати низькорівнево (compute_check +
CheckPasswordRequest), див. _complete_2fa.

Передумови: TELEGRAM_API_ID/HASH у .env; pip install telethon qrcode Pillow.
Після успіху створюється telegram.session — далі Recall (app.py/ярлик) сам
піднімає слухача. Повторний запуск, якщо вже залогінено, просто це підтвердить.
"""
from __future__ import annotations

import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


async def _complete_2fa(client, pwd: str) -> None:
    """Завершити 2FA після QR низькорівнево (client.sign_in(password=) тут кидає
    ValueError, бо SessionPasswordNeededError прийшов від ExportLoginTokenRequest)."""
    from telethon.tl.functions.account import GetPasswordRequest
    from telethon.tl.functions.auth import CheckPasswordRequest
    from telethon.password import compute_check
    info = await client(GetPasswordRequest())
    await client(CheckPasswordRequest(compute_check(info, pwd)))


async def run(api_id: int, api_hash: str, session: str) -> int:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError
    import qrcode

    client = TelegramClient(session, api_id, api_hash)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"[OK] Уже залогінено: {me.first_name} (@{me.username}, id={me.id})")
        await client.disconnect()
        return 0

    qr_login = await client.qr_login()
    png = os.path.abspath("telegram_qr.png")

    def _save():
        qrcode.make(qr_login.url).save(png)

    _save()
    try:
        os.startfile(png)                       # Windows: відкрити картинку на екрані
    except Exception:
        print(f"Відкрий вручну файл з QR: {png}")
    print("Скануй QR з телефону: Telegram → Налаштування → Пристрої → "
          "Підключити пристрій")

    ok = False
    for _ in range(12):                          # ~12×25с = до ~5 хв
        try:
            await qr_login.wait(timeout=25)
            ok = True
            break
        except asyncio.TimeoutError:
            await qr_login.recreate()
            _save()                              # QR оновився → перезберігаємо картинку
        except SessionPasswordNeededError:
            pwd = input("\nВведіть хмарний пароль Telegram (2FA) і натисніть Enter: ")
            await _complete_2fa(client, pwd)
            ok = True
            break

    try:
        os.remove(png)                           # картинка містить токен — прибираємо
    except OSError:
        pass

    if ok:
        me = await client.get_me()
        print(f"\n[OK] Вхід виконано: {me.first_name} (@{me.username}, id={me.id})")
        print("Сесію збережено. Тепер запускай Recall (ярлик на робочому столі).")
        await client.disconnect()
        return 0

    print("[!] QR не відскановано вчасно. Закрий вікно і запусти ще раз.")
    await client.disconnect()
    return 1


def main() -> int:
    _load_env()
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        print("[ПОМИЛКА] Немає TELEGRAM_API_ID / TELEGRAM_API_HASH у .env "
              "(значення з https://my.telegram.org).")
        return 1
    try:
        api_id_int = int(api_id)
    except ValueError:
        print(f"[ПОМИЛКА] TELEGRAM_API_ID має бути числом, а не '{api_id}'.")
        return 1
    try:
        import telethon  # noqa: F401
        import qrcode  # noqa: F401
    except ImportError:
        print("[ПОМИЛКА] Встанови залежності: "
              ".venv\\Scripts\\python.exe -m pip install telethon qrcode Pillow")
        return 1

    session = os.environ.get("TELEGRAM_SESSION", "telegram")
    print(f"Вхід у Telegram через QR (сесія: {session}.session)\n")
    return asyncio.run(run(api_id_int, api_hash, session))


if __name__ == "__main__":
    raise SystemExit(main())
