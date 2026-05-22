"""
Unit tests for IndicatorCalculator.

Each indicator is verified against hand-computed reference values from a small,
deterministic close-price sequence. Tests are independent of pandas-ta so the
math is the source of truth, not the library.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.core.events import BarEvent
from src.indicators import IndicatorCalculator


def make_bar(
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int = 0,
    ts_ist_hour: int = 10,
    minute: int = 0,
    token: int = 256265,
    symbol: str = "NIFTY 50",
    resolution: str = "1minute",
) -> BarEvent:
    """Build a BarEvent. High/low default to close±0.5 for simple test cases."""
    if high is None:
        high = close + 0.5
    if low is None:
        low = close - 0.5
    base = datetime(2026, 5, 21, ts_ist_hour - 5, 30 + minute, tzinfo=timezone.utc)  # IST = UTC+5:30
    return BarEvent(
        event_id=str(uuid.uuid4()),
        ts=base,
        bar_ts=base,
        instrument_token=token,
        symbol=symbol,
        resolution=resolution,
        o=Decimal(str(close)),
        h=Decimal(str(high)),
        l=Decimal(str(low)),
        c=Decimal(str(close)),
        v=volume,
    )


# ----------------- EMA -----------------


def test_ema_seeds_to_first_close():
    calc = IndicatorCalculator(1, "X", "1minute", ema_periods=(3,))
    updates = calc.update(make_bar(100.0))
    assert any(u.name == "ema3" and u.value == 100.0 for u in updates)


def test_ema_alpha_smoothing():
    # period=3 → alpha = 2/(3+1) = 0.5
    # bar1: ema = 100
    # bar2: ema = 0.5 * 110 + 0.5 * 100 = 105
    # bar3: ema = 0.5 * 120 + 0.5 * 105 = 112.5
    calc = IndicatorCalculator(1, "X", "1minute", ema_periods=(3,))
    calc.update(make_bar(100.0))
    calc.update(make_bar(110.0))
    updates = calc.update(make_bar(120.0))
    ema = next(u for u in updates if u.name == "ema3")
    assert ema.value == pytest.approx(112.5)


def test_ema_multiple_periods_independent():
    calc = IndicatorCalculator(1, "X", "1minute", ema_periods=(3, 10))
    for px in [100.0, 110.0, 120.0]:
        updates = calc.update(make_bar(px))
    names = {u.name for u in updates}
    assert "ema3" in names and "ema10" in names
    # ema3 reacts faster than ema10 to upward moves
    ema3 = next(u for u in updates if u.name == "ema3").value
    ema10 = next(u for u in updates if u.name == "ema10").value
    assert ema3 > ema10  # both rising, but ema3 closer to 120


# ----------------- RSI -----------------


def test_rsi_not_emitted_until_period_bars_seen():
    calc = IndicatorCalculator(1, "X", "1minute", rsi_period=14)
    # Feed 14 bars (1 seed + 13 diffs); RSI needs 14 diffs = 15 bars total
    for i in range(14):
        updates = calc.update(make_bar(100.0 + i))
    assert not any(u.name == "rsi14" for u in updates)


def test_rsi_all_gains_approaches_100():
    """RSI on a strictly rising series should be 100 (no losses)."""
    calc = IndicatorCalculator(1, "X", "1minute", rsi_period=14)
    rsi = None
    for i in range(20):
        updates = calc.update(make_bar(100.0 + i))
        for u in updates:
            if u.name == "rsi14":
                rsi = u.value
    assert rsi == pytest.approx(100.0)


def test_rsi_all_losses_approaches_zero():
    calc = IndicatorCalculator(1, "X", "1minute", rsi_period=14)
    rsi = None
    for i in range(20):
        updates = calc.update(make_bar(200.0 - i))
        for u in updates:
            if u.name == "rsi14":
                rsi = u.value
    assert rsi == pytest.approx(0.0)


def test_rsi_known_value_alternating():
    """RSI on alternating +1/-1 close changes should be ~50."""
    calc = IndicatorCalculator(1, "X", "1minute", rsi_period=14)
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
    rsi = None
    for c in closes:
        updates = calc.update(make_bar(c))
        for u in updates:
            if u.name == "rsi14":
                rsi = u.value
    assert rsi == pytest.approx(50.0, abs=1.0)


# ----------------- ATR -----------------


def test_atr_not_emitted_until_period_bars_seen():
    calc = IndicatorCalculator(1, "X", "1minute", atr_period=14)
    for i in range(14):
        updates = calc.update(make_bar(100.0 + i, high=101.0 + i, low=99.0 + i))
    assert not any(u.name == "atr14" for u in updates)


def test_atr_constant_range():
    """With constant H-L=2 and no gaps, ATR should stabilize at 2."""
    calc = IndicatorCalculator(1, "X", "1minute", atr_period=14)
    atr = None
    for i in range(20):
        # Each bar: close = 100, high = 101, low = 99 -> TR = 2
        updates = calc.update(make_bar(100.0, high=101.0, low=99.0))
        for u in updates:
            if u.name == "atr14":
                atr = u.value
    assert atr == pytest.approx(2.0)


def test_atr_gap_up_counted_in_tr():
    """If bar gaps above the previous close, TR uses |high - prev_close|."""
    calc = IndicatorCalculator(1, "X", "1minute", atr_period=2)
    calc.update(make_bar(100.0, high=100.5, low=99.5))   # prev_close=100
    calc.update(make_bar(105.0, high=106.0, low=104.0))   # TR = max(2, |106-100|=6, |104-100|=4) = 6
    updates = calc.update(make_bar(105.0, high=106.0, low=104.0))  # TR=2, atr = (6+2)/2 first time then smoothed
    atr = next(u for u in updates if u.name == "atr2").value
    # First fully-populated window: simple avg of [6, 2] = 4
    assert atr == pytest.approx(4.0)


# ----------------- VWAP -----------------


def test_vwap_skipped_when_volume_zero():
    """Index bars have v=0; VWAP should be omitted."""
    calc = IndicatorCalculator(1, "NIFTY 50", "1minute")
    updates = calc.update(make_bar(100.0, volume=0))
    assert not any(u.name == "vwap" for u in updates)


def test_vwap_single_bar_equals_typical_price():
    """Single bar VWAP = (H+L+C)/3."""
    calc = IndicatorCalculator(1, "X", "1minute")
    updates = calc.update(make_bar(100.0, high=102.0, low=98.0, volume=1000))
    vwap = next(u for u in updates if u.name == "vwap").value
    assert vwap == pytest.approx((102.0 + 98.0 + 100.0) / 3.0)


def test_vwap_weighted_average_across_bars():
    """Bar1: typical=100, vol=1; Bar2: typical=200, vol=3. VWAP = (100*1+200*3)/4 = 175."""
    calc = IndicatorCalculator(1, "X", "1minute")
    calc.update(make_bar(100.0, high=100.0, low=100.0, volume=1))
    updates = calc.update(make_bar(200.0, high=200.0, low=200.0, volume=3))
    vwap = next(u for u in updates if u.name == "vwap").value
    assert vwap == pytest.approx(175.0)


def test_vwap_resets_across_ist_trading_day():
    """VWAP must reset when the bar's IST date changes."""
    calc = IndicatorCalculator(1, "X", "1minute")
    # Day 1, 10:00 IST (04:30 UTC)
    day1 = BarEvent(
        event_id=str(uuid.uuid4()),
        ts=datetime(2026, 5, 21, 4, 30, tzinfo=timezone.utc),
        bar_ts=datetime(2026, 5, 21, 4, 30, tzinfo=timezone.utc),
        instrument_token=1, symbol="X", resolution="1minute",
        o=Decimal("100"), h=Decimal("100"), l=Decimal("100"), c=Decimal("100"), v=1000,
    )
    calc.update(day1)
    # Day 2, 10:00 IST (04:30 UTC)
    day2 = BarEvent(
        event_id=str(uuid.uuid4()),
        ts=datetime(2026, 5, 22, 4, 30, tzinfo=timezone.utc),
        bar_ts=datetime(2026, 5, 22, 4, 30, tzinfo=timezone.utc),
        instrument_token=1, symbol="X", resolution="1minute",
        o=Decimal("200"), h=Decimal("200"), l=Decimal("200"), c=Decimal("200"), v=500,
    )
    updates = calc.update(day2)
    vwap = next(u for u in updates if u.name == "vwap").value
    # Reset means VWAP = 200 (only day2's data counts)
    assert vwap == pytest.approx(200.0)


# ----------------- warmup + meta -----------------


def test_warmup_populates_state_without_emitting():
    calc = IndicatorCalculator(1, "X", "1minute", ema_periods=(3,))
    history = [make_bar(100.0), make_bar(110.0), make_bar(120.0)]
    calc.warmup(history)
    # warmup is silent (no return), but next .update should reflect populated state
    updates = calc.update(make_bar(130.0))
    ema = next(u for u in updates if u.name == "ema3")
    # alpha=0.5: state after 100→110→120 is 112.5; +130 → 0.5*130+0.5*112.5 = 121.25
    assert ema.value == pytest.approx(121.25)


def test_bar_count_increments():
    calc = IndicatorCalculator(1, "X", "1minute")
    assert calc.bar_count == 0
    calc.update(make_bar(100.0))
    calc.update(make_bar(101.0))
    assert calc.bar_count == 2


def test_update_attaches_metadata():
    calc = IndicatorCalculator(42, "NIFTY 50", "5minute", ema_periods=(3,))
    bar = make_bar(100.0, token=42, symbol="NIFTY 50", resolution="5minute")
    updates = calc.update(bar)
    ema = next(u for u in updates if u.name == "ema3")
    assert ema.instrument_token == 42
    assert ema.symbol == "NIFTY 50"
    assert ema.resolution == "5minute"
    assert ema.bar_ts == bar.bar_ts
