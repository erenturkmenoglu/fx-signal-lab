"""JSON payload builders shared by the REST API and the live stream."""

from __future__ import annotations

from typing import Any

from fxlab.domain.instruments import Instrument
from fxlab.domain.models import Bar, Observation
from fxlab.marketdata.base import ConnectionEvent


def obs_payload(obs: Observation, inst: Instrument) -> dict[str, Any]:
    mid = obs.mid
    spread_pips = None
    if obs.bid is not None and obs.ask is not None:
        spread_pips = round(inst.to_pips(obs.ask - obs.bid), 2)
    return {
        "ts": obs.ts_ms,
        "status": obs.status.value,
        "bid": obs.bid,
        "ask": obs.ask,
        "mid": mid,
        "mid_open": obs.mid_open,
        "mid_high": obs.mid_high,
        "mid_low": obs.mid_low,
        "spread_pips": spread_pips,
        "n_quotes": obs.n_quotes,
        "quote_ts": obs.quote_ts_ms,
        "age_ms": obs.age_ms,
        "source": obs.source.value,
    }


def bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "ts": bar.ts_ms,
        "timeframe": bar.timeframe,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "bid_close": bar.bid_close,
        "ask_close": bar.ask_close,
        "n_quotes": bar.n_quotes,
        "n_fresh": bar.n_fresh,
        "n_valid": bar.n_valid,
        "n_expected": bar.n_expected,
        "n_backfilled": bar.n_backfilled,
        "complete": bar.complete,
        "source": bar.source.value,
        "price_basis": bar.price_basis.value,
    }


def event_payload(event: ConnectionEvent) -> dict[str, Any]:
    return {"ts_ms": event.ts_ms, "provider": event.provider, "kind": event.kind, "detail": event.detail}
