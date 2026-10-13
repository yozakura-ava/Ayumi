"""Scorecard — FTMO-aware metric computation + JSON/console rendering.

Card db04d5b5 (walking skeleton).  The scorecard module takes per-strategy
trade lists (output from ``tournament.harness._simulate_trades``) and emits
ranked rows with the canonical column schema:

    strategy_id, symbol, timeframe, return_pct, max_dd_pct,
    daily_dd_breaches (3%), total_dd_breaches (10%), trade_count, source

Two FTMO columns are deliberately SEPARATE — daily breaches (3% per-day
limit) and total breaches (10% total-DD limit) are not aliased and are
not collapsed.  Ranking is deterministic (return_pct DESC, trade_count
DESC, strategy_id ASC) so re-runs produce identical ordering.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # pragma: no cover — import kept cheap at runtime
    from forex_bot.factory.risk_adjusted_ranking import (
        SelectionSetRanking,
        SelectionSetScore,
    )


# FTMO defaults are the canonical source of truth for the thresholds.  We
# import lazily so this module remains importable without pulling in
# risk.ftmo_params (which transitively pulls config.sessions etc. and
# inflates the harness cold-start time).
def _ftmo_daily_dd_pct() -> float:
    from risk.ftmo_params import FTMO_DAILY_DD_LIMIT_PCT

    return FTMO_DAILY_DD_LIMIT_PCT * 100.0  # 0.03 -> 3.0


def _ftmo_total_dd_pct() -> float:
    from risk.ftmo_params import FTMO_TOTAL_DD_LIMIT_PCT

    return FTMO_TOTAL_DD_LIMIT_PCT * 100.0  # 0.10 -> 10.0


SCORECARD_ROW_COLUMNS: tuple[str, ...] = (
    "strategy_id",
    "symbol",
    "timeframe",
    "return_pct",
    "max_dd_pct",
    "daily_dd_breaches",
    "total_dd_breaches",
    "trade_count",
    "source",
    # Card c0f284b8: carried in the canonical columns (not only in to_dict())
    # so the rendered JSON "columns" header stays truthful about what each row
    # actually contains.
    "cost_modeled",
    # Card 82b40a95: the statistical verdict columns.  They are ``None``
    # on an unranked single-window row — which is the honest value, not a
    # placeholder: it says "no DSR/BH-FDR pass was computed for this row".
    # The memo tooling reads these to refuse to present an ordering it
    # cannot justify (see :func:`ranking_ordering_statement`).
    "rank",
    "dsr_pvalue",
    "fdr_qvalue",
    "bh_rejected",
    "n_trials_cell",
    "n_trials_selection",
)

#: ``Scorecard.meta["ranking"]["method"]`` value that marks an ordering
#: produced by the real BH-FDR + DSR selection-set pass.
RANKING_METHOD_STATISTICAL = "dsr_bh_fdr_selection_set"

#: ``Scorecard.meta["ranking"]["method"]`` value for a scorecard the
#: harness emitted without any ranking.  Anything else is rejected by
#: :func:`ranking_ordering_statement`.
RANKING_METHOD_NONE = "unranked_no_trials"

#: The sentence the memo is required to print when no real ranking exists.
NO_RANKING_STATEMENT = (
    "harness produced no ranking — rows are UNORDERED and must not be "
    "presented as a leaderboard (no DSR/BH-FDR selection-set pass was run)"
)


@dataclass
class ScorecardRow:
    """One row in the tournament scorecard.

    All fields are serializable to JSON.  ``rank`` is set by
    :func:`rank_scorecard_rows` after sorting.
    """

    strategy_id: str
    symbol: str
    timeframe: str
    return_pct: float
    max_dd_pct: float
    daily_dd_breaches: int
    total_dd_breaches: int
    trade_count: int
    source: str
    cost_modeled: bool = True
    rank: int = 0
    # ── Card 82b40a95: statistical verdict fields ──
    # Populated only by :func:`ranked_scorecard_from_selection_set`.
    # Left ``None`` on unranked rows so an absent verdict is
    # distinguishable from a computed verdict of 1.0.
    dsr_pvalue: float | None = None
    fdr_qvalue: float | None = None
    bh_rejected: bool | None = None
    #: Trials in this row's own cell (the BH ``m``).
    n_trials_cell: int | None = None
    #: Trials across the whole selection set — the DSR deflation
    #: denominator.  Compare ``n_trials_selection`` against
    #: ``n_trials_cell`` to spot a verdict that was deflated too weakly.
    n_trials_selection: int | None = None

    @property
    def is_statistically_ranked(self) -> bool:
        """True iff this row carries a real DSR/BH-FDR verdict."""
        return self.dsr_pvalue is not None and self.fdr_qvalue is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Scorecard:
    """Ranked, ordered collection of ScorecardRows with optional metadata."""

    rows: list[ScorecardRow]
    meta: dict = field(default_factory=dict)

    def __iter__(self) -> Iterable[ScorecardRow]:  # type: ignore[override]
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int) -> ScorecardRow:
        return self.rows[idx]


# ── Equity curve + FTMO metric computation ───────────────────────────────────


@dataclass
class _EquitySnapshot:
    """Trade-level equity snapshot used for daily-DD bookkeeping."""

    bar_index: int
    date: date
    delta_fraction: float  # realized fraction of equity at that point


def _build_equity_curve(
    starting_equity: float,
    trades: list[dict],
    dates: list[str],
    trade_entry_bars: list[int],
) -> list[tuple[date, float]]:
    """Build a date -> equity mapping.

    Returns one entry per date with closed trades; entry equity for each
    day is the cumulative equity from previous days.  Empty days are
    omitted (caller treats gaps as carry-forward).
    """
    # Trade order: walks in bar-index order, but we don't strictly rely on it
    # for the date grouping — we group by the trade's ENTRY date instead.
    by_date: dict[date, float] = {}
    for trade, entry_bar in zip(trades, trade_entry_bars, strict=True):
        d = date.fromisoformat(dates[entry_bar])
        by_date[d] = by_date.get(d, 0.0) + trade["pnl_fraction"]

    equity = starting_equity
    curve: list[tuple[date, float]] = []
    for d in sorted(by_date):
        equity = equity * (1.0 + by_date[d])
        curve.append((d, equity))
    return curve


def _max_drawdown_pct(curve: list[tuple[date, float]]) -> float:
    """Peak-to-trough max drawdown as a percentage of starting equity.

    For the skeleton, starting equity is normalized to 1.0 and curve values
    are floats; the returned ``max_dd_pct`` is on the same scale as
    ``FTMO_TOTAL_DD_LIMIT_PCT * 100`` (= 10.0).  A curve that never trades
    is 0.0.
    """
    if not curve:
        return 0.0
    peak = curve[0][1]
    max_dd = 0.0
    for _, equity in curve:
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd * 100.0


def _daily_dd_breach_counts(
    curve: list[tuple[date, float]],
    daily_limit_pct: float,
    total_limit_pct: float,
    starting_equity: float = 1.0,
) -> tuple[int, int]:
    """Count distinct trading days where intra-day DD crossed the limits.

    Per-day equity is approximated as end-of-day equity (skeleton caveat:
    intraday equity snapshots are out of scope for this card and form the
    second decomposition card).  A "breach" day is one whose equity
    decline from prior day's equity (or starting equity, day 1) exceeded
    the threshold.  Days are counted by calendar date, NOT by bar — a
    single large gap is one breach, not many.

    ``starting_equity`` defaults to 1.0 (curve-normalized baseline).
    Callers that pre-scale the curve (e.g. by passing a non-1.0 account
    baseline) MUST pass the matching starting equity so day 1's
    comparison uses the correct baseline — otherwise a 4% loss on day 1
    of a 10000.0-baseline curve is silently mis-counted as a 0% drop.
    """
    if not curve:
        return 0, 0

    daily_breaches = 0
    total_breaches = 0
    # Day 1 is compared against starting equity (baseline).  Day N+1 is
    # compared against day N's end-of-day equity (track prior day's
    # close unconditionally — both gain and loss days update the
    # baseline, otherwise a loss streak would overstate day-2's drop).
    prev_equity = starting_equity
    for _, equity in curve:
        if prev_equity > 0:
            day_dd_pct = (prev_equity - equity) / prev_equity * 100.0
            if day_dd_pct >= daily_limit_pct:
                daily_breaches += 1
        prev_equity = equity

    # Total-DD breach: sweep the curve again, counting distinct dates
    # whose trough crossed 10%.
    peak = curve[0][1]
    breached_dates: set[date] = set()
    for d, equity in curve:
        if equity > peak:
            peak = equity
        if peak <= 0:
            continue
        if (peak - equity) / peak * 100.0 >= total_limit_pct:
            breached_dates.add(d)
    total_breaches = len(breached_dates)

    return daily_breaches, total_breaches


def build_scorecard_row(
    *,
    strategy_id: str,
    symbol: str,
    timeframe: str,
    starting_equity: float,
    trades: list[dict],
    dates: list[str],
    trade_entry_bars: list[int],
    source: str,
) -> ScorecardRow:
    """Compute one row's metrics from the trade list.

    ``trade_entry_bars[i]`` is the bar index where trade ``i`` was opened;
    ``dates[bar_idx]`` is the ISO date string for that bar.  Both are
    produced by ``tournament.harness._simulate_trades`` and
    ``tournament.harness._extract_signals_from_strategy``.

    Card c0f284b8: the row carries ``cost_modeled``, and it is FAIL-CLOSED.
    It is ``True`` only when ``trades`` is non-empty AND every trade positively
    proves its cost was applied (``cost_modeled=True`` on the trade dict).  A
    trade that could not be costed (no cost model supplied, or an unsizable
    position), a trade dict predating the flag, and the 0-trade case all yield
    ``False`` — we never assume costs were modeled.  Rows with ``False`` are
    excluded from verdict-grade ranking by
    :func:`tournament.harness.row_is_cost_modeled`.
    """
    # Cost provenance (card c0f284b8): a row is verdict-grade only when every
    # trade in it carried a modeled cost, AND it traded at all.
    #
    # Rin iter-1 LOW (fail-open defaults + all([])): a 0-trade row used to come
    # out True via all([]), and a trade dict missing the key defaulted to True.
    # Both are optimistic guesses about costs we never actually observed.  Now
    # it is fail-closed: no trades, or any trade whose provenance is absent,
    # means NOT cost-modeled -- so it cannot enter a verdict ranking.
    cost_modeled = bool(trades) and all(
        t.get("cost_modeled", False) for t in trades
    )

    # Edge case 4: 0 trades.  Still emit a valid row with zeros.
    if not trades:
        return ScorecardRow(
            strategy_id=strategy_id,
            symbol=symbol,
            timeframe=timeframe,
            return_pct=0.0,
            max_dd_pct=0.0,
            daily_dd_breaches=0,
            total_dd_breaches=0,
            trade_count=0,
            source=source,
            cost_modeled=cost_modeled,
        )

    daily_limit = _ftmo_daily_dd_pct()
    total_limit = _ftmo_total_dd_pct()

    # Trade P&L compounds on top of starting equity.
    starting = float(starting_equity) if starting_equity > 0 else 1.0
    final_equity = starting
    for trade in trades:
        final_equity *= 1.0 + float(trade.get("pnl_fraction", 0.0))
    return_pct = (final_equity / starting - 1.0) * 100.0

    # Equity curve + DD metrics
    curve = _build_equity_curve(starting, trades, dates, trade_entry_bars)
    max_dd_pct = _max_drawdown_pct(curve)
    daily_breaches, total_breaches = _daily_dd_breach_counts(
        curve,
        daily_limit_pct=daily_limit,
        total_limit_pct=total_limit,
        starting_equity=starting,
    )

    return ScorecardRow(
        strategy_id=strategy_id,
        symbol=symbol,
        timeframe=timeframe,
        return_pct=round(return_pct, 4),
        max_dd_pct=round(max_dd_pct, 4),
        daily_dd_breaches=int(daily_breaches),
        total_dd_breaches=int(total_breaches),
        trade_count=len(trades),
        source=source,
        cost_modeled=cost_modeled,
    )


# ── Ranking ──────────────────────────────────────────────────────────────────


def rank_scorecard_rows(rows: list[ScorecardRow]) -> Scorecard:
    """Sort and assign 1-based rank.  Deterministic tie-breaking.

    .. deprecated::
        Sprint C 1b.3 (card cc90a6b6) removed this raw-return sort in
        favour of Benjamini-Hochberg FDR on per-trial return series.
        See :mod:`forex_bot.factory.risk_adjusted_ranking`.  This
        function is now a loud-failure shim that raises
        :exc:`forex_bot.factory.risk_adjusted_ranking.RawReturnSortRemoved`
        so silent regressions are caught.  Callers must migrate to
        :func:`forex_bot.factory.risk_adjusted_ranking.rank_candidates_by_trial_returns`
        or the ``TrialReturnStore`` bridge
        :func:`forex_bot.factory.risk_adjusted_ranking.rank_from_trial_return_store`.

    The previous sort keys (return_pct DESC, trade_count DESC,
    strategy_id ASC) implemented *selection-on-the-maximum* under
    multiple testing — a procedure that, under the crypto factory's
    10k+ Optuna trials per sweep, silently promoted the best noise
    sample.  BH-FDR on per-trial return series is the correct
    replacement.

    Sort keys that **were** used (descending unless marked):
        1. ``return_pct`` (DESC) — primary quality metric
        2. ``trade_count`` (DESC) — more trades = higher confidence
        3. ``strategy_id`` (ASC) — final, deterministic by name
    """
    # Sprint C 1b.3 (card cc90a6b6): raw-return sort is removed.
    # Raise loud so the existing tournament harness / CLI cannot
    # silently keep using a metric the BH-FDR ranker replaced.
    from forex_bot.factory.risk_adjusted_ranking import (  # late-import to avoid cycle
        RawReturnSortRemoved,
    )

    raise RawReturnSortRemoved(
        "rank_scorecard_rows is removed (Sprint C 1b.3 / card cc90a6b6). "
        "Migrate to forex_bot.factory.risk_adjusted_ranking."
    )


# ── Card 82b40a95: selection-set ranking + memo guard ──────────────────────


def ranked_scorecard_from_selection_set(
    ranking: "SelectionSetRanking",
    *,
    rows_by_candidate_id: dict[str, "ScorecardRow"] | None = None,
    meta: dict | None = None,
) -> Scorecard:
    """Build a *verdict-grade* ranked :class:`Scorecard` from a selection set.

    This is the only sanctioned way for the tournament path to present an
    ordering.  It consumes a
    :class:`~forex_bot.factory.risk_adjusted_ranking.SelectionSetRanking`
    (BH-FDR ordering plus DSR deflation) and:

    * orders rows by **deflated significance**, never by ``return_pct``;
    * stamps each row with its DSR p-value, BH q-value, and both trial
      counts (per-cell and selection-set-wide);
    * records the ordering provenance in ``meta["ranking"]`` so the memo
      tooling can prove the order was not hand-rolled.

    Parameters
    ----------
    ranking
        The whole-selection-set result.
    rows_by_candidate_id
        Optional ``candidate_id -> ScorecardRow`` map from the harness's
        backtest run.  When a candidate has no row, a placeholder row is
        synthesised with ``source="selection_set_statistical_only"`` and
        zeroed performance columns — the ``source`` marker is the signal
        that its performance was **not** backtested, so it cannot be
        mistaken for a measured return of 0%.
    meta
        Optional extra metadata merged under ``meta``.

    Returns
    -------
    Scorecard
        Ranked rows (1-based) with the statistical verdict columns set
        and ``meta["ranking"]["method"] == RANKING_METHOD_STATISTICAL``.
    """
    rows_by_candidate_id = rows_by_candidate_id or {}

    # Candidate ids are only unique WITHIN a cell by default ("trial_0"
    # is generated positionally per cell).  Two cells would otherwise
    # render two different candidates under one identical label, and a
    # memo reader would read them as the same row.  Qualify the label
    # whenever the set actually contains a collision.
    all_ids = [s.candidate_id for s in ranking.all_scores()]
    id_counts: dict[str, int] = {}
    for cid in all_ids:
        id_counts[cid] = id_counts.get(cid, 0) + 1
    qualified_ids = {cid for cid, n in id_counts.items() if n > 1}

    ordered: list[ScorecardRow] = []
    for pos, score in enumerate(ranking.by_discovery(), start=1):
        base = rows_by_candidate_id.get(score.candidate_id)
        if base is None:
            archetype, pair, timeframe = score.cell_key
            label = score.candidate_id
            if score.candidate_id in qualified_ids:
                label = f"{archetype}|{pair}|{timeframe}|{score.candidate_id}"
            base = ScorecardRow(
                strategy_id=label or archetype,
                symbol=pair,
                timeframe=timeframe,
                return_pct=0.0,
                max_dd_pct=0.0,
                daily_dd_breaches=0,
                total_dd_breaches=0,
                trade_count=0,
                source="selection_set_statistical_only",
                cost_modeled=False,
            )
        base.rank = pos
        base.dsr_pvalue = float(score.dsr_pvalue)
        base.fdr_qvalue = float(score.q_value)
        base.bh_rejected = bool(score.bh_rejected)
        base.n_trials_cell = int(score.n_trials_cell)
        base.n_trials_selection = int(score.n_trials_selection)
        ordered.append(base)

    payload_meta: dict = dict(meta or {})
    payload_meta["ranking"] = {
        "method": RANKING_METHOD_STATISTICAL,
        "n_trials": int(ranking.n_trials),
        "n_cells": int(ranking.n_cells),
        "n_candidates": int(ranking.n_candidates),
        "n_discoveries": int(ranking.n_discoveries),
        "alpha": float(ranking.alpha),
        "order_key": (
            "is_discovery desc, dsr_pvalue asc, fdr_qvalue asc, "
            "candidate_id asc"
        ),
    }
    return Scorecard(rows=ordered, meta=payload_meta)


def ranking_ordering_statement(scorecard: Scorecard) -> str:
    """Return a sentence describing — or refusing — this scorecard's order.

    Card 82b40a95 scope item 3: the memo tooling must **refuse to present
    a hand-rolled ordering**.  A scorecard is only allowed to describe
    itself as ranked when :func:`ranked_scorecard_from_selection_set`
    produced it — i.e. when ``meta["ranking"]["method"]`` is
    :data:`RANKING_METHOD_STATISTICAL` and the rows actually carry
    statistical verdicts.  Anything else (the standalone single-window
    harness, a scorecard hand-built by a caller, rows all left at
    ``rank=0``) returns :data:`NO_RANKING_STATEMENT` verbatim, so an
    unordered set can never be rendered as a leaderboard.
    """
    ranking_meta = (scorecard.meta or {}).get("ranking")
    if not isinstance(ranking_meta, dict):
        return NO_RANKING_STATEMENT
    if ranking_meta.get("method") != RANKING_METHOD_STATISTICAL:
        return NO_RANKING_STATEMENT
    if not scorecard.rows:
        return NO_RANKING_STATEMENT
    if not all(row.is_statistically_ranked for row in scorecard.rows):
        return NO_RANKING_STATEMENT
    if all(row.rank <= 0 for row in scorecard.rows):
        return NO_RANKING_STATEMENT

    n_trials = ranking_meta.get("n_trials")
    n_discoveries = ranking_meta.get("n_discoveries")
    return (
        f"ranked by DSR + BH-FDR over N={n_trials} trials "
        f"({n_discoveries} discoveries) — "
        f"{ranking_meta.get('order_key', 'unknown order')}"
    )


# ── Renderers ────────────────────────────────────────────────────────────────


def render_console_table(scorecard: Scorecard) -> str:
    """Format the scorecard as a tabulate-friendly list-of-lists.

    Falls back to a hand-rolled ASCII table when ``tabulate`` is not
    installed (the project's CI may not have it).  Tests pass the rendered
    output to ``tabulate.tabulate`` themselves.

    Card 82b40a95: an unranked row (``rank == 0``) renders its rank cell
    as ``—`` rather than ``#0``.  ``#0`` reads like "top of the
    leaderboard" and is exactly the hand-rolled-ordering illusion the
    card forbids; the dash says "this row has no position", and the
    DSR/FDR columns appear only when a real selection-set pass produced
    verdicts, so the single-window console output is byte-identical to
    before.
    """
    stat_columns = any(row.is_statistically_ranked for row in scorecard.rows)
    headers = ["Rank", "Strategy", "Sym", "TF", "Return%", "MaxDD%", "DailyBreaches", "TotalBreaches", "Trades"]
    if stat_columns:
        headers += ["DSR-p", "FDR-q", "Ncell", "Nset"]
    body = []
    for row in scorecard.rows:
        cells = [
            f"#{row.rank}" if row.rank > 0 else "—",
            row.strategy_id,
            row.symbol,
            row.timeframe,
            f"{row.return_pct:+.2f}",
            f"{row.max_dd_pct:.2f}",
            row.daily_dd_breaches,
            row.total_dd_breaches,
            row.trade_count,
        ]
        if stat_columns:
            cells += [
                f"{row.dsr_pvalue:.4f}" if row.dsr_pvalue is not None else "—",
                f"{row.fdr_qvalue:.4f}" if row.fdr_qvalue is not None else "—",
                str(row.n_trials_cell) if row.n_trials_cell is not None else "—",
                str(row.n_trials_selection) if row.n_trials_selection is not None else "—",
            ]
        body.append(cells)
    return _format_table(headers, body)


def _format_table(headers: list[str], rows: list[list]) -> str:
    """Format a small ASCII table.

    Used both as the fall-back for ``render_console_table`` (when
    ``tabulate`` is unavailable) and as the deterministic console
    representation in tests (no dependency on tabulate).
    """
    try:
        import tabulate  # noqa: PLC0415 — late import keeps module import cheap

        return tabulate.tabulate(rows, headers=headers, tablefmt="github", numalign="right")
    except ImportError:
        # Pure-stdlib fallback.  Column widths are computed from headers +
        # values (truncated at the right of negative numbers — no padding
        # surprises between Python str() and numeric formatting).
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))
        sep = "| " + " | ".join("-" * w for w in widths) + " |"
        out = [sep]
        out.append("| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |")
        out.append(sep)
        for row in rows:
            out.append(
                "| "
                + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
                + " |"
            )
        out.append(sep)
        return "\n".join(out)


def render_scorecard_json(
    scorecard: Scorecard,
    *,
    include_meta: bool = True,
) -> str:
    """Render the scorecard as a JSON string (sorted keys, indent=2).

    Each row includes the rank and every column from
    :data:`SCORECARD_ROW_COLUMNS` plus ``rank``.  Metadata (run_meta +
    harness window meta) is emitted under the ``meta`` key when
    ``include_meta`` is True.
    """
    payload: dict = {
        # Card 82b40a95: ``rank`` moved INTO SCORECARD_ROW_COLUMNS (it is
        # part of the verdict schema now), so appending it again here would
        # emit a duplicated header.
        "columns": list(SCORECARD_ROW_COLUMNS),
        "rows": [row.to_dict() for row in scorecard.rows],
    }
    if include_meta:
        meta_out = dict(scorecard.meta or {})
        # Card 82b40a95 scope item 3: the memo tooling must be able to
        # tell a real ranking from a hand-rolled one without guessing.
        # Shipping the statement inside the JSON makes the refusal
        # travel with the artifact instead of living only in stdout.
        meta_out["ranking_statement"] = ranking_ordering_statement(scorecard)
        payload["meta"] = meta_out
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
