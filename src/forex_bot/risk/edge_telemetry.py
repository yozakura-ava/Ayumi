"""Edge telemetry — rolling R-multiple expectancy per strategy × symbol.

Tracks the actual edge (expectancy) of each strategy × symbol combination
based on closed trade outcomes. Used by the risk allocator to size positions
proportionally to demonstrated edge rather than uniform risk.

R-multiple = realized_pnl / risk_amount
Expectancy = average(R-multiples) over rolling window

Usage:
    from risk.edge_telemetry import EdgeTelemetryTracker

    tracker = EdgeTelemetryTracker()
    tracker.record_close(strategy_id="srmr_xauusd", symbol="XAUUSD",
                          risk_amount=50.0, pnl=35.0)
    expectancy = tracker.get_expectancy("srmr_xauusd", "XAUUSD")
    # → 0.70 (avg R-multiple)

    # Use for sizing:
    if expectancy > 0.5:
        risk_per_trade_pct = 0.007  # High edge → larger size
    elif expectancy > 0:
        risk_per_trade_pct = 0.005  # Positive edge → standard size
    else:
        risk_per_trade_pct = 0.003  # Negative edge → reduced size
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Single closed trade record for edge tracking."""

    strategy_id: str
    symbol: str
    risk_amount: float
    pnl: float
    r_multiple: float
    timestamp: str  # ISO-8601
    signal_id: Optional[str] = None

    @property
    def win(self) -> bool:
        return self.pnl > 0


@dataclass
class EdgeStats:
    """Rolling edge statistics for a strategy × symbol pair."""

    strategy_id: str
    symbol: str
    trades: deque = field(default_factory=lambda: deque(maxlen=50))
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_risk: float = 0.0
    best_r: float = 0.0
    worst_r: float = 0.0

    @property
    def expectancy(self) -> float:
        """Average R-multiple over rolling window."""
        if not self.trades:
            return 0.0
        return sum(t.r_multiple for t in self.trades) / len(self.trades)

    @property
    def win_rate(self) -> float:
        """Win rate over rolling window."""
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.win) / len(self.trades)

    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss over rolling window."""
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def avg_risk(self) -> float:
        """Average risk amount per trade."""
        if not self.trades:
            return 0.0
        return sum(t.risk_amount for t in self.trades) / len(self.trades)

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "total_trades": self.total_trades,
            "rolling_trades": len(self.trades),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "expectancy": round(self.expectancy, 4),
            "profit_factor": round(self.profit_factor, 4) if self.profit_factor != float("inf") else None,
            "total_pnl": round(self.total_pnl, 2),
            "avg_risk": round(self.avg_risk, 2),
            "best_r": round(self.best_r, 4),
            "worst_r": round(self.worst_r, 4),
        }


class EdgeTelemetryTracker:
    """Tracks rolling R-multiple expectancy per strategy × symbol.

    Thread-safe. Persists to JSONL on each record_close() call.
    """

    def __init__(self, persist_path: str = "data/edge_telemetry.jsonl"):
        self._persist_path = Path(persist_path)
        self._stats: dict[tuple[str, str], EdgeStats] = defaultdict(lambda: EdgeStats("", ""))
        self._lock = threading.RLock()
        self._load_history()

    def _load_history(self) -> None:
        """Load historical trades from JSONL on init."""
        if not self._persist_path.exists():
            return
        try:
            with open(self._persist_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        self._apply_record(
                            rec["strategy_id"],
                            rec["symbol"],
                            rec["risk_amount"],
                            rec["pnl"],
                            rec.get("signal_id"),
                            rec.get("timestamp"),
                        )
                    except (json.JSONDecodeError, KeyError):
                        continue
            logger.info(
                "EdgeTelemetry: loaded %d trades from %s",
                sum(s.total_trades for s in self._stats.values()),
                self._persist_path,
            )
        except Exception as exc:
            logger.warning("EdgeTelemetry: failed to load history: %s", exc)

    def _apply_record(
        self,
        strategy_id: str,
        symbol: str,
        risk_amount: float,
        pnl: float,
        signal_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> TradeRecord:
        """Apply a trade record to internal stats (no persistence)."""
        key = (strategy_id, symbol)
        if key not in self._stats:
            self._stats[key] = EdgeStats(strategy_id, symbol)

        stats = self._stats[key]
        r_multiple = pnl / risk_amount if risk_amount > 0 else 0.0
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        record = TradeRecord(
            strategy_id=strategy_id,
            symbol=symbol,
            risk_amount=risk_amount,
            pnl=pnl,
            r_multiple=r_multiple,
            timestamp=ts,
            signal_id=signal_id,
        )

        stats.trades.append(record)
        stats.total_trades += 1
        stats.total_pnl += pnl
        stats.total_risk += risk_amount
        if record.win:
            stats.wins += 1
        else:
            stats.losses += 1
        if r_multiple > stats.best_r:
            stats.best_r = r_multiple
        if r_multiple < stats.worst_r:
            stats.worst_r = r_multiple

        return record

    def record_close(
        self,
        strategy_id: str,
        symbol: str,
        risk_amount: float,
        pnl: float,
        signal_id: Optional[str] = None,
    ) -> TradeRecord:
        """Record a closed trade and persist to JSONL.

        Args:
            strategy_id: Strategy identifier (e.g. "srmr_xauusd")
            symbol: Trading symbol (e.g. "XAUUSD")
            risk_amount: Risk amount in account currency ($)
            pnl: Realized profit/loss in account currency ($)
            signal_id: Optional signal identifier for traceability

        Returns:
            TradeRecord for the closed trade
        """
        with self._lock:
            record = self._apply_record(strategy_id, symbol, risk_amount, pnl, signal_id)

            # Persist to JSONL
            try:
                self._persist_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._persist_path, "a") as f:
                    f.write(json.dumps(asdict(record)) + "\n")
            except Exception as exc:
                logger.warning("EdgeTelemetry: failed to persist: %s", exc)

            stats = self._stats[(strategy_id, symbol)]
            logger.info(
                "EdgeTelemetry: %s %s R=%.2f pnl=$%.2f expectancy=%.3f (%d trades)",
                strategy_id,
                symbol,
                record.r_multiple,
                pnl,
                stats.expectancy,
                stats.total_trades,
            )

            return record

    def get_expectancy(self, strategy_id: str, symbol: str) -> float:
        """Get rolling R-multiple expectancy for a strategy × symbol."""
        with self._lock:
            key = (strategy_id, symbol)
            stats = self._stats.get(key)
            return stats.expectancy if stats else 0.0

    def get_stats(self, strategy_id: str, symbol: str) -> Optional[EdgeStats]:
        """Get full edge stats for a strategy × symbol."""
        with self._lock:
            key = (strategy_id, symbol)
            return self._stats.get(key)

    def get_all_stats(self) -> list[dict]:
        """Get edge stats for all strategy × symbol pairs."""
        with self._lock:
            return [s.to_dict() for s in self._stats.values() if s.total_trades > 0]

    def get_risk_multiplier(self, strategy_id: str, symbol: str) -> float:
        """Get a risk sizing multiplier based on demonstrated edge.

        Returns:
            1.5 for high edge (expectancy > 0.5R)
            1.0 for positive edge (expectancy > 0)
            0.6 for negative edge (expectancy < 0)
            1.0 for insufficient data (< 5 trades)
        """
        with self._lock:
            key = (strategy_id, symbol)
            stats = self._stats.get(key)
            if not stats or len(stats.trades) < 5:
                return 1.0  # Insufficient data — standard size

            exp = stats.expectancy
            if exp > 0.5:
                return 1.5
            elif exp > 0:
                return 1.0
            else:
                return 0.6

    def write_state_snapshot(self, snapshot_path: Optional[str] = None) -> dict:
        """Atomically write current stats state to a JSON snapshot file.

        Card d8c2a10b: the canonical observability export (data/edge_telemetry_state.json)
        was drifting 47h+ from the operational path because the trade-by-trade JSONL
        only writes on record_close(). With the 75+ day zero-signal dry spell,
        record_close() rarely fires, so scorecards/evals read stale data.

        This method snapshots the CURRENT in-memory stats (rolling expectancy,
        win rate, profit factor, etc.) on a periodic cadence (matched to the
        launcher's 5-min operational loop) so external readers always see
        fresh state, even between trade closes.

        Snapshot file is rewritten atomically (tmp + rename) — readers can
        safely load it without locking concerns.

        Args:
            snapshot_path: Output path. Defaults to "data/edge_telemetry_state.json".

        Returns:
            The snapshot dict that was written (also useful for tests).
        """
        if snapshot_path is None:
            snapshot_path = "data/edge_telemetry_state.json"
        out_path = Path(snapshot_path)

        with self._lock:
            stats_list = [
                s.to_dict()
                for s in self._stats.values()
                if s.total_trades > 0
            ]
            snapshot = {
                "kind": "edge_telemetry_state_snapshot",
                "schema_version": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_strategy_symbol_pairs": len(stats_list),
                "total_trades": sum(s["total_trades"] for s in stats_list),
                "stats": stats_list,
            }

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, indent=2, default=str)
            os.replace(str(tmp_path), str(out_path))
            logger.debug(
                "EdgeTelemetry: state snapshot written (%d pairs, %d trades)",
                snapshot["total_strategy_symbol_pairs"],
                snapshot["total_trades"],
            )
        except Exception as exc:
            logger.warning("EdgeTelemetry: failed to write state snapshot: %s", exc)

        return snapshot
