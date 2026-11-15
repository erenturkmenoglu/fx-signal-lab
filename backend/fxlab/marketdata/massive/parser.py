"""Parsing of Massive forex WebSocket frames.

Documented wire format (docs: websocket/quickstart, websocket/forex/quotes):

* every frame is a JSON **array** of events; several events may be bundled;
* status events:  ``{"ev":"status","status":"auth_success","message":"authenticated"}``
* quote events:   ``{"ev":"C","p":"USD/CNH","x":"44","a":6.83366,"b":6.83363,"t":1536036818784}``
  with ``a`` = ask, ``b`` = bid, ``t`` = Unix milliseconds, ``x`` = exchange id
  (documented as integer, shown as a string in the sample - both are accepted).

Parsing is deliberately lenient about *types* and strict about *meaning*: values that
cannot be interpreted are passed on as ``None`` so the normaliser rejects and counts
them, instead of this layer guessing or raising.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fxlab.domain.models import RawQuote
from fxlab.marketdata.massive.symbols import QUOTE_CHANNEL, canonical_symbol


class FrameParseError(ValueError):
    """The frame is not valid JSON or not a list/object of events."""


@dataclass(frozen=True, slots=True)
class StatusEvent:
    status: str
    message: str


@dataclass(slots=True)
class ParsedFrame:
    quotes: list[RawQuote] = field(default_factory=list)
    statuses: list[StatusEvent] = field(default_factory=list)
    malformed: int = 0  # events that are recognisably quotes but unusable (e.g. no pair)
    unknown: int = 0  # events of a type this integration does not consume


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def parse_frame(payload: str | bytes, recv_ts_ms: int) -> ParsedFrame:
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise FrameParseError(str(exc)) from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise FrameParseError(f"expected a JSON array of events, got {type(data).__name__}")

    out = ParsedFrame()
    for event in data:
        if not isinstance(event, dict):
            out.malformed += 1
            continue
        ev = event.get("ev")
        if ev == QUOTE_CHANNEL:
            symbol = canonical_symbol(event.get("p"))
            if symbol is None:
                out.malformed += 1
                continue
            out.quotes.append(
                RawQuote(
                    symbol=symbol,
                    bid=_as_float(event.get("b")),
                    ask=_as_float(event.get("a")),
                    source_ts_ms=_as_int(event.get("t")),
                    recv_ts_ms=recv_ts_ms,
                    exchange=_as_int(event.get("x")),
                )
            )
        elif ev == "status":
            out.statuses.append(StatusEvent(str(event.get("status", "")), str(event.get("message", ""))))
        else:
            out.unknown += 1
    return out
