"""Defect card docs/defects/proxied-access-trusted-as-local.md: a proxy does not make a foreign node "local".

Usage: check_proxy_trust.py <auth.py> - loads the file as the gate module on a minimal
Flask app and replays requests the way `tailscale serve` delivers them (REMOTE_ADDR 127.0.0.1 +
X-Forwarded-For tailnet IP + the owner's Tailscale-User-Login). exit 1 if:
  A. server vps-prod got /api/* with no settings (empty RECALL_PROXY_ALLOWED_IPS);
  B. vps-prod got /api/* when only the phone is allowed;
  C. the normal path is broken: direct localhost or the allowed phone got 401.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VPS, PHONE, LOGIN = "100.64.0.9", "100.64.0.7", "owner@example.com"


def _app(path: Path):
    from flask import Flask, jsonify
    spec = importlib.util.spec_from_file_location(f"auth_under_check_{abs(hash(path))}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = Flask(__name__)
    app.add_url_rule("/api/history", "h", lambda: jsonify([]))
    mod.register(app)
    return app.test_client()


def _code(client, ip=None) -> int:
    headers = {"X-Forwarded-For": ip, "Tailscale-User-Login": LOGIN} if ip else {}
    return client.get("/api/history", headers=headers,
                      environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "app" / "core" / "auth.py"
    import logging
    logging.disable(logging.CRITICAL)
    for k in ("RECALL_LOCAL_TRUSTED", "RECALL_API_KEY", "RECALL_PROXY_ALLOWED_IPS"):
        os.environ.pop(k, None)
    client = _app(target)
    bad = []
    if _code(client, VPS) != 401:
        bad.append("A: vps-prod through serve got in with no settings")
    os.environ["RECALL_PROXY_ALLOWED_IPS"] = PHONE
    if _code(client, VPS) != 401:
        bad.append("B: vps-prod got in when only the phone is allowed")
    if _code(client) != 200 or _code(client, PHONE) != 200:
        bad.append("C: direct localhost or the allowed phone was refused")
    for b in bad:
        print(f"{target}: {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
