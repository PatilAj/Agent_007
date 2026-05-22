"""
Async wrapper around the Kite Connect REST SDK.

The official `kiteconnect.KiteConnect` is synchronous. We wrap it with
`asyncio.to_thread` so callers don't block the event loop, and bolt on:
  - rate limiting (token bucket, ~8 req/s — leave headroom under 10/s)
  - exponential-backoff retries on 5xx + network errors
  - structured logging for every call
  - typed return values for the calls we actually use

This wrapper deliberately does NOT expose the entire surface. Only the methods
the agent needs are wrapped, so we keep audit small and predictable.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.auth.kite_session import ensure_valid_token
from src.core.config import settings
from src.core.exceptions import (
    BrokerError,
    BrokerNetworkError,
    BrokerPermissionError,
    BrokerRateLimitError,
    TokenExpiredError,
)
from src.core.logging import get_logger

log = get_logger(__name__)


class TokenBucket:
    """Simple token bucket for rate limiting."""

    def __init__(self, rate_per_second: float, burst: int):
        self.rate = rate_per_second
        self.capacity = burst
        self.tokens = float(burst)
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


# 8 req/s with burst of 10 — under Kite's 10/s ceiling
_bucket = TokenBucket(rate_per_second=8.0, burst=10)


class KiteClient:
    """Singleton wrapper around the official Kite SDK."""

    def __init__(self) -> None:
        self._kite: Any | None = None
        self._access_token: str | None = None

    async def _ensure_ready(self) -> Any:
        if self._kite and self._access_token:
            return self._kite

        from kiteconnect import KiteConnect

        api_key = settings.kite_api_key.get_secret_value()
        token = await ensure_valid_token()
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)

        self._kite = kite
        self._access_token = token
        return kite

    def invalidate(self) -> None:
        """Force re-login on next call (e.g. after a TokenExpired error)."""
        self._kite = None
        self._access_token = None

    async def _call(self, fn_name: str, *args: Any, **kwargs: Any) -> Any:
        await _bucket.acquire()
        kite = await self._ensure_ready()
        fn: Callable[..., Any] = getattr(kite, fn_name)

        retryer = AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((BrokerNetworkError, BrokerRateLimitError)),
            reraise=True,
        )

        async for attempt in retryer:
            with attempt:
                try:
                    return await asyncio.to_thread(fn, *args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    self._classify_and_raise(fn_name, e)
        return None  # unreachable

    def _classify_and_raise(self, fn_name: str, e: Exception) -> None:
        """Convert SDK errors into our typed exception hierarchy."""
        cls_name = type(e).__name__
        msg = str(e)

        # kiteconnect-specific exception names
        if cls_name == "TokenException" or "Incorrect `api_key`" in msg or "Invalid `access_token`" in msg:
            self.invalidate()
            log.error("kite_token_expired", fn=fn_name, error=msg)
            raise TokenExpiredError(msg) from e
        if cls_name == "NetworkException":
            log.warning("kite_network_error", fn=fn_name, error=msg)
            raise BrokerNetworkError(msg) from e
        if "Too many requests" in msg:
            log.warning("kite_rate_limit", fn=fn_name, error=msg)
            raise BrokerRateLimitError(msg) from e
        if cls_name == "PermissionException":
            log.error("kite_permission_denied", fn=fn_name, error=msg,
                      hint="Check your Kite Connect subscription includes this endpoint")
            raise BrokerPermissionError(msg) from e
        if cls_name in {"OrderException", "InputException", "DataException", "GeneralException"}:
            log.error("kite_broker_error", fn=fn_name, error_class=cls_name, error=msg)
            raise BrokerError(f"{cls_name}: {msg}") from e

        log.exception("kite_unknown_error", fn=fn_name, error_class=cls_name)
        raise BrokerError(f"Unknown error from Kite SDK ({cls_name}): {msg}") from e

    # ----------------- thin typed wrappers around frequently-used calls -----------------

    async def profile(self) -> dict[str, Any]:
        return await self._call("profile")

    async def margins(self) -> dict[str, Any]:
        return await self._call("margins")

    async def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        if exchange:
            return await self._call("instruments", exchange)
        return await self._call("instruments")

    async def historical_data(
        self,
        instrument_token: int,
        from_dt: datetime,
        to_dt: datetime,
        interval: str,
        continuous: bool = False,
        oi: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._call(
            "historical_data",
            instrument_token,
            from_dt,
            to_dt,
            interval,
            continuous=continuous,
            oi=oi,
        )

    async def quote(self, instruments: list[str]) -> dict[str, Any]:
        return await self._call("quote", instruments)

    async def ltp(self, instruments: list[str]) -> dict[str, Any]:
        return await self._call("ltp", instruments)

    async def positions(self) -> dict[str, Any]:
        return await self._call("positions")

    async def orders(self) -> list[dict[str, Any]]:
        return await self._call("orders")

    async def place_order(
        self,
        variety: str,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        product: str,
        order_type: str,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        validity: str = "DAY",
        tag: str | None = None,
    ) -> str:
        """
        Place an order.

        CRITICAL: This is the only place real money leaves the account.
        Callers must go through the Risk Engine before invoking this.
        """
        kwargs: dict[str, Any] = {
            "variety": variety,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": int(quantity),
            "product": product,
            "order_type": order_type,
            "validity": validity,
        }
        if price is not None:
            kwargs["price"] = float(price)
        if trigger_price is not None:
            kwargs["trigger_price"] = float(trigger_price)
        if tag:
            kwargs["tag"] = tag

        return await self._call("place_order", **kwargs)

    async def cancel_order(self, variety: str, order_id: str) -> str:
        return await self._call("cancel_order", variety=variety, order_id=order_id)

    async def modify_order(self, variety: str, order_id: str, **kwargs: Any) -> str:
        return await self._call("modify_order", variety=variety, order_id=order_id, **kwargs)


# Singleton accessor
_client: KiteClient | None = None


def get_kite_client() -> KiteClient:
    global _client
    if _client is None:
        _client = KiteClient()
    return _client
