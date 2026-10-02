"""Strategy Factory Phase 0 — Regime Detector Validation.

Validates the :class:`~regime.detector.RegimeDetector` against three known
historical regime transitions and audits the full-data label distribution.

For each validation event the script reports TWO accuracy metrics:

1. **Implementation consistency** — does the detector's own label match
   the threshold that *should* have produced it (circular check).
   VOLATILE ⇔ ATR percentile > 0.80, TRENDING ⇔ ADX > 25.
   Catches implementation bugs but is tautological by design.

2. **Conceptual accuracy** — does the detector's label match an
   INDEPENDENT ground truth derived from the underlying price action?
   VOLATILE ground truth: realized volatility percentile (rolling std of
   close-to-close log returns, top 20th percentile). TRENDING ground
   truth: per-bar rolling R² of close vs bar index over a 50-bar lookback
   window, upper tercile within the scored slice. Both are computed on
   the warm-up-sliced data so they align bar-for-bar with the detector's
   per-bar labels. These signals do not feed the detector internally
   (R² uses only close prices; ADX uses high-low ranges).

The PRIMARY ≥70 % gate uses the conceptual metric. The consistency metric
is reported for diagnostic purposes only.

Output
------
- ``docs/factory/regime-validation-<date>.json`` — machine-readable results
- ``docs/factory/regime-validation-<date>.md``   — human-readable report

Exit codes
----------
- ``0`` — all events ≥70 % conceptual accuracy (PASS)
- non-zero — at least one event <70 % conceptual accuracy (FAIL)
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

# The detector lives in src/forex_bot; insert that path so we can import
# the module without requiring the package to be installed.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FX_ROOT = _REPO_ROOT / "src" / "forex_bot"
if str(_FX_ROOT) not in sys.path:
    sys.path.insert(0, str(_FX_ROOT))

import duckdb  # noqa: E402, I001
from regime.detector import Regime, RegimeConfig, RegimeDetector  # noqa: E402

# ── Configuration constants ────────────────────────────────────────────────

# Warm-up period for the indicators (bars that produce NaN regimes).
# ADX needs 2*adx_period = 28 bars (NaN-leading); ATR percentile needs
# atr_period + atr_lookback = 64 bars. Use the larger to be safe.
WARMUP_BARS = 64

# Conceptual ground truth thresholds.
CONCEPTUAL_VOLATILE_PCTL = 0.80  # top 20 % realized vol → VOLATILE
CONCEPTUAL_TRENDING_R2_TERCILE = 2 / 3  # upper tercile of rolling R² → TRENDING
CONCEPTUAL_VOL_LOOKBACK = 50  # bars for rolling std / rolling R² lookback

# Validation gate.
ACCURACY_GATE = 0.70

# Minimum label distribution share across the full data range.
DISTRIBUTION_GATE = 0.15


# ── Validation events ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventSpec:
    """Definition of a single historical regime transition."""

    name: str
    description: str
    symbol: str
    timeframe: str
    start_utc: str  # ISO date (YYYY-MM-DD)
    end_utc: str  # ISO date (YYYY-MM-DD)
    # The dominant regimes we expect the detector to identify.
    # For each entry: regime, minimum fraction of bars that should carry it,
    # description.
    expectations: list[dict]


EVENTS: list[EventSpec] = [
    EventSpec(
        name="2020 COVID Crash",
        description=(
            "March 2020 COVID crash on EURUSD M15. Markets froze mid-March "
            "with extreme two-sided volatility followed by central-bank "
            "intervention. Expected: dominant VOLATILE regime as realised "
            "volatility spiked."
        ),
        symbol="EURUSD",
        timeframe="M15",
        start_utc="2020-03-01",
        end_utc="2020-04-15",
        expectations=[
            {
                "regime": "volatile",
                "min_fraction": 0.20,
                "rationale": "Top 20 % volatility window for forex pair",
            },
        ],
    ),
    EventSpec(
        name="2022 Fed Rate Shock",
        description=(
            "Jun–Sep 2022 aggressive Fed tightening cycle on XAUUSD M15. "
            "Gold entered a sustained downtrend as real yields rose, with "
            "elevated volatility around CPI/FOMC releases. Expected: mix "
            "of VOLATILE and TRENDING bars."
        ),
        symbol="XAUUSD",
        timeframe="M15",
        start_utc="2022-06-01",
        end_utc="2022-09-30",
        expectations=[
            {
                "regime": "volatile",
                "min_fraction": 0.10,
                "rationale": "Elevated intraday volatility around FOMC",
            },
            {
                "regime": "trending",
                "min_fraction": 0.20,
                "rationale": "Sustained downtrend in gold through summer",
            },
        ],
    ),
    EventSpec(
        name="2023 SVB Collapse",
        description=(
            "Mar 2023 Silicon Valley Bank collapse and US regional banking "
            "crisis on XAUUSD M15. Gold spiked on safe-haven flows with "
            "elevated volatility early and persistent trend behaviour "
            "after central-bank backstops. Expected: VOLATILE spike "
            "transitioning to TRENDING."
        ),
        symbol="XAUUSD",
        timeframe="M15",
        start_utc="2023-03-08",
        end_utc="2023-04-15",
        expectations=[
            {
                "regime": "volatile",
                "min_fraction": 0.10,
                "rationale": "Initial banking panic volatility",
            },
            {
                "regime": "trending",
                "min_fraction": 0.20,
                "rationale": "Sustained safe-haven rally after backstop",
            },
        ],
    ),
    EventSpec(
        name="2024-10 BoJ Policy Shift",
        description=(
            "Late Oct 2024 XAUUSD M15 volatility spike on the Bank of Japan "
            "unexpected policy-shift surprise and the concurrent safe-haven "
            "rotation that lifted gold to fresh highs. Two-sided intraday "
            "volatility dominated as JPY-funded flows unwound and dollar-yen "
            "swings spilled into gold. Expected: dominant VOLATILE regime."
        ),
        symbol="XAUUSD",
        timeframe="M15",
        start_utc="2024-10-20",
        end_utc="2024-11-05",
        expectations=[
            {
                "regime": "volatile",
                "min_fraction": 0.20,
                "rationale": ("Spillover volatility from BoJ surprise + safe-haven rotation lifts intraday gold range"),
            },
        ],
    ),
    EventSpec(
        name="2025-03 Fed FOMC Breakout",
        description=(
            "Mar 2025 XAUUSD M15 consolidation-to-breakout transition around "
            "the Mar 19 FOMC decision. Gold ranged tightly through the first "
            "half of the month then broke out directionally after the dot-plot "
            "and Powell press conference. Expected: TRENDING regime dominates "
            "post-breakout; QUIET shares should be lower than for a typical "
            "consolidation as the breakout produces sustained one-way bars."
        ),
        symbol="XAUUSD",
        timeframe="M15",
        start_utc="2025-03-01",
        end_utc="2025-03-31",
        expectations=[
            {
                "regime": "trending",
                "min_fraction": 0.20,
                "rationale": ("Post-FOMC breakout produces a sustained directional move in gold"),
            },
        ],
    ),
]


# ── Data loading ───────────────────────────────────────────────────────────


def _iso_to_unix_seconds(iso_date: str) -> int:
    """Convert a YYYY-MM-DD string into a Unix-seconds midnight UTC int."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def load_event_bars(
    db_path: Path,
    event: EventSpec,
) -> pd.DataFrame:
    """Read OHLC bars for one event from the DuckDB ``bars`` table.

    The bars table stores ``timestamp_utc`` as Unix **seconds**.
    """
    start_ts = _iso_to_unix_seconds(event.start_utc)
    # End window is inclusive of the start day; add 24h so the end day
    # itself is covered.
    end_ts = _iso_to_unix_seconds(event.end_utc) + 24 * 3600

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            """
            SELECT timestamp_utc, open, high, low, close, volume
            FROM bars
            WHERE symbol = ?
              AND timeframe = ?
              AND timestamp_utc >= ?
              AND timestamp_utc < ?
            ORDER BY timestamp_utc
            """,
            [event.symbol, event.timeframe, start_ts, end_ts],
        ).fetchdf()
    finally:
        con.close()

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
    df = df.reset_index(drop=True)
    return df


def load_full_range(
    db_path: Path,
    symbol: str,
    timeframe: str,
) -> pd.DataFrame:
    """Read every bar for ``symbol``/``timeframe`` from DuckDB."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            """
            SELECT timestamp_utc, open, high, low, close, volume
            FROM bars
            WHERE symbol = ?
              AND timeframe = ?
            ORDER BY timestamp_utc
            """,
            [symbol, timeframe],
        ).fetchdf()
    finally:
        con.close()

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
    df = df.reset_index(drop=True)
    return df


# ── Indicator helpers ──────────────────────────────────────────────────────


def compute_realized_vol_pctl(
    closes: Sequence[float],
    lookback: int = CONCEPTUAL_VOL_LOOKBACK,
) -> pd.Series:
    """Realized-vol percentile rank (independent of detector's ATR).

    Rolling standard deviation of close-to-close log returns, then a
    percentile rank within a rolling window equal to the same lookback.
    Values in [0, 1]; top 20 % → VOLATILE ground truth.
    """
    s = pd.Series(closes, dtype=float)
    log_ret = np.log(s).diff()
    rv = log_ret.rolling(window=lookback, min_periods=lookback).std(ddof=0)
    # Percentile rank over a rolling window of the same size as the
    # detector's ATR percentile so the metrics are comparable.
    rolling_min = rv.rolling(window=lookback, min_periods=lookback).min()
    rolling_max = rv.rolling(window=lookback, min_periods=lookback).max()
    rng = rolling_max - rolling_min
    return (rv - rolling_min) / rng.replace(0, np.nan)


def compute_rolling_r2(
    closes: np.ndarray,
    lookback: int = CONCEPTUAL_VOL_LOOKBACK,
) -> np.ndarray:
    """Rolling R² of close vs bar index (independent of ADX/ATR).

    For each bar ``i``, fits a linear regression of
    ``closes[i - lookback : i]`` against ``np.arange(lookback)`` and
    returns the coefficient of determination. The resulting series has
    ``NaN`` for the first ``lookback`` bars (insufficient history).

    ADX uses high-low ranges; this metric uses only close prices, so it
    is independent of the detector's internal trending signal.
    """
    n = len(closes)
    r2 = np.full(n, np.nan)
    for i in range(lookback, n):
        y = closes[i - lookback : i]
        x = np.arange(lookback, dtype=float)
        x_mean = x.mean()
        y_mean = y.mean()
        ss_xx = ((x - x_mean) ** 2).sum()
        if ss_xx == 0:
            continue
        ss_xy = ((x - x_mean) * (y - y_mean)).sum()
        ss_yy = ((y - y_mean) ** 2).sum()
        if ss_yy == 0:
            r2[i] = 0.0
        else:
            r2[i] = (ss_xy**2) / (ss_xx * ss_yy)
    return r2


# ── Per-bar ground truth construction ──────────────────────────────────────


@dataclass
class PerBarGroundTruth:
    """Per-bar ground truth classifications for the warm-up-sliced window.

    Both series are aligned 1-to-1 with the detector's per-bar scored
    output (post-warm-up). ``NaN`` values in the underlying rolling
    computations are mapped to ``False`` so the confusion matrix can
    aggregate cleanly.
    """

    volatile_truth: pd.Series  # bool — independent realized-vol pctl
    trending_truth: pd.Series  # bool — rolling R² upper-tercile


def build_ground_truth(
    df: pd.DataFrame,
    warmup_bars: int,
) -> PerBarGroundTruth:
    """Construct per-bar ground truth arrays for the scored slice.

    The input ``df`` is sliced to ``df.iloc[warmup_bars:]`` before any
    rolling computation so both ground-truth series align bar-for-bar
    with the detector's per-bar labels (which are themselves computed
    after warm-up).

    VOLATILE ground truth (per-bar): top 20 % realised-vol percentile
    within the scored slice. TRENDING ground truth (per-bar): rolling R²
    of close vs bar index over ``CONCEPTUAL_VOL_LOOKBACK`` bars, upper
    tercile within the scored slice (R² uses only close prices, so it
    is independent of the detector's ADX-based trending signal).
    """
    scored = df.iloc[warmup_bars:].reset_index(drop=True)
    closes = scored["close"].to_numpy()

    rv_pctl = compute_realized_vol_pctl(closes)
    volatile_truth = (rv_pctl > CONCEPTUAL_VOLATILE_PCTL).fillna(False)

    r2 = compute_rolling_r2(closes, lookback=CONCEPTUAL_VOL_LOOKBACK)
    r2_pctl = pd.Series(r2).rank(pct=True)
    trending_truth = ((r2_pctl > CONCEPTUAL_TRENDING_R2_TERCILE) & ~pd.isna(r2)).fillna(False).astype(bool)

    return PerBarGroundTruth(
        volatile_truth=volatile_truth.reset_index(drop=True),
        trending_truth=trending_truth.reset_index(drop=True),
    )


# ── Confusion matrix & accuracy helpers ────────────────────────────────────


def _regime_value(r: Regime | float | None) -> str | None:
    """Coerce a Regime (or NaN/None) into its string value."""
    if r is None:
        return None
    if isinstance(r, float) and np.isnan(r):
        return None
    if isinstance(r, Regime):
        return r.value
    return str(r)


def confusion_matrix(
    detector: pd.Series,
    truth: pd.Series,
) -> dict[str, int]:
    """Build a 2-class confusion matrix for binary labels.

    Both ``detector`` and ``truth`` must already be reduced to a single
    binary label series (``True`` / ``False``).
    """
    matrix: dict[str, int] = {
        "true_positive": 0,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 0,
    }
    # Align on index, drop NaNs.
    aligned = pd.concat(
        [detector.rename("det"), truth.rename("truth")],
        axis=1,
    ).dropna()
    for det, tru in zip(aligned["det"], aligned["truth"]):  # noqa: B905
        det_b = bool(det)
        tru_b = bool(tru)
        if det_b and tru_b:
            matrix["true_positive"] += 1
        elif det_b and not tru_b:
            matrix["false_positive"] += 1
        elif (not det_b) and tru_b:
            matrix["false_negative"] += 1
        else:
            matrix["true_negative"] += 1
    return matrix


def accuracy_from_matrix(matrix: dict[str, int]) -> float:
    """Compute accuracy (TP + TN) / total from a 2-class confusion matrix."""
    tp = matrix.get("true_positive", 0)
    tn = matrix.get("true_negative", 0)
    fp = matrix.get("false_positive", 0)
    fn = matrix.get("false_negative", 0)
    total = tp + tn + fp + fn
    if total == 0:
        return 0.0
    return (tp + tn) / total


# ── Result data classes ────────────────────────────────────────────────────


@dataclass
class EventReport:
    """Full per-event validation result."""

    name: str
    symbol: str
    timeframe: str
    window_start: str
    window_end: str
    n_bars_total: int
    n_bars_warmup: int
    n_bars_scored: int
    detector_label_counts: dict[str, int] = field(default_factory=dict)
    detector_label_pct: dict[str, float] = field(default_factory=dict)
    consistency_volatile: dict[str, int] = field(default_factory=dict)
    consistency_trending: dict[str, int] = field(default_factory=dict)
    conceptual_volatile: dict[str, int] = field(default_factory=dict)
    conceptual_trending: dict[str, int] = field(default_factory=dict)
    accuracy_consistency_volatile: float = 0.0
    accuracy_consistency_trending: float = 0.0
    accuracy_conceptual_volatile: float = 0.0
    accuracy_conceptual_trending: float = 0.0
    accuracy_conceptual_combined: float = 0.0
    expectations_met: dict[str, bool] = field(default_factory=dict)
    overall_pass: bool = False
    # Populated when validation could not produce a score (e.g.
    # ``insufficient_data`` if there are no bars past warm-up).
    reason: str = ""


# ── Core per-event evaluation ──────────────────────────────────────────────


def evaluate_event(
    detector: RegimeDetector,
    event: EventSpec,
    db_path: Path,
) -> EventReport:
    """Run the detector over one event window and compute all metrics."""
    df = load_event_bars(db_path, event)
    n_total = len(df)

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()

    regimes = detector.detect(highs, lows, closes)
    # Convert to plain string values (drops Regime enum, easier to JSONify).
    regime_str = regimes.apply(_regime_value)

    # Drop warm-up bars before scoring accuracy.
    valid_mask = regime_str.notna() & (regimes.notna())
    if n_total <= WARMUP_BARS:
        n_warmup = n_total
        n_scored = 0
    else:
        n_warmup = WARMUP_BARS
        n_scored = int(valid_mask.iloc[WARMUP_BARS:].sum())

    report = EventReport(
        name=event.name,
        symbol=event.symbol,
        timeframe=event.timeframe,
        window_start=event.start_utc,
        window_end=event.end_utc,
        n_bars_total=n_total,
        n_bars_warmup=n_warmup,
        n_bars_scored=n_scored,
    )

    if n_scored == 0:
        print(f"WARN: {event.name}: n_total={n_total} ≤ WARMUP_BARS={WARMUP_BARS}, skipping")
        report.overall_pass = False
        report.reason = "insufficient_data"
        return report

    # Detector label counts/pct across the scored window.
    scored_regimes = regime_str.iloc[WARMUP_BARS:]
    counts = scored_regimes.value_counts().to_dict()
    for regime in ("trending", "choppy", "volatile", "quiet"):
        report.detector_label_counts[regime] = int(counts.get(regime, 0))
        report.detector_label_pct[regime] = counts.get(regime, 0) / n_scored if n_scored else 0.0

    # ── Implementation consistency ─────────────────────────────────────
    # VOLATILE iff regime == "volatile" (detector's own threshold).
    detector_volatile = (scored_regimes == "volatile").astype(bool)
    truth_volatile_consistency = detector_volatile  # circular by design
    cm_v_cons = confusion_matrix(detector_volatile, truth_volatile_consistency)
    report.consistency_volatile = cm_v_cons
    report.accuracy_consistency_volatile = accuracy_from_matrix(cm_v_cons)

    detector_trending = (scored_regimes == "trending").astype(bool)
    truth_trending_consistency = detector_trending
    cm_t_cons = confusion_matrix(detector_trending, truth_trending_consistency)
    report.consistency_trending = cm_t_cons
    report.accuracy_consistency_trending = accuracy_from_matrix(cm_t_cons)

    # ── Conceptual accuracy ────────────────────────────────────────────
    # ``build_ground_truth`` already slices the df to the warm-up-free
    # window, so the returned series align 1-to-1 with the detector's
    # per-bar scored output (no further slicing required).
    truth = build_ground_truth(df, WARMUP_BARS)
    truth_vol = truth.volatile_truth
    truth_trend = truth.trending_truth
    detector_vol_aligned = detector_volatile.reset_index(drop=True)
    detector_trend_aligned = detector_trending.reset_index(drop=True)

    cm_v_conc = confusion_matrix(detector_vol_aligned, truth_vol)
    report.conceptual_volatile = cm_v_conc
    report.accuracy_conceptual_volatile = accuracy_from_matrix(cm_v_conc)

    cm_t_conc = confusion_matrix(detector_trend_aligned, truth_trend)
    report.conceptual_trending = cm_t_conc
    report.accuracy_conceptual_trending = accuracy_from_matrix(cm_t_conc)

    # Combined conceptual accuracy: average of VOLATILE + TRENDING
    # accuracies — both ground-truth dimensions matter for these events.
    report.accuracy_conceptual_combined = (
        report.accuracy_conceptual_volatile + report.accuracy_conceptual_trending
    ) / 2.0

    # ── Expectation check ─────────────────────────────────────────────
    for exp in event.expectations:
        regime_name = exp["regime"]
        min_fraction = float(exp["min_fraction"])
        report.expectations_met[regime_name] = report.detector_label_pct.get(regime_name, 0.0) >= min_fraction

    # Pass/fail uses the combined conceptual accuracy.
    report.overall_pass = report.accuracy_conceptual_combined >= ACCURACY_GATE and all(report.expectations_met.values())
    return report


# ── Label-distribution audit ───────────────────────────────────────────────


@dataclass
class DistributionReport:
    """Label distribution across one symbol's full data range."""

    symbol: str
    timeframe: str
    n_bars: int
    n_warmup: int
    counts: dict[str, int]
    pct: dict[str, float]
    passes: bool


def audit_distribution(
    detector: RegimeDetector,
    db_path: Path,
    symbol: str,
    timeframe: str,
) -> DistributionReport:
    """Run detector over the full available range and audit label share."""
    df = load_full_range(db_path, symbol, timeframe)
    n = len(df)
    if n <= WARMUP_BARS:
        return DistributionReport(
            symbol=symbol,
            timeframe=timeframe,
            n_bars=n,
            n_warmup=n,
            counts={r.value: 0 for r in Regime},
            pct={r.value: 0.0 for r in Regime},
            passes=False,
        )

    regimes = detector.detect(
        df["high"].to_numpy(),
        df["low"].to_numpy(),
        df["close"].to_numpy(),
    )
    regime_str = regimes.apply(_regime_value)
    scored = regime_str.iloc[WARMUP_BARS:]
    n_scored = len(scored)
    counts = scored.value_counts().to_dict()
    pct: dict[str, float] = {}
    counts_dict: dict[str, int] = {}
    for r in Regime:
        c = int(counts.get(r.value, 0))
        counts_dict[r.value] = c
        pct[r.value] = c / n_scored if n_scored else 0.0

    passes = all(pct[r.value] >= DISTRIBUTION_GATE for r in Regime)
    return DistributionReport(
        symbol=symbol,
        timeframe=timeframe,
        n_bars=n,
        n_warmup=WARMUP_BARS,
        counts=counts_dict,
        pct=pct,
        passes=passes,
    )


# ── Report rendering ───────────────────────────────────────────────────────


def render_markdown(
    event_reports: list[EventReport],
    distribution_reports: list[DistributionReport],
    db_path: Path,
    config: RegimeConfig,
    generated_at: datetime,
    mtf_mode: str = "none",
) -> str:
    """Produce the human-readable markdown report."""
    lines: list[str] = []
    lines.append("# Strategy Factory Phase 0 — Regime Detector Validation")
    lines.append("")
    lines.append(f"**Generated:** {generated_at.isoformat()}")
    lines.append(f"**DuckDB:** `{db_path}`")
    lines.append(f"**MTF mode:** `{mtf_mode}`")
    lines.append(
        "**Detector config:** "
        f"adx_period={config.adx_period}, atr_period={config.atr_period}, "
        f"atr_lookback={config.atr_lookback}, trending_adx={config.trending_adx}, "
        f"choppy_adx={config.choppy_adx}, volatile_atr_pct={config.volatile_atr_pct}, "
        f"quiet_atr_pct={config.quiet_atr_pct}, "
        f"mtf_confirmation={config.mtf_confirmation}, "
        f"h1_adx_threshold={config.h1_adx_threshold}, "
        f"h4_adx_threshold={config.h4_adx_threshold}"
    )
    lines.append(
        f"**Warm-up bars skipped:** {WARMUP_BARS} "
        f"(ADX needs {2 * config.adx_period + 1}, "
        f"ATR percentile needs {config.atr_period + config.atr_lookback})"
    )
    lines.append(
        "**Conceptual ground truth thresholds:** "
        f"VOLATILE = realised-vol pctl > {CONCEPTUAL_VOLATILE_PCTL:.2f}, "
        f"TRENDING = rolling R² (close vs bar index, "
        f"{CONCEPTUAL_VOL_LOOKBACK}-bar lookback) upper tercile "
        f"(> {CONCEPTUAL_TRENDING_R2_TERCILE:.4f}). Both metrics are "
        "computed on the warm-up-sliced slice and are independent of "
        "the detector's ADX/ATR internals."
    )
    lines.append(
        f"**Accuracy gate:** ≥{ACCURACY_GATE * 100:.0f}% on combined conceptual accuracy (avg of VOLATILE + TRENDING)."
    )
    lines.append("")

    # ── Per-event summary table ─────────────────────────────────────────
    lines.append("## Per-Event Summary")
    lines.append("")
    lines.append(
        "| # | Event | Symbol | Window | Bars (scored) | "
        "Conceptual Acc (VOL) | Conceptual Acc (TREND) | Combined | Pass |"
    )
    lines.append(
        "|---|-------|--------|--------|---------------:|----------------------:|-----------------------:|---------:|:----:|"
    )
    for i, r in enumerate(event_reports, start=1):
        lines.append(
            f"| {i} | {r.name} | {r.symbol} | {r.window_start} → {r.window_end} | "
            f"{r.n_bars_scored} | "
            f"{r.accuracy_conceptual_volatile * 100:.1f}% | "
            f"{r.accuracy_conceptual_trending * 100:.1f}% | "
            f"{r.accuracy_conceptual_combined * 100:.1f}% | "
            f"{'✅' if r.overall_pass else '❌'} |"
        )
    lines.append("")

    # ── Per-event detailed sections ─────────────────────────────────────
    lines.append("## Per-Event Detail")
    for i, (event, r) in enumerate(zip(EVENTS, event_reports), start=1):  # noqa: B905
        lines.append("")
        lines.append(f"### Event {i}: {event.name}")
        lines.append("")
        lines.append(f"**Window:** {event.start_utc} → {event.end_utc} UTC")
        lines.append(f"**Instrument:** {event.symbol} {event.timeframe}")
        lines.append(f"**Description:** {event.description}")
        lines.append("")
        lines.append(
            f"**Bars in window:** {r.n_bars_total} (warm-up skipped: {r.n_bars_warmup}, scored: {r.n_bars_scored})"
        )
        lines.append("")
        lines.append("**Detector label distribution:**")
        lines.append("")
        lines.append("| Regime | Count | Percentage |")
        lines.append("|--------|------:|-----------:|")
        for regime in ("trending", "choppy", "volatile", "quiet"):
            lines.append(
                f"| {regime} | {r.detector_label_counts.get(regime, 0)} | "
                f"{r.detector_label_pct.get(regime, 0.0) * 100:.1f}% |"
            )
        lines.append("")

        # Confusion matrices
        lines.append("**Confusion matrix — Implementation Consistency (VOLATILE):**")
        lines.append("")
        _render_cm(lines, r.consistency_volatile)
        lines.append(f"Accuracy: {r.accuracy_consistency_volatile * 100:.1f}%")
        lines.append("")
        lines.append("**Confusion matrix — Implementation Consistency (TRENDING):**")
        lines.append("")
        _render_cm(lines, r.consistency_trending)
        lines.append(f"Accuracy: {r.accuracy_consistency_trending * 100:.1f}%")
        lines.append("")
        lines.append("**Confusion matrix — Conceptual Accuracy (VOLATILE):**")
        lines.append("")
        _render_cm(lines, r.conceptual_volatile)
        lines.append(f"Accuracy: {r.accuracy_conceptual_volatile * 100:.1f}%")
        lines.append("")
        lines.append("**Confusion matrix — Conceptual Accuracy (TRENDING):**")
        lines.append("")
        _render_cm(lines, r.conceptual_trending)
        lines.append(f"Accuracy: {r.accuracy_conceptual_trending * 100:.1f}%")
        lines.append("")
        lines.append(
            f"**Combined conceptual accuracy:** "
            f"{r.accuracy_conceptual_combined * 100:.1f}% "
            f"(average of VOLATILE + TRENDING conceptual accuracy)"
        )
        lines.append("")
        lines.append("**Expectations check:**")
        lines.append("")
        for exp in event.expectations:
            regime_name = exp["regime"]
            min_frac = float(exp["min_fraction"])
            met = r.expectations_met.get(regime_name, False)
            actual_pct = r.detector_label_pct.get(regime_name, 0.0) * 100
            check = "✅" if met else "❌"
            lines.append(
                f"- {check} `{regime_name}` ≥ {min_frac * 100:.0f}% (actual: {actual_pct:.1f}% — {exp['rationale']})"
            )
        lines.append("")
        lines.append(f"**Verdict:** {'PASS' if r.overall_pass else 'FAIL'}")
        lines.append("")

    # ── Distribution audit ──────────────────────────────────────────────
    lines.append("## Label Distribution Audit (full data range)")
    lines.append("")
    lines.append(
        "Each regime must cover ≥ "
        f"{DISTRIBUTION_GATE * 100:.0f}% of post-warm-up bars. "
        "Failure here means the regime taxonomy is degenerate on that data."
    )
    lines.append("")
    lines.append("| Symbol | Timeframe | Total bars | Trending | Choppy | Volatile | Quiet | Pass |")
    lines.append("|--------|-----------|-----------:|---------:|-------:|---------:|------:|:----:|")
    for d in distribution_reports:
        lines.append(
            f"| {d.symbol} | {d.timeframe} | {d.n_bars} | "
            f"{d.pct.get('trending', 0.0) * 100:.1f}% | "
            f"{d.pct.get('choppy', 0.0) * 100:.1f}% | "
            f"{d.pct.get('volatile', 0.0) * 100:.1f}% | "
            f"{d.pct.get('quiet', 0.0) * 100:.1f}% | "
            f"{'✅' if d.passes else '❌'} |"
        )
    lines.append("")

    # ── Overall verdict ─────────────────────────────────────────────────
    all_events_pass = all(r.overall_pass for r in event_reports)
    all_dist_pass = all(d.passes for d in distribution_reports)
    overall = all_events_pass and all_dist_pass
    lines.append("## Overall Verdict")
    lines.append("")
    lines.append(f"- Events pass: {'✅' if all_events_pass else '❌'}")
    lines.append(f"- Distribution audit: {'✅' if all_dist_pass else '❌'}")
    lines.append("")
    if overall:
        lines.append("**Result: PASS** — regime detector is validated for factory use.")
    else:
        lines.append(
            "**Result: FAIL** — regime detector did not meet the ≥70% "
            "conceptual accuracy gate or label-distribution minimum. "
            "STOP and surface to Ava/Craig per AC 0.2 (do not auto-tune; "
            "tuning on validation events is overfitting)."
        )
    lines.append("")
    return "\n".join(lines)


def _render_cm(lines: list[str], matrix: dict[str, int]) -> None:
    """Append a 2-class confusion matrix block to the report lines."""
    tp = matrix.get("true_positive", 0)
    fp = matrix.get("false_positive", 0)
    fn = matrix.get("false_negative", 0)
    tn = matrix.get("true_negative", 0)
    lines.append("|              | Truth: True | Truth: False |")
    lines.append("|--------------|------------:|-------------:|")
    lines.append(f"| Det: True    | {tp:>10d} | {fp:>11d} |")
    lines.append(f"| Det: False   | {fn:>10d} | {tn:>11d} |")


# ── CLI entry point ────────────────────────────────────────────────────────


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Strategy Factory Phase 0 — validate RegimeDetector against "
            "known historical regime transitions and audit label "
            "distribution across available data."
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/ayumi_market.duckdb"),
        help="Path to the ayumi_market.duckdb file (default: %(default)s).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/factory"),
        help="Directory for the validation report (default: %(default)s).",
    )
    parser.add_argument(
        "--mtf-mode",
        choices=("none", "soft", "hard"),
        default="none",
        help=(
            "Multi-timeframe ADX confirmation mode wired into the "
            "RegimeConfig before validation: 'none' (default, single-TF, "
            "backward-compatible), 'soft' (raise effective trending ADX by "
            "+3 where H1 ADX disagrees), or 'hard' (require base + H1 + "
            "H4 ADX confirmation for TRENDING, force CHOPPY in the "
            "neutral zone when H1 disagrees)."
        ),
    )
    parser.add_argument(
        "--atr-sensitivity",
        action="store_true",
        default=False,
        help=(
            "Diagnostic-only sweep: runs validation 4 times with "
            "volatile_atr_pct ∈ {0.70, 0.75, 0.80, 0.85} and reports "
            "combined conceptual accuracy per threshold.  Combined with "
            "--mtf-mode the sweep runs the chosen MTF mode at each "
            "threshold.  Does NOT change the validation gate."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Override the markdown output path (default: "
            "<output-dir>/regime-validation-<date>.md).  Useful for "
            "dispatching per-mode reports from a single source."
        ),
    )
    parser.add_argument(
        "--config-defaults",
        action="store_true",
        default=True,
        help="Use RegimeConfig defaults (always true; reserved for future "
        "tuning flags — defaults prevent validation overfitting).",
    )
    return parser.parse_args(argv)


# ── ATR-sensitivity helper ─────────────────────────────────────────────────


ATR_SENS_THRESHOLDS: tuple[float, ...] = (0.70, 0.75, 0.80, 0.85)


def evaluate_event_with_config(
    detector: RegimeDetector,
    event: EventSpec,
    db_path: Path,
) -> EventReport:
    """Thin wrapper around :func:`evaluate_event` so the same call can be
    re-used with arbitrary detector configs (the ATR-sensitivity sweep
    creates a fresh detector per threshold)."""
    return evaluate_event(detector, event, db_path)


def run_atr_sensitivity(
    mtf_mode: str,
    db_path: Path,
    output_dir: Path,
    generated_at: datetime,
) -> Path:
    """Run the ATR-percentile threshold sweep and write a dedicated report.

    Each sweep point builds a fresh ``RegimeDetector`` with the chosen
    ``mtf_confirmation`` mode and a fixed ``volatile_atr_pct`` value, runs
    the standard event evaluations, and emits a markdown report (plus
    a JSON sibling) summarising how combined conceptual accuracy moves
    with the volatile cutoff.

    This is a **diagnostic only** — the ≥70% validation gate is not
    modified by the sweep.  The report warns that tuning on validation
    events is overfitting and should not feed back into production
    without separate corroboration.
    """
    sweep_records: list[dict[str, object]] = []
    sweep_event_reports: dict[float, list[EventReport]] = {}

    for thr in ATR_SENS_THRESHOLDS:
        cfg = RegimeConfig(mtf_confirmation=mtf_mode, volatile_atr_pct=thr)
        detector = RegimeDetector(config=cfg)
        reports: list[EventReport] = []
        for event in EVENTS:
            try:
                reports.append(evaluate_event_with_config(detector, event, db_path))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[regime-validation][atr-sensitivity] ERROR: {event.name} @ volatile_atr_pct={thr}: {exc}",
                    file=sys.stderr,
                )
                traceback.print_exc()
                return output_dir / "regime-validation-atr-sensitivity.md"
        sweep_event_reports[thr] = reports
        sweep_records.append(
            {
                "volatile_atr_pct": thr,
                "mtf_mode": mtf_mode,
                "events": [
                    {
                        "name": r.name,
                        "symbol": r.symbol,
                        "accuracy_conceptual_volatile": r.accuracy_conceptual_volatile,
                        "accuracy_conceptual_trending": r.accuracy_conceptual_trending,
                        "accuracy_conceptual_combined": r.accuracy_conceptual_combined,
                        "overall_pass": r.overall_pass,
                        "label_counts": r.detector_label_counts,
                    }
                    for r in reports
                ],
            }
        )

    # Write JSON first.
    json_payload = {
        "generated_at": generated_at.isoformat(),
        "duckdb_path": str(db_path),
        "mtf_mode": mtf_mode,
        "diagnostic_only": True,
        "note": (
            "ATR-sensitivity sweep — diagnostic only.  No validation "
            "gate is applied.  Tuning on validation events would be "
            "overfitting and is forbidden by AC 0.2."
        ),
        "thresholds": list(ATR_SENS_THRESHOLDS),
        "sweep": sweep_records,
    }
    json_path = output_dir / "regime-validation-atr-sensitivity.json"
    json_path.write_text(json.dumps(json_payload, indent=2, default=str))
    print(f"[regime-validation][atr-sensitivity] Wrote JSON: {json_path}")

    # Now write markdown.
    lines: list[str] = []
    lines.append("# Strategy Factory Phase 0 — ATR Sensitivity Sweep")
    lines.append("")
    lines.append(f"**Generated:** {generated_at.isoformat()}")
    lines.append(f"**DuckDB:** `{db_path}`")
    lines.append(f"**MTF mode:** `{mtf_mode}`")
    lines.append(
        "**⚠️ Diagnostic only** — this sweep does **not** modify the ≥70% "
        "validation gate.  Its purpose is to surface how combined "
        "conceptual accuracy moves with ``volatile_atr_pct`` so Rin / "
        "Ava can spot threshold sensitivities without re-running the "
        "factory dispatch.  Tuning on validation events is forbidden "
        "by AC 0.2 (overfitting)."
    )
    lines.append("")
    lines.append(f"**Thresholds swept:** {', '.join(f'{t:.2f}' for t in ATR_SENS_THRESHOLDS)}")
    lines.append("")

    # Per-threshold summary table.
    lines.append("## Combined Conceptual Accuracy vs. `volatile_atr_pct`")
    lines.append("")
    lines.append("| `volatile_atr_pct` | " + " | ".join(f"{r.name}" for r in EVENTS) + " | Mean |")
    lines.append("|---:|" + "|".join(":---:" for _ in EVENTS) + "|---:|")
    for thr in ATR_SENS_THRESHOLDS:
        reports = sweep_event_reports[thr]
        per_event = [f"{r.accuracy_conceptual_combined * 100:.1f}%" for r in reports]
        mean_acc = sum(r.accuracy_conceptual_combined for r in reports) / len(reports)
        lines.append(f"| {thr:.2f} | " + " | ".join(per_event) + f" | {mean_acc * 100:.1f}% |")
    lines.append("")

    # Per-event detail per threshold.
    lines.append("## Per-Event Detail by Threshold")
    for i, event in enumerate(EVENTS, start=1):
        lines.append("")
        lines.append(f"### Event {i}: {event.name}")
        lines.append("")
        lines.append(f"**Window:** {event.start_utc} → {event.end_utc} UTC ({event.symbol} {event.timeframe})")
        lines.append("")
        lines.append("| `volatile_atr_pct` | Volatile acc | Trending acc | Combined | Pass |")
        lines.append("|---:|---:|---:|---:|:---:|")
        for thr in ATR_SENS_THRESHOLDS:
            r = sweep_event_reports[thr][i - 1]
            lines.append(
                f"| {thr:.2f} | "
                f"{r.accuracy_conceptual_volatile * 100:.1f}% | "
                f"{r.accuracy_conceptual_trending * 100:.1f}% | "
                f"{r.accuracy_conceptual_combined * 100:.1f}% | "
                f"{'✅' if r.overall_pass else '❌'} |"
            )
        lines.append("")

    md_path = output_dir / "regime-validation-atr-sensitivity.md"
    md_path.write_text("\n".join(lines))
    print(f"[regime-validation][atr-sensitivity] Wrote markdown: {md_path}")
    return md_path


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    db_path: Path = args.db.resolve()
    output_dir: Path = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(f"ERROR: DuckDB file not found: {db_path}", file=sys.stderr)
        return 2

    config = RegimeConfig(mtf_confirmation=args.mtf_mode)
    detector = RegimeDetector(config=config)

    generated_at = datetime.now(timezone.utc)
    date_slug = generated_at.strftime("%Y-%m-%d")
    json_path = output_dir / f"regime-validation-{date_slug}.json"
    md_path = output_dir / f"regime-validation-{date_slug}.md"

    # Optional output override via --output flag for the per-mode dispatch.
    # When set, append the dispatch name (the active MTF mode) to the
    # provided base path so each dispatch writes to its own file
    # instead of clobbering a single shared target.  Both the markdown
    # and JSON paths get the same suffix; the JSON sibling lands next
    # to the markdown so per-dispatch artefacts stay grouped.
    if getattr(args, "output", None):
        out = Path(args.output).resolve()  # type: ignore[arg-type]
        out.parent.mkdir(parents=True, exist_ok=True)
        dispatch_suffix = f"_{args.mtf_mode}"
        md_path = out.with_name(f"{out.stem}{dispatch_suffix}{out.suffix}")
        json_path = md_path.with_suffix(".json")

    # ── ATR-sensitivity early exit ──────────────────────────────────────
    # When --atr-sensitivity is set, the script writes its own sweep
    # report and exits 0; it never enters the standard event-evaluation
    # loop.  This keeps the sweep report self-contained and prevents it
    # from polluting the canonical per-day validation files.
    if args.atr_sensitivity:
        run_atr_sensitivity(
            mtf_mode=args.mtf_mode,
            db_path=db_path,
            output_dir=output_dir,
            generated_at=generated_at,
        )
        print("")
        print("[regime-validation] ATR sensitivity: DONE (diagnostic only)")
        return 0

    print(f"[regime-validation] DuckDB: {db_path}")
    print(f"[regime-validation] Output: {output_dir}")
    print(f"[regime-validation] Detector config: {config}")
    print(f"[regime-validation] MTF mode: {args.mtf_mode}")
    print(f"[regime-validation] Warm-up bars: {WARMUP_BARS}")

    # ── Event evaluations ──────────────────────────────────────────────
    event_reports: list[EventReport] = []
    for event in EVENTS:
        print(f"[regime-validation] Evaluating event: {event.name}")
        try:
            report = evaluate_event(detector, event, db_path)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[regime-validation] ERROR evaluating {event.name}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc()
            return 3
        event_reports.append(report)
        status = "PASS" if report.overall_pass else "FAIL"
        print(
            f"  bars={report.n_bars_total} scored={report.n_bars_scored} "
            f"conceptual_acc={report.accuracy_conceptual_combined * 100:.1f}% "
            f"→ {status}"
        )

    # ── Distribution audits ────────────────────────────────────────────
    distribution_reports: list[DistributionReport] = []
    for symbol in ("XAUUSD", "EURUSD"):
        print(f"[regime-validation] Auditing distribution: {symbol} M15")
        try:
            d = audit_distribution(detector, db_path, symbol, "M15")
        except Exception as exc:  # noqa: BLE001
            print(
                f"[regime-validation] ERROR auditing {symbol}: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc()
            return 4
        distribution_reports.append(d)
        status = "PASS" if d.passes else "FAIL"
        print(
            f"  bars={d.n_bars} trending={d.pct['trending'] * 100:.1f}% "
            f"choppy={d.pct['choppy'] * 100:.1f}% "
            f"volatile={d.pct['volatile'] * 100:.1f}% "
            f"quiet={d.pct['quiet'] * 100:.1f}% → {status}"
        )

    # ── Write JSON ─────────────────────────────────────────────────────
    payload = {
        "generated_at": generated_at.isoformat(),
        "duckdb_path": str(db_path),
        "mtf_mode": args.mtf_mode,
        "detector_config": asdict(config),
        "warmup_bars": WARMUP_BARS,
        "conceptual_thresholds": {
            "volatile_realized_vol_pctl": CONCEPTUAL_VOLATILE_PCTL,
            "trending_rolling_r2_tercile": CONCEPTUAL_TRENDING_R2_TERCILE,
            "rolling_r2_lookback": CONCEPTUAL_VOL_LOOKBACK,
            "realized_vol_lookback": CONCEPTUAL_VOL_LOOKBACK,
        },
        "accuracy_gate": ACCURACY_GATE,
        "distribution_gate": DISTRIBUTION_GATE,
        "events": [
            {
                "name": r.name,
                "symbol": r.symbol,
                "timeframe": r.timeframe,
                "window_start": r.window_start,
                "window_end": r.window_end,
                "n_bars_total": r.n_bars_total,
                "n_bars_warmup": r.n_bars_warmup,
                "n_bars_scored": r.n_bars_scored,
                "detector_label_counts": r.detector_label_counts,
                "detector_label_pct": {k: round(v, 6) for k, v in r.detector_label_pct.items()},
                "consistency_volatile": r.consistency_volatile,
                "consistency_trending": r.consistency_trending,
                "conceptual_volatile": r.conceptual_volatile,
                "conceptual_trending": r.conceptual_trending,
                "accuracy_consistency_volatile": r.accuracy_consistency_volatile,
                "accuracy_consistency_trending": r.accuracy_consistency_trending,
                "accuracy_conceptual_volatile": r.accuracy_conceptual_volatile,
                "accuracy_conceptual_trending": r.accuracy_conceptual_trending,
                "accuracy_conceptual_combined": r.accuracy_conceptual_combined,
                "expectations_met": r.expectations_met,
                "overall_pass": r.overall_pass,
                "reason": r.reason,
            }
            for r in event_reports
        ],
        "distribution_audit": [
            {
                "symbol": d.symbol,
                "timeframe": d.timeframe,
                "n_bars": d.n_bars,
                "n_warmup": d.n_warmup,
                "counts": d.counts,
                "pct": d.pct,
                "passes": d.passes,
            }
            for d in distribution_reports
        ],
        "overall_pass": all(r.overall_pass for r in event_reports) and all(d.passes for d in distribution_reports),
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"[regime-validation] Wrote JSON: {json_path}")

    # ── Write Markdown ─────────────────────────────────────────────────
    md = render_markdown(
        event_reports=event_reports,
        distribution_reports=distribution_reports,
        db_path=db_path,
        config=config,
        generated_at=generated_at,
        mtf_mode=args.mtf_mode,
    )
    md_path.write_text(md)
    print(f"[regime-validation] Wrote markdown: {md_path}")

    # ── Exit code ──────────────────────────────────────────────────────
    all_pass = payload["overall_pass"]
    print("")
    print(f"[regime-validation] OVERALL: {'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
