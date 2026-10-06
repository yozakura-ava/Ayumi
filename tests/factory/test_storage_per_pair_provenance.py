"""Tests for the per-pair ``data_hash_by_pair`` extension on
:class:`FactoryVerdictStore.write_verdicts` (card 68fb28f5 review note #1).

Coverage matrix:

* ``write_verdicts(data_hash_by_pair={pair: hash, …})`` stamps the
  per-row ``data_hash`` column from the pair-keyed map.
* A pair not present in ``data_hash_by_pair`` falls back to the
  run-level ``data_hash`` (legacy behavior preserved).
* ``data_hash_by_pair=None`` (default) preserves the v3 behavior —
  every row gets the run-level ``data_hash`` (or ``NULL`` when
  neither ``data_hash`` nor ``data_path`` is supplied).

All tests are pure (no Optuna, no market-data fetch, no network) so
they stay in the HR5 targeted-only envelope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forex_bot.factory.storage import FactoryVerdictStore
from forex_bot.factory.validation_runner import ValidationVerdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_verdict(
    *,
    candidate_id: str,
    pair: str,
    tier: str = "B",
) -> ValidationVerdict:
    """Build a minimal :class:`ValidationVerdict` for per-pair tests."""
    base: dict[str, Any] = {
        "candidate_id": candidate_id,
        "archetype_id": "crypto_native::crypto_ema_cross_trend",
        "pair": pair,
        "timeframe": "H1",
        "tier": tier,
        "windows_passed": 3,
        "windows_total": 4,
        "total_trades": 28,
        "mean_sharpe": 0.95,
        "mean_profit_factor": 1.18,
        "mean_win_rate": 0.55,
        "max_drawdown": 0.06,
        "dsr_pvalue": 0.04,
        "n_trials_used": 33,
        "pbo_score": 0.21,
        "pbo_tier_ceiling": "B",
        "cost_sensitivity": 0.08,
        "spread_pips": 0.5,
        "commission_per_lot_usd": 2.0,
        "slippage_pips": 0.5,
        "go_nogo": True,
        "reason": "tier-B pass",
        "ran_at": "2026-10-06T19:00:00+00:00",
    }
    return ValidationVerdict(**base)


# ---------------------------------------------------------------------------
# Per-pair data_hash propagation
# ---------------------------------------------------------------------------


def test_write_verdicts_stamps_per_pair_data_hash(tmp_path: Path) -> None:
    """Each row's ``data_hash`` is the hash keyed by ``v.pair``."""
    store = FactoryVerdictStore(tmp_path / "per_pair.duckdb")
    verdicts = [
        _make_verdict(candidate_id="BTCUSDT|v1", pair="BTCUSDT"),
        _make_verdict(candidate_id="ETHUSDT|v1", pair="ETHUSDT"),
        _make_verdict(candidate_id="SOLUSDT|v1", pair="SOLUSDT"),
    ]
    data_hash_by_pair = {
        "BTCUSDT": "btc_deadbeef0000000000000000000000000000000000000000000000000000",
        "ETHUSDT": "eth_feedface0000000000000000000000000000000000000000000000000000",
        "SOLUSDT": "sol_cafe1234000000000000000000000000000000000000000000000000000000",
    }
    n = store.write_verdicts(
        verdicts,
        git_commit="abc1234",
        data_hash_by_pair=data_hash_by_pair,
    )
    assert n == 3
    rows = store.fetch_verdicts()
    by_pair = {r["pair"]: r for r in rows}
    assert by_pair["BTCUSDT"]["data_hash"] == data_hash_by_pair["BTCUSDT"]
    assert by_pair["ETHUSDT"]["data_hash"] == data_hash_by_pair["ETHUSDT"]
    assert by_pair["SOLUSDT"]["data_hash"] == data_hash_by_pair["SOLUSDT"]
    # git_commit is stamped on every row (legacy contract preserved).
    for row in rows:
        assert row["git_commit"] == "abc1234"


def test_write_verdicts_per_pair_falls_back_to_run_hash(tmp_path: Path) -> None:
    """A pair missing from ``data_hash_by_pair`` falls back to the run hash."""
    store = FactoryVerdictStore(tmp_path / "per_pair_partial.duckdb")
    verdicts = [
        _make_verdict(candidate_id="BTCUSDT|v1", pair="BTCUSDT"),
        _make_verdict(candidate_id="ETHUSDT|v1", pair="ETHUSDT"),
        _make_verdict(candidate_id="SOLUSDT|v1", pair="SOLUSDT"),
    ]
    # Map only BTC; ETH + SOL fall back to the run-level data_hash.
    data_hash_by_pair = {
        "BTCUSDT": "btc_only",
    }
    store.write_verdicts(
        verdicts,
        git_commit="abc1234",
        data_hash="run_level_fallback",
        data_hash_by_pair=data_hash_by_pair,
    )
    rows = store.fetch_verdicts()
    by_pair = {r["pair"]: r for r in rows}
    assert by_pair["BTCUSDT"]["data_hash"] == "btc_only"
    assert by_pair["ETHUSDT"]["data_hash"] == "run_level_fallback"
    assert by_pair["SOLUSDT"]["data_hash"] == "run_level_fallback"


def test_write_verdicts_no_per_pair_uses_run_hash(tmp_path: Path) -> None:
    """``data_hash_by_pair=None`` (default) preserves the v3 behaviour."""
    store = FactoryVerdictStore(tmp_path / "v3_default.duckdb")
    verdicts = [
        _make_verdict(candidate_id="BTCUSDT|v1", pair="BTCUSDT"),
        _make_verdict(candidate_id="ETHUSDT|v1", pair="ETHUSDT"),
    ]
    store.write_verdicts(
        verdicts,
        git_commit="abc1234",
        data_hash="single_run_hash",
        # data_hash_by_pair omitted — falls back to run-level.
    )
    rows = store.fetch_verdicts()
    for row in rows:
        assert row["data_hash"] == "single_run_hash"


def test_write_verdicts_no_hash_at_all_yields_null(tmp_path: Path) -> None:
    """When neither ``data_hash`` nor ``data_hash_by_pair`` is set, column is NULL."""
    store = FactoryVerdictStore(tmp_path / "null_data_hash.duckdb")
    verdicts = [
        _make_verdict(candidate_id="BTCUSDT|v1", pair="BTCUSDT"),
    ]
    store.write_verdicts(verdicts, git_commit="abc1234")
    rows = store.fetch_verdicts()
    assert len(rows) == 1
    assert rows[0]["data_hash"] is None  # DuckDB NULL → None in the dict


def test_per_pair_overrides_run_hash_when_both_provided(tmp_path: Path) -> None:
    """Per-pair map wins over the run-level ``data_hash`` when both supplied."""
    store = FactoryVerdictStore(tmp_path / "override.duckdb")
    verdicts = [
        _make_verdict(candidate_id="BTCUSDT|v1", pair="BTCUSDT"),
        _make_verdict(candidate_id="ETHUSDT|v1", pair="ETHUSDT"),
    ]
    store.write_verdicts(
        verdicts,
        git_commit="abc1234",
        data_hash="run_level",
        data_hash_by_pair={
            "BTCUSDT": "btc_specific",
            "ETHUSDT": "eth_specific",
        },
    )
    rows = store.fetch_verdicts()
    by_pair = {r["pair"]: r for r in rows}
    assert by_pair["BTCUSDT"]["data_hash"] == "btc_specific"
    assert by_pair["ETHUSDT"]["data_hash"] == "eth_specific"
    # The run-level data_hash is NOT used because every pair had a
    # per-pair entry; the override is total when the map covers all
    # pairs in the batch.
    assert "run_level" not in {r["data_hash"] for r in rows}


# ---------------------------------------------------------------------------
# Empty batches are still well-behaved
# ---------------------------------------------------------------------------


def test_write_verdicts_empty_batch_with_per_pair(tmp_path: Path) -> None:
    """Empty batch returns 0 and does not touch the DB."""
    store = FactoryVerdictStore(tmp_path / "empty.duckdb")
    n = store.write_verdicts(
        [],
        git_commit="abc1234",
        data_hash_by_pair={"BTCUSDT": "anything"},
    )
    assert n == 0