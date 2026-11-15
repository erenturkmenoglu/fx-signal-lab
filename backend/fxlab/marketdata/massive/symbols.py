"""Massive symbol formats.

Massive uses a different spelling of the same currency pair depending on the API:

==========================  =================  ==========================================
Where                        Example            Source
==========================  =================  ==========================================
WebSocket subscription       ``C.EUR-USD``      docs: websocket/forex/quotes ("{from}-{to}")
WebSocket event field ``p``  ``EUR/USD``        docs sample response (``"p": "USD/CNH"``)
REST aggregates ticker       ``C:EURUSD``       docs: rest/forex/aggregates/custom-bars
REST quotes ticker           ``C:EUR-USD``      docs sample ``next_url`` of /v3/quotes
==========================  =================  ==========================================

The canonical internal symbol is the six-letter form ``EURUSD``.  Parsing accepts any
of the spellings above so a change of separator on the provider side cannot break us.
"""

from __future__ import annotations

from fxlab.domain.instruments import Instrument

QUOTE_CHANNEL = "C"

#: timeframe code -> (multiplier, timespan) of the REST aggregates endpoint
TIMEFRAME_TO_RANGE: dict[str, tuple[int, str]] = {
    "1s": (1, "second"),
    "5s": (5, "second"),
    "15s": (15, "second"),
    "30s": (30, "second"),
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "4h": (4, "hour"),
    "1d": (1, "day"),
}


def ws_pair(inst: Instrument) -> str:
    return inst.provider_symbols.massive_ws_pair or f"{inst.base}-{inst.quote}"


def ws_quote_channel(inst: Instrument) -> str:
    return f"{QUOTE_CHANNEL}.{ws_pair(inst)}"


def rest_aggs_ticker(inst: Instrument) -> str:
    return inst.provider_symbols.massive_rest_aggs or f"C:{inst.base}{inst.quote}"


def rest_quotes_ticker(inst: Instrument) -> str:
    return inst.provider_symbols.massive_rest_quotes or f"C:{inst.base}-{inst.quote}"


def canonical_symbol(pair: object) -> str | None:
    """``'EUR/USD' | 'EUR-USD' | 'C:EURUSD' | 'C:EUR-USD' | 'C.EUR-USD'`` -> ``'EURUSD'``."""
    if not isinstance(pair, str):
        return None
    text = pair.strip().upper()
    if text.startswith(("C:", "C.")):
        text = text[2:]
    text = text.replace("/", "").replace("-", "")
    if len(text) == 6 and text.isalpha():
        return text
    return None
