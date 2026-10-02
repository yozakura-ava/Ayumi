"""Tests for Phase 7: broker startup preflight + seed existing positions.

Scope:
- 7A: seed existing cTrader positions into SLPositionSizer at engine startup
- 7B: live-mode launcher hard block without remediation_validated.flag
- 7C: reconcile() returns enriched position data
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure src/forex_bot is on path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src" / "forex_bot"))

from adapters.ctrader.forward_test_engine import ForwardTestConfig, ForwardTestEngine
from adapters.ctrader.models import Position, PositionStatus, TradeDirection
from adapters.ctrader.open_api_spot_feed import OpenApiSpotFeed
from risk.sl_position_sizer import SLPositionSizer


class TestPreflightSeed:
    """Unit tests for seeding existing broker positions into the risk sizer."""

    def _make_engine(self, live_mode: bool = False):
        cfg = ForwardTestConfig(
            symbol="GBPUSD",
            symbols=["GBPUSD", "USDJPY"],
            live_mode=live_mode,
            execution_mode="live" if live_mode else "paper",
            openapi_host="demo.ctraderapi.com",
        )
        engine = ForwardTestEngine(config=cfg, strategies=[])
        engine._paper_trader = MagicMock()
        engine._position_monitor = MagicMock()
        engine._live_adapter = None
        return engine

    def _make_position(
        self,
        position_id: str,
        symbol: str,
        volume: float,
        entry_price: float,
        sl_price: float | None,
        direction: TradeDirection = TradeDirection.LONG,
    ) -> Position:
        return Position(
            position_id=str(position_id),
            symbol=symbol,
            direction=direction,
            volume=volume,
            entry_price=entry_price,
            current_price=entry_price,
            stop_loss=sl_price,
            take_profit=None,
            status=PositionStatus.OPEN,
        )

    def test_seed_three_positions_registers_correct_risk(self, caplog):
        """Three open positions with SL should be seeded with computed risk."""
        engine = self._make_engine()
        sizer = SLPositionSizer(account_balance=100_000.0)

        positions = [
            self._make_position("1001", "EURUSD", 0.5, 1.0800, 1.0700),
            self._make_position("1002", "GBPUSD", 1.0, 1.3000, 1.2900),
            self._make_position("1003", "USDJPY", 0.2, 150.00, 149.00),
        ]

        with caplog.at_level(logging.INFO, logger="ayumi.forward_test"):
            engine._seed_existing_positions(positions, sizer)

        # EURUSD: |1.0800 - 1.0700| = 0.0100 = 100 pips; 100 * 0.5 lots * $10/lot = $500
        # GBPUSD: |1.3000 - 1.2900| = 0.0100 = 100 pips; 100 * 1.0 lots * $10/lot = $1000
        # USDJPY: |150.00 - 149.00| = 1.00  = 100 pips; 100 * 0.2 lots * $6.5/lot = $130
        assert sizer.open_risk == pytest.approx(1630.0)
        assert "seeded_1001" in sizer.open_positions
        assert "seeded_1002" in sizer.open_positions
        assert "seeded_1003" in sizer.open_positions

        assert "Preflight: seeded 3 open cTrader positions" in caplog.text
        assert "totaling $1630.00" in caplog.text

    def test_seed_position_without_sl_uses_fallback_and_warns(self, caplog):
        """A position without SL should use lots * 100 and log a WARNING."""
        engine = self._make_engine()
        sizer = SLPositionSizer(account_balance=100_000.0)
        position = self._make_position("2001", "GBPUSD", 0.75, 1.3000, None)

        with caplog.at_level(logging.INFO, logger="ayumi.forward_test"):
            engine._seed_existing_positions([position], sizer)

        assert sizer.open_risk == pytest.approx(75.0)
        assert "seeded_2001" in sizer.open_positions
        assert "has no stop loss" in caplog.text
        assert "Preflight: seeded 1 open cTrader positions" in caplog.text
        assert "totaling $75.00" in caplog.text

    def test_seed_no_positions_logs_zero_risk(self, caplog):
        """Empty reconcile result should log 0 seeded positions and not crash."""
        engine = self._make_engine()
        sizer = SLPositionSizer(account_balance=100_000.0)

        with caplog.at_level(logging.INFO, logger="ayumi.forward_test"):
            engine._seed_existing_positions([], sizer)

        assert sizer.open_risk == pytest.approx(0.0)
        assert "Preflight: seeded 0 open cTrader positions" in caplog.text
        assert "totaling $0.00" in caplog.text


class TestReconcileEnrichment:
    """Tests for reconcile() data enrichment needed for seeding."""

    def test_reconcile_position_has_required_fields(self):
        """The Position dataclass must expose fields required by the seeder."""
        pos = Position(
            position_id="42",
            symbol="EURUSD",
            direction=TradeDirection.LONG,
            volume=0.5,
            entry_price=1.0800,
            current_price=1.0810,
            stop_loss=1.0700,
            take_profit=1.0900,
            status=PositionStatus.OPEN,
        )
        assert pos.position_id == "42"
        assert pos.symbol == "EURUSD"
        assert pos.volume == 0.5
        assert pos.entry_price == 1.0800
        assert pos.stop_loss == 1.0700

    def test_open_api_spot_feed_reconcile_returns_position_objects(self):
        """OpenApiSpotFeed.reconcile returns list[Position] with needed attrs."""
        feed = MagicMock(spec=OpenApiSpotFeed)
        feed.reconcile.return_value = []
        result = feed.reconcile()
        assert result == []


class TestLiveModeBlock:
    """Tests for the live-mode hard block."""

    def test_live_mode_without_validation_flag_raises(self, tmp_path, monkeypatch):
        """Live mode must raise RuntimeError when remediation flag is missing.

        The hard block requires BOTH the flag file AND the audit doc to be
        absent. The audit doc is the source of truth for crash-recovery: if
        only the flag is missing but the audit doc exists, the engine
        auto-recreates the flag (Phase 7 survivability fix, card
        ``survivable-remediation-flag``). This test isolates the hard-block
        branch by pointing the audit-doc lookup at a guaranteed-missing path
        under ``tmp_path``.
        """
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._validate_credentials",
            lambda self: True,
        )
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._build_components",
            lambda self: None,
        )
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._wire_callbacks",
            lambda self: None,
        )
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._start_market_feed",
            lambda self: False,
        )
        # Force the audit-doc fallback path off so the engine cannot
        # auto-recreate the flag — we want to exercise the hard block.
        # Point at a tmp_path that we never create so os.path.exists() is False.
        missing_audit = tmp_path / "does_not_exist" / "audit.md"
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine._REMEDIATION_AUDIT_DOC",
            str(missing_audit),
        )

        engine = ForwardTestEngine(
            config=ForwardTestConfig(live_mode=True, execution_mode="live"),
            strategies=[],
        )
        # Redirect the flag lookup to a tmp_path-resident file so the test
        # never reads or writes the production data/ tree. We patch the
        # module constant (``_REMEDIATION_VALIDATED_FLAG``) to a
        # tmp_path file guaranteed to be absent so the hard-block branch
        # fires without touching ``<repo>/data/ayumi/``.
        isolated_flag = tmp_path / "remediation_validated.flag"
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine._REMEDIATION_VALIDATED_FLAG",
            str(isolated_flag),
        )

        with pytest.raises(RuntimeError, match="Refusing to start in live mode"):
            engine.start()

    def test_paper_mode_ignores_validation_flag(self, tmp_path, monkeypatch):
        """Paper mode must start normally without remediation flag."""
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._validate_credentials",
            lambda self: True,
        )
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._build_components",
            lambda self: None,
        )
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._wire_callbacks",
            lambda self: None,
        )
        monkeypatch.setattr(
            "adapters.ctrader.forward_test_engine.ForwardTestEngine._start_market_feed",
            lambda self: True,
        )

        engine = ForwardTestEngine(
            config=ForwardTestConfig(live_mode=False, execution_mode="paper"),
            strategies=[],
        )

        assert engine.start() is True
        assert engine.is_running is True
        engine.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
