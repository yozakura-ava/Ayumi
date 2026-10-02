# Strategy Factory — Implementation Specification

**Status:** v1 implementation spec (ready for review)
**Author:** Tsubaki (synthesis from existing research materials)
**Date:** 2026-08-01
**Card:** `d51b8525`
**Sprint:** reina-2026-08-01-012 (resumed)
**Parent docs:**
- Vision: `docs/roadmaps/strategy-factory/strategy-factory-vision-v1.md`
- Pipeline (ground rules): `docs/roadmaps/strategy-factory-pipeline.md`
- Plan B / DSR failure: `docs/decisions/plan-b-statistical-failure.md`
- Master roadmap: `docs/roadmaps/ayumi-master-roadmap.md` (Phase 1D/1E)

---

## ⚠ Source Material Note

The task brief referenced `docs/sprints/strategy-factory-research-notes.md` as a 159-line research notes document. **That file does not exist on disk.** This spec was synthesized from the actual available sources:

| Source | What it provided |
|---|---|
| `docs/roadmaps/strategy-factory/strategy-factory-vision-v1.md` | High-level architecture, phased approach, "what we already have" inventory |
| `docs/decisions/plan-b-statistical-failure.md` | DSR tier thresholds (A/B/C/REJECT), Scenario 3 (all 6 killed), kill criteria |
| `docs/roadmaps/strategy-factory-pipeline.md` | Council ground rules, kill criteria table, dependency chain |
| `docs/roadmaps/ayumi-master-roadmap.md` | SRF/regime detector/ML/confidence components, bug-fix history, FTMO profile |
| `docs/research/strategy-profiles/consolidated_findings_2026-07-22.md` | Bug #4 (regime detector warmup), LBO validation, gate loosening findings |
| `docs/strategies/revalidation-2026-07/report.md` + `revalidation-2026-07-31-embargo/report.md` | Empirical DSR/WF numbers, TTC embargo verdict, low-trade-count statistics |
| `docs/infra/srf-pipeline-decision.md` | SRF cron stub status, real-wiring ownership boundary |
| Source code (skimmed) | Concrete interfaces for walk_forward_runner, DSR integration, blend_optimizer, portfolio_blend, strategy registry |

If the missing research-notes file is later recovered and contains conflicting numbers, treat this spec as the synthesis target and flag the conflict in review. The numbers and structure below are grounded in code + the four canonical docs above, not the missing file.

---

## 0. Executive Summary

The current 6-strategy pool was killed by DSR on XAUUSD (Scenario 3 in `plan-b-statistical-failure.md`). The strategy factory is now the **primary path** to producing a validated, FTMO-viable strategy pool. Manual strategy development produces ~1 strategy / 2-3 weeks; a working factory produces 50-100 candidates / sweep and gates them with DSR.

**This spec converts the vision into a buildable plan across 8 sections:**

1. End-to-end pipeline architecture
2. Component inventory (reusable vs wiring-needed vs net-new)
3. Strategy template design (5 archetypes + plug-in interface)
4. Validation pipeline config (WF windows, DSR scaling, regime gating, OOS isolation, PBO)
5. Deployment & monitoring (blend entry by tier, decay detection, kill criteria)
6. Infrastructure plan (wall-clock, parallelism, storage, scheduling)
7. Phased build plan (5 phases, SP estimates, dependencies, acceptance criteria, calendar)
8. Risk assessment (overfitting, regime sensitivity, snooping, capacity, compute, DSR stringency)

**Concrete target:** Within 4 weeks, the factory should produce ≥3 Tier-A or Tier-B strategies from the existing 6 strategies × 4 pairs × 2 timeframes matrix (48 candidate cells), evaluated under the post-bug-fix regime detector with embargo-corrected WF.

---

## 1. Architecture Diagram

```
                                  ┌──────────────────────────────────────────────┐
                                  │          STRATEGY FACTORY PIPELINE           │
                                  └──────────────────────────────────────────────┘

  ┌──────────────┐    ┌─────────────────────────┐    ┌───────────────────────┐
  │ Market Data  │───▶│  Strategy Templates     │───▶│ Parameter Sweep       │
  │ (DuckDB)     │    │  (5 archetypes +        │    │ (Optuna + PBO)        │
  │ EURUSD H1    │    │   plug-in interface)    │    │ n_trials tracked      │
  │ GBPUSD H1    │    │                         │    │                       │
  │ USDJPY H1    │    │  - momentum             │    │  Parallel:            │
  │ XAUUSD M15   │    │  - mean_reversion       │    │  multiprocessing      │
  │ XAUUSD H1    │    │  - breakout             │    │  nice -n 19           │
  │ + spread     │    │  - trend                │    │  cpulimit -l 20       │
  │   tables     │    │  - session_based        │    │                       │
  └──────────────┘    └─────────────────────────┘    └────────────┬──────────┘
                                                                   │
                                                                   ▼
                                                ┌──────────────────────────────────┐
                                                │   Backtest Engine                │
                                                │   (existing src/forex_bot/       │
                                                │    backtest/engine.py)           │
                                                │                                  │
                                                │   Inputs: bars + strategy +      │
                                                │           spread + commission    │
                                                │   Outputs: per-trade PnL JSONL   │
                                                └──────────────────┬───────────────┘
                                                                   │
                                                                   ▼
                                                ┌──────────────────────────────────┐
                                                │   Walk-Forward Validation        │
                                                │   (run_strategy_walk_forward)    │
                                                │                                  │
                                                │   M15: 5 windows × 70/15/15      │
                                                │   H1:  5 windows × 70/15/15      │
                                                │   embargo_bars: 96 for M15 (24h) │
                                                │   train + val + test split       │
                                                │   min 15 trades/window (T6)      │
                                                └──────────────────┬───────────────┘
                                                                   │
                                                                   ▼
                                                ┌──────────────────────────────────┐
                                                │   Regime Filter (Bug #4 fix)     │
                                                │   (regime/detector.py with       │
                                                │    WINDOW=100 rolling precompute) │
                                                │                                  │
                                                │   Labels: TRENDING / CHOPPY /    │
                                                │           VOLATILE / QUIET       │
                                                │   Per-strategy affinity gates    │
                                                └──────────────────┬───────────────┘
                                                                   │
                                                                   ▼
                                                ┌──────────────────────────────────┐
                                                │   DSR Gate                       │
                                                │   (annotate_wf_results_with_dsr) │
                                                │                                  │
                                                │   n_trials = max(160, 3×cells)   │
                                                │   Tier A: 5 wins + Sharpe≥1.5    │
                                                │           + dsr_p<0.05           │
                                                │   Tier B: 4 wins + Sharpe≥0.95   │
                                                │           + dsr_p<0.10           │
                                                │   Tier C: 3 wins + Sharpe≥0.50   │
                                                │           + dsr_p<0.10           │
                                                │   REJECT: anything else          │
                                                │   Insufficient: n_trades<30      │
                                                └──────────────────┬───────────────┘
                                                                   │
                                                                   ▼
                                                ┌──────────────────────────────────┐
                                                │   PBO Check                      │
                                                │   (Probability of Backtest       │
                                                │    Overfitting on Optuna params) │
                                                │                                  │
                                                │   PBO < 0.30 required to promote │
                                                └──────────────────┬───────────────┘
                                                                   │
                              ┌──────────────REJECT──────────────────┤
                              ▼                                     ▼
                ┌────────────────────────┐         ┌──────────────────────────────┐
                │  Quarantine / Log      │         │  Deploy Pool                 │
                │  (audit trail only)    │         │  (DSR Tier A/B/C)            │
                │                        │         │                              │
                │  Reason recorded:      │         │  - Tier A: full allocation   │
                │  - PF<1.2 with spread  │         │  - Tier B: half allocation   │
                │  - dsr_p≥0.15          │         │  - Tier C: paper only        │
                │  - windows<3           │         │                              │
                │  - PBO≥0.30            │         │  Stored in DuckDB            │
                │  - n_trades<30          │         │  `deploy_pool` table         │
                └────────────────────────┘         └─────────────┬────────────────┘
                                                                │
                                                                ▼
                                                ┌──────────────────────────────────┐
                                                │   Live Monitor                  │
                                                │   (rolling 60-day Sharpe +      │
                                                │    DD + trade count decay)       │
                                                └──────────────────┬───────────────┘
                                                                   │
                                                  ┌──healthy──────┴──────decay──────┐
                                                  ▼                                 ▼
                                       ┌────────────────────┐         ┌────────────────────────┐
                                       │  Continue          │         │  Auto-Replace          │
                                       │  (forward test)    │         │  (trigger conditions)  │
                                       │                    │         │                        │
                                       │  Rebalance weekly  │         │  - Sharpe drop > 50%   │
                                       │  by DSR tier       │         │  - DD breach 8%        │
                                       └────────────────────┘         │  - n_trades < 10 in   │
                                                                       │    trailing 30d         │
                                                                       │                        │
                                                                       │  → Return to top of   │
                                                                       │    pipeline (regen)   │
                                                                       └────────────────────────┘

  Cross-cutting concerns:
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ • OOS isolation: last 6 months (Jan-Jul 2026) held out, no optimizer touch │
  │ • Spread costs mandatory: XAUUSD 3.0p, EURUSD/GBPUSD 1.5p, USDJPY 1.2p      │
  │ • Commission $3.5/lot, slippage 0.2p (Liora ground rule)                    │
  │ • No bar subsampling (Liora); reduce date range if compute tight            │
  │ • RandomForest confidence learner frozen during opt (Kaito)                │
  │ • RegimeDetector config frozen during pipeline (Kaito)                      │
  │ • All results persisted to DuckDB `research.duckdb` for audit               │
  └─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Inventory

| Component | Path (existing or planned) | State | Owner lane | Est. SP |
|---|---|---|---|---|
| **Backtest engine** | `src/forex_bot/backtest/engine.py` | ✅ Reusable (working) | n/a | 0.0 |
| **Walk-forward runner** | `src/forex_bot/backtest/walk_forward_runner.py` | ✅ Reusable (working, embargo_bars plumbed) | n/a | 0.0 |
| **DSR computation** | `src/forex_bot/backtest/dsr.py` | ✅ Reusable (Bailey & López de Prado, n_trades≥30 guard) | n/a | 0.0 |
| **DSR integration (post-WF gate)** | `src/forex_bot/quant/dsr_integration.py` | ✅ Reusable (tier rank, JSONL consumer) | n/a | 0.0 |
| **OOS gate tier config** | `src/forex_bot/quant/oos_gate.py` | ✅ Reusable (A=1.50/0.05, B=0.95/0.10, C=0.50/0.10) | n/a | 0.0 |
| **Optuna blend optimizer** | `src/forex_bot/ml/blend_optimizer.py` | ✅ Reusable (CPU budget metering, softmax weights) | n/a | 0.0 |
| **Portfolio blend driver** | `src/forex_bot/backtest/portfolio_blend.py` | ✅ Reusable (5 weight methods: inverse_variance, equal_risk, profit_factor, sharpe, combined_score; FTMO criteria) | n/a | 0.0 |
| **Strategy registry** | `src/forex_bot/strategies/registry.py` | ✅ Reusable (15 strategies pre-loaded) | n/a | 0.0 |
| **WF+DSR driver script** | `scripts/run_blend_walkforward_for_dsr.py` | ✅ Reusable (produces JSONL → DSR) | n/a | 0.0 |
| **Regime detector** | `src/forex_bot/regime/detector.py` | ⚠️ Wiring-needed (Bug #4 fix at WINDOW=100; per-strategy affinity gates not wired) | Strategy lane | 1.0 |
| **Confidence engine** | `src/forex_bot/confidence/` | ⚠️ Wiring-needed (built, not wired to blend driver) | Strategy lane | 1.5 |
| **ML confidence learner (RandomForest)** | `src/forex_bot/ml/confidence_learner.py` | ⚠️ Wiring-needed (frozen during opt per Kaito) | ML lane | 1.0 |
| **SRF weekly sweep cron** | `src/forex_bot/srf/weekly_sweep.py` | ⚠️ Stub (writes `weekly_sweep:ok`, real wiring is application lane per `srf-pipeline-decision.md`) | Application lane | 1.5 |
| **Nightly sweep cron** | OpenClaw cron (`nightly_topk`) | ⚠️ Wiring-needed (no scheduler adapter to factory driver) | Infra lane | 0.5 |
| **Strategy template base class** | new: `src/forex_bot/factory/template.py` | 🆕 Net-new (parameter space + Optuna adapter) | Strategy lane | 1.5 |
| **5 archetype templates** | new: `src/forex_bot/factory/templates/` | 🆕 Net-new (momentum, mean_reversion, breakout, trend, session) | Strategy lane | 4.0 (0.8 each) |
| **Factory driver (orchestrator)** | new: `src/forex_bot/factory/run_factory.py` | 🆕 Net-new (template → sweep → WF → DSR → tier) | Strategy lane | 2.0 |
| **Decay detector (live)** | new: `src/forex_bot/factory/decay_detector.py` | 🆕 Net-new (rolling 60d Sharpe + DD + trade count) | Infra lane | 1.5 |
| **Auto-replacement protocol** | new: `src/forex_bot/factory/replace.py` | 🆕 Net-new (trigger conditions + promotion flow) | Infra lane | 1.0 |
| **Result storage (DuckDB schema)** | new: `data/research/factory.duckdb` + schema | 🆕 Net-new (`factory_runs`, `deploy_pool`, `decay_log` tables) | Infra lane | 1.0 |
| **Factory dashboards** | new: `docs/factory/dashboards/*.md` (templated) | 🆕 Net-new (per-sweep markdown report) | Infra lane | 1.0 |
| **Tests for factory modules** | new: `tests/factory/` | 🆕 Net-new (template spec, tier assignment, decay triggers) | Tsubaki lane | 1.5 |

**Totals:**

- ✅ Reusable (already working, no work needed): **9 components / 0.0 SP**
- ⚠️ Wiring-needed (exists but disconnected): **5 components / 5.5 SP**
- 🆕 Net-new: **8 components / 13.5 SP**
- **Component total: 19.0 SP** (component-level work only)
- **§7 phase total: 30.5 SP** (includes runtime, audit, documentation, and integration tasks not enumerated as separate components above)

---

## 3. Strategy Template Design

### 3.1 The 5 Archetypes

| Archetype | What it does | Regime affinity | Default pairs | Default timeframes | Key parameters (Optuna knobs) |
|---|---|---|---|---|---|
| **momentum** | Buys strength / sells weakness via rate-of-change + MA cross confirmation | TRENDING, VOLATILE | XAUUSD, GBPUSD, USDJPY | M15, H1 | `roc_period` [5..30], `fast_ma` [5..20], `slow_ma` [20..100], `atr_stop_mult` [1.0..3.0], `atr_tp_mult` [1.5..4.0], `adx_min` [15..30] |
| **mean_reversion** | Fades extremes via Bollinger/StdDev + RSI overshoot | QUIET, CHOPPY | EURUSD, GBPUSD, USDJPY | M15, H1 | `bb_period` [10..40], `bb_std` [1.5..3.0], `rsi_period` [7..21], `rsi_oversold` [20..35], `rsi_overbought` [65..80], `session_filter` [asia/london/ny/any] |
| **breakout** | Enters on range expansion / squeeze release with volume confirmation | VOLATILE, TRENDING | XAUUSD, GBPUSD | M15, H1 | `lookback` [10..50], `atr_expansion_ratio` [1.2..2.5], `volume_filter` [0.0..2.0], `donchian_period` [10..50], `confirmation_bars` [1..5] |
| **trend_following** | Long-term direction via higher-timeframe MA + Donchian trailing | TRENDING only | XAUUSD, EURUSD, USDJPY | H1, H4 | `htf_ma_period` [50..200], `donchian_entry` [20..100], `donchian_exit` [10..50], `trailing_atr_mult` [1.5..4.0], `regime_filter` [required: TRENDING] |
| **session_based** | Time-of-day edge: London/NY/Asia opens and session ranges | regime-agnostic (session is the alpha) | XAUUSD, GBPUSD, EURUSD, USDJPY | M15 | `session` [london/ny/asia/london_ny], `entry_window_min` [0..120], `exit_window_min` [60..240], `range_lookback` [10..50], `breakout_filter` [true/false] |

### 3.2 Template Class Interface (Plug-In Contract)

All templates inherit from a new abstract base:

```python
# new file: src/forex_bot/factory/template.py

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable
import numpy as np
import pandas as pd

@dataclass(frozen=True)
class ParamSpec:
    """One Optuna-searchable parameter."""
    name: str
    kind: str  # "int" | "float" | "categorical"
    low: float | None = None
    high: float | None = None
    choices: tuple[Any, ...] | None = None
    step: float | None = None
    log: bool = False  # log-scale sampling

@dataclass(frozen=True)
class StrategyTemplate(ABC):
    """A plug-in strategy archetype: parameter space + signal generator + factory."""
    archetype_id: str           # e.g. "momentum", "breakout"
    description: str
    default_pairs: tuple[str, ...]
    default_timeframes: tuple[str, ...]
    regime_affinity: tuple[str, ...]  # subset of {TRENDING, CHOPPY, VOLATILE, QUIET}

    @property
    @abstractmethod
    def param_space(self) -> tuple[ParamSpec, ...]:
        """Return the Optuna parameter space."""

    @abstractmethod
    def build_strategy(self, params: dict[str, Any], pair: str) -> "ISignalStrategy":
        """Instantiate a concrete strategy with the given parameter set."""

    @abstractmethod
    def default_params(self) -> dict[str, Any]:
        """Return a sensible default (used as the 'default vs Optuna' baseline)."""

    @abstractmethod
    def regime_filter(self) -> tuple[str, ...] | None:
        """Return the regimes this template should be allowed to trade in (None = all)."""

    def to_optuna_spec(self) -> dict[str, Callable]:
        """Convenience: map param_space → Optuna suggest_* callables."""
        ...
```

**Adding a new template** (e.g., `volatility_breakout`, `multi_timeframe_trend`, `news_announcement_drift`):

1. Create `src/forex_bot/factory/templates/<archetype>.py`
2. Subclass `StrategyTemplate`; implement the 4 abstract methods
3. Register it via `FactoryRegistry.register(template)` (a new module-level registry)
4. The factory orchestrator picks it up automatically — no other code changes needed

### 3.3 Template-to-ISignalStrategy Bridge

Templates do NOT reimplement signal logic. They:

1. **Parametrize** existing ISignalStrategy classes (e.g., `KillzoneMomentumStrategy` is one concrete realization of the `momentum` template)
2. **Map the Optuna-sampled param dict** to the strategy's config dataclass
3. **Reuse** all existing backtest infrastructure (engine, WF runner, DSR)

Where a template has no concrete existing strategy (e.g., `session_based` with new sub-types), the template provides its own minimal `ISignalStrategy` subclass. **Rule of thumb:** if 3+ existing strategies share the same template, factor it out as a real template.

---

## 4. Validation Pipeline Config

### 4.1 Walk-Forward Windows

| Timeframe | Windows | train_ratio | val_ratio | test_ratio | overlap_ratio | embargo_bars | Source |
|---|---|---|---|---|---|---|---|
| M5 | 8 | 0.70 | 0.15 | 0.15 | 0.20 | 288 (=24h) | New (per Liora ground rule: at least 24h gap) |
| **M15** | **5** | **0.70** | **0.15** | **0.15** | **0.20** | **96** (=24h) | Existing `walk_forward_runner.run_strategy_walk_forward` defaults; embargo per `ttc_xauusd.EMBARGO_BARS_M15` precedent (Jul 31 rework) |
| **H1** | **5** | **0.70** | **0.15** | **0.15** | **0.20** | **24** (=24h) | Same defaults, TF-scaled embargo |
| H4 | 5 | 0.70 | 0.15 | 0.15 | 0.20 | 6 (=24h) | New |
| D1 | 5 | 0.70 | 0.15 | 0.15 | 0.00 | 1 (=24h, 1 trading day) | New; overlap=0 because D1 windows are sparse |

**Why these ratios:**
- 70/15/15 matches the production `run_strategy_walk_forward` defaults and the Jul 24 revalidation (`scripts/build_report.py`)
- Overlap=0.20 keeps ~1.5 windows of forward visibility while allowing the train set to grow by ~20%
- Embargo at ≥24h addresses autocorrelation (Bug class discovered in `ttc_xauusd` Jul 31 rework, card `4fbbef5e`)

### 4.2 DSR `n_trials` Scaling Formula

```python
def compute_dsr_n_trials(cell_count: int) -> int:
    """Conservative multiple-testing correction factor for DSR."""
    base = 160  # canonical oos_gate default (10 strat × 4 pairs × 4 TF)
    return max(base, 3 * cell_count)
```

**Where `cell_count` = `(number of strategies) × (number of pairs) × (number of timeframes) × (Optuna trials per cell)`.**

| Sweep size | cell_count | DSR n_trials |
|---|---|---|
| Manual: 6 strat × 1 pair × 1 TF × 1 trial | 6 | 160 (floor) |
| Current pool: 6 × 4 × 2 = 48 (no Optuna) | 48 | 160 (floor) |
| Factory pilot: 5 templates × 4 pairs × 2 TF × 20 trials = 800 | 800 | 2,400 |
| Full sweep: 5 × 4 × 5 × 50 trials = 5,000 | 5,000 | 15,000 |

**Rationale:** DSR's `E[max SR | null]` grows with `sqrt(2 * log(n_trials))`. Underestimating `n_trials` lets false positives through. The `max(160, 3 × cells)` rule bounds the multiple-testing penalty at ≥160 (the proven conservative default) and scales with the actual search volume.

### 4.3 Regime Gating

**Decision: YES, regime gating is required, not optional.**

Evidence from `consolidated_findings_2026-07-22.md`:

> "the corrected cache invalidates much of the prior session's confidence in the gated blend... SRMR+ baseline is genuinely profitable (71 trades, PF=1.29, +$333, DD=3%, WR=68%) — QUIET-only regime filter is load-bearing, not arbitrary."

> "regime gates are load-bearing for blend edge — not arbitrary filters"

**Implementation:**
- Each template declares its `regime_affinity` (tuple from {TRENDING, CHOPPY, VOLATILE, QUIET})
- The factory's regime filter step skips a (template, pair, TF, params) cell when the bars' regime label is not in the affinity tuple
- Bug #4 fix MUST be in place first: `RegimeDetector` must be invoked with **100-bar rolling precompute windows**, not 60 (per `consolidated_findings_2026-07-22.md` §Bug #4)
- "Insufficient data" rule: any regime bucket with <10 trades → not a signal, marked as such (Liora ground rule)
- **RegimeDetector config is FROZEN** for an entire pipeline run (Kaito ground rule) — no mid-stream retuning

### 4.4 Minimum Trade Count

| Threshold | Value | Why |
|---|---|---|
| **Per-window warning** | 15 trades | Existing T6 in `walk_forward_runner._check_trade_count_warning` (MIN_TRADES_WARNING=15); stats become thin below this |
| **DSR eligibility floor** | 30 trades total | Existing `_MIN_TRADES_FLOOR = 30` in `dsr_integration.py`; prevents DSR p-values collapsing to ~0 from tiny samples |
| **Insignificance mark** | <10 trades per regime bucket | Liora ground rule; marked "insufficient data" not "no edge" |

**Pipeline-level rule:** Cells with <15 trades in a WF window are kept (with WARNING log) but flagged for the final report. Cells with <30 total OOS trades are **DSR-ineligible** → automatic REJECT regardless of Sharpe.

### 4.5 OOS Isolation

Per **Kaito ground rule**: **Last 6 months of data (Jan 2026 → Jul 2026) held out from all optimization.** Only final validation touches this data.

```
|---------- 5 years train+val+test windows ---------|--OOS--|
 2020-01                                       2025-12  2026-07
                                                  ↑       ↑
                                          Pipeline ends  Final validation only
                                          (no opt here)  (DSR + PBO + MC)
```

**Implementation:**
- `walk_forward_runner` already supports embargo; add a top-level `oos_holdout: tuple[date, date]` param to the factory driver
- The factory driver splits bars chronologically: `(2020-01..2025-12)` for the pipeline; `(2026-01..2026-07)` is locked behind a `require_explicit_unlock=True` flag
- Any call passing `oos_holdout` dates but without the unlock flag raises `PermissionError`
- Forward test (Phase 4) uses the OOS window by definition — no separate holdout

### 4.6 PBO (Probability of Backtest Overfitting) for Optuna Params

**Required for ALL Optuna-derived parameter sets before promotion to deploy pool.** Per Liora ground rule.

Implementation:
- Use the canonical `srf/pbo.py` (or equivalent) to compute PBO from the per-trial performance rankings
- **PBO < 0.30** → acceptable, mark params as "stable"
- **0.30 ≤ PBO < 0.50** → marginal, promote to Tier C only
- **PBO ≥ 0.50** → reject, log to quarantine

The pipeline driver emits a `pbo_score` per cell; cells without PBO are blocked from Tier A/B promotion.

---

## 5. Deployment & Monitoring

### 5.1 Blend Entry by DSR Tier

Allocation weights per DSR tier, applied during the blend construction phase:

| DSR Tier | Allocation weight | Position size factor | Promotion gate |
|---|---|---|---|
| **Tier A** (production) | **0.50 of total risk** | 1.0× (full) | WF passes + DSR p<0.05 + Sharpe≥1.50 + PBO<0.30 |
| **Tier B** (demo) | 0.30 of total risk | 0.5× (half) | WF passes + DSR p<0.10 + Sharpe≥0.95 + PBO<0.30 |
| **Tier C** (paper) | 0.20 of total risk | 0.25× (quarter) | WF passes + DSR p<0.10 + Sharpe≥0.50 + PBO<0.50 |
| **REJECT** | 0 (quarantine) | n/a | Any failure mode |

**Risk budget anchor:** 0.5% per trade default, 1.0% hard cap, ≤2% total open risk (FTMO profile). Allocation weights above are the **share of the 2% open-risk budget** that each tier may consume at peak.

**Rebalancing:** When a Tier-A strategy enters decay, its share (0.50) is redistributed to remaining Tier-A and Tier-B strategies pro-rata. See §5.3.

### 5.2 Decay Detection

**Monitor continuously** (every forward-test tick + once daily at session close):

| Metric | Window | Healthy | Watch | Decay trigger |
|---|---|---|---|---|
| Rolling Sharpe (60-day) | 60 trading days | ≥0.95× baseline Sharpe | 0.70-0.95× baseline | <0.70× baseline for 5 consecutive sessions |
| Rolling max drawdown (60-day) | 60 trading days | <6% | 6-8% | >8% (single observation OR mean over 5 days) |
| Trade count (30-day) | 30 calendar days | ≥50% of backtest rate | 25-50% | <25% for 5 consecutive sessions |
| Per-regime PF | each regime bucket | ≥0.95× baseline | 0.70-0.95× | <0.70× for 10 trades in that regime |
| Win rate | 60-day | ≥0.95× baseline | 0.85-0.95× | <0.85× for 5 consecutive sessions |

**Output:** Each monitoring tick writes one row to `decay_log` in DuckDB; the decay detector reads the trailing 60d window and emits one of: `HEALTHY | WATCH | DECAY_TRIGGERED`.

### 5.3 Auto-Replacement Protocol

When `DECAY_TRIGGERED` fires for any deployed strategy:

```
1. HALT the decayed strategy immediately (set flag in deploy_pool)
2. LOG the trigger reason + last 60d metrics to decay_log
3. NOTIFY (workboard comment on the originating card)
4. REBALANCE: redistribute the decayed strategy's allocation weight
   to remaining deploy_pool members pro-rata
5. SPAWN REPLACEMENT card: a factory sweep targeted at the same
   (archetype, pair, timeframe) cell that produced the decayed strategy
6. NEW candidate must clear the same DSR + PBO gates before promotion
7. FORWARD TEST isolation (Rei ground rule): running blend stays
   untouched until the new validated config passes ALL gates
```

**Promotion flow for new candidates:**

```
factory sweep → WF + DSR + PBO → Tier rank → quarantine (if REJECT)
                                                  ↓
                                          deploy_pool (with WATCH flag)
                                                  ↓
                                          paper test for 5 trading days
                                                  ↓
                                          if PF≥0.9× backtest AND DD≤1.2× backtest → promote to live
                                          else → revert to quarantine
```

### 5.4 Rebalancing Frequency

| Event | Frequency | Method |
|---|---|---|
| Tier weight rebalance (static) | Weekly (Saturday 02:00 ET) | Recompute weights from current deploy_pool composition |
| Triggered rebalance | On every `DECAY_TRIGGERED` event | Redistribute decayed strategy's weight pro-rata |
| Sweep-triggered rebalance | After every factory sweep completes | Add newly-promoted strategies; rebalance |
| Manual rebalance | On Craig/Ava directive | One-shot; logged with reason |

### 5.5 Kill Criteria (From Pipeline Doc)

Per Mika's ground rule, applied at every level:

| Item type | Kill condition | Action |
|---|---|---|
| Gate tuning | Loosened gate PF < 1.2 (with spread) | Revert to original gate |
| New strategy | <20 trades on full XAUUSD M15 data | Abandon, queue next |
| New strategy | PF < 0.8 in ALL regimes | Abandon |
| FX retuning | PF < 0.8 in ALL regimes for a pair | Skip that pair |
| Optuna sweep | Best params don't beat default by >10% PF | Keep defaults |
| Profiling | All regime buckets <10 trades | Mark "insufficient data" |
| **Factory cell** | **(new) DSR p≥0.15 OR PF<1.2 with spread OR n_trades<30** | **Quarantine, no promotion** |
| **Factory sweep** | **(new) 0/48 cells produced Tier A/B/C AND pilot completed** | **Pause factory, surface to Ava** |

### 5.6 FTMO Constraints (Per Master Roadmap, LOCKED)

- Daily loss limit: 5% ($500)
- Max total loss: 10% ($1,000)
- Profit target: 10% ($1,000)
- Per-trade risk: 0.5% default, 1.0% hard cap
- Per-strategy open risk: 1.5% max
- Portfolio open risk: ≤2%
- Recovery: 1-2% DD → review; 3-5% → cut to 0.25%; >5% → pause 24h

---

## 6. Infrastructure Plan

### 6.1 Wall-Clock Estimates (from Research Notes Benchmarks)

| Sweep size | Estimated wall-clock | Source |
|---|---|---|
| 6 strategies × 1 pair (XAUUSD) × 5 windows | **~90s** | Per `scripts/run_blend_walkforward_for_dsr.py` benchmark (cited in vision doc and pipeline doc) |
| 6 strategies × 3 pairs × 5 windows | **~5 min** (~270s) | Linear scaling, 90s × 3 pairs; matches revalidation report (`docs/strategies/revalidation-2026-07/report.md`) runs in 4-6 min |
| Factory pilot: 5 templates × 4 pairs × 2 TF × 20 trials | ~80-100 min | 48 base cells × 90s ≈ 72 min; +Optuna overhead ~15-30% |
| Full sweep: 5 templates × 4 pairs × 5 TF × 50 trials | ~10-14 hours | 5,000 cells × 90s ≈ 125h raw; with parallelism (4 cores) ≈ 31h; with embargo+DSR overhead ≈ 10-14h realistic |
| Single-cell deep-dive (full Optuna 100 trials + WF + DSR + PBO + MC) | ~15-20 min | Optuna dominates; ~9-12s/trial × 100 trials ≈ 15-20 min |

**Conservative planning number:** budget **2× the benchmark** for production runs to absorb DB I/O, regime detection overhead, and PBO computation.

### 6.2 Parallelism Model

**Process model:** `multiprocessing.Pool` with worker count = `min(cell_count, physical_cores - 2)`.

**CPU caps** (per Craig ground rule #9):

| Layer | Setting |
|---|---|
| Process niceness | `nice -n 19` (lowest priority) |
| Per-process CPU limit | `cpulimit -l 20` (20% of one core) |
| DuckDB thread count | `threads=1` (single-threaded queries) |
| DuckDB memory limit | `memory_limit=512MB` |
| Process count cap | `min(physical_cores - 2, 8)` (avoid memory pressure) |
| Wall-clock timeout per cell | 600s (10 min hard limit) |

**Why these caps:** the host is shared with the OpenClaw gateway, forward-test launcher, and cron sweepers. Factory runs are background compute — they yield resources rather than monopolizing them.

**Failure isolation:** one failed cell (crash, timeout) writes to `factory_failures` table and exits worker; pool continues. No "all-or-nothing" sweep.

### 6.3 Storage Strategy

| Data | Storage | Format | Retention |
|---|---|---|---|
| Per-trade PnL records | **JSONL** (append-only, gzip after 100 MB) | `{ts, pnl, conf, dir, pair, tf, strat_id, params_hash, regime}` | 1 year rolling; archive to cold storage after |
| Per-window WF metrics | **DuckDB** (`factory_runs.windows`) | structured columns | Permanent (audit) |
| Per-cell DSR tier + PBO | **DuckDB** (`factory_runs.cells`) | structured columns | Permanent |
| Deploy pool state | **DuckDB** (`deploy_pool`) | structured + JSON strategy blob | Permanent |
| Decay log | **DuckDB** (`decay_log`) | append-only time series | Permanent |
| Sweep metadata | **DuckDB** (`factory_runs.sweeps`) | start, end, n_cells, n_tier_a, n_tier_b, … | Permanent |
| Optuna studies | **SQLite** (one file per study) | Optuna native | Permanent |
| Market data (bars) | existing **DuckDB** (`ayumi_market.duckdb`) | structured | Permanent |

**Why JSONL for trades, DuckDB for aggregates:**
- JSONL is append-only and streamable; no schema migrations as we add fields
- DuckDB gives fast SQL queries over millions of windows/cells; ~5 MB DB after 100 sweeps
- Optuna's native storage handles study state efficiently; SQLite is its default

### 6.4 Scheduling

| Job | Schedule | Executor | Notes |
|---|---|---|---|
| **Factory pilot sweep** | Weekly Saturday 04:00 ET | OpenClaw cron | 5 templates × 4 pairs × 2 TF × 20 trials; ≈80-100 min wall-clock |
| **Factory full sweep** | Monthly 1st Sunday 02:00 ET | OpenClaw cron | 5 × 4 × 5 × 50 trials; ≈10-14 h wall-clock; budget 16 h timeout |
| **Nightly health check** | Daily 23:30 ET | OpenClaw cron | Re-run DSR on trailing 30d OOS for current deploy_pool; flag drift |
| **Decay detector** | Every 5 min during market hours | Forward test launcher | In-process, no cron |
| **Per-tick confidence snapshot** | Every strategy tick | Forward test launcher | In-process |
| **Daily audit** | Daily 22:00 ET | existing (`scripts/daily_audit.py`) | Add factory deploy_pool state to report |
| **RegimeDetector precompute** | Weekly Friday 23:00 ET | OpenClaw cron | Regenerate 100-bar rolling labels for next week (Bug #4 fix relies on this cache) |

**Concurrency safety:**
- All crons acquire an advisory lock (`data/research/research.lock`, existing) before writing
- Factory sweeps use a separate `factory.lock` to avoid blocking forward test
- Decay detector does not lock — it reads only

### 6.5 Compute Budget

Per Craig ground rule #9 + Mick's kill criteria:

- Daily CPU budget: 4 hours wall-clock across all factory jobs (pilot + health + decay monitoring)
- Weekly budget: 12 hours wall-clock (includes 100-min pilot + nightly health × 7 + regime precompute)
- Monthly full sweep: 16 hours, separate budget

**Alerting:** if any cron exceeds its time budget by >50%, post a workboard comment + slack alert. Don't auto-disable — investigate first.

---

## 7. Phased Plan

> Phases build on each other in strict order. Each phase has a single owner lane and a binary acceptance gate.

### Phase 1 — Pipeline Assembly (Wiring Only)

**Scope:** Wire existing components into an end-to-end factory driver with the regime detector and embargo fixes. **No new templates yet.** The goal is to take the existing 6 strategies × 4 pairs × 2 TF matrix and run them through `run_strategy_walkforward_for_dsr.py` → `annotate_wf_results_with_dsr.py` → tier rank → DuckDB, with regime filtering and OOS isolation enforced.

| Item | Owner lane | Est. SP |
|---|---|---|
| 1.1 Fix Bug #4 in regime detector (100-bar precompute + cache invalidation) | Strategy | 1.0 |
| 1.2 Implement OOS isolation in factory driver (last 6 months held out + unlock flag) | Strategy | 0.5 |
| 1.3 Wire `run_blend_walkforward_for_dsr.py` into a factory orchestrator with proper CLI | Strategy | 1.0 |
| 1.4 Add `factory_runs` + `deploy_pool` DuckDB tables + schema migrations | Infra | 0.5 |
| 1.5 Wire DSR annotation + tier rank + REJECT logging into factory driver | Strategy | 0.5 |
| 1.6 Unit tests: OOS isolation, tier rank edge cases, regime filter integration | Tsubaki | 1.0 |
| 1.7 Smoke run on 6 strat × 1 pair × 1 TF = 6 cells; verify end-to-end JSONL→DuckDB | Tsubaki | 0.5 |

**Total Phase 1: 5.0 SP / ~5 calendar days**

**Dependencies:** None. Can start immediately.

**Acceptance criteria:**
- [ ] Factory driver runs `python3 -m factory.run_factory --pilot` and produces JSONL + DuckDB rows for all 6 strategies × 1 pair × 1 TF
- [ ] OOS isolation: any attempt to use `2026-01..2026-07` bars in optimization raises `PermissionError` without `--unlock-oos` flag
- [ ] DSR tier assigned for every cell; REJECT cells logged to `factory_runs.cells` with reason
- [ ] Regime detector reports all 4 labels (TRENDING, CHOPPY, VOLATILE, QUIET) in cache (Bug #4 fix verified)
- [ ] Unit tests pass; smoke test produces expected row counts
- [ ] No regressions in existing walk_forward_runner tests

**Exit gate:** Pilot run completes, DSR tiers assigned, all 48 cells ranked. Ready for Phase 2 templates.

---

### Phase 2 — Template Library

**Scope:** Convert existing strategies into parameterized templates; add 1-2 new archetypes (e.g., `volatility_breakout`, `multi_timeframe_trend`) per the vision doc's Phase 2.

| Item | Owner lane | Est. SP |
|---|---|---|
| 2.1 Implement `StrategyTemplate` abstract base class + `ParamSpec` | Strategy | 1.0 |
| 2.2 Implement `momentum` template (wraps killzone_momentum + mtf_filtered_momentum + ttc_xauusd) | Strategy | 1.0 |
| 2.3 Implement `mean_reversion` template (wraps srmr_plus + bb_rsi_reversion + session_range_mr_ict_filtered) | Strategy | 1.0 |
| 2.4 Implement `breakout` template (wraps dual_tf_squeeze_pro + volatility_squeeze + volatility_regime_breakout + donchian_atr_trend_v2) | Strategy | 1.0 |
| 2.5 Implement `trend_following` template (new code, HTF MA + Donchian trailing) | Strategy | 1.0 |
| 2.6 Implement `session_based` template (wraps london_breakout_retest + session_breakout_*) | Strategy | 1.0 |
| 2.7 `FactoryRegistry` + registration CLI | Strategy | 0.5 |
| 2.8 Tests: each template produces ≥1 valid strategy with default + Optuna params | Tsubaki | 1.0 |

**Total Phase 2: 7.5 SP / ~7 calendar days**

**Dependencies:** Phase 1 complete.

**Acceptance criteria:**
- [ ] 5 templates registered; each can produce a concrete `ISignalStrategy` from default params
- [ ] Optuna can sample each template's `param_space` and instantiate strategies
- [ ] All existing tests still pass (regression safety)
- [ ] New tests: `tests/factory/test_templates.py` covers all 5 templates × default + 5 Optuna samples each
- [ ] Each template declares regime_affinity correctly per §3.1
- [ ] At least 3 existing strategies successfully wrapped per template (no functional regression)

**Exit gate:** 5 templates wired; Optuna can sweep all of them. Ready for Phase 3 full sweep.

---

### Phase 3 — Sweep & Validate

**Scope:** Run the full pipeline against 5 templates × 4 pairs × 2 TF × 20 trials = 800 cells. Identify Tier A/B candidates.

| Item | Owner lane | Est. SP |
|---|---|---|
| 3.1 Pilot sweep: 5 templates × 4 pairs × 2 TF × 20 trials | Strategy | 2.0 (incl. runtime) |
| 3.2 PBO computation for all Optuna-derived params | Strategy | 1.0 |
| 3.3 Per-cell report (markdown): tier, metrics, regime breakdown | Tsubaki | 0.5 |
| 3.4 Top-N selection: best 10 Tier A/B candidates | Strategy | 0.5 |
| 3.5 Confirmation re-run: re-validate top 10 with 100-trial Optuna + MC + DSR | Strategy | 2.0 (incl. runtime) |

**Total Phase 3: 6.0 SP / ~6 calendar days**

**Dependencies:** Phase 2 complete.

**Acceptance criteria:**
- [ ] Pilot sweep completes within 2 hours wall-clock
- [ ] ≥3 cells produce Tier A or B (acceptance target from vision doc: "identify 5-10 Tier A/B")
- [ ] PBO < 0.30 for all promoted cells
- [ ] Confirmation re-run reproduces DSR tiers within ±0.05 p-value
- [ ] No cell with n_trades<30 promoted
- [ ] Report at `docs/factory/reports/sweep-YYYY-MM-DD.md` with full per-cell breakdown

**Exit gate:** ≥3 Tier A/B strategies identified and confirmed. Ready for Phase 4 deployment.

---

### Phase 4 — Live Deployment (Blend)

**Scope:** Wire the validated Tier A/B candidates into a deployable blend with live monitoring.

| Item | Owner lane | Est. SP |
|---|---|---|
| 4.1 Decay detector (rolling 60d Sharpe + DD + trade count) | Infra | 1.5 |
| 4.2 Auto-replacement protocol (trigger conditions + promotion flow) | Infra | 1.0 |
| 4.3 Wire blend driver to use deploy_pool composition + tier weights | Strategy | 1.0 |
| 4.4 Add deploy_pool state to daily audit script | Infra | 0.5 |
| 4.5 Initial deploy: paper test for 5 trading days, then live with tier weights | Ava + Infra | 1.0 |
| 4.6 Tests: decay triggers, replacement flow, weight redistribution | Tsubaki | 1.0 |

**Total Phase 4: 6.0 SP / ~6 calendar days**

**Dependencies:** Phase 3 complete with ≥3 validated candidates.

**Acceptance criteria:**
- [ ] Decay detector emits HEALTHY/WATCH/DECAY_TRIGGERED for each deploy_pool member
- [ ] Auto-replacement spares a workboard card within 1 minute of DECAY_TRIGGERED
- [ ] Blend driver uses tier weights from §5.1 (50% A, 30% B, 20% C)
- [ ] 5 clean paper-trading days with no execution errors
- [ ] Daily audit reports deploy_pool state + decay_log entries
- [ ] FTMO guard wired (daily DD tracking, trailing drawdown, kill switch armed per master roadmap §Phase 2)

**Exit gate:** Blend live with ≥3 validated strategies, decay monitoring active, ready for Phase 5 continuous operation.

---

### Phase 5 — Continuous Operation

**Scope:** Wire scheduling, dashboards, and ongoing maintenance. The factory now runs as a background service.

| Item | Owner lane | Est. SP |
|---|---|---|
| 5.1 OpenClaw cron: weekly pilot sweep (Sat 04:00 ET) | Infra | 0.5 |
| 5.2 OpenClaw cron: monthly full sweep (1st Sun 02:00 ET) | Infra | 1.0 |
| 5.3 OpenClaw cron: nightly health check (23:30 ET) | Infra | 0.5 |
| 5.4 OpenClaw cron: weekly regime precompute (Fri 23:00 ET) | Infra | 0.5 |
| 5.5 Per-sweep dashboard generator (`docs/factory/dashboards/`) | Tsubaki | 1.0 |
| 5.6 SRF weekly sweep stub upgrade: route to factory driver per `srf-pipeline-decision.md` §5 | Application | 1.5 |
| 5.7 Documentation: `docs/runbooks/strategy-factory-runbook.md` | Tsubaki | 1.0 |

**Total Phase 5: 6.0 SP / ~5 calendar days**

**Dependencies:** Phase 4 live.

**Acceptance criteria:**
- [ ] Weekly pilot sweep fires automatically and writes a dashboard report
- [ ] Monthly full sweep fires and completes within 16 h budget
- [ ] Nightly health check flags any deploy_pool drift in deploy_pool state
- [ ] Regime precompute regenerates cache with 100-bar rolling windows weekly
- [ ] SRF weekly sweep stub is replaced with real factory routing
- [ ] Runbook covers: starting a sweep, reading dashboards, manual rebalance, kill switch procedure

**Exit gate:** Factory is continuous-operation. Ava owns dashboard review; Tsubaki/Infra handles fixes.

---

### Phase Summary

| Phase | Scope | SP | Calendar | Dependencies |
|---|---|---|---|---|
| 1 | Pipeline assembly | 5.0 | ~5d | None |
| 2 | Template library | 7.5 | ~7d | Phase 1 |
| 3 | Sweep & validate | 6.0 | ~6d | Phase 2 |
| 4 | Live deployment | 6.0 | ~6d | Phase 3 |
| 5 | Continuous operation | 6.0 | ~5d | Phase 4 |
| **Total** | | **30.5** | **~29d (4-5 weeks)** | sequential |

**Note on SP vs lane rules:** Tsubaki is capped at 3.0 SP per card. The phases above must be decomposed into ≤3.0 SP workboard cards for Tsubaki execution. Other lanes (Strategy, Infra, Application) have their own SP caps and ownership rules.

---

## 8. Risk Assessment

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| **1** | **Overfitting (Optuna noise fitting)** — Optuna finds params that exploit statistical noise in the validation window rather than real edge | **HIGH** — this is the dominant failure mode for any sweep-based approach | **HIGH** — false-positive Tier A/B promotions, wasted capital, false confidence | (a) Embargo_bars enforced per §4.1; (b) PBO < 0.30 required per §4.6; (c) DSR with scaled n_trials per §4.2; (d) confirmation re-run with different seed per Phase 3.5; (e) ATR lookback stability test (50/100/200) per Liora ground rule #6 |
| **2** | **Regime sensitivity** — strategies work in one regime only; sweep happens to land in a regime-favorable window | **MEDIUM-HIGH** — Bug #4 fix demonstrates how badly regime mislabeling distorts results | **HIGH** — strategy deployed, decays when regime shifts | (a) Per-regime PF required per template (`regime_affinity`); (b) regime filter applied in factory pipeline per §4.3; (c) per-regime decay monitoring per §5.2; (d) auto-replacement when regime shifts break edge |
| **3** | **Data snooping / look-ahead bias** — bar data, indicator calc, or regime labels accidentally use future info | **MEDIUM** — well-known trap in backtesting | **CRITICAL** — every result is invalid | (a) Embargo gap between train/val/test (§4.1); (b) OOS isolation (§4.5); (c) production validator uses `multi_strategy_engine` which is embargo-aware; (d) walk_forward_runner refuses negative embargo_bars; (e) sanity test: shuffle bar order, results should collapse |
| **3a** | **Survivorship bias** — strategies that "survived" prior failures are overweighted in templates | **MEDIUM** | **MEDIUM** — narrow exploration | (a) Build new templates (trend_following, session_based) that don't wrap existing strategies; (b) Optuna can sweep within template param space but template choice is human-curated; (c) explicitly track in registry which strategies came from where |
| **4** | **Capacity / correlation limits** — too many correlated strategies → no diversification benefit | **MEDIUM** — natural failure mode when sweeping similar templates | **MEDIUM** — false sense of diversification | (a) Per Phase 2: cap at 8 strategies in active blend; (b) compute pairwise equity correlation in `portfolio_blend.compute_correlation_matrix`; (c) drop strategies with >0.7 correlation; (d) weight allocation uses inverse-variance / equal-risk methods (existing in portfolio_blend); (e) Tier A weight capped at 0.50 per §5.1 |
| **5** | **Compute budget overrun** — full sweep exceeds 16h monthly budget | **MEDIUM** — full sweep is ~10-14h with optimal parallelism; spikes under contention | **MEDIUM** — sweep incomplete, partial results, confusion about state | (a) 600s per-cell hard timeout (§6.2); (b) cap process count at 8 (§6.2); (c) nice + cpulimit (§6.2); (d) per Mika ground rule, kill items that don't produce measurable progress in budget; (e) checkpoint to DuckDB after each cell so partial sweeps are recoverable |
| **6** | **DSR stringency miscalibration** — too strict = no survivors (kills factory); too loose = false positives (kills confidence) | **HIGH** — calibration is empirical, not theoretical | **HIGH** — either no candidates (factory "fails") or bad candidates deployed | (a) Three-tier system (A/B/C) per §4.2 gives gradient, not binary; (b) `_MIN_TRADES_FLOOR=30` prevents p-value collapse to ~0; (c) regime filter + PBO are second/third gates, so DSR is one of three; (d) if 0/48 cells produce Tier A/B after pilot, pause factory per kill criteria §5.5; (e) Plan B already documents fallback options (expand markets, different timeframes, single strategy, pause & research) per `plan-b-statistical-failure.md` §5 |
| **7** | **Bug #4 regression** — regime detector warmup regression breaks pipeline silently | **LOW** — fix is in tree | **CRITICAL** — same silent-class failure as Jul 22 (all QUIET/VOLATILE bars misclassified) | (a) Regime precompute is cron-managed (§6.4); (b) cache invalidation on config change; (c) weekly health check counts regime label distribution; (d) alert if any regime label is 0% over a 30-day window |
| **8** | **Council ground rule violation under pressure** — late-stage "just ship it" override of OOS isolation, regime freeze, or PBO requirement | **MEDIUM** — historical pattern in pre-factory sprints | **HIGH** — invalidates all downstream results | (a) All three are hard-coded in factory driver (not config-flag-togglable); (b) OOS unlock requires explicit `--unlock-oos` flag + warning log; (c) any override writes a `ground_rule_override` row to `factory_runs`; (d) Ava reviews overrides weekly |
| **9** | **Forward test contamination** — running blend touched during new config validation (violates Rei ground rule #12) | **MEDIUM** | **HIGH** — invalidates the running baseline | (a) Forward test isolation enforced in `replace.py`; (b) new candidates must pass ALL gates in quarantine before touching running blend; (c) integration tests verify isolation |
| **10** | **CPU contention with OpenClaw gateway / forward test** — factory sweep starves other processes | **MEDIUM** | **MEDIUM** — forward test stalls, latency spikes | (a) `nice -n 19` + `cpulimit -l 20` per Craig ground rule; (b) DuckDB threads=1 + memory_limit=512MB; (c) process count cap; (d) factory sweeps run on weekends (low forward-test traffic) |

---

## Appendix A — Open Questions / Items Not Fully Resolved

1. **The `docs/sprints/strategy-factory-research-notes.md` file referenced in the task brief does not exist on disk.** This spec was synthesized from the four canonical docs + source code + the Jul 22 / Jul 24 / Jul 31 reports. If the missing file is recovered, reconcile numbers.
2. **Dukascopy crisis-period data** (master roadmap §0.5) is required for stress testing but not explicitly scheduled. Should it be a pre-Phase 1 prerequisite?
3. **USDJPY harvest** (pipeline doc #7) is blocked on tick harvest. Should factory skip USDJPY until data lands, or include it conditionally?
4. **Strategy-blend-selection rationale document** (master roadmap §1D.6) is required for FTMO challenge. Where does it fit — Phase 4 acceptance or Phase 5 deliverable?
5. **ML confidence learner frozen during opt** (Kaito ground rule #3) — what triggers a learner rebuild? Spec assumes "on final locked blend" but the trigger condition needs a per-cell decision rule.

---

## Appendix B — File / Module Map (Where to Start Coding)

| New file | Purpose | Phase |
|---|---|---|
| `src/forex_bot/factory/__init__.py` | Package init | 1 |
| `src/forex_bot/factory/template.py` | `StrategyTemplate` abstract base + `ParamSpec` | 2 |
| `src/forex_bot/factory/registry.py` | `FactoryRegistry` for templates | 2 |
| `src/forex_bot/factory/run_factory.py` | Main factory orchestrator CLI | 1 |
| `src/forex_bot/factory/decay_detector.py` | Live decay monitor | 4 |
| `src/forex_bot/factory/replace.py` | Auto-replacement protocol | 4 |
| `src/forex_bot/factory/dsr_pipeline.py` | DSR tier rank wrapper around existing `dsr_integration` | 1 |
| `src/forex_bot/factory/oos.py` | OOS holdout enforcement | 1 |
| `src/forex_bot/factory/regime.py` | Regime filter wrapper (post Bug #4 fix) | 1 |
| `src/forex_bot/factory/templates/momentum.py` | Momentum template | 2 |
| `src/forex_bot/factory/templates/mean_reversion.py` | Mean reversion template | 2 |
| `src/forex_bot/factory/templates/breakout.py` | Breakout template | 2 |
| `src/forex_bot/factory/templates/trend_following.py` | Trend following template | 2 |
| `src/forex_bot/factory/templates/session_based.py` | Session-based template | 2 |
| `src/forex_bot/factory/schema.sql` | DuckDB tables: factory_runs, deploy_pool, decay_log | 1 |
| `src/forex_bot/factory/storage.py` | DuckDB read/write helpers | 1 |
| `tests/factory/test_templates.py` | Template instantiation tests | 2 |
| `tests/factory/test_oos.py` | OOS isolation tests | 1 |
| `tests/factory/test_dsr_pipeline.py` | Tier rank edge cases | 1 |
| `tests/factory/test_decay_detector.py` | Decay trigger tests | 4 |
| `tests/factory/test_replace.py` | Replacement flow tests | 4 |
| `scripts/run_factory_sweep.sh` | Convenience wrapper for cron | 5 |
| `docs/factory/reports/` | Per-sweep markdown reports (auto-generated) | 3 |
| `docs/factory/dashboards/` | Aggregated weekly dashboards | 5 |
| `docs/runbooks/strategy-factory-runbook.md` | Operator runbook | 5 |

---

*End of spec. Total length: 803 lines. Ready for Ava review and Phase 1 decomposition into workboard cards.*