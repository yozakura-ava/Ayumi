"""Kill-criteria checker that consolidates spread, news-buffer, and
per-strategy filters into a single ``list[KillCriterion]`` evaluation.

Phase 1c of the Brain + Behavioral Policy Consolidation sprint
(2026-07-10). Phase 1a already shipped
:class:`core.conviction.KillCriterion` (commit 7a14664) and Phase 1b
shipped :class:`policy.behavioral.BehavioralPolicy`. This module is the
signal-rejection half of the policy layer: a list of named checks
(spread too wide, ADX outside regime, session outside window, etc.)
that the live trade loop and backtest engine run before sizing.

Council decision (2026-07-10, Kaito / Nora / Ren / Sora) — Nora's
proposal: global floor (code-enforced) plus per-strategy YAML
overrides. The global criteria (``spread``, ``macro_event_buffer``)
are always evaluated; per-strategy criteria (``min_confluence``,
``adx_range``, ``session_window``) only fire when the strategy
config provides them. A signal is killed iff ANY criterion triggers.

This module is pure: no I/O, no logger side-effects, no time-of-day
dependencies beyond what the caller passes in via ``context``. Same
inputs ⇒ same outputs. Safe to unit-test and to call from the live
trade loop or the backtest engine without environment plumbing.
"""

from __future__ import annotations

from core.conviction import KillCriterion

__all__ = ["KillCriteriaChecker"]


# ---------------------------------------------------------------------------
# Defaults — council-approved Phase 1c global floor (2026-07-10)
# ---------------------------------------------------------------------------
# Global max spread in basis points. Matches the SpreadGate default
# (``default_max_spread = 2.0`` in ``confidence.gates.GateConfig``)
# but expressed in basis points instead of pips so the same number
# means the same thing regardless of the broker's pip convention.
DEFAULT_MAX_SPREAD_BPS: float = 2.0

# Macro-event buffer stub — always passes until a calendar feed is
# integrated. The number itself is not used today; we keep it in the
# config so callers wiring Phase 2 can pass it without changing the
# constructor signature.
DEFAULT_MACRO_EVENT_BUFFER_MINUTES: int = 30


class KillCriteriaChecker:
    """Evaluate a list of kill criteria against a signal context.

    Two layers of criteria, evaluated in this order:

    1. **Global** (always checked, regardless of strategy config):
       - ``spread`` — rejects if ``context['spread_bps']`` exceeds
         ``max_spread_bps`` from ``global_config`` (default 2.0).
       - ``macro_event_buffer`` — stub that always passes with
         evidence ``"News feed not integrated"``. Present in every
         result list so downstream consumers see a stable shape.

    2. **Per-strategy** (only when ``strategy_config`` supplies the
       key):
       - ``min_confluence`` — rejects if ``context['confluence_score']``
         is below the configured minimum.
       - ``adx_range`` — rejects if ``context['adx']`` falls outside
         the closed interval ``[lo, hi]`` from the config.
       - ``session_window`` — rejects if ``context['hour_utc']`` falls
         outside the UTC window ``[start, end)`` from the config, with
         midnight wrap-around handled the same way as
         :class:`confidence.gates.SessionGate`.

    Parameters
    ----------
    global_config:
        Optional dict of global overrides. Recognized keys:
          - ``max_spread_bps`` (float, default
            :data:`DEFAULT_MAX_SPREAD_BPS` = 2.0)
          - ``macro_event_buffer_minutes`` (int, default
            :data:`DEFAULT_MACRO_EVENT_BUFFER_MINUTES` = 30; stub, not
            enforced yet)
    strategy_config:
        Optional dict of per-strategy overrides loaded from the
        strategy YAML. Recognized keys (each independent — only those
        present are checked):
          - ``min_confluence`` (float)
          - ``adx_range`` (``[lo, hi]`` list/tuple of two floats)
          - ``session_window`` (``[start_hour, end_hour]`` list/tuple
            of two ints in UTC)

    Notes
    -----
    The checker is stateless across calls — every ``check`` is
    independent. Build one instance per (global, strategy) pair at
    startup and reuse it. No caching, no memoization; the cost of
    running the checks is negligible compared to the surrounding
    signal pipeline.
    """

    def __init__(
        self,
        global_config: dict | None = None,
        strategy_config: dict | None = None,
    ):
        cfg = global_config or {}
        self.max_spread_bps: float = float(cfg.get("max_spread_bps", DEFAULT_MAX_SPREAD_BPS))
        # Stored for forward compatibility with Phase 2 calendar
        # integration. Not used in evaluation yet.
        self.macro_event_buffer_minutes: int = int(
            cfg.get("macro_event_buffer_minutes", DEFAULT_MACRO_EVENT_BUFFER_MINUTES)
        )

        # Shallow-copy the strategy config so a caller mutating their
        # own dict afterwards cannot change policy state mid-flight.
        # Values are floats/ints/lists — all immutable or treated as
        # immutable per check.
        self.strategy_config: dict = dict(strategy_config or {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, context: dict) -> list[KillCriterion]:
        """Run every applicable criterion against ``context``.

        Parameters
        ----------
        context:
            Dict with required and optional keys:
              - ``symbol`` (str, required): instrument code, used in
                the spread evidence string (e.g. ``"GBPUSD"``).
              - ``spread_bps`` (float, required): current quoted spread
                in basis points.
              - ``hour_utc`` (int, required): current UTC hour 0-23,
                used by the session-window criterion.
              - ``adx`` (float, optional, default 0.0): latest ADX
                reading; only consulted when ``adx_range`` is set in
                ``strategy_config``.
              - ``confluence_score`` (float, optional, default 0.0):
                confluence score in ``[0.0, 1.0]``; only consulted
                when ``min_confluence`` is set in
                ``strategy_config``.
              - ``strategy_name`` (str, optional): name of the active
                strategy, used in evidence strings. Falls back to
                ``"unknown"`` when absent.

        Returns
        -------
        list[KillCriterion]
            One row per evaluated criterion, in evaluation order:
            ``spread``, ``macro_event_buffer``, then per-strategy
            criteria in ``(min_confluence, adx_range, session_window)``
            order, only including those whose key was present in the
            strategy config. Every row has ``triggered`` set to either
            ``True`` (kill) or ``False`` (pass) — never omitted.
        """
        symbol = str(context.get("symbol", ""))
        spread_bps = float(context.get("spread_bps", 0.0) or 0.0)
        hour_utc = int(context.get("hour_utc", 0) or 0)
        adx = float(context.get("adx", 0.0) or 0.0)
        confluence_score = float(context.get("confluence_score", 0.0) or 0.0)
        strategy_name = str(context.get("strategy_name", "unknown"))

        results: list[KillCriterion] = []

        # --- Global criteria (always checked) ---------------------------
        results.append(self._check_spread(symbol, spread_bps))
        results.append(self._check_macro_event_buffer())

        # --- Per-strategy criteria (only when configured) ---------------
        if "min_confluence" in self.strategy_config:
            results.append(
                self._check_min_confluence(
                    strategy_name,
                    confluence_score,
                    float(self.strategy_config["min_confluence"]),
                )
            )

        if "adx_range" in self.strategy_config:
            adx_lo, adx_hi = self.strategy_config["adx_range"]
            results.append(self._check_adx_range(strategy_name, adx, float(adx_lo), float(adx_hi)))

        if "session_window" in self.strategy_config:
            start_h, end_h = self.strategy_config["session_window"]
            results.append(self._check_session_window(strategy_name, hour_utc, int(start_h), int(end_h)))

        return results

    @staticmethod
    def any_triggered(results: list[KillCriterion]) -> bool:
        """Return True iff any criterion in ``results`` has triggered.

        Convenience helper so callers do not need to write the same
        ``any(...)`` comprehension everywhere. Returns False for an
        empty list (vacuously — nothing triggered).
        """
        return any(c.triggered for c in results)

    # ------------------------------------------------------------------
    # Individual criteria
    # ------------------------------------------------------------------

    def _check_spread(self, symbol: str, spread_bps: float) -> KillCriterion:
        """Reject if ``spread_bps`` exceeds the configured maximum.

        Evidence format on fail:
        ``"Spread {value} bps exceeds max {max} bps for {symbol}"``.
        On pass:
        ``"Spread {value} bps within max {max} bps for {symbol}"``.
        """
        threshold = self.max_spread_bps
        triggered = spread_bps > threshold
        if triggered:
            evidence = f"Spread {spread_bps} bps exceeds max {threshold} bps for {symbol}"
        else:
            evidence = f"Spread {spread_bps} bps within max {threshold} bps for {symbol}"
        return KillCriterion(
            name="spread",
            triggered=triggered,
            value=spread_bps,
            threshold=threshold,
            evidence=evidence,
        )

    def _check_macro_event_buffer(self) -> KillCriterion:
        """Stub for the macro-event blackout window.

        Always passes until a news-calendar feed is integrated
        (Phase 2). The ``value`` and ``threshold`` fields are filled
        with ``0.0`` so downstream aggregation math stays
        well-defined; the human-readable explanation lives in
        ``evidence``.
        """
        return KillCriterion(
            name="macro_event_buffer",
            triggered=False,
            value=0.0,
            threshold=0.0,
            evidence="News feed not integrated",
        )

    @staticmethod
    def _check_min_confluence(
        strategy_name: str,
        confluence_score: float,
        min_confluence: float,
    ) -> KillCriterion:
        """Reject if ``confluence_score`` is below ``min_confluence``.

        Evidence format on fail:
        ``"Confluence {value} below minimum {threshold} for strategy {name}"``.
        On pass:
        ``"Confluence {value} above minimum {threshold} for strategy {name}"``.
        """
        triggered = confluence_score < min_confluence
        if triggered:
            evidence = f"Confluence {confluence_score} below minimum {min_confluence} for strategy {strategy_name}"
        else:
            evidence = f"Confluence {confluence_score} above minimum {min_confluence} for strategy {strategy_name}"
        return KillCriterion(
            name="min_confluence",
            triggered=triggered,
            value=confluence_score,
            threshold=min_confluence,
            evidence=evidence,
        )

    @staticmethod
    def _check_adx_range(
        strategy_name: str,
        adx: float,
        adx_lo: float,
        adx_hi: float,
    ) -> KillCriterion:
        """Reject if ``adx`` is outside the closed interval ``[lo, hi]``.

        Threshold is reported as ``adx_lo`` (the lower bound) since
        :class:`KillCriterion` carries a single threshold field; the
        upper bound appears in the evidence string so logs and
        dashboards see the full range.

        Evidence format on fail:
        ``"ADX {value} below range [{lo}, {hi}] for strategy {name}"``
        or
        ``"ADX {value} above range [{lo}, {hi}] for strategy {name}"``.
        On pass:
        ``"ADX {value} within range [{lo}, {hi}] for strategy {name}"``.
        """
        if adx < adx_lo:
            triggered = True
            evidence = f"ADX {adx} below range [{adx_lo}, {adx_hi}] for strategy {strategy_name}"
        elif adx > adx_hi:
            triggered = True
            evidence = f"ADX {adx} above range [{adx_lo}, {adx_hi}] for strategy {strategy_name}"
        else:
            triggered = False
            evidence = f"ADX {adx} within range [{adx_lo}, {adx_hi}] for strategy {strategy_name}"
        return KillCriterion(
            name="adx_range",
            triggered=triggered,
            value=adx,
            threshold=adx_lo,
            evidence=evidence,
        )

    @staticmethod
    def _check_session_window(
        strategy_name: str,
        hour_utc: int,
        start_h: int,
        end_h: int,
    ) -> KillCriterion:
        """Reject if ``hour_utc`` falls outside ``[start_h, end_h)`` UTC.

        Midnight wrap-around is handled the same way as
        :meth:`confidence.gates.SessionGate._in_range`:

        - Non-wrap (``start <= end``): ``start <= hour < end`` is in.
        - Wrap (``start > end``): ``hour >= start or hour < end`` is in.

        A signal at ``end_h`` is OUTSIDE (half-open interval). The
        threshold field records ``start_h``; the end hour appears in
        the evidence string.

        Evidence format on fail:
        ``"Hour {value} UTC outside window [{start}, {end}] for strategy {name}"``.
        On pass:
        ``"Hour {value} UTC inside window [{start}, {end}] for strategy {name}"``.
        """
        in_window = KillCriteriaChecker._in_window(hour_utc, start_h, end_h)
        triggered = not in_window
        if triggered:
            evidence = f"Hour {hour_utc} UTC outside window [{start_h}, {end_h}] for strategy {strategy_name}"
        else:
            evidence = f"Hour {hour_utc} UTC inside window [{start_h}, {end_h}] for strategy {strategy_name}"
        return KillCriterion(
            name="session_window",
            triggered=triggered,
            value=float(hour_utc),
            threshold=float(start_h),
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _in_window(hour: int, start_h: int, end_h: int) -> bool:
        """Return True iff ``hour`` is inside ``[start_h, end_h)``.

        Handles midnight wrap-around the same way as
        :meth:`confidence.gates.SessionGate._in_range` so the two
        layers stay consistent: a strategy that allows London-NY
        overlap (``[8, 17]``) and one that allows Asia-London
        (``[22, 6]``) both work without a separate code path.

        Examples
        --------
        >>> KillCriteriaChecker._in_window(10, 8, 17)
        True
        >>> KillCriteriaChecker._in_window(23, 8, 17)
        False
        >>> KillCriteriaChecker._in_window(2, 22, 6)   # wrap
        True
        >>> KillCriteriaChecker._in_window(17, 8, 17)   # end is exclusive
        False
        """
        if start_h <= end_h:
            return start_h <= hour < end_h
        # Wrap-around (e.g. 22 -> 6): in if hour >= start OR hour < end.
        return hour >= start_h or hour < end_h
