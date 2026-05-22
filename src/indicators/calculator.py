"""
Stateful, incremental technical indicators.

One IndicatorCalculator instance is held per (instrument_token, resolution).
Call .update(bar) on each new closed BarEvent; returns a list of IndicatorUpdate
events to publish on the bus.

Indicators implemented:
  - ema20, ema50         : Exponential moving averages
  - rsi14                : Relative Strength Index (Wilder's smoothing)
  - atr14                : Average True Range (Wilder's smoothing)
  - vwap                 : Session-anchored Volume-Weighted Average Price (resets per IST trading day)

Math is incremental (O(1) per bar) — no DataFrame, no rolling-window recomputation.
Verified against known reference values in tests/unit/test_indicator_calculator.py.
"""
from __future__ import annotations

import uuid
from collections import deque
from datetime import date, datetime

import pytz

from src.core.events import BarEvent, IndicatorUpdate

IST = pytz.timezone("Asia/Kolkata")


class IndicatorCalculator:
    """Per-(token, resolution) indicator state machine."""

    def __init__(
        self,
        instrument_token: int,
        symbol: str,
        resolution: str,
        *,
        ema_periods: tuple[int, ...] = (20, 50),
        rsi_period: int = 14,
        atr_period: int = 14,
    ):
        self.instrument_token = instrument_token
        self.symbol = symbol
        self.resolution = resolution
        self.ema_periods = ema_periods
        self.rsi_period = rsi_period
        self.atr_period = atr_period

        # EMA state: current value per period (None until first bar)
        self._emas: dict[int, float | None] = {p: None for p in ema_periods}

        # RSI state (Wilder's smoothing)
        self._prev_close: float | None = None
        self._gains: deque[float] = deque(maxlen=rsi_period)
        self._losses: deque[float] = deque(maxlen=rsi_period)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

        # ATR state (Wilder's smoothing)
        self._trs: deque[float] = deque(maxlen=atr_period)
        self._atr: float | None = None

        # VWAP state (session-anchored to IST trading day)
        self._vwap_pv: float = 0.0
        self._vwap_v: int = 0
        self._vwap_session_date: date | None = None

        # Diagnostics
        self._bar_count: int = 0

    @property
    def bar_count(self) -> int:
        return self._bar_count

    def warmup(self, bars: list[BarEvent]) -> None:
        """Bootstrap state from historical bars without emitting events."""
        for bar in bars:
            self._consume(bar)

    def update(self, bar: BarEvent) -> list[IndicatorUpdate]:
        """Process a newly-closed bar. Returns IndicatorUpdate events to publish."""
        snapshot = self._consume(bar)
        return self._emit_updates(bar, snapshot)

    # ----------------- internals -----------------

    def _consume(self, bar: BarEvent) -> dict[str, float | None]:
        close = float(bar.c)
        high = float(bar.h)
        low = float(bar.l)
        volume = int(bar.v)

        # EMA: seed with first close, then alpha-smooth
        for period in self.ema_periods:
            alpha = 2.0 / (period + 1)
            cur = self._emas[period]
            self._emas[period] = close if cur is None else alpha * close + (1 - alpha) * cur

        rsi_value: float | None = None
        atr_value: float | None = None

        if self._prev_close is not None:
            change = close - self._prev_close
            gain = change if change > 0 else 0.0
            loss = -change if change < 0 else 0.0

            # RSI
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) == self.rsi_period:
                if self._avg_gain is None:
                    # First fully-populated window: simple average
                    self._avg_gain = sum(self._gains) / self.rsi_period
                    self._avg_loss = sum(self._losses) / self.rsi_period
                else:
                    # Wilder's smoothing
                    n = self.rsi_period
                    self._avg_gain = ((n - 1) * self._avg_gain + gain) / n
                    self._avg_loss = ((n - 1) * (self._avg_loss or 0.0) + loss) / n
                if self._avg_loss == 0:
                    rsi_value = 100.0
                else:
                    rs = self._avg_gain / self._avg_loss
                    rsi_value = 100.0 - 100.0 / (1.0 + rs)

            # ATR (True Range needs prev_close so guarded by same branch)
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
            self._trs.append(tr)
            if len(self._trs) == self.atr_period:
                if self._atr is None:
                    self._atr = sum(self._trs) / self.atr_period
                else:
                    n = self.atr_period
                    self._atr = ((n - 1) * self._atr + tr) / n
                atr_value = self._atr

        # VWAP (session-anchored to IST date)
        vwap_value: float | None = None
        bar_date_ist = bar.bar_ts.astimezone(IST).date()
        if self._vwap_session_date != bar_date_ist:
            self._vwap_pv = 0.0
            self._vwap_v = 0
            self._vwap_session_date = bar_date_ist
        if volume > 0:
            typical = (high + low + close) / 3.0
            self._vwap_pv += typical * volume
            self._vwap_v += volume
            vwap_value = self._vwap_pv / self._vwap_v

        self._prev_close = close
        self._bar_count += 1

        return {
            **{f"ema{p}": self._emas[p] for p in self.ema_periods},
            f"rsi{self.rsi_period}": rsi_value,
            f"atr{self.atr_period}": atr_value,
            "vwap": vwap_value,
        }

    def _emit_updates(
        self, bar: BarEvent, snapshot: dict[str, float | None]
    ) -> list[IndicatorUpdate]:
        out: list[IndicatorUpdate] = []
        for name, value in snapshot.items():
            if value is None:
                continue
            out.append(
                IndicatorUpdate(
                    event_id=str(uuid.uuid4()),
                    ts=bar.ts,
                    instrument_token=self.instrument_token,
                    symbol=self.symbol,
                    resolution=self.resolution,
                    bar_ts=bar.bar_ts,
                    name=name,
                    value=value,
                )
            )
        return out
