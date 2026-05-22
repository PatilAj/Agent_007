"""Config loading tests."""
from __future__ import annotations

import os

from src.core.config import Settings, get_settings


def test_settings_load_with_defaults():
    s = get_settings()
    assert s.mode in {"paper", "shadow", "live", "backtest"}
    assert s.risk.slot_capital_inr > 0
    assert s.risk.max_risk_per_trade_pct > 0
    assert s.market.timezone == "Asia/Kolkata"


def test_postgres_dsn_format():
    s = Settings()
    dsn = s.postgres_dsn
    assert dsn.startswith("postgresql+asyncpg://")
    assert "localhost" in dsn or "@" in dsn


def test_redis_url_format():
    s = Settings()
    assert s.redis_url.startswith("redis://")


def test_is_live_requires_both_flags():
    s = Settings(mode="live", enable_live=False)
    assert s.is_live is False
    s2 = Settings(mode="live", enable_live=True)
    assert s2.is_live is True
    s3 = Settings(mode="paper", enable_live=True)
    assert s3.is_live is False


def test_env_overrides_risk_values(monkeypatch):
    monkeypatch.setenv("MAX_RISK_PER_TRADE_PCT", "0.5")
    monkeypatch.setenv("SLOT_CAPITAL_INR", "100000")

    # Have to re-instantiate; cached singleton uses the original env
    s = Settings()
    s.apply_env_overrides_to_risk()
    assert s.risk.max_risk_per_trade_pct == 0.5
    assert s.risk.slot_capital_inr == 100000


def test_risk_config_defaults_are_safe():
    s = get_settings()
    # Sanity: nobody should ever set risk per trade above 5%
    assert s.risk.max_risk_per_trade_pct <= 5
    # Daily loss should be larger than per-trade
    assert s.risk.max_daily_loss_pct > s.risk.max_risk_per_trade_pct
    # Trade caps sane
    assert s.risk.max_trades_per_day <= 20
    assert s.risk.max_concurrent_positions <= 10
