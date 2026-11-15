"""Provider-independent market-data interfaces.

A provider integration is an *adapter*: it owns the connection, speaks the provider's
protocol and pushes provider-neutral records into a *sink*.  Everything downstream
(validation, sampling, storage, API) only knows these interfaces, so providers can be
added or swapped without touching the rest of the system.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from fxlab.domain.models import RawQuote


class ConnectionState(StrEnum):
    NOT_CONFIGURED = "not_configured"  # no API key: the system never falls back to fake data
    IDLE = "idle"  # nothing to subscribe to
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    SUBSCRIBING = "subscribing"
    STREAMING = "streaming"
    RECONNECT_WAIT = "reconnect_wait"
    AUTH_FAILED = "auth_failed"  # credentials rejected; retried slowly, needs user action
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    """A connection lifecycle event; persisted for the health panel and gap analysis."""

    ts_ms: int
    provider: str
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


#: event kinds emitted by adapters
EVENT_KINDS = (
    "connect_attempt",
    "connected",
    "auth_success",
    "auth_failed",
    "subscribed",
    "subscription_ack",
    "first_data",
    "disconnected",
    "reconnect_scheduled",
    "watchdog_timeout",
    "server_status",
    "error",
    "stopped",
)


@dataclass
class AdapterStatus:
    provider: str
    is_synthetic: bool
    state: ConnectionState
    state_since_ms: int
    endpoint: str | None = None
    subscriptions: list[str] = field(default_factory=list)
    connect_attempts: int = 0
    sessions_established: int = 0
    last_message_ms: int | None = None
    last_data_ms: int | None = None
    last_error: str | None = None
    last_error_ms: int | None = None
    next_retry_ms: int | None = None
    frames_received: int = 0
    quotes_received: int = 0
    parse_errors: int = 0
    malformed_events: int = 0
    unknown_events: int = 0
    sink_errors: int = 0

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["state"] = self.state.value
        return out


class MarketDataSink(Protocol):
    """Receiver of adapter output.  Calls are synchronous and must return quickly."""

    def on_raw_quote(self, raw: RawQuote) -> None: ...

    def on_connection_event(self, event: ConnectionEvent) -> None: ...


class MarketDataAdapter(Protocol):
    name: str
    #: True for adapters that fabricate data (mock).  Propagated to every API payload
    #: so synthetic prices can never be presented as market data.
    is_synthetic: bool

    async def run(self) -> None:
        """Run until :meth:`stop` is called.  Reconnects are handled internally."""

    async def stop(self) -> None: ...

    def status(self) -> AdapterStatus: ...
