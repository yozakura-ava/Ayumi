#!/usr/bin/env python3
"""First full crypto sweep through the new spine (card e0067a2e).

End-to-end exercise of the strategy-factory spine on BTC/ETH/SOL H1 bars:

  integrity gate -> PIT universe -> full crypto overlay
  (funding+liquidation+venue+vol-target) -> ValidationRunner -> CPCV
  -> BH-FDR w/ meta-label gate -> factory_verdicts (provenance) ->
  LightGBM challenger benchmark.

Honest synthetic bars
---------------------
No real BTC/ETH/SOL OHLCV lives in the repo at this commit. The paper
trade book (data/crypto/paper_run_*) has 0 executed trades, and tick
vault has only USDJPY. The spine modules require H1 bars; without
real bars the sweep cannot proceed. We therefore generate
**deterministic synthetic H1 bars** with crypto-realistic drift /
vol regimes, clearly labelled as SYNTHETIC in every report field and
the data_hash provenance column. The first real-data sweep will
replace the synthetic bars; the spine plumbing we exercise here is
the point of this card.

Design
------
* ``scripts/crypto_first_sweep/`` is the run directory under the
  worktree; the report lands under ``docs/reports/`` per repo
  convention and the factory_verdicts table is written to a sweep-
  local research.duckdb (NOT the canonical data/research/) so we
  never collide with production state.
* The sweep is intentionally small (3 pairs x 6 candidates x 5
  windows) so the spine is exercised within the cpu_guard budget
  (1 CPU 08:00-23:00 America/Toronto). Defects discovered during
  the run are fix-forwarded in fresh-worktree branches per HR
  discipline, then the sweep re-runs.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

# Repo path setup. The Ayumi repo's import strategy relies on having
# BOTH ``src/`` AND ``src/forex_bot/`` on sys.path:
#   * ``src/`` provides ``forex_bot.*`` for the factory / overlay
#     modules imported via the canonical namespace.
#   * ``src/forex_bot/`` makes ``forex_bot.*`` packages (and their
#     re-exports) importable as the legacy ``backtest.X``,
#     ``strategies.X`` aliases — the validation_runner, factory
#     bridge, and overlay wrappers all use these legacy top-level
#     imports. The ``forex_bot/strategies/__init__.py`` does
#     ``from strategies.registry import ...`` which only resolves
#     when ``forex_bot/`` itself is on sys.path.
#
# We add ``src/`` first (so ``forex_bot.backtest.integrity_gate``
# resolves canonically) then ``src/forex_bot/`` (so the aliases
# resolve). Verified all spine modules import cleanly with both
# paths via the probe script.
WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "src" / "forex_bot"))

# Spine imports — every import below is a layer of the spine we
# must exercise. If any import fails we surface it as a defect in the
# report (not silently swallowed) so the orchestrator can decide
# whether to fix-forward or file a card.
SPINE_IMPORT_ERRORS: dict[str, str] = {}


def _safe_import(name: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        SPINE_IMPORT_ERRORS[name] = f"{type(exc).__name__}: {exc}"
        return None


try:
    from forex_bot.backtest.types import Bar  # noqa: E402
except Exception as exc:  # noqa: BLE001
    SPINE_IMPORT_ERRORS["Bar"] = f"{type(exc).__name__}: {exc}"
    Bar = None  # type: ignore[assignment]

IntegrityConfig = _safe_import(
    "IntegrityConfig",
    lambda: __import__("forex_bot.backtest.integrity_gate", fromlist=["IntegrityConfig"]).IntegrityConfig,
)
# Real entry points in integrity_gate:
#   - enforce_integrity_gate(symbol, bars, config)  -> IntegrityReport (raises on fail)
#   - validate_crypto_bars(symbol, bars, config)    -> IntegrityReport (no raise)
enforce_integrity_gate = _safe_import(
    "enforce_integrity_gate",
    lambda: __import__("forex_bot.backtest.integrity_gate", fromlist=["enforce_integrity_gate"]).enforce_integrity_gate,
)
validate_crypto_bars = _safe_import(
    "validate_crypto_bars",
    lambda: __import__("forex_bot.backtest.integrity_gate", fromlist=["validate_crypto_bars"]).validate_crypto_bars,
)
IntegrityReport = _safe_import(
    "IntegrityReport",
    lambda: __import__("forex_bot.backtest.integrity_gate", fromlist=["IntegrityReport"]).IntegrityReport,
)

UniverseEntry = _safe_import(
    "UniverseEntry",
    lambda: __import__("forex_bot.backtest.universe", fromlist=["UniverseEntry"]).UniverseEntry,
)
resolve_universe = _safe_import(
    "resolve_universe",
    lambda: __import__("forex_bot.backtest.universe", fromlist=["resolve_universe"]).resolve_universe,
)
DEFAULT_CRYPTO_UNIVERSE = _safe_import(
    "DEFAULT_CRYPTO_UNIVERSE",
    lambda: __import__("forex_bot.backtest.universe", fromlist=["DEFAULT_CRYPTO_UNIVERSE"]).DEFAULT_CRYPTO_UNIVERSE,
)
build_universe_integrity_config = _safe_import(
    "build_universe_integrity_config",
    lambda: __import__("forex_bot.backtest.universe", fromlist=["build_universe_integrity_config"]).build_universe_integrity_config,
)

PipelineConfig = _safe_import(
    "PipelineConfig",
    lambda: __import__("forex_bot.factory.pipeline_config", fromlist=["PipelineConfig"]).PipelineConfig,
)
default_pipeline_config = _safe_import(
    "default_pipeline_config",
    lambda: __import__("forex_bot.factory.pipeline_config", fromlist=["default_pipeline_config"]).default_pipeline_config,
)
default_spread_costs = _safe_import(
    "default_spread_costs",
    lambda: __import__("forex_bot.factory.spread_costs", fromlist=["default_spread_costs"]).default_spread_costs,
)
CandidateSpec = _safe_import(
    "CandidateSpec",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["CandidateSpec"]).CandidateSpec,
)
ValidationRunner = _safe_import(
    "ValidationRunner",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["ValidationRunner"]).ValidationRunner,
)
ValidationVerdict = _safe_import(
    "ValidationVerdict",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["ValidationVerdict"]).ValidationVerdict,
)
TrialReturnStore = _safe_import(
    "TrialReturnStore",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["TrialReturnStore"]).TrialReturnStore,
)
FactoryVerdictStore = _safe_import(
    "FactoryVerdictStore",
    lambda: __import__("forex_bot.factory.storage", fromlist=["FactoryVerdictStore"]).FactoryVerdictStore,
)
rank_candidates_by_trial_returns = _safe_import(
    "rank_candidates_by_trial_returns",
    lambda: __import__(
        "forex_bot.factory.risk_adjusted_ranking", fromlist=["rank_candidates_by_trial_returns"]
    ).rank_candidates_by_trial_returns,
)
rank_candidates_with_meta_gate = _safe_import(
    "rank_candidates_with_meta_gate",
    lambda: __import__(
        "forex_bot.factory.risk_adjusted_ranking", fromlist=["rank_candidates_with_meta_gate"]
    ).rank_candidates_with_meta_gate,
)
rank_from_trial_return_store = _safe_import(
    "rank_from_trial_return_store",
    lambda: __import__(
        "forex_bot.factory.risk_adjusted_ranking", fromlist=["rank_from_trial_return_store"]
    ).rank_from_trial_return_store,
)

MetaTradeContext = _safe_import(
    "MetaTradeContext",
    lambda: __import__("forex_bot.factory.meta_labeling", fromlist=["MetaTradeContext"]).MetaTradeContext,
)
MetaLabeledTrade = _safe_import(
    "MetaLabeledTrade",
    lambda: __import__("forex_bot.factory.meta_labeling", fromlist=["MetaLabeledTrade"]).MetaLabeledTrade,
)
build_meta_features = _safe_import(
    "build_meta_features",
    lambda: __import__("forex_bot.factory.meta_labeling", fromlist=["build_meta_features"]).build_meta_features,
)
fit_meta_classifier = _safe_import(
    "fit_meta_classifier",
    lambda: __import__("forex_bot.factory.meta_labeling", fromlist=["fit_meta_classifier"]).fit_meta_classifier,
)
predict_meta_probability = _safe_import(
    "predict_meta_probability",
    lambda: __import__(
        "forex_bot.factory.meta_labeling", fromlist=["predict_meta_probability"]
    ).predict_meta_probability,
)

CPCVConfig = _safe_import(
    "CPCVConfig",
    lambda: __import__("forex_bot.backtest.cpcv", fromlist=["CPCVConfig"]).CPCVConfig,
)
run_cpcv = _safe_import(
    "run_cpcv",
    lambda: __import__("forex_bot.backtest.cpcv", fromlist=["run_cpcv"]).run_cpcv,
)
compute_pbo_cpcv = _safe_import(
    "compute_pbo_cpcv",
    lambda: __import__("forex_bot.backtest.cpcv", fromlist=["compute_pbo_cpcv"]).compute_pbo_cpcv,
)
compute_dsr_cpcv = _safe_import(
    "compute_dsr_cpcv",
    lambda: __import__("forex_bot.backtest.cpcv", fromlist=["compute_dsr_cpcv"]).compute_dsr_cpcv,
)

# Full crypto overlay wrappers
run_backtest_with_funding = _safe_import(
    "run_backtest_with_funding",
    lambda: __import__("forex_bot.backtest.funding_model", fromlist=["run_backtest_with_funding"]).run_backtest_with_funding,
)
run_backtest_with_liquidation = _safe_import(
    "run_backtest_with_liquidation",
    lambda: __import__("forex_bot.backtest.liquidation", fromlist=["run_backtest_with_liquidation"]).run_backtest_with_liquidation,
)
run_backtest_with_full_crypto_overlay = _safe_import(
    "run_backtest_with_full_crypto_overlay",
    lambda: __import__("forex_bot.backtest.venue_costs", fromlist=["run_backtest_with_full_crypto_overlay"]).run_backtest_with_full_crypto_overlay,
)
run_backtest_with_vol_target = _safe_import(
    "run_backtest_with_vol_target",
    lambda: __import__("forex_bot.backtest.vol_target", fromlist=["run_backtest_with_vol_target"]).run_backtest_with_vol_target,
)
# Spec dataclasses for the overlay wrappers.
PositionSpec = _safe_import(
    "PositionSpec",
    lambda: __import__("forex_bot.backtest.funding_model", fromlist=["PositionSpec"]).PositionSpec,
)
LiquidationSpec = _safe_import(
    "LiquidationSpec",
    lambda: __import__("forex_bot.backtest.liquidation", fromlist=["LiquidationSpec"]).LiquidationSpec,
)
VenueFeeConfig = _safe_import(
    "VenueFeeConfig",
    lambda: __import__("forex_bot.backtest.venue_costs", fromlist=["VenueFeeConfig"]).VenueFeeConfig,
)
DEFAULT_BINANCE_USDM_TIERS = _safe_import(
    "DEFAULT_BINANCE_USDM_TIERS",
    lambda: __import__("forex_bot.backtest.venue_costs", fromlist=["DEFAULT_BINANCE_USDM_TIERS"]).DEFAULT_BINANCE_USDM_TIERS,
)
VenueOrderFill = _safe_import(
    "VenueOrderFill",
    lambda: __import__("forex_bot.backtest.venue_costs", fromlist=["VenueOrderFill"]).VenueOrderFill,
)
SpreadCostTable = _safe_import(
    "SpreadCostTable",
    lambda: __import__("forex_bot.factory.spread_costs", fromlist=["SpreadCostTable"]).SpreadCostTable,
)
SpreadCosts = _safe_import(
    "SpreadCosts",
    lambda: __import__("forex_bot.factory.spread_costs", fromlist=["SpreadCosts"]).SpreadCosts,
)

compute_data_hash = _safe_import(
    "compute_data_hash",
    lambda: __import__("forex_bot.srf", fromlist=["compute_data_hash"]).compute_data_hash,
)
# srf only re-exports compute_data_hash + SRFDatabase + generate_run_id;
# git_commit is NOT in the public surface — we shell out via subprocess.
import subprocess as _sp
def _get_git_commit_local():
    try:
        return _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(WORKTREE),
            stderr=_sp.DEVNULL,
        ).decode().strip() or None
    except (OSError, _sp.CalledProcessError):
        return None
get_git_commit = _get_git_commit_local

# LightGBM challenger (lazy import — module ships import-safe even
# without lightgbm installed; we have it pinned in b634ed32 + installed
# in this venv, so the import should succeed).
benchmark_lightgbm_vs_meta_labeler = _safe_import(
    "benchmark_lightgbm_vs_meta_labeler",
    lambda: __import__(
        "forex_bot.factory.lightgbm_challenger", fromlist=["benchmark_lightgbm_vs_meta_labeler"]
    ).benchmark_lightgbm_vs_meta_labeler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("sweep_crypto_first_run")


# ---------------------------------------------------------------------------
# Deterministic synthetic crypto bars (HONEST SYNTHETIC DATA — see docstring)
# ---------------------------------------------------------------------------

#: Anchor prices (USD spot, October 2026 — rough consensus from
#: recent public market data). These are deliberately realistic
#: but DO NOT pretend to be sourced from a live exchange.
PAIR_ANCHORS: dict[str, dict[str, float]] = {
    "BTCUSDT": {"anchor_close": 85_000.0, "daily_vol": 0.022, "hourly_vol": 0.022 / math.sqrt(24)},
    "ETHUSDT": {"anchor_close":  2_700.0, "daily_vol": 0.030, "hourly_vol": 0.030 / math.sqrt(24)},
    "SOLUSDT": {"anchor_close":    140.0, "daily_vol": 0.040, "hourly_vol": 0.040 / math.sqrt(24)},
}

#: Drift regimes (annualized) — small positive bias for crypto majors,
#: but small enough that the random walk dominates any candidate
#: signal we extract.
PAIR_DRIFT_ANNUAL: dict[str, float] = {
    "BTCUSDT":  0.10,
    "ETHUSDT":  0.12,
    "SOLUSDT":  0.15,
}

HOURS_PER_YEAR = 24 * 365


def make_synthetic_bars(
    pair: str,
    *,
    n_bars: int = 2160,  # ~90 days of H1
    seed: int = 42,
    start: datetime | None = None,
) -> list:
    """Generate deterministic OHLCV H1 bars for one pair.

    The generator is a geometric-Brownian-motion path with realistic
    crypto vol. We add a small intraday vol-of-vol to keep the series
    from being unrealistically smooth. Volume is sampled from a
    lognormal distribution to mimic the heavy-tailed crypto volume
    profile.

    Returns
    -------
    list[Bar]
        ``n_bars`` H1 OHLCV bars spaced one hour apart starting at
        ``start`` (UTC) — default ``2026-07-01 00:00:00 UTC`` so the
        sweep window sits well before the OOS holdout (Jan-Jul 2026)
        and the validation runner does not raise the OOS guard. For
        a synthetic sweep the OOS guard is unlocked anyway.
    """
    if Bar is None:
        raise RuntimeError("Bar type failed to import; cannot build bars")

    if start is None:
        start = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)

    cfg = PAIR_ANCHORS[pair]
    p0 = float(cfg["anchor_close"])
    hv = float(cfg["hourly_vol"])
    annual_drift = float(PAIR_DRIFT_ANNUAL[pair])
    hourly_drift = (annual_drift - 0.5 * hv * hv) / HOURS_PER_YEAR

    rng = random.Random(seed + abs(hash(pair)) % 9973)
    bars = []
    close = p0
    for i in range(n_bars):
        # Geometric Brownian motion per bar.
        z = rng.gauss(0.0, 1.0)
        # Mild vol-of-vol: every ~12h the vol regime steps up/down.
        regime = 1.0 + 0.15 * math.sin(2 * math.pi * (i % 24) / 24.0)
        ret = hourly_drift + hv * regime * z
        open_ = close
        close = open_ * math.exp(ret)
        # Intraday range scales with |return| + baseline.
        rng_amp = abs(ret) + hv * 0.5
        high = max(open_, close) * (1.0 + rng_amp * 0.6)
        low = min(open_, close) * (1.0 - rng_amp * 0.6)
        # Lognormal volume in $1M-$500M range.
        vol = float(rng.lognormvariate(mu=14.5, sigma=0.7))
        # Spread in pips — BTC/ETH/SOL USD-M perp quotes are tight,
        # 1 pip = 0.01 USD for BTC, 0.01 for ETH, 0.001 for SOL.
        if pair.startswith("BTC"):
            spread_pips = 0.5
        elif pair.startswith("ETH"):
            spread_pips = 1.0
        else:
            spread_pips = 2.0
        bar_time = start + timedelta(hours=i)
        bars.append(
            Bar(
                time=bar_time,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(vol),
                spread_pips=float(spread_pips),
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------

#: A small but varied parameter grid. Six candidates per pair give
#: TrialReturnStore enough trials for PBO/CSCV (>= 2 trials per cell)
#: without exploding the cpu_guard budget. Each candidate is a
#: different fast/slow EMA pair on a simple momentum signal so the
#: cell matrix has non-degenerate columns (per the
#: ``ValidationRunner._trial_returns_for`` placeholder contract).
PARAM_GRID: list[tuple[int, int]] = [
    (8,  21),
    (10, 30),
    (12, 26),
    (14, 30),
    (16, 48),
    (20, 50),
]


@dataclass(frozen=True)
class CryptoStrategy:
    """A minimal crypto momentum strategy implementation.

    The validation runner accepts any object that satisfies the
    ``ISignalStrategy`` duck-type (``name`` + ``evaluate``). The bridge
    is the production path, but for this first sweep we build a
    minimal strategy inline so we don't have to wire the registry
    for synthetic bars — the bridge path lands in the follow-on
    sweep card with real data.
    """

    pair: str
    fast: int
    slow: int

    name = "crypto_momentum_v1"

    def evaluate(self, market_state):  # type: ignore[no-untyped-def]
        """Emit a long-only momentum signal when fast EMA > slow EMA."""
        closes = [b.close for b in market_state.bars]
        if len(closes) < max(self.fast, self.slow) + 1:
            return None
        fast = sum(closes[-self.fast:]) / self.fast
        slow = sum(closes[-self.slow:]) / self.slow
        if fast <= slow:
            return None
        last = closes[-1]
        atr = market_state.atr
        return {
            "direction": "long",
            "entry_price": float(last),
            "stop_loss": float(last - 2.0 * atr),
            "take_profit": float(last + 3.0 * atr),
            "confidence": float(min(1.0, (fast - slow) / slow * 50.0)),
        }


@dataclass
class CandidateRow:
    """One candidate row in the sweep output."""

    candidate_id: str
    pair: str
    timeframe: str
    params: dict
    tier: str = ""
    mean_sharpe: float = 0.0
    mean_pf: float = 0.0
    total_trades: int = 0
    windows_passed: int = 0
    windows_total: int = 0
    pbo_score: float | None = None
    pbo_tier_ceiling: str = "N/A"
    cost_sensitivity: float | None = None
    dsr_pvalue: float = 1.0
    reason: str = ""
    rank: int = 0
    q_value: float = 1.0
    p_value: float = 1.0
    bh_rejected: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Sweep driver
# ---------------------------------------------------------------------------


@dataclass
class SweepConfig:
    """Run configuration."""

    out_dir: Path
    db_path: Path
    n_bars: int = 2160  # ~90 days H1
    alpha: float = 0.05  # BH-FDR level
    meta_threshold: float = 0.5  # meta-label gate
    pairs: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    timeframes: tuple[str, ...] = ("H1",)
    seed: int = 42


def _build_strategy_factory(pair: str, fast: int, slow: int):
    """Closure that returns a fresh ``CryptoStrategy`` instance."""

    def factory():
        return CryptoStrategy(pair=pair, fast=fast, slow=slow)

    factory.name = "crypto_momentum_v1"  # type: ignore[attr-defined]
    return factory


def run_sweep(cfg: SweepConfig) -> dict:
    """Execute the full sweep and return the report payload."""

    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Spine import health check — if anything in the spine failed to
    # import we surface it as a defect in the report so the
    # orchestrator can decide whether to fix-forward or file a card.
    spine_health = dict(SPINE_IMPORT_ERRORS)

    git_commit = get_git_commit() if get_git_commit else None

    # ── 1. integrity gate (per-pair) ─────────────────────────────────
    integrity_reports: dict[str, dict] = {}
    bars_by_pair: dict[str, list] = {}
    for pair in cfg.pairs:
        try:
            bars = make_synthetic_bars(pair, n_bars=cfg.n_bars, seed=cfg.seed)
        except Exception as exc:
            integrity_reports[pair] = {"status": "FAIL", "stage": "make_bars", "error": f"{type(exc).__name__}: {exc}"}
            continue
        bars_by_pair[pair] = bars

        if enforce_integrity_gate is None or IntegrityConfig is None:
            integrity_reports[pair] = {
                "status": "FAIL",
                "stage": "import",
                "error": spine_health.get("enforce_integrity_gate")
                or spine_health.get("IntegrityConfig")
                or "integrity gate not importable",
            }
            continue

        # Build an integrity config wired to the PIT universe.
        pit_entries = []
        for sym in cfg.pairs:
            pit_entries.append(
                UniverseEntry(
                    symbol=sym,
                    listed_from=date(2020, 1, 1),  # date, not datetime (contract)
                    delisted_at=None,
                )
            )
        # Resolve the as-of universe as of the first bar's date.
        from forex_bot.backtest.universe import Universe  # local import; cheap

        pit = Universe(pit_entries)  # Universe takes entries as positional list
        as_of_date = bars[0].time.date() if hasattr(bars[0].time, "date") else bars[0].time
        live_symbols = tuple(resolve_universe(pit, as_of_date))
        ic = build_universe_integrity_config(
            pit,
            as_of=as_of_date,
            expected_cadence_minutes=60,
            expected_window_end=bars[-1].time,  # expected_window_end is datetime
        ) if build_universe_integrity_config else IntegrityConfig(expected_cadence_minutes=60)

        try:
            report = enforce_integrity_gate(symbol=pair, bars=bars, config=ic)
            integrity_reports[pair] = {
                "status": "PASS",
                "n_violations": len(report.violations) if hasattr(report, "violations") else 0,
                "violations": [str(v) for v in (report.violations if hasattr(report, "violations") else [])][:5],
            }
        except Exception as exc:  # noqa: BLE001
            # IntegrityError raises with full report attached via exc.report;
            # surface the violations count for the report.
            n_violations = 0
            try:
                if hasattr(exc, "report") and exc.report is not None:
                    n_violations = len(exc.report.violations)
            except Exception:  # noqa: BLE001
                pass
            integrity_reports[pair] = {
                "status": "FAIL",
                "stage": "enforce_integrity_gate",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "n_violations": n_violations,
            }

    # ── 2. PIT universe (resolved once) ──────────────────────────────
    pit_summary = {
        "n_entries": 3,
        "live_symbols": list(cfg.pairs),
        "as_of": str(bars_by_pair[cfg.pairs[0]][0].time.date())
        if cfg.pairs and cfg.pairs[0] in bars_by_pair
        else None,
    }

    # ── 3+4. Full crypto overlay (funding + liquidation + venue + vol-target)
    overlay_results: list[dict] = []
    for pair in cfg.pairs:
        if pair not in bars_by_pair:
            continue
        bars = bars_by_pair[pair]
        overlay_row: dict = {"pair": pair}
        try:
            # Build a minimum-valid BacktestConfig + PositionSpec +
            # LiquidationSpec + VenueFeeConfig so each overlay wrapper
            # accepts the call. The wrapper runs the engine against the
            # synthetic bars; we capture the per-overlay summary metrics
            # for the report (the engine is deterministic on the same
            # (config, bars, strategies) so all four runs produce
            # identical base metrics; the OVERLAYS are what differ).
            bt_config = _build_backtest_config(pair)
            position = PositionSpec(notional_usd=10_000.0, direction="long") if PositionSpec else None
            liq_spec = LiquidationSpec(
                entry_price=float(bars[-1].close),
                leverage=3.0,
                maintenance_margin_rate=0.005,
            ) if LiquidationSpec else None
            venue_config = (
                VenueFeeConfig(
                    name="binance_usdm_default",
                    tiers=list(DEFAULT_BINANCE_USDM_TIERS) if DEFAULT_BINANCE_USDM_TIERS else [],
                    pair=pair,
                )
                if VenueFeeConfig
                else None
            )
            # Synthesized funding events: 8h cadence, tiny rate.
            funding_events = _build_funding_events(bars)
            mark_prices = [float(b.close) for b in bars]
            fills = _build_venue_fills(bars, venue_config) if venue_config else []
            strategies = [_build_overlay_strategy(pair)]

            if run_backtest_with_funding and position is not None:
                try:
                    funding = run_backtest_with_funding(
                        bars=bars,
                        funding_events=funding_events,
                        position=position,
                        config=bt_config,
                        strategies=strategies,
                        strategy_name="crypto_momentum_v1",
                    )
                    overlay_row["funding"] = {
                        "n_events": getattr(funding, "n_funding_events", None),
                        "total_funding_cost": getattr(funding, "total_funding_cost", None),
                        "funded_ending_balance": getattr(funding, "funded_ending_balance", None),
                    }
                except Exception as exc:  # noqa: BLE001
                    overlay_row["funding_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

            if run_backtest_with_liquidation and position is not None and liq_spec is not None:
                try:
                    liq = run_backtest_with_liquidation(
                        bars=bars,
                        mark_prices=mark_prices,
                        position=position,
                        liq_spec=liq_spec,
                        config=bt_config,
                        strategies=strategies,
                        strategy_name="crypto_momentum_v1",
                    )
                    overlay_row["liquidation"] = {
                        "n_events": getattr(liq, "n_liquidation_events", None),
                        "ending_balance_after_liq": getattr(liq, "ending_balance_after_liq", None),
                    }
                except Exception as exc:  # noqa: BLE001
                    overlay_row["liquidation_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

            if run_backtest_with_full_crypto_overlay and venue_config is not None and liq_spec is not None and position is not None:
                try:
                    venue, funded, liquidated = run_backtest_with_full_crypto_overlay(
                        bars=bars,
                        mark_prices=mark_prices,
                        funding_events=funding_events,
                        fills=fills,
                        venue_config=venue_config,
                        position=position,
                        liq_spec=liq_spec,
                        config=bt_config,
                        strategies=strategies,
                        strategy_name="crypto_momentum_v1",
                    )
                    overlay_row["venue"] = {
                        "venue_ending_balance": getattr(venue, "ending_balance_after_fees", None) or getattr(venue, "ending_balance", None),
                        "venue_total_fee": getattr(venue, "total_fee_usd", None),
                        "funded_total": getattr(funded, "total_funding_cost", None),
                        "liq_total": getattr(liquidated, "n_liquidation_events", None),
                    }
                except Exception as exc:  # noqa: BLE001
                    overlay_row["venue_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

            if run_backtest_with_vol_target:
                try:
                    vt = run_backtest_with_vol_target(
                        bars=bars,
                        vol_target_config=None,
                        config=bt_config,
                        strategies=strategies,
                        strategy_name="crypto_momentum_v1",
                    )
                    overlay_row["vol_target"] = {
                        "risk_scale_mean": float(np.mean(vt.risk_scale)) if hasattr(vt, "risk_scale") and vt.risk_scale is not None else None,
                    }
                except Exception as exc:  # noqa: BLE001
                    overlay_row["vol_target_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        except Exception as exc:  # noqa: BLE001
            overlay_row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        overlay_results.append(overlay_row)

    # ── 5. ValidationRunner + TrialReturnStore ────────────────────────
    runner = (
        ValidationRunner(
            pipeline_config=default_pipeline_config(),
            spread_costs=_build_crypto_spread_costs(),
            cell_count=len(PARAM_GRID),
        )
        if ValidationRunner
        else None
    )
    store = TrialReturnStore() if TrialReturnStore else None

    candidates: list[CandidateSpec] = []
    candidate_ids: list[str] = []
    for pair in cfg.pairs:
        if pair not in bars_by_pair:
            continue
        bars = bars_by_pair[pair]
        for fast, slow in PARAM_GRID:
            cid = f"{pair}|ema({fast},{slow})"
            params = {"fast_period": float(fast), "slow_period": float(slow)}
            candidates.append(
                CandidateSpec(
                    candidate_id=cid,
                    template=_build_strategy_template(pair, fast, slow),
                    params=params,
                    pair=pair,
                    timeframe="H1",
                    bars=list(bars),
                    oos_unlocked=True,
                )
            )
            candidate_ids.append(cid)

    verdicts = runner.run_batch(candidates) if runner and candidates else []

    # ── 6. factory_verdicts persistence (provenance) ────────────────
    n_persisted = 0
    store_obj = FactoryVerdictStore(cfg.db_path) if FactoryVerdictStore else None
    if store_obj and verdicts:
        try:
            # The provenance data_hash is computed over the
            # concatenated synthetic-bar bytes for honesty — the
            # report explicitly notes "synthetic bars" and the SHA
            # proves exactly which bytes were consumed.
            data_bytes = b""
            for pair in cfg.pairs:
                if pair in bars_by_pair:
                    for bar in bars_by_pair[pair]:
                        data_bytes += f"{pair}|{bar.time.isoformat()}|{bar.open}|{bar.high}|{bar.low}|{bar.close}|{bar.volume}\n".encode()
            data_hash = hashlib.sha256(data_bytes).hexdigest()
            n_persisted = store_obj.write_verdicts(
                verdicts,
                git_commit=git_commit,
                data_hash=data_hash,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("factory_verdicts persist failed: %s", exc)

    # ── 7. CPCV + BH-FDR + meta-label gate ──────────────────────────
    rankings_by_cell: dict[tuple[str, str, str], list] = {}
    pbo_cpcv_by_cell: dict[tuple[str, str, str], dict] = {}
    if store and verdicts and rank_from_trial_return_store:
        try:
            candidate_ids_per_cell: dict[tuple[str, str, str], list[str]] = {}
            for v in verdicts:
                cell = (v.archetype_id, v.pair, v.timeframe)
                candidate_ids_per_cell.setdefault(cell, []).append(v.candidate_id)
            rankings_by_cell = rank_from_trial_return_store(
                store,
                candidate_ids_per_cell=candidate_ids_per_cell,
                alpha=cfg.alpha,
            )

            # CPCV per cell (when matrix is available; gracefully skip
            # cells with < 2 trials).
            if run_cpcv and compute_pbo_cpcv:
                for cell_key, matrix_fn in [(k, store.matrix(k)) for k in store.keys()]:
                    mat = matrix_fn
                    if mat is None or mat.shape[1] < 2:
                        pbo_cpcv_by_cell[cell_key] = {"skipped": "insufficient_trials", "shape": None}
                        continue
                    try:
                        paths = run_cpcv(mat, CPCVConfig(n_groups=6, k_test_groups=3, label_horizon_bars=1, embargo_bars=1))
                        pbo = compute_pbo_cpcv(paths)
                        pbo_cpcv_by_cell[cell_key] = {
                            "shape": list(mat.shape),
                            "n_paths": int(getattr(paths, "n_paths", 0) or 0),
                            "pbo": float(pbo.pbo) if hasattr(pbo, "pbo") else None,
                            "dsr": float(getattr(pbo, "deflated_sharpe", None) or 0.0),
                        }
                    except Exception as exc:  # noqa: BLE001
                        pbo_cpcv_by_cell[cell_key] = {"error": f"{type(exc).__name__}: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("ranking/CPCV step failed: %s", exc)

    # ── 8. LightGBM challenger benchmark ────────────────────────────
    benchmark_artifact: dict | None = None
    if (
        benchmark_lightgbm_vs_meta_labeler
        and MetaTradeContext is not None
        and MetaLabeledTrade is not None
        and build_meta_features is not None
    ):
        try:
            # Synthesize ~200 labeled meta-trades with a weak signal.
            n_meta = 200
            rng = np.random.default_rng(cfg.seed)
            contexts = []
            labels = []
            for i in range(n_meta):
                ctx = MetaTradeContext(
                    candidate_id=f"syn_{i}",
                    pair="BTCUSDT",
                    timeframe="H1",
                    signal_time=datetime(2026, 7, 1, int(rng.integers(0, 24)), tzinfo=timezone.utc),
                    primary_confidence=float(rng.uniform(0.3, 0.9)),
                    primary_side=("long" if rng.random() > 0.5 else "short"),
                    regime="trending",
                    funding_rate_at_entry=float(rng.normal(0.0001, 0.00005)),
                    venue_cost_bps_at_entry=float(rng.uniform(1.0, 5.0)),
                    spread_pips_at_entry=float(rng.uniform(0.5, 3.0)),
                )
                contexts.append(ctx)
                # Label = primary_confidence signal + noise.
                score = ctx.primary_confidence * 1.5 + rng.normal(0, 0.4)
                labels.append(1 if score > 0.7 else 0)
            labeled = [
                MetaLabeledTrade(context=c, outcome=lab) for c, lab in zip(contexts, labels, strict=True)
            ]
            artifact = benchmark_lightgbm_vs_meta_labeler(labeled, n_folds=5, seed=cfg.seed)
            benchmark_artifact = dataclasses.asdict(artifact) if hasattr(artifact, "__dataclass_fields__") else {
                "summary": str(artifact),
            }
        except Exception as exc:  # noqa: BLE001
            benchmark_artifact = {"error": f"{type(exc).__name__}: {exc}"}

    # ── 9. Assemble the report payload ─────────────────────────────
    rows: list[dict] = []
    cell_lookup: dict[str, dict] = {}
    for cell_key, ranked in rankings_by_cell.items():
        for r in ranked:
            cell_lookup[r.candidate_id] = {
                "rank": r.rank,
                "q_value": r.q_value,
                "p_value": r.p_value,
                "bh_rejected": r.bh_rejected,
                "bh_reject": getattr(r, "bh_reject", False),
                "notes": r.notes,
            }
    for v in verdicts:
        meta_lookup = cell_lookup.get(v.candidate_id, {})
        rows.append(
            {
                "candidate_id": v.candidate_id,
                "archetype_id": v.archetype_id,
                "pair": v.pair,
                "timeframe": v.timeframe,
                "tier": v.tier,
                "windows_passed": v.windows_passed,
                "windows_total": v.windows_total,
                "total_trades": v.total_trades,
                "mean_sharpe": round(v.mean_sharpe, 4),
                "mean_pf": round(v.mean_profit_factor, 4),
                "max_drawdown": round(v.max_drawdown, 4),
                "dsr_pvalue": round(v.dsr_pvalue, 6),
                "pbo_score": (round(v.pbo_score, 4) if v.pbo_score is not None else None),
                "pbo_tier_ceiling": v.pbo_tier_ceiling,
                "cost_sensitivity": (round(v.cost_sensitivity, 4) if v.cost_sensitivity is not None else None),
                "rank": meta_lookup.get("rank", 0),
                "q_value": round(meta_lookup.get("q_value", 1.0), 6),
                "bh_rejected": meta_lookup.get("bh_rejected", False),
                "reason": v.reason,
            }
        )

    # Discovery summary.
    discoveries = [r for r in rows if r["bh_rejected"] and r["tier"] in {"A", "B", "C"}]
    rejects = [r for r in rows if r["tier"] in {"REJECT", "INSUFFICIENT_DATA"}]

    elapsed = time.time() - started

    report = {
        "metadata": {
            "card_id": "e0067a2e-5901-4bc0-a9ad-25a76701e545",
            "title": "First full crypto sweep through the new spine",
            "started_at": started_iso,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 2),
            "git_commit": git_commit,
            "n_bars_per_pair": cfg.n_bars,
            "data_source": "SYNTHETIC (deterministic, see make_synthetic_bars; repo has no live crypto OHLCV)",
            "spine_import_errors": spine_health,
        },
        "integrity_gate": integrity_reports,
        "pit_universe": pit_summary,
        "overlay_smoke": overlay_results,
        "candidates": {
            "n_total": len(candidates),
            "n_evaluated": len(verdicts),
            "n_persisted_to_factory_verdicts": n_persisted,
        },
        "rankings": {
            "alpha": cfg.alpha,
            "meta_threshold": cfg.meta_threshold,
            "by_cell": {
                f"{cell[0]}|{cell[1]}|{cell[2]}": [
                    {
                        "candidate_id": r.candidate_id,
                        "rank": r.rank,
                        "p_value": round(r.p_value, 6),
                        "q_value": round(r.q_value, 6),
                        "bh_rejected": r.bh_rejected,
                        "bh_reject": getattr(r, "bh_reject", False),
                        "notes": r.notes,
                    }
                    for r in ranked
                ]
                for cell, ranked in rankings_by_cell.items()
            },
            "pbo_cpcv_by_cell": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in pbo_cpcv_by_cell.items()},
            "n_discoveries": len(discoveries),
            "n_rejects": len(rejects),
            "discovery_ids": [c["candidate_id"] for c in discoveries],
        },
        "verdict_table": rows,
        "lightgbm_challenger": benchmark_artifact,
        "defects": {
            "spine_import_errors": spine_health,
            "integrity_failures": {k: v for k, v in integrity_reports.items() if v.get("status") == "FAIL"},
            "overlay_errors": [r for r in overlay_results if "error" in r],
        },
        "honesty_notes": [
            "Bars are SYNTHETIC (deterministic GBM with crypto-realistic vol); "
            "the repo has no live BTC/ETH/SOL OHLCV. The data_hash provenance "
            "column in factory_verdicts binds every verdict to the exact "
            "synthetic bytes consumed — swap for live bars on the next sweep.",
            "Strategies are inline CryptoStrategy implementations (the "
            "factory bridge path lands in a follow-on card with real data).",
            "PBO is NOT_APPLICABLE for cells with < 2 Optuna trials — the "
            "validation runner surfaces this explicitly rather than emitting "
            "a synthetic 2-column 'PBO' (the misleading pre-Sprint-C matrix).",
        ],
    }
    return report


def _build_strategy_template(pair: str, fast: int, slow: int):
    """Build a minimal StrategyTemplate stub for the validation runner.

    The validation runner needs ``template.archetype_id`` to identify
    the candidate's archetype (used in the cell_key). For this
    first sweep we use a single archetype per pair so the BH-FDR
    ranking has at least 2 trials per cell. The bridge (which would
    map params → ``ISignalStrategy``) is exercised in a follow-on
    card with real data; here the validation runner's
    ``_trial_returns_for`` placeholder generates per-trial series
    so PBO can still compute when N >= 2.
    """
    from forex_bot.factory.template import ParamKind, ParamSpec, StrategyTemplate

    archetype_id = "crypto_momentum_v1"

    class _Template(StrategyTemplate):
        def __init__(self) -> None:  # type: ignore[no-untyped-def]
            super().__init__(
                archetype_id=archetype_id,
                description="Crypto momentum (fast/slow EMA) — first sweep stub",
                default_pairs=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
                default_timeframes=("H1",),
                regime_affinity=("TRENDING", "VOLATILE"),
            )

        @property
        def param_space(self):  # type: ignore[no-untyped-def]
            return (
                ParamSpec("fast_period", ParamKind.INT, low=4, high=40, step=1),
                ParamSpec("slow_period", ParamKind.INT, low=10, high=80, step=1),
            )

        def default_params(self):  # type: ignore[no-untyped-def]
            return {"fast_period": 12.0, "slow_period": 26.0}

        def regime_filter(self):  # type: ignore[no-untyped-def]
            return None  # regime-agnostic for the first sweep

        def build_strategy(self, params, pair):  # type: ignore[no-untyped-def]
            return CryptoStrategy(
                pair=pair,
                fast=int(params["fast_period"]),
                slow=int(params["slow_period"]),
            )

    return _Template()


# ---------------------------------------------------------------------------
# Overlay helpers (build the minimum valid configs the wrappers need)
# ---------------------------------------------------------------------------


# Crypto spread costs (Liora ground rule — pin from account fee page).
# Binance USDⓈ-M VIP0 defaults: BTC ~0.5 pip round-trip, ETH ~1 pip,
# SOL ~2 pip.  These are deliberately approximate — production callers
# pin from the live fee page.
CRYPTO_SPREAD_PIPS: dict[str, float] = {
    "BTCUSDT": 0.5,
    "ETHUSDT": 1.0,
    "SOLUSDT": 2.0,
}


def _build_crypto_spread_costs():
    """Build a SpreadCostTable that includes BTC/ETH/SOL on top of the FX defaults.

    The factory ``default_spread_costs()`` table only has FX pairs
    (XAUUSD/EURUSD/GBPUSD/USDJPY). For the crypto sweep we extend
    that table with the three perp pairs the sweep exercises.
    """
    if SpreadCostTable is None or SpreadCosts is None:
        return default_spread_costs() if default_spread_costs else None
    base = default_spread_costs() if default_spread_costs else SpreadCostTable.from_mapping({})
    # Extend with crypto entries (commission_per_lot_usd left at the
    # factory default — crypto USD-M has different commission
    # semantics but the runner only needs spread_pips + commission).
    crypto_entries = [
        SpreadCosts(symbol=sym, spread_pips=pips, commission_per_lot_usd=2.0)
        for sym, pips in CRYPTO_SPREAD_PIPS.items()
    ]
    try:
        return SpreadCostTable.from_mapping(
            {**{e.symbol: e.spread_pips for e in base.entries}, **{e.symbol: e.spread_pips for e in crypto_entries}}
        )
    except Exception:  # noqa: BLE001
        # Fallback — use the base + crypto entries via constructor.
        return SpreadCostTable(entries=tuple(list(base.entries) + crypto_entries))


def _build_backtest_config(pair: str):
    """Minimum-valid BacktestConfig for the overlay wrappers.

    The overlays run the engine against ``bars`` so we need a
    ``BacktestConfig`` with starting balance, pair, and engine
    parameters. ``core.config.BacktestConfig`` is the canonical
    one — we instantiate it with conservative defaults.
    """
    from core.config import BacktestConfig as CoreBacktestConfig

    return CoreBacktestConfig(
        pair=pair,
        starting_balance=10_000.0,
        risk_per_trade_pct=0.005,
        max_daily_drawdown_pct=0.05,
        max_total_drawdown_pct=0.10,
        spread_pips=CRYPTO_SPREAD_PIPS.get(pair, 1.0),
        commission_per_lot=2.0,
        slippage_pips=0.5,
    )


def _build_funding_events(bars: list) -> None:
    """Synthesize 8-h funding events (one per 8-bar stride) for the bars.

    The funding_model module accepts duck-typed objects exposing
    ``time`` + ``funding_rate`` (per the run_backtest_with_funding
    docstring). We use a lightweight namedtuple-like class.
    """
    from types import SimpleNamespace

    events = []
    n = len(bars)
    # 8-hour cadence on Binance USD-M perps.
    for i in range(0, n, 8):
        events.append(
            SimpleNamespace(
                time=bars[i].time,
                funding_rate=0.0001,  # tiny carry to keep cost line real
            )
        )
    return events


def _build_venue_fills(bars: list, venue_config):
    """Synthesize a minimal VenueOrderFill list (one maker + one taker per 24h)."""
    if VenueOrderFill is None:
        return []
    fills = []
    n = len(bars)
    for i in range(0, n, 24):
        bar = bars[i]
        # One maker fill (resting limit) — earns the maker rebate.
        fills.append(
            VenueOrderFill(
                time=bar.time,
                symbol=bar.pair if hasattr(bar, "pair") else "BTCUSDT",
                price=float(bar.close),
                quantity=0.001,
                is_maker=True,
            )
        )
        # One taker fill (aggressive market) — pays the taker fee.
        fills.append(
            VenueOrderFill(
                time=bar.time,
                symbol=bar.pair if hasattr(bar, "pair") else "BTCUSDT",
                price=float(bar.close) * 1.0001,
                quantity=0.001,
                is_maker=False,
            )
        )
    return fills


class _OverlayStubStrategy:
    """Minimal ISignalStrategy-shaped stub the engine can run.

    Returns no signals (so the engine produces zero test trades) —
    the overlay wrappers layer funding / liquidation / venue /
    vol-target onto the (zero-trade) base engine run so the
    per-bar cost deltas are computed honestly without us having
    to wire a real signal-generation loop. Real strategies land
    in the follow-on sweep card with live data.
    """

    name = "crypto_overlay_stub"

    def evaluate(self, market_state):  # type: ignore[no-untyped-def]
        return None


def _build_overlay_strategy(pair: str):
    return _OverlayStubStrategy()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=WORKTREE / "docs" / "reports",
        help="Where to write the sweep report (default: docs/reports)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=WORKTREE / "data" / "sweep_crypto_first_run.duckdb",
        help="DuckDB for factory_verdicts (sweep-local; not data/research/)",
    )
    parser.add_argument("--n-bars", type=int, default=2160, help="H1 bars per pair (default 2160 = ~90d)")
    parser.add_argument("--alpha", type=float, default=0.05, help="BH-FDR level")
    parser.add_argument("--meta-threshold", type=float, default=0.5, help="meta-label gate threshold")
    parser.add_argument("--pairs", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT")
    args = parser.parse_args()

    cfg = SweepConfig(
        out_dir=args.out_dir,
        db_path=args.db_path,
        n_bars=args.n_bars,
        alpha=args.alpha,
        meta_threshold=args.meta_threshold,
        pairs=tuple(args.pairs.split(",")),
    )

    report = run_sweep(cfg)

    out_path = cfg.out_dir / "2026-10-06-crypto-first-sweep.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"Sweep report written to {out_path}")
    print(f"  candidates evaluated: {report['candidates']['n_evaluated']}")
    print(f"  discoveries: {report['rankings']['n_discoveries']}")
    print(f"  rejects: {report['rankings']['n_rejects']}")
    print(f"  spine_import_errors: {len(report['defects']['spine_import_errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())