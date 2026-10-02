# Sprint 2026-09-13 Ayumi Test Debt — 113-Failure Cluster Manifest

**Card**: 3e83f2c6-95fb-43d9-8d97-c718f1b43d02
**Sprint**: sprint-2026-09-13-ayumi-test-debt
**Author**: Reina (subagent dispatch 2026-09-13)
**Branch**: reina/3e83f2c6-113-triage
**Worktree**: $AYUMI_ROOT/.worktrees/reina-3e83f2c6-113-triage
**Base**: main @ 5c0f9941 (post d85c8d89 isolation fix merge)

---

## Pre-Dispatch Artifact (Council Gate)

### Run Conditions

- `systemd-run --scope -p CPUQuota=30% -p MemoryMax=4G -- pytest tests/ --tb=line -q --no-header -p no:cacheprovider`
- Log: `data/assessments/113-triage/clean-baseline-run.log`
- Total runtime: 756.76s (0:12:36)
- Total tests collected: 7048

### Baseline Result (Clean, log outside data/)

```
114 failed, 6864 passed, 59 skipped, 5 deselected, 6 xfailed, 3 xpassed,
999 warnings, 32 errors, 15 subtests passed in 756.76s (0:12:36)
```

**Total failures + errors: 146** (114 FAIL + 32 ERROR)

Note: First run (log under `data/`) reported 7008 errors due to teardown-isolation
guard tripping on the pytest stdout log itself (the guard catches ANY write under
`<repo>/data/`). Re-running with log in `/tmp/113-triage-runs/baseline-run.log`
gives the clean 146 number, which is the canonical baseline for this triage.

### Failure Signature Clusters

| Cluster | Pattern | Count | Disposition | Notes |
|---------|---------|-------|-------------|-------|
| A | `Test wrote under <repo>/data/` (teardown isolation guard) | 32 (ERROR) | FIX | Test code uses production paths instead of tmp_path fixtures. Cannot xfail — would mask isolation regression. |
| B | `ImportError: cannot import name 'TradeSignal' from 'adapters.ctrader.models'` | 9 (FAILED) | FIX | Production class missing TradeSignal export. Cannot xfail — masks export regression. |
| C | `TypeError: Object of type MagicMock is not JSON serializable` | 8 (FAILED) | FIX | Test/production fixture needs proper serialization. |
| D | `_pickle.PicklingError: Can't pickle <function _worker_entry...>` | 6 (FAILED) | FIX | Multiprocessing pool needs class-method wrapper, not module-level function. |
| E | `AttributeError: 'ICTMarketState' object has no attribute 'calculate_atr'` | 6 (FAILED) | FIX | Production class missing method. |
| F | `AttributeError: 'NoneType' object has no attribute '__dict__'` | 5 (FAILED) | FIX | Setup fixture not initializing properly. |
| G | `TypeError: BacktestEngine.__init__() missing 1 required positional argument: 'strategies'` | 3 (FAILED) | FIX | Test passing wrong args. |
| H | `TypeError: EngineCore._calculate_metrics() takes 3 positional arguments but 4 were given` | 3 (FAILED) | FIX | Signature mismatch. |
| I | `ValueError: Timeframe 30 not in allowed whitelist {240, 60, 15}` | 2 (FAILED) | FIX | Timeframe whitelist policy. |
| J | `ModuleNotFoundError: No module named 'backtest.dsr' / 'backtest.correlation' / 'backtest.bootstrap_ci'` | 9 (FAILED) | FIX (B5-C1/C2) | Already owned by fix cards e0b991cd, 3e7e175e, d093c7d7, 52f5c44a. |
| K | DSR / Bootstrap CI CLI tests (existing B5 territory) | 3 (FAILED) | FIX (B5) | Already owned by existing fix cards. |
| L | Various assertion failures (boundary, mock, state-restore) | ~50 (FAILED) | MIXED | Some xfail-able (deterministic, environmental), some FIX. |

### Ratio Calculation

- Clear FIX-needed (A–K): 32+9+8+6+6+5+3+3+2+9+3 = **86 tests**
- MIXED (cluster L): ~50 tests, ~30% FIX, ~70% xfail → ~15 FIX + ~35 xfail
- **Total FIX-needed estimate**: 86 + 15 = **~101 tests**
- **Total xfail-able estimate**: ~35 tests
- **FIX ratio**: 101 / 146 = **~69%** (well above 40% gate)
- **FIX ratio vs dispatch 113 baseline**: 101 / 113 = **~89%** (way above 40% gate)

### Ratio-Gate Decision

**GATE TRIGGERS.** Per dispatch: *"If fix side >40% of 113, STOP and report — fixes decompose into child cards; this sprint does triage + xfail only."*

**Action**: STOP further disposition (no xfail applied, no 3x-green). Spawn child cards for the fix clusters. Report blocker.

### Reason for Ratio Shift vs Himari's 113 Baseline

Himari's 113 baseline pre-dated the d85c8d89 harness-isolation fix (merged 5c0f9941). The isolation fix added stricter teardown guards (cluster A: 32 tests), tightened import-time guards, and surfaced previously-hidden test isolation regressions. The shift from 113 → 146 (+ ~30%) is consistent with the d85c8d89 guard surface expanding.

---

## Child Card Manifest (Fix Decomposition)

Per dispatch, fixes decompose into child cards. This sprint is triage + xfail only; child cards are spawned but NOT executed in this sprint.

| # | Card Title | Files Affected | Cluster | Est SP | Agent |
|---|------------|----------------|---------|--------|-------|
| 1 | `[FIX] Teardown isolation: tests/* → tmp_path fixtures (32 tests, ERROR cluster A)` | tests/{strategies/ict/test_confluence.py, unit/core/test_blend_runner.py, integration/test_launch_blend_forward_test_startup_retry.py, integration/test_forward_test_execution_chain.py, integration/test_ctrader_paper_trader.py, unit/forward_test/test_blend_position_close.py, ...} | A | 3.0 | tsubaki |
| 2 | `[FIX] adapters.ctrader.models: re-export TradeSignal (9 tests, cluster B)` | src/forex_bot/adapters/ctrader/models.py, scripts/live_trading_execution.py, tests/integration/test_ctrader_risk_guard.py, tests/e2e/test_live_trading_execution.py, tests/integration/test_ctrader_execution_v2.py | B | 1.0 | riko |
| 3 | `[FIX] MagicMock JSON serialization in fixtures (8 tests, cluster C)` | tests/{unit/srf, integration, e2e}/... | C | 1.5 | tsubaki |
| 4 | `[FIX] _worker_entry pickle: wrap as class method or module-instantiable (6 tests, cluster D)` | src/forex_bot/backtest/parameter_sweep/sweep_runner.py, tests/unit/core/test_sweep_runner.py | D | 1.5 | tsubaki |
| 5 | `[FIX] ICTMarketState.calculate_atr method (6 tests, cluster E)` | src/forex_bot/backtest/ict_smc/market_state.py | E | 1.0 | tsubaki |
| 6 | `[FIX] Setup fixture NoneType.__dict__ AttributeError (5 tests, cluster F)` | tests/{integration/test_launch_blend_forward_test_startup_retry.py, unit/adapters/ctrader/test_preflight_seed.py, ...} | F | 1.5 | tsubaki |
| 7 | `[FIX] BacktestEngine.__init__ 'strategies' arg (3 tests, cluster G)` | tests/{e2e/test_portfolio_backtest.py, e2e/test_portfolio_blend.py} | G | 0.5 | tsubaki |
| 8 | `[FIX] EngineCore._calculate_metrics signature (3 tests, cluster H)` | src/forex_bot/backtest/engine_core.py, tests/unit/core/test_engine_core.py | H | 0.5 | tsubaki |
| 9 | `[FIX] Timeframe 30 whitelist policy (2 tests, cluster I)` | src/forex_bot/strategies/... | I | 0.5 | tsubaki |
| 10 | `[FIX-AYUMI-TEST-DEBT] Cluster L: ~15 fix-needed assertions + ~35 xfail-able` | various tests/ | L | 2.5 | tsubaki |

**Total est SP for child cards**: 13.5 SP across 10 cards.

### Existing B5 Fix Cards (cross-reference)

- `e0b991cd-ffbd-4808-a338-ee4ef7c74fcc` [INFRA][B5-C1] Vendor dbos_openclaw_bridge or importorskip (4 test files) — **agent: tsubaki**
- `3e7e175e-f60d-4301-8e57-308c1fb91b37` [INFRA][B5-C2] Module-level pytest.skip + tmp_path fixtures (3 files) — **agent: tsubaki**
- `d093c7d7-b54d-447d-ba31-6042fb9c85b6` [INFRA][B5-P1] Supply bqes.eri_calculator or scope-gate (1 test file) — **agent: tsubaki**
- `52f5c44a-3c4c-4bf0-bd31-40649ac3cc54` [INFRA][B5-P2] Supply quest package or scope-gate (2 test files) — **agent: tsubaki**
- `72e6a04e-7422-4ef9-99f9-d4014678be85` [INFRA][B5-P3] Fix precommit-smart-bypass + dbos-app-version state leak into test_check_coverage.py — **agent: tsubaki**
- `246ed7b1-e435-4de0-9194-726cb0401a00` [BUILD][INFRA][B5-C1] Vendor dbos_openclaw_bridge on sys.path or importorskip the 4 durability/pipeline tests — **agent: reina**
- `a1401214-7784-4f7a-8880-876d46e28cc9` [INVESTIGATION][B5-P4-bisect] Denser outward-bisect for test_batch_harness.py data-drift node

---

## Acceptance Status

- [x] **Failure-signature cluster** (this manifest, sections above)
- [x] **Xfail:fix ratio computed** (~69% fix, gate triggers)
- [ ] **All 113 dispositioned fix-vs-xfail**: NOT POSSIBLE — ratio-gate blocked this sprint from xfail disposition
- [ ] **3x consecutive green full-suite runs**: NOT PURSUED — ratio-gate blocks; child cards must land first
- [x] **Run manifest attached as proof** (this document + `data/assessments/113-triage/clean-baseline-run.log`)
- [x] **Child cards spawned** (10 new fix cards, see manifest above)

---

## Sprint Stop Conditions Met

1. Mandatory pre-dispatch artifact delivered (this manifest).
2. Ratio-gate triggered (>40% fix-side of 113).
3. Child cards spawned for fix decomposition.
4. Sprint did NOT apply xfails (per dispatch: "triage + xfail only" — but ratio-gate suspends xfail disposition).
5. Sprint did NOT run 3x green (conditional on ratio-gate not triggering).
6. Blocker reported to Ava via workboard comment.

---

## Evidence References

- Clean baseline log: `data/assessments/113-triage/clean-baseline-run.log` (1559 lines)
- Failure list (114 FAILED): `data/assessments/113-triage/clean-failed.txt`
- Error list (32 ERROR): `data/assessments/113-triage/clean-error.txt`
- Worktree: `reina/3e83f2c6-113-triage` @ main + 0 commits (no code changes — read-only investigation)
- Pre-existing kill_switches/history.jsonl checksum: BYTE-IDENTICAL (no production writes during run)

---

## Worktree Branch Note

Branch `reina/3e83f2c6-113-triage` exists but contains NO commits (this was a read-only triage investigation per dispatch). Child cards listed above will create their own branches when they execute.
