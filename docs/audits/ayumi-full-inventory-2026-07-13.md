# Ayumi Full Inventory — 2026-07-13

## 1. Repository Structure

### Source Trees (PROBLEM: 3 parallel trees)

| Tree | Files | Status | Action |
|------|-------|--------|--------|
| `src/forex_bot/` | 300 .py | **ACTIVE** — main codebase | Keep, clean |
| `src/forex_trading/` | 31 .py | **ORPHANED** — self-referencing only, not imported by main tree or tests | Remove or merge if any value |
| `src/crypto/` | 12 .py | **SEPARATE** — Cabal crypto bot, different workstream | Move to own repo or `src/cabal/` |
| `_archive/` | 65 .py | **DEAD** — stale backup from Apr 22 | Delete (git history preserves it) |
| `src/forex_bot/_deprecated/` | ? | **DEAD** | Delete |
| `src/forex_bot/backtest/strategy_legacy.py` | 1 | **DEAD** — legacy | Delete |

### Key Source Modules (`src/forex_bot/`)

```
src/forex_bot/
├── adapters/          — cTrader adapter, open API
├── analysis/          — analytics modules
├── analytics/         — regime detection, correlation, session logic, pattern detection
├── backtest/          — engine, enhanced_engine, walk_forward, portfolio_blend, ftmo_guard, data_loader, db_data_loader, parameter_sweep, ict_smc/, trade_management/, strategies/
├── cbot/              — (empty/legacy?)
├── common/            — shared utilities, logging config
├── confidence/        — confidence engine
├── config/            — configuration
├── core/              — engine core, types, indicators, pip calculator, trade management
├── data/              — data layer
├── engine/            — signal engine
├── forward_test/      — forward test infrastructure
├── hybrid/            — hybrid engine
├── indicators/        — technical indicators
├── ml/                — ML pipeline (features, train, predict, blend_optimizer, confidence_learner)
├── models/            — data models
├── monitoring/        — health monitor, cycle detector, drift detector
├── orchestrator/      — orchestration
├── overlays/          — DXY overlay, etc.
├── policy/            — behavioral policy
├── quant/             — quant pipeline, calibration, ICIR, OOS gate, DSR
├── reporting/         — reporting
├── risk/              — risk engine, FTMO guard, kill switch, position sizing, stop target
├── signal_engine/     — signal generation, signal stats, signal router
├── signals/           — signal definitions
├── srf/               — strategy research framework (walk-forward, Monte Carlo, PBO)
├── storage/           — storage layer
├── strategies/        — 16 strategy files + registry
├── utils/             — utilities
└── tests/             — inline tests (separate from /tests/)
```

### Strategies (16 in main tree)

| Strategy | Edge Doc | SRF Runs | Status |
|----------|----------|----------|--------|
| `bb_rsi_reversion` | ✓ | ✓ | Active |
| `donchian_atr_trend` | ✓ | — | New (Jul 12) |
| `dual_tf_squeeze_pro` | ✓ | — | Recent |
| `killzone_momentum` | ✓ | ✓ | Active |
| `london_breakout_retest` | ✓ | — | New (Jul 12) |
| `momentum` | — | — | Legacy? |
| `mtf_filtered_momentum` | — | — | Unknown |
| `rsi_threshold` | — | — | Legacy? |
| `session_breakout` | — | — | Legacy? |
| `session_range_mean_reversion` | — | ✓ | Active |
| `session_range_mr_ict_filtered` | — | — | Variant |
| `srmr_plus` | ✓ | ✓ | Active |
| `test_canary` | — | — | Test only |
| `ttc_xauusd` | ✓ | ✓ | Active |
| `volatility_regime_breakout` | ✓ | ✓ | Active |
| `volatility_squeeze` | ✓ | ✓ | Active |

### Scripts (28+ run scripts, many one-off)

**Keep (core pipeline):**
- `aggregate_ticks_to_bars.py` — tick→bar aggregation
- `download_dukascopy.py` / `download_dukascopy_crisis.py` — data download
- `download_ctrader_data.py` — cTrader data download
- `import_ticks.py` / `init_tick_db.py` — tick import
- `duckdb_migrate.py` — DB migration
- `run_test_scope.sh` — test runner
- `daily_audit.py` / `ftmo_daily.py` — daily audit
- `kill_switch.py` / `kill_switch_watchdog.py` — safety
- `health_check_tick_pipeline.py` — health check
- `pinned_eval_harness.py` — evaluation
- `launch_forward_test_v2.py` — latest forward test launcher

**Remove (one-off sweeps/stale):**
- `run_bollinger_focused.py`, `run_high_conviction_sweep.py`, `run_keltner_sweep_walkforward.py`
- `run_m15_ict_last_chance.py`, `run_ml_backtest_m15.py`, `run_momentum_sweep_walkforward.py`
- `run_scalper_m5_walkforward.py`, `run_session_range_mr_optuna.py`, `run_session_range_mr_regime_sweep.py`
- `run_srmr_plus_focused.py`, `run_srmr_plus_full_pipeline.py`
- `run_supertrend_rsi_parameter_sweep.py`, `run_supertrend_rsi_walkforward.py`
- `run_trailing_stop_sweep.py`, `run_tts_walkforward.py`, `run_vaps_ab_walkforward.py`
- `run_volatility_squeeze_sweep_walkforward.py`, `run_vrb_walkforward.py`
- `sweep_momentum_breakout_h1.py`, `walk_forward_*.py` (3 files)
- `phase3b_*.py` (in __pycache__ only, already deleted)
- `scripts/_deprecated/` — entire directory
- `launch_forward_test.py`, `launch_forward_test_preloaded.py`, `launch_blend_forward_test.py` — old versions
- `t3d_lot_test.py`, `spike_dual_client.py`, `live_test_fire.py`
- `test_*.py` scripts (5 files — these are scripts not pytest tests)
- `close_all_positions.py`, `close_all_demo_positions.py` (keep one)
- `compare_tick_quality.py`, `investigate_ttc_xauusd.py` (one-off investigations)
- `audit_tts_trades.py`, `categorize_losses.py`, `audit_bar_close.py`
- `generate_cohort_dashboard.py`, `kanban_hygiene.py`
- `extract_vtt_v2.py` (unrelated to trading)
- `ctrader_callback_linter.py`, `ctrader_credential_probe.py`, `probe_ctrader_credentials.py`
- `synthesize_timeframes.py`, `migrate_csv_to_duckdb.py`, `backfill_bars_to_timescaledb.py`

## 2. Data Layer

### DuckDB: `data/ayumi_market.duckdb` (53 GB)

| Table | Rows | Description |
|-------|------|-------------|
| `ticks` | 429,322,199 | Raw tick data |
| `bars` | 306,190,674 | Aggregated OHLCV bars |
| `import_log` | 1,878 | Import audit trail |

### DuckDB: `data/research/research.duckdb` (5.1 MB)

| Table | Rows | Description |
|-------|------|-------------|
| `strategies` | 8 | Registered strategies |
| `runs` | 144 | SRF walk-forward runs |
| `metrics_summary` | 63 | Aggregated metrics |
| `v_top_strategies` | 31 | Top strategy view |
| `v_pair_performance` | 3 | Per-pair performance |
| `v_promotion_queue` | 0 | Strategies ready for promotion |
| Other tables | 0 | Monte Carlo, portfolio_runs, trades, etc. (empty) |

### Other Data Files

| File | Purpose |
|------|---------|
| `data/trading.db` | SQLite — operational state |
| `data/workboard.db` | SQLite — workboard |
| `data/crypto/copy_trading.db` | SQLite — Cabal crypto |
| `data/forex/parquet/*.parquet` | 5 parquet files (USDJPY 1h/1d, EURUSD 1h, GBPUSD 1h, AUDUSD 1h) |
| `data/forward_test_health.json` | Forward test state |
| `data/risk_state_blend.json` | Risk state |
| `data/signal_stats.jsonl` | Signal statistics |
| `data/edge_telemetry.jsonl` | Edge telemetry |
| `data/decision_log.jsonl` | Decision log |
| `data/lifecycle_log.jsonl` | Lifecycle log |

## 3. Documentation (223+ files — CHAOS)

### Problem: 6+ overlapping plan/quest docs from July alone

| Document | Date | Purpose | Status |
|----------|------|---------|--------|
| `docs/plans/quest-ayumi-ftmo-2026-07-07.md` | Jul 7 | Quest v2 (full phases) | Superseded? |
| `docs/plans/quest-ayumi-ftmo-roadmap-2026-07-08.md` | Jul 8 | Post-sprint status | Latest quest status |
| `docs/plans/quest-ayumi-ftmo-phase-update-2026-07-08.md` | Jul 8 | Phase update | Redundant? |
| `docs/plans/quest-ayumi-ftmo-reviews-synthesis-2026-07-08.md` | Jul 8 | Council synthesis | Reference |
| `docs/plans/quest-pivot-final-synthesis-2026-07-08.md` | Jul 8 | Pivot synthesis | Key decisions |
| `docs/plans/multi-strategy-blend-plan-2026-07.md` | Jul 12 | Blend plan | Active |
| `docs/research/ftmo-risk-and-port-sizing-2026-07.md` | Jul | FTMO risk | Reference |

### Doc Categories to Consolidate/Remove

| Category | Count | Action |
|----------|-------|--------|
| `docs/media/` | 17 files | **Remove** — TikTok/Discord/YouTube content plans, not trading |
| `docs/decisions/ARCHIVED/` | 10 files | **Remove** — early planning, all archived |
| `docs/plans/` (old sprints) | 15+ files | **Archive** — keep latest, archive rest |
| `docs/forex/` (legacy) | 20+ files | **Consolidate** — many from early research phases |
| `docs/post-mortems/` | 10 files | **Keep** — valuable for learning |
| `docs/audits/` | 8 files | **Keep** — recent audits |
| `docs/edges/` | 8 files | **Keep** — strategy hypothesis docs |
| `docs/research/` | 20+ files | **Consolidate** — mix of trading and non-trading |
| `docs/runbooks/` | 6 files | **Consolidate** — need single test runbook |
| `docs/p5a/` | 4 files | **Archive** — completed phase |
| `docs/closeouts/` | 4 files | **Archive** — completed phase |
| `docs/sources/flight-logs/` | 6 files | **Remove** — unrelated content |

## 4. Test State

- **5,808 tests collected** (1 collection error in `test_transport_cooldown.py`)
- **5 deselected** (marked `live`)
- Test structure:
  - `tests/unit/` — unit tests by module
  - `tests/integration/` — integration tests (cTrader, connection, etc.)
  - `tests/e2e/` — end-to-end tests (backtest, forward test, live)
  - `tests/strategies/` — strategy tests
  - `tests/regression/` — regression tests
  - `tests/forex_bot/` — forex_bot specific
- `scripts/run_test_scope.sh` exists but no documented runbook

## 5. cTrader Integration

- **Order chain proven** (Jun 25, 2026 — commits 177116b, 4ecc973)
- Token lifecycle: assume fresh if expires_at missing
- Multiple launch scripts (need to consolidate to one)
- Forward test infrastructure exists with health monitoring

## 6. Current Pipeline State (Now → End)

### What Works NOW
1. ✅ Tick data download (Dukascopy + cTrader)
2. ✅ Tick storage in DuckDB (429M ticks)
3. ✅ Bar aggregation (306M bars)
4. ✅ 16 strategies implemented
5. ✅ 8 strategies with edge hypothesis docs
6. ✅ SRF walk-forward framework (144 runs)
7. ✅ Portfolio blend engine
8. ✅ FTMO guard module
9. ✅ cTrader order execution chain
10. ✅ Forward test infrastructure
11. ✅ Risk management (position sizing, kill switch, daily audit)

### What's BROKEN or MISSING
1. ❌ **No canonical roadmap** — 6+ overlapping docs, none authoritative
2. ❌ **No test runbook** — unclear what to run when
3. ❌ **No data pipeline runbook** — how to download, aggregate, validate
4. ❌ **Orphaned `forex_trading/` tree** — 31 files of dead code
5. ❌ **53GB DuckDB** — no retention/management policy
6. ❌ **No clear strategy → FTMO path** — blend plan exists but not wired end-to-end
7. ❌ **FTMO trailing guard** — carded but not implemented
8. ❌ **M3 timeframe data** — carded but not downloaded
9. ❌ **Portfolio blend driver** — carded but not wired to forward test
10. ❌ **VTX session fix** — carded but not done
11. ❌ **Repo cleanup** — dead code, stale docs, media content
12. ❌ **No CI/CD** — no automated test runs on push
13. ❌ **News blackout** — carded but not implemented
14. ❌ **Best day rule** — carded but not verified in live
15. ❌ **Quant analysis pipeline** — bootstrap CI exists, multiple testing correction exists, but not wired as standard procedure

## 7. Key Decisions Needed

1. **FTMO account type**: 1-Step vs 2-Step (Craig pending decision)
2. **FTMO account tier**: Standard vs Swing
3. **Strategy selection**: Which 3-5 of the 16 make the final blend?
4. **Data scope**: How many years of history per pair?
5. **`forex_trading/` tree**: Delete outright or salvage anything?
6. **`crypto/` tree**: Move to separate repo or keep as `src/cabal/`?
7. **Media docs**: Delete or move to separate docs repo?

---

*Generated 2026-07-13 17:25 EDT by Ava. This is the baseline — the canonical roadmap will supersede all prior plan docs.*
