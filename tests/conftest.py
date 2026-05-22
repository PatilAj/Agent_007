"""Pytest configuration and shared fixtures."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
import pytz


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(tz=pytz.UTC)


@pytest.fixture
def ist_now() -> datetime:
    return datetime.now(tz=pytz.timezone("Asia/Kolkata"))


@pytest.fixture(scope="session")
def event_loop():
    """Shared event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
