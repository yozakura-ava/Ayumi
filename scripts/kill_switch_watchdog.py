#!/usr/bin/env python3
"""Kill Switch Watchdog — monitors trading heartbeat and auto-activates kill switch.

Standalone process that reads the trading heartbeat file and activates the
global kill switch if the heartbeat goes stale during market hours.

Usage::

    python scripts/kill_switch_watchdog.py \\
        --heartbeat-file data/heartbeat_trading.json \\
        --check-interval 5

Market-hours aware: skips stale-heartbeat triggers on weekends
(Fri 21:55 UTC – Sun 21:00 UTC).
"""

import argparse
import json
import logging
import signal as sig_module
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src" / "forex_bot"))

from adapters.ctrader.kill_switch import KillSwitchManager

logger = logging.getLogger("ayumi.kill_switch_watchdog")

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_HEARTBEAT_FILE = "data/heartbeat_trading.json"
DEFAULT_CHECK_INTERVAL = 5  # seconds
DEFAULT_STALE_THRESHOLD = 30  # seconds — heartbeat older than this during market hours = kill

_WEEKEND_CLOSE_HOUR_UTC = 21
_WEEKEND_CLOSE_MINUTE_UTC = 55
_WEEKEND_OPEN_HOUR_UTC = 21


def _is_forex_market_closed(now: datetime | None = None) -> bool:
    """Check if the forex market is closed (weekend).

    Weekend: Friday 21:55 UTC – Sunday 21:00 UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    today = now.weekday()

    # Friday — closes at 21:55 UTC
    if today == 4:  # Friday
        if now.hour > _WEEKEND_CLOSE_HOUR_UTC:
            return True
        if now.hour == _WEEKEND_CLOSE_HOUR_UTC and now.minute >= _WEEKEND_CLOSE_MINUTE_UTC:
            return True

    # Saturday — fully closed
    if today == 5:
        return True

    # Sunday — opens at 21:00 UTC
    if today == 6:
        if now.hour < _WEEKEND_OPEN_HOUR_UTC:
            return True
        return False

    # Monday before 21:00 UTC — still closed (some brokers)
    if today == 0 and now.hour < _WEEKEND_OPEN_HOUR_UTC:
        return True

    return False


def _read_heartbeat(filepath: str) -> dict | None:
    """Read and parse the heartbeat file.

    Returns None if file is missing, empty, or contains invalid JSON.
    """
    path = Path(filepath)
    if not path.exists():
        return None

    try:
        raw = path.read_text().strip()
        if not raw:
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read heartbeat file %s: %s", filepath, exc)
        return None


def _heartbeat_age_seconds(heartbeat: dict) -> float:
    """Calculate age of heartbeat in seconds from the last_beat timestamp."""
    last_beat_str = heartbeat.get("last_beat")
    if not last_beat_str:
        return float("inf")

    try:
        # Parse ISO 8601 timestamp
        last_beat = datetime.fromisoformat(last_beat_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - last_beat).total_seconds()
    except (ValueError, TypeError) as exc:
        logger.warning("Failed to parse heartbeat timestamp '%s': %s", last_beat_str, exc)
        return float("inf")


class KillSwitchWatchdog:
    """Heartbeat watchdog that auto-activates the kill switch on stale heartbeat.

    Designed as a standalone process. If this process dies, it does NOT
    affect the trading engine — it only means auto-activation won't fire.
    The engine's own health monitor remains independent.
    """

    def __init__(
        self,
        heartbeat_file: str,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        stale_threshold: int = DEFAULT_STALE_THRESHOLD,
        state_dir: str | None = None,
    ):
        self._heartbeat_file = heartbeat_file
        self._check_interval = check_interval
        self._stale_threshold = stale_threshold
        self._running = False
        self._kill_switch = KillSwitchManager(state_dir=state_dir or str(PROJECT_ROOT / "data" / "kill_switches"))
        self._last_stale_log = 0.0  # monotonic timestamp of last stale-warning log

    @property
    def is_running(self) -> bool:
        return self._running

    def check_once(self) -> bool:
        """Perform a single heartbeat check.

        Returns True if heartbeat is healthy (or market is closed),
        False if heartbeat is stale and kill was activated.
        """
        now = datetime.now(timezone.utc)

        # Skip during market closure
        if _is_forex_market_closed(now):
            logger.debug("Market closed — skipping heartbeat check")
            return True

        heartbeat = _read_heartbeat(self._heartbeat_file)

        if heartbeat is None:
            # No heartbeat file at all
            age = float("inf")
        else:
            age = _heartbeat_age_seconds(heartbeat)

        if age > self._stale_threshold:
            # Throttle stale-warning logs to once per minute
            now_mono = time.monotonic()
            if now_mono - self._last_stale_log > 60:
                logger.warning(
                    "Heartbeat is stale: age=%.1fs threshold=%ds",
                    age if age != float("inf") else -1,
                    self._stale_threshold,
                )
                self._last_stale_log = now_mono

            # Check if engine_running flag is False — that's a clean shutdown, not a crash
            if heartbeat and heartbeat.get("engine_running") is False:
                logger.info("Engine reports engine_running=false — clean shutdown, not activating kill")
                return True

            # Activate global kill
            logger.critical(
                "HEARTBEAT STALE: age=%.1fs > %ds threshold — ACTIVATING GLOBAL KILL",
                age if age != float("inf") else -1,
                self._stale_threshold,
            )
            self._kill_switch.activate_global_kill(
                reason="heartbeat_stale",
                triggered_by="watchdog",
                close_positions=True,
            )
            return False

        return True

    def run(self):
        """Main watchdog loop. Blocks until stopped via SIGTERM/SIGINT."""
        self._running = True
        logger.info(
            "Kill switch watchdog started: heartbeat=%s interval=%ds stale_threshold=%ds",
            self._heartbeat_file,
            self._check_interval,
            self._stale_threshold,
        )

        while self._running:
            try:
                self.check_once()
            except Exception as exc:
                logger.error("Watchdog check error: %s", exc, exc_info=True)

            # Sleep in small increments to allow responsive shutdown
            slept = 0
            while self._running and slept < self._check_interval:
                time.sleep(1)
                slept += 1

        logger.info("Kill switch watchdog stopped")

    def stop(self):
        """Signal the watchdog to stop."""
        self._running = False


def main():
    parser = argparse.ArgumentParser(
        description="Ayumi Kill Switch Watchdog — heartbeat monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--heartbeat-file",
        default=DEFAULT_HEARTBEAT_FILE,
        help=f"Path to heartbeat JSON file (default: {DEFAULT_HEARTBEAT_FILE})",
    )
    parser.add_argument(
        "--check-interval",
        type=int,
        default=DEFAULT_CHECK_INTERVAL,
        help=f"Check interval in seconds (default: {DEFAULT_CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--stale-threshold",
        type=int,
        default=DEFAULT_STALE_THRESHOLD,
        help=f"Stale threshold in seconds (default: {DEFAULT_STALE_THRESHOLD})",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Kill switch state directory (default: data/kill_switches)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Resolve heartbeat file path
    heartbeat_file = args.heartbeat_file
    if not Path(heartbeat_file).is_absolute():
        heartbeat_file = str(PROJECT_ROOT / heartbeat_file)

    watchdog = KillSwitchWatchdog(
        heartbeat_file=heartbeat_file,
        check_interval=args.check_interval,
        stale_threshold=args.stale_threshold,
        state_dir=args.state_dir,
    )

    # Signal handlers for clean shutdown
    def _shutdown(signum, frame):
        logger.info("Shutdown signal received (sig=%d)", signum)
        watchdog.stop()

    sig_module.signal(sig_module.SIGTERM, _shutdown)
    sig_module.signal(sig_module.SIGINT, _shutdown)

    watchdog.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
