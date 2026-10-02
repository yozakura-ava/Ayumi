"""Regression tests for XAUUSD pip-size/contract convention.

Background
----------
Prior to this fix, ``TTSStrategy.__init__`` hardcoded::

    pip_size = 0.01 if symbol.upper().endswith("JPY") or symbol.upper() == "XAUUSD" else 0.0001

That forced XAUUSD into the JPY convention (``pip_size=0.01``) when the
canonical cTrader gold pip is ``0.1`` (see ``utils.pip_value.XAU_PIP``).
The fix replaces the hardcoded branch with
``pip_value_for_symbol(symbol)``.

These tests pin the canonical convention for both **SHORT** (sl > entry)
and **LONG** (sl < entry) XAUUSD sizing, plus a smoke-import of the
strategy module to ensure the new import line resolves cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src/forex_bot is on sys.path so the strategy can import utils.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SRC_FOREX_BOT = _REPO_ROOT / "src" / "forex_bot"
if str(_SRC_FOREX_BOT) not in sys.path:
    sys.path.insert(0, str(_SRC_FOREX_BOT))

from utils.pip_value import (  # noqa: E402  -- path injection above
    CRYPTO_PIP,
    DEFAULT_PIP,
    JPY_PIP,
    XAU_PIP,
    pip_value_for_symbol,
)

# ---------------------------------------------------------------------------
# Canonical pip_value_for_symbol convention
# ---------------------------------------------------------------------------


class TestCanonicalPipValue:
    """Pin the canonical pip_value_for_symbol convention (no XAUUSD drift)."""

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("XAUUSD", XAU_PIP),
            ("xauusd", XAU_PIP),  # case-insensitive
            ("GOLD", XAU_PIP),
            ("XAU", XAU_PIP),
            ("USDJPY", JPY_PIP),
            ("EURUSD", DEFAULT_PIP),
            ("GBPUSD", DEFAULT_PIP),
            ("XAGUSD", 0.001),
            ("BTCUSD", CRYPTO_PIP),
        ],
    )
    def test_pip_value_for_symbol_returns_canonical(self, symbol: str, expected: float) -> None:
        assert pip_value_for_symbol(symbol) == pytest.approx(expected)

    def test_xauusd_is_zero_point_one_not_zero_point_zero_one(self) -> None:
        """The exact assertion the original bug violated: XAUUSD pip = 0.1."""
        assert pip_value_for_symbol("XAUUSD") == pytest.approx(0.1)
        assert pip_value_for_symbol("XAUUSD") != pytest.approx(0.01)


# ---------------------------------------------------------------------------
# SHORT + LONG sizing math for XAUUSD
# ---------------------------------------------------------------------------


class TestXauusdSizingLongAndShort:
    """SHORT (sl > entry) and LONG (sl < entry) XAUUSD sizing with pip_size=0.1.

    Canonical math for the card's reference scenario:
        account  = $10,000
        risk_pct = 0.005  → risk_amount = $50
        entry    = 3355.30
        sl       = 3350.37 (LONG, sl < entry)
        pip_size = 0.1
        sl_distance_pips = |entry - sl| / 0.1 = 49.3
        pip_value_per_lot = $10.0
        lots = $50 / (49.3 × $10.0) ≈ 0.10 lots (NOT 0.05 lots)
    """

    @pytest.fixture()
    def sizer(self):
        from risk.sl_position_sizer import SLPositionSizer

        return SLPositionSizer(
            account_balance=10_000.0,
            risk_per_trade_pct=0.005,  # 0.5% → $50
            max_lot_size=1.0,
            daily_risk_cap_pct=0.03,
            max_positions_per_symbol=10,
            max_total_open_risk=10_000.0,
        )

    def test_long_xauusd_sizing_uses_canonical_pip(self, sizer) -> None:
        """LONG XAUUSD: sl < entry, 49.3 pips, ~0.10 lots (NOT 0.05 / NOT 6,185 pips)."""
        result = sizer.calculate("XAUUSD", 3355.30, 3350.37, profile="sniper")

        assert not result.blocked, f"Sizing blocked unexpectedly: {result.block_reason}"
        # 49.3 pips = 4.93 / 0.1   (canonical XAUUSD pip_size)
        assert result.sl_distance_pips == pytest.approx(49.3, abs=0.05)
        assert result.sl_distance_pips != pytest.approx(6185.0)
        # $50 / (49.3 × $10) = 0.1014...
        assert result.lots == pytest.approx(0.10, rel=0.05)
        assert result.lots != pytest.approx(0.05, abs=0.005)

    def test_short_xauusd_sizing_uses_canonical_pip(self, sizer) -> None:
        """SHORT XAUUSD: sl > entry, mirrored math still produces ~0.10 lots."""
        # SHORT: stop loss sits above entry. Use the symmetric mirror of the LONG scenario.
        result = sizer.calculate("XAUUSD", 3355.30, 3360.37, profile="sniper")

        assert not result.blocked, f"Sizing blocked unexpectedly: {result.block_reason}"
        # |3360.37 - 3355.30| = 5.07 → 5.07 / 0.1 = 50.7 pips
        assert result.sl_distance_pips == pytest.approx(50.7, abs=0.05)
        # $50 / (50.7 × $10) ≈ 0.0986 lots
        assert result.lots == pytest.approx(0.10, rel=0.05)

    def test_long_and_short_xauusd_are_symmetric(self, sizer) -> None:
        """LONG and SHORT XAUUSD with mirrored price distance → comparable lots."""
        long_result = sizer.calculate("XAUUSD", 3355.30, 3350.30, profile="sniper")
        short_result = sizer.calculate("XAUUSD", 3355.30, 3360.30, profile="sniper")

        assert not long_result.blocked
        assert not short_result.blocked
        # 5.00 / 0.1 = 50 pips each → $50 / (50 × $10) = 0.10 lots each
        assert long_result.sl_distance_pips == pytest.approx(50.0, abs=0.05)
        assert short_result.sl_distance_pips == pytest.approx(50.0, abs=0.05)
        assert long_result.lots == pytest.approx(0.10, rel=0.05)
        assert short_result.lots == pytest.approx(0.10, rel=0.05)

    def test_xauusd_lots_independent_of_jpy_branch(self, sizer) -> None:
        """Defensive: confirms XAUUSD does not inherit the JPY 0.01 branch.

        If pip_size were 0.01 instead of 0.1, the same $30 SL distance
        would yield 3000 pips and ~0.0017 lots — neither of which should
        occur under the canonical convention.
        """
        result = sizer.calculate("XAUUSD", 3350.0, 3320.0, profile="sniper")

        assert not result.blocked
        assert result.sl_distance_pips == pytest.approx(300.0, abs=0.5), (
            f"Expected 300 pips (canonical pip_size=0.1), got {result.sl_distance_pips}"
        )
        # $50 / (300 × $10) = 0.0166... lots — far from the 0.0017 the JPY bug produces
        assert result.lots > 0.01


# ---------------------------------------------------------------------------
# Smoke-import: ensures tts_strategy.py loads with the new pip_value import
# ---------------------------------------------------------------------------


class TestTTSStrategyModuleImport:
    """Verify TTSStrategy module imports cleanly after the pip_value fix.

    Instantiating the full strategy pulls in ML configs and signal engine
    components — that's outside the lane scope here. The module-level
    import smoke-test is sufficient to catch a missing/typo'd import
    line at the top of ``tts_strategy.py``.
    """

    def test_module_imports_without_error(self) -> None:
        import importlib

        # Force re-import in case other tests already cached it
        module_name = "backtest.strategies.tts_strategy"
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)

    def test_module_uses_canonical_pip_value_for_symbol(self) -> None:
        """Spot-check: TTSStrategy.__init__ source references the canonical helper.

        This is a static check (not runtime) — guards against future
        re-introduction of the hardcoded ``pip_size = 0.01 if ... ==
        "XAUUSD"`` branch.
        """
        from backtest.strategies import tts_strategy as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "pip_value_for_symbol" in src, (
            "TTSStrategy no longer uses canonical pip_value_for_symbol — "
            "XAUUSD convention drift regressed."
        )
        assert (
            'symbol.upper() == "XAUUSD"' not in src
            or "pip_value_for_symbol(symbol)" in src
        ), (
            "Hardcoded XAUUSD pip branch reappeared in TTSStrategy. "
            "Canonical: pip_value_for_symbol(symbol)."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
