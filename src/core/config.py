"""
Central configuration loader.

Loads layered config: base.yaml + <mode>.yaml + environment variables.
Environment variables override YAML.

Usage:
    from src.core.config import settings
    print(settings.market.timezone)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


# ----------------------------- nested config schemas -----------------------------

class MarketConfig(BaseModel):
    exchange: str = "NSE"
    segment: str = "NFO"
    timezone: str = "Asia/Kolkata"
    open_time: str = "09:15"
    close_time: str = "15:30"
    square_off_time: str = "15:15"
    no_entry_after: str = "14:45"


class UnderlyingConfig(BaseModel):
    symbol: str
    exchange: str
    lot_size: int


class InstrumentsConfig(BaseModel):
    underlyings: list[UnderlyingConfig] = Field(default_factory=list)
    stocks: list[str] = Field(default_factory=list)


class DataConfig(BaseModel):
    candle_resolutions: list[str] = Field(default_factory=lambda: ["1minute", "5minute"])
    historical_warmup_days: int = 5
    tick_buffer_size: int = 1000
    websocket_max_instruments: int = 3000


class RiskConfig(BaseModel):
    slot_capital_inr: float = 50000.0
    max_risk_per_trade_pct: float = 1.0
    max_premium_per_trade_pct: float = 5.0
    max_daily_loss_pct: float = 3.0
    max_weekly_loss_pct: float = 6.0
    max_monthly_dd_pct: float = 8.0
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    cooldown_after_losses_hours: int = 24
    max_concurrent_positions: int = 3
    spread_max_pct: float = 2.0
    premium_min_inr: float = 5.0


class OptionSelectorConfig(BaseModel):
    expiry_preference: str = "current_week"
    switch_to_next_week_days_left: int = 2
    delta_target_min: float = 0.40
    delta_target_max: float = 0.55
    min_oi_index: int = 5000
    min_oi_stock: int = 1000
    iv_percentile_max: int = 90


class ExecutionConfig(BaseModel):
    order_type: str = "LIMIT"
    limit_offset_pct: float = 0.2
    fill_timeout_seconds: int = 2
    max_retry_attempts: int = 3
    reconciler_interval_seconds: int = 60
    send_to_broker: bool = False  # default safe
    simulate_fills: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    log_dir: str = "logs/"


class KillSwitchConfig(BaseModel):
    enabled: bool = True
    flatten_on_trip: bool = False


class NotificationsConfig(BaseModel):
    telegram_enabled: bool = False
    email_enabled: bool = False


# ----------------------------- main settings -----------------------------

class Settings(BaseSettings):
    """
    Top-level settings. Reads from .env + YAML.

    Precedence: env vars > <mode>.yaml > base.yaml > defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- top-level mode ---
    mode: Literal["paper", "shadow", "live", "backtest"] = "paper"
    enable_live: bool = False

    # --- broker credentials (from env only) ---
    kite_api_key: SecretStr = SecretStr("")
    kite_api_secret: SecretStr = SecretStr("")
    kite_user_id: str = ""
    kite_password: SecretStr = SecretStr("")
    kite_totp_secret: SecretStr = SecretStr("")
    kite_pin: SecretStr = SecretStr("")
    kite_redirect_url: str = "http://127.0.0.1:8000/kite/callback"

    # --- database ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "trader"
    postgres_password: SecretStr = SecretStr("changeme_local_only")
    postgres_db: str = "trading_agent"

    # --- redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    # --- notifications ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # --- observability ---
    log_level: str = "INFO"
    sentry_dsn: str = ""

    # --- kill switch ---
    kill_switch: bool = False

    # --- env-overridable risk values (mirror YAML) ---
    slot_capital_inr: float | None = None
    max_risk_per_trade_pct: float | None = None
    max_daily_loss_pct: float | None = None
    max_trades_per_day: int | None = None
    max_consecutive_losses: int | None = None
    max_concurrent_positions: int | None = None

    # --- nested YAML-driven ---
    market: MarketConfig = Field(default_factory=MarketConfig)
    instruments: InstrumentsConfig = Field(default_factory=InstrumentsConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    option_selector: OptionSelectorConfig = Field(default_factory=OptionSelectorConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    kill_switch_cfg: KillSwitchConfig = Field(default_factory=KillSwitchConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # --- computed ---
    @property
    def postgres_dsn(self) -> str:
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pw}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_dsn_sync(self) -> str:
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{pw}@"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def is_live(self) -> bool:
        return self.mode == "live" and self.enable_live

    def apply_env_overrides_to_risk(self) -> None:
        """Pull env-overridable risk values into the RiskConfig (env wins)."""
        if self.slot_capital_inr is not None:
            self.risk.slot_capital_inr = self.slot_capital_inr
        if self.max_risk_per_trade_pct is not None:
            self.risk.max_risk_per_trade_pct = self.max_risk_per_trade_pct
        if self.max_daily_loss_pct is not None:
            self.risk.max_daily_loss_pct = self.max_daily_loss_pct
        if self.max_trades_per_day is not None:
            self.risk.max_trades_per_day = self.max_trades_per_day
        if self.max_consecutive_losses is not None:
            self.risk.max_consecutive_losses = self.max_consecutive_losses
        if self.max_concurrent_positions is not None:
            self.risk.max_concurrent_positions = self.max_concurrent_positions


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build settings: layer YAMLs, then let pydantic apply env vars on top."""
    base = _load_yaml(CONFIG_DIR / "base.yaml")

    # Determine mode from env (cheap parse) so we can layer correctly
    import os
    mode = os.getenv("MODE", "paper").lower()
    overlay_path = CONFIG_DIR / f"{mode}.yaml"
    overlay = _load_yaml(overlay_path)

    merged = _deep_merge(base, overlay)

    # Rename one key to avoid Pydantic clash with top-level `kill_switch`
    if "kill_switch" in merged and isinstance(merged["kill_switch"], dict):
        merged["kill_switch_cfg"] = merged.pop("kill_switch")

    settings = Settings(**merged)
    settings.apply_env_overrides_to_risk()
    return settings


# convenience singleton
settings = get_settings()
