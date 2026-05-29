"""
Order + trade journal — write side.

Persistence helpers for the Order and Trade tables. Used by the paper
executor (and later the shadow / live executor) to record every order
lifecycle event.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update

from src.core.events import OrderRequest, OrderStatus
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import DailyPnL, Order, Trade

log = get_logger(__name__)


async def insert_order_from_request(req: OrderRequest, status: OrderStatus = OrderStatus.PENDING) -> int:
    """Insert an Order row from a risk-approved OrderRequest. Returns row id."""
    async with get_session() as s:
        row = Order(
            client_order_id=req.client_order_id,
            broker_order_id=None,
            signal_event_id=req.signal_event_id,
            strategy_id=req.strategy_id,
            instrument_token=req.contract.instrument_token,
            tradingsymbol=req.contract.tradingsymbol,
            side=req.side.value,
            qty=req.qty,
            order_type=req.order_type,
            price=req.price,
            trigger_price=req.trigger_price,
            product=req.product,
            status=status.value,
            filled_qty=0,
            avg_fill_price=None,
        )
        s.add(row)
        await s.flush()
        return row.id


async def mark_order_filled(
    client_order_id: str,
    *,
    filled_qty: int,
    avg_fill_price: Decimal,
    broker_order_id: str | None = None,
) -> int | None:
    """Update an Order row to COMPLETE. Returns the row id."""
    async with get_session() as s:
        await s.execute(
            update(Order)
            .where(Order.client_order_id == client_order_id)
            .values(
                status=OrderStatus.COMPLETE.value,
                filled_qty=filled_qty,
                avg_fill_price=avg_fill_price,
                broker_order_id=broker_order_id,
            )
        )
        row_id = (
            await s.execute(
                select(Order.id).where(Order.client_order_id == client_order_id)
            )
        ).scalar_one_or_none()
    log.info(
        "order_filled",
        client_order_id=client_order_id,
        filled_qty=filled_qty,
        avg_fill_price=float(avg_fill_price),
    )
    return row_id


async def open_trade(
    *,
    entry_order_id: int,
    strategy_id: str,
    tradingsymbol: str,
    side: str,
    qty: int,
    entry_price: Decimal,
    opened_at: datetime,
) -> int:
    """Create a Trade row for an entry that just filled."""
    async with get_session() as s:
        t = Trade(
            entry_order_id=entry_order_id,
            strategy_id=strategy_id,
            tradingsymbol=tradingsymbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            opened_at=opened_at,
        )
        s.add(t)
        await s.flush()
        return t.id


async def close_trade(
    trade_id: int,
    *,
    exit_order_id: int,
    exit_price: Decimal,
    closed_at: datetime,
    exit_reason: str,
) -> Decimal:
    """Close a trade. Computes gross_pnl. Returns net_pnl (== gross for paper)."""
    async with get_session() as s:
        trade = (await s.execute(select(Trade).where(Trade.id == trade_id))).scalar_one()
        # For BUY trades (long options): pnl = (exit - entry) * qty
        # For SELL trades (short options, future use): pnl = (entry - exit) * qty
        if trade.side == "BUY":
            gross = (exit_price - trade.entry_price) * trade.qty
        else:
            gross = (trade.entry_price - exit_price) * trade.qty
        # No commission modeling in paper mode v1 — net = gross.
        net = gross
        await s.execute(
            update(Trade)
            .where(Trade.id == trade_id)
            .values(
                exit_order_id=exit_order_id,
                exit_price=exit_price,
                gross_pnl=gross,
                net_pnl=net,
                closed_at=closed_at,
                hold_seconds=int((closed_at - trade.opened_at).total_seconds()),
                exit_reason=exit_reason,
            )
        )
    log.info(
        "trade_closed",
        trade_id=trade_id,
        net_pnl=float(net),
        exit_reason=exit_reason,
    )
    return net


async def upsert_daily_pnl(
    *,
    trade_date_ist_midnight_utc: datetime,
    pnl_delta: Decimal,
    won: bool,
) -> None:
    """Incrementally update the per-day aggregate row. Creates if missing."""
    async with get_session() as s:
        row = (
            await s.execute(
                select(DailyPnL).where(DailyPnL.trade_date == trade_date_ist_midnight_utc)
            )
        ).scalar_one_or_none()
        if row is None:
            row = DailyPnL(
                trade_date=trade_date_ist_midnight_utc,
                gross_pnl=pnl_delta,
                net_pnl=pnl_delta,
                trade_count=1,
                win_count=1 if won else 0,
                loss_count=0 if won else 1,
                consecutive_losses=0 if won else 1,
                halted=False,
            )
            s.add(row)
        else:
            row.gross_pnl = row.gross_pnl + pnl_delta
            row.net_pnl = row.net_pnl + pnl_delta
            row.trade_count = row.trade_count + 1
            if won:
                row.win_count = row.win_count + 1
                row.consecutive_losses = 0
            else:
                row.loss_count = row.loss_count + 1
                row.consecutive_losses = row.consecutive_losses + 1
