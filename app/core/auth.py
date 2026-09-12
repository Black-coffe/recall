"""T1.2 (REMEDIATION_PLAN Волна 1): аутентифікаційний гейт на всі /api/*.

Проблема: 0 перевірок особи на ~90 ендпоінтах (запис/видалення/експорт/
дорогі Claude-виклики). Секретом досі захищений лише /api/telegram/ingest
(shared-secret, telegram_common.py) і MCP --transport http (RECALL_MCP_KEY).
У поєднанні з мережевим bind (Волна 0: дефолт 127.0.0.1, але opt-in
RECALL_BIND_ALL існує) — вектор атаки для LAN-доступу без пароля.

Рішення: ОДИН централізований `before_request`-гейт (не декоратор на кожен
ендпоінт — їх ~90, легко забути новий). Патерн ключа скопійовано з наявного
Telegram shared-secret (telegram_common.control_token) — не тягнемо
flask-login/JWT заради single-user застосунку.

Логіка рішення (у порядку перевірки):
  1. OPTIONS (CORS preflight) і шляхи поза allowlist'ом exempt — завжди crossed.
  2. RECALL_LOCAL_TRUSTED (дефолт "1"/увімкнено) І запит з localhost
     (request.remote_addr ∈ {127.0.0.1, ::1}) → пропускаємо БЕЗ ключа.
     Це зберігає поточний UX: застосунок працює на своїй машині без пароля.
  3. Інакше потрібен валідний ключ: RECALL_API_KEY має бути заданий у env,
     і клієнт має прислати той самий ключ у заголовку X-Recall-Api-Key
     (або Authorization: Bearer <key>). Порівняння — constant-time
     (hmac.compare_digest), щоб не текти час порівняння.
  4. Якщо RECALL_API_KEY НЕ заданий, а доступ не підпадає під local-trusted
     (не localhost, або RECALL_LOCAL_TRUSTED=0) → fail-closed: 401 для
     БУДЬ-ЯКОГО ключа/його відсутності. Без налаштованого ключа зовнішній
     доступ заборонений за замовчуванням.

Allowlist (без auth, гейт узагалі не застосовується):
  - усе, що НЕ починається з /api/  (SPA-шелл '/', /<path:_path>, /static/*,
    /favicon.ico, /sw.js — вони й так не /api/*, тож правило "гейт лише на
    /api/*" покриває їх автоматично)
  - /api/health (health-check лишається відкритим для моніторингу)
  - OPTIONS-запити (CORS preflight, інакше ламаємо CORS з app.py ~231)

НЕ гейтиться тут: /api/telegram/ingest має ВЛАСНИЙ shared-secret механізм
(X-Telegram-Token, telegram_common.control_token) — цей гейт додається
ПОВЕРХ нього, а не замінює. У дефолтному режимі (RECALL_LOCAL_TRUSTED=1)
listener::ingest() йде з localhost → local-trusted пропускає, поведінка не
змінюється. Якщо оператор вимкне RECALL_LOCAL_TRUSTED (RECALL_LOCAL_TRUSTED=0)
БЕЗ оновлення telegram_listener.py, щоб він теж слав X-Recall-Api-Key —
ingest почне отримувати 401 від ЦЬОГО гейта (telegram_listener.py навмисно
поза межами цієї задачі — не чіпали). Задокументовано як відома суміжна
проблема у звіті T1.2, не виправлено тут.
"""
from __future__ import annotations

import hmac
import logging
import os

from flask import Flask, jsonify, request

from app.core import settings

logger = logging.getLogger(__name__)

# Заголовок за зразком telegram_common.CONTROL_TOKEN_HEADER ("X-Telegram-Token").
API_KEY_HEADER = "X-Recall-Api-Key"

_LOCALHOST_ADDRS = {"127.0.0.1", "::1"}

# Шляхи /api/*, які лишаються відкритими без ключа (health-check для
# моніторингу — не повинен вимагати авторизацію, інакше зовнішні
# health-check'и/аптайм-монітори теж треба буде авторизовувати). /api/ready
# (config-registry-profiles S3, ще не існує на момент S2) — той самий клас:
# без auth, як /api/health.
_EXEMPT_API_PATHS = {"/api/health", "/api/ready"}


def _local_trusted_enabled() -> bool:
    """RECALL_LOCAL_TRUSTED; профіле-залежний дефолт живе в реєстрі
    (`app/core/settings.py::Setting.default_headless`, config-registry-fix-01):
    desktop — "1" (як і було, застосунок працює на своїй машині без пароля);
    headless — "0" (fail-closed — headless типово слухає ширше за одну машину,
    localhost більше не мається на увазі довіреним). Явне значення змінної
    середовища завжди має пріоритет над дефолтом профілю (settings.env)."""
    return settings.env_bool("RECALL_LOCAL_TRUSTED")


def _configured_api_key() -> str | None:
    val = os.environ.get("RECALL_API_KEY", "").strip()
    return val or None


def _is_localhost(remote_addr: str | None) -> bool:
    return (remote_addr or "") in _LOCALHOST_ADDRS


def _extract_presented_key() -> str | None:
    """X-Recall-Api-Key має пріоритет; Authorization: Bearer <key> — зручний фолбек
    (напр. для клієнтів, що вже мають стандартну Bearer-обв'язку)."""
    header_key = request.headers.get(API_KEY_HEADER)
    if header_key:
        return header_key.strip()
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[len("bearer "):].strip()
    return None


def _is_exempt(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    if not path.startswith("/api/"):
        # SPA-шелл ('/', deep-links), /static/*, /favicon.ico, /sw.js — усе
        # це поза /api/* і так і мало лишатись доступним без ключа.
        return True
    if path in _EXEMPT_API_PATHS:
        return True
    return False


def _is_authorized() -> bool:
    if _local_trusted_enabled() and _is_localhost(request.remote_addr):
        return True

    configured = _configured_api_key()
    if not configured:
        # Fail-closed: без налаштованого ключа зовнішній (не-local-trusted)
        # доступ заборонений за замовчуванням.
        return False

    presented = _extract_presented_key()
    if not presented:
        return False
    return hmac.compare_digest(presented, configured)


def register(app: Flask) -> None:
    """Реєструє єдиний before_request-гейт на весь застосунок."""

    if settings.profile() == "headless" and not _configured_api_key():
        logger.warning(
            "Auth gate: profile=headless без RECALL_API_KEY — non-localhost "
            "доступ буде відхилено (fail-closed), доки не задано ключ або "
            "явно не увімкнено RECALL_LOCAL_TRUSTED=1."
        )

    @app.before_request
    def _recall_auth_gate():  # noqa: ANN202 — Flask hook, без анотації повернення
        if _is_exempt(request.path, request.method):
            return None
        if _is_authorized():
            return None
        logger.warning(
            "Auth gate: 401 %s %s (remote=%s)",
            request.method, request.path, request.remote_addr,
        )
        return jsonify({"success": False, "error": "unauthorized"}), 401
