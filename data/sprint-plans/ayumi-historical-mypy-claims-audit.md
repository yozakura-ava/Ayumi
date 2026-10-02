# Ayumi Historical mypy Claims Audit

**Card:** c40bd9e3-b433-4717-837a-6691a5444ecb (sprint reina-2026-08-19-122)
**Researcher:** Satsuki (subagent, depth 1)
**Date:** 2026-08-19
**Repo:** $AYUMI_ROOT
**Scope:** Audit pre-2026-08-14 mypy success claims on Ayumi; classify vacuous vs real; flag live-trading-adjacent merges for senior review.

## Executive Summary

**Verdict counts (10 distinct pre-2026-08-14 claims sampled):**

| Verdict | Count | Notes |
|---------|-------|-------|
| VACUOUS (config-driven bare `mypy` relying on mypy_path) | 9 | Claim asserted type/mypy success; no explicit file args shown; relied on the broken (per task premise) `mypy_path = src/forex_bot` config. |
| REAL (explicit file/dir args) | 0 | No pre-2026-08-14 Ayumi claim documented an explicit `mypy <files>` invocation. |
| UNKNOWN (insufficient evidence) | 1 | Card 78904868 mypy deltas cite file-level counts but the actual mypy command isn't shown. |
| **Total** | **10** | |

**Bottom line:** Every pre-2026-08-14 mypy success claim on Ayumi that the audit could verify was either (a) made before mypy.ini existed, (b) made under the `mypy_path = src/forex_bot` config, or (c) made without an explicit file-arg invocation. No claim documented a real, scoped mypy run against the touched files. **Per the task's heuristic (a) all of these are VACUOUS** — the tool either checked nothing (when bare `mypy` is run with a non-resolvable `mypy_path` and no other args) or checked the wrong path.

**CRITICAL CONFLICT to surface to Himari/Tomoe:** A 2026-08-16 audit by Satsuki (`docs/audits/forward-test-engine-mypy-audit-2026-08-16.md`, HB#160) directly contradicts this audit's premise. That audit ran mypy on a clean checkout of `forward_test_engine.py` and found that `MYPYPATH=src/forex_bot` worked correctly (returned 196 errors across 25 files). The Satsuki 2026-08-19 plan (`docs/plans/forward-test-engine-mypy-remediation-2026-08-19.md`) shows the same — it required a symlink workaround for mypy 1.20 specifically because mypy 1.20 rejects hyphenated dir names. The c40bd9e3 task brief asserts `mypy_path=src/forex_bot` was "broken since inception"; the 2026-08-16 evidence shows it was NOT broken in mypy 2.2.0. The contradiction is material and is not silently resolved. **Routing: Himari for adjudication; Tomoe for the config-truth meta-finding.**

Supporting physical evidence on disk: `src/forex_bot/.mypy_cache/3.12/cache.db` (mtime 2026-07-10 12:13 UTC) shows mypy DID scan `src/forex_bot/adapters/ctrader/*.py` files — 22 `*.err.ff` entries. This is consistent with the 2026-08-16 audit (the config was working) and inconsistent with the c40bd9e3 premise (the config was broken).

## Method

1. Sampled pre-2026-08-14 mypy success claims from: (a) `git log --all --before='2026-08-14' --grep=mypy` on the Ayumi repo, (b) grep over `$AYUMI_ROOT/docs/` for `mypy`/`MyPy`, (c) SQL query of workboard comments (14 hits in `workboard_card_comments` body, 19 hits in `workboard_cards` title/notes). Note: most workboard hits are post-2026-08-14 (e.g., e1ba054c, bf8ef527, 78904868, 70a53daa) and reflect the discovery/post-discovery remediation work rather than pre-discovery claims.
2. For each claim, determined how mypy was invoked: (a) config-driven bare `mypy` run → VACUOUS, (b) explicit file/dir args → REAL, (c) no invocation evidence → UNKNOWN.
3. Cross-referenced: VACUOUS claims span 2026-04-05 (mypy.ini creation) through 2026-08-14 (discovery day). Listed all live-trading-adjacent merges (`src/forex_bot/adapters/ctrader/`, `src/forex_bot/risk/`, `src/forex_bot/engine/`, `src/forex_bot/forward_test/`, `src/forex_bot/adapters/ctrader/forward_test_engine.py`) that landed in the same windows as vacuous claims.
4. Verified root fact via `git show 2e27caa:mypy.ini` and `git show 2f0206d:mypy.ini` — both show `mypy_path = src/forex_bot` as the original (pre-discovery) state. Current main has `mypy_path = src` (per `git show 25897b8:mypy.ini`), changed 2026-08-19 in the SEV-3 history restore.

## Claims Table

| # | Date (UTC) | Source | Quote/Paraphrase | Invocation Type | Verdict |
|---|------------|--------|------------------|-----------------|---------|
| C1 | 2026-04-05 15:00:49 | commit `1cf0c87` (also `716b669`, `8e5d2b6`, `d786840`) | Commit message: "fix(types): resolve mypy type annotation errors across backtest and trade management modules" — 9 files, src/forex_bot/{adapters/ctrader/api_client.py, backtest/{engine,enhanced_engine,grid_strategy,strategies}.py, trade_management/{exit_refinement,partial_exit,trade_manager}.py, ml/mean_reversion.py} | Bare `mypy` (no mypy.ini existed at this point; added 13 min later in C2) | **VACUOUS** — mypy.ini did not exist; any `mypy` invocation was on an unconfigured scanner. |
| C2 | 2026-04-05 15:13:24 | commit `2e27caa` (also `2f0206d`) | Commit message: "fix(imports): resolve import architecture issues and install missing stubs" — body: "Add mypy.ini with pythonpath and sklearn ignore config" | Adds mypy.ini with `mypy_path = src/forex_bot` | **VACUOUS** — config introduced was the (per task premise) broken one. CAVEAT: 2026-08-16 audit found this config was actually functional in mypy 2.2.0; see Conflicts section. |
| C3 | 2026-04-05 15:33:59 | commit `a97539e` (also `66b5af68`) | Commit message: "fix(quant): resolve review issues — type safety, public API, test hygiene" — touches src/forex_bot/quant/cointegration.py, adfuller, coint_cache type | Implicit mypy verification (no command shown) | **VACUOUS** — claims "type safety" without explicit mypy invocation. Relies on C2's config. |
| C4 | 2026-04-05 15:45:18 | commit `dee44f85` (also `1e54542`) | Commit message: "fix(types): change int to float for tr_sum and heartbeat variables" — touches src/forex_bot/{adapters/ctrader/api_client.py, backtest/{strategies,engine,enhanced_engine,grid_strategy}.py} | Implicit mypy verification | **VACUOUS** — same as C1/C2. |
| C5 | 2026-04-05 15:45:48 | commit `d6d14623` (also `464eb8e0`) | Commit message: "fix(types): ensure metrics dict values are float in mean_reversion.py" — src/forex_bot/ml/mean_reversion.py | Implicit mypy verification | **VACUOUS** — same. |
| C6 | 2026-04-10 01:08:37 | commit `8124bb3` (also `ba443ad`) | Squash merge: contains "fix(types): resolve mypy type annotation errors across backtest and trade management modules" twice (cherry-picked from 1cf0c87) + many other PRs | Bare `mypy` (squash inherits C1's claim) | **VACUOUS** — squash message inherits the vacuous claim. Encompasses many forex_bot files. |
| C7 | 2026-04-07 (Friday) | `docs/_archive/plans/week1-technical-completion-summary.md` (AYUAA-541) | "MyPy type checking: No issues found" + "Quality: Ruff + MyPy clean" — refers to `scripts/week1_metrics_tracker.py`, Ayumi copy-trading/api scope (NOT src/forex_bot) | Bare `mypy` with `mypy_path=src/forex_bot` (added 2 days prior in C2) | **VACUOUS** — `mypy_path=src/forex_bot` does not include `scripts/` (copy-trading/api). The "MyPy clean" claim couldn't have been verified for the files cited, regardless of whether the config was broken or not. |
| C8 | 2026-07-19 17:35:25 | commit `f5dd59e` | Commit message: "fix: unify SessionType enum — backtest.types now re-exports core.types.SessionType" — touches src/forex_bot/backtest/types.py and core/types.py | Implicit mypy verification | **VACUOUS** — same. |
| C9 | 2026-08-14 16:25 EDT (post-discovery, same day) | `docs/sprints/reina-2026-08-14-ayumi-bugs-78904868-postmortem.md` (card 78904868) | "Rin R1 independent review: ... mypy deltas (pattern_detector 9→7, data_qa 7→1, grid_strategy 0→0)" — file-level counts reported | File-scope suggested (specific files named); actual mypy command NOT shown in postmortem or build report | **UNKNOWN** — file-level deltas imply scope, but invocation method is undocumented. If bare `mypy` was used, the deltas measure against the broken-baseline noise (VACUOUS). If explicit files were passed, deltas would be partial/REAL. Insufficient evidence. |
| C10 | 2026-08-14 14:20 EDT (post-discovery, same day) | workboard card `e1ba054c` (Tsubaki mypy baseline) | Builder completion report: "Ayumi mypy: 313 errors (19 rows, script-verified sum=313) + 9 informational = 322 diagnostics" | Bare `mypy` (worktree at `lint-phase0-20260814`, mypy 2.2.0) — config used is the post-C2 state (broken per task premise) | **VACUOUS** — captured under the (per task premise) broken config; per the 2026-08-16 audit, the 313 error count may itself be unreliable because the mypy config was scanned. |

> Note: C9 and C10 are dated 2026-08-14 (same day as the discovery). The discovery work surfaced the broken state and immediately began remediation. These are documented as borderline — they sit ON the discovery boundary. The pre-discovery vacuous claims (C1–C8) are the material finding.

## Live-Trading-Adjacent Merges Under Vacuous Claims (Flagged for Senior Review)

The following commits landed in `src/forex_bot/` live-trading paths (adapters/ctrader/, risk/, engine/, forward_test/) during the window covered by vacuous claims C1–C8 (2026-04-05 → 2026-08-14). Each is flagged for senior review because type safety in these paths was unverified under the (per task premise) broken mypy config. The list is not exhaustive — there are ~40+ such commits; selected highest-risk below.

| Date | SHA | Path | Title |
|------|-----|------|-------|
| 2026-04-05 15:45:18 | `dee44f85` | `src/forex_bot/adapters/ctrader/api_client.py` (FIXClient, _last_heartbeat) | fix(types): change int to float for tr_sum and heartbeat variables |
| 2026-04-10 01:08:37 | `8124bb3` (squash of PR #131 + many) | `src/forex_bot/adapters/ctrader/forward_test_engine.py`, `src/forex_bot/adapters/ctrader/portfolio_risk_guard.py`, etc. | feat(ctrader): integrate cTrader live market data into forward test engine (AYUAA-670) + many other ctrader changes |
| 2026-05-07 17:52:01 | `7850636` | `src/forex_bot/adapters/ctrader/` | merge: fix/dual-signal-path-position-sizing |
| 2026-05-11 12:35:05 | `f9f67ba` | `src/forex_bot/adapters/ctrader/` | feat(ctrader): add proactive token refresh, SL/TP direction fix, and backtest refactor |
| 2026-05-14 00:43:30 | `eab78ce` | `src/forex_bot/forward_test/`, `src/forex_bot/adapters/ctrader/` | fix(forward-test): wire live cTrader execution, fix XAUUSD sizing, add hardening |
| 2026-06-05 20:40:10 | `a4973c1` | `src/forex_bot/adapters/ctrader/` | feat(ctrader): Phase 1D — Position Monitoring & Lifecycle |
| 2026-06-09 21:12:33 | `7c992dd` | `src/forex_bot/adapters/ctrader/paper_trader.py` | fix(ayuaa-778): paper trading uses live bid/ask for fills instead of stale signal price |
| 2026-06-12 18:25:59 | `2225225` | `src/forex_bot/adapters/ctrader/` | feat: sprint 2026-06-12 — hardening, Kelly, regime labels, resilience tests, token TTL |
| 2026-06-16 18:24:29 | `171e6a6` | `src/forex_bot/adapters/ctrader/order_manager.py` | fix(ctrader): order execution event correlation + timeout race (BQ-1042) |
| 2026-06-26 18:17:00 | `cc17399` | `src/forex_bot/adapters/ctrader/order_manager.py` | fix(ctrader): guard stop-loss check when bid/ask are zero |
| 2026-06-27 21:33:09 | `bc4fa4f` | `src/forex_bot/adapters/ctrader/order_manager.py` | feat: modular order system — VolumeCalculator + unified SymbolInfo + price decode fix |
| 2026-06-27 20:23:35 | `42b23b7` | `src/forex_bot/adapters/ctrader/kill_switch.py` | fix: disable kill switch entirely — Craig directive Jun 27 |
| 2026-07-03 13:40:03 | `42ae947` | `src/forex_bot/adapters/ctrader/execution_permission.py` | feat: Phase 6 dual-instance policy injection + broker-mutating gate |
| 2026-07-04 19:55:24 | `d3c8ddad` | `src/forex_bot/adapters/ctrader/kill_switch.py` | feat: per-strategy freeze/unfreeze + auto-freeze triggers (BQ-685a) |
| 2026-07-04 20:59:55 | `b9d618c` | `src/forex_bot/adapters/ctrader/kill_switch.py` | feat: FTMO guard — daily loss, position limit, drawdown breaker (BQ-685c) |
| 2026-07-05 16:47:14 | `94b9e48` | `src/forex_bot/adapters/ctrader/order_manager.py` | feat: wire TP2/TP3 into OrderManager live + paper order methods |
| 2026-07-05 17:00:09 | `7258fed` | `src/forex_bot/adapters/ctrader/order_manager.py` | fix: F1+F2 — store TP2/TP3 on Position after amend_sl_tp |
| 2026-07-05 21:39:17 | `c59b4b7` | `src/forex_bot/risk/engine.py` | fix: reject stale RiskGuard state from different account scale |
| 2026-07-05 21:48:02 | `c3e9829` | `src/forex_bot/risk/engine.py` | fix: align daily_start_balance to live balance on first sync |
| 2026-07-05 22:15:29 | `a3c825d` | `src/forex_bot/adapters/ctrader/` (refactor) | refactor: rename ctrader TradeSignal → CTraderTradeSignal (F6b) |
| 2026-07-05 23:55:25 | `556ce2d` | `src/forex_bot/forward_test/` | feat: wire ConfidenceEngine into forward test live-fire path |
| 2026-07-06 19:41:47 | `1ed0cde` | `src/forex_bot/adapters/ctrader/order_manager.py` | fix(ayumi): inline SL/TP on MARKET orders + positionId stamping + amend retry |
| 2026-07-06 20:20:27 | `5e55283` | `src/forex_bot/risk/engine.py` | fix(risk_guard): change daily reset from UTC midnight to 17:00 America/Toronto |
| 2026-07-07 03:15:05 | `5e55283` | `src/forex_bot/adapters/ctrader/` (price scaling) | fix(ayumi): JPY 100x price-scaling in ticks + observability |
| 2026-07-07 14:10:45 | `96da356` | `src/forex_bot/forward_test/` | feat(forward_test): daily-reset health counters at 17:00 America/Toronto |
| 2026-07-08 02:57:50 | `2fb3576` | `src/forex_bot/adapters/ctrader/` (signals_failed_live) | fix(ayumi): split TIMEOUT out of signals_failed_live + reconciliation defense |
| 2026-07-08 13:07:18 | `a781583` | `src/forex_bot/risk/engine.py` | fix(ayumi): CET-aware daily risk reset — FTMO rollover fix |
| 2026-07-08 13:22:20 | `badea4b` | `src/forex_bot/forward_test/blend_runner.py` | fix(ayumi): wire _on_trade_executed to blend_runner.register_position_mapping in live mode |
| 2026-07-08 15:08:25 | `37f536f` | `src/forex_bot/risk/` | feat(ayumi): Phase 4 — regime-aware risk sizing + lower confidence threshold |
| 2026-07-08 15:39:26 | `8a033c3` | `src/forex_bot/adapters/ctrader/kill_switch.py` | feat(ayumi): Phase 6 — Hayate daily audit + drift detection + FTMO tracker |
| 2026-07-09 18:13:06 | `7a3209b` | `src/forex_bot/adapters/ctrader/paper_trader.py`, `src/forex_bot/risk/engine.py` | fix(ayumi): remove P&L double-counting in RiskGuard/PaperTrader + fix daily_start_balance sync |
| 2026-07-10 12:41:36 | `acfc5785` | `src/forex_bot/forward_test/` | fix(forward-test): ayumi-remediation sprint 2026-07-09 — 7 fixes + 2 guardrails |
| 2026-07-10 14:20:44 | `ff5dd5f` | `src/forex_bot/adapters/ctrader/` | fix(ctrader): universal digit-decoder fix for XAUUSD and all sub-5-digit symbols |
| 2026-07-10 15:20:14 | `5e783b7` | `src/forex_bot/risk/`, `src/forex_bot/adapters/ctrader/` | feat: persist closed-trade P&L to trading.db + fix signal_stats.jsonl ownership |
| 2026-07-10 15:38:02 | `51c1b00` | `src/forex_bot/adapters/ctrader/` (token lifecycle) | fix: restore daily_trade_count from state + wire token lifecycle force_refresh |
| 2026-07-10 16:08:06 | `3773429` | `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` | fix(ayumi): clean 16 ruff errors in spot_feed + document P&L drift fix |
| 2026-07-10 16:43:15 | `679245c` | `src/forex_bot/engine/` (KillCriteria wiring) | feat(engine): wire KillCriteria + BehavioralPolicy into live gate path (Phase 3) |
| 2026-07-12 19:01:54 | `b15520d` | `src/forex_bot/risk/engine.py`, `src/forex_bot/adapters/ctrader/risk_guard.py` | fix(audit,risk_guard): fix SH-008 stale log path + DH-004 sync_live_balance persistence |
| 2026-07-14 20:41:25 | `7b96417` | `src/forex_bot/adapters/ctrader/` (auth, kill switch, order timeout) | fix: cTrader auth callback swap, kill switch activation, order timeout rejection |
| 2026-07-15 20:08:12 | `787fd02` | `src/forex_bot/risk/`, `src/forex_bot/adapters/ctrader/kill_switch.py` | feat: enforce FTMO best-day rule via _blocked_until circuit breaker |
| 2026-07-16 18:32:41 | `f4c4f4c` | `src/forex_bot/adapters/ctrader/order_manager.py` | fix: new_order timeout race — add deferred margin + terminal status on deferred error |
| 2026-07-16 20:09:44 | `0d7ae25` | `src/forex_bot/adapters/ctrader/` (_convert_direction) | fix: _convert_direction isinstance always fails — value-based comparison |
| 2026-07-16 20:23:56 | `b5f942f` | `src/forex_bot/adapters/ctrader/kill_switch.py`, order_manager | fix: timeout race errorCode propagation + kill switch query scope narrowing |
| 2026-07-16 21:03:29 | `9d78a91` | `src/forex_bot/adapters/ctrader/` (live_fills, heartbeat) | fix: health observability — live_fills counter, rejection errorCode, heartbeat JSON |
| 2026-07-16 21:16:55 | `bb4cd32` | `src/forex_bot/adapters/ctrader/` (signal stats, TestCloseTrade) | fix: record rejected orders in signal stats + fix TestCloseTrade lot_size units |
| 2026-07-17 13:12:28 | `ee7d4003` | `src/forex_bot/adapters/ctrader/` (execution events) | fix(ctrader): late-fill registry for execution events at/after timeout boundary |
| 2026-07-17 14:57:29 | `d184d232` | `src/forex_bot/adapters/ctrader/` (heartbeat) | fix(ctrader): pre-emptive reconnect + heartbeat threshold tuning |
| 2026-07-17 16:12:44 | `4216e55b` | `src/forex_bot/engine/`, `src/forex_bot/risk/` | fix: align daily-reset to America/Toronto midnight for engine + risk guard |
| 2026-07-17 20:44:21 | `7c6e6a00` | `src/forex_bot/adapters/ctrader/` (in-flight orders) | fix(ctrader): prevent preemptive reconnect from destroying in-flight orders |
| 2026-07-19 17:35:25 | `f5dd59e` | `src/forex_bot/backtest/types.py`, `core/types.py` | fix: unify SessionType enum — backtest.types now re-exports core.types.SessionType |
| 2026-07-21 00:59:08 | `c09823b` | `src/forex_bot/risk/ftmo_params.py` | fix: centralize FTMO parameters in risk/ftmo_params.py (P0 divergence fix) |

**Senior review flag:** all 50+ commits above touched live-trading-adjacent paths under vacuous mypy claims. The 2026-08-16 Satsuki audit (post-discovery) found 47 actual mypy errors in `forward_test_engine.py` alone (including 1 real bug at line 2371 — bar-close leak detector silently no-op). Recommend senior review prioritize the order/risk/execution paths.

## Root Fact Verification

**Pre-discovery mypy.ini state** (verified via `git show 2e27caa:mypy.ini` and `git show 2f0206d:mypy.ini`, both 2026-04-05 15:13:24 UTC):

```
[mypy]
mypy_path = src/forex_bot
ignore_missing_imports = True

[mypy-sklearn.*]
ignore_missing_imports = True
```

**Current main mypy.ini state** (verified via `cat mypy.ini` and `git show 25897b8:mypy.ini`, 2026-08-19 SEV-3 restore):

```
[mypy]
mypy_path = src
ignore_missing_imports = True

[mypy-sklearn.*]
ignore_missing_imports = True
```

The `mypy_path` change occurred 2026-08-14 in worktree commit `91aa8ee2` (Tsubaki, card `e1ba054c`); restored to current main via `25897b8` on 2026-08-19.

## Conflicts Surfaced (NOT silently resolved)

1. **C40bd9e3 premise vs. Satsuki 2026-08-16 audit (HIMARI adjudication required).** The c40bd9e3 task brief asserts `mypy_path=src/forex_bot` was broken since inception. Satsuki's 2026-08-16 audit (`docs/audits/forward-test-engine-mypy-audit-2026-08-16.md`) ran mypy on a clean checkout of `forward_test_engine.py` and reported "MYPYPATH=src/forex_bot mypy treats src/forex_bot as a path root and resolves adapters.ctrader.forward_test_engine correctly (returns 196 errors across 25 files, which is the truth)." If the 2026-08-16 audit is correct, the c40bd9e3 premise is wrong; the 9 VACUOUS verdicts above may be over-stated (the config was actually scanning, just possibly with a different error set than what the `mypy_path=src` change now surfaces). The Satsuki 2026-08-19 plan (`docs/plans/forward-test-engine-mypy-remediation-2026-08-19.md`) further notes that mypy 1.20 rejects hyphenated dir names, requiring a symlink workaround. The 2026-08-16 audit used mypy 2.2.0 and got 47 errors; the 2026-08-19 plan also used mypy 2.2.0 (the project has multiple mypy versions in the wild). This audit is classified under the c40bd9e3 premise; Himari should confirm whether the premise is correct.
2. **Physical mypy_cache presence** (HIMARI/SATOSHI follow-up). `src/forex_bot/.mypy_cache/3.12/cache.db` has mtime 2026-07-10 12:13 UTC, with 22 `*.err.ff` entries for `adapters/ctrader/*.py` files. This indicates mypy DID scan those files at some point under the pre-discovery config. Consistent with the 2026-08-16 audit (config was working), inconsistent with the c40bd9e3 premise (config was broken). The cache is on disk and not deleted as of 2026-08-19.
3. **Re-baseline count drift** (HIMARI flag). The 2026-08-14 e1ba054c baseline reported "Ayumi mypy 313 errors + 9 informational = 322 diagnostics." The 2026-08-16 audit reported "196 errors across 25 files" for the same `mypy_path=src/forex_bot` config (mypy 2.2.0). The 2026-08-19 plan reported "54 errors on e51990f" with the new `mypy_path=src` config and a symlink workaround. Three different counts, all under different conditions. The "313" count from C10 is itself a vacuous number under the task premise but may be a real signal under the 2026-08-16 evidence.

## Limitations

1. **Sample size:** 10 claims, 5 from 2026-04-05 alone. The Ayumi git log has ~18 `fix(types)`-style commits pre-2026-08-14, plus several squash merges containing type-fix claims. A wider sample would likely yield more vacuous claims but not change the verdict profile.
2. **mypy invocation never documented:** No pre-2026-08-14 Ayumi sprint doc, postmortem, or workboard comment shows an explicit `mypy <files>` command. All 10 claims rely on inferred invocation from commit message wording — none show proof of how mypy was run.
3. **No CI to cross-check:** `cat .github/workflows/ci.yml.disabled` shows CI never ran mypy (`pip install ruff bandit` only). All claims are unverifiable from CI logs.
4. **Post-2026-08-14 claims excluded:** The 2026-08-14 sprint itself is when the broken config was identified; C9 and C10 sit on that boundary. The discovery work (e1ba054c, bf8ef527, 78904868, 70a53daa) is post-discovery by definition and is not in scope.
5. **mypy version unknown pre-2026-08-14:** The pre-discovery state of the .venv's mypy version isn't recorded. Mypy 1.20 rejects hyphenated paths; mypy 2.2.0 (per the 2026-08-16 audit) does not. If pre-discovery mypy was 1.20 or earlier, the c40bd9e3 premise is correct; if 2.2.0, it's wrong. Without the version pin, the c40bd9e3 vs 2026-08-16 conflict cannot be resolved from this audit alone.
6. **No agentic re-run:** This audit did not re-run mypy on pre-2026-08-14 Ayumi (read-only constraint per task). All verdicts are derived from the on-disk artifacts and historical docs.

## Recommended Consumer

- **Primary: Himari (Portfolio)** — for adjudication of the c40bd9e3 vs 2026-08-16 conflict and for senior-review routing on live-trading paths.
- **Secondary: Tomoe (Architecture)** — for the mypy.ini config-truth meta-finding (the config was either broken since inception or was working all along; the answer is material for the c40bd9e3 brief).
- **Cc: Tsubaki (Build)** — for re-verification of the 50+ live-trading-path commits under a confirmed-correct mypy config.

**Freshness:** re-validate on any merge to `main` touching `mypy.ini`, `forward_test_engine.py`, `signal_adapter.py`, `market_data_feed.py`, or any file under `src/forex_bot/{adapters/ctrader,risk,engine,forward_test}/`.
