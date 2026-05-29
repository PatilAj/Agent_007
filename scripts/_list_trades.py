"""Print every paper trade (open + closed) with P&L and a running total."""
import asyncio

import pytz
from sqlalchemy import text

from src.data.db import get_session

IST = pytz.timezone("Asia/Kolkata")


async def main() -> None:
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT id, strategy_id, tradingsymbol, side, qty, entry_price, "
            "exit_price, net_pnl, exit_reason, opened_at, closed_at, hold_seconds "
            "FROM trades ORDER BY id ASC"
        ))).fetchall()

    if not rows:
        print("No trades yet.")
        return

    header = (
        f"{'#':>3} {'strategy':<18} {'symbol':<22} {'side/qty':<9} "
        f"{'opened (IST)':<15} {'closed (IST)':<15} "
        f"{'entry':>8} {'exit':>8} {'pnl':>10} {'reason':<16} {'hold':>6}"
    )
    print(header)
    print("-" * len(header))

    running = 0.0
    wins = 0
    losses = 0
    open_count = 0
    for r in rows:
        opened = r.opened_at.astimezone(IST).strftime("%m-%d %H:%M")
        closed = r.closed_at.astimezone(IST).strftime("%m-%d %H:%M") if r.closed_at else "OPEN"
        pnl = float(r.net_pnl) if r.net_pnl is not None else 0.0
        exit_p = float(r.exit_price) if r.exit_price is not None else 0.0
        hold = f"{int(r.hold_seconds / 60)}m" if r.hold_seconds else "-"
        reason = r.exit_reason or "-"
        running += pnl
        if r.closed_at is None:
            open_count += 1
        elif pnl > 0:
            wins += 1
        else:
            losses += 1
        print(
            f"{r.id:>3} {r.strategy_id:<18} {r.tradingsymbol:<22} "
            f"{r.side + ' x' + str(r.qty):<9} {opened:<15} {closed:<15} "
            f"{float(r.entry_price):>8.2f} {exit_p:>8.2f} "
            f"{pnl:>+10.2f} {reason:<16} {hold:>6}"
        )
    print("-" * len(header))
    closed_count = wins + losses
    print(
        f"Trades: {len(rows)} ({closed_count} closed, {open_count} open) | "
        f"wins: {wins}  losses: {losses} | "
        f"Running net P&L: Rs {running:+,.2f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
