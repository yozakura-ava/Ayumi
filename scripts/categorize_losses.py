#!/usr/bin/env python3
"""Categorize closed losing trades by likely reason.

BQ-344: Loss Trade Categorization Script

Reads a backtest JSON file containing closed trade records (SimulatedTrade
schema from ``forex_bot.backtest.multi_strategy_engine``) and assigns each
loss to a likely cause using rule-based heuristics.  No ML, no external
APIs.

Input format
------------
The script accepts either:

* A bare JSON list of trade records, e.g. ``[{...}, {...}]``.
* A JSON object containing a ``trades`` array (matches the shape produced
  by ``forex_bot.backtest`` exporters that wrap trades under that key).
* A JSON object matching ``BacktestMetrics`` style where ``trades`` is the
  list of records (commonly seen in walk-forward exports).

For each losing trade the script assigns the highest-confidence category.
Categories (with their heuristics) are:

* ``bad_entry``        — entry beyond 1.5 std dev of the supplied session range
* ``stop_placement``   — SL distance <= 1x ATR
* ``wrong_direction``  — trade direction contradicts the supplied H4 EMA trend
* ``news_event``       — loss falls within a window flagged on the trade record
* ``spread_widening``  — entry spread > 2x the rolling average spread
* ``slippage``         — exit price deviates from expected stop/TP by a
                          magnitude consistent with abnormal slippage
* ``late_exit``        — partial close already locked profit before reversal
                          back to stop-loss territory
* ``other``            — no category reached the 0.4 confidence threshold

CLI::

    python3 scripts/categorize_losses.py --input trades.json --output report.json
    python3 scripts/categorize_losses.py --input trades.json --output report.json --json
    python3 scripts/categorize_losses.py --input trades.json --dry-run
    python3 scripts/categorize_losses.py --input trades.json --output report.json --verbose
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Category constants
# ---------------------------------------------------------------------------

CATEGORY_BAD_ENTRY = "bad_entry"
CATEGORY_STOP_PLACEMENT = "stop_placement"
CATEGORY_WRONG_DIRECTION = "wrong_direction"
CATEGORY_NEWS_EVENT = "news_event"
CATEGORY_SPREAD_WIDENING = "spread_widening"
CATEGORY_SLIPPAGE = "slippage"
CATEGORY_LATE_EXIT = "late_exit"
CATEGORY_OTHER = "other"

ALL_CATEGORIES: tuple[str, ...] = (
    CATEGORY_BAD_ENTRY,
    CATEGORY_STOP_PLACEMENT,
    CATEGORY_WRONG_DIRECTION,
    CATEGORY_NEWS_EVENT,
    CATEGORY_SPREAD_WIDENING,
    CATEGORY_SLIPPAGE,
    CATEGORY_LATE_EXIT,
    CATEGORY_OTHER,
)

# Thresholds — values taken from the BQ-344 judgment calls
BAD_ENTRY_STD_DEV_THRESHOLD = 1.5
STOP_PLACEMENT_ATR_MULTIPLIER = 1.0
SPREAD_WIDENING_MULTIPLIER = 2.0
CONFIDENCE_FLOOR = 0.4
NEWS_EVENT_KEY = "news_event"  # marker on a trade record indicating news window
SLIPPAGE_TOLERANCE_PIPS = 1.0  # anything beyond this from SL/TP is slippage

# Confidence base + weight when evidence aligns — chosen so the highest
# category typically lands at 0.55–0.85.
_CONFIDENCE_BASE = 0.45
_CONFIDENCE_EVIDENCE_BONUS = 0.35


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CategoryResult:
    """Per-trade categorization output."""

    category: str
    confidence: float
    rationale: str
    contributing_evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class CategorizationSummary:
    """Aggregate report for a collection of losing trades."""

    total_trades: int
    losing_trades: int
    category_counts: dict[str, int]
    category_percentages: dict[str, float]
    per_trade: list[dict[str, Any]] = field(default_factory=list)
    skipped_categories: list[str] = field(default_factory=list)
    schema_notes: list[str] = field(default_factory=list)
    input_path: str = ""
    generated_at: str = ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _extract_trade_records(payload: Any) -> list[dict[str, Any]]:
    """Pull the trade list out of a JSON payload.

    Accepts three shapes:

    * bare list -> returned as-is
    * dict with ``trades`` key -> that list
    * anything else -> empty list (caller reports schema issue)
    """
    if isinstance(payload, list):
        return [t for t in payload if isinstance(t, dict)]
    if isinstance(payload, dict):
        trades = payload.get("trades")
        if isinstance(trades, list):
            return [t for t in trades if isinstance(t, dict)]
    return []


def load_trades(input_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Load trades from a JSON file. Returns (records, schema_notes)."""
    notes: list[str] = []
    try:
        raw = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read input file {input_path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: invalid JSON in {input_path}: {exc}") from exc

    records = _extract_trade_records(payload)
    if not records:
        notes.append(
            "No trade records found in input. Expected a list, or an object with a 'trades' key. Check the input file."
        )
    return records, notes


# ---------------------------------------------------------------------------
# Trade field helpers
# ---------------------------------------------------------------------------


def _is_loss(trade: dict[str, Any]) -> bool:
    """Decide whether a trade counts as a loss."""
    outcome = trade.get("outcome")
    if outcome is not None:
        if isinstance(outcome, str) and outcome.lower() == "loss":
            return True
        if isinstance(outcome, str) and outcome.lower() != "loss":
            return False
    # Fall back to profit_loss
    pnl = trade.get("profit_loss", trade.get("pnl", 0.0))
    try:
        return float(pnl) < -0.01
    except (TypeError, ValueError):
        return False


def _pip_size(price: float) -> float:
    """Mirror ``MultiStrategyBacktestEngine._get_pip_value``."""
    if price >= 50:
        return 0.01
    if price >= 1:
        return 0.0001
    return 0.00000001


def _direction_value(trade: dict[str, Any]) -> str:
    """Normalize direction into 'long' / 'short' / ''."""
    d = trade.get("direction")
    if isinstance(d, str):
        return d.lower()
    return ""


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


# ---------------------------------------------------------------------------
# Individual category heuristics
# ---------------------------------------------------------------------------


def _categorize_bad_entry(trade: dict[str, Any]) -> CategoryResult | None:
    """Entry beyond 1.5 std dev of session range.

    Expects the trade record to optionally carry ``session_high``,
    ``session_low`` and ``session_mean`` (or ``atr`` as a proxy for
    standard deviation of the session).  If none are provided the rule
    cannot fire.
    """
    high = trade.get("session_high")
    low = trade.get("session_low")
    mean = trade.get("session_mean")
    std = trade.get("session_std")
    atr = trade.get("atr")
    entry = _to_float(trade.get("entry_price"))
    if entry == 0:
        return None

    # Prefer explicit std, fall back to ATR as a proxy
    sigma = _to_float(std) if std is not None else _to_float(atr, default=0.0)
    if sigma <= 0:
        return None

    # If we have a session mean, measure deviation from it; otherwise
    # measure deviation from the session midpoint.
    if mean is None and high is not None and low is not None:
        try:
            mean = (float(high) + float(low)) / 2.0
        except (TypeError, ValueError):
            mean = None

    if mean is None:
        return None

    deviation_pips = abs(entry - float(mean)) / _pip_size(entry)
    sigma_pips = sigma / _pip_size(entry)
    if sigma_pips <= 0:
        return None

    deviation_in_sigma = deviation_pips / sigma_pips
    if deviation_in_sigma >= BAD_ENTRY_STD_DEV_THRESHOLD:
        confidence = min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS)
        return CategoryResult(
            category=CATEGORY_BAD_ENTRY,
            confidence=confidence,
            rationale=(
                f"entry {deviation_in_sigma:.2f}\u03c3 beyond session mean "
                f"(threshold {BAD_ENTRY_STD_DEV_THRESHOLD:.2f}\u03c3)"
            ),
            contributing_evidence={
                "deviation_sigma": round(deviation_in_sigma, 4),
                "session_mean": float(mean),
                "session_std": sigma,
                "threshold_sigma": BAD_ENTRY_STD_DEV_THRESHOLD,
            },
        )
    return None


def _categorize_stop_placement(trade: dict[str, Any]) -> CategoryResult | None:
    """SL within 1x ATR of entry — stop is too tight, noise stopped us."""
    entry = _to_float(trade.get("entry_price"))
    sl = _to_float(trade.get("stop_loss"))
    atr = _to_float(trade.get("atr"))
    if entry == 0 or sl == 0 or atr <= 0:
        return None

    sl_distance_pips = abs(entry - sl) / _pip_size(entry)
    atr_pips = atr / _pip_size(entry)
    if atr_pips <= 0:
        return None

    ratio = sl_distance_pips / atr_pips
    if ratio <= STOP_PLACEMENT_ATR_MULTIPLIER:
        confidence = min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS)
        return CategoryResult(
            category=CATEGORY_STOP_PLACEMENT,
            confidence=confidence,
            rationale=(f"stop {ratio:.2f}x ATR from entry (threshold {STOP_PLACEMENT_ATR_MULTIPLIER:.2f}x ATR)"),
            contributing_evidence={
                "sl_distance_atr_ratio": round(ratio, 4),
                "threshold_atr_multiple": STOP_PLACEMENT_ATR_MULTIPLIER,
                "sl_distance_pips": round(sl_distance_pips, 2),
                "atr_pips": round(atr_pips, 2),
            },
        )
    return None


def _categorize_wrong_direction(trade: dict[str, Any]) -> CategoryResult | None:
    """Trade direction contradicts H4 EMA trend at entry."""
    direction = _direction_value(trade)
    if direction not in {"long", "short"}:
        return None

    h4_trend = trade.get("h4_trend") or trade.get("ema_h4_trend")
    h4_ema_value = trade.get("h4_ema") or trade.get("ema_h4_value")
    entry_price = _to_float(trade.get("entry_price"))

    if h4_trend is not None:
        trend = str(h4_trend).lower()
        if trend in {"long", "short", "bullish", "bearish"}:
            normalized = "long" if trend in {"long", "bullish"} else "short"
            if normalized != direction:
                confidence = min(
                    1.0,
                    _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS * (1.0 if h4_ema_value is not None else 0.5),
                )
                return CategoryResult(
                    category=CATEGORY_WRONG_DIRECTION,
                    confidence=confidence,
                    rationale=(f"trade direction '{direction}' contradicts H4 trend '{normalized}'"),
                    contributing_evidence={
                        "trade_direction": direction,
                        "h4_trend": normalized,
                        "h4_ema_value": (float(h4_ema_value) if h4_ema_value is not None else None),
                    },
                )
            return None  # direction agrees with trend — no rule fired
    elif h4_ema_value is not None and entry_price:
        # Infer trend from EMA vs entry price
        try:
            ema = float(h4_ema_value)
            if (direction == "long" and entry_price < ema) or (direction == "short" and entry_price > ema):
                confidence = min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS * 0.6)
                return CategoryResult(
                    category=CATEGORY_WRONG_DIRECTION,
                    confidence=confidence,
                    rationale=(f"trade direction '{direction}' against H4 EMA {ema}"),
                    contributing_evidence={
                        "trade_direction": direction,
                        "h4_ema_value": ema,
                        "entry_price": entry_price,
                    },
                )
        except (TypeError, ValueError):
            return None
    return None


def _categorize_news_event(trade: dict[str, Any]) -> CategoryResult | None:
    """Loss falls within a flagged news window.

    The base trade schema does not carry a forex calendar.  Trade records
    may mark this themselves with a boolean/string ``news_event`` field
    or by carrying a ``news_events`` array referencing known releases.
    """
    marker = trade.get(NEWS_EVENT_KEY)
    if _to_bool(marker):
        return CategoryResult(
            category=CATEGORY_NEWS_EVENT,
            confidence=min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS),
            rationale="trade record flagged news_event window",
            contributing_evidence={"news_event": True},
        )

    news_list = trade.get("news_events")
    if isinstance(news_list, list) and news_list:
        return CategoryResult(
            category=CATEGORY_NEWS_EVENT,
            confidence=min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS * 0.7),
            rationale=f"{len(news_list)} news event(s) recorded near entry",
            contributing_evidence={"news_events": news_list},
        )
    return None


def _categorize_spread_widening(trade: dict[str, Any]) -> CategoryResult | None:
    """Entry spread > 2x the rolling average spread."""
    spread = _to_float(trade.get("spread_at_entry") or trade.get("spread_pips"))
    rolling = _to_float(trade.get("spread_avg_rolling"))
    if spread <= 0 or rolling <= 0:
        return None

    ratio = spread / rolling
    if ratio >= SPREAD_WIDENING_MULTIPLIER:
        # How far past the threshold — stronger multiplier -> higher confidence
        bonus = min(
            _CONFIDENCE_EVIDENCE_BONUS,
            _CONFIDENCE_EVIDENCE_BONUS * (ratio / (SPREAD_WIDENING_MULTIPLIER * 2)),
        )
        confidence = min(1.0, _CONFIDENCE_BASE + bonus)
        return CategoryResult(
            category=CATEGORY_SPREAD_WIDENING,
            confidence=confidence,
            rationale=(f"entry spread {ratio:.2f}x rolling average (threshold {SPREAD_WIDENING_MULTIPLIER:.2f}x)"),
            contributing_evidence={
                "spread_ratio": round(ratio, 4),
                "spread_at_entry": spread,
                "spread_avg_rolling": rolling,
                "threshold_multiplier": SPREAD_WIDENING_MULTIPLIER,
            },
        )
    return None


def _categorize_slippage(trade: dict[str, Any]) -> CategoryResult | None:
    """Exit price deviates from expected stop/TP by abnormal amount."""
    entry = _to_float(trade.get("entry_price"))
    exit_price = _to_float(trade.get("exit_price"))
    sl = _to_float(trade.get("stop_loss"))
    tp1 = _to_float(trade.get("take_profit_1"))
    exit_reason = str(trade.get("exit_reason") or "").lower()

    if entry == 0 or exit_price == 0:
        return None

    pip = _pip_size(entry)
    expected = None
    if exit_reason in {"sl", "stop_loss"} and sl != 0:
        expected = sl
    elif exit_reason in {
        "tp1",
        "tp2",
        "tp3",
        "take_profit_1",
        "take_profit_2",
        "take_profit_3",
    }:
        if exit_reason == "tp1" and tp1 != 0:
            expected = tp1
        elif exit_reason == "tp2":
            expected = _to_float(trade.get("take_profit_2"))
        elif exit_reason == "tp3":
            expected = _to_float(trade.get("take_profit_3"))
    if expected is None or expected == 0:
        return None

    slippage_pips = abs(exit_price - expected) / pip
    if slippage_pips < SLIPPAGE_TOLERANCE_PIPS:
        return None

    confidence = min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS * 0.8)
    return CategoryResult(
        category=CATEGORY_SLIPPAGE,
        confidence=confidence,
        rationale=(f"exit {slippage_pips:.2f} pips from expected {expected} (exit_reason={exit_reason})"),
        contributing_evidence={
            "slippage_pips": round(slippage_pips, 4),
            "expected_price": expected,
            "actual_price": exit_price,
            "exit_reason": exit_reason,
        },
    )


def _categorize_late_exit(trade: dict[str, Any]) -> CategoryResult | None:
    """Partial close locked profit but trade still ended at a loss.

    Schema support: ``partial_closed`` flag and ``partial_close_pnl``.
    If partial close produced profit but the overall P&L is negative, the
    exit was late (didn't lock the gain soon enough).
    """
    if not _to_bool(trade.get("partial_closed")):
        return None

    partial_pnl = _to_float(trade.get("partial_close_pnl"))
    final_pnl = _to_float(trade.get("profit_loss"))
    if partial_pnl <= 0:
        return None
    if final_pnl >= -0.01:
        return None

    confidence = min(1.0, _CONFIDENCE_BASE + _CONFIDENCE_EVIDENCE_BONUS * 0.6)
    return CategoryResult(
        category=CATEGORY_LATE_EXIT,
        confidence=confidence,
        rationale=(
            f"partial close gained {partial_pnl:.2f} but trade closed at {final_pnl:.2f} \u2014 exit was too late"
        ),
        contributing_evidence={
            "partial_close_pnl": partial_pnl,
            "final_pnl": final_pnl,
            "partial_close_price": _to_float(trade.get("partial_close_price")),
        },
    )


# ---------------------------------------------------------------------------
# Per-trade dispatch
# ---------------------------------------------------------------------------


def _all_categories() -> Iterable[tuple[str, callable]]:
    """Return (name, callable) pairs in priority order."""
    return (
        (CATEGORY_BAD_ENTRY, _categorize_bad_entry),
        (CATEGORY_STOP_PLACEMENT, _categorize_stop_placement),
        (CATEGORY_WRONG_DIRECTION, _categorize_wrong_direction),
        (CATEGORY_NEWS_EVENT, _categorize_news_event),
        (CATEGORY_SPREAD_WIDENING, _categorize_spread_widening),
        (CATEGORY_SLIPPAGE, _categorize_slippage),
        (CATEGORY_LATE_EXIT, _categorize_late_exit),
    )


def categorize_trade(trade: dict[str, Any]) -> CategoryResult:
    """Return the best matching category for a single losing trade."""
    best: CategoryResult | None = None
    for _, heuristic in _all_categories():
        result = heuristic(trade)
        if result is None:
            continue
        if best is None or result.confidence > best.confidence:
            best = result
    if best is None or best.confidence < CONFIDENCE_FLOOR:
        return CategoryResult(
            category=CATEGORY_OTHER,
            confidence=max(best.confidence if best else 0.0, 0.0),
            rationale="no category reached the 0.4 confidence threshold",
            contributing_evidence={},
        )
    return best


# ---------------------------------------------------------------------------
# Batch & reporting
# ---------------------------------------------------------------------------


def summarize(
    trades: list[dict[str, Any]],
    *,
    input_path: str = "",
    schema_notes: list[str] | None = None,
) -> CategorizationSummary:
    """Run categorization across all losing trades and build a summary."""
    notes = list(schema_notes or [])
    losing = [t for t in trades if _is_loss(t)]

    per_trade: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    skipped = _detect_skipped_categories(trades)
    for idx, trade in enumerate(losing):
        result = categorize_trade(trade)
        counts[result.category] += 1
        per_trade.append(
            {
                "trade_index": idx,
                "entry_time": trade.get("entry_time"),
                "exit_time": trade.get("exit_time"),
                "direction": trade.get("direction"),
                "profit_loss": trade.get("profit_loss"),
                "category": result.category,
                "confidence": round(result.confidence, 4),
                "rationale": result.rationale,
                "evidence": result.contributing_evidence,
            }
        )

    total_losses = max(1, len(losing))
    percentages = {cat: round(counts.get(cat, 0) / total_losses * 100.0, 2) for cat in ALL_CATEGORIES}

    return CategorizationSummary(
        total_trades=len(trades),
        losing_trades=len(losing),
        category_counts={cat: counts.get(cat, 0) for cat in ALL_CATEGORIES},
        category_percentages=percentages,
        per_trade=per_trade,
        skipped_categories=skipped,
        schema_notes=notes,
        input_path=input_path,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _detect_skipped_categories(trades: list[dict[str, Any]]) -> list[str]:
    """List categories that could never fire because the data is absent.

    A category is reported as skipped only if the trade records lack the
    fields it requires for *every* losing trade.
    """
    losing = [t for t in trades if _is_loss(t)]
    if not losing:
        return []

    skipped: list[str] = []

    def _any_field(*names: str) -> bool:
        return any(any(name in t for name in names) for t in losing)

    if not _any_field("session_high", "session_low", "session_mean", "session_std"):
        skipped.append(CATEGORY_BAD_ENTRY)
    if not _any_field("atr"):
        skipped.append(CATEGORY_STOP_PLACEMENT)
    if not _any_field("h4_trend", "ema_h4_trend", "h4_ema", "ema_h4_value"):
        skipped.append(CATEGORY_WRONG_DIRECTION)
    if not _any_field("news_event", "news_events"):
        skipped.append(CATEGORY_NEWS_EVENT)
    if not _any_field("spread_at_entry", "spread_pips") or not _any_field("spread_avg_rolling"):
        skipped.append(CATEGORY_SPREAD_WIDENING)
    return skipped


# ---------------------------------------------------------------------------
# Serialization & reporting
# ---------------------------------------------------------------------------


def summary_to_dict(summary: CategorizationSummary) -> dict[str, Any]:
    """Convert a summary dataclass into a JSON-friendly dict."""
    return asdict(summary)


def render_text_report(summary: CategorizationSummary) -> str:
    """Render a human-readable summary suitable for stdout."""
    lines: list[str] = []
    lines.append("Loss Categorization Report")
    lines.append("=" * 40)
    if summary.input_path:
        lines.append(f"Input:           {summary.input_path}")
    lines.append(f"Total trades:    {summary.total_trades}")
    lines.append(f"Losing trades:   {summary.losing_trades}")
    lines.append("")
    lines.append("Distribution by category:")
    lines.append(f"  {'category':<20} {'count':>6} {'pct':>8}")
    for cat in ALL_CATEGORIES:
        cnt = summary.category_counts.get(cat, 0)
        pct = summary.category_percentages.get(cat, 0.0)
        lines.append(f"  {cat:<20} {cnt:>6} {pct:>7.2f}%")
    if summary.skipped_categories:
        lines.append("")
        lines.append("Categories skipped (missing data):")
        for cat in summary.skipped_categories:
            lines.append(f"  - {cat}")
    if summary.schema_notes:
        lines.append("")
        lines.append("Schema notes:")
        for note in summary.schema_notes:
            lines.append(f"  * {note}")
    lines.append("")
    lines.append("Per-trade categorization:")
    if not summary.per_trade:
        lines.append("  (no losing trades)")
    for entry in summary.per_trade:
        lines.append(
            f"  #{entry['trade_index']:>3} {str(entry['direction']):<5} "
            f"category={entry['category']:<18} "
            f"confidence={entry['confidence']:.2f} "
            f"\u2014 {entry['rationale']}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Categorize closed losing trades by likely reason (BQ-344).",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to input JSON file with trade records.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write report JSON.  Defaults to stdout when omitted.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON to stdout (in addition to any --output file).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and summarize but do not write the output file.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the human-readable summary to stdout in addition to JSON output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    records, notes = load_trades(input_path)
    summary = summarize(records, input_path=str(input_path), schema_notes=notes)

    if args.dry_run:
        sys.stdout.write(render_text_report(summary))
        return 0

    report = summary_to_dict(summary)
    json_blob = json.dumps(report, indent=2, default=str)
    text_blob = render_text_report(summary)

    if args.output:
        Path(args.output).write_text(json_blob, encoding="utf-8")
        if args.json:
            sys.stdout.write(json_blob)
            sys.stdout.write("\n")
        if args.verbose:
            sys.stdout.write(text_blob)
        sys.stdout.write(f"\nReport written to: {args.output}\n")
    elif args.json:
        sys.stdout.write(json_blob)
        sys.stdout.write("\n")
        if args.verbose:
            sys.stdout.write("\n")
            sys.stdout.write(text_blob)
    else:
        sys.stdout.write(text_blob)
        if args.verbose:
            sys.stdout.write("\n--- JSON ---\n")
            sys.stdout.write(json_blob)
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
