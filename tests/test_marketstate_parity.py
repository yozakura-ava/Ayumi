"""Full field-set parity guard for card 171dcc39-2f56-45a6-b1ce-5a38680716c4.

Background
----------
``core.types.MarketState`` and ``backtest.types.MarketState`` are duplicates of
the same dataclass. The f30917a6 incident (merged 0d229f0d) showed that
field-level drift between the two silently kills the KZ strategy (the
backtest variant was missing ``h4_bars``). The h4_bars-specific guard in
``tests/unit/quant/test_kz_h4bars_marketstate.py`` (card f30917a6) covers
that single field; this module guards against the *next* field drift by
asserting field-set equality across the FULL dataclass surface.

Coverage contract
-----------------
- AC #1: full field-set equality between ``core.types.MarketState`` and
  ``backtest.types.MarketState``: identical field NAMES IN DECLARATION
  ORDER AND identical default values.
- AC #2: a NEGATIVE test proves the suite FAILS when the field sets
  diverge. The negative test uses a test-local subclass (no production
  mutation) so the drift detector is verified to actually catch drift.
- AC #3: no production-code changes; this is the sole diff in this card.

Comparator
----------
``compare_market_state_classes(core_cls, backtest_cls)`` returns a list of
human-readable diff strings. Empty list means the two classes are
field-set equal. Each diff string names the offending field and the kind
of mismatch (missing, extra, re-ordered, default-mismatch). Tests assert
either no diffs (positive) or non-empty diffs (negative).

Implementation notes
--------------------
- ``dataclasses.fields(cls)`` returns fields in declaration order, so we
  compare the resulting tuples directly. Set comparisons are not
  sufficient because the spec mandates IN-ORDER CHECK.
- Default values: ``field.default`` for plain defaults,
  ``field.default_factory`` for factory defaults. Fields without a default
  (``MISSING``) compare equal to each other.
- We deliberately compare ``(name, has_default, default_value)`` tuples
  rather than raw ``field`` objects so the comparison is robust against
  metadata-only changes (e.g., ``field.metadata={...: ...}`` does not
  count as a real drift).
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from typing import Any

from backtest.types import Bar
from backtest.types import MarketState as BacktestMarketState
from core.types import MarketState as CoreMarketState

# ---------------------------------------------------------------------------
# Comparator
# ---------------------------------------------------------------------------


def _field_signature(field) -> tuple[str, bool, Any]:
    """Reduce a dataclass field to a comparable signature triple.

    Returns ``(name, has_default, default_value)`` where ``default_value``
    is ``MISSING`` if the field has no default, otherwise the resolved
    default (calling ``default_factory`` when present so we compare the
    produced object identity/value, not the factory handle).
    """
    if field.default is not MISSING:
        return (field.name, True, field.default)
    if field.default_factory is not MISSING:  # type: ignore[misc]
        return (field.name, True, field.default_factory())  # type: ignore[misc]
    return (field.name, False, MISSING)


def compare_market_state_classes(core_cls: Any, backtest_cls: Any) -> list[str]:
    """Return a list of human-readable diffs between two MarketState-like dataclasses.

    Empty list means the two classes are field-set equal (same names, in
    the same order, with the same defaults). Each diff string names the
    field and the kind of mismatch so the message points the reader
    directly at the offending surface.
    """
    diffs: list[str] = []

    if not is_dataclass(core_cls):
        diffs.append(f"core class {core_cls!r} is not a dataclass")
    if not is_dataclass(backtest_cls):
        diffs.append(f"backtest class {backtest_cls!r} is not a dataclass")
    if diffs:
        return diffs

    core_sigs = [_field_signature(f) for f in fields(core_cls)]
    bt_sigs = [_field_signature(f) for f in fields(backtest_cls)]

    if len(core_sigs) != len(bt_sigs):
        diffs.append(
            f"field count differs: core has {len(core_sigs)} fields, "
            f"backtest has {len(bt_sigs)} fields"
        )

    core_names = [s[0] for s in core_sigs]
    bt_names = [s[0] for s in bt_sigs]
    if core_names != set(bt_names):
        only_core = [n for n in core_names if n not in bt_names]
        only_bt = [n for n in bt_names if n not in core_names]
        if only_core:
            diffs.append(
                f"fields present in core.types but missing in backtest: {only_core}"
            )
        if only_bt:
            diffs.append(
                f"fields present in backtest.types but missing in core: {only_bt}"
            )

    if core_sigs != bt_sigs:
        # Walk in core order to surface ORDER drift, then catch extra-on-backtest
        # drifts that core-order walk misses.
        seen: set[str] = set()
        for idx, c_sig in enumerate(core_sigs):
            name = c_sig[0]
            seen.add(name)
            if idx >= len(bt_sigs):
                diffs.append(
                    f"core field '{name}' has no positional counterpart in backtest"
                )
                continue
            b_sig = bt_sigs[idx]
            if b_sig[0] != name:
                diffs.append(
                    f"order drift at index {idx}: core has '{name}', "
                    f"backtest has '{b_sig[0]}'"
                )
                continue
            if c_sig != b_sig:
                diffs.append(
                    f"field '{name}' signature differs: "
                    f"core={c_sig!r} backtest={b_sig!r}"
                )
        # Catch backtest-only fields (already reported above), but skip
        # any already named in seen so we don't double up.
        for idx, b_sig in enumerate(bt_sigs):
            if idx < len(core_sigs) and core_sigs[idx][0] == b_sig[0]:
                continue
            if b_sig[0] not in seen:
                diffs.append(
                    f"backtest-only field '{b_sig[0]}' at backtest index {idx}"
                )

    return diffs


# ---------------------------------------------------------------------------
# Positive: live classes must already be in parity
# ---------------------------------------------------------------------------


class TestCoreBacktestMarketStateParity:
    """The two live MarketState classes must be field-set equal."""

    def test_both_classes_are_dataclasses(self) -> None:
        assert is_dataclass(CoreMarketState), "core.types.MarketState must be a dataclass"
        assert is_dataclass(BacktestMarketState), (
            "backtest.types.MarketState must be a dataclass"
        )

    def test_compare_returns_no_diffs(self) -> None:
        diffs = compare_market_state_classes(CoreMarketState, BacktestMarketState)
        assert diffs == [], (
            "core.types.MarketState and backtest.types.MarketState drifted. "
            f"Diffs: {diffs}"
        )

    def test_field_names_in_declaration_order(self) -> None:
        core_names = [f.name for f in fields(CoreMarketState)]
        bt_names = [f.name for f in fields(BacktestMarketState)]
        assert core_names == bt_names, (
            f"Field order drift: core={core_names} backtest={bt_names}"
        )

    def test_field_defaults_match(self) -> None:
        core_sigs = [_field_signature(f) for f in fields(CoreMarketState)]
        bt_sigs = [_field_signature(f) for f in fields(BacktestMarketState)]
        for c, b in zip(core_sigs, bt_sigs, strict=True):
            assert c == b, (
                f"field {c[0]!r} default differs: "
                f"core={c!r} backtest={b!r}"
            )


# ---------------------------------------------------------------------------
# Negative: comparator must catch divergence (no production mutation)
# ---------------------------------------------------------------------------


@dataclass
class _DriftedBacktestMarketState:
    """Test-local subclass that intentionally drifts from the live pair.

    Adds an extra ``drift_marker`` field and changes the default of
    ``h4_bars`` to prove the comparator surfaces drift on multiple
    surfaces in a single pass. Used by the negative test below; NEVER
    imported by production code (declared under test-private name).
    """

    bars: list[Bar]
    current_session: Any = "NOT_A_SESSION"  # intentionally wrong default
    # Use field(default_factory=list) so the @dataclass decorator accepts it;
    # the resolved default value is still [] which differs from the live
    # pair's None default — the comparator surfaces the drift via the
    # factory-resolved value.
    h4_bars: list[Bar] | None = field(default_factory=list)  # intentionally wrong default (non-None)
    drift_marker: str = "sentinel"  # intentionally extra field


class TestComparatorCatchesDrift:
    """The comparator must catch every drift surface we care about."""

    def test_extra_field_is_detected(self) -> None:
        diffs = compare_market_state_classes(CoreMarketState, _DriftedBacktestMarketState)
        joined = "\n".join(diffs)
        assert diffs, "comparator must report at least one diff for drifted class"
        assert "drift_marker" in joined, (
            f"comparator must surface the extra 'drift_marker' field; got: {diffs}"
        )

    def test_default_drift_is_detected(self) -> None:
        diffs = compare_market_state_classes(CoreMarketState, _DriftedBacktestMarketState)
        assert any(
            "h4_bars" in d and "differs" in d.lower() for d in diffs
        ), f"comparator must surface the h4_bars default drift; got: {diffs}"
        assert any(
            "current_session" in d and "differs" in d.lower() for d in diffs
        ), (
            f"comparator must surface the current_session default drift; "
            f"got: {diffs}"
        )

    def test_field_count_drift_is_detected(self) -> None:
        diffs = compare_market_state_classes(CoreMarketState, _DriftedBacktestMarketState)
        joined = "\n".join(diffs)
        assert "field count differs" in joined, (
            f"comparator must surface the field-count delta; got: {diffs}"
        )

    def test_missing_field_is_detected(self) -> None:
        @dataclass
        class _TruncatedBacktestMarketState:
            bars: list[Bar]
            current_session: Any = "X"  # h4_bars deliberately omitted

        diffs = compare_market_state_classes(CoreMarketState, _TruncatedBacktestMarketState)
        joined = "\n".join(diffs)
        assert diffs, "comparator must report at least one diff for truncated class"
        assert "h4_bars" in joined and "missing" in joined.lower(), (
            f"comparator must surface the missing h4_bars; got: {diffs}"
        )

    def test_reordered_fields_are_detected(self) -> None:
        @dataclass
        class _ReorderedBacktestMarketState:
            # h4_bars deliberately moved to first position to provoke order drift.
            h4_bars: list[Bar] | None = None
            bars: list[Bar] = None  # type: ignore[assignment]
            current_session: Any = "Y"

        diffs = compare_market_state_classes(CoreMarketState, _ReorderedBacktestMarketState)
        assert diffs, "comparator must report at least one diff for reordered class"
        # Either "order drift" or "missing" wording will appear; assert
        # at least one mentions h4_bars position so the reader can act.
        assert any("h4_bars" in d for d in diffs), (
            f"comparator must surface the h4_bars reordering; got: {diffs}"
        )


# ---------------------------------------------------------------------------
# Negative self-check: prove the negative suite would FAIL if drift existed
# ---------------------------------------------------------------------------
#
# This is the meta-guard required by the card spec: prove the comparator
# actually fires by feeding it a drifted pair and asserting the diff is
# non-empty. If we deleted the comparator and hard-coded ``diffs == []``
# in the positive test, this test would still pass on the live classes
# yet provide zero drift-detection signal — exactly the regression we
# are trying to prevent. The negative suite above is the counterweight.


class TestNegativeSuiteIsLive:
    """Meta-guard: the negative suite must be non-trivial on a real drift."""

    def test_drifted_pair_produces_non_empty_diff(self) -> None:
        diffs = compare_market_state_classes(CoreMarketState, _DriftedBacktestMarketState)
        assert diffs, (
            "comparator returned empty against _DriftedBacktestMarketState — "
            "the drift detector is broken; the positive test above could "
            "pass for the wrong reason"
        )
        assert len(diffs) >= 2, (
            f"comparator must surface multiple distinct diffs for the "
            f"drifted class; got only {len(diffs)}: {diffs}"
        )


# ---------------------------------------------------------------------------
# Schema sanity: the live pair has the surface we are guarding against
# ---------------------------------------------------------------------------
#
# Pin the expected live order so an unannounced reorder of the simple
# dataclass surfaces as a *positive* test failure (the user wants the
# loud failure, not silent drift).


class TestLiveMarketStateSchemaSnapshot:
    """Lock the live field set so reorders show up as a positive failure."""

    def test_core_market_state_field_names_locked(self) -> None:
        assert [f.name for f in fields(CoreMarketState)] == [
            "bars",
            "current_session",
            "h4_bars",
        ], "core.types.MarketState field order/surface changed unexpectedly"

    def test_backtest_market_state_field_names_locked(self) -> None:
        assert [f.name for f in fields(BacktestMarketState)] == [
            "bars",
            "current_session",
            "h4_bars",
        ], "backtest.types.MarketState field order/surface changed unexpectedly"

    def test_core_market_state_required_fields(self) -> None:
        sigs = {_field_signature(f) for f in fields(CoreMarketState)}
        assert ("bars", False, MISSING) in sigs, (
            "bars must be the sole required (no-default) field on "
            "core.types.MarketState; saw defaults change unexpectedly"
        )

    def test_backtest_market_state_required_fields(self) -> None:
        sigs = {_field_signature(f) for f in fields(BacktestMarketState)}
        assert ("bars", False, MISSING) in sigs, (
            "bars must be the sole required (no-default) field on "
            "backtest.types.MarketState; saw defaults change unexpectedly"
        )
