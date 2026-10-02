from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DATA_SILENCE_SEC = 30.0
_DEFAULT_ZERO_SIGNAL_TICKS = 100
_DEFAULT_ZERO_PNL_VARIANCE_TRADES = 10
_DEFAULT_CHECK_INTERVAL_SEC = 5.0
_DEFAULT_MIN_UPTIME_SEC = 60.0


class HealthAlertKind(Enum):
    DATA_SILENCE = "data_silence"
    ZERO_SIGNALS = "zero_signals"
    ZERO_PNL_VARIANCE = "zero_pnl_variance"


@dataclass
class HealthAlert:
    kind: HealthAlertKind
    message: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    context: dict = field(default_factory=dict)


@dataclass
class HealthMonitorConfig:
    data_silence_threshold_sec: float = _DEFAULT_DATA_SILENCE_SEC
    zero_signal_tick_threshold: int = _DEFAULT_ZERO_SIGNAL_TICKS
    zero_pnl_variance_trade_threshold: int = _DEFAULT_ZERO_PNL_VARIANCE_TRADES
    check_interval_sec: float = _DEFAULT_CHECK_INTERVAL_SEC
    min_uptime_before_alerts_sec: float = _DEFAULT_MIN_UPTIME_SEC


@dataclass
class HealthSnapshot:
    healthy: bool = True
    alerts: list[HealthAlert] = field(default_factory=list)
    last_tick_at: Optional[datetime] = None
    ticks_received: int = 0
    signals_generated: int = 0
    trades_completed: int = 0
    last_check_at: Optional[datetime] = None


class HealthMonitor:
    """Read-only health monitor for forward-test engines.

    Detects three failure modes from post-mortem patterns (AYUAA-778, AYUAA-807):
      1. Data silence -- no ticks received within threshold
      2. Zero signals -- N ticks processed but zero signals generated
      3. Zero P&L variance -- N trades completed but all have identical P&L

    Design constraints:
      - Read-only: never modifies trading state
      - Fail-safe: exceptions in monitoring never propagate to callers
      - Composable: accepts counters via record_tick / record_signal /
        record_trade_pnl so any engine can feed it
    """

    def __init__(
        self,
        config: Optional[HealthMonitorConfig] = None,
        on_alert: Optional[Callable[[HealthAlert], None]] = None,
    ):
        self._config = config or HealthMonitorConfig()
        self._on_alert = on_alert

        self._lock = threading.Lock()
        self._last_tick_at: Optional[datetime] = None
        self._ticks_received: int = 0
        self._signals_generated: int = 0
        self._trade_pnls: list[float] = []
        self._trades_completed: int = 0
        self._alerts: list[HealthAlert] = []
        self._healthy: bool = True
        self._last_check_at: Optional[datetime] = None

        self._start_time: Optional[datetime] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def start(self):
        if self._thread is not None:
            return
        self._start_time = datetime.now(timezone.utc)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="health-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10.0)
            self._thread = None

    def record_tick(self, tick_at: Optional[datetime] = None):
        with self._lock:
            self._ticks_received += 1
            self._last_tick_at = tick_at or datetime.now(timezone.utc)

    def record_signal(self):
        with self._lock:
            self._signals_generated += 1

    def record_trade_pnl(self, pnl: float):
        with self._lock:
            self._trade_pnls.append(pnl)
            self._trades_completed += 1

    def get_snapshot(self) -> HealthSnapshot:
        with self._lock:
            return HealthSnapshot(
                healthy=self._healthy,
                alerts=list(self._alerts),
                last_tick_at=self._last_tick_at,
                ticks_received=self._ticks_received,
                signals_generated=self._signals_generated,
                trades_completed=self._trades_completed,
                last_check_at=self._last_check_at,
            )

    def reset_alerts(self):
        with self._lock:
            self._alerts.clear()
            self._healthy = True

    def check(self) -> list[HealthAlert]:
        """Run all checks and return any new alerts."""
        new_alerts: list[HealthAlert] = []
        try:
            new_alerts.extend(self._check_data_silence())
            new_alerts.extend(self._check_zero_signals())
            new_alerts.extend(self._check_zero_pnl_variance())
        except Exception as exc:
            logger.error("HealthMonitor check error: %s", exc, exc_info=True)

        with self._lock:
            self._last_check_at = datetime.now(timezone.utc)
            if new_alerts:
                self._alerts.extend(new_alerts)
                self._healthy = False

        for alert in new_alerts:
            logger.warning("Health alert [%s]: %s", alert.kind.value, alert.message)
            if self._on_alert:
                try:
                    self._on_alert(alert)
                except Exception as exc:
                    logger.error("Health alert callback error: %s", exc, exc_info=True)

        return new_alerts

    def _uptime_sec(self) -> float:
        if self._start_time is None:
            return 0.0
        return (datetime.now(timezone.utc) - self._start_time).total_seconds()

    def _check_data_silence(self) -> list[HealthAlert]:
        cfg = self._config
        if self._uptime_sec() < cfg.min_uptime_before_alerts_sec:
            return []
        with self._lock:
            last_tick = self._last_tick_at
        if last_tick is None:
            if self._uptime_sec() >= cfg.data_silence_threshold_sec:
                return [
                    HealthAlert(
                        kind=HealthAlertKind.DATA_SILENCE,
                        message=(f"No ticks received since startup ({self._uptime_sec():.0f}s ago)"),
                        context={"uptime_sec": self._uptime_sec()},
                    )
                ]
            return []
        staleness = (datetime.now(timezone.utc) - last_tick).total_seconds()
        if staleness >= cfg.data_silence_threshold_sec:
            return [
                HealthAlert(
                    kind=HealthAlertKind.DATA_SILENCE,
                    message=f"No ticks received for {staleness:.0f}s",
                    context={"staleness_sec": staleness},
                )
            ]
        return []

    def _check_zero_signals(self) -> list[HealthAlert]:
        cfg = self._config
        with self._lock:
            ticks = self._ticks_received
            signals = self._signals_generated
        if ticks >= cfg.zero_signal_tick_threshold and signals == 0:
            return [
                HealthAlert(
                    kind=HealthAlertKind.ZERO_SIGNALS,
                    message=(
                        f"Zero signals generated after {ticks} ticks -- check strategy thresholds and session windows"
                    ),
                    context={"ticks_received": ticks},
                )
            ]
        return []

    def _check_zero_pnl_variance(self) -> list[HealthAlert]:
        cfg = self._config
        with self._lock:
            pnls = list(self._trade_pnls)
            trades = self._trades_completed
        if trades < cfg.zero_pnl_variance_trade_threshold:
            return []
        if len(pnls) < 2:
            return []
        mean = sum(pnls) / len(pnls)
        variance = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        if variance == 0.0:
            return [
                HealthAlert(
                    kind=HealthAlertKind.ZERO_PNL_VARIANCE,
                    message=(
                        f"Zero P&L variance across {trades} trades "
                        f"(all P&L = {pnls[0]:.2f}) -- "
                        "trades may not be executing"
                    ),
                    context={
                        "trades_completed": trades,
                        "pnl_value": pnls[0],
                    },
                )
            ]
        return []

    def _loop(self):
        while not self._stop_event.wait(self._config.check_interval_sec):
            try:
                self.check()
            except Exception as exc:
                logger.error("HealthMonitor loop error: %s", exc, exc_info=True)
