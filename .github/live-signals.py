"""The live signals on the simulator: settings of one's own, the day's picture, signals on the stream.

Run inside the application's container, which has the WebSocket library:

    python - [address of the application] < .github/live-signals.py

Nothing here is market data: the prices are the simulator's.
"""

import asyncio
import gzip
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from websockets.asyncio.client import connect

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
#: indicators this quick signal within seconds on the simulator's prices
EAGER = {
    "rsi_period": 2,
    "rsi_buy_below": 45,
    "rsi_sell_above": 55,
    "bollinger_period": 2,
    "bollinger_width": 0.5,
    "rule": "either",
}


def call(
    path: str, body: Any = None, *, method: str = "GET", compressed: bool = False
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if compressed:
        headers["Accept-Encoding"] = "gzip"
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as refused:
        return refused.code, {}, refused.read()


def get(path: str) -> Any:
    status, _headers, raw = call(path)
    assert status == 200, (path, status, raw[:200])
    return json.loads(raw)


def put(body: Any) -> Any:
    status, _headers, raw = call("/api/live/settings", body, method="PUT")
    assert status == 200, (status, raw[:200])
    return json.loads(raw)


async def main() -> None:
    form = get("/api/live/settings")
    defaults = form["defaults"]
    assert form["settings"] == defaults, "the settings are not at their defaults at the start"
    assert (defaults["bar_seconds"], defaults["rsi_period"], defaults["bollinger_period"], defaults["rule"]) == (1, 14, 20, "both")
    assert form["bar_seconds"] == [1, 5, 15, 30, 60] and form["rules"] == ["both", "either"], form

    # what cannot be used is refused with the reason, and nothing changes
    status, _headers, raw = call("/api/live/settings", {"rsi_period": 0}, method="PUT")
    assert status == 400 and b"rsi_period must be between 2 and 1000" in raw, (status, raw)
    # bands that no price could reach: four prices are never two standard deviations from their average
    status, _headers, raw = call("/api/live/settings", {"bollinger_period": 4}, method="PUT")
    assert status == 400 and b"the bands would never be met" in raw, (status, raw)
    assert get("/api/live/settings")["settings"] == defaults

    saved = put(EAGER)["settings"]
    assert {k: saved[k] for k in EAGER} == EAGER and saved["channel_long_days"] == defaults["channel_long_days"], saved

    ws_url = BASE.replace("http", "ws", 1) + "/ws/live"
    async with connect(ws_url, proxy=None, max_size=None) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        assert hello["type"] == "hello", hello["type"]

        # the picture of the day; a long answer travels compressed when the browser can take it
        status, headers, raw = call("/api/live/view?symbol=EURUSD", compressed=True)
        assert status == 200 and headers.get("content-encoding") == "gzip", (status, headers.get("content-encoding"))
        view = json.loads(gzip.decompress(raw))
        assert view["symbol"] == "EURUSD" and view["synthetic"] is True and view["settings"] == saved
        assert view["day"]["start"] <= view["server_time_ms"] < view["day"]["end"], view["day"]
        priced = [value for value in view["seconds"]["bid"] if value is not None]
        assert len(priced) >= 5 and view["newest"] is not None, len(priced)
        assert [c["name"] for c in view["channels"]] == ["short", "medium", "long"] and view["history"] is None

        # the rule goes on with every new second: its signals arrive on the stream
        pushed: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + 60
        while len(pushed) < 3 and asyncio.get_running_loop().time() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if message["type"] == "signal":
                assert message["symbol"] == "EURUSD", message
                pushed.append(message["signal"])
        assert len(pushed) == 3, f"the simulator's prices raised {len(pushed)} signals in a minute"

        # what was pushed is in the picture a page would get now, unchanged, at the bid of its second
        later = get("/api/live/view?symbol=EURUSD")
        assert [s for s in later["signals"] if s in pushed] == pushed, "a pushed signal is not in the day's list"
        for signal in pushed:
            at = (signal["t"] - later["seconds"]["start"]) // 1000
            assert later["seconds"]["bid"][at] == signal["bid"] < signal["ask"], signal
            assert signal["side"] in ("buy", "sell") and signal["by"] in ("both", "rsi", "band"), signal
            assert set(signal) == {"t", "side", "bid", "ask", "rsi", "middle", "upper", "lower", "by"}, sorted(signal)

        # the channels on their own, and the minutes behind them without the day's seconds
        channels = get("/api/live/channels?symbol=EURUSD")
        assert [c["name"] for c in channels["channels"]] == ["short", "medium", "long"] and channels["bar_ms"] == 60_000
        lean = get("/api/live/view?symbol=EURUSD&history=true&seconds=false")
        assert lean["seconds"]["bid"] == [] and lean["history"] is not None and lean["newest"] >= later["newest"]
        assert len(lean["history"]["t"]) == len(lean["history"]["bid"]) == min(lean["minutes"], 14_400), lean["minutes"]

        # new settings: every open page is told to ask again
        assert put(defaults)["settings"] == defaults
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if message["type"] == "hello":
                break

    final = get("/api/live/view?symbol=EURUSD")
    assert final["settings"] == defaults and get("/api/live/settings")["settings"] == defaults
    print(
        f"ok: {len(priced)} seconds of the day in the picture ({len(raw)} bytes compressed);",
        f"3 signals pushed on the stream and found in it; {lean['minutes']} one-minute bars behind the channels;",
        f"{len(final['signals'])} signals on those seconds with the default settings",
    )


asyncio.run(main())
