"""Token bucket rate limiter correctness."""
from __future__ import annotations

import asyncio
import time

import pytest

from src.broker.kite_client import TokenBucket


@pytest.mark.asyncio
async def test_burst_acquires_immediately():
    bucket = TokenBucket(rate_per_second=10, burst=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # 5 from burst → near-instant
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_acquires_throttle_after_burst():
    bucket = TokenBucket(rate_per_second=10, burst=2)
    start = time.monotonic()
    # 2 burst + 3 throttled @ 10/s → ~0.3s for last 3
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # at least some throttling
    assert elapsed < 1.0   # but not absurd


@pytest.mark.asyncio
async def test_concurrent_acquires_serialised():
    bucket = TokenBucket(rate_per_second=5, burst=1)

    async def one():
        await bucket.acquire()

    start = time.monotonic()
    await asyncio.gather(*[one() for _ in range(3)])
    elapsed = time.monotonic() - start
    # 1 burst + 2 @ 5/s → ~0.4s
    assert elapsed >= 0.3
