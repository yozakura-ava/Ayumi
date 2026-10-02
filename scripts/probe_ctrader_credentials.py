#!/usr/bin/env python3
"""Credential health probe for cTrader OpenAPI.

Validates that cTrader credentials are present and can authenticate
against the cTrader OpenAPI server without starting the trading engine.

Designed for daily cron / CI use.  Reads credentials from environment
(or .env file), validates they are non-empty, then attempts a lightweight
application + account auth via the existing CTraderOpenApiClient.

Usage:
    python3 scripts/probe_ctrader_credentials.py
    python3 scripts/probe_ctrader_credentials.py --json
    python3 scripts/probe_ctrader_credentials.py --verbose

Exit codes:
    0 — All credentials valid and authentication succeeded
    1 — Credentials missing or authentication failed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# .env loading (prefer python-dotenv, fall back to manual parsing)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    _env_file = PROJECT_ROOT / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class ProbeResult(NamedTuple):
    """Outcome of a credential probe."""

    success: bool
    stage: str  # "validation" | "auth" | "import" | "error"
    message: str
    details: dict


REQUIRED_CREDS = [
    "CTRADER_OPENAPI_CLIENT_ID",
    "CTRADER_OPENAPI_CLIENT_SECRET",
    "CTRADER_OPENAPI_ACCESS_TOKEN",
    "CTRADER_OPENAPI_ACCOUNT_ID",
]


# ---------------------------------------------------------------------------
# Core probe functions (each is independently testable)
# ---------------------------------------------------------------------------


def get_credentials() -> dict[str, str]:
    """Read cTrader credentials from environment."""
    return {key: os.environ.get(key, "").strip() for key in REQUIRED_CREDS}


def validate_credentials(creds: dict[str, str]) -> list[str]:
    """Return list of missing or empty credential keys."""
    return [k for k in REQUIRED_CREDS if not creds.get(k)]


def check_auth(creds: dict[str, str], host: str | None = None, port: int = 5035) -> ProbeResult:
    """Attempt to authenticate with cTrader OpenAPI.

    Imports ``ctrader_open_api`` and ``CTraderOpenApiClient`` lazily so
    that credential validation works even when the SDK is not installed.

    Parameters
    ----------
    creds : dict
        Credential map (must contain all REQUIRED_CREDS keys).
    host : str, optional
        Override host (defaults to CTRADER_HOST env var or SDK default).
    port : int
        Override port (default 5035, the standard demo port).

    Returns
    -------
    ProbeResult
    """
    try:
        # Make src/forex_bot importable  (hyphen in dir name prevents normal import)
        _src = str(PROJECT_ROOT / "src" / "forex_bot")
        if _src not in sys.path:
            sys.path.insert(0, _src)

        from adapters.ctrader.open_api_client import CTraderOpenApiClient  # noqa: E402

        host = host or os.environ.get("CTRADER_HOST")
        account_id = int(creds["CTRADER_OPENAPI_ACCOUNT_ID"])

        client = CTraderOpenApiClient(
            client_id=creds["CTRADER_OPENAPI_CLIENT_ID"],
            client_secret=creds["CTRADER_OPENAPI_CLIENT_SECRET"],
            account_id=account_id,
            access_token=creds["CTRADER_OPENAPI_ACCESS_TOKEN"],
            host=host,
            port=port,
        )

        ok = client.connect()
        client.disconnect()

        if ok:
            return ProbeResult(
                success=True,
                stage="auth",
                message="Authentication succeeded",
                details={"account_id": str(account_id)},
            )
        return ProbeResult(
            success=False,
            stage="auth",
            message="Authentication failed — server rejected credentials",
            details={"account_id": str(account_id)},
        )
    except ImportError as exc:
        return ProbeResult(
            success=False,
            stage="import",
            message=f"ctrader_open_api library not available: {exc}",
            details={},
        )
    except Exception as exc:
        return ProbeResult(
            success=False,
            stage="error",
            message=f"Authentication error: {exc}",
            details={},
        )


def run_probe(host: str | None = None, port: int = 5035) -> ProbeResult:
    """Run the full credential probe.

    Steps:
        1. Read credentials from environment.
        2. Validate all required credentials are present and non-empty.
        3. Attempt authentication against cTrader OpenAPI.
    """
    creds = get_credentials()
    missing = validate_credentials(creds)

    if missing:
        return ProbeResult(
            success=False,
            stage="validation",
            message=f"Missing credentials: {', '.join(missing)}",
            details={"missing": missing},
        )

    return check_auth(creds, host=host, port=port)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe cTrader credential health without starting the engine.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output including credential prefixes",
    )
    args = parser.parse_args()

    result = run_probe()

    if args.json:
        print(
            json.dumps(
                {
                    "success": result.success,
                    "stage": result.stage,
                    "message": result.message,
                    "details": result.details,
                },
                indent=2,
            )
        )
    elif args.verbose:
        creds = get_credentials()
        prefixes = {k: (v[:8] + "..." if len(v) > 8 else "(empty)") for k, v in creds.items()}
        print("cTrader Credential Probe")
        print(f"  Stage:   {result.stage}")
        print(f"  Status:  {'PASS' if result.success else 'FAIL'}")
        print(f"  Message: {result.message}")
        print(f"  Credential prefixes: {prefixes}")
        if result.details:
            print(f"  Details: {result.details}")
    else:
        status = "PASS" if result.success else "FAIL"
        print(f"[{status}] cTrader credential probe: {result.message}")

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
