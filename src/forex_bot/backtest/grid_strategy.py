from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from utils.pip_value import DEFAULT_PIP, pip_value_for_symbol

from .engine import Bar, MarketState, StrategySignal, TradeDirection
from .strategies import ISignalStrategy


class GridDirection(Enum):
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


@dataclass
class GridLevel:
    level_index: int
    price: float
    size_percent: float
    tp_multiplier: float
    is_buy: bool
    order_placed: bool = False
    order_filled: bool = False
    filled_price: float = 0.0
    filled_time: datetime | None = None


@dataclass
class GridConfig:
    grid_spacing_pips: float = 20.0
    num_levels: int = 10
    initial_spacing_type: str = "fixed"
    atr_multiplier: float = 0.5
    position_sizing_type: str = "equal"
    base_lot_size: float = 0.01
    max_concurrent_positions: int = 5
    grid_expiry_bars: int = 0
    direction: GridDirection = GridDirection.BOTH
    atr_period: int = 14
    atr_ma_period: int = 50
    use_midnight_grid: bool = False
    grid_start_time: datetime | None = None
    pair: str = "EURUSD"

    @property
    def spacing_type(self) -> str:
        return self.initial_spacing_type

    def to_preset(self, pair: str) -> GridConfig:
        presets = GRID_PRESETS.get(pair, GRID_PRESETS["EURUSD"])
        return GridConfig(
            grid_spacing_pips=presets["grid_spacing_pips"],
            num_levels=presets["num_levels"],
            initial_spacing_type=self.initial_spacing_type,
            atr_multiplier=self.atr_multiplier,
            position_sizing_type=self.position_sizing_type,
            base_lot_size=self.base_lot_size,
            max_concurrent_positions=self.max_concurrent_positions,
            grid_expiry_bars=self.grid_expiry_bars,
            direction=self.direction,
            atr_period=self.atr_period,
            atr_ma_period=self.atr_ma_period,
            use_midnight_grid=self.use_midnight_grid,
            grid_start_time=self.grid_start_time,
            pair=pair,
        )

    @classmethod
    def ftmo(cls, pair: str = "EURUSD") -> "GridConfig":
        """FTMO-constrained conservative grid preset for the given pair.

        Pass ``pair`` to bind the returned config to that symbol; omit it
        (or pass an empty string) to keep the EURUSD default. Card
        d8b13347 fixed the missing ``pair=`` forward so the dataclass
        default no longer silently overrides the caller's intent.
        """
        effective_pair = pair if pair else "EURUSD"
        presets = GRID_PRESETS.get(effective_pair, GRID_PRESETS["EURUSD"])
        return cls(
            grid_spacing_pips=presets["grid_spacing_pips"],
            num_levels=presets["num_levels"],
            atr_multiplier=presets.get("atr_multiplier", 0.5),
            max_concurrent_positions=presets.get("max_concurrent_positions", 5),
            initial_spacing_type="fixed",
            position_sizing_type="equal",
            pair=effective_pair,
        )


GRID_PRESETS: dict[str, dict] = {
    "EURUSD": {
        "grid_spacing_pips": 15.0,
        "num_levels": 10,
        "atr_multiplier": 0.5,
        "max_concurrent_positions": 5,
    },
    "GBPJPY": {
        "grid_spacing_pips": 25.0,
        "num_levels": 8,
        "atr_multiplier": 0.6,
        "max_concurrent_positions": 4,
    },
    "USDJPY": {
        "grid_spacing_pips": 20.0,
        "num_levels": 10,
        "atr_multiplier": 0.5,
        "max_concurrent_positions": 5,
    },
    "XAUUSD": {
        "grid_spacing_pips": 150.0,
        "num_levels": 6,
        "atr_multiplier": 0.4,
        "max_concurrent_positions": 3,
    },
}


class GridState:
    def __init__(self, config: GridConfig):
        self.config = config
        self.levels: list[GridLevel] = []
        self.grid_active: bool = False
        self.grid_start_bar: int = 0
        self.filled_count: int = 0
        self.base_price: float = 0.0
        self.pip_size: float = self._get_pip_size(config.pair)
        self.bar_count: int = 0

    def _get_pip_size(self, pair: str) -> float:
        try:
            return pip_value_for_symbol(pair)
        except (ValueError, TypeError):
            return DEFAULT_PIP

    def initialize_grid(self, mid_price: float) -> None:
        self.levels = []
        self.base_price = mid_price
        spacing = self.config.grid_spacing_pips * self.pip_size

        for i in range(1, self.config.num_levels + 1):
            buy_price = mid_price - (spacing * i)
            sell_price = mid_price + (spacing * i)
            size = self._calculate_position_size(i)

            self.levels.append(
                GridLevel(
                    level_index=i,
                    price=buy_price,
                    size_percent=size,
                    tp_multiplier=i * 0.5,
                    is_buy=True,
                )
            )
            self.levels.append(
                GridLevel(
                    level_index=-i,
                    price=sell_price,
                    size_percent=size,
                    tp_multiplier=i * 0.5,
                    is_buy=False,
                )
            )

        self.grid_active = True

    def _calculate_position_size(self, level_index: int) -> float:
        if self.config.position_sizing_type == "equal":
            return self.config.base_lot_size
        elif self.config.position_sizing_type == "increasing":
            return self.config.base_lot_size * (1 + 0.1 * level_index)
        elif self.config.position_sizing_type == "decreasing":
            return self.config.base_lot_size * (1 + 0.1 * (self.config.num_levels - level_index))
        return self.config.base_lot_size

    def check_level_triggered(self, bar: Bar) -> tuple[GridLevel, float] | None:
        for level in self.levels:
            if level.order_filled:
                continue

            if level.is_buy:
                if bar.low <= level.price:
                    level.order_filled = True
                    level.filled_price = level.price
                    level.filled_time = bar.time
                    self.filled_count += 1
                    return (level, level.price)
            else:
                if bar.high >= level.price:
                    level.order_filled = True
                    level.filled_price = level.price
                    level.filled_time = bar.time
                    self.filled_count += 1
                    return (level, level.price)

        return None

    def get_tp_for_level(self, level: GridLevel, entry_price: float) -> float:
        tp_distance = abs(entry_price - self.base_price) * level.tp_multiplier
        if level.is_buy:
            return entry_price + tp_distance
        return entry_price - tp_distance

    def get_sl_for_level(self, level: GridLevel, entry_price: float) -> float:
        sl_distance = abs(entry_price - self.base_price) * 0.25
        if level.is_buy:
            return entry_price - sl_distance
        return entry_price + sl_distance

    def is_expired(self) -> bool:
        if self.config.grid_expiry_bars <= 0:
            return False
        return self.bar_count - self.grid_start_bar >= self.config.grid_expiry_bars

    def is_max_positions_reached(self) -> bool:
        return self.filled_count >= self.config.max_concurrent_positions

    def reset(self) -> None:
        self.levels = []
        self.grid_active = False
        self.grid_start_bar = 0
        self.filled_count = 0
        self.base_price = 0.0
        self.bar_count = 0


class GridStrategy(ISignalStrategy):
    def __init__(
        self,
        grid_spacing_pips: float = 20.0,
        num_levels: int = 10,
        atr_multiplier: float = 0.5,
        position_sizing_type: str = "equal",
        base_lot_size: float = 0.01,
        max_concurrent_positions: int = 5,
        grid_expiry_bars: int = 0,
        direction: GridDirection = GridDirection.BOTH,
        atr_period: int = 14,
        atr_ma_period: int = 50,
        use_midnight_grid: bool = False,
        pair: str = "EURUSD",
    ):
        self.config = GridConfig(
            grid_spacing_pips=grid_spacing_pips,
            num_levels=num_levels,
            atr_multiplier=atr_multiplier,
            position_sizing_type=position_sizing_type,
            base_lot_size=base_lot_size,
            max_concurrent_positions=max_concurrent_positions,
            grid_expiry_bars=grid_expiry_bars,
            direction=direction,
            atr_period=atr_period,
            atr_ma_period=atr_ma_period,
            use_midnight_grid=use_midnight_grid,
            pair=pair,
        )
        self.state = GridState(self.config)
        self._bar_index = 0

    @property
    def name(self) -> str:
        return f"Grid ({self.config.pair})"

    def evaluate(self, state: MarketState) -> StrategySignal | None:
        if len(state.bars) < self.config.atr_period + 1:
            return None

        self._bar_index = len(state.bars) - 1
        latest = state.latest_bar

        if not self.state.grid_active:
            if self.config.initial_spacing_type != "fixed":
                atr = state.atr if state.atr > 0 else self._calculate_atr(state.bars)
                self.config.grid_spacing_pips = atr / self._get_pip_size(self.config.pair) * self.config.atr_multiplier
                self.config.grid_spacing_pips = max(5.0, min(self.config.grid_spacing_pips, 100.0))

            self.state.initialize_grid(latest.close)
            self.state.grid_start_bar = self._bar_index
            self.state.bar_count = self._bar_index
            return None

        self.state.bar_count = self._bar_index

        if self.state.is_expired() or self.state.is_max_positions_reached():
            self.state.reset()
            return None

        triggered = self.state.check_level_triggered(latest)
        if triggered is None:
            return None

        level, entry_price = triggered
        direction = TradeDirection.LONG if level.is_buy else TradeDirection.SHORT
        sl = self.state.get_sl_for_level(level, entry_price)
        risk = abs(entry_price - sl)
        tp1 = entry_price + risk * 1.0 if direction == TradeDirection.LONG else entry_price - risk * 1.0
        tp2 = entry_price + risk * 2.0 if direction == TradeDirection.LONG else entry_price - risk * 2.0
        tp3 = entry_price + risk * 3.0 if direction == TradeDirection.LONG else entry_price - risk * 3.0

        if self.config.direction == GridDirection.LONG and not level.is_buy:
            return None
        if self.config.direction == GridDirection.SHORT and level.is_buy:
            return None

        confidence = min(
            0.85,
            0.60 + (1.0 - self.state.filled_count / self.config.max_concurrent_positions) * 0.25,
        )
        rationale = (
            f"Grid {'long' if level.is_buy else 'short'} triggered: level={level.level_index}, "
            f"entry={entry_price:.5f}, tp1={tp1:.5f}, sl={sl:.5f}"
        )

        return StrategySignal(
            direction=direction,
            confidence=confidence,
            entry_price=entry_price,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            rationale=rationale,
        )

    def _calculate_atr(self, bars: list[Bar]) -> float:
        if len(bars) < self.config.atr_period + 1:
            return 0.0001
        tr_sum = 0.0
        for i in range(len(bars) - self.config.atr_period, len(bars)):
            if i > 0:
                tr = max(
                    bars[i].high - bars[i].low,
                    max(
                        abs(bars[i].high - bars[i - 1].close),
                        abs(bars[i].low - bars[i - 1].close),
                    ),
                )
                tr_sum += tr
        return tr_sum / self.config.atr_period

    def _get_pip_size(self, pair: str) -> float:
        try:
            return pip_value_for_symbol(pair)
        except (ValueError, TypeError):
            return DEFAULT_PIP

    def reset_grid(self) -> None:
        self.state.reset()

    def get_grid_status(self) -> dict:
        return {
            "active": self.state.grid_active,
            "filled_count": self.state.filled_count,
            "max_positions": self.config.max_concurrent_positions,
            "base_price": self.state.base_price,
            "levels": [
                {
                    "index": lvl.level_index,
                    "price": lvl.price,
                    "filled": lvl.order_filled,
                    "filled_price": lvl.filled_price,
                }
                for lvl in self.state.levels
            ],
        }


def create_grid_strategy_from_preset(pair: str, **overrides) -> GridStrategy:
    preset = GRID_PRESETS.get(pair, GRID_PRESETS["EURUSD"])
    preset_copy = {k: v for k, v in preset.items() if k not in overrides}
    return GridStrategy(pair=pair, **preset_copy, **overrides)
