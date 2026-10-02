"""SRF Multi-Objective Optuna upgrade.

Replaces single-scalar objective with Pareto front optimization:
  - Maximize OOS profit factor
  - Maximize DSR
  - Minimize max drawdown
  - Maximize parameter stability (CV-based)

Uses optuna.study with directions=['maximize', 'maximize', 'minimize', 'maximize'].
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import optuna
import pandas as pd
from optuna.samplers import NSGAIISampler
from optuna.study import StudyDirection

logger = logging.getLogger(__name__)

# F821 fix (card 9cdbfd0a): PROJECT_ROOT was referenced but never defined.
# Same convention as sibling srf modules (backup_db.py, nightly_topk.py, weekly_sweep.py).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


# ── Pareto front utilities ─────────────────────────────────────────────────


def pareto_front_trials(study: optuna.Study) -> list[optuna.trial.FrozenTrial]:
    """Extract non-dominated Pareto front trials from a multi-objective study."""
    trials = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE and t.values is not None]
    if not trials:
        return []

    n_obj = len(trials[0].values)
    directions = study.directions

    def dominates(a: optuna.trial.FrozenTrial, b: optuna.trial.FrozenTrial) -> bool:
        """True if a Pareto-dominates b (a is at least as good in all, strictly better in one)."""
        av, bv = a.values, b.values
        at_least_one_better = False
        for i in range(n_obj):
            if directions[i] == StudyDirection.MAXIMIZE:
                if av[i] < bv[i]:
                    return False
                if av[i] > bv[i]:
                    at_least_one_better = True
            else:  # MINIMIZE
                if av[i] > bv[i]:
                    return False
                if av[i] < bv[i]:
                    at_least_one_better = True
        return at_least_one_better

    front = []
    for t in trials:
        dominated = False
        for other in trials:
            if other is t:
                continue
            if dominates(other, t):
                dominated = True
                break
        if not dominated:
            front.append(t)

    return front


def summarize_pareto_front(
    study: optuna.Study,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Return top-K Pareto-optimal trials with params and user attrs."""
    front = pareto_front_trials(study)
    front.sort(key=lambda t: sum(t.values or [0]), reverse=True)

    results = []
    for t in front[:top_k]:
        results.append(
            {
                "trial_number": t.number,
                "values": t.values,
                "params": dict(t.params),
                "user_attrs": dict(t.user_attrs),
            }
        )
    return results


# ── Multi-objective objective builder ──────────────────────────────────────


def build_multi_objective(
    pair: str,
    bars: pd.DataFrame,
    strategy_factory_fn: Callable,
    walk_forward_fn: Callable,
    n_windows: int = 3,
    train_ratio: float = 0.7,
    stability_fn: Callable | None = None,
) -> Callable:
    """Build a multi-objective Optuna objective function.

    Returns a function suitable for study.optimize() that returns 4 values:
      [0] OOS profit factor (maximize)
      [1] DSR/PSR proxy (maximize)
      [2] Max drawdown (minimize)
      [3] Parameter stability (maximize)

    Parameters
    ----------
    pair : trading pair
    bars : OHLCV bars DataFrame
    strategy_factory_fn : callable(params_dict) -> strategy instance
    walk_forward_fn : callable(strategy, bars, ...) -> walk-forward result
    n_windows, train_ratio : walk-forward params
    stability_fn : optional callable(performance_list) -> float stability score
    """

    def objective(trial: optuna.Trial) -> tuple[float, float, float, float]:
        try:
            wf_result = walk_forward_fn(
                strategy=strategy_factory_fn(trial.params),
                bars=bars,
                pair=pair,
                n_windows=n_windows,
                train_ratio=train_ratio,
            )
        except Exception as exc:
            raise optuna.TrialPruned(f"Walk-forward error: {exc}") from exc

        agg = wf_result.aggregated
        if agg is None:
            raise optuna.TrialPruned("No aggregated metrics")

        # Objective 1: OOS profit factor (maximize)
        oos_pf = max(agg.mean_profit_factor, 0.0)

        # Objective 2: DSR proxy — Sharpe-like consistency metric
        # If DSR not available, use mean_pnl / std_pnl across windows
        if hasattr(agg, "dsr") and agg.dsr is not None:
            dsr = agg.dsr
        else:
            pnl_std = max(agg.std_total_pnl, 0.01)
            dsr = agg.mean_total_pnl / pnl_std

        # Objective 3: Max drawdown (minimize)
        max_dd = max(agg.mean_max_drawdown, 0.0)

        # Objective 4: Parameter stability (maximize)
        if stability_fn and hasattr(agg, "window_metrics"):
            window_perfs = [w.get("profit_factor", 0) for w in agg.window_metrics]
            stability = stability_fn(window_perfs)
        else:
            # Use inverse CV of win rate across windows
            wr_std = getattr(agg, "std_win_rate", 0.1)
            stability = 1.0 / (1.0 + wr_std * 10)

        # Store user attrs for later analysis
        trial.set_user_attr("win_rate", agg.mean_win_rate)
        trial.set_user_attr("profit_factor", agg.mean_profit_factor)
        trial.set_user_attr("max_drawdown", agg.mean_max_drawdown)
        trial.set_user_attr("total_trades", agg.mean_trade_count * agg.total_windows)
        trial.set_user_attr("dsr", dsr)
        trial.set_user_attr("stability", stability)
        trial.set_user_attr("go_nogo", getattr(wf_result, "go_nogo", False))

        return (oos_pf, dsr, max_dd, stability)

    return objective


# ── Multi-objective study runner ───────────────────────────────────────────


def run_multi_objective_study(
    pair: str,
    tf: str = "M15",
    n_trials: int = 100,
    n_windows: int = 3,
    train_ratio: float = 0.7,
    seed: int = 42,
    timeout: int | None = None,
    strategy_factory_fn: Callable | None = None,
    walk_forward_fn: Callable | None = None,
    bars: pd.DataFrame | None = None,
    stability_fn: Callable | None = None,
) -> dict[str, Any]:
    """Run a multi-objective Optuna study producing a Pareto front.

    Returns dict with pareto_trials, best params per objective, and metrics.
    """
    studies_dir = PROJECT_ROOT / "reports" / "optuna_studies"
    studies_dir.mkdir(parents=True, exist_ok=True)

    study_path = studies_dir / f"{pair}_{tf}_pareto.db"
    study_name = f"{pair}_{tf}_pareto"

    directions = ["maximize", "maximize", "minimize", "maximize"]

    if study_path.exists():
        study = optuna.load_study(
            study_name=study_name,
            storage=f"sqlite:///{study_path}",
            sampler=NSGAIISampler(seed=seed),
        )
        logger.info("Resuming Pareto study for %s/%s (%d trials)", pair, tf, len(study.trials))
    else:
        study = optuna.create_study(
            study_name=study_name,
            storage=f"sqlite:///{study_path}",
            sampler=NSGAIISampler(seed=seed),
            directions=directions,
        )
        logger.info("New Pareto study for %s/%s", pair, tf)

    # If no custom factories provided, try the existing TTS optimizer path
    if bars is None:
        from ml.optuna_optimizer import load_bars

        bars = load_bars(pair, tf)

    if strategy_factory_fn is None or walk_forward_fn is None:
        # Default: use existing TTS optimizer internals
        from ml.optuna_optimizer import build_wf_objective as build_scalar

        # Wrap the existing scalar objective to extract multi-objective values
        scalar_obj = build_scalar(pair, tf, bars, n_windows=n_windows, train_ratio=train_ratio)

        def wrapped_objective(trial: optuna.Trial) -> tuple[float, float, float, float]:
            """Wrap scalar TTS objective to produce 4D Pareto values."""
            score = scalar_obj(trial)
            # Extract attrs that scalar objective set
            attrs = trial.user_attrs
            oos_pf = attrs.get("profit_factor", 0.0)
            dsr = attrs.get("dsr", score if score > 0 else 0.0)
            max_dd = attrs.get("max_drawdown", 1.0)
            stability = 1.0 / (1.0 + abs(attrs.get("win_rate", 0.5) - 0.5) * 2)
            return (oos_pf, dsr, max_dd, stability)

        objective = wrapped_objective
    else:
        objective = build_multi_objective(
            pair,
            bars,
            strategy_factory_fn,
            walk_forward_fn,
            n_windows,
            train_ratio,
            stability_fn,
        )

    logger.info("Running %d trials (%d-objective Pareto)...", n_trials, len(directions))
    study.optimize(objective, n_trials=n_trials, timeout=timeout, show_progress_bar=True)

    # Extract Pareto front
    pareto_summary = summarize_pareto_front(study, top_k=20)

    # Best per-objective
    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    best_pf = max(completed, key=lambda t: t.values[0] if t.values else 0) if completed else None
    best_dsr = max(completed, key=lambda t: t.values[1] if t.values else 0) if completed else None
    best_dd = min(completed, key=lambda t: t.values[2] if t.values else float("inf")) if completed else None
    best_stab = max(completed, key=lambda t: t.values[3] if t.values else 0) if completed else None

    result = {
        "pair": pair,
        "timeframe": tf,
        "n_trials": len(study.trials),
        "n_complete": len(completed),
        "n_pareto": len(pareto_summary),
        "pareto_front": pareto_summary,
        "best_pf_trial": {
            "number": best_pf.number,
            "values": best_pf.values,
            "params": dict(best_pf.params),
            "attrs": dict(best_pf.user_attrs),
        }
        if best_pf
        else None,
        "best_dsr_trial": {
            "number": best_dsr.number,
            "values": best_dsr.values,
            "params": dict(best_dsr.params),
        }
        if best_dsr
        else None,
        "best_dd_trial": {
            "number": best_dd.number,
            "values": best_dd.values,
            "params": dict(best_dd.params),
        }
        if best_dd
        else None,
        "best_stability_trial": {
            "number": best_stab.number,
            "values": best_stab.values,
            "params": dict(best_stab.params),
        }
        if best_stab
        else None,
    }

    # Save report

    report_dir = PROJECT_ROOT / "reports" / "optuna_studies"
    report_path = report_dir / f"{pair}_{tf}_pareto_report.json"
    import json

    report_path.write_text(json.dumps(result, indent=2, default=str))
    logger.info("Pareto report saved: %s", report_path)

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SRF Multi-Objective Optuna")
    parser.add_argument("--pair", required=True)
    parser.add_argument("--tf", default="M15")
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--windows", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result = run_multi_objective_study(
        pair=args.pair,
        tf=args.tf,
        n_trials=args.trials,
        n_windows=args.windows,
        timeout=args.timeout,
    )

    print(f"\n{'=' * 60}")
    print(f"  Pareto Front: {result['n_pareto']} solutions from {result['n_complete']} trials")
    print(f"{'=' * 60}")

    for i, t in enumerate(result["pareto_front"][:5]):
        print(f"\n  #{i + 1} Trial #{t['trial_number']}")
        print(
            f"    Values: PF={t['values'][0]:.2f} DSR={t['values'][1]:.3f} "
            f"DD={t['values'][2]:.3f} Stab={t['values'][3]:.3f}"
        )


if __name__ == "__main__":
    main()
