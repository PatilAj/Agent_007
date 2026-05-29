"""
Instrument catalog.

Every trading day, Kite publishes a CSV of all tradable instruments with
fresh tokens, lot sizes, expiry dates, etc. We download this once a day
(pre-market) and upsert into the `instruments` table.

Lot sizes can change at expiry rollover. Never hardcode them.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

import pytz
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.broker.kite_client import get_kite_client
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import Instrument

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


# Exchanges we care about (skip CDS, MCX commodities for now)
TRACKED_EXCHANGES = {"NSE", "NFO", "BSE", "BFO"}


def _parse_expiry(raw: Any) -> datetime | None:
    if raw in (None, "", "1970-01-01"):
        return None
    if isinstance(raw, datetime):
        # Kite SDK occasionally returns naive datetimes — localize to IST.
        return IST.localize(raw).astimezone(timezone.utc) if raw.tzinfo is None else raw
    # date BUT NOT datetime: pykiteconnect returns datetime.date for option expiry.
    # Must be checked AFTER the datetime branch since datetime is a subclass of date.
    if isinstance(raw, date):
        dt = datetime.combine(raw, time(0, 0))
        return IST.localize(dt).astimezone(timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
            return IST.localize(dt).astimezone(timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            return None
    return None


def _derive_underlying(row: dict[str, Any]) -> str | None:
    """For options/futures, the underlying symbol can be derived from `name`."""
    itype = row.get("instrument_type", "")
    if itype in {"CE", "PE", "FUT"}:
        # Kite uses `name` like "NIFTY" or "RELIANCE" for the underlying
        return row.get("name") or None
    if itype == "EQ":
        return row.get("tradingsymbol")
    return None


async def refresh_instrument_catalog() -> int:
    """Fetch full Kite instrument dump and upsert into Postgres.

    Returns the number of rows ingested.
    """
    client = get_kite_client()
    log.info("instruments_fetch_start")
    rows: list[dict[str, Any]] = await client.instruments()
    log.info("instruments_fetch_done", row_count=len(rows))

    filtered = [r for r in rows if r.get("exchange") in TRACKED_EXCHANGES]
    log.info("instruments_filtered", kept=len(filtered), total=len(rows))

    upsert_batch: list[dict[str, Any]] = []
    for r in filtered:
        upsert_batch.append(
            {
                "instrument_token": int(r["instrument_token"]),
                "exchange_token": int(r.get("exchange_token") or 0),
                "tradingsymbol": r["tradingsymbol"],
                "name": r.get("name"),
                "exchange": r["exchange"],
                "segment": r.get("segment") or r["exchange"],
                "instrument_type": r.get("instrument_type") or "EQ",
                "expiry": _parse_expiry(r.get("expiry")),
                "strike": Decimal(str(r["strike"])) if r.get("strike") else None,
                "lot_size": int(r.get("lot_size") or 1),
                "tick_size": float(r.get("tick_size") or 0.05),
                "underlying": _derive_underlying(r),
                "fetched_at": datetime.now(tz=timezone.utc),
            }
        )

    if not upsert_batch:
        log.warning("instruments_empty_batch")
        return 0

    # Chunked upsert to avoid Postgres' 32767 bind-parameter cap per statement.
    # 13 columns per row, so CHUNK * 13 must stay under 32767. 2000 keeps margin.
    CHUNK = 2000
    inserted = 0
    async with get_session() as session:
        # We do full replace for simplicity: tokens are reused across days
        # but old rows can be stale (expired contracts). Wipe + reinsert.
        await session.execute(delete(Instrument))
        for i in range(0, len(upsert_batch), CHUNK):
            chunk = upsert_batch[i : i + CHUNK]
            stmt = pg_insert(Instrument).values(chunk)
            # On conflict (re-run in same day), update fields
            stmt = stmt.on_conflict_do_update(
                index_elements=["instrument_token"],
                set_={
                    "tradingsymbol": stmt.excluded.tradingsymbol,
                    "lot_size": stmt.excluded.lot_size,
                    "expiry": stmt.excluded.expiry,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            await session.execute(stmt)
            inserted += len(chunk)
    log.info("instruments_upsert_done", rows=inserted)
    return inserted


async def find_underlying_token(symbol: str, exchange: str = "NSE") -> int | None:
    """Find the instrument_token for a spot/index symbol."""
    async with get_session() as s:
        stmt = (
            select(Instrument.instrument_token)
            .where(Instrument.tradingsymbol == symbol)
            .where(Instrument.exchange == exchange)
            .limit(1)
        )
        return (await s.execute(stmt)).scalar_one_or_none()


async def find_option_chain(
    underlying: str,
    expiry: datetime | None = None,
) -> list[Instrument]:
    """Get all CE/PE rows for an underlying (and specific expiry if given)."""
    async with get_session() as s:
        stmt = select(Instrument).where(Instrument.underlying == underlying).where(
            Instrument.instrument_type.in_(["CE", "PE"])
        )
        if expiry is not None:
            stmt = stmt.where(Instrument.expiry == expiry)
        stmt = stmt.order_by(Instrument.expiry, Instrument.strike, Instrument.instrument_type)
        return list((await s.execute(stmt)).scalars().all())


async def get_distinct_expiries(underlying: str) -> list[datetime]:
    async with get_session() as s:
        stmt = (
            select(Instrument.expiry)
            .where(Instrument.underlying == underlying)
            .where(Instrument.instrument_type.in_(["CE", "PE"]))
            .distinct()
            .order_by(Instrument.expiry)
        )
        result = (await s.execute(stmt)).scalars().all()
        return [e for e in result if e is not None]


async def count_instruments() -> int:
    from sqlalchemy import func

    async with get_session() as s:
        return (await s.execute(select(func.count(Instrument.instrument_token)))).scalar_one()
