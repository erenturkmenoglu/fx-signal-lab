"""Live signals with a database: the day in memory, the stream going on, the channels, the routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect

from fxlab.clock import ManualClock
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import Bar, DataSource, Observation, ObsStatus, PriceBasis
from fxlab.pipeline.hub import LiveHub
from fxlab.research.live import LiveError, LiveSettings, Signal, channel, payload, scan
from fxlab.services.live_signals import SETTINGS_KEY, Feed, LiveSignals, warmup_seconds
from fxlab.storage import repo
from fxlab.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, SECOND_MS
from tests.conftest import T0, utc_ms
from tests.integration.test_api import AppServer, mock_settings
from tests.integration.test_auth import PASSWORD, protected

pytestmark = pytest.mark.db

SPREAD = 0.00008
#: quick indicators: signals within seconds
QUICK = {"rsi_period": 5, "rsi_buy_below": 35, "rsi_sell_above": 65, "bollinger_period": 8, "bollinger_width": 1.5}
QUICK_SETTINGS = LiveSettings.from_mapping(QUICK)
#: Thursday 12 November 2026, 14:00 UTC, belongs to the trading day that began at 17:00 New York the day before
DAY_START = utc_ms(2026, 11, 11, 22)


def walk(n: int, seed: int, first: float = 1.16) -> np.ndarray:
    steps = np.random.default_rng(seed).choice([-2, -1, 0, 0, 1, 2], size=n)
    return np.round(first + np.cumsum(steps) * 0.00001, 5)


def second(ts: int, bid: float | None, status: ObsStatus = ObsStatus.FRESH) -> Observation:
    ask = None if bid is None else bid + SPREAD
    return Observation("EURUSD", ts, status, bid, ask, None, None, None, 1 if bid else 0, ts, 0, DataSource.LIVE)


def seconds(first_ts: int, bid: np.ndarray) -> list[Observation]:
    return [second(first_ts + i * SECOND_MS, float(b)) for i, b in enumerate(bid)]


def minute_bar(ts: int, close: float, source: DataSource, bid_close: float | None = None) -> Bar:
    basis = PriceBasis.BID if source is DataSource.MASSIVE_AGG else PriceBasis.MID
    return Bar("EURUSD", "1m", ts, close, close, close, close, source, basis, bid_close=bid_close)


async def instrument(
    engine: AsyncEngine, registry: InstrumentRegistry, recent: list[Observation] | None = None
) -> Feed:
    async with engine.begin() as conn:
        ids = await repo.sync_instruments(conn, registry)
    inst = registry.get("EURUSD")
    return Feed(inst, ids["EURUSD"], inst.make_calendar(), recent or [])


async def store(engine: AsyncEngine, feed: Feed, observations: list[Observation]) -> None:
    async with engine.begin() as conn:
        await repo.upsert_observations(conn, [repo.obs_row(feed.instrument_id, o) for o in observations])


def expected(first_ts: int, bid: np.ndarray, usable: np.ndarray | None = None, since: int = 0) -> list[dict[str, Any]]:
    ts = first_ts + np.arange(len(bid), dtype=np.int64) * SECOND_MS
    ok = np.ones(len(bid), dtype=bool) if usable is None else usable
    return [payload(s, 5) for s in scan(ts, bid, bid + SPREAD, ok, QUICK_SETTINGS) if s.ts_ms >= since]


async def quick(engine: AsyncEngine, now: int) -> tuple[LiveSignals, LiveHub, ManualClock]:
    hub, clock = LiveHub(), ManualClock(now)
    live = LiveSignals(engine, clock, hub)
    await live.save_settings(QUICK)
    return live, hub, clock


def published(queue: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


# ------------------------------------------------------------------------------- the day
async def test_the_day_is_read_from_the_stored_seconds_and_judged_by_the_rule(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    feed = await instrument(engine, registry)
    n = 2400  # forty minutes up to 14:00
    first, bid = T0 - n * SECOND_MS, walk(n, seed=1)
    await store(engine, feed, seconds(first, bid))
    live, _hub, _clock = await quick(engine, T0 + 400)

    view = await live.view(feed)
    assert view["symbol"] == "EURUSD" and view["price_precision"] == 5 and view["pip_size"] == 0.0001
    assert view["day"] == {"start": DAY_START, "end": DAY_START + DAY_MS}
    assert view["settings"] == QUICK_SETTINGS.as_dict() and view["warmup_s"] == warmup_seconds(QUICK_SETTINGS) == 600
    # one value for every second of the day so far; nothing is invented where nothing was stored
    values = view["seconds"]["bid"]
    assert view["seconds"]["start"] == DAY_START and len(values) == (T0 - DAY_START) // SECOND_MS
    assert values[-n:] == bid.tolist() and set(values[:-n]) == {None}
    assert view["newest"] == T0 - SECOND_MS
    # the signals are the rule's on those seconds, value for value
    assert view["signals"] == expected(first, bid) and len(view["signals"]) > 10
    assert json.loads(json.dumps(view)) == view  # plain numbers: it travels as it is
    assert view["history"] is None


async def test_new_seconds_continue_the_rule_and_their_signals_are_published(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    feed = await instrument(engine, registry)
    n, more = 900, 600
    first, bid = T0 - n * SECOND_MS, walk(n + more, seed=2)
    await store(engine, feed, seconds(first, bid[:n]))
    live, hub, _clock = await quick(engine, T0)
    queue = hub.subscribe()
    # nobody has asked for the instrument: its seconds are not wanted yet
    assert live.watching("EURUSD") is False
    live.on_observation(feed, second(T0, float(bid[n])), True)
    before = await live.view(feed)
    assert live.watching("EURUSD") and before["newest"] == T0 - SECOND_MS and published(queue) == []

    for i in range(n, n + more):
        live.on_observation(feed, second(first + i * SECOND_MS, float(bid[i])), True)
    after = await live.view(feed)
    whole = expected(first, bid)
    assert after["signals"] == whole and after["newest"] == first + (n + more - 1) * SECOND_MS
    assert after["seconds"]["bid"][-(n + more) :] == bid.tolist()
    new = [s for s in whole if s["t"] >= T0]
    assert len(new) > 5 and live.raised == len(new)
    assert published(queue) == [{"type": "signal", "symbol": "EURUSD", "signal": s} for s in new]
    # a second that comes twice, or late, changes nothing
    live.on_observation(feed, second(T0, 9.99), True)
    assert (await live.view(feed))["signals"] == whole and published(queue) == []


async def test_stale_seconds_and_a_closed_gate_carry_no_signals(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    feed = await instrument(engine, registry)
    n, more = 600, 900
    first, bid = T0 - n * SECOND_MS, walk(n + more, seed=3)
    await store(engine, feed, seconds(first, bid[:n]))
    live, hub, _clock = await quick(engine, T0)
    await live.view(feed)
    queue = hub.subscribe()
    usable = np.ones(n + more, dtype=bool)
    stale, held = range(n + 100, n + 103), range(n + 400, n + 460)
    for i in range(n, n + more):
        ts = first + i * SECOND_MS
        if i in stale:  # the quote is too old: the price is kept out of the line and out of the rule
            usable[i] = False
            live.on_observation(feed, second(ts, float(bid[i]), ObsStatus.STALE), True)
        elif i in held:  # the price is fine, the gate says no (a feed that has just dropped, a doubtful clock)
            usable[i] = False
            live.on_observation(feed, second(ts, float(bid[i])), False)
        else:
            live.on_observation(feed, second(ts, float(bid[i])), True)
    view = await live.view(feed)
    whole = expected(first, bid, usable)
    assert view["signals"] == whole
    values = view["seconds"]["bid"][-(n + more) :]
    assert [values[i] for i in stale] == [None] * 3  # a hole in the line
    assert [values[i] for i in held] == bid[held.start : held.stop].tolist()  # drawn, but not judged
    quiet = QUICK_SETTINGS.first_signal_at
    times = {(s["t"] - first) // SECOND_MS for s in whole}
    assert not times & set(range(held.start, held.stop + quiet)) and not times & set(
        range(stale.start, stale.stop + quiet)
    )
    assert len(published(queue)) == len([s for s in whole if s["t"] >= T0]) > 5


async def test_a_new_trading_day_starts_with_the_newest_seconds_as_its_warm_up(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    feed = await instrument(engine, registry)
    day_end = DAY_START + DAY_MS  # Thursday 22:00 UTC
    n, more = 1200, 900
    first, bid = day_end - n * SECOND_MS, walk(n + more, seed=4)
    await store(engine, feed, seconds(first, bid[:n]))
    live, hub, clock = await quick(engine, day_end - 200)
    assert (await live.view(feed))["day"]["end"] == day_end
    queue = hub.subscribe()
    for i in range(n, n + more):
        clock.set(first + (i + 1) * SECOND_MS)
        live.on_observation(feed, second(first + i * SECOND_MS, float(bid[i])), True)
    view = await live.view(feed)
    assert view["day"] == {"start": day_end, "end": day_end + DAY_MS}
    assert view["seconds"] == {"start": day_end, "bid": bid[n:].tolist()}
    # the day is judged from its warm-up on: the ten minutes before it, exactly as a fresh read would
    warm = 600
    assert view["signals"] == expected(day_end - warm * SECOND_MS, bid[n - warm :], since=day_end)
    assert [m["signal"] for m in published(queue)] == view["signals"] and len(view["signals"]) > 5
    # and a fresh read of the same seconds from the database agrees
    await store(engine, feed, seconds(day_end, bid[n:]))
    live.invalidate("EURUSD")
    assert live.watching("EURUSD") is False
    again = await live.view(feed)
    assert again["signals"] == view["signals"] and again["seconds"] == view["seconds"]


async def test_seconds_that_are_not_stored_yet_and_seconds_that_arrive_while_reading_are_not_lost(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    n = 900
    first, bid = T0 - n * SECOND_MS, walk(n + 3, seed=5)
    stored, unflushed = seconds(first, bid[: n - 40]), seconds(first + (n - 40) * SECOND_MS, bid[n - 40 : n])
    feed = await instrument(engine, registry, recent=unflushed)  # the last forty are still in the writer's hands
    await store(engine, feed, stored)
    live, hub, _clock = await quick(engine, T0)
    queue = hub.subscribe()

    read = live._load

    async def slow(feed: Feed) -> Any:
        day = await read(feed)
        await asyncio.sleep(0.05)
        return day

    live._load = slow  # type: ignore[method-assign]
    asked = asyncio.create_task(live.view(feed))
    await asyncio.sleep(0.02)
    assert live.watching("EURUSD")
    for i in range(n, n + 3):  # three seconds arrive while the day is being read
        live.on_observation(feed, second(first + i * SECOND_MS, float(bid[i])), True)
    view = await asked
    assert view["seconds"]["bid"][-(n + 3) :] == bid.tolist()
    assert view["signals"] == expected(first, bid)
    assert published(queue) == []  # they belong to the picture the page gets, not to the stream after it


async def test_outside_market_hours_prices_are_drawn_but_not_judged_and_a_closed_market_shows_the_last_day(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    feed = await instrument(engine, registry)
    sunday_open = utc_ms(2026, 11, 15, 22)  # 17:00 New York
    before, after = 300, 900
    first, bid = sunday_open - before * SECOND_MS, walk(before + after, seed=6)
    await store(engine, feed, seconds(first, bid))
    live, _hub, clock = await quick(engine, sunday_open + after * SECOND_MS)
    view = await live.view(feed)
    assert view["day"]["start"] == sunday_open and view["seconds"]["bid"] == bid[before:].tolist()
    # the quotes of the minutes before the open do not warm the indicators up: the run starts at the open
    assert view["signals"] == expected(sunday_open, bid[before:]) and view["signals"]
    assert view["signals"][0]["t"] >= sunday_open + QUICK_SETTINGS.first_signal_at * SECOND_MS

    # on a Saturday the page shows the day that ended with Friday's close
    clock.set(utc_ms(2026, 11, 14, 12))
    live.invalidate("EURUSD")
    friday = await live.view(feed)
    assert friday["day"] == {"start": utc_ms(2026, 11, 12, 22), "end": utc_ms(2026, 11, 13, 22)}
    assert friday["seconds"]["bid"] == [] and friday["signals"] == [] and friday["newest"] is None


# -------------------------------------------------------------------------------- channels
async def test_the_channels_are_fitted_to_the_minutes_of_the_stored_bars_and_of_the_day(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    feed = await instrument(engine, registry)
    # two earlier days of one-minute bars, 08:00 to 20:00 UTC: the provider's (bid) on the first day and on
    # the morning of the second, this system's (mid, with their bid close) on the whole second day
    bars: list[Bar] = []
    line: dict[int, float] = {}
    for day, sources in ((10, ("provider",)), (11, ("provider", "own"))):
        for minute in range(8 * 60, 20 * 60):
            ts = utc_ms(2026, 11, day) + minute * MINUTE_MS
            price = round(1.15 + 0.000004 * len(line) + 0.0002 * np.sin(len(line) / 9), 5)
            if "provider" in sources and (day == 10 or minute < 12 * 60):
                bars.append(minute_bar(ts, price, DataSource.MASSIVE_AGG))
                line[ts] = price
            if "own" in sources:
                # where the provider has the minute, this bar says something else and must lose
                bars.append(minute_bar(ts, price + 0.00044, DataSource.LIVE, bid_close=price + 0.0004))
                line.setdefault(ts, price + 0.0004)
    async with engine.begin() as conn:
        await repo.upsert_bars(conn, [repo.bar_row(feed.instrument_id, b) for b in bars])
    # today: 50 minutes and 20 seconds of seconds up to 14:00:20
    n = 50 * 60 + 20
    first, bid = T0 + 20 * SECOND_MS - n * SECOND_MS, walk(n, seed=7, first=1.156)
    await store(engine, feed, seconds(first, bid))
    live = LiveSignals(engine, ManualClock(T0 + 20 * SECOND_MS), LiveHub())
    await live.save_settings({"channel_short_hours": 0.5, "channel_medium_hours": 6, "channel_long_days": 1})

    for minute in range(50):  # the minutes of today: the bid at the last second of each
        line[first + minute * MINUTE_MS] = float(bid[minute * 60 + 59])
    ts = np.array(sorted(line), dtype=np.int64)
    close = np.array([line[int(t)] for t in ts])

    view = await live.view(feed, history=True)
    assert view["bar_ms"] == MINUTE_MS and view["minutes"] == len(ts) == 2 * 720 + 50
    assert view["history"] == {"t": ts[-1440:].tolist(), "bid": [round(v, 5) for v in close[-1440:].tolist()]}
    found = {c["name"]: c for c in view["channels"]}
    assert list(found) == ["short", "medium", "long"] and [found[k]["asked"] for k in found] == [30, 360, 1440]
    for name, bars_asked in (("short", 30), ("medium", 360), ("long", 1440)):
        assert found[name]["channel"] == channel(ts, close, bars_asked, 2.0, DAY_START)
    assert (
        found["short"]["channel"]["from"] == first + 20 * MINUTE_MS
        and found["short"]["channel"]["to"] == first + 49 * MINUTE_MS
    )
    assert found["long"]["channel"]["bars"] == 1440 and found["long"]["channel"]["day"]["from"] == first
    # a page that draws the minutes leaves the seconds out: everything else is the same answer
    lean = await live.view(feed, history=True, seconds=False)
    assert lean["seconds"] == {"start": view["seconds"]["start"], "bid": []} and len(view["seconds"]["bid"]) > 0
    assert {k: v for k, v in lean.items() if k != "seconds"} == {k: v for k, v in view.items() if k != "seconds"}
    # the lighter question, asked every minute, gives the same channels
    again = await live.channels(feed)
    assert again == {"symbol": "EURUSD", "bar_ms": MINUTE_MS, "minutes": len(ts), "channels": view["channels"]}

    # with fewer bars than a channel asks for it is fitted to those there are; with too few there is none
    await live.save_settings({"channel_long_days": 30, "channel_medium_hours": 240})
    longer = {c["name"]: c for c in (await live.channels(feed))["channels"]}
    assert longer["long"]["asked"] == 43200 and longer["long"]["channel"]["bars"] == len(ts)
    assert longer["medium"]["channel"]["bars"] == len(ts)


async def test_without_any_minutes_there_are_no_channels(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    feed = await instrument(engine, registry)
    await store(engine, feed, seconds(T0, walk(5, seed=8)))  # five seconds of a minute that has not ended
    live = LiveSignals(engine, ManualClock(T0 + 5 * SECOND_MS), LiveHub())
    view = await live.view(feed, history=True)
    assert [c["channel"] for c in view["channels"]] == [None, None, None]
    assert view["minutes"] == 0 and view["history"] == {"t": [], "bid": []} and view["signals"] == []
    # a minute counts once its last second has been seen, whatever is missing before it
    await store(engine, feed, seconds(T0 + 55 * SECOND_MS, walk(5, seed=9)))
    live.invalidate("EURUSD")
    assert (await live.view(feed))["minutes"] == 1


# -------------------------------------------------------------------------------- settings
async def test_settings_are_kept_and_what_is_left_out_stays(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    hub = LiveHub()
    live = LiveSignals(engine, ManualClock(T0), hub)
    assert await live.settings() == LiveSettings()
    form = await live.describe()
    assert form["settings"] == form["defaults"] == LiveSettings().as_dict() and form["rules"] == ["both", "either"]
    queue = hub.subscribe()
    saved = await live.save_settings({"rsi_period": 21, "rule": "either"})
    assert (saved.rsi_period, saved.rule, saved.bollinger_period) == (21, "either", 20)
    assert published(queue) == [{"type": "resync"}]  # every open page asks again
    saved = await live.save_settings({"bar_seconds": 5})
    assert (saved.rsi_period, saved.rule, saved.bar_seconds) == (21, "either", 5)
    for bad, reason in (
        ({"rsi_period": 0}, "rsi_period must be between"),
        ({"speed": 3}, "unknown setting(s): speed"),
        ("fast", "must be an object"),
        ({"rsi_buy_below": 80}, "rsi_buy_below must be lower than rsi_sell_above"),
    ):
        with pytest.raises(LiveError) as refused:
            await live.save_settings(bad)
        assert reason in str(refused.value)
    assert await live.settings() == saved and published(queue) == [{"type": "resync"}]
    # another start of the application finds them
    assert await LiveSignals(engine, ManualClock(T0), LiveHub()).settings() == saved
    # a stored value this version cannot read is not an obstacle: the defaults, and a line in the log
    async with engine.begin() as conn:
        await repo.set_meta(conn, SETTINGS_KEY, {"settings": {"rsi_period": -4}})
    assert await LiveSignals(engine, ManualClock(T0), LiveHub()).settings() == LiveSettings()


async def test_new_settings_work_the_day_out_again(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    feed = await instrument(engine, registry)
    n = 3000
    first, bid = T0 - n * SECOND_MS, walk(n, seed=9)
    await store(engine, feed, seconds(first, bid))
    live, _hub, _clock = await quick(engine, T0)
    quick_signals = (await live.view(feed))["signals"]
    await live.save_settings({"rule": "either"})
    either = (await live.view(feed))["signals"]
    assert len(either) > len(quick_signals) > 0
    await live.save_settings({"bar_seconds": 15, "rule": "both"})
    view = await live.view(feed)
    assert view["settings"]["bar_seconds"] == 15 and view["warmup_s"] == 9000  # 600 bars of 15 seconds
    assert 0 < len(view["signals"]) < len(quick_signals)
    assert all((s["t"] // SECOND_MS + 1) % 15 == 0 for s in view["signals"])
    ts = first + np.arange(n, dtype=np.int64) * SECOND_MS
    settings = LiveSettings.from_mapping({**QUICK, "bar_seconds": 15})
    usable = np.ones(n, dtype=bool)
    assert view["signals"] == [payload(s, 5) for s in scan(ts, bid, bid + SPREAD, usable, settings)]


# ----------------------------------------------------------------------- through the server
async def test_the_page_gets_its_picture_and_then_the_signals_from_the_stream(
    engine: AsyncEngine, migrated_database: str
) -> None:
    """The real application on mock data: what the routes give is what the stream goes on with."""
    async with (
        AppServer(mock_settings(migrated_database, mock_faults=False)) as app,
        httpx.AsyncClient(base_url=app.url) as http,
    ):
        form = (await http.get("/api/live/settings")).json()
        assert form["settings"] == LiveSettings().as_dict() and form["bar_seconds"] == [1, 5, 15, 30, 60]
        refused = await http.put("/api/live/settings", json={"rsi_period": 0})
        assert refused.status_code == 400 and "rsi_period must be between 2 and 1000" in refused.json()["detail"]
        assert (await http.put("/api/live/settings", json=[1, 2])).status_code == 400
        assert (await http.get("/api/live/view?symbol=NOPE")).status_code == 404
        # indicators this quick signal within seconds on the simulator's prices
        eager = {
            "rsi_period": 2,
            "rsi_buy_below": 45,
            "rsi_sell_above": 55,
            "bollinger_period": 2,
            "bollinger_width": 0.5,
            "rule": "either",
        }
        saved = await http.put("/api/live/settings", json=eager)
        assert saved.status_code == 200 and saved.json()["settings"]["rule"] == "either"

        async with connect(f"ws://127.0.0.1:{app.port}/ws/live", proxy=None) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            assert hello["type"] == "hello"
            first = (await http.get("/api/live/view")).json()  # asking is what starts the rule on new seconds
            assert first["symbol"] == "EURUSD" and first["synthetic"] is True
            # in its first second the application has no price yet, and says that no signal is allowed
            assert first["gate"] in ({"allowed": False, "reason": "NO DATA"}, {"allowed": True, "reason": "OK"})
            for _ in range(100):
                if first["gate"]["allowed"]:
                    break
                await asyncio.sleep(0.1)
                first = (await http.get("/api/live/view")).json()
            assert first["gate"] == {"allowed": True, "reason": "OK"}
            assert (
                first["settings"]["rsi_period"] == 2
                and first["day"]["start"] <= first["server_time_ms"] < first["day"]["end"]
            )
            pushed: list[dict[str, Any]] = []
            deadline = asyncio.get_running_loop().time() + 25
            while len(pushed) < 3 and asyncio.get_running_loop().time() < deadline:
                message = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if message["type"] == "signal":
                    assert message["symbol"] == "EURUSD"
                    pushed.append(message["signal"])
            assert len(pushed) == 3, "the simulator's prices raised no signals in 25 seconds"
            later = (await http.get("/api/live/view?history=true")).json()
            # what was pushed is in the picture a page would get now, unchanged
            assert [s for s in later["signals"] if s in pushed] == pushed
            assert later["newest"] >= pushed[-1]["t"] and len(later["seconds"]["bid"]) >= len(first["seconds"]["bid"])
            for signal in pushed:
                at = (signal["t"] - later["seconds"]["start"]) // SECOND_MS
                assert later["seconds"]["bid"][at] == signal["bid"] and signal["ask"] > signal["bid"]
            assert later["history"] is not None and {c["name"] for c in later["channels"]} == {
                "short",
                "medium",
                "long",
            }
            lean = (await http.get("/api/live/view?history=true&seconds=false")).json()
            assert lean["seconds"]["bid"] == [] and lean["history"] is not None and lean["newest"] >= later["newest"]
            assert [c["name"] for c in (await http.get("/api/live/channels")).json()["channels"]] == [
                "short",
                "medium",
                "long",
            ]

            # new settings: every open page is told to ask again (a fresh hello on the stream)
            assert (await http.put("/api/live/settings", json={"rsi_period": 3})).status_code == 200

            async def next_hello() -> dict[str, Any]:
                while True:
                    message = json.loads(await ws.recv())
                    if message["type"] == "hello":
                        return message  # type: ignore[no-any-return]

            assert (await asyncio.wait_for(next_hello(), 10))["type"] == "hello"
        assert (await http.get("/api/live/view")).json()["settings"]["rsi_period"] == 3

    # the settings outlive the application
    async with AppServer(mock_settings(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        assert (await http.get("/api/live/settings")).json()["settings"]["rsi_period"] == 3


async def test_with_a_password_the_live_routes_are_closed(engine: AsyncEngine, migrated_database: str) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url) as http:
        for path in ("/api/live/view", "/api/live/channels", "/api/live/settings"):
            assert (await http.get(path)).status_code == 401, path
        assert (await http.put("/api/live/settings", json={"rsi_period": 9})).status_code == 401
        assert (await http.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200
        assert (await http.get("/api/live/settings")).json()["settings"]["rsi_period"] == 14
        assert (await http.get("/api/live/view")).status_code == 200


def test_a_signal_travels_as_plain_numbers() -> None:
    signal = Signal(T0, "buy", 1.16, 1.16008, 28.5, 1.1602, 1.1604, 1.16, "both")
    assert json.loads(json.dumps(signal.as_dict())) == {
        "t": T0,
        "side": "buy",
        "bid": 1.16,
        "ask": 1.16008,
        "rsi": 28.5,
        "middle": 1.1602,
        "upper": 1.1604,
        "lower": 1.16,
        "by": "both",
    }
    assert json.loads(json.dumps(payload(signal, 5))) == signal.as_dict()
    assert HOUR_MS == 3_600_000  # the unit the channel settings are given in
