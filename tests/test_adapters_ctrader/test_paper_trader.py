"""Unit tests for PaperTrader SL/TP enforcement.

Tests that paper-mode positions close correctly at SL/TP levels, including
the side-selection / no-quote-sentinel contract enforced by card 9f051898.

History of the SL/TP check contract:
  * Pre-9310bdd0 — ``_check_stop_loss_hit`` had a guard ``if bid <= 0 and
    ask <= 0: return False`` that silently skipped SL evaluation when only
    a mid price was supplied. Positions stayed open indefinitely — the
    $25 → $113K loss bug.
  * Card 9310bdd0 — Added a ``current_price`` fallback so SL would still
    trigger when bid/ask were missing, using the bar mid as the trigger
    source. Closed the regression above but introduced false triggers:
    a bar mid could dip past the SL even when the actual exit-side
    quote was unavailable.
  * Card 9f051898 (bid/ask side-selection fix) — Replaced the
    current_price fallback for SL with a "no quote" sentinel: a zero/
    missing bid (LONG) or ask (SHORT) causes ``_check_stop_loss_hit`` to
    return ``False`` without evaluating against current_price. This
    matches what a real broker would do — without an exit-side quote
    you cannot conclude the SL was actually crossed.

TP evaluation (``_check_take_profit_hit``) continues to use the card
9310bdd0 current_price fallback; only SL is sentinel-gated.
"""

import sys
from pathlib import Path

import pytest

# Ensure src/forex_bot is importable
_FOREX_SRC = str(Path(__file__).resolve().parent.parent.parent / "src" / "forex_bot")
if _FOREX_SRC not in sys.path:
    sys.path.insert(0, _FOREX_SRC)

from adapters.ctrader.models import (  # noqa: I001
    CTraderTradeSignal,
    Position,
    PositionStatus,
    TradeDirection,
)
from adapters.ctrader.order_manager import (
    OrderManager,
)
from adapters.ctrader.paper_trader import PaperTrader
from adapters.ctrader.risk_guard import FTMOConfig

# ── Helpers ────────────────────────────────────────────────────────────


def _make_signal(
    symbol="EURUSD",
    direction=TradeDirection.LONG,
    entry=1.1000,
    sl=1.0950,
    tp=1.1120,
    confidence=0.8,
) -> CTraderTradeSignal:
    return CTraderTradeSignal(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        take_profit_1=tp,
        take_profit_2=0.0,
        take_profit_3=0.0,
        volume=0.0,
        confidence=confidence,
        rationale="test",
        strategy_id="test-strategy",
    )


def _open_paper_position(
    trader: PaperTrader,
    signal: CTraderTradeSignal | None = None,
) -> Position:
    """Open a paper position and return it."""
    signal = signal or _make_signal()
    result = trader.process_signal(signal, spread=0.0001, bid=signal.entry_price, ask=signal.entry_price)
    assert result.success, f"Signal was rejected: {result.rejection_reason}"
    assert result.position is not None
    return result.position


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def trader(tmp_path):
    """Fresh PaperTrader with no API client (pure paper mode)."""

    # Isolate risk guard state to temp dir
    ftmo = FTMOConfig()
    t = PaperTrader(
        ftmo_config=ftmo,
        starting_balance=100_000.0,
        state_path=str(tmp_path / "risk_guard.json"),
        stats_log_path=str(tmp_path / "signal_stats.jsonl"),
    )
    return t


# ── OrderManager unit tests ────────────────────────────────────────────


class TestStopLossCheck:
    """Direct tests for OrderManager._check_stop_loss_hit."""

    def setup_method(self):
        self.om = OrderManager()

    def test_long_sl_hit_with_bid(self):
        """LONG position: SL triggers when bid <= stop_loss."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.0940,
            stop_loss=1.0950,
        )
        assert self.om._check_stop_loss_hit(pos, 1.0940, bid=1.0940, ask=1.0942)

    def test_long_sl_not_hit(self):
        """LONG position: SL does not trigger when price above SL."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1000,
            stop_loss=1.0950,
        )
        assert not self.om._check_stop_loss_hit(pos, 1.1000, bid=1.1000, ask=1.1002)

    def test_short_sl_hit_with_ask(self):
        """SHORT position: SL triggers when ask >= stop_loss."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1060,
            stop_loss=1.1050,
        )
        assert self.om._check_stop_loss_hit(pos, 1.1060, bid=1.1058, ask=1.1060)

    # ── THE BUG FIX: bid/ask = 0 should NOT skip the check ───────

    def test_long_sl_hit_no_bid_ask(self):
        """LONG position: SL does NOT trigger when bid=0 (no-quote sentinel).

        Card 9f051898 (bid/ask side-selection fix): a zero/missing bid on
        a LONG is treated as a "no quote" sentinel — ``_check_stop_loss_hit``
        returns ``False`` without consulting current_price. This replaces
        the card 9310bdd0 current_price-fallback behaviour, which closed
        positions on a mid-price dip past SL even when the exit-side
        quote was unavailable (false triggers).
        """
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.0940,
            stop_loss=1.0950,
        )
        # bid=0 — no real LONG-side quote, SL eval is skipped (sentinel)
        assert not self.om._check_stop_loss_hit(pos, 1.0940, bid=0, ask=0)

    def test_short_sl_hit_no_bid_ask(self):
        """SHORT position: SL does NOT trigger when ask=0 (no-quote sentinel).

        Card 9f051898: a zero/missing ask on a SHORT is a "no quote"
        sentinel — ``_check_stop_loss_hit`` returns ``False`` without
        consulting current_price. Replaces card 9310bdd0's current_price
        fallback for SL.
        """
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1060,
            stop_loss=1.1050,
        )
        # ask=0 — no real SHORT-side quote, SL eval is skipped (sentinel)
        assert not self.om._check_stop_loss_hit(pos, 1.1060, bid=0, ask=0)

    def test_sl_not_hit_no_bid_ask(self):
        """LONG position: SL does NOT trigger when bid=0 (no-quote sentinel).

        Continues to pass under card 9f051898, but for a different reason
        than before: under the card 9310bdd0 current_price fallback this
        test passed because current_price=1.1000 hadn't crossed
        stop_loss=1.0950. Under card 9f051898 the sentinel short-circuits
        earlier (bid=0 → return False). Both behaviours agree on this
        input.
        """
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1000,
            stop_loss=1.0950,
        )
        assert not self.om._check_stop_loss_hit(pos, 1.1000, bid=0, ask=0)

    def test_sl_none_skips(self):
        """No stop_loss set → never triggers."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.0800,
        )
        pos.stop_loss = None
        assert not self.om._check_stop_loss_hit(pos, 1.0800, bid=0, ask=0)


class TestTakeProfitCheck:
    """Direct tests for OrderManager._check_take_profit_hit."""

    def setup_method(self):
        self.om = OrderManager()

    def test_long_tp_hit_with_ask(self):
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1130,
            take_profit=1.1120,
        )
        assert self.om._check_take_profit_hit(pos, 1.1130, bid=1.1128, ask=1.1130)

    def test_short_tp_hit_with_bid(self):
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.0870,
            take_profit=1.0880,
        )
        assert self.om._check_take_profit_hit(pos, 1.0870, bid=1.0870, ask=1.0872)

    def test_long_tp_hit_no_bid_ask(self):
        """TP triggers using current_price when bid/ask are 0 (card 9310bdd0)."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1130,
            take_profit=1.1120,
        )
        assert self.om._check_take_profit_hit(pos, 1.1130, bid=0, ask=0)

    def test_short_tp_hit_no_bid_ask(self):
        """SHORT TP triggers using current_price when bid/ask are 0."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.0870,
            take_profit=1.0880,
        )
        assert self.om._check_take_profit_hit(pos, 1.0870, bid=0, ask=0)

    def test_tp_not_hit_no_bid_ask(self):
        """TP does not trigger when price hasn't reached TP."""
        pos = Position(
            position_id="test",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            current_price=1.1050,
            take_profit=1.1120,
        )
        assert not self.om._check_take_profit_hit(pos, 1.1050, bid=0, ask=0)


# ── OrderManager.update_position integration ──────────────────────────


class TestUpdatePositionClosesAtSLTP:
    """Test that update_position actually closes positions at SL/TP."""

    def setup_method(self):
        self.om = OrderManager()

    def test_update_position_closes_on_sl(self):
        """update_position closes the position when SL is hit."""
        result = self.om.execute_paper_order(
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            stop_loss=1.0950,
            take_profit=1.1120,
        )
        pid = result.position.position_id

        # Price drops below SL
        self.om.update_position(pid, current_price=1.0940, bid=1.0940, ask=1.0942)

        pos = self.om.get_position(pid)
        assert pos.status.is_closed
        assert pos.closed_pnl < 0
        assert getattr(pos, "close_reason", "") == "sl_hit"

    def test_update_position_closes_on_tp(self):
        """update_position closes the position when TP is hit."""
        result = self.om.execute_paper_order(
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            stop_loss=1.0950,
            take_profit=1.1120,
        )
        pid = result.position.position_id

        # Price rises above TP
        self.om.update_position(pid, current_price=1.1130, bid=1.1128, ask=1.1130)

        pos = self.om.get_position(pid)
        assert pos.status.is_closed
        assert pos.closed_pnl > 0
        assert getattr(pos, "close_reason", "") == "tp_hit"

    def test_update_position_closes_on_sl_no_bid_ask(self):
        """LONG position: SL does NOT trigger when bid=0 (no-quote sentinel).

        Card 9f051898 (bid/ask side-selection fix): a zero/missing bid
        (LONG) is a "no quote" sentinel — ``update_position`` must NOT
        close the position via SL on bid=0. Without a real LONG-side quote
        we cannot conclude the SL level was actually crossed. Replaces the
        card 9310bdd0 current_price-fallback behaviour, which closed
        positions on a mid-price dip past SL even without an exit-side
        quote (false triggers).
        """
        result = self.om.execute_paper_order(
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            stop_loss=1.0950,
            take_profit=1.1120,
        )
        pid = result.position.position_id

        # Only mid price provided, no bid/ask — bid=0 → no-quote sentinel
        self.om.update_position(pid, current_price=1.0940, bid=0, ask=0)

        pos = self.om.get_position(pid)
        # SL eval is skipped — position must stay OPEN
        assert not pos.status.is_closed, (
            "LONG position must stay OPEN: bid=0 is no-quote sentinel "
            "for LONG under card 9f051898"
        )
        assert pos.status == PositionStatus.OPEN

    def test_update_position_closes_on_tp_no_bid_ask(self):
        """update_position closes at TP when only mid price is provided."""
        result = self.om.execute_paper_order(
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.1,
            entry_price=1.1000,
            stop_loss=1.0950,
            take_profit=1.1120,
        )
        pid = result.position.position_id

        self.om.update_position(pid, current_price=1.1130, bid=0, ask=0)

        pos = self.om.get_position(pid)
        assert pos.status.is_closed, "Position should be closed by TP"
        assert pos.closed_price == pytest.approx(1.1120, abs=0.0001)
        assert getattr(pos, "close_reason", "") == "tp_hit"

    def test_update_position_short_closes_on_sl_no_bid_ask(self):
        """SHORT position: SL does NOT trigger when ask=0 (no-quote sentinel).

        Card 9f051898: a zero/missing ask (SHORT) is a "no quote" sentinel —
        ``update_position`` must NOT close the position via SL on ask=0.
        Replaces card 9310bdd0's current_price fallback for SL.
        """
        result = self.om.execute_paper_order(
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            volume=0.1,
            entry_price=1.1000,
            stop_loss=1.1050,
            take_profit=1.0880,
        )
        pid = result.position.position_id

        self.om.update_position(pid, current_price=1.1060, bid=0, ask=0)

        pos = self.om.get_position(pid)
        # SL eval is skipped — position must stay OPEN
        assert not pos.status.is_closed, (
            "SHORT position must stay OPEN: ask=0 is no-quote sentinel "
            "for SHORT under card 9f051898"
        )
        assert pos.status == PositionStatus.OPEN

    def test_update_position_short_closes_on_tp_no_bid_ask(self):
        """SHORT position closes at TP with mid price only."""
        result = self.om.execute_paper_order(
            symbol="EURUSD",
            direction=TradeDirection.SHORT,
            volume=0.1,
            entry_price=1.1000,
            stop_loss=1.1050,
            take_profit=1.0880,
        )
        pid = result.position.position_id

        self.om.update_position(pid, current_price=1.0870, bid=0, ask=0)

        pos = self.om.get_position(pid)
        assert pos.status.is_closed, "Short should be closed by TP"
        assert pos.closed_price == pytest.approx(1.0880, abs=0.0001)


# ── PaperTrader end-to-end tests ──────────────────────────────────────


class TestPaperTraderSLTPEnforcement:
    """End-to-end tests through PaperTrader.update_market_prices."""

    def test_sl_closes_via_update_market_prices_no_bid_ask(self, trader):
        """LONG position stays OPEN when no bids/asks are supplied.

        Card 9f051898 (bid/ask side-selection fix): with no bid/ask
        passed to ``update_market_prices``, the PaperTrader forwards
        bid=0/ask=0 to ``OrderManager._check_stop_loss_hit``, which now
        treats the missing bid (LONG) as a "no quote" sentinel and skips
        SL evaluation. The pre-fix current_price-fallback (card 9310bdd0)
        closed the position on a mid-price dip past SL even without an
        exit-side quote (false triggers).
        """
        signal = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _pos = _open_paper_position(trader, signal)

        # Pass ONLY prices, no bids/asks — bid=0 → no-quote sentinel
        trader.update_market_prices(prices={"EURUSD": 1.0900})

        open_positions = trader.get_open_positions()
        # Position stays OPEN because bid=0 is no-quote sentinel for LONG
        assert len(open_positions) == 1, (
            "Position must stay OPEN: bid=0 is no-quote sentinel for LONG "
            "under card 9f051898"
        )

    def test_tp_closes_via_update_market_prices_no_bid_ask(self, trader):
        """TP fires when update_market_prices is called with only prices."""
        signal = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _pos = _open_paper_position(trader, signal)

        trader.update_market_prices(prices={"EURUSD": 1.1150})

        open_positions = trader.get_open_positions()
        assert len(open_positions) == 0, "Position should have been closed by TP"

    def test_sl_closes_with_bid_ask(self, trader):
        """SL fires normally when bid/ask are provided."""
        signal = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _open_paper_position(trader, signal)

        trader.update_market_prices(
            prices={"EURUSD": 1.0940},
            bids={"EURUSD": 1.0940},
            asks={"EURUSD": 1.0942},
        )

        assert len(trader.get_open_positions()) == 0

    def test_realized_pnl_updated_on_sl_close(self, trader):
        """Realized P&L is updated when SL closes a position internally.

        Card 9f051898 (bid/ask side-selection fix): SL evaluation now
        requires a real exit-side quote. This test supplies explicit
        bid/ask so SL evaluation runs (without them bid=0 would be the
        no-quote sentinel and the position would stay OPEN).
        """
        signal = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _open_paper_position(trader, signal)

        stats_before = trader.get_stats()
        assert stats_before.realized_pnl == 0.0

        # Pass bid/ask so SL eval runs (bid=0 would be no-quote sentinel)
        trader.update_market_prices(
            prices={"EURUSD": 1.0900},
            bids={"EURUSD": 1.0900},
            asks={"EURUSD": 1.0902},
        )

        stats_after = trader.get_stats()
        assert stats_after.realized_pnl < 0, "Realized P&L should be negative after SL hit"

    def test_realized_pnl_updated_on_tp_close(self, trader):
        """Realized P&L is updated when TP closes a position internally."""
        signal = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _open_paper_position(trader, signal)

        trader.update_market_prices(prices={"EURUSD": 1.1150})

        stats_after = trader.get_stats()
        assert stats_after.realized_pnl > 0, "Realized P&L should be positive after TP hit"

    def test_position_stays_open_when_no_sl_tp_hit(self, trader):
        """Position stays open when price is between SL and TP."""
        signal = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _open_paper_position(trader, signal)

        trader.update_market_prices(prices={"EURUSD": 1.1050})

        assert len(trader.get_open_positions()) == 1

    def test_short_sl_closes_no_bid_ask(self, trader):
        """SHORT position stays OPEN when no bids/asks are supplied.

        Card 9f051898: ask=0 (SHORT) is a "no quote" sentinel — SL
        evaluation is skipped, the position stays OPEN. Replaces the
        card 9310bdd0 current_price-fallback behaviour, which closed
        SHORT positions on a mid rise past SL even without an exit-side
        ask quote.
        """
        signal = _make_signal(
            direction=TradeDirection.SHORT,
            entry=1.1000,
            sl=1.1050,
            tp=1.0880,
        )
        _open_paper_position(trader, signal)

        trader.update_market_prices(prices={"EURUSD": 1.1100})

        # Position stays OPEN because ask=0 is no-quote sentinel for SHORT
        assert len(trader.get_open_positions()) == 1

    def test_short_tp_closes_no_bid_ask(self, trader):
        """SHORT position TP fires without bid/ask."""
        signal = _make_signal(
            direction=TradeDirection.SHORT,
            entry=1.1000,
            sl=1.1050,
            tp=1.0880,
        )
        _open_paper_position(trader, signal)

        trader.update_market_prices(prices={"EURUSD": 1.0850})

        assert len(trader.get_open_positions()) == 0

    def test_multiple_positions_close_independently(self, trader):
        """Multiple positions close independently as price crosses SL/TP.

        Card 9f051898 (bid/ask side-selection fix): explicit bid/ask are
        supplied so SL evaluation runs (with bid=0 the no-quote sentinel
        would skip the SL check). Pre-fix the test relied on the
        card 9310bdd0 current_price-fallback path.
        """
        # Open two positions with different SLs
        sig1 = _make_signal(entry=1.1000, sl=1.0950, tp=1.1120)
        _open_paper_position(trader, sig1)

        # Move price to hit SL of first position — pass bid/ask so SL runs
        trader.update_market_prices(
            prices={"EURUSD": 1.0940},
            bids={"EURUSD": 1.0940},
            asks={"EURUSD": 1.0942},
        )
        assert len(trader.get_open_positions()) == 0
