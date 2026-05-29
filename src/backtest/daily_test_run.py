"""
Backtest the *daily_paper_test* idea over historical NIFTY data.

Simulates: every weekday in the window, at fire_time (default 10:30 IST), apply
the live filters — yesterday's daily candle direction AND today's 15m EMA20-vs-
EMA50 must agree, plus no-trade zone check — and if everything passes, open one
ATM option trade priced by Black-Scholes. Exit at fixed SL/TP or 15:15 square-off.

This mirrors `src/workers/daily_test.py` so backtest behaviour matches what the
live ingestor will actually do every weekday.

Usage:
    python -m src.backtest.daily_test_run --days 60 --sl 5 --tp 10 --iv 0.15
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytz

from src.backtest.engine import EmittedSignal
from src.backtest.option_pricing import atm_strike, bs_price, next_weekly_expiry, year_fraction
from src.backtest.report import render_report
from src.backtest.simulator import simulate_trade
from src.core.config import settings
from src.core.events import OptionType, Side, SignalCandidate
from src.core.logging import configure_logging, get_logger

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")
STRATEGY_ID = "daily_paper_test"

# NIFTY contract lot size (per option contract). Sourced from the live
# instrument catalog (it was 75 historically; current is 65).
NIFTY_LOT_SIZE = 65

LUNCH_START = time(12, 30)
LUNCH_END = time(13, 30)
NO_ENTRY_AFTER = time(14, 30)


def _ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _in_no_trade_zone(t: time) -> bool:
    return (LUNCH_START <= t < LUNCH_END) or (t >= NO_ENTRY_AFTER)


async def _fetch(token: int, interval: str, days: int) -> list[dict[str, Any]]:
    from src.broker.kite_client import get_kite_client

    client = get_kite_client()
    to_dt = datetime.now(tz=IST)
    span = min(days, 60) if interval == "minute" else days
    from_dt = to_dt - timedelta(days=span)
    rows = await client.historical_data(
        instrument_token=token,
        from_dt=from_dt,
        to_dt=to_dt,
        interval=interval,
        oi=False,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = r["date"]
        if ts.tzinfo is None:
            ts = IST.localize(ts)
        out.append(
            {
                "ts": ts.astimezone(timezone.utc),
                "o": float(r["open"]),
                "h": float(r["high"]),
                "l": float(r["low"]),
                "c": float(r["close"]),
            }
        )
    return out


def _build_daily_by_date(daily_rows: list[dict]) -> dict[date, dict]:
    """Index daily candles by their IST trading date."""
    out: dict[date, dict] = {}
    for r in daily_rows:
        d = r["ts"].astimezone(IST).date()
        out[d] = r
    return out


def _previous_daily_dir(daily_by_date: dict[date, dict], today_ist: date) -> tuple[int, str]:
    """Direction of the most-recent completed daily candle strictly BEFORE today."""
    prior = sorted([d for d in daily_by_date.keys() if d < today_ist])
    if not prior:
        return 0, "no_prior_daily"
    r = daily_by_date[prior[-1]]
    if r["c"] > r["o"]:
        return 1, f"daily_up O={r['o']:.0f}<C={r['c']:.0f} on {prior[-1]}"
    if r["c"] < r["o"]:
        return -1, f"daily_down O={r['o']:.0f}>C={r['c']:.0f} on {prior[-1]}"
    return 0, "daily_flat"


def _intraday_dir_at(fifteen_m_rows: list[dict], at_ts_utc: datetime) -> tuple[int, str]:
    """15m EMA20-vs-EMA50 sign using only candles whose CLOSE time is at/before `at_ts_utc`.

    Kite returns the bar START in `ts`; close = start + 15 min.
    """
    closes: list[float] = []
    for r in fifteen_m_rows:
        close_ts = r["ts"] + timedelta(minutes=15)
        if close_ts <= at_ts_utc:
            closes.append(r["c"])
        else:
            break  # rows are time-sorted
    if len(closes) < 50:
        return 0, "15m_warmup_insufficient"
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if e20 is None or e50 is None:
        return 0, "ema_calc_failed"
    if e20 > e50:
        return 1, f"15m_up EMA20={e20:.0f}>EMA50={e50:.0f}"
    if e20 < e50:
        return -1, f"15m_down EMA20={e20:.0f}<EMA50={e50:.0f}"
    return 0, "15m_flat"


def _spot_at(one_m_rows: list[dict], target_ts_utc: datetime) -> tuple[datetime, float] | None:
    """Find the 1m bar that closes at `target_ts_utc`. close = start + 1 min."""
    for r in one_m_rows:
        close_ts = r["ts"] + timedelta(minutes=1)
        if close_ts == target_ts_utc:
            return close_ts, r["c"]
    return None


def _build_forward_path(one_m_rows: list[dict], after_ts_utc: datetime) -> tuple[list[tuple[datetime, float]], list[datetime]]:
    """1m spot path strictly AFTER `after_ts_utc` (close-stamped)."""
    path: list[tuple[datetime, float]] = []
    for r in one_m_rows:
        close_ts = r["ts"] + timedelta(minutes=1)
        if close_ts > after_ts_utc:
            path.append((close_ts, r["c"]))
    path_ts = [t for t, _ in path]
    return path, path_ts


def _all_trading_days(daily_by_date: dict[date, dict]) -> list[date]:
    return sorted(daily_by_date.keys())


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the daily_paper_test idea.")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--sl", type=float, default=5.0, help="SL %% of premium")
    ap.add_argument("--tp", type=float, default=10.0, help="TP %% of premium")
    ap.add_argument("--iv", type=float, default=0.15)
    ap.add_argument("--rate", type=float, default=0.065)
    ap.add_argument("--lot-size", type=int, default=0)
    ap.add_argument("--cost", type=float, default=50.0)
    ap.add_argument("--expiry-weekday", type=int, default=1, help="NIFTY weekly = Tue (1)")
    ap.add_argument("--fire-hour", type=int, default=10)
    ap.add_argument("--fire-minute", type=int, default=30)
    ap.add_argument("--symbol", type=str, default="NIFTY 50")
    args = ap.parse_args()

    configure_logging(level="WARNING", format="console", log_dir=settings.logging.log_dir)

    # The live risk engine sizes each trade dynamically: lots = floor(budget /
    # (premium × per-lot-size)). Mirror that here so backtest P&L reflects what
    # the agent will actually trade today.
    budget_per_trade = (
        float(settings.risk.slot_capital_inr)
        * float(settings.risk.max_premium_per_trade_pct)
        / 100.0
    )
    print(f"Dynamic sizing: budget Rs {budget_per_trade:,.0f}, per-lot {NIFTY_LOT_SIZE} shares.")

    from src.auth.kite_session import ensure_valid_token
    from src.broker.instrument_catalog import find_underlying_token

    await ensure_valid_token()
    token = await find_underlying_token(args.symbol, exchange="NSE")
    if token is None:
        print(f"ERROR: underlying {args.symbol!r} not in catalog.")
        return 1

    print(f"Fetching {args.days}d history for {args.symbol} (token {token}) ...")
    one_m = await _fetch(token, "minute", args.days)
    fifteen_m = await _fetch(token, "15minute", args.days)
    daily = await _fetch(token, "day", args.days)
    print(f"  1m={len(one_m)}  15m={len(fifteen_m)}  daily={len(daily)}")

    daily_by_date = _build_daily_by_date(daily)
    trading_days = _all_trading_days(daily_by_date)

    if not trading_days:
        print("No trading days in window.")
        return 1

    # Skip the first ~10 trading days so the 15m EMA warmup is meaningful.
    eval_days = trading_days[10:] if len(trading_days) > 10 else trading_days

    fired = 0
    skipped: dict[str, int] = {}
    trades = []

    for day in eval_days:
        fire_ist = IST.localize(datetime.combine(day, time(args.fire_hour, args.fire_minute)))
        fire_utc = fire_ist.astimezone(timezone.utc)
        if fire_ist.weekday() >= 5:
            continue  # daily candles only contain trading days but safeguard anyway
        if _in_no_trade_zone(fire_ist.time()):
            skipped["no_trade_zone"] = skipped.get("no_trade_zone", 0) + 1
            continue

        daily_dir, _ = _previous_daily_dir(daily_by_date, day)
        intraday_dir, _ = _intraday_dir_at(fifteen_m, fire_utc)

        if daily_dir == 0 or intraday_dir == 0:
            skipped["filter_zero"] = skipped.get("filter_zero", 0) + 1
            continue
        if daily_dir != intraday_dir:
            skipped["filter_disagree"] = skipped.get("filter_disagree", 0) + 1
            continue

        spot_pt = _spot_at(one_m, fire_utc)
        if spot_pt is None:
            skipped["no_spot_bar"] = skipped.get("no_spot_bar", 0) + 1
            continue
        _, spot = spot_pt

        opt = OptionType.CE if intraday_dir > 0 else OptionType.PE

        # Mirror the live risk engine's sizing: compute the entry premium first,
        # then `lots = floor(budget / (premium × NIFTY_LOT_SIZE))`. Skip days
        # where even 1 lot won't fit the budget (live risk would reject).
        strike = atm_strike(spot)
        expiry = next_weekly_expiry(fire_utc, weekday=args.expiry_weekday)
        entry_prem = bs_price(
            spot, strike, year_fraction(fire_utc, expiry), args.iv, opt, rate=args.rate
        )
        per_lot_cost = entry_prem * NIFTY_LOT_SIZE
        lots = int(budget_per_trade // per_lot_cost) if per_lot_cost > 0 else 0
        if lots < 1:
            skipped["premium_cap"] = skipped.get("premium_cap", 0) + 1
            continue
        qty = lots * NIFTY_LOT_SIZE

        sig = SignalCandidate(
            event_id=str(uuid.uuid4()),
            ts=fire_utc,
            strategy_id=STRATEGY_ID,
            underlying=args.symbol,
            side=Side.BUY,
            option_type=opt,
            confidence=70.0,
            rationale=[f"backtest daily test day={day} lots={lots}"],
            indicators_snapshot={},
            suggested_sl_pct=args.sl,
            suggested_target_pct=args.tp,
        )
        es = EmittedSignal(sig, spot)

        forward, forward_ts = _build_forward_path(one_m, fire_utc)
        if not forward:
            skipped["no_forward_bars"] = skipped.get("no_forward_bars", 0) + 1
            continue

        t = simulate_trade(
            es,
            forward,
            forward_ts,
            iv=args.iv,
            rate=args.rate,
            lot_size=qty,
            square_off=time(15, 15),
            cost_inr=args.cost,
            expiry_weekday=args.expiry_weekday,
        )
        if t is None:
            skipped["sim_failed"] = skipped.get("sim_failed", 0) + 1
            continue
        trades.append(t)
        fired += 1

    days_in_window = len(eval_days)
    print()
    print(f"Evaluated {days_in_window} trading days")
    print(f"  fired   : {fired}")
    print(f"  skipped : {sum(skipped.values())}  ({skipped})")
    print()

    window = f"{eval_days[0]} to {eval_days[-1]}"
    print(render_report(trades, window=window, iv=args.iv, lot_size=NIFTY_LOT_SIZE, cost_inr=args.cost))

    if trades:
        net = sum(t.net_pnl for t in trades)
        per_day = net / days_in_window
        per_fired = net / fired if fired else 0.0
        print(f"Per-calendar-day avg : Rs {per_day:+,.0f}")
        print(f"Per-fired-trade avg  : Rs {per_fired:+,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
