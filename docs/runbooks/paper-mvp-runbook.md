# Paper Trading MVP Runbook

> Last updated: 2026-04-22

## Quick Reference

| Action | Command |
|--------|---------|
| Start | `systemctl --user start ayumi-paper-mvp` |
| Stop | `systemctl --user stop ayumi-paper-mvp` |
| Restart | `systemctl --user restart ayumi-paper-mvp` |
| Status | `systemctl --user status ayumi-paper-mvp` |
| Health check | `./scripts/paper_status.sh` |
| Live logs | `journalctl --user -u ayumi-paper-mvp -f` |
| Alert log | `tail -f logs/alerts.log` |

---

## Service Management

### Start/Stop/Restart

```bash
# Start the paper trading engine
systemctl --user start ayumi-paper-mvp

# Stop gracefully (waits for current bar to finish)
systemctl --user stop ayumi-paper-mvp

# Restart (e.g., after config change)
systemctl --user restart ayumi-paper-mvp
```

The service runs under your user account (`$USER`) with automatic restart on failure (10-second delay).

### Enable/Disable Auto-Start

```bash
# Start automatically on login
systemctl --user enable ayumi-paper-mvp

# Do NOT auto-start
systemctl --user disable ayumi-paper-mvp
```

---

## Monitoring

### Quick Health Check

```bash
./scripts/paper_status.sh
```

Shows:
- Service running status + uptime
- Current balance and P&L
- Open positions (count + details)
- Today's trade count and P&L
- Current drawdown %
- Overall win rate
- Last 5 alerts

### Journal Logs

```bash
# Follow live output
journalctl --user -u ayumi-paper-mvp -f

# Last 100 lines
journalctl --user -u ayumi-paper-mvp -n 100

# Since this morning
journalctl --user -u ayumi-paper-mvp --since today

# Filter by priority (errors only)
journalctl --user -u ayumi-paper-mvp -p err
```

### Alert Log

```bash
# Follow alerts
tail -f logs/alerts.log

# Recent critical alerts
grep CRITICAL logs/alerts.log | tail -20

# Today's alerts
grep "$(date -u +%Y-%m-%d)" logs/alerts.log
```

---

## Alert Types & Severity

| Alert Type | Severity | Meaning |
|------------|----------|---------|
| `circuit_breaker` | CRITICAL | Trading halted — drawdown or risk limit breached |
| `daily_loss_limit` | CRITICAL | Daily loss exceeded configured limit |
| `drawdown_warning` | WARNING | Drawdown exceeds 15% of 20% FTMO limit |
| `anomaly` | WARNING | Unusual activity detected |
| `connection_lost` | WARNING | cTrader connection dropped |
| `connection_restored` | INFO | Reconnected successfully |
| `position_opened` | INFO | New position opened |
| `position_closed` | INFO | Position closed (includes P&L) |
| `daily_summary` | INFO | End-of-day performance summary |

### What to Do When Circuit Breaker Fires

1. **Check the alert** — look at `logs/alerts.log` for details (what triggered it, current drawdown %)
2. **Check the journal** — `journalctl --user -u ayumi-paper-mvp -n 50` for context
3. **Review open positions** — `./scripts/paper_status.sh` or query DB directly
4. **Do NOT restart immediately** — understand why it fired first
5. **Common causes:**
   - Drawdown exceeded 20% FTMO limit → review strategy behavior
   - Daily loss limit hit → check for runaway positions or data issues
   - Connection issues causing stale state
6. **Once resolved:** `systemctl --user restart ayumi-paper-mvp`

---

## Database Queries

The SQLite database lives at `data/trading.db`.

```bash
# Current balance
sqlite3 data/trading.db "SELECT balance FROM equity_curve ORDER BY timestamp DESC LIMIT 1;"

# Equity curve (last 50 points)
sqlite3 data/trading.db "SELECT timestamp, balance FROM equity_curve ORDER BY timestamp DESC LIMIT 50;"

# Open positions
sqlite3 data/trading.db "SELECT * FROM trades WHERE status = 'open';"

# Today's closed trades
sqlite3 data/trading.db "SELECT symbol, direction, realized_pnl FROM trades WHERE date(closed_at) = date('now') AND status = 'closed';"

# Win rate over all time
sqlite3 data/trading.db "SELECT COUNT(*) as total, SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins FROM trades WHERE status = 'closed';"
```

---

## Strategies

### Active Strategies
Strategies are defined in `src/forex_bot/strategies/`. The MVP loads a blend from `run_paper_mvp`.

### Adding/Modifying a Strategy

1. Create or edit the strategy in `src/forex_bot/strategies/`
2. Register it in the strategy factory (see `backtest/parameter_sweep/`)
3. Run a backtest to validate (see below)
4. Update `run_paper_mvp` to include the strategy
5. Restart: `systemctl --user restart ayumi-paper-mvp`

### Running Backtests

```bash
source .venv/bin/activate
PYTHONPATH=src/forex_bot:src python scripts/run_walk_forward.py
```

Strategy-specific backtest scripts live in `scripts/` (e.g., `run_tts_walkforward.py`, `run_scalper_m5_walkforward.py`).

---

## Key Files

| File | Purpose |
|------|---------|
| `deploy/ayumi-paper-mvp.service` | Systemd service definition |
| `deploy/setup.sh` | Installation script |
| `deploy/logrotate-ayumi` | Log rotation config |
| `scripts/paper_status.sh` | Quick health check |
| `src/forex_bot/run_paper_mvp` | Main entry point |
| `src/forex_bot/alerting.py` | AlertManager + channels |
| `src/forex_bot/storage/trade_store.py` | SQLite trade store |
| `src/forex_bot/storage/migrations.py` | DB schema migrations |
| `src/forex_bot/strategies/` | Strategy implementations |
| `src/forex_bot/adapters/ctrader/` | cTrader connection adapters |
| `data/trading.db` | Live trading database |
| `logs/alerts.log` | Alert log |
| `logs/*.log` | Application logs (rotated daily, 30-day retention) |

---

## Troubleshooting

### Service won't start
```bash
journalctl --user -u ayumi-paper-mvp --since "5 min ago"
```
Check for: missing venv, import errors, port conflicts, missing `.env`.

### No data appearing
- Verify cTrader connection in journal logs
- Check `data/trading.db` exists and has tables: `.tables`
- Confirm market hours (strategies only trade during sessions)

### High memory usage
- Check for unclosed DB connections or tick buffer growth
- `journalctl --user -u ayumi-paper-mvp | grep -i "memory\|buffer"`

### Log rotation not working
```bash
sudo logrotate -d /etc/logrotate.d/ayumi  # dry run
sudo logrotate -f /etc/logrotate.d/ayumi  # force
```
