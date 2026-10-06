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
from typing import Iterable


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
    rank: int = 0

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
    """
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


# ── Renderers ────────────────────────────────────────────────────────────────


def render_console_table(scorecard: Scorecard) -> str:
    """Format the scorecard as a tabulate-friendly list-of-lists.

    Falls back to a hand-rolled ASCII table when ``tabulate`` is not
    installed (the project's CI may not have it).  Tests pass the rendered
    output to ``tabulate.tabulate`` themselves.
    """
    headers = ["Rank", "Strategy", "Sym", "TF", "Return%", "MaxDD%", "DailyBreaches", "TotalBreaches", "Trades"]
    body = []
    for row in scorecard.rows:
        body.append(
            [
                f"#{row.rank}",
                row.strategy_id,
                row.symbol,
                row.timeframe,
                f"{row.return_pct:+.2f}",
                f"{row.max_dd_pct:.2f}",
                row.daily_dd_breaches,
                row.total_dd_breaches,
                row.trade_count,
            ]
        )
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
        "columns": list(SCORECARD_ROW_COLUMNS) + ["rank"],
        "rows": [row.to_dict() for row in scorecard.rows],
    }
    if include_meta:
        payload["meta"] = scorecard.meta
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
