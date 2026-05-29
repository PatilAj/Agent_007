"""
Black-Scholes option pricing + the contract-input helpers a backtest needs.

Why synthetic pricing: we only recorded the NIFTY *spot* index, never the
option chain, so there is no historical premium series to replay. Black-Scholes
lets us model an ATM option's premium from (spot, strike, time-to-expiry, IV,
rate) and — crucially — how that premium evolves as spot moves and time decays.
Results are therefore *model-based estimates*, sensitive to the IV assumption,
not tick-exact fills. Good enough to compare strategies and judge edge.

All functions are pure and deterministic.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

import pytz

from src.core.events import OptionType

IST = pytz.timezone("Asia/Kolkata")

# Seconds in a (calendar) year — used to express time-to-expiry as a fraction.
_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0

# NIFTY option strikes are spaced 50 points apart.
DEFAULT_STRIKE_STEP = 50.0

# Indian risk-free proxy (~ short-term G-sec). Minor effect intraday.
DEFAULT_RISK_FREE = 0.065


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _intrinsic(spot: float, strike: float, opt: OptionType) -> float:
    if opt == OptionType.CE:
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def bs_price(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    opt: OptionType,
    *,
    rate: float = DEFAULT_RISK_FREE,
) -> float:
    """Black-Scholes European option price.

    t_years: time to expiry in years (>0). iv: annualized vol (e.g. 0.15).
    Degenerate inputs (expiry reached / zero vol) collapse to intrinsic value.
    """
    if spot <= 0 or strike <= 0:
        return 0.0
    if t_years <= 0 or iv <= 0:
        return _intrinsic(spot, strike, opt)

    vol_sqrt_t = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t_years) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    discount = math.exp(-rate * t_years)

    if opt == OptionType.CE:
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def atm_strike(spot: float, step: float = DEFAULT_STRIKE_STEP) -> float:
    """Nearest tradable strike to spot (ATM)."""
    return round(spot / step) * step


def year_fraction(now: datetime, expiry: datetime) -> float:
    """Time to expiry in years, floored at a tiny positive number."""
    secs = (expiry - now).total_seconds()
    return max(secs, 60.0) / _SECONDS_PER_YEAR  # floor at 1 min to keep BS sane


def next_weekly_expiry(
    now: datetime,
    *,
    weekday: int = 1,            # Mon=0 .. Sun=6; NIFTY weekly = Tuesday (1)
    expiry_time: time = time(15, 30),
) -> datetime:
    """The next weekly-expiry datetime (in IST) at or after `now`.

    If today is the expiry weekday but past the expiry time, roll to next week.
    """
    ist_now = now.astimezone(IST)
    days_ahead = (weekday - ist_now.weekday()) % 7
    candidate = ist_now.replace(
        hour=expiry_time.hour, minute=expiry_time.minute, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= ist_now:
        candidate += timedelta(days=7)
    return candidate
