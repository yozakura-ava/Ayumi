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
7. **PBO ceiling** — for Optuna-derived params (``non-empty``), the
   per-trial return series is recorded into :class:`TrialReturnStore`
   and a CSCV matrix ``[T, N_trials]`` is built for the
   ``(archetype, pair, timeframe)`` cell.  PBO is computed via
   :func:`srf.pbo.compute_pbo` once at least 2 trials exist for the
   cell; otherwise the verdict carries ``pbo_score=None`` and
   ``pbo_tier_ceiling="NOT_APPLICABLE"`` so downstream consumers can
   distinguish "no PBO evidence" from a real rejection.  See
   :class:`TrialReturnStore` and :meth:`ValidationRunner._compute_pbo`.
8. **Cost sensitivity** — per-candidate gross-vs-spread cost penalty
   ratio, emitted on every Optuna-derived verdict.  This is a separate
   metric from PBO (cost sensitivity is one strategy's net return
   penalty; PBO is a tournament-level overfit probability).

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
* **PBO is per-cell, not per-candidate.** The synthetic 2-column
  "PBO" matrix previously synthesised in :meth:`_compute_pbo` was
  cost sensitivity masquerading as PBO; the real CSCV math
  (:func:`srf.pbo.compute_pbo`) requires N >= 2 trials for the same
  cell.  A single candidate emits ``NOT_APPLICABLE`` rather than a
  misleading number.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

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
    OOSConfig,
    PipelineConfig,
    compute_dsr_n_trials,
    default_pipeline_config,
)
from forex_bot.factory.spread_costs import (
    COMMISSION_PER_LOT_USD,
    PIP_SLIPPAGE,
    SpreadCosts,
    SpreadCostTable,
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

#: PBO ceiling string for cells with fewer than 2 recorded trials.
#: The real CSCV math requires ``N >= 2`` strategies; until enough
#: Optuna trials accumulate for a cell, the verdict reports
#: ``(pbo_score=None, pbo_tier_ceiling="NOT_APPLICABLE")`` so the
#: absence of evidence is explicit (instead of the misleading
#: synthetic 2-column "PBO" the runner previously emitted).
PBO_CEILING_NOT_APPLICABLE: str = "NOT_APPLICABLE"


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
    trial_returns: np.ndarray | None = None
    """Optional override for the per-bar return series this Optuna
    trial produced.  When supplied (e.g. by a real bridge that
    exposes the walk-forward equity curve), the runner uses this
    array as-is.  When ``None`` the runner falls back to a
    deterministic placeholder derived from ``(bars, params)``; see
    :meth:`ValidationRunner._trial_returns_for`.  This is the
    integration seam SFA-3 will use to wire real per-bar strategy
    returns into the CSCV matrix."""


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
    # PBO (None when not Optuna-derived, insufficient data, or
    # fewer than 2 trials recorded for the cell)
    pbo_score: float | None = None
    pbo_tier_ceiling: str = "N/A"
    # Cost sensitivity — gross-vs-spread cumulative penalty ratio
    # for this candidate.  Distinct from PBO (which is a tournament-
    # level overfit probability).  ``None`` when bars are too few
    # or the spread-cost table is missing the pair.
    cost_sensitivity: float | None = None
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
# TrialReturnStore — per-cell Optuna trial return accumulator
# ---------------------------------------------------------------------------


class TrialReturnStore:
    """Per-cell accumulator for Optuna-trial return series.

    Keyed by ``(archetype_id, pair, timeframe)``.  Each Optuna trial
    contributes one per-bar return series; once a cell has accumulated
    ``N >= 2`` trials, the store can build the CSCV matrix
    ``[T, N_trials]`` required by :func:`srf.pbo.compute_pbo`.

    This store is **in-memory** and lives for the duration of a single
    :class:`ValidationRunner` instance (one batch).  A DuckDB-backed
    persistence variant — so per-trial returns survive across batches
    and the cell matrix builds up over the full Optuna study — is a
    follow-up SFA-3 card.  The ``record`` / ``matrix`` interface is
    the integration seam; swapping the backend is a one-class change.

    Notes
    -----
    * ``record`` silently ignores degenerate inputs (``ndim != 1`` or
      ``size < 4``) so a bad trial doesn't poison the cell matrix.
    * ``matrix`` truncates each trial to the shortest length so the
      column-stack is rectangular (the CSCV math requires ``[T, N]``).
      Truncation is the honest choice for backtest equity curves —
      the alternative (padding with zeros) would inject artificial
      flat-bar periods that bias the IS/OOS split.
    """

    __slots__ = ("_cells",)

    def __init__(self) -> None:
        self._cells: dict[tuple[str, str, str], list[np.ndarray]] = {}

    @staticmethod
    def cell_key(archetype: str, pair: str, timeframe: str) -> tuple[str, str, str]:
        """Build the canonical cell key from (archetype, pair, timeframe)."""
        return (archetype, pair, timeframe)

    def record(self, key: tuple[str, str, str], returns: np.ndarray) -> None:
        """Append one trial's per-bar return series to the cell."""
        arr = np.asarray(returns, dtype=float)
        if arr.ndim != 1 or arr.size < 4:
            return
        self._cells.setdefault(key, []).append(arr)

    def matrix(self, key: tuple[str, str, str]) -> np.ndarray | None:
        """Return ``[T, N]`` CSCV matrix for the cell, or ``None`` if N < 2.

        ``T`` is the minimum trial length across the cell (honest
        truncation); ``N`` is the number of recorded trials.  Returns
        ``None`` when fewer than 2 trials are recorded or the cell
        has no usable trial length.
        """
        trials = self._cells.get(key)
        if not trials or len(trials) < 2:
            return None
        min_T = min(t.size for t in trials)
        if min_T < 4:
            return None
        return np.column_stack([t[:min_T] for t in trials])

    def trial_count(self, key: tuple[str, str, str]) -> int:
        """Number of trials currently recorded for the cell."""
        return len(self._cells.get(key, ()))

    def reset(self) -> None:
        """Drop all recorded trials.  Tests use this to isolate state."""
        self._cells.clear()

    def keys(self):  # type: ignore[no-untyped-def]
        """Snapshot of cell keys currently in the store (ordered)."""
        return list(self._cells.keys())


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
        trial_return_store: TrialReturnStore | None = None,
    ) -> None:
        self.pipeline_config = pipeline_config or default_pipeline_config()
        self.spread_costs = spread_costs or default_spread_costs()
        self._explicit_cell_count = cell_count
        # Per-cell Optuna trial return accumulator.  Defaults to a
        # fresh store; callers can inject a shared store so two
        # batches contribute to the same cell (DuckDB-backed
        # persistence is the SFA-3 follow-up).
        self.trial_return_store = trial_return_store or TrialReturnStore()

    # ── public ──────────────────────────────────────────────────────────

    def run_batch(self, candidates: Sequence[CandidateSpec]) -> list[ValidationVerdict]:
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
            strategy = build_strategy_from_template(candidate.template, candidate.params, candidate.pair)
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
            tier_reason = f"only {total_trades} trades (< {INSUFFICIENT_DATA_THRESHOLD} Liora threshold)"

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

        # ── Cost sensitivity (per-candidate gross-vs-spread penalty) ────
        # Distinct from PBO — cost sensitivity is a one-strategy net
        # return haircut, PBO is a tournament-level overfit probability.
        cost_sensitivity = self._compute_cost_sensitivity(candidate)

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
            cost_sensitivity=cost_sensitivity,
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
            return compute_dsr_n_trials(self._explicit_cell_count, self.pipeline_config.dsr)
        # Batch hint is not used here — the runner is stateless; the
        # caller passes cell_count when batching.
        _ = len_candidates_hint
        return DEFAULT_N_INDEPENDENT_TRIALS

    def _compute_pbo(self, candidate: CandidateSpec, total_trades: int) -> tuple[PBOScore | None, str]:
        """Compute PBO via CSCV on the ``(archetype, pair, timeframe)`` cell.

        The full CSCV needs ``N >= 2`` strategies and ``T >= 4`` periods
        (:func:`srf.pbo.compute_pbo`).  We build the ``[T, N_trials]``
        matrix from every Optuna trial's per-bar return series recorded
        in :attr:`trial_return_store` for the cell.  When fewer than
        two trials have been recorded yet — i.e. the very first
        candidate in a fresh Optuna study — the verdict carries
        ``(None, "NOT_APPLICABLE")`` so the absence of evidence is
        explicit instead of the misleading synthetic 2-column "PBO"
        the runner previously emitted (that 2-column matrix compared
        gross vs spread-adjusted returns of the **same** strategy —
        i.e. cost sensitivity, not overfit probability).

        The per-trial return series comes from
        :attr:`CandidateSpec.trial_returns` when supplied (real bridge
        output), else from the placeholder helper
        :meth:`_trial_returns_for` that derives a per-bar series from
        ``(bars, params)`` so different Optuna trials produce
        non-degenerate columns.
        """
        # Always record this trial's returns — even when the cell
        # already has >= 2 trials, the new column is needed for the
        # current candidate's matrix lookup.
        cell_key = TrialReturnStore.cell_key(
            candidate.template.archetype_id,
            candidate.pair,
            candidate.timeframe,
        )
        trial_returns = candidate.trial_returns
        if trial_returns is None:
            trial_returns = self._trial_returns_for(candidate)
        if trial_returns is not None:
            self.trial_return_store.record(cell_key, trial_returns)

        matrix = self.trial_return_store.matrix(cell_key)
        if matrix is None:
            # Fewer than 2 trials (or no usable trial length) — the
            # real CSCV math cannot run yet.  Surface that explicitly.
            return None, PBO_CEILING_NOT_APPLICABLE

        try:
            score = compute_pbo(matrix)
        except ValueError:
            return None, PBO_CEILING_NOT_APPLICABLE

        # If we had to truncate because the candidate under-validated
        # the WF window's bars, ``total_trades`` is a useful floor but
        # the matrix is already honest.  We only short-circuit on the
        # pre-matrix trade-count check (total_trades < 4) when the
        # store is empty — once a trial is recorded, the matrix is
        # the source of truth.
        _ = total_trades  # currently unused after the refactor; kept
        # in the signature so callers and dispatch contracts stay stable.

        ceiling = self.pipeline_config.pbo.tier_for(score.pbo)
        return score, ceiling

    def _trial_returns_for(self, candidate: CandidateSpec) -> np.ndarray | None:
        """Build a per-bar return series for this candidate's trial.

        This is the **placeholder** used until the bridge / walk-forward
        runner exposes per-trial equity curves (SFA-3 follow-up).  It
        transforms bar close-to-close returns by a deterministic factor
        derived from the params dict so different Optuna trials produce
        different columns in the CSCV matrix — otherwise the matrix
        would be degenerate (all columns the same → PBO undefined).

        Real strategies will pass :attr:`CandidateSpec.trial_returns`
        directly; this helper exists so the runner produces
        non-degenerate output in tests and during the transition.
        """
        if len(candidate.bars) < 2:
            return None
        closes = np.asarray([float(b.close) for b in candidate.bars], dtype=float)
        if closes.size < 2:
            return None
        bar_returns = np.diff(closes) / closes[:-1]
        # Deterministic factor in [0.25, 1.75] derived from the params
        # dict so different trials produce different per-bar series.
        seed_bytes = repr(sorted(candidate.params.items())).encode()
        h = hashlib.sha256(seed_bytes).digest()
        # Take a byte, scale to [0, 1], shift to [0.25, 1.75].
        raw = h[0] / 255.0
        factor = 0.25 + raw * 1.5
        return bar_returns * factor

    def _compute_cost_sensitivity(self, candidate: CandidateSpec) -> float | None:
        """Per-candidate gross-vs-spread cost penalty ratio.

        Returns ``|cum_spread_adj - cum_gross| / |cum_gross|`` — the
        relative haircut spread costs impose on this candidate's gross
        bar-return series.  ``None`` when bars are too few or the
        spread-cost table is missing the pair.

        **Not** a PBO signal: cost sensitivity is a one-strategy net
        return penalty.  PBO (computed separately in
        :meth:`_compute_pbo`) is the tournament-level overfit
        probability.  See card 4309d26b for the rename that retired the
        synthetic "PBO" matrix that was actually this metric in disguise.
        """
        if len(candidate.bars) < 4:
            return None
        closes = np.asarray([float(b.close) for b in candidate.bars], dtype=float)
        if closes.size < 4:
            return None
        bar_returns = np.diff(closes) / closes[:-1]
        gross_cum = float(bar_returns.sum())
        if gross_cum == 0.0:
            return None
        try:
            spread = self.spread_costs.get(candidate.pair)
        except KeyError:
            return None
        spread_cost_per_bar = spread.spread_pips * 1e-4
        spread_cum = float((bar_returns - spread_cost_per_bar).sum())
        if gross_cum == 0.0:
            return None
        penalty = abs(spread_cum - gross_cum) / abs(gross_cum)
        return float(penalty)

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
            commission_per_lot_usd=(spread.commission_per_lot_usd if spread else COMMISSION_PER_LOT_USD),
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
    "PBO_CEILING_NOT_APPLICABLE",
    "TrialReturnStore",
    "ValidationRunner",
    "ValidationVerdict",
    "run_validation_batch",
]
