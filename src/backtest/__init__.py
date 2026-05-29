"""
Backtesting package.

Replays historical spot candles through the *exact* live indicator → regime →
strategy components, then simulates each resulting option trade with a
Black-Scholes synthetic-pricing model (we have no historical option-premium
data, only the underlying index). Produces in-memory trades + an aggregate
performance report. Nothing here writes to the live trades/orders tables.
"""
