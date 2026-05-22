"""
Clock abstraction.

Two implementations:
- WallClock: real-time, used in live/paper/shadow modes.
- SimulatedClock: deterministic time, used in backtests and tests.

Always go through the clock; never call datetime.utcnow() directly in business
logic, or your code won't be back-testable.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    pass

IST = pytz.timezone("Asia/Kolkata")
UTC = pytz.UTC

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Return current time in UTC, tz-aware."""

    def now_ist(self) -> datetime:
        return self.now().astimezone(IST)

    def is_market_open(self) -> bool:
        n = self.now_ist()
        if n.weekday() >= 5:  # Sat/Sun
            return False
        t = n.time()
        return MARKET_OPEN <= t <= MARKET_CLOSE

    def seconds_until_market_open(self) -> float:
        n = self.now_ist()
        # Next market open
        target_day = n.date()
        target = IST.localize(datetime.combine(target_day, MARKET_OPEN))
        if n >= target:
            target = target + timedelta(days=1)
        # skip weekends
        while target.weekday() >= 5:
            target = target + timedelta(days=1)
        return (target - n).total_seconds()


class WallClock(Clock):
    """Real-time clock."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class SimulatedClock(Clock):
    """Deterministic clock for backtests and tests."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            start = UTC.localize(start)
        self._now = start.astimezone(UTC)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta) -> None:
        with self._lock:
            self._now = self._now + delta

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            when = UTC.localize(when)
        with self._lock:
            self._now = when.astimezone(UTC)


# default global clock — overridable in tests via dependency injection
_default: Clock = WallClock()


def get_clock() -> Clock:
    return _default


def set_clock(clock: Clock) -> None:
    """Replace the global clock. Use only in tests or backtest harness."""
    global _default
    _default = clock
