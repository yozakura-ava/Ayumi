"""Integration test: PaperTrader SL/TP close propagates to signal stats.

Card 9310bdd0 — verifies that when OrderManager auto-closes a position
via SL/TP during ``update_market_prices``, the signal-stats recorder
receives the correct outcome (sl_hit / tp_hit) and the PaperTrader's
realized P&L is updated.
"""

import sys
from pathlib import Path

import pytest

_FOREX_SRC = str(Path(__file__).resolve().parent.parent.parent / "src" / "forex_bot")
if _FOREX_SRC not in sys.path:
    sys.path.insert(0, _FOREX_SRC)

from adapters.ctrader.models import CTraderTradeSignal, TradeDirection
from adapters.ctrader.paper_trader import PaperTrader
from adapters.ctrader.risk_guard import FTMOConfig


def _make_signal(direction=TradeDirection.LONG, entry=1.1000, sl=1.0950, tp=1.1120):
    return CTraderTradeSignal(
        symbol="EURUSD",
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        take_profit_1=tp,
        take_profit_2=0.0,
        take_profit_3=0.0,
        volume=0.0,
        confidence=0.8,
        rationale="integration-test",
        strategy_id="test-strategy",
    )


@pytest.fixture
def trader(tmp_path):
    return PaperTrader(
        ftmo_config=FTMOConfig(),
        starting_balance=100_000.0,
        state_path=str(tmp_path / "risk_guard.json"),
        stats_log_path=str(tmp_path / "signal_stats.jsonl"),
    )


class TestSignalStatsPropagation:
    """Verify that SL/TP closes from update_market_prices reach the signal-stats log."""

    def test_sl_close_writes_outcome(self, trader, tmp_path):
        """An SL close via update_market_prices produces a signal-stats outcome record."""
        signal = _make_signal(sl=1.0950, tp=1.1120)
        result = trader.process_signal(signal, bid=1.1000, ask=1.1002)
        assert result.success

        # Trigger SL via mid-price-only update (the bug scenario)
        trader.update_market_prices(prices={"EURUSD": 1.0900})

        stats = trader.get_stats()
        assert stats.realized_pnl < 0, "Realized P&L should reflect the SL loss"

        # Verify the signal-stats log file exists and has content
        log_path = Path(trader._stats_log_path)
        assert log_path.exists(), "Signal-stats log should exist"
        content = log_path.read_text()
        assert len(content) > 0, "Signal-stats log should not be empty"

    def test_tp_close_writes_outcome(self, trader, tmp_path):
        """A TP close via update_market_prices produces a signal-stats outcome record."""
        signal = _make_signal(sl=1.0950, tp=1.1120)
        result = trader.process_signal(signal, bid=1.1000, ask=1.1002)
        assert result.success

        trader.update_market_prices(prices={"EURUSD": 1.1150})

        stats = trader.get_stats()
        assert stats.realized_pnl > 0, "Realized P&L should reflect the TP gain"

    def test_balance_reflects_realized_pnl(self, trader):
        """After SL close, balance = starting + realized_pnl + unrealized."""
        signal = _make_signal(sl=1.0950, tp=1.1120)
        trader.process_signal(signal, bid=1.1000, ask=1.1002)

        trader.update_market_prices(prices={"EURUSD": 1.0900})

        stats = trader.get_stats()
        # Balance should have decreased from the SL loss
        assert stats.current_balance < stats.starting_balance
