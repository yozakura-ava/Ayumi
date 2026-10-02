#!/usr/bin/env python3
"""
Live Trading Execution — Session Range Mean Reversion on GBPUSD
================================================================

Wires the GBPUSD Session Range MR strategy (passed walk-forward QA: 5/5 GO)
into cTrader live execution via QuantPipeline.

Usage:
    python scripts/live_trading_execution.py [--symbol SYMBOL] [--paper-mode]

Requirements:
    - .env with CTRADER_* credentials (demo account for paper/live)
    - Market data feed connectivity (AYUAA-409 verified)
    - FIX order execution (AYUAA-492 verified)
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "forex_bot"))

from adapters.ctrader.api_client import cTraderAPIClient
from adapters.ctrader.market_data_feed import LiveMarketDataFeed, SymbolInfo, Tick
from adapters.ctrader.models import (
    TradeDirection,
    TradeSignal,
    cTraderCredentials,
)
from adapters.ctrader.order_manager import OrderManager, PositionSizeConfig
from adapters.ctrader.paper_trader import PaperTrader
from adapters.ctrader.risk_guard import FTMOConfig
from backtest.engine import Bar, MarketState
from quant.config import QuantConfig
from quant.pipeline import QuantPipeline
from strategies.session_range_mean_reversion import (
    SessionRangeMeanReversionStrategy,
    SessionRangeMRConfig,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    symbol: str = "GBPUSD"
    paper_mode: bool = True
    trade_host: str = "localhost"
    trade_port: int = 5202
    quote_host: str = "localhost"
    quote_port: int = 5211
    starting_balance: float = 100000.0
    ftmo_daily_loss_limit: float = 0.05
    ftmo_max_drawdown: float = 0.10
    risk_per_trade: float = 0.02
    min_confidence: float = 0.55


class LiveTradingExecutor:
    def __init__(self, config: ExecutionConfig):
        self._config = config
        self._running = False
        self._lock = threading.RLock()

        self._bars: Dict[str, List[Bar]] = {}
        self._last_signal_time: Optional[datetime] = None

        self._trade_client: Optional[cTraderAPIClient] = None
        self._market_feed: Optional[LiveMarketDataFeed] = None
        self._paper_trader: Optional[PaperTrader] = None
        self._order_manager: Optional[OrderManager] = None
        self._quant_pipeline: Optional[QuantPipeline] = None
        self._strategy: Optional[SessionRangeMeanReversionStrategy] = None

        signal.signal(signal.SIGINT, self._on_shutdown)
        signal.signal(signal.SIGTERM, self._on_shutdown)

    def _load_credentials(self) -> cTraderCredentials:
        from dotenv import load_dotenv

        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            load_dotenv(env_path)

        return cTraderCredentials(
            host=self._config.trade_host,
            port=self._config.trade_port,
            use_ssl=False,
            sender_comp_id=os.environ["CTRADER_SENDER_COMP_ID"],
            target_comp_id=os.environ.get("CTRADER_TARGET_COMP_ID", "cServer"),
            sender_sub_id=os.environ.get("CTRADER_SENDER_SUB_ID", "TRADE"),
            username=os.environ["CTRADER_ACCOUNT"],
            password=os.environ["CTRADER_PASSWORD"],
        )

    def _create_strategy_config(self) -> SessionRangeMRConfig:
        return SessionRangeMRConfig(
            atr_period=14,
            atr_sl_multiplier=1.5,
            atr_tp_multiplier=2.0,
            rsi_period=14,
            rsi_long_level=30.0,
            rsi_short_level=70.0,
            session_range_min_pips=25.0,
            entry_near_extreme_pips=15.0,
            hard_cap_sl_pips=30.0,
            tp1_rr=1.0,
            tp2_rr=1.5,
            ema_trend_period=50,
            use_session_range_sl=True,
            session_range_sl_fraction=0.6,
        )

    def _setup_trading(self):
        ftmo_config = FTMOConfig(
            daily_loss_limit_pct=self._config.ftmo_daily_loss_limit,
            total_drawdown_limit_pct=self._config.ftmo_max_drawdown,
        )
        position_config = PositionSizeConfig(
            risk_per_trade_pct=self._config.risk_per_trade,
        )

        creds = self._load_credentials()

        if self._config.paper_mode:
            self._order_manager = OrderManager(position_config)
            self._paper_trader = PaperTrader(
                ftmo_config=ftmo_config,
                position_config=position_config,
                starting_balance=self._config.starting_balance,
            )
            logger.info("Running in PAPER mode (no real orders)")
        else:
            self._trade_client = cTraderAPIClient(creds)
            self._order_manager = OrderManager(
                position_config,
                api_client=self._trade_client,
            )
            self._paper_trader = PaperTrader(
                ftmo_config=ftmo_config,
                position_config=position_config,
                starting_balance=self._config.starting_balance,
                api_client=self._trade_client,
            )

            logon_event = threading.Event()
            self._trade_client.register_callback("on_logon", lambda m: logon_event.set())

            if not self._trade_client.connect():
                raise RuntimeError("Failed to connect to cTrader trade port")
            if not logon_event.wait(timeout=15):
                raise RuntimeError("Trade port logon timeout")

            logger.info("Connected to cTrader trade port (LIVE mode)")

        self._quant_pipeline = QuantPipeline(QuantConfig.ftmo())
        self._strategy = SessionRangeMeanReversionStrategy(self._create_strategy_config())

        self._paper_trader.register_callback(
            "on_trade_executed",
            lambda r: logger.info(
                f"Trade executed: {r.signal.direction.value} {r.signal.symbol} @ {r.signal.entry_price}"
            ),
        )
        self._paper_trader.register_callback(
            "on_position_closed",
            lambda p: logger.info(f"Position closed: {p.symbol} PnL={p.closed_pnl:.2f}"),
        )

        logger.info(f"Trading setup complete: {self._config.symbol} Session Range MR")

    def _setup_market_feed(self):
        creds = self._load_credentials()
        quote_sender_sub_id = os.environ.get("CTRADER_QUOTE_SENDER_SUB_ID", "QUOTE")
        quote_target_sub_id = os.environ.get("CTRADER_QUOTE_TARGET_SUB_ID", quote_sender_sub_id)
        quote_creds = cTraderCredentials(
            host=self._config.quote_host,
            port=self._config.quote_port,
            use_ssl=creds.use_ssl,
            sender_comp_id=creds.sender_comp_id,
            target_comp_id=creds.target_comp_id,
            sender_sub_id=quote_sender_sub_id,
            target_sub_id=quote_target_sub_id,
            username=creds.username,
            password=creds.password,
        )

        self._market_feed = LiveMarketDataFeed(quote_creds)

        self._market_feed.on_tick(self._on_tick)

        if not self._market_feed.start(auto_subscribe=[self._config.symbol]):
            raise RuntimeError("Failed to start market data feed")

        logger.info(f"Market data feed started for {self._config.symbol}")

    def _on_tick(self, tick: Tick):
        with self._lock:
            symbol_name = self._market_feed.symbols.get(
                tick.symbol_id, SymbolInfo(symbol_id=tick.symbol_id, name="UNKNOWN")
            ).name
            if symbol_name != self._config.symbol:
                return

            bar = Bar(
                time=tick.timestamp,
                open=tick.bid,
                high=tick.ask,
                low=tick.bid,
                close=tick.bid,
                volume=0,
            )

            if symbol_name not in self._bars:
                self._bars[symbol_name] = []
            self._bars[symbol_name].append(bar)

            if len(self._bars[symbol_name]) > 500:
                self._bars[symbol_name] = self._bars[symbol_name][-500:]

            self._evaluate_strategy(symbol_name)

    def _evaluate_strategy(self, symbol: str):
        if self._strategy is None or self._paper_trader is None:
            return

        bars = self._bars.get(symbol, [])
        if len(bars) < 50:
            return

        state = MarketState(bars=bars)

        signal = self._strategy.evaluate(state)
        if signal is None:
            return

        if signal.confidence < self._config.min_confidence:
            logger.debug(f"Signal confidence {signal.confidence:.2f} below minimum {self._config.min_confidence}")
            return

        atr = self._calculate_atr(bars)
        if self._quant_pipeline is not None:
            self._quant_pipeline.update_bars(bars[-1].high, bars[-1].low, bars[-1].close, atr)

            quant_decision = self._quant_pipeline.pre_trade_check(
                signal_symbol=symbol,
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                bar_time=bars[-1].time,
            )

            from quant.pipeline import TradeAction

            if quant_decision.action == TradeAction.REJECT:
                logger.info(f"Signal rejected by QuantPipeline: {quant_decision.reject_reason}")
                return

            lot_size = quant_decision.lot_size if quant_decision.lot_size else 0.1
        else:
            lot_size = 0.1

        direction = TradeDirection.LONG if signal.direction.value == "long" else TradeDirection.SHORT

        trade_signal = TradeSignal(
            symbol=symbol,
            direction=direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit_1=signal.take_profit_1,
            take_profit_2=signal.take_profit_2,
            take_profit_3=signal.take_profit_3,
            volume=lot_size,
            confidence=signal.confidence,
            rationale=signal.rationale,
        )

        result = self._paper_trader.process_signal(trade_signal)
        if result.success:
            self._last_signal_time = datetime.now(timezone.utc)
            logger.info(f"Signal traded: {trade_signal.direction.value} {lot_size} {symbol} @ {signal.entry_price}")
        else:
            logger.warning(f"Signal rejected: {result.rejection_reason}")

    def _calculate_atr(self, bars: List[Bar], period: int = 14) -> float:
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
        return tr_sum / period if period > 0 else 0.0001

    def _on_shutdown(self, signum, frame):
        logger.info("Shutdown signal received")
        self.stop()

    def start(self):
        logger.info("=" * 60)
        logger.info("LIVE TRADING EXECUTION")
        logger.info(f"Symbol: {self._config.symbol}")
        logger.info(f"Mode: {'PAPER' if self._config.paper_mode else 'LIVE'}")
        logger.info("Strategy: Session Range Mean Reversion")
        logger.info("=" * 60)

        self._setup_trading()
        self._setup_market_feed()

        self._running = True
        logger.info("Live trading started. Press Ctrl+C to stop.")

        while self._running:
            time.sleep(1)

            with self._lock:
                stats = self._paper_trader.get_stats() if self._paper_trader else None
                if stats:
                    logger.info(
                        f"Stats: balance={stats.current_balance:.2f}, "
                        f"trades={stats.trades_executed}, "
                        f"rejected={stats.trades_rejected}, "
                        f"blocked={stats.signals_blocked_by_risk}"
                    )

    def stop(self):
        self._running = False

        if self._market_feed:
            self._market_feed.stop()
            logger.info("Market feed stopped")

        if self._trade_client:
            self._trade_client.disconnect()
            logger.info("Trade client disconnected")

        with self._lock:
            if self._paper_trader:
                stats = self._paper_trader.get_stats()
                logger.info("=" * 60)
                logger.info("FINAL STATS")
                logger.info(f"Starting: {stats.starting_balance:.2f}")
                logger.info(f"Current: {stats.current_balance:.2f}")
                logger.info(f"PnL: {stats.current_balance - stats.starting_balance:.2f}")
                logger.info(f"Trades: {stats.trades_executed}")
                logger.info(f"Rejected: {stats.trades_rejected}")
                logger.info(f"Blocked by risk: {stats.signals_blocked_by_risk}")
                logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Live Trading Execution")
    parser.add_argument("--symbol", default="GBPUSD", help="Trading symbol (default: GBPUSD)")
    parser.add_argument("--paper-mode", action="store_true", help="Run in paper mode (default: True)")
    parser.add_argument("--live-mode", action="store_true", help="Run in live mode with real orders")
    parser.add_argument("--trade-host", default="localhost", help="Trade port host")
    parser.add_argument("--trade-port", type=int, default=5202, help="Trade port")
    parser.add_argument("--quote-host", default="localhost", help="Quote port host")
    parser.add_argument("--quote-port", type=int, default=5211, help="Quote port")
    parser.add_argument("--balance", type=float, default=100000.0, help="Starting balance")
    args = parser.parse_args()

    config = ExecutionConfig(
        symbol=args.symbol,
        paper_mode=not args.live_mode,
        trade_host=args.trade_host,
        trade_port=args.trade_port,
        quote_host=args.quote_host,
        quote_port=args.quote_port,
        starting_balance=args.balance,
    )

    executor = LiveTradingExecutor(config)
    try:
        executor.start()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        executor.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
