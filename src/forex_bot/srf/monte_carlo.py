"""SRF Monte Carlo robustness module.

Post-backtest Monte Carlo hardening: trade-order shuffle, slippage stress,
spread stress, block-bootstrap, and missed-trade simulation.

Outputs distributions of max_drawdown, PF, Sharpe, CAGR with 5th-percentile
worst-case as the headline metric.  Also computes prop-rule breach probability.

Results are stored in the ``monte_carlo_samples`` DuckDB table.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_N_ITERATIONS = 1000
DEFAULT_PIP_SIZE = 0.0001  # non-JPY
JPY_PIP_SIZE = 0.01
DEFAULT_SLIPPAGE_PIPS = (0.5, 1.0, 2.0)
DEFAULT_SPREAD_STRESS = (0.5, 1.0)  # 50%, 100% widening
DEFAULT_DROP_FRACTION = 0.05
DEFAULT_DAILY_LOSS_LIMIT = 0.05  # 5% of account
DEFAULT_ACCOUNT_SIZE = 10_000.0
BLOCK_SIZE_DEFAULT = 20  # bars per block for block-bootstrap


# ═══════════════════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class TradeRecord:
    """Minimal trade representation for MC simulation."""

    pnl: float
    entry_time: float = 0.0  # unix timestamp
    exit_time: float = 0.0
    direction: int = 1  # +1 long, -1 short
    entry_price: float = 0.0
    exit_price: float = 0.0


@dataclass
class MCResult:
    """Aggregated Monte Carlo results."""

    n_iterations: int
    # Distributions (length = n_iterations)
    max_drawdowns: np.ndarray
    profit_factors: np.ndarray
    sharpes: np.ndarray
    cagrs: np.ndarray
    # Headline metrics (5th percentile = worst case)
    p5_max_drawdown: float
    p5_profit_factor: float
    p5_sharpe: float
    p5_cagr: float
    # Prop-rule
    prop_rule_breach_prob: float
    # Metadata
    methods_used: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Return a flat dict suitable for JSON / DuckDB storage."""
        return {
            "n_iterations": self.n_iterations,
            "p5_max_drawdown": float(self.p5_max_drawdown),
            "p5_profit_factor": float(self.p5_profit_factor),
            "p5_sharpe": float(self.p5_sharpe),
            "p5_cagr": float(self.p5_cagr),
            "prop_rule_breach_prob": float(self.prop_rule_breach_prob),
            "mean_max_drawdown": float(np.mean(self.max_drawdowns)),
            "mean_profit_factor": float(np.mean(self.profit_factors)),
            "mean_sharpe": float(np.mean(self.sharpes)),
            "methods_used": self.methods_used,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Core simulation primitives
# ═══════════════════════════════════════════════════════════════════════════


def _equity_curve(pnls: np.ndarray, initial: float = DEFAULT_ACCOUNT_SIZE) -> np.ndarray:
    """Build equity curve from PnL array."""
    return initial + np.cumsum(pnls)


def _max_drawdown(equity: np.ndarray) -> float:
    """Compute max drawdown as a fraction (0.0 = no drawdown)."""
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / np.where(peak > 0, peak, 1.0)
    return float(np.max(dd))


def _profit_factor(pnls: np.ndarray) -> float:
    """Compute profit factor (gross profit / gross loss)."""
    gains = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return float(gains / losses)


def _sharpe(pnls: np.ndarray, periods_per_year: int = 252) -> float:
    """Annualised Sharpe ratio from per-trade PnL."""
    if len(pnls) < 2 or np.std(pnls) == 0:
        return 0.0
    return float(np.mean(pnls) / np.std(pnls) * math.sqrt(periods_per_year))


def _cagr(pnls: np.ndarray, initial: float = DEFAULT_ACCOUNT_SIZE, years: float = 1.0) -> float:
    """Compound annual growth rate."""
    final = initial + pnls.sum()
    if initial <= 0 or years <= 0 or final <= 0:
        return 0.0
    return float((final / initial) ** (1.0 / years) - 1.0)


def _daily_pnl_breach(
    pnls: np.ndarray,
    loss_limit: float = DEFAULT_DAILY_LOSS_LIMIT,
    initial: float = DEFAULT_ACCOUNT_SIZE,
) -> bool:
    """Check if any contiguous block reaches the daily loss limit.

    We treat each MC iteration as one "day" of trades; if the cumulative
    loss at any point exceeds ``loss_limit * initial``, it's a breach.
    """
    threshold = loss_limit * initial
    equity = initial + np.cumsum(pnls)
    return bool(np.any((initial - equity) >= threshold))


# ═══════════════════════════════════════════════════════════════════════════
# MC methods
# ═══════════════════════════════════════════════════════════════════════════


def trade_shuffle(
    trades: Sequence[TradeRecord],
    n_iter: int = DEFAULT_N_ITERATIONS,
    rng: np.random.Generator | None = None,
) -> list[np.ndarray]:
    """Plain trade-order shuffle — destroys serial dependence."""
    if rng is None:
        rng = np.random.default_rng()
    pnls = np.array([t.pnl for t in trades])
    return [rng.permutation(pnls) for _ in range(n_iter)]


def block_bootstrap(
    trades: Sequence[TradeRecord],
    n_iter: int = DEFAULT_N_ITERATIONS,
    block_size: int = BLOCK_SIZE_DEFAULT,
    rng: np.random.Generator | None = None,
) -> list[np.ndarray]:
    """Block-bootstrap — preserves short-range serial dependence."""
    if rng is None:
        rng = np.random.default_rng()
    pnls = np.array([t.pnl for t in trades])
    n = len(pnls)
    if n <= block_size:
        # Too few trades for blocking — fall back to shuffle
        return trade_shuffle(trades, n_iter, rng)
    results = []
    for _ in range(n_iter):
        blocks = []
        total = 0
        while total < n:
            start = rng.integers(0, n - block_size + 1)
            end = min(start + block_size, n)
            blocks.append(pnls[start:end])
            total += end - start
        results.append(np.concatenate(blocks)[:n])
    return results


def slippage_stress(
    trades: Sequence[TradeRecord],
    pip_size: float = DEFAULT_PIP_SIZE,
    slippage_pips: tuple[float, ...] = DEFAULT_SLIPPAGE_PIPS,
    rng: np.random.Generator | None = None,
) -> list[np.ndarray]:
    """Apply random slippage per trade and return PnL arrays."""
    if rng is None:
        rng = np.random.default_rng()
    base_pnls = np.array([t.pnl for t in trades])
    slip_values = np.array(slippage_pips) * pip_size
    results = []
    for slip in slip_values:
        # Random sign slippage (unfavourable half the time)
        signs = rng.choice([-1, 1], size=len(base_pnls))
        adjusted = base_pnls - signs * slip
        results.append(adjusted)
    return results


def spread_stress(
    trades: Sequence[TradeRecord],
    widen_factors: tuple[float, ...] = DEFAULT_SPREAD_STRESS,
    pip_size: float = DEFAULT_PIP_SIZE,
) -> list[np.ndarray]:
    """Widen effective spread, reducing each trade's PnL proportionally."""
    base_pnls = np.array([t.pnl for t in trades])
    # Approximate: each trade loses extra half-spread × widen_factor
    base_spread_cost = pip_size * 1.0  # assume ~1 pip base spread
    results = []
    for factor in widen_factors:
        extra_cost = base_spread_cost * factor
        adjusted = base_pnls - np.where(base_pnls > 0, extra_cost, -extra_cost)
        results.append(adjusted)
    return results


def missed_trade_sim(
    trades: Sequence[TradeRecord],
    drop_fraction: float = DEFAULT_DROP_FRACTION,
    n_iter: int = DEFAULT_N_ITERATIONS,
    rng: np.random.Generator | None = None,
) -> list[np.ndarray]:
    """Randomly drop a fraction of trades to simulate missed fills."""
    if rng is None:
        rng = np.random.default_rng()
    pnls = np.array([t.pnl for t in trades])
    n = len(pnls)
    n_keep = max(1, int(n * (1.0 - drop_fraction)))
    results = []
    for _ in range(n_iter):
        idx = rng.choice(n, size=n_keep, replace=False)
        results.append(pnls[idx])
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


def run_monte_carlo(
    trades: Sequence[TradeRecord],
    *,
    n_iterations: int = DEFAULT_N_ITERATIONS,
    pip_size: float = DEFAULT_PIP_SIZE,
    daily_loss_limit: float = DEFAULT_DAILY_LOSS_LIMIT,
    account_size: float = DEFAULT_ACCOUNT_SIZE,
    seed: int | None = None,
    use_block_bootstrap: bool = True,
    block_size: int = BLOCK_SIZE_DEFAULT,
) -> MCResult:
    """Run full Monte Carlo robustness suite.

    Combines shuffle, block-bootstrap, slippage, spread, and missed-trade
    simulations into a single distribution.

    Parameters
    ----------
    trades : sequence of TradeRecord
    n_iterations : iterations for shuffle/bootstrap/missed-trade
    pip_size : 0.0001 for non-JPY, 0.01 for JPY
    daily_loss_limit : prop-rule daily loss limit as fraction of account
    account_size : starting balance for equity curve calcs
    seed : RNG seed for reproducibility
    use_block_bootstrap : include block-bootstrap results alongside shuffle
    block_size : block size for bootstrap

    Returns
    -------
    MCResult with aggregated distributions and headline metrics.
    """
    rng = np.random.default_rng(seed)
    methods: list[str] = ["shuffle"]
    all_pnl_arrays = trade_shuffle(trades, n_iterations, rng)

    if use_block_bootstrap:
        methods.append("block_bootstrap")
        all_pnl_arrays.extend(block_bootstrap(trades, n_iterations, block_size, rng))

    methods.append("slippage")
    all_pnl_arrays.extend(slippage_stress(trades, pip_size, rng=rng))

    methods.append("spread")
    all_pnl_arrays.extend(spread_stress(trades, pip_size=pip_size))

    methods.append("missed_trade")
    all_pnl_arrays.extend(missed_trade_sim(trades, rng=rng))

    # Compute metrics for every simulated PnL array
    max_dds, pfs, sharpes, cagrs, breaches = [], [], [], [], 0
    for pnls in all_pnl_arrays:
        equity = _equity_curve(pnls, account_size)
        max_dds.append(_max_drawdown(equity))
        pfs.append(_profit_factor(pnls))
        sharpes.append(_sharpe(pnls))
        cagrs.append(_cagr(pnls, account_size))
        if _daily_pnl_breach(pnls, daily_loss_limit, account_size):
            breaches += 1

    max_dds = np.array(max_dds)
    pfs = np.array(pfs)
    sharpes = np.array(sharpes)
    cagrs = np.array(cagrs)
    n_total = len(all_pnl_arrays)

    return MCResult(
        n_iterations=n_total,
        max_drawdowns=max_dds,
        profit_factors=pfs,
        sharpes=sharpes,
        cagrs=cagrs,
        p5_max_drawdown=float(np.percentile(max_dds, 95)),  # 95th pct of DD = worst 5%
        p5_profit_factor=float(np.percentile(pfs, 5)),
        p5_sharpe=float(np.percentile(sharpes, 5)),
        p5_cagr=float(np.percentile(cagrs, 5)),
        prop_rule_breach_prob=breaches / n_total,
        methods_used=methods,
    )


# ═══════════════════════════════════════════════════════════════════════════
# DuckDB storage
# ═══════════════════════════════════════════════════════════════════════════


def store_mc_results(conn, run_id: str, result: MCResult) -> None:
    """Store MC summary metrics in the ``monte_carlo_samples`` table.

    Parameters
    ----------
    conn : active DuckDB connection
    run_id : parent run ID
    result : MCResult from run_monte_carlo
    """
    # Store aggregate as a single "sample" with idx=0
    metrics = result.summary()
    sample_rows = [
        (run_id, 0, "p5_max_drawdown", metrics["p5_max_drawdown"], None, None),
        (run_id, 0, "p5_profit_factor", metrics["p5_profit_factor"], None, None),
        (run_id, 0, "p5_sharpe", metrics["p5_sharpe"], None, None),
        (run_id, 0, "p5_cagr", metrics["p5_cagr"], None, None),
        (
            run_id,
            0,
            "prop_rule_breach_prob",
            metrics["prop_rule_breach_prob"],
            None,
            None,
        ),
        (run_id, 0, "mean_max_drawdown", metrics["mean_max_drawdown"], None, None),
        (run_id, 0, "mean_profit_factor", metrics["mean_profit_factor"], None, None),
        (run_id, 0, "mean_sharpe", metrics["mean_sharpe"], None, None),
        (run_id, 0, "n_iterations", float(metrics["n_iterations"]), None, None),
    ]
    conn.executemany(
        "INSERT INTO monte_carlo_samples VALUES (?, ?, ?, ?, ?, ?)",
        sample_rows,
    )
    logger.info("Stored %d MC metric rows for run %s", len(sample_rows), run_id)
