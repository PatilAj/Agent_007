"""Exception hierarchy for the trading agent."""
from __future__ import annotations


class TradingAgentError(Exception):
    """Base exception."""


# --- Configuration ---
class ConfigError(TradingAgentError):
    pass


# --- Authentication / Broker ---
class AuthError(TradingAgentError):
    pass


class TokenExpiredError(AuthError):
    pass


class BrokerError(TradingAgentError):
    """Generic broker (Kite) error."""


class BrokerRateLimitError(BrokerError):
    pass


class BrokerNetworkError(BrokerError):
    pass


class BrokerPermissionError(BrokerError):
    """Account lacks subscription/permission for this endpoint. Non-retryable."""


# --- Data layer ---
class DataError(TradingAgentError):
    pass


class InstrumentNotFoundError(DataError):
    pass


# --- Risk ---
class RiskGateRejection(TradingAgentError):
    """Risk gate denied an order."""

    def __init__(self, gate: str, reason: str, context: dict | None = None):
        self.gate = gate
        self.reason = reason
        self.context = context or {}
        super().__init__(f"[{gate}] {reason}")


class KillSwitchArmed(TradingAgentError):
    """Kill switch is armed — no new orders allowed."""


# --- Execution ---
class OrderRejectedError(TradingAgentError):
    pass


class ReconciliationError(TradingAgentError):
    pass
