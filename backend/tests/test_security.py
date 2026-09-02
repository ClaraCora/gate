from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from gate.api import create_app
from gate.config import DatabaseConfig, SecurityConfig, load_settings
from gate.database import Database
from gate.security import SessionError, SessionManager


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_signed_session_rejects_tampering_and_expiry() -> None:
    manager = SessionManager(
        SecurityConfig(allowed_origins=("http://test",)),
        password_hash=PasswordHasher().hash("correct horse battery staple"),
        session_secret="s" * 32,
    )
    now = datetime.now(UTC)
    token, session = manager.issue(now)

    assert manager.decode(token, now + timedelta(minutes=1)) == session
    with pytest.raises(SessionError):
        manager.decode(f"{token}x", now)
    with pytest.raises(SessionError):
        manager.decode(token, session.expires_at)


@pytest.mark.asyncio
async def test_login_session_and_csrf_protect_mutations(tmp_path: Path) -> None:
    security = SecurityConfig(enabled=True, allowed_origins=("http://test",))
    settings = load_settings().model_copy(
        update={
            "database": DatabaseConfig(url=_database_url(tmp_path / "security.db")),
            "security": security,
        }
    )
    database = Database(settings.database.url)
    app = create_app(
        settings,
        database=database,
        password_hash=PasswordHasher().hash("correct horse battery staple"),
        session_secret="s" * 32,
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            protected = await client.get("/api/v1/regions")
            missing_origin = await client.post(
                "/api/v1/session/login",
                json={"password": "correct horse battery staple"},
            )
            invalid_login = await client.post(
                "/api/v1/session/login",
                json={"password": "incorrect password"},
                headers={"Origin": "http://test", "X-Gate-Request": "webui"},
            )
            login = await client.post(
                "/api/v1/session/login",
                json={"password": "correct horse battery staple"},
                headers={"Origin": "http://test", "X-Gate-Request": "webui"},
            )
            csrf_token = login.json()["csrf_token"]
            missing_csrf = await client.post(
                "/api/v1/discovery/refresh",
                headers={"Origin": "http://test", "X-Gate-Request": "webui"},
            )
            logout = await client.delete(
                "/api/v1/session",
                headers={
                    "Origin": "http://test",
                    "X-Gate-Request": "webui",
                    "X-Gate-CSRF": csrf_token,
                },
            )

    assert protected.status_code == 401
    assert missing_origin.status_code == 403
    assert invalid_login.status_code == 401
    assert login.status_code == 200
    assert missing_csrf.status_code == 403
    assert logout.status_code == 204


@pytest.mark.asyncio
async def test_password_change_persists_and_invalidates_existing_sessions(
    tmp_path: Path,
) -> None:
    password_hash = PasswordHasher().hash("correct horse battery staple")
    database_url = _database_url(tmp_path / "password-change.db")
    security = SecurityConfig(enabled=True, allowed_origins=("http://test",))
    settings = load_settings().model_copy(
        update={
            "database": DatabaseConfig(url=database_url),
            "security": security,
        }
    )
    app = create_app(
        settings,
        password_hash=password_hash,
        session_secret="s" * 32,
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            login = await client.post(
                "/api/v1/session/login",
                json={"password": "correct horse battery staple"},
                headers={"Origin": "http://test", "X-Gate-Request": "webui"},
            )
            old_cookie = client.cookies.get("gate_session")
            csrf_token = login.json()["csrf_token"]
            headers = {
                "Origin": "http://test",
                "X-Gate-Request": "webui",
                "X-Gate-CSRF": csrf_token,
            }
            wrong_current = await client.put(
                "/api/v1/session/password",
                json={
                    "current_password": "wrong password",
                    "new_password": "new secure password 123",
                },
                headers=headers,
            )
            short_password = await client.put(
                "/api/v1/session/password",
                json={
                    "current_password": "correct horse battery staple",
                    "new_password": "too short",
                },
                headers=headers,
            )
            changed = await client.put(
                "/api/v1/session/password",
                json={
                    "current_password": "correct horse battery staple",
                    "new_password": "new secure password 123",
                },
                headers=headers,
            )
            current_session = await client.get("/api/v1/session")
            events = await client.get("/api/v1/events")
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                headers={"Cookie": f"gate_session={old_cookie}"},
            ) as stale_client:
                stale_session = await stale_client.get("/api/v1/regions")

    assert wrong_current.status_code == 400
    assert wrong_current.json()["detail"] == "当前密码不正确"
    assert short_password.status_code == 422
    assert changed.status_code == 200
    assert changed.json()["csrf_token"] != csrf_token
    assert current_session.json()["authenticated"] is True
    assert stale_session.status_code == 401
    assert events.json()[0]["code"] == "SECURITY_PASSWORD_CHANGED"

    restarted = create_app(
        settings,
        database=Database(database_url),
        password_hash=password_hash,
        session_secret="s" * 32,
        reconcile_on_startup=False,
        automation_on_startup=False,
    )
    async with restarted.router.lifespan_context(restarted):
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            old_login = await client.post(
                "/api/v1/session/login",
                json={"password": "correct horse battery staple"},
                headers={"Origin": "http://test", "X-Gate-Request": "webui"},
            )
            new_login = await client.post(
                "/api/v1/session/login",
                json={"password": "new secure password 123"},
                headers={"Origin": "http://test", "X-Gate-Request": "webui"},
            )

    assert old_login.status_code == 401
    assert new_login.status_code == 200


@pytest.mark.asyncio
async def test_disabled_auth_still_requires_same_origin_mutation_header(tmp_path: Path) -> None:
    settings = load_settings().model_copy(
        update={
            "database": DatabaseConfig(url=_database_url(tmp_path / "origin.db")),
            "security": SecurityConfig(enabled=False, allowed_origins=("http://test",)),
        }
    )
    app = create_app(
        settings,
        reconcile_on_startup=False,
        automation_on_startup=False,
    )

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            rejected = await client.post("/api/v1/discovery/refresh")

    assert rejected.status_code == 403
