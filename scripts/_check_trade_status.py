"""Show live status + unrealized P&L of the most recent trade. Diagnostic only."""
import asyncio

import pytz
from sqlalchemy import desc, select

from src.broker.kite_client import get_kite_client
from src.data.db import get_session
from src.data.models import Trade

IST = pytz.timezone("Asia/Kolkata")
SL_PCT = 30.0
TP_PCT = 60.0


async def _ltp(symbol: str) -> float:
    kite = get_kite_client()
    key = f"NFO:{symbol}"
    q = await kite.ltp([key])
    row = q.get(key) or next(iter(q.values()), {})
    return float(row.get("last_price") or 0)


async def main() -> None:
    async with get_session() as s:
        t = (await s.execute(select(Trade).order_by(desc(Trade.id)).limit(1))).scalar_one_or_none()
    if t is None:
        print("No trades.")
        return

    entry = float(t.entry_price)
    print(f"Trade #{t.id}  {t.tradingsymbol}  {t.side} x{t.qty}")
    print(f"  entry      : Rs {entry}")

    if t.closed_at is not None:
        exitp = float(t.exit_price) if t.exit_price else 0.0
        print(f"  CLOSED     : exit Rs {exitp}  reason={t.exit_reason}")
        print(f"  realized   : Rs {float(t.net_pnl or 0):,.2f}")
        return

    ltp = await _ltp(t.tradingsymbol)
    pnl_pct = (ltp - entry) / entry * 100 if entry else 0
    unrealized = (ltp - entry) * t.qty
    sl_price = entry * (1 - SL_PCT / 100)
    tp_price = entry * (1 + TP_PCT / 100)
    print(f"  status     : OPEN")
    print(f"  live LTP   : Rs {ltp:.2f}   ({pnl_pct:+.1f}% vs entry)")
    print(f"  unrealized : Rs {unrealized:+,.2f}")
    print(f"  SL @ Rs {sl_price:.2f} (-{SL_PCT:.0f}%)   TP @ Rs {tp_price:.2f} (+{TP_PCT:.0f}%)   square-off 15:15 IST")


if __name__ == "__main__":
    asyncio.run(main())
