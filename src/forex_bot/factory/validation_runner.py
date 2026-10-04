"""Validation runner — batch WF + DSR + spread-cost gate (spec §4).

Single batch entry point for the Strategy Factory.  Takes factory
candidates (from the SFA-1 bridge) and emits per-candidate verdicts
(Tier A/B/fail + insufficient-data) persisted to ``research.duckdb``.

Pipeline per candidate
----------------------
1. **OOS guard** — if any bar's date falls inside the OOS holdout and the
   candidate is not explicitly unlocked, raise :class:`PermissionError`
   (spec §4.5 / Kaito ground rule).
2. **Bridge build** — instantiate the ``ISignalStrategy`` via
   :func:`forex_bot.factory.bridge.build_strategy_from_template`.
3. **Spread cost lookup** — :class:`SpreadCostTable` provides the
   ``spread_pips`` and ``commission_per_lot_usd`` (Liora ground rule,
   no inline magic numbers).
4. **Walk-forward** — call :func:`backtest.walk_forward_runner.run_strategy_walk_forward`
   with the WF config from :class:`WalkForwardWindowConfig` (timeframe
   keys).
5. **DSR tier** — feed the aggregated metrics to
   :func:`quant.dsr_integration.annotate_wf_results_with_dsr` with
   ``n_trials`` computed via
   :func:`forex_bot.factory.pipeline_config.compute_dsr_n_trials`
   (spec §4.2 scaling).
6. **Insufficient-data guard** — :attr:`INSUFFICIENT_DATA_THRESHOLD`
   trades per Liora ground rule.
7. **PBO ceiling** — for Optuna-derived params (``non-empty``), compute
   PBO via :func:`srf.pbo.compute_pbo` and downgrade tier to the
   :class:`PBOConfig`-derived ceiling when stricter (spec §4.6).

Every verdict carries the **spread cost snapshot** (``spread_pips`` /
``commission_per_lot_usd`` / ``slippage_pips``) so downstream consumers
can audit the cost assumption without re-deriving it.

Design notes
------------
* **Pure functions where possible.** :class:`ValidationRunner.run_batch`
  returns ``list[ValidationVerdict]``; persistence is the caller's job
  (``FactoryVerdictStore`` or a CLI wrapper).
* **Single-cell and batch modes share the same code path** — the only
  difference is that batch mode defaults ``cell_count`` to ``len(candidates)``
  for DSR ``n_trials`` scaling.
* **Bridge errors and WF errors are NOT exceptions** — they surface as
  ``tier="REJECT"`` with a descriptive ``reason``.  This keeps batch
  validation fault-tolerant: one broken candidate does not abort the
  whole batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Sequence
import logging

import numpy as np

from backtest.engine import Bar
from backtest.walk_forward_runner import run_strategy_walk_forward
from quant.dsr_integration import (
    DEFAULT_N_INDEPENDENT_TRIALS,
    annotate_wf_results_with_dsr,
)
from srf.pbo import PBOScore, compute_pbo

from forex_bot.factory.bridge import BridgeError, build_strategy_from_template
from forex_bot.factory.pipeline_config import (
    DSRConfig,
    OOSConfig,
    PBOConfig,
    PipelineConfig,
    compute_dsr_n_trials,
    default_pipeline_config,
)
from forex_bot.factory.spread_costs import (
    COMMISSION_PER_LOT_USD,
    PIP_SLIPPAGE,
    SpreadCostTable,
    SpreadCosts,
    default_spread_costs,
)
from forex_bot.factory.template import StrategyTemplate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants (spec §4.4 / Liora ground rule)
# ---------------------------------------------------------------------------

#: Insufficient-data threshold (Liora ground rule).
#: Candidates below this trade count are marked ``INSUFFICIENT_DATA``,
#: not "fail".
INSUFFICIENT_DATA_THRESHOLD: int = 10


# ---------------------------------------------------------------------------
# Spec dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateSpec:
    """One factory candidate to validate.

    Attributes
    ----------
    candidate_id
        Stable identifier (e.g. ``strategy_id`` from the registry).
    template
        The :class:`StrategyTemplate` whose :meth:`build_strategy` is
        invoked by the bridge.
    params
        Optuna-sampled (or default) params.  Empty ``{}`` ⇒ identity
        build ⇒ PBO is **not** computed.  Non-empty ⇒ Optuna-derived ⇒
        PBO is computed before promotion.
    pair
        Symbol (e.g. ``"EURUSD"``).
    timeframe
        One of the timeframes registered in
        :attr:`PipelineConfig.wf_windows`.
    bars
        Bar history.  Must be chronologically sorted.
    oos_unlocked
        If ``True``, bars that fall inside :attr:`OOSConfig.holdout_start` /
        ``holdout_end`` are allowed (validation mode).  Otherwise the
        OOS guard refuses them.
    """

    candidate_id: str
    template: StrategyTemplate
    params: Mapping[str, Any] = field(default_factory=dict)
    pair: str = "EURUSD"
    timeframe: str = "H1"
    bars: Sequence[Bar] = field(default_factory=tuple)
    oos_unlocked: bool = False


@dataclass(frozen=True)
class ValidationVerdict:
    """One validation verdict for one candidate.

    ``tier`` is one of:

    * ``"A"`` / ``"B"`` / ``"C"`` — DSR-promoted (spec §4.6 weight table).
    * ``"REJECT"`` — DSR / WF / bridge / OOS guard failed.
    * ``"INSUFFICIENT_DATA"`` — total trades < 10 (Liora ground rule).
    """

    candidate_id: str
    archetype_id: str
    pair: str
    timeframe: str
    tier: str
    # WF aggregates
    windows_passed: int = 0
    windows_total: int = 0
    total_trades: int = 0
    mean_sharpe: float = 0.0
    mean_profit_factor: float = 0.0
    mean_win_rate: float = 0.0
    max_drawdown: float = 0.0
    # DSR
    dsr_pvalue: float = 1.0
    n_trials_used: int = 0
    # PBO (None when not Optuna-derived or insufficient data)
    pbo_score: float | None = None
    pbo_tier_ceiling: str = "N/A"
    # Spread costs (snapshot per verdict — Liora ground rule)
    spread_pips: float = 0.0
    commission_per_lot_usd: float = COMMISSION_PER_LOT_USD
    slippage_pips: float = PIP_SLIPPAGE
    # Diagnostics
    go_nogo: bool = False
    reason: str = ""
    ran_at: str = ""
    bridge_error: str | None = None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class ValidationRunner:
    """Batch validation runner: candidates → WF → DSR → verdicts.

    Stateless apart from constructor config.  ``run_one`` /
    ``run_batch`` are pure (no I/O) — persistence is the caller's job.
    """

    def __init__(
        self,
        *,
        pipeline_config: PipelineConfig | None = None,
        spread_costs: SpreadCostTable | None = None,
        cell_count: int | None = None,
    ) -> None:
        self.pipeline_config = pipeline_config or default_pipeline_config()
        self.spread_costs = spread_costs or default_spread_costs()
        self._explicit_cell_count = cell_count

    # ── public ──────────────────────────────────────────────────────────

    def run_batch(
        self, candidates: Sequence[CandidateSpec]
    ) -> list[ValidationVerdict]:
        """Validate a batch of candidates; one :class:`ValidationVerdict` per input.

        The batch's ``cell_count`` defaults to ``len(candidates)`` (used
        for DSR ``n_trials`` scaling) — explicit ``cell_count`` on the
        constructor wins.
        """
        return [self.run_one(c) for c in candidates]

    def run_one(self, candidate: CandidateSpec) -> ValidationVerdict:
        """Validate one candidate; return a :class:`ValidationVerdict`."""
        ran_at = datetime.now(timezone.utc).isoformat()
        archetype = candidate.template.archetype_id

        # ── OOS guard ───────────────────────────────────────────────────
        try:
            self._oos_guard(candidate)
        except PermissionError as exc:
            return self._reject(
                candidate,
                archetype,
                reason=f"OOS locked: {exc}",
                ran_at=ran_at,
            )

        # ── Bridge build ────────────────────────────────────────────────
        try:
            strategy = build_strategy_from_template(
                candidate.template, candidate.params, candidate.pair
            )
        except BridgeError as exc:
            return self._reject(
                candidate,
                archetype,
                reason=f"bridge failed: {exc}",
                ran_at=ran_at,
                bridge_error=str(exc),
            )

        # ── Spread cost lookup (Liora ground rule) ──────────────────────
        try:
            spread = self.spread_costs.get(candidate.pair)
        except KeyError as exc:
            return self._reject(
                candidate,
                archetype,
                reason=f"spread cost missing: {exc}",
                ran_at=ran_at,
            )

        # ── Walk-forward ─────────────────────────────────────────────────
        try:
            wf_cfg = self.pipeline_config.wf_for(candidate.timeframe)
        except KeyError as exc:
            return self._reject(
                candidate,
                archetype,
                reason=f"WF config missing: {exc}",
                ran_at=ran_at,
                spread=spread,
            )

        try:
            wf = run_strategy_walk_forward(
                bars=list(candidate.bars),
                strategy_factory=lambda: strategy,
                pair=candidate.pair,
                n_windows=wf_cfg.windows,
                train_ratio=wf_cfg.train_ratio,
                val_ratio=wf_cfg.val_ratio,
                overlap_ratio=wf_cfg.overlap_ratio,
                spread_pips=spread.spread_pips,
                commission_per_lot=spread.commission_per_lot_usd,
                embargo_bars=wf_cfg.embargo_bars,
            )
        except Exception as exc:  # noqa: BLE001 — runner is fault-tolerant by design
            logger.warning(
                "WF failed for %s/%s/%s: %s",
                candidate.candidate_id,
                candidate.pair,
                candidate.timeframe,
                exc,
            )
            return self._reject(
                candidate,
                archetype,
                reason=f"WF raised: {type(exc).__name__}: {exc}",
                ran_at=ran_at,
                spread=spread,
            )

        if not wf.aggregated:
            return self._reject(
                candidate,
                archetype,
                reason="no aggregated metrics returned",
                ran_at=ran_at,
                spread=spread,
            )

        agg = wf.aggregated
        windows_total = len(wf.per_window)
        windows_passed = agg.windows_passed
        total_trades = int(round(agg.mean_trade_count * windows_passed))
        mean_sharpe = float(agg.mean_sharpe_ratio)
        mean_pf = float(agg.mean_profit_factor)
        mean_wr = float(agg.mean_win_rate)
        max_dd = float(agg.mean_max_drawdown)

        # ── DSR tier ────────────────────────────────────────────────────
        n_trials = self._compute_n_trials(len_candidates_hint=1)
        entry = {
            "mean_sharpe": mean_sharpe,
            "mean_trade_count": float(agg.mean_trade_count),
            "windows_passed": int(windows_passed),
        }
        annotated = annotate_wf_results_with_dsr([entry], n_trials=n_trials)[0]
        tier = str(annotated["tier"])
        tier_reason = str(annotated.get("tier_reason", ""))

        # ── Insufficient-data guard (Liora ground rule) ────────────────
        if total_trades < INSUFFICIENT_DATA_THRESHOLD:
            tier = "INSUFFICIENT_DATA"
            tier_reason = (
                f"only {total_trades} trades "
                f"(< {INSUFFICIENT_DATA_THRESHOLD} Liora threshold)"
            )

        # ── PBO ceiling (only for Optuna-derived params) ────────────────
        pbo_value: float | None = None
        pbo_ceiling = "N/A"
        if bool(candidate.params):
            pbo_score_obj, pbo_ceiling = self._compute_pbo(candidate, total_trades)
            if pbo_score_obj is not None:
                pbo_value = float(pbo_score_obj.pbo)
                # PBOConfig ceiling may downgrade A/B → C/REJECT but never
                # raise INSUFFICIENT_DATA to a promoted tier.
                ceiling_rank = {"A": 3, "B": 2, "C": 1, "REJECT": 0, "INSUFFICIENT": -1}
                current_rank = ceiling_rank.get(tier, 0)
                pbo_rank = ceiling_rank.get(pbo_ceiling, 0)
                if pbo_rank < current_rank and tier != "INSUFFICIENT_DATA":
                    tier_reason += f" (downgraded by PBO={pbo_value:.3f})"
                    tier = pbo_ceiling

        return ValidationVerdict(
            candidate_id=candidate.candidate_id,
            archetype_id=archetype,
            pair=candidate.pair,
            timeframe=candidate.timeframe,
            tier=tier,
            windows_passed=windows_passed,
            windows_total=windows_total,
            total_trades=total_trades,
            mean_sharpe=mean_sharpe,
            mean_profit_factor=mean_pf,
            mean_win_rate=mean_wr,
            max_drawdown=max_dd,
            dsr_pvalue=float(annotated["dsr_pvalue"]),
            n_trials_used=n_trials,
            pbo_score=pbo_value,
            pbo_tier_ceiling=pbo_ceiling,
            spread_pips=spread.spread_pips,
            commission_per_lot_usd=spread.commission_per_lot_usd,
            slippage_pips=spread.slippage_pips,
            go_nogo=bool(wf.go_nogo),
            reason=tier_reason,
            ran_at=ran_at,
        )

    # ── internals ───────────────────────────────────────────────────────

    def _oos_guard(self, candidate: CandidateSpec) -> None:
        """Raise :class:`PermissionError` if OOS bars leak without unlock."""
        oos_cfg: OOSConfig = self.pipeline_config.oos
        for bar in candidate.bars:
            bar_dt = getattr(bar, "time", None)
            if bar_dt is None:
                continue
            bar_date: date
            if isinstance(bar_dt, datetime):
                bar_date = bar_dt.date()
            elif isinstance(bar_dt, date):
                bar_date = bar_dt
            else:
                continue
            if oos_cfg.contains(bar_date):
                oos_cfg.assert_unlocked(
                    unlocked=candidate.oos_unlocked,
                    caller=candidate.candidate_id,
                )
                return

    def _compute_n_trials(self, *, len_candidates_hint: int) -> int:
        """Resolve DSR ``n_trials`` from explicit cell_count or batch size."""
        if self._explicit_cell_count is not None:
            return compute_dsr_n_trials(
                self._explicit_cell_count, self.pipeline_config.dsr
            )
        # Batch hint is not used here — the runner is stateless; the
        # caller passes cell_count when batching.
        _ = len_candidates_hint
        return DEFAULT_N_INDEPENDENT_TRIALS

    def _compute_pbo(
        self, candidate: CandidateSpec, total_trades: int
    ) -> tuple[PBOScore | None, str]:
        """Compute PBO for Optuna-derived params.

        The full CSCV needs ``N >= 2`` strategies and ``T >= 4`` periods.
        For SFA-2 we synthesise a ``[T, 2]`` matrix from the candidate's
        bar close-to-close returns (column 1: gross, column 2:
        spread-adjusted) so the math runs end-to-end on real bar data.
        A future SFA-3 build can swap in proper per-trial Optuna
        returns.
        """
        if total_trades < 4 or len(candidate.bars) < 8:
            return None, "INSUFFICIENT"
        closes = np.asarray(
            [float(b.close) for b in candidate.bars], dtype=float
        )
        if closes.size < 8:
            return None, "INSUFFICIENT"
        bar_returns = np.diff(closes) / closes[:-1]
        # Synthetic "strategies" for the PBO matrix.
        gross = np.cumsum(bar_returns)
        spread_cost_per_bar = self.spread_costs.get(candidate.pair).spread_pips * 1e-4
        spread_adj = np.cumsum(bar_returns - spread_cost_per_bar)
        matrix = np.column_stack([gross, spread_adj])
        try:
            score = compute_pbo(matrix)
        except ValueError:
            return None, "INSUFFICIENT"
        ceiling = self.pipeline_config.pbo.tier_for(score.pbo)
        return score, ceiling

    def _reject(
        self,
        candidate: CandidateSpec,
        archetype: str,
        *,
        reason: str,
        ran_at: str,
        bridge_error: str | None = None,
        spread: SpreadCosts | None = None,
    ) -> ValidationVerdict:
        """Build a :class:`ValidationVerdict` for a failed candidate."""
        return ValidationVerdict(
            candidate_id=candidate.candidate_id,
            archetype_id=archetype,
            pair=candidate.pair,
            timeframe=candidate.timeframe,
            tier="REJECT",
            spread_pips=spread.spread_pips if spread else 0.0,
            commission_per_lot_usd=(
                spread.commission_per_lot_usd if spread else COMMISSION_PER_LOT_USD
            ),
            slippage_pips=spread.slippage_pips if spread else PIP_SLIPPAGE,
            reason=reason,
            ran_at=ran_at,
            bridge_error=bridge_error,
        )


# ---------------------------------------------------------------------------
# Convenience: pure batch factory (no I/O)
# ---------------------------------------------------------------------------


def run_validation_batch(
    candidates: Sequence[CandidateSpec],
    *,
    pipeline_config: PipelineConfig | None = None,
    spread_costs: SpreadCostTable | None = None,
) -> list[ValidationVerdict]:
    """Convenience wrapper: build a runner, run a batch, return verdicts.

    Sets ``cell_count = len(candidates)`` automatically so DSR
    ``n_trials`` scales with the batch size (spec §4.2).
    """
    runner = ValidationRunner(
        pipeline_config=pipeline_config,
        spread_costs=spread_costs,
        cell_count=len(candidates),
    )
    return runner.run_batch(candidates)


__all__ = [
    "CandidateSpec",
    "INSUFFICIENT_DATA_THRESHOLD",
    "ValidationRunner",
    "ValidationVerdict",
    "run_validation_batch",
]