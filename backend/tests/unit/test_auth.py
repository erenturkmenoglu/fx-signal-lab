"""Password hashing, session tokens and the login throttle (pure parts of fxlab.api.auth)."""

from __future__ import annotations

import pytest

from fxlab.api.auth import (
    ITERATIONS,
    Auth,
    LoginThrottle,
    SessionSigner,
    StoredPassword,
    check_new_password,
    hash_password,
)

FAST = 10_000  # iterations for tests; production hashes use ITERATIONS


def test_a_hash_verifies_its_password_and_nothing_else() -> None:
    stored = hash_password("correct horse battery", iterations=FAST)
    assert stored.startswith(f"pbkdf2_sha256:{FAST}:") and "correct" not in stored
    parsed = StoredPassword.parse(stored)
    assert parsed.matches("correct horse battery")
    assert not parsed.matches("correct horse batterY") and not parsed.matches("")
    # salted: the same password never gives the same hash twice
    assert hash_password("correct horse battery", iterations=FAST) != stored
    assert ITERATIONS >= 600_000  # the default is not the test value


def test_the_hash_is_safe_to_put_into_an_env_file() -> None:
    stored = hash_password("p@ss word $HOME #1 'x' \"y\"", iterations=FAST)
    assert all(ch.isalnum() or ch in ":_" for ch in stored)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "plain-password",
        "pbkdf2_sha256:10000:zz:00",
        "bcrypt:10000:00:00",
        "pbkdf2_sha256:5:" + "00" * 16 + ":" + "00" * 32,
    ],
)
def test_anything_else_is_not_accepted_as_a_hash(bad: str) -> None:
    with pytest.raises(ValueError, match="not a password hash"):
        StoredPassword.parse(bad)


def test_new_passwords_must_be_long_and_not_trivial() -> None:
    check_new_password("a long enough passphrase")
    with pytest.raises(ValueError, match="at least 12"):
        check_new_password("short")
    with pytest.raises(ValueError, match="too few different"):
        check_new_password("aaaaaaaaaaaaaaaa")


def test_session_tokens_expire_and_cannot_be_forged() -> None:
    signer = SessionSigner(b"k" * 32)
    token = signer.issue(now_s=1_000, ttl_s=60)
    assert signer.valid(token, now_s=1_059) and not signer.valid(token, now_s=1_060)
    expiry, mac = token.split(".")
    assert not signer.valid(f"{int(expiry) + 3600}.{mac}", now_s=1_000)  # a later expiry needs a new signature
    assert not SessionSigner(b"x" * 32).valid(token, now_s=1_000)  # another key
    for junk in (None, "", "abc", "1.2.3", f"{expiry}.", f".{mac}", f"-5.{mac}"):
        assert not signer.valid(junk, now_s=1_000)
    # hostile input is refused, never an exception: non-ASCII digits, non-ASCII signature, huge numbers
    for hostile in (f"١٢٣٤.{mac}", f"{expiry}.ünïcödé", f"{'9' * 5000}.{mac}", "x" * 10_000):
        assert not signer.valid(hostile, now_s=1_000)


def test_changing_the_password_ends_every_session() -> None:
    old = Auth(hash_password("the first passphrase", iterations=FAST))
    new = Auth(hash_password("the second passphrase", iterations=FAST))
    token = old.issue(now_s=1_000)
    assert old._signer is not None and new._signer is not None
    assert old._signer.valid(token, 1_001) and not new._signer.valid(token, 1_001)


def test_failed_logins_are_throttled_and_the_window_slides() -> None:
    throttle = LoginThrottle(limit=3, window_s=60)
    for second in (0, 10, 20):
        assert throttle.retry_after(second) == 0
        throttle.begin(second)
    assert throttle.retry_after(21) == pytest.approx(39)  # until the oldest failure is a minute old
    assert throttle.retry_after(60.5) == 0  # one slot is free again
    throttle.begin(60.5)
    assert throttle.retry_after(61) == pytest.approx(9)


def test_an_attempt_holds_its_slot_while_it_runs_and_a_right_password_gives_it_back() -> None:
    throttle = LoginThrottle(limit=2, window_s=60)
    throttle.begin(1.0)
    throttle.begin(1.5)  # two attempts in flight: a third has to wait even though nothing has failed yet
    assert throttle.retry_after(2.0) > 0
    throttle.succeeded(1.5)
    assert throttle.retry_after(2.0) == 0
    throttle.succeeded(99.0)  # unknown attempt: ignored


def test_login_is_off_without_a_hash_and_fails_closed_with_a_broken_one() -> None:
    assert Auth(None).enabled is False and Auth("").enabled is False
    broken = Auth("this-is-not-a-hash")
    assert broken.enabled is True and broken.problem and "unusable" in broken.problem


def test_a_public_address_without_a_password_stays_closed() -> None:
    """Served on a domain of its own, the dashboard must not be open because a line is missing from .env."""
    for missing in (None, ""):
        closed = Auth(missing, public=True)
        assert closed.enabled is True and closed.problem and "no password" in closed.problem
        assert "DASHBOARD_DOMAIN" in closed.problem and "make password" in closed.problem
    # with a password a public address is an ordinary protected one
    stored = hash_password("correct horse battery", iterations=FAST)
    usual = Auth(stored, public=True)
    assert usual.enabled is True and usual.problem is None
    # and without a public address nothing changes
    assert Auth(None, public=False).enabled is False
