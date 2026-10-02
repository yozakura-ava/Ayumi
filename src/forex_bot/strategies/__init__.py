"""Strategy package — registry and strategy discovery."""

from strategies.registry import StrategyConfig, StrategyRegistry, default_registry

__all__ = [
    "StrategyRegistry",
    "StrategyConfig",
    "default_registry",
    "get_default_registry",
]


def get_default_registry() -> StrategyRegistry:
    """Return a fresh registry pre-loaded with all known strategies."""
    return default_registry()
