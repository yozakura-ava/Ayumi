"""SRF Data QA Gate — validates input data before any sweep runs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

QA_LOG_PATH = "data/research/qa_failures.jsonl"


@dataclass
class ValidationFailure:
    check_name: str
    severity: str  # "hard" or "soft"
    detail: str
    metric_value: float | None = None
    threshold: float | None = None


@dataclass
class QAResult:
    passed: bool = False
    failures: list[ValidationFailure] = field(default_factory=list)
    row_count: int = 0
    date_range: str = ""
    checks_run: int = 0

    def __bool__(self) -> bool:
        return self.passed


def _log_failure(failure: ValidationFailure, pair: str, timeframe: int | str) -> None:
    """Append failure to qa_failures.jsonl."""
    Path(QA_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "timeframe": str(timeframe),
        **failure.__dict__,
    }
    with open(QA_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def validate_data(
    df: pd.DataFrame,
    pair: str,
    timeframe: int,
    *,
    expected_columns: list[str] | None = None,
) -> QAResult:
    """Run all QA checks on a bar DataFrame.

    Expected columns: timestamp, open, high, low, close (and optionally volume).
    """
    result = QAResult()
    result.checks_run = 5

    # ── Default expected columns ─────────────────────────────────────────
    if expected_columns is None:
        expected_columns = ["timestamp", "open", "high", "low", "close"]

    # ── Check 0: Column existence ────────────────────────────────────────
    missing_cols = [c for c in expected_columns if c not in df.columns]
    if missing_cols:
        f = ValidationFailure(
            check_name="missing_columns",
            severity="hard",
            detail=f"Missing required columns: {missing_cols}",
        )
        result.failures.append(f)
        _log_failure(f, pair, timeframe)
        result.passed = False
        return result

    result.row_count = len(df)

    # ── Date range for reporting ─────────────────────────────────────────
    ts_col = df["timestamp"]
    if pd.api.types.is_numeric_dtype(ts_col):
        # Epoch ms — convert
        ts_range = pd.to_datetime(ts_col, unit="ms")
    else:
        ts_range = pd.to_datetime(ts_col)
    result.date_range = f"{ts_range.min()} to {ts_range.max()}"

    # ── Check 1: Null rate < 0.1% ────────────────────────────────────────
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            null_rate = df[col].isnull().sum() / max(len(df), 1)
            if null_rate > 0.001:
                f = ValidationFailure(
                    check_name="null_rate",
                    severity="hard",
                    detail=f"Column '{col}' null rate {null_rate:.4f} exceeds 0.1%",
                    metric_value=null_rate,
                    threshold=0.001,
                )
                result.failures.append(f)
                _log_failure(f, pair, timeframe)

    # ── Check 2: Duplicate timestamps < 0.01% ────────────────────────────
    dup_rate = df["timestamp"].duplicated().sum() / max(len(df), 1)
    if dup_rate > 0.0001:
        f = ValidationFailure(
            check_name="duplicate_timestamps",
            severity="hard",
            detail=f"Duplicate timestamp rate {dup_rate:.6f} exceeds 0.01%",
            metric_value=dup_rate,
            threshold=0.0001,
        )
        result.failures.append(f)
        _log_failure(f, pair, timeframe)

    # ── Check 3: Spread sanity — no negative spreads, no high < low ──────
    if all(c in df.columns for c in ["high", "low"]):
        neg_spread = ((df["high"] - df["low"]) < 0).sum()
        if neg_spread > 0:
            f = ValidationFailure(
                check_name="negative_spread",
                severity="hard",
                detail=f"{neg_spread} bars where high < low",
                metric_value=float(neg_spread),
                threshold=0.0,
            )
            result.failures.append(f)
            _log_failure(f, pair, timeframe)

    # ── Check 4: Gap detection — no missing periods > 3× expected interval
    if len(df) > 1:
        if pd.api.types.is_numeric_dtype(df["timestamp"]):
            ts_ns = df["timestamp"].astype("int64").values
        else:
            ts_ns = pd.to_datetime(df["timestamp"]).astype("int64").values
        ns_diffs = ts_ns[1:] - ts_ns[:-1]
        expected_ns = timeframe * 60 * 1_000_000_000  # timeframe in minutes
        gap_mask = ns_diffs > 3 * expected_ns
        gap_count = gap_mask.sum()
        if gap_count > 0:
            # Weekends are expected — filter out gaps > 48h that span weekend
            weekend_mask = ns_diffs > 42 * 60 * 60 * 1_000_000_000  # > 42h
            real_gaps = gap_count - weekend_mask[gap_mask].sum()
            if real_gaps > 0:
                f = ValidationFailure(
                    check_name="data_gaps",
                    severity="soft",
                    detail=f"{real_gaps} gaps > 3× interval (excluding weekends)",
                    metric_value=float(real_gaps),
                    threshold=0.0,
                )
                result.failures.append(f)
                _log_failure(f, pair, timeframe)

    # ── Check 5: Minimum bar count ───────────────────────────────────────
    min_bars = 1000
    if len(df) < min_bars:
        f = ValidationFailure(
            check_name="insufficient_bars",
            severity="hard",
            detail=f"Only {len(df)} bars, need minimum {min_bars}",
            metric_value=float(len(df)),
            threshold=float(min_bars),
        )
        result.failures.append(f)
        _log_failure(f, pair, timeframe)

    # ── Result ───────────────────────────────────────────────────────────
    hard_failures = [f for f in result.failures if f.severity == "hard"]
    result.passed = len(hard_failures) == 0

    if result.passed:
        logger.info(
            "QA passed: %s %dm — %d bars, %s",
            pair,
            timeframe,
            result.row_count,
            result.date_range,
        )
    else:
        logger.warning(
            "QA FAILED: %s %dm — %d hard failures, %d soft",
            pair,
            timeframe,
            len(hard_failures),
            len([f for f in result.failures if f.severity == "soft"]),
        )

    return result
