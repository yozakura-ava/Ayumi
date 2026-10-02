# MC Project Consolidation Audit — signal-engine / ml-pipeline / fwd-test / backtest → Ayumi + Portfolio Intelligence

**Date:** 2026-09-14
**Requested by:** Craig (2026-09-14 08:49 EDT directive)
**Action:** Fold four stale Mission Control projects into Ayumi / Portfolio Intelligence and archive them.
**Verdict:** All four were stale tracking shells (last updated 2026-07-28, zero tasks, zero milestones). Their code already lives inside Ayumi's tree at `src/forex_bot/{signal_engine, ml, forward_test, backtest}`. **No code migration required.** Consolidation = documentation + Mission Control metadata only.

---

## Per-component findings (evidence cited)

### 1. signal_engine/ → ABSORBED_PARTIAL (Ayumi)
- **Live path (updated 2026-09-14, deep component audit):** `signal_engine` is more live than first recorded — `signal_stats` is the live telemetry spine (`forward_test/blend_runner.py:34`, ctrader adapters), and 11 more modules load transitively via `backtest/strategies/tts_strategy.py` (TTC_XAUUSD chain). Full evidence: workspace `docs/assessments/mc-consolidation-component-audit-2026-09-14.md`.
- **Idle remainder:** `confluence_scorer.py` (ConfluenceScorer, 7/14 booster features), `gate_validator.py`, `htf_analyzer.py`, `pattern_detector.py`, etc. are not imported by the live blend launcher or engine.
- **Open item transferred:** master roadmap task **1C.6** ("Audit signal_engine/ modules against v2.3 spec") is explicitly deferred to post-FTMO. 8/13 components lack test coverage.
- **MC disposition:** archived; 1C.6 + coverage debt recorded on the Ayumi project record.

### 2. ml/ → SUPERSEDED_PARTIAL (split: Ayumi idle / Portfolio Intelligence owns live prediction loops)
- **Evidence:** `scripts/launch_blend_forward_test.py` and `src/forex_bot/forward_test/blend_runner.py` import **nothing** from `ml/` (grep 2026-09-14). The live forward test runs strategies + regime detector + risk stack without the ML confidence layer.
- **NOT superseded by PI:** correction — `ml/` (forex historical-feature ML) and Portfolio Intelligence (multi-asset crypto/equity prediction loops) are different domains. `per_symbol_configs.py` is the lone load-bearing ml module (TTC chain); the rest is idle for the post-FTMO blend-confidence phase.
- **Portfolio Intelligence** (workspace `src/portfolio_intelligence/`) now owns the production prediction/outcome loop — its own `models/` (gradient_boosting, logistic, calibration, challenger lab, risk engine) and `features/` stacks, verified live 2026-09-10 (3,973 predictions, 3,766 outcomes, freshness canary armed — `docs/audits/portfolio_intelligence-audit-2026-09-10.md`).
- **Residual in Ayumi:** `ml/blend_optimizer.py` (Optuna) + `ml/confidence_learner.py` remain roadmap infrastructure for the post-FTMO blend-confidence phase (roadmap rev 4 §3 ML Pipeline).
- **MC disposition:** archived; prediction/outcome ownership noted on the portfolio_intelligence project record; blend-confidence remainder noted on the Ayumi record.

### 3. forward_test/ → ABSORBED_LIVE (Ayumi)
- **Evidence:** `launch_blend_forward_test.py:66` imports `forward_test.blend_runner.BlendForwardTestRunner` — this IS the current live forward test (XAUUSD SRMR+ since Sep 10).
- **BQ-559 close condition met:** the old MC note said "do not close BQ-559 without paper-trade evidence." Live log 2026-09-14 12:52 EDT: `ticks=106835 bars=60 … trades=288 live_fills>0` telemetry with `pnl_from_start=-0.57%`, `dd=3.12%` — trade evidence now exists (and then some: live fills).
- **MC disposition:** archived; BQ-559 evidence requirement satisfied by live blend telemetry.
- **⚠️ Side finding for Craig:** live equity $9,942.71 (-0.57% from start, peak $10,262.45, FTMO dd 3.12% vs 10% cap) — within envelope, but down from the Sep 8 snapshot. Watch, not alarm.

### 4. backtest/ → ABSORBED_ACTIVE (Ayumi)
- **Evidence:** heavy script consumption (`walk_forward_bb_rsi_mean_reversion.py`, `run_tts_walkforward.py`, `run_multi_strategy_optuna_sweep.py`, etc.); fresh artifacts in `reports/` (`blend-harness-2026-09-08-rerun4`, `srmr-wf-revalidation-2026-09-07`); tournament-harness spec designates `scripts/backtest_blend_harness.py` as the production reference harness.
- **Historical MC notes:** risk-cap bug fix (commit a78bfaf) and portfolio_blend normalization are already landed in-tree.
- **Open item transferred:** roadmap Phase 1D blend reconciliation (5-strategy blend position-management model mismatch, `run_blend_5strat.py`) — verify against the Sep 7-8 blend-harness runs during the post-FTMO blend phase.
- **MC disposition:** archived; open item noted on the Ayumi record. 23 dead `backtest/` modules (zero callsites) tracked by card `6f06c041-1c7c-47f5-9f72-6c34893e95fc`; Phase 1D.5/1D.6 closure tracked by card `e7b2a23a-4ce0-4e54-8851-b839b959a64b`.

---

## Mission Control changes applied
| Project | Action |
|---|---|
| signal-engine | status → archived (2026-09-14) |
| ml-pipeline | status → archived (2026-09-14) |
| fwd-test | status → archived (2026-09-14) |
| backtest | status → archived (2026-09-14) |
| ayumi | description updated to absorb: signal_engine 1C.6 audit + 8/13 coverage debt; ml blend-confidence remainder; backtest blend reconciliation check |
| portfolio_intelligence | description updated to note prediction/outcome loop ownership superseding legacy ml/ signal-generation notes |

## Documentation archive
This document is the canonical record of the consolidation. Legacy per-project docs (workspace `docs/forex/`, `docs/research/`, Ayumi `docs/_archive/`, `docs/archive/`) remain in place as history; their tracking authority transfers to the Ayumi master roadmap (rev 4) and this record.
