"""Password login for the dashboard and its API.

Off by default: without ``DASHBOARD_PASSWORD_HASH`` the application behaves as before and
must stay on localhost or a private network.  With it, everything under ``/api`` (except
the two health probes and ``/api/auth``) and the live stream need a session.

* The password itself is never stored.  ``.env`` holds a PBKDF2-SHA256 hash
  (``pbkdf2_sha256:<iterations>:<salt hex>:<hash hex>``); ``fxlab hash-password`` makes one.
* A session is a signed, expiring token in an HttpOnly, Secure, SameSite=Strict cookie.  The
  signing key is derived from the stored hash, so changing the password ends every session.
* Failed logins are throttled for everybody at once.  A guesser gets a few thousand tries a
  day against a hash that costs a few hundred milliseconds each: hopeless for any password
  ``fxlab hash-password`` accepts.  The price is that someone hammering the login can keep
  the owner from logging in for a minute at a time; sessions that exist are not affected.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import secrets
import time
from collections import deque
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from fxlab.logging import get_logger

log = get_logger(__name__)

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 12
SESSION_COOKIE = "fxlab_session"
SESSION_TTL_S = 30 * 24 * 3600
#: paths under /api that never need a session: container health checks and the login itself
OPEN_PATHS = ("/api/health/live", "/api/health/ready", "/api/auth/")


def check_new_password(password: str) -> None:
    """Raise ``ValueError`` when a password is too weak to protect a public address."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    if len(set(password)) < 5:
        raise ValueError("the password uses too few different characters")


def hash_password(password: str, *, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{ALGORITHM}:{iterations}:{salt.hex()}:{digest.hex()}"


@dataclass(frozen=True)
class StoredPassword:
    iterations: int
    salt: bytes
    digest: bytes

    @classmethod
    def parse(cls, stored: str) -> StoredPassword:
        parts = stored.strip().split(":")
        if len(parts) != 4 or parts[0] != ALGORITHM:
            raise ValueError("not a password hash made by `fxlab hash-password`")
        try:
            iterations, salt, digest = int(parts[1]), bytes.fromhex(parts[2]), bytes.fromhex(parts[3])
        except ValueError:
            raise ValueError("not a password hash made by `fxlab hash-password`") from None
        if not (10_000 <= iterations <= 10_000_000) or len(salt) < 8 or len(digest) != 32:
            raise ValueError("not a password hash made by `fxlab hash-password`")
        return cls(iterations, salt, digest)

    def matches(self, password: str) -> bool:
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, self.iterations)
        return hmac.compare_digest(candidate, self.digest)


class SessionSigner:
    """Stateless sessions: ``<expiry epoch seconds>.<HMAC-SHA256 of it>``."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def _mac(self, expiry: str) -> str:
        return hmac.new(self._key, expiry.encode("ascii"), hashlib.sha256).hexdigest()

    def issue(self, now_s: float, ttl_s: int = SESSION_TTL_S) -> str:
        expiry = str(int(now_s) + ttl_s)
        return f"{expiry}.{self._mac(expiry)}"

    def valid(self, token: str | None, now_s: float) -> bool:
        # the token comes from the client: nothing in here may raise, whatever it contains
        if not token or len(token) > 128 or token.count(".") != 1:
            return False
        expiry, mac = token.split(".")
        if not (expiry.isascii() and expiry.isdigit() and len(expiry) <= 12):
            return False
        if not hmac.compare_digest(mac.encode("utf-8"), self._mac(expiry).encode("ascii")):
            return False
        return int(expiry) > now_s


class LoginThrottle:
    """At most ``limit`` failed logins per ``window_s`` seconds, counted for everybody together.

    An attempt takes its slot when it starts and gives it back only if the password was right.
    So a burst of parallel guesses cannot outrun the limit, and the hashing they cost is bounded.
    """

    def __init__(self, limit: int = 5, window_s: float = 60.0) -> None:
        self.limit, self.window_s = limit, window_s
        self._attempts: deque[float] = deque()

    def retry_after(self, now_s: float) -> float:
        """0 when a login may be tried now, otherwise the seconds to wait."""
        while self._attempts and self._attempts[0] <= now_s - self.window_s:
            self._attempts.popleft()
        if len(self._attempts) < self.limit:
            return 0.0
        return self._attempts[0] + self.window_s - now_s

    def begin(self, now_s: float) -> None:
        self._attempts.append(now_s)

    def succeeded(self, now_s: float) -> None:
        with contextlib.suppress(ValueError):
            self._attempts.remove(now_s)


class Auth:
    """Everything the application needs to know about logins.  ``enabled`` is False without a hash."""

    def __init__(self, stored_hash: str | None, *, cookie_secure: bool = True) -> None:
        self.enabled = bool(stored_hash)
        self.cookie_secure = cookie_secure
        self.problem: str | None = None
        self._password: StoredPassword | None = None
        self._signer: SessionSigner | None = None
        self.throttle = LoginThrottle()
        if stored_hash:
            try:
                self._password = StoredPassword.parse(stored_hash)
            except ValueError as exc:
                # fail closed: a broken setting must not turn the login off
                self.problem = f"DASHBOARD_PASSWORD_HASH is unusable ({exc}); nobody can log in until it is fixed"
            key = hmac.new(stored_hash.strip().encode("utf-8"), b"fxlab-session-v1", hashlib.sha256).digest()
            self._signer = SessionSigner(key)

    # -- sessions ----------------------------------------------------------------------
    def issue(self, now_s: float | None = None, ttl_s: int = SESSION_TTL_S) -> str:
        assert self._signer is not None, "login is not enabled"
        return self._signer.issue(time.time() if now_s is None else now_s, ttl_s)

    def token_of(self, conn: HTTPConnection) -> str | None:
        token = conn.cookies.get(SESSION_COOKIE)
        if token:
            return token
        header = conn.headers.get("authorization", "")
        return header[7:].strip() if header.lower().startswith("bearer ") else None

    def authenticated(self, conn: HTTPConnection, now_s: float | None = None) -> bool:
        if not self.enabled:
            return True
        if self._signer is None or self.problem:
            return False
        return self._signer.valid(self.token_of(conn), time.time() if now_s is None else now_s)

    # -- login -------------------------------------------------------------------------
    async def check_password(self, password: str) -> bool:
        if self._password is None:
            return False
        # a few hundred milliseconds of hashing: off the event loop, the feed keeps running
        return await asyncio.to_thread(self._password.matches, password)


def auth_of(conn: HTTPConnection) -> Auth:
    auth: Auth = conn.app.state.auth
    return auth


async def require_session(request: Request) -> None:
    """Dependency for the API router: 401 without a session when login is enabled."""
    if request.url.path.startswith(OPEN_PATHS):
        return
    if not auth_of(request).authenticated(request):
        raise HTTPException(status_code=401, detail="login required")


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


router = APIRouter(prefix="/auth")


@router.get("/session")
async def session(request: Request) -> dict[str, bool]:
    """Does this installation need a login, and does this browser have a session?"""
    auth = auth_of(request)
    return {"required": auth.enabled, "authenticated": auth.authenticated(request)}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
    auth = auth_of(request)
    if not auth.enabled:
        return {"required": False, "authenticated": True}
    now = time.time()
    # behind a proxy the peer is the proxy; the forwarded address is a hint, not evidence
    client = {
        "peer": request.client.host if request.client else None,
        "forwarded_for": request.headers.get("x-forwarded-for", "")[:100] or None,
    }
    wait = auth.throttle.retry_after(now)
    if wait > 0:
        log.warning("login_throttled", **client)
        raise HTTPException(
            status_code=429,
            detail="too many failed logins; try again in a minute",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    auth.throttle.begin(now)
    if not await auth.check_password(body.password):
        log.warning("login_failed", **client)
        raise HTTPException(status_code=401, detail=auth.problem or "wrong password")
    auth.throttle.succeeded(now)
    log.info("login_ok", **client)
    response.set_cookie(
        SESSION_COOKIE,
        auth.issue(now),
        max_age=SESSION_TTL_S,
        httponly=True,
        secure=auth.cookie_secure,
        samesite="strict",
        path="/",
    )
    return {"required": True, "authenticated": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    auth = auth_of(request)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=auth.cookie_secure, samesite="strict")
    return {"required": auth.enabled, "authenticated": not auth.enabled}
