"""
Position tracker — current state of the trading book.

All values are derived on demand from canonical DB tables (orders, trades,
daily_pnl) — we deliberately avoid an in-memory cache that could drift from
DB after a crash/restart. Read-mostly workload, so DB latency is fine.

Methods used by the risk engine:
  - open_positions()         : Currently open trades (entry filled, no exit)
  - count_open()             : Quick gate for max_concurrent_positions
  - daily_pnl(today_ist)     : Realized P&L so far today
  - trade_count_today()      : For max_trades_per_day
  - consecutive_losses()     : For max_consecutive_losses cooldown
  - is_position_open_for(instrument_token): Avoid duplicate entries
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytz
from sqlalchemy import select, func

from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import DailyPnL, Trade

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=True)
class OpenPosition:
    trade_id: int
    instrument_token: int
    tradingsymbol: str
    side: str
    qty: int
    entry_price: Decimal
    opened_at: datetime
    strategy_id: str


def _today_ist_date(now: datetime | None = None) -> date:
    now = now or datetime.now(tz=timezone.utc)
    return now.astimezone(IST).date()


class PositionTracker:
    """Read-side view of orders/trades for the risk engine."""

    async def open_positions(self) -> list[OpenPosition]:
        async with get_session() as s:
            stmt = select(Trade).where(Trade.closed_at.is_(None)).order_by(Trade.opened_at.desc())
            rows = (await s.execute(stmt)).scalars().all()
        return [
            OpenPosition(
                trade_id=r.id,
                instrument_token=0,  # Trade row doesn't store token; resolved via tradingsymbol later if needed
                tradingsymbol=r.tradingsymbol,
                side=r.side,
                qty=r.qty,
                entry_price=r.entry_price,
                opened_at=r.opened_at,
                strategy_id=r.strategy_id,
            )
            for r in rows
        ]

    async def count_open(self) -> int:
        async with get_session() as s:
            stmt = select(func.count(Trade.id)).where(Trade.closed_at.is_(None))
            return int((await s.execute(stmt)).scalar_one())

    async def is_position_open_for(self, tradingsymbol: str) -> bool:
        async with get_session() as s:
            stmt = (
                select(func.count(Trade.id))
                .where(Trade.closed_at.is_(None))
                .where(Trade.tradingsymbol == tradingsymbol)
            )
            return int((await s.execute(stmt)).scalar_one()) > 0

    async def _today_row(self, now: datetime | None = None) -> DailyPnL | None:
        today = _today_ist_date(now)
        start = datetime.combine(today, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
        end = datetime.combine(today, datetime.max.time(), tzinfo=IST).astimezone(timezone.utc)
        async with get_session() as s:
            stmt = select(DailyPnL).where(
                DailyPnL.trade_date >= start, DailyPnL.trade_date <= end
            )
            return (await s.execute(stmt)).scalar_one_or_none()

    async def daily_pnl(self, now: datetime | None = None) -> Decimal:
        row = await self._today_row(now)
        return row.net_pnl if row else Decimal("0")

    async def trade_count_today(self, now: datetime | None = None) -> int:
        row = await self._today_row(now)
        return int(row.trade_count) if row else 0

    async def consecutive_losses(self, now: datetime | None = None) -> int:
        row = await self._today_row(now)
        return int(row.consecutive_losses) if row else 0


# Module-level shared instance — cheap (no state, all reads go to DB)
_tracker_singleton: PositionTracker | None = None


def get_position_tracker() -> PositionTracker:
    global _tracker_singleton
    if _tracker_singleton is None:
        _tracker_singleton = PositionTracker()
    return _tracker_singleton
