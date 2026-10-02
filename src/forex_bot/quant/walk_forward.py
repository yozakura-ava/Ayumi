from __future__ import annotations

import math
import warnings
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backtest.engine import Bar, MarketState, determine_session
from backtest.strategies import ISignalStrategy
from quant.statistical_validation import (
    GoNogoResult,
    evaluate_statistical_checks,
)

#: Default fallback annualization factor used when ``bars_in_window`` is not
#: supplied to :func:`_compute_metrics`. **This value preserves the legacy
#: (and incorrect) formula and is retained only for backward-compatibility**
#: — see :func:`_compute_metrics` migration note. New callers should pass
#: ``bars_in_window`` so the annualization is computed from the actual
#: window duration. Card 2bd35527-e118-44c8-aca7-019901b8fbe5.
_LEGACY_TRADES_PER_YEAR_FALLBACK = 252

#: Seconds in a tropical year (365.25 days). Used to convert a window's bar
#: time span into years for ``trades_per_year`` annualization. Card
#: 2bd35527-e118-44c8-aca7-019901b8fbe5.
_SECONDS_PER_YEAR = 365.25 * 24 * 3600.0


@dataclass(frozen=True)
class WindowMetrics:
    window_index: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    sharpe_ratio: float
    trade_count: int
    total_pnl: float
    passed_go_nogo: bool
    # Regime labels
    regime_volatility: str = "unknown"
    regime_trend: str = "unknown"
    regime_session: str = "unknown"
    regime_combined: str = "unknown"
    regime_quality: float = 0.0
    # BTC macro regime overlay (BQ-508)
    btc_regime: str = "unknown"


@dataclass(frozen=True)
class AggregatedMetrics:
    mean_win_rate: float
    std_win_rate: float
    mean_profit_factor: float
    std_profit_factor: float
    mean_max_drawdown: float
    std_max_drawdown: float
    mean_sharpe_ratio: float
    std_sharpe_ratio: float
    mean_trade_count: float
    std_trade_count: float
    mean_total_pnl: float
    std_total_pnl: float
    windows_passed: int
    total_windows: int


@dataclass
class WalkForwardResults:
    per_window: list[WindowMetrics] = field(default_factory=list)
    aggregated: AggregatedMetrics | None = None
    go_nogo: bool = False
    go_nogo_result: GoNogoResult | None = None


@dataclass
class WalkForwardValidator:
    data: list[Any]
    n_windows: int = 3
    train_ratio: float = 0.7
    val_ratio: float = 0.15
    overlap_ratio: float = 0.2
    embargo_bars: int = 0

    def __post_init__(self) -> None:
        if self.n_windows < 3:
            raise ValueError(f"n_windows must be >= 3, got {self.n_windows}")
        if self.train_ratio <= 0 or self.train_ratio >= 1:
            raise ValueError(f"train_ratio must be in (0, 1), got {self.train_ratio}")
        if self.val_ratio <= 0 or self.val_ratio >= 1:
            raise ValueError(f"val_ratio must be in (0, 1), got {self.val_ratio}")
        if self.train_ratio + self.val_ratio >= 1:
            raise ValueError("train_ratio + val_ratio must be < 1")
        if self.overlap_ratio < 0 or self.overlap_ratio >= 1:
            raise ValueError(f"overlap_ratio must be in [0, 1), got {self.overlap_ratio}")
        if self.embargo_bars < 0:
            raise ValueError(f"embargo_bars must be >= 0, got {self.embargo_bars}")

    def split(self, data: list[Any] | None = None) -> Generator[tuple[list[Any], list[Any], list[Any]], None, None]:
        source = data if data is not None else self.data
        n = len(source)
        if n == 0:
            return

        test_ratio = 1.0 - self.train_ratio - self.val_ratio
        full_window_size = int(n / self.n_windows)
        if full_window_size == 0:
            raise ValueError(f"Data length ({n}) is too small for {self.n_windows} windows")

        train_size = int(full_window_size * self.train_ratio)
        val_size = int(full_window_size * self.val_ratio)
        test_size = int(full_window_size * test_ratio)
        window_size = train_size + val_size + test_size

        if window_size == 0:
            return

        overlap_size = int(window_size * self.overlap_ratio)
        step = max(window_size - overlap_size, 1)

        for i in range(self.n_windows):
            start = i * step
            end = start + window_size
            if end > n:
                end = n
                start = end - window_size
                if start < 0:
                    start = 0

            window = source[start:end]
            if len(window) < window_size:
                actual_window_size = len(window)
                actual_train = int(actual_window_size * self.train_ratio)
                actual_val = int(actual_window_size * self.val_ratio)
            else:
                actual_train = train_size
                actual_val = val_size

            train = window[:actual_train]
            val = window[actual_train : actual_train + actual_val]
            # Embargo: skip embargo_bars between val end and test start
            # to prevent autocorrelation leakage at train/test boundaries.
            test_start = actual_train + actual_val + self.embargo_bars
            test = window[test_start:]

            if not train or not val or not test:
                continue

            yield train, val, test


def _compute_metrics(
    window_index: int,
    trades: list[dict[str, Any]],
    initial_balance: float = 10000.0,
    bars_in_window: list[Bar] | None = None,
) -> WindowMetrics:
    """Compute per-window metrics for a walk-forward slice.

    Migration note (card 2bd35527-e118-44c8-aca7-019901b8fbe5)
    ----------------------------------------------------------
    The Sharpe computation was changed from a per-trade dollar-PnL formula
    annualised as if daily to a returns-based formula annualised by the
    strategy's actual trade frequency. The legacy formula

        (mean_pnl / std_pnl) * sqrt(252)

    annualised per-trade dollar PnL as if it were a daily return and used
    raw dollars instead of returns. For M15–H4 strategies (30–2000+
    trades/yr) this inflated Sharpe by orders of magnitude — e.g. 917 on
    EURUSD H4 with 32 trades — making absolute values uninterpretable
    while still preserving the relative stream ranking.

    The fixed formula is

        sharpe = (mean_return / std_return) * sqrt(trades_per_year)

    where ``return_i = pnl_i / equity_before_trade_i`` and
    ``trades_per_year = trade_count / window_duration_years`` is derived
    from the bar times in ``bars_in_window``. Industry-typical FX Sharpe
    is 0.5–2.0; the fixed formula yields values in that band.

    Threshold semantics: DSR tier thresholds (``TIER_A_PRODUCTION.
    min_aggregate_sharpe=1.50`` etc., see ``quant/oos_gate.py:323``)
    compare mean Sharpe across streams on the same scale. After this fix
    the scale is a returns-based, properly-annualised Sharpe, so absolute
    tier membership may shift even when relative ranking is preserved.
    **The threshold *values* are unchanged.** Tuning the thresholds to
    recover the pre-fix tier membership would mask the bug; any
    threshold change requires a separate spec with explicit approval.

    Backward compatibility
    ----------------------
    ``bars_in_window`` is optional. When omitted (or ``None``) the legacy
    formula is used and a ``DeprecationWarning`` is emitted so callers
    can detect that they are still on the unfixed path. New callers
    should pass ``bars_in_window=test_bars`` to receive the fixed Sharpe.
    """
    if not trades:
        return WindowMetrics(
            window_index=window_index,
            win_rate=0.0,
            profit_factor=0.0,
            max_drawdown=0.0,
            sharpe_ratio=0.0,
            trade_count=0,
            total_pnl=0.0,
            passed_go_nogo=False,
        )

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_win = sum(wins)
    total_loss = abs(sum(losses))

    win_rate = len(wins) / len(pnls)
    if total_loss > 0:
        profit_factor = total_win / total_loss
    elif total_win > 0:
        profit_factor = 10.0
    else:
        profit_factor = 0.0
    total_pnl = sum(pnls)
    trade_count = len(pnls)

    balance = initial_balance
    peak_balance = initial_balance
    max_dd = 0.0
    for p in pnls:
        balance = max(0.0, balance + p)
        if balance > peak_balance:
            peak_balance = balance
        if peak_balance > 0:
            dd = (peak_balance - balance) / peak_balance
            if dd > max_dd:
                max_dd = dd

    # ---- Sharpe (returns-based, properly annualised) --------------------
    # See migration note in the docstring above.
    if trade_count < 2:
        sharpe_ratio = 0.0
    elif bars_in_window is None:
        # Legacy path: dollar PnL Sharpe, annualised as if daily. Kept for
        # back-compat with callers that have not migrated yet. Emits a
        # DeprecationWarning so the unfixed path is loud, not silent.
        warnings.warn(
            "_compute_metrics called without bars_in_window; using the "
            "legacy Sharpe formula (per-trade dollar PnL annualised as "
            "if daily). This is the pre-fix Sharpe-inflation bug. Pass "
            "bars_in_window=<bars for this window> to receive the fixed "
            "returns-based Sharpe. Card 2bd35527.",
            DeprecationWarning,
            stacklevel=2,
        )
        mean_pnl = total_pnl / trade_count
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (trade_count - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.0
        sharpe_ratio = (
            (mean_pnl / std_pnl) * math.sqrt(_LEGACY_TRADES_PER_YEAR_FALLBACK)
            if std_pnl > 0
            else 0.0
        )
    else:
        # Fixed path: per-trade returns scaled by sqrt(trades_per_year).
        # - returns_i = pnl_i / equity_before_trade_i  (returns basis)
        # - trades_per_year = trade_count / window_duration_years
        # - window_duration_years from bar.time span of bars_in_window
        equities: list[float] = []
        returns: list[float] = []
        running = initial_balance
        for pnl in pnls:
            equities.append(running)
            ret = pnl / running if running > 0 else 0.0
            returns.append(ret)
            running = max(0.0, running + pnl)

        mean_return = sum(returns) / trade_count
        variance = sum((r - mean_return) ** 2 for r in returns) / (trade_count - 1)
        std_return = math.sqrt(variance) if variance > 0 else 0.0

        trades_per_year = _trades_per_year_from_bars(bars_in_window, trade_count)
        if std_return > 0 and trades_per_year > 0:
            sharpe_ratio = (mean_return / std_return) * math.sqrt(trades_per_year)
        else:
            sharpe_ratio = 0.0

    min_trades = 5
    passed = trade_count >= min_trades and win_rate > 0.55 and profit_factor > 1.0 and total_pnl > 0 and max_dd < 0.10

    return WindowMetrics(
        window_index=window_index,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown=max_dd,
        sharpe_ratio=sharpe_ratio,
        trade_count=trade_count,
        total_pnl=total_pnl,
        passed_go_nogo=passed,
    )


def _trades_per_year_from_bars(
    bars_in_window: list[Bar], trade_count: int
) -> float:
    """Estimate ``trades_per_year`` from a window's bars.

    Uses the bar-time span of ``bars_in_window`` as the window duration.
    Falls back to ``trade_count`` (i.e. assume the window *is* one year)
    when bar times are missing, monotonic-inverse, or zero-span. Card
    2bd35527-e118-44c8-aca7-019901b8fbe5.
    """
    if trade_count <= 0 or len(bars_in_window) < 2:
        return float(trade_count) if trade_count > 0 else 0.0
    try:
        t_first = bars_in_window[0].time
        t_last = bars_in_window[-1].time
    except AttributeError:
        return float(trade_count)
    if not isinstance(t_first, datetime) or not isinstance(t_last, datetime):
        return float(trade_count)
    duration_seconds = (t_last - t_first).total_seconds()
    if duration_seconds <= 0:
        return float(trade_count)
    duration_years = duration_seconds / _SECONDS_PER_YEAR
    if duration_years <= 0:
        return float(trade_count)
    return trade_count / duration_years


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def run_strategy(
    strategy: ISignalStrategy,
    bars: list[Bar],
    n_windows: int = 3,
    train_ratio: float = 0.7,
    val_ratio: float = 0.15,
    overlap_ratio: float = 0.2,
    initial_balance: float = 10000.0,
    risk_per_trade_pct: float = 0.005,
    embargo_bars: int = 0,
) -> WalkForwardResults:
    validator = WalkForwardValidator(
        data=bars,
        n_windows=n_windows,
        train_ratio=train_ratio,
        val_ratio=val_ratio,
        overlap_ratio=overlap_ratio,
        embargo_bars=embargo_bars,
    )

    per_window: list[WindowMetrics] = []
    all_oos_pnls: list[float] = []
    for idx, (train, val, test) in enumerate(validator.split(bars)):  # noqa: B007
        if len(test) < 10:
            metrics = WindowMetrics(
                window_index=idx,
                win_rate=0.0,
                profit_factor=0.0,
                max_drawdown=0.0,
                sharpe_ratio=0.0,
                trade_count=0,
                total_pnl=0.0,
                passed_go_nogo=False,
            )
            per_window.append(metrics)
            continue

        trades = _run_strategy_window(
            strategy=strategy,
            test_bars=test,
            initial_balance=initial_balance,
            risk_per_trade_pct=risk_per_trade_pct,
        )
        metrics = _compute_metrics(
            idx,
            trades,
            initial_balance=initial_balance,
            bars_in_window=test,
        )
        per_window.append(metrics)
        all_oos_pnls.extend(t["pnl"] for t in trades)

    aggregated = None
    if per_window:
        wr_values = [m.win_rate for m in per_window]
        pf_values = [m.profit_factor for m in per_window]
        dd_values = [m.max_drawdown for m in per_window]
        sr_values = [m.sharpe_ratio for m in per_window]
        tc_values = [float(m.trade_count) for m in per_window]
        pnl_values = [m.total_pnl for m in per_window]

        mean_wr = _mean(wr_values)
        mean_pf = _mean(pf_values)
        mean_dd = _mean(dd_values)
        mean_sr = _mean(sr_values)
        mean_tc = _mean(tc_values)
        mean_pnl = _mean(pnl_values)

        aggregated = AggregatedMetrics(
            mean_win_rate=mean_wr,
            std_win_rate=_std(wr_values, mean_wr),
            mean_profit_factor=mean_pf,
            std_profit_factor=_std(pf_values, mean_pf),
            mean_max_drawdown=mean_dd,
            std_max_drawdown=_std(dd_values, mean_dd),
            mean_sharpe_ratio=mean_sr,
            std_sharpe_ratio=_std(sr_values, mean_sr),
            mean_trade_count=mean_tc,
            std_trade_count=_std(tc_values, mean_tc),
            mean_total_pnl=mean_pnl,
            std_total_pnl=_std(pnl_values, mean_pnl),
            windows_passed=sum(1 for m in per_window if m.passed_go_nogo),
            total_windows=len(per_window),
        )

    windows_passed = sum(1 for m in per_window if m.passed_go_nogo)
    total = len(per_window)
    go_nogo = total >= 3 and windows_passed >= 3

    go_nogo_result = evaluate_statistical_checks(all_oos_pnls)

    return WalkForwardResults(
        per_window=per_window,
        aggregated=aggregated,
        go_nogo=go_nogo,
        go_nogo_result=go_nogo_result,
    )


def _run_strategy_window(
    strategy: ISignalStrategy,
    test_bars: list[Bar],
    initial_balance: float = 10000.0,
    risk_per_trade_pct: float = 0.005,
) -> list[dict[str, Any]]:
    if len(test_bars) < 10:
        return []

    balance = initial_balance
    open_trade: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []

    for i in range(len(test_bars)):
        bar = test_bars[i]
        accumulated_bars = test_bars[: i + 1]
        state = MarketState(
            bars=accumulated_bars,
            current_session=determine_session(bar.time),
        )

        if open_trade is None and i >= 5:
            signal = strategy.evaluate(state)
            if signal is not None:
                entry_price = signal.entry_price
                sl = signal.stop_loss
                tp = signal.take_profit_1 if signal.take_profit_1 else entry_price
                risk_amount = balance * risk_per_trade_pct

                if signal.direction.value == "long":
                    sl_distance = entry_price - sl
                else:
                    sl_distance = sl - entry_price

                if sl_distance > 0:
                    lot_size = risk_amount / sl_distance
                else:
                    lot_size = 0.0

                open_trade = {
                    "direction": signal.direction.value,
                    "entry_price": entry_price,
                    "sl": sl,
                    "tp": tp,
                    "lot_size": lot_size,
                    "entry_bar_index": i,
                    "pnl": 0.0,
                }

        if open_trade is not None:
            direction = open_trade["direction"]
            entry_price = open_trade["entry_price"]
            sl = open_trade["sl"]
            tp = open_trade["tp"]
            lot_size = open_trade["lot_size"]

            pip_value = 0.0001 if entry_price < 50 else 0.01
            pnl = 0.0
            closed = False

            if direction == "long":
                if bar.low <= sl:
                    pips = (sl - entry_price) / pip_value
                    pnl = pips * lot_size * pip_value * 100000
                    closed = True
                elif bar.high >= tp:
                    pips = (tp - entry_price) / pip_value
                    pnl = pips * lot_size * pip_value * 100000
                    closed = True
            else:
                if bar.high >= sl:
                    pips = (entry_price - sl) / pip_value
                    pnl = pips * lot_size * pip_value * 100000
                    closed = True
                elif bar.low <= tp:
                    pips = (entry_price - tp) / pip_value
                    pnl = pips * lot_size * pip_value * 100000
                    closed = True

            if closed:
                balance = max(0.0, balance + pnl)
                open_trade["pnl"] = pnl
                trades.append({"pnl": pnl})
                open_trade = None

    if open_trade is not None:
        open_trade["pnl"] = 0.0
        trades.append({"pnl": 0.0})

    return trades


def _compute_atr_series(bars: list[Bar], period: int = 14) -> list[float]:
    """Compute rolling ATR series from bars."""
    if len(bars) < 2:
        return []
    true_ranges: list[float] = []
    for i in range(1, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        true_ranges.append(tr)
    if not true_ranges:
        return []
    # Simple rolling average of TR for ATR
    atr_series: list[float] = []
    for i in range(len(true_ranges)):
        start = max(0, i - period + 1)
        window = true_ranges[start : i + 1]
        atr_series.append(sum(window) / len(window))
    return atr_series


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2.0
    return s[n // 2]


def detect_regime_for_window(
    bars: list[Bar],
    btc_bars: list[Bar] | None = None,
) -> dict[str, Any]:
    """Detect regime for a walk-forward window. Returns regime label dict.

    Short-window guard: <14 bars returns all defaults.
    ATR percentile guard: <30 data points uses median instead of percentile.

    If ``btc_bars`` is provided, also computes the BTC macro regime overlay
    via :class:`~quant.btc_regime_overlay.BtcRegimeOverlay`.
    """
    from quant.regime import (  # noqa: I001
        SessionName,
        TrendDirection,
        combined_regime,
        session_regime,
        trend_regime,
        volatility_regime,
    )

    default = {
        "regime_volatility": "unknown",
        "regime_trend": "unknown",
        "regime_session": "unknown",
        "regime_combined": "unknown",
        "regime_quality": 0.0,
        "btc_regime": "unknown",
    }

    # Short-window guard (council amendment K-3)
    if len(bars) < 14:
        return default

    # Compute ATR series for volatility regime
    atr_series = _compute_atr_series(bars)

    # ATR percentile guard for small samples (council amendment L-2)
    if len(atr_series) < 30:
        # Use median-based classification instead of percentile
        if atr_series:
            current_atr = atr_series[-1]
            med = _median(atr_series)
            if med > 0:
                ratio = current_atr / med
                if ratio < 0.7:
                    vol_label = "low"
                elif ratio < 1.3:
                    vol_label = "normal"
                elif ratio < 1.8:
                    vol_label = "high"
                else:
                    vol_label = "extreme"
            else:
                vol_label = "normal"
        else:
            vol_label = "unknown"
        vol_result = None  # skip combined_regime volatility component
    else:
        vol_result = volatility_regime(atr_series)
        vol_label = vol_result.regime.value

    # Trend regime
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]
    trend_result = trend_regime(highs, lows, closes)

    # Map trend direction to required labels
    _trend_map = {
        TrendDirection.TRENDING: "trending",
        TrendDirection.RANGING: "ranging",
        TrendDirection.NEUTRAL: "neutral",
    }
    # Determine up/down from MA slope
    if trend_result.direction == TrendDirection.TRENDING:
        trend_label = "trending_up" if trend_result.ma_slope > 0 else "trending_down"
    else:
        trend_label = _trend_map.get(trend_result.direction, "unknown")

    # Session regime (use last bar's time)
    last_bar = bars[-1]
    hour = last_bar.time.hour
    day_of_week = last_bar.time.weekday()
    sess_result = session_regime(hour, day_of_week)

    _session_map = {
        SessionName.ASIA: "asian",
        SessionName.LONDON: "london",
        SessionName.NEW_YORK: "new_york",
        SessionName.CLOSE: "off_hours",
    }
    session_label = _session_map.get(sess_result.session, "unknown")

    # Combined regime with confidence
    if vol_result is not None:
        combined = combined_regime(vol_result, trend_result, sess_result)
        quality = combined.confidence
        combined_label = f"{vol_label}_{trend_label}_{session_label}"
    else:
        # Small sample: estimate quality conservatively
        quality = 0.3
        combined_label = f"{vol_label}_{trend_label}_{session_label}"

    # BTC macro regime overlay (BQ-508)
    btc_regime_label = "unknown"
    if btc_bars is not None:
        try:
            from quant.btc_regime_overlay import BtcRegimeOverlay

            overlay = BtcRegimeOverlay()
            btc_regime_label = overlay.regime_for_bars(btc_bars)
        except Exception:
            btc_regime_label = "neutral"

    return {
        "regime_volatility": vol_label,
        "regime_trend": trend_label,
        "regime_session": session_label,
        "regime_combined": combined_label,
        "regime_quality": quality,
        "btc_regime": btc_regime_label,
    }


def go_nogo_criteria(results: WalkForwardResults) -> bool:
    if len(results.per_window) < 3:
        return False
    passed = sum(1 for m in results.per_window if m.passed_go_nogo)
    return passed >= 2


def comparison_report(
    results_a: WalkForwardResults,
    results_b: WalkForwardResults,
) -> str:
    def fmt(value: float, precision: int = 4) -> str:
        return f"{value:.{precision}f}"

    def pct(value: float, precision: int = 2) -> str:
        return f"{value * 100:.{precision}f}%"

    lines: list[str] = []
    lines.append("=" * 80)
    lines.append("WALK-FORWARD COMPARISON REPORT")
    lines.append("=" * 80)
    lines.append("")

    agg_a = results_a.aggregated
    agg_b = results_b.aggregated

    if agg_a is None and agg_b is None:
        lines.append("No results to compare.")
        return "\n".join(lines)

    lines.append(f"{'Metric':<25} {'Strategy A':>18} {'Strategy B':>18} {'Delta':>14}")
    lines.append("-" * 80)

    metrics_to_compare = [
        ("Win Rate", "mean_win_rate", True),
        ("Profit Factor", "mean_profit_factor", False),
        ("Max Drawdown", "mean_max_drawdown", True),
        ("Sharpe Ratio", "mean_sharpe_ratio", False),
        ("Trade Count", "mean_trade_count", False),
        ("Total PnL", "mean_total_pnl", False),
    ]

    for label, attr_name, is_pct in metrics_to_compare:
        val_a = getattr(agg_a, attr_name, 0.0) if agg_a else 0.0
        val_b = getattr(agg_b, attr_name, 0.0) if agg_b else 0.0
        delta = val_a - val_b

        str_a = pct(val_a) if is_pct else fmt(val_a)
        str_b = pct(val_b) if is_pct else fmt(val_b)
        str_delta = pct(delta) if is_pct else fmt(delta)

        lines.append(f"{label:<25} {str_a:>18} {str_b:>18} {str_delta:>14}")

    lines.append("-" * 80)
    lines.append("")

    passed_a = sum(1 for m in results_a.per_window if m.passed_go_nogo)
    passed_b = sum(1 for m in results_b.per_window if m.passed_go_nogo)
    total_a = len(results_a.per_window)
    total_b = len(results_b.per_window)

    lines.append(
        f"Strategy A: {passed_a}/{total_a} windows passed GO/NO-GO  -> {'GO' if results_a.go_nogo else 'NO-GO'}"
    )
    lines.append(
        f"Strategy B: {passed_b}/{total_b} windows passed GO/NO-GO  -> {'GO' if results_b.go_nogo else 'NO-GO'}"
    )
    lines.append("")

    for label, results in [("Strategy A", results_a), ("Strategy B", results_b)]:
        stat = results.go_nogo_result
        if stat is not None:
            lines.append(f"{label} Statistical Validation:")
            lines.append(f"  Decision: {stat.decision.value}")
            if stat.p_value is not None:
                lines.append(f"  P-value: {stat.p_value:.4f}")
            lines.append(f"  Total OOS trades: {stat.total_oos_trades}")
            lines.append(f"  Min trades met: {stat.min_trades_met}")
            lines.append(f"  Significance met: {stat.significance_met}")
            if stat.full_bt_consistent is not None:
                lines.append(f"  Full BT consistent: {stat.full_bt_consistent}")
            if stat.multi_pair_status is not None:
                lines.append(f"  Multi-pair status: {stat.multi_pair_status}")
            lines.append("")

    if agg_a:
        lines.append("Strategy A Per-Window Details:")
        lines.append("-" * 80)
        lines.append(
            f"{'Window':<8} {'WR':>8} {'PF':>8} {'MaxDD':>10} {'Sharpe':>10} {'Trades':>8} {'PnL':>12} {'GO?':>6}"
        )
        for m in results_a.per_window:
            lines.append(
                f"{m.window_index:<8} {pct(m.win_rate):>8} {fmt(m.profit_factor):>8} "
                f"{pct(m.max_drawdown):>10} {fmt(m.sharpe_ratio):>10} {m.trade_count:>8} "
                f"{fmt(m.total_pnl):>12} {'YES' if m.passed_go_nogo else 'NO':>6}"
            )
        lines.append("")

    if agg_b:
        lines.append("Strategy B Per-Window Details:")
        lines.append("-" * 80)
        lines.append(
            f"{'Window':<8} {'WR':>8} {'PF':>8} {'MaxDD':>10} {'Sharpe':>10} {'Trades':>8} {'PnL':>12} {'GO?':>6}"
        )
        for m in results_b.per_window:
            lines.append(
                f"{m.window_index:<8} {pct(m.win_rate):>8} {fmt(m.profit_factor):>8} "
                f"{pct(m.max_drawdown):>10} {fmt(m.sharpe_ratio):>10} {m.trade_count:>8} "
                f"{fmt(m.total_pnl):>12} {'YES' if m.passed_go_nogo else 'NO':>6}"
            )
        lines.append("")

    lines.append("=" * 80)
    return "\n".join(lines)
