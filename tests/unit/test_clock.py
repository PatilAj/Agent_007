"""Tests for the clock abstraction."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytz

from src.core.clock import IST, SimulatedClock, WallClock


def test_wall_clock_returns_utc_tz_aware():
    c = WallClock()
    n = c.now()
    assert n.tzinfo is not None
    assert n.utcoffset() == timedelta(0)


def test_wall_clock_ist_conversion():
    c = WallClock()
    ist = c.now_ist()
    assert ist.tzinfo is not None
    # IST offset is +5:30
    assert ist.utcoffset() == timedelta(hours=5, minutes=30)


def test_simulated_clock_deterministic():
    start = datetime(2026, 5, 19, 9, 30, tzinfo=pytz.UTC)
    c = SimulatedClock(start)
    assert c.now() == start
    c.advance(timedelta(minutes=5))
    assert c.now() == start + timedelta(minutes=5)


def test_simulated_clock_market_open():
    # 09:30 IST on a Tuesday → 04:00 UTC → market is open
    ist_time = IST.localize(datetime(2026, 5, 19, 9, 30))
    c = SimulatedClock(ist_time.astimezone(timezone.utc))
    assert c.is_market_open() is True


def test_simulated_clock_market_closed_weekend():
    # Saturday 10 AM IST
    ist_time = IST.localize(datetime(2026, 5, 23, 10, 0))
    c = SimulatedClock(ist_time.astimezone(timezone.utc))
    assert c.is_market_open() is False


def test_simulated_clock_market_closed_after_hours():
    # 18:00 IST weekday
    ist_time = IST.localize(datetime(2026, 5, 19, 18, 0))
    c = SimulatedClock(ist_time.astimezone(timezone.utc))
    assert c.is_market_open() is False


def test_simulated_clock_set_replaces_time():
    c = SimulatedClock(datetime(2026, 1, 1, tzinfo=pytz.UTC))
    new_time = datetime(2026, 5, 19, 12, 0, tzinfo=pytz.UTC)
    c.set(new_time)
    assert c.now() == new_time


def test_seconds_until_market_open_returns_positive():
    # 06:00 IST weekday → market opens at 09:15 IST → ~3h15m = ~11700s
    ist_time = IST.localize(datetime(2026, 5, 19, 6, 0))
    c = SimulatedClock(ist_time.astimezone(timezone.utc))
    secs = c.seconds_until_market_open()
    assert 0 < secs <= 4 * 3600
