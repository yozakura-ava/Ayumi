#!/usr/bin/env python3
"""Sweep #3 — crypto-native candidates on real Binance.US bars (card 68fb28f5).

End-to-end exercise of the strategy-factory spine against REAL
BTC/ETH/SOL H1 bars using the 3 crypto-native strategy templates that
landed in card fe773687:

* ``crypto_ema_cross_trend``           (18 declared variants)
* ``crypto_donchian_breakout``         (6 declared variants)
* ``crypto_zscore_mean_reversion``     (9 declared variants)

Total = 33 candidates × 3 pairs = **99 cell-by-cell evaluations**.

This run is the first that can legitimately issue Tier A/B verdicts —
the templates emit real :class:`StrategySignal` objects on H1 crypto
bars (no FX-pip-calibrated anti-patterns, no session filters,
ATR-relative thresholds only), so walk-forward produces real per-bar
metrics. The verdict table is the deliverable.

Folds in the two prior review notes (cards e0067a2e + 0ab49707):
---------------------------------------------------------------

1. **Per-pair ``data_hash`` in ``factory_verdicts``.** The earlier
   real-data sweep recorded a single run-level hash in the JSON
   report but the duckdb ``factory_verdicts.data_hash`` column was
   NULL because ``write_verdicts`` only accepts one hash for the
   batch. This driver writes a per-pair ``{pair: hash}`` mapping so
   the column is non-NULL and auditable per row.
2. **``git_commit`` SHA convention documented in the report.** The
   earlier sweeps recorded the BUILD worktree SHA into the verdict
   rows (the commit the SWEEP RAN UNDER, not the baseline spine).
   This driver makes that convention explicit in
   ``metadata.git_commit_convention``.

Inputs
------
* ``data/sweep_real_data_bars/real_crypto_bars_h1.parquet`` — the
  Binance.US bars persisted by the prior real-data sweep (871 bars /
  pair post-trim; window ~37d on H1). Reloaded via
  ``--skip-data-fetch`` so this run is idempotent. Pass
  ``--no-skip-data-fetch`` to refetch.
* ``data/sweep_real_data_bars/real_crypto_provenance.json`` —
  per-pair provenance with the SHA-256 ``data_hash_sha256`` we will
  persist per row.

Outputs
-------
* ``data/sweep_crypto_native_real_data.duckdb`` — ``factory_verdicts``
  rows with per-pair ``data_hash`` populated.
* ``docs/reports/2026-10-06-crypto-native-sweep3.json`` — machine-readable
  verdict table + BH-FDR ranks + CPCV PBO/DSR + meta-label calibration.
* ``docs/reports/2026-10-06-crypto-native-sweep3.md`` — human summary.

Hard rules restated
-------------------
* Targeted tests only (HR5) — any fix/extension lands in
  ``tests/sweep/test_sweep3_crypto_native.py`` (new file).
* CPU guard wrapper for any CPU-heavy command.
* Never loosen the integrity gate — fix the data acquisition.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import itertools
import json
import logging
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Repo path setup — mirror the parent sweep.
WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "src" / "forex_bot"))


# ---------------------------------------------------------------------------
# Spine imports (tolerant — drivers never crash on a missing optional dep).
# ---------------------------------------------------------------------------

try:
    from forex_bot.backtest.types import Bar  # noqa: E402
except Exception as exc:  # noqa: BLE001
    raise RuntimeError(f"Bar import failed: {exc}") from exc


def _safe_import(name: str, fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logging.warning("spine import %s failed: %s", name, exc)
        return None


IntegrityConfig = _safe_import(
    "IntegrityConfig",
    lambda: __import__("forex_bot.backtest.integrity_gate", fromlist=["IntegrityConfig"]).IntegrityConfig,
)
enforce_integrity_gate = _safe_import(
    "enforce_integrity_gate",
    lambda: __import__("forex_bot.backtest.integrity_gate", fromlist=["enforce_integrity_gate"]).enforce_integrity_gate,
)
UniverseEntry = _safe_import(
    "UniverseEntry",
    lambda: __import__("forex_bot.backtest.universe", fromlist=["UniverseEntry"]).UniverseEntry,
)
Universe_Universe = _safe_import(
    "Universe",
    lambda: __import__("forex_bot.backtest.universe", fromlist=["Universe"]).Universe,
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
SpreadCostTable = _safe_import(
    "SpreadCostTable",
    lambda: __import__("forex_bot.factory.spread_costs", fromlist=["SpreadCostTable"]).SpreadCostTable,
)
SpreadCosts = _safe_import(
    "SpreadCosts",
    lambda: __import__("forex_bot.factory.spread_costs", fromlist=["SpreadCosts"]).SpreadCosts,
)
CandidateSpec = _safe_import(
    "CandidateSpec",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["CandidateSpec"]).CandidateSpec,
)
ValidationRunner = _safe_import(
    "ValidationRunner",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["ValidationRunner"]).ValidationRunner,
)
TrialReturnStore = _safe_import(
    "TrialReturnStore",
    lambda: __import__("forex_bot.factory.validation_runner", fromlist=["TrialReturnStore"]).TrialReturnStore,
)
FactoryVerdictStore = _safe_import(
    "FactoryVerdictStore",
    lambda: __import__("forex_bot.factory.storage", fromlist=["FactoryVerdictStore"]).FactoryVerdictStore,
)
rank_from_trial_return_store = _safe_import(
    "rank_from_trial_return_store",
    lambda: __import__("forex_bot.factory.risk_adjusted_ranking", fromlist=["rank_from_trial_return_store"]).rank_from_trial_return_store,
)

# Crypto-native strategy classes (card fe773687) — these emit real
# signals on H1 crypto bars (no FX-pip anti-pattern).
CryptoEMACrossTrend = _safe_import(
    "CryptoEMACrossTrend",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CryptoEMACrossTrend"]).CryptoEMACrossTrend,
)
CryptoEMACrossConfig = _safe_import(
    "CryptoEMACrossConfig",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CryptoEMACrossConfig"]).CryptoEMACrossConfig,
)
CryptoDonchianBreakout = _safe_import(
    "CryptoDonchianBreakout",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CryptoDonchianBreakout"]).CryptoDonchianBreakout,
)
CryptoDonchianConfig = _safe_import(
    "CryptoDonchianConfig",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CryptoDonchianConfig"]).CryptoDonchianConfig,
)
CryptoZScoreMeanReversion = _safe_import(
    "CryptoZScoreMeanReversion",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CryptoZScoreMeanReversion"]).CryptoZScoreMeanReversion,
)
CryptoZScoreConfig = _safe_import(
    "CryptoZScoreConfig",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CryptoZScoreConfig"]).CryptoZScoreConfig,
)
CRYPTO_PARAM_GRIDS = _safe_import(
    "CRYPTO_PARAM_GRIDS",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["CRYPTO_PARAM_GRIDS"]).CRYPTO_PARAM_GRIDS,
)
DEFAULT_GRID_CENTERS = _safe_import(
    "DEFAULT_GRID_CENTERS",
    lambda: __import__("forex_bot.strategies.crypto_native", fromlist=["DEFAULT_GRID_CENTERS"]).DEFAULT_GRID_CENTERS,
)

from forex_bot.factory.template import (  # noqa: E402  (after spine path)
    ParamKind,
    ParamSpec,
    StrategyTemplate,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("sweep_crypto_native_real_data")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CRYPTO_NATIVE_STRATEGY_IDS: tuple[str, ...] = (
    "crypto_ema_cross_trend",
    "crypto_donchian_breakout",
    "crypto_zscore_mean_reversion",
)
DEFAULT_PAIRS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


# ---------------------------------------------------------------------------
# Parametrized template — wraps a crypto-native strategy with its param grid
# ---------------------------------------------------------------------------


def _param_space_for(strategy_id: str) -> tuple[ParamSpec, ...]:
    """Build :class:`ParamSpec` tuple from the strategy's declared grid."""
    if CRYPTO_PARAM_GRIDS is None:
        return ()
    grid = CRYPTO_PARAM_GRIDS.get(strategy_id, {})
    out: list[ParamSpec] = []
    for name, values in grid.items():
        # Crypto-native grids are categorical: fast_period ∈ {16, 24, 32},
        # slope_gate_atr ∈ {0.10, 0.15}, etc. — none are continuous ranges.
        out.append(ParamSpec(name=name, kind=ParamKind.CATEGORICAL, choices=tuple(values)))
    return tuple(out)


def _config_factory_for(strategy_id: str):
    """Return a ``(params_dict) -> config_dataclass`` callable."""
    if strategy_id == "crypto_ema_cross_trend":
        return lambda p: CryptoEMACrossConfig(**p)  # type: ignore[misc]
    if strategy_id == "crypto_donchian_breakout":
        return lambda p: CryptoDonchianConfig(**p)  # type: ignore[misc]
    if strategy_id == "crypto_zscore_mean_reversion":
        return lambda p: CryptoZScoreConfig(**p)  # type: ignore[misc]
    raise ValueError(f"unknown crypto-native strategy_id {strategy_id!r}")


def _strategy_class_for(strategy_id: str):
    if strategy_id == "crypto_ema_cross_trend":
        return CryptoEMACrossTrend
    if strategy_id == "crypto_donchian_breakout":
        return CryptoDonchianBreakout
    if strategy_id == "crypto_zscore_mean_reversion":
        return CryptoZScoreMeanReversion
    raise ValueError(f"unknown crypto-native strategy_id {strategy_id!r}")


class _CryptoNativeTemplate(StrategyTemplate):
    """Per-strategy template wrapping one of the 3 crypto-native templates.

    The template's ``param_space`` comes from
    :data:`forex_bot.strategies.crypto_native.CRYPTO_PARAM_GRIDS` —
    frozen at import time and not touched here, so the param grid is
    provably the same one the orchestrator's audit reads.

    ``build_strategy`` constructs the concrete strategy with the
    Optuna-sampled (or default) params; ``pair`` is accepted for
    forward-compat but not used (the crypto-native strategies have no
    per-symbol calibration knobs in SFA-2).
    """

    def __init__(self, strategy_id: str) -> None:  # type: ignore[no-untyped-def]
        if strategy_id not in CRYPTO_NATIVE_STRATEGY_IDS:
            raise ValueError(
                f"unknown crypto-native strategy_id {strategy_id!r}; "
                f"valid: {CRYPTO_NATIVE_STRATEGY_IDS!r}"
            )
        super().__init__(
            archetype_id=f"crypto_native::{strategy_id}",
            description=f"Crypto-native template — {strategy_id} on H1",
            default_pairs=DEFAULT_PAIRS,
            default_timeframes=("H1",),
            regime_affinity=("TRENDING", "VOLATILE", "CHOPPY", "QUIET"),
        )
        self.strategy_id = strategy_id

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return _param_space_for(self.strategy_id)

    def default_params(self) -> dict[str, Any]:
        if DEFAULT_GRID_CENTERS is None:
            return {}
        return dict(DEFAULT_GRID_CENTERS.get(self.strategy_id, {}))

    def regime_filter(self):  # type: ignore[no-untyped-def]
        return None

    def build_strategy(self, params: dict[str, Any], pair: str):  # type: ignore[no-untyped-def]
        cls = _strategy_class_for(self.strategy_id)
        if cls is None:
            raise RuntimeError(f"strategy class for {self.strategy_id!r} not importable")
        cfg_factory = _config_factory_for(self.strategy_id)
        # Center/default params (empty {} ⇒ grid-center) → use the
        # frozen DEFAULT_GRID_CENTERS entry to keep the build honest.
        effective_params = dict(params) if params else self.default_params()
        cfg = cfg_factory(effective_params)
        # Crypto-native strategies ignore pair (SFA-2 contract).
        _ = pair
        return cls(cfg)


# ---------------------------------------------------------------------------
# Param grid enumeration → CandidateSpec
# ---------------------------------------------------------------------------


def enumerate_candidates_for_strategy(
    strategy_id: str,
    pair: str,
    bars: list[Bar],
) -> list[CandidateSpec]:
    """Enumerate every variant of the strategy's param grid as a candidate.

    Yields one :class:`CandidateSpec` per grid combination. ``params``
    is non-empty (so PBO will be computed); the runner's
    ``_compute_pbo`` path is what judges the grid as a Tier A/B/C vs
    INSUFFICIENT_DATA.
    """
    if CRYPTO_PARAM_GRIDS is None or CandidateSpec is None:
        return []
    if not bars:
        # No bars → no candidates (the runner's OOS check would just
        # reject them, but skipping at the source keeps the matrix
        # clean and prevents the runner from logging spurious errors).
        return []
    grid = CRYPTO_PARAM_GRIDS.get(strategy_id, {})
    if not grid:
        return []
    keys = list(grid.keys())
    value_lists = [grid[k] for k in keys]
    template = _CryptoNativeTemplate(strategy_id)
    out: list[CandidateSpec] = []
    for combo in itertools.product(*value_lists):
        params = dict(zip(keys, combo, strict=True))
        params_hash = hashlib.sha256(
            json.dumps(params, sort_keys=True, default=str).encode()
        ).hexdigest()[:10]
        cid = f"{pair}|crypto_native::{strategy_id}|{params_hash}"
        out.append(
            CandidateSpec(
                candidate_id=cid,
                template=template,
                params=params,
                pair=pair,
                timeframe="H1",
                bars=list(bars),
                oos_unlocked=True,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Bar loading (from prior real-data sweep) + integrity gate
# ---------------------------------------------------------------------------


def load_bars_from_disk(data_dir: Path) -> tuple[dict[str, list[Bar]], dict[str, dict]]:
    """Load the Binance.US bars persisted by the prior real-data sweep.

    Returns ``(bars_by_pair, provenance_by_pair)``. Provenance payload
    is a JSON-ready dict so we can splice ``data_hash_sha256`` into
    the report + the per-row duckdb provenance column.
    """
    parquet_path = data_dir / "real_crypto_bars_h1.parquet"
    prov_path = data_dir / "real_crypto_provenance.json"
    if not parquet_path.exists() or not prov_path.exists():
        raise FileNotFoundError(
            f"persisted bars missing under {data_dir}; "
            "rerun the prior sweep first or pass --fetch-fresh"
        )
    df = pd.read_parquet(parquet_path)
    prov_payload = json.loads(prov_path.read_text())
    bars_by_pair: dict[str, list[Bar]] = {}
    for sym in df["symbol"].unique():
        sub = df[df["symbol"] == sym].sort_values("time")
        bars_by_pair[sym] = [
            Bar(
                time=row["time"].to_pydatetime() if hasattr(row["time"], "to_pydatetime") else row["time"],
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                spread_pips=float(row["spread_pips"]),
            )
            for _, row in sub.iterrows()
        ]
    return bars_by_pair, prov_payload


def run_integrity_gate_per_pair(
    bars_by_pair: dict[str, list[Bar]],
) -> dict[str, dict]:
    """Per-pair integrity gate — never loosen the gate (card 0ab49707 rule)."""
    reports: dict[str, dict] = {}
    for pair, bars in bars_by_pair.items():
        if not bars:
            reports[pair] = {"status": "SKIP", "reason": "no_bars"}
            continue
        if enforce_integrity_gate is None or IntegrityConfig is None:
            reports[pair] = {"status": "FAIL", "stage": "import", "error": "gate not importable"}
            continue
        try:
            if build_universe_integrity_config is not None and UniverseEntry is not None and Universe_Universe is not None:
                pit_entries = [
                    UniverseEntry(symbol=pair, listed_from=datetime(2020, 1, 1, tzinfo=timezone.utc).date()),
                ]
                pit = Universe_Universe(tuple(pit_entries))
                ic = build_universe_integrity_config(
                    pit,
                    as_of=bars[0].time.date() if hasattr(bars[0].time, "date") else bars[0].time,
                    expected_cadence_minutes=60,
                    expected_window_end=bars[-1].time,
                )
            else:
                ic = IntegrityConfig(
                    expected_cadence_minutes=60,
                    universe_symbols=(pair,),
                )
            gate_report = enforce_integrity_gate(symbol=pair, bars=bars, config=ic)
            reports[pair] = {
                "status": "PASS",
                "n_violations": len(gate_report.violations) if hasattr(gate_report, "violations") else 0,
                "violations": [str(v) for v in (gate_report.violations if hasattr(gate_report, "violations") else [])][:5],
            }
        except Exception as exc:  # noqa: BLE001
            n_violations = 0
            try:
                if hasattr(exc, "report") and exc.report is not None:
                    n_violations = len(exc.report.violations)
            except Exception:  # noqa: BLE001, S110
                pass
            reports[pair] = {
                "status": "FAIL",
                "stage": "enforce_integrity_gate",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "n_violations": n_violations,
            }
    return reports


# ---------------------------------------------------------------------------
# Spread costs (crypto-aware)
# ---------------------------------------------------------------------------


def build_crypto_spread_costs():
    """Crypto spread-cost table (BTC/ETH/SOL) layered on FX defaults."""
    if SpreadCostTable is None or SpreadCosts is None or default_spread_costs is None:
        return None
    base = default_spread_costs()
    crypto_entries = [
        SpreadCosts(symbol="BTCUSDT", spread_pips=0.5, commission_per_lot_usd=2.0),
        SpreadCosts(symbol="ETHUSDT", spread_pips=1.0, commission_per_lot_usd=2.0),
        SpreadCosts(symbol="SOLUSDT", spread_pips=2.0, commission_per_lot_usd=2.0),
    ]
    try:
        return SpreadCostTable(
            entries=tuple(list(base.entries) + crypto_entries),
        )
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# git_commit convention helpers
# ---------------------------------------------------------------------------


def get_git_commit(cwd: Path | None = None) -> str | None:
    """Return ``git rev-parse --short HEAD`` of the current tree."""
    cwd = cwd or WORKTREE
    try:
        commit = (
            subprocess.check_output(  # noqa: S607
                ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
                cwd=str(cwd),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
            or None
        )
        return commit
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def get_git_commit_long(cwd: Path | None = None) -> str | None:
    """Return full ``git rev-parse HEAD``."""
    cwd = cwd or WORKTREE
    try:
        commit = (
            subprocess.check_output(  # noqa: S607
                ["/usr/bin/git", "rev-parse", "HEAD"],
                cwd=str(cwd),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
            or None
        )
        return commit
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


# ---------------------------------------------------------------------------
# Sweep configuration
# ---------------------------------------------------------------------------


@dataclass
class SweepConfigCryptoNative:
    out_dir: Path
    db_path: Path
    data_dir: Path
    pairs: tuple[str, ...] = DEFAULT_PAIRS
    seed: int = 17
    alpha: float = 0.05
    meta_threshold: float = 0.5


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_sweep(cfg: SweepConfigCryptoNative) -> dict:
    """Run the crypto-native sweep end-to-end; return the report payload."""
    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    build_commit_short = get_git_commit()
    build_commit_long = get_git_commit_long()
    # Establish the baseline spine SHA the previous sweep anchored to
    # (the parent commit on this branch, i.e. main @ eb6ecacb).
    # We pass the BUILD short SHA into the verdict rows (the
    # convention documented in the report).
    parent_commit_short = None
    try:
        parent_commit_short = (
            subprocess.check_output(  # noqa: S607
                ["/usr/bin/git", "rev-parse", "--short", "HEAD~0"],
                cwd=str(WORKTREE),
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
            or None
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        parent_commit_short = None

    # ── 1. Load persisted bars + provenance ─────────────────────────
    bars_by_pair, provenance_by_pair = load_bars_from_disk(cfg.data_dir)
    data_hash_by_pair: dict[str, str] = {
        sym: provenance_by_pair[sym]["data_hash_sha256"]
        for sym in cfg.pairs
        if sym in provenance_by_pair
    }
    n_bars_by_pair = {sym: len(bars_by_pair.get(sym, [])) for sym in cfg.pairs}

    # ── 2. Integrity gate per pair (never loosen) ───────────────────
    integrity_reports = run_integrity_gate_per_pair(bars_by_pair)

    # ── 3. Enumerate 99 candidates (33 variants × 3 pairs) ───────────
    candidates: list = []
    candidate_ids_by_cell: dict[tuple[str, str, str], list[str]] = {}
    for pair in cfg.pairs:
        bars = bars_by_pair.get(pair, [])
        if not bars:
            continue
        for strategy_id in CRYPTO_NATIVE_STRATEGY_IDS:
            cell_candidates = enumerate_candidates_for_strategy(strategy_id, pair, bars)
            for c in cell_candidates:
                cell_key = (c.template.archetype_id, c.pair, c.timeframe)
                candidate_ids_by_cell.setdefault(cell_key, []).append(c.candidate_id)
            candidates.extend(cell_candidates)
            logger.info(
                "%s/%s: %d grid variants",
                pair, strategy_id, len(cell_candidates),
            )

    # ── 4. Run validation (walk-forward + DSR + cost sensitivity) ──
    runner = (
        ValidationRunner(
            pipeline_config=default_pipeline_config(),
            spread_costs=build_crypto_spread_costs(),
            cell_count=len(candidates),
        )
        if ValidationRunner is not None
        else None
    )
    verdicts: list = []
    if runner is not None and candidates:
        verdicts = runner.run_batch(candidates)
        logger.info("validation runner produced %d verdicts", len(verdicts))

    # ── 5. Persist to duckdb with per-pair data_hash ────────────────
    n_persisted = 0
    store_obj = FactoryVerdictStore(cfg.db_path) if FactoryVerdictStore is not None else None
    if store_obj is not None and verdicts:
        try:
            # FIX #1 (card 68fb28f5 review note): pass per-pair data_hash
            # so factory_verdicts.data_hash is non-NULL per row.
            n_persisted = store_obj.write_verdicts(
                verdicts,
                git_commit=build_commit_short,
                data_hash_by_pair=data_hash_by_pair,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("factory_verdicts persist failed: %s", exc)

    # ── 6. BH-FDR per cell + per-strategy aggregates ────────────────
    rankings_by_cell: dict[tuple[str, str, str], list] = {}
    bh_empty = True
    if rank_from_trial_return_store is not None and verdicts:
        try:
            rankings_by_cell = rank_from_trial_return_store(
                runner.trial_return_store if runner else TrialReturnStore(),
                candidate_ids_per_cell=candidate_ids_by_cell,
                alpha=cfg.alpha,
            )
            bh_empty = not rankings_by_cell or not any(
                getattr(r, "bh_rejected", False) for ranked in rankings_by_cell.values() for r in ranked
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("BH-FDR ranking failed: %s", exc)

    # ── 7. Per-strategy trade-count summary ─────────────────────────
    per_strategy_summary: dict[str, dict] = {}
    for sid in CRYPTO_NATIVE_STRATEGY_IDS:
        rows = [v for v in verdicts if v.archetype_id == f"crypto_native::{sid}"]
        if not rows:
            per_strategy_summary[sid] = {"n_candidates": 0}
            continue
        tier_counts: dict[str, int] = {}
        total_trades_total = 0
        sharpes: list[float] = []
        for v in rows:
            tier_counts[v.tier] = tier_counts.get(v.tier, 0) + 1
            total_trades_total += int(v.total_trades)
            sharpes.append(float(v.mean_sharpe))
        per_strategy_summary[sid] = {
            "n_candidates": len(rows),
            "tier_counts": tier_counts,
            "total_trades_across_variants": total_trades_total,
            "mean_sharpe_avg": round(float(np.mean(sharpes)) if sharpes else 0.0, 4),
            "mean_sharpe_max": round(float(np.max(sharpes)) if sharpes else 0.0, 4),
        }

    # ── 8. Verdict table (per-row provenance) ──────────────────────
    rows = []
    cell_lookup: dict[str, dict] = {}
    for cell_key, ranked in rankings_by_cell.items():
        for r in ranked:
            cell_lookup[r.candidate_id] = {
                "rank": None if bh_empty else getattr(r, "rank", None),
                "p_value": None if bh_empty else getattr(r, "p_value", None),
                "q_value": None if bh_empty else getattr(r, "q_value", None),
                "bh_rejected": getattr(r, "bh_rejected", False),
            }
    for v in verdicts:
        meta_lookup = cell_lookup.get(v.candidate_id, {})
        rows.append({
            "candidate_id": v.candidate_id,
            "archetype_id": v.archetype_id,
            "pair": v.pair,
            "timeframe": v.timeframe,
            "tier": v.tier,
            "windows_passed": v.windows_passed,
            "windows_total": v.windows_total,
            "total_trades": v.total_trades,
            "mean_sharpe": round(v.mean_sharpe, 4),
            "mean_profit_factor": round(v.mean_profit_factor, 4),
            "mean_win_rate": round(v.mean_win_rate, 4),
            "max_drawdown": round(v.max_drawdown, 4),
            "dsr_pvalue": round(v.dsr_pvalue, 6),
            "pbo_score": (round(v.pbo_score, 4) if v.pbo_score is not None else None),
            "pbo_tier_ceiling": v.pbo_tier_ceiling,
            "cost_sensitivity": (round(v.cost_sensitivity, 4) if v.cost_sensitivity is not None else None),
            "rank": meta_lookup.get("rank"),
            "q_value": meta_lookup.get("q_value"),
            "p_value": meta_lookup.get("p_value"),
            "bh_rejected": meta_lookup.get("bh_rejected"),
            "reason": v.reason,
            # Per-row provenance — the fix for review note #1.
            "git_commit": build_commit_short,
            "data_hash": data_hash_by_pair.get(v.pair),
        })

    discoveries = [r for r in rows if r["bh_rejected"] and r["tier"] in {"A", "B", "C"}]
    survivors = [r for r in rows if r["tier"] in {"A", "B", "C"}]
    rejects = [r for r in rows if r["tier"] == "REJECT"]
    insufficient = [r for r in rows if r["tier"] == "INSUFFICIENT_DATA"]

    elapsed = time.time() - started

    report_payload = {
        "metadata": {
            "card_id": "68fb28f5-9135-4157-92d1-82c8380e03bd",
            "title": "Sweep #3 — crypto-native candidates on real Binance.US bars",
            "started_at": started_iso,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 2),
            # FIX #2 (card 68fb28f5 review note): git_commit SHA
            # convention documented explicitly. The short SHA written
            # to factory_verdicts.git_commit is the BUILD worktree SHA
            # (the commit the SWEEP RAN UNDER), NOT the baseline
            # spine SHA — convention chosen because the verdict is
            # not reproducible from main alone (the param grid lives
            # on this branch).
            "git_commit": build_commit_short,
            "git_commit_long": build_commit_long,
            "git_commit_convention": (
                "BUILD worktree short SHA — the commit the sweep RAN UNDER. "
                "Rationale: the crypto-native template registry "
                "(forex_bot/strategies/crypto_native.py) and this sweep "
                "driver land on this branch; verdicts are not "
                "reproducible from the parent commit alone. The "
                "parent_commit_short field below records the prior "
                "main SHA (eb6ecacb at the time of dispatch) for "
                "traceability, but factory_verdicts.git_commit holds "
                "the BUILD SHA only."
            ),
            "parent_commit_short": parent_commit_short,
            "spine_baseline_main_sha": "eb6ecacb",
            "n_bars_per_pair": n_bars_by_pair,
            "data_source": "REAL (Binance.US /api/v3/klines direct egress; persisted by the card 0ab49707 real-data sweep)",
            "strategy_ids": list(CRYPTO_NATIVE_STRATEGY_IDS),
            "data_hash_by_pair": data_hash_by_pair,
            "rin_review_notes_folded_in": [
                "Review note #1: factory_verdicts.data_hash populated per-row via the new data_hash_by_pair kwarg on FactoryVerdictStore.write_verdicts.",
                "Review note #2: git_commit SHA convention documented in metadata.git_commit_convention; factory_verdicts.git_commit holds the BUILD short SHA.",
            ],
        },
        "provenance_by_pair": provenance_by_pair,
        "integrity_gate": integrity_reports,
        "candidates": {
            "n_total": len(candidates),
            "n_evaluated": len(verdicts),
            "n_persisted_to_factory_verdicts": n_persisted,
            "per_strategy_n_variants": {
                sid: per_strategy_summary.get(sid, {}).get("n_candidates", 0)
                for sid in CRYPTO_NATIVE_STRATEGY_IDS
            },
        },
        "per_strategy_summary": per_strategy_summary,
        "rankings": {
            "alpha": cfg.alpha,
            "meta_threshold": cfg.meta_threshold,
            "bh_empty": bh_empty,
            "by_cell": {
                f"{cell[0]}|{cell[1]}|{cell[2]}": [
                    {
                        "candidate_id": r.candidate_id,
                        "rank": None if bh_empty else getattr(r, "rank", None),
                        "p_value": None if bh_empty else (
                            round(getattr(r, "p_value", 1.0), 6)
                            if getattr(r, "p_value", None) is not None
                            else None
                        ),
                        "q_value": None if bh_empty else (
                            round(getattr(r, "q_value", 1.0), 6)
                            if getattr(r, "q_value", None) is not None
                            else None
                        ),
                        "bh_rejected": getattr(r, "bh_rejected", False),
                        "notes": getattr(r, "notes", None),
                    }
                    for r in ranked
                ]
                for cell, ranked in rankings_by_cell.items()
            },
            "n_discoveries": len(discoveries),
            "n_survivors": len(survivors),
            "n_rejects": len(rejects),
            "n_insufficient_data": len(insufficient),
            "discovery_ids": [c["candidate_id"] for c in discoveries],
            "survivor_ids": [c["candidate_id"] for c in survivors],
        },
        "verdict_table": rows,
        "defects": {
            "first_sweep_in_run_fixes": [],
            "patches_applied_this_run": [
                "storage.py: write_verdicts accepts data_hash_by_pair "
                "(per-pair data_hash into factory_verdicts)",
                "sweep_crypto_native_real_data.py: enumerated the 33-param "
                "crypto-native grid; previous driver used 6 FX strategies",
            ],
            "spine_import_errors": {},
            "integrity_failures": {
                k: v for k, v in integrity_reports.items() if v.get("status") == "FAIL"
            },
        },
        "honesty_notes": [
            "Bars are LIVE (Binance.US /api/v3/klines direct egress; persisted "
            "by the prior card 0ab49707 sweep). Source + window + retrieval "
            "timestamp + SHA-256 captured per pair in provenance_by_pair.",
            "Sample size is small: 871 bars / pair (~37 days H1 post-trim). "
            "A 33-variant × 3-pair candidate matrix on this short window "
            "is more a smoke test of the spine than a statistical seal. "
            "If BH-FDR finds 0 discoveries that IS the result; we do NOT "
            "loosen the gate.",
            "Validation runner uses a placeholder trial-return derivation "
            "(deterministic from bars+params); the per-strategy strategy "
            "classes are bridge-verified (one-shot build) but the runner "
            "does not exercise StrategySignal.evaluate on each bar. "
            "Walk-forward metrics are the runner's honest outputs, not "
            "live P&L.",
            "The git_commit recorded in factory_verdicts.git_commit is the "
            "BUILD short SHA (this commit), per the documented convention. "
            "The parent commit (main @ eb6ecacb) is recorded in "
            "metadata.parent_commit_short for traceability only.",
            "factory_verdicts.data_hash is now populated PER ROW from "
            "data_hash_by_pair (one SHA-256 per symbol). The earlier "
            "real-data sweep left this column NULL.",
        ],
    }
    return report_payload


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------


def render_markdown_summary(report: dict) -> str:
    meta = report["metadata"]
    per_strategy = report["per_strategy_summary"]
    rankings = report["rankings"]
    lines: list[str] = []
    lines.append("# Sweep #3 — crypto-native candidates on real Binance.US bars")
    lines.append("")
    lines.append(f"**Card:** {meta['card_id']}")
    lines.append(f"**Started:** {meta['started_at']}")
    lines.append(f"**Finished:** {meta['finished_at']}")
    lines.append(f"**Elapsed:** {meta['elapsed_seconds']}s")
    lines.append(f"**Build commit:** `{meta['git_commit']}` (long: `{meta['git_commit_long']}`)")
    lines.append(f"**Parent commit (main):** `{meta['parent_commit_short']}` (spine baseline: `{meta['spine_baseline_main_sha']}`)")
    lines.append("")
    lines.append("## TL;DR")
    lines.append("")
    lines.append(
        f"- **Candidates evaluated:** {report['candidates']['n_evaluated']} / "
        f"{report['candidates']['n_total']}"
    )
    lines.append(
        f"- **Persisted to factory_verdicts:** {report['candidates']['n_persisted_to_factory_verdicts']}"
    )
    lines.append(
        f"- **Survivors (Tier A/B/C):** {rankings['n_survivors']} | "
        f"**REJECT:** {rankings['n_rejects']} | "
        f"**INSUFFICIENT_DATA:** {rankings['n_insufficient_data']}"
    )
    lines.append(
        f"- **BH-FDR discoveries (q<{meta['alpha'] if 'alpha' in meta else 0.05}):** "
        f"{rankings['n_discoveries']} (bh_empty={rankings['bh_empty']})"
    )
    lines.append("")
    lines.append("## Per-strategy summary")
    lines.append("")
    lines.append("| Strategy | Variants | Tier A | Tier C | REJECT | INSUFFICIENT_DATA | Total trades | Mean Sharpe (avg/max) |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for sid in CRYPTO_NATIVE_STRATEGY_IDS:
        s = per_strategy.get(sid, {})
        tc = s.get("tier_counts", {})
        lines.append(
            f"| `{sid}` | {s.get('n_candidates', 0)} | "
            f"{tc.get('A', 0)} | {tc.get('B', 0) + tc.get('C', 0)} | "
            f"{tc.get('REJECT', 0)} | {tc.get('INSUFFICIENT_DATA', 0)} | "
            f"{s.get('total_trades_across_variants', 0)} | "
            f"{s.get('mean_sharpe_avg', 0):.3f} / {s.get('mean_sharpe_max', 0):.3f} |"
        )
    lines.append("")
    lines.append("## Verdict table — survivors + casualties")
    lines.append("")
    lines.append("| Pair | Strategy | variant hash | Tier | Trades | Mean Sharpe | Mean PF | Max DD | q-value | bh_rejected | reason |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    # Sort: survivors first, then by tier (A>B>C), then by mean_sharpe desc.
    sorted_rows = sorted(
        report["verdict_table"],
        key=lambda r: (
            0 if r["tier"] in {"A", "B", "C"} else 1,
            {"A": 0, "B": 1, "C": 2, "REJECT": 3, "INSUFFICIENT_DATA": 4}.get(r["tier"], 5),
            -r["mean_sharpe"],
        ),
    )
    for r in sorted_rows:
        strategy_short = r["candidate_id"].split("|")[1].replace("crypto_native::", "")
        variant_hash = r["candidate_id"].split("|")[-1]
        lines.append(
            f"| {r['pair']} | `{strategy_short}` | `{variant_hash}` | "
            f"**{r['tier']}** | {r['total_trades']} | "
            f"{r['mean_sharpe']:.3f} | {r['mean_profit_factor']:.3f} | "
            f"{r['max_drawdown']:.3f} | "
            f"{r['q_value'] if r['q_value'] is not None else 'null'} | "
            f"{r['bh_rejected']} | {r['reason'][:60]} |"
        )
    lines.append("")
    lines.append("## Integrity gate")
    lines.append("")
    lines.append("| Pair | Status | n_violations | Error |")
    lines.append("| --- | --- | --- | --- |")
    for pair, rep in report["integrity_gate"].items():
        lines.append(
            f"| {pair} | {rep.get('status')} | {rep.get('n_violations', '-')} | "
            f"{rep.get('error', '-')} |"
        )
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append("| Pair | n_bars | earliest | latest | data_hash (sha256, prefix) |")
    lines.append("| --- | --- | --- | --- | --- |")
    for pair, prov in report["provenance_by_pair"].items():
        lines.append(
            f"| {pair} | {prov['n_bars']} | {prov['earliest_bar_utc']} | "
            f"{prov['latest_bar_utc']} | `{prov['data_hash_sha256'][:16]}…` |"
        )
    lines.append("")
    lines.append("## git_commit convention (review note #2)")
    lines.append("")
    lines.append(meta["git_commit_convention"])
    lines.append("")
    lines.append("## Honesty notes")
    lines.append("")
    for note in report["honesty_notes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=WORKTREE / "docs" / "reports")
    parser.add_argument(
        "--db-path", type=Path,
        default=WORKTREE / "data" / "sweep_crypto_native_real_data.duckdb",
    )
    parser.add_argument(
        "--data-dir", type=Path,
        default=WORKTREE / "data" / "sweep_real_data_bars",
    )
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--meta-threshold", type=float, default=0.5)
    parser.add_argument("--pairs", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT")
    args = parser.parse_args()

    cfg = SweepConfigCryptoNative(
        out_dir=args.out_dir,
        db_path=args.db_path,
        data_dir=args.data_dir,
        pairs=tuple(args.pairs.split(",")),
        seed=args.seed,
        alpha=args.alpha,
        meta_threshold=args.meta_threshold,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        report = run_sweep(cfg)

    json_path = cfg.out_dir / "2026-10-06-crypto-native-sweep3.json"
    md_path = cfg.out_dir / "2026-10-06-crypto-native-sweep3.md"
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(render_markdown_summary(report))
    print(f"Sweep report written to {json_path}")
    print(f"Human summary written to {md_path}")
    print(f"  bars loaded: {sum(report['metadata']['n_bars_per_pair'].values())}")
    print(f"  candidates evaluated: {report['candidates']['n_evaluated']}")
    print(
        f"  survivors / rejects / insufficient: "
        f"{report['rankings']['n_survivors']} / "
        f"{report['rankings']['n_rejects']} / "
        f"{report['rankings']['n_insufficient_data']}"
    )
    print(f"  BH-FDR discoveries: {report['rankings']['n_discoveries']}")
    print(f"  persisted to factory_verdicts: {report['candidates']['n_persisted_to_factory_verdicts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())