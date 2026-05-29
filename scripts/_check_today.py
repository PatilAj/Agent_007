"""Snapshot of today's agent state: data freshness + opening-range coverage + signals."""
import asyncio
from datetime import datetime, timezone

import pytz
from sqlalchemy import text

from src.data.db import get_session

IST = pytz.timezone("Asia/Kolkata")


async def main() -> None:
    today_ist = datetime.now(tz=timezone.utc).astimezone(IST).date()
    today_midnight_utc = IST.localize(datetime.combine(today_ist, datetime.min.time())).astimezone(timezone.utc)

    async with get_session() as s:
        latest = (await s.execute(text(
            "SELECT max(ts) FROM candles WHERE instrument_token=256265 AND resolution='1minute'"
        ))).scalar()
        first_today = (await s.execute(text(
            "SELECT min(ts) FROM candles WHERE instrument_token=256265 "
            "AND resolution='1minute' AND ts >= :t"
        ), {"t": today_midnight_utc})).scalar()
        sigs_today = (await s.execute(text(
            "SELECT count(*) FROM signals WHERE ts >= :t"
        ), {"t": today_midnight_utc})).scalar()
        rejs = (await s.execute(text(
            "SELECT context->>'reason' AS reason, count(*) FROM risk_events "
            "WHERE ts >= :t AND action='rejected' GROUP BY reason ORDER BY 2 DESC LIMIT 5"
        ), {"t": today_midnight_utc})).fetchall()
        latest_tick = (await s.execute(text(
            "SELECT max(ts) FROM ticks WHERE instrument_token=256265"
        ))).scalar()
        ticks_today = (await s.execute(text(
            "SELECT count(*) FROM ticks WHERE instrument_token=256265 AND ts >= :t"
        ), {"t": today_midnight_utc})).scalar()

    def fmt(t):
        return t.astimezone(IST).strftime("%H:%M:%S") if t else "(none)"

    print(f"now           : {datetime.now(tz=timezone.utc).astimezone(IST):%H:%M:%S} IST  {today_ist}")
    print(f"latest 1m     : {fmt(latest)} IST   (freshness check)")
    print(f"latest tick   : {fmt(latest_tick)} IST   (WSS health)")
    print(f"first today   : {fmt(first_today)} IST   (ORB needs <= 09:16 to arm)")
    print(f"ticks today   : {ticks_today}")
    print(f"signals today : {sigs_today}")
    if rejs:
        print("rejections today:")
        for reason, n in rejs:
            print(f"  {n:>3}  {reason}")


if __name__ == "__main__":
    asyncio.run(main())
