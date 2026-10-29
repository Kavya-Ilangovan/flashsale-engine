from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .config import Settings


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, body: str) -> bytes:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()


def issue_token(settings: Settings, user_id: str, now: float | None = None) -> str:
    payload = {"sub": user_id, "exp": int((now or time.time()) + settings.session_ttl_sec)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_b64(_mac(settings.session_secret, body))}"


def verify_token(settings: Settings, token: str | None, now: float | None = None) -> str | None:
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        supplied = _unb64(signature)
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not hmac.compare_digest(supplied, _mac(settings.session_secret, body)):
        return None
    if payload.get("exp", 0) <= (now or time.time()):
        return None
    return payload.get("sub")
