"""Catalog: server texts of the Telegram listener control-API that the UI's Telegram tab shows."""


def _m(uk: str, en: str) -> dict:
    return {"uk": uk, "en": en}


MESSAGES: dict = {
    # telegram_listener.py (control-API, 400 responses relayed by the telegram blueprint)
    "svc_telegram.listener.err.repairArgs": _m(
        "потрібні chat_id та непорожній ids",
        "chat_id and a non-empty ids are required"),
    "svc_telegram.listener.err.chatIdRequired": _m(
        "chat_id обов'язковий",
        "chat_id is required"),
}
