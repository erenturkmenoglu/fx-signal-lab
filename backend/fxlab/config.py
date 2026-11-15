"""Runtime configuration (environment variables / ``.env``).

Secrets are only ever read from the environment.  The API key is held as a
``SecretStr`` so it cannot leak through ``repr``/logging of the settings object.
Pair-specific settings do not live here but in ``config/instruments.yaml``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OFFICIAL_WS_HOST = "socket.massive.com"
OFFICIAL_REST_HOST = "api.massive.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- market data provider -------------------------------------------------------
    data_provider: Literal["massive", "mock"] = Field(
        "massive", description="'mock' generates clearly labelled synthetic data for development only"
    )
    massive_api_key: SecretStr | None = None
    massive_ws_url: str = f"wss://{OFFICIAL_WS_HOST}/forex"
    massive_rest_url: str = f"https://{OFFICIAL_REST_HOST}"
    massive_rest_max_calls_per_min: int = Field(
        0, ge=0, description="client-side REST throttle; 0 = unlimited (set 5 on the free Basic plan)"
    )
    massive_rest_timeout_s: float = 30.0
    massive_rest_max_retries: int = Field(5, ge=0)

    # --- websocket behaviour --------------------------------------------------------
    ws_connect_timeout_s: float = 15.0
    ws_auth_timeout_s: float = 10.0
    ws_silence_timeout_s: float = Field(
        30.0, description="no data for this long while the market is open => force a reconnect"
    )
    ws_backoff_base_s: float = 1.0
    ws_backoff_max_s: float = 60.0
    ws_stable_after_s: float = Field(30.0, description="streaming this long resets the back-off")
    ws_auth_failed_retry_s: float = 300.0
    ws_max_connections_retry_s: float = 60.0

    # --- storage --------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://fxlab:fxlab@localhost:5432/fxlab"
    persist_raw_ticks: bool = True
    retention_ticks_days: int = Field(30, ge=0, description="0 = keep raw ticks forever")
    retention_obs_days: int = Field(0, ge=0, description="0 = keep 1-second observations forever")
    compress_after_days: int = Field(7, ge=1)
    storage_policies_enabled: bool = Field(
        True, description="apply TimescaleDB compression/retention policies at start-up (false = manage them yourself)"
    )
    writer_flush_interval_s: float = 1.0
    writer_max_buffer_rows: int = Field(500_000, ge=1_000)

    # --- instruments ----------------------------------------------------------------
    instruments_file: Path = Path("config/instruments.yaml")

    # --- gap detection / recovery ---------------------------------------------------
    gap_min_seconds: int = Field(
        5, ge=1, description="shorter holes found by the periodic scan are not recorded (disconnects always are)"
    )
    gap_backfill_delay_s: float = Field(60.0, description="wait before asking the provider for history")
    gap_scan_interval_s: float = 900.0
    gap_scan_lookback_hours: int = 24
    gap_backfill_max_hours: int = Field(72, description="gaps longer than this are not auto-backfilled")
    sampler_max_catchup_s: int = 900

    # --- API / dashboard ------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    static_dir: Path | None = Field(None, description="built dashboard; served at / when set")
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- mock provider (development only) -------------------------------------------
    mock_seed: int | None = None
    mock_quotes_per_second: float = 4.0
    mock_faults: bool = Field(True, description="inject duplicates, outliers and silences into mock data")

    @field_validator("massive_api_key", mode="before")
    @classmethod
    def _blank_key_is_missing(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("static_dir", mode="before")
    @classmethod
    def _blank_path_is_missing(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # --- derived --------------------------------------------------------------------
    @property
    def api_key(self) -> str | None:
        return self.massive_api_key.get_secret_value() if self.massive_api_key else None

    @property
    def provider_configured(self) -> bool:
        return self.data_provider == "mock" or self.api_key is not None

    @property
    def official_endpoints(self) -> bool:
        """True when both Massive URLs point at Massive's production hosts."""
        return (
            urlparse(self.massive_ws_url).hostname == OFFICIAL_WS_HOST
            and urlparse(self.massive_rest_url).hostname == OFFICIAL_REST_HOST
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
