"""
Trade simulator.

Given a signal (with the spot at emit time) and the forward 1-minute spot path,
model the option trade with Black-Scholes:

  entry  : ATM strike, current-week expiry, premium = BS(spot, K, T, iv)
  walk   : at each later 1m bar recompute premium as spot moves and T decays
  exit   : first of  take-profit (+TP%) / stop-loss (-SL%) /
           square-off (15:15 IST) / end-of-data

SL/TP percentages come from the signal's own suggested levels (the strategy's
intent). NOTE: the live position watcher currently hardcodes 30/60 and ignores
these — a gap worth closing; the backtest evaluates the strategy as designed.

Costs: a flat round-trip INR cost per trade approximates brokerage+STT+fees.
Per-signal independent simulation (no portfolio concurrency cap) — measures raw
signal edge; a portfolio-level pass is a future refinement.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pytz

from src.backtest.engine import EmittedSignal
from src.backtest.option_pricing import atm_strike, bs_price, next_weekly_expiry, year_fraction
from src.core.events import OptionType

IST = pytz.timezone("Asia/Kolkata")

DEFAULT_SL_PCT = 30.0
DEFAULT_TP_PCT = 60.0


@dataclass
class BTTrade:
    strategy_id: str
    symbol: str
    option_type: str
    confidence: float
    entry_ts: datetime
    exit_ts: datetime
    entry_spot: float
    exit_spot: float
    strike: float
    entry_premium: float
    exit_premium: float
    qty: int
    gross_pnl: float
    cost: float
    net_pnl: float
    pnl_pct: float
    exit_reason: str
    hold_minutes: int


def _square_off_dt(day_ist: datetime, square_off: time) -> datetime:
    return day_ist.replace(
        hour=square_off.hour, minute=square_off.minute, second=0, microsecond=0
    )


def simulate_trade(
    es: EmittedSignal,
    spot_path: list[tuple[datetime, float]],
    path_ts: list[datetime],
    *,
    iv: float,
    rate: float,
    lot_size: int,
    square_off: time,
    cost_inr: float,
    expiry_weekday: int,
) -> BTTrade | None:
    """Simulate one option trade. Returns None if it can't be priced."""
    sig = es.signal
    opt = sig.option_type
    spot0 = es.spot
    if spot0 <= 0:
        return None

    strike = atm_strike(spot0)
    expiry = next_weekly_expiry(sig.ts, weekday=expiry_weekday)
    entry_prem = bs_price(spot0, strike, year_fraction(sig.ts, expiry), iv, opt, rate=rate)
    if entry_prem <= 0:
        return None

    sl_pct = sig.suggested_sl_pct if sig.suggested_sl_pct else DEFAULT_SL_PCT
    tp_pct = sig.suggested_target_pct if sig.suggested_target_pct else DEFAULT_TP_PCT
    sl_level = entry_prem * (1 - sl_pct / 100.0)
    tp_level = entry_prem * (1 + tp_pct / 100.0)

    square_off_dt = _square_off_dt(sig.ts.astimezone(IST), square_off)

    # Walk forward strictly after the signal bar (no look-ahead).
    start = bisect.bisect_right(path_ts, sig.ts)
    exit_prem = entry_prem
    exit_spot = spot0
    exit_ts = sig.ts
    exit_reason = "eod"

    for i in range(start, len(spot_path)):
        ts, spot_t = spot_path[i]
        if ts.astimezone(IST) >= square_off_dt:
            t_years = year_fraction(ts, expiry)
            exit_prem = bs_price(spot_t, strike, t_years, iv, opt, rate=rate)
            exit_spot, exit_ts, exit_reason = spot_t, ts, "square_off"
            break
        t_years = year_fraction(ts, expiry)
        prem_t = bs_price(spot_t, strike, t_years, iv, opt, rate=rate)
        exit_prem, exit_spot, exit_ts = prem_t, spot_t, ts
        if prem_t <= sl_level:
            exit_reason = "stop_loss"
            break
        if prem_t >= tp_level:
            exit_reason = "take_profit"
            break
    else:
        # ran out of data without hitting a level
        exit_reason = "eod"

    gross = (exit_prem - entry_prem) * lot_size  # long option
    net = gross - cost_inr
    pnl_pct = (exit_prem - entry_prem) / entry_prem * 100.0
    hold_min = int((exit_ts - sig.ts).total_seconds() // 60)

    return BTTrade(
        strategy_id=sig.strategy_id,
        symbol=f"NIFTY{int(strike)}{opt.value}",
        option_type=opt.value,
        confidence=sig.confidence,
        entry_ts=sig.ts,
        exit_ts=exit_ts,
        entry_spot=spot0,
        exit_spot=exit_spot,
        strike=strike,
        entry_premium=entry_prem,
        exit_premium=exit_prem,
        qty=lot_size,
        gross_pnl=gross,
        cost=cost_inr,
        net_pnl=net,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason,
        hold_minutes=hold_min,
    )
