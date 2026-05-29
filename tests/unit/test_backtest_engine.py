"""
Unit tests for the backtest replay engine + trade simulator.

We verify:
  - determinism: identical bars -> identical signals
  - the engine actually drives the live strategies (a trending sawtooth series
    produces at least one ema_regime CE signal)
  - the simulator resolves a trade with a sane exit and P&L sign
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytz

from src.backtest.engine import ReplayEngine
from src.backtest.simulator import simulate_trade
from src.backtest.option_pricing import bs_price
from src.core.events import BarEvent, OptionType, Side, SignalCandidate
from src.backtest.engine import EmittedSignal
from src.strategies.runner import default_strategies

IST = pytz.timezone("Asia/Kolkata")
TOKEN = 256265
SYMBOL = "NIFTY 50"

# Start at 09:31 IST on a weekday (2026-05-21 is a Thursday) = 04:01 UTC.
START = datetime(2026, 5, 21, 4, 1, tzinfo=timezone.utc)


def _bar(i: int, close: float) -> BarEvent:
    ts = START + timedelta(minutes=i)
    return BarEvent(
        event_id=str(uuid.uuid4()),
        ts=ts,
        instrument_token=TOKEN,
        symbol=SYMBOL,
        resolution="1minute",
        bar_ts=ts,
        o=Decimal(str(close)),
        h=Decimal(str(close + 3)),
        l=Decimal(str(close - 3)),
        c=Decimal(str(close)),
        v=0,
    )


def _sawtooth_uptrend(n: int = 150, base: float = 20000.0) -> list[BarEvent]:
    """Net uptrend with periodic dips — keeps EMA20>EMA50 while RSI stays < 70."""
    bars = []
    price = base
    for i in range(n):
        # +12 for 5 bars, then -10 once (net +50 per 6-bar cycle)
        price += 12.0 if (i % 6) != 5 else -10.0
        bars.append(_bar(i, price))
    return bars


def test_determinism_same_input_same_signals():
    bars = _sawtooth_uptrend()
    eng1 = ReplayEngine(default_strategies())
    eng2 = ReplayEngine(default_strategies())
    out1, out2 = [], []
    for b in bars:
        out1.extend(eng1.feed_bar(b))
    for b in bars:
        out2.extend(eng2.feed_bar(b))

    key = lambda es: (es.signal.strategy_id, es.signal.option_type.value,
                      es.signal.ts.isoformat(), round(es.signal.confidence, 4))
    assert [key(s) for s in out1] == [key(s) for s in out2]


def test_trending_series_emits_ce_signal():
    bars = _sawtooth_uptrend()
    eng = ReplayEngine(default_strategies())
    emitted = []
    for b in bars:
        emitted.extend(eng.feed_bar(b))
    ce = [e for e in emitted if e.signal.option_type == OptionType.CE
          and e.signal.strategy_id == "ema_regime_v2"]
    assert len(ce) >= 1
    assert ce[0].spot > 0


def _mk_emitted(opt: OptionType, ts: datetime, spot: float, sl=30.0, tp=60.0) -> EmittedSignal:
    sig = SignalCandidate(
        event_id=str(uuid.uuid4()), ts=ts, strategy_id="manual",
        underlying=SYMBOL, side=Side.BUY, option_type=opt, confidence=70.0,
        rationale=["t"], indicators_snapshot={}, suggested_sl_pct=sl, suggested_target_pct=tp,
    )
    return EmittedSignal(sig, spot)


def test_simulator_take_profit_on_rally():
    entry_ts = datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc)  # 10:30 IST
    spot0 = 20000.0
    # Build a forward path that rallies hard -> CE premium should hit +TP
    path = []
    for i in range(1, 120):
        ts = entry_ts + timedelta(minutes=i)
        path.append((ts, spot0 + i * 8.0))  # steady strong rally
    path_ts = [t for t, _ in path]
    es = _mk_emitted(OptionType.CE, entry_ts, spot0, sl=30.0, tp=60.0)
    from datetime import time as _t
    tr = simulate_trade(es, path, path_ts, iv=0.15, rate=0.065, lot_size=75,
                        square_off=_t(15, 15), cost_inr=0.0, expiry_weekday=1)
    assert tr is not None
    assert tr.exit_reason in ("take_profit", "square_off", "eod")
    # a strong rally on a CE must be profitable in premium terms
    assert tr.exit_premium > tr.entry_premium


def test_simulator_square_off_when_flat():
    entry_ts = datetime(2026, 5, 21, 5, 0, tzinfo=timezone.utc)
    spot0 = 20000.0
    path = []
    for i in range(1, 400):  # flat, runs to square-off (15:15)
        ts = entry_ts + timedelta(minutes=i)
        path.append((ts, spot0))
    path_ts = [t for t, _ in path]
    es = _mk_emitted(OptionType.CE, entry_ts, spot0)
    from datetime import time as _t
    tr = simulate_trade(es, path, path_ts, iv=0.15, rate=0.065, lot_size=75,
                        square_off=_t(15, 15), cost_inr=0.0, expiry_weekday=1)
    assert tr is not None
    assert tr.exit_reason == "square_off"
