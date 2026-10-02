"""Tests for promotion_validator (card 5d7c068b).

Covers AC4: clean quarter passes all 4; single-criterion failure drops
recommendation.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import promotion_validator as pv  # noqa: E402


def clean_quarter() -> dict:
    return {
        "quarter": "2026Q4",
        "orders": {"total": 100, "orphans": 0, "missed_exits": 0},
        "slippage_pips": {"median_vs_backtest": 0.3, "p95_vs_backtest": 1.0},
        "regime_hit_rate": 0.75,
        "incidents": {"sev1": 0, "sev2": 0, "traceable_to_active_edge": True},
    }


def test_clean_quarter_passes_all_four():
    v = pv.evaluate(clean_quarter())
    assert v["all_pass"] is True
    assert v["score"] == 1.0
    for name in ("order_fill_integrity", "slippage_distribution", "regime_hit_rate", "incident_cleanliness"):
        assert v["criteria"][name]["pass"], name
    assert v["recommendation"].startswith("PROMOTE")


def test_orphan_orders_fail_gate():
    d = clean_quarter()
    d["orders"]["orphans"] = 2
    v = pv.evaluate(d)
    assert v["all_pass"] is False
    assert v["criteria"]["order_fill_integrity"]["pass"] is False
    assert v["recommendation"].startswith("DO NOT PROMOTE")


def test_slippage_median_breach_drops_recommendation():
    d = clean_quarter()
    d["slippage_pips"]["median_vs_backtest"] = 0.6
    v = pv.evaluate(d)
    assert v["all_pass"] is False
    assert v["criteria"]["slippage_distribution"]["pass"] is False
    assert "slippage_distribution" in v["recommendation"]


def test_slippage_p95_breach_drops_recommendation():
    d = clean_quarter()
    d["slippage_pips"]["p95_vs_backtest"] = 1.6
    v = pv.evaluate(d)
    assert v["all_pass"] is False
    assert v["criteria"]["slippage_distribution"]["pass"] is False


def test_regime_hit_rate_below_70_fails():
    d = clean_quarter()
    d["regime_hit_rate"] = 0.69
    v = pv.evaluate(d)
    assert v["criteria"]["regime_hit_rate"]["pass"] is False
    assert v["recommendation"].startswith("DO NOT PROMOTE")


def test_boundary_regime_70_passes():
    d = clean_quarter()
    d["regime_hit_rate"] = 0.70
    v = pv.evaluate(d)
    assert v["criteria"]["regime_hit_rate"]["pass"] is True


def test_sev2_incident_traceable_fails():
    d = clean_quarter()
    d["incidents"]["sev2"] = 1
    v = pv.evaluate(d)
    assert v["criteria"]["incident_cleanliness"]["pass"] is False
    assert v["recommendation"].startswith("DO NOT PROMOTE")


def test_sev1_incident_untraceable_passes():
    d = clean_quarter()
    d["incidents"].update({"sev1": 1, "traceable_to_active_edge": False})
    v = pv.evaluate(d)
    assert v["criteria"]["incident_cleanliness"]["pass"] is True
    assert v["all_pass"] is True


def test_sample_2026q4_test_quarter_passes():
    v = pv.evaluate(pv.SAMPLE_2026Q4_TEST)
    assert v["all_pass"] is True


def test_weights_sum_to_one():
    assert abs(sum(pv.WEIGHTS.values()) - 1.0) < 1e-9


def test_cli_sample_quarter_exits_zero(capsys):
    rc = pv.main(["--quarter", "2026Q4-test"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recommendation:" in out
    assert "PROMOTE" in out


def test_render_contains_all_criteria():
    out = pv.render(pv.evaluate(clean_quarter()))
    for name in ("order_fill_integrity", "slippage_distribution", "regime_hit_rate", "incident_cleanliness"):
        assert name in out
