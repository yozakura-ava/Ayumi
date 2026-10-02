# ICIR (Information Coefficient Information Ratio) — Research Note

**Author:** Ava (research subagent)
**Date:** 2026-07-08
**Verdict (TL;DR):** **Partially applicable to Ayumi, with one important adaptation.** ICIR was designed for *cross-sectional* continuous-alpha forecasts over a broad asset universe, not single-instrument binary entry signals. But Ayumi's strategies already emit a continuous `confidence` score alongside each entry signal, and walk-forward splits naturally accommodate per-window IC series. The right adaptation is **trade-level cross-sectional IC** (a strategy's confidence on trade N vs realized R-multiple for trade N, evaluated across strategies/symbols/time) — not the textbook bar-level cross-sectional IC. As a *secondary* metric on top of PF/WR/Sharpe/MaxDD, ICIR is genuinely informative (especially for consistency-of-skill across regime windows), but it is **not a replacement** for the existing realized-PnL metrics and the implementation effort is non-trivial. Below: definitions, formulas, thresholds, implementation, and an honest applicability assessment.

---

## 1. Definitions

### 1.1 IC (Information Coefficient)
The IC is the **cross-sectional correlation between a signal's forecast and the realized return**, measured across assets at a single point in time, then repeated through time.

- **Pearson IC** — linear correlation of raw forecast scores vs raw forward returns.
- **Rank IC (Spearman)** — correlation of ranks. Robust to outliers, captures monotonic non-linearity. **Headline measure at most equity desks.** (MicroAlphas, [Information Coefficient guide](https://microalphas.com/information-coefficient/))

IC lives on [-1, +1]. A stable IC around 0.02–0.07 in a broad liquid equity universe is genuinely valuable; numbers above ~0.10 on a large universe usually indicate look-ahead or factor leakage. (MicroAlphas)

### 1.2 IR (Information Ratio) — the overloaded term
There are **two distinct uses** of "Information Ratio" in the literature and you have to know which one someone means:

1. **Portfolio-level IR (Grinold-Kahn):** the strategy's *active* return (alpha) per unit of *active* risk (tracking error), annualized. This is the classic Sharpe-style metric and is the IR you see in performance attribution reports.
2. **IC Information Ratio (ICIR):** the *consistency of the IC time series itself* — i.e., the Sharpe-like ratio of the period-by-period IC vector:
   ```
   ICIR = mean(IC_t) / std(IC_t)
   ```

These are **related but not the same**. The Fundamental Law of Active Management (Grinold & Kahn) connects them:

```
IR_portfolio  =  IC  ×  √BR  ×  TC
                └─┬─┘   └┬┘   └┬┘
                 skill  breadth  transfer-coefficient
                                          (realism discount)
```

Where BR is the number of *independent* bets per year and TC captures how much of the unconstrained ideal the live book actually expresses (long-only, turnover, risk limits, costs). (MicroAlphas; Grinold & Kahn, *Active Portfolio Management*, 1999/2008.)

### 1.3 ICIR (the focus of this doc)
The information-coefficient information ratio is the **time-series persistence of skill**:

```
ICIR_period  = mean(IC_t) / std(IC_t)
ICIR_annual  = ICIR_period × √(N_periods_per_year)
```

It's effectively the **Sharpe ratio of the IC vector**. A small but stable mean IC with low std is more valuable than a higher mean IC that swings wildly between windows — because consistency is what survives risk limits and drawdown controls.

---

## 2. Formulas (exact)

### 2.1 Cross-sectional IC at one date t

Given N assets at time t, with forecasts `f_{i,t}` and realized forward returns `r_{i,t→t+1}`:

- **Pearson IC:**
  ```
  IC_t = Σ((f_i − f̄)(r_i − r̄)) / (√(Σ(f_i − f̄)²) · √(Σ(r_i − r̄)²))
  ```
- **Rank (Spearman) IC:**
  ```
  rank_f  = rank of f_i at t        (1 = lowest forecast)
  rank_r  = rank of r_i at t→t+1    (1 = lowest return)
  IC_t    = Pearson(rank_f, rank_r)
  ```
  For small N, Spearman simplifies to:
  ```
  IC_t = 1 − (6 · Σ d_i²) / (n · (n² − 1))
  ```
  where `d_i = rank_f_i − rank_r_i`.

### 2.2 ICIR

After computing a series `{IC_t}` for t = 1..T:

```
mean_IC  = (1/T) · Σ IC_t
std_IC   = √((1/(T−1)) · Σ (IC_t − mean_IC)²)
ICIR     = mean_IC / std_IC
ICIR_ann = ICIR · √(periods_per_year)
```

### 2.3 Statistical significance (often reported alongside)
A t-statistic for whether the mean IC is non-zero:
```
t = mean_IC / (std_IC / √T)
```
Two-tailed p-value from `t_{T−1}`. Many desks reject signals at |t| < 2; some require |t| > 3 for production.

### 2.4 Inputs required
- **Cross-sectional panel** — at each evaluation time, you need a vector of forecasts and a vector of *matched* realized outcomes.
- **Matching convention** — forecast made using only information available at t; outcome observed over horizon t→t+1, where horizon is the natural holding period of the strategy.
- **Universe** — must be the one you could actually have traded, including delisted/bankrupt names to avoid survivorship bias. (MicroAlphas)

---

## 3. Interpretation: what is "good"?

### 3.1 Typical thresholds
From the practical quant literature and MicroAlphas:

| Metric | "OK" | "Good" | "Outstanding" | Notes |
|---|---|---|---|---|
| Mean Pearson IC (large equity universe) | 0.02 | 0.05 | 0.08+ | Anything >0.10 on a big universe → suspect look-ahead |
| Mean Rank IC | similar | similar | similar | Rank is usually a bit noisier; preferred for reporting |
| ICIR (per-period) | 0.5 | 1.0 | 2.0+ | Think Sharpe scale |
| ICIR (annualized) | 3.0 | 6.0 | 12.0+ | At daily rebalance (√252 ≈ 15.9) |
| IC t-stat | 2.0 | 3.0 | 4.0+ | Higher t-stats survive out-of-sample better |

These are rough orders of magnitude from equity long-short desks. Single-instrument intraday FX systems will look different — see §7.

### 3.2 Why "small" IC is OK
A single-name bet doesn't need high correlation — it needs a *positive expectation*. IC scales with the **square root of independent breadth**, not the size of any single bet. A small but repeatable edge across thousands of independent trades is exactly the regime where ICIR shines. (Grinold-Kahn's Fundamental Law; MicroAlphas)

### 3.3 Practical reading rules
- **Read mean IC and ICIR together.** A high ICIR with low mean IC ("small but steady") is often more valuable in production than the opposite.
- **Trust IC at the IC horizon.** If you compute IC over a 1-bar forward return but your trades hold for 50 bars, the number is overstated (factors that decay fast dominate) or understated (the holding-period return smooths signal noise) depending on context. The right horizon is the natural holding period of the strategy.
- **Be suspicious of high ICIR from low T.** ICIR with 5 observations and 0.5 std is not statistically meaningful. Hartzeit/Ye rule of thumb: T ≥ 30 minimum, T ≥ 100 preferred for a credible number.
- **Negative IC is also a signal.** Flip the sign and you have a working strategy.

---

## 4. Application in strategy development

### 4.1 Signal quality assessment
- **Headline diagnostic** for whether the forecast has *predictive* content before you spend time on portfolio construction.
- **Compare signals head-to-head** on a scale-free yardstick. Two signals with mean IC 0.04 and 0.05 look like the same on PF/Sharpe, but their ICIR of 1.5 vs 0.4 tells you one survives regime shifts and the other doesn't.
- **Monitor decay.** A rolling-window IC that trends toward zero is the first signal that the edge is being arbitraged away — long before PnL shows it.
- **Detect factor contamination.** If signal IC drops sharply when you neutralize against a known factor, the original IC was partly that factor.

### 4.2 Strategy evaluation and selection
- Complements realized metrics by separating **skill** from **luck** (and from execution).
- PF/WR/Sharpe on a small sample of trades are noisy. ICIR over many evaluation periods is a less noisy proxy for whether the strategy's *edge* is stable.
- Useful when picking between strategies that have similar PnL but different mechanics — does Strategy A's edge persist across regimes, or does it just happen to look great on a hot streak?

### 4.3 Comparing strategies across instruments/timeframes
This is where ICIR would shine on a multi-pair, multi-TF system like Ayumi — IF the implementation is cross-sectional across instruments. Without that, ICIR is strategy-specific, which limits comparison. (See §7.3 for how to do this honestly.)

### 4.4 Portfolio construction
The Fundamental Law gives a *capacity & sizing* bound:
- Given measured IC and an assumed independent-breadth, expected IR = IC · √BR · TC.
- Comparing the realized IR of the book against this bound tells you whether your portfolio construction is leaving money on the table (low TC).
- For Ayumi blend strategies: a single pip-target/book of strategies has limited cross-sectional breadth; IR will be IR-individual-strategy, not IR-portfolio-in-the-academic-sense.

---

## 5. Walk-forward context

### 5.1 Natural fit
Walk-forward analysis (WFA) is structured as T rolling windows — exactly the structure needed to compute a time series of IC values. Each window contributes one IC observation; the ICIR is then `mean(IC_w) / std(IC_w)`. (WFO mechanics: [QuantInsti](https://blog.quantinsti.com/walk-forward-optimization-introduction/), [IBKR/PyQuant](https://www.interactivebrokers.com/campus/ibkr-quant-news/the-future-of-backtesting-a-deep-dive-into-walk-forward-analysis/).)

### 5.2 Per-window computation recipe
For WFA with K optimization/test window pairs:
1. For each test window w_k, collect all `(forecast, realized)` pairs emitted by the strategy during window w_k.
2. Compute IC_w_k (cross-sectional) over that sample.
3. Compute IC_w_k for your headline number; report ICIR over the K windows.
4. Plot IC_w_k over time → look for collapse (skill decay) or trend (regime shift).

### 5.3 How ICIR complements PF/WR/Sharpe/MaxDD
| Dimension | PF / WR | Sharpe | MaxDD | ICIR |
|---|---|---|---|---|
| What it measures | Realized payoff | Risk-adj return | Downside risk | Skill persistence |
| Sample size needed | Trades | Daily rets | Daily rets | Windows/snapshots |
| Sensitive to luck | Very (small N) | Moderate | Low | **Low** (frame-of-reference) |
| Sensitive to regime change | Low | Moderate | Low | **High** (intended) |
| Distinguishes skill from execution | No | No | No | Yes (it's about forecast quality) |
| Adapts to parameter changes | Yes | Yes | Yes | Yes (per-window) |

ICIR gives you **a different axis** — it answers "is this strategy's *edge* consistent across windows?" which PF/WR/Sharpe answer only indirectly via realized outcomes.

### 5.4 Per-window IC + structural-break tests
Combine per-window IC with a CUSUM or Chow test on the IC time series to *automatically* detect when a strategy's edge decayed or shifted — the standard alpha-decay monitoring pattern. (MicroAlphas "signal decay patterns" section.)

### 5.5 Caveats for walk-forward IC
- With only 5 windows (your current WFA setting) and tens of trades per window, IC per window will be noisy; ICIR will be a 5-sample Sharpe and almost meaningless. **You'd need finer-grained windows or repeated evaluation periods within windows** (e.g., weekly IC snapshots) to get a stable ICIR. See §7.4.
- WFA by construction optimizes parameters on the training slice — **out-of-sample IC per window is contaminated** by the fact that the parameters were chosen to fit the training slice. Genuine OOS IC measurement requires parameters frozen *across multiple test windows*, or train/test segments the strategy never saw.

---

## 6. Python implementation (sketch)

### 6.1 Cross-sectional IC per period (canonical, equity-style)

```python
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

# panel: columns = ['date', 'asset', 'signal', 'fwd_ret']
# 'signal' = the forecast score at date t (continuous)
# 'fwd_ret' = realized return from t -> t+1 (continuous)

def cross_sectional_ic(panel: pd.DataFrame, method: str = "spearman") -> pd.Series:
    """One IC value per date."""
    def _ic(group: pd.DataFrame) -> float:
        if len(group) < 3:
            return np.nan
        if method == "spearman":
            return spearmanr(group["signal"], group["fwd_ret"]).correlation
        return group["signal"].corr(group["fwd_ret"])

    return panel.groupby("date").apply(_ic).dropna()


def icir(ic_series: pd.Series, periods_per_year: int = 252) -> dict:
    """Period and annualized ICIR, plus t-stat for significance."""
    n = len(ic_series)
    mean = ic_series.mean()
    std = ic_series.std(ddof=1)
    period_icir = mean / std if std > 0 else np.nan
    annualized_icir = period_icir * np.sqrt(periods_per_year)
    t_stat = mean / (std / np.sqrt(n)) if std > 0 else np.nan
    return {
        "n_periods": n,
        "mean_ic": mean,
        "std_ic": std,
        "icir_period": period_icir,
        "icir_annualized": annualized_icir,
        "t_stat": t_stat,
    }


# Demo (illustrative, not a claim)
panel = pd.read_csv("forecasts.csv", parse_dates=["date"])
ic = cross_sectional_ic(panel)
print(icir(ic, periods_per_year=252))
```

This is the textbook implementation. For a real equity signal panel of e.g. 3,000 names × 1,000 days, this gives a 1,000-element IC series and a stable ICIR.

### 6.2 ICIR for entry-level binary signals (single instrument)

For a system like Ayumi where each strategy emits a directional entry signal plus a confidence score:

```python
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

def trade_level_icir(
    signals: pd.DataFrame,
    fwd_ret_col: str = "fwd_return",
    conf_col: str = "confidence",
    *,
    periods_per_year: int | None = None,
) -> dict:
    """
    For a stream of entries (one row per trade):
      - Compute per-period (e.g., per-month or per-quarter-bucket) cross-sectional
        IC by treating all *signals* across all strategies/instruments within the
        bucket as the cross-section.
      - Returns mean IC, ICIR, t-stat.
    Equivalent to 6.1 but specialized for sparse entry signals.
    """
    df = signals.copy()
    if "date" not in df.columns:
        df["date"] = pd.to_datetime(df["entry_time"]).dt.to_period("W").start_time
    per_period = (
        df.groupby("date")
          .apply(lambda g: spearmanr(g[conf_col], g[fwd_ret_col]).correlation
                 if len(g) >= 3 else np.nan)
          .dropna()
    )
    if per_period.empty:
        return {"mean_ic": np.nan, "icir_period": np.nan, "n_periods": 0}
    pp = periods_per_year or max(1, int(52 / max(1, (per_period.index.to_series().diff().dt.days.median() or 7) / 7)))
    out = icir(per_period, periods_per_year=pp)
    out["period_granularity"] = "weekly"
    return out
```

### 6.3 Naive (one trade at a time) — appropriate only for the *single-signal* sanity check
```python
def naive_signal_vs_outcome_ic(signals: pd.DataFrame) -> float:
    """
    Pearson IC between the signal's confidence and the forward return-to-target
    across a flat series of trades. NOT cross-sectional — useful for sanity, but
    conflates market regime with skill. Don't use as your headline number.
    """
    return signals["confidence"].corr(signals["fwd_return"])
```
This treats the trade stream as one long sequence and produces one number. It is a proxy for IC but mixes "did the signal time the regime?" with "did the signal time cross-sectionally?" — exactly what MicroAlphas warns against in the cross-sectional/time-series confusion section.

### 6.4 What you actually want for Ayumi (the hybrid)
A row per **strategy × bar** (or per evaluation window) — `forecast = strategy.signal at bar end`, `outcome = realized R-multiple or forward return over next N bars`. Compute IC across strategies at each evaluation timestamp; aggregate ICIR over time. See §7.4.

---

## 7. Relevance to Ayumi (the honest section)

### 7.1 What Ayumi is
Per the registry (`src/forex_bot/strategies/registry.py`) and the run_walk_forward codebase (`src/forex_bot/backtest/walk_forward_runner.py`):

- **10+ blend strategies** generating directional entry signals with TP/SL levels across FX pairs (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, XAUUSD) at M15, H1, H4, D1.
- **Walk-forward validation** with 5 windows after Optuna parameter optimization on training slices.
- **Current metrics**: win_rate, profit_factor, sharpe_ratio, max_drawdown, sample count, total PnL — all aggregated across windows.
- **Signal output**: `{direction: BUY|SELL, confidence: float in [~0.30, ~0.85], TP, SL, …}`. Confirmed in `srmr_plus.py`, `signal_orchestrator.py`, `multi_strategy_engine.py` — every strategy emits a continuous `confidence` along with the binary direction.

### 7.2 The square-peg round-hole concern is mostly wrong
The ICIR literature is built around *cross-sectional* continuous alpha forecasts over a *broad universe* evaluated *every bar*. Ayumi has only **one of those three** — the `confidence` field is continuous, but the cross-section is small (≤10 strategies × 1 symbol × 1 timeframe = 1 obs per cell) and signals are sparse (only at entry).

But that doesn't make it inapplicable. It means **the natural Ayumi IC has to be built from trade entries aggregated over a window**, not at every bar. See §7.4.

### 7.3 What you already have that ICIR could exploit
The codebase shows: strategies already produce a continuous confidence/score; that score is already used for position sizing via `risk_amount = risk_sizer.get_risk_amount(signal.confidence)` (backtest/amalgamation.py, multi_strategy_engine.py). This means **a forecast-outcome relationship is already implicit in the system** — confidence drives size and yet it's never validated against realized outcomes as a *forecast*. That's a real gap.

Concrete settings where ICIR would be informative:

1. **Blend weighting.** Each strategy emits a confidence; the blend weights them. ICIR of `(weighted-blend-forecast, forward-realized-return)` per evaluation period tells you whether the blend is actually adding skill vs. just adding trades.
2. **Regime window comparison.** ICIR per walk-forward window (one IC observation per window, OOS-only) tells you which windows of market regime supported skill vs. which were noise.
3. **Forward-test decay monitoring.** On cTrader live, compute rolling 30-day ICIR of `(signal-confidence-aggregated-per-day, basket-realized-return-aggregated-per-day)`. A falling ICIR is the earliest signal of live decay — way before PnL reaction.
4. **Cross-strategy ranking.** ICIR per strategy on equivalent OOS slices is a principled single-number ranking.

### 7.4 The right adaptation for Ayumi
Construct a panel at the **evaluation-window level**, not at every bar:

| Column | Source |
|---|---|
| `window_id` | walk-forward window k (or rolling 4-week bucket for live) |
| `unit_id` | strategy id (or pair, or pair×strategy) |
| `signal` | mean confidence of signals emitted by `unit_id` during `window_id` (or some weighted aggregation) |
| `outcome` | mean (or median) R-multiple of trades from `unit_id` in `window_id` (or forward return over standardized horizon) |

Then run `cross_sectional_ic(panel)` with `groupby="window_id"`. One IC per window. Compute ICIR across windows.

**Why this works**: the cross-section at each window_id is `(strategy × pair × timeframe)` — that's your universe. Each unit_id has potentially multiple signal/outcome pairs within a window, and you aggregate them. IC measures whether high-confidence units actually realized better outcomes within that window; ICIR measures whether that skill persists across windows.

**Mathematical caveats**:
- Use **rank IC** for the same reasons MicroAlphas gives (outlier trade resistance, monotonic relationship between confidence and outcome).
- **Aggregate within-window carefully** — `mean(confidence)` weighted by number of signals is reasonable; for outcome, consider `median(r-multiple)` to dampen fat-tail winners.
- **N per window matters.** Aim for ≥ 5–10 unit-ids per window for a stable per-window IC.

### 7.5 Will ICIR improve the evaluation? (tradeoff table)

| Pro | Con |
|---|---|
| Complements PF/WR/Sharpe from a different axis (skill consistency) | Only K=5 walk-forward windows → ~5 ICIR observations → not credible |
| Already implicit in current confidence→sizing logic — makes it explicit | Requires a new evaluation pipeline (panel construction, per-window IC) |
| Excellent decay monitor on forward test (independent of PnL) | Sensitive to choice of evaluation window size; needs careful design |
| Ranks blend strategies on a scale-free number | Low N trades per window on Ayumi's per-strategy basis → noisy |
| Helps detect factor exposure / leakage | Implementation effort ~1–2 days of focused work plus test coverage |

**Verdict:** *Nice to have, not a foundation change.* If you can either (a) get more evaluation periods (smaller windows, e.g., 4-week buckets across years of data → 50+ periods) or (b) compute ICIR at the strategy-aggregate level across more cross-sectional units (instrument × strategy × regime), then it adds real information. With just 5 walk-forward windows, ICIR is too noisy to drive decisions.

### 7.6 Recommendation (concrete)
1. **Don't add ICIR to walk-forward output today.** With 5 windows it's noise.
2. **Do add ICIR to a longer-horizon evaluation pipeline**: build a daily/weekly panel of `(instrument × strategy × regime)` forecasts vs outcomes, compute IC time series, ICIR, t-stat. This becomes a *blend monitor*, not a walk-forward metric.
3. **Do add it to live forward test**: rolling 30/60/90-day ICIR of (per-day-basket-confidence, per-day-basket-realized-return) on cTrader. This is the single highest-value use given your existing data flow.
4. **If we ever move to cross-pair/cross-strategy portfolio construction** (vs. flat blend): IC and ICIR become essential inputs (Fundamental Law uses IC directly).
5. **The implementation is real work.** Plan ~1 day for panel construction, ~1 day for IC/IR/ICIR plumbing (you can reuse the sketch in §6), ~1 day for tests + integration with the existing walk-forward runner, plus a day of stat grounding (t-test, decay thresholds). Not a "one evening" change.

---

## 8. Sources

### Primary (textbook)
- **Grinold, R., & Kahn, R.** *Active Portfolio Management: Quantitative Models for Alpha, Alpha Extraction and Risk Management*. McGraw-Hill, 1st ed. 1999; 2nd ed. 2008. — Origin of the Information Coefficient, the Information Ratio (portfolio-level), and the Fundamental Law of Active Management.

### Primary (practitioner/reference)
- **MicroAlphas**, *Information Coefficient (IC): How to Measure Quant Signal Skill.* [microalphas.com/information-coefficient](https://microalphas.com/information-coefficient/) — Worked definitions of Pearson vs Rank IC, ICIR formula, IC decay patterns, Python panel implementation, and explicit thresholds for "what counts as good." (Used heavily in §1, §3, §5.5.)
- **MicroAlphas glossary** entries for *Information Ratio*, *Fundamental Law of Active Management*, *Transfer Coefficient*, *Breadth*. (Cited inline above.)

### Walk-forward (context)
- **QuantInsti blog**, *Walk-Forward Optimization: How It Works, Its Limitations, and Backtesting Implementation.* [blog.quantinsti.com/walk-forward-optimization-introduction](https://blog.quantinsti.com/walk-forward-optimization-introduction/) — Rolling-window structure, in-sample/out-of-sample cycle, documented limitations (window selection bias, regime lag, computational cost).
- **Interactive Brokers / PyQuant News**, *The Future of Backtesting: A Deep Dive into Walk Forward Analysis.* [interactivebrokers.com](https://www.interactivebrokers.com/campus/ibkr-quant-news/the-future-of-backtesting-a-deep-dive-into-walk-forward-analysis/) — Independent confirmation of WFA mechanics; recommends the same metric families (profitability, drawdown, Sharpe, win rate, consistency) used by Ayumi.

### Secondary (cross-check)
- **GitHub project:** *IIcodehub/FactorTest* — Lightweight quant factor backtesting framework with built-in IC/ICIR analysis. Confirms the panel-by-date → cross-sectional-IC → ICIR pattern is widely implemented in practice.
- **O'Reilly**, *Advances in Financial Machine Learning* (López de Prado) — for completeness and the *Deflated Sharpe Ratio* (multiple-testing adjustment to avoid spurious ICs).

### Caveat / honest annotations on sources
- MicroAlphas is a practitioner/Marcos López de Prado–style site that consolidates textbook content. It's a high-quality secondary source but not peer-reviewed.
- I could not pull AlphaArchitect's "IC at the Bargaining Table" article directly (Cloudflare 403); the alternate phrasing in this doc is from MicroAlphas + Grinold-Kahn, which is the canonical version anyway.
- DuckDuckGo's bot detection blocked some searches; the picture assembled from MicroAlphas + Grinold-Kahn + the walk-forward references above is consistent and complete for the question asked.

---

## 9. What we are *not* claiming

- I am **not** claiming ICIR will catch bugs that PF/WR/Sharpe/MaxDD miss on small WFA windows — at K=5 it cannot.
- I am **not** claiming the textbook cross-sectional formula applies as-is to binary entry signals.
- I am **not** claiming ICIR is a drop-in replacement for any existing Ayumi metric. It is a complement.
- I am **not** claiming implementation is trivial — the panel construction and the choice of evaluation granularity are real design decisions.

## 10. Open questions for Craig (if you decide to pursue)

1. **Where will the panel live?** Extend `walk_forward_runner.py`, add a sibling analyzer (`icir_analyzer.py`), or fold into the existing `analysis.py`?
2. **Granularity?** Per-eval-week, per-window, per-strategy, per-strategy×pair? Different aggregates answer different questions.
3. **Forward test timing?** Is the cTrader flow currently dumping per-trade data with the raw confidence score, or only fills? If fills only, the live ICIR monitoring path requires the orchestrator to start emitting confidence-at-time-of-decision.
4. **Risk budget for this work?** Recommended ~3–4 days; smaller if limited to forward-test monitoring only.

---

*End of research note. Citation tags: Wikipedia-style; please ping Ava if you want any claim expanded or want the full Python implementation grafted into `src/forex_bot/backtest/`.*
