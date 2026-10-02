"""Strategy Blend Backtest — runs historical signals through the full Ayumi pipeline.

Pipeline: adapter → confidence → router → sizer → equity tracking.

Each signal provides an outcome_pnl to simulate known historical results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from confidence.engine import ConfidenceEngine, ConfidenceResult
from confidence.gates import GateConfig
from orchestrator.strategy_adapter import StrategyAdapter
from risk.profile_router import Profile, ProfileRouter
from risk.sl_position_sizer import SLPositionSizer


@dataclass
class BacktestConfig:
    starting_balance: float = 10000.0
    risk_per_trade_pct: float = 0.005  # 0.5%
    daily_risk_cap_pct: float = 0.03  # 3%
    max_sniper: int = 3
    max_swarm: int = 5
    spread_pips: dict = field(
        default_factory=lambda: {
            "EURUSD": 1.0,
            "GBPUSD": 1.5,
            "USDJPY": 1.2,
            "XAUUSD": 3.0,
        }
    )
    london_open: int = 7
    london_close: int = 16
    ny_open: int = 13
    ny_close: int = 22


@dataclass
class BacktestResult:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    sniper_trades: int
    swarm_trades: int
    rejected_signals: int
    equity_curve: list[float]
    gross_profit: float
    gross_loss: float
    per_strategy: dict[str, dict]


class BlendBacktest:
    """Runs historical signals through the full Ayumi pipeline."""

    def __init__(self, config: BacktestConfig = None):
        self._config = config or BacktestConfig()

    def run(self, signals: list[dict]) -> BacktestResult:
        """Run backtest over historical signals.

        Each signal dict should have:
        - strategy_id, symbol, direction, entry_price, stop_loss, take_profit
        - confidence (raw strategy score)
        - timestamp (ISO string or datetime)
        - Optional: confluence_strategies, confluence_timeframes, spread, atr
        - outcome_pnl: actual P&L of this trade (for known outcomes)

        Note: Positions are simulated as instantaneous (open and close in same
        iteration). Router capacity limits are checked but not stress-tested
        with concurrent open positions. The forward test runner will exercise
        real concurrency.
        """
        cfg = self._config

        # Build pipeline components
        gate_config = GateConfig(
            default_max_spread=2.0,
            symbol_max_spreads={k: v for k, v in cfg.spread_pips.items()},
            london_open=cfg.london_open,
            london_close=cfg.london_close,
            ny_open=cfg.ny_open,
            ny_close=cfg.ny_close,
        )
        engine = ConfidenceEngine(gate_config)
        router = ProfileRouter(
            sniper_threshold=0.70,
            swarm_threshold=0.40,
            max_sniper=cfg.max_sniper,
            max_swarm=cfg.max_swarm,
        )
        sizer = SLPositionSizer(
            account_balance=cfg.starting_balance,
            risk_per_trade_pct=cfg.risk_per_trade_pct,
            daily_risk_cap_pct=cfg.daily_risk_cap_pct,
        )
        adapter = StrategyAdapter()

        # Sort signals by timestamp
        parsed = []
        for s in signals:
            ts = s["timestamp"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            parsed.append((ts, s))
        parsed.sort(key=lambda x: x[0])

        # Tracking state
        balance = cfg.starting_balance
        equity_curve = [balance]
        rejected = 0
        sniper_count = 0
        swarm_count = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        pnls: list[float] = []
        peak_balance = balance
        max_dd = 0.0
        max_dd_pct = 0.0
        per_strategy: dict[str, dict] = {}

        pos_counter = 0
        current_day = None

        for ts, sig_dict in parsed:
            # Reset daily counters on new day
            day_str = ts.strftime("%Y-%m-%d")
            if current_day is not None and day_str != current_day:
                sizer.reset_daily()
            current_day = day_str

            # Build OrchestratorTradeSignal via adapter
            try:
                signal = adapter.adapt_signal(sig_dict["strategy_id"], sig_dict)
            except (ValueError, KeyError):
                rejected += 1
                continue

            # Run through confidence engine
            spread = sig_dict.get("spread", cfg.spread_pips.get(signal.symbol, 1.0))
            hour_utc = ts.hour

            # Build confluences if present
            confluences = None
            if sig_dict.get("confluence_strategies") and sig_dict.get("confluence_timeframes"):
                confluences = []
                for strat, tf in zip(  # noqa: B905
                    sig_dict["confluence_strategies"],
                    sig_dict["confluence_timeframes"],
                ):
                    confluences.append(
                        {
                            "strategy": strat,
                            "direction": signal.direction.lower(),
                            "timeframe": tf,
                        }
                    )

            conf_result: ConfidenceResult = engine.score(
                raw_confidence=signal.confidence,
                symbol=signal.symbol,
                direction=signal.direction.lower(),
                spread=spread,
                atr=sig_dict.get("atr", 0.0),
                hour_utc=hour_utc,
                confluences=confluences,
            )

            if conf_result.blocked:
                rejected += 1
                continue

            # Route to profile
            profile = router.route(conf_result.final_score)
            if profile is None:
                rejected += 1
                continue

            # Size position
            size_result = sizer.calculate(
                symbol=signal.symbol,
                entry_price=signal.entry_price,
                sl_price=signal.stop_loss,
                profile=profile,
            )

            if size_result.blocked:
                rejected += 1
                continue

            # Register position with router
            pos_id = f"pos_{pos_counter}"
            pos_counter += 1
            router.register_open(pos_id, profile)
            sizer.register_open_position(size_result.risk_amount)

            # Apply outcome
            outcome_pnl = sig_dict.get("outcome_pnl", 0.0)
            win = outcome_pnl > 0

            # Track profile counts
            if profile == Profile.SNIPER:
                sniper_count += 1
            else:
                swarm_count += 1

            # Update state
            balance += outcome_pnl
            total_pnl += outcome_pnl
            pnls.append(outcome_pnl)
            if win:
                wins += 1
            else:
                losses += 1

            # Per-strategy tracking
            sid = sig_dict["strategy_id"]
            if sid not in per_strategy:
                per_strategy[sid] = {
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "total_pnl": 0.0,
                    "pnls": [],
                }
            per_strategy[sid]["trades"] += 1
            per_strategy[sid]["pnls"].append(outcome_pnl)
            per_strategy[sid]["total_pnl"] += outcome_pnl
            if win:
                per_strategy[sid]["wins"] += 1
            else:
                per_strategy[sid]["losses"] += 1

            # Close position
            sizer.close_position(outcome_pnl, size_result.risk_amount, win)
            router.close(pos_id)
            sizer.update_balance(balance)

            # Track drawdown
            if balance > peak_balance:
                peak_balance = balance
            dd = peak_balance - balance
            dd_pct = dd / peak_balance if peak_balance > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

            equity_curve.append(balance)

        # Calculate final stats
        total_trades = wins + losses
        win_rate = wins / total_trades if total_trades > 0 else 0.0

        win_pnls = [p for p in pnls if p > 0]
        loss_pnls = [p for p in pnls if p <= 0]
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0

        gross_profit = sum(win_pnls)
        gross_loss = abs(sum(loss_pnls))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0

        # Sharpe ratio (simplified — assumes equal time between trades)
        sharpe = 0.0
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            std = math.sqrt(variance) if variance > 0 else 0.0
            sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0

        # Clean per-strategy (remove raw pnls list from final output)
        clean_per_strategy = {}
        for sid, stats in per_strategy.items():
            t = stats["trades"]
            clean_per_strategy[sid] = {
                "trades": t,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": stats["wins"] / t if t > 0 else 0.0,
                "total_pnl": stats["total_pnl"],
            }

        return BacktestResult(
            total_trades=total_trades,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=win_rate,
            total_pnl=total_pnl,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            profit_factor=profit_factor,
            avg_win=avg_win,
            avg_loss=avg_loss,
            sniper_trades=sniper_count,
            swarm_trades=swarm_count,
            rejected_signals=rejected,
            equity_curve=equity_curve,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            per_strategy=clean_per_strategy,
        )
