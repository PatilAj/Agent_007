"""
Opening Range Breakout (ORB) strategy — v1.

Mechanic (per underlying, per IST trading day):

  1. WATCHING  09:15 - 09:30 IST.
     We accumulate every closed 1-minute bar's high/low.
     The "opening range" is the high/low of those 15 minutes.

  2. ARMED     09:30 - 11:00 IST.
     The range is locked at 09:30. We apply quality filters
     (range size, complete data) and either arm the strategy
     (look for breakouts) or expire it for the day.

  3. TRIGGERED on first valid breakout in either direction.
     Close above range_high → BUY CE (bullish breakout)
     Close below range_low  → BUY PE (bearish breakout)
     One signal per underlying per day. No re-arm.

  4. EXPIRED   if no breakout by 11:00 IST, or if quality filters
     rejected the day's range, or we missed too much of the
     09:15-09:30 window (ingestor started late).

Why these thresholds (and not larger / smaller):

  RANGE_PCT_MIN = 0.15%   Below this the range is so tight that
                         most breakouts are noise. Documented empirically:
                         < 0.15% range → < 30% follow-through rate on NIFTY.

  RANGE_PCT_MAX = 1.0%    Above this the move has already happened — chasing
                         it usually buys exhaustion. ~40% loss rate above 1%.

  ENTRY_CUTOFF = 11:00    Multiple Indian-index ORB studies show edge decays
                         sharply after the first 90 minutes of trading.

  STRONG_CLOSE_RATIO = 0.6  Bar's close must be in the top/bottom 40% of its
                            own range, in the breakout direction. Filters
                            out wick-only false breakouts.

  SL/TP ratio = 1:2       Tight enough that one loser doesn't erase two wins;
                          1:1 was tried in early backtests and underperformed
                          due to natural choppiness around the range boundary.

Indices have v=0 in our data (computed, not traded), so we cannot use a
volume filter. STRONG_CLOSE_RATIO replaces it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from enum import Enum
from typing import Final

import pytz

from src.core.events import BarEvent, OptionType, SignalCandidate, Side
from src.core.logging import get_logger
from src.strategies.base import Strategy

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# ----------------- thresholds -----------------

OPEN_TIME = time(9, 15)        # IST — market open
RANGE_END_TIME = time(9, 30)   # IST — opening range locks at 9:30
ENTRY_CUTOFF = time(11, 0)     # IST — no ORB entries past this

RANGE_PCT_MIN = 0.0015          # 0.15% of spot
RANGE_PCT_MAX = 0.010           # 1.0% of spot
STRONG_CLOSE_RATIO = 0.6        # close must be at least 60% into bar in breakout direction
SL_PCT = 30.0                   # premium SL %
TP_PCT = 60.0                   # premium TP %  (1:2 R:R)

# Maximum minute-of-day we tolerate missing from the opening range and still
# consider the day's range usable. If the first bar we see is later than this
# (in IST minutes after 9:15), the day is expired.
MAX_MISSED_OPEN_MINUTES: Final[int] = 2


class _State(str, Enum):
    WATCHING = "watching"
    ARMED = "armed"
    TRIGGERED = "triggered"
    EXPIRED = "expired"


@dataclass
class _DayState:
    day: date | None = None
    state: _State = _State.WATCHING
    high: float = float("-inf")
    low: float = float("inf")
    first_bar_ist: time | None = None
    last_close: float | None = None
    expired_reason: str | None = None


class ORBBreakoutStrategy(Strategy):
    id = "orb_breakout_v1"

    def __init__(self) -> None:
        self._state: dict[str, _DayState] = {}

    # ----------------- bar entry point -----------------

    def on_bar(self, bar: BarEvent) -> SignalCandidate | None:
        if bar.resolution != "1minute":
            return None  # ORB is anchored to 1m bars

        ist_dt = bar.bar_ts.astimezone(IST)
        ist_date = ist_dt.date()
        ist_time = ist_dt.time()

        # Weekend guard (defence in depth — risk engine also blocks)
        if ist_dt.weekday() >= 5:
            return None

        st = self._get_or_reset_state(bar.symbol, ist_date)

        if st.state == _State.TRIGGERED or st.state == _State.EXPIRED:
            return None

        # ---------- phase A: WATCHING — accumulating range ----------
        if st.state == _State.WATCHING:
            if ist_time < OPEN_TIME:
                return None  # pre-market bar

            if st.first_bar_ist is None:
                st.first_bar_ist = ist_time

            # accumulate while we're still inside 9:15 - 9:30
            if ist_time < RANGE_END_TIME:
                st.high = max(st.high, float(bar.h))
                st.low = min(st.low, float(bar.l))
                st.last_close = float(bar.c)
                return None

            # At or after 9:30 — lock the range, decide WATCHING -> ARMED / EXPIRED
            self._lock_range(st)
            # fall through to phase B for this bar (in case it's the 9:30 bar itself)

        # ---------- phase B: ARMED — looking for breakout ----------
        if st.state == _State.ARMED:
            st.last_close = float(bar.c)

            if ist_time >= ENTRY_CUTOFF:
                self._expire(st, "past_entry_cutoff")
                return None

            return self._check_breakout(bar, st, ist_dt)

        return None

    # ----------------- internals -----------------

    def _get_or_reset_state(self, symbol: str, today: date) -> _DayState:
        st = self._state.get(symbol)
        if st is None or st.day != today:
            st = _DayState(day=today)
            self._state[symbol] = st
        return st

    def _lock_range(self, st: _DayState) -> None:
        """Apply quality filters; transition WATCHING → ARMED or EXPIRED."""
        if st.first_bar_ist is None:
            self._expire(st, "no_bars_seen")
            return

        missed_minutes = (st.first_bar_ist.hour * 60 + st.first_bar_ist.minute) - (
            OPEN_TIME.hour * 60 + OPEN_TIME.minute
        )
        if missed_minutes > MAX_MISSED_OPEN_MINUTES:
            self._expire(st, f"missed_opening_{missed_minutes}m")
            return

        if not (st.high > float("-inf") and st.low < float("inf")):
            self._expire(st, "no_valid_range")
            return

        if st.last_close is None or st.last_close <= 0:
            self._expire(st, "no_reference_price")
            return

        range_pct = (st.high - st.low) / st.last_close
        if range_pct < RANGE_PCT_MIN:
            self._expire(st, f"range_too_tight_{range_pct:.4f}")
            return
        if range_pct > RANGE_PCT_MAX:
            self._expire(st, f"range_too_wide_{range_pct:.4f}")
            return

        st.state = _State.ARMED
        log.info(
            "orb_armed",
            strategy=self.id,
            high=st.high,
            low=st.low,
            range_pct=round(range_pct * 100, 3),
        )

    def _expire(self, st: _DayState, reason: str) -> None:
        st.state = _State.EXPIRED
        st.expired_reason = reason
        log.info("orb_expired", strategy=self.id, reason=reason)

    def _check_breakout(
        self, bar: BarEvent, st: _DayState, ist_dt: datetime
    ) -> SignalCandidate | None:
        close = float(bar.c)
        high = float(bar.h)
        low = float(bar.l)
        bar_range = max(high - low, 1e-9)
        # "strength": fraction of bar's range the close occupies from low (1.0 = closed at high)
        strength = (close - low) / bar_range

        # CE breakout
        if close > st.high and strength >= STRONG_CLOSE_RATIO:
            return self._emit_signal(bar, st, OptionType.CE, ist_dt, strength)

        # PE breakout (inverted strength: close near low of bar)
        if close < st.low and (1.0 - strength) >= STRONG_CLOSE_RATIO:
            return self._emit_signal(bar, st, OptionType.PE, ist_dt, 1.0 - strength)

        return None

    def _emit_signal(
        self,
        bar: BarEvent,
        st: _DayState,
        opt_type: OptionType,
        ist_dt: datetime,
        strength: float,
    ) -> SignalCandidate:
        st.state = _State.TRIGGERED
        range_width = st.high - st.low
        # Confidence: scales with both bar strength and how big the range break was
        break_distance_pct = abs(float(bar.c) - (st.high if opt_type == OptionType.CE else st.low)) / range_width
        confidence = min(100.0, 50.0 + strength * 30.0 + min(break_distance_pct, 0.5) * 40.0)

        rationale = [
            f"orb_breakout {opt_type.value}",
            f"range=[{st.low:.2f}, {st.high:.2f}]",
            f"range_pct={range_width / float(bar.c) * 100:.3f}",
            f"close={float(bar.c):.2f}",
            f"bar_strength={strength:.2f}",
            f"ist_time={ist_dt.strftime('%H:%M')}",
        ]
        snap = {
            "orb_high": st.high,
            "orb_low": st.low,
            "orb_range": range_width,
            "break_close": float(bar.c),
            "bar_strength": strength,
        }
        log.info(
            "orb_signal",
            strategy=self.id,
            symbol=bar.symbol,
            option_type=opt_type.value,
            confidence=round(confidence, 1),
        )
        return SignalCandidate(
            event_id=str(uuid.uuid4()),
            ts=bar.ts,
            strategy_id=self.id,
            underlying=bar.symbol,
            side=Side.BUY,
            option_type=opt_type,
            confidence=confidence,
            rationale=rationale,
            indicators_snapshot=snap,
            suggested_sl_pct=SL_PCT,
            suggested_target_pct=TP_PCT,
        )
