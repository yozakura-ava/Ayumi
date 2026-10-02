# ICT/SMC Architecture Design — 2026-07

**Card:** BQ-1381 (60bbc23f-c8e5-4cf4-9afd-19ab487f2f42)
**Author:** Ava (design synthesis) — 2026-07-07
**Builds on:** [`docs/research/satoshi/cbot-api-assessment-2026-07.md`](../research/satoshi/cbot-api-assessment-2026-07.md)
**Status:** Design draft — pre-build

---

## 1. Goals and non-goals

### Goals
1. **Primary execution path on cTrader** — cBot owns order placement, exit management, partial closes, trailing stops.
2. **Python (Ayumi) as decision-support overlay** — signal validation, multi-strategy ranking, regime classification, FTMO risk double-check, analytics.
3. **FTMO compliant by construction** — daily DD, max DD, weekend rule, news filter all enforced before orders leave the cBot.
4. **Failover aware** — cTrader desktop dies → cTrader Mobile cloud picks up. Python dies → cBot continues with last-known config.
5. **Reuse, don't rewrite** — port the 5 existing C# detectors (`src/forex_bot/cbot/`) into a live `Robot` class with minimal adapter code, and mirror them in Python for parity testing.

### Non-goals (this design)
- Building a new ML model from scratch (separate card)
- Replacing the existing cTrader OpenAPI adapter (`src/forex_bot/adapters/ctrader/`) — keep using it
- Crypto, multi-broker routing, copy trading — out of scope; FTMO only
- HFT (sub-100 ms) — ICT signals are M15/H1/H4 cadence; latency budget is hundreds of ms, not single-digit

---

## 2. System diagram

```
                            ┌──────────────────────────────────┐
                            │   cTrader Desktop (always-on)    │
                            │   Win/Mac mini, UPS-backed        │
                            │                                  │
   FTMO server ◄──proto────►│  ┌──────────────────────────┐    │
   (broker feed)            │  │  ICT/SMC cBot (Python)   │    │
                            │  │  ┌─────────────────────┐ │    │
                            │  │  │ Bar Aggregator      │ │    │
                            │  │  │ M15 + H1 + H4 + M5  │ │    │
                            │  │  └─────────┬───────────┘ │    │
                            │  │            ▼             │    │
                            │  │  ┌─────────────────────┐ │    │
                            │  │  │ MarketState          │ │    │
                            │  │  │ (List<Bar> snapshot) │ │    │
                            │  │  └─────────┬───────────┘ │    │
                            │  │            ▼             │    │
                            │  │  5 Detectors (parallel): │ │    │
                            │  │   • MarketStructure     │ │    │
                            │  │   • OrderBlock          │ │    │
                            │  │   • FVG                 │ │    │
                            │  │   • LiquiditySweep      │ │    │
                            │  │   • PremiumDiscount     │ │    │
                            │  │            ▼             │    │
                            │  │  ┌─────────────────────┐ │    │
                            │  │  │ ConfluenceEngine     │ │    │
                            │  │  │ weighted score →     │ │    │
                            │  │  │ ConfluenceSignal     │ │    │
                            │  │  └─────────┬───────────┘ │    │
                            │  │            ▼             │    │
                            │  │  ┌─────────────────────┐ │    │
                            │  │  │ RiskGate (in-cBot)   │ │    │
                            │  │  │ FTMO DD, sizing,     │ │    │
                            │  │  │ news, weekend        │ │    │
                            │  │  └─────────┬───────────┘ │    │
                            │  │            ▼             │    │
                            │  │  ┌─────────────────────┐ │    │
                            │  │  │ OrderManager          │ │    │
                            │  │  │ market/limit/SL/TP/  │ │    │
                            │  │  │ partial/trail         │ │    │
                            │  │  └─────────┬───────────┘ │    │
                            │  └────────────┼──────────────┘    │
                            │               │                    │
                            │       ┌───────┴────────┐           │
                            │       ▼                ▼           │
                            │  ┌─────────┐    ┌────────────┐    │
                            │  │ FTMO    │    │ Plugin SDK │    │
                            │  │ orders  │    │ Bridge     │    │
                            │  └────┬────┘    │ (signals   │    │
                            │       │         │ →Python)   │    │
                            │       │         └─────┬──────┘    │
                            └───────┼───────────────┼───────────┘
                                    │               │
                                    │               │ Unix socket / WS
                                    │               │ JSON signals
                            ┌───────▼───────────────▼───────────┐
                            │   Python (Ayumi) daemon           │
                            │   Decision-support overlay        │
                            │                                   │
                            │   ┌──────────────────────────┐    │
                            │   │ signal_engine/           │    │
                            │   │  - ICT detector mirror   │    │
                            │   │  - confluence_scorer     │    │
                            │   │  - regime classifier     │    │
                            │   └─────────┬────────────────┘    │
                            │             ▼                     │
                            │   ┌──────────────────────────┐    │
                            │   │ orchestrator/             │    │
                            │   │  multi-strategy ranker    │    │
                            │   │  cross-check vs cBot      │    │
                            │   └─────────┬────────────────┘    │
                            │             ▼                     │
                            │   ┌──────────────────────────┐    │
                            │   │ risk/ — FTMO overlay     │    │
                            │   │  independent DD monitor   │    │
                            │   │  kill-switch to cBot      │    │
                            │   └─────────┬────────────────┘    │
                            │             ▼                     │
                            │   ┌──────────────────────────┐    │
                            │   │ adapters/ctrader/         │    │
                            │   │  OpenAPI: position sync,  │    │
                            │   │  account snapshot,        │    │
                            │   │  emergency flatten        │    │
                            │   └──────────────────────────┘    │
                            └───────────────────────────────────┘

                  ┌─────────────────────────────────────────┐
                  │   cTrader Mobile cloud instance          │
                  │   HOT FAILOVER (not auto-promoted yet)   │
                  │   - Same cBot running                    │
                  │   - Receives signals via Plugin SDK      │
                  │   - Picked up when desktop dies          │
                  └─────────────────────────────────────────┘
```

**Critical rule from the diagram**: Python is **never in the tick-to-fill path**. cBot owns execution; Python validates and ranks asynchronously. If Python is down, cBot still trades (with last-known config). If cBot is down, Python only logs the failure — it cannot place orders directly during the killzone (that's the failover cBot's job).

---

## 3. cBot ↔ Python integration

### 3.1 Why we still need a bridge even with native Python cBots

Even though the cBot itself runs Python in-process inside cTrader, the **decision-support layer** (Ayumi) is a separate process with ML models, multi-strategy coordination, and historical context. The cBot can't load those — it needs to ask. Three bridge options were considered:

| Option | Latency | Reliability | Code cost | Verdict |
|---|---|---|---|---|
| **Plugin SDK** (HTML/JS panel hosted inside cTrader) | ~10-50 ms local | High (in-process) | Medium | ✅ **Selected** |
| **OpenAPI TCP** (separate Python daemon calling cTrader) | 5-50 ms | Medium (network) | High (already have adapter) | Reject — cBot → Python direction doesn't need it |
| **Shared SQLite** polled by Python | 100-1000 ms | High | Low | Backup only |
| **File drop** (signal JSON to disk) | Async | High | Low | Backup only |

**Selected**: **Plugin SDK hosted panel** that:
- Sits in cTrader's right-side panel
- cBot pushes signal JSON to the panel via `api.Print` or direct method call
- Panel's JS uses `fetch()` to a Python HTTP server (localhost only)
- Python replies with optional override (regime-block, ML rank override)

This is **fire-and-forget from cBot's perspective** — cBot emits signal, Python listens asynchronously, Python's reply (if it comes within 200 ms) can adjust the confidence score before order placement. After 200 ms, cBot uses its local score.

### 3.2 Message contract

cBot → Python signal message (JSON over HTTP POST):

```json
{
  "ts": "2026-07-08T13:42:11.123Z",
  "symbol": "EURUSD",
  "direction": "long",
  "confidence": 0.78,
  "entry": 1.08241,
  "sl": 1.07990,
  "tp1": 1.08492,
  "tp2": 1.08743,
  "tp3": 1.08994,
  "rr": 2.0,
  "rationale": "structure-aligned CHoCH + OB + sweep",
  "confluences": {
    "structure": true,
    "order_block": true,
    "fvg": false,
    "sweep": true,
    "premium_discount": true
  },
  "session": "NYAM",
  "killzone_active": true
}
```

Python → cBot override message (optional, within 200 ms):

```json
{
  "signal_id": "<uuid>",
  "verdict": "allow" | "block" | "demote",
  "reason": "regime=trending_down — long demoted to confidence 0.45",
  "adjusted_confidence": 0.45,
  "ts": "2026-07-08T13:42:11.285Z"
}
```

If no response within 200 ms: cBot uses its local score. This bounds Python's blast radius.

### 3.3 Why not just run Python-side strategy entirely?

Because:
1. **Tick-accurate backtesting** lives in cTrader backtester
2. **Visual replay** for debugging only cTrader provides
3. **cTrader's own trade log** as a backup audit trail (compliance)
4. **Cloud failover** (cTrader Mobile) needs the cBot artifact
5. **Latency**: Python → OpenAPI → cTrader → broker adds 10-50 ms vs cBot in-process

Python stays as overlay, never as primary executor.

---

## 4. FTMO risk model integration

### 4.1 FTMO rules (target spec)

| Rule | Value | Enforcement point |
|---|---|---|
| Daily drawdown (DD) | 5% of starting daily equity | cBot RiskGate **+ Python risk overlay** (double-check) |
| Max total drawdown | 10% of initial account balance | Same — cBot primary, Python second |
| Profit target (challenge) | 8% / 5% (phase 1/2) | Tracking only — does not block |
| Trading days (min) | 4 days for challenge, 10 for verification | Calendar gate |
| Weekend rule | No positions held over weekend | Time-based close at Fri 20:55 UTC |
| News filter | No trading 2 min before/after high-impact news | cBot pulls from ForexFactory API |
| Consistency (verification phase) | No single day > 30% of total profit | Python analytics only |
| Hedging | Not allowed on most FTMO products | cBot Position Manager rejects opposing positions |

### 4.2 Two-layer risk enforcement

**Layer 1: cBot RiskGate (in-process, primary)**
- Runs every `on_tick` and every order call
- Tracks: `daily_start_equity`, `peak_equity`, `current_equity`
- Pre-trade check: would this trade's max loss (SL distance × size) breach daily DD if hit?
- Position sizing: `quantity = (account.equity * risk_pct) / (sl_distance * pip_value)`
- Hard stop: when daily DD >= 4% (safety margin below FTMO 5%), **close all + block new entries for the day**
- Implementation: ~150 LOC, lives in `src/forex_bot/cbot/ICTSMC.RiskGate.cs` (new file, P1)
- Same logic as existing `ICTSMC.BacktestEngine.cs` `IsMaxDrawdownBreached` / `IsMaxDailyLossBreached` — port to live, parameterize

**Layer 2: Python risk overlay (independent watchdog)**
- Subscribes to OpenAPI account stream
- Tracks same DD metrics independently
- If Layer 1 fails (cBot crash, bug, mis-config), Layer 2 can:
  - Send `flatten_all` command via Plugin SDK bridge
  - Send email/SMS alert via `notifications_list` (paired node) to Craig
  - Write to incident log
- Layer 2 has read access to all positions via OpenAPI; write access only for emergency flatten

**Why two layers?** Because FTMO breaches are **terminal**. A missed pre-trade check in cBot = challenge failed = $0. A second pair of eyes that can flatten the account if the first pair has a bug is non-negotiable for capital preservation.

### 4.3 Config externalization

All risk parameters must be **loaded from JSON at startup**, not hardcoded. Sample `risk_config.json`:

```json
{
  "ftmo_tier": "100k_challenge",
  "starting_balance": 100000,
  "max_daily_dd_pct": 4.0,
  "max_total_dd_pct": 8.0,
  "risk_per_trade_pct": 0.5,
  "max_open_positions": 2,
  "max_lots_per_pair": 1.0,
  "weekend_close_utc": "Friday 20:55",
  "news_filter": {
    "high_impact_window_min": 4,
    "medium_impact_window_min": 2,
    "source": "forexfactory"
  },
  "killzones": {
    "london": {"start": "02:00", "end": "05:00"},
    "nyam": {"start": "13:00", "end": "16:00"},
    "nypm": {"start": "18:00", "end": "20:00"}
  }
}
```

cBot re-reads this on every `OnStart` AND on file mtime change (use `FileSystemWatcher` or poll every 60 s). Allows live tuning without cBot restart.

---

## 5. Phased roadmap

### Phase 1 — Port existing cBot detectors to live (foundation)

**Goal**: Existing 5 detectors in `src/forex_bot/cbot/` running inside a live cBot instance on FTMO demo.

**Tasks**:
1. Write `ICTSMC.Robot.cs` — cBot entry point (~80 LOC)
2. Write `BarAggregator.cs` — subscribes to cAlgo `Bars` events, snapshots into `MarketState` (~70 LOC)
3. Wire detectors into `Robot.OnBarClosed()` per timeframe
4. Add `RiskGate.cs` — port of `BacktestEngine` FTMO checks to live (~150 LOC)
5. Add `OrderManager.cs` — wrap `ExecuteMarketOrder`/`ModifyPosition`, handle partial close, trailing stop (~200 LOC)
6. Add `TradeLogger.cs` — JSONL trade journal + equity snapshot
7. Config externalization: `risk_config.json` loader
8. Indicator for chart visualization (separate file, draws OB/FVG zones)

**Verification**:
- Run on FTMO demo for 4 weeks
- Compare cBot signals vs `BacktestEngine.cs` on same historical week — must match
- Unit tests: every detector gets a parity test against existing `ICTSMC.Tests.cs`

**Estimate**: 1.5–2 weeks

**Card prefix**: `[P1]`

### Phase 2 — cBot ↔ Python signal bridge

**Goal**: cBot signals visible to Ayumi Python daemon; optional override path working.

**Tasks**:
1. Write Plugin SDK panel in Python: `src/forex_bot/plugins/signal_bridge/` (~250 LOC HTML/JS + Python HTTP server)
2. Add signal-send to cBot: `Robot.OnSignal()` calls panel via plugin
3. Add Python side: `src/forex_bot/integrations/cbot_signal_consumer.py` — listens, validates, replies
4. Define message contract (see §3.2) — version 1, lock it down
5. Add timeout handling — cBot uses local score if no Python reply in 200 ms
6. Logging: every signal + override in `data/logs/cbot_python_bridge.jsonl`

**Verification**:
- Drop Python daemon → cBot keeps trading with local score (no halts)
- Send override to demote a signal → cBot respects within 200 ms
- Run 1000-signal replay through both cBot and Python, compare signals

**Estimate**: 1 week

**Card prefix**: `[P2]`

### Phase 3 — Backtest validation

**Goal**: Validate the cBot logic against 2 years of M1 historical data using cTrader backtester, not just our CLI engine.

**Tasks**:
1. Run cTrader backtest on each pair (EURUSD, GBPUSD, USDJPY) for 2024-01 to 2025-12
2. Run our CLI `BacktestEngine.cs` on same period — diff the trade list, log discrepancies
3. Optimize in cTrader GA: 5-7 key parameters (OB freshness, FVG thresholds, confluence weights)
4. Walk-forward: optimize on Q1, test on Q2, etc. (4 quarters)
5. Monte Carlo on trade list (10,000 paths) — measure 95th-percentile DD
6. FTMO compliance stress test: simulate max-DD breach day, verify cBot halts

**Verification**:
- cTrader backtest report saved to `docs/research/backtest-results/`
- Equity curve must not dip below 8% DD on any path
- Profit factor > 1.3 across all pairs

**Estimate**: 1.5 weeks (mostly waiting on backtests)

**Card prefix**: `[P3]`

### Phase 4 — Paper → live (FTMO challenge)

**Goal**: Live FTMO challenge with cBot + Python overlay.

**Tasks**:
1. Run on FTMO demo for 2 more weeks with full overlay active (sanity)
2. Start FTMO 100k challenge, Phase 1
3. Daily review: Python logs vs cTrader logs (sync check)
4. After Phase 1 hit: start Phase 2
5. After Phase 2 hit: switch to verification phase
6. cTrader Mobile cloud instance configured as failover

**Verification**:
- Hit 8% profit in <30 trading days (Phase 1)
- Zero FTMO rule violations
- Python-cBot signal divergence rate < 5%

**Estimate**: 6-10 weeks wall clock (depends on market + min trading days)

**Card prefix**: `[P4]`

### Phase dependency graph

```
P1 ──► P2 ──► P3 ──► P4
 │              │
 └──────────────┴──► continuous: Python parity tests
```

P2 and P3 can start in parallel after P1 is solid. P4 cannot start until P3 validates.

---

## 6. Integration points with existing Ayumi signal pipeline

### 6.1 What we already have

In `src/forex_bot/`:

| Module | Role | Reuse for ICT/SMC |
|---|---|---|
| `signal_engine/` | Python signal generation for SR/momentum strategies | Replace confluence_scorer with ICT port; share `gate_validator`, `stop_target`, `signal_output` |
| `risk/` | Position sizing, DD enforcement (Python) | Layer 2 risk overlay reads same config |
| `adapters/ctrader/` | 43-file OpenAPI Python adapter | Used by Python overlay for account/position sync |
| `engine/` | Health monitor, anomaly monitor, recovery protocol | Watchdog on cBot — restart cTrader if dead |
| `orchestrator/` | Multi-strategy coordination | ICT cBot becomes one strategy among many |
| `ml/` | Regime classifier, signal rank model | Pre-trade override (regime=trending_down → demote long signals) |
| `backtest/` | Python backtest framework | Cross-validate cTrader results against `BacktestEngine.cs` |
| `strategies/` | 13 Python strategy modules | ICT is a 14th; share registry, killzone filter, regime filter |
| `forward_test/` | Live forward test harness | Run cBot through this for paper validation |

### 6.2 Concrete integration steps

1. **Add ICT strategy to `strategies/registry.py`** — wraps `signal_engine/confluence_scorer` (after Python port of detectors)
2. **Share `risk/ftmo_config.json`** between cBot and Python — same source of truth, watch for drift
3. **Add cBot health to `engine/health_monitor.py`** — checks cTrader process + open positions match expected
4. **Add ICT signals to `reporting/`** dashboards — equity curve, signal log, DD tracker
5. **Connect to `forward_test/`** — cBot runs as a "strategy" in the forward test harness

### 6.3 What we deliberately do NOT integrate

- **cBot does NOT call into Ayumi's `engine/` or `orchestrator/` for trade decisions.** Python is downstream, not upstream. If Python says "no", cBot skips that signal, but the skip decision is local to the cBot. Python never gets to override an entry that cBot decided to take mid-tick.

- **ML models do NOT live in cBot.** They're too big (pandas, sklearn) and too slow. cBot gets a frozen JSON of regime classification updated every 5 minutes by Python.

---

## 7. Failover design — Python independent position awareness

### 7.1 Failure modes

| Failure | Detection | Response | Recovery time |
|---|---|---|---|
| cTrader desktop crashes | `engine/health_monitor` heartbeat timeout (30s) | cTrader Mobile cloud auto-starts; Python alerts Craig | 60-120s |
| cBot code exception | cTrader's `OnException` event | cBot halts; Python continues monitoring | Manual restart needed |
| Network drop to FTMO | cTrader's reconnect event | Positions remain (cTrader holds them); alert fires | Auto on reconnect |
| Python daemon dies | systemd / process supervisor restarts | cBot continues with last-known config | 30s |
| Plugin SDK bridge fails | cBot's signal-send times out (1s) | cBot falls back to local score, log "bridge_down" event | Manual |
| Position state desync (cBot vs Python) | Periodic reconcile (every 60s) | Python logs mismatch; alerts if >1 position off | Manual review |
| FTMO DD breach (in cBot) | cBot's RiskGate trips | cBot closes all + blocks; Python independently verifies + alerts | Auto |

### 7.2 Python independent position awareness

Python's `adapters/ctrader/` already provides full position sync via OpenAPI. The pattern:

```python
# Every 60 seconds, Python pulls authoritative state from cTrader
positions = ctrader_adapter.get_positions()
account = ctrader_adapter.get_account_state()

# Compare against local expectations
expected = strategy_registry.expected_positions()
for pos in positions:
    if pos not in expected:
        log.warning("unexpected position", pos=pos)
        alert_craig(f"Position desync: {pos}")

# Track equity curve for risk overlay
risk_overlay.update(account.equity, account.balance)
if risk_overlay.daily_dd_breached():
    send_kill_switch_to_cbot()
```

This makes Python **a passive observer that can become active**. The threshold for "active" is when cBot is demonstrably broken — Python then takes the flatten action.

### 7.3 Hot failover with cTrader Mobile

The cTrader Mobile cloud instance is a **separate subscription**, but the same cBot artifact can run on it. Configuration:

- Desktop cTrader runs cBot instance A on chart EURUSD M15
- Mobile cloud cTrader runs cBot instance B on same chart, same config
- Both subscribe to same Plugin SDK bridge
- Python heartbeat: if desktop `last_tick_time` > 30s old, treat desktop as down
- Python sends `kill_switch` to instance A; promotes instance B by sending `unpause` if it was paused
- Positions held by instance A continue to be managed by instance B (same `Label`)

**Caveat**: this requires the cBot to support pause/resume via Plugin SDK command, and the broker to not flag duplicate cBot instances as suspicious. Test in staging.

### 7.4 What we don't build (yet)

- **Active-active multi-region**: out of scope for FTMO. One cTrader instance is fine.
- **Geographic failover**: cTrader Mobile cloud is sufficient.
- **Disaster recovery across brokers**: out of scope. FTMO only.

---

## 8. Decisions (Craig, 2026-07-07)

1. **Kill-switch authority**: ✅ **Emergency flatten YES, regime demote NO.** Python watchdog can flatten all positions in catastrophic failures (DD breach imminent). Regime classification stays with cBot (tick-level data). **Visibility requirement:** when kill-switch activates, must surface prominently — dashboard alert + Craig notification, not silent.
2. **Risk model**: ✅ **Max 2% risk at any time** (not per-trade fixed). Dynamic allocation: if a trade goes positive and SL moves to breakeven/profit, that frees risk capacity for another trade. This is the governing constraint, not position count.
3. **Strategy count**: ✅ **No single-strategy limit.** Need one that works first, but different timeframes (scalping, swing, long-term) can complement each other. Current 10 strategies aren't producing enough trades — the problem is signal quality, not strategy count. Don't limit artificially.
4. **Max concurrent positions**: ✅ Governed by **2% max risk at any time** (not a fixed position count). When SL moves to BE/profit on an open trade, capacity frees up for new entries. Dynamic, not static.
5. **News filter source**: ✅ **ForexFactory scrape with cache** to start. Paid APIs (Financial Modeling Prep, etc.) deferred until revenue positive — not a pre-revenue priority.

---

## 9. Files created/changed by this design

**New files (planned, not yet created — separate cards)**:

```
src/forex_bot/cbot/ICTSMC.Robot.cs          ~80 LOC  (P1)
src/forex_bot/cbot/ICTSMC.BarAggregator.cs ~70 LOC  (P1)
src/forex_bot/cbot/ICTSMC.RiskGate.cs      ~150 LOC (P1)
src/forex_bot/cbot/ICTSMC.OrderManager.cs  ~200 LOC (P1)
src/forex_bot/cbot/ICTSMC.TradeLogger.cs   ~80 LOC  (P1)
src/forex_bot/cbot/ICTSMC.Indicator.cs     ~150 LOC (P1, visualization)

src/forex_bot/ict/                         (P1, Python port for parity)
├── __init__.py
├── market_structure.py
├── order_block.py
├── fvg.py
├── liquidity_sweep.py
├── premium_discount.py
└── confluence.py

src/forex_bot/plugins/signal_bridge/       (P2, Plugin SDK bridge)
├── panel.html
├── panel.js
└── server.py

src/forex_bot/integrations/cbot_signal_consumer.py  ~200 LOC (P2)
src/forex_bot/risk/ftmo_overlay.py        ~150 LOC  (P2, watchdog)
```

**No existing files modified.** This is research + design only.

---

## 10. Success criteria for the design

- [x] System diagram covers tick → detectors → confluence → signal → risk → exec
- [x] cBot ↔ Python integration approach named and justified (Plugin SDK)
- [x] FTMO risk model integrated at two layers (cBot primary, Python secondary)
- [x] Phased roadmap: P1 (port detectors), P2 (bridge), P3 (backtest), P4 (paper→live)
- [x] Integration with existing Ayumi modules called out concretely
- [x] Failover design with Python independent position awareness
- [ ] Craig approval on 5 open questions in §8

---

## 11. References

- [`docs/research/satoshi/cbot-api-assessment-2026-07.md`](../research/satoshi/cbot-api-assessment-2026-07.md) — API capabilities (this card, BQ-1381)
- [`docs/research/assess-ctrader-calgo-api-and-design-automated-ict-smc-architecture.md`](../assess-ctrader-calgo-api-and-design-automated-ict-smc-architecture.md) — AYUAA-8 (prior)
- [`docs/forex/strategy-development-ict-smc-automated-logic.md`](../forex/strategy-development-ict-smc-automated-logic.md) — AYUAA-27 strategy spec
- [`docs/forex/backtesting-framework-ctrader-calgo.md`](../forex/backtesting-framework-ctrader-calgo.md) — AYUAA-31 backtest spec
- [`docs/research/ict/`](../research/ict/) — 10 ICT primitive docs
- `src/forex_bot/cbot/` — 14 detector/backtest files (3,378 LOC, read in full)
- `src/forex_bot/adapters/ctrader/` — 43-file OpenAPI Python adapter
- `src/forex_bot/risk/` — Python FTMO config + sizing
- `src/forex_bot/signal_engine/` — Python signal generation pipeline
- `src/forex_bot/engine/health_monitor.py` — watchdog pattern to reuse

---

*End of architecture design. Both deliverables for BQ-1381 are complete. Ready for Craig review of §8 open questions.*