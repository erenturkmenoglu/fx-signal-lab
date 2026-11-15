"""Settings, logging redaction, the live hub and payload builders."""

from __future__ import annotations

import io
import json
import logging

from fxlab.config import Settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import Bar, DataSource, Observation, ObsStatus, PriceBasis
from fxlab.logging import configure_logging, get_logger, register_secret
from fxlab.pipeline.hub import RESYNC_MESSAGE, LiveHub
from fxlab.pipeline.payloads import bar_payload, obs_payload
from tests.conftest import T0


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_blank_api_key_counts_as_missing_and_never_enables_mock() -> None:
    s = settings(massive_api_key="   ")
    assert s.api_key is None and s.data_provider == "massive" and not s.provider_configured
    assert settings(massive_api_key="abc123456").provider_configured
    assert settings(data_provider="mock").provider_configured


def test_api_key_is_not_exposed_by_repr() -> None:
    s = settings(massive_api_key="super-secret-key-123")
    assert "super-secret-key-123" not in repr(s) and "super-secret-key-123" not in str(s.model_dump())
    assert s.api_key == "super-secret-key-123"


def test_official_endpoint_detection() -> None:
    assert settings().official_endpoints
    assert settings().massive_ws_url == "wss://socket.massive.com/forex"
    assert settings().massive_rest_url == "https://api.massive.com"
    assert not settings(massive_ws_url="ws://127.0.0.1:9911/forex").official_endpoints
    assert not settings(massive_rest_url="http://localhost:9911").official_endpoints


def test_registered_secrets_are_redacted_from_logs() -> None:
    register_secret("super-secret-key-123")
    configure_logging("INFO", "json")
    stream = io.StringIO()
    handler = logging.getLogger().handlers[0]
    original = handler.stream  # type: ignore[attr-defined]
    handler.stream = stream  # type: ignore[attr-defined]
    try:
        get_logger("test").info("connecting", url="wss://x/?apiKey=super-secret-key-123", note="super-secret-key-123")
        logging.getLogger("thirdparty").warning("leaked %s", "super-secret-key-123")
    finally:
        handler.stream = original  # type: ignore[attr-defined]
    output = stream.getvalue()
    assert "super-secret-key-123" not in output and output.count("***REDACTED***") == 3
    first = json.loads(output.splitlines()[0])
    assert first["event"] == "connecting" and first["level"] == "info" and "timestamp" in first


def test_live_hub_never_blocks_and_tells_slow_clients_to_resync() -> None:
    hub = LiveHub(max_queue=3)
    fast, slow = hub.subscribe(), hub.subscribe()
    assert hub.subscriber_count == 2
    for i in range(3):
        hub.publish({"type": "obs", "i": i})
        fast.get_nowait()  # the fast client keeps up
    hub.publish({"type": "obs", "i": 3})  # the slow client's queue is full
    assert fast.get_nowait() == {"type": "obs", "i": 3}
    assert slow.qsize() == 1 and slow.get_nowait() == RESYNC_MESSAGE and hub.overflows == 1
    hub.unsubscribe(slow)
    hub.publish({"type": "obs", "i": 4})
    assert slow.empty() and hub.subscriber_count == 1


def test_payloads_carry_provenance_and_instrument_units(registry: InstrumentRegistry) -> None:
    usdjpy = registry.get("USDJPY")
    obs = Observation(
        "USDJPY", T0, ObsStatus.FRESH, 154.300, 154.312, 154.30, 154.32, 154.29, 4, T0 + 900, 100, DataSource.BACKFILL
    )
    payload = obs_payload(obs, usdjpy)
    assert payload["spread_pips"] == 1.2 and payload["mid"] == 154.306  # pips follow the pair's pip size
    assert (payload["status"], payload["source"], payload["n_quotes"]) == ("fresh", "backfill", 4)
    missing = Observation("USDJPY", T0, ObsStatus.MISSING, None, None, None, None, None, 0, None, None)
    assert obs_payload(missing, usdjpy)["mid"] is None and obs_payload(missing, usdjpy)["spread_pips"] is None

    bar = Bar("USDJPY", "1m", T0, 154.3, 154.4, 154.2, 154.35, DataSource.MASSIVE_AGG, PriceBasis.BID, n_quotes=90)
    payload = bar_payload(bar)
    assert (payload["source"], payload["price_basis"], payload["complete"]) == ("massive_agg", "bid", None)
