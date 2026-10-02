import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from risk.ftmo_params import (
    FTMO_BEST_DAY_CAP_PCT,
    FTMO_BEST_DAY_ENFORCE_PCT,
    FTMO_DAILY_DD_LIMIT_PCT,
    FTMO_MAX_CONCURRENT_POSITIONS,
    FTMO_MAX_TRADES_PER_DAY,
    FTMO_MIN_RISK_REWARD,
    FTMO_RISK_PER_TRADE_PCT,
    FTMO_TOTAL_DD_LIMIT_PCT,
)

from .models import CTraderTradeSignal, TradeDirection

logger = logging.getLogger(__name__)

# Trading day boundary: 17:00 America/Toronto (5 PM ET).
# Forex trading day rolls at 5 PM New York / Toronto time.
#
# FTMO contract: daily drawdown is calculated based on the previous
# day's balance at 5 PM EST (the broker's daily rollover). Per the
# task card (0d64bec9, 2026-09-18) adjudication directive — "adjudicate
# against FTMO contract like card 22fb282b did, cite source in proof
# if ambiguous, fail closed for live trading when in doubt" — the
# risk guard rolls its daily tracking window at 17:00 ET so the FTMO
# daily-loss budget aligns with the broker's reckoning.
#
# Note: commit 4216e55b (Craig, 2026-07-17) previously moved this to
# midnight Eastern for engine+guard consistency; that change
# deliberately deviated from FTMO and was reverted here because the
# live-trading FTMO daily-loss math requires 17:00 ET rollover. The
# engine-side daily reset at midnight is independent (it owns the
# ForwardTestHealth B5 loop, not the FTMO daily-loss math), so
# reverting _current_trading_day() does not affect engine-side reset.
_TRADING_TZ = ZoneInfo("America/Toronto")
_TRADING_DAY_RESET_HOUR = 17

# Default per-symbol max spread in pips.  Values are tightened to
# reject news-spike spreads while allowing normal interbank conditions.
# Reference: SRB-AYUMI-011 §5.1 rec 2.
_DEFAULT_SYMBOL_SPREADS: dict[str, float] = {
    "EURUSD": 1.0,  # tightest major — deep liquidity
    "GBPUSD": 2.0,
    "USDJPY": 1.0,  # tightest major — deep liquidity
    "XAUUSD": 30.0,  # gold: wider due to volatility, tightened from 40
    "AUDUSD": 2.0,
    "USDCHF": 2.0,
    "USDCAD": 2.0,
}


class RiskLimitType(Enum):
    DAILY_LOSS = "daily_loss"
    TOTAL_DRAWDOWN = "total_drawdown"
    MAX_TRADES = "max_trades"
    MAX_POSITIONS = "max_positions"
    MIN_RISK_REWARD = "min_risk_reward"
    POSITION_SIZE = "position_size"
    SPREAD = "spread"


@dataclass
class RiskLimitResult:
    allowed: bool
    limit_type: RiskLimitType
    message: str
    current_value: float = 0.0
    limit_value: float = 0.0


@dataclass
class FTMOProfile:
    """FTMO challenge risk profile.

    Defaults are imported from :mod:`risk.ftmo_params` (single source of
    truth) to prevent parameter divergence.  The previous inline defaults
    had ``daily_loss_limit_pct = 0.05`` (5%), which was 67% more permissive
    than the FTMO 1-Step Standard 3% daily DD limit.
    """

    risk_per_trade_pct: float = FTMO_RISK_PER_TRADE_PCT
    daily_loss_limit_pct: float = FTMO_DAILY_DD_LIMIT_PCT
    total_drawdown_limit_pct: float = FTMO_TOTAL_DD_LIMIT_PCT
    max_trades_per_day: int = FTMO_MAX_TRADES_PER_DAY
    max_positions: int = FTMO_MAX_CONCURRENT_POSITIONS
    min_risk_reward: float = FTMO_MIN_RISK_REWARD
    best_day_rule_max_pct: float = FTMO_BEST_DAY_CAP_PCT
    best_day_enforce_pct: float = FTMO_BEST_DAY_ENFORCE_PCT

    def __post_init__(self):
        if self.risk_per_trade_pct <= 0:
            raise ValueError(f"risk_per_trade_pct must be positive, got {self.risk_per_trade_pct}")
        if self.daily_loss_limit_pct <= 0:
            raise ValueError(f"daily_loss_limit_pct must be positive, got {self.daily_loss_limit_pct}")
        if self.max_trades_per_day <= 0:
            raise ValueError(f"max_trades_per_day must be positive, got {self.max_trades_per_day}")
        # Cross-check: theoretical worst-case risk (all trades hit SL) vs
        # daily limit.  This is a conservative guideline — the actual
        # enforcement happens at runtime via the daily loss circuit breaker
        # in check_trade_allowed_internal().  Warn rather than reject so
        # that standard FTMO params (0.5% risk × 10 trades = 5% worst-case)
        # remain valid alongside the 3% FTMO daily DD hard limit.
        max_total_risk = self.risk_per_trade_pct * self.max_trades_per_day
        if max_total_risk > self.daily_loss_limit_pct:
            import warnings as _w

            _w.warn(
                f"risk_per_trade_pct ({self.risk_per_trade_pct}) * "
                f"max_trades_per_day ({self.max_trades_per_day}) = "
                f"{max_total_risk:.4f} exceeds daily_loss_limit_pct "
                f"({self.daily_loss_limit_pct}). The runtime circuit "
                f"breaker enforces the daily limit dynamically.",
                UserWarning,
                stacklevel=2,
            )


FTMO_PROFILE_CHALLENGE = FTMOProfile()


@dataclass
class FTMOConfig:
    daily_loss_limit_pct: float = FTMO_PROFILE_CHALLENGE.daily_loss_limit_pct
    total_drawdown_limit_pct: float = FTMO_PROFILE_CHALLENGE.total_drawdown_limit_pct
    max_trades_per_day: int = FTMO_PROFILE_CHALLENGE.max_trades_per_day
    max_positions: int = FTMO_PROFILE_CHALLENGE.max_positions
    min_risk_reward: float = FTMO_PROFILE_CHALLENGE.min_risk_reward
    max_position_size_pct: float = FTMO_PROFILE_CHALLENGE.risk_per_trade_pct
    best_day_rule_max_pct: float = FTMO_PROFILE_CHALLENGE.best_day_rule_max_pct
    best_day_enforce_pct: float = FTMO_PROFILE_CHALLENGE.best_day_enforce_pct


@dataclass
class DailyTradingStats:
    date: date
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    best_day_profit: float = 0.0
    positive_days_pnl: float = 0.0


class RiskGuard:
    def __init__(
        self,
        ftmo_config: FTMOConfig | None = None,
        starting_balance: float = 100000.0,
        state_path: str = "data/state/risk_guard_state.json",
        *,
        symbol_max_spreads: dict[str, float] | None = None,
        default_max_spread: float = 2.0,
    ):
        self._config = ftmo_config or FTMOConfig()
        self._starting_balance = starting_balance
        self._peak_balance = starting_balance
        self._current_balance = starting_balance
        self._daily_start_balance = starting_balance
        self._current_day: date | None = None
        self._daily_stats: list[DailyTradingStats] = []
        self._lock = Lock()
        self._callbacks: list[Callable] = []
        self._daily_trade_count = 0
        self._total_trades = 0
        self._blocked_until: datetime | None = None
        self._circuit_breaker_triggered = False
        self._per_strategy_pnl: dict[str, float] = {}
        self._state_path = state_path
        # When True, update_balance() is a no-op — only sync_live_balance()
        # can change _current_balance.  Activated by sync_live_balance()
        # once the cTrader live feed provides an authoritative balance.
        self._live_balance_active = False

        # Spread gate wiring — allows RiskGuard to reject orders when
        # the live spread exceeds the per-symbol maximum.
        from confidence.gates import GateConfig, SpreadGate

        resolved_spreads = dict(_DEFAULT_SYMBOL_SPREADS)
        if symbol_max_spreads:
            resolved_spreads.update(symbol_max_spreads)
        self._gate_config = GateConfig(
            default_max_spread=default_max_spread,
            symbol_max_spreads=resolved_spreads,
        )
        self._spread_gate = SpreadGate(self._gate_config)

        self._restore_state()

    def check_signal(self, signal: CTraderTradeSignal) -> RiskLimitResult:
        with self._lock:
            return self._check_signal_internal(signal)

    def _check_signal_internal(self, signal: CTraderTradeSignal) -> RiskLimitResult:
        if self._circuit_breaker_triggered:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.DAILY_LOSS,
                message="Circuit breaker triggered - trading paused",
                current_value=1.0,
                limit_value=1.0,
            )

        if self._blocked_until and datetime.now(timezone.utc) < self._blocked_until:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.DAILY_LOSS,
                message=f"Trading blocked until {self._blocked_until}",
            )

        risk_reward = self._calculate_risk_reward(signal)
        if risk_reward < self._config.min_risk_reward:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.MIN_RISK_REWARD,
                message=f"Risk:Reward {risk_reward:.2f} below minimum {self._config.min_risk_reward}",
                current_value=risk_reward,
                limit_value=self._config.min_risk_reward,
            )

        return RiskLimitResult(
            allowed=True,
            limit_type=RiskLimitType.MIN_RISK_REWARD,
            message="Signal approved",
        )

    def check_trade_allowed(
        self,
        direction: TradeDirection,
        volume: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        account_balance: float | None = None,
        symbol: str | None = None,
        spread: float = 0.0,
    ) -> RiskLimitResult:
        with self._lock:
            return self._check_trade_allowed_internal(
                direction,
                volume,
                entry_price,
                stop_loss,
                take_profit,
                account_balance,
                symbol,
                spread,
            )

    def check_spread(self, symbol: str, spread: float) -> RiskLimitResult:
        """Check spread against per-symbol thresholds without evaluating other risk gates.

        Returns a RiskLimitResult with limit_type=SPREAD.  Useful for
        pre-checking spread at the signal-source level (e.g. in adapters)
        before a signal enters the full risk pipeline.
        """
        if spread <= 0 or not symbol:
            return RiskLimitResult(
                allowed=True,
                limit_type=RiskLimitType.SPREAD,
                message="No spread data — skipping spread gate",
            )
        gate_result = self._spread_gate.check(
            {
                "symbol": symbol,
                "spread": spread,
            }
        )
        if not gate_result.passed:
            threshold = self._gate_config.symbol_max_spreads.get(symbol, self._gate_config.default_max_spread)
            logger.warning(
                "spread_too_wide: symbol=%s spread=%.2f threshold=%.2f — trade blocked",
                symbol,
                spread,
                threshold,
            )
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.SPREAD,
                message=f"spread_too_wide: {symbol} spread={spread:.2f} threshold={threshold:.2f}",
                current_value=spread,
                limit_value=threshold,
            )
        return RiskLimitResult(
            allowed=True,
            limit_type=RiskLimitType.SPREAD,
            message="Spread within limits",
        )

    def _check_trade_allowed_internal(
        self,
        direction: TradeDirection,
        volume: float,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        account_balance: float | None = None,
        symbol: str | None = None,
        spread: float = 0.0,
    ) -> RiskLimitResult:
        if account_balance:
            self._current_balance = account_balance

        if self._circuit_breaker_triggered:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.DAILY_LOSS,
                message="Circuit breaker triggered - trading paused",
            )

        # Spread gate — reject if spread exceeds per-symbol maximum
        if spread > 0 and symbol:
            gate_result = self._spread_gate.check(
                {
                    "symbol": symbol,
                    "spread": spread,
                }
            )
            if not gate_result.passed:
                threshold = self._gate_config.symbol_max_spreads.get(symbol, self._gate_config.default_max_spread)
                logger.warning(
                    "spread_too_wide: symbol=%s spread=%.2f threshold=%.2f — trade blocked",
                    symbol,
                    spread,
                    threshold,
                )
                return RiskLimitResult(
                    allowed=False,
                    limit_type=RiskLimitType.SPREAD,
                    message=f"spread_too_wide: {symbol} spread={spread:.2f} threshold={threshold:.2f}",
                    current_value=spread,
                    limit_value=threshold,
                )

        if self._blocked_until and datetime.now(timezone.utc) < self._blocked_until:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.DAILY_LOSS,
                message=f"Trading blocked until {self._blocked_until}",
            )

        self._update_daily_tracking()

        # Only enforce daily loss limit when trades have actually occurred.
        # Without this guard, an externally-updated balance (e.g. from a
        # broker snapshot) can falsely trigger the circuit breaker when
        # no trades have been placed today.
        #
        # Daily loss limit is a FIXED dollar amount based on starting_balance
        # (prop-firm baseline), not a pct of the current day's start balance.
        # Craig's two-balance model (Jul 2026): daily_loss_limit_pct applies
        # to the original $10K starting balance, so 5% of $10K = $500 cap.
        daily_loss_pct = 0.0
        if self._daily_trade_count > 0:
            daily_loss_pct = (
                (self._daily_start_balance - self._current_balance) / self._starting_balance
                if self._starting_balance > 0
                else 0.0
            )
        if self._daily_trade_count > 0 and daily_loss_pct >= self._config.daily_loss_limit_pct:
            self._trigger_circuit_breaker(
                RiskLimitType.DAILY_LOSS,
                daily_loss_pct,
                self._config.daily_loss_limit_pct,
            )
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.DAILY_LOSS,
                message=f"Daily loss limit {daily_loss_pct * 100:.2f}% >= {self._config.daily_loss_limit_pct * 100}%",
                current_value=daily_loss_pct,
                limit_value=self._config.daily_loss_limit_pct,
            )

        # DD% measured from the fixed prop-firm starting balance ($10K),
        # not from the peak balance.  Craig's direction: "Everything based
        # on cTrader balance. Keep original 10k for drawdowns."
        if self._starting_balance > 0:
            drawdown_pct = (self._starting_balance - self._current_balance) / self._starting_balance
        else:
            drawdown_pct = 0.0
        if drawdown_pct >= self._config.total_drawdown_limit_pct:
            self._trigger_circuit_breaker(
                RiskLimitType.TOTAL_DRAWDOWN,
                drawdown_pct,
                self._config.total_drawdown_limit_pct,
            )
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.TOTAL_DRAWDOWN,
                message=f"Total drawdown {drawdown_pct * 100:.2f}% >= {self._config.total_drawdown_limit_pct * 100}%",
                current_value=drawdown_pct,
                limit_value=self._config.total_drawdown_limit_pct,
            )

        if self._daily_trade_count >= self._config.max_trades_per_day:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.MAX_TRADES,
                message=f"Daily trade count {self._daily_trade_count} >= {self._config.max_trades_per_day}",
                current_value=float(self._daily_trade_count),
                limit_value=float(self._config.max_trades_per_day),
            )

        # Risk-based position size check: SL hit cost as % of balance.
        # Notional exposure (volume * 100k) is not meaningful for FTMO rules —
        # what matters is how much you lose if SL is hit.
        sl_distance = abs(entry_price - stop_loss)
        # Use SymbolInfo metadata when symbol is available; fall back to price heuristic.
        from .models import get_symbol_info

        if symbol:
            sym_info = get_symbol_info(symbol)
            pip_size = sym_info.pip_size
            pip_value_per_lot = sym_info.pip_value_per_lot
        else:
            # Heuristic fallback: infer from price level
            is_jpy_pair = abs(entry_price) > 50
            if is_jpy_pair:
                if abs(entry_price) > 1000:
                    pip_size = 0.01
                    pip_value_per_lot = 1.0
                else:
                    pip_size = 0.01
                    pip_value_per_lot = 6.5
            else:
                pip_size = 0.0001
                pip_value_per_lot = 10.0
        sl_pips = sl_distance / pip_size if pip_size > 0 else 0
        risk_amount = volume * sl_pips * pip_value_per_lot
        risk_pct = risk_amount / self._current_balance if self._current_balance > 0 else float("inf")
        # Use >= with epsilon tolerance to avoid false rejections when risk
        # lands exactly at the limit due to rounding/approximation.
        epsilon = 0.0001  # 0.01% tolerance
        if risk_pct > self._config.max_position_size_pct + epsilon:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.POSITION_SIZE,
                message=f"Position risk {risk_pct * 100:.2f}% > max {self._config.max_position_size_pct * 100}%",
                current_value=risk_pct,
                limit_value=self._config.max_position_size_pct,
            )

        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        if risk == 0 or reward / risk < self._config.min_risk_reward:
            return RiskLimitResult(
                allowed=False,
                limit_type=RiskLimitType.MIN_RISK_REWARD,
                message=f"Risk:Reward below minimum {self._config.min_risk_reward}",
            )

        return RiskLimitResult(
            allowed=True,
            limit_type=RiskLimitType.POSITION_SIZE,
            message="Trade allowed",
        )

    def _calculate_risk_reward(self, signal: CTraderTradeSignal) -> float:
        risk = abs(signal.entry_price - signal.stop_loss)
        reward = abs(signal.take_profit_1 - signal.entry_price)
        if risk == 0:
            return 0.0
        return reward / risk

    def _current_trading_day(self) -> date:
        """Return the current trading day based on 17:00 America/Toronto.

        Trading day rolls at 17:00 Eastern (5 PM ET — the broker's
        daily rollover).  Aligns with the FTMO daily-loss math which
        is calculated from the previous day's balance at 5 PM EST.

        Card 0d64bec9 (sprint 2026-09-18-ayumi-prodbug-24): live
        trading requires FTMO-aligned daily rollover so the daily
        loss budget is comparable to the broker's reckoning. See
        FTMO contract citation in the module-level docstring for
        _TRADING_DAY_RESET_HOUR.
        """
        now_tz = datetime.now(_TRADING_TZ)
        if now_tz.hour >= _TRADING_DAY_RESET_HOUR:
            return now_tz.date()
        return now_tz.date() - timedelta(days=1)

    def _update_daily_tracking(self):
        today = self._current_trading_day()
        if self._current_day is None:
            self._current_day = today
            self._daily_start_balance = self._current_balance
            self._daily_trade_count = 0
        elif today != self._current_day:
            self._record_daily_stats()
            self._current_day = today
            self._daily_start_balance = self._current_balance
            self._daily_trade_count = 0

    def _record_daily_stats(self):
        if self._current_day:
            stats = DailyTradingStats(
                date=self._current_day,
                trades_count=self._daily_trade_count,
                pnl=self._current_balance - self._daily_start_balance,
            )
            self._daily_stats.append(stats)

            if stats.pnl > 0:
                if stats.pnl > self._get_best_day_profit():
                    self._check_best_day_rule(stats)

    def _get_best_day_profit(self) -> float:
        return max((s.pnl for s in self._daily_stats if s.pnl > 0), default=0.0)

    def _check_best_day_rule(self, stats: DailyTradingStats):
        """Evaluate FTMO best-day rule and enforce via _blocked_until when exceeded.

        Two thresholds:
          - ``best_day_enforce_pct`` (default 40%): halts trading until UTC
            midnight to prevent the ratio from climbing further.
          - ``best_day_rule_max_pct`` (default 50%): FTMO hard cap — logged
            as a CRITICAL warning for audit trail.
        """
        positive_days = [s for s in self._daily_stats if s.pnl > 0]
        if len(positive_days) < 2:
            return

        total_positive_pnl = sum(s.pnl for s in positive_days)
        best_day_pnl = max(s.pnl for s in positive_days)
        best_day_pct = best_day_pnl / total_positive_pnl if total_positive_pnl > 0 else 0

        if best_day_pct > self._config.best_day_rule_max_pct:
            logger.critical(
                f"Best day rule HARD CAP breached: Best day {best_day_pct * 100:.1f}% > "
                f"{self._config.best_day_rule_max_pct * 100:.1f}% FTMO limit"
            )

        if best_day_pct > self._config.best_day_enforce_pct:
            next_midnight = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            self._blocked_until = next_midnight
            logger.warning(
                f"Best day rule ENFORCED: Best day {best_day_pct * 100:.1f}% > "
                f"{self._config.best_day_enforce_pct * 100:.1f}% enforcement threshold. "
                f"Trading halted until UTC midnight ({next_midnight.isoformat()})."
            )

    def _trigger_circuit_breaker(self, limit_type: RiskLimitType, current: float, limit: float):
        if limit_type == RiskLimitType.DAILY_LOSS:
            # R2: Daily loss = block until UTC midnight (NOT 5 min)
            next_midnight = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            self._blocked_until = next_midnight
            # Do NOT set _circuit_breaker_triggered for daily loss —
            # this allows auto-recovery at UTC midnight without manual reset.
            logger.critical(
                f"DAILY LOSS HALT: blocking until UTC midnight "
                f"({next_midnight.isoformat()}) — {limit_type.value} = "
                f"{current * 100:.2f}% >= {limit * 100:.2f}%"
            )
        else:
            # Total drawdown or other breaches: PERMANENT circuit breaker.
            # R2 (Kaito): No auto-recovery, no _blocked_until expiry.
            # Requires explicit reset_circuit_breaker() call.
            self._circuit_breaker_triggered = True
            self._blocked_until = None  # permanent — no time-based expiry
            logger.critical(
                f"MAX DRAWDOWN BREACH: permanent block until manual reset "
                f"— {limit_type.value} = "
                f"{current * 100:.2f}% >= {limit * 100:.2f}%"
            )

        self._save_state()

        # Activate kill switch based on breach type
        # Uses injected kill_switch if available, otherwise creates one.
        # Tests should inject a mock to avoid writing to production state.
        try:
            ks = getattr(self, "_kill_switch", None)
            if ks is None:
                from .kill_switch import KillSwitchManager

                ks = KillSwitchManager()
            if limit_type == RiskLimitType.DAILY_LOSS:
                ks.activate_global_kill("ftmo_daily_loss_limit", "risk_guard", close_positions=True)
            elif limit_type == RiskLimitType.TOTAL_DRAWDOWN:
                ks.activate_global_kill("ftmo_total_drawdown", "risk_guard", close_positions=True)
        except Exception:
            logger.error("Failed to activate kill switch from risk guard", exc_info=True)

        for callback in self._callbacks:
            try:
                callback(limit_type, current, limit)
            except Exception as e:
                logger.error(f"Circuit breaker callback error: {e}")

    def record_trade(
        self,
        pnl: float,
        is_win: bool,
        trade_count_increment: int = 1,
    ):
        with self._lock:
            self._total_trades += trade_count_increment
            self._daily_trade_count += trade_count_increment

            # NOTE: Do NOT add pnl to _current_balance here.
            # _current_balance is synced from PaperTrader via update_balance()
            # / sync_live_balance(), which already includes all realised and
            # unrealised P&L.  Adding pnl here double-counts every trade close.

            if self._current_balance > self._peak_balance:
                self._peak_balance = self._current_balance

            self._update_daily_tracking()

            # Enforce daily loss limit immediately after recording trade.
            # Previously, daily loss was only checked in check_trade_allowed()
            # with a guard requiring _daily_trade_count > 0, meaning the loss
            # from trade #0 was not evaluated until trade #1 attempted to open.
            # This immediate check ensures the circuit breaker fires right
            # after any trade that breaches the daily loss limit.
            if self._daily_trade_count > 0 and self._starting_balance > 0:
                daily_loss_pct = (self._daily_start_balance - self._current_balance) / self._starting_balance
                if daily_loss_pct >= self._config.daily_loss_limit_pct:
                    self._trigger_circuit_breaker(
                        RiskLimitType.DAILY_LOSS,
                        daily_loss_pct,
                        self._config.daily_loss_limit_pct,
                    )
                    logger.warning(
                        "Daily loss limit breached on trade close: %.2f%% >= %.2f%% (pnl=%.2f)",
                        daily_loss_pct * 100,
                        self._config.daily_loss_limit_pct * 100,
                        pnl,
                    )

            self._save_state()

    def record_strategy_trade(self, strategy_id: str, pnl: float):
        with self._lock:
            self._per_strategy_pnl[strategy_id] = self._per_strategy_pnl.get(strategy_id, 0.0) + pnl

    def set_kill_switch(self, kill_switch):
        """Inject a kill switch instance (used by ForwardTestEngine to share state)."""
        self._kill_switch = kill_switch

    def update_balance(self, new_balance: float):
        """Update current balance.

        When live-balance mode is active (after :meth:`sync_live_balance`
        has been called with an authoritative cTrader balance), this method
        becomes a no-op.  PaperTrader.update_market_prices recalculates
        ``_current_balance`` from ``starting_balance + pnl`` on every tick;
        in live mode that recalculation is incorrect because the cTrader
        balance already includes all realised and unrealised P&L.
        Ignoring the paper recalculation prevents the in-memory balance
        from reverting to ``starting_balance`` between sync intervals.
        """
        with self._lock:
            if self._live_balance_active:
                return
            self._current_balance = new_balance
            if new_balance > self._peak_balance:
                self._peak_balance = new_balance

    def sync_live_balance(self, balance: float):
        """Set the authoritative balance from the cTrader live feed.

        Activates live-balance mode: subsequent :meth:`update_balance`
        calls are ignored so that PaperTrader tick recalculation cannot
        overwrite the synced value.  The balance will only change when
        this method is called again with a fresh cTrader balance.

        Should be called:
        - After the first successful ``_sync_live_balance`` from the engine
        - Periodically (e.g. every 5 min) to refresh the live balance
        """
        with self._lock:
            self._live_balance_active = True
            self._current_balance = balance
            if balance > self._peak_balance:
                self._peak_balance = balance
            self._save_state()

    def disable_live_balance(self):
        """Deactivate live-balance mode (testing / fallback to paper)."""
        with self._lock:
            self._live_balance_active = False

    def reset_daily_tracking(self):
        with self._lock:
            self._daily_start_balance = self._current_balance
            self._daily_trade_count = 0

    def reset_circuit_breaker(self, reason: str = "manual reset") -> bool:
        """Manually clear circuit breaker state.

        Only works for permanent (total drawdown) blocks, not daily loss
        time-based blocks.  Daily loss blocks expire automatically at UTC
        midnight and cannot be manually cleared.

        Returns True if the permanent block was cleared, False otherwise.
        """
        with self._lock:
            if self._blocked_until is not None:
                logger.warning("Cannot reset time-based block (daily loss). Wait for UTC midnight.")
                return False
            if not self._circuit_breaker_triggered:
                logger.info("Circuit breaker not triggered — nothing to reset.")
                return True
            self._circuit_breaker_triggered = False
            logger.info("Circuit breaker manually reset: %s", reason)
            self._save_state()
            return True

    def _save_state(self) -> None:
        """Persist RiskGuard state atomically to JSON (R1)."""
        state = {
            "peak_balance": self._peak_balance,
            "current_balance": self._current_balance,
            "daily_start_balance": self._daily_start_balance,
            "current_day": self._current_day.isoformat() if self._current_day else None,
            "daily_trade_count": self._daily_trade_count,
            "total_trades": self._total_trades,
            "circuit_breaker_triggered": self._circuit_breaker_triggered,
            "blocked_until": self._blocked_until.isoformat() if self._blocked_until else None,
            "last_save_ts": datetime.now(timezone.utc).isoformat(),
        }

        path = Path(self._state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            logger.error("Failed to save RiskGuard state", exc_info=True)

    def _restore_state(self) -> None:
        """Restore RiskGuard state from JSON file (R1)."""
        path = Path(self._state_path)
        if not path.exists():
            logger.info("No RiskGuard state file at %s — starting fresh", path)
            return

        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Corrupt RiskGuard state file %s: %s", path, e)
            return

        try:
            self._peak_balance = raw.get("peak_balance", self._peak_balance)
            self._current_balance = raw.get("current_balance", self._current_balance)
            self._daily_start_balance = raw.get("daily_start_balance", self._daily_start_balance)

            # Guard against stale state from a different account scale.
            # If restored balances are wildly out of proportion to the
            # configured starting_balance (e.g. a 100K state file loaded
            # into a 10K run), discard the entire stale payload.
            _tol = self._starting_balance * 5  # 5x tolerance is generous
            _stale = any(
                abs(v - self._starting_balance) > _tol
                for v in (
                    self._peak_balance,
                    self._current_balance,
                    self._daily_start_balance,
                )
            )
            if _stale:
                logger.warning(
                    "Stale RiskGuard state (balances ~%.2f vs starting %.2f) — resetting to fresh",
                    self._current_balance,
                    self._starting_balance,
                )
                self._peak_balance = self._starting_balance
                self._current_balance = self._starting_balance
                self._daily_start_balance = self._starting_balance
                self._daily_trade_count = 0
                self._total_trades = 0
                self._circuit_breaker_triggered = False
                self._blocked_until = None
                self._current_day = self._current_trading_day()
                self._save_state()
                return

            # Startup sanity gate: reject impossible state values.
            # peak_balance or daily_start_balance > 2x starting_balance
            # is impossible in normal operation (would require 100% gain
            # in a single day).  Reset to fresh state if detected.
            _sanity_max = self._starting_balance * 2
            if self._daily_start_balance > _sanity_max or self._peak_balance > _sanity_max:
                logger.warning(
                    "Startup sanity gate: impossible state values "
                    "(daily_start=%.2f peak=%.2f > 2x starting=%.2f) "
                    "— resetting to fresh",
                    self._daily_start_balance,
                    self._peak_balance,
                    self._starting_balance,
                )
                self._peak_balance = self._starting_balance
                self._current_balance = self._starting_balance
                self._daily_start_balance = self._starting_balance
                self._daily_trade_count = 0
                self._total_trades = 0
                self._circuit_breaker_triggered = False
                self._blocked_until = None
                self._current_day = datetime.now(timezone.utc).date()
                self._save_state()
                return

            day_str = raw.get("current_day")
            self._current_day = date.fromisoformat(day_str) if day_str else None

            self._daily_trade_count = raw.get("daily_trade_count", 0)
            self._total_trades = raw.get("total_trades", 0)
            self._circuit_breaker_triggered = raw.get("circuit_breaker_triggered", False)

            blocked_str = raw.get("blocked_until")
            if blocked_str:
                self._blocked_until = datetime.fromisoformat(blocked_str)
            else:
                self._blocked_until = None

            # If restored state is from a previous trading day, reset daily counters
            today = self._current_trading_day()
            if self._current_day is not None and self._current_day != today:
                logger.info(
                    "State from %s — resetting daily tracking for %s",
                    self._current_day,
                    today,
                )
                self._current_day = today
                self._daily_start_balance = self._current_balance
                self._daily_trade_count = 0
                # Clear expired daily-loss block
                if self._blocked_until and datetime.now(timezone.utc) >= self._blocked_until:
                    self._blocked_until = None

            logger.info("Restored RiskGuard state from %s", path)
        except Exception as e:
            logger.error("Failed to restore RiskGuard state: %s", e)

    def register_circuit_breaker_callback(self, callback: Callable):
        self._callbacks.append(callback)

    @property
    def daily_trade_count(self) -> int:
        return self._daily_trade_count

    @property
    def total_trades(self) -> int:
        return self._total_trades

    @property
    def current_drawdown_pct(self) -> float:
        """Drawdown from fixed prop-firm starting balance, not peak.

        Craig's two-balance model: DD% = (starting - live) / starting.
        Peak balance is tracked for informational purposes only.
        """
        if self._starting_balance <= 0:
            return 0.0
        return (self._starting_balance - self._current_balance) / self._starting_balance

    @property
    def current_daily_loss_pct(self) -> float:
        return (self._daily_start_balance - self._current_balance) / self._daily_start_balance

    @property
    def is_blocked(self) -> bool:
        if self._circuit_breaker_triggered:
            return True
        if self._blocked_until is not None and datetime.now(timezone.utc) < self._blocked_until:
            return True
        return False

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "total_trades": self._total_trades,
                "daily_trades": self._daily_trade_count,
                "current_balance": self._current_balance,
                "peak_balance": self._peak_balance,
                "daily_loss_pct": self.current_daily_loss_pct,
                "total_drawdown_pct": self.current_drawdown_pct,
                "is_blocked": self.is_blocked,
                "per_strategy_pnl": dict(self._per_strategy_pnl),
            }
