#!/usr/bin/env python3
"""Daily cTrader credential health probe.

Tests cTrader API auth WITHOUT starting the trading engine:
  1. Credential load (data/.credentials or .env migration)
  2. Access token validity (placeholder / expiry / missing)
  3. Refresh capability (refresh token present + non-placeholder)
  4. API reachability (HTTPS ping to cTrader OAuth endpoint)

Writes a JSONL record to data/ops/ctrader_credential_health.jsonl and exits:
  0 = healthy
  1 = degraded (token near expiry or refresh-only issue)
  2 = broken (missing/placeholder tokens or API unreachable)

Cron example (daily at 06:00):
  0 6 * * * cd $AYUMI_ROOT && source .venv/bin/activate && python3 scripts/ctrader_credential_probe.py >> data/ops/ctrader_credential_probe.log 2>&1
"""  # noqa: E501

from __future__ import annotations

import json
import logging
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Allow running from repo root without package install
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "src" / "forex_bot"))

from adapters.ctrader.auth import CredentialError, CTraderAuth
from adapters.ctrader.token_manager import TokenStatus

logger = logging.getLogger("ctrader_credential_probe")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

OUTPUT_PATH = project_root / "data" / "ops" / "ctrader_credential_health.jsonl"
CTRADER_OAUTH_REFRESH_URL = "https://openapi.ctrader.com/apps/token"


# Local copy of placeholder values to avoid depending on private TokenManager internals
_PLACEHOLDER_VALUES = {
    "",
    "none",
    "null",
    "todo",
    "changeme",
    "***",
    "new-access",
    "new-refresh",
}


def _write_health_record(record: dict) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _api_reachable(url: str = CTRADER_OAUTH_REFRESH_URL, timeout: float = 10.0) -> tuple[bool, str]:
    """Lightweight HTTPS reachability check; no credentials sent."""
    parsed = urlparse(url)
    host = parsed.hostname or "openapi.ctrader.com"
    port = parsed.port or 443
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as ssock:
                ssock.settimeout(timeout)
                ssock.sendall(
                    f"HEAD {parsed.path or '/'} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("utf-8")
                )
                response = ssock.recv(1024).decode("utf-8", errors="ignore")
        if "HTTP/1.1" in response or "HTTP/2" in response or "400" in response:
            return True, f"API reachable at {host}:{port}"
        return False, f"Unexpected response from {host}:{port}: {response[:120]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"API unreachable at {host}:{port}: {exc}"


def _overall_status(checks: dict) -> tuple[int, str]:
    """Derive exit code and summary from individual checks."""
    if not checks["credentials_loaded"]:
        return 2, "credentials_not_loaded"
    if checks["token_status"] in (TokenStatus.MISSING, TokenStatus.PLACEHOLDER):
        return 2, "access_token_missing_or_placeholder"
    if not checks["api_reachable"]:
        return 2, "api_unreachable"
    if checks["refresh_status"] == "missing_or_placeholder":
        return 2, "refresh_token_missing_or_placeholder"
    if checks["token_status"] == TokenStatus.EXPIRED:
        return 2, "access_token_expired"
    if checks["token_status"] == TokenStatus.CRITICAL:
        return 2, "access_token_critical"
    if checks["token_status"] == TokenStatus.WARNING:
        return 1, "access_token_near_expiry"
    return 0, "healthy"


def main() -> int:
    started_at = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat()

    checks = {
        "credentials_loaded": False,
        "token_status": TokenStatus.MISSING,
        "token_message": "",
        "days_remaining": 0.0,
        "token_hash": None,
        "refresh_status": "missing",
        "api_reachable": False,
        "api_message": "",
    }

    # 1. Load credentials without starting engine
    try:
        auth = CTraderAuth.create(auto_migrate=True)
        checks["credentials_loaded"] = True
        logger.info("Credentials loaded (account_id=%s)", auth.account_id)
    except CredentialError as exc:
        checks["token_message"] = f"CredentialError: {exc}"
        logger.error("Failed to load credentials: %s", exc)
    except Exception as exc:  # noqa: BLE001
        checks["token_message"] = f"Unexpected credential load error: {exc}"
        logger.exception("Unexpected error loading credentials")

    # 2. Validate access token
    if checks["credentials_loaded"]:
        try:
            validation = auth.validate_startup()
            checks["token_status"] = validation.get("status", TokenStatus.MISSING)
            checks["token_message"] = validation.get("message", "")
            checks["days_remaining"] = validation.get("days_remaining", 0.0)
            checks["token_hash"] = validation.get("token_hash")
            logger.info(
                "Token validation: status=%s days_remaining=%s",
                checks["token_status"],
                checks["days_remaining"],
            )
        except Exception as exc:  # noqa: BLE001
            checks["token_message"] = f"Token validation error: {exc}"
            logger.exception("Token validation failed")

        # 3. Refresh capability (presence only; avoids invalidating a good token)
        refresh_token = auth.refresh_token
        if not refresh_token:
            checks["refresh_status"] = "missing"
        elif refresh_token.lower() in _PLACEHOLDER_VALUES:
            checks["refresh_status"] = "missing_or_placeholder"
        else:
            checks["refresh_status"] = "ok_but_untested"

    # 4. API reachability
    checks["api_reachable"], checks["api_message"] = _api_reachable()
    logger.info("API reachability: %s", checks["api_message"])

    exit_code, summary = _overall_status(checks)
    duration_ms = round((time.monotonic() - started_at) * 1000, 2)

    record = {
        "timestamp": timestamp,
        "exit_code": exit_code,
        "status": summary,
        "checks": checks,
        "duration_ms": duration_ms,
    }
    _write_health_record(record)

    logger.info(
        "Probe complete: status=%s exit_code=%d duration_ms=%s",
        summary,
        exit_code,
        duration_ms,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
