"""
Unit tests for the EMARegimeStrategy v2 — Phase 3 (now with v2 enhancements).

v2 adds:
  - Time-of-day filter (skip first 15 min after 9:15 IST + weekends)
  - RSI confirmation (no CE if overbought, no PE if oversold)
  - ATR-based SL sizing
  - Same per-underlying cooldown as v1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.core.events import (
    IndicatorUpdate,
    OptionType,
    RegimeLabel,
    RegimeUpdate,
    Side,
)
from src.strategies.ema_regime import (
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    SL_PCT_MAX,
    SL_PCT_MIN,
    EMARegimeStrategy,
)


# Default test time: 2026-05-22 (Thursday) 04:30 UTC = 10:00 IST — well past the 9:30 filter
DEFAULT_TS = datetime(2026, 5, 22, 4, 30, tzinfo=timezone.utc)


def make_regime(
    label: RegimeLabel,
    *,
    symbol: str = "NIFTY 50",
    score: float = 0.7,
    ts: datetime | None = None,
    resolution: str = "1minute",
    token: int = 256265,
    rationale: dict | None = None,
) -> RegimeUpdate:
    return RegimeUpdate(
        event_id=str(uuid.uuid4()),
        ts=ts or DEFAULT_TS,
        instrument_token=token,
        symbol=symbol,
        resolution=resolution,
        label=label,
        score=score,
        rationale=rationale or {"spread_pct": 0.005},
    )


def make_indicator(
    name: str = "rsi14",
    value: float = 50.0,
    *,
    symbol: str = "NIFTY 50",
    ts: datetime | None = None,
) -> IndicatorUpdate:
    return IndicatorUpdate(
        event_id=str(uuid.uuid4()),
        ts=ts or DEFAULT_TS,
        instrument_token=256265,
        symbol=symbol,
        resolution="1minute",
        bar_ts=ts or DEFAULT_TS,
        name=name,
        value=value,
    )


# ----------------- basic behaviour -----------------


def test_trending_up_produces_ce_buy_signal():
    strat = EMARegimeStrategy()
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None
    assert sig.side == Side.BUY
    assert sig.option_type == OptionType.CE
    assert sig.underlying == "NIFTY 50"
    assert sig.strategy_id == "ema_regime_v2"


def test_trending_down_produces_pe_buy_signal():
    strat = EMARegimeStrategy()
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_DOWN))
    assert sig is not None
    assert sig.option_type == OptionType.PE


@pytest.mark.parametrize(
    "label",
    [RegimeLabel.RANGE, RegimeLabel.HIGH_VOL, RegimeLabel.LOW_VOL, RegimeLabel.UNKNOWN],
)
def test_non_trending_labels_produce_no_signal(label: RegimeLabel):
    strat = EMARegimeStrategy()
    assert strat.on_regime(make_regime(label)) is None


def test_confidence_scales_with_score():
    strat = EMARegimeStrategy()
    weak = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, score=0.3, symbol="A"))
    strong = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, score=0.9, symbol="B"))
    assert weak is not None and strong is not None
    assert strong.confidence > weak.confidence


# ----------------- time-of-day filter -----------------


def test_skip_first_15_min_after_open():
    """Signal at 9:20 IST (= 03:50 UTC) should be skipped (warmup zone)."""
    strat = EMARegimeStrategy()
    early_ts = datetime(2026, 5, 22, 3, 50, tzinfo=timezone.utc)  # 9:20 IST
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, ts=early_ts))
    assert sig is None


def test_signal_after_warmup_window_passes():
    strat = EMARegimeStrategy()
    ok_ts = datetime(2026, 5, 22, 4, 0, tzinfo=timezone.utc)  # 9:30 IST — exactly at the boundary
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, ts=ok_ts))
    assert sig is not None


def test_weekend_blocks_signal():
    strat = EMARegimeStrategy()
    saturday = datetime(2026, 5, 23, 5, 0, tzinfo=timezone.utc)  # Sat 10:30 IST
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, ts=saturday))
    assert sig is None


# ----------------- RSI confirmation -----------------


def test_rsi_overbought_blocks_ce():
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("rsi14", RSI_OVERBOUGHT + 5))
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is None


def test_rsi_oversold_blocks_pe():
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("rsi14", RSI_OVERSOLD - 5))
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_DOWN))
    assert sig is None


def test_rsi_in_middle_allows_both():
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("rsi14", 55.0))
    up = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, symbol="A"))
    strat.on_indicator(make_indicator("rsi14", 45.0, symbol="B"))
    down = strat.on_regime(make_regime(RegimeLabel.TRENDING_DOWN, symbol="B"))
    assert up is not None
    assert down is not None


def test_rsi_overbought_does_not_block_pe():
    """Overbought is bullish exhaustion — PE buys are fine."""
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("rsi14", 85.0))
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_DOWN))
    assert sig is not None


# ----------------- ATR-based SL -----------------


def test_default_sl_when_atr_unknown():
    strat = EMARegimeStrategy()
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None
    assert sig.suggested_sl_pct == 25.0  # default
    assert sig.suggested_target_pct == 50.0  # 2× sl


def test_atr_based_sl_within_bounds():
    """High ATR should produce wider SL, bounded by SL_PCT_MAX."""
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("ema20", 25000.0))
    strat.on_indicator(make_indicator("atr14", 500.0))  # 2% of 25000
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None
    # premium move heuristic: atr_pct * 8 = 2 * 8 = 16% — within [15, 40] band
    assert SL_PCT_MIN <= sig.suggested_sl_pct <= SL_PCT_MAX
    assert sig.suggested_target_pct == sig.suggested_sl_pct * 2


def test_atr_sl_capped_at_max():
    """Extreme ATR shouldn't blow past SL_PCT_MAX."""
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("ema20", 25000.0))
    strat.on_indicator(make_indicator("atr14", 2500.0))  # 10% ATR — pathological
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None
    assert sig.suggested_sl_pct == SL_PCT_MAX


def test_atr_sl_floored_at_min():
    """Very low ATR shouldn't go below SL_PCT_MIN."""
    strat = EMARegimeStrategy()
    strat.on_indicator(make_indicator("ema20", 25000.0))
    strat.on_indicator(make_indicator("atr14", 10.0))  # 0.04% ATR
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None
    assert sig.suggested_sl_pct == SL_PCT_MIN


# ----------------- cooldown -----------------


def test_cooldown_blocks_rapid_resignal_for_same_underlying():
    strat = EMARegimeStrategy(cooldown_seconds=60)
    s1 = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, ts=DEFAULT_TS))
    s2 = strat.on_regime(
        make_regime(RegimeLabel.TRENDING_UP, ts=DEFAULT_TS + timedelta(seconds=30))
    )
    assert s1 is not None
    assert s2 is None


def test_cooldown_expires():
    strat = EMARegimeStrategy(cooldown_seconds=60)
    strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, ts=DEFAULT_TS))
    later = strat.on_regime(
        make_regime(RegimeLabel.TRENDING_UP, ts=DEFAULT_TS + timedelta(seconds=120))
    )
    assert later is not None


def test_cooldown_is_per_underlying():
    strat = EMARegimeStrategy(cooldown_seconds=600)
    s_nifty = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, symbol="NIFTY 50"))
    s_bank = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP, symbol="NIFTY BANK"))
    assert s_nifty is not None
    assert s_bank is not None


# ----------------- on_indicator just tracks state -----------------


def test_on_indicator_returns_none_but_records_state():
    strat = EMARegimeStrategy()
    assert strat.on_indicator(make_indicator("rsi14", 75.0)) is None
    # Verify state was recorded by triggering a CE signal that should be blocked
    blocked = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert blocked is None


# ----------------- multi-timeframe alignment filter -----------------


def _ema_indicator(
    name: str, value: float, *, resolution: str, symbol: str = "NIFTY 50"
) -> IndicatorUpdate:
    return IndicatorUpdate(
        event_id=str(uuid.uuid4()),
        ts=DEFAULT_TS,
        instrument_token=256265,
        symbol=symbol,
        resolution=resolution,
        bar_ts=DEFAULT_TS,
        name=name,
        value=value,
    )


def test_mtf_filter_disabled_when_higher_tfs_unknown():
    """Default behaviour: don't block when 5m/15m EMAs haven't been seen yet."""
    strat = EMARegimeStrategy()
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None  # MTF state is 0 (unknown) -> allowed


def test_mtf_5m_disagreeing_blocks_ce():
    strat = EMARegimeStrategy()
    # 5m EMA20 < EMA50 -> bearish on 5m; CE should be skipped
    strat.on_indicator(_ema_indicator("ema20", 24_900, resolution="5minute"))
    strat.on_indicator(_ema_indicator("ema50", 25_000, resolution="5minute"))
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is None


def test_mtf_15m_disagreeing_blocks_pe():
    strat = EMARegimeStrategy()
    strat.on_indicator(_ema_indicator("ema20", 25_100, resolution="15minute"))
    strat.on_indicator(_ema_indicator("ema50", 25_000, resolution="15minute"))
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_DOWN))
    assert sig is None


def test_mtf_both_agreeing_allows_signal():
    strat = EMARegimeStrategy()
    strat.on_indicator(_ema_indicator("ema20", 25_100, resolution="5minute"))
    strat.on_indicator(_ema_indicator("ema50", 25_000, resolution="5minute"))
    strat.on_indicator(_ema_indicator("ema20", 25_100, resolution="15minute"))
    strat.on_indicator(_ema_indicator("ema50", 25_000, resolution="15minute"))
    sig = strat.on_regime(make_regime(RegimeLabel.TRENDING_UP))
    assert sig is not None
