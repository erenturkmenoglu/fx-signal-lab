"""The command line interface, run the way an operator runs it (synchronously, from a shell)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator

import pytest

from fxlab.cli import main
from fxlab.clock import SystemClock
from fxlab.config import get_settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.services.provider_check import recent_open_window
from tests.conftest import INSTRUMENTS_YAML, utc_ms
from tests.support.fake_massive import FakeMassive, FakeMassiveServer


class ThreadedFake:
    """The fake provider on its own thread and event loop, so synchronous CLI code can talk to it."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.server: FakeMassiveServer | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def start() -> None:
            self.server = await FakeMassiveServer().start()
            self._ready.set()

        self.loop.create_task(start())
        self.loop.run_forever()

    def __enter__(self) -> ThreadedFake:
        self._thread.start()
        assert self._ready.wait(10), "fake provider did not start"
        return self

    def __exit__(self, *exc: object) -> None:
        assert self.server is not None
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(15)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(10)

    @property
    def fake(self) -> FakeMassive:
        assert self.server is not None
        return self.server.fake

    def publish(self, quotes: list[dict[str, float]]) -> None:
        asyncio.run_coroutine_threadsafe(self.fake.publish(quotes), self.loop).result(10)


@pytest.fixture()
def cli_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    monkeypatch.setenv("INSTRUMENTS_FILE", str(INSTRUMENTS_YAML))
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.chdir("/")  # no stray .env file
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture()
def provider(cli_env: pytest.MonkeyPatch) -> Iterator[ThreadedFake]:
    with ThreadedFake() as fake:
        assert fake.server is not None
        cli_env.setenv("MASSIVE_API_KEY", fake.fake.api_key)
        cli_env.setenv("MASSIVE_WS_URL", fake.server.ws_url)
        cli_env.setenv("MASSIVE_REST_URL", fake.server.rest_url)
        get_settings.cache_clear()
        yield fake


def test_instruments_command(cli_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["instruments"]) == 0
    out = capsys.readouterr().out
    assert "EURUSD   yes" in out and "USDTRY   no" in out and out.count("\n") == 9


def test_verify_provider_without_a_key_fails_clearly(
    cli_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify-provider", "--no-db"]) == 1
    assert "MASSIVE_API_KEY is not set" in capsys.readouterr().out


def test_verify_provider_report(provider: ThreadedFake, capsys: pytest.CaptureFixture[str]) -> None:
    registry = InstrumentRegistry.from_yaml(INSTRUMENTS_YAML)
    _, end = recent_open_window(registry.get("EURUSD"), SystemClock().now_ms())
    provider.publish([{"b": 1.16, "a": 1.16008, "t": end - 60_000 + i * 1000} for i in range(60)])

    assert main(["verify-provider", "--seconds", "1.5", "--no-db", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses["rest_auth_market_status"] == "ok" and statuses["history_quotes"] == "ok"
    assert statuses["websocket"] == "no_data"  # nobody is publishing live quotes in this test
    assert report["official_endpoints"] is False

    assert main(["verify-provider", "--seconds", "1", "--no-db", "--no-ws"]) == 0
    text = capsys.readouterr().out
    assert "[OK    ] rest_auth_market_status" in text and "[SKIP  ] websocket" in text
    assert provider.fake.api_key not in text


@pytest.mark.db
def test_migrate_history_and_gaps_commands(
    provider: ThreadedFake, cli_env: pytest.MonkeyPatch, migrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_env.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    assert main(["migrate"]) == 0
    assert "latest revision" in capsys.readouterr().out

    base = utc_ms(2026, 11, 9, 9, 0, 0)
    provider.publish(
        [{"b": 1.16 + (i % 4) * 0.0001, "a": 1.1601 + (i % 4) * 0.0001, "t": base + i * 6_000} for i in range(50)]
    )
    assert (
        main(
            [
                "history",
                "--symbol",
                "eurusd",
                "--timeframe",
                "1m",
                "--start",
                "2026-11-09T09:00:00Z",
                "--end",
                "2026-11-09T09:05:00Z",
            ]
        )
        == 0
    )
    job = json.loads(capsys.readouterr().out)
    assert (job["status"], job["rows_written"], job["symbol"]) == ("done", 5, "EURUSD")

    assert (
        main(["history", "--symbol", "EURUSD", "--timeframe", "7m", "--start", "2026-11-09", "--end", "2026-11-10"])
        == 2
    )
    assert "unsupported timeframe" in capsys.readouterr().err

    assert main(["gaps", "list"]) == 0
    assert capsys.readouterr().out.startswith("   ID SYMBOL")
    # nothing has been captured live in this database: the comparison says so and fails
    assert main(["reconcile", "--minutes", "5"]) == 1
    assert "no live ticks stored for EURUSD yet" in capsys.readouterr().out
    # scan/fill talk to the running application; without one the command says so instead of hanging
    assert main(["gaps", "scan", "--api", "http://127.0.0.1:9"]) == 1
    assert "could not reach the running application" in capsys.readouterr().err
