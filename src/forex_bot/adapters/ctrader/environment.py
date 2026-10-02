"""Strongly typed cTrader environment model.

Prevents demo/live confusion by cross-checking endpoint, account,
and configured environment at startup. Misconfiguration fails closed.
"""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("ayumi.ctrader.environment")


class Environment(Enum):
    DEMO = "demo"
    LIVE = "live"
    OFFLINE = "offline"

    @classmethod
    def from_string(cls, value: str) -> "Environment":
        normalized = value.lower().strip()
        if normalized in ("demo", "practice", "test"):
            return cls.DEMO
        if normalized in ("live", "production", "real"):
            return cls.LIVE
        if normalized in ("offline", "paper", "backtest", "none"):
            return cls.OFFLINE
        raise ValueError(f"Unknown environment: {value!r}")

    @property
    def is_demo(self) -> bool:
        return self == Environment.DEMO

    @property
    def is_live(self) -> bool:
        return self == Environment.LIVE

    @property
    def is_offline(self) -> bool:
        return self == Environment.OFFLINE


# Known endpoint domains per environment
DEMO_HOSTS = {
    "demo.ctraderapi.com",
    "demo-uk-eqx-01.p.c-trader.com",
    "demo-eu-eqx-01.p.c-trader.com",
}
LIVE_HOSTS = {
    "live.ctraderapi.com",
    "live-uk-eqx-01.p.c-trader.com",
    "live-eu-eqx-01.p.c-trader.com",
}


def _infer_environment(host: str) -> Environment:
    """Classify an endpoint host as demo/live/offline.

    If host contains 'demo', it's DEMO.
    If host contains 'live', it's LIVE.
    Otherwise OFFLINE.
    """
    host_lower = host.lower()
    if host_lower in DEMO_HOSTS:
        return Environment.DEMO
    if host_lower in LIVE_HOSTS:
        return Environment.LIVE
    return Environment.OFFLINE


def validate_endpoint_environment(host: str, env: Environment) -> None:
    """Cross-check that the endpoint matches the configured environment.

    Raises ValueError on mismatch (fail closed).
    """
    host_lower = host.lower().strip()
    if env == Environment.DEMO:
        if host_lower in LIVE_HOSTS:
            raise ValueError(
                f"SECURITY: Demo environment configured but live endpoint detected: {host}. "
                "Refusing to start — check CTRADER_HOST and environment configuration."
            )
    elif env == Environment.LIVE:
        if host_lower in DEMO_HOSTS:
            raise ValueError(
                f"SECURITY: Live environment configured but demo endpoint detected: {host}. "
                "Refusing to start — check CTRADER_HOST and environment configuration."
            )
    # OFFLINE doesn't connect, so no endpoint check needed


def log_startup_environment(
    env: Environment,
    host: str,
    account_id: str,
    kill_switch_active: bool,
    kill_switch_mode: str,
) -> None:
    """Log startup environment info without secrets."""
    logger.info(
        "[Environment] mode=%s endpoint=%s account=%s kill_switch=%s/%s",
        env.value,
        host,
        account_id,
        "active" if kill_switch_active else "inactive",
        kill_switch_mode,
    )
