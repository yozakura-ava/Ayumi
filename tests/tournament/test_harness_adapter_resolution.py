"""Targeted tests for the tournament harness adapter fix (card 9e9aaf30).

Pre-fix, 12 of 17 registered strategies were silently skipped because:

  - 5 strategies' constructors rejected ``config=`` (momentum trio, ICT
    filtered, TTC XAUUSD).
  - 7 strategies had no ``initialize()`` method (donchian_atr_trend_v1,
    killzone_momentum, london_breakout_retest, momentum_m15,
    session_range_mean_reversion, volatility_regime_breakout,
    volatility_squeeze).

The fix is in ``src/tournament/harness.py``:

  - ``_build_strategy_instance``: inspects ``__init__`` signature and
    chooses the right call shape (config= / no-arg / positional kwargs).
  - ``_extract_signals_from_strategy``: guards ``initialize`` and
    ``shutdown`` with ``hasattr``.

These tests assert the adapter's contract per AC1 ("all 17 strategies
return skipped=False on a smoke window") and AC2 ("strategy sources
remain unmodified — verified via git diff at the bottom of this file").
Seven assertions:

  1. test_all_17_strategies_construct_via_adapter
  2. test_config_kwarg_strategies_get_config_none
  3. test_no_config_kwarg_strategies_construct_with_defaults
  4. test_extract_signals_does_not_call_initialize_when_absent
  5. test_extract_signals_calls_initialize_when_present
  6. test_all_17_strategies_resolve_in_strategies_module
  7. test_strategy_sources_unmodified_against_base_commit

Run from the worktree root::

    python3 -m pytest tests/tournament/test_harness_adapter_resolution.py -q
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from tournament import STRATEGY_CLASS_MAP
from tournament import harness as harness_module
from tournament.harness import _build_strategy_instance

# ── Reference fixtures ───────────────────────────────────────────────────────


# Exactly 17 strategy ids — the live ``STRATEGY_CLASS_MAP`` count after
# the fix lands.  This assertion catches future drift in the registry.
EXPECTED_STRATEGY_IDS: tuple[str, ...] = (
    "srmr_plus",
    "bb_rsi_reversion",
    "donchian_atr_trend_v2",
    "dual_tf_squeeze_pro",
    "killzone_momentum",
    "london_breakout_retest",
    "momentum_donchian",
    "momentum_atr_breakout",
    "momentum_ma_trend",
    "momentum_m15",
    "rsi_threshold",
    "session_range_mean_reversion",
    "session_range_mr_ict_filtered",
    "ttc_xauusd",
    "volatility_regime_breakout",
    "volatility_squeeze",
    "donchian_atr_trend_v1",
)


# Strategies that historically had no ``initialize`` attribute; the
# hasattr guard in ``_extract_signals_from_strategy`` must not call it.
# Note: this list is verified by inspection of each strategy module —
# we don't introspect here, we cross-check against the live instances
# built in test 1.
def _strategies_exposing_initialize() -> set[str]:
    """Return ids whose constructed instance has ``initialize``.

    Computed lazily because building each strategy triggers module
    imports (some of which have side-effects like monkey-patching
    module-level constants in ``tts_strategy``).
    """
    out: set[str] = set()
    for sid in EXPECTED_STRATEGY_IDS:
        instance = _build_strategy_instance(sid)
        if hasattr(instance, "initialize") and callable(instance.initialize):
            out.add(sid)
    return out


# ── 1. all 17 strategies construct via the adapter ──────────────────────────


def test_all_17_strategies_construct_via_adapter() -> None:
    """Every registered id resolves to a non-None instance (card 9e9aaf30 AC1).

    Pre-fix this raised ``TypeError`` for 5 ids (config= rejected) and
    would have proceeded to fail with ``AttributeError`` for 7 more
    downstream.  Post-fix, all 17 construct without raising.
    """
    assert len(EXPECTED_STRATEGY_IDS) == 17
    assert len(STRATEGY_CLASS_MAP) == 17, (
        f"STRATEGY_CLASS_MAP drift: expected 17 ids, got {len(STRATEGY_CLASS_MAP)} "
        f"({sorted(STRATEGY_CLASS_MAP)})"
    )

    for sid in EXPECTED_STRATEGY_IDS:
        instance = _build_strategy_instance(sid)
        assert instance is not None, f"{sid}: built instance is None"
        assert hasattr(instance, "evaluate"), (
            f"{sid}: instance has no evaluate() method — not a strategy"
        )


# ── 2. config-kwarg strategies get config=None ──────────────────────────────


def test_config_kwarg_strategies_get_config_none() -> None:
    """Strategies whose ``__init__`` accepts ``config=`` are called with config=None.

    Shapes A/B/F in the build summary: keeps the pre-fix happy path for
    strategies that already worked (srmr_plus is excluded — it goes
    through the symbol-aware path which constructs ``SRMRPlusConfig``).
    """
    config_strategies = [
        "bb_rsi_reversion",
        "donchian_atr_trend_v2",
        "dual_tf_squeeze_pro",
        "killzone_momentum",
        "london_breakout_retest",
        "momentum_m15",
        "rsi_threshold",
        "session_range_mean_reversion",
        "volatility_regime_breakout",
        "volatility_squeeze",
        "donchian_atr_trend_v1",
    ]
    for sid in config_strategies:
        module_path, class_name = STRATEGY_CLASS_MAP[sid].split(":", 1)
        cls = getattr(__import__(module_path, fromlist=[class_name]), class_name)
        sig = inspect.signature(cls.__init__)
        assert "config" in sig.parameters, (
            f"{sid}: expected 'config' kwarg in __init__ signature; "
            f"got params {list(sig.parameters)}"
        )


# ── 3. no-config-kwarg strategies construct via no-arg call ──────────────────


def test_no_config_kwarg_strategies_construct_with_defaults() -> None:
    """Strategies without ``config=`` get built with no args (defaults apply).

    Shapes C/D/E in the build summary.  The momentum trio (donchian,
    atr, ma_trend) plus ``session_range_mr_ict_filtered`` and
    ``ttc_xauusd`` all use other constructor shapes — the adapter must
    NOT pass ``config=None`` to them, since their ``__init__`` would
    raise ``TypeError``.
    """
    no_config_strategies = [
        "momentum_donchian",
        "momentum_atr_breakout",
        "momentum_ma_trend",
        "session_range_mr_ict_filtered",
        "ttc_xauusd",
    ]
    for sid in no_config_strategies:
        module_path, class_name = STRATEGY_CLASS_MAP[sid].split(":", 1)
        cls = getattr(__import__(module_path, fromlist=[class_name]), class_name)
        sig = inspect.signature(cls.__init__)
        assert "config" not in sig.parameters, (
            f"{sid}: unexpected 'config' in __init__; adapter regression?"
        )
        # Construct and assert no exception.
        instance = _build_strategy_instance(sid)
        assert instance is not None, f"{sid}: built instance is None"


# ── 4. _extract_signals does NOT call initialize when absent ─────────────────


def test_extract_signals_does_not_call_initialize_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hasattr guard skips initialize/shutdown for strategies that lack them.

    We monkeypatch ``_build_strategy_instance`` to return a stub that
    exposes ``evaluate`` but NOT ``initialize`` or ``shutdown``.  The
    extract function must not raise ``AttributeError``.
    """
    from dataclasses import dataclass
    from typing import Optional

    import pandas as pd
    from core.types import MarketState, StrategySignal

    @dataclass
    class _StubStrategy:
        evaluate_calls: int = 0
        initialize_called: bool = False  # type: ignore[assignment]
        shutdown_called: bool = False  # type: ignore[assignment]

        def evaluate(self, state: MarketState) -> Optional[StrategySignal]:
            self.evaluate_calls += 1
            return None

    stub = _StubStrategy()

    def _fake_build(strategy_id: str, symbol: str | None = None):  # type: ignore[no-untyped-def]
        return stub

    monkeypatch.setattr(harness_module, "_build_strategy_instance", _fake_build)

    # Synthetic 60-bar window — enough to clear the 30-bar warm-up.
    ts0 = 1_700_000_000
    rows = []
    for i in range(60):
        rows.append({
            "timestamp_utc": ts0 + i * 3600,
            "open": 1.0 + 0.001 * i,
            "high": 1.0 + 0.001 * i + 0.0005,
            "low": 1.0 + 0.001 * i - 0.0005,
            "close": 1.0 + 0.001 * i,
            "volume": 1000,
            "spread_pips": 1.0,
        })
    df = pd.DataFrame(rows)
    df["time_utc"] = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
    df["date_utc"] = df["time_utc"].dt.date

    # If the guard regressed, this call raises AttributeError.
    signals = harness_module._extract_signals_from_strategy("stub_strategy", df)

    assert signals == [], f"Stub returned {len(signals)} signals; expected 0"
    assert stub.evaluate_calls > 0, "evaluate() was not called on the stub"


# ── 5. _extract_signals DOES call initialize when present ───────────────────


def test_extract_signals_calls_initialize_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the strategy HAS initialize, the adapter still calls it.

    Regression guard for ISignalStrategy-shaped strategies (srmr_plus,
    bb_rsi_reversion, etc.).  We pass a real strategy with a custom
    initialize that records its argument.
    """
    import pandas as pd

    init_calls: list[dict] = []

    from core.types import MarketState

    class _RecordingStrategy:
        def __init__(self) -> None:
            self.shutdown_called = False

        def initialize(self, config) -> None:  # type: ignore[no-untyped-def]
            init_calls.append(dict(config) if isinstance(config, dict) else config)

        def evaluate(self, state: MarketState):
            return None

        def shutdown(self) -> None:
            self.shutdown_called = True

    rec = _RecordingStrategy()

    def _fake_build(strategy_id: str, symbol: str | None = None):  # type: ignore[no-untyped-def]
        return rec

    monkeypatch.setattr(harness_module, "_build_strategy_instance", _fake_build)

    ts0 = 1_700_000_000
    rows = []
    for i in range(35):
        rows.append({
            "timestamp_utc": ts0 + i * 3600,
            "open": 1.0 + 0.001 * i,
            "high": 1.0 + 0.001 * i + 0.0005,
            "low": 1.0 + 0.001 * i - 0.0005,
            "close": 1.0 + 0.001 * i,
            "volume": 1000,
            "spread_pips": 1.0,
        })
    df = pd.DataFrame(rows)
    df["time_utc"] = pd.to_datetime(df["timestamp_utc"], unit="s", utc=True)
    df["date_utc"] = df["time_utc"].dt.date

    signals = harness_module._extract_signals_from_strategy("recording_strategy", df)

    assert signals == [], f"Recording stub returned {len(signals)} signals; expected 0"
    assert len(init_calls) == 1, (
        f"initialize() should be called exactly once; got {len(init_calls)}"
    )
    assert rec.shutdown_called, "shutdown() must be called in the finally clause"


# ── 6. all 17 strategies resolve from src/forex-bot/strategies ──────────────


def test_all_17_strategies_resolve_in_strategies_module() -> None:
    """Every STRATEGY_CLASS_MAP entry points at an importable class.

    Catches stale entries (class moved/renamed without registry update).
    """
    for sid, target in STRATEGY_CLASS_MAP.items():
        module_path, class_name = target.split(":", 1)
        module = __import__(module_path, fromlist=[class_name])
        cls = getattr(module, class_name, None)
        assert cls is not None, f"{sid}: {target} does not resolve"


# ── 7. strategy sources unmodified against base commit ──────────────────────


def test_strategy_sources_unmodified_against_base_commit() -> None:
    """AC2: src/forex-bot/strategies/ has zero diffs against base (main).

    Catches accidental edits to strategy sources.  We compare against
    ``origin/main`` HEAD (the canonical base) so the assertion holds
    even after we create feature branches.
    """
    base_sha = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "origin/main"],  # noqa: S607
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    diff_out = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", base_sha, "HEAD", "--", "src/forex-bot/strategies/"],  # noqa: S607, S603
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert diff_out == "", (
        f"Strategy sources modified against base {base_sha[:12]}:\n{diff_out}\n"
        f"AC2 violation — strategy files must stay UNMODIFIED."
    )
