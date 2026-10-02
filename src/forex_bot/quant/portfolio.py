from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backtest.strategies import ISignalStrategy

logger = logging.getLogger(__name__)


class AllocationMethod(Enum):
    EQUAL_WEIGHT = "equal_weight"
    RISK_PARITY = "risk_parity"
    CONFIDENCE_WEIGHTED = "confidence_weighted"
    MANUAL = "manual"


@dataclass(frozen=True)
class StrategyAllocation:
    strategy_name: str
    symbol: str
    weight: float = 1.0
    max_risk_pct: float = 2.0
    max_positions: int = 3
    enabled: bool = True
    walk_forward_score: float = 0.0
    timeframe: str = "M15"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioConstraints:
    max_total_risk_pct: float = 5.0
    max_correlated_risk_pct: float = 3.0
    correlation_threshold: float = 0.70
    max_open_positions: int = 10
    max_drawdown_pct: float = 10.0
    max_daily_loss_pct: float = 3.0
    enforce_correlation_limits: bool = True


@dataclass(frozen=True)
class PortfolioConfig:
    allocations: tuple[StrategyAllocation, ...] = ()
    constraints: PortfolioConstraints = PortfolioConstraints()
    allocation_method: AllocationMethod = AllocationMethod.EQUAL_WEIGHT
    rebalance_on_close: bool = True


class ConflictResolution(Enum):
    HIGHEST_CONFIDENCE = "highest_confidence"
    HIGHEST_WEIGHT = "highest_weight"
    REJECT_CONFLICTING = "reject_conflicting"


@dataclass(frozen=True)
class PortfolioSignal:
    strategy_name: str
    symbol: str
    signal: Any
    weight: float
    adjusted_lot_size: float | None = None


@dataclass
class PortfolioTracker:
    balance: float = 100_000.0
    peak_balance: float = 100_000.0
    open_positions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    daily_pnl: float = 0.0
    daily_start_balance: float = 100_000.0
    strategy_pnl: dict[str, float] = field(default_factory=dict)
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    current_day: Any = None

    @property
    def max_drawdown_pct(self) -> float:
        if self.peak_balance <= 0:
            return 0.0
        return (self.peak_balance - self.balance) / self.peak_balance * 100

    @property
    def daily_loss_pct(self) -> float:
        if self.daily_start_balance <= 0:
            return 0.0
        return (self.daily_start_balance - self.balance) / self.daily_start_balance * 100

    def update_daily(self, bar_time: Any) -> None:
        day = bar_time.date()
        if self.current_day is None:
            self.current_day = day
            self.daily_start_balance = self.balance
            self.daily_pnl = 0.0
        elif day != self.current_day:
            self.current_day = day
            self.daily_start_balance = self.balance
            self.daily_pnl = 0.0

    def on_trade_closed(self, strategy_name: str, pnl: float) -> None:
        self.balance += pnl
        self.total_trades += 1
        self.daily_pnl += pnl
        self.strategy_pnl[strategy_name] = self.strategy_pnl.get(strategy_name, 0.0) + pnl
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

    def get_open_count_for_symbol(self, symbol: str) -> int:
        return len(self.open_positions.get(symbol, []))

    def get_total_open_positions(self) -> int:
        return sum(len(positions) for positions in self.open_positions.values())

    def get_open_symbols(self) -> set[str]:
        return set(self.open_positions.keys())


class StrategyPortfolio:
    def __init__(
        self,
        config: PortfolioConfig,
        tracker: PortfolioTracker | None = None,
    ):
        self._config = config
        self._tracker = tracker or PortfolioTracker()
        self._strategies: dict[str, ISignalStrategy] = {}
        self._correlation_cache: dict[str, float] = {}
        self._conflict_resolution = ConflictResolution.HIGHEST_CONFIDENCE

    @property
    def config(self) -> PortfolioConfig:
        return self._config

    @property
    def tracker(self) -> PortfolioTracker:
        return self._tracker

    def add_strategy(self, strategy: ISignalStrategy, allocation: StrategyAllocation) -> None:
        key = f"{allocation.strategy_name}:{allocation.symbol}"
        self._strategies[key] = strategy

    def set_correlation(self, pair_a: str, pair_b: str, correlation: float) -> None:
        key = f"{pair_a}:{pair_b}"
        self._correlation_cache[key] = correlation
        self._correlation_cache[f"{pair_b}:{pair_a}"] = correlation

    def get_correlation(self, pair_a: str, pair_b: str) -> float:
        if pair_a == pair_b:
            return 1.0
        return self._correlation_cache.get(f"{pair_a}:{pair_b}", 0.0)

    def get_enabled_allocations(self) -> list[StrategyAllocation]:
        return [a for a in self._config.allocations if a.enabled]

    def evaluate_all(
        self,
        market_states: dict[str, Any],
    ) -> list[PortfolioSignal]:
        allocations = self.get_enabled_allocations()
        constraints = self._config.constraints

        if self._tracker.daily_loss_pct >= constraints.max_daily_loss_pct:
            return []

        if self._tracker.max_drawdown_pct >= constraints.max_drawdown_pct:
            return []

        if self._tracker.get_total_open_positions() >= constraints.max_open_positions:
            return []

        portfolio_signals: list[PortfolioSignal] = []

        for allocation in allocations:
            key = f"{allocation.strategy_name}:{allocation.symbol}"
            strategy = self._strategies.get(key)
            if strategy is None:
                continue

            state = market_states.get(allocation.symbol)
            if state is None:
                continue

            if self._tracker.get_open_count_for_symbol(allocation.symbol) >= allocation.max_positions:
                continue

            try:
                signal = strategy.evaluate(state)
            except Exception:
                logger.warning(
                    "Strategy %s evaluate() failed, skipping",
                    allocation.strategy_name,
                    exc_info=True,
                )
                continue

            if signal is None:
                continue

            if not self._passes_correlation_check(allocation, signal):
                continue

            weight = self._calculate_weight(allocation)
            portfolio_signals.append(
                PortfolioSignal(
                    strategy_name=allocation.strategy_name,
                    symbol=allocation.symbol,
                    signal=signal,
                    weight=weight,
                )
            )

        return self._resolve_conflicts(portfolio_signals)

    def _passes_correlation_check(
        self,
        allocation: StrategyAllocation,
        signal: Any,
    ) -> bool:
        constraints = self._config.constraints

        if not constraints.enforce_correlation_limits:
            return True

        open_symbols = self._tracker.get_open_symbols()
        if not open_symbols:
            return True

        signal_direction = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)

        for open_sym in open_symbols:
            corr = self.get_correlation(allocation.symbol, open_sym)
            if corr >= constraints.correlation_threshold:
                for existing_positions in self._tracker.open_positions.get(open_sym, []):
                    existing_direction = existing_positions.get("direction")
                    if existing_direction is None:
                        logger.warning(
                            "Open position on %s missing 'direction' field, skipping correlation check",
                            open_sym,
                        )
                        continue
                    if existing_direction == signal_direction and corr >= constraints.correlation_threshold:
                        return False

        return True

    def _calculate_weight(self, allocation: StrategyAllocation) -> float:
        method = self._config.allocation_method

        if method == AllocationMethod.MANUAL:
            return allocation.weight

        if method == AllocationMethod.CONFIDENCE_WEIGHTED:
            base = allocation.weight
            if allocation.walk_forward_score > 0:
                return base * (0.5 + 0.5 * allocation.walk_forward_score)
            return base

        enabled = self.get_enabled_allocations()
        if method == AllocationMethod.EQUAL_WEIGHT:
            return 1.0 / max(len(enabled), 1)

        if method == AllocationMethod.RISK_PARITY:
            total = sum(a.max_risk_pct for a in enabled)
            return allocation.max_risk_pct / max(total, 1.0)

        return allocation.weight

    def _resolve_conflicts(
        self,
        signals: list[PortfolioSignal],
    ) -> list[PortfolioSignal]:
        by_symbol: dict[str, list[PortfolioSignal]] = {}
        for ps in signals:
            by_symbol.setdefault(ps.symbol, []).append(ps)

        resolved: list[PortfolioSignal] = []
        for symbol, sym_signals in by_symbol.items():  # noqa: B007
            if len(sym_signals) == 1:
                resolved.append(sym_signals[0])
                continue

            same_direction = [s for s in sym_signals if s.signal.direction == sym_signals[0].signal.direction]
            diff_direction = [s for s in sym_signals if s.signal.direction != sym_signals[0].signal.direction]

            if same_direction and not diff_direction:
                best = max(same_direction, key=lambda s: s.signal.confidence)
                resolved.append(best)
            elif self._conflict_resolution == ConflictResolution.HIGHEST_CONFIDENCE:
                best = max(sym_signals, key=lambda s: s.signal.confidence)
                resolved.append(best)
            elif self._conflict_resolution == ConflictResolution.REJECT_CONFLICTING:
                continue

        return resolved

    def calculate_position_size(
        self,
        signal: Any,
        allocation: StrategyAllocation,
    ) -> float:
        entry = signal.entry_price
        sl = signal.stop_loss
        if sl == 0 or entry == 0:
            return 0.0

        risk_distance = abs(entry - sl)
        risk_amount = self._tracker.balance * (allocation.max_risk_pct / 100.0)
        lot_size = risk_amount / (risk_distance * 100_000)

        total_weight = sum(self._calculate_weight(a) for a in self.get_enabled_allocations())
        if total_weight > 0:
            weight = self._calculate_weight(allocation) / total_weight
        else:
            weight = 1.0

        adjusted = lot_size * weight

        max_risk_total = self._tracker.balance * (self._config.constraints.max_total_risk_pct / 100.0)
        current_risk = sum(
            abs(p.get("entry_price", 0) - p.get("stop_loss", 0)) * p.get("lot_size", 0) * 100_000
            for positions in self._tracker.open_positions.values()
            for p in positions
        )

        remaining_risk = max(0, max_risk_total - current_risk)
        max_lot_for_risk = remaining_risk / (risk_distance * 100_000) if risk_distance > 0 else adjusted

        return min(adjusted, max_lot_for_risk)

    def open_position(
        self,
        signal: Any,
        allocation: StrategyAllocation,
        lot_size: float,
    ) -> None:
        symbol = allocation.symbol
        if symbol not in self._tracker.open_positions:
            self._tracker.open_positions[symbol] = []

        self._tracker.open_positions[symbol].append(
            {
                "strategy_name": allocation.strategy_name,
                "direction": signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "lot_size": lot_size,
                "confidence": signal.confidence,
            }
        )

    def close_position(self, symbol: str, strategy_name: str, pnl: float) -> None:
        positions = self._tracker.open_positions.get(symbol, [])
        for i, pos in enumerate(positions):
            if pos.get("strategy_name") == strategy_name:
                positions.pop(i)
                self._tracker.on_trade_closed(strategy_name, pnl)
                return
        logger.warning(
            "No open position found for %s/%s — possible orphaned position",
            symbol,
            strategy_name,
        )


def build_default_portfolio() -> StrategyPortfolio:
    from backtest.stat_arb import StatArbStrategy
    from backtest.strategies import (
        RegimeSwitchingRouter,
    )
    from strategies.momentum import MATrendFollowingStrategy, MomentumConfig
    from strategies.session_range_mean_reversion import (
        SessionRangeMeanReversionStrategy,
    )
    from strategies.volatility_squeeze import VolatilitySqueezeStrategy

    try:
        from strategies.grid.adapter import GridStrategyAdapter
        from strategies.grid.config import GridConfig

        _grid_available = True
    except ImportError:
        _grid_available = False
        GridStrategyAdapter = None  # type: ignore
        GridConfig = None  # type: ignore

    allocations = (
        StrategyAllocation(
            strategy_name="Session-Range Mean Reversion",
            symbol="GBPUSD",
            weight=1.5,
            max_risk_pct=1.5,
            max_positions=2,
            walk_forward_score=1.0,
            timeframe="H1",
            tags=("anchor", "mean_reversion"),
        ),
        StrategyAllocation(
            strategy_name="Session-Range Mean Reversion",
            symbol="EURUSD",
            weight=1.0,
            max_risk_pct=1.0,
            max_positions=2,
            walk_forward_score=0.8,
            timeframe="H1",
            tags=("mean_reversion",),
        ),
        StrategyAllocation(
            strategy_name="Regime-Switching Router",
            symbol="GBPUSD",
            weight=1.0,
            max_risk_pct=1.0,
            max_positions=1,
            walk_forward_score=0.8,
            timeframe="H1",
            tags=("regime_router",),
        ),
        StrategyAllocation(
            strategy_name="Statistical Arbitrage",
            symbol="EURUSD",
            weight=0.75,
            max_risk_pct=1.0,
            max_positions=1,
            walk_forward_score=0.6,
            timeframe="M15",
            tags=("stat_arb", "pairs_trading"),
        ),
        StrategyAllocation(
            strategy_name="Keltner Channel Breakout",
            symbol="EURUSD",
            weight=0.75,
            max_risk_pct=1.0,
            max_positions=2,
            walk_forward_score=0.6,
            timeframe="H1",
            tags=("breakout",),
        ),
        StrategyAllocation(
            strategy_name="MA Trend Following",
            symbol="GBPJPY",
            weight=0.75,
            max_risk_pct=1.0,
            max_positions=2,
            walk_forward_score=0.6,
            timeframe="H1",
            tags=("momentum", "trending"),
        ),
        StrategyAllocation(
            strategy_name="Grid Trading (EURUSD)",
            symbol="EURUSD",
            weight=0.5,
            max_risk_pct=0.5,
            max_positions=5,
            walk_forward_score=0.5,
            timeframe="M15",
            tags=("grid",),
        ),
        StrategyAllocation(
            strategy_name="Grid Trading (XAUUSD)",
            symbol="XAUUSD",
            weight=0.5,
            max_risk_pct=0.5,
            max_positions=3,
            walk_forward_score=0.5,
            timeframe="M15",
            tags=("grid", "commodity"),
        ),
    )

    constraints = PortfolioConstraints(
        max_total_risk_pct=5.0,
        max_correlated_risk_pct=3.0,
        correlation_threshold=0.70,
        max_open_positions=10,
        max_drawdown_pct=10.0,
        max_daily_loss_pct=3.0,
        enforce_correlation_limits=True,
    )

    config = PortfolioConfig(
        allocations=allocations,
        constraints=constraints,
        allocation_method=AllocationMethod.CONFIDENCE_WEIGHTED,
        rebalance_on_close=True,
    )

    portfolio = StrategyPortfolio(config)

    gbpusd_mr = SessionRangeMeanReversionStrategy()
    portfolio.add_strategy(gbpusd_mr, allocations[0])

    eurusd_mr = SessionRangeMeanReversionStrategy()
    portfolio.add_strategy(eurusd_mr, allocations[1])

    regime_router = RegimeSwitchingRouter(
        ranging_strategies=[SessionRangeMeanReversionStrategy()],
        volatile_strategies=[VolatilitySqueezeStrategy()],
    )
    portfolio.add_strategy(regime_router, allocations[2])

    stat_arb = StatArbStrategy()
    portfolio.add_strategy(stat_arb, allocations[3])

    from backtest.strategies import KeltnerChannelBreakoutStrategy

    keltner = KeltnerChannelBreakoutStrategy()
    portfolio.add_strategy(keltner, allocations[4])

    momentum_config = MomentumConfig(
        atr_period=14,
        atr_sl_multiplier=2.0,
        min_adx=20.0,
        rsi_period=14,
        session_filter=True,
        min_confidence=0.50,
    )
    ma_trend = MATrendFollowingStrategy(
        fast_period=8,
        slow_period=21,
        trend_ma_period=50,
        momentum=momentum_config,
    )
    portfolio.add_strategy(ma_trend, allocations[5])

    if _grid_available:
        eurusd_grid = GridStrategyAdapter(GridConfig.eurusd())
        portfolio.add_strategy(eurusd_grid, allocations[6])

        xauusd_grid = GridStrategyAdapter(GridConfig.xauusd())
        portfolio.add_strategy(xauusd_grid, allocations[7])
    else:
        logger.warning("strategies.grid is not available; grid strategies omitted from default portfolio")

    portfolio.set_correlation("EURUSD", "GBPUSD", 0.80)
    portfolio.set_correlation("EURUSD", "USDJPY", 0.30)
    portfolio.set_correlation("GBPUSD", "USDJPY", 0.40)
    portfolio.set_correlation("EURUSD", "XAUUSD", 0.15)
    portfolio.set_correlation("GBPUSD", "XAUUSD", 0.10)
    portfolio.set_correlation("GBPJPY", "EURUSD", 0.35)
    portfolio.set_correlation("GBPJPY", "GBPUSD", 0.65)

    return portfolio
