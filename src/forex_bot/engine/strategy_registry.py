from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeAlias

import yaml
from backtest.strategies import ISignalStrategy

logger = logging.getLogger(__name__)

StrategyFactory: TypeAlias = Callable[..., ISignalStrategy]


@dataclass(frozen=True)
class StrategySlot:
    id: str
    strategy_type: str
    symbol: str
    timeframe: str
    params: dict[str, Any]
    min_confidence: float
    enabled: bool = True
    weight: float = 1.0


_BUILTIN_FACTORIES: dict[str, StrategyFactory] = {}


def register_factory(strategy_type: str):
    def decorator(fn: StrategyFactory) -> StrategyFactory:
        _BUILTIN_FACTORIES[strategy_type] = fn
        return fn

    return decorator


def _default_factories() -> dict[str, StrategyFactory]:
    factories = dict(_BUILTIN_FACTORIES)

    if "srmr_plus" not in factories:
        from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy

        def _make_srmr(params: dict[str, Any]) -> ISignalStrategy:
            return SRMRPlusStrategy(config=SRMRPlusConfig(**params))

        factories["srmr_plus"] = _make_srmr

    if "ttc" not in factories:
        from backtest.ttc_forward_strategy import TTCSignalForwardStrategy

        def _make_ttc(params: dict[str, Any]) -> ISignalStrategy:
            return TTCSignalForwardStrategy(
                instrument=params.get("symbol", "XAUUSD"),
                timeframe=params.get("timeframe", "H1"),
                min_confidence=params.get("min_confidence", 0.40),
                rr_ratio=params.get("rr_ratio", 3.0),
            )

        factories["ttc"] = _make_ttc

    return factories


class StrategyRegistry:
    def __init__(self, config_path: str | Path):
        self._config_path = Path(config_path)
        self._slots: list[StrategySlot] = []
        self._factories: dict[str, StrategyFactory] = {}

    def load(self) -> list[StrategySlot]:
        self._factories = _default_factories()
        raw = self._load_yaml()
        self._slots = self._parse_slots(raw)
        logger.info(
            "Loaded %d strategy slots (%d enabled)",
            len(self._slots),
            sum(1 for s in self._slots if s.enabled),
        )
        return self._slots

    def get_enabled(self) -> list[StrategySlot]:
        return [s for s in self._slots if s.enabled]

    def instantiate(self, slot: StrategySlot) -> ISignalStrategy:
        factory = self._factories.get(slot.strategy_type)
        if factory is None:
            raise ValueError(
                f"No factory registered for strategy_type '{slot.strategy_type}'. "
                f"Available: {list(self._factories.keys())}"
            )
        return factory(slot.params)

    def register_factory(self, strategy_type: str, factory: StrategyFactory):
        self._factories[strategy_type] = factory

    def _load_yaml(self) -> dict:
        if not self._config_path.exists():
            raise FileNotFoundError(f"Strategy config not found: {self._config_path}")
        with open(self._config_path) as f:
            return yaml.safe_load(f)

    def _parse_slots(self, raw: dict) -> list[StrategySlot]:
        forward_test = raw.get("forward_test", {})
        strategies = forward_test.get("strategies", [])
        slots = []
        for entry in strategies:
            slot = StrategySlot(
                id=entry["id"],
                strategy_type=entry["type"],
                symbol=entry["symbol"],
                timeframe=entry.get("timeframe", "H1"),
                params=entry.get("params", {}),
                min_confidence=entry.get("min_confidence", 0.40),
                enabled=entry.get("enabled", True),
                weight=entry.get("weight", 1.0),
            )
            slots.append(slot)
        return slots

    @property
    def ftmo_config(self) -> dict:
        raw = self._load_yaml()
        return raw.get("forward_test", {}).get("ftmo", {})

    @property
    def account_balance(self) -> float:
        raw = self._load_yaml()
        return raw.get("forward_test", {}).get("account_balance", 100000.0)
