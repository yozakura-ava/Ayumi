"""Tournament harness — register strategies, run them unmodified, score FTMO columns.

Card db04d5b5 (walking skeleton).  The harness:

1. Resolves a default DuckDB bar source (data/ayumi_market.duckdb in the
   primary/main worktree; overridable via ``--db-path`` or ``AYUMI_DUCKDB_PATH``).
   The primary-worktree lookup mirrors ``scripts/backtest_blend_harness.py``'s
   isolation-guard resolver so the harness works whether it is invoked from
   main, a feature worktree, or CI without surprising per-tree bar state.
2. Loads USDJPY H1 bars (deterministic smoke slice: 7 days, ~120 bars).
3. For each strategy id provided, instantiates the strategy class from
   ``STRATEGY_CLASS_MAP`` (the canonical id → class registry), walks the
   bars chronologically (each strategy sees a sliding ``MarketState``),
   captures every ``StrategySignal``, and runs a deterministic OHLC-bar
   trade simulator that fills on entry, exits on SL (worst-case) or TP,
   and tracks per-strategy equity curves.
4. Builds a ScorecardRow per strategy via
   :func:`tournament.scorecard.build_scorecard_row`, ranks the rows
   deterministically, and returns the rows + metadata to the caller.

Strategies are NEVER modified.  ``STRATEGY_CLASS_MAP`` is the only place
that maps strategy id to a concrete class; adding a new strategy means
adding one line here, not editing strategy code.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection
    from pandas import DataFrame

import duckdb

from tournament.scorecard import Scorecard, build_scorecard_row

logger = logging.getLogger("ayumi.tournament.harness")

# ── Strategy class registry ─────────────────────────────────────────────────
# Single source of truth mapping strategy_id (as registered in
# ``src.forex_bot.strategies.registry.default_registry()``) to its concrete
# class.  Strategies are imported lazily inside ``_build_strategy_class`` so
# the harness never touches a strategy module that the smoke run does not
# need (card risk 2: side-effect imports).
#
# To add a new strategy to the tournament: add ONE entry here.  Strategy
# source code under src/forex_bot/strategies/ remains unmodified.
STRATEGY_CLASS_MAP: dict[str, str] = {
    # id : "module:class_name"
    # ── Pre-existing (card db04d5b5) ──
    "srmr_plus": "strategies.srmr_plus:SRMRPlusStrategy",
    "bb_rsi_reversion": "strategies.bb_rsi_reversion:BBRSIMeanReversion",
    # ── Card c4b86732 registration sweep (runnable-as-is, config=None pattern) ──
    # Donchian + ATR trend-following (v2 — ISignalStrategy with init/shutdown)
    "donchian_atr_trend_v2": "strategies.donchian_atr_trend_v2:DonchianATRTrendV2Strategy",
    # Dual-timeframe squeeze (H1 context + M15 entry trigger)
    "dual_tf_squeeze_pro": "strategies.dual_tf_squeeze_pro:DualTFSqueezeProStrategy",
    # Killzone momentum (XAUUSD M5/H1 FX)
    "killzone_momentum": "strategies.killzone_momentum:KillzoneMomentumStrategy",
    # London breakout + retest (XAUUSD M15 primary)
    "london_breakout_retest": "strategies.london_breakout_retest:LondonBreakoutRetestStrategy",
    # Momentum trio (donchian / ATR-volatility / MA-trend)
    "momentum_donchian": "strategies.momentum:DonchianBreakoutStrategy",
    "momentum_atr_breakout": "strategies.momentum:ATRVolatilityBreakoutStrategy",
    "momentum_ma_trend": "strategies.momentum:MATrendFollowingStrategy",
    # EURUSD M15 momentum (M15-tuned)
    "momentum_m15": "strategies.momentum_m15:MomentumM15Strategy",
    # RSI threshold crossover (execution-path validation)
    "rsi_threshold": "strategies.rsi_threshold:SimpleRSIThresholdStrategy",
    # Session-range mean reversion
    "session_range_mean_reversion": "strategies.session_range_mean_reversion:SessionRangeMeanReversionStrategy",
    # Session-range MR + ICT confluence filter
    "session_range_mr_ict_filtered": "strategies.session_range_mr_ict_filtered:SessionRangeMRWithICTFilter",
    # TTC XAUUSD M15 (TTSStrategy adapter)
    "ttc_xauusd": "strategies.ttc_xauusd:TTCXAUUSDStrategy",
    # Volatility regime breakout
    "volatility_regime_breakout": "strategies.volatility_regime_breakout:VolatilityRegimeBreakoutStrategy",
    # Volatility squeeze (BB-in-KC)
    "volatility_squeeze": "strategies.volatility_squeeze:VolatilitySqueezeStrategy",
    # Donchian + ATR trend v1 (legacy)
    "donchian_atr_trend_v1": "strategies.donchian_atr_trend:DonchianATRTrendStrategy",
    # ── DEAD/STALE — listed here for triage visibility, NOT registered ──
    # ORBStrategy: requires non-Optional `dict` config (config=None raises).
    # MTFFilteredMomentumStrategy: requires positional `inner_strategy`.
    # SessionBreakoutStrategy: requires non-Optional `dict` config.
}


def _build_strategy_instance(strategy_id: str, symbol: str | None = None):
    """Resolve a strategy id to a live instance via STRATEGY_CLASS_MAP.

    Raises ``KeyError`` for unknown ids, ``ImportError`` for missing modules.
    Returns the constructed instance.

    For strategies whose config can be symbol-aware (e.g. ``SRMRPlusConfig``
    needs ``symbol`` to resolve pip size for USDJPY), the harness passes
    the active symbol so the strategy runs unmodified — no code change
    is ever needed inside src/forex_bot/strategies/.

    Side-effect imports (e.g. yaml loaders) are isolated to first-call;
    subsequent imports hit the module cache.
    """
    if strategy_id not in STRATEGY_CLASS_MAP:
        raise KeyError(
            f"strategy_id '{strategy_id}' is not in STRATEGY_CLASS_MAP "
            f"(known ids: {sorted(STRATEGY_CLASS_MAP)})"
        )

    module_path, class_name = STRATEGY_CLASS_MAP[strategy_id].split(":", 1)
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if strategy_id == "srmr_plus":
        # USDJPY at ~150-160 falls into the "price>=50" branch of
        # ``_resolve_pip_size`` which raises without an explicit symbol.
        # Pass through the active symbol so SRMR+ resolves its pip size
        # identically to the production launcher pattern (no strategy
        # code change needed).
        from strategies.srmr_plus import SRMRPlusConfig

        return cls(config=SRMRPlusConfig(symbol=symbol))

    # Card 9e9aaf30 (adapter fix): strategies in ``STRATEGY_CLASS_MAP`` use
    # heterogeneous constructor shapes — some accept ``config=``, some take
    # positional kwargs (donchian/ATR/MA trio), some take a single
    # ``mr_config``/``ict_config`` pair, and at least one (TTCXAUUSDStrategy)
    # takes no args at all.  The previous blanket ``cls(config=None)`` raised
    # ``TypeError`` for 5 of 17 strategies; the tournament loop swallowed
    # that into ``skipped: True`` so 12 strategies never ran.
    #
    # We now inspect the class ``__init__`` signature and call accordingly.
    # Constraint (AC2): strategy sources stay UNMODIFIED — this is the only
    # place where the cross-strategy shape mismatch is reconciled.
    try:
        init_sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        # Built-in / C-implemented __init__ (rare in this registry); fall
        # back to a no-arg call so we don't pass an unexpected kwarg.
        return cls()

    params = init_sig.parameters
    # Drop the implicit ``self`` parameter.
    has_config_kwarg = "config" in params
    has_any_non_self_kwarg = any(
        name != "self"
        and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        for name, p in params.items()
    )

    if has_config_kwarg:
        # Shape A/B/F: strategies that accept ``config=``. Pass None so the
        # strategy's own defaults apply (matches pre-fix behavior for the
        # srmr_plus / bb_rsi_reversion / donchian_atr_trend_v2 / v1 paths
        # which used ``cls(config=None)`` successfully).
        return cls(config=None)
    if has_any_non_self_kwarg:
        # Shape C/D: positional/kw-only args but no ``config=``. Construct
        # with no args so each strategy's own defaults are used; this is the
        # safest additive change — we never invent config values the
        # strategy author did not specify.
        return cls()
    # Shape E: no-arg constructors (e.g. TTCXAUUSDStrategy monkey-patches
    # module-level constants in its module body, then constructs trivially).
    return cls()


class TournamentEmptyWindow(Exception):
    """Raised when the harness window contains 0 bars."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class TournamentNoSignals(TournamentEmptyWindow):
    """Raised when a registered strategy emits 0 signals over the window.

    Card c4b86732 (fail-loud guard, AC1).  A registered strategy that
    processes 0 bars OR emits 0 signals over the window must never exit
    silently — the harness raises this exception so the CLI surfaces a
    non-zero exit + clear error naming strategy/symbol/window.

    Subclasses :class:`TournamentEmptyWindow` so existing
    ``except TournamentEmptyWindow`` handlers in the CLI catch both
    cases (0 bars in window AND 0 signals per registered strategy).
    """

    def __init__(self, strategy_id: str, symbol: str, timeframe: str,
                 start_date: str | None, end_date: str | None,
                 bars_processed: int, signals_emitted: int):
        detail = (
            f"strategy '{strategy_id}' produced 0 signals on "
            f"{symbol}/{timeframe} window {start_date}..{end_date} "
            f"(bars_processed={bars_processed}, signals={signals_emitted}). "
            f"This indicates a configuration/window mismatch — the harness "
            f"exits non-zero to surface the silent-death failure mode."
        )
        super().__init__(detail)
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.start_date = start_date
        self.end_date = end_date
        self.bars_processed = bars_processed
        self.signals_emitted = signals_emitted


# ── DuckDB path resolution ───────────────────────────────────────────────────


def _main_worktree_root() -> Path | None:
    """Return the PRIMARY (main) worktree's repo root via ``git worktree list``.

    Mirrors ``scripts/backtest_blend_harness.py:_main_worktree_root`` (card
    e1e32b07).  On ANY failure returns ``None`` so the caller can fall back
    to the current tree.  Bare and detached worktrees are skipped so a
    bare-repo layout cannot be selected as the data root.
    """
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],  # noqa: S607
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.debug("_main_worktree_root: git failed (%s); falling back", exc)
        return None
    if completed.returncode != 0:
        logger.debug("_main_worktree_root: git rc=%s; falling back", completed.returncode)
        return None
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
            continue
        if bare or detached:
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


def _project_root() -> Path:
    """Resolve the current tree's repo root (parent of ``src/``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src").is_dir():
            return parent
    # last-resort: cwd
    return Path.cwd()


def resolve_default_duckdb_path() -> Path:
    """Resolve the default DuckDB bar source.

    Search order (first hit wins):

    1. ``AYUMI_DUCKDB_PATH`` env var (explicit override; primary CI hook).
    2. ``<main_worktree>/data/ayumi_market.duckdb`` via ``git worktree list``
       (the same isolation pattern as ``scripts/backtest_blend_harness.py``).
    3. ``<current_tree>/data/ayumi_market.duckdb``.

    Note: even when this function returns a path that exists, callers
    should still treat a missing file as a hard error — duckdb raises
    ``IOException`` on open, which we re-raise as ``TournamentEmptyWindow``
    if the file is absent so the smoke run can surface a clear message.
    """
    env = os.environ.get("AYUMI_DUCKDB_PATH", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    main_root = _main_worktree_root()
    candidates: list[Path] = []
    if main_root is not None:
        candidates.append(main_root / "data" / "ayumi_market.duckdb")
    candidates.append(_project_root() / "data" / "ayumi_market.duckdb")
    for c in candidates:
        if c.is_file():
            return c
    # Default to main_root candidate (or first) so the caller can surface a
    # meaningful "file not found" error at open-time.
    return candidates[0]


# ── Bar loading ──────────────────────────────────────────────────────────────


def load_bars_for_window(
    db_path: Path | str,
    *,
    symbol: str = "USDJPY",
    timeframe: str = "H1",
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[DataFrame, int]:
    """Load bar window from DuckDB into a sorted DataFrame.

    Returns ``(df, raw_row_count)``.  ``raw_row_count`` is the row count
    BEFORE the in-Python window filter — used by callers to detect empty
    vs filtered windows.

    Raises ``TournamentEmptyWindow`` when no rows match the query (covers
    edge case 3: empty dataset path).
    """
    db = Path(db_path)
    if not db.is_file():
        raise TournamentEmptyWindow(
            f"duckdb file not found: {db} — set AYUMI_DUCKDB_PATH or pass --db-path"
        )

    import pandas  # noqa: F401 — type only; duckdb.fetch_df returns a pandas DataFrame

    query = (
        "SELECT timestamp_utc, open, high, low, close, volume, spread_pips "
        "FROM bars WHERE symbol = ? AND timeframe = ?"
    )
    params: list = [symbol.upper(), timeframe]

    if start_date is not None:
        start_ts = int(
            datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        )
        query += " AND timestamp_utc >= ?"
        params.append(start_ts)
    if end_date is not None:
        end_ts = int(
            datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            + 86399  # end-of-day inclusive
        )
        query += " AND timestamp_utc <= ?"
        params.append(end_ts)
    query += " ORDER BY timestamp_utc ASC"

    con: DuckDBPyConnection = duckdb.connect(str(db), read_only=True)
    try:
        df = con.execute(query, params).fetch_df()
    finally:
        con.close()

    if df.empty:
        raise TournamentEmptyWindow(
            f"no {timeframe} bars for {symbol} in window "
            f"(start={start_date}, end={end_date}, db={db})"
        )

    df["time_utc"] = df["timestamp_utc"].apply(
        lambda ts: datetime.fromtimestamp(int(ts), tz=timezone.utc)
    )
    df["date_utc"] = df["time_utc"].dt.date
    return df, len(df)


# ── Strategy import helper ───────────────────────────────────────────────────


# ── Bar loading ──────────────────────────────────────────────────────────────


# ── Trade simulator (deterministic, bar-by-bar) ──────────────────────────────


@dataclass
class _OpenTrade:
    """Internal state for an open position in the simulator."""

    entry_bar_index: int
    entry_price: float
    sl_price: float
    tp_price: float
    direction: int  # +1 long, -1 short
    r_fraction: float  # fraction of equity at risk (e.g. 0.005 = 0.5%)
    rr_target: float  # reward:risk ratio at TP1


def _cost_fraction_for_trade(
    *,
    entry_price: float,
    sl_price: float,
    cost_model: "CostModel",
    r_fraction: float,
    equity: float,
) -> float | None:
    """Compute per-trade cost as a fraction of equity (FTMO sizing).

    Card 05fa0065 precondition: cost-free scoring produces false positives.
    The cost is per-round-turn (entry + exit): the ``CostModel`` captures
    spread + slippage (entry + exit) + commission in dollar terms; this
    helper converts that to an equity fraction so it can be subtracted from
    ``pnl_fraction`` directly.

    qty_lots = (r_fraction × equity) / (sl_distance_pips × pip_value_per_lot)

    cost_per_trade = (spread_pips + 2×slippage_pips) × qty_lots × pip_value_per_lot
                   + commission_per_lot × qty_lots

    cost_fraction = cost_per_trade / equity

    Equity is taken at entry time (v1 approximation — the trade's r_fraction
    * equity is the risk budget, so cost as a fraction of starting equity is
    a tight approximation for the per-trade impact).  When ``sl_distance``
    is zero (degenerate signal), cost_fraction falls back to the worst-case
    ``spread_pips × pip_value_per_lot × qty + commission × qty`` evaluated
    at the trade's symbol-typical 10-pip SL distance to avoid division
    blow-up; this is a defensive fallback, not a primary path (signal
    validation rejects zero-SL upstream).  That fallback still produces a
    real non-zero cost, so the trade stays verdict-eligible.

    Card c0f284b8: this function returns ``None`` — NOT ``0.0`` — when the
    trade cannot be sized.  Returning ``0.0`` used to produce a *silently*
    cost-free trade: the row scored as if the round turn were free, which is
    precisely the false-positive class card 05fa0065 was raised to kill.
    ``None`` means "cost not modeled", and the caller flags the trade
    ``cost_modeled=False`` so the row can be excluded from verdict-grade
    ranking instead of quietly ranking as a winner.
    """
    sl_distance_price = abs(entry_price - sl_price)
    sl_distance_pips = sl_distance_price / cost_model.pip_size if cost_model.pip_size > 0 else 0.0
    if sl_distance_pips <= 0:
        # Defensive: treat as 10 pips so cost stays in a sane range.
        sl_distance_pips = 10.0
    pip_value = cost_model.pip_value_per_lot
    if pip_value <= 0:
        # Can't size the trade — the cost is unknown, not zero.
        return None

    if equity <= 0:
        # No equity to express a fraction of; cost is unknown, not zero.
        return None

    # FTMO sizing: qty lots implied by risk budget.
    qty_lots = (r_fraction * equity) / (sl_distance_pips * pip_value)
    if qty_lots <= 0:
        # Risk budget is zero/non-positive — nothing can be sized.
        return None

    spread_cost = cost_model.spread_pips * qty_lots * pip_value
    slippage_cost = 2.0 * cost_model.slippage_pips * qty_lots * pip_value
    commission_cost = cost_model.commission_per_lot * qty_lots
    cost_per_trade = spread_cost + slippage_cost + commission_cost
    return cost_per_trade / equity


def _simulate_trades(
    df: DataFrame,
    signals: list[tuple[int, float, float, float]],  # (bar_idx, sl, tp1, dir)
    *,
    starting_equity: float = 1.0,
    risk_per_trade: float = 0.005,
    rr_target: float = 1.5,
    max_bars_held: int = 100,
    cost_model: "CostModel | None" = None,
) -> list[dict]:
    """Walk open trades against subsequent bars; emit closed-trade events.

    ``signals`` is a per-strategy tuple of ``(bar_idx, entry_price, sl, tp1, dir)``
    recorded when ``StrategySignal.direction`` was non-None.  When both SL
    and TP would be hit on the same bar, SL wins (worst-case; standard
    conservative venue convention for prop-firm backtests).

    Each closed trade emits a dict with keys ``pnl_fraction``, ``bars_held``,
    ``exit_reason``.  ``pnl_fraction`` is the fraction of equity at entry
    that the trade realized (positive = win, negative = loss).

    Card c0f284b8: every emitted trade also carries ``cost_modeled`` (bool).
    It is ``False`` when no ``cost_model`` was supplied, or when
    :func:`_cost_fraction_for_trade` could not size the trade.  Such trades
    keep ``cost_fraction == 0.0`` for backward compatibility, but the flag is
    what marks them as NOT cost-modeled so callers can exclude them from
    verdict-grade ranking.

    The function NEVER mutates ``df``.  Date partitioning for FTMO daily-DD
    bookkeeping lives in :func:`scorecard.build_scorecard_row`.
    """
    trades: list[dict] = []
    pending: list[_OpenTrade] = []

    def _close_at(trade: _OpenTrade, exit_price: float, reason: str, held: int) -> None:
        delta = (exit_price - trade.entry_price) * trade.direction
        # Convert price delta to "R-units": SL distance defines 1R.
        sl_distance = abs(trade.entry_price - trade.sl_price)
        r_units = delta / sl_distance if sl_distance > 0 else 0.0
        # Trade P&L as a fraction of equity at entry: r_units * r_fraction (1R = +0.5%).
        pnl_fraction = r_units * trade.r_fraction
        # Card c0f284b8: ``_cost_fraction_for_trade`` returns None when the
        # trade cannot be sized.  A None cost is NOT a zero cost — it is an
        # UNMODELED cost, recorded as ``cost_modeled=False`` so the row is
        # excluded from verdict-grade ranking rather than silently scoring
        # as a free round turn.  ``cost_fraction`` stays a float for
        # backward compatibility with existing consumers.
        cost_fraction: float | None = None
        cost_modeled = False
        if cost_model is not None:
            cost_fraction = _cost_fraction_for_trade(
                entry_price=trade.entry_price,
                sl_price=trade.sl_price,
                cost_model=cost_model,
                r_fraction=trade.r_fraction,
                equity=starting_equity,
            )
            cost_modeled = cost_fraction is not None
        # Cost applies to BOTH wins and losses equally (round-turn cost).
        pnl_fraction_net = pnl_fraction - (cost_fraction or 0.0)
        trades.append(
            {
                "pnl_fraction": pnl_fraction_net,
                "pnl_fraction_gross": pnl_fraction,
                "cost_fraction": cost_fraction or 0.0,
                "cost_modeled": cost_modeled,
                "bars_held": held,
                "exit_reason": reason,
                "entry_bar": trade.entry_bar_index,
            }
        )

    for i in range(len(df)):
        opens = df["open"].iloc[i]
        highs = df["high"].iloc[i]
        lows = df["low"].iloc[i]

        # 1. Close any pending trades where SL/TP would have been hit
        next_pending: list[_OpenTrade] = []
        for trade in pending:
            held = i - trade.entry_bar_index
            if held >= max_bars_held:
                _close_at(trade, opens, "max_hold", held)
                continue
            if trade.direction == 1:
                # Long: SL if low <= sl, TP if high >= tp
                if lows <= trade.sl_price and highs >= trade.tp_price:
                    _close_at(trade, trade.sl_price, "sl_first", held)  # worst-case
                elif lows <= trade.sl_price:
                    _close_at(trade, trade.sl_price, "sl", held)
                elif highs >= trade.tp_price:
                    _close_at(trade, trade.tp_price, "tp", held)
                else:
                    next_pending.append(trade)
            else:
                # Short: SL if high >= sl, TP if low <= tp
                if highs >= trade.sl_price and lows <= trade.tp_price:
                    _close_at(trade, trade.sl_price, "sl_first", held)  # worst-case
                elif highs >= trade.sl_price:
                    _close_at(trade, trade.sl_price, "sl", held)
                elif lows <= trade.tp_price:
                    _close_at(trade, trade.tp_price, "tp", held)
                else:
                    next_pending.append(trade)
        pending = next_pending

        # 2. Open any new trades whose signal was generated on bar i
        # (signal bar i means entry at bar i's close = next bar's open convention;
        # for the skeleton, we open at the SAME bar's close to keep the simulation
        # simple and the equity-curve entry semantics unambiguous).
        for sig_idx, sl_price, tp_price, direction in signals:
            if sig_idx != i:
                continue
            entry_price = float(df["close"].iloc[i])
            pending.append(
                _OpenTrade(
                    entry_bar_index=i,
                    entry_price=entry_price,
                    sl_price=float(sl_price),
                    tp_price=float(tp_price),
                    direction=int(direction),
                    r_fraction=risk_per_trade,
                    rr_target=rr_target,
                )
            )

    # Close any trades still open at the end (force-close at last close).
    last_close = float(df["close"].iloc[-1])
    for trade in pending:
        held = (len(df) - 1) - trade.entry_bar_index
        _close_at(trade, last_close, "eod", held)

    return trades


def _extract_signals_from_strategy(
    strategy_id: str,
    df: DataFrame,
    *,
    symbol: str | None = None,
) -> list[tuple[int, float, float, float]]:
    """Walk bars, run the strategy in dry-run mode, capture signals.

    Returns a flat list of tuples: ``(entry_bar_index, stop_loss, take_profit_1,
    direction)``.  ``direction`` is +1 for LONG, -1 for SHORT.

    No trades are placed; this is purely a signal-extraction pass.  The
    actual trade simulation runs separatey so the caller can compare
    signal frequency to trade outcome.

    Card b1bb93e8 — the dominant O(N²) hot path was the harness-side
    list-copy of ``bars_window`` when constructing ``MarketState``;
    each bar's ``MarketState(bars=list(bars_window))`` was O(N), so
    across N bars the harness did N²/2 list copies.  Verified by
    grep across ``src/forex_bot/strategies/*.py``: zero strategies
    mutate ``state.bars`` (no append/extend/insert/remove/pop/clear/
    __setitem__); passing the live ``bars_window`` reference is
    therefore safe and turns the harness from O(N²) to O(N) without
    any strategy-source change.

    The strategies' own indicator walks (ATR / RSI / ADX / EMA in
    each strategy's evaluate()) were already O(period) per call; the
    list-copy in MarketState was the dominant O(N²) component, not
    the indicators.  See ``tests/tournament/test_indicator_cache.py``
    for the indicator-cache module preserved as a future-useful
    additive library (NOT loaded by the harness in this iteration
    because the list-copy fix alone was sufficient and the cache
    introduced equivalence-drift risk across heterogeneous
    implementations).
    """
    from core.types import Bar, MarketState, TradeDirection

    strategy = _build_strategy_instance(strategy_id, symbol=symbol)
    # Card 9e9aaf30 (adapter fix): only ISignalStrategy-style strategies
    # expose ``initialize``/``shutdown``. Non-ISignalStrategy strategies
    # (donchian_atr_trend_v1, killzone_momentum, london_breakout_retest,
    # momentum_m15, session_range_mean_reversion, volatility_regime_breakout,
    # volatility_squeeze, and the momentum trio) take all their config in
    # ``__init__`` and have no onboarding lifecycle.  Guarding with
    # ``hasattr`` keeps the harness's adapter uniform without forcing a
    # shape into the strategy sources.
    if hasattr(strategy, "initialize") and callable(strategy.initialize):
        strategy.initialize({})

    bars_window: list[Bar] = []
    signals: list[tuple[int, float, float, float]] = []
    it_open = df["open"].tolist()
    it_close = df["close"].tolist()
    it_high = df["high"].tolist()
    it_low = df["low"].tolist()
    it_time = df["time_utc"].tolist()
    it_spread = df["spread_pips"].fillna(0.0).tolist()

    bars_processed = 0
    try:
        for i in range(len(df)):
            bar = Bar(
                time=it_time[i],
                open=float(it_open[i]),
                high=float(it_high[i]),
                low=float(it_low[i]),
                close=float(it_close[i]),
                volume=0.0,
                period=None,  # type: ignore[arg-type]
                spread_pips=float(it_spread[i]),
            )
            bars_window.append(bar)
            # Card b1bb93e8: skip the O(N) list-copy of `bars_window`
            # when constructing ``MarketState``.  The strategies
            # read but never mutate ``state.bars`` (verified by
            # grep across ``src/forex_bot/strategies/*.py``: zero
            # append/extend/insert/remove/pop/clear/__setitem__
            # operations on ``state.bars``); passing the live list
            # is therefore safe and turns the harness from O(N²)
            # (cumulative list-copy across N bars) to O(N).  This
            # is the dominant O(N²) bottleneck — the indicator walks
            # in the strategies are already O(period) per call.
            if len(bars_window) < 30:
                continue  # warm-up

            state = MarketState(bars=bars_window)
            signal = strategy.evaluate(state)
            if signal is not None and signal.direction in (
                TradeDirection.LONG,
                TradeDirection.SHORT,
            ):
                direction = 1 if signal.direction == TradeDirection.LONG else -1
                signals.append(
                    (i, signal.stop_loss, signal.take_profit_1, direction)
                )

            # Progress logging — every 1000 bars (card c4b86732 AC4)
            # Observed at the inner-loop granularity so long runs are
            # observable (2026-09-14 GBPUSD run was 2h13m silent without
            # this line).  Logged at INFO so default verbosity shows it.
            bars_processed = i + 1
            if bars_processed % 1000 == 0:
                logger.info(
                    "[tournament.harness] progress strategy=%s bars_processed=%d signals_so_far=%d",
                    strategy_id,
                    bars_processed,
                    len(signals),
                )
    finally:
        if hasattr(strategy, "shutdown") and callable(strategy.shutdown):
            strategy.shutdown()

    return signals


# ── Tournament harness ──────────────────────────────────────────────────────


@dataclass
class CostModel:
    """FTMO-realistic per-round-turn cost model for one symbol.

    Per ``scripts/lbo_cost_stress.py`` and the card 05fa0065 spec:

      cost_per_trade_USD =
          (spread_pips + 2 × slippage_pips) × qty_lots × pip_value_per_lot
        + commission_per_lot × qty_lots

    Where ``qty_lots`` is derived from the FTMO risk budget::

        qty_lots = (r_fraction × equity) / (sl_distance_pips × pip_value_per_lot)

    and ``sl_distance_pips = abs(entry_price - sl_price) / pip_size``.

    All fields are required.  ``pip_size`` is the smallest price increment
    the venue calls a "pip" for this symbol (XAUUSD: 0.10, GBPUSD: 0.0001,
    USDJPY: 0.01, etc.).  ``pip_value_per_lot`` is the dollar value of one
    pip on a 1-lot position (XAUUSD/GBPUSD both = $10 at standard lot).

    The model is per-round-turn: cost is applied once per closed trade,
    regardless of partial exits (partial-exit cost attribution is a v2
    concern; v1 charges cost to the closing event).
    """

    spread_pips: float
    commission_per_lot: float
    slippage_pips: float
    pip_value_per_lot: float
    pip_size: float


# Card 05fa0065 acceptance: "FTMO-realistic costs as a PRECONDITION" —
# cost-free scoring produces false positives (council finding 2026-09-15).
# Per ``scripts/lbo_cost_stress.py`` conventions, the cost model is per-round-
# turn (entry + exit): spread + 2×slippage (entry/exit) + commission.
#
# Card c0f284b8 (Sprint E gate 2) — SINGLE SOURCE OF TRUTH
# -------------------------------------------------------
# Spread / commission / slippage used to be hardcoded here AND in
# ``forex_bot.factory.spread_costs`` (``default_spread_costs``), and the two
# disagreed: this table said XAUUSD 2.5 pips / GBPUSD 2.0 pips while the Liora
# ground-rule table said XAUUSD 3.0 pips / GBPUSD 1.5 pips.  Two copies of a
# cost constant means a matrix verdict can silently run on the wrong number.
#
# The Liora table (``forex_bot.factory.spread_costs``) is now the ONLY place
# these numbers are defined — its own module docstring already claims to be
# "the single source of truth for those defaults".  This module reads it and
# adds only the venue facts the Liora table does not carry (pip_size,
# pip_value_per_lot), which are sizing facts rather than cost assumptions.
#
# The load is defensive only about *how* the module is reached, never about
# *what* it contains: both branches below execute the same
# ``spread_costs.py`` file, so there is still exactly one constant.
#   1. Preferred: normal package import.
#   2. Fallback: direct file load.  Needed because ``forex_bot.factory.__init__``
#      eagerly imports ``bridge`` → ``from backtest.strategies... import ...``,
#      which is broken on this import root (no top-level ``backtest`` module),
#      so ``import forex_bot.factory.spread_costs`` currently raises
#      ModuleNotFoundError.  That is a separate pre-existing defect; the
#      tournament harness must not inherit it.
# If neither path works this raises — never silently falls back to a local
# hardcoded spread, which is the exact defect this card closes.
_LIORA_MODULE_NAME = "ayumi_liora_spread_costs"


def _liora_spread_costs():
    """Return the ``spread_costs`` module (the Liora ground-rule table).

    See the block comment above for why the direct-file fallback exists.
    Raises :exc:`ImportError` if neither route can load the single source of
    truth; it never substitutes a locally hardcoded spread.
    """
    try:
        from forex_bot.factory import spread_costs  # noqa: PLC0415

        return spread_costs
    except ImportError:
        pass

    path = Path(__file__).resolve().parents[1] / "forex_bot" / "factory" / "spread_costs.py"
    if not path.is_file():
        raise ImportError(
            "Liora spread-cost source of truth unreachable: neither "
            "'forex_bot.factory.spread_costs' nor "
            f"{path} could be loaded. Refusing to fall back to a hardcoded "
            "spread constant (card c0f284b8)."
        )
    spec = importlib.util.spec_from_file_location(_LIORA_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"could not build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module uses ``from __future__ import
    # annotations`` + dataclasses, which resolve annotations through
    # sys.modules during class creation.
    sys.modules[_LIORA_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


# pip_size / pip_value_per_lot are venue sizing facts, not Liora cost
# assumptions, so they stay local to the harness.
_VENUE_PIP_FACTS: dict[str, tuple[float, float]] = {
    # symbol: (pip_value_per_lot, pip_size)
    "XAUUSD": (10.0, 0.10),  # 1 pip = $0.10 price move on a 100-oz lot
    "GBPUSD": (10.0, 0.0001),  # 1 pip = $0.0001 on a 100K-base lot
}

_LIORA = _liora_spread_costs()

FTMO_COST_DEFAULTS: dict[str, CostModel] = {
    symbol: CostModel(
        spread_pips=_LIORA.default_spread_costs().get(symbol).spread_pips,
        commission_per_lot=_LIORA.COMMISSION_PER_LOT_USD,
        slippage_pips=_LIORA.PIP_SLIPPAGE,
        pip_value_per_lot=_VENUE_PIP_FACTS[symbol][0],
        pip_size=_VENUE_PIP_FACTS[symbol][1],
    )
    for symbol in _VENUE_PIP_FACTS
}


def cost_model_for(symbol: str) -> CostModel | None:
    """Return the canonical FTMO cost model for ``symbol``, or ``None``.

    Spread/commission/slippage come from the Liora ground-rule table
    (:func:`forex_bot.factory.spread_costs.default_spread_costs`), never a
    local literal (card c0f284b8).

    Symbols without a known cost model return ``None``; this mirrors the LBO
    cost-stress study which only covers XAUUSD/GBPUSD; other symbols will need
    their own cost-model row before being promoted to FTMO-realistic scoring.
    A ``None`` model means the run is UNMODELED, not free: the harness marks
    those rows ``cost_modeled=False`` (card c0f284b8) so they cannot be
    mistaken for cost-free winners.
    """
    return FTMO_COST_DEFAULTS.get(symbol.upper())


def verdict_grade_rows(rows):
    """Return only the rows whose costs were actually modeled.

    Card c0f284b8 AC2: a row that could not be costed must never enter a
    verdict-grade ranking.  This is the single place that answers "is this row
    eligible to be ranked?", so the tournament matrix (card 37078a83) and any
    later verdict consumer share one definition instead of re-deriving it.
    """
    return [row for row in rows if getattr(row, "cost_modeled", True)]


@dataclass
class TournamentHarness:
    """Register strategies + run them on a single window.

    Parameters
    ----------
    strategy_ids
        Ordered list of strategy ids (must all be keys of
        :data:`STRATEGY_CLASS_MAP`).
    db_path
        DuckDB bar source.  ``None`` triggers
        :func:`resolve_default_duckdb_path`.
    symbol, timeframe
        Bar window selector.
    start_date, end_date
        Optional ``YYYY-MM-DD`` bounds.
    starting_equity
        Normalized starting equity for the simulator (default 1.0;
        scorecard returns ``return_pct`` as a fraction of starting equity
        so the absolute value is irrelevant — present for future cap
        extension).
    """

    strategy_ids: list[str]
    db_path: Path | str | None = None
    symbol: str = "USDJPY"
    timeframe: str = "H1"
    start_date: str | None = None
    end_date: str | None = None
    starting_equity: float = 1.0
    cost_model: CostModel | None = None

    # populated by ``run()``
    source: str = field(default="", init=False)
    bars_loaded: int = field(default=0, init=False)
    bar_window_meta: dict = field(default_factory=dict, init=False)

    # ── Self-checks (cheap, run at construction) ─────────────────────────
    def __post_init__(self) -> None:
        if not self.strategy_ids:
            raise ValueError("strategy_ids must be non-empty (edge case 1)")
        unknown = [s for s in self.strategy_ids if s not in STRATEGY_CLASS_MAP]
        if unknown:
            raise KeyError(
                f"unknown strategy_ids: {unknown} (known: {sorted(STRATEGY_CLASS_MAP)})"
            )
        # Preserve insertion order, dedupe
        seen: set[str] = set()
        deduped: list[str] = []
        for sid in self.strategy_ids:
            if sid not in seen:
                deduped.append(sid)
                seen.add(sid)
        self.strategy_ids = deduped

    # ── The run ──────────────────────────────────────────────────────────
    def run(self) -> Scorecard:
        """Run all strategies on the window; return ranked scorecard rows.

        Raises ``TournamentEmptyWindow`` if the loaded bar window is empty.
        """
        if shutil.which("git") is None:
            # Tournament-specific guard: data-source resolution depends on
            # ``git worktree list``, but ``load_bars_for_window`` accepts an
            # explicit path too.  Log the absence; do not hard-fail.
            logger.debug("git binary not on PATH — main-tree duckdb auto-resolve disabled")

        duckdb_path = (
            Path(self.db_path)
            if self.db_path is not None
            else resolve_default_duckdb_path()
        )
        self.source = str(duckdb_path)

        df, raw_count = load_bars_for_window(
            duckdb_path,
            symbol=self.symbol,
            timeframe=self.timeframe,
            start_date=self.start_date,
            end_date=self.end_date,
        )
        self.bars_loaded = len(df)
        self.bar_window_meta = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "raw_duckdb_rows": raw_count,
            "bars_loaded": int(len(df)),
            "first_bar_utc": df["time_utc"].iloc[0].isoformat(),
            "last_bar_utc": df["time_utc"].iloc[-1].isoformat(),
        }

        rows = []
        run_meta: dict[str, dict] = {}
        for strategy_id in self.strategy_ids:
            try:
                signals = _extract_signals_from_strategy(
                    strategy_id, df, symbol=self.symbol
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "strategy %s raised %s during signal extraction — skipping: %s",
                    strategy_id,
                    exc.__class__.__name__,
                    exc,
                )
                run_meta[strategy_id] = {
                    "signals": 0,
                    "trades": 0,
                    "skipped": True,
                    "reason": "signal_extraction_error",
                    "error": str(exc),
                }
                continue

            # Per-cell fail-loud guard (card 82d33f73, council binding #1+#3).
            # A registered strategy that processes 0 bars OR emits 0
            # signals over the window must NOT silently die — the loop
            # must mark this strategy as `signals_skipped=True` with the
            # strategy/symbol/window context in `error`, then continue
            # to the next strategy.  This unifies the silent-empty path
            # (was: import-error swallowed by `except Exception`, 0 rows,
            # exit 0) with the fail-loud path (was: signal-empty raised
            # TournamentNoSignals, exit 1).  Both now produce 0 rows for
            # this strategy; the CLI checks `if not scorecard.rows`
            # AFTER the loop and exits non-zero if every strategy was
            # empty (single source of truth).
            #
            # Council binding #3 (Kaito/Sora 2026-10-04): tolerate
            # TournamentNoSignals per-cell as skip-and-continue, not
            # card-blocking.  The matrix driver already handles per-cell
            # failures (worker_runner exit_code=3/4); this change makes
            # the single-cell CLI consistent.
            bars_processed = len(df)
            if bars_processed == 0 or len(signals) == 0:
                reason = (
                    "warmup_window_too_small"
                    if bars_processed == 0
                    else "no_signals"
                )
                logger.error(
                    "fail-loud guard: strategy=%s symbol=%s window=%s..%s "
                    "bars_processed=%d signals=%d — skip-and-continue (reason=%s)",
                    strategy_id, self.symbol, self.start_date, self.end_date,
                    bars_processed, len(signals), reason,
                )
                run_meta[strategy_id] = {
                    "signals": 0,
                    "trades": 0,
                    "skipped": True,
                    "reason": reason,
                    "bars_processed": bars_processed,
                    "error": (
                        f"strategy '{strategy_id}' produced 0 signals on "
                        f"{self.symbol}/{self.timeframe} window "
                        f"{self.start_date}..{self.end_date} "
                        f"(bars_processed={bars_processed}, signals=0)"
                    ),
                }
                continue

            trades = _simulate_trades(
                df, signals, cost_model=self.cost_model,
                starting_equity=self.starting_equity,
            )
            row = build_scorecard_row(
                strategy_id=strategy_id,
                symbol=self.symbol,
                timeframe=self.timeframe,
                starting_equity=self.starting_equity,
                trades=trades,
                dates=[d.isoformat() for d in df["date_utc"].tolist()],
                trade_entry_bars=[t["entry_bar"] for t in trades],
                source=self.source,
            )
            run_meta[strategy_id] = {
                "signals": len(signals),
                "trades": len(trades),
                "skipped": False,
            }
            rows.append(row)

        if not rows:
            # Edge case 1/2: every strategy was skipped or produced 0
            # signals.  Emit an empty scorecard; the CLI / worker_runner
            # applies the loud-exit guard (this is the single source of
            # truth for the silent-empty failure mode — card 82d33f73).
            logger.warning(
                "no scorecard rows produced (all %d strategies skipped or 0-signals)",
                len(self.strategy_ids),
            )

        ranked = self._scorecard_with_rank_or_unranked(rows)
        # Attach run metadata as a side-channel (deprecated part of the row schema).
        ranked.meta = {**ranked.meta, "run_meta": run_meta, "harness_meta": self.bar_window_meta}
        return ranked

    @staticmethod
    def _scorecard_with_rank_or_unranked(rows):
        """Return the scorecard ranked if a per-trial series is available.

        Sprint C card cc90a6b6 (1b.3) deprecates
        :func:`tournament.scorecard.rank_scorecard_rows` — the raw-return
        sort is replaced by Benjamini-Hochberg FDR on per-trial return
        series (see :mod:`forex_bot.factory.risk_adjusted_ranking`).

        The standalone :class:`TournamentHarness` runs a single bar
        window and does not have Optuna trial repetitions, so it cannot
        build the ``[T, N]`` matrix the new ranker needs.  Two options
        were considered:

        * Call the deprecated ``rank_scorecard_rows`` anyway and let
          it raise — this breaks every harness-integration test on
          every run, which is the silent-use problem the deprecation is
          supposed to solve.
        * Skip ranking here; the scorecard still carries every row,
          and the rank field is left at the default ``0`` for downstream
          consumers that do have per-trial data to wire in.

        The harness picks option (2): it produces an unranked
        :class:`Scorecard` for the standalone path.  Factory-driven
        callers (:func:`tournament.front_door.run_front_door`,
        :class:`forex_bot.factory.validation_runner.ValidationRunner`)
        own the ranking step now and must call
        :func:`forex_bot.factory.risk_adjusted_ranking.rank_from_trial_return_store`
        directly.
        """
        from tournament.scorecard import Scorecard

        return Scorecard(rows=list(rows), meta={})
