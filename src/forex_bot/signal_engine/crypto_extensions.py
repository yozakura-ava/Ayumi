"""Crypto extension factors for the signal confidence engine (Phase 6).

Card: c8e50067-aa96-4616-87f5-fd4e75d5b872 (CRYPTO-A3)
Sprint: 2026-10-04-crypto-phase-a
Lane: [BUILD][CRYPTO-LANE]

What this module provides
========================

Phase 6 crypto extension factors per signal_confidence_engine-v2.3 §7.2:

* ``oi_signal``        (weight 0.08) — OI increasing in trade direction = 1.0
* ``funding_extreme``  (weight 0.05) — funding at extreme (top/bottom 5%
                                       of 90d range) opposing trade = 1.0
                                       (contrarian)
* ``btc_dominance``    (weight 0.05) — Falling BTC dom + long alt = 1.0,
                                       rising dom + short alt = 1.0

These are added to the existing 14 §7.1 forex boosters (minus the
forex-only ``dxy_correlation``) to form the §7.2 crypto weight table
that sums to 1.17 — normalized to 1.0 via ``CRYPTO_NORMALIZATION_FACTOR``
BEFORE interaction bonuses per the task brief and the spec comment
("normalize first … then bonuses can push above 1.0, capped at 1.0").

Hard constraints (council requirements, 2026-10-04)
====================================================

1. **Same classification source** — factors fire ONLY on
   ``SymbolTypeGate.route(ctx).is_crypto``. Re-imports the same gate
   ``sl_position_sizer`` uses (``confidence.symbol_type_gating``) so
   A1 + A3 cannot drift. Forex symbols get ``CryptoFactorScores``
   with all zeros and ``settlement_vetoed=False`` — the score output
   is byte-identical with/without extensions loaded.
2. **§7.3 normalization BEFORE interaction bonuses** — the divide-by-
   1.17 step runs first, so a max-weight crypto raw of 1.17 becomes
   ~1.0, then the existing 1.08×/1.05× interaction bonuses can push
   above 1.0 and the final cap brings it back to 1.0.
3. **BTCUSDT / ETHUSDT / SOLUSDT only** — other symbols raise
   :class:`UnsupportedCryptoSymbolError`.
4. **Funding settlement-window veto** — Binance settles perp funding
   at 00:00 / 08:00 / 16:00 UTC. The brief asks that we "normalize
   OR veto, don't feed raw" at those boundaries; we VETO (set the
   funding_extreme factor to 0.0 + flip ``settlement_vetoed=True``).
   Default window: ±5 minutes of the boundary. Off-by-default tight
   in tests can drop the window to zero so the behaviour is
   deterministic without clock tricks.
5. **Feed from ``CryptoAdapter`` output** — consumers pass
   ``OpenInterestSnapshot``, ``FundingRateSnapshot`` and (for
   the 90d-range extreme) sequences of past snapshots. The module
   does NO I/O itself — callers wire the adapter. Unit tests mock
   the adapter by passing fake snapshots.
6. **Factor isolation** — for non-crypto routes the public surface
   returns all zeros (no I/O, no side effects). The forex path is
   tested by feeding the SAME candidate through ``ConfluenceScorer``
   directly and through ``apply_crypto_normalization`` and asserting
   the output matches exactly.

Public API
==========

* :func:`is_crypto_route`        — gate-check helper (single source)
* :func:`compute_crypto_factors` — top-level factor computation
* :func:`apply_crypto_normalization` — §7.3 ``raw / 1.17``
* :func:`is_funding_settlement_window` — 00/08/16 UTC veto helper
* :data:`CRYPTO_NORMALIZATION_FACTOR`  — 1.17
* :data:`SUPPORTED_CRYPTO_SYMBOLS`     — ``("BTCUSDT", "ETHUSDT", "SOLUSDT")``
* :data:`CRYPTO_WEIGHTS`               — §7.2 weights for OI/funding/BTC.dom
* :class:`CryptoFactorScores`          — structured result
* :class:`CryptoFactorInputs`          — input bundle for factor compute
* :class:`UnsupportedCryptoSymbolError`
* :class:`SettlementWindowVeto`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Protocol, Sequence, runtime_checkable

# ──────────────────────────────────────────────────────────────────────
# Symbol whitelist (BTCUSDT/ETHUSDT/SOLUSDT only — task constraint 3).
# Mirrors :data:`crypto_adapter.SUPPORTED_SYMBOLS` so the two modules
# stay aligned; the adapter is the gate that enforces this for live
# data, while this module enforces it for factor computation.
# ──────────────────────────────────────────────────────────────────────

SUPPORTED_CRYPTO_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# Map altcoin → "is this NOT BTC". The BTC-dominance factor only
# applies to altcoins: BTC dominance describes BTC's share of the
# market, so for BTCUSDT itself the factor is structurally 0.0.
_BTC_SYMBOL = "BTCUSDT"


# ──────────────────────────────────────────────────────────────────────
# Spec §7.2 crypto weight table (OI/funding/BTC.dom rows).
# Forex-only ``dxy_correlation`` (0.06) is excluded from crypto per
# the spec; the remaining 14 forex boosters already live in
# ``ConfluenceScorer`` and are NOT touched here (this module is
# additive — only the crypto-specific rows live in this file).
# ──────────────────────────────────────────────────────────────────────

WEIGHT_OI_SIGNAL: float = 0.08
WEIGHT_FUNDING_EXTREME: float = 0.05
WEIGHT_BTC_DOMINANCE: float = 0.05

# Sum of crypto-only weights in this module (sanity-checked in tests).
_CRYPTO_WEIGHTS_SUM: float = (
    WEIGHT_OI_SIGNAL + WEIGHT_FUNDING_EXTREME + WEIGHT_BTC_DOMINANCE
)

#: Mapping ``factor_name → weight`` for the three crypto-only factors.
CRYPTO_WEIGHTS: dict[str, float] = {
    "oi_signal": WEIGHT_OI_SIGNAL,
    "funding_extreme": WEIGHT_FUNDING_EXTREME,
    "btc_dominance": WEIGHT_BTC_DOMINANCE,
}


# ──────────────────────────────────────────────────────────────────────
# §7.3 normalization divisor.  Crypto total weight = 1.17 (0.89 forex
# boosters excluding DXY + 0.08 + 0.05 + 0.05 + 0.06 liquidation +
# 0.04 CME gap → 1.17).  This module only feeds the three crypto-only
# rows; the *full* §7.2 total of 1.17 is divided by this number to
# re-normalize the WHOLE crypto base to a 0–1 scale BEFORE bonuses.
# ──────────────────────────────────────────────────────────────────────

CRYPTO_NORMALIZATION_FACTOR: float = 1.17


# ──────────────────────────────────────────────────────────────────────
# Funding settlement-window veto (task constraint 4).
# ──────────────────────────────────────────────────────────────────────

#: Binance perpetual funding settles at these UTC hours (00, 08, 16).
SETTLEMENT_HOURS_UTC: tuple[int, ...] = (0, 8, 16)
#: Default ±-minute veto window around each settlement hour.
DEFAULT_SETTLEMENT_WINDOW_MINUTES: int = 5


# ──────────────────────────────────────────────────────────────────────
# Funding-extreme threshold (task constraint from spec §7.2 row):
# funding in the top or bottom 5% of the 90d range.
# ──────────────────────────────────────────────────────────────────────

FUNDING_EXTREME_PERCENTILE: float = 0.05


# ──────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────


class UnsupportedCryptoSymbolError(ValueError):
    """Raised when a non-whitelisted symbol is passed to factor compute.

    The whitelist is ``BTCUSDT/ETHUSDT/SOLUSDT`` per the task brief.
    """


class SettlementWindowVeto(Exception):
    """Marker raised when a funding snapshot falls in a settlement window.

    Carries the violating ``timestamp`` so callers can log the precise
    moment without re-deriving it.  Not raised for normal use —
    :func:`compute_crypto_factors` catches it and flips
    :attr:`CryptoFactorScores.settlement_vetoed` instead, so callers
    only need to handle the structured result.
    """


# ──────────────────────────────────────────────────────────────────────
# Structured I/O — kept narrow so callers can wire
# :class:`forex_bot.data.crypto_adapter.BinanceCryptoAdapter` directly
# or pass fake snapshots in tests.
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OISnapshotLike:
    """Minimal interface for an OI snapshot fed into the engine.

    Mirrors :class:`forex_bot.data.crypto_adapter.OpenInterestSnapshot`
    but uses duck-typing (attribute access) so we don't take a hard
    import dependency on the adapter module — keeps this module
    standalone and unit-testable without spinning up Binance fakes.
    """

    symbol: str
    time: datetime
    open_interest: float


@dataclass(frozen=True)
class FundingSnapshotLike:
    """Minimal interface for a funding snapshot fed into the engine.

    Mirrors :class:`forex_bot.data.crypto_adapter.FundingRateSnapshot`.
    """

    symbol: str
    time: datetime
    funding_rate: float


@dataclass(frozen=True)
class CryptoFactorInputs:
    """Bundle of inputs required to compute crypto extension factors.

    Attributes
    ----------
    symbol : str
        Symbol the factors are being computed for. Must be one of
        :data:`SUPPORTED_CRYPTO_SYMBOLS` when passed to
        :func:`compute_crypto_factors` for an ``is_crypto`` route.
    direction : str
        Trade direction — ``"long"`` or ``"short"``.
    oi_snapshot : Optional[OISnapshotLike]
        Latest OI snapshot from the adapter. ``None`` ⇒ factor = 0.0
        (callers should also feed it to readiness checks).
    oi_history : Sequence[OISnapshotLike]
        Past OI snapshots oldest-first. Used to derive the OI trend
        (compare latest vs N-bar ago). Empty ⇒ factor = 0.0.
    funding_snapshot : Optional[FundingSnapshotLike]
        Latest funding snapshot from the adapter. ``None`` ⇒ factor
        = 0.0 + ``settlement_vetoed=False``.
    funding_history : Sequence[FundingSnapshotLike]
        Past funding snapshots oldest-first. Used to compute the 90d
        range (top/bottom percentile) for the ``funding_extreme``
        factor. Empty ⇒ factor = 0.0.
    btc_dominance : Optional[float]
        Current BTC dominance as a fraction (e.g. ``0.52`` for 52%).
        ``None`` ⇒ ``btc_dominance`` factor = 0.0.
    btc_dominance_prev : Optional[float]
        Previous BTC dominance snapshot for trend direction. ``None``
        ⇒ factor = 0.0 (cannot determine direction without a delta).
    settlement_window_minutes : int
        ±-minute window around each settlement hour to apply the
        veto. Defaults to :data:`DEFAULT_SETTLEMENT_WINDOW_MINUTES`
        (5 min). Tests pass ``0`` to disable the veto and exercise
        the funding-extreme branch deterministically.
    """

    symbol: str
    direction: str
    oi_snapshot: Optional[OISnapshotLike] = None
    oi_history: Sequence[OISnapshotLike] = field(default_factory=tuple)
    funding_snapshot: Optional[FundingSnapshotLike] = None
    funding_history: Sequence[FundingSnapshotLike] = field(default_factory=tuple)
    btc_dominance: Optional[float] = None
    btc_dominance_prev: Optional[float] = None
    settlement_window_minutes: int = DEFAULT_SETTLEMENT_WINDOW_MINUTES


@dataclass(frozen=True)
class CryptoFactorScores:
    """Result of :func:`compute_crypto_factors`.

    Attributes
    ----------
    symbol : str
        Echo of the input symbol.
    is_crypto_route : bool
        Whether the route was classified as crypto via the shared
        :class:`SymbolTypeGate`. ``False`` ⇒ all scores are 0.0 and
        ``settlement_vetoed`` is ``False`` (forex isolation contract).
    oi_signal : float
        Score in [0.0, 1.0] per spec §7.2.
    funding_extreme : float
        Score in [0.0, 1.0]. 0.0 when vetoed.
    btc_dominance : float
        Score in [0.0, 1.0]. 0.0 for ``BTCUSDT`` (it IS the
        dominance reference) and for non-alt directions.
    settlement_vetoed : bool
        True iff the latest funding snapshot fell inside the
        configured settlement window. ``funding_extreme`` is then
        forced to 0.0 even if the percentile test would have scored
        higher.
    veto_reason : str
        Human-readable explanation of any veto. Empty when no veto.
    raw_weighted_sum : float
        ``oi_signal * 0.08 + funding_extreme * 0.05 + btc_dominance * 0.05``
        — the crypto-only contribution that gets ADDED to the
        forex-boosters raw before §7.3 normalization.
    applied_weights : dict[str, float]
        Echo of the weights used, for downstream logging.
    """

    symbol: str
    is_crypto_route: bool
    oi_signal: float
    funding_extreme: float
    btc_dominance: float
    settlement_vetoed: bool
    veto_reason: str
    raw_weighted_sum: float
    applied_weights: dict[str, float] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Gate helper — single classification source (task constraint 1).
# ──────────────────────────────────────────────────────────────────────


@runtime_checkable
class SymbolTypeGateLike(Protocol):
    """Minimal duck-typed interface for the gate dependency.

    Lets ``is_crypto_route`` / ``compute_crypto_factors`` accept any
    object that exposes ``.route(ctx) -> SymbolTypeRoutingLike`` —
    production code passes :class:`SymbolTypeGate` from
    ``confidence.symbol_type_gating``; tests pass a stub with the
    same surface (see ``TestIsCryptoRoute::test_custom_gate_is_used_when_passed``).
    """

    def route(self, ctx: dict) -> "SymbolTypeRoutingLike": ...


@runtime_checkable
class SymbolTypeRoutingLike(Protocol):
    """Minimal duck-typed interface for a SymbolTypeGate.route() result."""

    is_crypto: bool


def is_crypto_route(
    ctx: dict,
    *,
    gate: Optional[SymbolTypeGateLike] = None,
) -> bool:
    """Return True iff ``ctx`` routes to the crypto detector stack.

    Uses the SAME :class:`SymbolTypeGate` that ``sl_position_sizer``
    imports (``confidence.symbol_type_gating``). The ``gate`` kwarg
    is provided so tests can inject a stub gate without monkey-
    patching the import — production callers pass nothing.

    Parameters
    ----------
    ctx : dict
        Gate context. Must contain ``"symbol"``; may contain an
        explicit ``"symbol_type"`` override.
    gate : optional
        Pre-built gate instance. Defaults to
        ``SymbolTypeGate().route(ctx)`` via the lazy import below.
    """
    if gate is not None:
        routing = gate.route(ctx)
        return bool(routing.is_crypto)

    # Lazy import — keeps this module importable without the
    # confidence stack being available at module-load time.
    from confidence.symbol_type_gating import SymbolTypeGate  # noqa: E402  lazy import

    routing = SymbolTypeGate().route(ctx)
    return bool(routing.is_crypto)


# ──────────────────────────────────────────────────────────────────────
# Funding settlement-window check (task constraint 4).
# ──────────────────────────────────────────────────────────────────────


def is_funding_settlement_window(
    timestamp: datetime,
    *,
    window_minutes: int = DEFAULT_SETTLEMENT_WINDOW_MINUTES,
) -> bool:
    """Return True iff ``timestamp`` falls in a funding settlement window.

    Settlement hours are :data:`SETTLEMENT_HOURS_UTC` (00, 08, 16 UTC).
    A timestamp qualifies if its absolute distance to ANY settlement
    boundary (today's, yesterday's, tomorrow's) is within
    ``window_minutes`` — i.e. 23:55 to 00:05 with the default
    5-minute window straddles midnight, so 23:56 UTC MUST veto
    even though its hour (23) is not in SETTLEMENT_HOURS_UTC.

    Naive datetimes are interpreted as UTC (the adapter emits UTC).
    """
    if not isinstance(timestamp, datetime):
        return False
    # Normalize to UTC. Naive datetimes are treated as UTC (the
    # crypto adapter emits tz-aware UTC; defensive only).
    if timestamp.tzinfo is None:
        ts_utc = timestamp.replace(tzinfo=timezone.utc)
    else:
        ts_utc = timestamp.astimezone(timezone.utc)

    if window_minutes <= 0:
        # 0-minute window ⇒ no veto (used by tests).
        return False

    from datetime import timedelta

    window = timedelta(minutes=window_minutes)
    base_day = ts_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    # Check today + yesterday + tomorrow so cross-midnight windows
    # (23:55–00:05) are caught even though 23 is not a settlement hour.
    for day_offset in (-1, 0, 1):
        day_base = base_day + timedelta(days=day_offset)
        for hour in SETTLEMENT_HOURS_UTC:
            boundary = day_base + timedelta(hours=hour)
            if abs(ts_utc - boundary) <= window:
                return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Factor primitives — pure functions, each testable in isolation.
# ──────────────────────────────────────────────────────────────────────


def _safe_oi_trend(
    oi_snapshot: OISnapshotLike,
    oi_history: Sequence[OISnapshotLike],
) -> Optional[str]:
    """Return ``"up"``, ``"down"``, or ``None`` if undeterminable.

    Trend is computed as ``latest_oi - oi_history[-1]`` when the
    history has at least one earlier snapshot. Strict ``>``/``<``
    (no tolerance) — zero delta returns ``None`` so the factor
    stays neutral rather than guessing.
    """
    if not oi_history:
        return None
    prev = oi_history[-1].open_interest
    cur = oi_snapshot.open_interest
    if cur > prev:
        return "up"
    if cur < prev:
        return "down"
    return None


def _oi_signal_score(
    *,
    direction: str,
    oi_snapshot: Optional[OISnapshotLike],
    oi_history: Sequence[OISnapshotLike],
) -> float:
    """``oi_signal`` factor per spec §7.2.

    "OI increasing in trade direction = 1.0, decreasing = 0.0" —
    direction-relative interpretation:

    * long  + OI up   → 1.0  (longs accumulating → confirms move)
    * short + OI down → 1.0  (shorts liquidating → confirms move)
    * opposite trend  → 0.0
    * flat / unknown  → 0.0  (no signal, don't fake)
    """
    if oi_snapshot is None or not oi_history:
        return 0.0
    if direction not in ("long", "short"):
        return 0.0
    trend = _safe_oi_trend(oi_snapshot, oi_history)
    if trend is None:
        return 0.0
    if direction == "long" and trend == "up":
        return 1.0
    if direction == "short" and trend == "down":
        return 1.0
    return 0.0


def _funding_extreme_score(
    *,
    direction: str,
    funding_snapshot: Optional[FundingSnapshotLike],
    funding_history: Sequence[FundingSnapshotLike],
) -> float:
    """``funding_extreme`` factor per spec §7.2.

    "Funding at extreme (top/bottom 5% of 90d range) opposing trade
    = 1.0 (contrarian)" — when funding is at the top of its 90d
    range (longs paying shorts → overcrowded longs), going SHORT is
    contrarian → 1.0. Conversely at the bottom going LONG is 1.0.

    The settlement-window veto lives in :func:`compute_crypto_factors`
    (which calls us first and then forces zero if vetoed). This
    function returns the pure percentile-based score.
    """
    if funding_snapshot is None:
        return 0.0
    if direction not in ("long", "short"):
        return 0.0

    rates = [s.funding_rate for s in funding_history] + [funding_snapshot.funding_rate]
    # Need a non-trivial range to compute an extreme — fewer than
    # 2 distinct observations means we cannot rank the latest.
    if len(rates) < 2:
        return 0.0

    # Rank-based percentile (not z-score) because funding
    # distributions are heavy-tailed — a few spikes dominate the
    # std, rank is more robust.
    sorted_rates = sorted(rates)
    n = len(sorted_rates)
    rank = sorted_rates.index(funding_snapshot.funding_rate)
    # Top extreme: latest is in the top FUNDING_EXTREME_PERCENTILE
    # of the distribution.  Bottom extreme: latest is in the bottom
    # FUNDING_EXTREME_PERCENTILE.
    top_cutoff = max(0, int(round(n * (1.0 - FUNDING_EXTREME_PERCENTILE))) - 1)
    bottom_cutoff = min(n - 1, int(round(n * FUNDING_EXTREME_PERCENTILE)))

    # latest funding rate dominates the top → extreme long crowding
    # → contrarian score applies when trade is short
    if rank >= top_cutoff and direction == "short":
        return 1.0
    # latest funding rate dominates the bottom → extreme short crowding
    # → contrarian score applies when trade is long
    if rank <= bottom_cutoff and direction == "long":
        return 1.0
    return 0.0


def _btc_dominance_score(
    *,
    symbol: str,
    direction: str,
    btc_dominance: Optional[float],
    btc_dominance_prev: Optional[float],
) -> float:
    """``btc_dominance`` factor per spec §7.2.

    "Falling BTC dom + long alt = 1.0, rising dom + short alt = 1.0" —
    only meaningful for altcoins (ETH, SOL); BTCUSDT itself returns
    0.0 (it IS the dominance reference, so the factor is undefined).

    Direction check is on the SIGN of the delta; flat (no delta) and
    missing data return 0.0 — no fake signal.
    """
    if symbol not in SUPPORTED_CRYPTO_SYMBOLS:
        return 0.0
    if symbol == _BTC_SYMBOL:
        return 0.0  # BTC dominance describes BTC itself
    if direction not in ("long", "short"):
        return 0.0
    if btc_dominance is None or btc_dominance_prev is None:
        return 0.0
    if btc_dominance > btc_dominance_prev and direction == "short":
        return 1.0
    if btc_dominance < btc_dominance_prev and direction == "long":
        return 1.0
    return 0.0


# ──────────────────────────────────────────────────────────────────────
# §7.3 normalization (task constraint 2).
# ──────────────────────────────────────────────────────────────────────


def apply_crypto_normalization(raw_score: float) -> float:
    """Divide ``raw_score`` by :data:`CRYPTO_NORMALIZATION_FACTOR`.

    Implements §7.3 "raw / 1.17" — applied BEFORE interaction bonuses
    per the task brief and the spec comment ("normalize first … then
    bonuses can push above 1.0, capped at 1.0").

    For non-crypto callers this function is a pure arithmetic helper
    (the caller decides whether to apply it). The test suite asserts
    that ``apply_crypto_normalization(x) == x / 1.17`` exactly so
    the §7.3 contract is bit-stable.
    """
    return float(raw_score) / CRYPTO_NORMALIZATION_FACTOR


# ──────────────────────────────────────────────────────────────────────
# Top-level factor compute — gate-checked, structured result.
# ──────────────────────────────────────────────────────────────────────


def compute_crypto_factors(
    ctx: dict,
    inputs: CryptoFactorInputs,
    *,
    gate: Optional[SymbolTypeGateLike] = None,
) -> CryptoFactorScores:
    """Compute the three crypto-only extension factors for ``ctx``.

    Behaviour matrix
    ----------------
    * **Non-crypto route** (``SymbolTypeGate.route(ctx).is_crypto``
      is False) → all scores 0.0, ``settlement_vetoed=False``.
      Forex isolation test relies on this contract.
    * **Crypto route but unsupported symbol** → raises
      :class:`UnsupportedCryptoSymbolError` (task constraint 3).
      We raise rather than silently zero because misclassifying a
      symbol is a serious bug — silent failure here would let
      unvetted symbols slip through confidence scoring.
    * **Crypto route, supported symbol, no snapshots** → all scores
      0.0 (no data, no fake signal). Caller is expected to gate on
      adapter readiness upstream.
    * **Crypto route, supported symbol, funding in settlement window**
      → ``funding_extreme=0.0``, ``settlement_vetoed=True`` with a
      human-readable ``veto_reason``. OI/BTC-dominance factors still
      fire (they don't suffer the funding-window distortion).
    * **Happy path** → scores in [0.0, 1.0]; ``raw_weighted_sum`` is
      the weighted sum of the three crypto-only factors.

    The result is FROZEN — callers can safely cache it for the
    duration of a signal evaluation.
    """
    symbol = inputs.symbol
    is_crypto = is_crypto_route(ctx, gate=gate)

    if not is_crypto:
        # Forex isolation contract: zero scores, no veto, no I/O.
        return CryptoFactorScores(
            symbol=symbol,
            is_crypto_route=False,
            oi_signal=0.0,
            funding_extreme=0.0,
            btc_dominance=0.0,
            settlement_vetoed=False,
            veto_reason="",
            raw_weighted_sum=0.0,
            applied_weights=dict(CRYPTO_WEIGHTS),
        )

    if symbol not in SUPPORTED_CRYPTO_SYMBOLS:
        raise UnsupportedCryptoSymbolError(
            f"crypto_extensions: {symbol!r} not in whitelist "
            f"{SUPPORTED_CRYPTO_SYMBOLS}; A3 supports only these symbols"
        )

    direction = inputs.direction

    oi = _oi_signal_score(
        direction=direction,
        oi_snapshot=inputs.oi_snapshot,
        oi_history=list(inputs.oi_history),
    )

    funding_score = _funding_extreme_score(
        direction=direction,
        funding_snapshot=inputs.funding_snapshot,
        funding_history=list(inputs.funding_history),
    )

    # ── Funding settlement-window veto (task constraint 4) ──────────
    vetoed = False
    veto_reason = ""
    if inputs.funding_snapshot is not None and is_funding_settlement_window(
        inputs.funding_snapshot.time,
        window_minutes=inputs.settlement_window_minutes,
    ):
        funding_score = 0.0
        vetoed = True
        veto_reason = (
            f"funding snapshot at {inputs.funding_snapshot.time.isoformat()} "
            f"falls inside the +/-{inputs.settlement_window_minutes}-minute "
            f"settlement window around {SETTLEMENT_HOURS_UTC} UTC; "
            f"extreme-percentile signal vetoed"
        )

    btc_dom = _btc_dominance_score(
        symbol=symbol,
        direction=direction,
        btc_dominance=inputs.btc_dominance,
        btc_dominance_prev=inputs.btc_dominance_prev,
    )

    raw = (
        oi * WEIGHT_OI_SIGNAL
        + funding_score * WEIGHT_FUNDING_EXTREME
        + btc_dom * WEIGHT_BTC_DOMINANCE
    )

    return CryptoFactorScores(
        symbol=symbol,
        is_crypto_route=True,
        oi_signal=oi,
        funding_extreme=funding_score,
        btc_dominance=btc_dom,
        settlement_vetoed=vetoed,
        veto_reason=veto_reason,
        raw_weighted_sum=raw,
        applied_weights=dict(CRYPTO_WEIGHTS),
    )


# ──────────────────────────────────────────────────────────────────────
# Convenience: convert any adapter snapshot to our duck-typed shapes.
# Lets callers pass live ``OpenInterestSnapshot`` /
# ``FundingRateSnapshot`` from ``forex_bot.data.crypto_adapter``
# without writing adapter code in every call site.
# ──────────────────────────────────────────────────────────────────────


@runtime_checkable
class AdapterOISnapshotLike(Protocol):
    """Duck-typed shape of ``forex_bot.data.crypto_adapter.OpenInterestSnapshot``.

    Avoids a hard import dependency on the adapter (and keeps this
    module importable without the data layer). Production callers
    pass the adapter dataclass directly; tests pass ``SimpleNamespace``
    or a fake dataclass with the same fields.
    """

    symbol: str
    time: datetime
    open_interest: float


@runtime_checkable
class AdapterFundingSnapshotLike(Protocol):
    """Duck-typed shape of ``forex_bot.data.crypto_adapter.FundingRateSnapshot``."""

    symbol: str
    time: datetime
    funding_rate: float


def to_oi_snapshot(snapshot: AdapterOISnapshotLike) -> OISnapshotLike:
    """Convert an adapter ``OpenInterestSnapshot`` (or duck-typed obj) to
    :class:`OISnapshotLike`. Cheap O(1) — just delegates to dataclass
    constructor so consumers don't need to write the adapter bridge.
    """
    return OISnapshotLike(
        symbol=str(snapshot.symbol),
        time=snapshot.time,
        open_interest=float(snapshot.open_interest),
    )


def to_funding_snapshot(snapshot: AdapterFundingSnapshotLike) -> FundingSnapshotLike:
    """Convert an adapter ``FundingRateSnapshot`` (or duck-typed obj) to
    :class:`FundingSnapshotLike`.
    """
    return FundingSnapshotLike(
        symbol=str(snapshot.symbol),
        time=snapshot.time,
        funding_rate=float(snapshot.funding_rate),
    )


def adapter_factors(
    ctx: dict,
    *,
    symbol: str,
    direction: str,
    oi_snapshot: Optional[AdapterOISnapshotLike],
    funding_snapshot: Optional[AdapterFundingSnapshotLike],
    oi_history: Iterable[AdapterOISnapshotLike] = (),
    funding_history: Iterable[AdapterFundingSnapshotLike] = (),
    btc_dominance: Optional[float] = None,
    btc_dominance_prev: Optional[float] = None,
    settlement_window_minutes: int = DEFAULT_SETTLEMENT_WINDOW_MINUTES,
    gate: Optional[SymbolTypeGateLike] = None,
) -> CryptoFactorScores:
    """High-level helper: pull from the adapter, return scores.

    The CRYPTO-A2 adapter exposes ``latest_oi(symbol)`` and
    ``latest_funding(symbol)`` returning ``OpenInterestSnapshot`` /
    ``FundingRateSnapshot`` instances (or ``None`` when not yet
    polled). Pass those values directly; this helper normalizes
    them to the duck-typed shapes and calls
    :func:`compute_crypto_factors`.

    Example
    -------
    ::

        scores = adapter_factors(
            ctx={"symbol": "BTCUSDT"},
            symbol="BTCUSDT",
            direction="long",
            oi_snapshot=adapter.latest_oi("BTCUSDT"),
            funding_snapshot=adapter.latest_funding("BTCUSDT"),
            oi_history=oi_history_window,
            funding_history=funding_history_window,
            btc_dominance=0.52,
            btc_dominance_prev=0.53,
        )
        raw = confluence_raw + scores.raw_weighted_sum
        raw = apply_crypto_normalization(raw)
        # ... apply interaction bonuses, then cap at 1.0
    """
    inputs = CryptoFactorInputs(
        symbol=symbol,
        direction=direction,
        oi_snapshot=to_oi_snapshot(oi_snapshot) if oi_snapshot is not None else None,
        funding_snapshot=(
            to_funding_snapshot(funding_snapshot)
            if funding_snapshot is not None
            else None
        ),
        oi_history=tuple(to_oi_snapshot(s) for s in oi_history),
        funding_history=tuple(to_funding_snapshot(s) for s in funding_history),
        btc_dominance=btc_dominance,
        btc_dominance_prev=btc_dominance_prev,
        settlement_window_minutes=settlement_window_minutes,
    )
    return compute_crypto_factors(ctx, inputs, gate=gate)


__all__ = [
    "SUPPORTED_CRYPTO_SYMBOLS",
    "WEIGHT_OI_SIGNAL",
    "WEIGHT_FUNDING_EXTREME",
    "WEIGHT_BTC_DOMINANCE",
    "CRYPTO_WEIGHTS",
    "CRYPTO_NORMALIZATION_FACTOR",
    "SETTLEMENT_HOURS_UTC",
    "DEFAULT_SETTLEMENT_WINDOW_MINUTES",
    "FUNDING_EXTREME_PERCENTILE",
    "UnsupportedCryptoSymbolError",
    "SettlementWindowVeto",
    "OISnapshotLike",
    "FundingSnapshotLike",
    "CryptoFactorInputs",
    "CryptoFactorScores",
    "SymbolTypeGateLike",
    "SymbolTypeRoutingLike",
    "AdapterOISnapshotLike",
    "AdapterFundingSnapshotLike",
    "is_crypto_route",
    "is_funding_settlement_window",
    "apply_crypto_normalization",
    "compute_crypto_factors",
    "adapter_factors",
    "to_oi_snapshot",
    "to_funding_snapshot",
]
