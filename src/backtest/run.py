"""
Backtest CLI.

    python -m src.backtest.run --days 60 --iv 0.15

Fetches historical NIFTY spot candles directly from Kite (in-memory, the DB is
never touched), replays them through the live indicator/regime/strategy
components, simulates each option trade with Black-Scholes, and prints a
performance report.

Why fetch fresh instead of reading the candles table: that table mixes two
timestamp conventions (backfill = bar-start, live aggregator = bar-close).
Pulling straight from Kite gives a single, uniform bar-start series we
normalize consistently to close-time.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytz

from src.backtest.engine import ReplayEngine, bar_from_candle
from src.backtest.report import render_report
from src.backtest.simulator import simulate_trade
from src.core.config import settings
from src.core.logging import configure_logging, get_logger
from src.strategies.runner import default_strategies

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

RES_LIST = ["1minute", "5minute", "15minute"]
RES_RANK = {"1minute": 0, "5minute": 1, "15minute": 2}
RES_TO_KITE = {"1minute": "minute", "5minute": "5minute", "15minute": "15minute"}
MINUTE_CAP_DAYS = 60  # Kite's per-request cap for minute interval


async def _fetch(token: int, resolution: str, days: int) -> list[dict[str, Any]]:
    from src.broker.kite_client import get_kite_client

    client = get_kite_client()
    to_dt = datetime.now(tz=IST)
    span = min(days, MINUTE_CAP_DAYS) if resolution == "1minute" else days
    from_dt = to_dt - timedelta(days=span)
    rows = await client.historical_data(
        instrument_token=token,
        from_dt=from_dt,
        to_dt=to_dt,
        interval=RES_TO_KITE[resolution],
        oi=False,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = r["date"]
        if ts.tzinfo is None:
            ts = IST.localize(ts)
        out.append(
            {"ts": ts.astimezone(timezone.utc), "o": r["open"], "h": r["high"],
             "l": r["low"], "c": r["close"], "v": r.get("volume") or 0, "oi": None}
        )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest the trading strategies.")
    ap.add_argument("--days", type=int, default=60, help="history window length (minute capped at 60)")
    ap.add_argument("--warmup-days", type=int, default=5, help="leading days used only to warm indicators")
    ap.add_argument("--iv", type=float, default=0.15, help="annualized implied vol for BS pricing")
    ap.add_argument("--rate", type=float, default=0.065, help="risk-free rate")
    ap.add_argument("--lot-size", type=int, default=0, help="override lot size (0 = use config)")
    ap.add_argument("--cost", type=float, default=50.0, help="round-trip cost per trade (INR)")
    ap.add_argument("--expiry-weekday", type=int, default=1, help="weekly expiry weekday (Mon=0; NIFTY=1 Tue)")
    ap.add_argument("--symbol", type=str, default="NIFTY 50", help="underlying symbol")
    args = ap.parse_args()

    configure_logging(level="WARNING", format="console", log_dir=settings.logging.log_dir)

    lot_size = args.lot_size or next(
        (u.lot_size for u in settings.instruments.underlyings if u.symbol == args.symbol), 75
    )

    from src.auth.kite_session import ensure_valid_token
    from src.broker.instrument_catalog import find_underlying_token

    await ensure_valid_token()
    token = await find_underlying_token(args.symbol, exchange="NSE")
    if token is None:
        print(f"ERROR: underlying {args.symbol!r} not in instrument catalog. Run refresh_instruments.")
        return 1

    print(f"Fetching {args.days}d history for {args.symbol} (token {token}) ...")
    bars = []
    for res in RES_LIST:
        rows = await _fetch(token, res, args.days)
        for row in rows:
            bars.append(bar_from_candle(row, token=token, symbol=args.symbol, resolution=res))
        print(f"  {res}: {len(rows)} candles")

    if not bars:
        print("No history returned (is the Historical Data API enabled on this account?).")
        return 1

    # Chronological merge; finest resolution first on ties.
    bars.sort(key=lambda b: (b.bar_ts, RES_RANK[b.resolution]))
    first_ts = bars[0].bar_ts
    live_start = first_ts + timedelta(days=args.warmup_days)

    engine = ReplayEngine(default_strategies())
    emitted = []
    spot_path: list[tuple[datetime, float]] = []
    for bar in bars:
        if bar.resolution == "1minute" and bar.bar_ts >= live_start:
            spot_path.append((bar.bar_ts, float(bar.c)))
        if bar.bar_ts < live_start:
            engine.warm_bar(bar)
        else:
            emitted.extend(engine.feed_bar(bar))

    path_ts = [t for t, _ in spot_path]
    print(f"Signals generated: {len(emitted)}.  Simulating option trades ...\n")

    trades = []
    for es in emitted:
        t = simulate_trade(
            es, spot_path, path_ts,
            iv=args.iv, rate=args.rate, lot_size=lot_size,
            square_off=_parse_hhmm(settings.market.square_off_time),
            cost_inr=args.cost, expiry_weekday=args.expiry_weekday,
        )
        if t is not None:
            trades.append(t)

    window = f"{first_ts.astimezone(IST):%Y-%m-%d} to {bars[-1].bar_ts.astimezone(IST):%Y-%m-%d}"
    print(render_report(trades, window=window, iv=args.iv, lot_size=lot_size, cost_inr=args.cost))
    return 0


def _parse_hhmm(s: str):
    from datetime import time

    h, m = s.split(":")
    return time(int(h), int(m))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
