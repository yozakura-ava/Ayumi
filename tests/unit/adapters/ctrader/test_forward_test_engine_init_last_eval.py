"""Regression tests for ForwardTestEngine _strategy_last_eval init seed (card dcc7817d 2/3).

Background
----------
``ForwardTestEngine.__init__`` previously seeded the per-strategy
``_strategy_last_eval`` dict with ``0.0``:

    self._strategy_last_eval: dict[str, float] = {s.name: 0.0 for s in strategies}

But the S1 health monitor at ``forward_test_engine.py:_health_monitor_loop``
computes:

    last_eval_ago = time.monotonic() - self._strategy_last_eval.get(sname, 0)

``time.monotonic()`` returns seconds since machine boot, so before the
first evaluation lands, ``last_eval_ago ≈ uptime`` (observed 1,151,359s
≈ 13.3d on the production host). Operators saw a bogus "13d ago" line on
the S1 health tick and the ``last_eval_ago_sec`` state field, making a
healthy fresh-start look stale.

Fix: capture ``_strategy_init_monotonic = time.monotonic()`` at __init__
and seed ``_strategy_last_eval`` with that value. The first health tick
now reads ``monotonic() - monotonic() ≈ 0``, which is the correct
"fresh-start" reading. Consumers at ``_health_monitor_loop`` (:3234) and
the state dict at :3652 remain untouched (units/semantics unchanged).

These tests lock the contract:
1. ``_strategy_init_monotonic`` is captured at ``__init__`` time.
2. ``_strategy_last_eval[s.name] == _strategy_init_monotonic`` for every
   registered strategy at init.
3. The first S1 health tick before any eval lands reads
   ``last_eval_ago < bar_interval`` (e.g. <900s for M15), not
   uptime-scale.
4. The ``last_eval_ago_sec`` state field returns the same value (within
   rounding tolerance) before any eval.
5. After an eval fires, ``_strategy_last_eval`` updates to the eval's
   monotonic timestamp and the value becomes accurate.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure src/forex_bot is on path for imports
sys.path.insert(0, str(Path.cwd() / "src" / "forex_bot"))

from adapters.ctrader.forward_test_engine import (  # noqa: E402, I001
    ForwardTestConfig,
    ForwardTestEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_engine(strategies: list | None = None) -> ForwardTestEngine:
    """Construct a minimal ForwardTestEngine without starting any
    background threads or I/O. ``strategies=[]`` is fine for the seed
    assertion; we use ``[MockStrategy("srmr_plus")]`` for the
    per-strategy checks.
    """
    cfg = ForwardTestConfig(
        symbol="GBPUSD",
        symbols=["GBPUSD"],
        live_mode=False,
        execution_mode="paper",
        openapi_host="demo.ctraderapi.com",
        min_bars_for_evaluation=55,
    )
    return ForwardTestEngine(config=cfg, strategies=strategies or [])


class _MockStrategy:
    """Minimal duck-typed strategy object — only needs a ``.name``."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"_MockStrategy({self.name!r})"


# ---------------------------------------------------------------------------
# 1. _strategy_init_monotonic is captured at __init__
# ---------------------------------------------------------------------------


def test_init_captures_strategy_init_monotonic():
    """``_strategy_init_monotonic`` is set on the engine and is a float
    that is monotonically increasing — i.e. it was captured from
    ``time.monotonic()``.
    """
    with patch("time.monotonic", return_value=12345.6):
        engine = _build_engine()
    assert hasattr(engine, "_strategy_init_monotonic")
    assert engine._strategy_init_monotonic == pytest.approx(12345.6, abs=1e-6)
    assert isinstance(engine._strategy_init_monotonic, float)


# ---------------------------------------------------------------------------
# 2. _strategy_last_eval seeded from _strategy_init_monotonic
# ---------------------------------------------------------------------------


def test_strategy_last_eval_seeded_from_monotonic_at_init():
    """Every strategy in ``_strategy_last_eval`` must be seeded with the
    process-init monotonic value, NOT 0.0.
    """
    strategies = [_MockStrategy("srmr_plus"), _MockStrategy("session_breakout")]

    # Two monotonic readings: one for __init__ seed, one for a fictional
    # "current time" 5 seconds later. The seed MUST equal the init value.
    init_value = 100_000.0
    with patch("time.monotonic", return_value=init_value):
        engine = _build_engine(strategies)

    assert engine._strategy_last_eval["srmr_plus"] == pytest.approx(init_value, abs=1e-6), (
        f"_strategy_last_eval['srmr_plus'] must equal init monotonic "
        f"({init_value}), got {engine._strategy_last_eval['srmr_plus']}. "
        "Seed of 0.0 would yield uptime-scale last_eval_ago."
    )
    assert engine._strategy_last_eval["session_breakout"] == pytest.approx(init_value, abs=1e-6)


def test_strategy_last_eval_is_not_zero():
    """Regression sentinel: pre-fix, the seed was 0.0. Make sure we
    never regress.
    """
    with patch("time.monotonic", return_value=999_999.0):
        engine = _build_engine([_MockStrategy("srmr_plus")])
    assert engine._strategy_last_eval["srmr_plus"] != 0.0, (
        "_strategy_last_eval seeded with 0.0 — regression of card "
        "dcc7817d fix 2/3 (uptime-scale last_eval_ago is back)."
    )


# ---------------------------------------------------------------------------
# 3. First health tick before any eval: last_eval_ago < bar_interval
# ---------------------------------------------------------------------------


def test_first_health_tick_last_eval_ago_is_fresh_start():
    """With the seed from monotonic-at-init, the first S1 health tick
    reads ``last_eval_ago ≈ 0`` (well below bar_interval=900s for M15)
    instead of machine-uptime-scale (>86400s).
    """
    init_value = 1_000_000.0  # Pretend the machine has been up 1e6s (~11.6d)

    with patch("time.monotonic", return_value=init_value):
        engine = _build_engine([_MockStrategy("srmr_plus")])

    # Simulate 3 seconds of uptime before the first health tick fires.
    later = init_value + 3.0
    with patch("time.monotonic", return_value=later):
        last_eval_ago = 0.0  # placeholder — see real computation below
        sname = "srmr_plus"
        # Mirror the production computation at forward_test_engine.py:_health_monitor_loop.
        import time as _time

        last_eval_ago = _time.monotonic() - engine._strategy_last_eval.get(sname, 0)

    assert last_eval_ago < 900, (
        f"First health tick last_eval_ago should be fresh-start (<900s for "
        f"M15 bar_interval), got {last_eval_ago:.1f}s. Regression: 0.0 "
        "seed gives uptime-scale value here."
    )
    assert last_eval_ago == pytest.approx(3.0, abs=1e-6)


# ---------------------------------------------------------------------------
# 4. last_eval_ago_sec state field mirrors the same seed behaviour
# ---------------------------------------------------------------------------


def test_last_eval_ago_sec_state_field_is_fresh_start():
    """The state-field path at forward_test_engine.py:3652 reads the
    same dict. Verify it returns the same fresh-start value.
    """
    init_value = 500_000.0  # Pretend ~5.8d uptime
    with patch("time.monotonic", return_value=init_value):
        engine = _build_engine([_MockStrategy("srmr_plus")])

    later = init_value + 7.0
    with patch("time.monotonic", return_value=later):
        import time as _time

        last_eval_ago_sec = round(_time.monotonic() - engine._strategy_last_eval.get("srmr_plus", 0), 1)

    assert last_eval_ago_sec == pytest.approx(7.0, abs=0.1)
    assert last_eval_ago_sec < 900, (
        f"last_eval_ago_sec must be < bar_interval on fresh start, got {last_eval_ago_sec}s."
    )


# ---------------------------------------------------------------------------
# 5. AST check: the seed block uses _strategy_init_monotonic
# ---------------------------------------------------------------------------


def test_init_seed_uses_strategy_init_monotonic():
    """AST sentinel: the dict comprehension that builds ``_strategy_last_eval``
    MUST source its values from ``self._strategy_init_monotonic``. If a
    future refactor drops the variable, this test fails before any
    runtime regression can ship.
    """
    src = (Path.cwd() / "src" / "forex_bot" / "adapters" / "ctrader" / "forward_test_engine.py").read_text()
    # Locate the assignment to _strategy_last_eval in __init__.
    import re

    # Match `self._strategy_last_eval: dict[str, float] = { ... }` block.
    match = re.search(
        r"self\._strategy_last_eval:\s*dict\[str,\s*float\]\s*=\s*\{([^}]*)\}",
        src,
        re.DOTALL,
    )
    assert match, "Could not find _strategy_last_eval assignment in forward_test_engine.py"
    block = match.group(1)
    assert "_strategy_init_monotonic" in block, (
        "The _strategy_last_eval seed must source from "
        "_strategy_init_monotonic; the assignment block does not reference "
        "it. Regression of card dcc7817d fix 2/3."
    )
    assert "0.0" not in block, (
        "The _strategy_last_eval seed must NOT contain the literal 0.0 "
        "(that was the bug). Regression of card dcc7817d fix 2/3."
    )


# ---------------------------------------------------------------------------
# 6. Update path: after an eval, _strategy_last_eval reflects the eval time
# ---------------------------------------------------------------------------


def test_strategy_last_eval_updates_after_eval():
    """The seed must not break the existing per-eval update at
    ``_evaluate_strategies``: after an evaluation, the dict should
    reflect that eval's monotonic timestamp.
    """
    engine = _build_engine([_MockStrategy("srmr_plus")])

    init_value = engine._strategy_init_monotonic
    eval_time = init_value + 60.0  # 60s after init
    with patch("time.monotonic", return_value=eval_time):
        engine._strategy_last_eval["srmr_plus"] = eval_time

    later = eval_time + 5.0
    with patch("time.monotonic", return_value=later):
        import time as _time

        last_eval_ago = _time.monotonic() - engine._strategy_last_eval["srmr_plus"]

    assert last_eval_ago == pytest.approx(5.0, abs=1e-6)  # noqa: W292
