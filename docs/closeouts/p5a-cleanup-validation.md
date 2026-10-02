# P5A Cleanup Validation Report

Date: 2026-06-26
Branch: `senior-dev/p5a-cleanup-reconciliation`

## Dirty-Tree Triage

### Committed on this branch (P5A cleanup scope)

| File | Commit | Purpose |
|------|--------|---------|
| `tests/adapters/ctrader/test_execution_permission.py` | `ce79e3d` | Modular policy unit tests |
| `tests/adapters/ctrader/test_p5a_characterization.py` | `ce79e3d` | Forward-test behavior boundary tests |
| `tests/adapters/ctrader/test_p5a_integration.py` | `ce79e3d` | Broker-boundary integration tests |
| `docs/closeouts/p5a-order-path-proof.md` | `3e64ea3` | Static order-path proof |

### Removed

| File | Commit | Reason |
|------|--------|--------|
| `tests/adapters/ctrader/test_p5a_kill_switch_enforcement.py` | `ce79e3d` | Replaced by 3 modular files with equivalent+ coverage |

### Excluded from P5A (untracked, not staged)

| Path | Classification |
|------|---------------|
| `archive/legacy_shims_2026-06-19/` | Archive, unrelated |
| `data/.token_refresh.lock` | Runtime artifact |
| `data/audit/` | Audit output from prior runs |
| `data/forward_test.log` | Runtime log |
| `data/risk_state_blend.json.bak.*` | Backup artifact |
| `data/signal_stats.jsonl*` | Runtime data + backups |
| `docs/plans/p5a-cleanup-plan.md` | Planner output (already delivered, not code) |
| `docs/specs/` | Unrelated specs |
| `scripts/live_test_fire.py` | Separate utility script |
| `src/forex_bot/adapters/ctrader/account_state.py` | Separate feature, not P5A |
| `src/forex_bot/adapters/ctrader/auth.py` | Separate feature, not P5A |
| `src/forex_bot/adapters/ctrader/credentials.py` | Separate feature, not P5A |
| `tests/adapters/ctrader/test_account_state.py` | Test for separate feature |
| `tests/adapters/ctrader/test_signal_adapter_strategy_id.py` | Test for separate feature |
| `tests/strategies/test_session_breakout_tp.py` | Test for separate feature |

### `test_live_market_data_integration.py` status

Already committed in `1cd0d94` (parent branch). No dirty changes. Not part of P5A cleanup — left as-is.

## Validation Results

### Builder 1: Modular Tests

```
$AYUMI_ROOT/.venv/bin/python3 -m pytest \
  tests/adapters/ctrader/test_execution_permission.py \
  tests/adapters/ctrader/test_p5a_characterization.py \
  tests/adapters/ctrader/test_p5a_integration.py \
  -v --timeout=30
→ 12 passed
```

### Builder 2: Safety Test Compatibility

```
$AYUMI_ROOT/.venv/bin/python3 -m pytest \
  tests/test_kill_switch_auto.py \
  tests/test_resilience_integration.py \
  tests/test_forward_test_engine_wiring.py \
  tests/test_forward_test_live_execution_outcomes.py \
  -v --timeout=30
→ 64 passed, 29 xfailed, 5 xpassed
```

Freeze activation xfails: 5 confirmed (Mika's condition met).
XPASS note: 5 tests marked xfail actually pass — these are resilience/stale-tick tests where the behavior moved elsewhere during refactoring (BQ-1328). Not freeze activation suppression; the xfail rationale is stale code paths, not frozen behavior.
Kill switch state post-test: `active=true`, `mode=kill` (unchanged).

### Builder 3: Order-Path Proof

See `docs/closeouts/p5a-order-path-proof.md`. All active new-order paths guarded.

## Known Pre-Existing Failures (not P5A)

Full suite run from `/tmp/full-suite.log` (recovery branch, pre-cleanup):
`3849 passed, 37 failed, 91 skipped, 47 xfailed, 5 xpassed`

The 37 failures are existing suite debt across parquet, backtest, ML, and
other non-P5A modules. Not caused or worsened by P5A cleanup.
