"""Selective Pairing Test Harness with Walk-Forward Validation.

Runs ICT/SMC components individually and in C(n,2) pairs with proper
walk-forward validation, session filtering, and configurable risk parameters.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

from .engine import (
    DEFAULT_SPREAD_PIPS,
    BacktestMetrics,
    Bar,
    ExitReason,
    SimulatedTrade,
    TradeDirection,
    TradeOutcome,
    determine_session,
    get_spread_for_pair,
)
from .ict_smc.confluence_engine import SignalConfluenceEngine
from .ict_smc.models import (
    ConfluenceSignal,
    ICTMarketState,
)
from .trade_management.session_filter import SessionFilter

COMPONENT_NAMES = [
    "structure",
    "order_block",
    "fvg",
    "liquidity_sweep",
    "premium_discount",
    "h4_context",
]


@dataclass
class PairingConfig:
    risk_per_trade_pct: float = 0.005
    max_open_trades: int = 3
    sl_atr_multiplier: float = 1.5
    atr_period: int = 14
    tp1_rr: float = 1.0
    tp2_rr: float = 2.0
    tp3_rr: float = 3.0
    min_confidence: float = 0.55
    min_confluence: int = 0
    spread_pips: float = 0.0
    commission_per_lot: float = 3.5
    leverage: int = 100
    max_total_drawdown_pct: float = 0.05
    max_daily_drawdown_pct: float = 0.02
    min_bars_before_signal: int = 30
    starting_balance: float = 10000.0
    round_trip_spread: bool = True
    slippage_pips: float = 0.2
    swap_per_lot_per_day: float = -2.0
    pair: str = ""
    allow_entry_sessions: list[str] = field(default_factory=lambda: ["london", "ny_am", "ny_pm"])

    @property
    def effective_spread_pips(self) -> float:
        if self.spread_pips > 0:
            return self.spread_pips
        if self.pair:
            return get_spread_for_pair(self.pair)
        return DEFAULT_SPREAD_PIPS


@dataclass
class ComponentResult:
    component: str
    total_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    total_pnl: float = 0.0
    expectancy: float = 0.0
    avg_risk_reward: float = 0.0

    @classmethod
    def from_metrics(cls, component: str, m: BacktestMetrics) -> ComponentResult:
        return cls(
            component=component,
            total_trades=m.total_trades,
            win_rate=m.win_rate,
            profit_factor=m.profit_factor,
            sharpe_ratio=m.sharpe_ratio,
            max_drawdown_pct=m.max_drawdown_pct,
            total_pnl=m.total_pnl,
            expectancy=m.expectancy,
            avg_risk_reward=m.avg_risk_reward,
        )


@dataclass
class WindowMetrics:
    window_id: int
    train_start: str = ""
    train_end: str = ""
    test_start: str = ""
    test_end: str = ""
    train_bars: int = 0
    test_bars: int = 0
    components: dict[str, ComponentResult] = field(default_factory=dict)
    pairs: dict[str, ComponentResult] = field(default_factory=dict)


@dataclass
class PairingReport:
    config: dict = field(default_factory=dict)
    windows: list[dict] = field(default_factory=list)
    aggregate_components: dict[str, dict] = field(default_factory=dict)
    aggregate_pairs: dict[str, dict] = field(default_factory=dict)

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, default=str))


def _calculate_atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        return 0.0001
    tr_sum = 0.0
    for i in range(len(bars) - period, len(bars)):
        if i > 0:
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low - bars[i - 1].close),
            )
            tr_sum += tr
    return tr_sum / period


class SelectivePairingHarness:
    def __init__(self, config: PairingConfig | None = None):
        self.config = config or PairingConfig()
        self._session_filter = SessionFilter(
            enabled=True,
            allow_entry_sessions=self.config.allow_entry_sessions,
        )

    # ------------------------------------------------------------------
    # Component-level signal generation
    # ------------------------------------------------------------------

    def _build_confluence_engine(self, active_components: set[str]) -> SignalConfluenceEngine:
        weights = {
            "structure": 0.30 if "structure" in active_components else 0.0,
            "order_block": 0.25 if "order_block" in active_components else 0.0,
            "fvg": 0.15 if "fvg" in active_components else 0.0,
            "liquidity_sweep": 0.15 if "liquidity_sweep" in active_components else 0.0,
            "premium_discount": 0.10 if "premium_discount" in active_components else 0.0,
            "session": 0.05,
        }

        return SignalConfluenceEngine(
            min_confidence=self.config.min_confidence,
            structure_weight=weights["structure"],
            ob_weight=weights["order_block"],
            fvg_weight=weights["fvg"],
            sweep_weight=weights["liquidity_sweep"],
            pd_weight=weights["premium_discount"],
            session_weight=weights["session"],
            h4_weight=0.15 if "h4_context" in active_components else 0.0,
            default_sl_multiplier=self.config.sl_atr_multiplier,
            tp1_rr=self.config.tp1_rr,
            tp2_rr=self.config.tp2_rr,
            tp3_rr=self.config.tp3_rr,
        )

    def _generate_signals(
        self,
        bars: list[Bar],
        active_components: set[str],
        h4_bars: list[Bar] | None = None,
    ) -> list[ConfluenceSignal]:
        engine = self._build_confluence_engine(active_components)
        signals: list[ConfluenceSignal] = []
        min_bars = self.config.min_bars_before_signal

        for i in range(min_bars, len(bars)):
            bar = bars[i]
            session = determine_session(bar.time)

            entry_check = self._session_filter.check_entry(bar)
            if not entry_check.allow_entry:
                continue

            state = ICTMarketState(bars=bars[: i + 1])
            state.current_session = session

            h4_slice = None
            if h4_bars is not None and "h4_context" in active_components:
                h4_slice = [b for b in h4_bars if b.time <= bar.time]
                if not h4_slice:
                    h4_slice = None

            signal = engine.evaluate(state, h4_slice)
            if signal is not None:
                if self.config.min_confluence > 0 and signal.confluence_count < self.config.min_confluence:
                    continue
                signals.append(signal)

        return signals

    # ------------------------------------------------------------------
    # Backtest execution
    # ------------------------------------------------------------------

    def _run_backtest(self, bars: list[Bar], signals: list[ConfluenceSignal]) -> BacktestMetrics:
        cfg = self.config
        balance = cfg.starting_balance
        peak_balance = balance
        max_drawdown = 0.0
        max_daily_loss = 0.0
        current_day = None
        daily_start_balance = balance
        trades: list[SimulatedTrade] = []
        equity_curve: list[float] = [balance]
        open_trades: list[SimulatedTrade] = []
        cost_tracker = {"spread": 0.0, "commission": 0.0}

        signal_map: dict[datetime, list[ConfluenceSignal]] = {}
        for sig in signals:
            signal_map.setdefault(sig.signal_time, []).append(sig)

        for i in range(len(bars)):
            bar = bars[i]

            day = bar.time.date()
            if current_day is None:
                current_day = day
                daily_start_balance = balance
            elif day != current_day:
                daily_loss = daily_start_balance - balance
                if daily_loss > max_daily_loss:
                    max_daily_loss = daily_loss
                current_day = day
                daily_start_balance = balance

            if peak_balance > 0:
                drawdown_pct = (peak_balance - balance) / peak_balance
            else:
                drawdown_pct = 0.0
            if drawdown_pct >= cfg.max_total_drawdown_pct:
                break

            if daily_start_balance > 0:
                daily_loss_pct = (daily_start_balance - balance) / daily_start_balance
            else:
                daily_loss_pct = 0.0
            if daily_loss_pct >= cfg.max_daily_drawdown_pct:
                equity_curve.append(balance)
                continue

            to_close: list[SimulatedTrade] = []
            for trade in open_trades:
                hit, exit_price, reason = _check_trade_exit(trade, bar)
                if hit:
                    _close_trade(trade, i, bar.time, exit_price, reason, cfg, cost_tracker)
                    balance += trade.profit_loss
                    if balance > peak_balance:
                        peak_balance = balance
                    dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
                    if dd > max_drawdown:
                        max_drawdown = dd
                    trades.append(trade)
                    to_close.append(trade)
                    equity_curve.append(balance)
            for t in to_close:
                open_trades.remove(t)

            if len(open_trades) < cfg.max_open_trades:
                bar_signals = signal_map.get(bar.time, [])
                for sig in bar_signals:
                    if len(open_trades) >= cfg.max_open_trades:
                        break
                    risk_amount = balance * cfg.risk_per_trade_pct
                    risk = abs(sig.entry_price - sig.stop_loss)
                    if risk == 0:
                        continue

                    pip_value = _get_pip_value(sig.entry_price)
                    spread_cost = cfg.effective_spread_pips * pip_value
                    if sig.direction == TradeDirection.LONG:
                        effective_entry = sig.entry_price + spread_cost
                    else:
                        effective_entry = sig.entry_price - spread_cost
                    adjusted_risk = abs(effective_entry - sig.stop_loss)
                    if adjusted_risk == 0:
                        continue

                    lot_size = risk_amount / adjusted_risk
                    margin = lot_size * effective_entry / cfg.leverage
                    if margin > balance:
                        continue

                    trade = SimulatedTrade(
                        entry_bar_index=i,
                        exit_bar_index=-1,
                        direction=sig.direction,
                        entry_price=effective_entry,
                        stop_loss=sig.stop_loss,
                        take_profit_1=sig.take_profit_1,
                        take_profit_2=sig.take_profit_2,
                        take_profit_3=sig.take_profit_3,
                        exit_price=0.0,
                        lot_size=lot_size,
                        risk_amount=risk_amount,
                        pips=0.0,
                        profit_loss=0.0,
                        outcome=TradeOutcome.OPEN,
                        exit_reason=ExitReason.STOP_LOSS,
                        entry_time=bar.time,
                        exit_time=bar.time,
                        confidence_score=sig.confidence_score,
                        confluence_count=sig.confluence_count,
                        rationale=sig.rationale,
                    )
                    open_trades.append(trade)

            equity_curve.append(balance)

        for trade in open_trades:
            last_price = trades[-1].exit_price if trades else trade.entry_price
            _close_trade(
                trade,
                len(bars) - 1,
                bars[-1].time,
                last_price,
                ExitReason.END_OF_DATA,
                cfg,
                cost_tracker,
            )
            balance += trade.profit_loss
            if balance > peak_balance:
                peak_balance = balance
            trades.append(trade)

        return _calculate_metrics(
            trades,
            equity_curve,
            0,
            cfg.starting_balance,
            max_drawdown,
            max_daily_loss,
            peak_balance,
            cost_tracker,
        )

    # ------------------------------------------------------------------
    # Individual component testing
    # ------------------------------------------------------------------

    def run_individual(
        self,
        bars: list[Bar],
        h4_bars: list[Bar] | None = None,
    ) -> dict[str, ComponentResult]:
        results: dict[str, ComponentResult] = {}
        for comp in COMPONENT_NAMES:
            signals = self._generate_signals(bars, {comp}, h4_bars)
            metrics = self._run_backtest(bars, signals)
            results[comp] = ComponentResult.from_metrics(comp, metrics)
        return results

    # ------------------------------------------------------------------
    # Pair testing
    # ------------------------------------------------------------------

    def run_pairs(
        self,
        bars: list[Bar],
        h4_bars: list[Bar] | None = None,
    ) -> dict[str, ComponentResult]:
        results: dict[str, ComponentResult] = {}
        for comp_a, comp_b in combinations(COMPONENT_NAMES, 2):
            pair_key = f"{comp_a}+{comp_b}"
            signals = self._generate_signals(bars, {comp_a, comp_b}, h4_bars)
            metrics = self._run_backtest(bars, signals)
            results[pair_key] = ComponentResult.from_metrics(pair_key, metrics)
        return results

    # ------------------------------------------------------------------
    # Walk-forward validation
    # ------------------------------------------------------------------

    def run_walk_forward(
        self,
        bars: list[Bar],
        n_windows: int = 3,
        train_ratio: float = 0.6,
        test_ratio: float = 0.2,
        h4_bars: list[Bar] | None = None,
    ) -> list[WindowMetrics]:
        total = len(bars)
        window_size = total // n_windows
        windows: list[WindowMetrics] = []

        for w in range(n_windows):
            start = w * window_size
            end = (w + 1) * window_size if w < n_windows - 1 else total
            window_bars = bars[start:end]
            wlen = len(window_bars)

            wm = WindowMetrics(window_id=w)

            if wlen < 100:
                windows.append(wm)
                continue

            train_end_idx = int(wlen * train_ratio)
            gap_end_idx = int(wlen * (train_ratio + test_ratio))
            train_bars = window_bars[:train_end_idx]
            test_bars = window_bars[gap_end_idx:]

            wm.train_start = str(train_bars[0].time)
            wm.train_end = str(train_bars[-1].time)
            wm.test_start = str(test_bars[0].time) if test_bars else ""
            wm.test_end = str(test_bars[-1].time) if test_bars else ""
            wm.train_bars = len(train_bars)
            wm.test_bars = len(test_bars)

            for comp in COMPONENT_NAMES:
                signals = self._generate_signals(train_bars, {comp}, h4_bars)
                metrics = self._run_backtest(train_bars, signals)
                wm.components[comp] = ComponentResult.from_metrics(comp, metrics)

            if test_bars:
                for comp in COMPONENT_NAMES:
                    signals = self._generate_signals(test_bars, {comp}, h4_bars)
                    metrics = self._run_backtest(test_bars, signals)
                    test_key = f"{comp}_test"
                    wm.components[test_key] = ComponentResult.from_metrics(test_key, metrics)

            for comp_a, comp_b in combinations(COMPONENT_NAMES, 2):
                pair_key = f"{comp_a}+{comp_b}"
                signals = self._generate_signals(train_bars, {comp_a, comp_b}, h4_bars)
                metrics = self._run_backtest(train_bars, signals)
                wm.pairs[pair_key] = ComponentResult.from_metrics(pair_key, metrics)

            if test_bars:
                for comp_a, comp_b in combinations(COMPONENT_NAMES, 2):
                    pair_key = f"{comp_a}+{comp_b}_test"
                    signals = self._generate_signals(test_bars, {comp_a, comp_b}, h4_bars)
                    metrics = self._run_backtest(test_bars, signals)
                    wm.pairs[pair_key] = ComponentResult.from_metrics(pair_key, metrics)

            windows.append(wm)

        return windows

    # ------------------------------------------------------------------
    # Full report
    # ------------------------------------------------------------------

    def run_full_report(
        self,
        bars: list[Bar],
        n_windows: int = 3,
        train_ratio: float = 0.6,
        test_ratio: float = 0.2,
        h4_bars: list[Bar] | None = None,
        output_path: str | None = None,
    ) -> PairingReport:
        individual = self.run_individual(bars, h4_bars)
        pairs = self.run_pairs(bars, h4_bars)
        windows = self.run_walk_forward(bars, n_windows, train_ratio, test_ratio, h4_bars)

        agg_components: dict[str, dict] = {}
        for comp, cr in individual.items():
            agg_components[comp] = asdict(cr)

        agg_pairs: dict[str, dict] = {}
        for pair_key, cr in pairs.items():
            agg_pairs[pair_key] = asdict(cr)

        window_dicts = []
        for wm in windows:
            wd = {
                "window_id": wm.window_id,
                "train_start": wm.train_start,
                "train_end": wm.train_end,
                "test_start": wm.test_start,
                "test_end": wm.test_end,
                "train_bars": wm.train_bars,
                "test_bars": wm.test_bars,
                "components": {k: asdict(v) for k, v in wm.components.items()},
                "pairs": {k: asdict(v) for k, v in wm.pairs.items()},
            }
            window_dicts.append(wd)

        report = PairingReport(
            config=asdict(self.config),
            windows=window_dicts,
            aggregate_components=agg_components,
            aggregate_pairs=agg_pairs,
        )

        if output_path:
            report.to_json(output_path)

        return report


# ------------------------------------------------------------------
# Pure-function helpers (no state, easy to test)
# ------------------------------------------------------------------


def _check_trade_exit(trade: SimulatedTrade, bar: Bar) -> tuple[bool, float, ExitReason]:
    if trade.direction == TradeDirection.LONG:
        if bar.low <= trade.stop_loss:
            return True, trade.stop_loss, ExitReason.STOP_LOSS
        if bar.high >= trade.take_profit_3:
            return True, trade.take_profit_3, ExitReason.TAKE_PROFIT_3
        if bar.high >= trade.take_profit_2:
            return True, trade.take_profit_2, ExitReason.TAKE_PROFIT_2
        if bar.high >= trade.take_profit_1:
            return True, trade.take_profit_1, ExitReason.TAKE_PROFIT_1
    else:
        if bar.high >= trade.stop_loss:
            return True, trade.stop_loss, ExitReason.STOP_LOSS
        if bar.low <= trade.take_profit_3:
            return True, trade.take_profit_3, ExitReason.TAKE_PROFIT_3
        if bar.low <= trade.take_profit_2:
            return True, trade.take_profit_2, ExitReason.TAKE_PROFIT_2
        if bar.low <= trade.take_profit_1:
            return True, trade.take_profit_1, ExitReason.TAKE_PROFIT_1
    return False, 0.0, ExitReason.STOP_LOSS


def _close_trade(
    trade: SimulatedTrade,
    bar_index: int,
    exit_time: datetime,
    exit_price: float,
    reason: ExitReason,
    config: PairingConfig,
    cost_tracker: dict | None = None,
) -> None:
    trade.exit_bar_index = bar_index
    trade.exit_time = exit_time
    trade.exit_reason = reason

    pip_value = _get_pip_value(trade.entry_price)
    standard_lots = trade.lot_size / 100000.0
    spread_pips = config.effective_spread_pips
    commission = standard_lots * config.commission_per_lot

    if cost_tracker is not None:
        cost_tracker["commission"] += commission

    if config.round_trip_spread:
        spread_price = spread_pips * pip_value
        if trade.direction == TradeDirection.LONG:
            exit_price -= spread_price
        else:
            exit_price += spread_price
        spread_dollars = spread_pips * pip_value * trade.lot_size
        if cost_tracker is not None:
            cost_tracker["spread"] += spread_dollars

    slippage_price = config.slippage_pips * pip_value
    if trade.direction == TradeDirection.LONG:
        exit_price -= slippage_price
    else:
        exit_price += slippage_price

    trade.exit_price = exit_price

    holding_days = (exit_time.date() - trade.entry_time.date()).days
    if holding_days > 0 and config.swap_per_lot_per_day != 0.0:
        swap_cost = standard_lots * config.swap_per_lot_per_day * holding_days
    else:
        swap_cost = 0.0

    if trade.direction == TradeDirection.LONG:
        trade.pips = (exit_price - trade.entry_price) / pip_value
    else:
        trade.pips = (trade.entry_price - exit_price) / pip_value

    trade.profit_loss = trade.pips * standard_lots * pip_value * 100000.0 - commission + swap_cost
    trade.outcome = (
        TradeOutcome.WIN
        if trade.profit_loss > 0.01
        else (TradeOutcome.LOSS if trade.profit_loss < -0.01 else TradeOutcome.BREAKEVEN)
    )


def _get_pip_value(price: float) -> float:
    if price >= 50:
        return 0.01
    if price >= 1:
        return 0.0001
    return 0.00000001


def _calculate_metrics(
    trades: list[SimulatedTrade],
    equity_curve: list[float],
    rejected: int,
    starting_balance: float,
    max_drawdown: float,
    max_daily_loss: float,
    peak_balance: float,
    cost_tracker: dict | None = None,
) -> BacktestMetrics:
    import math

    ending = equity_curve[-1] if equity_curve else starting_balance
    wins = [t for t in trades if t.outcome == TradeOutcome.WIN]
    losses = [t for t in trades if t.outcome == TradeOutcome.LOSS]
    total_trades = len(trades)

    win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0.0
    avg_win = sum(t.profit_loss for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.profit_loss for t in losses) / len(losses) if losses else 0.0
    largest_win = max((t.profit_loss for t in wins), default=0.0)
    largest_loss = min((t.profit_loss for t in losses), default=0.0)

    total_wins = sum(t.profit_loss for t in wins)
    total_losses = abs(sum(t.profit_loss for t in losses))
    if total_losses > 0:
        profit_factor = total_wins / total_losses
    elif total_wins > 0:
        profit_factor = total_wins
    else:
        profit_factor = 0.0

    avg_rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0
    expectancy = (win_rate / 100 * avg_win) - ((1 - win_rate / 100) * abs(avg_loss))
    avg_hold = sum(t.exit_bar_index - t.entry_bar_index for t in trades) / total_trades if total_trades > 0 else 0.0

    sharpe = 0.0
    if len(equity_curve) >= 2:
        returns = []
        for j in range(1, len(equity_curve)):
            if equity_curve[j - 1] != 0:
                returns.append((equity_curve[j] - equity_curve[j - 1]) / equity_curve[j - 1])
        if returns:
            mean_r = sum(returns) / len(returns)
            std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / len(returns))
            if std_r == 0:
                sharpe = 999.0 if mean_r > 0 else 0.0
            else:
                sharpe = (mean_r / std_r) * math.sqrt(252)

    return BacktestMetrics(
        starting_balance=starting_balance,
        ending_balance=ending,
        total_pnl=ending - starting_balance,
        total_pnl_pct=(ending - starting_balance) / starting_balance if starting_balance else 0.0,
        win_rate=win_rate,
        total_trades=total_trades,
        winning_trades=len(wins),
        losing_trades=len(losses),
        breakeven_trades=sum(1 for t in trades if t.outcome == TradeOutcome.BREAKEVEN),
        avg_win=avg_win,
        avg_loss=avg_loss,
        largest_win=largest_win,
        largest_loss=largest_loss,
        profit_factor=profit_factor,
        max_drawdown_pct=max_drawdown * 100,
        max_drawdown_dollar=max_drawdown * peak_balance,
        max_daily_loss_dollar=max_daily_loss,
        sharpe_ratio=sharpe,
        avg_risk_reward=avg_rr,
        expectancy=expectancy,
        avg_holding_bars=avg_hold,
        equity_curve=equity_curve,
        trades=trades,
        total_spread_cost=cost_tracker["spread"] if cost_tracker else 0.0,
        total_commission_cost=cost_tracker["commission"] if cost_tracker else 0.0,
        rejected_signals=rejected,
    )
