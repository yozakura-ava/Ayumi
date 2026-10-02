"""TTC XAUUSD Forward Test Runner — M15 bars, Optuna-validated strategy.

Launches a ForwardTestEngine instance for TTCXAUUSDStrategy on XAUUSD M15
bars via cTrader live market data.

Usage:
  PYTHONPATH=src/forex_bot:src python -m run_ttc_xauusd_forward
  PYTHONPATH=src/forex_bot:src python -m run_ttc_xauusd_forward --paper-only
"""

import logging
import os
import signal as sig
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not _env_path.exists():
        _env_path = Path(__file__).resolve().parents[3] / ".env"
    load_dotenv(_env_path)
except ImportError:
    pass

from adapters.ctrader.forward_test_engine import ForwardTestConfig, ForwardTestEngine
from adapters.ctrader.risk_guard import FTMOConfig
from strategies.ttc_xauusd import TTCXAUUSDStrategy

logger = logging.getLogger(__name__)

LOG_DIR = "logs/ttc_xauusd"
STATUS_INTERVAL_S = 60

SYMBOL = "XAUUSD"
BAR_PERIOD_MINUTES = 15
STARTING_BALANCE = 100_000.0
MIN_CONFIDENCE = 0.50


def _validate_env() -> list[str]:
    """Return list of missing env vars."""
    required = [
        "CTRADER_OPENAPI_CLIENT_ID",
        "CTRADER_OPENAPI_CLIENT_SECRET",
        "CTRADER_OPENAPI_ACCESS_TOKEN",
        "CTRADER_OPENAPI_ACCOUNT_ID",
    ]
    return [k for k in required if not os.environ.get(k)]


@dataclass
class EngineSlot:
    engine: ForwardTestEngine
    thread: threading.Thread | None = None
    running: bool = False


class TTCXAUUSDForwardTest:
    def __init__(self, paper_only: bool = False):
        self._paper_only = paper_only
        self._shutdown = False
        self._slot: EngineSlot | None = None
        self._status_thread: threading.Thread | None = None

    def _build_engine(self) -> EngineSlot:
        ftmo = FTMOConfig(
            daily_loss_limit_pct=0.04,
            total_drawdown_limit_pct=0.07,
            min_risk_reward=0.0,
            max_trades_per_day=10,
            max_positions=5,
        )

        strategy = TTCXAUUSDStrategy()
        config = ForwardTestConfig(
            symbol=SYMBOL,
            starting_balance=STARTING_BALANCE,
            min_confidence=MIN_CONFIDENCE,
            bar_period_minutes=BAR_PERIOD_MINUTES,
            min_bars_for_evaluation=50,
            max_bars_per_symbol=500,
            stats_interval_sec=STATUS_INTERVAL_S,
            live_mode=not self._paper_only,
            log_dir=LOG_DIR,
            health_monitor_interval_sec=5.0,
            max_reconnect_attempts=20,
            reset_on_start=True,
        )

        engine = ForwardTestEngine(
            config=config,
            strategies=[strategy],
            ftmo_config=ftmo,
        )
        logger.info("Engine built: %s (M15, TTC XAUUSD)", SYMBOL)
        return EngineSlot(engine=engine)

    def _on_signal_traded(self, signal):
        logger.info(
            f"SIGNAL: {signal.direction.value} {signal.symbol} @ {signal.entry_price:.5f} conf={signal.confidence:.2f}"
        )

    def _status_loop(self):
        while not self._shutdown:
            time.sleep(STATUS_INTERVAL_S)
            if self._shutdown:
                break
            if not self._slot or not self._slot.running:
                continue
            now = datetime.now(timezone.utc).strftime("%H:%M:%S")
            pe = self._slot
            health = pe.engine.health
            stats = pe.engine.get_stats().get("trading", {})
            bal = stats.get("current_balance", 0)
            trades = stats.get("trades_executed", 0)
            signals = health.signals_generated
            ticks = health.ticks_received
            connected = "Y" if health.connected else "N"
            print(
                f"[{now} UTC] {SYMBOL}: bal=${bal:.2f} trades={trades} signals={signals} ticks={ticks} conn={connected}"
            )

    def run(self):
        missing = _validate_env()
        if missing:
            print(f"ERROR: Missing required env vars: {', '.join(missing)}")
            sys.exit(1)

        self._slot = self._build_engine()

        print("=" * 60)
        mode = "PAPER-ONLY" if self._paper_only else "DEMO EXECUTION"
        print(f"TTC XAUUSD FORWARD TEST ({mode})")
        print("=" * 60)
        print(f"Symbol: {SYMBOL}")
        print("Bar period: M15")
        print("Strategy: TTCXAUUSDStrategy (Optuna-validated)")
        print(f"Starting balance: ${STARTING_BALANCE:,.2f}")
        print(f"Min confidence: {MIN_CONFIDENCE}")
        print("-" * 60)

        self._slot.engine.register_callback("on_signal_traded", self._on_signal_traded)
        t = threading.Thread(
            target=self._run_engine,
            args=(self._slot,),
            daemon=True,
            name="ft-ttc-xauusd",
        )
        self._slot.thread = t
        t.start()
        self._slot.running = True

        sig.signal(sig.SIGINT, self._shutdown_handler)
        sig.signal(sig.SIGTERM, self._shutdown_handler)

        self._status_thread = threading.Thread(target=self._status_loop, daemon=True, name="status")
        self._status_thread.start()

        print("Engine started. Press Ctrl+C to stop.")
        print("-" * 60)

        try:
            while not self._shutdown:
                time.sleep(1)
        except KeyboardInterrupt:
            self._shutdown_handler()

    def _run_engine(self, pe: EngineSlot):
        try:
            success = pe.engine.start()
            if not success:
                logger.error(f"{SYMBOL}: engine failed to start")
                pe.running = False
                return
            logger.info(f"{SYMBOL}: engine running")
            while not self._shutdown and pe.engine.is_running:
                time.sleep(1)
            pe.engine.stop()
            logger.info(f"{SYMBOL}: engine stopped")
        except Exception as e:
            logger.error(f"{SYMBOL}: engine error: {e}", exc_info=True)
        finally:
            pe.running = False

    def _shutdown_handler(self, signum=None, frame=None):
        print("\nShutting down...")
        self._shutdown = True
        if self._slot and self._slot.running:
            self._slot.engine.stop()
        time.sleep(2)
        if self._slot:
            stats = self._slot.engine.get_stats().get("trading", {})
            bal = stats.get("current_balance", 0)
            trades = stats.get("trades_executed", 0)
            pnl = stats.get("realized_pnl", 0)
            print(f"  {SYMBOL}: bal=${bal:.2f} trades={trades} pnl=${pnl:.2f}")
        print("Shutdown complete.")
        sys.exit(0)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="TTC XAUUSD Forward Test")
    parser.add_argument(
        "--paper-only",
        action="store_true",
        help="Local simulation only - no real broker orders",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    os.makedirs(LOG_DIR, exist_ok=True)
    system = TTCXAUUSDForwardTest(paper_only=args.paper_only)
    system.run()


if __name__ == "__main__":
    main()
