"""
oos_gate.py — Out-of-sample statistical gate for Ayumi strategy evaluation.

Implements the Deflated Sharpe Ratio (DSR) gate on top of walk-forward results,
following Bailey & López de Prado (2014), "The Deflated Sharpe Ratio: Adjusting
for Selection Bias, Multiple Testing, and Non-Normality."

This is the production-ready module that fixes the 5 first-order defects
identified by Kaito's council review of the §7 sketch in
``docs/research/oos-gate-research-2026-07-08.md``. See
``/root/.openclaw/council-workspace/data/kaito-reviews/quest-pivot-2026-07-08.md``
§3 for the full review.

The 5 defects fixed (relative to the §7 sketch):

1. ``min_windows_passed`` default → 3 (was 5). Matches the verbal 3-of-5 rule
   and the canonical ``go_nogo_criteria.AggregateCriteria`` default. Without
   this fix, instantiating ``GateConfig()`` silently rejects every stream.
2. ``n_independent_trials`` default → 160 (was 30). Counts ALL evaluations
   across strategies × pairs × timeframes = 10 × 4 × 4 = 160, per the
   conservative multiple-testing correction in research doc §6.
3. Annualization derived from ``bar_period_minutes`` (was hard-coded to
   ``252 * 24 = 6048``). The fix accepts a bar period and computes the correct
   factor: M5→72576, M15→24192, M30→12096, H1→6048, H4→1512, D1→252.
4. ``n_obs`` semantics → use total trade count for per-trade Sharpe
   computation. Sharpe is computed from the concatenated per-trade return
   series (not from a weighted average of per-window Sharpes), so ``n_obs``
   matches the sample the Sharpe was actually computed from.
5. Skewness/kurtosis → compute from actual per-trade returns using
   ``scipy.stats.skew`` and ``scipy.stats.kurtosis`` (excess kurtosis,
   converted to regular by adding 3 for the Bailey formula). Falls back to
   0.0/3.0 (normal distribution) only when ``n_trades < 30``.

Tier definitions follow the research doc §8:

- Tier A (production deployment, real capital): ``min_windows_passed=5``,
  ``min_aggregate_sharpe=1.50``, ``dsr_alpha=0.05``.
- Tier B (cTrader demo, small live capital): ``min_windows_passed=4``,
  ``min_aggregate_sharpe=0.95``, ``dsr_alpha=0.10``.
- Tier C (paper trading): ``min_windows_passed=3``, ``min_aggregate_sharpe=0.50``,
  ``dsr_alpha=0.10``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy import stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Forex calendar: 252 trading days/year, 24-hour sessions (forex is 24/5 but
# the 24h convention is standard in academic Sharpe literature).
TRADING_DAYS_PER_YEAR = 252
HOURS_PER_TRADING_DAY = 24
MINUTES_PER_HOUR = 60
MINUTES_PER_YEAR = TRADING_DAYS_PER_YEAR * HOURS_PER_TRADING_DAY * MINUTES_PER_HOUR  # 362_880

# Euler-Mascheroni constant (used in the expected-max-Sharpe approximation).
EULER_MASCHERONI = 0.5772156649015329


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def annualization_from_bar_period(bar_period_minutes: int) -> float:
    """Compute the annualization factor (bars per year) for a given bar period.

    Defect 3 fix: replaces the hard-coded ``252 * 24`` in the §7 sketch.

    Examples:
        M1  → 362_880
        M5  →  72_576   (252 * 24 * 12)
        M15 →  24_192   (252 * 24 * 4)
        M30 →  12_096   (252 * 24 * 2)
        H1  →   6_048   (252 * 24)
        H4  →   1_512   (252 * 6)
        D1  →     252

    Args:
        bar_period_minutes: bar period in minutes. Must be > 0.

    Returns:
        Number of bars per year (the annualization factor).

    Raises:
        ValueError: if ``bar_period_minutes`` is not a positive integer.
    """
    if not isinstance(bar_period_minutes, (int, np.integer)) or bar_period_minutes <= 0:
        raise ValueError(f"bar_period_minutes must be a positive integer, got {bar_period_minutes!r}")
    return MINUTES_PER_YEAR / int(bar_period_minutes)


def expected_max_sharpe(n_trials: int) -> float:
    """Expected maximum Sharpe ratio under H0 (no edge) for N independent trials.

    Bailey & López de Prado (2014) approximation:

        E[max SR] ≈ (1-γ) * Φ⁻¹(1 - 1/N) + γ * Φ⁻¹(1 - 1/(N·e))

    where ``γ ≈ 0.5772`` is the Euler-Mascheroni constant.

    Args:
        n_trials: number of independent trials. Must be ≥ 1.

    Returns:
        Expected max Sharpe under the null. ``0.0`` when ``n_trials ≤ 1``.
    """
    if n_trials <= 1:
        return 0.0
    z1 = stats.norm.ppf(1.0 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    return (1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio
# ---------------------------------------------------------------------------


def deflated_sharpe_ratio(
    observed_sr: float,
    n_trials: int,
    n_obs: int,
    skewness: float = 0.0,
    kurtosis_regular: float = 3.0,
) -> float:
    """Compute the Deflated Sharpe Ratio one-sided p-value.

    Bailey & López de Prado (2014) Eq. 5.

        SE(SR)² = [1 - γ₃·SR + (γ₄ - 1)·SR²/4] / (n - 1)
        DSR_z   = (SR_observed - E[max SR | null]) / SE(SR)
        p       = 1 - Φ(DSR_z)

    where ``γ₃`` is skewness and ``γ₄`` is the regular kurtosis (4th
    standardized moment, ``γ₄ = 3`` for a normal distribution).

    Args:
        observed_sr: the strategy's measured Sharpe ratio (annualized).
        n_trials: number of independent trials for the multiple-testing
            correction. Use the conservative count (all evaluations across
            strategies × pairs × timeframes), not per-strategy trials.
        n_obs: sample size used to compute ``observed_sr``. Must match the
            sample the Sharpe was actually computed from (Defect 4 fix).
        skewness: γ₃ (sample skewness).
        kurtosis_regular: γ₄ (regular kurtosis; 3 for normal).

    Returns:
        One-sided p-value for H0: true SR ≤ E[max SR | null]. Lower is better.
    """
    if n_trials < 1 or n_obs < 2:
        return 1.0

    e_max = expected_max_sharpe(n_trials)

    # Bailey & López de Prado (2014) Eq. 5 — variance of SR estimator with
    # non-normality adjustment. ``kurtosis_regular`` is γ₄ (3 for normal).
    sr_var = (1.0 - skewness * observed_sr + (kurtosis_regular - 1.0) / 4.0 * observed_sr**2) / (n_obs - 1)
    se_sr = math.sqrt(max(sr_var, 1e-12))

    if se_sr == 0:
        return 0.5

    z = (observed_sr - e_max) / se_sr
    return float(1.0 - stats.norm.cdf(z))


# ---------------------------------------------------------------------------
# Minimum Track Record Length
# ---------------------------------------------------------------------------


def min_track_record_length(
    observed_sr: float,
    n_trials: int = 1,
    skewness: float = 0.0,
    kurtosis_regular: float = 3.0,
    alpha: float = 0.05,
) -> int:
    """Compute the minimum track record length (MinTRL) for the DSR test.

    Bailey & López de Prado (2014), derived from the DSR hypothesis test.
    Returns the minimum number of observations (e.g., trades) needed to
    reject H0: true SR ≤ E[max SR | null] at significance level ``alpha``.

    The formula inverts the DSR z-statistic:

        z_α = (SR - E[max SR]) * sqrt(n - 1) / sqrt(V)

    where ``V = 1 - γ₃·SR + (γ₄ - 1)/4 · SR²`` is the non-normality-adjusted
    variance of the SR estimator (Bailey & López de Prado 2014, Eq. 5).
    Solving for ``n``:

        MinTRL = ceil( z_α² · V / (SR - E[max SR])² ) + 1

    The ``+1`` accounts for the ``(n - 1)`` denominator in the SE formula.

    Args:
        observed_sr: the strategy's measured Sharpe ratio (annualized).
        n_trials: number of independent trials for multiple-testing
            correction. ``1`` means no correction (standard SR test).
        skewness: γ₃ (sample skewness; 0.0 for normal).
        kurtosis_regular: γ₄ (regular kurtosis; 3.0 for normal).
        alpha: one-sided significance level (default 0.05).

    Returns:
        Minimum number of observations needed. Returns ``-1`` when the
        observed SR does not exceed the expected max under the null
        (i.e., the strategy cannot be validated at any sample size).
    """
    if alpha <= 0 or alpha >= 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    if observed_sr <= 0:
        return -1

    e_max = expected_max_sharpe(n_trials) if n_trials > 1 else 0.0
    sr_excess = observed_sr - e_max
    if sr_excess <= 0:
        return -1

    z_alpha = stats.norm.ppf(1.0 - alpha)

    # Non-normality-adjusted variance term (same as DSR's SE² numerator).
    variance_term = 1.0 - skewness * observed_sr + (kurtosis_regular - 1.0) / 4.0 * observed_sr**2
    # Ensure variance term is positive (it can go negative for extreme skew/kurt).
    variance_term = max(variance_term, 1e-12)

    n_min = (z_alpha**2 * variance_term) / (sr_excess**2)
    return int(math.ceil(n_min)) + 1


def _compute_sharpe_and_moments(
    trade_returns: np.ndarray,
) -> tuple[float, float, float, int]:
    """Compute (sharpe_per_observation, skewness, kurtosis_regular, n_obs).

    Defect 4 fix: compute Sharpe from per-trade returns (not per-window Sharpes).
    Defect 5 fix: compute skewness/kurtosis via ``scipy.stats.skew`` and
    ``scipy.stats.kurtosis`` (which returns excess kurtosis; we add 3 to get
    regular kurtosis for the Bailey formula).

    Returns:
        ``(sharpe_per_observation, skewness, kurtosis_regular, n_obs)``.
        When ``n < 2``, Sharpe is 0.0. When ``n < 3`` or ``n < 4``, skewness
        and kurtosis fall back to normal values (0.0 / 3.0).
    """
    n = int(len(trade_returns))
    if n < 2:
        return 0.0, 0.0, 3.0, n

    mean_r = float(np.mean(trade_returns))
    std_r = float(np.std(trade_returns, ddof=1))
    if std_r == 0.0:
        return 0.0, 0.0, 3.0, n

    sharpe = mean_r / std_r

    skew = float(stats.skew(trade_returns, bias=False)) if n >= 3 else 0.0
    excess_kurt = float(stats.kurtosis(trade_returns, bias=False)) if n >= 4 else 0.0
    return sharpe, skew, excess_kurt + 3.0, n


# ---------------------------------------------------------------------------
# Configuration and Result Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Configuration for the OOS gate.

    Defaults match the canonical 3-of-5 rule from
    ``go_nogo_criteria.AggregateCriteria``. Callers evaluating for production
    deployment should override ``min_windows_passed`` and the Sharpe/DSR
    thresholds with the tier-specific values (see :class:`TierConfig`).
    """

    # Defect 1 fix: default = 3 (was 5). Matches the verbal 3-of-5 rule
    # and the canonical go_nogo_criteria.AggregateCriteria default.
    min_windows_passed: int = 3

    # Per-window criteria (kept for parity with go_nogo_criteria defaults)
    min_window_pf: float = 1.0
    min_window_wr: float = 0.55
    min_window_trades: int = 5
    min_total_oos_trades: int = 50

    # OOS gate thresholds (paper-trading tier defaults; production overrides)
    dsr_alpha: float = 0.05
    min_aggregate_sharpe: float = 0.50

    # Defect 2 fix: default = 160 (was 30). Counts ALL evaluations across
    # strategies × pairs × timeframes = 10 × 4 × 4 = 160 per the conservative
    # multiple-testing correction in research doc §6.
    n_independent_trials: int = 160
    apply_multiple_testing_correction: bool = True

    # Defect 5 fix: skewness/kurtosis are computed from data when
    # ``n_trades >= min_observations_for_moments``, otherwise we fall back to
    # the normal-distribution assumption (skew=0, kurtosis=3).
    min_observations_for_moments: int = 30


@dataclass(frozen=True)
class TierConfig:
    """Tier-specific thresholds. Used by :func:`tier_rank_streams`."""

    name: str
    min_windows_passed: int
    min_aggregate_sharpe: float
    dsr_alpha: float


# Tier definitions from research doc §8. Order: strictest first.
TIER_A_PRODUCTION = TierConfig(
    name="A",
    min_windows_passed=5,
    min_aggregate_sharpe=1.50,
    dsr_alpha=0.05,
)
TIER_B_DEMO = TierConfig(
    name="B",
    min_windows_passed=4,
    min_aggregate_sharpe=0.95,
    dsr_alpha=0.10,
)
TIER_C_PAPER = TierConfig(
    name="C",
    min_windows_passed=3,
    min_aggregate_sharpe=0.50,
    dsr_alpha=0.10,
)


@dataclass(frozen=True)
class WalkForwardResults:
    """Walk-forward analysis results for one strategy evaluation.

    The preferred Sharpe input is ``per_window_trade_returns``: a sequence
    of per-window sequences of per-trade decimal returns. The gate computes
    Sharpe from the concatenated series (Defect 4 fix).

    If only per-window aggregate PnL is available, wrap each window's aggregate
    in a one-element list — but the resulting Sharpe will be coarse (one
    observation per window) and is not recommended for production.

    Attributes:
        strategy_name: identifier (e.g., ``"SRMR+"``).
        pair: trading pair (e.g., ``"GBPUSD"``).
        timeframe: timeframe label (e.g., ``"M15"``).
        bar_period_minutes: bar period in minutes (1, 5, 15, 30, 60, 240, 1440).
        per_window_trade_returns: per-window per-trade returns. Each inner
            sequence is the trade returns for that window; empty means 0 trades.
        per_window_win_rate: optional per-window win rates (used for per-window
            WR check). ``None`` skips the WR check.
        per_window_pf: optional per-window profit factors (used for per-window
            PF check). ``None`` skips the PF check.
        sample_duration_days: optional total sample duration in days. When
            provided, used to compute ``trades_per_year`` for proper
            per-trade-Sharpe annualization. When ``None``, falls back to
            treating each bar as a potential trade (over-estimates trades/year
            and therefore Sharpe).
    """

    strategy_name: str
    pair: str
    timeframe: str
    bar_period_minutes: int
    per_window_trade_returns: Sequence[Sequence[float]]
    per_window_win_rate: Sequence[float | None] | None = None
    per_window_pf: Sequence[float | None] | None = None
    sample_duration_days: float | None = None


@dataclass
class GateResult:
    """Result of evaluating a strategy against the OOS gate."""

    go_nogo: bool
    reason: str
    tier: str = "REJECT"  # "A", "B", "C", or "REJECT"
    details: dict = field(default_factory=dict)

    # Decomposed components (for transparency and downstream consumers)
    wf_passed: bool = False
    dsr_pvalue: float = 1.0
    aggregate_sharpe: float = 0.0
    windows_passed: int = 0
    total_oos_trades: int = 0
    expected_max_sr_under_null: float = 0.0


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------


def _per_window_check(
    window_returns: Sequence[float],
    per_window_pf: Sequence[float | None] | None,
    per_window_wr: Sequence[float | None] | None,
    window_idx: int,
    cfg: GateConfig,
) -> bool:
    """Apply per-window criteria (trade count, PF, WR) to a single window.

    Returns ``True`` iff the window passes all configured checks.
    """
    n_trades = len(window_returns)
    if n_trades < cfg.min_window_trades:
        return False

    if per_window_pf is not None and window_idx < len(per_window_pf):
        pf = per_window_pf[window_idx]
        if pf is not None and pf <= cfg.min_window_pf:
            return False

    if per_window_wr is not None and window_idx < len(per_window_wr):
        wr = per_window_wr[window_idx]
        if wr is not None and wr <= cfg.min_window_wr:
            return False

    return True


def _trades_per_year(
    total_trades: int,
    sample_duration_days: float | None,
    bar_period_minutes: int,
) -> float:
    """Compute trades_per_year for annualizing per-trade Sharpe.

    When ``sample_duration_days`` is provided, uses the explicit duration.
    Otherwise falls back to a bar-period-based heuristic that over-estimates
    trades/year (assumes ~1 trade per bar). Caller should prefer the explicit
    duration whenever available.
    """
    if sample_duration_days is not None and sample_duration_days > 0:
        years = sample_duration_days / 365.25
        return total_trades / max(years, 1e-9)
    return annualization_from_bar_period(bar_period_minutes)


def _build_gate_config(
    base: GateConfig,
    tier: TierConfig,
) -> GateConfig:
    """Construct a GateConfig with tier-specific overrides applied."""
    return GateConfig(
        min_windows_passed=tier.min_windows_passed,
        min_window_pf=base.min_window_pf,
        min_window_wr=base.min_window_wr,
        min_window_trades=base.min_window_trades,
        min_total_oos_trades=base.min_total_oos_trades,
        dsr_alpha=tier.dsr_alpha,
        min_aggregate_sharpe=tier.min_aggregate_sharpe,
        n_independent_trials=base.n_independent_trials,
        apply_multiple_testing_correction=base.apply_multiple_testing_correction,
        min_observations_for_moments=base.min_observations_for_moments,
    )


def evaluate_oos_gate(
    wf_results: WalkForwardResults,
    config: GateConfig | None = None,
) -> GateResult:
    """Evaluate one walk-forward result against the OOS gate.

    Steps:
        1. Apply per-window criteria (trade count, PF, WR).
        2. Apply aggregate criteria (windows passed, total trades).
        3. Compute annualized Sharpe from concatenated per-trade returns
           (Defect 4 fix).
        4. Compute skewness/kurtosis from per-trade returns (Defect 5 fix);
           fall back to normal-distribution values when ``n_trades < 30``.
        5. Apply DSR multiple-testing correction with ``n_independent_trials``
           (Defect 2 fix; default = 160).
        6. Apply Sharpe floor and DSR alpha threshold.

    Args:
        wf_results: walk-forward results for one strategy evaluation.
        config: optional GateConfig override. ``None`` uses the canonical
            defaults (paper-trading tier).

    Returns:
        :class:`GateResult` with verdict, reason, and decomposed components.
    """
    cfg = config or GateConfig()

    n_windows = len(wf_results.per_window_trade_returns)
    if n_windows == 0:
        return GateResult(
            go_nogo=False,
            reason="No windows provided",
            tier="REJECT",
        )

    # --- Per-window checks ---
    windows_passed = 0
    total_oos_trades = 0
    all_trade_returns: list[float] = []

    for i in range(n_windows):
        window_returns = list(wf_results.per_window_trade_returns[i])
        n_trades = len(window_returns)
        total_oos_trades += n_trades
        all_trade_returns.extend(window_returns)
        if _per_window_check(
            window_returns,
            wf_results.per_window_pf,
            wf_results.per_window_win_rate,
            i,
            cfg,
        ):
            windows_passed += 1

    wf_passed = windows_passed >= cfg.min_windows_passed and total_oos_trades >= cfg.min_total_oos_trades

    if not wf_passed:
        return GateResult(
            go_nogo=False,
            reason=(
                f"WF failed: {windows_passed}/{n_windows} windows passed "
                f"(need {cfg.min_windows_passed}), {total_oos_trades} OOS trades "
                f"(need {cfg.min_total_oos_trades})"
            ),
            tier="REJECT",
            wf_passed=False,
            windows_passed=windows_passed,
            total_oos_trades=total_oos_trades,
        )

    # --- Aggregate Sharpe from per-trade returns (Defect 4 fix) ---
    returns_arr = np.asarray(all_trade_returns, dtype=float)
    sharpe_per_trade, skew_from_data, kurt_regular_from_data, n_obs = _compute_sharpe_and_moments(returns_arr)

    trades_per_year = _trades_per_year(
        total_trades=total_oos_trades,
        sample_duration_days=wf_results.sample_duration_days,
        bar_period_minutes=wf_results.bar_period_minutes,
    )
    aggregate_sharpe = sharpe_per_trade * math.sqrt(trades_per_year)

    # --- Defect 5 fix: use moments from data only if sufficient trades ---
    if total_oos_trades < cfg.min_observations_for_moments:
        skew_used = 0.0
        kurt_used = 3.0
        moments_source = "fallback_normal"
    else:
        skew_used = skew_from_data
        kurt_used = kurt_regular_from_data
        moments_source = "from_data"

    # --- DSR (Defect 2 fix: n_independent_trials default = 160) ---
    n_eff_trials = cfg.n_independent_trials if cfg.apply_multiple_testing_correction else 1
    dsr_p = deflated_sharpe_ratio(
        observed_sr=aggregate_sharpe,
        n_trials=n_eff_trials,
        n_obs=total_oos_trades,
        skewness=skew_used,
        kurtosis_regular=kurt_used,
    )
    e_max = expected_max_sharpe(n_eff_trials)

    details = {
        "windows_passed": windows_passed,
        "total_oos_trades": total_oos_trades,
        "aggregate_sharpe_annualized": aggregate_sharpe,
        "sharpe_per_trade": sharpe_per_trade,
        "trades_per_year": trades_per_year,
        "annualization_factor": annualization_from_bar_period(wf_results.bar_period_minutes),
        "bar_period_minutes": wf_results.bar_period_minutes,
        "expected_max_sr_under_null": e_max,
        "dsr_pvalue": dsr_p,
        "n_trials_used": n_eff_trials,
        "min_windows_required": cfg.min_windows_passed,
        "min_aggregate_sharpe_required": cfg.min_aggregate_sharpe,
        "dsr_alpha": cfg.dsr_alpha,
        "skewness": skew_used,
        "kurtosis_regular": kurt_used,
        "moments_source": moments_source,
    }

    if aggregate_sharpe < cfg.min_aggregate_sharpe:
        return GateResult(
            go_nogo=False,
            reason=(f"Aggregate Sharpe {aggregate_sharpe:.2f} below floor {cfg.min_aggregate_sharpe:.2f}"),
            tier="REJECT",
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
            tier="REJECT",
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
            f"GO: {windows_passed}/{n_windows} windows passed; DSR p={dsr_p:.4f}, aggregate SR={aggregate_sharpe:.2f}"
        ),
        tier="PASS",  # tier_rank_streams may upgrade
        wf_passed=True,
        dsr_pvalue=dsr_p,
        aggregate_sharpe=aggregate_sharpe,
        windows_passed=windows_passed,
        total_oos_trades=total_oos_trades,
        expected_max_sr_under_null=e_max,
        details=details,
    )


def tier_rank_streams(
    wf_results_list: Sequence[WalkForwardResults],
    config: GateConfig | None = None,
) -> list[GateResult]:
    """Rank streams into Tier A / B / C / REJECT.

    Each stream is evaluated against all three tiers (production → demo →
    paper, strictest first); the highest tier that passes is assigned. If no
    tier passes, the stream is ``REJECT``ed and the strictest-tier evaluation
    is returned for diagnostic visibility.

    Args:
        wf_results_list: sequence of :class:`WalkForwardResults`.
        config: optional base :class:`GateConfig` shared across tiers. Tier
            thresholds override ``min_windows_passed``, ``min_aggregate_sharpe``,
            and ``dsr_alpha``. Other fields (n_independent_trials, per-window
            criteria, etc.) are inherited from the base config.

    Returns:
        One :class:`GateResult` per input, with ``tier`` set to ``"A"``,
        ``"B"``, ``"C"``, or ``"REJECT"``.
    """
    cfg = config or GateConfig()
    tiers = (TIER_A_PRODUCTION, TIER_B_DEMO, TIER_C_PAPER)
    results: list[GateResult] = []

    for wf in wf_results_list:
        assigned_tier = "REJECT"
        result: GateResult | None = None
        for tier in tiers:
            tier_cfg = _build_gate_config(cfg, tier)
            r = evaluate_oos_gate(wf, tier_cfg)
            if r.go_nogo:
                assigned_tier = tier.name
                result = r
                break

        if result is None:
            # Strictest-tier evaluation for diagnostics.
            strictest_cfg = _build_gate_config(cfg, TIER_A_PRODUCTION)
            result = evaluate_oos_gate(wf, strictest_cfg)

        result.tier = assigned_tier
        results.append(result)

    return results


__all__ = [
    "annualization_from_bar_period",
    "expected_max_sharpe",
    "deflated_sharpe_ratio",
    "evaluate_oos_gate",
    "tier_rank_streams",
    "GateConfig",
    "GateResult",
    "TierConfig",
    "WalkForwardResults",
    "TIER_A_PRODUCTION",
    "TIER_B_DEMO",
    "TIER_C_PAPER",
    "min_track_record_length",
]
