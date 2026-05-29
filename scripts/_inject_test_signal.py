"""
Inject ONE synthetic paper signal through the LIVE pipeline, then watch the
result. Paper-only, no real money.

It replicates exactly what the strategy runner's `_emit` does (persist +
publish to stream:signals + notify), so the already-running ingestor's risk
engine, paper executor, and position watcher process it identically to an
organic signal. This script does NOT run any risk/executor logic itself.

Direction is DATA-DRIVEN: it reads the current 15-minute EMA20-vs-EMA50 trend
from fresh Kite candles and buys CE in an uptrend, PE in a downtrend — so a
test trade is never directionally backwards versus the visible market.

The signal is tagged strategy_id="manual_test" so it stays distinguishable
from organic strategy signals in the journal.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytz
from sqlalchemy import func, select

from src.core.bus import STREAM_SIGNALS, EventBus
from src.core.events import OptionType, Side, SignalCandidate
from src.data.db import get_session
from src.data.models import Order, Signal, Trade
from src.journal import persist_signal
from src.notifications import notify_signal

IST = pytz.timezone("Asia/Kolkata")
NIFTY_TOKEN = 256265


def _ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


async def _current_trend() -> tuple[OptionType, str]:
    """Read the current 15m EMA20-vs-EMA50 trend from fresh Kite candles.

    Returns (option_type, human_reason). CE if EMA20 > EMA50 (uptrend),
    else PE. Falls back to PE-neutral if data is unavailable.
    """
    from src.auth.kite_session import ensure_valid_token
    from src.broker.kite_client import get_kite_client

    await ensure_valid_token()
    client = get_kite_client()
    to_dt = datetime.now(tz=IST)
    rows = await client.historical_data(
        instrument_token=NIFTY_TOKEN,
        from_dt=to_dt - timedelta(days=7),
        to_dt=to_dt,
        interval="15minute",
        oi=False,
    )
    closes = [float(r["close"]) for r in rows]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if e20 is None or e50 is None:
        return OptionType.PE, "trend unknown (insufficient data) -> defaulting PE"
    if e20 > e50:
        return OptionType.CE, f"15m uptrend (EMA20 {e20:.0f} > EMA50 {e50:.0f}) -> CE"
    return OptionType.PE, f"15m downtrend (EMA20 {e20:.0f} < EMA50 {e50:.0f}) -> PE"


async def main() -> None:
    bus = await EventBus.connect()

    # Snapshot current max ids so we can detect what THIS injection creates.
    async with get_session() as s:
        max_order_id = (await s.execute(select(func.max(Order.id)))).scalar() or 0
        max_trade_id = (await s.execute(select(func.max(Trade.id)))).scalar() or 0

    opt_type, trend_reason = await _current_trend()
    now = datetime.now(tz=timezone.utc)
    sig = SignalCandidate(
        event_id=str(uuid.uuid4()),
        ts=now,
        strategy_id="manual_test",
        underlying="NIFTY 50",
        side=Side.BUY,
        option_type=opt_type,
        confidence=75.0,
        rationale=[
            "manual pipeline-validation trade",
            f"direction from live trend: {trend_reason}",
            f"NIFTY 50 ATM {opt_type.value}, current-week expiry",
            "verifies Rs 3L sizing -> paper fill -> watcher-managed exit",
        ],
        indicators_snapshot={},
        suggested_sl_pct=30.0,
        suggested_target_pct=60.0,
    )

    print(f"Injecting signal {sig.event_id[:8]} at {now.astimezone(IST):%H:%M:%S} IST")
    print(f"  direction: {trend_reason}")
    print(f"  NIFTY 50  BUY {opt_type.value}  conf=75  (strategy_id=manual_test)")

    # --- replicate runner._emit ---
    await persist_signal(sig)
    await bus.publish(STREAM_SIGNALS, sig)
    await notify_signal(
        bus,
        strategy_id=sig.strategy_id,
        underlying=sig.underlying,
        side=sig.side.value,
        option_type=sig.option_type.value,
        confidence=sig.confidence,
    )
    print("Published to stream:signals. The running agent now owns it.\n")

    # --- watch the outcome (up to ~2 min) ---
    last_state = ""
    for i in range(40):
        await asyncio.sleep(3)
        async with get_session() as s:
            srow = (
                await s.execute(select(Signal).where(Signal.event_id == sig.event_id))
            ).scalar_one_or_none()
            new_orders = list(
                (await s.execute(select(Order).where(Order.id > max_order_id).order_by(Order.id))).scalars().all()
            )
            new_trades = list(
                (await s.execute(select(Trade).where(Trade.id > max_trade_id).order_by(Trade.id))).scalars().all()
            )

        secs = (i + 1) * 3
        if srow is not None and not srow.accepted and srow.rejection_reason:
            print(f"[{secs:>3}s] REJECTED by risk engine: {srow.rejection_reason}")
            break

        if new_trades:
            t = new_trades[0]
            verdict = "accepted" if (srow and srow.accepted) else "pending"
            print(f"[{secs:>3}s] FILLED  signal={verdict}")
            print(f"        order id : {new_orders[0].id if new_orders else '?'}")
            print(f"        symbol   : {t.tradingsymbol}")
            print(f"        side/qty : {t.side} x {t.qty}")
            print(f"        entry    : Rs {t.entry_price}")
            print(f"        notional : Rs {float(t.entry_price) * t.qty:,.2f}")
            print(f"        trade id : {t.id}  (open; watcher manages SL 30% / TP 60% / 15:15 square-off)")
            break

        state = f"signal={'accepted' if (srow and srow.accepted) else 'pending'} orders={len(new_orders)}"
        if state != last_state:
            print(f"[{secs:>3}s] {state} ... waiting for fill")
            last_state = state
    else:
        print("\nTimed out after ~2 min. Check the ingestor log / `/trades` on the bot.")

    await bus.close()


if __name__ == "__main__":
    asyncio.run(main())
