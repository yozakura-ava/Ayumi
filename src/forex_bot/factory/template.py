"""Strategy template plug-in contract (§3.2 of the Strategy-Factory spec).

A :class:`StrategyTemplate` is a *plug-in* — it declares:

* an ``archetype_id`` and a free-text ``description``;
* the default pairs / timeframes the template should be evaluated on;
* the regime affinity (subset of ``{TRENDING, CHOPPY, VOLATILE, QUIET}``);
* an Optuna parameter space (:class:`ParamSpec` tuple);
* a :meth:`build_strategy` factory that maps sampled params + a pair to a
  concrete ``ISignalStrategy`` (via :mod:`.bridge`);
* a :meth:`default_params` baseline (used as the "default vs Optuna"
  reference);
* a :meth:`regime_filter` returning the regimes the template is allowed to
  trade in (``None`` = no filter).

SFA-1 ships **only the contract** — concrete templates (momentum / mean
reversion / breakout / trend-following / session-based) land in SFA-2.  The
template base class is generic so the bridge can consume anything a future
archetype throws at it without further changes here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class ParamKind(str, Enum):
    """Optuna sampling kind."""

    INT = "int"
    FLOAT = "float"
    CATEGORICAL = "categorical"


# Regime labels — kept as a single tuple so downstream code can index without
# importing regime-detection code (which would couple the contract to a
# concrete detector).
TRENDING = "TRENDING"
CHOPPY = "CHOPPY"
VOLATILE = "VOLATILE"
QUIET = "QUIET"

REGIME_LABELS: tuple[str, ...] = (TRENDING, CHOPPY, VOLATILE, QUIET)

# Canonical archetype affinities — mirrors §3.1 of the spec.  Templates MAY
# override this with a narrower tuple but the defaults below are the
# council-approved starting point.
ARCHETYPE_AFFINITY: dict[str, tuple[str, ...]] = {
    "momentum": (TRENDING, VOLATILE),
    "mean_reversion": (QUIET, CHOPPY),
    "breakout": (VOLATILE, TRENDING),
    "trend_following": (TRENDING,),
    # ``trend`` is the strategy_type used by the existing
    # ``strategies.registry`` for trend-following entries (donchian_atr_trend_v2,
    # usdjpy_d1_trend).  Alias to ``trend_following`` so the spec §3.1
    # affinity propagates unchanged.
    "trend": (TRENDING,),
    "session_based": REGIME_LABELS,  # regime-agnostic by design
}


@dataclass(frozen=True)
class ParamSpec:
    """One Optuna-searchable parameter.

    Notes
    -----
    * For :attr:`ParamKind.INT` / :attr:`ParamKind.FLOAT` provide ``low`` /
      ``high`` (and optionally ``step``, ``log``).  ``choices`` is ignored.
    * For :attr:`ParamKind.CATEGORICAL` provide ``choices``; ``low`` / ``high``
      are ignored.
    * The dataclass is frozen so a template's parameter space can be hashed /
      compared for equality (handy for caching).
    """

    name: str
    kind: ParamKind
    low: float | None = None
    high: float | None = None
    choices: tuple[Any, ...] | None = None
    step: float | None = None
    log: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ParamSpec.name must be a non-empty string")
        if self.kind in (ParamKind.INT, ParamKind.FLOAT):
            if self.low is None or self.high is None:
                raise ValueError(
                    f"ParamSpec({self.name!r}) of kind={self.kind.value} "
                    "requires low and high bounds"
                )
            if self.low > self.high:
                raise ValueError(
                    f"ParamSpec({self.name!r}) low={self.low} > high={self.high}"
                )
            if self.kind is ParamKind.INT and self.step is not None and self.step <= 0:
                raise ValueError(
                    f"ParamSpec({self.name!r}) step must be positive for INT"
                )
        elif self.kind is ParamKind.CATEGORICAL:
            if not self.choices:
                raise ValueError(
                    f"ParamSpec({self.name!r}) categorical requires non-empty choices"
                )


@dataclass(frozen=True)
class StrategyTemplate(ABC):
    """Plug-in strategy archetype contract.

    Subclasses MUST implement :meth:`param_space`, :meth:`build_strategy`,
    :meth:`default_params`, and :meth:`regime_filter`.  All four are pure —
    they take only the declared inputs and return plain data — so a template
    can be unit-tested without touching Optuna, the backtest engine, or
    market data.
    """

    archetype_id: str
    description: str
    default_pairs: tuple[str, ...]
    default_timeframes: tuple[str, ...]
    regime_affinity: tuple[str, ...]

    def __post_init__(self) -> None:
        # dataclass(frozen=True) + __post_init__ requires object.__setattr__
        # — keep this guard idempotent and dependency-free.
        if not self.archetype_id:
            raise ValueError("archetype_id must be a non-empty string")
        unknown = [r for r in self.regime_affinity if r not in REGIME_LABELS]
        if unknown:
            raise ValueError(
                f"StrategyTemplate({self.archetype_id!r}) has unknown regimes "
                f"{unknown!r}; expected subset of {REGIME_LABELS!r}"
            )
        if not self.default_pairs:
            raise ValueError(
                f"StrategyTemplate({self.archetype_id!r}) default_pairs must be non-empty"
            )
        if not self.default_timeframes:
            raise ValueError(
                f"StrategyTemplate({self.archetype_id!r}) default_timeframes must be non-empty"
            )

    # ------------------------------------------------------------------
    # Required: parameter space + strategy factory
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def param_space(self) -> tuple[ParamSpec, ...]:
        """Optuna-searchable parameter space for this archetype."""

    @abstractmethod
    def build_strategy(self, params: Mapping[str, Any], pair: str) -> Any:
        """Instantiate a concrete ``ISignalStrategy`` for ``pair`` using ``params``.

        Implementations MUST honour the spec §3.3 contract: re-use existing
        strategies from :mod:`forex_bot.strategies.registry` where possible
        and only fall back to a hand-rolled ``ISignalStrategy`` when no
        registry strategy fits the archetype.
        """

    @abstractmethod
    def default_params(self) -> dict[str, Any]:
        """Return a sensible default parameter mapping."""

    @abstractmethod
    def regime_filter(self) -> tuple[str, ...] | None:
        """Return the regimes this template is allowed to trade in.

        ``None`` means "no filter — run in every regime the detector emits".
        A non-empty tuple is treated as a whitelist (only listed regimes are
        traded in).
        """

    # ------------------------------------------------------------------
    # Convenience — derived from the four primitives above
    # ------------------------------------------------------------------

    @property
    def param_names(self) -> tuple[str, ...]:
        """Convenience: ordered tuple of parameter names."""
        return tuple(p.name for p in self.param_space)

    def validate_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate that ``params`` is in ``param_space``; return a coerced copy.

        Used by the bridge to reject Optuna samples that fall outside the
        declared space (defence-in-depth — Optuna already enforces the
        bounds).
        """
        space = {p.name: p for p in self.param_space}
        if set(space) != set(params):
            missing = set(space) - set(params)
            extra = set(params) - set(space)
            raise ValueError(
                f"params for template {self.archetype_id!r} mismatch param_space: "
                f"missing={sorted(missing)!r} extra={sorted(extra)!r}"
            )
        coerced: dict[str, Any] = {}
        for name, value in params.items():
            spec = space[name]
            if spec.kind is ParamKind.INT:
                coerced[name] = int(value)
            elif spec.kind is ParamKind.FLOAT:
                coerced[name] = float(value)
            else:  # categorical
                if value not in spec.choices:
                    raise ValueError(
                        f"param {name!r} value {value!r} not in choices {spec.choices!r}"
                    )
                coerced[name] = value
        return coerced

    # ------------------------------------------------------------------
    # Optuna adapter — lazy, optional
    # ------------------------------------------------------------------

    def to_optuna_spec(self) -> dict[str, Any]:
        """Convenience: map :attr:`param_space` → Optuna ``suggest_*`` spec.

        Returned as a plain dict so a future Optuna runner can iterate it
        without importing Optuna at template-definition time.  The mapping
        is intentionally conservative (no lambdas) — runners are responsible
        for turning each entry into a real ``optuna.Trial.suggest_*`` call.

        Layout::

            {
              "roc_period": {"kind": "int", "low": 5, "high": 30, "step": 1, "log": False},
              "session":    {"kind": "categorical", "choices": ("asia","london","ny")},
              ...
            }
        """
        out: dict[str, Any] = {}
        for spec in self.param_space:
            entry: dict[str, Any] = {"kind": spec.kind.value}
            if spec.kind is ParamKind.INT:
                entry.update(
                    low=spec.low,
                    high=spec.high,
                    step=spec.step if spec.step is not None else 1,
                    log=spec.log,
                )
            elif spec.kind is ParamKind.FLOAT:
                entry.update(
                    low=spec.low,
                    high=spec.high,
                    step=spec.step,
                    log=spec.log,
                )
            else:
                entry["choices"] = tuple(spec.choices) if spec.choices else ()
            out[spec.name] = entry
        return out


__all__ = [
    "ARCHETYPE_AFFINITY",
    "CHOPPY",
    "ParamKind",
    "ParamSpec",
    "QUIET",
    "REGIME_LABELS",
    "StrategyTemplate",
    "TRENDING",
    "VOLATILE",
]