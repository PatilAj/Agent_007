"""
Paper executor — simulated fills for the trading agent.

Lifecycle:
  1. Consume OrderRequest from stream:orders.
  2. Persist the Order row as PENDING.
  3. Fetch a current quote for the contract (Kite REST).
  4. Simulate a fill:
       - BUY (long option): fill at `min(limit_price, ask)` if ask known, else last.
         Add small adverse slippage (paper) to keep behaviour realistic.
       - SELL: fill at `max(limit_price, bid)` (covered by sell-side logic later).
  5. Mark the Order COMPLETE + create an entry Trade row.
  6. Emit an OrderUpdate so other consumers update their view.

No commission/STT modeling yet — that's straightforward to layer on top
once we have a real fee schedule from Kite to validate against.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from src.broker.kite_client import get_kite_client
from src.core.bus import STREAM_ORDERS, STREAM_ORDER_UPDATES, EventBus
from src.core.config import settings
from src.core.events import OrderRequest, OrderStatus, OrderUpdate
from src.core.logging import get_logger
from src.journal.order_journal import (
    insert_order_from_request,
    mark_order_filled,
    open_trade,
)
from src.notifications import notify_fill

log = get_logger(__name__)

# Paper-mode slippage in basis points (1 bp = 0.01%). Adverse to the trader
# so paper results err on the cautious side. Tweak from config later.
PAPER_SLIPPAGE_BPS = 5  # 0.05% adverse


async def _fetch_quote(tradingsymbol: str) -> tuple[float, float, float]:
    """Return (ltp, bid, ask). 0.0 for any value not available."""
    kite = get_kite_client()
    key = f"NFO:{tradingsymbol}"
    q = await kite.quote([key])
    row = q.get(key) or next(iter(q.values()), {})
    last = float(row.get("last_price") or 0)
    depth = row.get("depth") or {}
    bid = float(depth.get("buy", [{}])[0].get("price") or 0) if depth.get("buy") else 0
    ask = float(depth.get("sell", [{}])[0].get("price") or 0) if depth.get("sell") else 0
    return last, bid, ask


def _simulate_fill_price(side: str, last: float, bid: float, ask: float, limit_price: float | None) -> float:
    """Pick a realistic fill price in paper mode.

    For BUY: pay the asking side + small slippage; cap at LIMIT if given.
    For SELL: hit the bid - small slippage; floor at LIMIT if given.
    """
    slip = PAPER_SLIPPAGE_BPS / 10000.0
    if side == "BUY":
        base = ask if ask > 0 else (last if last > 0 else 0.0)
        fill = base * (1 + slip)
        if limit_price is not None and limit_price > 0:
            fill = min(fill, limit_price)
        return fill
    # SELL
    base = bid if bid > 0 else (last if last > 0 else 0.0)
    fill = base * (1 - slip)
    if limit_price is not None and limit_price > 0:
        fill = max(fill, limit_price)
    return fill


class PaperExecutor:
    """Stateless paper-mode executor. All side effects go through journals."""

    async def execute(self, req: OrderRequest, bus: EventBus) -> None:
        # 1. Persist PENDING row first — guarantees we have an audit row even if quote/fill fails
        order_row_id = await insert_order_from_request(req, status=OrderStatus.PENDING)

        # 2. Quote
        try:
            last, bid, ask = await _fetch_quote(req.contract.tradingsymbol)
        except Exception as e:  # noqa: BLE001
            log.exception("paper_quote_failed", symbol=req.contract.tradingsymbol, error=str(e))
            await self._emit_update(bus, req, OrderStatus.REJECTED, 0, None, f"quote_failed: {e}")
            return

        # 3. Fill simulation
        limit_price = float(req.price) if req.price else None
        fill_price = _simulate_fill_price(req.side.value, last, bid, ask, limit_price)
        if fill_price <= 0:
            await self._emit_update(bus, req, OrderStatus.REJECTED, 0, None, "no quote available for fill")
            return

        fill_price_dec = Decimal(f"{fill_price:.2f}")

        # 4. Mark COMPLETE + open Trade
        await mark_order_filled(
            req.client_order_id,
            filled_qty=req.qty,
            avg_fill_price=fill_price_dec,
            broker_order_id=f"paper-{uuid.uuid4().hex[:12]}",
        )
        await open_trade(
            entry_order_id=order_row_id,
            strategy_id=req.strategy_id,
            tradingsymbol=req.contract.tradingsymbol,
            side=req.side.value,
            qty=req.qty,
            entry_price=fill_price_dec,
            opened_at=datetime.now(tz=timezone.utc),
        )

        log.info(
            "paper_order_filled",
            symbol=req.contract.tradingsymbol,
            side=req.side.value,
            qty=req.qty,
            fill_price=float(fill_price_dec),
            ltp=last, bid=bid, ask=ask,
        )

        await notify_fill(
            bus,
            tradingsymbol=req.contract.tradingsymbol,
            side=req.side.value,
            qty=req.qty,
            fill_price=fill_price_dec,
        )

        await self._emit_update(bus, req, OrderStatus.COMPLETE, req.qty, fill_price_dec, None)

    async def _emit_update(
        self,
        bus: EventBus,
        req: OrderRequest,
        status: OrderStatus,
        filled_qty: int,
        avg_fill_price: Decimal | None,
        rejection_reason: str | None,
    ) -> None:
        upd = OrderUpdate(
            event_id=str(uuid.uuid4()),
            ts=datetime.now(tz=timezone.utc),
            client_order_id=req.client_order_id,
            broker_order_id=None,
            status=status,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            rejection_reason=rejection_reason,
        )
        await bus.publish(STREAM_ORDER_UPDATES, upd)


async def run_executor_loop(
    bus: EventBus,
    executor: PaperExecutor | None = None,
    group: str = "paper-executor",
    consumer: str = "ex-1",
) -> None:
    executor = executor or PaperExecutor()
    stream = STREAM_ORDERS
    log.info("paper_executor_starting", stream=stream, mode=settings.mode)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=5):
        try:
            req = OrderRequest.model_validate(payload)
            if settings.mode == "live" and not settings.enable_live:
                log.warning("executor_live_blocked_enable_live_false")
            elif settings.mode in ("paper", "shadow"):
                await executor.execute(req, bus)
            else:
                log.warning("executor_unknown_mode", mode=settings.mode)
        except Exception as e:  # noqa: BLE001
            log.exception("paper_executor_error", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)
