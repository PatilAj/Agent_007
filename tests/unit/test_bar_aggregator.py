"""
Bar aggregator tests.

These tests verify the most error-prone code in the whole pipeline:
correct bar boundary alignment, OHLC tracking, volume diffing, and the
"new bar opens / old bar closes" transition.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytz

from src.core.events import TickEvent
from src.data.bar_aggregator import BarAggregator, floor_to_resolution

IST = pytz.timezone("Asia/Kolkata")


def _ist(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return IST.localize(datetime(year, month, day, hour, minute, second)).astimezone(timezone.utc)


def _tick(ts: datetime, ltp: float, volume: int | None = None, token: int = 256265) -> TickEvent:
    return TickEvent(
        event_id=str(uuid.uuid4()),
        ts=ts,
        instrument_token=token,
        symbol="NIFTY 50",
        ltp=Decimal(str(ltp)),
        volume=volume,
    )


# ----------------------------- floor_to_resolution -----------------------------

class TestFloorToResolution:
    def test_floor_to_5min_at_932_returns_930(self):
        ts = _ist(2026, 5, 19, 9, 32, 17)
        f = floor_to_resolution(ts, 5)
        assert f == _ist(2026, 5, 19, 9, 30)

    def test_floor_to_1min_at_932_55_returns_932(self):
        ts = _ist(2026, 5, 19, 9, 32, 55)
        f = floor_to_resolution(ts, 1)
        assert f == _ist(2026, 5, 19, 9, 32)

    def test_floor_to_15min_at_947_returns_945(self):
        ts = _ist(2026, 5, 19, 9, 47)
        f = floor_to_resolution(ts, 15)
        assert f == _ist(2026, 5, 19, 9, 45)

    def test_floor_to_5min_at_market_open_returns_915(self):
        ts = _ist(2026, 5, 19, 9, 15, 1)
        f = floor_to_resolution(ts, 5)
        assert f == _ist(2026, 5, 19, 9, 15)


# ----------------------------- BarAggregator -----------------------------

@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.publish = AsyncMock(return_value="msg-1")
    return bus


@pytest.mark.asyncio
async def test_first_tick_opens_bar(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    closed = await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0))
    assert closed == []
    assert agg.open_bar_count == 1
    assert agg.closed_count == 0


@pytest.mark.asyncio
async def test_ticks_in_same_bar_update_hlc(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 20), 102.0))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 40), 99.5))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 55), 101.0))
    # No close yet — still in 9:30 bar
    assert agg.closed_count == 0
    assert agg.open_bar_count == 1


@pytest.mark.asyncio
async def test_tick_in_next_bar_closes_previous(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 30), 101.0))
    # Cross into 9:31
    closed = await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 31, 2), 102.0))
    assert len(closed) == 1
    bar = closed[0]
    assert bar.o == Decimal("100.0")
    assert bar.h == Decimal("101.0")
    assert bar.l == Decimal("100.0")
    assert bar.c == Decimal("101.0")
    # Bar close time = bar_start + 1min = 9:31
    assert bar.bar_ts == _ist(2026, 5, 19, 9, 31)


@pytest.mark.asyncio
async def test_ohlc_with_full_range(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["5minute"])
    # bar 9:30–9:35
    prices = [100, 105, 95, 102, 98, 103]
    for i, p in enumerate(prices):
        await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, i * 5), float(p)))
    # cross into 9:35 bar
    closed = await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 35, 1), 110.0))
    assert len(closed) == 1
    bar = closed[0]
    assert bar.o == Decimal("100")
    assert bar.h == Decimal("105")
    assert bar.l == Decimal("95")
    assert bar.c == Decimal("103")


@pytest.mark.asyncio
async def test_multiple_resolutions_track_independently(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute", "5minute"])
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 30), 101.0))
    # Cross 1-min boundary but NOT 5-min
    closed = await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 31, 5), 102.0))
    # Only the 1-min should close
    assert len(closed) == 1
    assert closed[0].resolution == "1minute"
    # 5-min still open — we have 1m and 5m active
    assert agg.open_bar_count == 2


@pytest.mark.asyncio
async def test_volume_diffs_correctly(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    # Kite gives cumulative day-volume
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0, volume=10000))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 30), 101.0, volume=10500))
    closed = await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 31, 5), 102.0, volume=10800))
    # Bar 9:30 saw cumulative go from 10000 → 10500 = delta 500
    assert closed[0].v == 500


@pytest.mark.asyncio
async def test_separate_tokens_have_separate_bars(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0, token=1))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 200.0, token=2))
    assert agg.open_bar_count == 2  # two independent in-progress bars


@pytest.mark.asyncio
async def test_bus_publish_called_on_close(mock_bus):
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 31, 5), 102.0))
    mock_bus.publish.assert_called_once()
    args, kwargs = mock_bus.publish.call_args
    assert args[0] == "stream:bars.1minute"


@pytest.mark.asyncio
async def test_low_price_becomes_new_low(mock_bus):
    """Regression: tick lower than current low must update low."""
    agg = BarAggregator(mock_bus, resolutions=["1minute"])
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 5), 100.0))
    await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 30, 30), 95.0))   # new low
    closed = await agg.handle_tick(_tick(_ist(2026, 5, 19, 9, 31, 5), 96.0))
    assert closed[0].l == Decimal("95.0")
