"""Data-quality gate.

A single, pure decision function answering: *is the data for this instrument good
enough to generate a trading signal right now?*  Phase 1 only reports the answer
(dashboard, API); the live signal engine of Phase 4 must call the very same function
and stay silent whenever it says no.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fxlab.domain.models import ObsStatus


class GateReason(StrEnum):
    OK = "OK"
    PROVIDER_NOT_CONFIGURED = "PROVIDER NOT CONFIGURED"
    INSTRUMENT_UNSUPPORTED = "INSTRUMENT UNSUPPORTED"
    MARKET_CLOSED = "MARKET CLOSED"
    FEED_DISCONNECTED = "FEED DISCONNECTED"
    NO_DATA = "NO DATA"
    DATA_STALE = "DATA STALE"
    CLOCK_SKEW = "CLOCK SKEW"


@dataclass(frozen=True)
class GateDecision:
    signals_allowed: bool
    reason: GateReason


def evaluate_signal_gate(
    *,
    provider_configured: bool,
    instrument_supported: bool,
    market_open: bool,
    feed_streaming: bool,
    last_status: ObsStatus | None,
    clock_skew_suspected: bool,
) -> GateDecision:
    """Return whether signals may be generated, and the first reason why not."""
    if not provider_configured:
        return GateDecision(False, GateReason.PROVIDER_NOT_CONFIGURED)
    if not instrument_supported:
        return GateDecision(False, GateReason.INSTRUMENT_UNSUPPORTED)
    if not market_open:
        return GateDecision(False, GateReason.MARKET_CLOSED)
    if not feed_streaming:
        return GateDecision(False, GateReason.FEED_DISCONNECTED)
    if last_status is None or last_status is ObsStatus.MISSING:
        return GateDecision(False, GateReason.NO_DATA)
    if last_status is ObsStatus.STALE:
        return GateDecision(False, GateReason.DATA_STALE)
    if clock_skew_suspected:
        return GateDecision(False, GateReason.CLOCK_SKEW)
    return GateDecision(True, GateReason.OK)
