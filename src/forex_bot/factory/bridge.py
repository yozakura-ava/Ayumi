"""Template → ``ISignalStrategy`` bridge (§3.3 of the Strategy-Factory spec).

This module is the thin glue between the :class:`StrategyTemplate` contract
and the existing ``src/forex_bot/strategies/registry.py`` strategy classes.

Rule of thumb (spec §3.3):
    Templates do NOT re-implement signal logic.  They (1) parametrize
    existing ``ISignalStrategy`` classes, (2) map Optuna-sampled params to
    the strategy's config dataclass, (3) re-use the existing backtest
    infrastructure.

For SFA-1 the bridge ships:

* :class:`BridgeError` — single exception type for all bridge failures
* :func:`build_strategy_from_template` — single-cell bridge
  (template + params + pair → ``ISignalStrategy``)
* :func:`build_strategies_for_registry` — multi-cell bridge: walks the
  existing ``strategies.registry`` and wires every active strategy through
  the bridge using the supplied ``template_factory``.
* :data:`REGISTRY_STRATEGY_BUILDERS` — mapping ``strategy_id → default
  builder`` for the 15 entries pre-loaded by ``strategies.registry``.
* :class:`RegistryStrategyTemplate` — a thin :class:`StrategyTemplate`
  subclass that wraps an existing ``StrategyConfig`` and produces its
  default ``ISignalStrategy`` instance via the registry builders.

The bridge is intentionally **wiring-only**: it does not invent signal
logic, validation logic, or new Optuna search spaces.  Concrete
:paramclass:`StrategyTemplate` subclasses for each archetype (momentum /
mean_reversion / breakout / trend_following / session_based) land in
SFA-2.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

# Local imports — avoid heavy strategy modules at package import time so a
# `from forex_bot.factory import …` stays cheap.
from backtest.strategies.isignal_strategy import ISignalStrategy
from strategies.registry import StrategyConfig, StrategyRegistry

from forex_bot.factory.template import (
    ARCHETYPE_AFFINITY,
    REGIME_LABELS,
    ParamSpec,
    StrategyTemplate,
)


class BridgeError(RuntimeError):
    """Raised when the template → ``ISignalStrategy`` bridge fails."""


# ---------------------------------------------------------------------------
# Bridge primitives
# ---------------------------------------------------------------------------


def _looks_like_strategy(obj: Any) -> bool:
    """Duck-type check for the strategy contract (``name`` + ``evaluate``).

    Some registry strategies are pre-ISignalStrategy (they predate the formal
    interface) and only expose the same ``name`` + ``evaluate`` surface.  The
    bridge accepts both flavours so SFA-1 can wire the existing 15-entry
    registry through unchanged (acceptance criterion for this card).
    """
    cls = type(obj)
    # ``name`` may be a ``@property`` descriptor on the class, or a plain
    # attribute on the instance.  Either satisfies the contract.
    has_name = isinstance(getattr(cls, "name", None), property) or hasattr(obj, "name")
    has_evaluate = callable(getattr(obj, "evaluate", None))
    return bool(has_name and has_evaluate)


# Public alias — exposed for downstream tests / callers that need the same
# duck-type check the bridge applies.
looks_like_strategy = _looks_like_strategy


def build_strategy_from_template(
    template: StrategyTemplate,
    params: Mapping[str, Any],
    pair: str,
) -> Any:
    """Bridge core: validate params, call ``template.build_strategy``, duck-type check.

    Parameters
    ----------
    template
        The :class:`StrategyTemplate` instance that owns the parameter space
        and the build rule.
    params
        A mapping of ``{param_name: value}``.  It MUST match
        ``template.param_space`` exactly (validated via
        :meth:`StrategyTemplate.validate_params`).
    pair
        The symbol the strategy will trade (e.g. ``"EURUSD"``).  Passed
        through to ``template.build_strategy``.

    Returns
    -------
    StrategyLike
        The instantiated strategy — either an
        :class:`backtest.strategies.isignal_strategy.ISignalStrategy` subclass
        or any duck-typed object with a ``name`` + ``evaluate`` surface.

    Raises
    ------
    BridgeError
        If ``template`` is not a :class:`StrategyTemplate`, if ``params``
        fails validation, or if ``template.build_strategy`` does not return
        a strategy-like object (has both ``name`` and ``evaluate``).
    """
    if not isinstance(template, StrategyTemplate):
        raise BridgeError(
            f"template must be a StrategyTemplate, got {type(template).__name__}"
        )
    try:
        validated = template.validate_params(params)
    except ValueError as exc:
        raise BridgeError(
            f"template {template.archetype_id!r} rejected params {dict(params)!r}: {exc}"
        ) from exc
    strategy = template.build_strategy(validated, pair)
    if not _looks_like_strategy(strategy):
        raise BridgeError(
            f"template {template.archetype_id!r} build_strategy returned "
            f"{type(strategy).__name__}; expected ISignalStrategy-like "
            "(needs .name and .evaluate())"
        )
    return strategy


def build_strategies_for_registry(
    registry: StrategyRegistry,
    template_factory: Callable[[StrategyConfig], StrategyTemplate],
    *,
    params_per_strategy: Mapping[str, Mapping[str, Any]] | None = None,
    pair: str | None = None,
) -> dict[str, ISignalStrategy]:
    """Walk ``registry`` and wire every active strategy through the bridge.

    Parameters
    ----------
    registry
        The existing :class:`strategies.registry.StrategyRegistry` (use
        :func:`strategies.registry.default_registry` for the canonical
        pre-loaded set).
    template_factory
        Callable that maps a :class:`StrategyConfig` to a
        :class:`StrategyTemplate`.  Defaults to :class:`RegistryStrategyTemplate`
        (use a partial / lambda to bind a different one).
    params_per_strategy
        Optional mapping ``strategy_id → params dict`` for strategies with
        non-empty param spaces.  Defaults to empty params (identity build).
    pair
        Optional override symbol.  When ``None`` the strategy's first
        registered symbol is used (uppercased).

    Returns
    -------
    dict[str, ISignalStrategy]
        ``strategy_id → built ISignalStrategy`` for every active entry in
        the registry.

    Raises
    ------
    BridgeError
        Propagated from :func:`build_strategy_from_template` on the first
        failure — the helper stops at the first broken strategy so the
        caller can fix it before proceeding.
    """
    params_per_strategy = params_per_strategy or {}
    out: dict[str, ISignalStrategy] = {}
    for cfg in registry.get_all_active():
        params = dict(params_per_strategy.get(cfg.strategy_id, {}) or {})
        symbols = [s.upper() for s in cfg.symbols]
        if pair is not None:
            sym = pair.upper()
        elif symbols:
            sym = symbols[0]
        else:
            raise BridgeError(
                f"registry strategy {cfg.strategy_id!r} has no symbols and "
                "no explicit pair was provided"
            )
        template = template_factory(cfg)
        out[cfg.strategy_id] = build_strategy_from_template(template, params, sym)
    return out


# ---------------------------------------------------------------------------
# Default builders for the 15 strategies pre-loaded by strategies.registry
# ---------------------------------------------------------------------------


def _make_default_builder(
    cls: type[ISignalStrategy] | None = None,
    *,
    init_kwargs: Mapping[str, Any] | None = None,
) -> Callable[[str], ISignalStrategy]:
    """Build a ``(pair) -> ISignalStrategy`` zero-config builder.

    ``pair`` is currently unused (kept for SFA-2 per-pair optuna sampling
    where builders may need to know which symbol they target).

    ``cls`` may be ``None`` at module-import time (the placeholder pattern
    below) — the lazy resolver in :func:`build_default_registry_strategy`
    patches it before first call.  When ``None`` is still present at call
    time we raise :class:`BridgeError` so the placeholder fails loud
    instead of constructing a ``NoneType`` instance.
    """
    init_kwargs = dict(init_kwargs or {})

    def _builder(_pair: str) -> ISignalStrategy:
        if cls is None:
            raise BridgeError(
                "default builder invoked before class resolution; "
                "strategy_id has no (module, class) mapping"
            )
        return cls(**init_kwargs) if init_kwargs else cls()

    return _builder


def _make_session_breakout_builder(session_name: str) -> Callable[[str], ISignalStrategy]:
    """Builder for the three ``session_breakout_*`` registry entries.

    :class:`SessionBreakoutStrategy` requires a dict config — we provide
    canonical session defaults so the bridge can construct a working
    instance without touching the template.
    """
    from forex_bot.strategies.session_breakout import SessionBreakoutStrategy  # noqa: WPS433

    defaults_by_session: dict[str, dict[str, Any]] = {
        "london": {
            "name": "Session Breakout London",
            "range_start_hour": 7,
            "range_end_hour": 9,
            "trade_start_hour": 9,
            "trade_end_hour": 16,
            "min_range_pips": 15.0,
            "max_range_pips": 80.0,
            "buffer_pips": 3.0,
        },
        "ny": {
            "name": "Session Breakout NY",
            "range_start_hour": 12,
            "range_end_hour": 14,
            "trade_start_hour": 14,
            "trade_end_hour": 21,
            "min_range_pips": 15.0,
            "max_range_pips": 80.0,
            "buffer_pips": 3.0,
        },
        "asian": {
            "name": "Session Breakout Asian",
            "range_start_hour": 0,
            "range_end_hour": 2,
            "trade_start_hour": 2,
            "trade_end_hour": 9,
            "min_range_pips": 12.0,
            "max_range_pips": 70.0,
            "buffer_pips": 3.0,
        },
    }
    config = dict(defaults_by_session[session_name])

    def _builder(_pair: str) -> ISignalStrategy:
        return SessionBreakoutStrategy(config=config)

    return _builder


def _make_mtf_filtered_builder() -> Callable[[str], ISignalStrategy]:
    """Builder for ``mtf_filtered_momentum`` — needs an inner strategy."""
    from forex_bot.strategies.killzone_momentum import KillzoneMomentumStrategy  # noqa: WPS433
    from forex_bot.strategies.mtf_filtered_momentum import (  # noqa: WPS433
        MTFFilteredMomentumStrategy,
    )

    def _builder(_pair: str) -> ISignalStrategy:
        return MTFFilteredMomentumStrategy(inner_strategy=KillzoneMomentumStrategy())

    return _builder


def _make_ict_filtered_builder() -> Callable[[str], ISignalStrategy]:
    """Builder for ``session_range_mr_ict_filtered``."""
    from forex_bot.strategies.session_range_mr_ict_filtered import (  # noqa: WPS433
        SessionRangeMRWithICTFilter,
    )

    def _builder(_pair: str) -> ISignalStrategy:
        return SessionRangeMRWithICTFilter()

    return _builder


def _placeholder_builder(reason: str) -> Callable[[str], ISignalStrategy]:
    """Return a builder that raises :class:`BridgeError` with ``reason``.

    Used for registry entries whose concrete strategy class is not yet
    present in ``forex_bot.strategies``.  The bridge still knows the
    mapping exists (so ``strategy_id`` coverage is provable) but refuses
    to fabricate an instance.
    """
    def _builder(_pair: str) -> ISignalStrategy:
        raise BridgeError(reason)

    return _builder


# Canonical default builders — keys MUST match the ``strategy_id`` values in
# ``strategies.registry.default_registry``.  The mapping is intentionally
# declarative (a plain dict, no hidden magic) so a reviewer can audit
# which strategies the bridge can instantiate today.
REGISTRY_STRATEGY_BUILDERS: dict[str, Callable[[str], ISignalStrategy]] = {
    "srmr_plus": _make_default_builder(None),  # placeholder; patched below
    "bb_rsi_reversion": _make_default_builder(None),  # placeholder
    "killzone_momentum": _make_default_builder(None),
    "momentum": _make_default_builder(None),
    "mtf_filtered_momentum": _make_mtf_filtered_builder(),
    "session_range_mr_ict_filtered": _make_ict_filtered_builder(),
    "usdjpy_d1_trend": _placeholder_builder(
        "no concrete strategy class for strategy_id='usdjpy_d1_trend' "
        "(registry-only entry; class lands in SFA-2)"
    ),
    "session_range_mean_reversion": _make_default_builder(None),
    "volatility_squeeze": _make_default_builder(None),
    "session_breakout_london": _make_session_breakout_builder("london"),
    "session_breakout_ny": _make_session_breakout_builder("ny"),
    "session_breakout_asian": _make_session_breakout_builder("asian"),
    "ttc_xauusd": _make_default_builder(None),
    "donchian_atr_trend_v2": _make_default_builder(None),
    "dual_tf_squeeze_pro": _make_default_builder(None),
}


# Late-bound class lookup so the builders dict above stays import-cheap.
# Each entry is populated on first access by
# ``build_default_registry_strategy``.  We avoid eager ``from … import``
# at module top-level so an unrelated test (e.g. a pure template-contract
# test) does not need every strategy module to import cleanly.
_LAZY_STRATEGY_CLASSES: dict[str, tuple[str, str]] = {
    "srmr_plus": ("forex_bot.strategies.srmr_plus", "SRMRPlusStrategy"),
    "bb_rsi_reversion": ("forex_bot.strategies.bb_rsi_reversion", "BBRSIMeanReversion"),
    "killzone_momentum": ("forex_bot.strategies.killzone_momentum", "KillzoneMomentumStrategy"),
    "momentum": ("forex_bot.strategies.momentum", "DonchianBreakoutStrategy"),
    "session_range_mean_reversion": (
        "forex_bot.strategies.session_range_mean_reversion",
        "SessionRangeMeanReversionStrategy",
    ),
    "volatility_squeeze": ("forex_bot.strategies.volatility_squeeze", "VolatilitySqueezeStrategy"),
    "ttc_xauusd": ("forex_bot.strategies.ttc_xauusd", "TTCXAUUSDStrategy"),
    "donchian_atr_trend_v2": (
        "forex_bot.strategies.donchian_atr_trend_v2",
        "DonchianATRTrendV2Strategy",
    ),
    "dual_tf_squeeze_pro": (
        "forex_bot.strategies.dual_tf_squeeze_pro",
        "DualTFSqueezeProStrategy",
    ),
}


def _resolve_strategy_class(strategy_id: str) -> type:
    """Late-resolve a strategy class for ``strategy_id``.

    No inheritance check — pre-ISignalStrategy strategies are accepted
    (their ``name`` + ``evaluate`` duck-type is validated at the
    bridge-call site, not here).
    """
    if strategy_id not in _LAZY_STRATEGY_CLASSES:
        raise BridgeError(
            f"no lazy class mapping for strategy_id {strategy_id!r}"
        )
    module_name, class_name = _LAZY_STRATEGY_CLASSES[strategy_id]
    import importlib

    module = importlib.import_module(module_name)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise BridgeError(
            f"strategy class {class_name!r} not found in module {module_name!r}"
        )
    return cls


def build_default_registry_strategy(strategy_id: str, pair: str = "EURUSD") -> Any:
    """Instantiate the default strategy registered under ``strategy_id``.

    Looks up the entry in :data:`REGISTRY_STRATEGY_BUILDERS`.  For the
    simple ``_make_default_builder`` entries the underlying strategy class
    is resolved lazily on first call.

    Raises
    ------
    BridgeError
        If no builder is registered for ``strategy_id`` or the placeholder
        path fires.
    """
    if strategy_id not in REGISTRY_STRATEGY_BUILDERS:
        raise BridgeError(
            f"no default builder for strategy_id {strategy_id!r} "
            f"(registered: {sorted(REGISTRY_STRATEGY_BUILDERS)!r})"
        )
    builder = REGISTRY_STRATEGY_BUILDERS[strategy_id]
    # Late-resolve the class ONLY for entries that have a (module, class)
    # mapping.  Placeholders and special builders (session_breakout_*,
    # mtf_filtered_momentum, session_range_mr_ict_filtered) are skipped so
    # their bespoke error path runs on first call.
    if strategy_id in _LAZY_STRATEGY_CLASSES and getattr(builder, "_resolved", None) is None:
        cls = _resolve_strategy_class(strategy_id)
        REGISTRY_STRATEGY_BUILDERS[strategy_id] = _make_default_builder(cls)
        builder = REGISTRY_STRATEGY_BUILDERS[strategy_id]
    return builder(pair)


# ---------------------------------------------------------------------------
# RegistryStrategyTemplate — wraps an existing StrategyConfig
# ---------------------------------------------------------------------------


class RegistryStrategyTemplate(StrategyTemplate):
    """:class:`StrategyTemplate` that wraps an existing ``StrategyConfig``.

    Used by :func:`build_strategies_for_registry` (default ``template_factory``)
    to wire every active strategy in ``strategies.registry`` through the
    bridge without writing per-archetype code in SFA-1.

    Notes
    -----
    * :attr:`param_space` is empty — identity template, no Optuna knobs.
      Real per-archetype templates land in SFA-2.
    * :attr:`regime_affinity` is derived from the config's ``strategy_type``
      via :data:`ARCHETYPE_AFFINITY` (spec §3.1).
    * :meth:`build_strategy` ignores ``params`` and ``pair`` (no
      parametrization in SFA-1) and returns the strategy's default
      ``ISignalStrategy`` instance.
    """

    def __init__(self, config: StrategyConfig) -> None:
        affinity = ARCHETYPE_AFFINITY.get(config.strategy_type, REGIME_LABELS)
        super().__init__(
            archetype_id=config.strategy_type,
            description=config.name,
            default_pairs=tuple(s.upper() for s in config.symbols),
            default_timeframes=tuple(config.timeframes),
            regime_affinity=affinity,
        )
        self.strategy_id: str = config.strategy_id
        self._strategy_config: StrategyConfig = config

    @property
    def param_space(self) -> tuple[ParamSpec, ...]:
        return ()

    def default_params(self) -> dict[str, Any]:
        return {}

    def regime_filter(self) -> tuple[str, ...] | None:
        return self.regime_affinity

    def build_strategy(self, params: Mapping[str, Any], pair: str) -> ISignalStrategy:
        # SFA-1: identity build (no Optuna knobs yet).  Pair is accepted
        # for forward-compatibility with SFA-2 per-pair parametrization.
        _ = params  # explicit unused
        _ = pair
        return build_default_registry_strategy(self.strategy_id)

    @property
    def strategy_config(self) -> StrategyConfig:
        """Underlying :class:`StrategyConfig` (read-only handle)."""
        return self._strategy_config


__all__ = [
    "BridgeError",
    "REGISTRY_STRATEGY_BUILDERS",
    "RegistryStrategyTemplate",
    "build_default_registry_strategy",
    "build_strategies_for_registry",
    "build_strategy_from_template",
    "looks_like_strategy",
]
