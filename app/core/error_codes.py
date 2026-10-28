"""The one table of error codes: code -> canonical HTTP status.

Stdlib only, so the MCP process can import it without pulling Flask. The table does not force
a route to use the canonical status; `tests/test_error_codes.py` only checks that every
`error_code` literal in the MCP server, the blueprints and the auth gate is a key here.
"""
from __future__ import annotations

ERROR_CODES: dict[str, int] = {
    # generic codes, one per status class
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "RATE_LIMITED": 429,
    "INTERNAL_ERROR": 500,
    "UNAVAILABLE": 503,
    # local boundary
    "HOST_REJECTED": 403,
    "ORIGIN_REJECTED": 403,
    # jobs
    "JOB_FAILED": 500,
    "JOB_CRASHED": 500,
    "INVALID_ARGUMENT": 400,
    "TOO_MANY_WAITERS": 429,
    # MCP side
    "MCP_READONLY": 403,
    "SECRET_IN_ARGUMENT": 400,
    "INVALID_ACTION": 400,
    "PRIVATE": 403,
    "MCP_VISUAL_OFF": 403,
    "MCP_VISUAL_TEXT_ONLY": 403,
    "PRIVACY_CHECK_FAILED": 503,
    "AMBIGUOUS_SESSION": 409,
    "AMBIGUOUS_PROJECT": 409,
    "APP_UNREACHABLE": 502,
    # blueprint inventory
    "ALREADY_RUNNING": 409,
    "BAD_ACTION": 400,
    "INVALID_TRANSITION": 409,
    "BAD_SETTINGS": 400,
    "COPILOT_BUSY": 409,
    "COPILOT_DISABLED": 503,
    "COPILOT_MOVE_FAILED": 500,
    "CRASHED": 500,
    "DEVICE_ENUM_FAILED": 500,
    "EMPTY": 400,
    "EMPTY_QUESTION": 400,
    "INTERNAL": 500,
    "INVALID_REASON": 400,
    "INVALID_TOP_K": 400,
    "NO_COPILOT": 404,
    "NO_FINAL": 404,
    "NO_STREAMS": 409,
    "NO_SYSTEM_STREAM": 409,
    "NO_VIDEO": 404,
    "NO_MEDIA": 404,
    "MODEL_NOT_INSTALLED": 400,
    "RECORDING_ACTIVE": 409,
    "RECORDING_DISABLED": 503,
    "RECORDING_FAILED": 500,
    "RECORDING_KEPT": 409,
    "RECORDING_START_FAILED": 500,
    "RECORDING_STREAM_FAILED": 500,
    "RETARGET_FAILED": 500,
    "SESSION_LIMIT": 429,
    "TIMEOUT": 504,
    "UNSUPPORTED_SOURCE_SPEC": 400,
    "DB_ERROR": 500,
    # mail blueprint (`_err(code, key, status)` in app/blueprints/mail.py)
    "INVALID_ADDRESS": 400,
    "EMPTY_PASSWORD": 400,
    "NO_PASSWORD": 409,
    "BAD_FLAG": 400,
    "BAD_CATEGORY": 400,
    "PRIVATE_CATEGORY_CHANGE": 403,
    "BAD_ACCOUNT": 400,
    "BAD_MONTHS": 400,
    "MAIL_POLLER_OFF": 503,
    "CALENDAR_POLLER_OFF": 503,  # k6-calendar-ics: sync now while the poller is not running
    "USE_GATED_ROUTE": 400,  # the D variant of a plain mail route lives on its own gated route
    # MailError codes (app/services/mail_imap.py), mapped by `_MAIL_ERRORS` in mail.py
    "AUTH_FAILED": 400,
    "CONNECT_FAILED": 502,
    "FOLDER_MISSING": 502,
    "PROTOCOL": 502,
    # approval dialog
    "APPROVAL_DENIED": 403,
    "APPROVAL_UNAVAILABLE": 503,
    "APPROVAL_PENDING": 409,
    "RATE_CEILING": 429,
    # mcp-archive-org agent writes
    "NOT_MCP_EVENT": 403,
    "UNDO_NOT_LAST": 409,
    "OWNER_AUTHORED": 403,
    "USE_UI": 403,
    "SPEAKER_ID_REQUIRED": 400,
    "IS_SELF": 403,
    "JOB_ACTIVE": 409,
    "BULK_LIMIT": 400,
    "EVICTION_NOT_APPROVED": 409,
    # mcp-pipeline: background transcription, intake by path, model download
    "NOT_FINALIZED": 409,
    "PATH_NOT_ALLOWED": 403,
    "UNSUPPORTED_EXTENSION": 400,
    "FILE_TOO_LARGE": 413,
    "UNKNOWN_MODEL": 400,
    # live-video-frames: frames of a recording that is still running
    "PAST_LIVE_EDGE": 409,
    "NOT_RECORDED": 404,
    # mcp-telegram-automation: Telegram chats, pipes on demand
    "LISTENER_OFFLINE": 503,
    "COOLDOWN": 429,
    "PIPES_DISABLED": 409,
    "NOT_CONFIGURED": 503,
    "NOT_MONITORED": 404,
    # mcp-recording-live: agent recordings, owner sessions, start notice, cancel window
    "OWNER_SESSION": 403,
    "NOTIFY_UNAVAILABLE": 503,
    "CANCEL_WINDOW_CLOSED": 409,
    # wave2-recording-calls: Windows refused a capture stream (privacy setting / E_ACCESSDENIED)
    "DEVICE_ACCESS_DENIED": 403,
    # wave4-client-calls: "what did I miss" live summary
    "AGENT_SESSION": 403,
    "LIVE_BUDGET_EXHAUSTED": 429,
}

_BY_STATUS = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
    409: "CONFLICT", 429: "RATE_LIMITED", 503: "UNAVAILABLE",
}


def status_for(code: str) -> int:
    """Canonical HTTP status of a code (500 for an unknown one)."""
    return ERROR_CODES.get(code, 500)


def code_for_status(status: int) -> str:
    """Generic code for an HTTP status."""
    if status in _BY_STATUS:
        return _BY_STATUS[status]
    if 400 <= status < 500:
        return "BAD_REQUEST"
    if status == 502:
        return "APP_UNREACHABLE"
    return "INTERNAL_ERROR"
