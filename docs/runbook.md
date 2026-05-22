# Daily operations runbook

## Before market open (every trading day)

### 07:30 IST — Token refresh
```bash
python -m src.workers.token_refresh
```
Expected: log line `token_refresh_done`. If it fails:
- Verify TOTP secret in `.env` matches the Zerodha 2FA QR
- Try manual login at https://kite.zerodha.com/ to ensure account isn't locked
- Check that Zerodha didn't push a forced password change

### 08:00 IST — Instrument catalog refresh
```bash
python -m src.workers.refresh_instruments
```
Expected: `instrument_refresh_done` with non-zero row count (~80k–120k rows).

### 08:30 IST — Health check
```bash
python -m scripts.health_check
```
All 6 components must be green. If kill switch shows ARMED, decide whether to disarm:
```bash
python -m scripts.kill_switch off
```

### 09:00 IST — Start the agent
(To be added in Phase 1 — currently no live worker exists.)

## During the day

- Monitor Grafana dashboard / Telegram alerts.
- If anything looks wrong, **hit the kill switch first, debug later**:
  ```bash
  python -m scripts.kill_switch on --reason "investigating XYZ"
  ```

## After market close (15:35 IST)

- EOD reconciliation: verify P&L against Kite's tradebook
- Daily journal export (Phase 3)

## Common issues

### Kite token expired mid-day (should be rare)
- Token shouldn't expire until ~06:00 next day. If it does:
  ```bash
  python -m src.workers.token_refresh
  ```
- Then restart the agent.

### WSS disconnect
- The ingestor reconnects with exponential backoff automatically.
- If reconnects exceed 10 in a minute, the agent halts (auto kill switch).

### Database full / disk full
- TimescaleDB chunks auto-rotate. To trim manually:
  ```sql
  SELECT drop_chunks('ticks', INTERVAL '90 days');
  ```
