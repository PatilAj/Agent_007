"""
Position watcher — exit side of paper mode.

For every open Trade, periodically:
  - Fetch the option's current LTP (Kite quote API).
  - Compute current P&L vs entry.
  - Check exit conditions in priority order:
       1. Hard square-off after `market.square_off_time` (IST)
       2. Stop loss hit (drop ≥ `signal.suggested_sl_pct` from entry)
       3. Take profit hit (rise ≥ `signal.suggested_target_pct`)
       4. End-of-day after `market.close_time` (defensive backstop)
  - If exiting: create a SELL order row, close the trade, update DailyPnL,
    publish an OrderUpdate.

The watcher persists nothing of its own — it's purely a scheduler over
the canonical DB tables.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time, timezone
from decimal import Decimal

import pytz
from sqlalchemy import select, text

from src.broker.kite_client import get_kite_client
from src.core.bus import STREAM_ORDER_UPDATES, EventBus
from src.core.config import settings
from src.core.events import OrderStatus, OrderUpdate
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import Order, Trade
from src.journal.order_journal import close_trade, upsert_daily_pnl
from src.notifications import notify_position_exit

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# How aggressively to re-check P&L on open positions
WATCH_INTERVAL_SECONDS = 30

# Fallback SL/TP if a trade's originating signal didn't supply them
# (e.g. legacy rows). Live behaviour now reads each signal's own
# suggested_sl_pct / suggested_target_pct via the chain Trade -> Order -> Signal.
DEFAULT_SL_PCT = 30.0
DEFAULT_TP_PCT = 60.0


async def _trade_sl_tp(trade: Trade) -> tuple[float, float]:
    """Look up the originating signal's suggested SL/TP for this trade.

    Falls back to the module defaults if the chain is missing (legacy rows
    or trades opened without going through a signal).
    """
    async with get_session() as s:
        row = (
            await s.execute(
                text(
                    """
                    SELECT s.suggested_sl_pct, s.suggested_target_pct
                    FROM orders o
                    JOIN signals s ON s.event_id = o.signal_event_id
                    WHERE o.id = :order_id
                    """
                ),
                {"order_id": trade.entry_order_id},
            )
        ).first()
    if row is None or row[0] is None or row[1] is None:
        return DEFAULT_SL_PCT, DEFAULT_TP_PCT
    return float(row[0]), float(row[1])


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m))


def _now_ist() -> datetime:
    return datetime.now(tz=timezone.utc).astimezone(IST)


async def _ltp_for(symbol: str) -> float:
    kite = get_kite_client()
    key = f"NFO:{symbol}"
    q = await kite.ltp([key])
    row = q.get(key) or next(iter(q.values()), {})
    return float(row.get("last_price") or 0)


async def _evaluate_one(trade: Trade, bus: EventBus) -> None:
    now_ist = _now_ist()
    square_off = _parse_hhmm(settings.market.square_off_time)
    close_t = _parse_hhmm(settings.market.close_time)

    # 0. Overnight stale: any trade opened on a prior IST day must exit immediately.
    #    Without this the time-of-day check below only fires once today's 15:15 hits,
    #    so a trade carried over from yesterday sits open for half a day.
    force_exit_reason: str | None = None
    opened_ist_date = trade.opened_at.astimezone(IST).date()
    if opened_ist_date < now_ist.date():
        force_exit_reason = "stale_overnight"

    # 1. Hard same-day square-off / market-close
    if force_exit_reason is None:
        if now_ist.time() >= square_off:
            force_exit_reason = "square_off"
        elif now_ist.time() >= close_t:
            force_exit_reason = "market_close"

    try:
        ltp = await _ltp_for(trade.tradingsymbol)
    except Exception as e:  # noqa: BLE001
        log.warning("watcher_quote_failed", symbol=trade.tradingsymbol, error=str(e))
        if force_exit_reason is None:
            return
        ltp = float(trade.entry_price)  # use entry as a no-info fallback

    if ltp <= 0:
        if force_exit_reason is None:
            return
        ltp = float(trade.entry_price)

    # 2. P&L-based exits (only relevant for long-option positions in v1)
    pnl_pct = (ltp - float(trade.entry_price)) / float(trade.entry_price) * 100
    # Honor the originating signal's suggested SL/TP so live behaviour matches
    # what the strategy / daily-test intended (and what the backtester measures).
    sl_pct, tp_pct = await _trade_sl_tp(trade)

    exit_reason: str | None = force_exit_reason
    if exit_reason is None:
        if trade.side == "BUY":
            if pnl_pct <= -sl_pct:
                exit_reason = "stop_loss"
            elif pnl_pct >= tp_pct:
                exit_reason = "take_profit"
        else:
            # SELL — inverted (not used in v1, present for symmetry)
            if pnl_pct >= sl_pct:
                exit_reason = "stop_loss"
            elif pnl_pct <= -tp_pct:
                exit_reason = "take_profit"

    if exit_reason is None:
        return  # still in range

    # 3. Persist the exit
    exit_side = "SELL" if trade.side == "BUY" else "BUY"
    exit_price_dec = Decimal(f"{ltp:.2f}")
    async with get_session() as s:
        exit_order = Order(
            client_order_id=str(uuid.uuid4()),
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
            signal_event_id=None,
            strategy_id=trade.strategy_id,
            instrument_token=0,  # not stored on Trade row; reconstructable from symbol
            tradingsymbol=trade.tradingsymbol,
            side=exit_side,
            qty=trade.qty,
            order_type="MARKET",
            price=exit_price_dec,
            trigger_price=None,
            product="MIS",
            status=OrderStatus.COMPLETE.value,
            filled_qty=trade.qty,
            avg_fill_price=exit_price_dec,
        )
        s.add(exit_order)
        await s.flush()
        exit_order_id = exit_order.id

    closed_at = datetime.now(tz=timezone.utc)
    net_pnl = await close_trade(
        trade.id,
        exit_order_id=exit_order_id,
        exit_price=exit_price_dec,
        closed_at=closed_at,
        exit_reason=exit_reason,
    )

    # 4. Daily PnL roll-up
    today_ist_midnight = datetime.combine(
        now_ist.date(), datetime.min.time(), tzinfo=IST
    ).astimezone(timezone.utc)
    await upsert_daily_pnl(
        trade_date_ist_midnight_utc=today_ist_midnight,
        pnl_delta=net_pnl,
        won=net_pnl > 0,
    )

    # 5. Emit OrderUpdate for downstream listeners
    await bus.publish(
        STREAM_ORDER_UPDATES,
        OrderUpdate(
            event_id=str(uuid.uuid4()),
            ts=closed_at,
            client_order_id=exit_order.client_order_id,
            broker_order_id=exit_order.broker_order_id,
            status=OrderStatus.COMPLETE,
            filled_qty=trade.qty,
            avg_fill_price=exit_price_dec,
            rejection_reason=None,
        ),
    )

    log.info(
        "position_exited",
        symbol=trade.tradingsymbol,
        reason=exit_reason,
        entry=float(trade.entry_price),
        exit=float(exit_price_dec),
        pnl=float(net_pnl),
    )

    await notify_position_exit(
        bus,
        tradingsymbol=trade.tradingsymbol,
        exit_reason=exit_reason,
        entry=trade.entry_price,
        exit_price=exit_price_dec,
        pnl=net_pnl,
    )


async def run_position_watcher(bus: EventBus, interval_seconds: int = WATCH_INTERVAL_SECONDS) -> None:
    log.info("position_watcher_starting", interval=interval_seconds)
    while True:
        try:
            async with get_session() as s:
                stmt = select(Trade).where(Trade.closed_at.is_(None))
                open_trades = list((await s.execute(stmt)).scalars().all())
            for t in open_trades:
                try:
                    await _evaluate_one(t, bus)
                except Exception as e:  # noqa: BLE001
                    log.exception("watcher_eval_error", trade_id=t.id, error=str(e))
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "shutting down" in msg or "refused" in msg or "winerror 1225" in msg:
                log.warning("watcher_db_unavailable", hint="DB went away; will retry on next tick")
            else:
                log.exception("watcher_loop_error", error=str(e))
        await asyncio.sleep(interval_seconds)
