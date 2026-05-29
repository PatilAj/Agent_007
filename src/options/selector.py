"""
Option contract selector.

Given a SignalCandidate that names the underlying ("NIFTY 50") and the
option type (CE/PE), pick the specific tradable contract:

  1. Map the trading-app underlying name to Kite's option chain `name` field
     ("NIFTY 50" → "NIFTY",  "NIFTY BANK" → "BANKNIFTY",  ...).
  2. Pick the desired expiry — "current_week" prefers the nearest expiry,
     but rolls to the next week if fewer than `switch_days_left` days remain
     (avoids tail-day theta blowups).
  3. Resolve current spot price via the most recent tick on the underlying.
  4. Pick the strike closest to spot (ATM). Delta-based selection is left
     for v2 once we have a Greeks source.

Returns a fully-populated `OptionContract` event from `src.core.events`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select, text

from src.broker.instrument_catalog import find_underlying_token, find_option_chain, get_distinct_expiries
from src.core.config import settings
from src.core.events import OptionContract, OptionType
from src.core.exceptions import DataError
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import Instrument

log = get_logger(__name__)


class OptionSelectionError(DataError):
    """No tradable option contract could be found for the signal."""


# Map the human-readable underlying names used in our config + signals
# to the `name` field Kite uses in its instruments dump for derivatives.
UNDERLYING_NAME_MAP: dict[str, str] = {
    "NIFTY 50": "NIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "NIFTY FIN SERVICE": "FINNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
}


def to_chain_name(underlying: str) -> str:
    """Translate to the chain `name` Kite uses; return as-is if unknown."""
    return UNDERLYING_NAME_MAP.get(underlying, underlying)


def pick_expiry(
    expiries: Iterable[datetime],
    *,
    now: datetime,
    preference: str = "current_week",
    switch_days_left: int = 2,
) -> datetime | None:
    """Pick the most appropriate expiry from a list, given a preference."""
    future = sorted([e for e in expiries if e > now])
    if not future:
        return None

    if preference == "current_week":
        # Take the nearest expiry, but skip to next if too close to expiry day
        nearest = future[0]
        days_left = (nearest - now).days
        if days_left < switch_days_left and len(future) > 1:
            return future[1]
        return nearest

    if preference == "next_week" and len(future) > 1:
        return future[1]

    return future[0]


def pick_strike(
    chain: list[Instrument],
    *,
    spot: float,
    option_type: OptionType,
) -> Instrument | None:
    """Pick the strike closest to spot (ATM) for the requested CE/PE."""
    candidates = [c for c in chain if c.instrument_type == option_type.value and c.strike is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(float(c.strike) - spot))  # type: ignore[arg-type]


async def get_spot_price(underlying_token: int) -> float | None:
    """Most recent LTP from our ticks table for the given spot/index token."""
    async with get_session() as s:
        result = await s.execute(
            text(
                "SELECT ltp FROM ticks "
                "WHERE instrument_token = :tok "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"tok": underlying_token},
        )
        row = result.first()
        return float(row[0]) if row else None


async def select_option_for_signal(
    underlying: str,
    option_type: OptionType,
    *,
    now: datetime | None = None,
) -> OptionContract:
    """End-to-end selection — used by the strategy runner."""
    now = now or datetime.now(tz=timezone.utc)
    chain_name = to_chain_name(underlying)
    cfg = settings.option_selector

    # 1. Spot price (needed for ATM selection)
    underlying_token = await find_underlying_token(underlying, exchange="NSE")
    if underlying_token is None:
        raise OptionSelectionError(f"Underlying {underlying!r} not found in instrument catalog")

    spot = await get_spot_price(underlying_token)
    if spot is None or spot <= 0:
        raise OptionSelectionError(
            f"No spot tick available for {underlying!r} (token={underlying_token}). "
            "Ingestor may not have run yet."
        )

    # 2. Available expiries
    expiries = await get_distinct_expiries(chain_name)
    expiry = pick_expiry(
        expiries,
        now=now,
        preference=cfg.expiry_preference,
        switch_days_left=cfg.switch_to_next_week_days_left,
    )
    if expiry is None:
        raise OptionSelectionError(
            f"No future expiries available for {chain_name!r}. Instrument catalog stale?"
        )

    # 3. Full option chain for the chosen expiry, then pick strike
    chain = await find_option_chain(chain_name, expiry=expiry)
    if not chain:
        raise OptionSelectionError(f"Empty option chain for {chain_name!r} expiry {expiry}")

    picked = pick_strike(chain, spot=spot, option_type=option_type)
    if picked is None:
        raise OptionSelectionError(
            f"No {option_type.value} strikes near spot {spot} for {chain_name!r} expiry {expiry}"
        )

    log.info(
        "option_selected",
        underlying=underlying,
        chain=chain_name,
        spot=spot,
        expiry=expiry.isoformat(),
        strike=float(picked.strike) if picked.strike is not None else None,
        symbol=picked.tradingsymbol,
        option_type=option_type.value,
    )

    return OptionContract(
        instrument_token=picked.instrument_token,
        tradingsymbol=picked.tradingsymbol,
        underlying=underlying,
        expiry=picked.expiry,  # type: ignore[arg-type]
        strike=picked.strike,  # type: ignore[arg-type]
        option_type=option_type,
        lot_size=picked.lot_size,
    )
