"""
Unit tests for ORBBreakoutStrategy.

The state machine is the bulk of the logic, so we cover it carefully:

  - Range tracking during 9:15 - 9:30 (WATCHING)
  - Transition at 9:30: quality filters → ARMED or EXPIRED
  - Breakout detection with strong-close requirement
  - One-and-done per underlying per day
  - Cross-day reset
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.core.events import BarEvent, OptionType, Side
from src.strategies.orb_breakout import (
    ENTRY_CUTOFF,
    RANGE_PCT_MAX,
    RANGE_PCT_MIN,
    STRONG_CLOSE_RATIO,
    ORBBreakoutStrategy,
)

# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------


def _bar(
    *,
    ist_hour: int,
    ist_minute: int,
    o: float, h: float, l: float, c: float,
    symbol: str = "NIFTY 50",
    token: int = 256265,
    resolution: str = "1minute",
    day: tuple[int, int, int] = (2026, 5, 26),  # Tuesday
) -> BarEvent:
    # IST minute -> UTC minute (IST = UTC + 5:30)
    ist_total = ist_hour * 60 + ist_minute
    utc_total = ist_total - 330  # 5h30m
    utc_hour, utc_minute = divmod(utc_total, 60)
    ts = datetime(day[0], day[1], day[2], utc_hour, utc_minute, tzinfo=timezone.utc)
    return BarEvent(
        event_id=str(uuid.uuid4()),
        ts=ts,
        bar_ts=ts,
        instrument_token=token,
        symbol=symbol,
        resolution=resolution,
        o=Decimal(str(o)),
        h=Decimal(str(h)),
        l=Decimal(str(l)),
        c=Decimal(str(c)),
        v=0,
    )


def _feed_opening_range(
    strat: ORBBreakoutStrategy,
    *,
    high: float, low: float,
    last_close: float | None = None,
    n_bars: int = 15,
    symbol: str = "NIFTY 50",
) -> None:
    """Feed n bars across 9:15 - 9:29 to build a range."""
    last_close = last_close or (high + low) / 2
    for i in range(n_bars):
        m = 15 + i
        # Make the FIRST bar carry both high and low; intermediates stay inside
        h = high if i == 0 else high - 1
        l = low if i == 0 else low + 1
        strat.on_bar(_bar(ist_hour=9, ist_minute=m, o=l, h=h, l=l, c=last_close, symbol=symbol))


# ------------------------------------------------------------------
# WATCHING phase
# ------------------------------------------------------------------


def test_range_accumulates_during_915_to_929():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=24_900, last_close=25_000)
    st = strat._state["NIFTY 50"]
    assert st.high == 25_100
    assert st.low == 24_900


def test_pre_market_bar_ignored():
    strat = ORBBreakoutStrategy()
    bar = _bar(ist_hour=9, ist_minute=14, o=25_000, h=25_100, l=24_900, c=25_050)
    result = strat.on_bar(bar)
    assert result is None
    # No state recorded for that pre-market bar
    assert strat._state["NIFTY 50"].high == float("-inf")


def test_weekend_bars_ignored():
    strat = ORBBreakoutStrategy()
    # 2026-05-23 = Saturday
    bar = _bar(ist_hour=9, ist_minute=20, o=25_000, h=25_100, l=24_900, c=25_050, day=(2026, 5, 23))
    assert strat.on_bar(bar) is None


# ------------------------------------------------------------------
# Range-quality filters
# ------------------------------------------------------------------


def test_range_too_tight_expires():
    strat = ORBBreakoutStrategy()
    # 0.04% range — well below RANGE_PCT_MIN
    _feed_opening_range(strat, high=25_005, low=25_000, last_close=25_000)
    # Trigger the lock with a 9:30 bar
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_005, h=25_010, l=25_000, c=25_008))
    assert strat._state["NIFTY 50"].state.value == "expired"
    assert "tight" in (strat._state["NIFTY 50"].expired_reason or "")


def test_range_too_wide_expires():
    strat = ORBBreakoutStrategy()
    # 2.0% range — above RANGE_PCT_MAX (1.0%)
    _feed_opening_range(strat, high=25_500, low=25_000, last_close=25_250)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_300, h=25_400, l=25_200, c=25_350))
    assert strat._state["NIFTY 50"].state.value == "expired"
    assert "wide" in (strat._state["NIFTY 50"].expired_reason or "")


def test_range_in_band_arms():
    strat = ORBBreakoutStrategy()
    # 0.5% range — squarely within band
    _feed_opening_range(strat, high=25_125, low=25_000, last_close=25_062)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_062, h=25_080, l=25_050, c=25_070))
    assert strat._state["NIFTY 50"].state.value == "armed"


def test_late_start_expires_day():
    """If we don't see a bar within the first 2 minutes of open, give up on ORB."""
    strat = ORBBreakoutStrategy()
    # First bar is at 9:20 - 5 minutes late
    strat.on_bar(_bar(ist_hour=9, ist_minute=20, o=25_000, h=25_100, l=24_900, c=25_050))
    # Lock attempt at 9:30
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_060, l=25_040, c=25_055))
    assert strat._state["NIFTY 50"].state.value == "expired"
    assert "missed_opening" in (strat._state["NIFTY 50"].expired_reason or "")


# ------------------------------------------------------------------
# Breakout detection
# ------------------------------------------------------------------


def test_strong_ce_breakout_emits_signal():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    # arm
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_080, l=25_040, c=25_060))
    # breakout — close above high, close in upper part of bar
    sig = strat.on_bar(
        _bar(ist_hour=10, ist_minute=0, o=25_080, h=25_140, l=25_070, c=25_130)
    )
    assert sig is not None
    assert sig.side == Side.BUY
    assert sig.option_type == OptionType.CE
    assert sig.strategy_id == "orb_breakout_v1"
    assert "orb_breakout" in sig.rationale[0]


def test_strong_pe_breakout_emits_signal():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    # bearish breakout — close below low, close in lower part of bar
    sig = strat.on_bar(
        _bar(ist_hour=10, ist_minute=0, o=25_020, h=25_030, l=24_960, c=24_965)
    )
    assert sig is not None
    assert sig.option_type == OptionType.PE


def test_weak_wick_above_high_does_not_emit():
    """Bar's wick pokes above range_high but close is in the lower 60% — false break."""
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    # high=25_150 (above range high), but close=25_055 ~ low of the bar
    sig = strat.on_bar(
        _bar(ist_hour=10, ist_minute=0, o=25_050, h=25_150, l=25_050, c=25_055)
    )
    assert sig is None


def test_one_signal_per_day_per_underlying():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    sig1 = strat.on_bar(_bar(ist_hour=10, ist_minute=0, o=25_080, h=25_140, l=25_070, c=25_130))
    # second breakout same day - must be ignored
    sig2 = strat.on_bar(_bar(ist_hour=10, ist_minute=30, o=25_140, h=25_200, l=25_135, c=25_195))
    assert sig1 is not None
    assert sig2 is None


def test_no_signal_past_entry_cutoff():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    # try to break out at 11:30 IST - after the 11:00 cutoff
    sig = strat.on_bar(_bar(ist_hour=11, ist_minute=30, o=25_080, h=25_140, l=25_070, c=25_130))
    assert sig is None
    assert strat._state["NIFTY 50"].state.value == "expired"


def test_bar_inside_range_does_nothing_while_armed():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    # close inside the range
    sig = strat.on_bar(_bar(ist_hour=10, ist_minute=15, o=25_060, h=25_080, l=25_055, c=25_070))
    assert sig is None
    assert strat._state["NIFTY 50"].state.value == "armed"


# ------------------------------------------------------------------
# Cross-day & cross-symbol isolation
# ------------------------------------------------------------------


def test_state_resets_on_new_day():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    # signal day 1
    sig1 = strat.on_bar(_bar(ist_hour=10, ist_minute=0, o=25_080, h=25_140, l=25_070, c=25_130))
    assert sig1 is not None
    # new day - state should reset to fresh WATCHING
    next_day = (2026, 5, 27)
    _feed_opening_range(strat, high=25_300, low=25_200, last_close=25_250)  # ignored - wrong day in helper
    bar_915 = _bar(ist_hour=9, ist_minute=15, o=25_200, h=25_300, l=25_200, c=25_250, day=next_day)
    strat.on_bar(bar_915)
    st = strat._state["NIFTY 50"]
    assert st.day == datetime(*next_day, tzinfo=timezone.utc).date()
    assert st.state.value == "watching"


def test_two_underlyings_have_independent_state():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050, symbol="NIFTY 50")
    _feed_opening_range(strat, high=53_200, low=53_000, last_close=53_100, symbol="NIFTY BANK")
    # lock both
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050, symbol="NIFTY 50"))
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=53_100, h=53_110, l=53_090, c=53_100, symbol="NIFTY BANK"))
    # signal on NIFTY only
    sig = strat.on_bar(_bar(ist_hour=10, ist_minute=0, o=25_080, h=25_140, l=25_070, c=25_130, symbol="NIFTY 50"))
    assert sig is not None
    # NIFTY BANK should still be ARMED, not affected
    assert strat._state["NIFTY BANK"].state.value == "armed"


# ------------------------------------------------------------------
# Signal payload contents
# ------------------------------------------------------------------


def test_signal_carries_orb_levels_in_rationale_and_snapshot():
    strat = ORBBreakoutStrategy()
    _feed_opening_range(strat, high=25_100, low=25_000, last_close=25_050)
    strat.on_bar(_bar(ist_hour=9, ist_minute=30, o=25_050, h=25_055, l=25_045, c=25_050))
    sig = strat.on_bar(_bar(ist_hour=10, ist_minute=0, o=25_080, h=25_140, l=25_070, c=25_130))
    assert sig is not None
    assert sig.indicators_snapshot["orb_high"] == 25_100
    assert sig.indicators_snapshot["orb_low"] == 25_000
    assert 0 < sig.confidence <= 100
    assert sig.suggested_sl_pct > 0
    assert sig.suggested_target_pct > sig.suggested_sl_pct  # 1:2 R:R


# ------------------------------------------------------------------
# Sanity on constants - prevents accidental tuning regressions
# ------------------------------------------------------------------


def test_constants_sane():
    assert 0 < RANGE_PCT_MIN < RANGE_PCT_MAX < 0.05
    assert 0.5 <= STRONG_CLOSE_RATIO <= 0.9
    assert ENTRY_CUTOFF.hour == 11
