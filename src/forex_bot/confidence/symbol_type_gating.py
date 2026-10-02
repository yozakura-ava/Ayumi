"""Symbol-type gating for the confidence scoring engine.

Routes symbols to the correct detector stack based on their
``SymbolType`` classification.  Crypto-native detectors (OI, Funding,
Liquidations) are used for ``crypto_perp`` symbols unchanged.
Forex symbols route to substitute detectors with different cadence
and signal interpretation per SRB-AYUMI-008 §4.1.

Integration
-----------
``SymbolTypeGate`` is designed to be added to the ``ConfidenceEngine``
gate pipeline via ``engine.add_gate()``.  It does NOT modify the
existing gate pipeline — it is an additive gate that inspects the
symbol's type and returns metadata about which detector stack should
be consulted by downstream consumers.

Usage
-----
::

    from confidence.symbol_type_gating import SymbolTypeGate
    from models.instrument import classify_symbol

    gate = SymbolTypeGate()
    ctx = {
        "symbol": "EURUSD",
        "symbol_type": classify_symbol("EURUSD"),
    }
    result = gate.check(ctx)
    # result.gate_name == "symbol_type_gate"
    # result.passed is True
    # result.metadata["detectors"] == ["cot_positioning", "rate_differential", "order_flow_proxy"]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from confidence.gates import GateCheck
from models.instrument import (
    DEFAULT_INSTRUMENTS,
    DETECTOR_STACK,
    Instrument,
    SymbolType,
    classify_symbol,
)


@dataclass
class SymbolTypeRouting:
    """Routing decision produced by the symbol-type gate.

    Attributes
    ----------
    symbol : str
        The trading symbol that was classified.
    symbol_type : SymbolType
        Resolved asset-class tag.
    detectors : list[str]
        Detector names that should be active for this symbol type.
    is_crypto : bool
        Convenience flag — True if symbol routes to the crypto detector stack.
    is_forex : bool
        Convenience flag — True if symbol routes to a forex detector stack.
    """

    symbol: str
    symbol_type: SymbolType
    detectors: list[str] = field(default_factory=list)
    is_crypto: bool = False
    is_forex: bool = False


class SymbolTypeGate:
    """Confidence gate that classifies symbols and routes to detectors.

    This gate does NOT reject signals — it always passes and attaches
    routing metadata.  Downstream consumers (confidence engine, scoring
    layer) read the routing to decide which detector modules to consult.

    Parameters
    ----------
    instrument_registry : dict[str, Instrument], optional
        Mapping of symbol → Instrument.  If not provided, uses
        ``DEFAULT_INSTRUMENTS`` from ``models.instrument``.  Symbols
        not in the registry are classified heuristically via
        ``classify_symbol()``.
    """

    def __init__(
        self,
        instrument_registry: Optional[dict[str, Instrument]] = None,
    ) -> None:
        self._registry = instrument_registry if instrument_registry is not None else DEFAULT_INSTRUMENTS

    def check(self, ctx: dict[str, Any]) -> GateCheck:
        """Classify the symbol and return routing metadata.

        Parameters
        ----------
        ctx : dict
            Gate context.  Must contain ``"symbol"``.  May contain
            ``"symbol_type"`` (a ``SymbolType``) to override
            classification.

        Returns
        -------
        GateCheck
            Always passes (``passed=True``).  Routing info is attached
            via ``GateCheck.reason`` (human-readable) and can be
            retrieved in full via :meth:`route`.
        """
        symbol: str = ctx.get("symbol", "")
        explicit_type = ctx.get("symbol_type")

        # Resolve symbol type: explicit override → registry → heuristic
        if isinstance(explicit_type, SymbolType):
            symbol_type = explicit_type
        elif symbol in self._registry:
            symbol_type = self._registry[symbol].symbol_type
        else:
            symbol_type = classify_symbol(symbol)

        detectors = DETECTOR_STACK.get(symbol_type, [])
        is_crypto = symbol_type in (SymbolType.crypto_perp, SymbolType.crypto_spot)
        is_forex = symbol_type in (
            SymbolType.forex_major,
            SymbolType.forex_cross,
            SymbolType.forex_exotic,
        )

        stack_label = "crypto" if is_crypto else ("forex" if is_forex else "other")
        reason = f"symbol_type={symbol_type.value}, stack={stack_label}, detectors={detectors}"

        return GateCheck(
            gate_name="symbol_type_gate",
            passed=True,
            reason=reason,
            boost=0.0,
        )

    def route(self, ctx: dict[str, Any]) -> SymbolTypeRouting:
        """Return the full routing decision for a symbol.

        This is the primary API for downstream consumers that need
        the detector list.  ``check()`` returns a ``GateCheck`` for
        pipeline compatibility; ``route()`` returns the structured
        routing data.

        Parameters
        ----------
        ctx : dict
            Gate context with ``"symbol"`` key.

        Returns
        -------
        SymbolTypeRouting
        """
        symbol: str = ctx.get("symbol", "")
        explicit_type = ctx.get("symbol_type")

        if isinstance(explicit_type, SymbolType):
            symbol_type = explicit_type
        elif symbol in self._registry:
            symbol_type = self._registry[symbol].symbol_type
        else:
            symbol_type = classify_symbol(symbol)

        detectors = DETECTOR_STACK.get(symbol_type, [])

        return SymbolTypeRouting(
            symbol=symbol,
            symbol_type=symbol_type,
            detectors=detectors,
            is_crypto=symbol_type in (SymbolType.crypto_perp, SymbolType.crypto_spot),
            is_forex=symbol_type
            in (
                SymbolType.forex_major,
                SymbolType.forex_cross,
                SymbolType.forex_exotic,
            ),
        )


def get_detector_stack(symbol_type: SymbolType) -> list[str]:
    """Return the list of detector names for a given symbol type.

    Convenience function for callers that already have a ``SymbolType``
    and just need the detector list.

    Parameters
    ----------
    symbol_type : SymbolType
        The asset-class tag.

    Returns
    -------
    list[str]
        Detector names (may be empty for unknown types).
    """
    return DETECTOR_STACK.get(symbol_type, [])
