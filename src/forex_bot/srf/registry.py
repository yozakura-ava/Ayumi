"""SRF Strategy Registry — decorator-based registration with search spaces."""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class StrategyRegistration:
    name: str
    version: str
    module_path: str
    status: str  # "draft", "production", "retired"
    strategy_class: type
    search_space: dict[str, tuple] = field(default_factory=dict)
    default_params: dict[str, Any] = field(default_factory=dict)


# ── Global registry ──────────────────────────────────────────────────────

_REGISTRY: dict[str, StrategyRegistration] = {}


def register_strategy(
    name: str,
    *,
    version: str = "1.0",
    status: str = "production",
    default_params: dict | None = None,
):
    """Decorator that registers a strategy class in the SRF registry.

    Usage:
        @register_strategy("srmr_plus", version="1.0", status="production")
        class SRMRPlusStrategy:
            @staticmethod
            def search_space():
                return {"rsi_long_level": (15.0, 48.0), ...}
    """

    def decorator(cls: type) -> type:
        space = {}
        if hasattr(cls, "search_space"):
            space = cls.search_space()

        _REGISTRY[name] = StrategyRegistration(
            name=name,
            version=version,
            status=status,
            module_path=f"{cls.__module__}.{cls.__name__}",
            strategy_class=cls,
            search_space=space,
            default_params=default_params or {},
        )
        logger.info("Registered strategy: %s v%s [%s]", name, version, status)
        return cls

    return decorator


def get_strategy(name: str) -> StrategyRegistration | None:
    """Retrieve a registered strategy by name."""
    return _REGISTRY.get(name)


def list_strategies(*, status: str | None = None) -> list[StrategyRegistration]:
    """List all registered strategies, optionally filtered by status."""
    strategies = list(_REGISTRY.values())
    if status:
        strategies = [s for s in strategies if s.status == status]
    return strategies


def discover_strategies(package: str = "strategies") -> None:
    """Auto-discover and register strategies from a package directory.

    Imports all modules in the package so decorators fire.
    """
    try:
        pkg = importlib.import_module(package)
    except ImportError:
        logger.warning("Strategy package '%s' not found", package)
        return

    if not hasattr(pkg, "__path__"):
        # Single module, not a package
        return

    for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
        full_name = f"{package}.{modname}"
        try:
            importlib.import_module(full_name)
        except Exception as exc:
            logger.debug("Skipping %s: %s", full_name, exc)

    logger.info("Discovery complete: %d strategies registered", len(_REGISTRY))


# ── Auto-discovery fallback ─────────────────────────────────────────────

# Modules in the strategies package that are not strategies
_SKIP_MODULES = frozenset({"__init__", "registry", "test_canary"})


def _is_zero_arg_constructible(cls: type) -> bool:
    """Check if a class can be instantiated with no arguments.

    Returns True only when every parameter of ``__init__`` (after ``self``)
    has a default value. Classes with required constructor arguments are
    excluded so the SRF walk-forward runner can call them with zero args.
    """
    try:
        sig = inspect.signature(cls.__init__)
        params = sig.parameters
        # Skip 'self'
        non_self = [p for name, p in params.items() if name != "self"]
        # All params must have defaults
        return all(p.default is not inspect.Parameter.empty for p in non_self)
    except (ValueError, TypeError):
        return False


def _auto_discover_strategy_classes(
    package: str = "strategies",
) -> dict[str, type]:
    """Scan ``package`` for strategy classes without decorator registration.

    Finds classes that exhibit a strategy-like interface (``evaluate`` or
    ``on_bar`` method) and returns them keyed by module name.
    """
    result: dict[str, type] = {}

    try:
        pkg = importlib.import_module(package)
    except ImportError:
        logger.warning("Package '%s' not found for auto-discovery", package)
        return result

    if not hasattr(pkg, "__path__"):
        return result

    for _importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname in _SKIP_MODULES:
            continue

        full_name = f"{package}.{modname}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as exc:
            logger.debug("Skipping %s: %s", full_name, exc)
            continue

        # Find the primary strategy class: first class defined in this
        # module (not imported) that has an ``evaluate`` or ``on_bar``
        # method and is not a Config dataclass.
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if not isinstance(obj, type):
                continue
            if getattr(obj, "__module__", "") != full_name:
                continue
            if attr_name.endswith("Config"):
                continue
            if hasattr(obj, "evaluate") or hasattr(obj, "on_bar"):
                if not _is_zero_arg_constructible(obj):
                    logger.debug("Skipping %s — not zero-arg constructible", modname)
                    continue
                result[modname] = obj
                break

    return result


# ── Factory discovery ───────────────────────────────────────────────────


def _make_zero_arg_factory(cls: type) -> Callable:
    """Wrap a strategy class for walk-forward runner compatibility.

    The walk-forward runner checks param count and calls factory(train_bars)
    when params > 0. Strategy constructors expect (config, ...) not (train_bars).
    This wrapper ensures factory() is always called with no args.
    """
    return lambda: cls()


def get_strategy_factories() -> dict[str, Callable]:
    """Return a mapping of strategy name → factory callable.

    Each value is a callable (typically the strategy class itself) that
    produces a strategy instance suitable for ``StrategyRunner.run()`` and
    walk-forward validation.

    Discovery order:
      1. Decorator-based registry (strategies using ``@register_strategy``)
      2. Auto-discovery fallback (scans ``strategies.*`` for classes with
         an ``evaluate`` or ``on_bar`` method)

    Returns all production strategies discoverable in the ``strategies``
    package (currently 17).
    """
    # Ensure decorator-based discovery has run
    if not _REGISTRY:
        discover_strategies("strategies")

    factories: dict[str, Callable] = {}

    # Tier 1: decorator-registered strategies
    for name, reg in _REGISTRY.items():
        if reg.status == "production":
            if _is_zero_arg_constructible(reg.strategy_class):
                factories[name] = _make_zero_arg_factory(reg.strategy_class)
            else:
                logger.warning("Skipping strategy '%s' — not zero-arg constructible", name)

    # Tier 2: auto-discovery for strategies not using decorators
    auto = _auto_discover_strategy_classes("strategies")
    for name, cls in auto.items():
        if name not in factories:
            factories[name] = _make_zero_arg_factory(cls)

    logger.info("Strategy factories resolved: %d entries", len(factories))
    return factories
