import csv
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from .models import Order, Position

try:  # Discord notifier is optional; degrade silently if missing.
    from .discord_notifier import DiscordNotifier, TradeOpenedEvent
except ImportError:  # pragma: no cover - import-time defensive guard
    DiscordNotifier = None  # type: ignore[assignment]
    TradeOpenedEvent = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    trade_id: str
    timestamp: str
    symbol: str
    direction: str
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    exit_price: Optional[float]
    closed_pnl: float
    status: str
    comment: str
    strategy_id: str = ""


class TradeLogger:
    def __init__(
        self,
        log_dir: str = "logs/trades",
        strategy_name: str = "unknown",
        notifier: Optional["DiscordNotifier"] = None,
    ):
        self._log_dir = log_dir
        self._strategy_name = strategy_name
        self._records: list[TradeRecord] = []
        # Notifier is auto-instantiated from env when DISCORD_WEBHOOK is set,
        # unless an explicit one is passed in (used by tests + future wiring).
        if notifier is not None:
            self._notifier: Optional["DiscordNotifier"] = notifier
        elif DiscordNotifier is not None:
            try:
                self._notifier = DiscordNotifier()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("[TradeLogger] notifier init failed: %s", e)
                self._notifier = None
        else:
            self._notifier = None
        os.makedirs(log_dir, exist_ok=True)

    def log_trade_opened(self, order: Order, position: Optional[Position] = None, strategy_id: str = ""):
        timestamp = datetime.now(timezone.utc).isoformat()
        record = TradeRecord(
            trade_id=position.position_id if position else order.order_id,
            timestamp=timestamp,
            symbol=order.symbol,
            direction=order.direction.value,
            volume=order.volume,
            entry_price=order.filled_price or order.price or 0,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            exit_price=None,
            closed_pnl=0.0,
            status="open",
            comment=order.comment,
            strategy_id=strategy_id,
        )
        self._records.append(record)
        self._flush_record(record)
        logger.info(
            f"[TRADE LOG] OPENED {record.direction} {record.volume} {record.symbol} "
            f"@ {record.entry_price} SL={record.stop_loss} TP={record.take_profit}"
        )
        # Discord notification is fire-and-forget; never raises.
        self._notify_trade_opened(record, order)

    def _notify_trade_opened(self, record: TradeRecord, order: Order) -> None:
        if self._notifier is None or not self._notifier.enabled:
            return
        try:
            self._notifier.notify_trade_opened(
                TradeOpenedEvent(
                    symbol=record.symbol,
                    direction=record.direction,
                    volume=record.volume,
                    entry_price=record.entry_price,
                    stop_loss=record.stop_loss,
                    take_profit=record.take_profit,
                    strategy_id=record.strategy_id,
                    timestamp=record.timestamp,
                    timeframe=getattr(order, "timeframe", "") or "",
                    comment=record.comment,
                )
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[TradeLogger] notifier raised unexpectedly: %s", e)

    def log_position_closed(self, position: Position, strategy_id: str = ""):
        record = TradeRecord(
            trade_id=position.position_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=position.symbol,
            direction=position.direction.value,
            volume=position.volume,
            entry_price=position.entry_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            exit_price=position.closed_price,
            closed_pnl=position.closed_pnl,
            status="closed",
            comment=position.comment,
            strategy_id=strategy_id,
        )
        self._records.append(record)
        self._flush_record(record)
        logger.info(f"[TRADE LOG] CLOSED {record.direction} {record.symbol} PnL={record.closed_pnl:.2f}")

    def get_summary(self) -> dict:
        closed = [r for r in self._records if r.status == "closed"]
        wins = [r for r in closed if r.closed_pnl > 0]
        losses = [r for r in closed if r.closed_pnl <= 0]
        total_pnl = sum(r.closed_pnl for r in closed)
        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else 0.0,
            "total_pnl": total_pnl,
            "avg_win": sum(r.closed_pnl for r in wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(r.closed_pnl for r in losses) / len(losses) if losses else 0.0,
            "open_positions": sum(1 for r in self._records if r.status == "open"),
        }

    def _flush_record(self, record: TradeRecord):
        filename = self._get_filename()
        file_exists = os.path.exists(filename)
        with open(filename, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[k for k in asdict(record)])
            if not file_exists:
                writer.writeheader()
            writer.writerow(asdict(record))

    def _get_filename(self) -> str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self._log_dir, f"{self._strategy_name}_{date_str}.csv")
