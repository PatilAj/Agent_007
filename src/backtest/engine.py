"""
Replay engine.

Drives the SAME components the live system uses — IndicatorCalculator,
RegimeDetector, and the Strategy instances — synchronously over a
chronological stream of historical bars. This guarantees backtest signals
match live behavior (no strategy reimplementation, no drift).

Wiring mirrors the live loops exactly:
  - 1m bars        → strategy.on_bar         (ORB)
  - every bar      → IndicatorCalculator.update → IndicatorUpdate(s)
  - each update    → strategy.on_indicator    (ema_regime MTF/RSI/ATR state)
                   → RegimeDetector → classify → on label change → on_regime

Signals are paired with the spot price (latest 1-minute close) at emit time,
which the trade simulator needs to price the option.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from src.core.events import BarEvent, RegimeUpdate, SignalCandidate
from src.indicators import IndicatorCalculator
from src.regime.detector import RegimeDetector
from src.strategies.base import Strategy

RES_MINUTES: dict[str, int] = {"1minute": 1, "3minute": 3, "5minute": 5, "15minute": 15}


@dataclass
class EmittedSignal:
    signal: SignalCandidate
    spot: float  # underlying spot (latest 1m close) at emit time


def bar_from_candle(
    row: dict,
    *,
    token: int,
    symbol: str,
    resolution: str,
) -> BarEvent:
    """Build a BarEvent from a stored candle row.

    Stored `ts` is the bar START (Kite convention). The live aggregator tags
    bars with their CLOSE time, and strategies interpret bar_ts as close — so
    we normalize START → CLOSE here to match live time-of-day logic.
    """
    start = row["ts"]
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    close = start + _td(resolution)
    return BarEvent(
        event_id=str(uuid.uuid4()),
        ts=close,
        instrument_token=token,
        symbol=symbol,
        resolution=resolution,  # type: ignore[arg-type]
        bar_ts=close,
        o=Decimal(str(row["o"])),
        h=Decimal(str(row["h"])),
        l=Decimal(str(row["l"])),
        c=Decimal(str(row["c"])),
        v=int(row["v"] or 0),
        oi=int(row["oi"]) if row.get("oi") is not None else None,
    )


def _td(resolution: str):
    from datetime import timedelta

    return timedelta(minutes=RES_MINUTES[resolution])


class ReplayEngine:
    """Synchronous re-implementation of the live loop wiring for backtests."""

    def __init__(self, strategies: list[Strategy]):
        self.strategies = strategies
        self._calcs: dict[tuple[int, str], IndicatorCalculator] = {}
        self._dets: dict[tuple[int, str], RegimeDetector] = {}
        self._last_spot: float = 0.0

    def _calc(self, token: int, symbol: str, res: str) -> IndicatorCalculator:
        key = (token, res)
        c = self._calcs.get(key)
        if c is None:
            c = IndicatorCalculator(token, symbol, res)
            self._calcs[key] = c
        return c

    def _det(self, token: int, symbol: str, res: str) -> RegimeDetector:
        key = (token, res)
        d = self._dets.get(key)
        if d is None:
            d = RegimeDetector(token, symbol, res)
            self._dets[key] = d
        return d

    def warm_bar(self, bar: BarEvent) -> None:
        """Warm indicator state only — no detectors/strategies.

        Mirrors live startup: calculators are warmed from history while
        strategies + regime detectors start fresh at go-live.
        """
        if bar.resolution == "1minute":
            self._last_spot = float(bar.c)
        self._calc(bar.instrument_token, bar.symbol, bar.resolution).update(bar)

    def feed_bar(self, bar: BarEvent) -> list[EmittedSignal]:
        """Process one bar through the full pipeline; return any signals."""
        out: list[EmittedSignal] = []
        if bar.resolution == "1minute":
            self._last_spot = float(bar.c)

        # ORB consumes raw 1m bars
        for strat in self.strategies:
            sig = strat.on_bar(bar)
            if sig is not None:
                out.append(EmittedSignal(sig, self._last_spot))

        calc = self._calc(bar.instrument_token, bar.symbol, bar.resolution)
        det = self._det(bar.instrument_token, bar.symbol, bar.resolution)

        for upd in calc.update(bar):
            # 1) strategies update their own indicator-derived state first
            for strat in self.strategies:
                sig = strat.on_indicator(upd)
                if sig is not None:
                    out.append(EmittedSignal(sig, self._last_spot))

            # 2) detector consumes the update, then classify + maybe transition
            det.update_indicator(upd.name, upd.value)
            if upd.name == "ema20" and upd.value is not None:
                det.update_close(upd.value)

            label, score, rationale = det.classify_current()
            if det.should_emit(label):
                regime_ev = RegimeUpdate(
                    event_id=str(uuid.uuid4()),
                    ts=upd.ts,
                    instrument_token=bar.instrument_token,
                    symbol=bar.symbol,
                    resolution=bar.resolution,
                    label=label,
                    score=score,
                    rationale=rationale,
                )
                for strat in self.strategies:
                    sig = strat.on_regime(regime_ev)
                    if sig is not None:
                        out.append(EmittedSignal(sig, self._last_spot))
        return out
