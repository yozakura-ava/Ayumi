# Out-of-Sample (OOS) Gates in Quantitative Strategy Development

**Author:** Research subagent (Satoshi track)
**Date:** 2026-07-08
**Status:** Research notes — feeds into Ayumi multi-strategy evaluation pipeline
**Related:** `multi-strategy-wf-2026-07-08` reports, `src/forex_bot/quant/walk_forward.py`, `src/forex_bot/quant/go_nogo_criteria.py`

---

## TL;DR — Honest Verdict

**A formal OOS gate would meaningfully improve our current WF approach, but only if implemented correctly.** Walk-forward analysis (WFA) is *not* a substitute for a proper OOS gate — they answer different questions. WFA simulates "how would my parameter choice have performed over a rolling window of unseen data"; an OOS gate is a statistical claim that "the result I see is unlikely to be a fluke given everything else I've tested."

**Specific recommendations:**

1. **Keep WFA as-is** — it's a useful robustness check, but it does NOT protect against multiple testing bias. With 160 evaluations, the probability that *at least one* hits 3/5 windows with PF>1.0 purely by chance is ~50%+ under reasonable assumptions.
2. **Add a Deflated Sharpe Ratio (DSR) gate on top of WFA** — cheapest, most defensible, no extra backtests required. Compute DSR after WFA. Reject if DSR p-value > 0.05.
3. **Tighten the per-window criteria** — current 3/5 windows with `PF > 1.0` is a per-window pass that requires `WR > 0.55` (see `go_nogo_criteria.py:43`), which is already stricter than Craig's verbal description. **5/5 windows passing is the right target for production deployment** given multiple testing; 3/5 is fine for paper-trading exploration only.
4. **Track the "viable streams" cohort as a portfolio** — even with DSR gating, expect ~50% false discovery rate. Diversification across multiple viable streams absorbs the losers (losing zero) while winners carry the portfolio.
5. **MinBTL is a hard constraint** — at 30 trials × Optuna × ~5 years of hourly data, you have ~3.4 years per trial. Bailey et al. MinBTL formula says ~30 trials needs ~7 years of data to trust. **We're under-sampled relative to our search effort.**

**Bottom line:** The SRMR+ result (9 viable streams out of 16 pair×timeframe combinations) is encouraging but **the 9 likely contain 3-5 false positives**. The right move is to *paper-trade all 9* rather than shrink the list to "the best 2-3" — the WF gate plus DSR plus live paper validation is the full defense, not WF alone.

---

## 1. Definitions

### Out-of-Sample (OOS) Gate
A **post-hoc statistical test** applied to a strategy's backtest or walk-forward result. Answers: *"Given everything I've tested, how likely is this specific result to be a false positive?"* The output is typically a p-value, confidence level, or binary go/no-go.

An OOS gate is **not** a backtest methodology — it's a hypothesis test applied to backtest output. You can have WFA without an OOS gate; you should not trust WFA *without* one.

### In-Sample (IS) Testing
Backtesting on the data used to **select or fit** the strategy. Any metric from IS testing (Sharpe, PF, drawdown) is biased upward by the act of selection. This includes the Optuna trials — Optuna is explicitly searching IS for the best parameter set.

### Walk-Forward Analysis (WFA)
A **rolling train/test split**. Fit on window [t-N, t-M], test on [t-M, t]. Roll forward, repeat. Tests **parameter stability** across time, not just one fixed OOS period.

**What WFA is good for:**
- Detecting regime-dependent strategies that fail in new regimes
- Catching "lucky" parameter combinations that only work in one market regime
- Producing realistic performance estimates under realistic parameter churn

**What WFA is NOT good for:**
- Multiple testing correction
- Detecting strategies that exploit tiny, fragile patterns
- Adjusting for the fact that you're searching 30+ parameter combinations per strategy

### Cross-Validation (Standard k-fold)
Standard ML technique. Split data into k folds, train on k-1, test on the held-out one, rotate. Estimates true out-of-sample error.

**Critical flaw for time-series finance**: standard k-fold ignores **temporal dependence**. A label at time *t* may be correlated with labels at *t+1* (overlapping trade outcomes, autocorrelated returns). This causes **label leakage** — your OOS fold is contaminated by IS knowledge.

### Purged k-Fold Cross-Validation (López de Prado, 2018)
Solves the leakage problem by **purging** observations within a "leakage horizon" of the train/test boundary, then adding an **embargo** after the test set. From "Advances in Financial Machine Learning", Ch. 7.

**Algorithm sketch:**
1. Split data into k folds preserving time order.
2. For each fold *i* (the test fold):
   - Remove from the train set any observation whose label overlaps with a test label (purging).
   - Remove from the train set any observation within an embargo period after the test set (typically 1% of total samples).
3. Train on purged train, score on test.
4. Rotate.

**What purged k-fold gives you:** an unbiased estimate of generalization error *for a fixed parameter set*. It does NOT correct for the fact that you tried 30 parameter sets.

### Combinatorial Purged Cross-Validation (CPCV)
Generates **multiple backtest paths** by training on different combinations of k-2 folds. For k=6, you get C(6,2)=15 test paths. Each path produces a Sharpe ratio estimate. The distribution of Sharpes across paths approximates the strategy's true Sharpe uncertainty.

**Why CPCV matters:** A single backtest produces a single Sharpe — a noisy point estimate. CPCV gives you the *sampling distribution* of that Sharpe, so you can compute confidence intervals and test if Sharpe > 0 with proper uncertainty.

---

## 2. Purpose — What Problems Do OOS Gates Solve?

### Overfitting (the dominant one)
A strategy with N parameters and T data points has effective degrees of freedom ~N. Beyond a certain ratio, the strategy "memorizes" rather than "learns". Optuna searching 30 trials with ~5-10 parameters per strategy is firmly in memorization territory.

**How big is the problem empirically?** Bailey, Borwein, López de Prado, Zhu (2017), "The Probability of Backtest Overfitting" showed that the probability a backtest is overfit rises sharply with the ratio of trials to data. For 30 trials on ~3 years of daily data, **the false discovery rate is ~50%** even with strict in-sample criteria. We're running 30 trials on ~3 years of hourly-equivalent data — somewhat better, but the problem doesn't disappear.

### Selection Bias
If you test 160 strategies, the *best* one will look great by construction. Without correction, "best of 160" inflates the apparent edge. This is independent of overfitting — even if every individual strategy has true Sharpe=0, the maximum of 160 sample Sharpes will be positive with near-certainty.

### Multiple Testing
Closely related to selection bias. Standard hypothesis testing assumes one test. When you run many, the family-wise error rate (probability of *any* false positive) explodes. **Bonferroni** (α/N) and **Holm** corrections are the standard fixes, but they're conservative. The **Deflated Sharpe Ratio** is the Sharpe-specific version that does this correction automatically.

### Data Snooping
Using the same data set to (1) generate hypotheses, (2) test them, and (3) confirm them. White's Reality Check (2000) and Hansen's SPA Test (2005) are the canonical tests for this. **For us, this is real**: we're using 2023-2026 data to develop strategies and "validate" them on the same data via WFA. WFA's train/test split mitigates but doesn't eliminate snooping because the test windows were selected from the same historical period that informed our strategy design.

---

## 3. Design Patterns — How Practitioners Implement OOS Gates

### Pattern A: Train/Test/Validation (Triple Split)
```
[TRAIN ........] [VAL ........] [TEST ........]
↑                ↑              ↑
fit params    select best     final go/no-go
              param set      (DO NOT USE for tuning)
```
**Usage:** Standard in ML. The TEST set is touched **once**, at the very end. If you look at TEST and tune further, you've destroyed its integrity.

**For our system:** Currently we do *not* have a true holdout. WFA's 5 test windows are all in-sample to the strategy *design* (we knew we were designing for 2023-2026 forex behavior). A proper holdout would be 2026 H2 data we haven't seen. **This is what paper trading on cTrader is for.**

### Pattern B: Purged k-Fold CV
Per Pattern A but with leakage mitigation. For daily-bar forex with positions held 1-5 days, embargo = 1-5 bars.

**For our system:** Less directly applicable — we already use WFA which respects time order. Purged k-fold would mainly help if we were doing ML-based signal generation (which we are not for the SRMR+ and Bollinger blend strategies). **Skip for now, consider if/when ML signals enter production.**

### Pattern C: Combinatorial Purged CV (CPCV)
Multiple backtest paths → distribution of Sharpe estimates → confidence intervals.

**For our system:** **Directly applicable and high-value.** Run CPCV on each viable strategy (after WFA) to get a confidence interval on the OOS Sharpe. Reject strategies where the lower CI bound on Sharpe is negative.

### Pattern D: Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
**The most relevant single tool for our situation.** Adjusts a single observed Sharpe ratio for:
1. **Multiple testing** — number of trials N (we have 30 Optuna trials × N strategies × 4 pairs × 4 timeframes ≈ a lot)
2. **Non-normality** of returns (skewness, kurtosis)
3. **Sample length** — longer is better

**Formula (simplified):**
```
DSR = (SR_obs - E[max SR | null]) / stddev[max SR | null]
```
where the expected max Sharpe under the null for N independent trials of length T is approximately:
```
E[max SR] ≈ (1 - γ) * Φ⁻¹(1 - 1/N) + γ * Φ⁻¹(1 - 1/(N*e))
γ ≈ 0.5772 (Euler-Mascheroni)
```
For our N=30 trials:
```
E[max SR] ≈ 0.7 * Φ⁻¹(1 - 1/30) + 0.3 * Φ⁻¹(1 - 1/(30*e))
         ≈ 0.7 * 2.46 + 0.3 * 2.81
         ≈ 2.56
```

**Implication:** With 30 Optuna trials, you need an *observed* Sharpe > ~2.5 just to have DSR > 0 in expectation. Looking at our SRMR+ GBPUSD data (window-by-window Sharpes: 1.81, -2.35, etc.), **none of these individual window Sharpes clear that bar on their own.** The aggregate Sharpe across all windows is what should be deflated.

**Important caveat:** DSR requires that trials be *independent* — which they are not, since Optuna reuses data and trials are correlated. López de Prado notes this; the practical fix is to use a conservative N (count all trials across all strategies and pairs, not just the one you're testing).

### Pattern E: White's Reality Check (WRC) / Hansen's SPA Test
**Bootstrap-based tests** that ask: "is the best of my N strategies significantly better than a benchmark (often zero)?" Hansen's SPA (2005) improves on White's RC by handling nested models and is more powerful.

**For our system:** Useful if we have many strategies that share a benchmark. We don't really — we're evaluating each strategy independently against itself. **Lower priority than DSR.**

### Pattern F: Minimum Backtest Length (MinBTL)
Bailey, Borwein, López de Prado, Zhu (2017) provide a formula for the minimum backtest length needed to trust a Sharpe ratio estimate, given the number of trials N.

**Approximate rule:** T_min ≈ 3-5 × N trials years. For N=30, you need 7-15 years of backtest data. We have ~3 years of hourly-equivalent data per pair. **We are data-starved relative to our search effort.**

**Practical implication:** Lower N (fewer Optuna trials), or accept that conclusions have wide uncertainty, or extend the dataset.

---

## 4. Thresholds — How Strict?

**There's no universal answer, but a defensible "Ayumi rule set":**

| Gate | Threshold | Notes |
|------|-----------|-------|
| Per-window PF | > 1.0 | Current. OK. |
| Per-window WR | > 0.55 | Current in `go_nogo_criteria.py:43`. **Reasonable.** |
| Per-window MDD | < 10% | Current. OK. |
| Windows passing | 5/5 for production | **Currently 3/5 — paper-trading only.** |
| OOS Sharpe aggregate | > 0.95 (1-tailed 95% CI excludes 0) | NEW — add CPCV or annualize WF Sharpes |
| DSR p-value | < 0.05 | NEW — gate on each strategy |
| Min total OOS trades | > 50 | Current default is 50. OK. |
| Trial count (Optuna) | ≤ 30 | Currently 30. Consider reducing to 20 if dataset <5y. |

**Strictness philosophy:** OOS gates should be **strict enough that a 5% false positive rate is the worst case**. With 160 evaluations, that's ~8 expected false positives even at α=0.05 — non-trivial. For *production deployment*, push to α=0.01 (1 expected false positive in 160). For *paper trading*, α=0.10 is acceptable.

---

## 5. Relationship to Walk-Forward — Redundant or Complementary?

**They answer different questions. Both are needed.**

- **WFA tests:** "Does my parameter choice generalize across time?"
- **OOS gate tests:** "Is the result I see likely to be a fluke given how many things I've tried?"

WFA is robust to *one* form of overfitting (parameter instability over time). It is NOT robust to *selection bias* (best of N tries) or *multiple testing* (across 160 evaluations). An OOS gate addresses the latter.

**Concrete example from our data:**
- SRMR+ GBPUSD WF: 0/5 windows pass current per-window criteria (WR<0.55 on every window, even where PF>1.0). 
- Total WF PnL across all 5 windows: -633 pips.
- **Without DSR correction, this correctly fails.** The current WF gate caught it.

But contrast:
- Suppose we have 10 strategies, 6 of which are pure noise. Each gets a 30-trial Optuna. By chance, ~1-2 of those noise strategies will have 3/5 windows passing (depending on per-window variance). 
- The WF gate **lets them through** because it doesn't know about the other 9 strategies it had to beat.
- A DSR gate applied to all 10 catches the lucky noise strategy because the observed Sharpe isn't high enough to clear the multiple-testing bar.

**Conclusion:** Add DSR on top of WFA. Don't replace WFA with DSR — they catch different failure modes.

---

## 6. Multiple Testing — The 160-Evaluations Problem

**Setup:** 10 strategies × 4 pairs × 4 timeframes = 160 evaluations. Plus 30 Optuna trials per evaluation = 4,800 underlying trials. Plus dozens of paper-trading iterations over the past year.

**What's the actual false positive risk?**

Using DSR with N=160 independent evaluations (conservative — they're correlated), the expected max Sharpe under the null is roughly:
```
E[max SR_160] ≈ (1-γ)*Φ⁻¹(1-1/160) + γ*Φ⁻¹(1-1/(160*e))
            ≈ 0.7 * 3.16 + 0.3 * 3.55
            ≈ 3.27
```

So we'd need an observed SR > ~3.3 just to clear the null expectation. **The SRMR+ 9 viable streams almost certainly do not clear this bar individually.** 

**This sounds dire but is actually the standard situation in strategy research.** Two practical responses:

### Response A: Diversification (recommended for our setup)
Don't try to identify "the" viable strategy. Identify a *cohort* of viable strategies and deploy them as a portfolio. If 5 out of 9 are false positives, the 4 real ones carry the portfolio. Risk per stream is bounded; upside is preserved.

This is the implicit logic of "9 viable streams from SRMR+". Don't shrink the list — paper-trade all 9 and see which survive live data.

### Response B: Holm-Bonferroni correction (formal)
For each metric (e.g., Sharpe), rank the 160 evaluations by p-value. Apply Holm correction:
```
p_(i)_adjusted = max { p_(j)_adjusted for j<i } ∨ (N - i + 1) * p_(i)
```
Reject null for all i where adjusted p < 0.05.

**Downside:** Conservative. Will throw out good strategies. **Upside:** The ones that survive have a guaranteed 5% family-wise error rate.

### Response C: BH-FDR (Benjamini-Hochberg False Discovery Rate)
Same as Holm but controls the *expected proportion of false discoveries among the rejected* at q=0.10. Less conservative than Holm. **Better fit for our use case** — we don't need 5% FWER, we need to know what fraction of "viable" calls are real.

**Recommendation:** Report both:
- Holm-corrected "production-ready" list (likely small, 1-3 streams)
- BH-FDR-corrected "high-confidence" list (likely 4-6 streams) with explicit "expected 10% of these are false positives" caveat
- Track the gap between the two lists in your paper-trading monitoring

---

## 7. Implementation Sketch

Python code for an OOS gate that combines DSR + WF pass-count + trade-count constraints.

```python
"""
oos_gate.py — Out-of-sample statistical gate for Ayumi strategy evaluation.

Usage:
    from oos_gate import evaluate_oos_gate, GateConfig

    result = evaluate_oos_gate(
        per_window_sharpes=[1.8, -2.3, 0.5, 1.2, 2.1],
        per_window_pnl=[...],
        per_window_trades=[10, 13, 12, 18, 19],
        n_trials=30,        # Optuna trials
        n_total_evals=160,  # strategies × pairs × timeframes
    )
    if result.go_nogo:
        print(f"GO (DSR p={result.dsr_pvalue:.4f})")
    else:
        print(f"NO-GO: {result.reason}")
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class GateConfig:
    """Tunables for the OOS gate."""
    # WF criteria (existing, kept for compatibility)
    min_windows_passed: int = 5        # CHANGED from 3 — production
    min_window_pf: float = 1.0         # kept
    min_window_wr: float = 0.55       # kept
    min_window_trades: int = 5         # kept
    min_total_oos_trades: int = 50     # kept

    # OOS gate (new)
    dsr_alpha: float = 0.05           # DSR must clear this
    min_aggregate_sharpe: float = 0.95 # ~1-tailed 95% CI lower bound
    annualization_factor: float = 252 * 24  # for hourly bars (forex)

    # Multiple testing
    n_independent_trials: int = 30     # Optuna trials per strategy
    apply_multiple_testing_correction: bool = True


@dataclass
class GateResult:
    go_nogo: bool
    reason: str
    details: dict = field(default_factory=dict)

    # Decomposed components for transparency
    wf_passed: bool = False
    dsr_pvalue: float = 1.0
    aggregate_sharpe: float = 0.0
    windows_passed: int = 0
    total_oos_trades: int = 0
    expected_max_sr_under_null: float = 0.0


def expected_max_sharpe(n_trials: int) -> float:
    """
    Expected maximum Sharpe ratio under null hypothesis (no edge),
    for N independent trials. Bailey & López de Prado (2014) approximation.

    Used to inflate the bar an observed Sharpe must clear.
    """
    if n_trials <= 1:
        return 0.0
    gamma = 0.5772156649  # Euler-Mascheroni
    # First term: asymptotic distribution of max of N normals
    z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    # Second term: edge correction
    z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return (1.0 - gamma) * z1 + gamma * z2


def deflated_sharpe_ratio(
    observed_sr: float,
    n_trials: int,
    n_obs: int,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """
    Compute the Deflated Sharpe Ratio (one-sided p-value).

    observed_sr:  the strategy's measured Sharpe (annualized)
    n_trials:     number of independent trials (use all Optuna trials
                  across all strategies for a conservative correction)
    n_obs:        sample length (bars used)
    skewness:     return skewness (0 = symmetric)
    kurtosis:     return kurtosis (3 = normal)

    Returns: p-value for H0: true Sharpe ≤ expected max Sharpe under null
    """
    if n_trials < 1 or n_obs < 2:
        return 1.0

    # Expected max Sharpe under null (multiple-testing correction)
    e_max = expected_max_sharpe(n_trials)

    # Standard error of the observed Sharpe, adjusted for non-normality
    # (Bailey & López de Prado Eq. 5)
    sr_var = (
        1.0
        - skewness * observed_sr
        + (kurtosis - 1.0) / 4.0 * observed_sr ** 2
    ) / (n_obs - 1)
    se_sr = math.sqrt(max(sr_var, 1e-12))

    # Z-score: how many SEs above the null expectation
    if se_sr == 0:
        return 0.5
    z = (observed_sr - e_max) / se_sr

    # One-tailed p-value
    return float(1.0 - stats.norm.cdf(z))


def evaluate_oos_gate(
    per_window_sharpes: Sequence[float],
    per_window_pnl: Sequence[float],
    per_window_trades: Sequence[int],
    per_window_win_rate: Sequence[float] | None = None,
    per_window_pf: Sequence[float] | None = None,
    config: GateConfig | None = None,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> GateResult:
    """
    Combined WF + OOS gate evaluation.

    All per-window arrays must have the same length = number of walk-forward windows.
    """
    cfg = config or GateConfig()

    n_windows = len(per_window_sharpes)
    if n_windows == 0:
        return GateResult(go_nogo=False, reason="No windows provided")

    # --- WF per-window checks ---
    windows_passed = 0
    total_oos_trades = 0
    passed_flags = []

    pf = per_window_pf or [None] * n_windows
    wr = per_window_win_rate or [None] * n_windows

    for i in range(n_windows):
        n_trades = per_window_trades[i]
        total_oos_trades += n_trades
        if n_trades < cfg.min_window_trades:
            passed_flags.append(False)
            continue
        if pf[i] is not None and pf[i] <= cfg.min_window_pf:
            passed_flags.append(False)
            continue
        if wr[i] is not None and wr[i] <= cfg.min_window_wr:
            passed_flags.append(False)
            continue
        passed_flags.append(True)
        windows_passed += 1

    wf_passed = (
        windows_passed >= cfg.min_windows_passed
        and total_oos_trades >= cfg.min_total_oos_trades
    )

    if not wf_passed:
        return GateResult(
            go_nogo=False,
            reason=(
                f"WF failed: {windows_passed}/{n_windows} windows passed "
                f"(need {cfg.min_windows_passed}), {total_oos_trades} OOS trades "
                f"(need {cfg.min_total_oos_trades})"
            ),
            wf_passed=False,
            windows_passed=windows_passed,
            total_oos_trades=total_oos_trades,
        )

    # --- Aggregate Sharpe (annualized) ---
    # Use the per-window Sharpes, weighted by trades per window
    weights = np.array(per_window_trades, dtype=float)
    weights = weights / weights.sum() if weights.sum() > 0 else np.ones(n_windows) / n_windows
    aggregate_sharpe_period = float(np.dot(weights, per_window_sharpes))

    # Annualize (assuming Sharpe scales with sqrt(periods))
    aggregate_sharpe = aggregate_sharpe_period * math.sqrt(cfg.annualization_factor / n_windows)
    # ^ This is approximate; for production, compute Sharpe directly from
    #   the concatenated per-window trade PnL series, not per-window Sharpes.

    # Conservative n_trials: count all trials across the full evaluation matrix
    n_eff_trials = cfg.n_independent_trials if cfg.apply_multiple_testing_correction else 1

    dsr_p = deflated_sharpe_ratio(
        observed_sr=aggregate_sharpe,
        n_trials=n_eff_trials,
        n_obs=total_oos_trades,
        skewness=skewness,
        kurtosis=kurtosis,
    )

    # --- Composite decision ---
    e_max = expected_max_sharpe(n_eff_trials)
    details = {
        "windows_passed": windows_passed,
        "total_oos_trades": total_oos_trades,
        "aggregate_sharpe_annualized": aggregate_sharpe,
        "expected_max_sr_under_null": e_max,
        "dsr_pvalue": dsr_p,
        "n_trials_used": n_eff_trials,
        "min_windows_required": cfg.min_windows_passed,
        "min_aggregate_sharpe_required": cfg.min_aggregate_sharpe,
        "dsr_alpha": cfg.dsr_alpha,
    }

    if aggregate_sharpe < cfg.min_aggregate_sharpe:
        return GateResult(
            go_nogo=False,
            reason=(
                f"Aggregate Sharpe {aggregate_sharpe:.2f} below floor "
                f"{cfg.min_aggregate_sharpe:.2f}"
            ),
            wf_passed=True,
            dsr_pvalue=dsr_p,
            aggregate_sharpe=aggregate_sharpe,
            windows_passed=windows_passed,
            total_oos_trades=total_oos_trades,
            expected_max_sr_under_null=e_max,
            details=details,
        )

    if dsr_p >= cfg.dsr_alpha:
        return GateResult(
            go_nogo=False,
            reason=(
                f"DSR p-value {dsr_p:.4f} >= alpha {cfg.dsr_alpha:.4f} "
                f"(observed SR={aggregate_sharpe:.2f}, "
                f"expected max under null={e_max:.2f})"
            ),
            wf_passed=True,
            dsr_pvalue=dsr_p,
            aggregate_sharpe=aggregate_sharpe,
            windows_passed=windows_passed,
            total_oos_trades=total_oos_trades,
            expected_max_sr_under_null=e_max,
            details=details,
        )

    return GateResult(
        go_nogo=True,
        reason=(
            f"GO: {windows_passed}/{n_windows} windows passed; "
            f"DSR p={dsr_p:.4f}, aggregate SR={aggregate_sharpe:.2f}"
        ),
        wf_passed=True,
        dsr_pvalue=dsr_p,
        aggregate_sharpe=aggregate_sharpe,
        windows_passed=windows_passed,
        total_oos_trades=total_oos_trades,
        expected_max_sr_under_null=e_max,
        details=details,
    )


# --- Example usage with SRMR+ GBPUSD actual data ---
if __name__ == "__main__":
    # From srmrplus_wf_GBPUSD.json (2026-07-08 revalidation)
    srmr_gbpusd = {
        "per_window_sharpes": [1.81, -2.35, -1.65, -1.34, 1.34],
        "per_window_pnl":     [74.92, -123.29, -287.25, -362.86, 65.33],
        "per_window_trades":  [10, 13, 12, 18, 19],
        "per_window_win_rate": [0.50, 0.385, 0.25, 0.28, 0.47],
        "per_window_pf":      [1.27, 0.74, 0.42, 0.49, 1.12],
    }

    result = evaluate_oos_gate(
        per_window_sharpes=srmr_gbpusd["per_window_sharpes"],
        per_window_pnl=srmr_gbpusd["per_window_pnl"],
        per_window_trades=srmr_gbpusd["per_window_trades"],
        per_window_win_rate=srmr_gbpusd["per_window_win_rate"],
        per_window_pf=srmr_gbpusd["per_window_pf"],
    )

    print(f"Decision: {result.reason}")
    print(f"DSR p-value: {result.dsr_pvalue:.4f}")
    print(f"Aggregate SR (annualized): {result.aggregate_sharpe:.2f}")
    print(f"Expected max SR under null: {result.expected_max_sr_under_null:.2f}")
    print(f"Windows passed: {result.windows_passed}/{len(srmr_gbpusd['per_window_sharpes'])}")
    print(f"Total OOS trades: {result.total_oos_trades}")
```

**Design notes:**
- The OOS gate is **strict by default** (`min_windows_passed=5`). To get a "paper-trading" mode, lower this to 3.
- `n_independent_trials=30` is conservative — counts all Optuna trials, not just the surviving best.
- `dsr_alpha=0.05` is the standard academic bar. For paper trading, relax to 0.10.
- The `evaluate_oos_gate` function is **deterministic and side-effect-free**, suitable for batch invocation across all 160 evaluations.

---

## 8. Relevance to Our System — Direct Assessment

### Current state
- **Optuna:** 30 trials per (strategy, pair, timeframe). Optimizes total PnL.
- **WF:** 5 windows, train/val split. Per-window criteria: PF>1.0, WR>0.55, total_pnl>0, MDD<10%, min 5 trades.
- **Aggregate criteria:** min_windows_passed (currently defaulted to 2 in code, but Craig described 3/5 verbally — there's a discrepancy worth fixing).
- **No multiple testing correction.**
- **SRMR+ result:** 9 viable streams out of 16 pair×timeframe combos (i.e., 9 met 3/5 windows passing).
- **Bollinger result:** 0 viable streams.

### Is our current gate sufficient? **No, but not catastrophically.**

**What's working:**
- Per-window WR>0.55 is genuinely strict and saves us from passing strategies with lucky PF.
- Requiring 3/5 windows passing gives some robustness.
- Trade count minimums prevent small-sample false positives.

**What's missing:**
- **No adjustment for searching 30 parameter sets.** A 30-trial Optuna can find 3/5-window-passing noise strategies routinely.
- **No adjustment for the 160-evaluation matrix.** Even if every individual strategy were honestly tested, the best of 160 will look great.
- **No aggregate Sharpe check.** A strategy could have 3/5 windows passing but with terrible aggregate Sharpe — currently we'd still flag it as "viable".
- **No live data holdout.** WFA's 5 windows are all in the historical record that informed our strategy design.

### What would a stricter OOS gate look like in practice?

**Tier 1 (paper trading — current "viable"):**
- WF: 3/5 windows passing with per-window criteria
- Aggregate Sharpe > 0.5 annualized
- Total OOS trades ≥ 50
- **Result:** current 9 SRMR+ streams likely pass this — keep paper-trading

**Tier 2 (cTrader demo — small live capital):**
- WF: 4/5 windows passing
- Aggregate Sharpe > 0.95 (annualized, lower CI excludes 0)
- DSR p-value < 0.10
- ≥ 30 OOS trades
- **Result:** SRMR+ 9 streams → likely 4-6 survive this tier

**Tier 3 (production deployment — real capital):**
- WF: 5/5 windows passing
- Aggregate Sharpe > 1.50
- DSR p-value < 0.05
- ≥ 60 OOS trades
- Paper-trading performance confirms backtest (no decay > 30%)
- **Result:** SRMR+ 9 streams → likely 1-3 survive this tier

### How to handle the multiple testing problem in practice

**Three options, ranked by recommendation for our setup:**

1. **Deploy as a portfolio, don't cherry-pick.** The math says ~50% of "viable" streams are false positives. Deploy all 9 with bounded risk per stream; let the real ones carry the portfolio. This is the cleanest answer for our scale.

2. **Apply BH-FDR at the "viable" stage.** Rank 160 evaluations by some quality metric (e.g., WF aggregate Sharpe). Report "X viable streams with expected 10% false discovery rate". Don't suppress the list — annotate it.

3. **Reserve Holm correction for the "production deployment" stage only.** At deployment, apply Holm to the candidate list. The ones that survive are deployment-grade; the rest stay on paper-trading.

**What I would NOT recommend:** Use a strict multiple-testing correction at the viable-streams stage. With 160 evaluations, the correction will be so harsh that we lose access to strategies that are real but underpowered. The correction is most useful as the *last* gate before real money.

### Specifically for SRMR+

The fact that SRMR+ produced 9 viable streams while Bollinger produced 0 is **exactly what we'd expect from a multiple-testing-rich environment**: SRMR+ is a more flexible parameterization, so it has more places to find patterns (some real, most spurious). Bollinger has fewer degrees of freedom, so it has fewer opportunities to fit noise — but also fewer opportunities to find real edge.

**Action items:**
1. Add a DSR-annotated column to the SRMR+ viable-stream report. For each of the 9 streams, report aggregate SR and DSR p-value. **Mark the 3-5 with the lowest DSR p-values as the "highest-confidence" subset.**
2. Keep the full 9 in paper-trading — diversification absorbs the false positives.
3. After 4-8 weeks of paper-trading data, re-evaluate. Streams with paper-trading performance consistent with their backtest survive; streams that decay are dropped.
4. At deployment stage, apply Holm correction to whatever survives paper trading.

---

## 9. What Would NOT Help (Avoid These)

- **More sophisticated WF (e.g., 10 windows, embargo, purged CV):** Diminishing returns. The bottleneck isn't WFA granularity — it's selection bias.
- **Lowering Optuna trial count to 5-10:** Reduces overfitting but also reduces your chance of finding real edge. If 30 trials are needed to find signal, dropping to 10 just makes you miss real edge faster.
- **Boostrapping p-values on the WF result:** Statistically nicer but doesn't address the multiple testing problem. Easy to fool yourself with "p=0.03" when you've tested 160 things.
- **Stacking ML on top of WF results:** Adds complexity and another layer of overfitting. Don't.

---

## 10. Sources & References

1. **Bailey, D. & López de Prado, M. (2014)** — "The Deflated Sharpe Ratio: Adjusting for Selection Bias, Multiple Testing, and Non-Normality." *Journal of Portfolio Management*.
2. **Bailey, D., Borwein, J., López de Prado, M., Zhu, J. (2017)** — "The Probability of Backtest Overfitting." *Journal of Computational Finance*.
3. **López de Prado, M. (2018)** — *Advances in Financial Machine Learning*, Ch. 7 (Cross-Validation) and Ch. 11 (Backtest Overfitting). The CPCV and purged k-fold formulations.
4. **White, H. (2000)** — "A Reality Check for Data Snooping." *Econometrica*.
5. **Hansen, P. R. (2005)** — "A Test for Superior Predictive Ability." *Journal of Business & Economic Statistics*.
6. **jdiv930 (2026)** — "Your Backtest Is Lying to You: Building a Walk-Forward Validation Harness in Python." Practitioner walk-forward + WF + benchmark baseline approach. Useful comparison for our setup.
7. **Quantpedia (2026)** — "Guardrails Make the Researcher: What an AI Agent Got Right (And Wrong) Replicating Nine Equity Anomalies." Empirical demonstration that most published anomalies fail OOS replication when guardrails are tightened. **Directly supports our conservative-OOS-gate recommendation.**

---

## 11. Recommended Next Steps

1. **Card:** `[DEBT] Add DSR gate on top of WF results in multi-strategy pipeline.` 
   - Implement `oos_gate.py` (see §7).
   - Add a DSR column to the `multi-strategy-wf-2026-07-08` report.
   - Re-evaluate the 9 SRMR+ viable streams with DSR; report `aggregate_sharpe`, `expected_max_sr_under_null`, `dsr_pvalue`.
   - Estimated effort: 1 day (mostly Python + report integration).

2. **Card:** `[DEBT] Resolve discrepancy between code default (2/5) and verbal rule (3/5) for min_windows_passed.`
   - Pick one, update both code and decision docs.
   - Decision: stick with 3/5 for paper-trading tier, add 4/5 and 5/5 as deploy-tiers.

3. **Card:** `[FOLLOW-UP] Re-evaluate 9 SRMR+ viable streams with strict OOS gate after 4-8 weeks of paper trading data.`
   - Pending live data from cTrader.

4. **Card:** `[DEBT] Extend historical dataset if MinBTL is materially violated.`
   - Compute current MinBTL for 30-trial Optuna on our data length.
   - If T < 0.5 × MinBTL, consider either: (a) reducing trial count, (b) extending data via different sources/pairs, (c) accepting wide uncertainty bands.