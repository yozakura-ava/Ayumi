# Build Summary — card c4b86732-9a6c-4169-b5de-e0a12e63280c

> **Card:** [BUILD][AYUMI] Tournament harness fail-loud guard + 17-strategy registration sweep
> **Branch:** tsubaki/c4b86732-harness-fail-loud
> **Worktree:** $AYUMI_ROOT/.worktrees/tsubaki-c4b86732-harness-guard
> **Builder:** Tsubaki (build only — Ava reviews, Reina merges)
> **Date:** 2026-09-14

## Scope (from card notes, allowed_files enforced)

- `scripts/run_tournament.py` (edit)
- `src/tournament/harness.py` (edit — fail-loud guard, progress logging, registration sweep)
- `src/tournament/scorecard.py` (edit — only if needed for empty-handling; not modified)
- `src/tournament/__init__.py` (re-export new exception class)
- `tests/test_tournament_empty_window.py` (NEW)
- `tests/test_tournament_registration.py` (NEW)
- `docs/plans/ayumi-tournament-harness-spec.md` (appendix only — SRMR+ diagnosis)

**Explicitly OUT of scope:** `src/forex_bot/strategies/*` (no edits, only triage by inspection).

## Acceptance criteria coverage

| AC | Implementation | Verification |
|---|---|---|
| AC1: fail-loud guard (0 bars / 0 signals → non-zero exit) | `TournamentNoSignals` exception in `harness.py` raised when `bars_processed == 0` OR `signals == 0` per registered strategy | `test_tournament_empty_window.py` |
| AC2: SRMR+ hours rejection (documentation only) | Appendix to `ayumi-tournament-harness-spec.md` | Doc read-back |
| AC3: ~17 strategy registration sweep | 15 runnable registered in `STRATEGY_CLASS_MAP`; 3 dead/stale in triage table in card proof | `test_tournament_registration.py` |
| AC4: progress logging every 1000 bars | `logger.info` in `_extract_signals_from_strategy` inner loop | manual log inspection (no test — visual evidence) |
| AC5: scoped tests via run_test_scope.sh | both new test files | `scripts/run_test_scope.sh tests/test_tournament_empty_window.py tests/test_tournament_registration.py` |

## Triage table (preliminary — final in card proof)

| Strategy class | Module | Verdict | Reason |
|---|---|---|---|
| DonchianATRTrendV2Strategy | donchian_atr_trend_v2.py | runnable | config=None OK; ISignalStrategy; init/shutdown OK |
| DualTFSqueezeProStrategy | dual_tf_squeeze_pro.py | runnable | config=None OK; ISignalStrategy; init/shutdown OK |
| KillzoneMomentumStrategy | killzone_momentum.py | runnable | config=None OK; no initialize/shutdown (harness skips gracefully) |
| LondonBreakoutRetestStrategy | london_breakout_retest.py | runnable | config=None OK; no initialize/shutdown |
| DonchianBreakoutStrategy | momentum.py | runnable | config=None OK; no initialize/shutdown |
| ATRVolatilityBreakoutStrategy | momentum.py | runnable | config=None OK; no initialize/shutdown |
| MATrendFollowingStrategy | momentum.py | runnable | config=None OK; no initialize/shutdown |
| MomentumM15Strategy | momentum_m15.py | runnable | config=None OK; no initialize/shutdown (M15-tuned) |
| SimpleRSIThresholdStrategy | rsi_threshold.py | runnable | config=None OK; ISignalStrategy; init/shutdown OK |
| SessionRangeMeanReversionStrategy | session_range_mean_reversion.py | runnable | config=None OK; no initialize/shutdown |
| SessionRangeMRWithICTFilter | session_range_mr_ict_filtered.py | runnable | config=None OK; no initialize/shutdown; imports check OK |
| TTCXAUUSDStrategy | ttc_xauusd.py | runnable | config=None OK; ISignalStrategy; init/shutdown OK; XAUUSD M15-tuned |
| VolatilityRegimeBreakoutStrategy | volatility_regime_breakout.py | runnable | config=None OK; no initialize/shutdown |
| VolatilitySqueezeStrategy | volatility_squeeze.py | runnable | config=None OK; no initialize/shutdown |
| DonchianATRTrendStrategy (v1) | donchian_atr_trend.py | runnable | config=None OK; no initialize/shutdown (legacy) |
| ORBStrategy | orb.py | **DEAD** | `__init__(self, config: dict)` — required positional dict; config=None raises `AttributeError: 'NoneType' object has no attribute 'get'` |
| MTFFilteredMomentumStrategy | mtf_filtered_momentum.py | **DEAD** | `__init__(self, inner_strategy: ISignalStrategy, ...)` — required positional inner_strategy; cannot register as standalone |
| SessionBreakoutStrategy | session_breakout.py | **DEAD** | `__init__(self, config: dict)` — required positional dict; config=None raises `TypeError: 'NoneType' object is not subscriptable` |

**Runnable-as-is count:** 15 strategy classes (12 individual + 3 from `momentum.py` trio). The card spec calls out "~17 strategy classes" — the spec list contains 3 dead ones (orb, mtf_filtered_momentum, session_breakout) that cannot be registered with the harness's one-line pattern. The dead-stale count is 3; total inspected: 18.

## Verification command

```
scripts/run_test_scope.sh tests/test_tournament_empty_window.py tests/test_tournament_registration.py
```

## Out-of-scope (deferred)

- Strategy code edits to make the 3 dead strategies registerable (orb, mtf_filtered_momentum, session_breakout) — separate child card.
- Intraday-equity snapshots (skeleton caveat 4) — decomposition card B.
- Multi-symbol matrix — decomposition card C.

## Compute policy compliance

All local runs <5 min. No avaworker stage required for the verification command (synthetic DuckDB fixtures are <10 KB).

## Build summary written FIRST, before implementation. ✅

---END-METADATA---
card_id: c4b86732-9a6c-4169-b5de-e0a12e63280c
build_id: c4b86732-build-20260914_183000
builder: tsubaki
build_date: 2026-09-14T18:30:00-04:00
skills_used:
  - workboard-management
  - card-creation
tools_used:
  - workboard_read
  - workboard_claim
  - git_worktree
  - python3
  - pytest
allowed_files:
  - scripts/run_tournament.py
  - src/tournament/harness.py
  - src/tournament/scorecard.py
  - src/tournament/__init__.py
  - tests/test_tournament_empty_window.py
  - tests/test_tournament_registration.py
  - docs/plans/ayumi-tournament-harness-spec.md
verified_by: rin:review-c4b86732
---END-METADATA---
