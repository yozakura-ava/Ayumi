# Architecture Spec v2 — Ayumi Forex Bot

**Author:** Kai (Lead Engineer)
**Date:** 2026-04-17
**Issue:** [AYU-41](/AYU/issues/AYU-41)
**Status:** Draft — Pending Ayumi Review
**Parent:** [AYU-38](/AYU/issues/AYU-38)

---

## 1. Executive Summary

This spec defines the target architecture for the Ayumi forex_bot v2. The current codebase (~27,000 lines across 115 Python files) is functional but suffers from massive code duplication (4 independent engine classes, 7+ copies of each technical indicator, 5+ pip-value implementations), 48 stub test files, dead modules, and subtle bugs that undermine backtest validity.

The v2 architecture consolidates to a **single composed backtest engine**, a **shared indicators library**, a **formal strategy protocol**, and **unified signal types** — while preserving the proven strategies (SRM, Killzone Momentum, TTC signal engine) and the operational live paper trading system.

**Design principles:**
- FTMO readiness is the north star
- Live paper trading must not break during migration
- Incremental migration path (not big-bang rewrite)
- One correct implementation of every shared concept
- Every module tested before it ships

---

## 2. Module Responsibility Matrix

```
src/forex_bot/
  core/              # Shared types: Bar, MarketState, enums, PipCalculator, SpreadModel
  indicators/        # Single correct implementation of ATR, RSI, ADX, EMA, SMA, STD, Bollinger, MACD, Stochastic
  engine/            # Single backtest engine built from composable mixins
  strategies/        # Production strategies implementing IStrategy protocol
  execution/         # cTrader adapters, order management, FTMO risk guard
  quant/             # Walk-forward validation, regime detection, portfolio, position sizing
  signal/            # TTC signal engine (pattern detection, confluence, gate validation)
  ml/                # ML pipeline (confidence learning, signal filtering)
  data/              # Historical data loading and storage
  config/            # Session definitions, strategy presets
```

### Responsibility Breakdown

| Module | Owns | Depends On | Lines (est.) |
|--------|------|------------|-------------|
| `core/` | `Bar`, `BarPeriod`, `MarketState`, `TradeDirection`, `TradeOutcome`, `ExitReason`, `SessionType`, `StrategySignal`, `SimulatedTrade`, `BacktestConfig`, `BacktestMetrics`, `PipCalculator`, `SpreadModel`, `IStrategy` protocol | `datetime`, `dataclasses`, `enum` | ~400 |
| `indicators/` | `atr()`, `rsi()`, `adx()`, `ema()`, `sma()`, `std()`, `bollinger_bands()`, `macd()`, `stochastic()`, `roc()`, `atr_percentile()` — all vectorized numpy/pandas, no state | `numpy`, `pandas` | ~300 |
| `engine/` | `BacktestEngine` (composed), mixins: `ProgressiveSLMixin`, `TradeManagementMixin`, `CombinedSignalMixin` | `core`, `indicators`, `quant`, `strategies` | ~800 |
| `strategies/` | `SessionRangeMeanReversion`, `KillzoneMomentum`, `Momentum`, `MLMeanReversion`, `TTSStrategy`, `ICTSMCStrategy`, grid adapter | `core`, `indicators`, `signal` | ~1,500 |
| `execution/` | `PaperTrader`, `OrderManager`, `RiskGuard`, `FTMOProfile`, `cTraderSignalAdapter`, `cTraderLiveAdapter`, `APIClient`, `MarketDataFeed`, `TradeLogger` | `core`, `strategies` | ~3,000 |
| `quant/` | `WalkForwardValidator`, `regime()`, `vaps()`, `StrategyPortfolio`, `QuantPipeline`, `position_sizing()`, `correlation()` | `core`, `indicators` | ~2,500 |
| `signal/` | `SignalEngineBridge`, `PatternDetector`, `GateValidator`, `ConfluenceScorer`, `HTFAnalyzer`, `SessionAnalyzer`, `SwingDetector`, `LevelCounter`, `StopTargetCalculator`, `TPManager` — all using `core.Signal` type | `core`, `indicators` | ~3,500 |
| `ml/` | `train_model()`, `features()`, `signal_simulator()`, `ConfidenceLearner`, `MLMeanReversionStrategy`, `per_symbol_configs` | `core`, `indicators` | ~3,000 |
| `data/` | `CTraderHistoricalClient`, `DataLoader` (CSV + API) | `pandas` | ~500 |
| `config/` | `SessionDefinition`, `KillzoneHours`, `StrategyPresets` | `datetime` | ~100 |

**Estimated total: ~15,600 lines** (down from ~27,000 — 42% reduction through deduplication and dead code removal)

---

## 3. Interface Definitions

### 3.1 Core Types (`core/types.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Protocol, Optional

class TradeDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"

class SessionType(StrEnum):
    ASIAN = "asian"
    LONDON = "london"
    NY_AM = "ny_am"
    NY_PM = "ny_pm"
    OUTSIDE = "outside"

class TradeOutcome(StrEnum):
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    OPEN = "open"

class ExitReason(StrEnum):
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    TAKE_PROFIT_3 = "take_profit_3"
    STOP_LOSS = "stop_loss"
    SIGNAL_FLIP = "signal_flip"
    END_OF_DATA = "end_of_data"
    MAX_DAILY_LOSS = "max_daily_loss"
    TIME_STOP = "time_stop"
    MOMENTUM_REVERSAL = "momentum_reversal"
    WEEKEND_CLOSE = "weekend_close"
    TRAILING_STOP = "trailing_stop"

@dataclass(frozen=True)
class BarPeriod:
    minutes: int

    @classmethod
    def M15(cls) -> "BarPeriod": return cls(15)
    @classmethod
    def H1(cls) -> "BarPeriod": return cls(60)
    @classmethod
    def H4(cls) -> "BarPeriod": return cls(240)
    @classmethod
    def D1(cls) -> "BarPeriod": return cls(1440)

@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    period: BarPeriod = field(default_factory=BarPeriod.H1)

@dataclass
class MarketState:
    bars: list[Bar]
    current_session: SessionType = SessionType.OUTSIDE

    @property
    def latest_bar(self) -> Bar:
        return self.bars[-1]

    @property
    def atr(self) -> float:
        from forex_bot.indicators import atr
        if len(self.bars) < 15:
            return 0.0001
        return atr(
            [b.high for b in self.bars],
            [b.low for b in self.bars],
            [b.close for b in self.bars],
            period=14,
        )

@dataclass
class StrategySignal:
    direction: TradeDirection
    confidence: float
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    take_profit_3: float
    rationale: str
    is_volatile: bool = False

@dataclass
class SimulatedTrade:
    entry_bar_index: int
    exit_bar_index: int | None = None
    direction: TradeDirection = TradeDirection.NEUTRAL
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    take_profit_3: float = 0.0
    exit_price: float = 0.0
    lot_size: float = 0.0
    risk_amount: float = 0.0
    pips: float = 0.0
    profit_loss: float = 0.0
    outcome: TradeOutcome = TradeOutcome.OPEN
    exit_reason: ExitReason | None = None
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    confidence_score: float = 0.0
    confluence_count: int = 0
    rationale: str = ""
```

### 3.2 PipCalculator (`core/pip.py`)

```python
class PipCalculator:
    """Single source of truth for pip value and spread calculations."""

    @staticmethod
    def pip_value(price: float) -> float:
        if price >= 50.0:
            return 0.01
        elif price >= 1.0:
            return 0.0001
        else:
            return 0.00000001

    @staticmethod
    def pips_to_price(price: float, pips: float) -> float:
        return pips * PipCalculator.pip_value(price)

    @staticmethod
    def price_to_pips(price: float, price_diff: float) -> float:
        pv = PipCalculator.pip_value(price)
        return price_diff / pv if pv > 0 else 0.0
```

### 3.3 SpreadModel (`core/spread.py`)

```python
@dataclass(frozen=True)
class SpreadModel:
    """
    Consistent spread application. Spread is applied at entry (round-trip equivalent).
    All engines use the same model — no more entry-only vs exit-only vs round-trip inconsistency.
    """
    spread_pips: float
    slippage_pips: float = 0.0

    def adjust_entry_long(self, price: float) -> float:
        return price + self._total_spread_price(price)

    def adjust_entry_short(self, price: float) -> float:
        return price - self._total_spread_price(price)

    def _total_spread_price(self, price: float) -> float:
        return PipCalculator.pips_to_price(price, self.spread_pips + self.slippage_pips)
```

### 3.4 IStrategy Protocol (`core/protocol.py`)

```python
class IStrategy(Protocol):
    """Formal protocol for all backtest-compatible strategies."""

    @property
    def name(self) -> str: ...

    def evaluate(self, state: MarketState) -> StrategySignal | None: ...
```

Key changes from v1:
- Uses `typing.Protocol` instead of duck-typing ABC (structural subtyping — any class with `name` + `evaluate` satisfies it)
- `TradeDirection.NEUTRAL` now exists in the enum (fixes TD-gap in adapter models)
- All strategies in `strategies/` will formally satisfy this protocol

### 3.5 BacktestConfig (`core/config.py`)

```python
@dataclass
class BacktestConfig:
    starting_balance: float = 100_000.0
    risk_per_trade_pct: float = 0.5
    max_daily_drawdown_pct: float = 0.05
    max_total_drawdown_pct: float = 0.10
    spread_pips: float = 1.5
    commission_per_lot: float = 3.5
    leverage: int = 100
    min_confidence: float = 0.50
    min_confluences: int = 2
    min_risk_reward: float = 1.5
    max_open_trades: int = 3
    min_bars_before_signal: int = 50
    partial_close_enabled: bool = True
    trailing_stop_enabled: bool = True
    regime_filter_enabled: bool = False
    slippage_pips: float = 0.5
    swap_per_lot_per_day: float = -3.5
    pair: str = "EURUSD"
    sharpe_annualization_factor: float = 252.0  # Explicit — no more sqrt(6048) inconsistency
```

### 3.6 Shared Indicators (`indicators/__init__.py`)

All indicator functions are **stateless, vectorized** (accept lists/arrays, return scalars or arrays). No class instances, no internal state.

```python
def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    """True Range averaged over `period` bars (Wilder smoothing)."""

def rsi(closes: Sequence[float], period: int = 14) -> float:
    """RSI using Wilder exponential smoothing (standard)."""

def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    """ADX with +DI/-DI. Wilder smoothing, no initialization bugs."""

def ema(values: Sequence[float], period: int) -> float:
    """Standard exponential moving average."""

def sma(values: Sequence[float], period: int) -> float:
    """Simple moving average."""

def std(values: Sequence[float], period: int) -> float:
    """Population standard deviation over period."""

def bollinger_bands(closes: Sequence[float], period: int = 20, num_std: float = 2.0) -> tuple[float, float, float]:
    """Returns (upper, middle, lower)."""

def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram)."""

def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               k_period: int = 14, d_period: int = 3) -> tuple[float, float]:
    """Returns (%K, %D)."""

def roc(closes: Sequence[float], period: int = 12) -> float:
    """Rate of change as percentage."""

def atr_percentile(closes: Sequence[float], period: int = 14, lookback: int = 50) -> float:
    """Current ATR percentile rank within lookback window."""
```

**Implementation rules:**
- RSI: Wilder EWM smoothing (matches ml/features.py — the correct version). Current killzone_momentum RSI uses simple average — this is a bug.
- ADX: Full +DM/-DM/TR with Wilder smoothing. Initialize smoothed values as sum of first `period` values (fixes TD-05 initialization bugs).
- ATR: True Range averaged with Wilder smoothing (same as RSI method).

### 3.7 Backtest Engine (`engine/`)

The v2 engine is built from a **base class + optional mixins**, not from 4 independent copies.

```
engine/
  base.py           # EngineCore — shared state, metrics, P&L, daily tracking
  mixins.py         # ProgressiveSLMixin, CombinedSignalMixin
  trade_mgmt.py     # TradeManagementMixin — delegates to quant.trade_management
  engine.py         # BacktestEngine = EngineCore + mixins (final composed class)
  config.py         # TradeManagementConfig (moved from backtest/trade_management/config.py)
```

#### EngineCore (`engine/base.py`)

```python
class EngineCore:
    """Shared backtest engine logic. Not used directly — composed into BacktestEngine."""

    def __init__(self, config: BacktestConfig, spread_model: SpreadModel):
        self.config = config
        self.spread_model = spread_model
        self.pip_calc = PipCalculator()
        self._reset()

    def _reset(self) -> None:
        self.balance = self.config.starting_balance
        self.peak_balance = self.balance
        self.max_drawdown = 0.0
        self.current_day: date | None = None
        self.daily_start_balance = self.balance
        self.max_daily_loss = 0.0
        self.total_spread_cost = 0.0
        self.total_commission_cost = 0.0
        self.rejected_signals = 0

    def _update_daily_tracking(self, bar_time: datetime) -> None: ...

    def _is_max_drawdown_breached(self) -> bool: ...

    def _is_max_daily_loss_breached(self) -> bool: ...

    def _close_trade(self, trade: SimulatedTrade, bar_index: int,
                     exit_time: datetime, exit_price: float,
                     reason: ExitReason) -> None:
        """Unified P&L calculation — spread + slippage + swap + commission.
        Uses SpreadModel consistently."""

    def _close_all_open_trades(self, open_trades: list[SimulatedTrade],
                               bar_index: int, exit_time: datetime,
                               exit_price: float) -> list[SimulatedTrade]: ...

    def _calculate_metrics(self, trades: list[SimulatedTrade],
                           equity_curve: list[float]) -> BacktestMetrics: ...

    def _calculate_sharpe_ratio(self, equity_curve: list[float]) -> float:
        """Annualized Sharpe using config.sharpe_annualization_factor (default sqrt(252))."""

    def _open_trade(self, signal: StrategySignal, bar: Bar, bar_index: int,
                    lot_size: float | None = None) -> SimulatedTrade | None:
        """Uses SpreadModel for entry adjustment. Margin check. Max lot cap."""
```

#### BacktestEngine (`engine/engine.py`)

```python
class BacktestEngine(EngineCore, ProgressiveSLMixin, TradeManagementMixin):
    """Composed backtest engine. Replaces all 4 v1 engine variants."""

    def __init__(self, config: BacktestConfig,
                 strategies: list[IStrategy],
                 trade_mgmt_config: TradeManagementConfig | None = None,
                 quant_config: QuantConfig | None = None,
                 spread_model: SpreadModel | None = None):
        EngineCore.__init__(self, config, spread_model or SpreadModel(config.spread_pips, config.slippage_pips))
        ProgressiveSLMixin.__init__(self, config)
        TradeManagementMixin.__init__(self, trade_mgmt_config or TradeManagementConfig.default())
        self.strategies = strategies
        self.quant_pipeline = QuantPipeline(quant_config) if quant_config else None

    def run_single(self, strategy: IStrategy, bars: list[Bar]) -> BacktestMetrics:
        """Run a single strategy. Full working backtest loop."""

    def run_all(self, bars: list[Bar]) -> dict[str, BacktestMetrics]:
        """Run each strategy independently."""

    def run_combined(self, bars: list[Bar],
                    method: CombineMethod = CombineMethod.WEIGHTED) -> BacktestMetrics:
        """Run combined signal from all strategies."""

    def run_walk_forward(self, bars: list[Bar], wf_config: WalkForwardConfig) -> WalkForwardResults:
        """Walk-forward validation. Delegates to quant.walk_forward."""
```

#### ProgressiveSLMixin (`engine/mixins.py`)

```python
class ProgressiveSLMixin:
    """TP1/TP2/TP3 exit with progressive SL management.
    Fixes: TP1 now checked (v1 engine.py missed it).
    Fixes: uses PipCalculator instead of inline pip value detection."""

    def _check_trade_exit(self, trade: SimulatedTrade, bar: Bar) -> tuple[bool, float, ExitReason]: ...

    def _progressive_sl_update(self, trade: SimulatedTrade, bar: Bar) -> None:
        """Move SL to BE+1pip at TP1, SL to TP1 at TP2.
        Uses PipCalculator.pip_value() — no more hasattr monkey-patching."""
```

### 3.8 Signal Engine Unification (`signal/`)

The signal engine's `Signal` type currently uses `str` for direction and a single `take_profit`. V2 aligns it with `core.Signal`:

```python
@dataclass
class Signal:
    symbol: str
    direction: TradeDirection          # Was str — now enum
    entry_price: float
    stop_loss: float
    take_profit: float                 # Single TP (signal engine domain)
    confidence: float
    gates_passed: list[str]
    boosters_active: list[str]
    pattern_type: str
    timeframe: str = "H1"
    timestamp: datetime | None = None
    reversal_score: float = 0.0
    quality_score: float = 0.0
    setup_type: str = ""

    def to_strategy_signal(self) -> StrategySignal:
        """Convert to backtest StrategySignal with 3 TPs derived from risk."""
        risk = abs(self.entry_price - self.stop_loss)
        return StrategySignal(
            direction=self.direction,
            confidence=self.confidence,
            entry_price=self.entry_price,
            stop_loss=self.stop_loss,
            take_profit_1=self.entry_price + risk * 1.0 if self.direction == TradeDirection.LONG else self.entry_price - risk * 1.0,
            take_profit_2=self.entry_price + risk * 2.0 if self.direction == TradeDirection.LONG else self.entry_price - risk * 2.0,
            take_profit_3=self.entry_price + risk * 3.0 if self.direction == TradeDirection.LONG else self.entry_price - risk * 3.0,
            rationale=f"[{self.pattern_type}] {self.setup_type}",
        )
```

### 3.9 Execution Layer (`execution/`)

No structural changes to `adapters/ctrader/` — just rename directory and fix bugs:

```python
execution/
  api_client.py       # FIX 4.4 protocol (unchanged)
  market_data_feed.py # WebSocket market data (unchanged)
  order_manager.py    # Order lifecycle (unchanged)
  risk_guard.py       # FTMO risk guard — fix TD-10 (_running flag)
  paper_trader.py     # Paper trader — fix TD-10
  signal_adapter.py   # Strategy-to-cTrader bridge
  trade_logger.py     # Logging (unchanged)
  models.py           # Execution domain models
```

### 3.10 Quant Layer (`quant/`)

Structure preserved. Key changes:
- Fix `_compute_metrics()` infinity PF bug (TD: zero losing trades division)
- Fix GO/NO-GO criteria discrepancy (>=3 vs >=2)
- Add minimum trade threshold (>=15 trades/window per strategy assessment recommendation)
- Remove `regime_detection.py` (dead duplicate)
- `quant/indicators.py` is **deleted** — all indicators now in `indicators/`

```python
quant/
  __init__.py
  walk_forward.py     # Fix: PF cap at 10.0 when 0 losses (no infinity), min 15 trades
  regime.py           # Uses indicators.adx() instead of hand-rolled
  vaps.py             # Uses indicators.atr() instead of inline
  position_sizing.py  # Unchanged
  portfolio.py        # Unchanged
  pipeline.py         # Uses indicators instead of inline
  config.py           # Unchanged
  correlation.py      # Cache optimization (TD-18)
  cointegration.py    # Unchanged
```

### 3.11 ML Layer (`ml/`)

Delete dead modules. Replace monkey-patching with config objects.

```python
ml/
  __init__.py         # Proper exports
  train_model.py      # Unchanged
  features.py         # Uses indicators.* instead of inline
  signal_simulator.py # Unchanged
  confidence_learner.py # Unchanged
  mean_reversion.py   # MLMeanReversionStrategy — fix O(n) per-bar rebuild
  per_symbol_configs.py # Unchanged (auto-generated)
  # DELETED: tier_optimizer.py, data_source.py, predict.py, weight_optimizer.py, optuna_optimizer.py, per_symbol_optimizer.py, backtest_with_ml.py, run_pipeline.py
```

---

## 4. Data Flow Diagrams

### 4.1 Backtest Flow (Single Strategy)

```
Bars (list[Bar])
  |
  v
BacktestEngine.run_single(strategy, bars)
  |
  +-- For each bar:
  |     |
  |     +-- _update_daily_tracking()
  |     +-- _is_max_drawdown_breached() → skip if true
  |     +-- _is_max_daily_loss_breached() → skip if true
  |     +-- _check_open_trades(open_trades, bar)
  |     |     +-- ProgressiveSLMixin._progressive_sl_update(trade, bar)
  |     |     +-- ProgressiveSLMixin._check_trade_exit(trade, bar)
  |     |     +-- EngineCore._close_trade(trade, ...)
  |     |
  |     +-- strategy.evaluate(state) → StrategySignal | None
  |     +-- [optional] QuantPipeline.pre_trade_check(signal)
  |     +-- [optional] TradeManagementMixin.check_entry_allowed(bar, signal)
  |     +-- EngineCore._open_trade(signal, bar)
  |
  +-- _close_all_open_trades() at end
  +-- _calculate_metrics() → BacktestMetrics
  v
BacktestMetrics
```

### 4.2 Backtest Flow (Combined Signals)

```
Bars (list[Bar])
  |
  v
BacktestEngine.run_combined(bars, method=WEIGHTED)
  |
  +-- For each bar:
  |     +-- (same drawdown/daily checks as single)
  |     +-- For each strategy: strategy.evaluate(state) → signals[]
  |     +-- _combine_signals(signals, method)
  |     |     +-- Separate long/short signals
  |     |     +-- Weighted average confidence per direction
  |     |     +-- Pick dominant direction if >= min_confidence
  |     |     +-- Aggregate: avg entry, worst SL, avg TPs
  |     +-- _open_trade(combined_signal, bar)
  |
  v
BacktestMetrics
```

### 4.3 Live Paper Trading Flow

```
cTrader Market Data Feed
  |
  v
cTraderLiveAdapter.evaluate_all_strategies(market_states, spread)
  |
  +-- For each (strategy, symbol):
  |     +-- cTraderSignalAdapter.evaluate_and_trade(state, spread)
  |           |
  |           +-- strategy.evaluate(state) → StrategySignal
  |           +-- Confidence filter (>= min_confidence)
  |           +-- PaperTrader.process_signal(signal, spread)
  |                 |
  |                 +-- RiskGuard.check_signal(signal) → R:R >= 1.5?
  |                 +-- OrderManager.calculate_position_size()
  |                 +-- RiskGuard.check_trade_allowed() → FTMO compliance
  |                 +-- _execute_order() (paper or live)
  v
Trade executed → Position tracked → SL/TP monitored via market data updates
```

### 4.4 Walk-Forward Validation Flow

```
Bars (list[Bar])
  |
  v
WalkForwardValidator.split()
  |  → [(train_0, val_0, test_0), (train_1, val_1, test_1), ...]
  |
  +-- For each window:
  |     +-- BacktestEngine.run_single(strategy, test_bars)
  |     +-- _compute_metrics(trades) → WindowMetrics
  |     +-- GO/NO-GO per window (WR>55%, PF>1.0, DD<10%, PnL>0, trades>=15)
  |
  +-- Aggregate → WalkForwardResults
  +-- Final GO: windows_passed >= 3 AND total_windows >= 3
  v
WalkForwardResults
```

### 4.5 Signal Engine Flow

```
Bars (DataFrame)
  |
  v
SignalEngineBridge.run(df)
  |
  +-- SwingDetector.detect(df) → swings[]
  +-- LevelCounter.count(swings) → levels[]
  +-- For each bar in lookback window:
  |     +-- Find nearest completed level
  |     +-- Check proximity (< 0.5%)
  |     +-- Determine direction from level type
  |     +-- GateValidator.validate(candidate, htf_state, session_state, levels)
  |     |     +-- G1: symmetry
  |     |     +-- G2: level completion
  |     |     +-- G3: session alignment
  |     |     +-- G4: HTF alignment (not CONFLICTING)
  |     |     +-- G5: no opposing active signal
  |     +-- ConfluenceScorer.score(candidate, htf_state, session_state)
  |     |     +-- 7 weighted boosters (session, level, HTF, EMA, boardroom, volume, pattern)
  |     +-- Build Signal with ATR-based SL and 3:1 R:R
  |
  v
list[Signal]
  |
  +-- signal.to_strategy_signal() → StrategySignal (for backtest)
  +-- cTraderSignalAdapter (for live execution)
```

---

## 5. Migration Strategy

### Phase 3A: Foundation (No Behavior Change)

These tasks can be done atomically without affecting any existing functionality.

| Step | Task | Files Changed | Risk |
|------|------|--------------|------|
| 3A.1 | Create `core/` package with all types extracted from `backtest/engine.py` | New `core/` package | LOW — pure extraction |
| 3A.2 | Create `indicators/` package with single correct implementations | New `indicators/` package | LOW — new module, not yet wired |
| 3A.3 | Create `PipCalculator` and `SpreadModel` in `core/` | New `core/pip.py`, `core/spread.py` | LOW |
| 3A.4 | Delete dead code: `ctrader_fix/`, `quant/regime_detection.py`, `ml/tier_optimizer.py`, `ml/data_source.py`, `ml/predict.py`, `signal_validator.py`, `backtest/parameter_sweep/legacy_optimizer.py` | Delete 7 files | LOW — zero imports |
| 3A.5 | Delete standalone ML scripts: `ml/weight_optimizer.py`, `ml/optuna_optimizer.py`, `ml/per_symbol_optimizer.py`, `ml/backtest_with_ml.py`, `ml/run_pipeline.py` | Delete 5 files | LOW — standalone scripts |
| 3A.6 | Write tests for `core/` types | New test files | LOW |
| 3A.7 | Write tests for `indicators/` (all functions) | New test files | LOW |
| 3A.8 | Write tests for `PipCalculator` and `SpreadModel` | New test files | LOW |

### Phase 3B: Engine Consolidation

| Step | Task | Files Changed | Risk |
|------|------|--------------|------|
| 3B.1 | Create `engine/base.py` — extract `EngineCore` with shared methods from all 4 engines | New file | MEDIUM — must match behavior |
| 3B.2 | Create `engine/mixins.py` — `ProgressiveSLMixin` (fixes TP1 check, uses PipCalculator) | New file | MEDIUM |
| 3B.3 | Create `engine/trade_mgmt.py` — `TradeManagementMixin` wrapping existing `TradeManager` | New file | MEDIUM |
| 3B.4 | Create `engine/engine.py` — composed `BacktestEngine` | New file | HIGH — replaces all 4 engines |
| 3B.5 | Migrate `backtest/runner.py` to use new `BacktestEngine` | Modify runner.py | HIGH |
| 3B.6 | Run full test suite — compare metrics between old and new engine on all production strategies | Validation | CRITICAL |
| 3B.7 | Fix critical bugs during migration: ADX (TD-05), Parabolic SAR (TD-07), confluence weights (TD-08), paper_trader._running (TD-10), H4 context drop (TD-09), infinity PF | Various | HIGH |
| 3B.8 | Migrate `adapters/ctrader/signal_adapter.py` to use new engine types | Modify signal_adapter.py | MEDIUM |

### Phase 3C: Strategy Migration

| Step | Task | Files Changed | Risk |
|------|------|--------------|------|
| 3C.1 | Migrate SRM strategy to use `indicators.*` instead of inline copies | Modify `strategies/session_range_mean_reversion.py` | LOW — same logic, different imports |
| 3C.2 | Migrate Killzone Momentum to use `indicators.*` | Modify `strategies/killzone_momentum.py` | LOW |
| 3C.3 | Migrate Momentum strategy to use `indicators.*` | Modify `strategies/momentum.py` | LOW |
| 3C.4 | Migrate all `backtest/strategies/strategy_legacy.py` strategies to use `indicators.*` | Modify strategy_legacy.py | MEDIUM |
| 3C.5 | Ensure all strategies formally satisfy `IStrategy` protocol | Verify | LOW |
| 3C.6 | Migrate signal engine to use `core.Signal` with `TradeDirection` enum | Modify `signal/` | MEDIUM |
| 3C.7 | Add `signal.to_strategy_signal()` conversion method | Modify signal data_types.py | LOW |
| 3C.8 | Migrate `ml/features.py` to use `indicators.*` | Modify features.py | LOW |
| 3C.9 | Migrate `quant/regime.py` to use `indicators.adx()` | Modify regime.py | LOW |
| 3C.10 | Migrate `quant/vaps.py` to use `indicators.atr()` | Modify vaps.py | LOW |

### Phase 3D: Test Infrastructure

| Step | Task | Risk |
|------|------|------|
| 3D.1 | Replace 48 stub test files with real tests — priority: engine, indicators, walk-forward, risk guard, SRM, Killzone | HIGH effort, LOW risk |
| 3D.2 | Add regression tests comparing old vs new engine metrics (golden dataset) | MEDIUM |
| 3D.3 | Add integration test: full backtest pipeline (load data → run strategy → verify metrics) | MEDIUM |
| 3D.4 | Add live paper trading smoke test (mock cTrader, verify signal flow) | MEDIUM |

### Phase 3E: Cleanup

| Step | Task | Risk |
|------|------|------|
| 3E.1 | Delete old engine files: `backtest/engine.py`, `backtest/enhanced_engine.py`, `backtest/multi_strategy_engine.py`, `backtest/amalgamation.py` | LOW — after validation |
| 3E.2 | Rename `backtest/` → `engine/` (or keep backtest as alias) | LOW |
| 3E.3 | Rename `signal_engine/` → `signal/` | LOW |
| 3E.4 | Rename `adapters/` → `execution/` | LOW |
| 3E.5 | Archive `cbot/` C# project (move to `archive/` or separate repo) | LOW |
| 3E.6 | Replace `datetime.utcnow()` with `datetime.now(timezone.utc)` everywhere | LOW |
| 3E.7 | Replace `print()` with structured logging | LOW |

### Migration Safety Net

- **Golden dataset**: Run all production strategies on 2 years of EURUSD/GBPUSD/XAUUSD H1 data with v1 engines. Save full metrics. After each migration step, re-run and diff. Any metric deviation >0.1% triggers investigation.
- **Feature flags**: New engine behind `USE_V2_ENGINE=true` env var during 3B. Both engines available during transition.
- **Rollback plan**: Git tags at each phase boundary. Revert if golden dataset deviates.

---

## 6. Execution Task Breakdown (Phase 3 Input)

### Tier 1: Foundation (Can start immediately, no dependencies)

| Task | Est. Effort | Dependencies |
|------|-------------|-------------|
| Create `core/` package with types, PipCalculator, SpreadModel | S (2h) | None |
| Create `indicators/` package with all functions + tests | M (4h) | None |
| Delete 12 dead/stale files | S (1h) | None |
| Write core/ tests | S (2h) | core/ creation |

### Tier 2: Engine Consolidation (Depends on Tier 1)

| Task | Est. Effort | Dependencies |
|------|-------------|-------------|
| Build `EngineCore` base class | M (4h) | core/ |
| Build mixins (ProgressiveSL, TradeManagement) | M (4h) | core/ |
| Compose `BacktestEngine` | M (4h) | EngineCore, mixins |
| Fix critical bugs (TD-05, TD-07, TD-08, TD-09, TD-10) | M (6h) | EngineCore |
| Golden dataset + regression comparison | M (4h) | BacktestEngine |
| Migrate runner.py | S (2h) | BacktestEngine |

### Tier 3: Strategy + Signal Migration (Depends on Tier 1)

| Task | Est. Effort | Dependencies |
|------|-------------|-------------|
| Migrate SRM to shared indicators | S (2h) | indicators/ |
| Migrate Killzone to shared indicators | S (2h) | indicators/ |
| Migrate remaining strategies | M (4h) | indicators/ |
| Unify signal engine types | M (4h) | core/ |
| Migrate quant/ml to shared indicators | M (4h) | indicators/ |

### Tier 4: Tests + Cleanup (Depends on Tier 2 + Tier 3)

| Task | Est. Effort | Dependencies |
|------|-------------|-------------|
| Replace 48 stub test files | L (16h) | All above |
| Integration tests | M (4h) | All above |
| Delete old engine files + rename directories | S (2h) | Validation pass |
| datetime/print cleanup | S (2h) | None |

**Estimated total: ~60 hours of focused implementation work**

---

## 7. Test Plan

### 7.1 Test Priority Order

| Priority | Module | Tests Required | Current State |
|----------|--------|---------------|---------------|
| P0 | `indicators/` | ATR, RSI, ADX, EMA, SMA, STD, Bollinger, MACD, Stochastic — known-answer tests with published values | No tests |
| P0 | `core/pip.py` | PipCalculator for JPY pairs, non-JPY pairs, commodities | No tests |
| P0 | `core/spread.py` | SpreadModel entry adjustment for long/short | No tests |
| P0 | `engine/base.py` | _close_trade P&L calculation, _calculate_metrics, _calculate_sharpe_ratio | Existing tests reference old engine |
| P1 | `engine/engine.py` | Full backtest loop with mock strategy, verify metrics | 48 stub tests |
| P1 | `quant/walk_forward.py` | GO/NO-GO criteria, PF infinity bug, window splitting | ~30 assertions |
| P1 | `execution/risk_guard.py` | FTMO rules, circuit breaker, daily loss | 41 assertions |
| P2 | `strategies/session_range_mean_reversion.py` | Known signals on golden dataset | Stub |
| P2 | `strategies/killzone_momentum.py` | Known signals on golden dataset | Stub |
| P2 | `signal/gate_validator.py` | Each gate pass/fail | Stub |
| P2 | `signal/confluence_scorer.py` | Booster scoring | Stub |
| P3 | `ml/features.py` | Feature matrix shape and values | No tests |
| P3 | `ml/mean_reversion.py` | ISignalStrategy interface compliance | Stub |
| P3 | `quant/portfolio.py` | Correlation check, conflict resolution | 86 assertions |

### 7.2 Test Approach

```
tests/
  unit/
    test_core_types.py        # Bar, MarketState, StrategySignal, SimulatedTrade
    test_pip_calculator.py    # All price ranges, JPY vs non-JPY
    test_spread_model.py      # Entry adjustment
    test_indicators.py        # Known-answer tests for all 11 indicators
    test_engine_core.py       # P&L, metrics, Sharpe, daily tracking
    test_engine_progressive_sl.py  # TP1/TP2/TP3 exit, SL movement
    test_engine_combined.py   # Signal combination methods
    test_walk_forward.py      # GO/NO-GO, window splitting, PF cap
    test_risk_guard.py        # FTMO rules, circuit breaker
    test_gate_validator.py    # G1-G5 pass/fail
    test_confluence_scorer.py # 7 boosters
    test_regime.py            # Volatility, trend, session, combined
    test_vaps.py              # Position sizing with regime
    test_portfolio.py         # Correlation, conflict resolution, allocation
  integration/
    test_backtest_pipeline.py    # Load data → run strategy → verify metrics
    test_walk_forward_pipeline.py # Full walk-forward on golden dataset
    test_signal_engine_pipeline.py # Bars → SignalEngineBridge → signals
    test_live_paper_flow.py       # Mock cTrader → signal → risk guard → execution
  regression/
    golden_eurusd_h1.pkl       # Pre-computed metrics for EURUSD H1 (2yr)
    golden_gbpusd_h1.pkl       # Pre-computed metrics for GBPUSD H1 (2yr)
    test_regression_metrics.py # Re-run strategies, compare to golden
```

### 7.3 Minimum Test Bar

Before Phase 3E cleanup (deleting old engines), all of the following must pass:

- [ ] All `indicators/` functions have known-answer tests
- [ ] `PipCalculator` tested for JPY, non-JPY, commodity pairs
- [ ] `SpreadModel` tested for long/short entry adjustment
- [ ] `EngineCore._close_trade` produces identical P&L to v1 `BacktestEngine._close_trade` on 10 specific trades
- [ ] `EngineCore._calculate_sharpe_ratio` matches v1 (sqrt(252)) — verified against golden dataset
- [ ] `BacktestEngine.run_single()` produces identical metrics to `MultiStrategyBacktestEngine._run_single_strategy()` on golden dataset
- [ ] `BacktestEngine.run_combined()` produces identical metrics to `AmalgamatedBacktestEngine.run()` on golden dataset
- [ ] Walk-forward produces identical GO/NO-GO decisions (with PF infinity fix applied)
- [ ] All 48 stub test files replaced with real tests (minimum 5 assertions each)
- [ ] `pytest tests/ -q` passes with 0 failures

---

## 8. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Engine consolidation introduces metric drift | MEDIUM | HIGH | Golden dataset comparison at every step |
| Indicator migration changes strategy behavior | LOW | HIGH | Known-answer tests; strategies are math-equivalent |
| Live paper trading breaks during migration | LOW | CRITICAL | Feature flag; old engine available until validated |
| Walk-forward results change after PF infinity fix | HIGH | MEDIUM | Expected — old results were invalid; document the change |
| ADX fix changes strategy signals | MEDIUM | MEDIUM | Expected — old ADX had bugs; re-baseline all strategies |

---

## 9. Decisions Requiring Ayumi Input

1. **`cbot/` C# project** — Archive in-repo under `archive/` or move to separate repo? Recommendation: separate repo to avoid confusion.

2. **Sharpe annualization** — Standardize on `sqrt(252)` everywhere (trading days). `runner.py` currently uses `sqrt(6048)` (H1 bars/year). This will change Sharpe values in existing reports. Confirm this is acceptable.

3. **Feature flag duration** — How long should both old and new engines coexist? Recommendation: keep old engines until golden dataset passes, then delete in same PR.

4. **Minimum trade count for walk-forward** — Strategy assessment recommends >=15 trades/window. Current walk-forward uses >=5. Confirm raising to 15 is acceptable (may cause more NO-GO results).

5. **ICT/SMC confluence weight fix** — Current weights sum to 1.15 (TD-08). Proposed fix: redistribute to sum to 1.0. This will change ICT strategy signals. Confirm this is acceptable.

## 10. Incident Postmortem — 2026-06-16 Zero-Eval

**Symptom.** The forward test had been running for hours with thousands of ticks and a handful of bars built, but every one of the nine registered strategies reported `evals=0` and `last=N days ago` in the heartbeat JSON. With market data flowing and bars finalising, no strategy was ever invoked.

**Root cause.** The bug lived in the launcher, not the base engine. `BlendForwardTestEngine._evaluate_strategies` in `scripts/launch_blend_forward_test.py` (line 229) overrode the base class method from `src/forex_bot/adapters/ctrader/forward_test_engine.py:836` to add blend-aware signal routing, but the override never incremented the per-strategy health counters. The base class increments `_strategy_eval_counts[name]`, `_strategy_no_signal_counts[name]`, and `_strategy_last_eval[name]` explicitly inside its evaluation loop; the subclass assumed that bookkeeping happened implicitly elsewhere. It does not.

**Why it existed.** The subclass predates the health-counter instrumentation. When the per-strategy health fields were added to the base engine in an earlier refactor, the launcher override was not re-audited. The override's only intentional divergence was blend-routing — counters were an accidental casualty of the assumption that "the base loop is still doing its job." It is not, once you replace the method.

**Fix.** Commit `47e4a0c` (BQ-1037) added explicit counter increments to the launcher override at line 282–289, mirroring the base class pattern: increment on every strategy invocation, increment the no-signal counter on a `None` return, stamp `time.monotonic()` into `_strategy_last_eval`. The counter block is now the first thing inside the per-strategy `try` so it fires even if downstream signal processing throws.

**Verification.** Forward test restart at 10:30 EDT showed all nine strategies at `evals=18` within minutes (heartbeat `data/heartbeat_trading.json`), and at least one signal was routed to the paper trader within the same window.

**Lesson.** A subclass that overrides a method which mutates `self` is not free — the override inherits the base class's contract, including its side effects. Whenever an engine method increments counters, caches, or health fields, any subclass override must either (a) call the base method, (b) re-implement the increments, or (c) be reviewed against the base. None of the three was done here. Add a CI check or a code-review checklist item: "if you override `_evaluate_strategies`, `_on_tick`, or `_bar_completed`, grep the base for `self._strategy_` and confirm those lines are preserved."

## 11. BQ-1043 Infrastructure Rebuild (2026-06-16)

**Scope.** Full rebuild of the cTrader adapter layer. Replaced 5 legacy modules with a clean, testable infrastructure spanning session, credentials, token lifecycle, market data, order gateway, position tracking, and event handling.

**New modules (`src/forex_bot/adapters/ctrader/`):**
- `session.py` — `cTraderSession` state machine (replaces `auth.py`)
- `credential_store.py` — atomic credential persistence (replaces `credentials.py`)
- `token_lifecycle.py` — token rotation and validation (replaces `token_manager.py`, `oauth_refresh.py`)
- `market_data_feed.py` — spot feed abstraction (replaces `open_api_spot_feed.py`)
- `order_gateway.py` — order placement with error classification
- `position_tracker.py` — position reconciliation against cTrader
- `execution_event_handler.py` — execution event routing
- `connection_watchdog.py`, `reconnect_strategy.py`, `error_classifier.py`, `kill_switch.py` — resilience layer

**Archived (with compatibility shims) in `archive/legacy_ctrader/`:**
- `open_api_spot_feed.py`, `token_manager.py`, `oauth_refresh.py`, `credentials.py`, `auth.py`

**Volume fix.** cTrader volume is in centi-lots. The base unit `lotSize = 100_000_000` (1 standard lot = 100M volume units). Conversion: `volume_in_units = lots × lotSize`. For 0.01 lots, send `volume = 1_000_000` (10^6). Confirmed empirically — initial attempts using `lots × 100_000` were 1000x too small.

**First verified trade cycle.** Position 266374347 (GBPUSD, 0.01 lots) opened, tracked through `position_tracker.py`, closed cleanly via the order gateway. Confirms end-to-end pipeline works.

**Shim strategy.** 5 compatibility shims (`auth.py`, `credentials.py`, `oauth_refresh.py`, `open_api_spot_feed.py`, `token_manager.py`) re-export from `archive.legacy_ctrader._pkg/` so the legacy launcher and its tests continue to function. Remove shims once the v2 launcher is the sole entry point.

**Verification.** 3504 tests collected, 0 collection errors after archive + shim.
