# P5A-TEST-MOD: Test Modularization Plan (v2 — Revised)

**Card:** `adb55aac` — P5A-TEST-MOD — Test modularization audit + split  
**Repo:** `$AYUMI_ROOT/`  
**Main HEAD:** `f87e181fe569ca178423a27c60dfa016eb500974`  
**Generated:** 2026-06-26  
**Planner:** Autobuild Stage 1 subagent  
**Revised:** 2026-06-26 — Council review amendments (Kaito, Liora, Rei)

---

## Revision Summary

This is a full rewrite of the plan based on council review findings. Key changes from v1:

| # | Amendment | Source |
|---|-----------|--------|
| 1 | Heavy-import count corrected from 26 → **40** (using corrected grep pattern) | Liora |
| 2 | Complete file manifest generated as TSV — no "etc." anywhere | Liora |
| 3 | `tests/unit/` subcategorized into core/risk/data/ict/analytics/execution/hybrid | Kaito |
| 4 | Integration path flattened: `tests/integration/ctrader/` not `tests/integration/adapters/ctrader/` | Rei |
| 5 | All category file lists expanded to be complete | Liora |
| 6 | Path navigation audit + per-task fix sub-tasks + `pythonpath` fix | Rei |
| 7 | `py_compile` replaced with `pytest --collect-only` in all acceptance criteria | Rei |
| 8 | Runner script redesigned with `--collect-only`, mutual exclusivity, markers, `python3` | Rei |
| 9 | All file counts corrected to match verified manifest data | All |

---

## Data Audit (Verified)

### Raw counts
- Total `.py` test files: **225** (verified with `find tests/ -name '*.py' -type f`)
- Files in `tests/` root (flat): **187**
- Files in existing subdirectories: **38**
  - `tests/unit/`: 9 (plus `__init__.py`)
  - `tests/regression/`: 4 (including `conftest.py`, `generate_golden.py`, `__init__.py`)
  - `tests/adapters/ctrader/`: 10 (including `__init__.py`)
  - `tests/fixtures/`: 2 (including `__init__.py`)
  - `tests/strategies/`: 1 (plus `tests/strategies/ict/`: 4 including `__init__.py`)
  - `tests/test_hybrid/`: 7 (including `__init__.py`)

### Heavy-import audit — 40 files (corrected)

**Grep command used:**
```bash
grep -rlE '^\s*(import|from)\s+(numpy|pandas|scipy|sklearn)' tests/ --include='*.py'
```

This pattern catches both module-level AND lazy/inline imports (the v1 plan used a pattern that only caught module-level imports, missing 14 files).

**Breakdown by library:**
- pandas: 30 files
- numpy: 28 files
- sklearn: 1 file (`tests/test_ml_pipeline.py`)
- scipy: 0 files

**All 40 heavy-import files with import scope:**

| File | Libraries | Scope |
|------|-----------|-------|
| `tests/fixtures/historical_strategy_pnls.py` | numpy | module-level |
| `tests/regression/conftest.py` | numpy,pandas | module-level |
| `tests/regression/generate_golden.py` | numpy,pandas | module-level |
| `tests/regression/test_regression_metrics.py` | numpy | module-level |
| `tests/test_adx_calculation.py` | numpy,pandas | module-level |
| `tests/test_amalgamation_ict_only.py` | numpy,pandas | module-level |
| `tests/test_backfill.py` | pandas | module-level |
| `tests/test_backtest_engine.py` | numpy,pandas | module-level |
| `tests/test_cointegration.py` | numpy | module-level |
| `tests/test_correlation.py` | numpy,pandas | module-level |
| `tests/test_ctrader_client.py` | pandas | module-level |
| `tests/test_data_source.py` | pandas | module-level |
| `tests/test_high_conviction_strategy.py` | numpy,pandas | module-level |
| `tests/test_htf_analyzer.py` | numpy | module-level |
| `tests/test_ict_features.py` | numpy,pandas | module-level |
| `tests/test_indicators.py` | numpy,pandas | module-level |
| `tests/test_keltner_channel_breakout.py` | numpy,pandas | module-level |
| `tests/test_killzone_momentum_strategy.py` | numpy,pandas | module-level |
| `tests/test_ml_mean_reversion.py` | numpy,pandas | module-level |
| `tests/test_ml_pipeline.py` | numpy,pandas,sklearn | module-level |
| `tests/test_momentum_breakout_strategy.py` | numpy,pandas | module-level |
| `tests/test_parquet_integration.py` | pandas | module-level |
| `tests/test_regime_switching_router.py` | numpy,pandas | module-level |
| `tests/test_risk.py` | numpy,pandas | module-level |
| `tests/test_session_range_mean_reversion_strategy.py` | numpy,pandas | module-level |
| `tests/test_session_range_mr_regime_filter.py` | numpy,pandas | module-level |
| `tests/test_signal_engine_untested_components.py` | pandas | module-level |
| `tests/test_srmr_plus.py` | numpy,pandas | module-level |
| `tests/test_stat_arb_strategy.py` | numpy,pandas | module-level |
| `tests/test_strategies.py` | numpy,pandas | module-level |
| `tests/test_supertrend_rsi_blend_strategy.py` | numpy,pandas | module-level |
| `tests/test_sweep_runner.py` | numpy,pandas | module-level |
| `tests/test_swing_detector.py` | numpy | module-level |
| `tests/test_tts_strategy.py` | numpy,pandas | module-level |
| `tests/test_usdjpy_d1_trend_strategy.py` | numpy,pandas | module-level |
| `tests/unit/test_engine_core.py` | numpy,pandas | module-level |
| `tests/unit/test_engine_v2.py` | numpy,pandas | module-level |
| `tests/unit/test_indicators.py` | numpy,pandas | module-level |
| `tests/unit/test_mixins.py` | pandas | module-level |
| `tests/unit/test_trade_mgmt.py` | pandas | module-level |

All 40 files use module-level imports (none are lazy/inline).

### Path navigation audit

- Files with `sys.path.insert` or `sys.path.append`: **65 files** (81 total call sites)
- Files with `Path(__file__)`: **44 references** across files

**Root cause:** Tests manually inject `src/forex_bot` and `src` into `sys.path` to import project modules. After files move into subdirectories, these relative path calculations break because `Path(__file__).parent.parent` resolves to a different directory.

**Fix strategy (Task 7):**
1. Add `tests` to `pythonpath` in `pytest.ini` (currently `src/forex_bot src`, becomes `src/forex_bot src tests`)
2. Create `tests/_project_root.py` helper for data-path construction
3. Remove all `sys.path.insert`/`sys.path.append` calls — they become redundant once `pythonpath` includes `src/forex_bot` and `src`
4. Replace `Path(__file__)`-based path construction with `from _project_root import PROJECT_ROOT`

---

## Categorization Scheme

### Target layout

| Category | Path | Test files | Heavy | What belongs |
|----------|------|-----------|-------|--------------|
| **unit/core** | `tests/unit/core/` | 34 | 9 | Engine, indicators, types, calculators, mixins, trade mgmt, backtest types, logging, data loaders, ML confidence, pipeline features, parameter sweep, blend optimizer/runner |
| **unit/risk** | `tests/unit/risk/` | 13 | 1 | Position sizing, risk, kill switch, confidence engine, gate tuner/validator, stop target, TP manager, profile router, state persistence |
| **unit/data** | `tests/unit/data/` | 4 | 4 | Loaders, sources, backfill, parquet, data source, historical client |
| **unit/ict** | `tests/unit/ict/` | 3 | 0 | ICT-specific pure logic: H4 context, volume delta, multi-session M/W |
| **unit/analytics** | `tests/unit/analytics/` | 16 | 5 | Session analyzers, HTF analyzer, regime, correlation, cointegration, swing detector, VAPS, statistical validation, pattern detector, ML pipeline, quant pipeline |
| **unit/execution** | `tests/unit/execution/` | 4 | 0 | Paper trader, position monitor, position tracker, execution mode |
| **unit/hybrid** | `tests/unit/hybrid/` | 6 | 0 | Hybrid strategy engine, CLI, risk manager, signal, paper trader, integration |
| **integration** | `tests/integration/` | 42 | 0 | cTrader adapter, connection manager, order gateway, execution wiring, market data feed, token lifecycle, credentials, reconnect, copy trading, protocols, session, error classifier, health monitor, signal router, rejection circuit breaker |
| **integration/ctrader** | `tests/integration/ctrader/` | 9 | 0 | P5A cTrader adapter tests: account state, auth errors, environment, execution permission, volume decoder, characterization, integration, signal adapter, token lifecycle wiring |
| **strategies** | `tests/strategies/` | 46 | 16 | Strategy implementations, signal engine, regime routing, strategy adapter/executor/registry, confluence, spread, ML strategy tests, ICT features, Kelly criterion, statistical study |
| **strategies/ict** | `tests/strategies/ict/` | 3 | 0 | ICT pure logic: FVG, order block, confluence |
| **e2e** | `tests/e2e/` | 31 | 1 | Full backtest runs, forward test, live trading, portfolio, walk-forward, multi-symbol engine, resilience integration |
| **regression** | `tests/regression/` | 3 | 2 | Regression metrics, golden file generation, statistical validation regression |
| **fixtures** | `tests/fixtures/` | 1 | 1 | Shared fixtures (`historical_strategy_pnls.py`) |
| **infra** (non-test) | various | 10 | 1 | `__init__.py`, `conftest.py` files — stay in place or move with their directory |

**Totals:** 215 testable files + 10 infrastructure = 225 total

### Why not a separate `tests/heavy/`?

A functional split makes category scopes meaningful. Heavy import isolation is handled by the runner (`--heavy` flag). Distributing heavy files across categories keeps each functional area honest about its dependencies. The heaviest categories (strategies: 16 heavy, unit/core: 9 heavy) can still be run in isolated pytest processes via `run_test_scope.sh`.

### Why flatten integration/ctrader/?

The v1 plan proposed `tests/integration/adapters/ctrader/` — a 4-deep path that makes `parents[N]` indexing fragile. Using `tests/integration/ctrader/` keeps max depth at 3 (`tests/integration/ctrader/file.py` → `parents[3]` = project root).

### Authoritative manifest

The complete file-by-file manifest is at `docs/p5a/test-move-manifest.tsv`. Format: tab-separated with columns `source_path`, `target_path`, `category`, `heavy_import`, `import_scope`. Every test file appears exactly once. Builders read this file — the prose lists below are supplementary.

---

## Task Breakdown

All tasks use `git mv` for moves. No test logic is modified — only file location and path-navigation fixes.

### Task 1: Prepare target directory skeleton (0.5 SP)
- **Scope:** Create new subdirectories and `__init__.py` files:
  ```
  tests/unit/core/
  tests/unit/risk/
  tests/unit/data/
  tests/unit/ict/
  tests/unit/analytics/
  tests/unit/execution/
  tests/unit/hybrid/
  tests/integration/
  tests/integration/ctrader/
  tests/e2e/
  ```
  Create empty `__init__.py` in each. Remove `tests/adapters/` directory (files move to `tests/integration/ctrader/`).
- **Acceptance:**
  - `find tests/ -type d` lists all expected categories.
  - Each new category directory has an `__init__.py`.

### Task 2: Move unit-scope tests into `tests/unit/` subcategories (2 SP)
- **Scope:** Move 80 files into unit subcategories per the manifest.
  - **unit/core/ (34 files):** `test_adx_calculation.py`, `test_atr_provider.py`, `test_asia_session_analyzer.py` (no — analytics), ... (see manifest)
  - **unit/risk/ (13 files):** `test_risk.py`, `test_risk_sizer.py`, `test_sl_position_sizer.py`, `test_kill_switch.py`, `test_kill_switch_auto.py`, `test_position_sizing.py`, `test_profile_router.py`, `test_state_persistence.py`, `test_confidence_engine.py`, `test_gate_tuner.py`, `test_gate_validator.py`, `test_stop_target.py`, `test_tp_manager.py`
  - **unit/data/ (4 files):** `test_backfill.py`, `test_parquet_integration.py`, `test_data_source.py`, `test_ctrader_client.py`
  - **unit/ict/ (3 files):** `test_h4_context.py`, `test_volume_delta.py`, `test_multi_session_mw.py`
  - **unit/analytics/ (16 files):** `test_asia_session_analyzer.py`, `test_htf_analyzer.py`, `test_correlation.py`, `test_cointegration.py`, `test_regime_detection.py`, `test_regime.py`, `test_mtf_regime.py`, `test_swing_detector.py`, `test_vaps.py`, `test_statistical_validation.py`, `test_trend_atr_filter.py`, `test_pattern_detector.py`, `test_session_logic.py`, `test_level_counter.py`, `test_quant_pipeline.py`, `test_ml_pipeline.py`
  - **unit/execution/ (4 files):** `test_paper_trader.py`, `test_position_monitor.py`, `test_position_tracker.py`, `test_execution_mode.py`
  - **unit/hybrid/ (6 files):** all files from `tests/test_hybrid/` (drop `test_` prefix on directory name)
  - Also move existing `tests/unit/*.py` into appropriate subcategory (mostly unit/core/)
  - **Name collision note:** `tests/test_indicators.py` (tests `ml/features.py`) and `tests/unit/test_indicators.py` (tests `indicators/` module) have the same basename. The root-level one is renamed to `test_ml_indicators.py` on move.
- **Sub-task 2a:** Fix all `__file__`-relative path navigation in moved files (replace `sys.path.insert` calls, update `Path(__file__).parent.parent` references)
- **Files:** 80 files (see manifest for exact source→target mapping)
- **Acceptance:**
  - `pytest --collect-only tests/unit/` succeeds and discovers expected test items.
  - `grep -rn "sys\.path\.\(insert\|append\)" tests/unit/` returns nothing (all removed).
  - No `from test_hybrid` or `from tests.test_hybrid` imports remain.

### Task 3: Move integration tests into `tests/integration/` (1.5 SP)
- **Scope:** Move 51 files (42 root-level + 9 from `tests/adapters/ctrader/`) into `tests/integration/` and `tests/integration/ctrader/`.
  - **integration/ (42 files):** `test_ctrader_adapter.py`, `test_ctrader_api_client.py`, `test_ctrader_api_client_class.py`, `test_ctrader_connection.py`, `test_ctrader_execution_v2.py`, `test_ctrader_live_execution.py`, `test_ctrader_market_data_feed.py`, `test_ctrader_models.py`, `test_ctrader_order_manager.py`, `test_ctrader_paper_trader.py`, `test_ctrader_risk_guard.py`, `test_connection_manager.py`, `test_connection_manager_wiring.py`, `test_connection_self_healing.py`, `test_connection_watchdog.py`, `test_concurrent_sessions.py`, `test_token_lifecycle.py`, `test_token_lifecycle_safety.py`, `test_order_gateway.py`, `test_execution_event_handler.py`, `test_trade_execution.py`, `test_market_data_feed.py`, `test_open_api_client.py`, `test_open_api_spot_feed.py`, `test_credential_probe.py`, `test_credential_store.py`, `test_reconnect_logic.py`, `test_reconnect_strategy.py`, `test_symbol_discovery.py`, `test_protocols.py`, `test_session.py`, `test_trade_journal.py`, `test_signal_router.py`, `test_rejection_circuit_breaker.py`, `test_signals_traded_counter.py`, `test_copy_trading_api.py`, `test_copy_trading_models.py`, `test_copy_trading_services.py`, `test_xauusd_pip_calculation.py`, `test_bar_builder.py`, `test_error_classifier.py`, `test_health_monitor.py`
  - **integration/ctrader/ (9 files):** `test_account_state.py`, `test_auth_error_types.py`, `test_environment.py`, `test_execution_permission.py`, `test_open_api_volume_decoder.py`, `test_p5a_characterization.py`, `test_p5a_integration.py`, `test_signal_adapter_strategy_id.py`, `test_token_lifecycle_wiring.py` (all from `tests/adapters/ctrader/`)
- **Sub-task 3a:** Fix all `__file__`-relative path navigation in moved files
- **Acceptance:**
  - `pytest --collect-only tests/integration/` succeeds.
  - `grep -rn "sys\.path\.\(insert\|append\)" tests/integration/` returns nothing.
  - No `from adapters.ctrader` or `from tests.adapters` imports remain (replaced with direct imports via `pythonpath`).

### Task 4: Move strategy tests into `tests/strategies/` (1.5 SP)
- **Scope:** Move 46 files into `tests/strategies/` plus 3 existing ICT files stay.
  - **strategies/ (46 files):** `test_bb_rsi_reversion.py`, `test_gbpusd_bb_reversion.py`, `test_commodity_strategies.py`, `test_grid_strategy.py`, `test_grid_trading.py`, `test_high_conviction_strategy.py`, `test_hybrid_strategy.py`, `test_killzone_momentum_strategy.py`, `test_keltner_channel_breakout.py`, `test_momentum_breakout_strategy.py`, `test_rsi_strategy.py`, `test_session_range_gbpusd.py`, `test_session_range_mean_reversion_strategy.py`, `test_session_range_mr_ict_filtered.py`, `test_session_range_mr_regime_filter.py`, `test_srmr_plus.py`, `test_stat_arb_strategy.py`, `test_supertrend_rsi_blend_strategy.py`, `test_tts_strategy.py`, `test_usdjpy_d1_trend_strategy.py`, `test_volatility_squeeze.py`, `test_strategies.py`, `test_amalgamation_ict_only.py`, `test_lod_hod_stop_buffer.py`, `test_transaction_costs.py`, `test_wednesday_reversal.py`, `test_regime_switching_router.py`, `test_signal_orchestrator.py`, `test_signal_validator.py`, `test_signal_stats.py`, `test_signal_output.py`, `test_strategy_adapter.py`, `test_strategy_executor.py`, `test_strategy_registry.py`, `test_strategy_logging.py`, `test_confluence_detector.py`, `test_confluence_scorer.py`, `test_spread_classifier.py`, `test_spread_regime_classifier.py`, `test_ml_mean_reversion.py`, `test_ict_features.py`, `test_runner_bugs.py`, `test_statistical_study.py`, `test_kelly_criterion.py`, `test_kelly_backtest_integration.py`, `test_session_breakout_tp.py` (already in strategies/)
  - **strategies/ict/ (3 files):** stay in place (`test_confluence.py`, `test_fvg.py`, `test_order_block.py`)
- **Sub-task 4a:** Fix all `__file__`-relative path navigation in moved files
- **Acceptance:**
  - `pytest --collect-only tests/strategies/` succeeds.
  - `grep -rn "sys\.path\.\(insert\|append\)" tests/strategies/` returns nothing.

### Task 5: Move E2E tests into `tests/e2e/` (1 SP)
- **Scope:** Move 31 files:
  - `test_backtest_engine.py`, `test_blend_backtest.py`, `test_portfolio.py`, `test_portfolio_backtest.py`, `test_portfolio_blend.py`, `test_walk_forward.py`, `test_walk_forward_runner.py`, `test_regime_walk_forward.py`, `test_grid_backtest.py`, `test_backtest_q1_3_to_1_rr.py`, `test_backtest_q1_mw_study.py`, `test_backtest_q2_lod_hod_stop_rate.py`, `test_forward_test_engine_credentials.py`, `test_forward_test_engine_credentials_failures.py`, `test_forward_test_engine_reconnect.py`, `test_forward_test_engine_wiring.py`, `test_forward_test_live_execution.py`, `test_forward_test_live_execution_outcomes.py`, `test_launch_forward_test_v2.py`, `test_live_ctrader.py`, `test_live_market_data_integration.py`, `test_live_trading_execution.py`, `test_live_trading_monitor.py`, `test_multi_strategy_forward.py`, `test_multi_symbol_engine.py`, `test_multitf_forward_test.py`, `test_resilience_integration.py`, `test_vaps_engine.py`, `test_selective_pairing.py`, `test_backtest_engine_close_all.py`, `test_portfolio_risk_guard.py`
- **Sub-task 5a:** Fix all `__file__`-relative path navigation in moved files
- **Acceptance:**
  - `pytest --collect-only tests/e2e/` succeeds.
  - `grep -rn "sys\.path\.\(insert\|append\)" tests/e2e/` returns nothing.

### Task 6: Consolidate regression + fixtures (0.5 SP)
- **Scope:** Move `test_statistical_validation_regression.py` → `tests/regression/`. Verify existing regression files stay. Verify `tests/fixtures/` unchanged. Remove empty `tests/adapters/` directory after Task 3.
- **Sub-task 6a:** Fix path navigation in moved file
- **Acceptance:**
  - `pytest --collect-only tests/regression/` succeeds.
  - `tests/fixtures/historical_strategy_pnls.py` still at original path.

### Task 7: Update `pytest.ini`, create `_project_root.py`, conftest cleanup (1.5 SP)
- **Scope:**
  1. Update `pytest.ini`:
     ```ini
     pythonpath = src/forex_bot src tests
     ```
     (add `tests` to existing `pythonpath`)
  2. Create `tests/_project_root.py`:
     ```python
     """Centralized project root for path construction in tests."""
     from pathlib import Path
     PROJECT_ROOT = Path(__file__).resolve().parent.parent  # tests/ → project root
     ```
  3. Update `tests/conftest.py` — ensure it doesn't rely on specific file locations that have moved. Audit for any hard-coded paths.
  4. Verify that `norecursedirs` excludes `_archive`, `worktrees`, etc.
- **Acceptance:**
  - `pytest --collect-only tests/` completes and discovers all 215 testable files.
  - No duplicate test collection from old/new paths.
  - `python3 -c "from _project_root import PROJECT_ROOT; print(PROJECT_ROOT)"` works from `tests/` context.

### Task 8: Create `scripts/run_test_scope.sh` (1 SP)
- **Scope:** Build the scoped test runner.

**Full script design:**
```bash
#!/usr/bin/env bash
set -euo pipefail
REPO=$AYUMI_ROOT
cd "$REPO"

usage() {
  cat <<EOF
Usage: $0 [SCOPE] [PYTEST_ARGS...]

Scopes:
  --unit        Run tests/unit/ (all subcategories)
  --integration Run tests/integration/ (including ctrader/)
  --strategies  Run tests/strategies/ (including ict/)
  --e2e         Run tests/e2e/
  --regression  Run tests/regression/
  --heavy       Run only heavy-import files (numpy/pandas/scipy/sklearn)
  --full        Run all categories sequentially in separate processes

Options:
  --collect-only  Pass --collect-only to pytest (dry run)
  --live          Include tests marked @pytest.mark.live
  --help          Show this help

Default: excludes live tests (-m "not live")
EOF
}

# Parse flags
SCOPE=""
COLLECT_ONLY=""
LIVE=""
PYTEST_EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --unit)        SCOPE="unit"; shift ;;
    --integration) SCOPE="integration"; shift ;;
    --strategies)  SCOPE="strategies"; shift ;;
    --e2e)         SCOPE="e2e"; shift ;;
    --regression)  SCOPE="regression"; shift ;;
    --heavy)       SCOPE="heavy"; shift ;;
    --full)        SCOPE="full"; shift ;;
    --collect-only) COLLECT_ONLY="--collect-only"; shift ;;
    --live)        LIVE="1"; shift ;;
    --help|-h)     usage; exit 0 ;;
    *)             PYTEST_EXTRA+=("$1"); shift ;;
  esac
done

if [[ -z "$SCOPE" ]]; then
  usage
  exit 1
fi

# Build marker filter
MARKER=()
if [[ -z "$LIVE" ]]; then
  MARKER=(-m "not live")
fi

# Heavy-import discovery
find_heavy() {
  grep -rlE '^\s*(import|from)\s+(numpy|pandas|scipy|sklearn)' \
    tests/unit tests/integration tests/strategies tests/e2e tests/regression \
    --include='*.py' 2>/dev/null || true
}

run_pytest() {
  local label="$1"
  shift
  local files=("$@")
  if [[ ${#files[@]} -eq 0 ]]; then
    echo "==> ${label}: no files to run"
    return 0
  fi
  echo "==> Running scope: ${label} (${#files[@]} files)"
  python3 -m pytest "${MARKER[@]}" $COLLECT_ONLY --tb=short "${files[@]}" "${PYTEST_EXTRA[@]}"
}

case "$SCOPE" in
  unit)
    run_pytest "unit" tests/unit/
    ;;
  integration)
    run_pytest "integration" tests/integration/
    ;;
  strategies)
    run_pytest "strategies" tests/strategies/
    ;;
  e2e)
    run_pytest "e2e" tests/e2e/
    ;;
  regression)
    run_pytest "regression" tests/regression/
    ;;
  heavy)
    mapfile -t HEAVY_FILES < <(find_heavy)
    if [[ ${#HEAVY_FILES[@]} -eq 0 ]]; then
      echo "==> heavy: no heavy-import files found"
      exit 0
    fi
    run_pytest "heavy (${#HEAVY_FILES[@]} files)" "${HEAVY_FILES[@]}"
    ;;
  full)
    # Run each category in a separate process for memory isolation
    # Does NOT also run --heavy (mutually exclusive use case)
    run_pytest "unit" tests/unit/
    run_pytest "integration" tests/integration/
    run_pytest "strategies" tests/strategies/
    run_pytest "regression" tests/regression/
    # Skip e2e by default in --full unless --live is also passed
    if [[ -n "$LIVE" ]]; then
      run_pytest "e2e" tests/e2e/
    else
      echo "==> e2e: skipped (use --live to include)"
    fi
    ;;
esac
```

- **Design notes:**
  - `--full` and `--heavy` are mutually exclusive scopes (not combined)
  - Default excludes `-m "not live"` unless `--live` passed
  - `--collect-only` is a passthrough flag (works with any scope)
  - Heavy discovery uses corrected grep pattern with `^\s*(import|from)\s+`
  - Empty-list guard: checks `${#files[@]} -eq 0` before running pytest
  - Uses `python3` not `python`
  - `--full` skips e2e by default (e2e tests are live/forward-test heavy)

- **Acceptance:**
  - `bash scripts/run_test_scope.sh --unit --collect-only` collects from `tests/unit/`
  - `bash scripts/run_test_scope.sh --integration --collect-only` collects from `tests/integration/`
  - `bash scripts/run_test_scope.sh --strategies --collect-only` collects from `tests/strategies/`
  - `bash scripts/run_test_scope.sh --e2e --collect-only` collects from `tests/e2e/`
  - `bash scripts/run_test_scope.sh --regression --collect-only` collects from `tests/regression/`
  - `bash scripts/run_test_scope.sh --heavy --collect-only` discovers and collects heavy-import files
  - `bash scripts/run_test_scope.sh --full --collect-only` runs all categories sequentially (e2e skipped without `--live`)
  - `bash scripts/run_test_scope.sh --full --live --collect-only` includes e2e
  - `chmod +x scripts/run_test_scope.sh`

### Task 9: Write audit document (0.5 SP)
- **Scope:** Write `docs/p5a/test-modularization-audit-2026-06-26.md` summarizing the audit findings.
- **Acceptance:**
  - Document matches verified data (225 files, 40 heavy-import, path navigation counts).
  - References the manifest TSV as authoritative source.

---

## Complete File Manifest

The authoritative file manifest is at: **`docs/p5a/test-move-manifest.tsv`**

Format: `source_path\ttarget_path\tcategory\theavy_import\timport_scope`

All 225 files are listed with exactly one target path. No "etc." — the manifest is complete.

### Move counts per task

| Task | Category | Files to move | Heavy among them |
|------|----------|--------------|-----------------|
| 2 | unit/core | 34 | 9 |
| 2 | unit/risk | 13 | 1 |
| 2 | unit/data | 4 | 4 |
| 2 | unit/ict | 3 | 0 |
| 2 | unit/analytics | 16 | 5 |
| 2 | unit/execution | 4 | 0 |
| 2 | unit/hybrid | 6 | 0 |
| 3 | integration | 42 | 0 |
| 3 | integration/ctrader | 9 | 0 |
| 4 | strategies | 46 | 16 |
| 4 | strategies/ict | 0 (already placed) | 0 |
| 5 | e2e | 31 | 1 |
| 6 | regression | 1 move + 2 verify | 2 |
| — | fixtures | 0 (stays) | 1 |
| — | infra | 0 (stays) | 1 |
| **Total moves** | | **209** | **38** |

---

## Dependencies

```
Task 1 (skeleton)  ─┬─→ Task 2 (unit, 80 files) ──────┐
                    ├─→ Task 3 (integration, 51 files) ┤
                    ├─→ Task 4 (strategies, 46 files) ─┤
                    ├─→ Task 5 (e2e, 31 files) ────────┤
                    └─→ Task 6 (regression, 1 file) ───┘
                                                       ↓
                                                Task 7 (pytest.ini + _project_root + conftest)
                                                       ↓
                                                Task 8 (run_test_scope.sh)
                                                       ↓
                                                Task 9 (audit doc)
```

- Task 1 must complete before tasks 2–6 (directories must exist).
- Tasks 2–6 can run in parallel — file lists are mutually exclusive (verified by manifest).
- Task 7 depends on tasks 2–6 — `pythonpath` and `_project_root.py` only make sense after files are in final locations.
- Task 8 depends on task 7 — runner uses the final directory structure.
- Task 9 can be drafted in parallel with task 8, finalized after.

**Parallelism limit:** Up to 3 builder subagents can run tasks 2–6 concurrently. Task 2 is the largest (80 files) and should start first.

---

## Path Navigation Fix Protocol

Every move task (2–6) includes a sub-task to fix path navigation:

### What to fix
1. **Remove all `sys.path.insert` / `sys.path.append` calls** — redundant after `pythonpath` update in Task 7
2. **Replace `Path(__file__).parent.parent` constructs** with `from _project_root import PROJECT_ROOT`
3. **Replace `os.path.dirname(__file__)` constructs** with `from _project_root import PROJECT_ROOT`
4. **Update any `from tests.foo import bar`** to use the new package path

### How to find affected files in each task
```bash
# sys.path manipulation
grep -rlE 'sys\.path\.(insert|append)' <target_dir>/ --include='*.py'

# Path(__file__) usage
grep -rl 'Path(__file__)' <target_dir>/ --include='*.py'
```

### Order of operations
Task 7 (pytest.ini update) should be applied BEFORE the path fix sub-tasks, so that removing `sys.path` calls doesn't break imports during the transition. If tasks 2–6 run in parallel, builders should:
1. Move files with `git mv`
2. Leave `sys.path` calls in place temporarily
3. After all moves are done, Task 7 runs
4. Then a cleanup pass removes `sys.path` calls and fixes `Path(__file__)` references

**Alternative:** Task 7 can be split — 7a (pytest.ini update) runs first, then tasks 2–6 can include path fixes.

---

## Risks (Revised)

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **Stale import paths** after moves break pytest collection. | High | High | After each move batch, run `pytest --collect-only <target_dir>/`. Fix imports before declaring the task complete. |
| **`sys.path` removal breaks imports** if done before `pythonpath` update. | High | High | Task 7 (`pythonpath` + `_project_root.py`) must complete before path-fix sub-tasks. Or keep sys.path calls during moves, remove in cleanup pass. |
| **Cross-directory test imports** (tests importing from other test modules). | Medium | High | Grep for `from tests\.` and `from test_` across all test files. Move shared helpers to `tests/fixtures/` or `tests/conftest.py`. |
| **conftest.py scope changes** — fixtures defined in root `conftest.py` may not apply to deeper nested tests. | Medium | Medium | Audit `tests/conftest.py` fixtures. May need to add `conftest.py` at category level for shared fixtures. |
| **Duplicate test collection** if both old and new paths exist. | Low | Medium | Use `git mv` only. Verify `pytest --collect-only tests/` after Task 7. |
| **Heavy tests exhaust memory within a category.** | Medium | High | Runner isolates categories in separate pytest processes. `--heavy` flag for targeted heavy-file runs. If strategies/ (16 heavy) still OOMs, split further. |
| **Live/e2e tests run during CI.** | Medium | High | Runner defaults to `-m "not live"`. `--full` skips e2e unless `--live` passed. |
| **65 files with sys.path manipulation** — highest-risk part of the migration. | High | Medium | Comprehensive grep audit done. Fix protocol documented. All 81 call sites must be removed. |

### Rollback plan
- All moves use `git mv` — `git checkout -- tests/` reverts everything.
- `pytest.ini` change is one-line addition (additive, backward compatible).
- `_project_root.py` is a new file — deletion reverts.
- Runner script is additive — removal has no effect on existing pytest usage.

---

## Pre-Council Checklist (Updated)

- [x] Source files inspected — all 225 test files listed and categorized.
- [x] Heavy-import count verified — **40 files** (corrected from v1's 26).
- [x] Path navigation audit — **65 files** with sys.path manipulation, **44 Path(__file__)** references.
- [x] Complete file manifest generated — `docs/p5a/test-move-manifest.tsv` (225 entries, zero gaps).
- [x] Unit subcategorization applied — 6 subcategories (core/risk/data/ict/analytics/execution/hybrid).
- [x] Integration path flattened — `tests/integration/ctrader/` not `tests/integration/adapters/ctrader/`.
- [x] Acceptance criteria use `pytest --collect-only` (not `py_compile`).
- [x] Runner script uses `python3`, has `--collect-only` passthrough, mutually exclusive `--full`/`--heavy`, defaults to `-m "not live"`.
- [x] Empty-list guard in runner.
- [x] SP estimates reflect larger scope (v1 underestimated: unit was 131 files, now 80 with subcategories).
- [x] Dependencies identified — `pytest.ini`, `tests/conftest.py`, `_project_root.py`.
- [x] Protected files excluded — no personality/memory/config files touched.
- [x] Risk assessment complete — stale imports, path navigation, memory, live tests.
- [x] Rollback plan included — `git mv` + `git checkout`.
