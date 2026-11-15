"""MOCK market-data adapter - SYNTHETIC PRICES, FOR DEVELOPMENT ONLY.

This adapter fabricates a quote stream so the pipeline, storage and dashboard can be
developed and demonstrated without a provider subscription.  It is **never** a
substitute for market data:

* it is only used when ``DATA_PROVIDER=mock`` is set explicitly - a missing API key
  does *not* fall back to it;
* ``is_synthetic = True`` travels with every status payload, the dashboard shows a
  permanent "MOCK DATA" banner, rows are stored with ``source = 'mock'`` and the
  database refuses to mix mock and real data (see ``storage/guard.py``);
* prices start from round placeholder levels (1.1000, 150.00 ...) and follow a
  random walk; they have no relation to any real market.

To exercise the data-quality machinery it can inject faults: duplicates, crossed and
absurdly wide quotes, quiet spells (-> CARRIED) and silences (-> STALE).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

from fxlab.clock import Clock
from fxlab.domain.instruments import Instrument
from fxlab.domain.models import RawQuote
from fxlab.logging import get_logger
from fxlab.marketdata.base import AdapterStatus, ConnectionEvent, ConnectionState, MarketDataSink

log = get_logger(__name__)

#: round, obviously artificial starting levels
_START_LEVELS = {
    "EURUSD": 1.1000,
    "GBPUSD": 1.3000,
    "USDJPY": 150.00,
    "EURGBP": 0.8500,
    "EURJPY": 165.00,
    "USDZAR": 18.0000,
    "USDPLN": 4.0000,
    "USDTRY": 40.0000,
}


@dataclass
class _Sim:
    inst: Instrument
    mid: float
    spread_pips: float
    quiet_until_ms: int = 0
    last_raw: RawQuote | None = None  # the last quote actually emitted
    last_source_ts_ms: int = 0


class MockMarketDataAdapter:
    name = "mock"
    is_synthetic = True

    def __init__(
        self,
        *,
        instruments: Sequence[Instrument],
        sink: MarketDataSink,
        clock: Clock,
        quotes_per_second: float = 4.0,
        seed: int | None = None,
        faults: bool = True,
        annual_vol: float = 0.08,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._rate = max(0.1, quotes_per_second)
        self._rng = random.Random(seed)
        self._faults = faults
        self._vol_per_sqrt_s = annual_vol / math.sqrt(365 * 24 * 3600)
        self._sims = [
            _Sim(inst, _START_LEVELS.get(inst.symbol, 1.0), max(0.3, inst.costs.assumed_spread_pips))
            for inst in instruments
        ]
        self._stop = asyncio.Event()
        self._status = AdapterStatus(
            provider=self.name,
            is_synthetic=True,
            state=ConnectionState.IDLE,
            state_since_ms=clock.now_ms(),
            endpoint="in-process synthetic generator",
            subscriptions=[f"MOCK.{s.inst.symbol}" for s in self._sims],
        )

    def status(self) -> AdapterStatus:
        return self._status

    async def stop(self) -> None:
        self._stop.set()

    def _emit(self, kind: str, **detail: object) -> None:
        self._sink.on_connection_event(
            ConnectionEvent(self._clock.now_ms(), self.name, kind, {"synthetic": True, **detail})
        )

    def _set_state(self, state: ConnectionState) -> None:
        self._status.state = state
        self._status.state_since_ms = self._clock.now_ms()

    async def run(self) -> None:
        if not self._sims:
            await self._stop.wait()
            self._set_state(ConnectionState.STOPPED)
            return
        log.warning("MOCK market-data adapter started: prices are SYNTHETIC, not market data")
        self._status.connect_attempts += 1
        self._status.sessions_established += 1
        self._emit("connected")
        self._emit("subscribed", channels=self._status.subscriptions)
        self._set_state(ConnectionState.STREAMING)
        first = True
        total_rate = self._rate * len(self._sims)
        last_ms = self._clock.now_ms()
        while not self._stop.is_set():
            wait_s = self._rng.expovariate(total_rate)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
            if self._stop.is_set():
                break
            now = self._clock.now_ms()
            dt_s = max(0.001, (now - last_ms) / 1000)
            last_ms = now
            for sim in self._sims:  # every instrument's random walk advances with time
                sim.mid *= math.exp(self._rng.gauss(0, self._vol_per_sqrt_s * math.sqrt(dt_s)))
            sim = self._rng.choice(self._sims)
            if now < sim.quiet_until_ms:
                continue
            for raw in self._make_quotes(sim, now):
                self._status.frames_received += 1
                self._status.quotes_received += 1
                self._status.last_message_ms = now
                self._status.last_data_ms = now
                if first:
                    first = False
                    self._emit("first_data")
                self._sink.on_raw_quote(raw)
        self._set_state(ConnectionState.STOPPED)
        self._emit("stopped")

    def _make_quotes(self, sim: _Sim, now_ms: int) -> list[RawQuote]:
        rng = self._rng
        inst = sim.inst
        half = inst.from_pips(sim.spread_pips * rng.uniform(0.7, 1.5)) / 2
        bid = inst.round_price(sim.mid - half)
        ask = inst.round_price(sim.mid + half)
        # simulated provider-to-us latency; source timestamps never run backwards
        source_ts = max(sim.last_source_ts_ms, now_ms - rng.randint(5, 40))
        raw = RawQuote(inst.symbol, bid, ask, source_ts, now_ms, exchange=0)
        out = [raw]
        if self._faults:
            roll = rng.random()
            if roll < 0.010 and sim.last_raw is not None:  # duplicate delivery of the previous quote
                last = sim.last_raw
                return [RawQuote(inst.symbol, last.bid, last.ask, last.source_ts_ms, now_ms, 0)]
            elif roll < 0.013:  # absurdly wide quote next to the real one
                wide = inst.from_pips(inst.data_quality.max_spread_pips * 20)
                out.append(
                    RawQuote(
                        inst.symbol,
                        inst.round_price(sim.mid - wide),
                        inst.round_price(sim.mid + wide),
                        source_ts,
                        now_ms,
                        0,
                    )
                )
            elif roll < 0.015:  # crossed quote
                out.append(RawQuote(inst.symbol, ask, bid, source_ts, now_ms, 0))
            elif roll < 0.035:  # quiet spell: carried observations
                sim.quiet_until_ms = now_ms + rng.randint(1500, 4000)
            elif roll < 0.037:  # silence longer than the stale threshold
                sim.quiet_until_ms = now_ms + inst.data_quality.stale_after_ms + rng.randint(1000, 4000)
        sim.last_raw = raw
        sim.last_source_ts_ms = source_ts
        return out
