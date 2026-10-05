"""Tournament front door (SFA-3 — wire survivors into factory validation).

This module is the bridge between the tournament harness
(:mod:`tournament.harness`) and the factory validation pipeline
(:mod:`forex_bot.factory.validation_runner`).  It reads a tournament
scorecard JSON, applies the FTMO survivor filter, builds one
:class:`forex_bot.factory.validation_runner.CandidateSpec` per survivor,
runs the validation runner, persists verdicts to ``research.duckdb`` via
:func:`forex_bot.factory.storage.FactoryVerdictStore`, and emits a
summary dict suitable for reporting.

Design goals
------------
* **No hand-copying.**  The front door consumes tournament scorecards
  programmatically and emits ``CandidateSpec`` rows directly — the
  tournament survivor list never needs to be re-typed into the factory
  driver.
* **Deterministic.**  Survivor filtering uses a single FTMO-shaped rule
  (max-DD < 10% AND trade_count ≥ 20) consistent with the Liora ground
  rule (``scripts/offload/run_matrix_remote.py:_verdict_filter``) and the
  spec §7 exit gate.
* **Pure where possible.**  :func:`select_survivors` is pure; only
  :func:`run_front_door` touches I/O (DuckDB bars + research.duckdb).
* **OOS-locked by default.**  Per spec §4.5 the Jan-Jul 2026 window is
  locked; the front door filters bars to the pre-OOS window unless the
  caller passes ``oos_unlocked=True`` (validation mode).

Public API
----------
* :func:`load_scorecard`              — parse a tournament JSON to rows.
* :func:`select_survivors`            — apply FTMO filter to rows.
* :func:`build_candidates`            — survivors → ``CandidateSpec`` list.
* :func:`load_bars`                   — DuckDB → ``Bar`` list.
* :func:`run_front_door`              — scorecard → verdicts → persistence.
* :func:`render_pilot_summary`        — verdicts → markdown summary.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import duckdb
from backtest.engine import Bar

from forex_bot.factory.pipeline_config import (
    OOSConfig,
    PipelineConfig,
    default_pipeline_config,
)
from forex_bot.factory.spread_costs import (
    SpreadCostTable,
    default_spread_costs,
)
from forex_bot.factory.storage import FactoryVerdictStore
from forex_bot.factory.templates.registry_template import RegistryBackedTemplate
from forex_bot.factory.validation_runner import (
    CandidateSpec,
    ValidationRunner,
    ValidationVerdict,
)

logger = logging.getLogger("ayumi.tournament.front_door")


# ── Survivor criteria (Liora ground rule, spec §7 exit gate) ────────────────

DEFAULT_MAX_DRAWDOWN_PCT: float = 10.0
DEFAULT_MIN_TRADES: int = 20


@dataclass(frozen=True)
class SurvivorCriteria:
    """FTMO-shaped survivor filter.

    Default values mirror the Liora ground rule used by
    ``scripts/offload/run_matrix_remote.py:_verdict_filter`` (max-DD < 10%
    AND trade_count ≥ 20) — these are the most pragmatic ceiling that
    rejects obviously-broken strategies without over-pruning.  Override
    in tests / research runs by constructing a new
    :class:`SurvivorCriteria`.
    """

    max_drawdown_pct: float = DEFAULT_MAX_DRAWDOWN_PCT
    min_trades: int = DEFAULT_MIN_TRADES

    def accepts(self, *, max_dd_pct: float, trade_count: int) -> bool:
        return max_dd_pct < self.max_drawdown_pct and trade_count >= self.min_trades


# ── Scorecard loading ──────────────────────────────────────────────────────


def load_scorecard(scorecard_path: Path | str) -> list[dict[str, Any]]:
    """Load a tournament scorecard JSON; return the row payload as dicts.

    Tolerates the two shapes produced by the harness:

    * ``{"columns": [...], "rows": [{...}, ...], "meta": {...}}`` — the
      canonical :func:`tournament.scorecard.render_scorecard_json` output.
    * ``[{"strategy_id": ..., ...}, ...]`` — a bare list (rarely produced
      but seen in some node-side scripts).

    Raises :class:`ValueError` for unrecognised payloads so callers can
    fail loud (no silent-empty mode).
    """
    path = Path(scorecard_path)
    payload = json.loads(path.read_text())
    if isinstance(payload, dict) and "rows" in payload and isinstance(payload["rows"], list):
        return list(payload["rows"])
    if isinstance(payload, list):
        return list(payload)
    raise ValueError(
        f"unrecognised scorecard shape at {path}: "
        f"expected dict-with-rows or list, got {type(payload).__name__}"
    )


def select_survivors(
    rows: Iterable[dict[str, Any]],
    criteria: SurvivorCriteria | None = None,
) -> list[dict[str, Any]]:
    """Apply the FTMO survivor filter to tournament rows.

    Returns the surviving rows in the order they appeared in the input
    (deterministic).  Rows that lack ``max_dd_pct`` or ``trade_count``
    fields are skipped (treated as malformed — fail-loud for data
    quality).
    """
    criteria = criteria or SurvivorCriteria()
    survivors: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        try:
            max_dd = float(row["max_dd_pct"])
            trades = int(row["trade_count"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if criteria.accepts(max_dd_pct=max_dd, trade_count=trades):
            survivors.append(row)
    if skipped:
        logger.warning("select_survivors: skipped %d malformed rows", skipped)
    return survivors


# ── Bar loading ────────────────────────────────────────────────────────────


def load_bars(
    db_path: Path | str,
    *,
    symbol: str,
    timeframe: str,
    end_date: date | None = None,
    include_oos: bool = False,
    oos_config: OOSConfig | None = None,
) -> list[Bar]:
    """Load bars from DuckDB into :class:`backtest.engine.Bar` instances.

    The default :attr:`end_date` is :attr:`OOSConfig.holdout_start`
    (Jan 1, 2026) so the OOS lock is honoured without ceremony.
    :attr:`include_oos=True` widens the window to ``9999-12-31`` but
    only when the caller passes ``oos_unlocked=True`` to
    :func:`build_candidates` (the validator's own OOS guard will then
    refuse bars unless ``candidate.oos_unlocked`` is also True).
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"DuckDB bar source not found: {db_path}")
    cfg = oos_config or default_pipeline_config().oos
    if end_date is None:
        # Default to the day BEFORE OOS starts so the OOS lock is honoured
        # without the front door having to remember the off-by-one.  The
        # validation runner's own OOS guard then walks every bar and
        # rejects any that fall in :attr:`OOSConfig.holdout_start`..
        # :attr:`OOSConfig.holdout_end` (inclusive on both ends).
        if include_oos:
            end_date = date(9999, 12, 31)
        else:
            end_date = cfg.holdout_start - timedelta(days=1)

    end_ts = int(
        datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc).timestamp()
        + 86399
    )

    query = (
        "SELECT timestamp_utc, open, high, low, close, volume, spread_pips "
        "FROM bars WHERE symbol = ? AND timeframe = ? "
        "AND timestamp_utc <= ? ORDER BY timestamp_utc ASC"
    )
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(query, [symbol.upper(), timeframe, end_ts]).fetch_df()
    finally:
        con.close()
    if df.empty:
        raise ValueError(
            f"no {timeframe} bars for {symbol} up to {end_date} (db={db_path})"
        )

    bars: list[Bar] = []
    for _, row in df.iterrows():
        ts = int(row["timestamp_utc"])
        bars.append(
            Bar(
                time=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]) if row["volume"] is not None else 0.0,
                spread_pips=float(row["spread_pips"]) if row["spread_pips"] is not None else 0.0,
            )
        )
    return bars


# ── Candidate building ─────────────────────────────────────────────────────


def _strategy_id_from_row(row: dict[str, Any]) -> str:
    sid = row.get("strategy_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError(f"scorecard row missing strategy_id: {row!r}")
    return sid


def _pair_from_row(row: dict[str, Any], default: str) -> str:
    sym = row.get("symbol")
    return sym if isinstance(sym, str) and sym else default


def _timeframe_from_row(row: dict[str, Any], default: str) -> str:
    tf = row.get("timeframe")
    return tf if isinstance(tf, str) and tf else default


def build_candidates(
    survivors: Sequence[dict[str, Any]],
    *,
    bars_by_pair_tf: dict[tuple[str, str], list[Bar]],
    pair: str,
    timeframe: str,
    strategy_factory: Callable[[str], Any],
    pipeline_config: PipelineConfig | None = None,
    oos_unlocked: bool = False,
) -> list[CandidateSpec]:
    """Build one :class:`CandidateSpec` per survivor.

    Parameters
    ----------
    survivors
        Scorecard rows that passed the FTMO survivor filter.
    bars_by_pair_tf
        ``{(symbol, timeframe): {pool, Bar}``} — bar windows keyed by
        ``(pair, timeframe)``.  ``build_candidates`` looks up the right
        window per survivor and reuses the same list reference for all
        survivors on the same ``(pair, timeframe)``.
    pair, timeframe
        Fallback values when the row does not name its own.
    strategy_factory
        ``Callable[[strategy_id], Any]`` — returns the strategy instance
        for a given ``strategy_id``.  Typically a closure around
        :func:`tournament.harness._build_strategy_instance`.
    pipeline_config
        Default :func:`forex_bot.factory.pipeline_config.default_pipeline_config`
        when ``None``.  Used to surface the OOS guard (validation
        runner enforces it on each call).
    oos_unlocked
        Forwarded to every emitted :class:`CandidateSpec`.  ``False`` is
        the safe default (spec §4.5 — Jan-Jul 2026 locked).
    """
    cfg = pipeline_config or default_pipeline_config()
    out: list[CandidateSpec] = []
    for row in survivors:
        sid = _strategy_id_from_row(row)
        row_pair = _pair_from_row(row, pair)
        row_tf = _timeframe_from_row(row, timeframe)
        bars = bars_by_pair_tf.get((row_pair.upper(), row_tf))
        if not bars:
            logger.warning(
                "build_candidates: no bars for (%s, %s) — skipping survivor %s",
                row_pair,
                row_tf,
                sid,
            )
            continue
        strategy = strategy_factory(sid)
        # Bind `strategy` into a default argument so each loop iteration
        # captures its own value (B023 — closure-by-reference would
        # otherwise alias every template's builder to the last iteration's
        # strategy instance).
        bound_strategy = strategy
        template = RegistryBackedTemplate(
            archetype_id=f"registry_{sid}",
            description=f"Registry-backed pass-through for tournament survivor {sid}",
            default_pairs=(row_pair,),
            default_timeframes=(row_tf,),
            regime_affinity=("TRENDING", "CHOPPY", "VOLATILE", "QUIET"),
            # B023 closure-by-reference guard: default arg captures the
            # strategy instance at template-construction time so each
            # loop iteration gets its own builder.  ``type: ignore[misc]``
            # silences mypy's lambda-inference complaint — the closure
            # cannot be typed statically.
            builder=lambda params, pair, _s=bound_strategy: _s,  # type: ignore[misc]
        )
        out.append(
            CandidateSpec(
                candidate_id=sid,
                template=template,
                params={},
                pair=row_pair,
                timeframe=row_tf,
                bars=bars,
                oos_unlocked=oos_unlocked,
            )
        )
        _ = cfg  # cfg only used for OOS window consistency; runner enforces
    return out


# ── Front-door orchestration ───────────────────────────────────────────────


@dataclass
class FrontDoorResult:
    """Outcome of a single :func:`run_front_door` invocation."""

    scorecard_path: Path
    survivor_count: int = 0
    candidate_count: int = 0
    verdict_count: int = 0
    verdicts: list[ValidationVerdict] = field(default_factory=list)
    row_counts: dict[str, int] = field(default_factory=dict)
    ranked_table: list[dict[str, Any]] = field(default_factory=list)
    research_db: Path | None = None

    def tier_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.verdicts:
            out[v.tier] = out.get(v.tier, 0) + 1
        return out


def run_front_door(
    *,
    scorecard_path: Path | str,
    db_path: Path | str,
    pair: str,
    timeframe: str,
    research_db: Path | str,
    strategy_factory: Callable[[str], Any],
    pipeline_config: PipelineConfig | None = None,
    spread_costs: SpreadCostTable | None = None,
    criteria: SurvivorCriteria | None = None,
    oos_unlocked: bool = False,
    include_oos: bool = False,
) -> FrontDoorResult:
    """End-to-end: scorecard → survivors → validation → persistence.

    Parameters
    ----------
    scorecard_path
        Tournament JSON (output of :func:`scripts/run_tournament.py`).
    db_path
        DuckDB bar source (``data/ayumi_market.duckdb`` or override).
    pair, timeframe
        Default fallback when the scorecard row does not name its own.
    research_db
        ``research.duckdb`` path — verdicts are persisted via
        :class:`FactoryVerdictStore`.  The store is idempotent (natural
        key + ``INSERT OR REPLACE``).
    strategy_factory
        ``(strategy_id) -> strategy_instance``.  Use a closure around
        :func:`tournament.harness._build_strategy_instance` for the
        canonical wiring.
    criteria
        Custom survivor filter; defaults to FTMO-shaped
        (:class:`SurvivorCriteria` defaults).
    oos_unlocked, include_oos
        Forwarded to candidate building / bar loading.  Both default
        to ``False`` — the spec §4.5 lock is honoured.
    """
    scorecard_path = Path(scorecard_path)
    cfg = pipeline_config or default_pipeline_config()
    spread = spread_costs or default_spread_costs()

    rows = load_scorecard(scorecard_path)
    survivors = select_survivors(rows, criteria)
    logger.info(
        "front_door: scorecard=%s rows=%d survivors=%d",
        scorecard_path,
        len(rows),
        len(survivors),
    )

    # Load bars once per (pair, timeframe) — reused for every survivor.
    bars_by_pair_tf: dict[tuple[str, str], list[Bar]] = {}
    for s in survivors:
        row_pair = _pair_from_row(s, pair)
        row_tf = _timeframe_from_row(s, timeframe)
        key = (row_pair.upper(), row_tf)
        if key in bars_by_pair_tf:
            continue
        bars_by_pair_tf[key] = load_bars(
            db_path,
            symbol=row_pair,
            timeframe=row_tf,
            include_oos=include_oos,
            oos_config=cfg.oos,
        )

    candidates = build_candidates(
        survivors,
        bars_by_pair_tf=bars_by_pair_tf,
        pair=pair,
        timeframe=timeframe,
        strategy_factory=strategy_factory,
        pipeline_config=cfg,
        oos_unlocked=oos_unlocked,
    )

    runner = ValidationRunner(
        pipeline_config=cfg,
        spread_costs=spread,
        cell_count=len(candidates),
    )
    verdicts = runner.run_batch(candidates) if candidates else []

    research_db = Path(research_db)
    written = 0
    if verdicts and research_db.parent.is_dir():
        store = FactoryVerdictStore(research_db)
        written = store.write_verdicts(verdicts)

    ranked = _ranked_table(verdicts)
    result = FrontDoorResult(
        scorecard_path=scorecard_path,
        survivor_count=len(survivors),
        candidate_count=len(candidates),
        verdict_count=len(verdicts),
        verdicts=list(verdicts),
        row_counts={
            "scorecard_rows": len(rows),
            "survivors": len(survivors),
            "candidates": len(candidates),
            "verdicts": len(verdicts),
            "written": written,
        },
        ranked_table=ranked,
        research_db=research_db if written else None,
    )
    return result


def _ranked_table(verdicts: Sequence[ValidationVerdict]) -> list[dict[str, Any]]:
    """Sort verdicts by tier promotion (A→B→C→INSUFFICIENT→REJECT), then mean_sharpe."""
    tier_rank = {"A": 0, "B": 1, "C": 2, "INSUFFICIENT_DATA": 3, "REJECT": 4}
    sorted_v = sorted(
        verdicts,
        key=lambda v: (
            tier_rank.get(v.tier, 99),
            -float(v.mean_sharpe),
            v.candidate_id,
        ),
    )
    out: list[dict[str, Any]] = []
    for v in sorted_v:
        out.append(
            {
                "rank": len(out) + 1,
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
                "dsr_pvalue": round(v.dsr_pvalue, 4),
                "go_nogo": v.go_nogo,
                "reason": v.reason,
                "ran_at": v.ran_at,
            }
        )
    return out


# ── Markdown summary ──────────────────────────────────────────────────────


def render_pilot_summary(
    result: FrontDoorResult,
    *,
    title: str,
    scorecard_meta: dict[str, Any] | None = None,
) -> str:
    """Render a markdown summary suitable for ``docs/research/``."""
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- **Scorecard**: `{result.scorecard_path}`")
    lines.append(
        f"- **Row counts**: scorecard_rows={result.row_counts.get('scorecard_rows', 0)} "
        f"survivors={result.row_counts.get('survivors', 0)} "
        f"candidates={result.row_counts.get('candidates', 0)} "
        f"verdicts={result.row_counts.get('verdicts', 0)} "
        f"written={result.row_counts.get('written', 0)}"
    )
    if scorecard_meta:
        harness = scorecard_meta.get("harness_meta", {})
        if harness:
            lines.append(
                f"- **Tournament window**: "
                f"{harness.get('start_date', '?')} → {harness.get('end_date', '?')} "
                f"({harness.get('bars_loaded', '?')} bars)"
            )
        symbol = harness.get("symbol") if harness else None
        timeframe = harness.get("timeframe") if harness else None
        if symbol:
            lines.append(f"- **Symbol/Timeframe**: {symbol}/{timeframe}")
    tier_counts = result.tier_counts()
    if tier_counts:
        ordered = ["A", "B", "C", "INSUFFICIENT_DATA", "REJECT"]
        lines.append(
            "- **Tier distribution**: "
            + ", ".join(f"{t}={tier_counts.get(t, 0)}" for t in ordered if tier_counts.get(t, 0))
        )
    if result.research_db is not None:
        lines.append(f"- **Research DB**: `{result.research_db}`")
    lines.append("")

    lines.append("## Ranked table")
    lines.append("")
    if not result.ranked_table:
        lines.append("_No verdicts — all candidates failed pre-validation._")
        lines.append("")
        return "\n".join(lines)

    headers = [
        "Rank",
        "Candidate",
        "Archetype",
        "Pair/TF",
        "Tier",
        "Sharpe",
        "PF",
        "WinRate",
        "MaxDD",
        "Trades",
        "DSR p",
        "Go",
    ]
    body = [
        [
            f"#{row['rank']}",
            row["candidate_id"],
            row["archetype_id"],
            f"{row['pair']}/{row['timeframe']}",
            row["tier"],
            f"{row['mean_sharpe']:+.3f}",
            f"{row['mean_profit_factor']:.3f}",
            f"{row['mean_win_rate']:.3f}",
            f"{row['max_drawdown']:.3f}",
            row["total_trades"],
            f"{row['dsr_pvalue']:.3f}",
            "yes" if row["go_nogo"] else "no",
        ]
        for row in result.ranked_table
    ]
    lines.extend(_format_md_table(headers, body))
    lines.append("")
    lines.append("## Tier definitions (spec §4.6)")
    lines.append("")
    lines.append("- **A**: PBO < 0.30 (full promotion)")
    lines.append("- **B**: PBO 0.30 ≤ 0.50 (marginal)")
    lines.append("- **C**: PBO ≥ 0.50 (capped; promotion blocked)")
    lines.append("- **INSUFFICIENT_DATA**: < 10 trades (Liora ground rule)")
    lines.append("- **REJECT**: WF / bridge / OOS guard failed")
    return "\n".join(lines)


def _format_md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Format a small markdown table (GitHub-flavoured)."""
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


__all__ = [
    "DEFAULT_MAX_DRAWDOWN_PCT",
    "DEFAULT_MIN_TRADES",
    "FrontDoorResult",
    "SurvivorCriteria",
    "build_candidates",
    "load_bars",
    "load_scorecard",
    "render_pilot_summary",
    "run_front_door",
    "select_survivors",
]
