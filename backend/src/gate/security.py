from __future__ import annotations

import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from gate.config import SecurityConfig

SESSION_COOKIE = "gate_session"


class SessionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SessionData:
    expires_at: datetime
    csrf_token: str


class SessionManager:
    def __init__(
        self,
        config: SecurityConfig,
        *,
        password_hash: str | None,
        session_secret: str | None,
    ) -> None:
        self.config = config
        self.password_hash = password_hash
        self.session_secret = session_secret.encode("utf-8") if session_secret else None
        self.password_hasher = PasswordHasher()

    @property
    def configured(self) -> bool:
        if not self.config.enabled:
            return True
        return bool(
            self.password_hash
            and self.session_secret is not None
            and len(self.session_secret) >= 32
        )

    def origin_allowed(self, origin: str | None) -> bool:
        if origin is None:
            return False
        return origin.rstrip("/") in self.config.allowed_origins

    def verify_password(self, password: str) -> bool:
        if not self.config.enabled or not self.configured or self.password_hash is None:
            return False
        try:
            return self.password_hasher.verify(self.password_hash, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False

    def hash_password(self, password: str) -> str:
        return self.password_hasher.hash(password)

    def replace_credentials(self, password_hash: str, session_secret: str) -> None:
        self.password_hash = password_hash
        self.session_secret = session_secret.encode("utf-8")

    def issue(self, now: datetime | None = None) -> tuple[str, SessionData]:
        if not self.configured or self.session_secret is None:
            raise SessionError("authentication is not configured")
        issued_at = (now or datetime.now(UTC)).replace(microsecond=0)
        session = SessionData(
            expires_at=issued_at + timedelta(hours=self.config.session_hours),
            csrf_token=secrets.token_urlsafe(32),
        )
        payload = {
            "csrf": session.csrf_token,
            "exp": int(session.expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(12),
        }
        encoded = self._encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = self._encode(hmac.new(self.session_secret, encoded, hashlib.sha256).digest())
        return f"{encoded.decode()}.{signature.decode()}", session

    def decode(self, token: str | None, now: datetime | None = None) -> SessionData:
        if not token or not self.configured or self.session_secret is None:
            raise SessionError("missing or invalid session")
        try:
            encoded, supplied_signature = token.split(".", 1)
            encoded_bytes = encoded.encode("ascii")
            expected_signature = self._encode(
                hmac.new(self.session_secret, encoded_bytes, hashlib.sha256).digest()
            ).decode("ascii")
            if not secrets.compare_digest(supplied_signature, expected_signature):
                raise SessionError("missing or invalid session")
            payload = json.loads(self._decode(encoded_bytes))
            expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
            csrf_token = str(payload["csrf"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SessionError("missing or invalid session") from exc
        if expires_at <= (now or datetime.now(UTC)):
            raise SessionError("session expired")
        return SessionData(expires_at=expires_at, csrf_token=csrf_token)

    @staticmethod
    def _encode(value: bytes) -> bytes:
        return base64.urlsafe_b64encode(value).rstrip(b"=")

    @staticmethod
    def _decode(value: bytes) -> bytes:
        padding = b"=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding)


def hash_password_main() -> None:
    password = sys.stdin.readline().rstrip("\r\n")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters")
    print(PasswordHasher().hash(password))


def reset_password_main() -> None:
    if sys.stdin.isatty():
        password = getpass.getpass("New Gate administrator password: ")
        confirmation = getpass.getpass("Confirm new password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match")
    else:
        password = sys.stdin.readline().rstrip("\r\n")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters")

    async def persist() -> None:
        from gate.config import load_settings
        from gate.database import Database

        settings = load_settings()
        database = Database(settings.database.url)
        try:
            await database.initialize(settings.regions)
            await database.set_security_credentials(
                PasswordHasher().hash(password),
                secrets.token_urlsafe(48),
            )
        finally:
            await database.close()

    asyncio.run(persist())
    print("Gate administrator password has been reset. Restart gate-api.service.")
