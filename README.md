# Trading Agent — Kite Connect Options Trading System

A risk-first, expectancy-driven options trading agent for Indian markets (NSE/BFO) built on Zerodha Kite Connect.

> **Status**: Phase 0 (Foundation) — built and runnable.
> **Mode**: Paper / Shadow only. Live trading gated behind explicit config and human approval.

---

## Phase progress

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation: repo, config, auth, instrument catalog, DB schema, logging, kill-switch primitives | ✅ Built |
| 1 | Data layer: WSS ingestion, bar aggregation, persistence | ⏳ Next |
| 2 | Indicators + regime detection | ⏳ |
| 3 | Strategy engine + option selector + signal journal | ⏳ |
| 4 | Risk engine + execution (paper mode) | ⏳ |
| 5 | Backtesting engine | ⏳ |
| 6 | Shadow live | ⏳ |
| 7 | Controlled live (₹50k cap) | ⏳ |
| 8 | Scale + multi-strategy | ⏳ |

---

## Quick start

### 1. Prereqs
- Python 3.11+
- Docker + docker-compose
- A funded Zerodha account with Kite Connect API subscription (₹2000/month)
- Your Kite Connect `api_key` and `api_secret`

### 2. Setup

```bash
# Clone and enter
cd trading-agent

# Create virtualenv
python3.11 -m venv .venv
source .venv/bin/activate

# Install
pip install -e ".[dev]"

# Copy env template and fill in your secrets
cp .env.example .env
# Edit .env with KITE_API_KEY, KITE_API_SECRET, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET

# Start Postgres + Redis
docker-compose up -d

# Run DB migrations
alembic upgrade head

# Verify everything by running the test suite
pytest

# Daily Kite login (interactive first time, automated after)
python -m src.workers.token_refresh
```

### 3. Verify Phase 0 health

```bash
# Run the health check CLI
python -m scripts.health_check

# Expected output:
# ✅ Config loaded
# ✅ Database reachable
# ✅ Redis reachable
# ✅ Kite token valid (expires in N hours)
# ✅ Instrument catalog: 12,345 instruments loaded
# ✅ Kill switch: DISARMED (trading allowed)
```

---

## Architecture (high-level)

```
[Kite Connect REST + WSS]
        ↓
[Broker layer]  ← daily token refresh
        ↓
[Event bus: Redis Streams]
        ↓
[Data → Indicators → Regime → Strategy → Option Selector → Risk Engine → Order Manager]
        ↓
[Postgres (journal) + TimescaleDB (ticks/candles)]
        ↓
[FastAPI dashboard + Telegram alerts]
```

See `docs/architecture.md` for details.

---

## Safety model

The system has **four hard kill-switches**, any one of which halts all new orders:

1. `KILL_SWITCH=on` env var (process-wide)
2. Redis flag `kill_switch:armed` (cluster-wide)
3. Daily-loss gate in Risk Engine (auto-trip)
4. Manual CLI: `python -m scripts.kill_switch on`

Live trading mode requires:
- `MODE=live` in env
- `kill_switch:armed` to be unset
- Daily loss counter below limit
- All health checks green

Default mode is `paper`. **You cannot accidentally place real orders.**

---

## Project layout

See `docs/project-structure.md`.

---

## License

Proprietary / Personal use only. Not for redistribution. Do not commercialise without SEBI compliance review.
