"""Front-door tests (SFA-3 — tournament → factory wiring).

The tests cover the deterministic contract surface:

* :func:`load_scorecard` accepts both canonical and bare-list payloads.
* :func:`select_survivors` applies the FTMO filter (max-DD < 10%,
  trade_count ≥ 20) and skips malformed rows.
* :func:`build_candidates` emits one :class:`CandidateSpec` per
  survivor with the right bars and OOS lock state.
* :func:`render_pilot_summary` produces a markdown table with the
  canonical columns.

The end-to-end :func:`run_front_door` path is exercised in the pilot
runner (``scripts/run_pilot_sfa3.py``); we keep the unit tests focused
on the pure components to stay deterministic and fast.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# These tests live in tests/tournament/.  Insert both src/ and
# src/forex_bot on import so the imports below are stable across
# layouts (CI vs local).

_HERE = Path(__file__).resolve()
for _p in (
    str(_HERE.parents[2] / "src"),
    str(_HERE.parents[2] / "src" / "forex_bot"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest.engine import Bar  # noqa: E402

from forex_bot.factory.validation_runner import (  # noqa: E402
    CandidateSpec,
    ValidationVerdict,
)
from tournament.front_door import (  # noqa: E402
    SurvivorCriteria,
    build_candidates,
    load_scorecard,
    render_pilot_summary,
    select_survivors,
)

# ── fixtures ───────────────────────────────────────────────────────────────


def _make_bar(close: float = 1.0, ts: int = 0) -> Bar:
    return Bar(
        time=datetime.fromtimestamp(ts or 1_700_000_000, tz=timezone.utc),
        open=close,
        high=close + 0.001,
        low=close - 0.001,
        close=close,
        volume=0.0,
        spread_pips=1.0,
    )


def _sample_rows() -> list[dict]:
    return [
        {"strategy_id": "alpha", "symbol": "EURUSD", "timeframe": "H1",
         "return_pct": 5.0, "max_dd_pct": 4.0, "trade_count": 30},
        {"strategy_id": "beta", "symbol": "EURUSD", "timeframe": "H1",
         "return_pct": -2.0, "max_dd_pct": 12.0, "trade_count": 25},
        {"strategy_id": "gamma", "symbol": "EURUSD", "timeframe": "H1",
         "return_pct": 3.0, "max_dd_pct": 7.0, "trade_count": 5},
        {"strategy_id": "delta", "symbol": "EURUSD", "timeframe": "H1",
         "return_pct": 8.0, "max_dd_pct": 6.0, "trade_count": 22},
        {"strategy_id": "broken", "symbol": "EURUSD", "timeframe": "H1"},
    ]


# ── load_scorecard ─────────────────────────────────────────────────────────


def test_load_scorecard_canonical_shape(tmp_path: Path) -> None:
    payload = {
        "columns": ["strategy_id", "max_dd_pct", "trade_count"],
        "rows": [
            {"strategy_id": "x", "max_dd_pct": 5.0, "trade_count": 10},
            {"strategy_id": "y", "max_dd_pct": 1.0, "trade_count": 30},
        ],
        "meta": {"harness_meta": {"bars_loaded": 100}},
    }
    p = tmp_path / "scorecard.json"
    p.write_text(json.dumps(payload))
    rows = load_scorecard(p)
    assert len(rows) == 2
    assert rows[0]["strategy_id"] == "x"


def test_load_scorecard_bare_list_shape(tmp_path: Path) -> None:
    payload = [{"strategy_id": "a"}, {"strategy_id": "b"}]
    p = tmp_path / "scorecard.json"
    p.write_text(json.dumps(payload))
    rows = load_scorecard(p)
    assert len(rows) == 2
    assert rows[0]["strategy_id"] == "a"


def test_load_scorecard_rejects_bad_shape(tmp_path: Path) -> None:
    p = tmp_path / "scorecard.json"
    p.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(ValueError, match="unrecognised scorecard shape"):
        load_scorecard(p)


# ── select_survivors ───────────────────────────────────────────────────────


def test_select_survivors_default_criteria() -> None:
    rows = _sample_rows()
    survivors = select_survivors(rows)
    # alpha: dd=4, trades=30  -> survives
    # gamma: dd=7, trades=5   -> fails (low trades)
    # delta: dd=6, trades=22  -> survives
    # beta: dd=12 -> fails
    # broken -> skipped (malformed)
    assert [r["strategy_id"] for r in survivors] == ["alpha", "delta"]


def test_select_survivors_custom_criteria() -> None:
    rows = _sample_rows()
    criteria = SurvivorCriteria(max_drawdown_pct=15.0, min_trades=4)
    survivors = select_survivors(rows, criteria)
    # beta (dd=12, trades=25) and gamma (dd=7, trades=5) also survive
    assert {r["strategy_id"] for r in survivors} == {"alpha", "beta", "gamma", "delta"}


def test_select_survivors_skips_malformed_rows() -> None:
    rows = _sample_rows()
    survivors = select_survivors(rows)
    assert all("strategy_id" in r for r in survivors)
    assert "broken" not in {r["strategy_id"] for r in survivors}


# ── build_candidates ───────────────────────────────────────────────────────


def _fake_factory(strategy_id: str) -> object:
    """Closure-shaped factory: each call returns a sentinel with .name + .evaluate."""
    class _Sentinel:
        name = strategy_id

        def evaluate(self, state):  # noqa: ARG002
            return None

    return _Sentinel()


def test_build_candidates_emits_one_per_survivor() -> None:
    rows = [
        {"strategy_id": "alpha", "symbol": "EURUSD", "timeframe": "H1"},
        {"strategy_id": "delta", "symbol": "EURUSD", "timeframe": "H1"},
    ]
    bars = [_make_bar()]
    candidates = build_candidates(
        rows,
        bars_by_pair_tf={("EURUSD", "H1"): bars},
        pair="EURUSD",
        timeframe="H1",
        strategy_factory=_fake_factory,
    )
    assert len(candidates) == 2
    assert {c.candidate_id for c in candidates} == {"alpha", "delta"}
    for c in candidates:
        assert isinstance(c, CandidateSpec)
        assert c.pair == "EURUSD"
        assert c.timeframe == "H1"
        assert c.bars is bars
        assert c.oos_unlocked is False  # spec §4.5 default
        assert c.params == {}


def test_build_candidates_oos_unlocked_propagates() -> None:
    rows = [{"strategy_id": "alpha", "symbol": "EURUSD", "timeframe": "H1"}]
    bars = [_make_bar()]
    candidates = build_candidates(
        rows,
        bars_by_pair_tf={("EURUSD", "H1"): bars},
        pair="EURUSD",
        timeframe="H1",
        strategy_factory=_fake_factory,
        oos_unlocked=True,
    )
    assert candidates[0].oos_unlocked is True


def test_build_candidates_skips_when_no_bars() -> None:
    rows = [{"strategy_id": "alpha", "symbol": "GBPUSD", "timeframe": "H1"}]
    candidates = build_candidates(
        rows,
        bars_by_pair_tf={("EURUSD", "H1"): [_make_bar()]},
        pair="EURUSD",
        timeframe="H1",
        strategy_factory=_fake_factory,
    )
    assert candidates == []  # no bars for GBPUSD → skipped


def test_build_candidates_template_round_trip() -> None:
    """Registry-backed template must build the strategy the factory returned."""
    rows = [{"strategy_id": "alpha", "symbol": "EURUSD", "timeframe": "H1"}]
    factory_obj = _fake_factory("alpha")
    candidates = build_candidates(
        rows,
        bars_by_pair_tf={("EURUSD", "H1"): [_make_bar()]},
        pair="EURUSD",
        timeframe="H1",
        strategy_factory=lambda sid: factory_obj,
    )
    assert candidates[0].template.archetype_id == "registry_alpha"
    built = candidates[0].template.build_strategy({}, "EURUSD")
    assert built is factory_obj
    # Template contract:
    template = candidates[0].template
    assert template.param_space == ()
    assert template.default_params() == {}
    assert template.regime_filter() is None


# ── render_pilot_summary ──────────────────────────────────────────────────


def _fake_verdict(
    candidate_id: str,
    *,
    tier: str = "REJECT",
    mean_sharpe: float = 0.0,
) -> ValidationVerdict:
    return ValidationVerdict(
        candidate_id=candidate_id,
        archetype_id=f"registry_{candidate_id}",
        pair="EURUSD",
        timeframe="H1",
        tier=tier,
        windows_passed=3,
        windows_total=5,
        total_trades=15,
        mean_sharpe=mean_sharpe,
        mean_profit_factor=1.1,
        mean_win_rate=0.55,
        max_drawdown=0.08,
        dsr_pvalue=0.7,
        n_trials_used=9,
        go_nogo=False,
        reason=f"fixture verdict for {candidate_id}",
        ran_at=datetime.now(timezone.utc).isoformat(),
    )


def test_render_pilot_summary_includes_ranked_table(tmp_path: Path) -> None:
    from tournament.front_door import FrontDoorResult

    verdicts = [
        _fake_verdict("alpha", tier="A", mean_sharpe=1.2),
        _fake_verdict("beta", tier="B", mean_sharpe=0.6),
        _fake_verdict("gamma", tier="INSUFFICIENT_DATA", mean_sharpe=0.1),
    ]
    result = FrontDoorResult(
        scorecard_path=tmp_path / "x.json",
        survivor_count=3,
        candidate_count=3,
        verdict_count=3,
        verdicts=verdicts,
        row_counts={"scorecard_rows": 17, "survivors": 3, "candidates": 3, "verdicts": 3, "written": 3},
        ranked_table=[
            {"rank": 1, "candidate_id": v.candidate_id, "archetype_id": v.archetype_id,
             "pair": v.pair, "timeframe": v.timeframe, "tier": v.tier,
             "windows_passed": v.windows_passed, "windows_total": v.windows_total,
             "total_trades": v.total_trades, "mean_sharpe": round(v.mean_sharpe, 4),
             "mean_profit_factor": round(v.mean_profit_factor, 4),
             "mean_win_rate": round(v.mean_win_rate, 4), "max_drawdown": round(v.max_drawdown, 4),
             "dsr_pvalue": round(v.dsr_pvalue, 4), "go_nogo": v.go_nogo, "reason": v.reason,
             "ran_at": v.ran_at}
            for v in sorted(verdicts, key=lambda v: v.tier)
        ],
        research_db=tmp_path / "research.duckdb",
    )
    md = render_pilot_summary(result, title="Test summary")
    assert "# Test summary" in md
    assert "Scorecard" in md
    assert "Tier distribution" in md
    # A is tier rank 0 → first row
    first_data_row = md.splitlines()[next(
        i for i, line in enumerate(md.splitlines()) if line.startswith("| #1")
    )]
    assert "alpha" in first_data_row
    assert "A" in first_data_row


def test_render_pilot_summary_no_verdicts(tmp_path: Path) -> None:
    from tournament.front_door import FrontDoorResult

    result = FrontDoorResult(
        scorecard_path=tmp_path / "x.json",
        survivor_count=0,
        candidate_count=0,
        verdict_count=0,
        verdicts=[],
        row_counts={"scorecard_rows": 17, "survivors": 0, "candidates": 0, "verdicts": 0, "written": 0},
        ranked_table=[],
        research_db=None,
    )
    md = render_pilot_summary(result, title="Empty")
    assert "Empty" in md
    assert "No verdicts" in md
