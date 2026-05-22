# Project layout

```
trading-agent/
├── pyproject.toml             # Python project + dependency declaration
├── docker-compose.yml         # Postgres (TimescaleDB) + Redis for local dev
├── alembic.ini                # DB migration config
├── alembic/                   # DB migration scripts
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_init.py       # Initial schema + hypertables
├── config/
│   ├── base.yaml              # Default config — checked into git
│   └── paper.yaml             # Paper-mode overrides
├── .env.example               # Env var template — copy to .env locally
├── README.md
│
├── src/
│   ├── core/
│   │   ├── config.py          # Pydantic settings, YAML+env layering
│   │   ├── clock.py           # WallClock / SimulatedClock
│   │   ├── events.py          # Typed event contracts (Pydantic)
│   │   ├── bus.py             # Redis Streams event bus
│   │   ├── exceptions.py      # Exception hierarchy
│   │   ├── kill_switch.py     # Defence-in-depth kill switch
│   │   └── logging.py         # structlog config
│   │
│   ├── auth/
│   │   └── kite_session.py    # Daily Kite login (TOTP-based)
│   │
│   ├── broker/
│   │   ├── kite_client.py     # Rate-limited async wrapper around Kite SDK
│   │   ├── kite_ws.py         # WebSocket manager (Phase 1)
│   │   └── instrument_catalog.py
│   │
│   ├── data/
│   │   ├── db.py              # Async SQLAlchemy engine
│   │   ├── models.py          # ORM models
│   │   └── repositories/      # Phase 1+
│   │
│   ├── indicators/            # Phase 2
│   ├── regime/                # Phase 2
│   ├── strategies/            # Phase 3
│   ├── options/               # Phase 3
│   ├── risk/                  # Phase 4
│   ├── execution/             # Phase 4
│   ├── journal/               # Phase 3+
│   ├── backtest/              # Phase 5
│   ├── api/                   # Phase 6
│   ├── notifications/         # Phase 4
│   └── workers/
│       ├── token_refresh.py
│       └── refresh_instruments.py
│
├── scripts/
│   ├── health_check.py        # Phase 0 smoke test CLI
│   └── kill_switch.py         # Manual kill switch CLI
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
│
├── ops/                       # Grafana dashboards, Prometheus rules (Phase 7)
│
└── docs/
    ├── architecture.md
    ├── runbook.md
    └── project-structure.md
```
