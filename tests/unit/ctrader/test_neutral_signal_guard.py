"""Tests for the NEUTRAL-direction guard in ForwardTestEngine._execute_signal_live.

Card: eaceb5fa-1fdf-4df2-bb45-5e8649c92249 (Phase 1 — unblocks live fills)

Bug history
-----------
Blend/amalgamation can produce ``direction == TradeDirection.NEUTRAL`` when
long/short votes are tied (see backtest/amalgamation.py:248, :257). The
legacy mapping at forward_test_engine.py ~line 1428:

    side = BUY if direction == LONG else SELL

silently coerced NEUTRAL → SELL while preserving the original LONG-style
SL/TP. The broker then rejected the order with TRADING_BAD_STOPS
("SL for SELL pending order should be > entry price; TP for SELL pending
order should be < entry price"). This produced the 0-fill campaign that
ran 2026-07-11 → 2026-07-15 (HB#24-35 evidence trail).

Fix
---
Insert an early-return guard at the top of ``_execute_signal_live`` that
skips NEUTRAL signals with an INFO log. No re-pricing logic — just don't
place an order we know will be rejected. Phase 2 will recompute SL/TP for
tied blends and reinstate NEUTRAL fills.

This test module covers the three ACs:

1. NEUTRAL signal does NOT call ``new_order`` (returns None early).
2. NEUTRAL signal emits an INFO log with the skip reason.
3. LONG and SHORT signals still pass the guard (reach ``new_order``).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ─────────────────────────────────────────────────────────
#
# The repo root (``$AYUMI_ROOT``) contains a top-level
# ``backtest/`` package that only exposes bootstrap/sweep scripts. The real
# ``backtest`` package (with ``backtest.engine``, ``backtest.amalgamation``,
# etc.) lives at ``src/forex_bot/backtest/``. When pytest sets the cwd as
# the first entry on ``sys.path``, Python resolves ``import backtest`` to
# the shadow package, and ``from backtest.engine import Bar, MarketState``
# inside ``forward_test_engine.py:33`` fails with
# ``ModuleNotFoundError: No module named 'backtest.engine'``.
#
# Inserting ``src/forex_bot`` at position 0 of ``sys.path`` at module load
# is too early — pytest mutates ``sys.path`` again before any test
# function runs, putting the cwd back at position 0. The fix is to
# re-insert the path at the start of every test via an autouse fixture.
_FOREX_BOT_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))


@pytest.fixture(autouse=True)
def _ensure_forex_bot_on_path():
    """Put ``src/forex_bot`` at the head of ``sys.path`` for each test."""
    sys.path.insert(0, _FOREX_BOT_SRC)
    yield
    # No cleanup — leaving the path inserted is harmless for subsequent tests.


# ── Helpers ────────────────────────────────────────────────────────────


def _make_signal(direction):
    """Construct a CTraderTradeSignal with the requested direction."""
    from adapters.ctrader.models import CTraderTradeSignal

    base = dict(
        symbol="GBPUSD",
        direction=direction,
        entry_price=1.25000,
        stop_loss=1.24500,
        take_profit_1=1.26000,
        take_profit_2=1.27000,
        take_profit_3=1.28000,
        volume=0.1,
        confidence=0.70,
        rationale="test_signal",
        strategy_id="test_strategy",
        timestamp=datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    return CTraderTradeSignal(**base)


def _make_engine():
    """Build a minimal ForwardTestEngine bypassing __init__.

    Wires enough internals so ``_execute_signal_live`` reaches the guard
    under test and (for LONG/SHORT paths) the broker interaction.
    """
    # Explicitly import the module so it lands in ``sys.modules`` AND
    # on the ``adapters.ctrader`` namespace.  ``adapters.ctrader.__init__``
    # only imports from ``.models``; without this preload, string-based
    # ``patch("adapters.ctrader.forward_test_engine.ForwardTestEngine.__init__")``
    # fails with ``AttributeError: module 'adapters.ctrader' has no
    # attribute 'forward_test_engine'`` because mock's resolver traverses
    # the package namespace, not sys.modules.
    import adapters.ctrader.forward_test_engine as _fte  # noqa: F401

    with patch.object(
        _fte.ForwardTestEngine,
        "__init__",
        return_value=None,
    ):
        ForwardTestEngine = _fte.ForwardTestEngine

        engine = ForwardTestEngine.__new__(ForwardTestEngine)
        engine._lock = MagicMock()
        engine._kill_switch = None  # P5A permission gate will pass

        # Health object so signal-handler stats code paths don't blow up
        engine._health = MagicMock()

        # Paper trader stub (used by some downstream paths; not strictly
        # required for this guard but kept for parity with sibling tests)
        engine._paper_trader = MagicMock()

        # Stats-recorder bookkeeping: real __init__ populates these; we
        # bypass it, so set the minimum the FILLED-branch retry loop needs.
        engine._stats_retry_max = 0  # zero retries — record_signal noop
        engine._stats_recorder = None

        # Order manager stub for downstream TP2/TP3 / position-mapping hooks
        engine._order_manager = MagicMock()
        engine._register_blend_position_mapping = MagicMock()
        engine._resolve_order_manager = MagicMock(return_value=None)

        return engine


def _wire_market_feed(engine, *, new_order_return):
    """Attach a mocked OpenApiSpotFeed so LONG/SHORT paths reach new_order.

    Uses ``spec=OpenApiSpotFeed`` so the ``isinstance(self._market_feed,
    OpenApiSpotFeed)`` pre-flight check in ``_execute_signal_live`` passes.
    A bare ``MagicMock()`` would fail that isinstance test and short-circuit
    the flow before ``new_order`` is reached.

    Also wires ``_state_mgr = None`` so the T5 spot-feed-not-operational
    check at forward_test_engine.py:1424 short-circuits cleanly, and
    stubs ``_calculate_live_volume`` to return a positive float (the real
    method depends on broker account state we don't model here).
    """
    from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed

    feed = MagicMock(spec=OpenApiSpotFeed)
    feed._state_mgr = None  # T5 pre-flight: state_mgr None ⇒ skip
    feed.resolve_symbol_id.return_value = 2  # GBPUSD cTrader symbolId
    feed.lots_to_volume.return_value = 1000  # raw volume in cents
    feed.new_order.return_value = new_order_return
    engine._market_feed = feed

    engine._calculate_live_volume = MagicMock(return_value=0.1)
    return feed


def _patch_permission_policy_to_allow():
    """Patch ExecutionPermissionPolicy so the P5A gate returns ``allowed=True``.

    Without this, the permission policy defaults to denying and the
    LONG/SHORT tests can't reach ``new_order``.
    """
    return patch(
        "adapters.ctrader.execution_permission.ExecutionPermissionPolicy.can_send_order",
        return_value=(True, "test_allow"),
    )


# ── Tests ──────────────────────────────────────────────────────────────


class TestNeutralGuardSkips:
    """AC1 + AC2: NEUTRAL signals are skipped with an INFO log."""

    def test_neutral_signal_returns_none_without_calling_new_order(self):
        """NEUTRAL → returns None; new_order is NEVER called."""
        from adapters.ctrader.models import TradeDirection

        engine = _make_engine()
        feed = _wire_market_feed(engine, new_order_return=MagicMock())

        signal = _make_signal(TradeDirection.NEUTRAL)

        result = engine._execute_signal_live(signal, strategy_id="session_range_mr")

        assert result is None, f"Expected None for NEUTRAL signal, got {result!r}"
        (
            feed.new_order.assert_not_called(),
            ("new_order must NOT be called when direction is NEUTRAL"),
        )

    def test_neutral_signal_emits_info_log_with_skip_reason(self, caplog):
        """NEUTRAL → INFO log mentions the skip reason."""
        from adapters.ctrader.models import TradeDirection

        engine = _make_engine()
        _wire_market_feed(engine, new_order_return=MagicMock())

        signal = _make_signal(TradeDirection.NEUTRAL)

        with caplog.at_level(
            logging.INFO,
            logger="ayumi.forward_test",
        ):
            result = engine._execute_signal_live(
                signal,
                strategy_id="session_range_mr",
            )

        assert result is None

        skip_records = [r for r in caplog.records if "Skipping live execution" in r.getMessage()]
        assert len(skip_records) == 1, (
            f"Expected exactly 1 skip log, got {len(skip_records)}: {[r.getMessage() for r in caplog.records]}"
        )
        record = skip_records[0]
        assert record.levelno == logging.INFO
        # Skip message must include: symbol, direction, strategy_id, reason
        msg = record.getMessage()
        assert signal.symbol in msg, f"symbol missing from skip log: {msg!r}"
        assert "NEUTRAL" in msg, f"NEUTRAL missing from skip log: {msg!r}"
        assert "session_range_mr" in msg, f"strategy_id missing from skip log: {msg!r}"
        assert "no side mapping" in msg.lower(), f"skip reason phrase missing from log: {msg!r}"

    def test_neutral_signal_does_not_trigger_permission_policy_warning(self, caplog):
        """NEUTRAL guard runs BEFORE permission policy — no 'blocked' warning."""
        from adapters.ctrader.models import TradeDirection

        engine = _make_engine()
        _wire_market_feed(engine, new_order_return=MagicMock())
        signal = _make_signal(TradeDirection.NEUTRAL)

        with caplog.at_level(
            logging.WARNING,
            logger="ayumi.forward_test",
        ):
            engine._execute_signal_live(signal, strategy_id="srmr")

        blocked = [r for r in caplog.records if "_execute_signal_live blocked" in r.getMessage()]
        assert blocked == [], (
            f"Permission-policy 'blocked' warning should NOT fire for NEUTRAL; "
            f"the guard short-circuits earlier. Got: {[r.getMessage() for r in blocked]}"
        )


class TestLongShortPassThrough:
    """AC3: LONG and SHORT signals still reach new_order unchanged."""

    def test_long_signal_reaches_new_order(self):
        """LONG → guard does NOT short-circuit; new_order IS called."""
        from adapters.ctrader.models import TradeDirection

        engine = _make_engine()
        sentinel_order = MagicMock()
        sentinel_order.order_id = "live-long-001"
        feed = _wire_market_feed(engine, new_order_return=sentinel_order)

        signal = _make_signal(TradeDirection.LONG)

        with _patch_permission_policy_to_allow():
            # Stub _classify_live_order_outcome so the call doesn't trip on
            # MagicMock attribute access — we're testing the guard, not the
            # classifier.
            with patch.object(
                type(engine),
                "_classify_live_order_outcome",
                return_value=MagicMock(),
            ):
                engine._execute_signal_live(signal, strategy_id="trend_follow")

        feed.new_order.assert_called_once()
        call_kwargs = feed.new_order.call_args.kwargs
        # Side must be BUY (not silently coerced)
        # ProtoOATradeSide.BUY == 1 in cTrader Open API.
        assert call_kwargs["side"] == 1, f"LONG signal must map to side=BUY (1), got {call_kwargs['side']!r}"
        assert call_kwargs["symbol_id"] == 2
        assert call_kwargs["order_type"] == 1  # ProtoOAOrderType.MARKET

    def test_short_signal_reaches_new_order(self):
        """SHORT → guard does NOT short-circuit; new_order IS called."""
        from adapters.ctrader.models import TradeDirection

        engine = _make_engine()
        sentinel_order = MagicMock()
        sentinel_order.order_id = "live-short-001"
        feed = _wire_market_feed(engine, new_order_return=sentinel_order)

        signal = _make_signal(TradeDirection.SHORT)

        with _patch_permission_policy_to_allow():
            with patch.object(
                type(engine),
                "_classify_live_order_outcome",
                return_value=MagicMock(),
            ):
                engine._execute_signal_live(signal, strategy_id="trend_follow")

        feed.new_order.assert_called_once()
        call_kwargs = feed.new_order.call_args.kwargs
        # ProtoOATradeSide.SELL == 2 in cTrader Open API.
        assert call_kwargs["side"] == 2, f"SHORT signal must map to side=SELL (2), got {call_kwargs['side']!r}"

    def test_long_and_short_skip_logs_are_never_emitted(self, caplog):
        """Sanity: the NEUTRAL skip log must NOT appear for LONG/SHORT."""
        from adapters.ctrader.models import TradeDirection

        engine = _make_engine()
        _wire_market_feed(engine, new_order_return=MagicMock())

        with caplog.at_level(
            logging.INFO,
            logger="ayumi.forward_test",
        ):
            with _patch_permission_policy_to_allow():
                with patch.object(
                    type(engine),
                    "_classify_live_order_outcome",
                    return_value=MagicMock(),
                ):
                    engine._execute_signal_live(
                        _make_signal(TradeDirection.LONG),
                        strategy_id="long_strat",
                    )
                    engine._execute_signal_live(
                        _make_signal(TradeDirection.SHORT),
                        strategy_id="short_strat",
                    )

        skip_records = [r for r in caplog.records if "Skipping live execution" in r.getMessage()]
        assert skip_records == [], (
            f"LONG/SHORT must NOT emit NEUTRAL-skip logs; got {[r.getMessage() for r in skip_records]}"
        )
