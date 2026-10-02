"""Confidence gate tuner — finds optimal thresholds from historical trade data."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass

# Default gate values when data is insufficient
DEFAULT_MAX_SPREADS = {
    "EURUSD": 2.0,
    "GBPUSD": 2.5,
    "USDJPY": 2.0,
    "XAUUSD": 4.0,
}
DEFAULT_SESSION_HOURS = list(range(7, 22))  # London+NY
DEFAULT_ATR_RANGE = (0.0, 5.0)
MIN_TRADES_PER_SYMBOL = 20


@dataclass
class GateTuneResult:
    """Result of gate tuning."""

    spread_gates: dict[str, float]
    session_gates: dict[str, list[int]]
    volatility_gates: dict[str, tuple[float, float]]
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class GateTuner:
    """Tunes confidence gate thresholds using historical trade data."""

    def __init__(self, trade_log_path: str = "logs/trades.jsonl"):
        self._trade_log = trade_log_path

    def _load_trades(self) -> list[dict]:
        if not os.path.exists(self._trade_log):
            return []
        trades = []
        import json

        with open(self._trade_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return trades

    def tune_spread_gates(self) -> dict[str, float]:
        """Find per-symbol max spread where win rate > 50%."""
        trades = self._load_trades()
        if not trades:
            return dict(DEFAULT_MAX_SPREADS)

        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            sym = t.get("symbol", "")
            if sym:
                by_symbol[sym].append(t)

        result = {}
        for sym, sym_trades in by_symbol.items():
            if len(sym_trades) < MIN_TRADES_PER_SYMBOL:
                result[sym] = DEFAULT_MAX_SPREADS.get(sym, 2.0)
                continue

            # Sort by spread, compute cumulative win rate
            sorted_trades = sorted(sym_trades, key=lambda t: t.get("spread", 0))
            wins = 0
            max_spread = DEFAULT_MAX_SPREADS.get(sym, 2.0)
            for i, t in enumerate(sorted_trades):
                if t.get("pnl", 0) > 0:
                    wins += 1
                wr = wins / (i + 1)
                if wr >= 0.50:
                    max_spread = t.get("spread", 0)
                else:
                    break
            result[sym] = max_spread
        return result

    def tune_session_gates(self) -> dict[str, list[int]]:
        """Find winning session hours per symbol (UTC)."""
        trades = self._load_trades()
        if not trades:
            return {"default": DEFAULT_SESSION_HOURS}

        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            sym = t.get("symbol", "")
            if sym:
                by_symbol[sym].append(t)

        result = {}
        for sym, sym_trades in by_symbol.items():
            if len(sym_trades) < MIN_TRADES_PER_SYMBOL:
                result[sym] = DEFAULT_SESSION_HOURS
                continue

            hour_stats: dict[int, dict] = defaultdict(lambda: {"wins": 0, "total": 0})
            for t in sym_trades:
                ts = t.get("timestamp", "")
                if not ts:
                    continue
                try:
                    hour = int(ts.split("T")[1][:2])
                except (IndexError, ValueError):
                    continue
                hour_stats[hour]["total"] += 1
                if t.get("pnl", 0) > 0:
                    hour_stats[hour]["wins"] += 1

            avg_wr = sum(s["wins"] for s in hour_stats.values()) / max(sum(s["total"] for s in hour_stats.values()), 1)
            good_hours = [h for h, s in hour_stats.items() if s["total"] >= 3 and (s["wins"] / s["total"]) >= avg_wr]
            result[sym] = sorted(good_hours) if good_hours else DEFAULT_SESSION_HOURS
        return result

    def tune_volatility_gates(self) -> dict[str, tuple[float, float]]:
        """Find profitable ATR ranges per symbol."""
        trades = self._load_trades()
        if not trades:
            return {"default": DEFAULT_ATR_RANGE}

        by_symbol: dict[str, list[dict]] = defaultdict(list)
        for t in trades:
            sym = t.get("symbol", "")
            if sym:
                by_symbol[sym].append(t)

        result = {}
        for sym, sym_trades in by_symbol.items():
            if len(sym_trades) < MIN_TRADES_PER_SYMBOL:
                result[sym] = DEFAULT_ATR_RANGE
                continue

            winning_atrs = [t.get("atr", 0) for t in sym_trades if t.get("pnl", 0) > 0 and t.get("atr")]
            if len(winning_atrs) < 5:
                result[sym] = DEFAULT_ATR_RANGE
                continue

            winning_atrs.sort()
            # Use 10th-90th percentile of winning trades' ATR
            lo = winning_atrs[max(0, len(winning_atrs) // 10)]
            hi = winning_atrs[min(len(winning_atrs) - 1, 9 * len(winning_atrs) // 10)]
            result[sym] = (round(lo, 4), round(hi, 4))
        return result

    def get_recommendations(self) -> GateTuneResult:
        """Run all tuning and return recommendations."""
        warnings = []
        trades = self._load_trades()
        if not trades:
            warnings.append("No trade data available — returning all defaults")

        from collections import Counter

        symbols = Counter(t.get("symbol", "") for t in trades if t.get("symbol"))
        for sym, count in symbols.items():
            if count < MIN_TRADES_PER_SYMBOL:
                warnings.append(f"{sym}: only {count} trades (need {MIN_TRADES_PER_SYMBOL}), using defaults")

        return GateTuneResult(
            spread_gates=self.tune_spread_gates(),
            session_gates=self.tune_session_gates(),
            volatility_gates=self.tune_volatility_gates(),
            warnings=warnings,
        )
