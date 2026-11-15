"""The login, through a real uvicorn server: what is open, what is closed, and how one gets in."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from fxlab.api.auth import SESSION_COOKIE, Auth, StoredPassword, hash_password
from fxlab.cli import main
from fxlab.config import get_settings
from fxlab.storage import repo
from fxlab.storage.guard import META_KEY
from tests.conftest import INSTRUMENTS_YAML
from tests.integration.test_api import AppServer, mock_settings

pytestmark = pytest.mark.db

PASSWORD = "correct horse battery"
HASH = hash_password(PASSWORD, iterations=10_000)
CLOSED = ["/api/status", "/api/instruments", "/api/gaps", "/api/events", "/api/observations?symbol=EURUSD"]


def protected(database_url: str, **overrides: Any) -> Any:
    # plain http in tests: a Secure cookie would never be sent back by the client
    values: dict[str, Any] = dict(dashboard_password_hash=HASH, session_cookie_secure=False)
    values.update(overrides)
    return mock_settings(database_url, **values)


async def test_without_a_password_nothing_changes(engine: AsyncEngine, migrated_database: str) -> None:
    async with AppServer(mock_settings(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        assert (await http.get("/api/auth/session")).json() == {"required": False, "authenticated": True}
        assert (await http.get("/api/status")).status_code == 200
        assert (await http.get("/docs")).status_code == 200
        assert (await http.post("/api/auth/login", json={"password": "anything"})).json()["authenticated"] is True


async def test_with_a_password_everything_but_health_and_login_is_closed(
    engine: AsyncEngine, migrated_database: str
) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        # open: what container health checks and the login page need
        assert (await http.get("/api/health/live")).status_code == 200
        assert (await http.get("/api/health/ready")).json() == {"ready": True, "problems": []}
        assert (await http.get("/api/auth/session")).json() == {"required": True, "authenticated": False}
        # closed: data, actions, the API description and the live stream
        for path in CLOSED:
            response = await http.get(path)
            assert response.status_code == 401 and response.json() == {"detail": "login required"}, path
            assert response.headers["cache-control"] == "no-store"
        assert (await http.post("/api/gaps/scan")).status_code == 401
        assert (await http.post("/api/history/jobs", json={})).status_code == 401
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await http.get(path)).status_code == 404, path
        with pytest.raises(InvalidStatus) as refused:
            async with connect(f"ws://127.0.0.1:{app.port}/ws/live", proxy=None):
                pass
        assert refused.value.response.status_code == 403
        assert PASSWORD not in (await http.get("/api/auth/session")).text and HASH not in (await http.get("/")).text


async def test_login_logout_and_the_session_cookie(engine: AsyncEngine, migrated_database: str) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        wrong = await http.post("/api/auth/login", json={"password": "correct horse batterY"})
        assert wrong.status_code == 401 and wrong.json() == {"detail": "wrong password"}
        assert SESSION_COOKIE not in http.cookies

        ok = await http.post("/api/auth/login", json={"password": PASSWORD})
        assert ok.status_code == 200 and ok.json() == {"required": True, "authenticated": True}
        cookie = ok.headers["set-cookie"]
        assert cookie.startswith(f"{SESSION_COOKIE}=") and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Max-Age=2592000" in cookie and "Path=/" in cookie and PASSWORD not in cookie

        # the cookie opens the API ...
        assert (await http.get("/api/auth/session")).json() == {"required": True, "authenticated": True}
        status = await http.get("/api/status")
        assert status.status_code == 200 and status.json()["provider"]["label"] == "MOCK"
        assert status.headers["x-content-type-options"] == "nosniff" and status.headers["x-frame-options"] == "DENY"
        # ... and the live stream
        token = http.cookies[SESSION_COOKIE]
        async with connect(
            f"ws://127.0.0.1:{app.port}/ws/live",
            additional_headers={"Cookie": f"{SESSION_COOKIE}={token}"},
            proxy=None,
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            assert hello["type"] == "hello" and hello["status"]["provider"]["label"] == "MOCK"

        # a tampered token is worth nothing
        expiry, mac = token.split(".")
        async with httpx.AsyncClient(base_url=app.url, cookies={SESSION_COOKIE: f"{int(expiry) + 1}.{mac}"}) as other:
            assert (await other.get("/api/status")).status_code == 401

        out = await http.post("/api/auth/logout")
        assert out.json() == {"required": True, "authenticated": False}
        assert SESSION_COOKIE not in http.cookies and (await http.get("/api/status")).status_code == 401


async def test_the_session_cookie_is_https_only_by_default(engine: AsyncEngine, migrated_database: str) -> None:
    settings = mock_settings(migrated_database, dashboard_password_hash=HASH)
    async with AppServer(settings) as app, httpx.AsyncClient(base_url=app.url) as http:
        cookie = (await http.post("/api/auth/login", json={"password": PASSWORD})).headers["set-cookie"]
        assert "; Secure" in cookie and "HttpOnly" in cookie


async def test_failed_logins_are_throttled_for_everybody(engine: AsyncEngine, migrated_database: str) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        for _ in range(5):
            assert (await http.post("/api/auth/login", json={"password": "guess guess guess"})).status_code == 401
        locked = await http.post("/api/auth/login", json={"password": "guess guess guess"})
        assert locked.status_code == 429 and 1 <= int(locked.headers["retry-after"]) <= 61
        # the right password has to wait as well: the throttle must not reveal anything
        assert (await http.post("/api/auth/login", json={"password": PASSWORD})).status_code == 429
        # a session that exists is not affected
        token = Auth(HASH).issue()
        assert (await http.get("/api/status", headers={"Authorization": f"Bearer {token}"})).status_code == 200


async def test_a_burst_of_parallel_guesses_cannot_outrun_the_throttle(
    engine: AsyncEngine, migrated_database: str
) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        guesses = [http.post("/api/auth/login", json={"password": f"guess number {n:04d}"}) for n in range(40)]
        codes = [r.status_code for r in await asyncio.gather(*guesses)]
        assert codes.count(401) == 5 and codes.count(429) == 35  # five were hashed, the rest never were


async def test_hostile_session_values_are_refused_not_crashed_on(engine: AsyncEngine, migrated_database: str) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        for value in ("9" * 5000 + ".abc", "1.2.3", "..", "0." + "f" * 64, "%D9%A1%D9%A2.abc", "x" * 4000):
            response = await http.get("/api/status", headers={"Cookie": f"{SESSION_COOKIE}={value}"})
            assert response.status_code == 401, value[:20]
        # raw non-ASCII bytes in the header (here: Arabic-Indic digits), as a hostile client could send them
        response = await http.get("/api/status", headers={"Authorization": "Bearer ١٢.x".encode()})
        assert response.status_code == 401


async def test_the_open_readiness_probe_does_not_leak_error_texts(engine: AsyncEngine, migrated_database: str) -> None:
    async with engine.begin() as conn:  # make start-up fail: the database already belongs to real data
        await repo.set_meta(
            conn, META_KEY, {"provider": "massive", "synthetic": False, "feed_host": "socket.massive.com"}
        )
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        anonymous = await http.get("/api/health/ready")
        assert anonymous.status_code == 503
        assert anonymous.json() == {
            "ready": False,
            "problems": ["not ready; the reasons are in the log and behind the login"],
        }
        token = Auth(HASH).issue()
        owner = await http.get("/api/health/ready", headers={"Authorization": f"Bearer {token}"})
        assert owner.status_code == 503 and "Refusing to mix" in owner.json()["problems"][0]


async def test_security_headers(engine: AsyncEngine, migrated_database: str) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        for path in ("/api/auth/session", "/api/status"):
            headers = (await http.get(path)).headers
            assert "frame-ancestors 'none'" in headers["content-security-policy"]
            assert "script-src 'self'" in headers["content-security-policy"]
            assert headers["strict-transport-security"] == "max-age=31536000"
            assert headers["referrer-policy"] == "no-referrer"
    async with AppServer(mock_settings(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        headers = (await http.get("/api/status")).headers
        assert "content-security-policy" in headers and "strict-transport-security" not in headers


async def test_a_token_from_another_password_or_an_expired_one_is_refused(
    engine: AsyncEngine, migrated_database: str
) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:

        async def status_with(token: str) -> int:
            return (await http.get("/api/status", headers={"Authorization": f"Bearer {token}"})).status_code

        assert await status_with(Auth(HASH).issue()) == 200
        assert await status_with(Auth(HASH).issue(now_s=1_000_000, ttl_s=60)) == 401  # expired long ago
        assert await status_with(Auth(hash_password("another passphrase", iterations=10_000)).issue()) == 401


async def test_a_broken_hash_closes_everything_and_says_so(engine: AsyncEngine, migrated_database: str) -> None:
    settings = protected(migrated_database, dashboard_password_hash="pbkdf2_sha256:oops")
    async with AppServer(settings) as app, httpx.AsyncClient(base_url=app.url) as http:
        ready = await http.get("/api/health/ready")
        assert ready.status_code == 503 and "DASHBOARD_PASSWORD_HASH is unusable" in ready.json()["problems"][0]
        assert (await http.get("/api/status")).status_code == 401
        refused = await http.post("/api/auth/login", json={"password": PASSWORD})
        assert refused.status_code == 401 and "unusable" in refused.json()["detail"]


async def test_the_command_line_works_against_a_protected_application(
    engine: AsyncEngine, migrated_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INSTRUMENTS_FILE", str(INSTRUMENTS_YAML))
    monkeypatch.setenv("DATABASE_URL", migrated_database)
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", HASH)
    monkeypatch.chdir("/")
    get_settings.cache_clear()
    try:
        async with AppServer(protected(migrated_database)) as app:
            # `fxlab gaps scan` runs in the same container as the application and shares its secret
            code = await asyncio.to_thread(main, ["gaps", "scan", "--api", app.url])
            assert code == 0 and "queued" in capsys.readouterr().out
            # without the secret it is refused like anybody else
            monkeypatch.delenv("DASHBOARD_PASSWORD_HASH")
            get_settings.cache_clear()
            assert await asyncio.to_thread(main, ["gaps", "scan", "--api", app.url]) == 1
            assert "login required" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_hash_password_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir("/")
    get_settings.cache_clear()
    monkeypatch.setattr("sys.stdin", io.StringIO("a long enough passphrase\n"))
    assert main(["hash-password", "--stdin"]) == 0
    printed = capsys.readouterr().out.strip()
    assert StoredPassword.parse(printed).matches("a long enough passphrase") and "passphrase" not in printed

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["hash-password", "--stdin"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "at least 12 characters" in captured.err
    get_settings.cache_clear()
