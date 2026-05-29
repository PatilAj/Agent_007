"""
Unit tests for the backtest Black-Scholes pricing + contract-input helpers.

Reference values are the standard textbook case:
  S=100, K=100, T=1y, sigma=20%, r=5%  ->  Call ~= 10.4506,  Put ~= 5.5735
"""
from __future__ import annotations

import math
from datetime import datetime, time

import pytz

from src.backtest.option_pricing import (
    atm_strike,
    bs_price,
    next_weekly_expiry,
    year_fraction,
)
from src.core.events import OptionType

IST = pytz.timezone("Asia/Kolkata")


def test_bs_reference_call_and_put():
    call = bs_price(100, 100, 1.0, 0.20, OptionType.CE, rate=0.05)
    put = bs_price(100, 100, 1.0, 0.20, OptionType.PE, rate=0.05)
    assert abs(call - 10.4506) < 0.01
    assert abs(put - 5.5735) < 0.01


def test_put_call_parity():
    s, k, t, iv, r = 100.0, 95.0, 0.5, 0.25, 0.06
    call = bs_price(s, k, t, iv, OptionType.CE, rate=r)
    put = bs_price(s, k, t, iv, OptionType.PE, rate=r)
    # C - P == S - K e^{-rT}
    assert abs((call - put) - (s - k * math.exp(-r * t))) < 1e-9


def test_intrinsic_at_expiry():
    assert bs_price(110, 100, 0.0, 0.2, OptionType.CE) == 10.0
    assert bs_price(90, 100, 0.0, 0.2, OptionType.PE) == 10.0
    assert bs_price(90, 100, 0.0, 0.2, OptionType.CE) == 0.0


def test_zero_vol_collapses_to_intrinsic():
    assert bs_price(110, 100, 1.0, 0.0, OptionType.CE) == 10.0


def test_call_increases_with_spot():
    lo = bs_price(100, 100, 0.1, 0.15, OptionType.CE)
    hi = bs_price(105, 100, 0.1, 0.15, OptionType.CE)
    assert hi > lo


def test_atm_strike_rounds_to_50():
    assert atm_strike(23967) == 23950
    assert atm_strike(23926) == 23950
    assert atm_strike(24024) == 24000


def test_year_fraction_positive_and_floored():
    now = IST.localize(datetime(2026, 5, 27, 10, 0))
    later = IST.localize(datetime(2026, 5, 27, 15, 30))
    yf = year_fraction(now, later)
    assert yf > 0
    # past expiry is floored, never negative
    assert year_fraction(later, now) > 0


def test_next_weekly_expiry_is_tuesday_1530():
    # Wednesday 2026-05-27 10:00 IST -> next Tuesday 2026-06-02 15:30 IST
    now = IST.localize(datetime(2026, 5, 27, 10, 0))
    exp = next_weekly_expiry(now, weekday=1, expiry_time=time(15, 30))
    assert exp.weekday() == 1  # Tuesday
    assert (exp.hour, exp.minute) == (15, 30)
    assert exp > now


def test_next_weekly_expiry_rolls_after_cutoff():
    # Tuesday after 15:30 -> next week's Tuesday
    tue_late = IST.localize(datetime(2026, 6, 2, 16, 0))
    exp = next_weekly_expiry(tue_late, weekday=1, expiry_time=time(15, 30))
    assert exp.weekday() == 1
    assert (exp - tue_late).days >= 6
