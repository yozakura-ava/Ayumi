"""COT Weekly Confidence Signal.

Wraps :class:`~forex_bot.data.cot_fetcher.COTFetcher` into a signal
module that the confidence pipeline can use as a **weekly structural
pre-filter and confidence modifier**.

Design philosophy
-----------------
COT (Commitment of Traders) data is published weekly by the CFTC and
reflects futures-market positioning of non-commercial (speculative)
traders.  It is *not* an entry signal — it is a structural regime
filter:

- When speculator positioning **aligns** with the trade direction,
  confidence gets a small boost (max +0.05).
- When positioning **diverges**, confidence is penalised
  (max −0.05).
- When positioning is **neutral** or data is insufficient, no
  adjustment is applied.

Integration with the confidence engine
---------------------------------------
Two integration points are provided:

1. **COTConfidenceGate** — a soft gate compatible with
   :class:`~confidence.engine.ConfidenceEngine`.  Add via
   ``engine.add_gate(COTConfidenceGate(fetcher))``.  The gate always
   passes unless ``hard_block_on_divergence`` is set, in which case
   extreme divergence blocks the signal.

2. **adjust_confidence()** — a standalone function for manual
   integration: ``adjusted = adjust_confidence(raw, pair, direction)``.

Threshold logic
---------------
The confidence adjustment is derived from the normalised net-position
shift relative to a 4-week lookback average:

  ``shift = (current_net - prior_avg_net) / total_open_interest``
  ``normalised = clamp(shift, -1.0, +1.0)``
  ``adjustment = clamp(normalised * 0.10, -0.05, +0.05)``

A **regime change** (net-long → net-short or vice versa) amplifies
the adjustment by 1.5×, capped at ±0.05.

These thresholds are intentionally conservative — COT is a weekly
structural indicator, not a tactical timing signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from confidence.gates import GateCheck
from data.cot_fetcher import (
    COTDivergenceSignal,
    COTFetcher,
    COTFormat,
)

logger = logging.getLogger(__name__)

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_LOOKBACK_WEEKS: int = 4
DEFAULT_MAX_ADJUSTMENT: float = 0.05
DEFAULT_SHIFT_SCALING: float = 0.10
DEFAULT_REGIME_AMPLIFICATION: float = 1.5

# Pairs supported by the COT signal (maps to CFTC forex futures)
SUPPORTED_PAIRS: frozenset[str] = frozenset(
    {
        "USDJPY",
        "EURUSD",
        "GBPUSD",
        "USDCHF",
        "USDCAD",
        "AUDUSD",
        "NZDUSD",
    }
)

# When True, extreme divergence blocks the signal entirely.
# Default False — COT is advisory, not a hard filter.
DEFAULT_HARD_BLOCK: bool = False

# Strength threshold for hard-blocking (only used when hard_block=True).
# If |strength| exceeds this, the gate fails.
DEFAULT_BLOCK_THRESHOLD: float = 0.75


# ─── Data Classes ────────────────────────────────────────────────────────────


@dataclass
class COTSignalResult:
    """Full result of a COT confidence assessment.

    Attributes:
        pair: The FX pair assessed.
        direction: Trade direction assessed ("long" / "short").
        bias: COT-derived bias ("long" / "short" / "neutral").
        aligned: Whether bias matches direction.
        strength: Normalised positioning shift, −1.0 to +1.0.
        adjustment: Confidence delta applied (−0.05 to +0.05).
        regime_change: Whether a bias flip occurred vs prior weeks.
        signal_date: COT report date used (ISO).
        rationale: Human-readable explanation.
    """

    pair: str
    direction: str
    bias: str
    aligned: bool
    strength: float
    adjustment: float
    regime_change: bool
    signal_date: str
    rationale: str

    @property
    def is_neutral(self) -> bool:
        """True when COT has insufficient data or neutral bias."""
        return self.bias == "neutral"


# ─── Public API ──────────────────────────────────────────────────────────────


def assess_cot(
    pair: str,
    direction: str,
    fetcher: Optional[COTFetcher] = None,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    fmt: COTFormat = COTFormat.LEGACY,
) -> COTSignalResult:
    """Assess COT positioning for a trade signal.

    This is the primary entry point.  It fetches the latest COT data,
    computes the weekly divergence signal, and returns a structured
    result with the confidence adjustment.

    Args:
        pair: FX pair, e.g. ``"USDJPY"``.
        direction: Trade direction: ``"long"`` or ``"short"``.
        fetcher: Pre-configured :class:`COTFetcher`.  A temporary
            instance is created if ``None``.
        lookback_weeks: Weeks of history for trend comparison.
        fmt: COT report format (Legacy or Disaggregated).

    Returns:
        :class:`COTSignalResult` with adjustment and metadata.
    """
    pair = pair.upper()
    direction = direction.lower()

    if pair not in SUPPORTED_PAIRS:
        logger.debug("COT signal: %s not in supported pairs, neutral", pair)
        return COTSignalResult(
            pair=pair,
            direction=direction,
            bias="neutral",
            aligned=False,
            strength=0.0,
            adjustment=0.0,
            regime_change=False,
            signal_date="",
            rationale=f"Pair {pair} not supported by COT signal",
        )

    _fetcher = fetcher or COTFetcher()
    div: COTDivergenceSignal = _fetcher.get_divergence_signal(
        pair,
        lookback_weeks=lookback_weeks,
        fmt=fmt,
    )

    aligned = div.bias == direction
    regime_change = "Regime change" in div.rationale

    # Determine adjustment sign based on alignment
    if div.bias == "neutral":
        adjustment = 0.0
    elif aligned:
        # Trade direction confirmed by COT → boost
        adjustment = abs(div.confidence_adjustment)
    else:
        # Trade direction opposed by COT → penalise
        adjustment = -abs(div.confidence_adjustment)

    return COTSignalResult(
        pair=pair,
        direction=direction,
        bias=div.bias,
        aligned=aligned,
        strength=div.strength,
        adjustment=adjustment,
        regime_change=regime_change,
        signal_date=div.signal_date,
        rationale=div.rationale,
    )


def adjust_confidence(
    raw_confidence: float,
    pair: str,
    direction: str,
    fetcher: Optional[COTFetcher] = None,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
) -> float:
    """Apply COT-based adjustment to a raw confidence score.

    Convenience wrapper around :func:`assess_cot` that returns only
    the adjusted confidence value, clamped to ``[0.0, 1.0]``.

    Args:
        raw_confidence: Strategy confidence (0.0–1.0).
        pair: FX pair.
        direction: ``"long"`` or ``"short"``.
        fetcher: Optional pre-configured fetcher.
        lookback_weeks: Trend comparison window.

    Returns:
        Adjusted confidence in ``[0.0, 1.0]``.
    """
    result = assess_cot(pair, direction, fetcher, lookback_weeks)
    adjusted = raw_confidence + result.adjustment
    return max(0.0, min(1.0, adjusted))


# ─── Confidence Engine Gate ─────────────────────────────────────────────────


class COTConfidenceGate:
    """Soft confidence gate based on weekly COT positioning.

    Integrates with :class:`~confidence.engine.ConfidenceEngine` via
    ``engine.add_gate(COTConfidenceGate(fetcher))``.

    Behaviour:
        - **Aligned** (COT bias == trade direction): passes, small boost.
        - **Neutral** (insufficient data): passes, no boost.
        - **Divergent** (COT bias ≠ trade direction):
            - If ``hard_block_on_divergence=False`` (default): passes,
              no boost (confidence dampening handled by the engine
              via the returned negative boost).
            - If ``hard_block_on_divergence=True`` and
              ``|strength| > block_threshold``: fails, blocks signal.

    The gate's ``boost`` return value can be positive (alignment) or
    negative (divergence) to allow the engine to adjust the final score.
    """

    def __init__(
        self,
        fetcher: COTFetcher,
        lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
        hard_block_on_divergence: bool = DEFAULT_HARD_BLOCK,
        block_threshold: float = DEFAULT_BLOCK_THRESHOLD,
    ) -> None:
        self._fetcher = fetcher
        self._lookback = lookback_weeks
        self._hard_block = hard_block_on_divergence
        self._block_threshold = block_threshold

    @property
    def gate_name(self) -> str:
        return "cot"

    def check(self, ctx: dict[str, Any]) -> GateCheck:
        """Run the COT gate check.

        Expects in ``ctx``:
            - ``symbol``: FX pair (e.g. ``"USDJPY"``).
            - ``direction``: ``"long"`` or ``"short"``.
        """
        symbol = ctx.get("symbol", "")
        direction = ctx.get("direction", "long")

        result = assess_cot(
            pair=symbol,
            direction=direction,
            fetcher=self._fetcher,
            lookback_weeks=self._lookback,
        )

        if result.is_neutral:
            return GateCheck(
                gate_name=self.gate_name,
                passed=True,
                reason=f"COT neutral: {result.rationale}",
                boost=0.0,
            )

        if result.aligned:
            return GateCheck(
                gate_name=self.gate_name,
                passed=True,
                reason=f"COT aligned ({result.bias}, Δ={result.adjustment:+.4f})",
                boost=result.adjustment,
            )

        # Divergent
        if self._hard_block and abs(result.strength) > self._block_threshold:
            return GateCheck(
                gate_name=self.gate_name,
                passed=False,
                reason=(
                    f"COT divergence exceeds block threshold "
                    f"(|{result.strength:.2f}| > {self._block_threshold}): "
                    f"bias={result.bias} vs direction={direction}"
                ),
            )

        # Soft divergence — pass but dampen via negative boost
        return GateCheck(
            gate_name=self.gate_name,
            passed=True,
            reason=f"COT divergent ({result.bias} vs {direction}, Δ={result.adjustment:+.4f})",
            boost=result.adjustment,  # negative value
        )


__all__ = [
    "COTSignalResult",
    "COTConfidenceGate",
    "assess_cot",
    "adjust_confidence",
    "DEFAULT_LOOKBACK_WEEKS",
    "DEFAULT_MAX_ADJUSTMENT",
    "DEFAULT_SHIFT_SCALING",
    "DEFAULT_REGIME_AMPLIFICATION",
    "SUPPORTED_PAIRS",
]
