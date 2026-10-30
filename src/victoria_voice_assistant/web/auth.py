from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE_NAME = "victoria_session"
SESSION_TTL_S = 30 * 24 * 60 * 60


class SessionSigner:
    """Issues and verifies HMAC-signed session cookies.

    The signing key is random per process, so restarting the web process signs
    everyone out. That is acceptable for a household app and avoids storing secrets.
    """

    def __init__(self, password: str) -> None:
        self._password_digest = hashlib.sha256(password.encode()).digest()
        self._key = secrets.token_bytes(32)

    def check_password(self, candidate: str) -> bool:
        digest = hashlib.sha256(candidate.encode()).digest()
        return hmac.compare_digest(digest, self._password_digest)

    def issue(self, now: float | None = None) -> str:
        expires = int((now if now is not None else time.time()) + SESSION_TTL_S)
        return f"{expires}.{self._sign(str(expires))}"

    def verify(self, token: str | None, now: float | None = None) -> bool:
        if not token or "." not in token:
            return False
        expires_text, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, self._sign(expires_text)):
            return False
        try:
            expires = int(expires_text)
        except ValueError:
            return False
        return expires > (now if now is not None else time.time())

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
