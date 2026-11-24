from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, body: bytes, timestamp: str) -> str:
    """HMAC-SHA256 over '{timestamp}.{body}', hex-prefixed with 'sha256='.

    Server signs outgoing webhooks with this; client verifies with the same
    shared secret (the subscription's webhook_secret).
    """
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, body: bytes, timestamp: str, signature: str,
           max_age_sec: int = 300, now: float | None = None) -> bool:
    """Verify signature + freshness (anti-replay)."""
    import time

    now = now if now is not None else time.time()
    try:
        if abs(now - int(timestamp)) > max_age_sec:
            return False
    except (TypeError, ValueError):
        return False
    expected = sign(secret, body, timestamp)
    return hmac.compare_digest(expected, signature or "")


__all__ = ["sign", "verify"]
