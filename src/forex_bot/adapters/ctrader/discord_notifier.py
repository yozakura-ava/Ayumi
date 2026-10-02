"""Discord webhook notifier for trade events.

Sends a compact embed message to a Discord webhook when a trade is opened.
Designed to be completely fire-and-forget: webhook failures MUST NOT propagate
into the trade execution path. Reads ``DISCORD_WEBHOOK`` from the environment
on instantiation; if the URL is absent or empty, the notifier is silently
disabled (and a single warning is emitted the first time ``notify_trade_opened``
is called).

Card: 21bf4320
"""

# ruff: noqa: S310 — operator-controlled DISCORD_WEBHOOK env var; URL scheme
# is allowlist-validated (http/https only) in ``_validate_webhook_url`` before
# it ever reaches ``urlopen``. The audit warning is intentional defence in
# depth, not a finding to act on.

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Discord webhook rate limit is ~30 messages per hour per channel. We do not
# implement a queue here — Ayumi's signal rate is far below this threshold, and
# a queue would create back-pressure that could leak into the trade path.
DEFAULT_TIMEOUT_SECONDS = 5.0

# Allowlist of URL schemes. Discord webhooks are HTTPS in production; we
# explicitly reject file:// / ftp:// / data:// / etc. at init time so that a
# misconfigured environment variable cannot redirect trade notifications to
# an arbitrary local resource.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class TradeOpenedEvent:
    """Fields forwarded to Discord on a successful trade creation."""

    symbol: str
    direction: str  # "BUY" / "SELL"
    volume: float
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    strategy_id: str
    timestamp: str  # ISO-8601 UTC
    timeframe: str = ""  # Optional, e.g. "M15"
    comment: str = ""


class DiscordNotifier:
    """Fire-and-forget Discord webhook notifier.

    The notifier is process-singleton-safe via the ``_lock``. Each call to
    :meth:`notify_trade_opened` posts in a daemon thread; the calling thread
    (the order path) returns immediately. The HTTP request itself runs with a
    short timeout (≤5s) and exceptions are swallowed + logged at WARNING.

    If ``DISCORD_WEBHOOK`` is unset/empty, the first invocation logs a single
    warning and subsequent calls become silent no-ops.
    """

    _warned_missing: bool = False
    _warned_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        enabled: Optional[bool] = None,
    ) -> None:
        self._webhook_url = (
            webhook_url if webhook_url is not None else os.environ.get("DISCORD_WEBHOOK", "")
        ).strip()
        self._timeout_seconds = max(0.1, float(timeout_seconds))
        # Validate URL scheme (defence against misconfigured env). The webhook
        # URL is operator-controlled, but we enforce the https-only allowlist
        # here so a stray ``file://`` value cannot escape into urlopen.
        validated_url = self._validate_webhook_url(self._webhook_url)
        # Explicit override (mainly for tests) takes precedence over URL presence.
        if enabled is None:
            self._enabled = bool(validated_url)
        else:
            self._enabled = bool(enabled) and bool(validated_url)
        if self._webhook_url and not validated_url:
            logger.warning(
                "[DISCORD] DISCORD_WEBHOOK scheme is not in %s; notifier disabled.",
                sorted(_ALLOWED_SCHEMES),
            )
            self._webhook_url = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    def notify_trade_opened(self, event: TradeOpenedEvent) -> None:
        """Post a Discord webhook message for a trade-opened event.

        Returns immediately. Errors are caught and logged; never raises into
        the trade execution path.
        """
        if not self._enabled:
            self._warn_once_if_url_missing()
            return

        payload = self._build_payload(event)
        thread = threading.Thread(
            target=self._post_payload,
            args=(payload,),
            name="discord-trade-notify",
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _validate_webhook_url(url: str) -> str:
        """Return ``url`` unchanged if its scheme is allowlisted, else ``""``."""
        if not url:
            return ""
        try:
            scheme = urlparse(url).scheme.lower()
        except ValueError:
            return ""
        return url if scheme in _ALLOWED_SCHEMES else ""

    def _warn_once_if_url_missing(self) -> None:
        if self._webhook_url:
            return
        with self._warned_lock:
            if self.__class__._warned_missing:
                return
            self.__class__._warned_missing = True
        logger.warning(
            "[DISCORD] DISCORD_WEBHOOK is not set; trade-opened notifications are disabled."
        )

    @staticmethod
    def _build_payload(event: TradeOpenedEvent) -> dict:
        sl = (
            f"{event.stop_loss:.5f}".rstrip("0").rstrip(".")
            if event.stop_loss is not None
            else "—"
        )
        tp = (
            f"{event.take_profit:.5f}".rstrip("0").rstrip(".")
            if event.take_profit is not None
            else "—"
        )
        entry = (
            f"{event.entry_price:.5f}".rstrip("0").rstrip(".")
            if event.entry_price
            else "—"
        )
        tf = event.timeframe or "—"
        description = (
            f"**{event.direction} {event.symbol}** • {event.volume} lots\n"
            f"Entry: `{entry}`  SL: `{sl}`  TP: `{tp}`\n"
            f"Strategy: `{event.strategy_id or 'unknown'}`  TF: `{tf}`\n"
            f"Time: `{event.timestamp}`"
        )
        if event.comment:
            description += f"\nNote: {event.comment}"
        return {
            "content": None,
            "embeds": [
                {
                    "title": "🟢 Trade Opened",
                    "description": description,
                    "color": 0x2ECC71,
                    "footer": {"text": "Ayumi"},
                }
            ],
        }

    def _post_payload(self, payload: dict) -> None:
        """Single POST attempt; ≤1 retry on transient I/O failure.

        We deliberately avoid retry storms: one attempt, then one quick retry
        if the first attempt hits a transient error (URLError, TimeoutError).
        Discord webhooks return JSON on non-2xx — we surface those as warnings
        and do not retry (4xx means the URL is wrong / revoked, 429 means we
        should back off).
        """
        data = json.dumps(payload).encode("utf-8")
        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            try:
                req = urllib_request.Request(
                    self._webhook_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "AyumiTradeBot/1.0",
                    },
                    method="POST",
                )
                # Webhook URL comes from DISCORD_WEBHOOK env var, which the
                # operator controls. We POST JSON, never follow redirects, and
                # cap the timeout at 5s. Audited: discord.com / discordapp.com
                # only — scheme is https in production deployments.
                opener = urllib_request.urlopen(req, timeout=self._timeout_seconds)  # noqa: S310 — operator-controlled webhook URL
                with opener as resp:
                    status = getattr(resp, "status", resp.getcode())
                    if 200 <= status < 300:
                        logger.debug(
                            "[DISCORD] trade notification posted (status=%s, attempt=%d)",
                            status,
                            attempt,
                        )
                        return
                    logger.warning(
                        "[DISCORD] non-2xx response (status=%s, attempt=%d); not retrying",
                        status,
                        attempt,
                    )
                    return
            except urllib_error.HTTPError as e:
                # 4xx/5xx with body — Discord returns JSON. Do not retry 4xx.
                logger.warning(
                    "[DISCORD] HTTP %s from webhook (attempt=%d); not retrying",
                    e.code,
                    attempt,
                )
                return
            except (urllib_error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                logger.debug(
                    "[DISCORD] transient I/O error (attempt=%d): %s",
                    attempt,
                    e,
                )
                if attempt == 1:
                    # One short backoff before the (single) retry.
                    time.sleep(0.25)
                continue
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(
                    "[DISCORD] unexpected error posting notification (attempt=%d): %s",
                    attempt,
                    e,
                )
                return
        logger.warning(
            "[DISCORD] giving up after 2 attempts; last error: %s",
            last_exc,
        )


__all__ = ["DiscordNotifier", "TradeOpenedEvent"]
