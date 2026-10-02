from __future__ import annotations

import csv
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from .models import Order, Position

logger = logging.getLogger(__name__)


@dataclass
class JournalEntry:
    trade_id: str
    timestamp: str
    strategy_id: str
    strategy_type: str
    symbol: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    exit_price: Optional[float]
    closed_pnl: float
    status: str
    signal_confidence: float
    signal_rationale: str
    duration_seconds: float = 0.0


class TradeJournal:
    def __init__(self, log_dir: str = "logs/forward_test"):
        self._log_dir = log_dir
        self._entries: list[JournalEntry] = []
        self._open_trades: dict[str, dict] = {}
        os.makedirs(log_dir, exist_ok=True)

    def log_open(
        self,
        strategy_id: str,
        strategy_type: str,
        signal_confidence: float,
        signal_rationale: str,
        order: Order,
        position: Optional[Position] = None,
    ):
        entry = JournalEntry(
            trade_id=position.position_id if position else order.order_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy_id=strategy_id,
            strategy_type=strategy_type,
            symbol=order.symbol,
            direction=order.direction.value,
            volume=order.volume,
            entry_price=order.filled_price or order.price or 0,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            exit_price=None,
            closed_pnl=0.0,
            status="open",
            signal_confidence=signal_confidence,
            signal_rationale=signal_rationale,
        )
        self._entries.append(entry)
        self._open_trades[entry.trade_id] = {
            "entry": entry,
            "opened_at": datetime.now(timezone.utc),
        }
        self._flush_entry(entry)
        self._flush_strategy_entry(entry)
        logger.info(
            "[JOURNAL] OPENED %s %s %s vol=%.2f @ %.5f conf=%.2f",
            entry.strategy_id,
            entry.direction,
            entry.symbol,
            entry.volume,
            entry.entry_price,
            entry.signal_confidence,
        )

    def log_close(self, position: Position, strategy_id: str = "", strategy_type: str = ""):
        trade_id = position.position_id
        open_info = self._open_trades.pop(trade_id, None)

        duration = 0.0
        confidence = 0.0
        rationale = ""
        strat_id = strategy_id
        strat_type = strategy_type

        if open_info:
            opened_at = open_info["opened_at"]
            duration = (datetime.now(timezone.utc) - opened_at).total_seconds()
            confidence = open_info["entry"].signal_confidence
            rationale = open_info["entry"].signal_rationale
            strat_id = strat_id or open_info["entry"].strategy_id
            strat_type = strat_type or open_info["entry"].strategy_type

        self._entries = [e for e in self._entries if not (e.trade_id == trade_id and e.status == "open")]

        entry = JournalEntry(
            trade_id=trade_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy_id=strat_id,
            strategy_type=strat_type,
            symbol=position.symbol,
            direction=position.direction.value,
            volume=position.volume,
            entry_price=position.entry_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            exit_price=position.closed_price,
            closed_pnl=position.closed_pnl,
            status="closed",
            signal_confidence=confidence,
            signal_rationale=rationale,
            duration_seconds=duration,
        )
        self._entries.append(entry)
        self._flush_entry(entry)
        self._flush_strategy_entry(entry)
        logger.info(
            "[JOURNAL] CLOSED %s %s pnl=%.2f duration=%.0fs",
            entry.strategy_id,
            entry.symbol,
            entry.closed_pnl,
            entry.duration_seconds,
        )

    def get_strategy_summary(self, strategy_id: str) -> dict:
        closed = [e for e in self._entries if e.strategy_id == strategy_id and e.status == "closed"]
        if not closed:
            return {"strategy_id": strategy_id, "total_trades": 0}
        wins = [e for e in closed if e.closed_pnl > 0]
        losses = [e for e in closed if e.closed_pnl <= 0]
        return {
            "strategy_id": strategy_id,
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed),
            "total_pnl": sum(e.closed_pnl for e in closed),
            "avg_confidence": sum(e.signal_confidence for e in closed) / len(closed),
            "avg_duration_sec": (sum(e.duration_seconds for e in closed) / len(closed)),
        }

    def get_portfolio_summary(self) -> dict:
        closed = [e for e in self._entries if e.status == "closed"]
        wins = [e for e in closed if e.closed_pnl > 0]
        losses = [e for e in closed if e.closed_pnl <= 0]
        open_count = sum(1 for e in self._entries if e.status == "open")

        strategy_ids = set(e.strategy_id for e in self._entries if e.strategy_id)
        per_strategy = {sid: self.get_strategy_summary(sid) for sid in strategy_ids}

        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "total_pnl": sum(e.closed_pnl for e in closed),
            "open_positions": open_count,
            "per_strategy": per_strategy,
        }

    def export_csv(self, path: str):
        closed = [e for e in self._entries if e.status == "closed"]
        if not closed:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fieldnames = [k for k in asdict(closed[0])]
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for entry in closed:
                writer.writerow(asdict(entry))
        logger.info("Exported %d journal entries to %s", len(closed), path)

    def _flush_entry(self, entry: JournalEntry):
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = os.path.join(self._log_dir, f"journal_{date_str}.csv")
        self._append_csv(filename, entry)

    def _flush_strategy_entry(self, entry: JournalEntry):
        if not entry.strategy_id:
            return
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = os.path.join(self._log_dir, f"strategy_{entry.strategy_id}_{date_str}.csv")
        self._append_csv(filename, entry)

    @staticmethod
    def _append_csv(filename: str, entry: JournalEntry):
        file_exists = os.path.exists(filename)
        fieldnames = [k for k in asdict(entry)]
        with open(filename, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(asdict(entry))
