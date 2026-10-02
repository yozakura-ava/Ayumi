"""Regression test: BlendForwardTestRunner.start() must always find _persistence (card 238f07a1).

Background
----------
``ayumi-forward-test.service`` crash-looped 5x on 2026-09-11 13:30-13:33 UTC with::

    AttributeError: 'BlendForwardTestRunner' object has no attribute '_persistence'
        at src/forex_bot/forward_test/blend_runner.py:189 in start()

Root cause (diagnosed in-worktree): the service runs from the MAIN tree
(WorkingDirectory=$AYUMI_ROOT). Commit 823e5e29
("fix(forward-test): periodic edge-telemetry state snapshot ...") landed
directly on main at 13:29:29 UTC. During the direct main-tree edit window,
systemd restarts imported blend_runner.py at an intermediate on-disk state
where ``start()`` referenced ``self._persistence`` before the matching
``__init__`` assignment (line 130) had been flushed — an inconsistent
transient, not a defect at any commit. At every commit, ``_persistence``
is assigned in ``__init__`` before ``start()`` uses it.

These tests lock the init-order contract so a future edit that reorders or
drops the ``StatePersistence`` construction fails loudly instead of
crash-looping the live service:

1. ``__init__`` always leaves a non-None ``_persistence`` (a
   ``StatePersistence`` instance bound to the configured ``state_path``).
2. ``start()`` completes without AttributeError and restores sizer balance
   from a state file written by a prior ``save`` (round-trip across a
   simulated process restart).
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKTREE = Path.cwd()
SRC = WORKTREE / "src" / "forex_bot"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from forward_test.blend_runner import BlendForwardTestRunner  # noqa: E402, I001
from risk.state_persistence import StatePersistence  # noqa: E402


def _build_runner(state_path: str) -> BlendForwardTestRunner:
    return BlendForwardTestRunner(
        config={
            "account_balance": 10_000.0,
            "risk_per_trade_pct": 0.005,
            "daily_risk_cap_pct": 0.03,
            "spread_pips": {},
            "state_path": state_path,
        }
    )


def test_init_assigns_persistence_before_start(tmp_path):
    """__init__ must leave a usable _persistence bound to state_path."""
    state_path = str(tmp_path / "risk_state.json")
    runner = _build_runner(state_path)
    assert isinstance(runner._persistence, StatePersistence)
    assert runner._persistence._path == Path(state_path)


def test_start_round_trip_no_attribute_error(tmp_path):
    """start() must not raise AttributeError and must restore saved state.

    Simulates the production crash path: construct → start() after a prior
    process saved state (the exact systemd restart sequence).
    """
    state_path = str(tmp_path / "risk_state.json")

    # First "process": save sizer state.
    first = _build_runner(state_path)
    first._persistence.save(first._sizer)
    assert Path(state_path).exists()

    # Second "process" (systemd restart): fresh construction → start().
    second = _build_runner(state_path)
    second.start()  # must not raise AttributeError
    assert second._balance == second._sizer.account_balance
