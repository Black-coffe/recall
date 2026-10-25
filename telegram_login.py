#!/usr/bin/env python
"""Phase 17 — one-time Telegram login via QR code (MTProto/Telethon).

Run (a REAL console is needed to enter the 2FA password):
    double-click telegram_login.bat
    or:  .venv\\Scripts\\python.exe telegram_login.py

Why QR and not a code: Telegram sends the code to the service chat «Telegram» (easy to
miss; it does NOT arrive by SMS while an active session exists). QR is simpler:
scan it with the phone and you are done. If a cloud password (2FA) is enabled, the script asks for
it in the console (entered locally, sent nowhere).

⚠️ Telethon caveat: after a QR login `client.sign_in(password=...)` raises
ValueError, so 2FA must be completed at a low level (compute_check +
CheckPasswordRequest), see _complete_2fa.

Prerequisites: TELEGRAM_API_ID/HASH in .env; pip install telethon qrcode Pillow.
On success telegram.session is created; after that Recall (app.py / shortcut) starts
the listener itself. A repeat run when already logged in simply confirms that.
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
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass


async def _complete_2fa(client, pwd: str) -> None:
    """Complete 2FA after QR at a low level (client.sign_in(password=) raises here
    ValueError because SessionPasswordNeededError came from ExportLoginTokenRequest)."""
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
        print(f"[OK] Already logged in: {me.first_name} (@{me.username}, id={me.id})")
        await client.disconnect()
        return 0

    qr_login = await client.qr_login()
    png = os.path.abspath("telegram_qr.png")

    def _save():
        qrcode.make(qr_login.url).save(png)

    _save()
    try:
        os.startfile(png)                       # Windows: open the image on screen
    except Exception:
        print(f"Open the QR file manually: {png}")
    print("Scan the QR with your phone: Telegram → Settings → Devices → "
          "Link Desktop Device")

    ok = False
    for _ in range(12):                          # ~12×25s = up to ~5 min
        try:
            await qr_login.wait(timeout=25)
            ok = True
            break
        except asyncio.TimeoutError:
            await qr_login.recreate()
            _save()                              # QR refreshed → save the image again
        except SessionPasswordNeededError:
            pwd = input("\nEnter the Telegram cloud password (2FA) and press Enter: ")
            await _complete_2fa(client, pwd)
            ok = True
            break

    try:
        os.remove(png)                           # the image contains a token — remove it
    except OSError:
        pass

    if ok:
        me = await client.get_me()
        print(f"\n[OK] Logged in: {me.first_name} (@{me.username}, id={me.id})")
        print("Session saved. Now launch Recall (desktop shortcut).")
        await client.disconnect()
        return 0

    print("[!] QR was not scanned in time. Close the window and run again.")
    await client.disconnect()
    return 1


def main() -> int:
    _load_env()
    api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    if not api_id or not api_hash:
        print("[ERROR] TELEGRAM_API_ID / TELEGRAM_API_HASH missing in .env "
              "(values from https://my.telegram.org).")
        return 1
    try:
        api_id_int = int(api_id)
    except ValueError:
        print(f"[ERROR] TELEGRAM_API_ID must be a number, not '{api_id}'.")
        return 1
    try:
        import telethon  # noqa: F401
        import qrcode  # noqa: F401
    except ImportError:
        print("[ERROR] Install the dependencies: "
              ".venv\\Scripts\\python.exe -m pip install telethon qrcode Pillow")
        return 1

    session = os.environ.get("TELEGRAM_SESSION", "telegram")
    print(f"Telegram QR login (session: {session}.session)\n")
    return asyncio.run(run(api_id_int, api_hash, session))


if __name__ == "__main__":
    raise SystemExit(main())
