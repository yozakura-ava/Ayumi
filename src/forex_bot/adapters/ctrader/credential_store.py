"""Single source of truth for cTrader credentials.

Reads from .env on startup. Writes refreshed tokens back to .env.
No separate credential file. No migration. No drift.

.env keys:
    CTRADER_OPENAPI_CLIENT_ID
    CTRADER_OPENAPI_CLIENT_SECRET
    CTRADER_OPENAPI_ACCESS_TOKEN
    CTRADER_OPENAPI_REFRESH_TOKEN
    CTRADER_OPENAPI_ACCOUNT_ID
    CTRADER_OPENAPI_TRADER_LOGIN
    CTRADER_OPENAPI_TOKEN_EXPIRES_AT   (ISO-8601 UTC, written by update_tokens)
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ayumi.credentials")

# .env key → field name mapping
_ENV_KEYS = {
    "CTRADER_OPENAPI_CLIENT_ID": "client_id",
    "CTRADER_OPENAPI_CLIENT_SECRET": "client_secret",
    "CTRADER_OPENAPI_ACCESS_TOKEN": "access_token",
    "CTRADER_OPENAPI_REFRESH_TOKEN": "refresh_token",
    "CTRADER_OPENAPI_ACCOUNT_ID": "account_id",
    "CTRADER_OPENAPI_TRADER_LOGIN": "trader_login",
    "CTRADER_OPENAPI_TOKEN_EXPIRES_AT": "expires_at",
}


@dataclass(frozen=True)
class Credentials:
    """Frozen credential snapshot."""

    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str
    account_id: int
    trader_login: int
    expires_at: Optional[datetime] = None


class CredentialStore:
    """Thread-safe credential store backed by .env.

    .env is the ONLY credential source. No migration, no separate file.
    Refreshed tokens are written back to .env atomically.
    """

    def __init__(self, env_path: str | Path = ".env"):
        self._path = Path(env_path)
        self._lock = threading.Lock()
        self._cached: Credentials | None = None

    def load(self) -> Credentials:
        """Load credentials from .env. Caches in memory."""
        with self._lock:
            if self._cached is not None:
                return self._cached
            return self._load_unsafe()

    def get(self) -> Credentials:
        """Get cached credentials (loads if needed)."""
        if self._cached is None:
            return self.load()
        return self._cached

    def update_tokens(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        """Update tokens in .env. Called ONLY by token_lifecycle.

        Writes the new access_token, refresh_token, and computed expires_at
        back to .env atomically. Creates a backup of the previous .env first.
        Other keys are preserved.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        expires_at_str = expires_at.isoformat()
        with self._lock:
            # Backup current .env before overwriting
            if self._path.exists():
                backup_path = self._path.with_suffix(".env.token_backup")
                shutil.copy2(str(self._path), str(backup_path))
                logger.debug("Token backup written to %s", backup_path)

            # Read current .env, update only the token lines
            lines = self._path.read_text().splitlines() if self._path.exists() else []

            # Track what we've updated
            updated = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    new_lines.append(line)
                    continue
                key, _, _ = stripped.partition("=")
                key = key.strip()
                if key == "CTRADER_OPENAPI_ACCESS_TOKEN":
                    new_lines.append(f"{key}={access_token}")
                    updated.add(key)
                elif key == "CTRADER_OPENAPI_REFRESH_TOKEN":
                    new_lines.append(f"{key}={refresh_token}")
                    updated.add(key)
                elif key == "CTRADER_OPENAPI_TOKEN_EXPIRES_AT":
                    new_lines.append(f"{key}={expires_at_str}")
                    updated.add(key)
                else:
                    new_lines.append(line)

            # Add any token keys that weren't in .env
            if "CTRADER_OPENAPI_ACCESS_TOKEN" not in updated:
                new_lines.append(f"CTRADER_OPENAPI_ACCESS_TOKEN={access_token}")
            if "CTRADER_OPENAPI_REFRESH_TOKEN" not in updated:
                new_lines.append(f"CTRADER_OPENAPI_REFRESH_TOKEN={refresh_token}")
            if "CTRADER_OPENAPI_TOKEN_EXPIRES_AT" not in updated:
                new_lines.append(f"CTRADER_OPENAPI_TOKEN_EXPIRES_AT={expires_at_str}")

            # Atomic write
            tmp = self._path.with_suffix(".env.tmp")
            tmp.write_text("\n".join(new_lines) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)

            # Update cache
            current = self._cached or self._load_unsafe()
            self._cached = Credentials(
                client_id=current.client_id,
                client_secret=current.client_secret,
                access_token=access_token,
                refresh_token=refresh_token,
                account_id=current.account_id,
                trader_login=current.trader_login,
                expires_at=expires_at,
            )
            logger.info("Tokens written to .env, expires_at=%s", expires_at.isoformat())

    # ── Internal ───────────────────────────────────────────────────────────

    def _load_unsafe(self) -> Credentials:
        """Load from .env (caller must hold lock)."""
        if not self._path.exists():
            raise RuntimeError(f"No .env found at {self._path}")

        env_data = self._read_env_file()
        if "access_token" not in env_data or not env_data["access_token"]:
            raise RuntimeError("No CTRADER_OPENAPI_ACCESS_TOKEN in .env — run OAuth setup first")

        creds = self._dict_to_credentials(env_data)
        self._cached = creds
        return creds

    def _read_env_file(self) -> dict:
        """Parse .env into a dict."""
        data: dict[str, str | int] = {}
        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # Strip inline comments
            if " #" in value:
                value = value.split(" #")[0].strip()

            if key in _ENV_KEYS:
                field = _ENV_KEYS[key]
                if field in ("account_id", "trader_login"):
                    data[field] = int(value) if value else 0
                elif field == "expires_at":
                    # Parse ISO-8601 datetime; store as datetime object
                    if value:
                        try:
                            data[field] = datetime.fromisoformat(value)
                        except ValueError:
                            logger.warning(
                                "Unparseable CTRADER_OPENAPI_TOKEN_EXPIRES_AT=%r — ignoring",
                                value,
                            )
                else:
                    data[field] = value

        data.setdefault("version", 1)
        return data

    def _dict_to_credentials(self, data: dict) -> Credentials:
        """Convert dict to Credentials dataclass."""
        expires_at = data.get("expires_at")
        # Ensure timezone-aware if present
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return Credentials(
            client_id=str(data.get("client_id", "")),
            client_secret=str(data.get("client_secret", "")),
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            account_id=int(data.get("account_id", 0)),
            trader_login=int(data.get("trader_login", 0)),
            expires_at=expires_at,
        )
