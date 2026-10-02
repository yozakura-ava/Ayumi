# Phase 5A Cleanup Plan

Date: 2026-06-26  
Baseline: `9a1d949`  
Recovery branch: `recovery/ayumi-mvp-rebuild` at `183a996`  
Secondary worktree branch: `senior-dev/p5a-mvp-safety-shell` at `c4c8e3e`  
Repo remote verified: `origin git@github.com:jalapeno777/Ayumi.git`; `origin/HEAD -> origin/main`

## Executive Decision

`183a996` should remain the canonical P5A implementation branch because it is the named recovery branch Craig gave and contains the same production code as `c4c8e3e`, plus the broader 9-test enforcement proof file.

The cleanup should reconcile by adopting the worktree branch's modular test layout from `c4c8e3e` onto `recovery/ayumi-mvp-rebuild`, without recommitting or rewriting the already-landed P5A production code. The resulting merge candidate should be: production code from either branch, because those files are byte-identical, plus modularized tests from `c4c8e3e`, plus the one unrelated dirty recovery test fixture handled deliberately as a separate cleanup item.

## Data Audit Findings

### Branch and Diff Audit

Recovery branch `183a996` changes from `9a1d949`:

- `src/forex_bot/adapters/ctrader/execution_permission.py`
- `src/forex_bot/adapters/ctrader/forward_test_engine.py`
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
- `tests/adapters/ctrader/test_p5a_kill_switch_enforcement.py`
- `tests/test_ctrader_execution_v2.py`
- `tests/test_forward_test_engine_wiring.py`
- `tests/test_forward_test_live_execution.py`
- `tests/test_forward_test_live_execution_outcomes.py`
- `tests/test_kill_switch_auto.py`
- `tests/test_resilience_integration.py`

Secondary branch `c4c8e3e` changes from `9a1d949`:

- Same 3 production files and same 6 existing test files as recovery.
- Replaces the single 375-line `test_p5a_kill_switch_enforcement.py` with:
  - `tests/adapters/ctrader/test_execution_permission.py`
  - `tests/adapters/ctrader/test_p5a_characterization.py`
  - `tests/adapters/ctrader/test_p5a_integration.py`

Verified with direct file comparison: the production files are identical between `183a996` and `c4c8e3e`:

- `execution_permission.py`: identical
- `forward_test_engine.py`: identical
- `open_api_spot_feed.py`: identical
- Shared modified tests checked spotwise are also identical where present.

The branches diverge only in P5A test organization.

### Syntax Audit

`python3 -m py_compile` passed on both branches for every modified Python source and P5A test file checked:

- Recovery: 4 modified P5A Python files checked, 0 syntax errors.
- Worktree: 6 modified P5A Python files checked, 0 syntax errors.

### Test Audit

The system Python lacked required packages and did not recognize `--timeout=30`; rerun with `$AYUMI_ROOT/.venv/bin/python3` succeeded.

Recovery branch:

- `tests/adapters/ctrader/test_p5a_kill_switch_enforcement.py -v --timeout=30`: `11 passed`
- `tests/test_kill_switch_auto.py -v --timeout=30`: `31 passed, 5 xfailed`

Secondary worktree:

- `tests/adapters/ctrader/test_execution_permission.py tests/adapters/ctrader/test_p5a_characterization.py tests/adapters/ctrader/test_p5a_integration.py -v --timeout=30`: `12 passed`
- `tests/test_kill_switch_auto.py -v --timeout=30`: `31 passed, 5 xfailed`

Additional recovery targeted check after combining the relevant safety surfaces:

- P5A enforcement, kill-switch auto, forward-test wiring/outcomes/live execution, cTrader execution v2, and resilience integration subset: `114 passed, 29 xfailed, 5 xpassed`

Full-suite evidence:

- `/tmp/full-suite.log`: `3849 passed`, `37 failed`, `91 skipped`, `47 xfailed`, `5 xpassed` in 95.90s. Failures are broad existing suite debt, not concentrated in P5A.
- `/tmp/audit-suite.log`: run hit memory/native-extension failure during `tests/test_parquet_integration.py::test_loads_bid_ask_parquet_with_spread`, with `pyarrow/parquet` and many heavy modules loaded. This is the evidence for modularized test execution and no full-suite-after-every-change rule.

### Runtime State Audit

- `systemctl is-active ayumi-forward-test.service`: `inactive`.
- Recovery `.env` md5: `fd97f00920199646df05046d19753912`, unchanged.
- Secondary worktree has no `.env`; do not create one. Use recovery repo as the `.env` authority.
- Recovery `data/kill_switches/global.state`: `active=true`, `mode=kill`, `reason=ftmo_daily_loss_limit`.
- Secondary worktree `data/kill_switches/global.state`: `active=true`, `mode=kill`, `reason=ftmo_daily_loss_limit`.

### Order Path Audit

Direct grep found these relevant order surfaces:

- `forward_test_engine.py`: policy check before `_market_feed.new_order(...)`.
- `open_api_spot_feed.py`: `set_permission_policy()`, `new_order()` policy guard before `ProtoOANewOrderReq`, and `send_order()` delegates to `new_order()`.
- `order_gateway.py`: constructs `ProtoOANewOrderReq`, but council review and grep found no current instantiation of `OrderGateway`.
- `order_manager.py`: calls `_api_client.send_order(...)`; current live path routes through `OpenApiSpotFeed.send_order() -> new_order()`, but the path remains a future audit risk if a different API client is injected.

Known P5A scope-out per Rei: `OpenApiSpotFeed.close_position()`, `cancel_order()`, and `amend_sl_tp()` are broker-mutating but not new-order paths. They are not to be changed in this cleanup. Document them as Phase 6 broker-operation gating.

### Pre-Council Checklist Status

Requested file `docs/plans/planner-pre-council-checklist.md` is not present in `$AYUMI_ROOT`. A repo search found no replacement checklist. I applied the checklist requirements embedded in the autobuild skill instead: primary-source read, data audit, scope and acceptance criteria, risk register, builder sizing, targeted validation, council review ask, and Craig approval gate. This absence should be logged as a process issue, not hidden.

## Status of `183a996` and `c4c8e3e`

### `183a996` Recovery Branch

What landed:

- Removed the disabled kill-switch bypass behavior from the live order path.
- Added `ExecutionPermissionPolicy`, default deny on missing policy state and exception.
- Added `_execute_signal_live()` pre-flight policy gate.
- Added `OpenApiSpotFeed.new_order()` defense-in-depth policy gate.
- Added `set_permission_policy()` wiring.
- Marked the five freeze activation tests xfail rather than un-commenting freeze code.
- Added one large P5A enforcement test file.

Verified:

- Production P5A files compile.
- P5A enforcement tests pass.
- `test_kill_switch_auto.py` passes with 5 expected xfails.
- Kill switch remains active kill mode.
- `.env` unchanged.
- Forward test service inactive.

Unverified:

- Full suite is not clean; it has 37 failures in existing debt.
- Audit suite can still exhaust memory when heavy test groups co-load.
- Forward test restart is not executed yet and requires Craig approval because no trade authorization exists.

### `c4c8e3e` Secondary Worktree

What landed:

- Same production implementation as recovery.
- Same shared test fixes.
- Cleaner P5A tests split into unit, characterization, and integration files.

Verified:

- Production files are byte-identical to recovery.
- Modular P5A tests pass.
- `test_kill_switch_auto.py` passes with 5 expected xfails.
- Kill switch remains active kill mode.

Unverified:

- Worktree lacks `.env`, so `.env` md5 cannot be checked there.
- This branch is not the named recovery branch and should not supersede `183a996` wholesale.

Canonical choice:

- Canonical production branch: `recovery/ayumi-mvp-rebuild` at `183a996`.
- Canonical test organization target: split P5A tests from `c4c8e3e`.

## Branch Reconciliation

1. Keep `recovery/ayumi-mvp-rebuild` as the merge candidate branch.
2. Cherry-pick or manually port only the modular P5A test-file organization from `c4c8e3e`:
   - Add `tests/adapters/ctrader/test_execution_permission.py`.
   - Add `tests/adapters/ctrader/test_p5a_characterization.py`.
   - Add `tests/adapters/ctrader/test_p5a_integration.py`.
   - Remove or retire `tests/adapters/ctrader/test_p5a_kill_switch_enforcement.py` after confirming equivalent coverage.
3. Do not change the production P5A code during reconciliation unless validation finds a real defect.
4. Keep the uncommitted recovery change in `tests/test_live_market_data_integration.py` out of the P5A reconciliation commit unless Craig approves it as a separate test-isolation builder.
5. Ignore unrelated untracked files during P5A cleanup unless they block validation:
   - `account_state.py`, `auth.py`, `credentials.py`, signal strategy tests, data logs, audit outputs, and `scripts/live_test_fire.py` are outside this P5A merge.

## Test Cleanup Strategy

Craig's modularization rule is mandatory because `/tmp/audit-suite.log` shows memory pressure in a heavy import stack (`pyarrow`, `pandas`, `scipy`, `statsmodels`, `sklearn`, `psutil`, etc.).

Test cleanup goals:

- Split P5A test coverage by import weight and responsibility.
- Avoid co-loading forward-test engine, cTrader OpenAPI, parquet/backtest, and ML/statistics stacks in the same validation step.
- Use targeted tests per builder.
- Run the full suite once at sprint close only, with expectation that existing non-P5A failures remain tracked separately.

Proposed P5A test grouping:

- Policy unit group:
  - `tests/adapters/ctrader/test_execution_permission.py`
  - Low import weight, validates default-deny and `is_active()` use.
- Characterization group:
  - `tests/adapters/ctrader/test_p5a_characterization.py`
  - Covers forward-test behavior boundary.
- Broker-boundary integration group:
  - `tests/adapters/ctrader/test_p5a_integration.py`
  - Covers `OpenApiSpotFeed.new_order()` policy gate.
- Existing kill-switch auto group:
  - `tests/test_kill_switch_auto.py`
  - Keeps 5 freeze activation tests xfail, preserving Mika's no-monitor-reactivation condition.
- Existing cTrader compatibility group:
  - `tests/test_ctrader_execution_v2.py`
  - Verifies shared open API spot feed behavior after policy injection.

Do not run parquet/backtest/ML integration tests in the same step as cTrader live adapter tests during builder validation.

## Validation Plan

Use the shared venv:

```bash
$AYUMI_ROOT/.venv/bin/python3 -m pytest ...
```

Do not use system `python3` for validation; it lacks required packages and may not load pytest-timeout.

Builder validation:

- Run syntax checks for only modified Python files.
- Run only the targeted test group owned by that builder.
- Run static grep proof for order paths after any order-path change.
- Do not run the full suite after each builder.

Sprint-close validation:

1. `git status --short` and confirm only intended files are staged/committed.
2. `python3 -m py_compile` for all modified Python files.
3. Targeted safety suite:

```bash
$AYUMI_ROOT/.venv/bin/python3 -m pytest \
  tests/adapters/ctrader/test_execution_permission.py \
  tests/adapters/ctrader/test_p5a_characterization.py \
  tests/adapters/ctrader/test_p5a_integration.py \
  tests/test_kill_switch_auto.py \
  tests/test_ctrader_execution_v2.py \
  tests/test_forward_test_engine_wiring.py \
  tests/test_forward_test_live_execution.py \
  tests/test_forward_test_live_execution_outcomes.py \
  tests/test_resilience_integration.py \
  -v --timeout=30
```

4. One full suite at sprint close only:

```bash
$AYUMI_ROOT/.venv/bin/python3 -m pytest tests/ -q --timeout=30
```

Expected closeout handling:

- If the full suite still shows the known non-P5A failures from `/tmp/full-suite.log`, document the delta and do not block P5A solely on pre-existing failures.
- If memory exhaustion repeats, rerun modular groups and record the audit-suite failure as the blocker for full-suite reliability, not P5A safety.

## Merge Plan

Verified remote:

- `origin git@github.com:jalapeno777/Ayumi.git`
- `origin/HEAD -> origin/main`

Merge sequence:

1. Stay on `recovery/ayumi-mvp-rebuild`; do not recommit `183a996`.
2. Create a cleanup branch from recovery if edits are needed:

```bash
git checkout recovery/ayumi-mvp-rebuild
git checkout -b senior-dev/p5a-cleanup-reconciliation
```

3. Port modular tests from `c4c8e3e` and remove the monolithic P5A test file if coverage is equivalent.
4. Run targeted validation.
5. Commit cleanup only, conventional format:

```text
test(p5a): modularize safety-shell validation

Co-Authored-By: Paperclip <noreply@paperclip.ing>
```

6. After council re-review and Craig approval, merge to `main`:

```bash
git checkout main
git pull --ff-only origin main
git merge --no-ff senior-dev/p5a-cleanup-reconciliation
```

7. Run sprint-close targeted validation on `main`.
8. Push only after validation and Craig approval:

```bash
git push origin main
```

Protected constraints:

- No `.env` changes.
- No kill-switch state changes.
- No gateway restart.
- No trades.
- No un-commenting freeze activation code.

## Forward Test Restart Plan

This is a post-merge operational step and requires Craig's explicit approval. It must not authorize trades and must preserve `active=true`, `mode=kill`.

Pre-start checks:

1. Confirm service inactive:

```bash
systemctl is-active ayumi-forward-test.service
```

2. Confirm `.env` hash:

```bash
md5sum .env
```

Expected: `fd97f00920199646df05046d19753912`.

3. Confirm kill switch active:

```bash
cat data/kill_switches/global.state
```

Expected: `"active": true`, `"mode": "kill"`.

Start and verify:

1. Start service:

```bash
sudo systemctl start ayumi-forward-test.service
```

2. Auth check:

```bash
journalctl -u ayumi-forward-test.service -n 100 --no-pager | grep -Ei 'auth|authenticated|token|connected'
```

Pass: connected/authenticated or known safe auth status visible. Fail: crash loop, credential write, or unauthorized refresh behavior.

3. Kill-switch enforcement check:

```bash
journalctl -u ayumi-forward-test.service -n 200 --no-pager | grep -Ei 'kill switch|permission|blocked|enforced'
cat data/kill_switches/global.state
```

Pass: logs acknowledge active/enforced kill switch; state remains `active=true`, `mode=kill`.

4. First tick check:

```bash
journalctl -u ayumi-forward-test.service -f --no-pager
```

Observe until first tick/market data heartbeat is logged.

5. No-orders observation window:

Observe for 15 minutes with kill switch still active. Verify no new order request:

```bash
journalctl -u ayumi-forward-test.service --since '15 minutes ago' --no-pager | grep -Ei 'new_order|ProtoOANewOrderReq|order sent|FILLED|PENDING'
```

Pass: no broker order send, or only explicit "blocked by permission policy" denial logs.

Rollback:

```bash
sudo systemctl stop ayumi-forward-test.service
systemctl is-active ayumi-forward-test.service
```

If code rollback is required after service stop:

```bash
git revert <cleanup-merge-commit>
```

Do not clear the kill switch as part of rollback.

## Sprint Closeout Deliverables

Concrete deliverables:

- Cleanup plan: `docs/plans/p5a-cleanup-plan.md`
- Sprint closeout doc: `docs/closeouts/p5a-cleanup-closeout.md`
- Validation log: `docs/closeouts/p5a-cleanup-validation.md`
- Static order-path proof: `docs/closeouts/p5a-order-path-proof.md`
- Forward test restart log: `docs/closeouts/p5a-forward-test-restart.md`
- Council re-review artifacts:
  - `/tmp/council-kaito-p5a-cleanup-review.md`
  - `/tmp/council-mika-p5a-cleanup-review.md`
  - `/tmp/council-rei-p5a-cleanup-review.md`
- Google Drive upload of the closeout package after Craig approval.
- Workboard card completion on `boardId="default"` only.
- Mission Control sync with final P5A status, remaining P5B/Phase 6 scope-outs, and forward-test state.

## Builder Decomposition

All builders are in-session dev work. Do not dispatch Tsukasa. Each builder is <=1.5 SP, <=4 files, <=15 minutes.

### Builder 1: Modularize P5A Tests

Scope:

- Port modular test layout from `c4c8e3e` onto recovery cleanup branch.
- Replace monolithic P5A test file only after equivalent coverage is preserved.

Files:

- `tests/adapters/ctrader/test_execution_permission.py`
- `tests/adapters/ctrader/test_p5a_characterization.py`
- `tests/adapters/ctrader/test_p5a_integration.py`
- `tests/adapters/ctrader/test_p5a_kill_switch_enforcement.py`

Estimate: 1.5 SP  
Timeout: 15 minutes

Acceptance criteria:

- Modular files exist on cleanup branch.
- Monolithic file removed or explicitly retained only if coverage gap is documented.
- No production code changed.
- Tests pass:

```bash
$AYUMI_ROOT/.venv/bin/python3 -m pytest \
  tests/adapters/ctrader/test_execution_permission.py \
  tests/adapters/ctrader/test_p5a_characterization.py \
  tests/adapters/ctrader/test_p5a_integration.py \
  -v --timeout=30
```

### Builder 2: Existing Safety Test Compatibility

Scope:

- Verify xfail handling remains intentional.
- Ensure existing modified tests still use isolated kill-switch state and no production state file mutation.

Files:

- `tests/test_kill_switch_auto.py`
- `tests/test_resilience_integration.py`
- `tests/test_forward_test_engine_wiring.py`
- `tests/test_forward_test_live_execution_outcomes.py`

Estimate: 1.0 SP  
Timeout: 15 minutes

Acceptance criteria:

- Five freeze activation tests remain xfail.
- No freeze activation code is un-commented.
- Tests pass:

```bash
$AYUMI_ROOT/.venv/bin/python3 -m pytest \
  tests/test_kill_switch_auto.py \
  tests/test_resilience_integration.py \
  tests/test_forward_test_engine_wiring.py \
  tests/test_forward_test_live_execution_outcomes.py \
  -v --timeout=30
```

### Builder 3: Static Order-Path Proof

Scope:

- Produce documented proof of guarded new-order paths.
- Document Phase 6 scope-out for `close_position()`, `cancel_order()`, and `amend_sl_tp()`.

Files:

- `docs/closeouts/p5a-order-path-proof.md`

Estimate: 1.0 SP  
Timeout: 15 minutes

Acceptance criteria:

- Proof lists every grep hit for `new_order`, `send_order`, and `ProtoOANewOrderReq`.
- Proof distinguishes active guarded paths, delegated paths, dead/uninstantiated paths, tests, and Phase 6 broker-mutating scope-outs.
- Verification command:

```bash
grep -rn 'new_order\|send_order\|ProtoOANewOrderReq\|close_position\|cancel_order\|amend_sl_tp' src/forex_bot/adapters/ctrader/ --include='*.py'
```

### Builder 4: Recovery Dirty-Tree Triage

Scope:

- Decide whether the uncommitted `tests/test_live_market_data_integration.py` fixture belongs in P5A cleanup or a separate follow-up.
- Do not stage unrelated untracked files.

Files:

- `tests/test_live_market_data_integration.py`
- `docs/closeouts/p5a-cleanup-validation.md`

Estimate: 1.0 SP  
Timeout: 15 minutes

Acceptance criteria:

- Dirty-tree report identifies unrelated files excluded from P5A.
- If the fixture is included, it has a clear P5A reason and passes:

```bash
$AYUMI_ROOT/.venv/bin/python3 -m pytest tests/test_live_market_data_integration.py -v --timeout=30
```

- If excluded, it remains unstaged and is listed as a separate cleanup item.

### Builder 5: Merge Readiness and Closeout Package

Scope:

- Prepare merge-ready closeout docs and validation summary.
- No service start.
- No push.

Files:

- `docs/closeouts/p5a-cleanup-closeout.md`
- `docs/closeouts/p5a-cleanup-validation.md`
- `docs/plans/p5a-cleanup-plan.md`

Estimate: 1.5 SP  
Timeout: 15 minutes

Acceptance criteria:

- Closeout docs include test commands and results.
- Known full-suite failures are compared against `/tmp/full-suite.log`.
- Memory crash from `/tmp/audit-suite.log` is documented.
- Craig approval ask is included.

## Council Review Ask

Use Kaito, Mika, and Rei again because the cleanup scope is still P5A safety shell, branch reconciliation, modular validation, and forward-test risk.

Ask them to re-review:

- Canonical branch decision: recovery production code + modular worktree tests.
- Test modularization strategy against Craig's memory-exhaustion rule.
- Forward test restart plan, especially no-orders verification and rollback.
- Risk register completeness.
- Whether any original council condition is weakened by the reconciliation.

Expected review artifacts:

- `/tmp/council-kaito-p5a-cleanup-review.md`
- `/tmp/council-mika-p5a-cleanup-review.md`
- `/tmp/council-rei-p5a-cleanup-review.md`

Council conditions already carried forward:

- Kaito: startup behavior documented, 5 freeze tests handled, default deny, static proof.
- Mika: no monitor reactivation, default deny, two-layer enforcement, no state mutation.
- Rei: document broker-mutating scope-outs, document residual TOCTOU, use `is_active()`.

## Craig Approval Gate

Single approval step before builder dispatch:

Craig approves dispatch of Builders 1-5 as scoped above, with these constraints:

- No trades.
- No `.env` mutation.
- No kill-switch state change.
- No gateway restart.
- No un-commenting freeze activation code.
- No Tsukasa dispatch.
- Targeted tests per builder only; full suite once at sprint close.
- Forward test restart is a separate explicit approval after merge readiness.

Craig should see before approving:

- This plan.
- The data audit summary.
- The council cleanup reviews.
- The exact builder list with files, SP, timeout, and verification command.

## Risk Register

| Risk | Impact | Mitigation |
|---|---:|---|
| Merge picks wrong branch and loses coverage | High | Use recovery as canonical production branch; port modular tests from worktree only. |
| Full suite memory exhaustion repeats | Medium | Modular builder validation; full suite once at close; document `/tmp/audit-suite.log` failure. |
| Hidden new-order path remains unguarded | Critical | Static grep proof; document `OrderGateway` as uninstantiated and `send_order()` delegation. |
| Broker-mutating methods beyond new orders remain ungated | Medium | Explicit Phase 6 scope-out for `close_position()`, `cancel_order()`, `amend_sl_tp()`; no code changes in P5A cleanup. |
| Policy false-allows when kill switch is active | Critical | Preserve `ExecutionPermissionPolicy.is_active()` default-deny behavior; targeted unit tests. |
| Tests mutate production kill-switch state | High | Use `tmp_path` state dirs and mocks; verify `global.state` before and after validation. |
| `.env` changes during merge/restart | High | Md5 check before and after; do not create `.env` in secondary worktree. |
| Forward test sends order after restart | Critical | Keep kill switch active; verify permission-block logs; observe 15 minutes for no order sends; rollback by stopping service. |
| Dirty untracked files get swept into cleanup commit | Medium | Explicit dirty-tree triage; stage only intended P5A files. |
| Council checklist absence causes process blind spot | Low | Record missing file; use autobuild planner requirements; create follow-up to restore checklist if needed. |

## Definition of Done

- Cleanup branch contains canonical P5A production code and modular tests.
- All builder acceptance commands pass or documented pre-existing failures are isolated.
- Static order-path proof exists.
- Council re-review completed and synthesized.
- Craig approves merge.
- Merge to `origin/main` completed only after approval.
- Forward test restart performed only after separate explicit approval.
- Closeout package completed, uploaded, workboard card completed on `default`, and Mission Control synced.
