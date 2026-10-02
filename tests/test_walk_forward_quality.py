"""Tests for walk-forward quality guardrails (T6).

Covers:
- Profit Factor sanitisation: no Infinity in outputs
- Zero gross_loss + zero gross_profit -> PF = 0.0
- Trade count warning flag when < 15 trades
"""

import math
import sys

sys.path.insert(0, "src/forex_bot")

from backtest.walk_forward_runner import (  # noqa: I001
    MIN_TRADES_WARNING,
    PF_CAP,
    _check_trade_count_warning,
    _sanitize_profit_factor,
)
from quant.walk_forward import _compute_metrics

# ---------------------------------------------------------------------------
# Test 1: PF with zero loss returns a capped value, not Infinity
# ---------------------------------------------------------------------------


class TestProfitFactorZeroLoss:
    """When gross_loss == 0 and gross_profit > 0, PF must be finite."""

    def test_sanitize_inf_returns_cap(self):
        """Direct sanitisation of Infinity -> PF_CAP."""
        result = _sanitize_profit_factor(float("inf"))
        assert not math.isinf(result), "PF is still Infinity"
        assert result == PF_CAP

    def test_sanitize_negative_inf_returns_cap(self):
        result = _sanitize_profit_factor(float("-inf"))
        assert not math.isinf(result)
        assert result == PF_CAP

    def test_compute_metrics_zero_loss_all_wins(self):
        """_compute_metrics with all-winning trades returns finite PF."""
        trades = [
            {"pnl": 10.0},
            {"pnl": 20.0},
            {"pnl": 15.0},
            {"pnl": 5.0},
            {"pnl": 30.0},
        ]
        metrics = _compute_metrics(0, trades, initial_balance=10000.0)
        assert not math.isinf(metrics.profit_factor), f"PF is Infinity for all-win trades: {metrics.profit_factor}"
        assert metrics.profit_factor > 0.0
        assert metrics.profit_factor <= PF_CAP, f"PF {metrics.profit_factor} exceeds cap {PF_CAP}"


# ---------------------------------------------------------------------------
# Test 2: PF with both zero returns 0.0
# ---------------------------------------------------------------------------


class TestProfitFactorBothZero:
    """When gross_loss == 0 AND gross_profit == 0, PF must be 0.0."""

    def test_sanitize_zero_returns_zero(self):
        result = _sanitize_profit_factor(0.0)
        assert result == 0.0

    def test_sanitize_nan_returns_zero(self):
        """NaN (from 0/0) should become 0.0."""
        result = _sanitize_profit_factor(float("nan"))
        assert result == 0.0

    def test_compute_metrics_empty_trades(self):
        """No trades -> PF = 0.0."""
        metrics = _compute_metrics(0, [], initial_balance=10000.0)
        assert metrics.profit_factor == 0.0


# ---------------------------------------------------------------------------
# Test 3: < 15 trades produces warning
# ---------------------------------------------------------------------------


class TestTradeCountWarning:
    """Windows with fewer than 15 trades must trigger a warning."""

    def test_warning_returned_below_threshold(self):
        """_check_trade_count_warning returns True when < 15 trades."""
        assert _check_trade_count_warning(0, 5) is True
        assert _check_trade_count_warning(1, 14) is True

    def test_no_warning_at_threshold(self):
        """15 or more trades -> no warning."""
        assert _check_trade_count_warning(0, 15) is False
        assert _check_trade_count_warning(1, 50) is False

    def test_warning_logged(self, caplog):
        """Warning message is logged with window and trade count."""
        import logging

        with caplog.at_level(logging.WARNING, logger="backtest.walk_forward_runner"):
            result = _check_trade_count_warning(3, 8)

        assert result is True
        # Verify the log message contains key info
        log_text = caplog.text
        assert "Window 3" in log_text
        assert "8 trades" in log_text
        assert "statistical significance" in log_text

    def test_min_trades_constant(self):
        """The MIN_TRADES_WARNING constant is 15."""
        assert MIN_TRADES_WARNING == 15
