"""Tests for SRMR+ strategies.yaml config loader (card 25cbea7a).

The forward test launcher previously instantiated SRMRPlusConfig() with
defaults, producing 0 signals for XAUUSD. The Optuna-validated params
(PF=7.16, WR=73.4%) live in src/forex_bot/config/strategies.yaml. These
tests pin down the loader behavior so production wiring stays correct.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_FOREX_SRC = str(Path(__file__).resolve().parent.parent.parent / "src" / "forex_bot")
if _FOREX_SRC not in sys.path:
    sys.path.insert(0, _FOREX_SRC)

from strategies.srmr_plus import (  # noqa: E402  (sys.path tweak above)
    SRMRPlusConfig,
    SRMRPlusStrategy,
    load_srmr_config_from_yaml,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_STRATEGIES_YAML = _REPO_ROOT / "src" / "forex_bot" / "config" / "strategies.yaml"

# Validated values (mirror strategies.yaml:164-183)
_XAUUSD_M15_PARAMS = {
    "atr_period": 21,
    "rsi_period": 12,
    "rsi_long_level": 41.8,
    "rsi_short_level": 62.3,
    "adx_period": 28,
    "adx_max_threshold": 38.4,
    "session_range_min_pips": 176.9,
    "entry_near_extreme_pips": 200.4,
    "hard_cap_sl_pips": 153.7,
    "tp1_rr": 2.59,
    "tp2_rr": 0.53,
    "ema_trend_period": 20,
    "use_same_day_range": True,
    "pip_value": 0.01,
    "dxy_overlay": False,
}


@pytest.fixture(scope="module")
def xauusd_m15_config() -> SRMRPlusConfig:
    """Load the XAUUSD M15 config from the real strategies.yaml."""
    cfg = load_srmr_config_from_yaml(
        "XAUUSD",
        timeframe="M15",
        config_path=_STRATEGIES_YAML,
    )
    assert cfg is not None, "strategies.yaml must define srmr_xauusd_m15"
    return cfg


class TestLoadSrmrConfigFromYaml:
    """Verify the strategies.yaml loader returns the validated config."""

    def test_loads_xauusd_m15_validated_entry(self, xauusd_m15_config):
        """The Optuna-validated srmr_xauusd_m15 params must load."""
        for key, expected in _XAUUSD_M15_PARAMS.items():
            actual = getattr(xauusd_m15_config, key)
            assert actual == pytest.approx(expected), f"srmr_xauusd_m15.{key}: expected {expected}, got {actual}"

    def test_symbol_forced_to_requested_value(self, xauusd_m15_config):
        """``symbol`` must be the requested value, not whatever YAML carries."""
        assert xauusd_m15_config.symbol == "XAUUSD"

    def test_differs_from_defaults(self, xauusd_m15_config):
        """Loaded config must NOT equal SRMRPlusConfig() defaults."""
        defaults = SRMRPlusConfig()
        assert xauusd_m15_config.rsi_long_level != defaults.rsi_long_level
        assert xauusd_m15_config.tp1_rr != defaults.tp1_rr
        assert xauusd_m15_config.hard_cap_sl_pips != defaults.hard_cap_sl_pips

    def test_case_insensitive_symbol_and_timeframe(self):
        """Lowercase inputs must resolve identically to uppercase."""
        upper = load_srmr_config_from_yaml("XAUUSD", timeframe="M15", config_path=_STRATEGIES_YAML)
        lower = load_srmr_config_from_yaml("xauusd", timeframe="m15", config_path=_STRATEGIES_YAML)
        assert upper is not None and lower is not None
        for key in _XAUUSD_M15_PARAMS:
            assert getattr(upper, key) == pytest.approx(getattr(lower, key))

    def test_unknown_symbol_returns_none(self):
        """Symbols with no validated entry must fall back to None (defaults)."""
        cfg = load_srmr_config_from_yaml("BCHUSD", timeframe="M15", config_path=_STRATEGIES_YAML)
        assert cfg is None

    def test_unknown_timeframe_returns_none(self):
        """Symbols with no entry for the requested timeframe must fall back."""
        # XAUUSD has M15, H1, H4 — but not M5
        cfg = load_srmr_config_from_yaml("XAUUSD", timeframe="M5", config_path=_STRATEGIES_YAML)
        assert cfg is None

    def test_disabled_entry_returns_none(self):
        """Disabled entries (e.g. srmr_usdjpy_h1, enabled: false) must return None."""
        cfg = load_srmr_config_from_yaml("USDJPY", timeframe="H1", config_path=_STRATEGIES_YAML)
        assert cfg is None

    def test_nonexistent_yaml_returns_none(self, monkeypatch, caplog):
        """Missing file must not raise; caller should fall back to defaults."""
        # Force the resolver to return a non-existent path so the open()
        # in the helper triggers FileNotFoundError. Without this monkeypatch
        # the auto-discovery fallback would happily find the real YAML.
        from strategies import srmr_plus

        monkeypatch.setattr(
            srmr_plus,
            "_resolve_strategies_yaml_path",
            lambda _cfg: Path("/nonexistent/strategies.yaml"),
        )
        with caplog.at_level(logging.WARNING, logger="strategies.srmr_plus"):
            cfg = load_srmr_config_from_yaml(
                "XAUUSD",
                timeframe="M15",
                config_path="/nonexistent/strategies.yaml",
            )
        assert cfg is None
        assert any(
            "not found" in rec.getMessage().lower() or "fall" in rec.getMessage().lower() for rec in caplog.records
        )

    def test_default_yaml_path_resolves(self):
        """With config_path=None the helper must find strategies.yaml by walking parents."""
        cfg = load_srmr_config_from_yaml("XAUUSD", timeframe="M15")
        assert cfg is not None, "helper failed to auto-discover strategies.yaml — check _resolve_strategies_yaml_path"
        assert cfg.rsi_long_level == pytest.approx(41.8)


class TestSrmrStrategyWithLoadedConfig:
    """End-to-end: SRMRPlusStrategy must accept the loaded config."""

    def test_strategy_instantiates_with_loaded_config(self, xauusd_m15_config):
        """SRMRPlusStrategy(config=<loaded>) must construct cleanly."""
        strat = SRMRPlusStrategy(config=xauusd_m15_config)
        assert strat.name == "SRMR+"
        assert strat.config.rsi_long_level == pytest.approx(41.8)
        assert strat.config.symbol == "XAUUSD"

    def test_loaded_config_enables_validated_signals(self, xauusd_m15_config):
        """Sanity: tp1_rr=2.59 + hard_cap_sl_pips=153.7 are well above the
        class defaults, so the loaded config reflects the validated,
        high-conviction tuning rather than the conservative fallback."""
        defaults = SRMRPlusConfig()
        assert xauusd_m15_config.tp1_rr > defaults.tp1_rr
        assert xauusd_m15_config.hard_cap_sl_pips > defaults.hard_cap_sl_pips
        # XAUUSD-specific wide session range
        assert xauusd_m15_config.session_range_min_pips > 100.0
