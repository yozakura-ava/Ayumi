# cTrader cAlgo API Assessment — 2026-07

**Card:** BQ-1381 (60bbc23f-c8e5-4cf4-9afd-19ab487f2f42)
**Author:** Satoshi (research) — 2026-07-07
**Scope:** API capabilities, tick fidelity, hardware/perf, viability verdict
**Prior context:** [`docs/research/assess-ctrader-calgo-api-and-design-automated-ict-smc-architecture.md`](../assess-ctrader-calgo-api-and-design-automated-ict-smc-architecture.md) (AYUAA-8, done Apr-2026). This document updates that assessment against the **2026 cTrader Algo release** and grounds it in the **actual cBot code** now in tree.

---

## 1. TL;DR

- **cTrader Algo (formerly cAlgo) gained first-class Python support in 2026.** AYUAA-8's "C# native only" recommendation is obsolete. cBots, indicators, and plugins can now be written in either C# or Python against the same `Algo API` (verified on `help.ctrader.com/ctrader-algo/`, fetched 2026-07-08).
- **Tick-level backtesting is available from server data.** Visual and non-real-time modes; genetic-algo optimizer; Renko/range-bar replay; HTML report export — confirmed in `help.ctrader.com/ctrader-algo/how-tos/cbots/backtest-a-cbot/`.
- **The existing C# detectors (`src/forex_bot/cbot/`) are well-structured and already production-shaped**, but currently **only run inside the legacy `ICTSMC.Program.cs` CLI test runner** — they are not wired to a live cBot `Robot` class. They use `List<Bar>` from CSV, not live cAlgo `Bars`/`MarketData`.
- **Viability verdict: cTrader = primary execution platform for ICT/SMC.** Python layer (Ayumi) becomes a signal validator, risk overlay, and analytics layer that consumes cBot signals over WebSocket/Plugin channel. cTrader owns the order path; Python owns the decisions that need ML, multi-strategy ranking, or external data.

---

## 2. What changed since AYUAA-8

AYUAA-8 (Apr 2026) was correct for its time but two things have moved:

1. **Native Python in cTrader Algo** — `class MACDCrossover():` with `on_start`, `on_tick`, `on_bar_closed` is now first-class. No need for OpenAPI TCP bridge for the strategy layer itself.
2. **Plugin SDK has matured** — Web app plugins via plugin SDK can run in any cTrader app (desktop/web/mobile). Plugin SDK plugins can place orders with user permission, which makes them viable for the cBot→Python signal bridge we previously designed as OpenAPI.

The AYUAA-8 spec also under-allocated to **risk model enforcement at the platform boundary**. Today the FTMO model is more relevant because we have a working backtest that already implements it (see §3.2).

---

## 3. cBot code inventory — what we actually have

Path: `src/forex_bot/cbot/`. Total: **3,378 lines** across 14 files.

| File | Lines | Role | Live-cAlgo ready? |
|---|---|---|---|
| `ICTSMC.Models.cs` | 198 | Domain types (Bar, TimeFrame, OrderBlock, FairValueGap, LiquidityPool, MarketState, ConfluenceSignal) | ✅ Pure data classes |
| `ICTSMC.FVGDetector.cs` | 135 | 3-candle FVG detection, mini/massive threshold, mitigation tracking | ⚠️ Uses `List<Bar>` not cAlgo `Bars` |
| `ICTSMC.OrderBlockDetector.cs` | 136 | Bullish/bearish OB with freshness window, strength scoring, mitigation | ⚠️ Same — needs cAlgo data adapter |
| `ICTSMC.LiquiditySweepDetector.cs` | 171 | Pool detection (50-bar lookback), sweep strength scoring, ATR-bounded sweep distance | ⚠️ Same |
| `ICTSMC.MarketStructureAnalyzer.cs` | 230 | Swing points, BOS/CHoCH detection, ATR-14 calc, bias determination | ⚠️ Same |
| `ICTSMC.PremiumDiscountClassifier.cs` | 97 | 20-bar equilibrium, premium/discount zone scoring | ⚠️ Same |
| `ICTSMC.ConfluenceEngine.cs` | 367 | Signal scoring (5 detectors + session), R:R calc, rationale generation | ⚠️ Same |
| `ICTSMC.BacktestEngine.cs` | 543 | Bar-by-bar simulation, FTMO risk enforcement (`MaxDailyLossBreached`, `IsMaxDrawdownBreached`), trade journal, metrics | ✅ Standalone, no cAlgo dependency |
| `ICTSMC.BacktestModels.cs` | 231 | `BacktestConfig`, `SimulatedTrade`, `BacktestMetrics`, ExitReason enum | ✅ |
| `ICTSMC.CsvDataLoader.cs` | 218 | Historical OHLCV ingestion from CSV (Dukascopy-style format) | ✅ |
| `ICTSMC.MockData.cs` | 191 | Synthetic trend/range/Choch data for unit tests | ✅ |
| `ICTSMC.Program.cs` | 364 | CLI test runner (`--backtest`, `--report`, `--mr-exit`, etc.) | ✅ Drives the whole thing |
| `ICTSMC.Tests.cs` | 237 | Unit tests for detectors | ✅ |
| `ICTSMC.BacktestTests.cs` | 260 | Integration tests for backtest engine | ✅ |

### 3.1 Key architectural gap

All detectors read `state.Bars` (a `List<Bar>` populated externally). They do **not** subscribe to a cAlgo `Bars` object, do not implement `Robot.OnTick()`, and do not call `ExecuteMarketOrder()`. They are **pure analysis library code** that the CLI harness feeds manually.

To become a live cBot, the wiring work is:

```
cAlgo Bars → bar-aggregator → MarketState.Bars → detectors → ConfluenceEngine
                                                                   ↓
                                              ConfluenceSignal → OrderManager → Trade API
```

This is small in volume (~150 LOC adapter) but high in criticality — every callback boundary needs unit tests.

### 3.2 What already works

`ICTSMC.BacktestEngine.cs` already implements:
- **FTMO daily drawdown** (`MaxDailyLossBreached`, line ~74) tracking via `_currentDay` reset
- **Max total drawdown** (`IsMaxDrawdownBreached`) tracking `_peakBalance`
- **Position sizing** via `BacktestConfig.RiskPerTradePct`
- **Trade journal** with `ExitReason` enum (TP1/TP2/TP3, SL, signal-flip, end-of-data, max daily loss)
- **Duplicate-signal suppression** (`IsDuplicateSignal`, `MinBarsBetweenTrades`)

This is the **production-shape risk layer**. It's just bound to a simulated order book, not the live one.

---

## 4. cTrader Algo API — current 2026 capabilities

Source: `help.ctrader.com/ctrader-algo/` documentation (fetched 2026-07-08).

### 4.1 Languages

- **C#** — full Algo API, .NET SDK required
- **Python** — full Algo API, native interpreter, "the only major platform offering native integration out of the box" (per docs)
- Both languages expose the same `Algo API` surface: `api.Symbol`, `api.Positions`, `api.ExecuteMarketOrder`, `api.Indicators.*`, etc.
- **Plugin SDK**: web plugins (HTML/JS) via hosted URL + plugin SDK; desktop plugins via Algo API in C#/Python

### 4.2 Algo types

| Type | Purpose | Backtestable | Cloud-runnable |
|---|---|---|---|
| **cBot** | Automated strategy, instance-bound to a chart, runs `on_tick` / `on_bar_closed` | ✅ Built-in | ✅ (cTrader Web/Mobile) |
| **Custom indicator** | Computed series, drawn on chart, reusable from cBots | ✅ (via host cBot) | ✅ |
| **Plugin** | UI extension, can place orders with user consent | ❌ | ❌ (local only) |

For our use case: **cBot = execution**, **indicator = visualization/analysis**, **plugin = bridge to Python if we choose that path**.

### 4.3 Data feeds

- `OnTick()` fires per tick (bid/ask change) — verified in C# and Python docs
- `Bars` object: built-in timeframes (M1, M2-M30, H1-H12, Daily, Weekly, Monthly) + custom via `CustomTimeFrame`
- `MarketData.GetBars(TimeFrame, Symbol)` for multi-timeframe context
- Real-time + historical: brokers typically provide 2-5 years of M1 history server-side
- `MarketSessionsChanged` event for session transitions (London/NY/Tokyo/Sydney)

### 4.4 Order execution

- `ExecuteMarketOrder(TradeType, SymbolName, VolumeInUnits, Label, SL, TP)` — atomic with SL/TP
- `PlaceLimitOrder`, `PlaceStopOrder`, `PlaceStopLimitOrder`
- `ModifyPosition(Position, SL, TP)` — for trailing, partial-close math
- Async variants available (`ExecuteMarketOrderAsync`)
- `TradeResult` with `.IsSuccessful`, `.Error` — must check on every call
- **No native trailing stop** — implement in `on_tick` against ATR or swing distance
- **No native OCO/bracket** — manual logic in cBot
- **No native partial close** — split position into multiple positions, or `ModifyPosition` reduced volume on remaining

### 4.5 Backtesting — the 2026 upgrade

Per `help.ctrader.com/ctrader-algo/how-tos/cbots/backtest-a-cbot/`:

| Option | Detail |
|---|---|
| **Data source** | (a) Tick data from server — **most accurate**; (b) M1 from server; (c) M1 from CSV; (d) H1 from server |
| **Spread** | Fixed, current-symbol-tracking, or random in range |
| **Commission** | Per-million-units USD |
| **Modes** | Non-real-time (final results) **or** Real-time visual playback with adjustable speed |
| **Chart types** | Time bars, Renko, range bars |
| **Optimization** | Genetic algorithm (cTrader-native, separate from Ayumi's Optuna pipeline) |
| **Output** | Equity chart, Trade statistics tab (net profit, profit factor, max DD, win rate, avg trade, etc.), Positions, Orders, History, Events, Log, **HTML report export** |

This is materially better than the AYUAA-8 view, which assumed M1-only.

### 4.6 Cloud vs local

- cBots run **locally** on the user's cTrader desktop, or **in the cloud** (cTrader Web/Mobile cloud instance).
- Plugins run **only locally** — relevant if we use the Plugin SDK as a bridge.
- Backtests and optimization run **locally**.

### 4.7 Hard limitations

| Limit | Detail |
|---|---|
| Order rate | **500 orders/min** per cTrader account (demo). Exceeding → **all trading banned for 60s**. AYUAA-8 noted this; still applies. |
| Single-threaded | cBot callbacks (`on_tick`, `on_bar_closed`) are single-threaded. No concurrency primitives. Concurrent state mutation must use locks. |
| No external DB | Local file system + cTrader's own persistence. For richer state (trade journal, equity curve), write to JSON/SQLite via File API. |
| Plugin trading | Requires user permission prompt on first trade. Annoying for unattended. |

---

## 5. Tick resolution and backtest fidelity

### 5.1 What ICT/SMC needs

ICT signals fire on:
- M15/H1/H4 candle closes (structure shifts, BOS, CHoCH) — order of minutes
- M5/M1 wicks for sweep detection — order of seconds
- Real-time price for entry — sub-second

The cAlgo `Bars` object is bar-based, not tick-based. `OnTick` is the only true tick callback. **The right pattern is**:
- Detectors operate on `Bars[TimeFrame.M15]` etc. — fed by `on_bar_closed`
- Liquidity sweep detector ALSO listens to `on_tick` for wick-rejection in real-time (current C# code is bar-based; sweep detection happens once per bar — `ICTSMC.LiquiditySweepDetector.cs` lines 96-122)
- This is acceptable for M5+ sweeps but may miss M1 scalping

### 5.2 Backtest fidelity

- Tick data from server is the gold standard for backtesting. **Available 2026.**
- Our existing `CsvDataLoader.cs` reads Dukascopy tick CSV format — we can pre-stage tick CSVs and run backtests through the cTrader CSV data source option
- Risk: server-side tick history depth varies by broker. FTMO servers typically have 2+ years of M1; less reliable for older data.

### 5.3 What the existing BacktestEngine.cs is missing vs cTrader backtest

| Capability | Our `BacktestEngine` | cTrader backtester |
|---|---|---|
| Real tick replay | ❌ (M1 OHLCV only) | ✅ Tick from server |
| Slippage modeling | ❌ | ✅ (spread option) |
| Commission modeling | ✅ (per-lot config) | ✅ (per-million-units) |
| Optimization | ❌ | ✅ Genetic algorithm |
| Walk-forward | ❌ | ❌ (must script externally) |
| Visual replay | ❌ | ✅ |
| HTML report | ❌ | ✅ |

**Implication**: For signal-quality research, cTrader backtest > our CLI engine. For risk/MC/parameter sweep, our CLI engine + Optuna > cTrader's GA optimizer.

---

## 6. Viability verdict

### 6.1 Primary question: cTrader vs Python as execution platform

| Concern | cTrader cBot | Python (OpenAPI / external) |
|---|---|---|
| Latency | Sub-millisecond (in-process) | 5-50ms (network + serialization) |
| Reliability | Embedded, restart on crash | Independent process, must supervise |
| Backtesting | Built-in tick replay | DIY (have `BacktestEngine` + Dukascopy) |
| Order types | All (market/limit/stop/stop-limit) | All via OpenAPI |
| Trailing stop | Manual (in `on_tick`) | Manual |
| Multi-symbol | One instance per chart | Single process, multiple symbols |
| ML/feature work | Limited (no pandas) | Full Python ecosystem |
| Strategy iteration | Recompile, redeploy | Edit, restart |
| Hardware cost | cTrader running 24/7 | Python daemon |
| Compliance/audit | cTrader's own trade log + our file logger | Our file logger only |

**Verdict**: **cTrader as primary execution**, Python as **decision-support overlay**. The reason is reliability + backtest fidelity + the fact that the existing cBot code is already 95% there. Python's role shrinks to:
1. **Signal validation** — duplicate-detect, regime-check, ML-rank before sending to cBot
2. **Multi-strategy coordination** — pick best signal across ICT/SMC, SR, momentum strategies
3. **Risk overlay** — independent drawdown monitor that can kill-switch the cBot via Plugin SDK
4. **Analytics** — equity curves, walk-forward, parameter sweeps (Optuna)

### 6.2 The hybrid model

```
┌─────────────────────────────────────────────────────────────────┐
│  cTrader Desktop (always on)                                    │
│  ├── ICT/SMC cBot (Python) — primary execution                  │
│  ├── Indicator — visualization of OB/FVG/sweep zones            │
│  └── Plugin SDK bridge — sends signal JSON to Python side       │
└─────────────────────────────┬───────────────────────────────────┘
                              │ WebSocket / Unix socket
┌─────────────────────────────▼───────────────────────────────────┐
│  Python (Ayumi) — decision support                              │
│  ├── signal_engine/confluence_scorer (Python port of ICTSMC)    │
│  ├── risk/ — FTMO enforcement, position sizing                  │
│  ├── orchestrator/ — multi-strategy ranking                     │
│  └── ml/ — regime classification, signal-rank model             │
└─────────────────────────────────────────────────────────────────┘
```

### 6.3 Reject the "primary Python, signal cBot" path

If we tried to make Python primary and cTrader dumb-execution, we'd need:
- A OpenAPI TCP/Protobuf daemon (have it: `src/forex_bot/adapters/ctrader/`)
- Full position management in Python (we have it)
- Risk enforcement in Python (we have it)

But we'd lose:
- Tick-accurate backtesting on platform (have to DIY)
- Visual replay debugging
- Native indicator overlay on chart
- cTrader's own trade log as a backup audit trail
- Cloud execution as a failover (cTrader Mobile)

Not worth it. **cTrader stays primary.**

---

## 7. Hardware / performance requirements

### 7.1 Per-cBot footprint (estimates)

| Resource | Idle (London/NY off-hours) | Active (killzone) |
|---|---|---|
| CPU | <1% of one core | 2-5% of one core |
| RAM | 80-150 MB (cTrader process share) | 150-250 MB |
| Network | Idle | ~50 KB/s tick stream per symbol |
| Disk | <10 MB/day logs | <10 MB/day |

### 7.2 Scaling

- **Per cBot instance**: ~200 MB RAM, 1 core
- **Per symbol on same cBot**: negligible extra (one `Bars` object each)
- **Per instance of the same cBot** (e.g., running on multiple charts): ~200 MB each
- **Heavy optimization** (genetic algo, 1000+ runs): CPU-bound, 10-30 min per sweep on a modern desktop

### 7.3 Recommended host spec (FTMO challenge day-to-day)

| Spec | Minimum | Recommended |
|---|---|---|
| CPU | Quad-core x86 (e.g., i5-8th gen) | 6+ cores (handles optimization parallel runs) |
| RAM | 8 GB | 16 GB (cTrader + Python side + browser) |
| Disk | SSD 256 GB | NVMe 512 GB |
| Network | 10 Mbps up | 50 Mbps up (redundancy) |
| OS | Windows 10/11 or macOS 12+ | Same |
| UPS | — | Recommended (broker/server disconnects during news = unfilled SL = max-DD breach) |

cTrader must stay awake. Recommended setup:
- Dedicated Windows/Mac mini, never sleep
- Auto-login on boot, cBot auto-start on chart open
- Monitor daemon (Python) watching cTrader process — restart if dead
- cTrader Mobile cloud instance as **hot failover** for order path during desktop outage

### 7.4 Latency budgets

| Path | Budget |
|---|---|
| Tick → detector → signal | <5 ms (single bar close, all 5 detectors) |
| Signal → cBot order call | <2 ms |
| cBot → cTrader server (FTMO) | 10-50 ms |
| cBot → Python bridge (if used) | 50-200 ms (WebSocket round trip) |
| Total tick-to-fill on broker | 60-260 ms |

Python bridge adds latency but is **off the critical path** if cBot executes locally and Python only validates/reranks. **Critical rule: if Python is in the tick-to-fill loop, it's wrong.**

---

## 8. Failure modes and known landmines

1. **500-orders/min rate limit** — cBot can loop on `ModifyPosition` (trailing stop). Mitigation: trailing updates only on bar close, not every tick. Or update on distance threshold (e.g., once per 1 pip move).
2. **cTrader desktop crash during news** — `OnException()` should fire `close_all_emergency()`. Test this in staging.
3. **Server disconnect → reconnect** — cTrader handles automatically but position state can desync. Cross-check `Account.Positions` against our trade journal on every `OnStart` and after every reconnect.
4. **Stale Bars reference after timeframe change** — common cBot bug. Always re-subscribe.
5. **Plugin trading permission prompt** — only on first trade; subsequent trades are silent. But if user revoked, cBot silently fails. Detect via `TradeResult.Error` and alert.
6. **CSV-based backtest file format drift** — Dukascopy format occasionally changes. `CsvDataLoader.cs` is one place that breaks silently. **Add a fixture test that catches format changes.**

---

## 9. Recommendations

1. **Promote cBot code to live**: ~150 LOC adapter (`Robot.OnTick` → bar aggregator → detectors → ConfluenceEngine → `ExecuteMarketOrder`). 1-2 days.
2. **Write Python port of detectors** in parallel: `src/forex_bot/ict/` with the same algorithm. Both codebases validate each other.
3. **Use cTrader backtester as truth source** for tick-accurate validation, our `BacktestEngine.cs` for risk-correctness and Monte Carlo.
4. **Build the Plugin SDK bridge** (`src/forex_bot/plugins/ctrader_signal_bridge`) for cBot → Python signal reporting.
5. **Stand up cTrader Mobile cloud instance** as hot failover.
6. **Document FTMO rule changes** — they update rule wording ~2x/year. Review quarterly.

---

## 10. References

**Internal:**
- `src/forex_bot/cbot/` — 14 files, 3,378 lines (read in full for this assessment)
- `docs/research/assess-ctrader-calgo-api-and-design-automated-ict-smc-architecture.md` — AYUAA-8 (prior)
- `docs/forex/strategy-development-ict-smc-automated-logic.md` — AYUAA-27 spec
- `docs/forex/backtesting-framework-ctrader-calgo.md` — AYUAA-31 spec
- `docs/research/ict/` — 10 sub-docs on ICT primitives
- `src/forex_bot/adapters/ctrader/` — 43-file cTrader OpenAPI adapter (already used by Python side)

**External (fetched 2026-07-08):**
- `help.ctrader.com/ctrader-algo/` — Algorithmic trading using cTrader Algo
- `help.ctrader.com/ctrader-algo/documentation/cbots/` — cBot lifecycle, Python + C#
- `help.ctrader.com/ctrader-algo/documentation/python-basics/` — Native Python cBot support
- `help.ctrader.com/ctrader-algo/how-tos/cbots/backtest-a-cbot/` — Tick-from-server backtest, visual mode, optimization, HTML report
- `help.ctrader.com/ctrader-algo/documentation/plugins/` — Plugin SDK for cBot↔Python bridge

---

*End of assessment. See `docs/designs/ict-smc-architecture.md` for the architecture design that builds on this verdict.*