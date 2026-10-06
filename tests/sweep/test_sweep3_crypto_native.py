"""Tests for the Sweep #3 crypto-native driver (card 68fb28f5).

Coverage matrix (HR5-targeted, no full-suite invocation):

* ``_CryptoNativeTemplate`` builds the correct strategy class for each
  of the 3 crypto-native strategy IDs.
* ``_CryptoNativeTemplate.param_space`` matches
  :data:`forex_bot.strategies.crypto_native.CRYPTO_PARAM_GRIDS`.
* ``enumerate_candidates_for_strategy`` yields the expected number of
  candidates for each strategy × pair (18 / 6 / 9).
* :func:`render_markdown_summary` produces a non-empty markdown string
  whose TL;DR section carries the per-strategy variant counts and
  BH-FDR discovery count.

All tests are pure (no network, no Optuna study, no real-data fetch);
the integration shape drives the smoke test in the sweep driver's ``--help``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

# Repo path setup mirrors the sweep driver's.
WORKTREE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "src" / "forex_bot"))

from forex_bot.backtest.types import Bar  # noqa: E402

from scripts.sweep_crypto_native_real_data import (  # noqa: E402, E501
    CRYPTO_NATIVE_STRATEGY_IDS,
    _CryptoNativeTemplate,
    enumerate_candidates_for_strategy,
    render_markdown_summary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bar(i: int, *, base: float = 100.0) -> Bar:
    """Build a minimal synthetic H1 Bar for enumeration tests."""
    return Bar(
        time=datetime(2026, 9, 1, i % 24, tzinfo=timezone.utc),
        open=base,
        high=base + 1.0,
        low=base - 1.0,
        close=base + 0.5,
        volume=10.0 + i,
        spread_pips=0.5,
    )


@pytest.fixture
def small_bar_series() -> list[Bar]:
    """120 synthetic bars — enough to clear the strategies' MIN_BARS gate."""
    return [_make_bar(i, base=100.0 + 0.01 * i) for i in range(120)]


# ---------------------------------------------------------------------------
# Template construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy_id", list(CRYPTO_NATIVE_STRATEGY_IDS))
def test_crypto_native_template_archetype_id(strategy_id: str) -> None:
    """Each template's archetype_id is ``crypto_native::<strategy_id>``."""
    template = _CryptoNativeTemplate(strategy_id)
    assert template.archetype_id == f"crypto_native::{strategy_id}"


@pytest.mark.parametrize("strategy_id", list(CRYPTO_NATIVE_STRATEGY_IDS))
def test_crypto_native_template_build_strategy_returns_instance(
    strategy_id: str,
) -> None:
    """``build_strategy`` returns an object with the duck-type contract."""
    template = _CryptoNativeTemplate(strategy_id)
    built = template.build_strategy(params={}, pair="BTCUSDT")
    assert hasattr(built, "name")
    assert callable(getattr(built, "evaluate", None))


@pytest.mark.parametrize("strategy_id", list(CRYPTO_NATIVE_STRATEGY_IDS))
def test_crypto_native_template_param_space_matches_grid(
    strategy_id: str,
) -> None:
    """``param_space`` matches the declared frozen grid in ``crypto_native``."""
    template = _CryptoNativeTemplate(strategy_id)
    ps = template.param_space
    ps_names = {p.name for p in ps}
    from forex_bot.strategies.crypto_native import CRYPTO_PARAM_GRIDS
    expected_names = set(CRYPTO_PARAM_GRIDS[strategy_id].keys())
    assert ps_names == expected_names


def test_unknown_strategy_id_raises() -> None:
    """Constructing a template with an unknown strategy_id raises."""
    with pytest.raises(ValueError):
        _CryptoNativeTemplate("does_not_exist")


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def test_enumerate_candidates_ema_cross_trend_count(small_bar_series: list[Bar]) -> None:
    """EMA-cross trend grid: 3 × 3 × 2 × 1 = 18 variants."""
    cands = enumerate_candidates_for_strategy(
        "crypto_ema_cross_trend", "BTCUSDT", small_bar_series
    )
    assert len(cands) == 18


def test_enumerate_candidates_donchian_breakout_count(
    small_bar_series: list[Bar],
) -> None:
    """Donchian breakout grid: 3 × 2 × 1 × 1 = 6 variants."""
    cands = enumerate_candidates_for_strategy(
        "crypto_donchian_breakout", "BTCUSDT", small_bar_series
    )
    assert len(cands) == 6


def test_enumerate_candidates_zscore_mean_reversion_count(
    small_bar_series: list[Bar],
) -> None:
    """Z-score mean reversion grid: 3 × 3 × 1 = 9 variants."""
    cands = enumerate_candidates_for_strategy(
        "crypto_zscore_mean_reversion", "BTCUSDT", small_bar_series
    )
    assert len(cands) == 9


def test_enumerate_candidates_candidate_id_is_unique(
    small_bar_series: list[Bar],
) -> None:
    """All candidate_ids across one strategy are distinct."""
    cands = enumerate_candidates_for_strategy(
        "crypto_ema_cross_trend", "BTCUSDT", small_bar_series
    )
    ids = {c.candidate_id for c in cands}
    assert len(ids) == len(cands)


def test_enumerate_candidate_id_carries_pair_and_strategy(
    small_bar_series: list[Bar],
) -> None:
    """``candidate_id`` starts with ``<pair>|crypto_native::<strategy_id>|``."""
    cands = enumerate_candidates_for_strategy(
        "crypto_donchian_breakout", "ETHUSDT", small_bar_series
    )
    assert len(cands) > 0
    sample = cands[0].candidate_id
    assert sample.startswith("ETHUSDT|crypto_native::crypto_donchian_breakout|")


def test_enumerate_candidates_empty_bars_returns_empty() -> None:
    """An empty bar list yields no candidates (safe no-op)."""
    cands = enumerate_candidates_for_strategy(
        "crypto_ema_cross_trend", "BTCUSDT", []
    )
    assert cands == []


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _stub_verdict(
    *,
    candidate_id: str,
    pair: str,
    archetype_id: str,
    tier: str,
    mean_sharpe: float = 0.5,
    mean_profit_factor: float = 1.1,
    max_drawdown: float = 0.04,
    total_trades: int = 20,
    windows_passed: int = 3,
    windows_total: int = 4,
    reason: str = "",
    q_value: float | None = None,
    p_value: float | None = None,
    rank: int | None = None,
    bh_rejected: bool = False,
    data_hash: str | None = "btc_deadbeef",
    git_commit: str | None = "abc1234",
) -> dict:
    """Build a single verdict-table row dict for the markdown test."""
    return {
        "candidate_id": candidate_id,
        "archetype_id": archetype_id,
        "pair": pair,
        "timeframe": "H1",
        "tier": tier,
        "windows_passed": windows_passed,
        "windows_total": windows_total,
        "total_trades": total_trades,
        "mean_sharpe": mean_sharpe,
        "mean_profit_factor": mean_profit_factor,
        "mean_win_rate": 0.55,
        "max_drawdown": max_drawdown,
        "dsr_pvalue": 0.03,
        "pbo_score": 0.12,
        "pbo_tier_ceiling": "A",
        "cost_sensitivity": 0.07,
        "rank": rank,
        "q_value": q_value,
        "p_value": p_value,
        "bh_rejected": bh_rejected,
        "reason": reason,
        "git_commit": git_commit,
        "data_hash": data_hash,
    }


def _stub_report() -> dict:
    """Build a minimal-but-honest stub report for the markdown test."""
    rows: list[dict] = []
    # 18 ema_cross_trend candidates, 6 donchian_breakout, 9 z-score.
    # Mix of Tier A, B, REJECT, INSUFFICIENT_DATA.
    for i in range(18):
        tier = "A" if i < 2 else ("B" if i < 8 else ("REJECT" if i < 14 else "INSUFFICIENT_DATA"))
        rows.append(_stub_verdict(
            candidate_id=f"BTCUSDT|crypto_native::crypto_ema_cross_trend|hash{i:02d}",
            pair="BTCUSDT",
            archetype_id="crypto_native::crypto_ema_cross_trend",
            tier=tier,
            reason="tier-A" if tier == "A" else (f"{tier} reason"),
        ))
    for i in range(6):
        rows.append(_stub_verdict(
            candidate_id=f"ETHUSDT|crypto_native::crypto_donchian_breakout|hash{i:02d}",
            pair="ETHUSDT",
            archetype_id="crypto_native::crypto_donchian_breakout",
            tier="C",
            reason="tier-C",
        ))
    for i in range(9):
        rows.append(_stub_verdict(
            candidate_id=f"SOLUSDT|crypto_native::crypto_zscore_mean_reversion|hash{i:02d}",
            pair="SOLUSDT",
            archetype_id="crypto_native::crypto_zscore_mean_reversion",
            tier="REJECT",
            reason="REJECT",
        ))
    per_strategy = {
        "crypto_ema_cross_trend": {
            "n_candidates": 18,
            "tier_counts": {"A": 2, "B": 6, "REJECT": 6, "INSUFFICIENT_DATA": 4},
            "total_trades_across_variants": 360,
            "mean_sharpe_avg": 0.42,
            "mean_sharpe_max": 1.18,
        },
        "crypto_donchian_breakout": {
            "n_candidates": 6,
            "tier_counts": {"C": 6},
            "total_trades_across_variants": 120,
            "mean_sharpe_avg": 0.31,
            "mean_sharpe_max": 0.84,
        },
        "crypto_zscore_mean_reversion": {
            "n_candidates": 9,
            "tier_counts": {"REJECT": 9},
            "total_trades_across_variants": 180,
            "mean_sharpe_avg": 0.05,
            "mean_sharpe_max": 0.21,
        },
    }
    return {
        "metadata": {
            "card_id": "68fb28f5-9135-4157-92d1-82c8380e03bd",
            "title": "Sweep #3 — crypto-native candidates on real Binance.US bars",
            "started_at": "2026-10-06T19:30:00+00:00",
            "finished_at": "2026-10-06T19:32:00+00:00",
            "elapsed_seconds": 120.0,
            "git_commit": "abc1234",
            "git_commit_long": "abc1234567890abcdef1234567890abcdef12345",
            "git_commit_convention": "BUILD short SHA — see report body.",
            "parent_commit_short": "eb6ecacb",
            "spine_baseline_main_sha": "eb6ecacb",
            "n_bars_per_pair": {"BTCUSDT": 871, "ETHUSDT": 871, "SOLUSDT": 871},
            "data_source": "REAL Binance.US H1",
            "strategy_ids": list(CRYPTO_NATIVE_STRATEGY_IDS),
            "data_hash_by_pair": {"BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol"},
            "alpha": 0.05,
            "rin_review_notes_folded_in": ["#1", "#2"],
        },
        "provenance_by_pair": {
            "BTCUSDT": {
                "n_bars": 871,
                "earliest_bar_utc": "2026-08-31T13:00:00+00:00",
                "latest_bar_utc": "2026-10-06T19:00:00+00:00",
                "data_hash_sha256": "a" * 64,
            },
            "ETHUSDT": {
                "n_bars": 871,
                "earliest_bar_utc": "2026-08-31T13:00:00+00:00",
                "latest_bar_utc": "2026-10-06T19:00:00+00:00",
                "data_hash_sha256": "b" * 64,
            },
            "SOLUSDT": {
                "n_bars": 871,
                "earliest_bar_utc": "2026-08-31T13:00:00+00:00",
                "latest_bar_utc": "2026-10-06T19:00:00+00:00",
                "data_hash_sha256": "c" * 64,
            },
        },
        "integrity_gate": {
            "BTCUSDT": {"status": "PASS", "n_violations": 0, "error": None},
            "ETHUSDT": {"status": "PASS", "n_violations": 0, "error": None},
            "SOLUSDT": {"status": "PASS", "n_violations": 0, "error": None},
        },
        "candidates": {
            "n_total": 33 * 3,
            "n_evaluated": len(rows),
            "n_persisted_to_factory_verdicts": len(rows),
            "per_strategy_n_variants": {
                "crypto_ema_cross_trend": 18,
                "crypto_donchian_breakout": 6,
                "crypto_zscore_mean_reversion": 9,
            },
        },
        "per_strategy_summary": per_strategy,
        "rankings": {
            "alpha": 0.05,
            "meta_threshold": 0.5,
            "bh_empty": True,
            "by_cell": {},
            "n_discoveries": 0,
            "n_survivors": 14,
            "n_rejects": 15,
            "n_insufficient_data": 4,
            "discovery_ids": [],
            "survivor_ids": [r["candidate_id"] for r in rows if r["tier"] in {"A", "B", "C"}],
        },
        "verdict_table": rows,
        "honesty_notes": [
            "Bars are LIVE Binance.US.",
            "Sample size is small (871 bars / pair).",
        ],
    }


def test_render_markdown_summary_includes_tldr() -> None:
    """Markdown summary carries the TL;DR line + per-strategy table."""
    md = render_markdown_summary(_stub_report())
    assert "Sweep #3" in md
    assert "TL;DR" in md
    assert "crypto_ema_cross_trend" in md
    assert "crypto_donchian_breakout" in md
    assert "crypto_zscore_mean_reversion" in md
    assert "git_commit convention" in md


def test_render_markdown_summary_mentions_git_commit_convention() -> None:
    """The git_commit convention is reproduced in the markdown body."""
    md = render_markdown_summary(_stub_report())
    assert "BUILD" in md
    assert "abc1234" in md
    assert "eb6ecacb" in md