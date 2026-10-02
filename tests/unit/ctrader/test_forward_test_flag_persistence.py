"""Unit tests for ForwardTestEngine remediation flag persistence (Card 2893597d).

Verifies that the forward test engine survives restart when the
``remediation_validated.flag`` file is missing but the underlying audit doc
(source of truth) still exists.

Test scenarios (see ``_enforce_remediation_gate`` in ``forward_test_engine.py``):
1. Paper mode (live_mode=False)                        → always allowed.
2. Flag present                                         → no-op, no filesystem touch.
3. Flag missing + audit doc present                     → flag is auto-recreated.
4. Flag missing + audit doc missing  (live_mode=True)  → RuntimeError.
"""

from __future__ import annotations

import os
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Pre-import shim: bypass ``backtest`` package to avoid the statsmodels chain.
# ``forward_test_engine`` imports ``from backtest.engine import Bar, MarketState``,
# which transitively pulls statsmodels. The shim is idempotent.
# ---------------------------------------------------------------------------
def _install_backtest_stub():
    if "backtest" in sys.modules and getattr(sys.modules["backtest"], "_tsukasa_stub", False):
        return

    class _Fake:
        pass

    fake_pkg = types.ModuleType("backtest")
    fake_pkg.__path__ = []  # mark as a package
    fake_pkg._tsukasa_stub = True
    sys.modules["backtest"] = fake_pkg

    def _make(name: str, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod

    _make(
        "backtest.engine",
        Bar=_Fake,
        MarketState=_Fake,
        TradeDirection=_Fake,
        TradeAction=_Fake,
    )
    _make(
        "backtest.types",
        determine_session=lambda *a, **k: None,
        SessionType=_Fake,
        TradeDirection=_Fake,
        TradeAction=_Fake,
    )
    _make(
        "backtest.strategies",
        ISignalStrategy=_Fake,
        BBStrategy=_Fake,
        RSIStrategy=_Fake,
        MACrossStrategy=_Fake,
    )
    _make("backtest.ict_smc", ICTSMCStrategy=_Fake)
    _make("backtest.enhanced_engine", EnhancedBacktestEngine=_Fake)
    _make("backtest.grid_strategy", GridStrategy=_Fake)
    _make("backtest.hybrid_strategy", HybridStrategy=_Fake, HybridConfig=_Fake)
    _make(
        "backtest.multi_strategy_engine",
        MultiStrategyBacktestEngine=_Fake,
        MultiStrategyConfig=_Fake,
    )
    _make(
        "backtest.stat_arb",
        StatArbBacktestEngine=_Fake,
        StatArbStrategy=_Fake,
        StatArbBacktestResult=_Fake,
    )
    _make(
        "backtest.amalgamation",
        AmalgamatedBacktestEngine=_Fake,
        AmalgamationConfig=_Fake,
        AmalgamationEngine=_Fake,
        ComponentExtractor=_Fake,
        ComponentProfile=_Fake,
        ConfidenceMethod=_Fake,
        ExtractionResult=_Fake,
        VotingMethod=_Fake,
    )
    _make("backtest.data_loader", CsvDataLoader=_Fake)
    _make(
        "backtest.trade_management",
        ManagedTrade=_Fake,
        TradeAction=_Fake,
        TradeManagementConfig=_Fake,
        TradeManager=_Fake,
    )
    _make(
        "backtest.statistical_study",
        CriterionResult=_Fake,
        GoNoGoCriteria=_Fake,
        StatisticalStudy=_Fake,
        StatisticalStudyResult=_Fake,
    )
    _make(
        "backtest.pattern_detector",
        ConsolidationFilter=_Fake,
        ConsolidationMetrics=_Fake,
        MWPattern=_Fake,
        MWPatternDetector=_Fake,
    )


_install_backtest_stub()


# Now safe to import the module under test
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "forex_bot"))

from adapters.ctrader import forward_test_engine as fte_module  # noqa: E402
from adapters.ctrader.forward_test_engine import ForwardTestEngine  # noqa: E402


@pytest.fixture
def tmp_flag_paths(tmp_path, monkeypatch):
    """Redirect the module-level flag + audit doc paths to temp files."""
    flag_path = tmp_path / "remediation_validated.flag"
    audit_path = tmp_path / "ayumi-live-remediation-session-audit.md"

    monkeypatch.setattr(fte_module, "_REMEDIATION_VALIDATED_FLAG", str(flag_path))
    monkeypatch.setattr(fte_module, "_REMEDIATION_AUDIT_DOC", str(audit_path))

    return flag_path, audit_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_paper_mode_is_unconditional(tmp_flag_paths):
    """live_mode=False → no flag check, no audit doc required."""
    flag_path, audit_path = tmp_flag_paths
    assert not flag_path.exists()
    assert not audit_path.exists()

    # Paper mode must not raise even with both files absent.
    ForwardTestEngine._enforce_remediation_gate(live_mode=False)
    assert not flag_path.exists(), "flag must not be created in paper mode"


def test_flag_present_is_noop(tmp_flag_paths):
    """Flag present → do not touch it."""
    flag_path, audit_path = tmp_flag_paths
    flag_path.write_text("original-content\n")
    audit_path.write_text("# Audit\n")
    original = flag_path.read_text()

    ForwardTestEngine._enforce_remediation_gate(live_mode=True)
    assert flag_path.read_text() == original, "existing flag must be preserved byte-for-byte"


def test_flag_auto_recreated_from_audit_doc(tmp_flag_paths):
    """Card 2893597d acceptance criterion #2: flag is recreated from audit doc."""
    flag_path, audit_path = tmp_flag_paths
    audit_path.write_text("# Ayumi Live Remediation Session Audit\nDate: 2026-06-30\nRemediation validated.\n")
    assert not flag_path.exists()

    ForwardTestEngine._enforce_remediation_gate(live_mode=True)

    assert flag_path.exists(), "flag should be auto-recreated from audit doc"
    body = flag_path.read_text()
    assert str(audit_path) in body
    assert "auto-recreated" in body
    assert "source of truth" in body


def test_runtime_error_when_both_flag_and_audit_missing(tmp_flag_paths):
    """Refuse to start in live mode when there is no source of truth."""
    flag_path, audit_path = tmp_flag_paths
    assert not flag_path.exists()
    assert not audit_path.exists()

    with pytest.raises(RuntimeError) as exc_info:
        ForwardTestEngine._enforce_remediation_gate(live_mode=True)

    msg = str(exc_info.value)
    assert "remediation not validated" in msg
    assert "flag and audit doc both missing" in msg


def test_nested_directory_created(tmp_flag_paths):
    """If the flag's parent directory doesn't exist, auto-recreation creates it."""
    flag_path, audit_path = tmp_flag_paths
    # Place the flag in a non-existent nested directory.
    nested_dir = flag_path.parent / "nested" / "deeper"
    nested_flag = nested_dir / "remediation_validated.flag"

    # Redirect flag path only; keep audit doc at the simpler temp_path.

    fte_module._REMEDIATION_VALIDATED_FLAG = str(nested_flag)
    audit_path.write_text("# Audit\n")

    ForwardTestEngine._enforce_remediation_gate(live_mode=True)

    assert nested_flag.exists(), "flag should be created in nested directory"
    assert nested_dir.is_dir()
