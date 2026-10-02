#!/usr/bin/env python3
"""Backtest Blend Harness — production-fidelity backtest without cTrader.

Drives the BlendForwardTestEngine evaluation loop using precomputed bars
from DuckDB instead of a live cTrader connection.  Constructs the same
component pipeline as ``launch_blend_forward_test.py`` but skips
``engine.start()`` and manually feeds bars through the evaluation loop.

This enables offline validation of the blend strategy without cTrader
connectivity, market hours, or demo account dependencies.

Usage::

    python3 scripts/backtest_blend_harness.py [options]

Options:
    --start DATE        Backtest start date (YYYY-MM-DD)
    --end DATE          Backtest end date (YYYY-MM-DD)
    --symbol SYM        Symbol to backtest (default: XAUUSD)
    --costs             Apply trading costs (spread, commission, slippage)
    --spread FLOAT      Spread in price units with --costs (default: 2.5)
    --commission FLOAT  Commission per lot USD with --costs (default: 3.5)
    --slippage FLOAT    Slippage in price units with --costs (default: 0.2)
    --db-path PATH      Override DuckDB path (default: data/ayumi_market.duckdb)
    --output PATH       Output JSON results path (default: data/backtest_results.json)
    --verbose           Enable DEBUG logging
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # F821 fix (card 9cdbfd0a): type-only import for the compute_stats annotation;
    # runtime loading is dynamic via importlib (see _load_module below).
    from forward_test.blend_runner import BlendForwardTestRunner

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

logger = logging.getLogger("ayumi.backtest_harness")


# ── Main-worktree root resolver (card e1e32b07) ────────────────────────────
# Card e1e32b07 (2026-09-09): when the harness is imported from a non-primary
# git worktree, ``PROJECT_ROOT`` resolves to the worktree's repo root, so the
# guard's ``LIVE_*`` paths point at the worktree's ``data/`` directory (which
# typically does NOT contain ``forward_test.pid``). The live engine running
# in the MAIN tree therefore slips past the refuse-when-live check by
# construction — the guard is bypassed from any worktree that does not have
# its own ``data/forward_test.pid``.
#
# The fix: resolve a separate ``_GUARD_LIVE_ROOT`` that prefers the MAIN
# worktree (the FIRST entry of ``git worktree list --porcelain``) and
# resolves the LIVE paths against that root. ``PROJECT_ROOT`` itself is
# unchanged so non-guard uses (duckdb path, launcher import) stay on the
# current tree where the harness's own dependencies live.
#
# Fallback policy: ANY failure (git missing, timeout, parse error, missing
# entry, non-existent dir) falls back to ``PROJECT_ROOT`` — i.e. the
# pre-fix behavior. This preserves fail-closed guard semantics (the guard
# still runs against SOME data/ directory; it just may not see the main
# one). The guard's own fail-closed logic remains the safety net.


def _main_worktree_root() -> Path | None:
    """Return the MAIN (primary) worktree's repo root, or ``None`` on failure.

    Runs ``git worktree list --porcelain`` with a short timeout and parses
    the output as BLOCKS (records separated by blank lines; each block
    starts with ``worktree <path>`` and may carry flags ``bare``,
    ``detached``, etc.). Returns the path of the FIRST non-bare,
    non-detached worktree entry — the primary checkout.

    Bare and detached blocks are SKIPPED: in a bare-repo layout, the
    bare entry would otherwise be selected, anchoring LIVE paths to the
    bare repo (which has no ``data/`` directory) and bypassing the
    refuse-when-live guard. Card e1e32b07 REWORK (Rin HIGH finding).

    Any failure (non-zero exit, empty output, parse error, no valid
    non-bare primary entry, path not a directory) returns ``None`` so
    the caller can fall back to ``PROJECT_ROOT``.

    NOTE: This function is deliberately permissive — it never raises. The
    caller decides what fallback to use (here: ``PROJECT_ROOT``). Card
    e1e32b07.
    """
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],  # noqa: S607 — hard-coded binary name, no shell interpolation
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.debug(
            "_main_worktree_root: git worktree list failed (%s); "
            "falling back to PROJECT_ROOT",
            exc,
        )
        return None
    if completed.returncode != 0:
        logger.debug(
            "_main_worktree_root: git worktree list rc=%s stderr=%r; "
            "falling back to PROJECT_ROOT",
            completed.returncode,
            completed.stderr.strip()[:200] if completed.stderr else "",
        )
        return None
    # Porcelain format: BLOCKS separated by blank lines (`\n\n`). Each
    # block starts with `worktree <path>` and may carry flags `bare`,
    # `detached`, etc. We want the FIRST non-bare, non-detached block —
    # the primary worktree's checkout directory.
    #
    # Card e1e32b07 REWORK (Rin HIGH finding): a bare entry MUST be
    # skipped, otherwise in a bare-repo layout the bare repo's path
    # would be selected, anchoring LIVE paths to a directory with no
    # `data/` and bypassing the refuse-when-live guard.
    for block in completed.stdout.split("\n\n"):
        path_line: str | None = None
        bare = False
        detached = False
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == "bare":
                bare = True
            elif stripped == "detached":
                detached = True
            elif stripped.startswith("worktree "):
                path_line = stripped[len("worktree "):].strip()
        if path_line is None:
            # Block has no worktree line (corrupt / unexpected); skip.
            continue
        if bare or detached:
            # Skip bare and detached entries — only the primary
            # non-bare, non-detached worktree anchors LIVE paths.
            continue
        if not path_line:
            return None
        try:
            candidate = Path(path_line)
        except (TypeError, ValueError):
            return None
        if candidate.is_dir():
            return candidate
        return None
    return None


# ── Live state paths (card 758273a7-c3f1-41fa-95dc-7384c260acd7) ───────────
# Card 758273a7 (2026-09-08): the harness previously wrote to these paths,
# overwriting live forward-test state during root-run re-runs and
# contaminating the live engine's startup state. The harness now uses an
# isolated per-run temp directory by default and refuses to run if any of
# these live paths show a contamination signal (active PID or foreign
# ownership).
#
# Card e1e32b07 (2026-09-09): LIVE_* paths are resolved against the MAIN
# worktree's root, not the per-worktree PROJECT_ROOT. The guard must see the
# MAIN tree's ``data/forward_test.pid`` etc. even when the harness runs from
# a non-primary worktree — otherwise the refuse-when-live gate is bypassed
# by construction.
_GUARD_LIVE_ENV_BYPASS = "AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT"


def _resolve_guard_live_root() -> Path:
    """Resolve the root the guard's LIVE paths are anchored to.

    Default: the MAIN (primary) worktree's repo root via
    ``_main_worktree_root()``. On ANY failure, falls back to
    ``PROJECT_ROOT`` (current worktree) — i.e. the pre-fix behavior.

    Bypass: setting ``AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT=1`` forces the
    guard's LIVE paths back onto the current ``PROJECT_ROOT``. This is
    intended ONLY for operator debugging on the main tree itself (where
    ``PROJECT_ROOT`` IS the main root) or for tests that drive the guard
    via monkeypatched ``LIVE_*`` constants. When set, the bypass is
    logged at WARNING level so any unexpected use shows up loudly in
    operator logs. Card e1e32b07.
    """
    if os.environ.get(_GUARD_LIVE_ENV_BYPASS, "").strip() in ("1", "true", "yes"):
        logger.warning(
            "Isolation guard: AYUMI_HARNESS_GUARD_USE_LOCAL_ROOT=1 — LIVE "
            "paths anchored to current PROJECT_ROOT (%s) instead of main "
            "worktree. Card e1e32b07 bypass; use only for tests/main-tree "
            "operator debugging. The refuse-when-live check is operating "
            "on the CURRENT tree only.",
            PROJECT_ROOT,
        )
        return PROJECT_ROOT
    main_root = _main_worktree_root()
    if main_root is None:
        logger.debug(
            "Isolation guard: could not resolve main worktree root; "
            "LIVE paths anchored to PROJECT_ROOT (%s). The guard still "
            "runs (fail-closed) but may not see the main tree's "
            "data/forward_test.pid. Card e1e32b07.",
            PROJECT_ROOT,
        )
        return PROJECT_ROOT
    if main_root == PROJECT_ROOT:
        # Already running from the main tree — no divergence to log.
        return main_root
    logger.info(
        "Isolation guard: LIVE paths anchored to MAIN worktree root %s "
        "(current PROJECT_ROOT=%s). The refuse-when-live check covers "
        "the main tree even when the harness runs from a non-primary "
        "worktree. Card e1e32b07.",
        main_root,
        PROJECT_ROOT,
    )
    return main_root


_GUARD_LIVE_ROOT = _resolve_guard_live_root()
LIVE_FORWARD_TEST_PID = _GUARD_LIVE_ROOT / "data" / "forward_test.pid"
LIVE_KILL_SWITCH_GLOBAL_STATE = _GUARD_LIVE_ROOT / "data" / "kill_switches" / "global.state"
LIVE_RISK_STATE_BLEND = _GUARD_LIVE_ROOT / "data" / "risk_state_blend.json"


class RefuseToRun(RuntimeError):
    """Raised by the isolation guard when a contamination signal is detected.

    Card 758273a7: the harness refuses to start rather than risk writing to
    live forward-test state files. Distinct from RuntimeError so callers
    (including the CLI entrypoint) can surface a clear, actionable error.
    """


# ── Isolation guard (card 758273a7) ─────────────────────────────────────────


def _resolve_state_dir(args: argparse.Namespace) -> Path:
    """Resolve the harness state directory.

    Default: a fresh ``tempfile.mkdtemp(prefix="ayumi_harness_state_")``
    per-run, deleted on success. This directory holds the isolated
    risk_state_blend.json and kill_switches/ subdirectory. The harness
    never writes to ``data/kill_switches/`` or ``data/risk_state_blend.json``
    when this default is used.

    Operators may pass ``--state-dir PATH`` for debugging; the guard then
    enforces strict ownership checks on the live paths regardless.
    """
    if getattr(args, "state_dir", None):
        resolved = Path(args.state_dir).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    tmp = Path(tempfile.mkdtemp(prefix="ayumi_harness_state_"))
    logger.info("Isolation: state dir = %s (auto-cleaned on success)", tmp)
    return tmp


def _cleanup_state_dir(state_dir: Path, keep: bool = False) -> None:
    """Remove the harness state dir on successful exit unless ``keep`` is True.

    Card 758273a7: ensures harness temp state never accumulates on disk.
    On failure paths the directory is preserved so an operator can
    inspect what the harness wrote.
    """
    if keep:
        return
    if not str(state_dir).startswith(tempfile.gettempdir()):
        # Defensive: only auto-clean state dirs we created under tempfile.gettempdir()
        return
    try:
        shutil.rmtree(state_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to clean harness state dir %s: %s", state_dir, exc)


def _verify_isolation_or_refuse(args: argparse.Namespace, state_dir: Path) -> None:
    """Startup guard (card 758273a7).

    Refuses to start the harness when:

      (a) ``data/forward_test.pid`` exists AND the PID responds to
          ``kill -0`` (live engine is active). A stale PID file (process
          gone) is logged and the harness continues.

      (b) Any of the live state files is owned by a different user than
          the invoking user. This catches (i) prior contamination from
          a foreign-user harness run, and (ii) a foreign-user harness
          about to contaminate state again. Bypass with
          ``--allow-foreign-ownership``.

    The guard always runs against the LIVE paths, not ``state_dir``,
    because the bug pattern is harness-writes-touching-live-data.
    """
    allow_foreign = bool(getattr(args, "allow_foreign_ownership", False))

    # (a) live forward_test PID check
    # Rin REWORK (card 758273a7 verdict 4f384bfc, comment 4f384bfc): the
    # pre-rework guard caught PermissionError as STALE (fail-open). That
    # was wrong on two axes:
    #   * PermissionError on read_text() — the file is locked or owned by
    #     a foreign user. That is a SECURITY signal, not a stale marker.
    #   * PermissionError on os.kill(pid, 0) — the process exists but is
    #     not owned by us (EPERM). That is exactly the live engine running
    #     under a foreign user account, which is the contamination pattern
    #     that caused the 2026-09-08 incident.
    # Split the exception handling: read errors and EPERM refuse; only
    # ProcessLookupError (PID is gone) is treated as stale.
    if LIVE_FORWARD_TEST_PID.exists():
        # Read phase — fail closed on any read failure.
        try:
            pid = int(LIVE_FORWARD_TEST_PID.read_text().strip())
        except (ValueError, OSError, PermissionError) as exc:
            raise RefuseToRun(
                f"LIVE forward_test PID file {LIVE_FORWARD_TEST_PID} is "
                f"unreadable ({exc.__class__.__name__}: {exc}). Failing "
                f"closed — refusing to run. Investigate the PID file's "
                f"permissions/ownership before retrying. "
                f"Card 758273a7 rework (Rin verdict 4f384bfc HIGH #3)."
            ) from exc

        # Probe phase — signal 0 returns ESRCH for missing PID, EPERM
        # for foreign-owned alive PID, 0 for our-owned alive PID.
        try:
            os.kill(pid, 0)
            # Probe succeeded without exception — the PID exists and we
            # own it (or it's accessible). Refuse — live engine is active.
            raise RefuseToRun(
                f"LIVE forward_test engine is running (PID {pid} responds "
                f"to signal 0). Harness refuses to start while live engine "
                f"is active to prevent concurrent state writes. Stop the "
                f"live engine or remove {LIVE_FORWARD_TEST_PID} if it is "
                f"a stale marker."
            )
        except ProcessLookupError as exc:
            # PID is genuinely gone — stale marker. Log + continue.
            logger.warning(
                "Stale PID file at %s (PID %d does not exist: %s); "
                "continuing — operator should remove the stale marker.",
                LIVE_FORWARD_TEST_PID,
                pid,
                exc,
            )
        except PermissionError as exc:
            # EPERM — PID exists but is owned by a foreign user. This is
            # the contamination pattern from the 2026-09-08 incident.
            # FAIL CLOSED.
            raise RefuseToRun(
                f"LIVE forward_test PID {pid} exists but is not owned by "
                f"the invoking user (EPERM: {exc}). This may indicate the "
                f"live engine is running under a foreign user account — "
                f"exactly the contamination pattern from the 2026-09-08 "
                f"Phase 1 restart incident. Harness refuses to start. "
                f"Card 758273a7 rework (Rin verdict 4f384bfc HIGH #3)."
            ) from exc

    # (b) foreign-ownership check on live state files
    if not allow_foreign:
        invoking_uid = os.getuid()
        try:
            invoking_user = pwd.getpwuid(invoking_uid).pw_name
        except KeyError:
            invoking_user = f"uid:{invoking_uid}"

        for live_file in (LIVE_KILL_SWITCH_GLOBAL_STATE, LIVE_RISK_STATE_BLEND):
            if not live_file.exists():
                continue
            st = live_file.stat()
            if st.st_uid == invoking_uid:
                continue
            try:
                file_owner = pwd.getpwuid(st.st_uid).pw_name
            except KeyError:
                file_owner = f"uid:{st.st_uid}"
            raise RefuseToRun(
                f"LIVE state file {live_file} is owned by {file_owner} "
                f"(uid={st.st_uid}); invoking user is {invoking_user} "
                f"(uid={invoking_uid}). This signals prior harness "
                f"contamination (card 758273a7 — root-run harness wrote "
                f"live state on 2026-09-08). Use chown to align ownership, "
                f"or pass --allow-foreign-ownership if you understand the risk."
            )


def _build_isolated_kill_switch(state_dir: Path) -> KillSwitchManager:
    """Construct a KillSwitchManager bound to the harness's isolated state dir.

    Card 758273a7: this replaces the engine's default KillSwitchManager
    (which would point at ``data/kill_switches/`` and contaminate live
    state on every FTMO guard activation). All consumers wired by
    ``engine._build_components()`` see THIS instance via
    ``self._kill_switch`` once the harness reassigns it.
    """
    isolated_ks_dir = state_dir / "kill_switches"
    isolated_ks_dir.mkdir(parents=True, exist_ok=True)
    return KillSwitchManager(state_dir=str(isolated_ks_dir), disabled=False)


@contextmanager
def _isolation_patch(isolated_ks: KillSwitchManager):
    """Exception-safe context that patches all KillSwitchManager bindings
    and flips ``_disabled=False`` for the harness run, restoring the
    originals on EVERY exit path (success, exception, RefuseToRun).

    Card 758273a7 REWORK (Rin verdict 4f384bfc HIGH #1 + #2):
      * Original module-level patches restored on exception (was: only
        restored on success path, leaking _HarnessIsolatedKS into
        subsequent in-process constructions).
      * Original ``KillSwitchManager._disabled`` value restored (was:
        flipped to False and never restored, leaking into subsequent
        in-process runs that would default to enabled despite the
        production class default).

    Caller is responsible for building ``isolated_ks`` BEFORE entering
    the context (the subclass defined inside this context references
    ``isolated_ks._state_dir``).
    """
    # Capture originals BEFORE any flip / patch.
    _saved_disabled = KillSwitchManager._disabled

    import adapters.ctrader.forward_test_engine as _fte_module
    import adapters.ctrader.kill_switch as _ks_module
    _saved_ks_cls = _ks_module.KillSwitchManager
    _saved_fte_ks = _fte_module.KillSwitchManager

    _pt_module = None
    _saved_pt_ks = None
    try:
        from adapters.ctrader import paper_trader as _pt_module  # type: ignore[no-redef]  # noqa: F841
        _saved_pt_ks = getattr(_pt_module, "KillSwitchManager", None)
    except ImportError:
        pass

    _rg_module = None
    _saved_rg_ks = None
    try:
        from adapters.ctrader import risk_guard as _rg_module  # type: ignore[no-redef]  # noqa: F841
        # risk_guard.py does ``from .kill_switch import KillSwitchManager``
        # INSIDE the function (lazy), not at module level — so the module
        # attribute may not exist. Capture only if present.
        _saved_rg_ks = getattr(_rg_module, "KillSwitchManager", None)
    except ImportError:
        pass

    # Define the patched subclass using the caller-supplied isolated_ks.
    class _HarnessIsolatedKS(KillSwitchManager):
        """Subclass used only inside the harness — defaults state_dir to
        the harness's isolated dir so EVERY ``KillSwitchManager()`` call
        (engine.__init__, PaperTrader.__init__, RiskGuard fallback) writes
        to the isolated dir, never to live data/kill_switches/. Card 758273a7.
        """

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("state_dir", str(isolated_ks._state_dir))
            kwargs.setdefault("disabled", False)
            super().__init__(*args, **kwargs)

    try:
        # Apply patches.
        KillSwitchManager._disabled = False
        _ks_module.KillSwitchManager = _HarnessIsolatedKS
        _fte_module.KillSwitchManager = _HarnessIsolatedKS
        if _pt_module is not None and _saved_pt_ks is not None:
            _pt_module.KillSwitchManager = _HarnessIsolatedKS
        if _rg_module is not None and _saved_rg_ks is not None:
            _rg_module.KillSwitchManager = _HarnessIsolatedKS
        logger.debug(
            "Harness isolation patches APPLIED (_disabled=%s -> False, "
            "modules: ks,fte,pt=%s,rg=%s)",
            _saved_disabled,
            _pt_module is not None,
            _rg_module is not None,
        )
        yield
    finally:
        # ALWAYS restore — even on exception, RefuseToRun, or KeyboardInterrupt.
        KillSwitchManager._disabled = _saved_disabled
        _ks_module.KillSwitchManager = _saved_ks_cls
        _fte_module.KillSwitchManager = _saved_fte_ks
        if _pt_module is not None and _saved_pt_ks is not None:
            _pt_module.KillSwitchManager = _saved_pt_ks
        if _rg_module is not None and _saved_rg_ks is not None:
            _rg_module.KillSwitchManager = _saved_rg_ks
        logger.debug(
            "Harness isolation patches RESTORED (_disabled=%s, modules restored)",
            _saved_disabled,
        )

# ── Import blend pipeline from launch script ─────────────────────────────────
# The BlendForwardTestEngine, CorrelationGate, RegimeGate, HeartbeatTracker,
# and supporting constants live in launch_blend_forward_test.py at module
# level.  We import them dynamically so the backtest harness always uses
# the production class definitions without duplicating code.

_spec = importlib.util.spec_from_file_location(
    "_launch_blend",
    str(PROJECT_ROOT / "scripts" / "launch_blend_forward_test.py"),
)
if _spec is None:
    # Card 758273a7 (mypy baseline fix): spec_from_file_location can return
    # None on missing modules / read errors. Raise explicitly so the failure
    # mode is loud at startup rather than a downstream AttributeError on
    # _spec.loader. Survives ``python -O`` (asserts would not).
    raise ImportError(
        f"Could not load module spec for {PROJECT_ROOT / 'scripts' / 'launch_blend_forward_test.py'} "
        f"(spec_from_file_location returned None)"
    )
_launch = importlib.util.module_from_spec(_spec)
if _spec.loader is None:
    raise ImportError(
        f"Module spec for {PROJECT_ROOT / 'scripts' / 'launch_blend_forward_test.py'} "
        f"has no loader (loader=None)"
    )
# Card 4083ac2d-... (harness spec-load crash fix): register the spec-loaded
# module in sys.modules under its __name__ BEFORE exec_module.  The
# @dataclass decorator does a sys.modules[cls.__module__] lookup at class-
# creation time (via typing.get_type_hints under PEP 563 / from __future__
# import annotations), and the launcher's _Slot dataclass triggers that
# path.  Without this registration, dataclass evaluation raises
# AttributeError: 'NoneType' object has no attribute '__dict__'.
# Standard library idiom for importlib.util.spec_from_file_location.
sys.modules[_spec.name] = _launch
_spec.loader.exec_module(_launch)

BlendForwardTestEngine = _launch.BlendForwardTestEngine
CorrelationGate = _launch.CorrelationGate
RegimeGate = _launch.RegimeGate
HeartbeatTracker = _launch.HeartbeatTracker
STRATEGY_ID_MAP = _launch.STRATEGY_ID_MAP
STRATEGY_TIMEFRAMES = _launch.STRATEGY_TIMEFRAMES
build_blend_runner = _launch.build_blend_runner

from adapters.ctrader.forward_test_engine import ForwardTestConfig
from adapters.ctrader.kill_switch import KillSwitchManager
from adapters.ctrader.risk_guard import FTMOConfig
from common.logging_config import setup_logging
from core.types import Bar, BarPeriod
from risk.ftmo_guard import FTMOGuard
from strategies.donchian_atr_trend_v2 import (
    DonchianATRConfig,
    DonchianATRTrendV2Strategy,
)
from strategies.dual_tf_squeeze_pro import (
    DualTFSqueezeProConfig,
    DualTFSqueezeProStrategy,
)
from strategies.killzone_momentum import (
    KillzoneMomentumConfig,
    KillzoneMomentumStrategy,
)
from strategies.london_breakout_retest import (
    LondonBreakoutConfig,
    LondonBreakoutRetestStrategy,
)
from strategies.srmr_plus import SRMRPlusConfig, SRMRPlusStrategy
from strategies.ttc_xauusd import TTCXAUUSDStrategy

# ── DuckDB bar loading ────────────────────────────────────────────────────────


def load_bars_from_duckdb(
    db_path: str,
    symbol: str,
    timeframe: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[Bar]:
    """Load OHLCV bars from DuckDB and convert to ``Bar`` objects.

    DuckDB ``bars`` table columns:
        symbol, timeframe, timestamp_utc (seconds), open, high, low,
        close, volume, spread_pips
    """
    period = BarPeriod.H1() if timeframe == "H1" else BarPeriod.M15()

    query = (
        "SELECT timestamp_utc, open, high, low, close, volume, spread_pips FROM bars WHERE symbol = ? AND timeframe = ?"
    )
    params: list = [symbol, timeframe]
    if start_ts is not None:
        query += " AND timestamp_utc >= ?"
        params.append(start_ts)
    if end_ts is not None:
        query += " AND timestamp_utc <= ?"
        params.append(end_ts)
    query += " ORDER BY timestamp_utc ASC"

    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute(query, params).fetchall()
    finally:
        con.close()

    bars = []
    for ts, o, h, l, c, vol, sp in rows:  # noqa: E741
        bars.append(
            Bar(
                time=datetime.fromtimestamp(ts, tz=timezone.utc),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=vol,
                period=period,
                spread_pips=sp,
            )
        )
    logger.info("Loaded %d %s %s bars from %s", len(bars), symbol, timeframe, db_path)
    return bars


# ── Statistics computation ───────────────────────────────────────────────────


def compute_stats(
    paper_trader,
    blend_runner: BlendForwardTestRunner,
    commission_per_lot: float = 0.0,
    position_strategy_map: dict | None = None,
) -> dict:
    """Compute backtest performance statistics from PaperTrader state.

    Returns a dict with PF, net P&L, max drawdown, win rate, trade count,
    and per-strategy contribution breakdown.

    ``position_strategy_map`` maps position_id → strategy_id (populated by
    the patched _route_signal during the backtest eval loop).
    """
    if position_strategy_map is None:
        position_strategy_map = {}

    om = paper_trader._order_manager
    all_positions = list(om._positions.values())

    # Separate closed and open positions
    closed = [p for p in all_positions if p.status.is_closed]
    open_positions = [p for p in all_positions if not p.status.is_closed]

    # Apply commission adjustment to each closed trade
    trades = []
    for p in closed:
        pnl = p.closed_pnl - (abs(p.volume) * commission_per_lot)
        strategy_id = position_strategy_map.get(p.position_id, "unknown")
        trades.append(
            {
                "strategy_id": strategy_id,
                "symbol": p.symbol,
                "direction": p.direction.value if hasattr(p.direction, "value") else str(p.direction),
                "volume": p.volume,
                "entry_price": p.entry_price,
                "exit_price": p.closed_price or 0.0,
                "pnl": pnl,
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
            }
        )

    # Core metrics
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    net_pnl = gross_profit - gross_loss
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total_trades = len(trades)
    win_rate = wins / total_trades if total_trades > 0 else 0.0

    # Max drawdown (peak-to-trough on cumulative P&L curve)
    peak = 0.0
    max_dd = 0.0
    cumulative = 0.0
    for t in sorted(trades, key=lambda x: x["closed_at"] or ""):
        cumulative += t["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Per-strategy breakdown
    per_strategy: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        sid = t["strategy_id"]
        per_strategy[sid]["trades"] += 1
        per_strategy[sid]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            per_strategy[sid]["wins"] += 1

    strategy_stats = {}
    for sid, s in sorted(per_strategy.items()):
        strategy_stats[sid] = {
            "trades": s["trades"],
            "pnl": round(s["pnl"], 2),
            "win_rate": round(s["wins"] / s["trades"], 4) if s["trades"] else 0.0,
        }

    # Open positions (unrealized)
    open_unrealized = sum(p.unrealized_pnl for p in open_positions)

    # Compute true realized P&L from closed positions (including commission)
    true_realized = sum(t["pnl"] for t in trades)
    true_final_balance = paper_trader._starting_balance + true_realized + open_unrealized

    return {
        "total_trades": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(pf, 4) if pf != float("inf") else pf,
        "net_pnl": round(net_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "max_drawdown": round(max_dd, 2),
        "open_positions": len(open_positions),
        "open_unrealized": round(open_unrealized, 2),
        "starting_balance": paper_trader._starting_balance,
        "final_balance": round(true_final_balance, 2),
        "per_strategy": strategy_stats,
    }


# ── Main backtest ────────────────────────────────────────────────────────────


def run_backtest(args: argparse.Namespace) -> dict:
    """Run the full backtest and return results dict."""
    symbol = args.symbol.upper()
    symbols = [symbol]

    # ── Isolation (card 758273a7-c3f1-41fa-95dc-7384c260acd7) ────────────
    # Resolve an isolated per-run state dir, then run the guard. On any
    # RefuseToRun the function returns immediately with an error dict so
    # the CLI surfaces a clear exit-1 message instead of crashing.
    state_dir = _resolve_state_dir(args)
    try:
        _verify_isolation_or_refuse(args, state_dir)
    except RefuseToRun as exc:
        logger.error("HARNESS REFUSED TO START: %s", exc)
        return {"error": "isolation_guard_refused", "detail": str(exc)}

    # ── Parse date range ──────────────────────────────────────────────────
    start_ts = None
    end_ts = None
    if args.start:
        start_ts = int(datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    if args.end:
        end_ts = int(
            datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 86399  # end of day
        )

    # ── Load bars from DuckDB ─────────────────────────────────────────────
    db_path = args.db_path or str(PROJECT_ROOT / "data" / "ayumi_market.duckdb")
    m15_bars = load_bars_from_duckdb(db_path, symbol, "M15", start_ts, end_ts)
    h1_bars = load_bars_from_duckdb(db_path, symbol, "H1", start_ts, end_ts)

    if not m15_bars:
        logger.error("No M15 bars found for %s in the specified date range", symbol)
        return {"error": "no_m15_bars"}
    if not h1_bars:
        logger.error("No H1 bars found for %s in the specified date range", symbol)
        return {"error": "no_h1_bars"}

    # ── Construct strategies (same as launch_blend_forward_test.py main()) ──
    strategies = [
        KillzoneMomentumStrategy(config=KillzoneMomentumConfig()),
        TTCXAUUSDStrategy(),
        DualTFSqueezeProStrategy(config=DualTFSqueezeProConfig()),
        DonchianATRTrendV2Strategy(config=DonchianATRConfig()),
        SRMRPlusStrategy(config=SRMRPlusConfig(symbol=symbol)),
        LondonBreakoutRetestStrategy(config=LondonBreakoutConfig()),
    ]

    active_names = {s.name for s in strategies}
    active_strategy_timeframes = {k: v for k, v in STRATEGY_TIMEFRAMES.items() if k in active_names}
    active_strategy_id_map = {k: v for k, v in STRATEGY_ID_MAP.items() if k in active_names}

    logger.info("Strategy pool: %s", [s.name for s in strategies])

    # ── Reset stale risk state — ISOLATED (card 758273a7) ───────────────
    # The blend runner loads risk_state_blend.json on start(), which may
    # contain stale open_risk from a previous live/session run.  This would
    # block all new signals (open_risk + new > max).  Delete before run.
    # The path is now isolated to the harness's per-run state dir — never
    # the live data/risk_state_blend.json (card 758273a7 fix: harness must
    # never overwrite live forward-test state).
    state_path = state_dir / "risk_state_blend.json"
    if state_path.exists():
        state_path.unlink()
        logger.info("Cleared stale risk state: %s (isolated)", state_path)

    # ── Build blend runner ────────────────────────────────────────────────
    blend_runner = build_blend_runner()

    # Card 758273a7 (2026-09-08): build_blend_runner() (defined in
    # scripts/launch_blend_forward_test.py:1285+) defaults its state_path
    # config to "data/risk_state_blend.json" (the LIVE engine state file).
    # The runner's _persistence attribute then writes to that path on
    # blend_runner.stop(). Patch the persistence path to the isolated
    # state dir BEFORE the eval loop so any save call (including the one
    # fired by blend_runner.stop() at the end of run_backtest) targets
    # the isolated dir, never the live engine state file.
    if hasattr(blend_runner, "_persistence") and blend_runner._persistence is not None:
        isolated_risk_state = state_dir / "risk_state_blend.json"
        blend_runner._persistence._path = isolated_risk_state
        logger.info(
            "Isolation: blend_runner._persistence._path redirected to %s "
            "(was default data/risk_state_blend.json)",
            isolated_risk_state,
        )
    else:
        logger.warning(
            "Isolation: blend_runner has no _persistence attribute; "
            "skipping risk state redirect (card 758273a7 — may indicate "
            "build_blend_runner API drift)."
        )

    # Verify clean risk state after construction
    if hasattr(blend_runner, "_sizer") and blend_runner._sizer:
        open_risk = blend_runner._sizer._open_risk
        if open_risk > 0:
            logger.warning("Resetting non-zero open_risk after build: $%.2f", open_risk)
            blend_runner._sizer._open_positions.clear()
            blend_runner._sizer._position_symbols.clear()
        logger.info("Blend runner sizer open_risk: $%.2f", blend_runner._sizer._open_risk)

    # Override spread for XAUUSD if --costs
    if args.costs:
        blend_runner._config["spread_pips"][symbol] = args.spread

    correlation_gate = CorrelationGate()
    regime_gate = RegimeGate()
    heartbeat = HeartbeatTracker(interval=500)

    # ── Build engine config ───────────────────────────────────────────────
    config = ForwardTestConfig(
        symbol=symbol,
        symbols=symbols,
        starting_balance=10_000.0,
        min_confidence=0.30,
        max_bars_per_symbol=500,
        min_bars_for_evaluation=55,
        live_mode=False,
        execution_mode="paper",
        strategy_timeframes=active_strategy_timeframes,
        bar_period_minutes=15,
        preload_bar_count=200,
    )

    # ── Build dummy credentials (paper mode — never used) ─────────────────
    from adapters.ctrader.models import cTraderCredentials

    credentials = cTraderCredentials(
        host="",
        port=0,
        use_ssl=False,
        username="",
        password="",
        sender_comp_id="",
        target_comp_id="",
        sender_sub_id="",
    )

    # ── Explicit kill-switch enable for harness (card 09e99147) ──────────
    # Card 09e99147 (2026-09-08, REWORK): the production launcher has
    # KillSwitchManager class default `_disabled = True` (Craig directive Jun 27),
    # which suppresses all FTMOGuard/RiskGuard activations. The harness is the
    # safety-critical backtest path — it must enforce the kill switch so a
    # runaway trade cannot drain the account unbounded (parent card 76046374
    # evidence: harness re-run on 2026-09-07 lost -$123,700 because
    # activate_global_kill was suppressed with "kill switch disabled").
    #
    # Rin REWORK (2026-09-08): flipping the class attribute AFTER engine
    # construction leaves PositionMonitor and PaperTrader._risk_guard holding
    # the original disabled instance — the engine wires them in
    # _build_components() (forward_test_engine.py:1026,1041). The fix is to
    # flip BEFORE construction so engine._kill_switch = KillSwitchManager()
    # inside BlendForwardTestEngine.__init__ picks up `_disabled=False` via
    # class default and propagates the enabled instance to every consumer.
    #
    # NOTE (Rin verdict 4f384bfc REWORK-2, 2026-09-08): the ``_disabled=False``
    # flip itself happens INSIDE ``_isolation_patch(isolated_ks)`` below, in
    # its try block (line ~304). Doing the flip HERE (before the context
    # manager runs) would cause ``_isolation_patch`` to capture the
    # already-flipped False at its capture site (~line 263), so its finally
    # would restore False instead of the production default True — leaking
    # the harness-enabled state into subsequent in-process runs.
    # The ``logger.warning`` that announces the flip also lives INSIDE the
    # ``with`` block below (single source of truth for the override log).

    # Card 758273a7 (2026-09-08): construct an ISOLATED KillSwitchManager
    # bound to the harness state dir (NOT data/kill_switches/). The engine's
    # __init__ at forward_test_engine.py:535 still constructs a default
    # KillSwitchManager() — we will replace engine._kill_switch with this
    # isolated instance BEFORE engine._build_components() so every consumer
    # (market_feed at :989/:1100, ExecutionPermissionPolicy at :992/:1103,
    # PositionMonitor at :1029, PaperTrader._risk_guard at :1041) sees the
    # isolated instance via self._kill_switch. FTMO guard activations then
    # write to state_dir/kill_switches/, never to live data/kill_switches/.
    isolated_ks = _build_isolated_kill_switch(state_dir)
    logger.info(
        "Isolation: kill switch bound to %s (NOT %s)",
        isolated_ks._state_dir,
        LIVE_KILL_SWITCH_GLOBAL_STATE.parent,
    )

    # Card 758273a7 (REWORK, Rin verdict 4f384bfc): all module-level
    # ``KillSwitchManager`` patches and the ``_disabled = False`` flip
    # are applied by ``_isolation_patch(isolated_ks)`` below, in an
    # exception-safe ``try/finally`` (via contextmanager). On ANY exit
    # path (success, exception, RefuseToRun, KeyboardInterrupt) the
    # original module references AND the original ``_disabled`` value
    # are restored — addressing Rin HIGH findings #1 and #2.
    #
    # Card 758273a7 — broader patch covers these ``KillSwitchManager()``
    # default construction sites:
    #   * forward_test_engine.py:535 (engine._kill_switch)
    #   * paper_trader.py:75 (paper_trader._kill_switch, created BEFORE
    #     the engine's set_kill_switch override at :1041)
    #   * risk_guard.py:567 (fallback when self._kill_switch is None)
    # All resolve to the ``_HarnessIsolatedKS`` subclass while inside
    # the context manager, defaulting ``state_dir`` to the isolated dir.

    with _isolation_patch(isolated_ks):
        logger.warning(
            "HARNESS KILL-SWITCH OVERRIDE: explicitly ENABLED for backtest "
            "harness (disabled=False; class default is True per Craig directive). "
            "Production launcher scripts/launch_blend_forward_test.py is UNAFFECTED "
            "and remains administratively disabled until Craig re-enables it."
        )

        # ── Create engine ─────────────────────────────────────────────
        engine = BlendForwardTestEngine(
        config=config,
        strategies=strategies,
        ftmo_config=FTMOConfig(min_risk_reward=0.0),
        credentials=credentials,
        blend_runner=blend_runner,
        correlation_gate=correlation_gate,
        heartbeat=heartbeat,
        strategy_id_map=active_strategy_id_map,
        regime_gate=regime_gate,
        blend_mode=True,
    )

        # Card 758273a7: defensively re-affirm engine._kill_switch is the
        # isolated instance. The subclassed __init__ used during engine
        # construction already wrote to the isolated dir, but we also
        # explicitly set the canonical isolated_ks reference here so any
        # later code reading engine._kill_switch sees the exact instance we
        # built at the top of this section (consistent identity for tests).
        # NOTE: the module-level KillSwitchManager patch stays in effect
        # through engine._build_components() below — PaperTrader.__init__
        # (paper_trader.py:75) and any RiskGuard fallback (risk_guard.py:567)
        # also need the patched subclass so their default-state-dir kill
        # switch instances point at the isolated dir. Patch restore is
        # deferred to AFTER the eval loop completes (see end of run_backtest).
        engine._kill_switch = isolated_ks

        # ── Build internal components WITHOUT starting the feed ───────────────
        # _build_components() creates PaperTrader, cTraderLiveAdapter,
        # TradeLogger, and PositionMonitor.  In paper mode (live_mode=False)
        # it skips OpenApiSpotFeed construction entirely.
        # Because we flipped KillSwitchManager._disabled=False above BEFORE
        # engine construction, every consumer built here (PositionMonitor at
        # forward_test_engine.py:1026, risk_guard at :1041, ExecutionPermissionPolicy
        # at :992/:1103, market_feed at :989/:1100) receives the SAME enabled
        # instance via `self._kill_switch`. There is no post-hoc rewire.
        engine._build_components()

        # ── Verify all consumers reference the enabled instance (defense-in-depth) ──
        # Asserts that the engine's kill_switch is enabled AND that PositionMonitor
        # + risk_guard see the same enabled instance. A split-state regression would
        # cause assertions here to fail.
        _enabled_ks = engine._kill_switch
        if _enabled_ks.is_disabled:
            raise RuntimeError(
                f"REGRESSION: engine._kill_switch is still disabled after harness override; "
                f"class default was {KillSwitchManager._disabled}"
            )
        if engine._paper_trader is not None and getattr(engine._paper_trader, "_risk_guard", None) is not None:
            _rg_ks = engine._paper_trader._risk_guard._kill_switch
            if _rg_ks is not _enabled_ks or _rg_ks.is_disabled:
                raise RuntimeError(
                    f"REGRESSION: PaperTrader._risk_guard sees disabled kill_switch "
                    f"({_rg_ks!r}, is_disabled={_rg_ks.is_disabled}) instead of the "
                    f"enabled engine._kill_switch ({_enabled_ks!r})"
                )
        if engine._position_monitor is not None:
            _pm_ks = engine._position_monitor._kill_switch
            if _pm_ks is not _enabled_ks or _pm_ks.is_disabled:
                raise RuntimeError(
                    f"REGRESSION: PositionMonitor sees disabled kill_switch "
                    f"({_pm_ks!r}, is_disabled={_pm_ks.is_disabled}) instead of the "
                    f"enabled engine._kill_switch ({_enabled_ks!r})"
                )

        logger.info(
            "HARNESS: Kill switch ENABLED — all consumers (PositionMonitor, risk_guard) "
            "reference the same enabled instance (id=%s); eval loop will HALT on breach",
            id(_enabled_ks),
        )
        if engine._kill_switch.is_globally_killed():
            logger.warning(
                "HARNESS: Kill switch state file shows ACTIVE (%s) — orders will be BLOCKED",
                engine._kill_switch.get_status().get("reason", "unknown"),
            )

        # ── Wire FTMO guard (card aa3a1cbe — Bug 1 fix) ────────────────────────
        # Production main loop (scripts/launch_blend_forward_test.py:1708-1741)
        # instantiates an FTMOGuard bound to the engine's kill_switch and calls
        # .update() after every iteration. The harness drives _evaluate_strategies()
        # directly per bar and was missing this enforcer, letting one runaway trade
        # drain -$123,700 on a $10K starting balance. Mirror the production pattern
        # so the 3% daily-DD + 10% trailing-DD circuit breakers are enforced here too.
        # engine._kill_switch is constructed in forward_test_engine.py:513 inside
        # BlendForwardTestEngine.__init__, so it is non-None at this point.
        _ftmo_guard = FTMOGuard(
            kill_switch=engine._kill_switch,
            starting_balance=10_000.0,
            challenge_type="1-step",
            trailing_dd=True,
        )
        logger.info(
            "FTMOGuard active in harness: kill_switch=%s starting_balance=$%.2f daily_loss=%.1f%% dd_freeze=%.1f%%",
            "wired" if engine._kill_switch is not None else "NONE",
            10_000.0,
            _ftmo_guard._max_daily_loss_pct,
            _ftmo_guard._dd_freeze_pct,
        )

        # Mark engine as running so internal guards pass
        engine._running = True
        engine._start_time = datetime.now(timezone.utc)
        engine._preload_complete = True

        # Configure slippage model if --costs
        if args.costs and engine._paper_trader:
            om = engine._paper_trader._order_manager
            # For XAUUSD, pip_value = 0.01 ($0.01 per pip)
            om._slippage_model.base_pips = 0.0
            om._slippage_model.random_pips = 0.0
            om._slippage_model.pip_value = args.slippage  # interpret as price units

        paper_trader = engine._paper_trader

        # ── Strategy tracking via patched _route_signal ──────────────────────
        # The production _route_signal constructs a new CTraderTradeSignal
        # (exec_signal) without carrying strategy_id, so positions lose their
        # strategy attribution.  We patch the method to capture position_id →
        # strategy_id mapping by comparing open positions before/after each
        # signal routing.

        position_strategy_map: dict[str, str] = {}
        _orig_route = engine._route_signal

        def _tracked_route(signal, strategy_name):
            sid = active_strategy_id_map.get(strategy_name, strategy_name.lower().replace(" ", "_"))
            pos_before = set()
            if engine._paper_trader:
                pos_before = set(engine._paper_trader._order_manager._positions.keys())
            _orig_route(signal, strategy_name)
            if engine._paper_trader:
                pos_after = set(engine._paper_trader._order_manager._positions.keys())
                for pid in pos_after - pos_before:
                    position_strategy_map[pid] = sid

        engine._route_signal = _tracked_route

        logger.info("Engine constructed. PaperTrader balance: $%.2f", paper_trader._starting_balance)
        logger.info("Strategies in live_adapter: %s", list(engine._live_adapter._strategies.keys()))
        logger.info(
            "Adapters: %s",
            [k for k in engine._live_adapter._adapters.keys() if symbol in k],
        )

        # ── Drive evaluation loop ─────────────────────────────────────────────
        # Merge M15 and H1 bars into a single chronological event stream.
        # Each event is (timeframe_label, bar).  When multiple events share
        # the same timestamp, H1 bars are processed before M15 bars (H1
        # closes before M15 evaluation sees the updated H1 window).
        m15_key = f"{symbol}:15"
        h1_key = f"{symbol}:60"

        events: list[tuple[str, Bar]] = []
        for bar in m15_bars:
            events.append(("M15", bar))
        for bar in h1_bars:
            events.append(("H1", bar))
        events.sort(key=lambda x: (x[1].time, 0 if x[0] == "H1" else 1))

        # Determine spread for price updates
        spread_price = args.spread if args.costs else 0.0

        bars_processed = 0
        # Card 75b24f98: rate-limit per-key FTMO pre-trade-gate log lines
        # (moved out of the loop to avoid locals() mutation — locals()
        # assignment is not reliably observable inside CPython loops).
        _pre_gate_keys_seen: set[str] = set()

        for tf_label, bar in events:
            key = h1_key if tf_label == "H1" else m15_key

            # Append bar to engine's bar store (respecting max_bars limit)
            if key not in engine._bars:
                engine._bars[key] = []
            engine._bars[key].append(bar)
            if len(engine._bars[key]) > config.max_bars_per_symbol:
                engine._bars[key] = engine._bars[key][-config.max_bars_per_symbol :]

            # Update paper trader prices (for open position TP/SL checks)
            close = bar.close
            bid = close - spread_price / 2 if spread_price > 0 else close
            ask = close + spread_price / 2 if spread_price > 0 else close
            paper_trader.update_market_prices(
                {symbol: close},
                bids={symbol: bid},
                asks={symbol: ask},
            )

            # Set engine spread from bar data or --costs override
            bar_spread = getattr(bar, "spread_pips", 0.0)
            if args.costs:
                engine._current_spread = spread_price
            elif bar_spread > 0:
                # Convert bar spread_pips to approximate price units
                engine._current_spread = bar_spread * 0.01  # rough pip→price for XAUUSD
            else:
                engine._current_spread = 0.0

            # ── FTMO pre-trade gate (card 75b24f98 — pre-trade ordering) ────
            # The post-eval call on _ftmo_guard below only RECORDS state and
            # FREEZEs a breach that has already been realized — it does not
            # gate entry.  A same-bar entry that would push daily-DD past the
            # 3% line is accepted first, then the guard fires, but the P&L
            # damage is already inside that bar.  Call should_allow_new_position()
            # BEFORE _evaluate_strategies() so the breach is preempted.
            # The post-eval update() is kept intact (production main loop pattern
            # in scripts/launch_blend_forward_test.py:1984-2010) — it still
            # detects intraday escalations and triggers FREEZE/KILL via the
            # shared kill_switch.
            _pre_gate_open, _pre_gate_reason = _ftmo_guard.should_allow_new_position()
            if not _pre_gate_open:
                # Block entry on this bar; log the gate reason (rate-limited
                # to once per key to avoid log spam during prolonged freezes).
                if key not in _pre_gate_keys_seen:
                    logger.info(
                        "FTMO pre-trade gate BLOCKED entry on %s (%s); "
                        "post-eval update() will still detect escalation.",
                        key,
                        _pre_gate_reason,
                    )
                    _pre_gate_keys_seen.add(key)
                skip_evaluate = True
            else:
                skip_evaluate = False

            # Trigger evaluation (skipped when FTMO gate blocks entry)
            if not skip_evaluate:
                engine._bar_completed[key] = True
                engine._evaluate_strategies(symbol)
                engine._bar_completed[key] = False

            # ── FTMO guard update (card aa3a1cbe — Bug 1 fix) ────────────────
            # Production invokes _ftmo_guard.update(balance, open_n) after every
            # fill/iteration (launch_blend_forward_test.py:1724). Without this call
            # daily_loss_pct stays 0.0, kill_switch never engages, and the harness
            # lets a losing position run unbounded. We mirror the production
            # pattern here; on FREEZE/KILL we halt the eval loop so downstream
            # compute_stats() reflects the guard-engaged state, not the unbounded
            # post-breach state the harness originally surfaced.
            open_n = len(paper_trader._order_manager._positions)
            _ftmo_action = _ftmo_guard.update(paper_trader._current_balance, open_n)
            if _ftmo_action.value in ("freeze", "kill"):
                engine._running = False
                logger.warning(
                    "FTMO guard engaged in harness: %s — halting eval loop "
                    "(balance=$%.2f, open=%d, daily_loss=%.2f%%, dd=%.2f%%)",
                    _ftmo_action.value,
                    paper_trader._current_balance,
                    open_n,
                    _ftmo_guard.daily_loss_pct,
                    _ftmo_guard.current_dd_pct,
                )
                break

            # Card 644c565b: also halt when engine._kill_switch reports
            # GLOBAL KILL. _ftmo_guard.update() keys on realized loss; risk_guard
            # can fire GLOBAL KILL on a 3.17% daily-DD breach while _ftmo_guard
            # only ever sees the 0.55% realized leg (re-run #3). Without this
            # second condition the eval loop drains all events past the kill
            # point and compute_stats() reflects post-kill phantom activity.
            if engine._kill_switch.is_globally_killed():
                engine._running = False
                _ks_status = engine._kill_switch.get_status()
                logger.warning(
                    "Global kill_switch engaged — halting eval loop "
                    "(reason=%s, triggered_by=%s, balance=$%.2f, open=%d, "
                    "daily_loss=%.2f%%, dd=%.2f%%)",
                    _ks_status.get("reason", "unknown"),
                    _ks_status.get("triggered_by", "unknown"),
                    paper_trader._current_balance,
                    open_n,
                    _ftmo_guard.daily_loss_pct,
                    _ftmo_guard.current_dd_pct,
                )
                break

            bars_processed += 1
            if bars_processed % 5000 == 0:
                stats = paper_trader.get_stats()
                logger.info(
                    "Progress: %d/%d events | trades=%d | balance=$%.2f",
                    bars_processed,
                    len(events),
                    stats.trades_executed,
                    paper_trader._current_balance,
                )

        logger.info("Evaluation complete: %d events processed", bars_processed)

        # ── Compute and return results ────────────────────────────────────────
        commission = args.commission if args.costs else 0.0
        results = compute_stats(
            paper_trader,
            blend_runner,
            commission_per_lot=commission,
            position_strategy_map=position_strategy_map,
        )
        results["symbol"] = symbol
        results["bars_processed"] = bars_processed
        results["m15_bars"] = len(m15_bars)
        results["h1_bars"] = len(h1_bars)
        results["costs_applied"] = args.costs
        if args.costs:
            results["cost_params"] = {
                "spread": args.spread,
                "commission_per_lot": args.commission,
                "slippage": args.slippage,
            }
        results["date_range"] = {
            "start": m15_bars[0].time.isoformat(),
            "end": m15_bars[-1].time.isoformat(),
        }

        # ── Print summary ─────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print(f"  BACKTEST RESULTS — {symbol}")
        print("=" * 60)
        print(f"  Date Range:     {results['date_range']['start'][:10]} → {results['date_range']['end'][:10]}")
        print(f"  Bars Processed: {bars_processed:,} ({len(m15_bars):,} M15 + {len(h1_bars):,} H1)")
        print(f"  Total Trades:   {results['total_trades']}")
        print(f"  Win Rate:       {results['win_rate']:.1%} ({results['wins']}W / {results['losses']}L)")
        print(f"  Profit Factor:  {results['profit_factor']:.4f}")
        print(f"  Net P&L:        ${results['net_pnl']:,.2f}")
        print(f"  Gross Profit:   ${results['gross_profit']:,.2f}")
        print(f"  Gross Loss:     ${results['gross_loss']:,.2f}")
        print(f"  Max Drawdown:   ${results['max_drawdown']:,.2f}")
        print(f"  Final Balance:  ${results['final_balance']:,.2f}")
        print(f"  Open Positions: {results['open_positions']} (unrealized: ${results['open_unrealized']:,.2f})")
        if results["costs_applied"]:
            cp = results["cost_params"]
            print(
                f"  Costs:          spread={cp['spread']}, commission=${cp['commission_per_lot']}/lot, slippage={cp['slippage']}"  # noqa: E501
            )

        print("\n  Per-Strategy Contribution:")
        print("  " + "-" * 56)
        print(f"  {'Strategy':<30} {'Trades':>7} {'P&L':>12} {'Win Rate':>10}")
        print("  " + "-" * 56)
        for sid, s in results["per_strategy"].items():
            print(f"  {sid:<30} {s['trades']:>7} ${s['pnl']:>10,.2f} {s['win_rate']:>9.1%}")
        print("=" * 60)

        # ── Save JSON output ──────────────────────────────────────────────────
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info("Results saved to %s", output_path)

        # ── Cleanup ───────────────────────────────────────────────────────────
        blend_runner.stop()


        # Card 758273a7: surface the isolated state dir + auto-clean policy.
        results["_state_dir"] = str(state_dir)
        results["_state_dir_kept"] = bool(getattr(args, "keep_state_dir", False))
        _cleanup_state_dir(state_dir, keep=bool(getattr(args, "keep_state_dir", False)))

        return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Backtest Blend Harness — production-fidelity backtest without cTrader"
    )
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--symbol", default="XAUUSD", help="Symbol (default: XAUUSD)")
    parser.add_argument("--costs", action="store_true", help="Apply trading costs")
    parser.add_argument("--spread", type=float, default=2.5, help="Spread in price units (default: 2.5)")
    parser.add_argument(
        "--commission",
        type=float,
        default=3.5,
        help="Commission per lot USD (default: 3.5)",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.2,
        help="Slippage in price units (default: 0.2)",
    )
    parser.add_argument("--db-path", default=None, help="Override DuckDB path")
    parser.add_argument("--output", default="data/backtest_results.json", help="Output JSON path")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument(
        "--state-dir",
        default=None,
        help=(
            "Harness state directory (risk_state_blend.json + kill_switches/). "
            "Default: fresh tempfile.mkdtemp per run, auto-cleaned on success. "
            "Card 758273a7: harness must NEVER write to data/kill_switches/ or "
            "data/risk_state_blend.json unless --allow-foreign-ownership is set."
        ),
    )
    parser.add_argument(
        "--allow-foreign-ownership",
        action="store_true",
        help=(
            "Bypass the foreign-ownership guard on live state files. Operator-only "
            "debugging flag — does NOT bypass the live-PID guard. Card 758273a7."
        ),
    )
    parser.add_argument(
        "--keep-state-dir",
        action="store_true",
        help=(
            "Preserve the harness state dir after a successful run (default: "
            "auto-clean). Useful for post-mortem inspection of isolated "
            "kill_switch/global.state or risk_state_blend.json. Card 758273a7."
        ),
    )
    args = parser.parse_args()

    level = "DEBUG" if args.verbose else "INFO"
    setup_logging(level=level)

    results = run_backtest(args)
    if "error" in results:
        sys.exit(1)


if __name__ == "__main__":
    main()
