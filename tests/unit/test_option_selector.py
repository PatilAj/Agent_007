"""
Unit tests for the option selector — pure helpers only.

The async `select_option_for_signal` orchestrator needs DB + ticks, so we
test it via integration later. Here we lock down the synchronous helpers
that decide *which* expiry and *which* strike to pick.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.core.events import OptionType
from src.options.selector import (
    UNDERLYING_NAME_MAP,
    pick_expiry,
    pick_strike,
    to_chain_name,
)


@dataclass
class FakeInstrument:
    """Minimal stand-in for `src.data.models.Instrument` rows."""

    instrument_type: str
    strike: Decimal | None


def now_utc() -> datetime:
    return datetime(2026, 5, 22, 4, 0, tzinfo=timezone.utc)


# ----------------- to_chain_name -----------------


def test_to_chain_name_maps_known_underlyings():
    assert to_chain_name("NIFTY 50") == "NIFTY"
    assert to_chain_name("NIFTY BANK") == "BANKNIFTY"
    assert to_chain_name("NIFTY FIN SERVICE") == "FINNIFTY"


def test_to_chain_name_returns_unknown_as_is():
    assert to_chain_name("SOMETHING_NEW") == "SOMETHING_NEW"


def test_all_underlyings_in_config_have_mappings():
    """Catch the case where a new underlying is added to config but not mapped."""
    expected = {"NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE"}
    assert expected.issubset(UNDERLYING_NAME_MAP.keys())


# ----------------- pick_expiry -----------------


def test_pick_expiry_returns_none_when_all_past():
    now = now_utc()
    expiries = [now - timedelta(days=1), now - timedelta(days=8)]
    assert pick_expiry(expiries, now=now) is None


def test_pick_expiry_picks_nearest_when_far_enough():
    now = now_utc()
    near = now + timedelta(days=5)
    far = now + timedelta(days=12)
    assert pick_expiry([near, far], now=now, switch_days_left=2) == near


def test_pick_expiry_rolls_to_next_when_too_close():
    """If <2 days to expiry, prefer the next one to avoid theta blowup."""
    now = now_utc()
    near = now + timedelta(days=1)   # only 1 day left
    next_one = now + timedelta(days=8)
    picked = pick_expiry([near, next_one], now=now, switch_days_left=2)
    assert picked == next_one


def test_pick_expiry_keeps_near_when_only_one_available():
    """If there's no fallback, take what we have even if close."""
    now = now_utc()
    near = now + timedelta(hours=12)
    assert pick_expiry([near], now=now, switch_days_left=2) == near


def test_pick_expiry_next_week_preference():
    now = now_utc()
    nearest = now + timedelta(days=3)
    after = now + timedelta(days=10)
    assert (
        pick_expiry([nearest, after], now=now, preference="next_week") == after
    )


# ----------------- pick_strike -----------------


def test_pick_strike_returns_none_for_empty_chain():
    assert pick_strike([], spot=25000.0, option_type=OptionType.CE) is None


def test_pick_strike_picks_closest_to_spot():
    chain = [
        FakeInstrument(instrument_type="CE", strike=Decimal("24900")),
        FakeInstrument(instrument_type="CE", strike=Decimal("25000")),
        FakeInstrument(instrument_type="CE", strike=Decimal("25100")),
    ]
    picked = pick_strike(chain, spot=25040.0, option_type=OptionType.CE)
    assert picked is not None
    assert picked.strike == Decimal("25000")


def test_pick_strike_filters_by_option_type():
    chain = [
        FakeInstrument(instrument_type="CE", strike=Decimal("24900")),
        FakeInstrument(instrument_type="PE", strike=Decimal("25000")),  # PE — should be ignored
        FakeInstrument(instrument_type="CE", strike=Decimal("25100")),
    ]
    picked = pick_strike(chain, spot=25000.0, option_type=OptionType.CE)
    assert picked is not None
    assert picked.instrument_type == "CE"
    # Of the two CEs, 25100 is closer to 25000? abs(24900-25000)=100, abs(25100-25000)=100. Tie.
    # min() keeps the first → 24900. Either is acceptable; assert it's a CE.
    assert picked.strike in (Decimal("24900"), Decimal("25100"))


def test_pick_strike_ignores_rows_with_no_strike():
    chain = [
        FakeInstrument(instrument_type="CE", strike=None),
        FakeInstrument(instrument_type="CE", strike=Decimal("25000")),
    ]
    picked = pick_strike(chain, spot=25000.0, option_type=OptionType.CE)
    assert picked is not None
    assert picked.strike == Decimal("25000")
