"""Forward Test Engine — wires cTrader live market data into the PaperTrader.

Bridges ``LiveMarketDataFeed`` (tick streaming) with ``PaperTrader`` (signal
processing / risk / P&L tracking) and ``cTraderLiveAdapter`` (strategy
evaluation) so the forward test runs on real market data end-to-end.

Usage::

    engine = ForwardTestEngine(config, strategies=[my_strategy])
    engine.start()
    ...
    engine.stop()
"""

import json
import logging
import os
import signal as sig_module
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .market_data_feed import LiveMarketDataFeed

from backtest.engine import Bar, MarketState
from backtest.strategies import ISignalStrategy
from backtest.types import SessionType, determine_session

# Confidence engine for live-fire gating
from confidence.engine import ConfidenceEngine
from confidence.gates import GateConfig
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)

# Phase 1b: BehavioralPolicy — streak + drawdown cooldown size multiplier
# applied to live signals after KillCriteria and before _execute_signal_live.
from policy.behavioral import BehavioralPolicy

# Phase 1c: KillCriteriaChecker — global+per-strategy criterion evaluator
# wired into the live-fire gate after the ConfidenceEngine pass.
from policy.kill_criteria import KillCriteriaChecker

# Import the canonical trading-date helper from risk.ftmo_guard.
# The daily reset boundary is 00:00 America/Toronto (Craig decision Jul 17).
from risk.ftmo_guard import _trading_date

# Phase 0 forward-test diagnostics — see signal_engine/signal_stats.py
from signal_engine.signal_stats import SignalRecord, SignalStatsRecorder

# Lazy-import to avoid an import cycle at module load: api_client imports from
# open_api_spot_feed which itself has no circular dep, but keeping the import
# local lets tests patch the module path before the class is resolved.
from .api_client import cTraderAPIClient  # noqa: E402
from .connection_state import ConnectionState
from .credential_store import CredentialStore
from .kill_switch import KillSwitchManager
from .market_data_feed import Tick
from .models import (
    CTraderTradeSignal,
    OrderStatus,
    TradeDirection,
    cTraderCredentials,
    get_symbol_info,
)
from .open_api_spot_feed import OpenApiSpotFeed
from .order_manager import PositionSizeConfig
from .paper_trader import PaperTrader
from .position_monitor import PositionMonitor
from .risk_guard import FTMOConfig
from .signal_adapter import cTraderLiveAdapter
from .token_lifecycle import TokenLifecycle
from .trade_logger import TradeLogger

logger = logging.getLogger("ayumi.forward_test")

_DEFAULT_RECONNECT_DELAY_SEC = 5.0
_DEFAULT_MAX_RECONNECT_DELAY_SEC = 120.0
_DEFAULT_STALE_TICK_THRESHOLD_SEC = 300.0
_DEFAULT_MAX_RECONNECT_ATTEMPTS = 20
# Card f37e7b74: sustained-tick gate for reconnect-success classification.
# cTrader sends exactly ONE snapshot spot event per ProtoOASubscribeSpotsReq,
# so a naive "any tick" reset condition lets a subscribed-but-silent broker
# farm one tick → counter reset → health-check fires → reconnect (flap loop).
# Gate: counter resets only after this many ticks arrive within
# _DEFAULT_SUSTAINED_TICKS_WINDOW_SEC seconds of a successful reconnect.
_DEFAULT_SUSTAINED_TICKS_REQUIRED = 3  # 7d3b535d iter2: 5 -> 3 (faster recovery from snapshot-only state)
_DEFAULT_SUSTAINED_TICKS_WINDOW_SEC = 60.0  # 7d3b535d iter2: 120 -> 60s (gate before 24m secondary flap)
# Proactive session rotation: cTrader Open API closes authenticated sessions on
# a server-side ~24h rolling window measured from auth time. Waiting for the
# server-side cap to land produces a chaotic reconnect storm (card 7d3b535d;
# 4 restart deaths observed Sep 8-10 at the 21:21 UTC + 21:45 UTC daily
# double-flap). Rotating proactively 30 min before the cap gives a clean,
# predictable exit so systemd's auto-restart is the only churn — no flap.
_DEFAULT_PROACTIVE_ROTATION_SEC = 23 * 3600 + 30 * 60  # 23h30m — well under the 24h server cap.
# PROJECT_ROOT mirrors .env resolution at line ~1000: Path(__file__).resolve().parents[4]
#   parents[0] = ctrader/  parents[1] = adapters/  parents[2] = forex_bot/
#   parents[3] = src/      parents[4] = <repo root>
_PROJECT_ROOT = Path(__file__).resolve().parents[4]

# Phase 7: live-mode validation gate.  Operator (Ava) creates this flag after
# remediation is validated; the launcher refuses live mode if it is absent.
# Absolute paths ensure auto-recreate works from any CWD (fixes PermissionError
# when the service is launched from a non-project working directory).
_REMEDIATION_VALIDATED_FLAG = str(_PROJECT_ROOT / "data" / "ayumi" / "remediation_validated.flag")
# Fallback: the audit doc is the source of truth. If the flag file is missing
# (e.g. deleted by git clean, systemd cleanup, or process restart) but the
# audit doc exists, the flag is auto-recreated with a warning instead of
# crashing. This makes the forward test survivable across unplanned restarts.
_REMEDIATION_AUDIT_DOC = str(_PROJECT_ROOT / "docs" / "audits" / "ayumi-live-remediation-session-audit-2026-06-30.md")


# Heartbeat writer defaults
_HEARTBEAT_FILE = "data/heartbeat_trading.json"
_HEARTBEAT_INTERVAL_SEC = 5.0  # piggybacks on health monitor loop

# Error rate monitor defaults
_ERROR_RATE_WINDOW_SEC = 60.0
_ERROR_RATE_THRESHOLD_PCT = 0.50  # >50% error rate in 60s window → freeze


def _safe_attr(obj: object, attr: str, default):
    """Read ``obj.attr`` and fall back to ``default`` for unset / mock attrs.

    Card 0d64bec9: the heartbeat write must succeed on bare / partial
    engine instances (where ``ForwardTestEngine.__new__`` skips
    ``__init__``) and on MagicMock fixtures used by
    ``TestHeartbeatAtomicWrite``. Both cases raise ``TypeError`` on
    ``json.dumps`` because the values are either missing or are
    auto-created ``MagicMock`` instances that are not JSON-serializable.

    A plain ``getattr(obj, attr, default)`` is NOT sufficient because
    ``MagicMock`` auto-creates attributes on access — it never raises
    ``AttributeError`` so the default is never returned. We detect
    ``MagicMock`` (via the private ``_mock_name`` marker that all
    MagicMock instances carry, and via ``unittest.mock.Mock`` isinstance
    for the broader family) and fall back to the default.

    In production, ``_health`` is a real ``ForwardTestHealth`` dataclass
    instance whose attributes are concrete values, so this helper is a
    no-op pass-through. The defensive check exists purely so the
    heartbeat write never fails because of a missing or mock-typed field
    — which the watchdog would otherwise misread as a stale/missing file
    and fire a false ``heartbeat_stale`` kill.
    """
    try:
        from unittest.mock import Mock
    except ImportError:
        Mock = None  # type: ignore[assignment]
    try:
        val = getattr(obj, attr, default)
    except Exception:
        return default
    if Mock is not None and isinstance(val, Mock):
        return default
    # MagicMock exposes ``_mock_name`` on every instance; use it as a
    # belt-and-suspenders signal even if Mock import failed.
    if hasattr(val, "_mock_name") and hasattr(val, "_mock_methods"):
        return default
    return val

# Single source of truth for market-close detection lives in .market_hours.
# Local alias preserves the existing call sites without renaming.
from .market_hours import is_forex_market_closed as _is_forex_market_closed


@dataclass
class ForwardTestConfig:
    symbol: str = "GBPUSD"
    symbols: list[str] = None  # multi-symbol support; if None, defaults to [symbol]
    starting_balance: float = 100_000.0
    min_confidence: float = 0.50
    max_bars_per_symbol: int = 500
    min_bars_for_evaluation: int = 50
    quote_host: str = "live-uk-eqx-01.p.c-trader.com"
    quote_port: int = 5211
    use_ssl: bool = True
    quote_sender_sub_id: str = "QUOTE"
    quote_target_sub_id: Optional[str] = None
    log_dir: str = "logs/trades"
    stats_interval_sec: float = 60.0
    live_mode: bool = False
    execution_mode: str = "paper"  # "paper" | "live" — must be explicit
    live_fire_min_confidence: float = 0.55  # Lowered per Phase 4 — allow more signals through (was 0.65)
    trade_host: Optional[str] = None
    trade_port: Optional[int] = None
    evaluation_interval_sec: float = 1.0
    bar_period_minutes: int = 60
    # BQ-1335: Lowered from 900.0 to 60.0 so a stuck connection (no ticks) triggers reconnect
    # within 1 minute instead of 15 minutes. False reconnects during quiet markets are
    # acceptable trade-off — better than letting a connection stay dead for 15+ minutes.
    stale_tick_threshold_sec: float = 60.0
    reconnect_delay_sec: float = _DEFAULT_RECONNECT_DELAY_SEC
    max_reconnect_delay_sec: float = _DEFAULT_MAX_RECONNECT_DELAY_SEC
    max_reconnect_attempts: int = _DEFAULT_MAX_RECONNECT_ATTEMPTS
    # Card f37e7b74: sustained-tick gate thresholds. The reconnect counter
    # only resets once sustained_ticks_required ticks have arrived within
    # sustained_ticks_window_sec seconds of the last successful reconnect.
    # The single snapshot spot event from ProtoOASubscribeSpotsReq is
    # explicitly NOT enough — that is what allowed the reconnect-flap loop.
    sustained_ticks_required: int = _DEFAULT_SUSTAINED_TICKS_REQUIRED
    sustained_ticks_window_sec: float = _DEFAULT_SUSTAINED_TICKS_WINDOW_SEC
    # Card 7d3b535d (rework of f37e7b74 INSUFFICIENT verdict): proactive
    # 24h session rotation. The cTrader Open API closes server-side
    # sessions on a 24h rolling window; rotating 30 min before the cap
    # is the primary mitigation against the 21:21 UTC daily primary flap.
    proactive_rotation_sec: float = _DEFAULT_PROACTIVE_ROTATION_SEC
    health_monitor_interval_sec: float = 5.0  # Runs every 5s for heartbeat + error monitoring
    clear_stuck_positions_on_start: bool = False
    reset_on_start: bool = False
    strategy_timeframes: dict[str, int] = (
        None  # strategy_name -> period_minutes; empty/None = all use bar_period_minutes
    )
    use_openapi_feed: bool = False  # True = Open API spot feed, False = FIX feed
    openapi_host: str = "demo.ctraderapi.com"
    openapi_port: int = 5035
    preload_bar_count: int = 200  # bars fetched per symbol/timeframe on startup

    def __post_init__(self):
        # Consistency check: live_mode=True implies execution_mode="live"
        if self.live_mode and self.execution_mode != "live":
            self.execution_mode = "live"
        if self.strategy_timeframes is None:
            self.strategy_timeframes = {}
        if self.symbols is None:
            self.symbols = [self.symbol]


@dataclass
class ForwardTestHealth:
    connected: bool = False
    last_tick_at: Optional[datetime] = None
    ticks_received: int = 0
    ticks_per_second: float = 0.0
    signals_generated: int = 0
    signals_traded: int = 0
    signals_rejected: int = 0
    uptime_sec: float = 0.0
    evaluation_errors: int = 0
    reconnection_attempts: int = 0
    reconnection_successes: int = 0
    # Last reconnect failure reason (set by the health monitor before
    # _attempt_reconnect so diagnostic logs can surface why we're
    # retrying). Added for card ecbecd01 reconnect instrumentation.
    last_error: Optional[str] = None
    bars_built: int = 0
    consecutive_risk_rejections: int = 0
    # T1/T2/T7 — counters distinguishing sent / pending / filled / failed live
    # orders. ``signals_traded`` is retained as a backwards-compatible alias
    # for the FILLED count.
    signals_sent: int = 0
    signals_failed_live: int = 0
    signals_pending: int = 0
    signals_cancelled: int = 0
    # Card ce6de98d (B): indeterminate timeout counter. Incremented when
    # the spot feed's event.wait expires (event.wait returned False) but
    # the order may still arrive via late_fill_registry. Distinct from
    # signals_sent (genuine SENT awaiting ack) and signals_failed_live
    # (terminal REJECTED). Operators see this as a diagnostic for
    # "broker has not yet acknowledged" — late-fill callbacks downgrade
    # indeterminate to FILLED if the order eventually confirms.
    signals_indeterminate: int = 0
    signals_accepted: int = 0  # blend runner accepted the signal (T2)
    # Card 0d7d7557 (iter2): NOT_CONNECTED attempts (broker unreachability,
    # pre- or post-contact). Distinct from signals_failed_live (broker
    # rejection: REJECTED / CANCELLED outcomes), so the B5 warning can
    # tell operators "broker unreachable" vs "broker rejected" instead
    # of conflating both into the rejected bucket. Counted in the daily
    # reset family — operators want to see per-trading-day unreachability
    # churn, and reset_daily_counters zeroes it alongside signals_sent
    # and signals_failed_live.
    signals_unreachable: int = 0
    # Card 0d7d7557: regime-gate rejection counter. Bumped by the
    # eval loop in scripts/launch_blend_forward_test.py when the
    # regime-gate check rejects a strategy emit (choppy / trending
    # mismatch). IS a daily-reset counter — it is read-path
    # observability for the B5 health line and reset_daily_counters
    # zeroes it alongside the rest of the daily family, so operators
    # can see per-trading-day gate churn. (Card 0d7d7557 iter2 L4: the
    # original comment incorrectly said "NOT a daily-reset counter";
    # this field IS in the daily-reset family.)
    signals_filtered_by_regime_gate: int = 0
    symbol_resolution_failures: int = 0  # total failed symbol resolutions (Task 2)
    # Phase 1d wiring — diagnostics for KillCriteria + BehavioralPolicy gates.
    # ``signals_killed_by_criteria`` counts signals blocked by any
    # ``KillCriterion.triggered=True`` rule (spread / macro / per-strategy).
    # ``behavioral_adjustments`` counts signals whose size was reduced below
    # 1.0× by ``BehavioralPolicy.evaluate`` (streak or DD cooldown).
    # ``last_kill_reasons`` and ``last_behavioral_multiplier`` snapshot the
    # most recent gate decision for live-dashboard / log scrapers.
    signals_killed_by_criteria: int = 0
    behavioral_adjustments: int = 0
    last_kill_reasons: list = field(default_factory=list)
    last_behavioral_multiplier: float = 1.0
    # Health-observability fields (cards 45aad19f / 2bb667ce / a8a757c4).
    # ``live_fills`` counts every confirmed fill (synchronous or late-callback),
    # replacing the ad-hoc ``_live_fill_count`` attribute.  ``seeded_positions``
    # counts carry-over positions discovered at startup via broker reconcile.
    # ``last_rejection_errorcode`` and ``rejection_breakdown`` surface the
    # broker's errorCode so operators don't have to grep 30K+ log lines.
    live_fills: int = 0
    seeded_positions: int = 0
    last_rejection_errorcode: str = ""
    rejection_breakdown: dict[str, int] = field(default_factory=dict)
    # Last trading-day observed for daily counter reset. Reset to None on
    # process restart; the B5 health loop resets the daily counters when
    # this lags the current trading day (see 17:00 America/Toronto boundary
    # in risk_guard._TRADING_DAY_RESET_HOUR).
    _last_health_trading_day: Optional[date] = field(default=None, repr=False, compare=False)

    def reset_daily_counters(self) -> None:
        """Reset per-trading-day counters to zero.

        Mirrors risk_guard._current_trading_day() boundary (17:00
        America/Toronto). Counters touched (in order of declaration):
        signals_sent, signals_failed_live, signals_pending,
        signals_cancelled, signals_accepted, signals_unreachable,
        signals_filtered_by_regime_gate, signals_rejected,
        signals_traded.

        NOTE: signals_generated is intentionally NOT reset — it is a
        lifetime diagnostic of strategy productivity across the session,
        not a daily guardrail metric. See BQ-1175 follow-up discussion.

        Card 0d7d7557 iter2 L4: docstring previously omitted
        ``signals_filtered_by_regime_gate`` (the comment on the field
        even incorrectly said it was NOT a daily-reset counter). Both
        ``signals_filtered_by_regime_gate`` (regime-gate churn) and
        ``signals_unreachable`` (broker unreachability churn) are reset
        here so operators see per-trading-day gate/unreachability
        counters in the B5 health line, not session-lifetime totals.
        """
        self.signals_sent = 0
        self.signals_failed_live = 0
        self.signals_pending = 0
        self.signals_cancelled = 0
        self.signals_accepted = 0
        self.signals_unreachable = 0
        self.signals_filtered_by_regime_gate = 0
        self.signals_rejected = 0
        self.signals_traded = 0
        # Card ce6de98d (B): reset the new indeterminate counter alongside
        # the other daily counters so operators see per-trading-day
        # indeterminate churn in the B5 health line (rather than
        # session-lifetime totals that conflate today's broker stalls
        # with last week's).
        self.signals_indeterminate = 0


class LiveExecutionStatus(Enum):
    """Terminal state of a live order placement attempt.

    Used by ``_execute_signal_live`` to disambiguate the failure modes of
    ``OpenApiSpotFeed.new_order``. The status is what the caller should
    use to decide whether to count the attempt as a fill, a sent-but-pending
    acknowledgement, or a definitive failure.

    Values:
        FILLED        — cTrader confirmed the execution event and the order
                        is filled. Caller should increment the live-fills
                        counter and release the correlation gate.
        SENT          — the order was sent to cTrader but no execution event
                        has arrived yet. The engine should NOT count this as
                        a fill. Late events are delivered via the spot feed's
                        ``on_order_filled`` / ``on_order_rejected`` /
                        ``on_order_cancelled`` callbacks (see ``_wire_live_fill_callbacks``).
        REJECTED      — cTrader explicitly rejected the order (or the spot
                        feed was not operational). Caller should log a
                        warning and release correlation + risk.
        TIMEOUT       — the order was sent but no execution event arrived
                        within the spot feed's ``_ORDER_TIMEOUT_SEC`` window.
                        Caller should log a warning and release correlation + risk.
        NOT_CONNECTED — the spot feed was not operational at send time.
                        Caller should log a warning and release correlation + risk.
        CANCELLED     — cTrader sent ``ORDER_CANCELLED`` (manual cancel, GTD
                        expiry, etc). Caller should log a warning and release
                        correlation + risk.
        SKIPPED       — pre-flight failure (no feed, unknown symbol, zero
                        volume). The caller does not get an outcome object;
                        ``_execute_signal_live`` returns ``None`` directly.
    """

    FILLED = "filled"
    SENT = "sent"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    NOT_CONNECTED = "not_connected"
    CANCELLED = "cancelled"


@dataclass
class LiveExecutionOutcome:
    """Result of a single ``_execute_signal_live`` invocation.

    Attributes:
        status: The terminal status — see :class:`LiveExecutionStatus`.
        order: The Order object returned by ``OpenApiSpotFeed.new_order``.
                May be ``None`` if the order could not be constructed at all
                (e.g. unknown symbol). The caller can log ``order.order_id``
                and ``order.comment`` for traceability.
        symbol: The trade symbol (for late-callback correlation).
        direction: BUY or SELL (for late-callback correlation).
        strategy_id: The strategy that produced the signal (for late-callback).
        reason: A human-readable reason — e.g. ``"timeout_awaiting_event"``
                or the broker's ``errorCode``. Empty string if unknown.
    """

    status: LiveExecutionStatus
    order: Optional[object] = None
    symbol: str = ""
    direction: str = ""
    strategy_id: str = ""
    reason: str = ""


class ForwardTestEngine:
    # INVARIANT: Strategy evaluation only occurs on CLOSED bars.
    # - self._bars contains finalized bars only
    # - self._current_bar contains the FORMING bar (excluded from evaluation)
    # - Evaluation triggers ONLY when _bar_completed flag is set (bar just finalized)
    # - Never pass self._current_bar into MarketState or strategy evaluation

    # Allowed timeframe whitelist
    _ALLOWED_TIMEFRAMES = {15, 60, 240}

    # Card dcc7817d (3/3): ticks-flow-but-bars-static watchdog threshold.
    # 20 minutes per Satsuki's M15-boundary correction (15 min would
    # fire at every M15 close under low tick rate). Class-level constant
    # so the health-monitor loop, the heartbeat writer, and any future
    # watchdog consumer read the same value.
    _BARS_STATIC_THRESHOLD_SEC = 1200.0
    # Rate-limit between consecutive BARS STATIC WARNINGs (s).
    _BARS_STATIC_WARN_INTERVAL_SEC = 300.0

    @staticmethod
    def _bar_key(symbol: str, period_minutes: int) -> str:
        return f"{symbol}:{period_minutes}"

    def __init__(
        self,
        config: ForwardTestConfig,
        strategies: list[ISignalStrategy],
        ftmo_config: Optional[FTMOConfig] = None,
        position_config: Optional[PositionSizeConfig] = None,
        credentials: Optional[cTraderCredentials] = None,
        *,
        blend_mode: bool = False,
        slot_tracker: Optional[Callable[[str, float], None]] = None,
    ):
        self._config = config
        self._strategies = strategies
        self._ftmo_config = ftmo_config
        self._position_config = position_config
        self._blend_mode = blend_mode
        # Card 4083ac2d-...: optional observer for live SL amendments.
        # Stored on the engine and forwarded to PositionMonitor in
        # _build_components so the per-symbol at-risk cap stays accurate
        # after each successful broker amend. Mirrors the slot_tracker
        # constructor arg on PositionMonitor.
        self._slot_tracker = slot_tracker
        self._running = False
        self._lock = threading.RLock()
        self._eval_semaphore = threading.Semaphore(1)

        # Derive required timeframes
        self._strategy_timeframes: dict[str, int] = config.strategy_timeframes or {}
        self._required_timeframes: set[int] = (
            set(self._strategy_timeframes.values()) if self._strategy_timeframes else {config.bar_period_minutes}
        )

        # Startup config fail-fast: whitelist check.
        # These were previously ``assert`` statements but asserts are stripped
        # under ``python -O``. Misconfigured timeframes/strategies must crash
        # at startup on every Python invocation, not only debug runs — converted
        # to explicit raises so the fail-fast gate survives optimization.
        for tf in self._required_timeframes:
            if tf not in self._ALLOWED_TIMEFRAMES:
                raise ValueError(
                    f"Timeframe {tf} not in allowed whitelist {self._ALLOWED_TIMEFRAMES}"
                )

        # Startup config fail-fast: every key in strategy_timeframes must
        # match a registered strategy .name
        if self._strategy_timeframes:
            registered_names = {s.name for s in strategies}
            for stg_name in self._strategy_timeframes:
                if stg_name not in registered_names:
                    raise ValueError(
                        f"strategy_timeframes key '{stg_name}' does not match any registered "
                        f"strategy .name property. Registered: {sorted(registered_names)}"
                    )

        self._bars: dict[str, list[Bar]] = {}  # key = _bar_key(symbol, period_minutes)
        self._current_bar: dict[str, Optional[Bar]] = {}  # same key scheme
        self._paper_trader: Optional[PaperTrader] = None
        self._position_monitor: Optional[PositionMonitor] = None
        self._token_lifecycle: Optional[TokenLifecycle] = None
        self._market_feed: Optional[LiveMarketDataFeed] = None
        self._live_adapter: Optional[cTraderLiveAdapter] = None
        self._trade_logger: Optional[TradeLogger] = None
        self._live_client = None
        self._api_client = None  # T4: cTraderAPIClient wrapper (None in paper mode)
        self._preload_complete: bool = False  # T2: blocks evaluation until bars loaded
        self._credentials = credentials

        self._start_time: Optional[datetime] = None
        self._tick_timestamps: list[datetime] = []
        self._tick_rate_window_sec = 10.0

        self._callbacks: list[tuple[str, "Callable"]] = []
        self._health = ForwardTestHealth()
        # Consecutive stats recording failures (resets on success).
        self._stats_fail_count: int = 0
        # Cached confidence from last successful stats recording.
        self._last_known_good_confidence: Optional[float] = None

        # Retry configuration for stats recording (env-overridable)
        self._stats_retry_max: int = int(os.environ.get("STATS_RETRY_MAX", "3"))
        self._stats_retry_base_delay: float = float(os.environ.get("STATS_RETRY_BASE_DELAY", "2.0"))

        self._last_evaluation_at: float = 0.0

        # Card dcc7817d (2/3): Capture the process-init monotonic clock so
        # per-strategy ``_strategy_last_eval`` can be seeded from it. The
        # health monitor reads ``time.monotonic() - last_eval`` to compute
        # ``last_eval_ago``; a seed of ``0.0`` would yield ``uptime``
        # (~13.3d observed) before the first eval lands, polluting the
        # S1 health log and the ``last_eval_ago_sec`` state field. With a
        # monotonic-at-init seed the first health tick reads ~0s and the
        # value becomes accurate the moment the first eval updates the
        # entry at :func:`_evaluate_strategies`.
        self._strategy_init_monotonic: float = time.monotonic()

        # Card dcc7817d (3/3): ticks-flow-but-bars-static watchdog state.
        # ``_last_bar_built_at`` is updated inside ``_store_bar`` whenever
        # a new bar finalizes; ``_ticks_at_last_bar_built`` is the
        # ``ticks_received`` counter snapshot at the same moment. The
        # health monitor compares (now - _last_bar_built_at) against the
        # 20-minute threshold and gates on
        # ``ticks_received > _ticks_at_last_bar_built`` so M15 boundary
        # tick-only periods (no new bar) don't false-positive.
        self._last_bar_built_at: float = self._strategy_init_monotonic
        self._ticks_at_last_bar_built: int = 0
        # Rate-limit the watchdog WARNING the same way the existing
        # total_bars==0 detector does (300s between warnings).
        self._last_bars_static_warning_time: float = 0.0
        # Threshold lives at class-level — see _BARS_STATIC_THRESHOLD_SEC.

        # Bar-completion flags: set when a bar is finalized for a timeframe
        # key = _bar_key(symbol, timeframe), value = True when new bar completed
        self._bar_completed: dict[str, bool] = {}

        # Phase 6A: position_id → signal_id mapping for close() wiring.
        # Populated in _on_trade_executed when PaperTrader creates a position
        # from a blend_runner signal, consumed in _on_position_closed.
        self._position_id_to_signal_id: dict[str, str] = {}

        # Phase 6B: track last daily-reset date for day-boundary detection.
        self._last_reset_date: Optional[str] = None

        # Rejection circuit breaker (T5)
        self._consecutive_risk_rejections: int = 0
        self._rejection_cooldown_until: float = 0.0  # monotonic timestamp
        self._reconnect_delay: float = config.reconnect_delay_sec
        self._last_reconnect_attempt_at: float = 0.0
        # BQ-1335: Track when spot feed entered a stuck non-operational state.
        # When the feed has been RECONNECTING/FAILED for >60s, force a reconnect
        # regardless of backoff or tick freshness, so a stuck connection doesn't
        # persist for hours (the previous behavior with stale_tick_threshold=900s).
        self._reconnect_stuck_at: Optional[float] = None
        self._stuck_reconnect_threshold_sec: float = 60.0
        # Card f37e7b74: sustained-tick gate state. Set when a reconnect
        # attempt succeeds; cleared when the gate either passes (counter
        # reset) or expires (window elapsed without enough ticks). Window
        # uses engine-side monotonic time, not feed timestamps, so clock
        # skew between broker and engine cannot widen or shrink the gate.
        self._post_reconnect_at: Optional[float] = None
        self._post_reconnect_ticks: list[float] = []
        # Card 7d3b535d (rework of f37e7b74 INSUFFICIENT verdict): engine
        # monotonic-start timestamp used for proactive 24h session rotation.
        # Set in :meth:`start` after a successful feed bring-up; consulted
        # by :meth:`_health_monitor_loop` to log + trigger a clean exit
        # before the server-side 24h cap lands. ``None`` before start.
        self._start_monotonic: Optional[float] = None
        self._health_monitor_thread: Optional[threading.Thread] = None
        self._stop_health_monitor = threading.Event()
        self._current_spread: float = 0.0
        self._current_bid: float = 0.0
        self._current_ask: float = 0.0

        # Kill switch — global safety system
        self._kill_switch = KillSwitchManager()
        if self._kill_switch.is_globally_killed():
            logger.warning(
                "STARTUP: Kill switch ACTIVE (%s) — orders will be BLOCKED",
                self._kill_switch.get_status().get("reason", "unknown"),
            )
        else:
            logger.info("STARTUP: Kill switch CLEAR — enforcement active")

        # Heartbeat writer
        self._heartbeat_file = _HEARTBEAT_FILE
        self._heartbeat_pid = os.getpid()

        # Error rate monitor — rolling 60s window
        self._eval_timestamps: deque[float] = deque()  # monotonic timestamps of evaluations
        self._eval_errors: deque[float] = deque()  # monotonic timestamps of errors
        self._feed_disconnect_frozen = False  # track if we already froze for feed disconnect

        # S1: Per-strategy diagnostic counters
        self._strategy_eval_counts: dict[str, int] = {s.name: 0 for s in strategies}
        self._strategy_no_signal_counts: dict[str, int] = {s.name: 0 for s in strategies}
        self._strategy_last_eval: dict[str, float] = {s.name: self._strategy_init_monotonic for s in strategies}

        # B5 Pipeline warning: rate-limit + grace period
        self._last_pipeline_warning_time: float = 0.0
        _PIPELINE_GRACE_SEC = 1200  # 20 minutes

        # Symbol resolution diagnostics (Task 2)
        self._symbol_resolution_failures: dict[int, int] = {}
        self._symbol_resolution_last_warn: dict[int, float] = {}

        # Precompute normalized config symbols for fast comparison (Task 3)
        self._cfg_symbols_normalized: set[str] = {s.upper().replace("/", "") for s in self._config.symbols}

        # Confidence engine for live-fire gating.  Only used when live_mode
        # is active — paper mode bypasses the engine entirely so existing
        # behaviour is unchanged.
        self._confidence_engine: Optional[ConfidenceEngine] = None
        if self._config.live_mode:
            self._confidence_engine = ConfidenceEngine(gate_config=GateConfig())
            logger.info(
                "ConfidenceEngine initialised for live-fire gating (min_confidence=%.2f)",
                self._config.live_fire_min_confidence,
            )

        # Phase 1d: KillCriteriaChecker — global+per-strategy criterion
        # evaluator. Stateless across calls; built once at startup. Only
        # wired into the live-fire path (paper mode bypasses it).
        self._kill_criteria_checker: Optional[KillCriteriaChecker] = None
        if self._config.live_mode:
            self._kill_criteria_checker = KillCriteriaChecker(global_config={"max_spread_bps": 2.0})
            logger.info(
                "KillCriteriaChecker initialised for live-fire gating (max_spread_bps=2.0)",
            )

        # Phase 1d: BehavioralPolicy — streak + drawdown cooldown size
        # multiplier. Applied to live signals after KillCriteria pass.
        self._behavioral_policy: Optional[BehavioralPolicy] = None
        if self._config.live_mode:
            self._behavioral_policy = BehavioralPolicy()
            logger.info("BehavioralPolicy initialised for live-fire sizing")

        # Track consecutive losses (resets on win) for BehavioralPolicy
        # context. Reset semantics:
        #   - incremented on a losing position close
        #   - zeroed on a winning position close
        # Updated in ``_on_position_closed`` so the live-fire gate sees
        # the freshest streak when sizing the next signal.
        self._consecutive_losses: int = 0

    @property
    def health(self) -> ForwardTestHealth:
        with self._lock:
            return ForwardTestHealth(
                connected=self._market_feed.is_running if self._market_feed else False,
                last_tick_at=self._health.last_tick_at,
                ticks_received=self._health.ticks_received,
                ticks_per_second=self._health.ticks_per_second,
                signals_generated=self._health.signals_generated,
                signals_traded=self._health.signals_traded,
                signals_rejected=self._health.signals_rejected,
                uptime_sec=self._health.uptime_sec,
                evaluation_errors=self._health.evaluation_errors,
                reconnection_attempts=self._health.reconnection_attempts,
                reconnection_successes=self._health.reconnection_successes,
                bars_built=self._health.bars_built,
                consecutive_risk_rejections=self._health.consecutive_risk_rejections,
                signals_sent=self._health.signals_sent,
                signals_failed_live=self._health.signals_failed_live,
                signals_pending=self._health.signals_pending,
                signals_cancelled=self._health.signals_cancelled,
                signals_accepted=self._health.signals_accepted,
                signals_unreachable=self._health.signals_unreachable,
                signals_filtered_by_regime_gate=self._health.signals_filtered_by_regime_gate,
                signals_indeterminate=self._health.signals_indeterminate,
                signals_killed_by_criteria=self._health.signals_killed_by_criteria,
                behavioral_adjustments=self._health.behavioral_adjustments,
                last_kill_reasons=list(self._health.last_kill_reasons),
                last_behavioral_multiplier=self._health.last_behavioral_multiplier,
                live_fills=self._health.live_fills,
                seeded_positions=self._health.seeded_positions,
                last_rejection_errorcode=self._health.last_rejection_errorcode,
                rejection_breakdown=dict(self._health.rejection_breakdown),
            )

    @property
    def paper_trader(self) -> Optional[PaperTrader]:
        return self._paper_trader

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            logger.warning("ForwardTestEngine already running")
            return True

        # Phase 7B: live-mode validation gate — refuse to start against real
        # broker until remediation has been validated.
        #
        # Survivable check: if the flag file is missing but the audit doc
        # exists, auto-recreate the flag and continue with a warning. This
        # prevents crashes/kill-9/OOM from permanently killing the forward
        # test when the underlying validation has already been done.
        self._enforce_remediation_gate(self._config.live_mode)

        if not self._validate_credentials():
            logger.error("Invalid credentials — aborting start")
            return False

        self._build_components()
        self._wire_callbacks()

        if self._config.clear_stuck_positions_on_start and self._paper_trader:
            cleared = self._paper_trader.clear_stuck_positions()
            logger.info("Cleared %d stuck position(s) on start", cleared)

        if self._config.reset_on_start and self._paper_trader:
            self._paper_trader.reset()
            logger.info("Paper trader reset on start")

        if not self._start_market_feed():
            logger.error("Failed to start market data feed")
            return False

        # T2: Preload bars from API after feed is connected
        self._preload_complete = False
        if isinstance(self._market_feed, OpenApiSpotFeed):
            self._preload_historical_bars()

        # Phase 7A: preflight — reconcile open broker positions and seed risk.
        # Only applies when running through the blend pipeline (has _blend_runner).
        blend_runner = getattr(self, "_blend_runner", None)
        if blend_runner is not None and isinstance(self._market_feed, OpenApiSpotFeed):
            try:
                positions = self._market_feed.reconcile()
                self._seed_existing_positions(positions, blend_runner._sizer)
            except Exception as exc:
                logger.warning("Preflight reconcile failed: %s", exc)

        # Sync live balance from cTrader after feed connects (two-balance model)
        if isinstance(self._market_feed, OpenApiSpotFeed):
            self._sync_live_balance()

        self._running = True
        self._start_time = datetime.now(timezone.utc)
        # Card 7d3b535d: monotonic-start anchor for proactive 24h session
        # rotation. Must be set AFTER the feed bring-up succeeds so we
        # measure from a healthy state, not from a half-wired start that
        # immediately entered a reconnect storm.
        self._start_monotonic = time.monotonic()

        # Phase 6B: Initialize daily reset tracker using trading date.
        # America/Toronto midnight is the canonical daily reset boundary.
        self._last_reset_date = _trading_date(self._start_time)

        self._stop_health_monitor.clear()
        self._health_monitor_thread = threading.Thread(
            target=self._health_monitor_loop,
            name="forward-test-health-monitor",
            daemon=True,
        )
        self._health_monitor_thread.start()

        try:
            sig_module.signal(sig_module.SIGINT, self._on_shutdown)
            sig_module.signal(sig_module.SIGTERM, self._on_shutdown)
        except (ValueError, RuntimeError):
            logger.debug("Signal handler registration not available (subprocess/thread context)")

        logger.info(
            "Forward test started: symbols=%s strategies=%s mode=%s eval_interval=%.1fs bar_period=%dm",
            self._config.symbols,
            [s.name for s in self._strategies],
            "LIVE" if self._config.live_mode else "PAPER",
            self._config.evaluation_interval_sec,
            self._config.bar_period_minutes,
        )

        # B5: Startup diagnostic
        logger.info("[B5 Startup] Strategy list: %s", [s.name for s in self._strategies])
        logger.info("[B5 Startup] Symbols: %s", self._config.symbols)
        logger.info("[B5 Startup] Bar period: %dm", self._config.bar_period_minutes)
        logger.info("[B5 Startup] Min confidence: %.2f", self._config.min_confidence)
        logger.info(
            "[B5 Startup] Min bars for evaluation: %d",
            self._config.min_bars_for_evaluation,
        )
        logger.info(
            "[B5 Startup] Strategy timeframes: %s",
            self._strategy_timeframes or {s.name: self._config.bar_period_minutes for s in self._strategies},
        )
        logger.info(
            "[B5 Startup] Required timeframes: %s",
            sorted(self._required_timeframes),
        )
        return True

    @staticmethod
    def _enforce_remediation_gate(live_mode: bool) -> None:
        """Enforce the remediation validation gate for live mode.

        Returns silently when:
          - ``live_mode`` is False (paper mode is always allowed); or
          - the flag file already exists; or
          - the flag is missing BUT the audit doc (source of truth) exists —
            in which case the flag is auto-recreated from the audit doc with
            a warning. This makes the forward test survivable across crashes,
            kill -9, OOM, or ``git clean``.

        Raises ``RuntimeError`` only when ``live_mode`` is True AND both the
        flag and the audit doc are missing.
        """
        if not live_mode:
            return
        if os.path.exists(_REMEDIATION_VALIDATED_FLAG):
            return
        if os.path.exists(_REMEDIATION_AUDIT_DOC):
            logger.warning(
                "remediation_validated.flag missing but audit doc exists "
                "(%s) — auto-recreating flag. This is expected after "
                "crashes, kill -9, OOM, or git clean.",
                _REMEDIATION_AUDIT_DOC,
            )
            _flag_content = (
                f"auto-recreated {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                f"source: {_REMEDIATION_AUDIT_DOC}\n"
                f"reason: flag was missing on startup; audit doc is "
                f"source of truth.\n"
            )
            os.makedirs(os.path.dirname(_REMEDIATION_VALIDATED_FLAG), exist_ok=True)
            with open(_REMEDIATION_VALIDATED_FLAG, "w") as f:
                f.write(_flag_content)
            return
        raise RuntimeError(
            "Refusing to start in live mode: remediation not validated (flag and audit doc both missing)"
        )

    def stop(self):
        if not self._running:
            return

        self._running = False
        self._stop_health_monitor.set()

        if self._health_monitor_thread is not None:
            self._health_monitor_thread.join(timeout=10.0)
            self._health_monitor_thread = None

        with self._lock:
            for key in list(self._current_bar.keys()):
                self._finalize_and_store_bar(key)

        if self._market_feed:
            self._market_feed.stop()

        # Write final heartbeat with engine_running=false (clean shutdown signal)
        self._write_heartbeat()

        self._update_health()
        stats = self._paper_trader.get_stats() if self._paper_trader else None
        if stats:
            logger.info(
                "Forward test stopped: balance=%.2f trades=%d pnl=%.2f errors=%d reconnects=%d",
                stats.current_balance,
                stats.trades_executed,
                stats.current_balance - stats.starting_balance,
                self._health.evaluation_errors,
                self._health.reconnection_attempts,
            )

    def _sync_live_balance(self) -> None:
        """Fetch live balance from cTrader and sync to RiskGuard + PaperTrader.

        Implements the two-balance model (Craig, Jul 2026):
        - current_balance → live cTrader balance (position sizing, risk checks)
        - starting_balance → $10K prop-firm baseline (DD%, daily stop)

        Called once after feed connects and periodically from the health loop.
        """
        if not isinstance(self._market_feed, OpenApiSpotFeed):
            return
        try:
            from .account_state import get_balance

            live_balance = get_balance(
                self._market_feed.connection,
                self._market_feed.ctid_account_id,
                timeout=5.0,
            )
            if live_balance is not None:
                live_balance = float(live_balance)
                self._live_balance = live_balance

                # Update RiskGuard current balance (starting_balance stays at prop-firm baseline)
                if self._paper_trader and hasattr(self._paper_trader, "_risk_guard"):
                    rg = self._paper_trader._risk_guard
                    rg.update_balance(live_balance)
                    # Reset daily_start_balance on new trading day (not just first-ever sync).
                    # The old condition (_daily_trade_count == 0 AND _total_trades == 0) only
                    # fired once ever; after any trade, mid-day restarts loaded a stale
                    # daily_start_balance from the state file, causing daily P&L drift.
                    today = rg._current_trading_day()
                    if rg._current_day != today:
                        if rg._current_day is not None:
                            rg._record_daily_stats()
                        rg._current_day = today
                        rg._daily_start_balance = live_balance
                        rg._daily_trade_count = 0
                        rg._save_state()
                        logger.info(
                            "[Balance Sync] New trading day: daily_start_balance reset to $%.2f",
                            live_balance,
                        )
                    dd_pct = rg.current_drawdown_pct * 100
                    logger.info(
                        "[Balance Sync] RiskGuard synced: live=$%.2f starting=$%.2f dd_from_start=%.2f%%",
                        live_balance,
                        rg._starting_balance,
                        dd_pct,
                    )

                # Update PaperTrader internal balance
                if self._paper_trader:
                    self._paper_trader._current_balance = live_balance
            else:
                logger.warning("[Balance Sync] cTrader returned None balance — using starting balance")
        except Exception as exc:
            logger.warning("[Balance Sync] Failed to fetch live balance: %s", exc)

    def register_callback(self, event: str, callback: Callable):
        self._callbacks.append((event, callback))

    def _validate_credentials(self) -> bool:
        # Validate quote credentials (used for market data)
        creds = self._build_quote_credentials()
        if not creds.host:
            logger.error("Credential validation: host is empty")
            return False
        if not creds.username:
            logger.error("Credential validation: username (CTRADER_ACCOUNT) is empty")
            return False
        if not creds.password:
            logger.error("Credential validation: password (CTRADER_PASSWORD) is empty")
            return False
        # sender_comp_id is only required for the deprecated FIX feed.
        # OpenAPI feed uses OAuth tokens and does not need it.
        use_fix = getattr(self._config, "use_fix_feed", False)
        if use_fix and not creds.sender_comp_id:
            logger.warning("Credential validation: sender_comp_id is empty — may cause FIX logon failure")
        return True

    def _build_live_credentials(self) -> dict | None:
        """Build kwargs dict for the cTrader Open API spot feed."""
        try:
            store = CredentialStore(".env")
            # Sprint 024 (card 591cbfe6): inject the engine's KillSwitchManager
            # so the kill-switch re-arm in TokenLifecycle can dispatch to it
            # after ``auth_failure_threshold`` consecutive auth failures.
            lifecycle = TokenLifecycle(
                store,
                kill_switch=getattr(self, "_kill_switch", None),
            )
            access_token = lifecycle.ensure_valid()
            creds = store.get()
        except Exception as exc:
            logger.error("Failed to load cTrader credentials: %s", exc)
            return None

        if not access_token:
            logger.error("cTrader access token is empty after credential load")
            return None

        # Store lifecycle on the engine so it persists for the engine's lifetime.
        self._token_lifecycle = lifecycle

        # Ownership chain: OpenApiSpotFeed holds lifecycle via self._token_lifecycle.
        # ForwardTestEngine holds it via self._token_lifecycle. CredentialStore is held
        # by lifecycle._store. Neither will be GC'd while the engine is alive.
        return {
            "ctid_account_id": creds.account_id,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "access_token": access_token,
            "refresh_token": creds.refresh_token or None,
            "host": self._config.openapi_host,
            "port": self._config.openapi_port,
            "token_lifecycle": lifecycle,
        }

    # Env vars the spot feed requires.  Used by ``_describe_missing_live_creds``
    # to build actionable error messages on startup failures.
    _REQUIRED_LIVE_CRED_ENV_VARS = (
        "CTRADER_OPENAPI_CLIENT_ID",
        "CTRADER_OPENAPI_CLIENT_SECRET",
        "CTRADER_OPENAPI_ACCESS_TOKEN",
        "CTRADER_OPENAPI_ACCOUNT_ID",
    )

    def _describe_missing_live_creds(self) -> list[str]:
        """Return the subset of ``_REQUIRED_LIVE_CRED_ENV_VARS`` not set in the
        current process environment.  Used to surface a useful error message
        when ``live_mode=True`` but the operator forgot to populate ``.env``.
        """
        return [name for name in self._REQUIRED_LIVE_CRED_ENV_VARS if not os.environ.get(name)]

    def _build_components(self):
        cfg = self._config
        ftmo = self._ftmo_config or FTMOConfig()
        pos_cfg = self._position_config or PositionSizeConfig()

        # T4: lazily-resolved api_client wrapper.  If we build a live spot
        # feed we wrap it in cTraderAPIClient so ``PaperTrader.is_live_mode``
        # flips True and the ``OrderManager._wire_live_callbacks`` path is
        # triggered.
        api_client = None
        if cfg.live_mode:
            live_creds = self._build_live_credentials()
            if live_creds is None:
                # T5: Loud failure — instead of a silent early-return that
                # leaves the engine in a half-built state (``_paper_trader=None``
                # AND ``_market_feed=None``), raise a RuntimeError that lists
                # every missing env var.  This makes "I forgot to set the
                # OpenAPI token" fail in 5 seconds with an actionable message
                # rather than after 15 minutes of "no fills" warnings.
                missing = self._describe_missing_live_creds()
                msg = (
                    "live_mode=True but OpenAPI credentials are missing or invalid. "
                    "Required env vars: " + ", ".join(self._REQUIRED_LIVE_CRED_ENV_VARS) + "."
                )
                if missing:
                    msg += " Missing: " + ", ".join(missing) + "."
                msg += " If you intended paper mode, omit --live (or pass --paper-only)."
                logger.error(msg)
                raise RuntimeError(msg)

            self._market_feed = OpenApiSpotFeed(**live_creds)
            self._market_feed.validate_wiring()
            self._market_feed.set_kill_switch(self._kill_switch)
            from .execution_permission import ExecutionPermissionPolicy

            policy = ExecutionPermissionPolicy(kill_switch=self._kill_switch)
            self._market_feed.set_permission_policy(policy)
            api_client = cTraderAPIClient(**live_creds)
            api_client.set_permission_policy(policy)  # Phase 6: close defense-in-depth gap
            self._api_client = api_client
            logger.info("OpenApiSpotFeed + cTraderAPIClient constructed for live_mode")

        self._paper_trader = PaperTrader(
            ftmo_config=ftmo,
            position_config=pos_cfg,
            starting_balance=cfg.starting_balance,
            api_client=api_client,
        )

        self._live_adapter = cTraderLiveAdapter(
            paper_trader=self._paper_trader,
            strategies=self._strategies,
            symbols=cfg.symbols,
            # In live mode we return signals from the adapter and execute them
            # directly against the OpenApiSpotFeed so the engine controls real
            # order placement instead of relying on the paper-trader chain.
            blend_mode=self._blend_mode or cfg.live_mode,
        )

        strategy_names = "+".join(s.name for s in self._strategies)
        self._trade_logger = TradeLogger(
            log_dir=cfg.log_dir,
            strategy_name=strategy_names,
        )

        self._paper_trader.register_callback("on_trade_executed", self._on_trade_executed)
        self._paper_trader.register_callback("on_position_closed", self._on_position_closed)

        # Position monitor — centralized lifecycle tracking
        self._position_monitor = PositionMonitor(
            order_manager=self._paper_trader._order_manager,
            risk_guard=self._paper_trader._risk_guard,
            kill_switch=self._kill_switch,
            # Sprint Task 1.5 (card a7b8e896): wire the live spot feed so
            # PositionMonitor.check_tp_levels can amend the broker SL/TP when
            # price crosses TP2/TP3 (ratcheting).  In paper mode the feed is
            # still constructed (no live creds required) but amend_sl_tp is
            # a no-op against the local PaperTrader so it's safe to wire.
            market_feed=self._market_feed,
            # Card 4083ac2d-...: forward the engine's slot_tracker so the
            # per-symbol at-risk cap is updated after each successful ratchet.
            slot_tracker=self._slot_tracker,
        )

        # Share kill switch with risk guard so circuit breaker uses
        # the same instance (avoids creating a new KillSwitchManager
        # that writes to production state from tests)
        self._paper_trader._risk_guard.set_kill_switch(self._kill_switch)

    def _wire_callbacks(self):
        if self._market_feed is None:
            return

        self._market_feed.on_tick(self._on_tick)

    def _start_market_feed(self) -> bool:
        cfg = self._config

        # Use OpenAPI feed by default (FIX feed archived)
        use_fix = getattr(cfg, "use_fix_feed", False)
        if not use_fix:
            return self._start_openapi_feed()

        # multi-symbol routing — candidate for extraction if complexity grows
        subscribe_names = []
        for sym in cfg.symbols:
            symbol_key = sym.upper().replace("/", "")
            subscribe_name = self._resolve_feed_symbol_name(symbol_key)
            if subscribe_name is None:
                logger.error("Cannot resolve symbol %s for market data feed", sym)
                return False
            subscribe_names.append(subscribe_name)

        success = self._market_feed.start(auto_subscribe=subscribe_names)
        if success:
            logger.info("Market data feed connected for %s", cfg.symbols)
        return success

    def _resolve_feed_symbol_name(self, symbol_key: str) -> Optional[str]:
        name_to_id = self._market_feed.name_to_id if self._market_feed else {}
        feed_names = list(name_to_id.keys())

        slash_name = symbol_key[:3] + "/" + symbol_key[3:]
        no_slash_name = symbol_key

        for candidate in [slash_name, no_slash_name]:
            if candidate in feed_names:
                return candidate

        logger.warning(
            "Symbol %s not found in feed symbol map: %s",
            symbol_key,
            feed_names,
        )
        return None

    def _start_openapi_feed(self) -> bool:
        """Start the Open API spot feed instead of the FIX feed."""
        if self._market_feed is None:
            live_creds = self._build_live_credentials()
            if live_creds is None:
                logger.error("Missing Open API credentials in env")
                return False

            self._market_feed = OpenApiSpotFeed(**live_creds)
            self._market_feed.validate_wiring()
            self._market_feed.set_kill_switch(self._kill_switch)
            from .execution_permission import ExecutionPermissionPolicy

            policy = ExecutionPermissionPolicy(kill_switch=self._kill_switch)
            self._market_feed.set_permission_policy(policy)
            # Reconstruct _api_client on reconnect so OrderManager and
            # PaperTrader don't hold a dead connection.  The old instance's
            # TCP socket may be closed while the GIL keeps the wrapper alive
            # — is_connected would then silently skip every live order.
            if self._config.live_mode:
                self._api_client = cTraderAPIClient(**live_creds)
                self._api_client.set_permission_policy(policy)
                # Propagate the fresh client to PaperTrader + OrderManager
                if self._paper_trader is not None:
                    self._paper_trader.set_api_client(self._api_client)
                logger.info("_api_client reconstructed on reconnect")
            # Sprint Task 1.5 (card a7b8e896): feed was constructed lazily
            # AFTER _build_components, so wire it into the position monitor
            # now so TP2/TP3 ratcheting has an amend_sl_tp channel.
            if self._position_monitor is not None:
                self._position_monitor.set_market_feed(self._market_feed)
            self._wire_callbacks()

        subscribe_names = []
        for sym in self._config.symbols:
            subscribe_names.append(sym.upper().replace("/", ""))

        success = self._market_feed.start(auto_subscribe=subscribe_names)
        if success:
            logger.info("Open API spot feed connected for %s", self._config.symbols)
        return success

    def _build_quote_credentials(self) -> cTraderCredentials:
        import os
        from pathlib import Path

        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parents[4] / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)

        host = os.environ.get("CTRADER_HOST", self._config.quote_host)
        port = int(os.environ.get("CTRADER_READONLY_SSL_PORT", str(self._config.quote_port)))

        quote_sender_sub_id = os.environ.get("CTRADER_QUOTE_SENDER_SUB_ID") or self._config.quote_sender_sub_id
        _raw_target = os.environ.get("CTRADER_QUOTE_TARGET_SUB_ID")
        quote_target_sub_id = _raw_target or self._config.quote_target_sub_id or quote_sender_sub_id

        return cTraderCredentials(
            host=host,
            port=port,
            use_ssl=self._config.use_ssl,
            sender_comp_id=os.environ.get("CTRADER_SENDER_COMP_ID", ""),
            target_comp_id=os.environ.get("CTRADER_TARGET_COMP_ID", "cServer"),
            sender_sub_id=quote_sender_sub_id,
            target_sub_id=quote_target_sub_id,
            username=os.environ.get("CTRADER_ACCOUNT", ""),
            password=os.environ.get("CTRADER_PASSWORD", ""),
        )

    def _bar_period_start(self, ts: datetime, period_minutes: int = 0) -> datetime:
        minutes = period_minutes or self._config.bar_period_minutes
        return ts.replace(second=0, microsecond=0) - timedelta(minutes=ts.minute % minutes)

    def _finalize_current_bar(self, key: str) -> Optional[Bar]:
        current = self._current_bar.get(key)
        if current is None:
            return None
        finalized = Bar(
            time=current.time,
            open=current.open,
            high=current.high,
            low=current.low,
            close=current.close,
            volume=current.volume,
        )
        self._current_bar[key] = None
        return finalized

    def _assert_bar_integrity(self, bar: Bar):
        """Verify bar OHLC integrity.

        These were previously ``assert`` statements but asserts are stripped
        under ``python -O``. Bar data flows in from the live tick stream, so
        a malformed bar (high < close, low > open) must fail loudly on every
        Python invocation, not just debug runs — converted to explicit raises
        so the live-trading integrity gate survives optimization.
        """
        if not bar.high >= max(bar.open, bar.close):
            raise ValueError(
                f"Bar integrity fail: high={bar.high} < max(open={bar.open}, close={bar.close})"
            )
        if not bar.low <= min(bar.open, bar.close):
            raise ValueError(
                f"Bar integrity fail: low={bar.low} > min(open={bar.open}, close={bar.close})"
            )

    def _store_bar(self, key: str, bar: Bar):
        self._assert_bar_integrity(bar)
        if key not in self._bars:
            self._bars[key] = []
        self._bars[key].append(bar)
        self._health.bars_built += 1
        # Card dcc7817d (3/3): Update the bars-static watchdog timestamp
        # under the same lock the bars_built counter lives under. Paired
        # with ``_ticks_at_last_bar_built`` this is what the health
        # monitor uses to detect "ticks-flow-but-bars-static ≥20 min"
        # (M15 boundary safe per Satsuki correction). The lock keeps the
        # pair consistent against concurrent ``_on_tick`` updates.
        with self._lock:
            self._last_bar_built_at = time.monotonic()
            self._ticks_at_last_bar_built = self._health.ticks_received
        if len(self._bars[key]) > self._config.max_bars_per_symbol:
            self._bars[key] = self._bars[key][-self._config.max_bars_per_symbol :]

    def _finalize_and_store_bar(self, key: str) -> Optional[Bar]:
        finalized = self._finalize_current_bar(key)
        if finalized is not None:
            self._store_bar(key, finalized)
            # Mark this timeframe as having a new completed bar
            self._bar_completed[key] = True
        return finalized

    def _update_current_bar(self, tick: Tick, key: str, bar_time: datetime) -> Bar:
        current = self._current_bar.get(key)

        if current is not None and current.time == bar_time:
            mid = tick.mid
            updated = Bar(
                time=current.time,
                open=current.open,
                high=max(current.high, tick.ask),
                low=min(current.low, tick.bid),
                close=mid,
                volume=current.volume + 1,
            )
            self._current_bar[key] = updated
            return updated

        self._finalize_and_store_bar(key)

        new_bar = Bar(
            time=bar_time,
            open=tick.mid,
            high=tick.ask,
            low=tick.bid,
            close=tick.mid,
            volume=1,
        )
        self._current_bar[key] = new_bar
        return new_bar

    def _on_tick(self, tick: Tick):
        with self._lock:
            self._health.ticks_received += 1
            now = datetime.now(timezone.utc)
            self._health.last_tick_at = now

            self._tick_timestamps.append(now)

            # Card f37e7b74: sustained-tick gate bookkeeping. If a reconnect
            # has just succeeded, accumulate this tick's monotonic time so
            # the health monitor can decide whether we've truly recovered.
            # Prune ticks that fall after the window-end boundary — these
            # arrived too late to count toward recovery. The single
            # snapshot spot event from ProtoOASubscribeSpotsReq only fires
            # ONCE per subscribe, so the first tick is necessary but not
            # sufficient for the gate to pass.
            if self._post_reconnect_at is not None:
                now_mono = time.monotonic()
                window_end = self._post_reconnect_at + self._config.sustained_ticks_window_sec
                self._post_reconnect_ticks.append(now_mono)
                while (
                    self._post_reconnect_ticks
                    and self._post_reconnect_ticks[0] > window_end
                ):
                    self._post_reconnect_ticks.pop(0)
            cutoff = now.timestamp() - self._tick_rate_window_sec
            self._tick_timestamps = [t for t in self._tick_timestamps if t.timestamp() > cutoff]
            if self._tick_timestamps:
                window = self._tick_timestamps[-1].timestamp() - self._tick_timestamps[0].timestamp()
                self._health.ticks_per_second = len(self._tick_timestamps) / window if window > 0 else 0.0

        symbol_name = self._resolve_symbol_name(tick)
        if symbol_name is None:
            return
        # Use precomputed normalized config symbols for comparison
        if symbol_name not in self._cfg_symbols_normalized:
            return

        # Build bars for ALL required timeframes from this tick
        with self._lock:
            for tf in self._required_timeframes:
                bar_time = self._bar_period_start(tick.timestamp, period_minutes=tf)
                key = self._bar_key(symbol_name, tf)
                self._update_current_bar(tick, key, bar_time)

            self._update_paper_trader_prices(tick, symbol_name)
            self._current_spread = tick.spread
            self._current_bid = tick.bid
            self._current_ask = tick.ask

            # Phase 1D: Update position monitor (MAE/MFE, water marks, time tracking)
            if self._position_monitor is not None:
                self._position_monitor.update_positions(
                    prices={symbol_name: tick.mid},
                    bids={symbol_name: tick.bid},
                    asks={symbol_name: tick.ask},
                )

        # Evaluation trigger: purely event-driven — only on bar completion
        # Per-timeframe evaluation threshold (Rei #7): check primary timeframe.
        # Use the minimum required timeframe so the gate reflects the actual
        # strategy timeframe, not the legacy default bar_period_minutes which
        # may have no bars when running a subset of strategies (card 18ac48b1).
        min_tf = min(self._required_timeframes) if self._required_timeframes else self._config.bar_period_minutes
        primary_key = self._bar_key(symbol_name, min_tf)
        with self._lock:
            bar_count = len(self._bars.get(primary_key, []))
            current_bar = self._current_bar.get(primary_key)
            total_bars = bar_count + (1 if current_bar else 0)

        if total_bars < self._config.min_bars_for_evaluation:
            return

        # Check if any required timeframe has a new completed bar
        has_new_bar = False
        with self._lock:
            for tf in self._required_timeframes:
                key = self._bar_key(symbol_name, tf)
                if self._bar_completed.get(key, False):
                    has_new_bar = True
                    break

        if not has_new_bar:
            return

        # Consume the completion flags
        with self._lock:
            for tf in self._required_timeframes:
                key = self._bar_key(symbol_name, tf)
                self._bar_completed[key] = False

        logger.debug("Evaluating on bar completion for %s", symbol_name)
        self._last_evaluation_at = time.monotonic()
        self._evaluate_strategies(symbol_name)

    def _resolve_symbol_name(self, tick: Tick) -> Optional[str]:
        if self._market_feed is None:
            return None

        symbol_info = self._market_feed.symbols.get(tick.symbol_id)
        if symbol_info is None:
            # Diagnostic: rate-limited WARNING per symbol_id (max 1/min)
            sid = tick.symbol_id
            self._symbol_resolution_failures[sid] = self._symbol_resolution_failures.get(sid, 0) + 1
            self._health.symbol_resolution_failures += 1
            now_mono = time.monotonic()
            last_warn = self._symbol_resolution_last_warn.get(sid, 0.0)
            if now_mono - last_warn >= 60.0:
                self._symbol_resolution_last_warn[sid] = now_mono
                known_ids = list(self._market_feed.symbols.keys())
                logger.warning(
                    "[Symbol Resolution] symbol_id=%d not found in feed symbols. "
                    "known_ids=%s (failures for this id: %d)",
                    sid,
                    known_ids,
                    self._symbol_resolution_failures[sid],
                )
            return None

        feed_name = symbol_info.name
        no_slash = feed_name.replace("/", "")

        if no_slash in self._cfg_symbols_normalized:
            return no_slash

        # Diagnostic: normalized name not in config symbols
        sid = tick.symbol_id
        self._symbol_resolution_failures[sid] = self._symbol_resolution_failures.get(sid, 0) + 1
        self._health.symbol_resolution_failures += 1
        now_mono = time.monotonic()
        last_warn = self._symbol_resolution_last_warn.get(sid, 0.0)
        if now_mono - last_warn >= 60.0:
            self._symbol_resolution_last_warn[sid] = now_mono
            logger.warning(
                "[Symbol Resolution] feed_name='%s' normalized='%s' "
                "not in config symbols %s (symbol_id=%d, failures: %d)",
                feed_name,
                no_slash,
                sorted(self._cfg_symbols_normalized),
                sid,
                self._symbol_resolution_failures[sid],
            )
        return None

    def _update_paper_trader_prices(self, tick: Tick, symbol_name: str):
        if self._paper_trader is None:
            return

        mid_price = tick.mid
        self._paper_trader.update_market_prices(
            {symbol_name: mid_price},
            bids={symbol_name: tick.bid},
            asks={symbol_name: tick.ask},
        )

    # Rejection circuit breaker constants (T5)
    _REJECTION_BREAKER_THRESHOLD = 5
    _REJECTION_COOLDOWN_SEC = 60.0

    def _calculate_live_volume(self, signal: CTraderTradeSignal) -> float:
        """Compute position size for a live order without depending on PaperTrader.

        Refactored in T4 so the live execution path stays independent of the
        paper-only chain.  Uses ``PositionSizeConfig`` directly (the same
        defaults ``PaperTrader`` is built with) and falls back to the
        configured starting balance if no ``paper_trader`` is available.
        """
        if self._paper_trader is not None:
            balance = self._paper_trader.balance
            return self._paper_trader._order_manager.calculate_position_size(
                account_balance=balance,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                symbol=signal.symbol,
            )
        # No paper trader (the typical live-mode case once T4 wires things
        # up).  Use the configured position-size defaults directly.
        cfg = self._position_config or PositionSizeConfig()
        if signal.stop_loss is None or signal.entry_price is None:
            return 0.0
        sl_distance = abs(signal.entry_price - signal.stop_loss)
        if sl_distance == 0:
            return cfg.default_lot_size
        balance = self._config.starting_balance
        risk_amount = balance * cfg.risk_per_trade_pct

        # Determine pip size and pip value per lot from SymbolInfo when available
        # so that crypto and other non-FX symbols calculate correct position sizes.
        pip_value = 0.0001  # forex default (1 pip = 0.0001)
        dollar_per_pip_per_lot = 10.0  # standard FX lot pip value in USD
        if self._market_feed is not None:
            sym_id = None
            try:
                sym_id = self._market_feed.resolve_symbol_id(signal.symbol)
            except Exception:  # noqa: S110 — best-effort symbol lookup; falls back to default pip_value below
                pass
            if sym_id is not None:
                sym_info = self._market_feed.symbols.get(sym_id)
                if sym_info is not None:
                    pip_value = sym_info.pip_size
                    dollar_per_pip_per_lot = sym_info.pip_value_per_lot

        sl_pips = sl_distance / pip_value
        if sl_pips <= 0:
            return cfg.default_lot_size
        lots = risk_amount / (sl_pips * dollar_per_pip_per_lot)
        lots = max(cfg.min_lot_size, min(cfg.max_lot_size, lots))
        return lots

    def _seed_existing_positions(self, positions: list, sizer) -> None:
        """Seed the identity-keyed risk sizer with open cTrader positions.

        Phase 7A: before the first strategy evaluation, reconcile with the
        broker and register each existing open position's risk under a synthetic
        ``seeded_{positionId}`` signal_id.  Seeded risk consumes ``open_risk``
        budget (so the daily cap still works for new signals) but does NOT add
        to ``daily_risk_used`` because these positions were not opened today.
        """
        total_seeded_risk = 0.0
        seeded_count = 0

        for pos in positions:
            position_id = getattr(pos, "position_id", None)
            if position_id is None:
                continue
            symbol = getattr(pos, "symbol", "")
            lots = getattr(pos, "volume", 0.0) or 0.0
            entry_price = getattr(pos, "entry_price", 0.0) or 0.0
            sl_price = getattr(pos, "stop_loss", None) or None

            sym_info = get_symbol_info(symbol)
            pip_size = sym_info.pip_size
            pip_value_per_lot = sym_info.pip_value_per_lot

            if sl_price is not None and entry_price and lots:
                price_distance = abs(entry_price - sl_price)
                pips = price_distance / pip_size if pip_size else 0.0
                risk_amount = pips * lots * pip_value_per_lot
            else:
                # Conservative fallback: assume $100 of risk per lot when broker
                # position has no stop loss.
                risk_amount = lots * 100.0
                logger.warning(
                    "Preflight: position %s (%s %.4f lots) has no stop loss — using conservative risk estimate $%.2f",
                    position_id,
                    symbol,
                    lots,
                    risk_amount,
                )

            signal_id = f"seeded_{position_id}"
            try:
                sizer.register(signal_id, float(risk_amount))
                total_seeded_risk += float(risk_amount)
                seeded_count += 1
            except ValueError as exc:
                logger.warning(
                    "Preflight: could not seed position %s under %s: %s",
                    position_id,
                    signal_id,
                    exc,
                )

        logger.info(
            "Preflight: seeded %d open cTrader positions totaling $%.2f risk",
            seeded_count,
            total_seeded_risk,
        )
        # Record seeded positions in health so operators can distinguish
        # session fills from carry-over (card 2bb667ce AC2).
        self._health.seeded_positions = seeded_count

    def _execute_signal_live(self, signal: CTraderTradeSignal, strategy_id: str = "") -> Optional[LiveExecutionOutcome]:
        """Place a real cTrader order via the OpenApiSpotFeed.

        Returns ``None`` for pre-flight failures (no feed, unknown symbol,
        zero calculated volume, kill switch active, NEUTRAL direction) — no
        outcome object is created in those cases because there is no
        ``Order`` to carry forward to a late callback.
        """
        # Phase 1 (card eaceb5fa): NEUTRAL-direction guard.
        #
        # Blend/amalgamation can produce `direction == TradeDirection.NEUTRAL`
        # when long/short votes are tied. The legacy mapping at line ~1428
        # (`side = BUY if direction == LONG else SELL`) silently coerced
        # NEUTRAL → SELL while preserving the original LONG-style SL/TP,
        # which the broker then rejected with TRADING_BAD_STOPS. Skipping
        # NEUTRAL signals here is the smallest safe change: no re-pricing,
        # no LLM cost, no fill — just don't place an order we know will be
        # rejected. Phase 2 will recompute SL/TP for tied blends.
        if signal.direction == TradeDirection.NEUTRAL:
            logger.info(
                "Skipping live execution for NEUTRAL signal %s %s (strategy=%s): "
                "NEUTRAL direction has no side mapping; not safe to place live order.",
                signal.symbol,
                signal.direction,
                strategy_id or "<none>",
            )
            return None

        # P5A: primary permission gate — block before any broker interaction
        # or volume/state mutation. This closes the TOCTOU window between
        # _evaluate_strategies()'s kill-switch check and order dispatch.
        from .execution_permission import ExecutionPermissionPolicy

        policy = ExecutionPermissionPolicy(kill_switch=getattr(self, "_kill_switch", None))
        allowed, reason = policy.can_send_order()
        if not allowed:
            logger.warning("_execute_signal_live blocked: %s", reason)
            return None

        direction_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)

        if self._market_feed is None or not isinstance(self._market_feed, OpenApiSpotFeed):
            logger.warning("Cannot execute live order: no OpenApiSpotFeed available")
            return None

        # T5: also catch the upstream case where live_mode is requested but
        # the feed failed to start — the spot feed may exist as a Python
        # object but its state manager is not operational.  We still return
        # a NOT_CONNECTED outcome so the caller can release risk cleanly.
        state_mgr = getattr(self._market_feed, "_state_mgr", None)
        if state_mgr is not None and not getattr(state_mgr, "is_operational", True):
            logger.warning(
                "Live order skipped: spot feed not operational for %s %s",
                direction_str,
                signal.symbol,
            )
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.NOT_CONNECTED,
                order=None,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason="spot_feed_not_operational",
            )

        try:
            symbol_id = self._market_feed.resolve_symbol_id(signal.symbol)
        except Exception as exc:
            logger.warning("Live order rejected: unknown symbol %s (%s)", signal.symbol, exc)
            return None

        side = ProtoOATradeSide.BUY if signal.direction == TradeDirection.LONG else ProtoOATradeSide.SELL

        volume_lots = self._calculate_live_volume(signal)
        if volume_lots <= 0.0:
            logger.warning("Live order rejected: calculated volume is zero for %s", signal.symbol)
            return None

        volume_raw = self._market_feed.lots_to_volume(symbol_id, volume_lots)

        # Inline SL/TP on MARKET orders (verified against cTrader demo
        # 2026-07-06 — the broker accepts absolute SL/TP on MARKET orders;
        # previous naked-then-amend pattern was unnecessary). Fall back to
        # the amend path only when the signal has no SL/TP (strategies
        # without protection). Fix landed in commit 1ed0cdee (2026-07-06).
        inline_sl = signal.stop_loss if signal.stop_loss else None
        inline_tp = signal.take_profit_1 if signal.take_profit_1 else None

        order = self._market_feed.new_order(
            symbol_id=symbol_id,
            side=side,
            volume=volume_raw,
            order_type=ProtoOAOrderType.MARKET,
            sl=inline_sl,
            tp=inline_tp,
            comment=signal.rationale,
        )

        outcome = self._classify_live_order_outcome(order, signal, strategy_id)

        # If the order filled with inline SL/TP, no amend is needed — the
        # broker already has them. Still stash TP2/TP3 for ratcheting
        # (Task 1.5). If inline SL/TP were absent, fall through to the
        # amend path below for strategies without protection.
        if outcome.status == LiveExecutionStatus.FILLED and inline_sl and inline_tp:
            position_id = getattr(order, "position_id", None) or getattr(order, "order_id", None)
            # Phase 1A audit §5.4 fix: wire cTrader positionId → blend
            # signal_id in live mode (paper_trader on_trade_executed never
            # fires here, so _on_trade_executed can't do it).
            self._register_blend_position_mapping(signal, position_id)
            logger.info(
                "SL/TP attached inline on MARKET order %s (position %s): sl=%.5f tp=%.5f",
                getattr(order, "order_id", ""),
                position_id,
                inline_sl,
                inline_tp,
            )
            # cTrader's amend proto only accepts a single TP, so TP2/TP3 are
            # NOT sent to the broker. Instead, stash them on the Position via
            # OrderManager so position_monitor can ratchet the broker TP
            # when price crosses those levels (Task 1.5).
            tp2 = getattr(signal, "take_profit_2", None)
            tp3 = getattr(signal, "take_profit_3", None)
            if tp2 is not None or tp3 is not None:
                order_manager = self._resolve_order_manager()
                if order_manager is not None:
                    stored = order_manager.update_position_tp_levels(
                        position_id,
                        tp2,
                        tp3,
                    )
                    if not stored:
                        logger.warning(
                            "TP2/TP3 not stored on Position %s — TP ratcheting will not activate (non-fatal)",
                            position_id,
                        )
                else:
                    logger.warning(
                        "No OrderManager available — TP2/TP3 cannot be stored on Position %s (non-fatal)",
                        position_id,
                    )
        elif outcome.status == LiveExecutionStatus.FILLED and signal.stop_loss and signal.take_profit_1:
            # Fallback: strategies without inline SL/TP require amend
            position_id = getattr(order, "position_id", None) or getattr(order, "order_id", None)
            # Phase 1A audit §5.4 fix: same wiring as the inline-SL/TP branch.
            self._register_blend_position_mapping(signal, position_id)
            try:
                # Bounded retry (defense in depth — the inline path is the
                # primary fix, this only runs for the late-fill / no-inline
                # case). 3 attempts with linear backoff handles transient
                # broker issues without spamming.
                amended = False
                for attempt in range(3):
                    amended = self._market_feed.amend_sl_tp(
                        position_id,
                        signal.stop_loss,
                        signal.take_profit_1,
                        symbol_id=symbol_id,
                    )
                    if amended:
                        break
                    time.sleep(0.2 * (attempt + 1))
                if amended:
                    logger.info(
                        "SL/TP attached to position %s: sl=%.5f tp=%.5f",
                        position_id,
                        signal.stop_loss,
                        signal.take_profit_1,
                    )
                    # F1 fix (Sprint Task 1.3, card a7b8e896): the cTrader
                    # amend proto only accepts a single TP, so TP2/TP3 are
                    # NOT sent to the broker.  Instead, stash them on the
                    # Position via OrderManager so position_monitor can
                    # ratchet the broker TP when price crosses those levels
                    # (Task 1.5).  Only store on a confirmed amend success
                    # — otherwise the position is unprotected and ratcheting
                    # would be premature.
                    tp2 = getattr(signal, "take_profit_2", None)
                    tp3 = getattr(signal, "take_profit_3", None)
                    if tp2 is not None or tp3 is not None:
                        order_manager = self._resolve_order_manager()
                        if order_manager is not None:
                            stored = order_manager.update_position_tp_levels(
                                position_id,
                                tp2,
                                tp3,
                            )
                            if not stored:
                                logger.warning(
                                    "F1: TP2/TP3 not stored on Position %s — "
                                    "TP ratcheting will not activate (non-fatal)",
                                    position_id,
                                )
                        else:
                            logger.warning(
                                "F1: no OrderManager available — TP2/TP3 cannot be stored on Position %s (non-fatal)",
                                position_id,
                            )
                else:
                    logger.warning(
                        "F1: amend_sl_tp returned False after 3 attempts for position %s — "
                        "TP2/TP3 NOT stored (position remains on TP1 only, non-fatal)",
                        position_id,
                    )
            except Exception as amend_err:
                logger.warning(
                    "SL/TP amend error for position %s: %s (non-fatal)",
                    position_id,
                    amend_err,
                )

        # Log the outcome so operators can correlate with cTrader terminal
        # state and the engine's health counters.
        if outcome.status == LiveExecutionStatus.FILLED:
            logger.info(
                "Live order FILLED: %s %s %s lots=%.2f raw_volume=%d order_id=%s",
                direction_str,
                signal.symbol,
                signal.rationale,
                volume_lots,
                volume_raw,
                getattr(order, "order_id", ""),
            )
        elif outcome.status == LiveExecutionStatus.SENT:
            logger.info(
                "Live order SENT (awaiting cTrader ack): %s %s order_id=%s",
                direction_str,
                signal.symbol,
                getattr(order, "order_id", ""),
            )
            # Late-fill guard: register callbacks so when the execution event
            # arrives after event.wait() timed out, we still count it as a
            # fill and release correlation / risk.  Without this, the engine
            # would log SENT and then never update its state — the most likely
            # silent failure mode after the fix.
            self._register_late_fill_callbacks(order, signal, strategy_id)
        elif outcome.status == LiveExecutionStatus.REJECTED:
            logger.warning(
                "Live order REJECTED: %s %s reason=%s order_id=%s",
                direction_str,
                signal.symbol,
                outcome.reason,
                getattr(order, "order_id", ""),
            )
            # Record rejection in signal stats so rejection rate is non-zero.
            _rej_signal_id = getattr(order, "order_id", None) or signal.strategy_id
            _rej_error_code = getattr(order, "error_code", None) or getattr(order, "errorCode", None) or ""
            try:
                _rej_recorder = getattr(self, "_stats_recorder", None) or SignalStatsRecorder()
                _rej_recorder.record_rejection(
                    signal_id=_rej_signal_id,
                    rejection_reason=outcome.reason,
                    error_code=_rej_error_code,
                )
            except Exception:
                logger.debug(
                    "Rejection stats recording failed (non-fatal): %s %s",
                    signal.symbol,
                    _rej_signal_id,
                )
        elif outcome.status == LiveExecutionStatus.TIMEOUT:
            logger.warning(
                "Live order TIMEOUT: %s %s order_id=%s (no execution event in window)",
                direction_str,
                signal.symbol,
                getattr(order, "order_id", ""),
            )
            # Same late-fill protection as SENT — a TIMEOUT may be followed
            # by a real fill arriving a few ms later.
            self._register_late_fill_callbacks(order, signal, strategy_id)
        elif outcome.status == LiveExecutionStatus.NOT_CONNECTED:
            logger.warning(
                "Live order NOT_CONNECTED: %s %s order_id=%s",
                direction_str,
                signal.symbol,
                getattr(order, "order_id", ""),
            )
        elif outcome.status == LiveExecutionStatus.CANCELLED:
            logger.warning(
                "Live order CANCELLED: %s %s order_id=%s",
                direction_str,
                signal.symbol,
                getattr(order, "order_id", ""),
            )

        # Phase 0 signal-stats hook: record the open line for this signal.
        # CRITICAL: stats recording must NEVER crash the execution path.
        # The outcome has already been classified and the order sent — losing
        # a stats line is acceptable; losing the outcome return is not.
        #
        # Retry strategy: transient I/O errors (file locks, disk contention,
        # network-attached storage) can cause record_signal() to fail.
        # We retry with exponential backoff (2s/4s/8s by default) before
        # falling back to graceful degradation (last-known-good confidence).
        signal_confidence = float(signal.confidence)
        _stats_recorded = False  # Reserved for future logging; not yet wired up.
        for attempt in range(self._stats_retry_max):
            try:
                self._stats_recorder = getattr(self, "_stats_recorder", None) or SignalStatsRecorder()
                self._stats_recorder.record_signal(
                    SignalRecord(
                        signal_id=order.order_id if order and order.order_id else signal.strategy_id,
                        timestamp=signal.timestamp.isoformat() if signal.timestamp else "",
                        strategy=signal.strategy_id or strategy_id or "unknown",
                        symbol=signal.symbol,
                        direction=direction_str.upper() if direction_str else "",
                        confidence=signal_confidence,
                        rationale_tags=[signal.rationale] if signal.rationale else [],
                        confluence_score=0.0,
                        lots=float(volume_lots),
                        entry_price=float(signal.entry_price),
                        sl_price=float(signal.stop_loss),
                        tp_price=float(signal.take_profit_1),
                    )
                )
                # Reset on success: counter tracks *consecutive* failures,
                # not lifetime totals.
                self._stats_fail_count = 0
                self._last_known_good_confidence = signal_confidence
                _stats_recorded = True  # Reserved for future logging.
                break
            except Exception as stats_err:
                if attempt < self._stats_retry_max - 1:
                    delay = self._stats_retry_base_delay * (2**attempt)
                    logger.warning(
                        "Signal stats recording attempt %d/%d failed (retry in %.1fs): %s",
                        attempt + 1,
                        self._stats_retry_max,
                        delay,
                        stats_err,
                    )
                    time.sleep(delay)
                else:
                    # All retries exhausted — graceful degradation
                    self._stats_fail_count = getattr(self, "_stats_fail_count", 0) + 1
                    last_good = self._last_known_good_confidence
                    if last_good is not None:
                        logger.warning(
                            "Signal stats recording failed after %d attempts "
                            "(consecutive_fails=%d, using last-known-good confidence=%.4f): %s",
                            self._stats_retry_max,
                            self._stats_fail_count,
                            last_good,
                            stats_err,
                        )
                    else:
                        logger.warning(
                            "Signal stats recording failed after %d attempts "
                            "(consecutive_fails=%d, no prior confidence cached): %s",
                            self._stats_retry_max,
                            self._stats_fail_count,
                            stats_err,
                        )

        return outcome

    # Reason strings used by OpenApiSpotFeed when the order could not be sent.
    _NOT_CONNECTED_REASON = "not_connected"
    _TIMEOUT_REASON = "timeout_awaiting_event"
    _CANCELLED_REASON = "order_cancelled"
    # Card ce6de98d (B): distinct reason string for the new INDETERMINATE
    # timeout semantics. The spot feed leaves order.status = PENDING and
    # tags reason = _INDETERMINATE_REASON when event.wait expires but
    # the order may still arrive via the late-fill registry. The
    # classifier below detects this string and routes to the
    # signals_indeterminate counter (additive) instead of the legacy
    # signals_sent (which was conflating genuine SENT with TIMEOUT).
    _INDETERMINATE_REASON = "indeterminate_awaiting_event"

    # Rejection log path — JSONL file written alongside trade logs so
    # operators have a dedicated, greppable record of every broker
    # rejection with the real errorCode.  Fixes the "see rejection log
    # for errorCode" message in launch_blend_forward_test.py that
    # pointed at a log file which was never created (card d88336dc AC3).
    _REJECTION_LOG_PATH = "logs/rejections.jsonl"

    def _process_live_outcome(self, outcome: LiveExecutionOutcome) -> None:
        """Update health counters based on a synchronous live-order outcome.

        Called from ``_evaluate_strategies`` after ``_execute_signal_live``
        returns, and from ``_release_late`` for late-arriving outcomes.
        Centralises counter logic so both the synchronous and callback
        paths agree on what each status means.
        """
        with self._lock:
            # Card ce6de98d (B): TIMEOUT with indeterminate reason gets its
            # own counter (signals_indeterminate) and does NOT bump
            # signals_sent or signals_failed_live. The indeterminate state
            # is non-terminal: the order may still arrive via the
            # late_fill_registry. The late-fill callback path still
            # upgrades it to FILLED, so the indeterminate counter is
            # purely a diagnostic for "we waited the timeout window and
            # did not yet hear back from the broker".
            if outcome.status == LiveExecutionStatus.TIMEOUT and outcome.reason == self._INDETERMINATE_REASON:
                self._health.signals_indeterminate += 1
                # Keep signals_pending unchanged for indeterminate: the
                # order is still in flight and the late-fill callback may
                # still resolve it. We do NOT bump signals_sent (would
                # conflate with genuine SENT awaiting ack).
                logger.info(
                    "Live order indeterminate (TIMEOUT but pending late-fill): "
                    "%s %s reason=%s signals_indeterminate=%d",
                    outcome.symbol,
                    outcome.direction,
                    outcome.reason,
                    self._health.signals_indeterminate,
                )
                return
            if outcome.status == LiveExecutionStatus.FILLED:
                self._health.live_fills += 1
                self._health.signals_traded += 1
                self._health.signals_pending = max(0, self._health.signals_pending - 1)
                # Backward-compat: keep ``_live_fill_count`` in sync so
                # the launch script's ``getattr(engine, '_live_fill_count', 0)``
                # reads correctly until it is migrated to ``health.live_fills``.
                self._live_fill_count = self._health.live_fills
            elif outcome.status in (
                LiveExecutionStatus.SENT,
                LiveExecutionStatus.TIMEOUT,
            ):
                self._health.signals_sent += 1
                self._health.signals_pending += 1
            else:
                # REJECTED, CANCELLED, NOT_CONNECTED — terminal failures.
                self._health.signals_failed_live += 1
                self._health.signals_pending = max(0, self._health.signals_pending - 1)
                # Surface the broker errorCode so operators don't have to
                # grep through 30K+ log lines (card 45aad19f).
                code = outcome.reason or "unknown"
                # Strip description text after the first colon / underscore
                # so ``TRADING_BAD_STOPS: New SL for SELL...`` becomes
                # ``TRADING_BAD_STOPS``.
                short_code = code.split(":")[0].split("_")[0].strip() or code
                self._health.last_rejection_errorcode = short_code
                self._health.rejection_breakdown[short_code] = self._health.rejection_breakdown.get(short_code, 0) + 1
                # Write structured rejection log entry (card d88336dc AC3).
                # The launch script's health message references a rejection
                # log that was never created — this writes it.
                self._write_rejection_log(outcome, short_code)

    def _write_rejection_log(self, outcome: LiveExecutionOutcome, short_code: str) -> None:
        """Append a rejection event to the JSONL rejection log.

        Called from :meth:`_process_live_outcome` for every terminal failure
        (REJECTED, CANCELLED, NOT_CONNECTED, TIMEOUT).  Writes one JSON object
        per line to ``logs/rejections.jsonl`` so operators can ``grep`` or
        ``jq`` the file for specific errorCodes without digging through the
        main log.

        Format::

            {"ts": "2026-07-17T20:30:00Z", "status": "REJECTED",
             "symbol": "EURUSD", "direction": "BUY", "strategy": "ttc_xauusd",
             "error_code": "TRADING_BAD_STOPS", "reason": "..."}
        """
        try:
            from pathlib import Path

            log_path = Path(_PROJECT_ROOT) / self._REJECTION_LOG_PATH
            log_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": outcome.status.value,
                "symbol": outcome.symbol,
                "direction": outcome.direction,
                "strategy": outcome.strategy_id,
                "error_code": short_code,
                "reason": outcome.reason,
            }
            with open(log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("Failed to write rejection log: %s", exc)

    def _classify_live_order_outcome(self, order, signal: CTraderTradeSignal, strategy_id: str) -> LiveExecutionOutcome:
        """Translate a spot-feed ``Order`` into a :class:`LiveExecutionOutcome`.

        The classification inspects ``order.status`` (an :class:`OrderStatus`)
        and the ``reason`` attribute the spot feed attaches on its failure
        branches.  All six LiveExecutionStatus values are reachable.
        """
        direction_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)

        if order is None:
            # Defensive: the spot feed currently never returns None, but if
            # a future change makes it so, treat it as NOT_CONNECTED rather
            # than crashing the engine.
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.NOT_CONNECTED,
                order=None,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason="order_is_none",
            )

        status = getattr(order, "status", None)
        reason = getattr(order, "reason", "") or ""

        if status == OrderStatus.REJECTED:
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.REJECTED,
                order=order,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason=reason or "rejected",
            )

        if status == OrderStatus.CANCELLED:
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.CANCELLED,
                order=order,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason=reason or self._CANCELLED_REASON,
            )

        if status == OrderStatus.FILLED:
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.FILLED,
                order=order,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason=reason or "order_filled",
            )

        if reason == self._TIMEOUT_REASON:
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.TIMEOUT,
                order=order,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason=reason,
            )

        # Card ce6de98d (B): indeterminate timeout semantics. The spot feed
        # leaves order.status = PENDING and tags reason =
        # "indeterminate_awaiting_event" when event.wait expires but the
        # order may still arrive via late_fill_registry. We classify this
        # as TIMEOUT with the indeterminate reason so _process_live_outcome
        # can bump the new signals_indeterminate counter (instead of
        # signals_sent, which conflates genuine SENT with TIMEOUT). The
        # late-fill callback path still upgrades to FILLED if the broker
        # eventually confirms the fill.
        if reason == self._INDETERMINATE_REASON:
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.TIMEOUT,
                order=order,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason=reason,
            )

        if reason == self._NOT_CONNECTED_REASON:
            return LiveExecutionOutcome(
                status=LiveExecutionStatus.NOT_CONNECTED,
                order=order,
                symbol=signal.symbol,
                direction=direction_str,
                strategy_id=strategy_id,
                reason=reason,
            )

        # PENDING without a known failure reason: order was sent, awaiting
        # the cTrader execution event.  We classify this as SENT and rely
        # on the late-fill callback to upgrade it to FILLED if the event
        # arrives after event.wait() returned.
        return LiveExecutionOutcome(
            status=LiveExecutionStatus.SENT,
            order=order,
            symbol=signal.symbol,
            direction=direction_str,
            strategy_id=strategy_id,
            reason=reason,
        )

    def _resolve_order_manager(self):
        """Return the live ``OrderManager`` instance, or ``None`` in paper mode.

        Sprint Task 1.3 (card a7b8e896) helper: F1 (immediate amend) and
        F2 (late-fill amend) both need to call
        :meth:`OrderManager.update_position_tp_levels` so that TP2/TP3
        are stored on the Position after the broker amend succeeds.  In
        live mode the engine always builds a :class:`PaperTrader` (and
        therefore an OrderManager) so the typical return is the
        live-mode manager; in paper mode no OrderManager is constructed
        and this returns ``None`` so the caller can short-circuit.

        The accessor is intentionally permissive — direct attribute,
        property, or ``_paper_trader`` indirection all work — because
        test scaffolding sometimes swaps one of these out.
        """
        # Direct attribute override (test scaffolding / future refactor)
        om = getattr(self, "_order_manager", None)
        if om is not None:
            return om
        # Property/indirection via PaperTrader (the production path)
        paper = getattr(self, "_paper_trader", None)
        if paper is not None:
            return getattr(paper, "_order_manager", None)
        return None

    def _register_late_fill_callbacks(self, order, signal: CTraderTradeSignal, strategy_id: str) -> None:
        """Register one-shot callbacks so a late execution event upgrades SENT
        or TIMEOUT outcomes to a definitive terminal state.

        The spot feed's ``event.wait(timeout)`` in ``new_order`` has a race
        window: it can return False (timeout) *just* before the execution
        event arrives.  When that happens, the engine sees a TIMEOUT or SENT
        outcome but the broker has a real fill.  Without this callback
        registration, the engine would log the failure and never update
        ``_live_fill_count`` or release correlation / risk — the position
        would silently leak.

        We register ``on_order_filled`` / ``on_order_rejected`` /
        ``on_order_cancelled`` callbacks that look up the pending outcome by
        ``order.order_id``, update the counters, and free the slots.  Each
        callback also self-removes after firing so it does not fire twice.
        """
        feed = self._market_feed
        if feed is None:
            return
        if not hasattr(feed, "register_callback"):
            return

        order_id = getattr(order, "order_id", "")
        if not order_id:
            return

        # _pending_outcome_keys tracks which (order_id) entries we have
        # registered callbacks for so we can avoid double-registering if the
        # engine sees two signals in quick succession.
        self._pending_outcome_keys = getattr(self, "_pending_outcome_keys", set())
        if order_id in self._pending_outcome_keys:
            return
        self._pending_outcome_keys.add(order_id)

        direction_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)

        fired = [False]  # mutable flag so closure can self-dedupe

        def _release_late(rv_status: LiveExecutionStatus, *args, **kwargs):
            # Dedupe: each late-fill closure can fire from any of the three
            # registered events (filled/rejected/cancelled) for ANY order's
            # execution event. Self-dedupe with a closure flag and only act
            # if the incoming event actually matches our captured order_id.
            if fired[0]:
                return
            if not args:
                return
            cb_order = args[0]
            cb_order_id = getattr(cb_order, "order_id", "")
            if cb_order_id and cb_order_id != order_id:
                return
            fired[0] = True
            self._pending_outcome_keys.discard(order_id)

            # Extract cTrader's positionId from the execution event for the
            # amend_sl_tp call. The proto execution event carries the real
            # position ID on order.positionId / position.positionId / deal.positionId.
            ctrader_position_id = None
            message = args[1] if len(args) > 1 else None
            if message is not None:
                order_payload = getattr(message, "order", None)
                position_payload = getattr(message, "position", None)
                deal_payload = getattr(message, "deal", None)
                for source in (order_payload, position_payload, deal_payload):
                    pid = getattr(source, "positionId", None)
                    if pid is not None:
                        try:
                            pid_int = int(pid)
                            if pid_int != 0:
                                ctrader_position_id = pid_int
                                break
                        except (ValueError, TypeError):
                            # positionId is not an integer (e.g. UUID-like clientOrderId)
                            continue

            with self._lock:
                if rv_status == LiveExecutionStatus.FILLED:
                    self._health.live_fills += 1
                    self._health.signals_traded += 1
                    self._health.signals_pending = max(0, self._health.signals_pending - 1)
                    # Backward-compat: keep _live_fill_count in sync.
                    self._live_fill_count = self._health.live_fills
                    # Reconciliation defense: if the synchronous TIMEOUT path
                    # spuriously bumped signals_failed_live before this late
                    # fill arrived, undo that increment here. Bounded at zero
                    # so the counter can never go negative. This pairs with
                    # the launcher's fix that stops the synchronous path from
                    # bumping signals_failed_live for TIMEOUT (which is an
                    # awaiting-ack state, not a terminal failure).
                    self._health.signals_failed_live = max(0, self._health.signals_failed_live - 1)
                    logger.info(
                        "Late fill detected for order %s (%s %s) — live_fills=%d",
                        order_id,
                        direction_str,
                        signal.symbol,
                        self._health.live_fills,
                    )
                    # Phase 1A audit §5.4 fix: wire the cTrader
                    # positionId → blend signal_id mapping so the close
                    # path can release sizer risk. Without this, the
                    # first live close would leak a sizer risk slot.
                    self._register_blend_position_mapping(signal, ctrader_position_id)
                    # Attach SL/TP via position amend. The synchronous FILLED
                    # path does this in execute_live_order, but late fills
                    # arrive via this callback path and were previously left
                    # naked without risk protection.
                    if (
                        signal.stop_loss is not None
                        and signal.take_profit_1 is not None
                        and self._market_feed is not None
                        and isinstance(ctrader_position_id, int)
                        and ctrader_position_id != 0
                    ):
                        try:
                            symbol_id = self._market_feed.resolve_symbol_id(signal.symbol)
                            # Bounded retry (defense in depth — the inline
                            # attach on the sync path is the primary fix;
                            # late fills reach this branch via the fill
                            # callback). 3 attempts with linear backoff
                            # handles transient broker issues without
                            # spamming.
                            amended = False
                            for attempt in range(3):
                                amended = self._market_feed.amend_sl_tp(
                                    ctrader_position_id,
                                    signal.stop_loss,
                                    signal.take_profit_1,
                                    symbol_id=symbol_id,
                                )
                                if amended:
                                    break
                                time.sleep(0.2 * (attempt + 1))
                            if amended:
                                logger.info(
                                    "Late SL/TP attached to position %s (order %s): sl=%.5f tp=%.5f",
                                    ctrader_position_id,
                                    order_id,
                                    signal.stop_loss,
                                    signal.take_profit_1,
                                )
                                # F2 fix (Sprint Task 1.3, card a7b8e896):
                                # mirror the F1 immediate-path fix — store
                                # TP2/TP3 on the Position after amend
                                # success so position_monitor can ratchet
                                # (Task 1.5).  cTrader's amend proto only
                                # accepts one TP; TP2/TP3 are client-side
                                # tracking fields.
                                tp2 = getattr(signal, "take_profit_2", None)
                                tp3 = getattr(signal, "take_profit_3", None)
                                if tp2 is not None or tp3 is not None:
                                    order_manager = self._resolve_order_manager()
                                    if order_manager is not None:
                                        stored = order_manager.update_position_tp_levels(
                                            ctrader_position_id,
                                            tp2,
                                            tp3,
                                        )
                                        if not stored:
                                            logger.warning(
                                                "F2: TP2/TP3 not stored on Position %s — "
                                                "TP ratcheting will not activate (non-fatal)",
                                                ctrader_position_id,
                                            )
                                    else:
                                        logger.warning(
                                            "F2: no OrderManager available — TP2/TP3 cannot be "
                                            "stored on Position %s (non-fatal)",
                                            ctrader_position_id,
                                        )
                            else:
                                logger.warning(
                                    "F2: late amend_sl_tp returned False after 3 attempts for position %s — "
                                    "TP2/TP3 NOT stored (position remains on TP1 only, non-fatal)",
                                    ctrader_position_id,
                                )
                        except Exception as amend_err:
                            logger.warning(
                                "Late SL/TP amend error for position %s (order %s): %s (non-fatal)",
                                ctrader_position_id,
                                order_id,
                                amend_err,
                            )
                    elif rv_status == LiveExecutionStatus.FILLED and signal.stop_loss is not None:
                        logger.warning(
                            "Late fill for order %s but no cTrader positionId — SL/TP skipped",
                            order_id,
                        )
                elif rv_status in (
                    LiveExecutionStatus.REJECTED,
                    LiveExecutionStatus.CANCELLED,
                    LiveExecutionStatus.NOT_CONNECTED,
                    LiveExecutionStatus.TIMEOUT,
                ):
                    self._health.signals_failed_live += 1
                    self._health.signals_pending = max(0, self._health.signals_pending - 1)
                    # Surface the broker errorCode in health output (card 45aad19f).
                    late_reason = getattr(message, "description", "") or rv_status.value
                    if late_reason:
                        short_code = late_reason.split(":")[0].split("_")[0].strip() or late_reason
                        self._health.last_rejection_errorcode = short_code
                        self._health.rejection_breakdown[short_code] = (
                            self._health.rejection_breakdown.get(short_code, 0) + 1
                        )
                    logger.warning(
                        "Late outcome for order %s: %s (%s %s) reason=%s",
                        order_id,
                        rv_status.value,
                        direction_str,
                        signal.symbol,
                        late_reason,
                    )
            # Free correlation / risk on the launcher side, if it exists.
            gate = getattr(self, "_correlation_gate", None)
            if gate is not None and rv_status != LiveExecutionStatus.FILLED:
                try:
                    gate.release(signal.symbol, direction_str)
                except Exception:  # noqa: S110 — best-effort correlation gate release on launcher side; state will be re-resolved on next signal
                    pass
            blend_runner = getattr(self, "_blend_runner", None)
            if blend_runner is not None and rv_status != LiveExecutionStatus.FILLED:
                try:
                    # Delegate to canonical helper — never construct the
                    # id locally (Phase 5 regression fix).
                    signal_id = blend_runner.make_signal_id(signal)
                    if hasattr(blend_runner, "cancel_risk"):
                        blend_runner.cancel_risk(
                            signal_id,
                            getattr(
                                signal,
                                "risk_amount",
                                self._config.starting_balance * 0.01,
                            ),
                        )
                except Exception:  # noqa: S110 — best-effort blend_runner.cancel_risk rollback; signal rejection is final
                    pass

        try:
            feed.register_callback(
                "on_order_filled",
                lambda *a, **k: _release_late(LiveExecutionStatus.FILLED, *a, **k),
            )
            feed.register_callback(
                "on_order_rejected",
                lambda *a, **k: _release_late(LiveExecutionStatus.REJECTED, *a, **k),
            )
            feed.register_callback(
                "on_order_cancelled",
                lambda *a, **k: _release_late(LiveExecutionStatus.CANCELLED, *a, **k),
            )
        except Exception as exc:
            # Best-effort: if the feed has been stopped or the callback path
            # raises, fall back to logging so we don't crash the engine.
            logger.warning(
                "Could not register late-fill callbacks for order %s: %s",
                order_id,
                exc,
            )

    def _evaluate_strategies(self, symbol: str):
        if self._live_adapter is None:
            return

        # Kill switch gate — checked before any strategy evaluation
        if getattr(self, "_kill_switch", None) and self._kill_switch.is_globally_killed():
            logger.debug("Kill switch active — skipping strategy evaluation")
            return

        # T5: Rejection circuit breaker — cooldown check
        if time.monotonic() < self._rejection_cooldown_until:
            logger.debug("Rejection cooldown active, skipping evaluation")
            return

        if not self._eval_semaphore.acquire(blocking=False):
            logger.debug("Evaluation already in progress — skipping")
            return

        try:
            # Build per-timeframe bar snapshots
            tf_bars: dict[int, list[Bar]] = {}
            with self._lock:
                for tf in self._required_timeframes:
                    key = self._bar_key(symbol, tf)
                    bars = list(self._bars.get(key, []))
                    tf_bars[tf] = bars

            # Defensive: verify no forming bars leaked into evaluation
            for tf_key in tf_bars:
                forming = self._current_bar.get(tf_key)
                if forming is not None and tf_bars[tf_key]:
                    if forming.time == tf_bars[tf_key][-1].time:
                        logger.error(
                            "Bar-close invariant violated: forming bar leaked into evaluation for %s",
                            tf_key,
                        )
                        tf_bars[tf_key] = tf_bars[tf_key][:-1]  # remove the leaked bar

            if not any(tf_bars.values()):
                return

            # S1: Bar eval triggered — confirm evaluation IS being called
            logger.info(
                "[S1] Bar eval triggered: symbol=%s bars_15m=%d bars_60m=%d",
                symbol,
                len(tf_bars.get(15, [])),
                len(tf_bars.get(60, [])),
            )

            # Record evaluation attempt for error-rate monitor
            self._eval_timestamps.append(time.monotonic())

            # Resolve each strategy's timeframe
            strategy_tf_map: dict[str, int] = {}
            for s in self._strategies:
                strategy_tf_map[s.name] = self._strategy_timeframes.get(s.name, self._config.bar_period_minutes)

            # Per-strategy evaluation with correct timeframe bars
            for strategy in self._strategies:
                tf = strategy_tf_map[strategy.name]
                bars = tf_bars.get(tf, [])

                # Per-timeframe evaluation threshold (Rei #7)
                if len(bars) < self._config.min_bars_for_evaluation:
                    continue

                _latest = bars[-1] if bars else None
                _session = determine_session(_latest.time) if _latest else SessionType.OUTSIDE
                state = MarketState(bars=bars, current_session=_session)

                # T5: Capture risk block count before evaluation
                _pre_risk_blocks = self._paper_trader.get_stats().signals_blocked_by_risk if self._paper_trader else 0

                try:
                    signals = self._live_adapter.evaluate_all_strategies(
                        {symbol: state},
                        spread=self._current_spread,
                        bid=self._current_bid,
                        ask=self._current_ask,
                    )
                except Exception as exc:
                    with self._lock:
                        self._health.evaluation_errors += 1
                    self._eval_errors.append(time.monotonic())
                    logger.error(
                        "Strategy %s evaluation error (total=%d): %s",
                        strategy.name,
                        self._health.evaluation_errors,
                        exc,
                        exc_info=True,
                    )
                    continue

                # S1: Per-strategy diagnostic counters
                self._strategy_eval_counts[strategy.name] += 1
                if not signals:
                    self._strategy_no_signal_counts[strategy.name] += 1
                self._strategy_last_eval[strategy.name] = time.monotonic()

                # S1: INFO-level per-strategy eval log
                # Includes a `reason` field on no_signal so log greps can
                # distinguish:
                #   - no_signal reason=strategy_conditions_not_met  (normal)
                #   - no_signal reason=no_adapter_for_symbol         (config gap)
                #   - no_signal reason=confidence_below_threshold    (filtered)
                # The DEBUG-level line below provides additional bar/spread
                # context for diagnosing why a strategy stopped firing.
                no_signal_reason = "n/a"
                if not signals:
                    no_signal_reason = "strategy_conditions_not_met"
                    # Last-bar snapshot for diagnosing "why no signal"
                    _last_bar = bars[-1] if bars else None
                    _last_bar_time = _last_bar.time.isoformat() if _last_bar else "none"
                    logger.debug(
                        "[S1] no_signal reason=%s strategy=%s symbol=%s "
                        "bar_count=%d last_bar_time=%s last_eval_ts=%.0f",
                        no_signal_reason,
                        strategy.name,
                        symbol,
                        len(bars),
                        _last_bar_time,
                        self._strategy_last_eval[strategy.name],
                    )
                logger.info(
                    "[S1] Strategy %s: eval #%d, signals=%d, total_no_signal=%d reason=%s",
                    strategy.name,
                    self._strategy_eval_counts[strategy.name],
                    len(signals),
                    self._strategy_no_signal_counts[strategy.name],
                    no_signal_reason,
                )

                # T5: Check if risk guard blocked any signals (circuit breaker tracking)
                _post_risk_blocks = self._paper_trader.get_stats().signals_blocked_by_risk if self._paper_trader else 0
                _new_risk_rejections = _post_risk_blocks - _pre_risk_blocks

                if _new_risk_rejections > 0:
                    # Actual RiskGuard rejections — track for circuit breaker
                    self._consecutive_risk_rejections += _new_risk_rejections
                    with self._lock:
                        self._health.signals_rejected += _new_risk_rejections
                        self._health.consecutive_risk_rejections = self._consecutive_risk_rejections
                    logger.warning(
                        "Risk guard rejected %d signal(s) (consecutive=%d/%d)",
                        _new_risk_rejections,
                        self._consecutive_risk_rejections,
                        self._REJECTION_BREAKER_THRESHOLD,
                    )
                    if self._consecutive_risk_rejections >= self._REJECTION_BREAKER_THRESHOLD:
                        self._rejection_cooldown_until = time.monotonic() + self._REJECTION_COOLDOWN_SEC
                        logger.warning(
                            "Rejection circuit breaker TRIPPED at %d consecutive — cooldown for %.0fs",
                            self._consecutive_risk_rejections,
                            self._REJECTION_COOLDOWN_SEC,
                        )
                        # Check if daily loss limit is the cause
                        if self._paper_trader:
                            stats = self._paper_trader.get_stats()
                            drawdown_pct = (
                                abs((stats.current_balance - stats.starting_balance) / stats.starting_balance)
                                if stats.starting_balance > 0
                                else 0
                            )
                            if drawdown_pct > 0.03:
                                logger.critical(
                                    "Daily drawdown %.1f%% — possible daily loss limit breach",
                                    drawdown_pct * 100,
                                )
                elif signals:
                    # Signal passed risk — reset consecutive counter
                    self._consecutive_risk_rejections = 0
                    with self._lock:
                        self._health.consecutive_risk_rejections = 0

                with self._lock:
                    self._health.signals_generated += len(signals)

                for s in signals:
                    if not self._config.live_mode:
                        # Paper mode: the paper trader will execute this
                        # signal synchronously, so counting it as traded is
                        # accurate.  In live mode we defer this bump to
                        # ``_process_live_outcome`` which only fires after a
                        # confirmed FILLED outcome.
                        with self._lock:
                            self._health.signals_traded += 1
                    logger.info(
                        "Signal traded: %s %s %s @ %.5f conf=%.2f",
                        s.direction.value,
                        s.volume,
                        s.symbol,
                        s.entry_price,
                        s.confidence,
                    )
                    if self._config.live_mode:
                        # ── ConfidenceEngine live-fire gate ──────────────
                        # Score every signal through the multi-layer pipeline
                        # (strategy score → confluence boost → gate validation)
                        # before sending a real order to the broker.
                        if self._confidence_engine is not None:
                            direction_str = s.direction.value if hasattr(s.direction, "value") else str(s.direction)
                            hour_utc = datetime.now(timezone.utc).hour
                            conf_result = self._confidence_engine.score(
                                raw_confidence=s.confidence,
                                symbol=s.symbol,
                                direction=direction_str,
                                spread=self._current_spread,
                                hour_utc=hour_utc,
                            )
                            logger.info(
                                "[ConfidenceEngine] %s %s conf=%.3f → "
                                "strategy=%.3f boost=%.3f final=%.3f "
                                "gates_passed=%s gates_failed=%s blocked=%s",
                                direction_str,
                                s.symbol,
                                s.confidence,
                                conf_result.strategy_score,
                                conf_result.confluence_boost,
                                conf_result.final_score,
                                conf_result.gates_passed,
                                conf_result.gates_failed,
                                conf_result.blocked,
                            )
                            if conf_result.blocked:
                                logger.warning(
                                    "[ConfidenceEngine] Signal BLOCKED by gate %s for %s %s: %s",
                                    conf_result.gates_failed,
                                    direction_str,
                                    s.symbol,
                                    conf_result.block_reason,
                                )
                                with self._lock:
                                    self._health.signals_rejected += 1
                                continue
                            if conf_result.final_score < self._config.live_fire_min_confidence:
                                logger.warning(
                                    "[ConfidenceEngine] Signal REJECTED — "
                                    "final_score %.3f < live_fire_min_confidence "
                                    "%.3f for %s %s",
                                    conf_result.final_score,
                                    self._config.live_fire_min_confidence,
                                    direction_str,
                                    s.symbol,
                                )
                                with self._lock:
                                    self._health.signals_rejected += 1
                                continue
                        # ── end ConfidenceEngine gate ─────────────────────

                        # ── KillCriteria gate ─────────────────────────────
                        # Phase 1c: global+per-strategy kill criteria. Runs
                        # AFTER ConfidenceEngine so we only spend cycles on
                        # signals that already passed multi-gate scoring.
                        # The checker is stateless — see policy/kill_criteria.py.
                        if self._kill_criteria_checker is not None:
                            strategy_name = (
                                getattr(s, "strategy_name", "") or getattr(s, "strategy_id", "") or "unknown"
                            )
                            kc_context = {
                                "symbol": s.symbol,
                                "spread_bps": self._current_spread,
                                "hour_utc": hour_utc,
                                "adx": getattr(s, "adx", 0.0) or 0.0,
                                "confluence_score": conf_result.confluence_boost if conf_result else 0.0,
                                "strategy_name": strategy_name,
                            }
                            kc_results = self._kill_criteria_checker.check(kc_context)
                            triggered = [r for r in kc_results if r.triggered]
                            if triggered:
                                for r in triggered:
                                    logger.info(
                                        "[KillCriteria] %s %s TRIGGERED: %s",
                                        direction_str,
                                        s.symbol,
                                        r,
                                    )
                                with self._lock:
                                    self._health.signals_rejected += 1
                                    self._health.signals_killed_by_criteria += 1
                                    self._health.last_kill_reasons = [r.name for r in triggered]
                                continue
                            # Debug-level: log all passed criteria so an
                            # operator can audit which rules ran without
                            # flooding INFO in production.
                            for r in kc_results:
                                logger.debug(
                                    "[KillCriteria] %s %s PASSED: %s",
                                    direction_str,
                                    s.symbol,
                                    r,
                                )
                        # ── end KillCriteria gate ──────────────────────────

                        # ── BehavioralPolicy sizing ────────────────────────
                        # Phase 1b: streak + DD cooldown size multiplier.
                        # Applied AFTER kill criteria so we don't size a
                        # signal that was just going to be killed. The
                        # multiplier clamps volume in [min, max] per
                        # council-approved semantics (Kaito/Nora/Ren/Sora,
                        # 2026-07-10).
                        if self._behavioral_policy is not None:
                            dd_pct = 0.0
                            # ForwardTestEngine holds the risk guard on the
                            # paper trader (``self._paper_trader._risk_guard``),
                            # not directly. Gracefully degrade to 0.0 when
                            # the paper trader or its guard is not yet
                            # constructed (early-startup race window).
                            paper_trader = getattr(self, "_paper_trader", None)
                            rg = getattr(paper_trader, "_risk_guard", None) if paper_trader else None
                            if rg is not None:
                                dd_pct = rg.current_daily_loss_pct * 100
                            bp_context = {
                                "consecutive_losses": self._consecutive_losses,
                                "daily_drawdown_pct": dd_pct,
                            }
                            bp_result = self._behavioral_policy.evaluate(s.volume, bp_context)
                            with self._lock:
                                self._health.last_behavioral_multiplier = bp_result.multiplier
                            if bp_result.multiplier < 1.0:
                                original_volume = s.volume
                                s.volume = s.volume * bp_result.multiplier
                                logger.info(
                                    "[BehavioralPolicy] %s %s base=%.2f adjusted=%.2f mult=%.2f reasons=%s",
                                    direction_str,
                                    s.symbol,
                                    original_volume,
                                    s.volume,
                                    bp_result.multiplier,
                                    bp_result.adjustments,
                                )
                                with self._lock:
                                    self._health.behavioral_adjustments += 1
                        # ── end BehavioralPolicy sizing ────────────────────

                        # Capture and process the outcome so counters
                        # (live_fills, signals_sent, signals_failed_live,
                        # rejection_breakdown) stay accurate on the
                        # synchronous path.  Previously the return value
                        # was discarded, leaving live_fills permanently 0
                        # for synchronous fills.
                        outcome = self._execute_signal_live(s)
                        if outcome is not None:
                            self._process_live_outcome(outcome)
                    self._trigger_callback("on_signal_traded", s)
        except Exception as exc:
            with self._lock:
                self._health.evaluation_errors += 1
            self._eval_errors.append(time.monotonic())
            logger.error(
                "Strategy evaluation outer error (total=%d): %s",
                self._health.evaluation_errors,
                exc,
                exc_info=True,
            )
        finally:
            self._eval_semaphore.release()

    def _preload_historical_bars(self):
        """T2: Fetch historical bars from the API and preload them into the engine."""
        if not isinstance(self._market_feed, OpenApiSpotFeed):
            return

        for sym in self._config.symbols:
            for tf in self._required_timeframes:
                # Skip if already preloaded by launcher
                key = self._bar_key(sym, tf)
                if key in self._bars and len(self._bars[key]) >= self._config.min_bars_for_evaluation:
                    logger.info(
                        "Skipping preload for %s %dm — already has %d bars",
                        sym,
                        tf,
                        len(self._bars[key]),
                    )
                    continue
                try:
                    bars = self._market_feed.fetch_trendbars(
                        symbol=sym,
                        period_minutes=tf,
                        count=self._config.preload_bar_count,
                    )
                    if bars:
                        self.preload_bars(sym, tf, bars)
                        logger.info(
                            "Preloaded %d bars for %s %dm",
                            len(bars),
                            sym,
                            tf,
                        )
                    else:
                        logger.warning(
                            "No bars returned for %s %dm — evaluation may be delayed",
                            sym,
                            tf,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to preload bars for %s %dm: %s",
                        sym,
                        tf,
                        exc,
                    )

        self._preload_complete = True
        logger.info("Bar preloading complete")

    def _write_heartbeat(self):
        """Atomically write the trading heartbeat file.

        Uses temp + rename to guarantee no partial reads by the watchdog.

        Card 0d64bec9 — defensive getattr against bare/partial engine
        instances: ``_last_bar_built_at`` and ``_ticks_at_last_bar_built``
        are set in ``__init__`` so a ``ForwardTestEngine.__new__``-only
        test fixture (the ``TestHeartbeatAtomicWrite`` mocks) does not
        have them. Likewise the ``_health`` field is a MagicMock in
        those tests where most counters are unset; we coerce each
        access through ``getattr`` with a sane default so the JSON
        shape is always serializable and the heartbeat file is always
        written atomically (temp + os.replace), never leaving a
        half-written file for the watchdog to read.
        """
        # ``_health`` may be unset on a bare instance — guard the whole
        # access so we don't crash on ``None`` (which the watchdog would
        # then misread as a missing file → false stale-heartbeat kill).
        # MagicMock test fixtures (TestHeartbeatAtomicWrite) auto-create
        # attributes on access, returning MagicMock for unset fields;
        # ``_safe_attr`` detects that and falls back to the default so
        # the JSON shape stays integer/str/dict and ``json.dumps`` does
        # not raise ``TypeError: Object of type MagicMock is not JSON
        # serializable`` — the previous behaviour that left the
        # heartbeat file unwritten and the watchdog reading stale data.
        health = getattr(self, "_health", None)
        if health is None:
            health_ticks_received = 0
            health_live_fills = 0
            health_signals_traded = 0
            health_signals_sent = 0
            health_signals_failed_live = 0
            health_signals_unreachable = 0
            health_signals_pending = 0
            health_signals_filtered_by_regime_gate = 0
            health_signals_indeterminate = 0
            health_seeded_positions = 0
            health_last_rejection_errorcode = ""
            health_rejection_breakdown: dict[str, int] = {}
            health_ticks_per_second = 0.0
        else:
            health_ticks_received = _safe_attr(health, "ticks_received", 0)
            health_live_fills = _safe_attr(health, "live_fills", 0)
            health_signals_traded = _safe_attr(health, "signals_traded", 0)
            health_signals_sent = _safe_attr(health, "signals_sent", 0)
            health_signals_failed_live = _safe_attr(health, "signals_failed_live", 0)
            health_signals_unreachable = _safe_attr(health, "signals_unreachable", 0)
            health_signals_pending = _safe_attr(health, "signals_pending", 0)
            health_signals_filtered_by_regime_gate = _safe_attr(
                health, "signals_filtered_by_regime_gate", 0
            )
            health_signals_indeterminate = _safe_attr(health, "signals_indeterminate", 0)
            health_seeded_positions = _safe_attr(health, "seeded_positions", 0)
            health_last_rejection_errorcode = _safe_attr(health, "last_rejection_errorcode", "")
            raw_breakdown = _safe_attr(health, "rejection_breakdown", {})
            health_rejection_breakdown = dict(raw_breakdown) if isinstance(raw_breakdown, dict) else {}
            health_ticks_per_second = _safe_attr(health, "ticks_per_second", 0.0)

        # ``_last_bar_built_at`` is set in __init__ — guard against bare
        # instances so the heartbeat write never crashes on a partial
        # engine (the watchdog would otherwise see no heartbeat file
        # and fire a false heartbeat_stale kill).
        last_bar_built_at = getattr(self, "_last_bar_built_at", time.monotonic())
        ticks_at_last_bar_built = getattr(self, "_ticks_at_last_bar_built", 0)
        bar_age_sec = max(0.0, time.monotonic() - last_bar_built_at)
        # ``_BARS_STATIC_THRESHOLD_SEC`` is a class-level constant but
        # guard via getattr so a bare instance with no class attribute
        # resolution still gets a sane threshold.
        bars_static_threshold = getattr(self, "_BARS_STATIC_THRESHOLD_SEC", 1200.0)

        try:
            heartbeat = {
                "last_beat": datetime.now(timezone.utc).isoformat(),
                "pid": getattr(self, "_heartbeat_pid", 0),
                "ticks_received": health_ticks_received,
                "engine_running": getattr(self, "_running", False),
                "stats_fails": getattr(self, "_stats_fail_count", 0),
                "stats_last_known_good": getattr(self, "_last_known_good_confidence", None),
                # Health-observability fields (cards 45aad19f / 2bb667ce / a8a757c4).
                # These mirror ``ForwardTestHealth`` so downstream consumers
                # (Hayate daily audit, dashboard) can read fill counts,
                # rejection breakdown, and seeded positions directly from
                # the heartbeat JSON without grepping logs.
                "live_fills": health_live_fills,
                "trades": health_signals_traded,
                "signals_sent": health_signals_sent,
                "signals_failed_live": health_signals_failed_live,
                "signals_unreachable": health_signals_unreachable,
                "signals_pending": health_signals_pending,
                "signals_filtered_by_regime_gate": health_signals_filtered_by_regime_gate,
                # Card ce6de98d (B): surface signals_indeterminate so the
                # heartbeat JSON (Hayate daily audit / dashboard) can see
                # the new diagnostic counter without grepping logs.
                "signals_indeterminate": health_signals_indeterminate,
                "seeded_positions": health_seeded_positions,
                "last_rejection_errorcode": health_last_rejection_errorcode,
                "rejection_breakdown": health_rejection_breakdown,
                # Card 8ad140c5 finding #5 (sprint reina-2026-08-18-106):
                # surface the spot-feed counters (order_error_session_conflict,
                # unmatched_late_fills) in the ACTIVE blend heartbeat JSON so
                # downstream consumers see them without grepping logs.
                # Additive — no existing keys are renamed or removed. The
                # counters live on the OpenApiSpotFeed instance held by
                # the engine (``self._market_feed``); we read via getattr
                # with a default of 0 so the heartbeat file is always
                # written even if the spot feed is not yet wired.
                "order_error_session_conflict": self._safe_spot_feed_counter("_order_error_session_conflict_count"),
                "unmatched_late_fills": self._safe_spot_feed_counter("_unmatched_late_fills_count"),
                # Card dcc7817d (3/3): surface throughput + bars-static
                # watchdog fields so the Hayate SH-002 audit can read them
                # from the heartbeat JSON (without grepping logs). SH-002
                # reads ``tps_5min_avg`` first then ``tps_recent`` then
                # ``tps`` (scripts/daily_audit.py:188-189) — we publish
                # ``tps_recent`` (instantaneous) and ``tps_5min_avg``
                # (same value for now; rolling buffer is out of scope for
                # this card). ``bars_static_sec`` is the age of the last
                # bar build; ``bars_static`` is the boolean the watchdog
                # tripped on (read by the Hayate audit if/when it's
                # extended). All fields read with ``getattr(..., 0)`` so
                # older readers that pre-date this commit tolerate the
                # heartbeat cleanly.
                "tps": round(health_ticks_per_second, 2),
                "tps_recent": round(health_ticks_per_second, 2),
                "tps_5min_avg": round(health_ticks_per_second, 2),
                "bars_static_sec": round(bar_age_sec, 1),
                "bars_static": (
                    bar_age_sec >= bars_static_threshold
                    and health_ticks_received > ticks_at_last_bar_built
                ),
                "ticks_since_last_bar": int(
                    max(
                        0,
                        health_ticks_received - ticks_at_last_bar_built,
                    )
                ),
            }
            json_str = json.dumps(heartbeat, indent=2)

            filepath = Path(self._heartbeat_file)
            filepath.parent.mkdir(parents=True, exist_ok=True)

            fd, tmp_path = tempfile.mkstemp(
                dir=str(filepath.parent),
                prefix=".heartbeat_trading.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w") as f:
                    f.write(json_str)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(filepath))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning("Failed to write heartbeat: %s", exc)

    def _safe_spot_feed_counter(self, attr_name: str) -> int:
        """Read a counter attribute from the live spot feed safely.

        Card 8ad140c5 finding #5 (sprint reina-2026-08-18-106): the
        spot-feed counters (``_order_error_session_conflict_count``,
        ``_unmatched_late_fills_count``) live on the OpenApiSpotFeed
        instance stored at ``self._market_feed``. We expose them via
        the ACTIVE blend heartbeat JSON so downstream consumers (Hayate
        daily audit, dashboard) can see them without grepping logs.

        Returns 0 when:
          * the engine has not yet wired the spot feed (start() hasn't
            been called yet — heartbeat file is still written, just
            with a 0 value);
          * the spot feed exists but does not expose the named
            attribute (defensive against forward/backward-compat
            drift);
          * any exception is raised (log + return 0 so heartbeat
            write never fails because of a counter read).
        """
        try:
            feed = getattr(self, "_market_feed", None)
            if feed is None:
                return 0
            value = getattr(feed, attr_name, 0)
            # Numeric guard: a MagicMock would return a MagicMock for any
            # attribute access; coerce to int with fallback to 0 so the
            # heartbeat JSON shape stays integer.
            return int(value) if isinstance(value, (int, float)) else 0
        except Exception as exc:
            logger.debug("Safe-counter read failed for attr=%s: %s", attr_name, exc)
            return 0

    def _check_error_rate(self):
        """Check evaluation error rate in rolling 60s window.

        If error rate exceeds 50%, activate GLOBAL FREEZE.
        Resets counters on successful evaluation (called when no error).
        """
        now_mono = time.monotonic()
        cutoff = now_mono - _ERROR_RATE_WINDOW_SEC

        # Prune old entries
        while self._eval_timestamps and self._eval_timestamps[0] < cutoff:
            self._eval_timestamps.popleft()
        while self._eval_errors and self._eval_errors[0] < cutoff:
            self._eval_errors.popleft()

        total_evals = len(self._eval_timestamps)
        if total_evals < 5:
            return  # not enough data to judge

        error_count = len(self._eval_errors)
        error_rate = error_count / total_evals

        if error_rate > _ERROR_RATE_THRESHOLD_PCT:
            logger.critical(
                "Error rate %.1f%% (%d/%d) in 60s window — FREEZE activation is P6 scope",
                error_rate * 100,
                error_count,
                total_evals,
            )
            # P6 scope-out: freeze activation intentionally remains commented
            # out per Phase 4 priority list. Tracked in P5A closeout.
            # self._kill_switch.activate_global_freeze(
            #     reason="high_error_rate",
            #     triggered_by="error_monitor",
            # )
            self._eval_timestamps.clear()
            self._eval_errors.clear()

    def _check_feed_health_kill_switch(self):
        """Check feed disconnect and activate FREEZE if needed.

        On feed disconnect: activate GLOBAL FREEZE (not kill — positions
        have broker-side SL/TP).
        On feed reconnect: log info but do NOT auto-recover.
        """
        with self._lock:
            feed_connected = self._market_feed.is_running if self._market_feed else False
            last_tick = self._health.last_tick_at

        # Only freeze if feed is actually disconnected.
        # "feed connected but no tick yet" = still initializing, not a disconnect.
        if not feed_connected:
            if not self._feed_disconnect_frozen:
                logger.warning("Feed disconnect detected — FREEZE activation is P6 scope, continuing")
                # P6 scope-out: freeze activation intentionally remains commented
                # out per Phase 4 priority list. Tracked in P5A closeout.
                # self._kill_switch.activate_global_freeze(
                #     reason="feed_disconnect",
                #     triggered_by="feed_health_monitor",
                # )
                self._feed_disconnect_frozen = False
        elif last_tick is not None and self._feed_disconnect_frozen:
            logger.info("Feed reconnected — kill switch remains active (manual recovery required)")
            # Do NOT auto-recover. Manual recovery required.

    def _health_monitor_loop(self):
        # B5: Periodic diagnostic tracking
        _last_diagnostic_log = time.monotonic()
        _diagnostic_interval = 60.0

        while not self._stop_health_monitor.wait(self._config.health_monitor_interval_sec):
            try:
                # Card 7d3b535d: proactive 24h session rotation. cTrader
                # Open API terminates authenticated sessions on a 24h
                # rolling window; waiting for the server-side cap causes
                # a chaotic reconnect storm with daily double-flap
                # (21:21 + 21:45 UTC). Rotating proactively 30 min before
                # the cap gives a clean predictable exit so systemd's
                # auto-restart is the only churn.
                if self._running and self._start_monotonic is not None:
                    age_mono = time.monotonic() - self._start_monotonic
                    if age_mono >= self._config.proactive_rotation_sec:
                        logger.info(
                            "[Rotation] Engine uptime %.0fs reached proactive "
                            "rotation threshold (%.0fs); requesting clean stop "
                            "for systemd auto-restart with fresh 24h session",
                            age_mono,
                            self._config.proactive_rotation_sec,
                        )
                        self._running = False
                        # Set the stop event so the while-loop exits and
                        # the launcher main loop sees is_running=False.
                        self._stop_health_monitor.set()
                        return
                self._update_health()
                self._check_connection_health()

                # Phase 6B: Daily risk reset check — detect day boundary
                # and call reset_daily() on the sizer.  This runs in the
                # health monitor loop so it fires even without new signals.
                # Uses America/Toronto midnight (Craig decision Jul 17) so
                # the daily loss budget rolls over at Toronto midnight
                # regardless of host timezone.
                now_dt = datetime.now(timezone.utc)
                day_str = _trading_date(now_dt)
                if self._last_reset_date is not None and day_str != self._last_reset_date:
                    blend_runner = getattr(self, "_blend_runner", None)
                    if blend_runner is not None:
                        if hasattr(blend_runner, "daily_reset"):
                            # Forward trading date so the blend runner's
                            # internal day-tracking and sizer logging
                            # stay consistent.
                            blend_runner.daily_reset(now=now_dt)
                        else:
                            sizer = getattr(blend_runner, "_sizer", None)
                            if sizer is not None:
                                pre_daily = sizer._daily_risk_used
                                pre_open = sizer.open_risk
                                positions_carried = len(sizer.open_positions)
                                sizer.reset_daily(cet_date=day_str)
                                logger.info(
                                    "Daily risk reset: daily_used=%.2f→0.00, "
                                    "open_risk=%.2f, positions_carried=%d, "
                                    "trading_date=%s",
                                    pre_daily,
                                    pre_open,
                                    positions_carried,
                                    day_str,
                                )
                self._last_reset_date = day_str

                # Write heartbeat (atomic)
                if self._running:
                    self._write_heartbeat()

                # Feed health → kill switch
                self._check_feed_health_kill_switch()

                # Error rate monitor
                self._check_error_rate()

                # B5: Periodic health diagnostic log (every 60s)
                now = time.monotonic()
                if now - _last_diagnostic_log >= _diagnostic_interval:
                    _last_diagnostic_log = now
                    with self._lock:
                        ticks = self._health.ticks_received
                        bars = self._health.bars_built
                        signals = self._health.signals_generated
                        traded = self._health.signals_traded
                        errors = self._health.evaluation_errors
                    logger.info(
                        "[B5 Periodic] ticks=%d bars_built=%d signals=%d traded=%d eval_errors=%d",
                        ticks,
                        bars,
                        signals,
                        traded,
                        errors,
                    )
                    # B5 Amendment 4: tick-to-bar pipeline health (revised)
                    # Count TOTAL bars across all keys + preloaded bars
                    # to avoid false positives when bars_built counter hasn't
                    # incremented yet (e.g. started mid-M15 interval).
                    if ticks > 0:
                        with self._lock:
                            total_bars = sum(len(v) for v in self._bars.values())
                            # Include current (forming) bars in the count
                            total_bars += sum(1 for v in self._current_bar.values() if v is not None)
                        uptime = self._health.uptime_sec
                        in_grace = uptime < 1200  # 20-minute startup grace

                        if total_bars == 0:
                            if in_grace:
                                logger.debug(
                                    "[B5 Pipeline] %d ticks, 0 total bars — within startup grace (%.0fs < 1200s)",
                                    ticks,
                                    uptime,
                                )
                            else:
                                # Rate-limit WARNING to once per 5 minutes
                                if now - self._last_pipeline_warning_time >= 300:
                                    self._last_pipeline_warning_time = now
                                    # Gather diagnostics
                                    with self._lock:
                                        bar_keys = {k: len(v) for k, v in self._bars.items()}
                                        current_keys = {k for k, v in self._current_bar.items() if v is not None}
                                        last_tick = self._health.last_tick_at
                                    cfg_symbols = list(self._cfg_symbols_normalized)
                                    logger.warning(
                                        "[B5 Pipeline] STALLED: %d ticks, 0 total bars "
                                        "(uptime=%.0fs, grace_expired). "
                                        "symbols=%s timeframes=%s "
                                        "bar_keys=%s current_forming=%s "
                                        "last_tick=%s",
                                        ticks,
                                        uptime,
                                        cfg_symbols,
                                        sorted(self._required_timeframes),
                                        bar_keys,
                                        current_keys,
                                        last_tick.isoformat() if last_tick else "None",
                                    )
                                else:
                                    logger.debug(
                                        "[B5 Pipeline] Still stalled but rate-limited (last warning %.0fs ago)",
                                        now - self._last_pipeline_warning_time,
                                    )
                        # Card dcc7817d (3/3): ticks-flow-but-bars-static
                        # detector. Different from the total_bars==0 case
                        # above: bars exist, but no NEW bar has finalised
                        # for ≥20 min while ticks keep arriving. Tick-
                        # gated (only fires when ticks_received has grown
                        # past the value seen at the last bar build) so a
                        # quiet market (no ticks) doesn't false-positive.
                        # Threshold 1200s per Satsuki's M15-boundary
                        # correction (15 min would fire at every M15
                        # close under low tick rate).
                        elif (
                            not in_grace
                            and total_bars > 0
                            and (now - self._last_bar_built_at >= self._BARS_STATIC_THRESHOLD_SEC)
                            and (self._health.ticks_received > self._ticks_at_last_bar_built)
                        ):
                            if now - self._last_bars_static_warning_time >= 300:
                                self._last_bars_static_warning_time = now
                                ticks_since = self._health.ticks_received - self._ticks_at_last_bar_built
                                logger.warning(
                                    "[B5 Pipeline] BARS STATIC: %d ticks since "
                                    "last bar, age=%.0fs, threshold=%.0fs, "
                                    "total_bars=%d, last_bar_built_at_age=%.0fs",
                                    ticks_since,
                                    now - self._last_bar_built_at,
                                    self._BARS_STATIC_THRESHOLD_SEC,
                                    total_bars,
                                    now - self._last_bar_built_at,
                                )
                            else:
                                logger.debug(
                                    "[B5 Pipeline] Bars-static still tripping "
                                    "but rate-limited (last warning %.0fs ago)",
                                    now - self._last_bars_static_warning_time,
                                )

                    # Phase 1D: Portfolio summary from position monitor
                    if self._position_monitor is not None:
                        summary = self._position_monitor.get_portfolio_summary()
                        if summary["position_count"] > 0:
                            logger.info(
                                "[Portfolio] positions=%d unrealized_pnl=%.2f "
                                "notional=%.2f symbols=%s mfe=%.2f mae=%.2f",
                                summary["position_count"],
                                summary["total_unrealized_pnl"],
                                summary["total_notional_exposure"],
                                summary["positions_by_symbol"],
                                summary["total_mfe"],
                                summary["total_mae"],
                            )

                    # S1: Per-strategy diagnostic in B5 Periodic health
                    for sname in sorted(self._strategy_eval_counts):
                        last_eval_ago = time.monotonic() - self._strategy_last_eval.get(sname, 0)
                        logger.info(
                            "[S1 Health] %s: evals=%d no_signal=%d last=%.0fs ago",
                            sname,
                            self._strategy_eval_counts[sname],
                            self._strategy_no_signal_counts[sname],
                            last_eval_ago,
                        )
            except Exception as exc:
                logger.error("Health monitor error: %s", exc, exc_info=True)

    def _check_connection_health(self):
        if not self._running:
            return

        # BQ-1335: Stuck-state detection — if the spot feed is reporting
        # RECONNECTING/FAILED for longer than 60s, force a reconnect regardless
        # of tick freshness or backoff gate. Without this, a connection that
        # silently enters RECONNECTING (e.g. server-side hangup) can stay
        # stuck indefinitely because ticks never become stale (no ticks = no
        # staleness) and the backoff gate keeps skipping reconnect attempts.
        feed_state_mgr = getattr(self._market_feed, "state_manager", None) if self._market_feed is not None else None
        feed_state = feed_state_mgr.state if feed_state_mgr is not None else None
        is_stuck_state = (
            feed_state in (ConnectionState.RECONNECTING, ConnectionState.FAILED) if feed_state is not None else False
        )

        now_mono = time.monotonic()
        if is_stuck_state:
            if self._reconnect_stuck_at is None:
                self._reconnect_stuck_at = now_mono
            stuck_for = now_mono - self._reconnect_stuck_at
            if stuck_for >= self._stuck_reconnect_threshold_sec:
                # Don't trigger reconnect during forex market close — the
                # server intentionally drops sessions outside market hours.
                if _is_forex_market_closed():
                    return
                logger.warning(
                    "Spot feed stuck in %s for %.1fs (>= %.1fs) — forcing reconnect",
                    feed_state.value if feed_state is not None else "?",
                    stuck_for,
                    self._stuck_reconnect_threshold_sec,
                )
                # Bypass backoff gate so this forced attempt happens immediately.
                self._last_reconnect_attempt_at = 0.0
                self._attempt_reconnect()
                return
        else:
            # State is no longer stuck — clear the timer.
            if self._reconnect_stuck_at is not None:
                self._reconnect_stuck_at = None

        with self._lock:
            feed_connected = self._market_feed.is_running if self._market_feed else False
            last_tick = self._health.last_tick_at

        staleness = float("inf")
        if last_tick is not None:
            staleness = (datetime.now(timezone.utc) - last_tick).total_seconds()

        is_healthy = feed_connected and last_tick is not None and staleness < self._config.stale_tick_threshold_sec
        if is_healthy:
            self._reconnect_delay = self._config.reconnect_delay_sec
            # Card f37e7b74: sustained-tick gate. The counter is NOT reset
            # merely because the feed is currently sending ticks — the
            # single snapshot spot event from ProtoOASubscribeSpotsReq used
            # to short-circuit the gate and let a silent-but-subscribed
            # broker farm one tick per reconnect. We require N ticks within
            # T seconds of the last successful reconnect before clearing
            # the consecutive-failure counter. If the window has elapsed
            # without enough ticks, the gate expires and the counter is
            # left untouched (next reconnect attempt will trip the breaker
            # once max_reconnect_attempts is reached).
            if self._post_reconnect_at is not None:
                now_mono = time.monotonic()
                window_age = now_mono - self._post_reconnect_at
                if window_age > self._config.sustained_ticks_window_sec:
                    # Window expired without sustained recovery — drop the
                    # window state so we don't keep evaluating it forever.
                    logger.warning(
                        "Sustained-tick gate EXPIRED after %.1fs with only "
                        "%d/%d ticks — counter NOT reset (attempt=%d)",
                        window_age,
                        len(self._post_reconnect_ticks),
                        self._config.sustained_ticks_required,
                        self._health.reconnection_attempts,
                    )
                    # Card 7d3b535d (rework of f37e7b74 INSUFFICIENT): when
                    # the gate expires with only the snapshot event tick
                    # (``_post_reconnect_ticks <= 1``), the feed is in a
                    # "subscribed-but-silent" state — exactly the 21:45 UTC
                    # secondary flap mode. Bypass the reconnect-storm path
                    # (``_attempt_reconnect`` which counts toward the
                    # circuit breaker) and call ``_start_market_feed``
                    # directly so the feed gets a fresh subscribe cycle.
                    # This is the primary mitigation against the 24-min
                    # secondary flap observed Sep 8-10.
                    if len(self._post_reconnect_ticks) <= 1:
                        self._resubscribe_market_feed(skip_circuit=True)
                    self._post_reconnect_at = None
                    self._post_reconnect_ticks = []
                elif len(self._post_reconnect_ticks) >= self._config.sustained_ticks_required:
                    with self._lock:
                        if self._health.reconnection_attempts > 0:
                            logger.info(
                                "Sustained tick recovery — reset consecutive "
                                "reconnect counter (%d -> 0) after %d ticks "
                                "in %.1fs (gate: %d ticks / %.1fs)",
                                self._health.reconnection_attempts,
                                len(self._post_reconnect_ticks),
                                window_age,
                                self._config.sustained_ticks_required,
                                self._config.sustained_ticks_window_sec,
                            )
                            self._health.reconnection_attempts = 0
                    self._post_reconnect_at = None
                    self._post_reconnect_ticks = []
                # else: gate not yet satisfied — leave counter alone.
            return

        if feed_connected and last_tick is None:
            # No ticks received yet — skip reconnect (feed still initializing)
            return

        if feed_connected and _is_forex_market_closed():
            self._reconnect_delay = self._config.reconnect_delay_sec
            with self._lock:
                if self._health.reconnection_attempts > 0:
                    logger.info(
                        "Market closed — reset consecutive reconnect counter (%d -> 0)",
                        self._health.reconnection_attempts,
                    )
                    self._health.reconnection_attempts = 0
            return

        now = time.monotonic()
        if now - self._last_reconnect_attempt_at < self._reconnect_delay:
            return

        logger.warning(
            "Connection health check failed: connected=%s last_tick_ago=%.1fs threshold=%.1fs — reconnecting",
            feed_connected,
            staleness,
            self._config.stale_tick_threshold_sec,
        )

        self._last_reconnect_attempt_at = now
        self._attempt_reconnect()

    def _attempt_reconnect(self):
        with self._lock:
            self._health.reconnection_attempts += 1
            attempts = self._health.reconnection_attempts

        # Card f37e7b74: clear any stale post-reconnect window state at the
        # top of every attempt so a stalled-mid-window reconnect doesn't
        # carry forward a half-filled tick list from the previous cycle.
        self._post_reconnect_at = None
        self._post_reconnect_ticks = []

        if attempts >= self._config.max_reconnect_attempts:
            logger.critical(
                "Reconnect circuit-breaker tripped: %d consecutive attempts with no ticks "
                "(max=%d). Stopping engine gracefully.",
                attempts,
                self._config.max_reconnect_attempts,
            )
            # Set running=False directly instead of calling stop() to avoid
            # self-join deadlock (stop() calls _health_monitor_thread.join() which
            # is the current thread when called from _health_monitor_loop).
            self._running = False
            return

        logger.info(
            "Reconnection attempt %d (backoff=%.1fs)",
            self._health.reconnection_attempts,
            self._reconnect_delay,
        )
        # Structured diagnostic for post-mortem analysis (card ecbecd01).
        # Surfaces attempt number, current backoff, the last failure reason,
        # and how much validity remains on the auth token so operators can
        # correlate ALREADY_LOGGED_IN / token-expiry-driven reconnect storms.
        # IMPORTANT (card 54f3c0fa): ``_token_expires_at`` is a monotonic
        # ABSOLUTE expiry timestamp set once at successful auth — it is NOT
        # an age. Report remaining-seconds (computed at log time) so that
        # consecutive emissions actually decrease when the clock advances,
        # and emit ``None`` when the feed has not yet authed.
        # Card 0d64bec9: ``_market_feed`` may be a ``MagicMock`` test fixture
        # (TestReconnectCircuitBreaker._make_engine) where
        # ``getattr(MagicMock(), '_token_expires_at', None)`` returns a
        # MagicMock auto-attribute (NOT None). Subtracting ``time.monotonic()``
        # from a MagicMock yields another MagicMock which ``json.dumps``
        # cannot serialise — so we route the read through ``_safe_attr``
        # which detects Mock instances and falls back to the default.
        _token_expires_at = _safe_attr(self._market_feed, "_token_expires_at", None)
        if _token_expires_at is None:
            _token_validity_remaining_s = None
        else:
            _token_validity_remaining_s = _token_expires_at - time.monotonic()
        logger.info(
            "Reconnect diagnostic: %s",
            json.dumps(
                {
                    "attempt_no": self._health.reconnection_attempts,
                    "backoff_s": self._reconnect_delay,
                    "last_error": str(self._health.last_error or "unknown"),
                    "token_validity_remaining_s": _token_validity_remaining_s,
                }
            ),
        )

        if self._market_feed:
            try:
                self._market_feed.stop()
            except Exception as exc:
                logger.warning("Error stopping feed for reconnect: %s", exc)

        success = self._start_market_feed()
        if success:
            with self._lock:
                self._health.reconnection_successes += 1
            self._reconnect_delay = self._config.reconnect_delay_sec
            # BQ-1335: Clear the stuck-state timer now that we've recovered.
            self._reconnect_stuck_at = None
            # Card f37e7b74: do NOT reset reconnection_attempts=0 here.
            # The first tick we receive after this point is the snapshot
            # spot event from ProtoOASubscribeSpotsReq, which alone is not
            # evidence of a healthy feed. Open the sustained-tick window;
            # the reset happens in _health_monitor_loop once N ticks have
            # arrived within T seconds.
            self._post_reconnect_at = time.monotonic()
            self._post_reconnect_ticks = []
            logger.info(
                "Reconnection successful — sustained-tick gate active "
                "(need %d ticks in %.1fs to reset counter)",
                self._config.sustained_ticks_required,
                self._config.sustained_ticks_window_sec,
            )
        else:
            # Full-jitter backoff per AWS recommendations
            import random

            base = self._config.reconnect_delay_sec  # 5.0
            cap = self._config.max_reconnect_delay_sec  # 120.0
            attempts = self._health.reconnection_attempts
            exponent = min(attempts, 8)  # cap exponent to prevent overflow
            backoff_cap = min(cap, base * (2**exponent))
            self._reconnect_delay = random.uniform(0, backoff_cap)  # noqa: S311 — non-cryptographic full-jitter on reconnection delay (decorrelated jitter per AWS Architecture Blog)
            logger.warning(
                "Reconnection failed — next attempt in %.1fs (full-jitter, attempt=%d)",
                self._reconnect_delay,
                attempts,
            )

    def _resubscribe_market_feed(self, *, skip_circuit: bool = False) -> bool:
        """Quiet re-subscribe path for the single-snapshot-only state.

        Card 7d3b535d (rework of f37e7b74 INSUFFICIENT). When the
        sustained-tick gate expires with <=1 tick in the window, the feed
        is in "subscribed-but-silent" state — the snapshot event from
        ProtoOASubscribeSpotsReq fires once but subsequent ticks never
        arrive (server-side subscription propagation lag). This is the
        21:45 UTC secondary flap mode observed Sep 8-10.

        Bypasses :meth:`_attempt_reconnect` (which counts each failed
        attempt toward ``max_reconnect_attempts`` and trips the
        circuit-breaker after 20 attempts — the chaotic flap). Instead
        this calls :meth:`_start_market_feed` directly which:
        - Stops the existing feed cleanly.
        - Re-runs the OpenAPI bring-up (TCP + auth + fresh subscribe).
        - Opens a new sustained-tick gate window so we can detect
          whether the re-subscribe itself delivered ticks.

        Returns True iff re-subscribe succeeded; False if the feed could
        not be brought up (caller should fall through to standard
        stale-threshold handling in that case).
        """
        if not skip_circuit:
            # Defensive: callers must opt in to skipping the circuit
            # counter. Standard reconnect storm path is the right tool
            # when we genuinely don't know what's wrong.
            return self._attempt_reconnect()
        logger.info(
            "[Resubscribe] Single-snapshot-only detected — bypassing "
            "reconnect storm; calling _start_market_feed() directly"
        )
        if self._market_feed is not None:
            try:
                self._market_feed.stop()
            except Exception as exc:
                logger.warning("[Resubscribe] feed.stop() raised: %s", exc)
        success = self._start_market_feed()
        if success:
            with self._lock:
                self._health.reconnection_successes += 1
            self._reconnect_delay = self._config.reconnect_delay_sec
            self._reconnect_stuck_at = None
            # Open a new sustained-tick gate window so we can verify the
            # re-subscribe actually delivers ticks (vs repeating the
            # same snapshot-only state). Behaviour mirrors
            # ``_attempt_reconnect``'s success branch.
            self._post_reconnect_at = time.monotonic()
            self._post_reconnect_ticks = []
            logger.info(
                "[Resubscribe] successful — sustained-tick gate reopened "
                "(need %d ticks in %.1fs)",
                self._config.sustained_ticks_required,
                self._config.sustained_ticks_window_sec,
            )
        else:
            logger.warning(
                "[Resubscribe] _start_market_feed returned False — "
                "fall through to standard stale-threshold handling"
            )
        return success

    def _register_blend_position_mapping(self, signal: "CTraderTradeSignal", ctrader_position_id) -> None:
        """Wire a live-mode cTrader ``positionId`` to the blend_runner's
        canonical ``signal_id``.

        In live mode the ``paper_trader`` is bypassed entirely (orders go
        straight to cTrader via ``_execute_signal_live``), so
        ``paper_trader``'s ``on_trade_executed`` callback never fires and
        ``_on_trade_executed`` never gets a chance to wire the mapping.

        Without this mapping, :meth:`_on_position_closed` can't resolve
        the cTrader ``positionId`` back to the ``strategy_id + "_" +
        timestamp`` key the sizer stored risk under, so the first live
        close leaks a sizer risk slot (Phase 1A execution-path audit
        §5.4, "LIVE-mode position_id → signal_id mapping gap").

        Called from both:
        - the synchronous FILLED branch in :meth:`_execute_signal_live`
          (most live fills — ``event.wait()`` returned FILLED)
        - the late-fill FILLED branch in :meth:`_release_late` (audit
          §5.4 recommendation: covers the race where the execution
          event arrives after ``event.wait()`` timed out)
        """
        blend_runner = getattr(self, "_blend_runner", None)
        if blend_runner is None:
            return
        if not isinstance(ctrader_position_id, int) or ctrader_position_id == 0:
            return
        try:
            signal_id = blend_runner.make_signal_id(signal)
        except Exception as exc:
            logger.warning(
                "Could not build blend signal_id for cTrader position %s: %s",
                ctrader_position_id,
                exc,
            )
            return
        try:
            blend_runner.register_position_mapping(str(ctrader_position_id), signal_id)
        except Exception as exc:
            # Defensive — never let a mapping failure break the trade path.
            logger.warning(
                "register_position_mapping failed for cTrader position %s: %s",
                ctrader_position_id,
                exc,
            )

    def _on_trade_executed(self, result):
        # Phase 6A: Map position_id → signal_id for close() wiring.
        # The blend_runner registers risk under signal_id = strategy_id + "_" +
        # timestamp.  PaperTrader creates a Position with position_id = "POS_...".
        # We capture the mapping here so _on_position_closed can resolve it.
        if hasattr(result, "position") and result.position and hasattr(result, "signal") and result.signal:
            position_id = result.position.position_id
            signal = result.signal
            signal_id = (signal.strategy_id or "") + "_" + str(signal.timestamp.timestamp() if signal.timestamp else "")
            self._position_id_to_signal_id[position_id] = signal_id

            blend_runner = getattr(self, "_blend_runner", None)
            if blend_runner is not None and hasattr(blend_runner, "register_position_mapping"):
                blend_runner.register_position_mapping(position_id, signal_id)

        if self._trade_logger and result.order:
            # Propagate strategy_id from the originating signal so downstream
            # surfaces (TradeRecord + Discord notification) carry real provenance
            # instead of "unknown". Card 21bf4320 / Rin F-1.
            strategy_id = ""
            if getattr(result, "signal", None) is not None:
                strategy_id = getattr(result.signal, "strategy_id", "") or ""
            self._trade_logger.log_trade_opened(
                result.order, result.position, strategy_id=strategy_id
            )
        self._trigger_callback("on_trade_executed", result)

    def _on_position_closed(self, position):
        # Phase 6A: Wire close() into blend_runner → sizer.
        # When a position closes (SL hit, TP hit, or manual close), the
        # reserved risk must be released and PnL recorded.
        blend_runner = getattr(self, "_blend_runner", None)
        if blend_runner is not None:
            pnl = getattr(position, "closed_pnl", 0.0)
            position_id = getattr(position, "position_id", "")

            if hasattr(blend_runner, "close_position"):
                blend_runner.close_position(position_id, pnl)
            elif hasattr(blend_runner, "on_fill"):
                # Backward-compat: resolve signal_id from engine mapping
                signal_id = self._position_id_to_signal_id.pop(position_id, position_id)
                close_price = getattr(position, "closed_price", None) or getattr(position, "current_price", 0.0)
                blend_runner.on_fill(signal_id, close_price, pnl)

            # Log with signal_id, pnl, and remaining open_risk
            sizer = getattr(blend_runner, "_sizer", None)
            remaining_open_risk = sizer.open_risk if sizer is not None else 0.0
            signal_id = self._position_id_to_signal_id.get(position_id, position_id)
            logger.info(
                "Position closed: signal_id=%s pnl=%.2f open_risk=%.2f",
                signal_id,
                pnl,
                remaining_open_risk,
            )
        # Persist closed trade to trading.db (DEBT card 248d4f98)
        try:
            from data.trading_db import insert_closed_trade

            sig_id = self.position_id_to_signal_id.get(position_id, "")
            # strategy_id is the prefix before the last underscore-timestamp
            strategy = sig_id.rsplit("_", 1)[0] if "_" in sig_id else sig_id
            direction = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
            insert_closed_trade(
                trade_id=str(position_id),
                strategy_name=strategy,
                symbol=position.symbol,
                direction=direction,
                entry_price=position.entry_price,
                exit_price=getattr(position, "closed_price", None),
                entry_time=getattr(position, "opened_at", None),
                exit_time=getattr(position, "closed_at", None),
                lot_size=getattr(position, "volume", 0.0),
                stop_loss=getattr(position, "stop_loss", None),
                take_profit=getattr(position, "take_profit", None),
                source="live" if getattr(self._config, "live_mode", False) else "paper",
                pnl=pnl,
                close_reason=getattr(position, "comment", "") or None,
            )
        except Exception as db_exc:
            logger.warning("trading.db write failed (non-fatal): %s", db_exc)

        if self._trade_logger:
            self._trade_logger.log_position_closed(position)
        # Phase 1d: feed the BehavioralPolicy streak counter. A losing
        # close increments the counter (used by streak-loss cooldown);
        # a winning close resets it. ``closed_pnl`` is signed — positive
        # for wins, negative for losses. Attribute may be missing on
        # synthetic position objects during tests, so guard with getattr.
        closed_pnl = getattr(position, "closed_pnl", None)
        if closed_pnl is not None:
            with self._lock:
                if closed_pnl > 0:
                    self._consecutive_losses = 0
                elif closed_pnl < 0:
                    self._consecutive_losses += 1
        self._trigger_callback("on_position_closed", position)

    def _update_health(self):
        with self._lock:
            if self._start_time:
                self._health.uptime_sec = (datetime.now(timezone.utc) - self._start_time).total_seconds()

    def _trigger_callback(self, event: str, *args, **kwargs):
        for evt, callback in self._callbacks:
            if evt == event:
                try:
                    callback(*args, **kwargs)
                except Exception as e:
                    logger.error("Callback error for %s: %s", event, e)

    def _on_shutdown(self, signum, frame):
        logger.info("Shutdown signal received (sig=%d)", signum)
        self.stop()

    def preload_bars(self, symbol: str, period_minutes: int, bars: list[Bar]):
        """Preload historical bars for a specific symbol+timeframe."""
        key = self._bar_key(symbol, period_minutes)
        self._bars[key] = bars[-self._config.max_bars_per_symbol :]
        logger.info("Preloaded %d bars into key '%s'", len(self._bars[key]), key)

    def get_bars_including_forming(self, symbol: str, period_minutes: int) -> list[Bar]:
        """Get bars for a symbol+timeframe.

        WARNING: Includes the current FORMING bar as the last element if one exists.
        Do NOT use this for strategy evaluation — use self._bars directly instead.
        """
        key = self._bar_key(symbol, period_minutes)
        bars = list(self._bars.get(key, []))
        current = self._current_bar.get(key)
        if current is not None:
            bars.append(current)
        return bars

    def get_stats(self) -> dict:
        self._update_health()
        stats = self._paper_trader.get_stats() if self._paper_trader else None
        health = self.health
        return {
            "health": {
                "connected": health.connected,
                "last_tick_at": health.last_tick_at.isoformat() if health.last_tick_at else None,
                "ticks_received": health.ticks_received,
                "ticks_per_second": round(health.ticks_per_second, 2),
                "uptime_sec": round(health.uptime_sec, 1),
                "current_spread": round(self._current_spread, 5),
                # Phase 1d: kill-criteria + behavioral-policy diagnostics.
                "signals_killed_by_criteria": self._health.signals_killed_by_criteria,
                "behavioral_adjustments": self._health.behavioral_adjustments,
                "last_kill_reasons": list(self._health.last_kill_reasons),
                "last_behavioral_multiplier": self._health.last_behavioral_multiplier,
            },
            "trading": {
                "current_balance": stats.current_balance if stats else 0,
                "trades_executed": stats.trades_executed if stats else 0,
                "trades_rejected": stats.trades_rejected if stats else 0,
                "signals_blocked_by_risk": stats.signals_blocked_by_risk if stats else 0,
                "realized_pnl": stats.realized_pnl if stats else 0,
                "unrealized_pnl": stats.unrealized_pnl if stats else 0,
            },
            # S1: Per-strategy diagnostic stats
            "strategy_stats": {
                sname: {
                    "evals": self._strategy_eval_counts.get(sname, 0),
                    "no_signal": self._strategy_no_signal_counts.get(sname, 0),
                    "last_eval_ago_sec": round(time.monotonic() - self._strategy_last_eval.get(sname, 0), 1),
                }
                for sname in sorted(self._strategy_eval_counts)
            },
        }
