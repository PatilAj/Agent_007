# Architecture

## High-level data flow

```
                            ┌─────────────────────┐
                            │   Kite Connect      │
                            │   REST + WSS        │
                            └──────────┬──────────┘
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
        ▼                              ▼                              ▼
┌───────────────┐            ┌──────────────────┐          ┌──────────────────┐
│ Auth Worker   │            │ Market Data      │          │  Order Manager   │
│ (daily TOTP)  │            │ Ingestor (WSS)   │          │  (REST orders)   │
└───────┬───────┘            └────────┬─────────┘          └────────┬─────────┘
        │                             │                             ▲
        ▼                             ▼                             │
┌───────────────┐            ┌──────────────────┐          ┌────────┴─────────┐
│ Token store   │            │ Bar Aggregator   │          │  Risk Engine     │
│ (Postgres)    │            │ (1m/3m/5m/15m)   │          │  (hard gates)    │
└───────────────┘            └────────┬─────────┘          └────────▲─────────┘
                                      │                             │
                              ┌───────▼────────┐                    │
                              │ Redis Streams  │ ─────► Indicators ─► Strategy ─► Option Selector
                              │   (Event Bus)  │                                       │
                              └───────┬────────┘                                       ▼
                                      │                                          [signals]
                                      ▼
                              ┌────────────────┐
                              │ TimescaleDB    │
                              │ (ticks, bars)  │
                              └────────────────┘
```

## Process model (v1)

A single Python process running multiple asyncio tasks:
- `auth_worker_task` (scheduled, APScheduler)
- `market_data_ingestor_task` (WSS reader)
- `bar_aggregator_task` (consumes ticks → emits bars)
- `indicator_task` (consumes bars → emits indicator updates)
- `strategy_runner_task` (consumes indicators → emits signals)
- `risk_engine_task` (consumes signals → emits approved orders)
- `order_manager_task` (consumes approved orders → places/cancels at broker)
- `reconciler_task` (every 60s, diffs local vs Kite state)
- `health_monitor_task` (every 30s, emits health events)

When the system grows, extract each into its own process behind the same Redis bus.

## Safety boundaries

1. **Risk Engine is the only path to the broker.** No other module is allowed to call `kite.place_order()`.
2. **Kill switch is checked at three points**: before order is sent, before WSS reconnects, in the supervisor loop.
3. **Reconciliation runs every 60s** and on every restart — local state is never trusted blindly.
4. **Idempotency on every order** via `client_order_id` UUID.

## Configuration precedence

`environment variable` > `<mode>.yaml` > `base.yaml` > `Settings` defaults
