"""
Unit tests for KiteClient._classify_and_raise.

In particular: transient socket/TLS failures from the underlying requests/urllib3
stack (the WinError 10054 RST we hit on the corporate network) must map to
BrokerNetworkError, NOT BrokerError — otherwise the tenacity retryer treats
them as fatal and the backfill blows up on a single hiccup.
"""
from __future__ import annotations

from ssl import SSLError as PySSLError

import pytest
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError as ReqConnectionError,
    ReadTimeout,
)
from urllib3.exceptions import ProtocolError

from src.broker.kite_client import KiteClient
from src.core.exceptions import (
    BrokerError,
    BrokerNetworkError,
    BrokerPermissionError,
    BrokerRateLimitError,
    TokenExpiredError,
)


def _classify(e: Exception):
    """Helper: call the private classifier in isolation."""
    client = KiteClient.__new__(KiteClient)  # bypass __init__ — pure function call
    client._kite = None
    client._access_token = None
    client._classify_and_raise("test_fn", e)


# ----------------- transient network errors should be retryable -----------------


@pytest.mark.parametrize(
    "exc",
    [
        ReqConnectionError("forcibly closed"),
        ReadTimeout("read timed out"),
        ChunkedEncodingError("incomplete read"),
        ProtocolError("connection aborted"),
        PySSLError("handshake failed"),
        ConnectionResetError(10054, "WinError 10054"),
        TimeoutError("socket timed out"),
    ],
)
def test_transient_network_errors_map_to_broker_network_error(exc):
    """All these are transient; tenacity must see BrokerNetworkError and retry."""
    with pytest.raises(BrokerNetworkError):
        _classify(exc)


# ----------------- specific kite SDK class names still routed correctly -----------------


class _FakeTokenException(Exception):
    pass


class _FakePermissionException(Exception):
    pass


class _FakeOrderException(Exception):
    pass


def test_token_exception_routes_to_token_expired():
    e = _FakeTokenException("Invalid `access_token`")
    with pytest.raises(TokenExpiredError):
        _classify(e)


def test_permission_exception_routes_to_permission_error():
    e = _FakePermissionException("not subscribed")
    # rename the class so classifier sees "PermissionException"
    e.__class__.__name__ = "PermissionException"
    with pytest.raises(BrokerPermissionError):
        _classify(e)


def test_rate_limit_routes_to_rate_limit_error():
    e = Exception("Too many requests")
    with pytest.raises(BrokerRateLimitError):
        _classify(e)


def test_order_exception_routes_to_broker_error():
    e = _FakeOrderException("invalid product")
    e.__class__.__name__ = "OrderException"
    with pytest.raises(BrokerError) as excinfo:
        _classify(e)
    # Must NOT be the network subclass — must be plain BrokerError
    assert not isinstance(excinfo.value, BrokerNetworkError)


# ----------------- structural: BrokerNetworkError is in the retryer config -----------------


def test_retryer_retries_broker_network_error():
    """Belt-and-suspenders: the retry config in _call must list BrokerNetworkError."""
    import inspect

    source = inspect.getsource(KiteClient._call)
    assert "BrokerNetworkError" in source
    assert "BrokerRateLimitError" in source
