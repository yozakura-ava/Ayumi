"""OAuth token refresh wrapper for cTrader Open API.

A lightweight, thread-safe wrapper around the cTrader OAuth refresh endpoint.
Reads credentials from the existing JSON credentials file, refreshes the
access token using the refresh token grant, and writes updated tokens back
atomically.

Design goals (research doc section 4):
    - Atomic file writes (.tmp + rename) to prevent corruption
    - 5-minute ``expiry_buffer`` so tokens refresh BEFORE expiry
    - Thread-safe: multiple connections can call ``refresh_if_needed()``
    - Explicit error paths: NO_REFRESH_TOKEN, HTTP 400, HTTP 5xx

This module does NOT replace the existing TokenManager — it is a focused,
testable wrapper that can be used alongside it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("ayumi.connection.oauth_refresh")

# ── Constants ──────────────────────────────────────────────────────────────

CTRADER_OAUTH_URL = "https://openapi.ctrader.com/apps/token"
DEFAULT_EXPIRY_BUFFER_S = 300  # 5 minutes — refresh before actual expiry
REQUEST_TIMEOUT_S = 10


# ── Exceptions ─────────────────────────────────────────────────────────────


class OAuthRefreshError(Exception):
    """Base exception for OAuth refresh failures."""

    def __init__(self, message: str, *, retry_hint: bool = False):
        super().__init__(message)
        self.retry_hint = retry_hint


class NoRefreshTokenError(OAuthRefreshError):
    """Raised when the credentials file has no refresh_token."""


class OAuthHttpError(OAuthRefreshError):
    """Raised when the OAuth endpoint returns an error HTTP status."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message, retry_hint=status_code >= 500)
        self.status_code = status_code


# ── Data class ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthToken:
    """Immutable token snapshot."""

    access_token: str
    refresh_token: str
    expires_at: float  # Unix epoch seconds


# ── Manager ────────────────────────────────────────────────────────────────


class OAuthRefreshManager:
    """Thread-safe OAuth token refresh wrapper.

    Args:
        credentials_path: Path to the JSON credentials file.
        client_id: OAuth client ID (from credentials file if not given).
        client_secret: OAuth client secret (from credentials file if not given).
        expiry_buffer: Seconds before expiry to trigger proactive refresh.
    """

    def __init__(
        self,
        credentials_path: str | Path = "data/.credentials",
        *,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        expiry_buffer: int = DEFAULT_EXPIRY_BUFFER_S,
    ):
        self._path = Path(credentials_path)
        self._expiry_buffer = expiry_buffer
        self._lock = threading.Lock()
        self._cached_token: Optional[OAuthToken] = None

        # Load client_id / client_secret from file if not provided
        if client_id is None or client_secret is None:
            creds = self._read_credentials()
            client_id = client_id or creds.get("client_id", "")
            client_secret = client_secret or creds.get("client_secret", "")

        self._client_id = client_id
        self._client_secret = client_secret

    # ── Public API ─────────────────────────────────────────────────────────

    def get_current_token(self) -> Optional[OAuthToken]:
        """Return the cached token, or load from disk if no cache."""
        with self._lock:
            if self._cached_token is not None:
                return self._cached_token

        # Try loading from disk
        try:
            creds = self._read_credentials()
            access = creds.get("access_token", "")
            refresh = creds.get("refresh_token", "")
            if not access or not refresh:
                return None

            # We don't have expires_at in the credentials file,
            # so assume the token is fresh on first load.
            token = OAuthToken(
                access_token=access,
                refresh_token=refresh,
                expires_at=time.time() + 3600,  # assume 1h if unknown
            )
            with self._lock:
                self._cached_token = token
            return token
        except Exception:
            return None

    def needs_refresh(self, token: Optional[OAuthToken] = None) -> bool:
        """Check if the token should be refreshed based on expiry buffer."""
        tok = token or self.get_current_token()
        if tok is None:
            return True
        return time.time() + self._expiry_buffer >= tok.expires_at

    def refresh_if_needed(self, force: bool = False) -> OAuthToken:
        """Refresh the token if it's within the expiry buffer.

        Thread-safe: if another thread is already refreshing, this call
        blocks on the lock and returns the refreshed token.

        Args:
            force: If True, refresh regardless of expiry.

        Returns:
            The current (possibly refreshed) OAuthToken.

        Raises:
            NoRefreshTokenError: If no refresh token is available.
            OAuthHttpError: On HTTP errors from the OAuth endpoint.
            OAuthRefreshError: On other refresh failures.
        """
        with self._lock:
            if not force and self._cached_token is not None:
                if not self.needs_refresh(self._cached_token):
                    return self._cached_token

            return self._do_refresh()

    def force_refresh(self) -> OAuthToken:
        """Force a token refresh regardless of expiry."""
        with self._lock:
            return self._do_refresh()

    # ── Internal ───────────────────────────────────────────────────────────

    def _do_refresh(self) -> OAuthToken:
        """Execute the refresh — caller must hold ``self._lock``."""
        creds = self._read_credentials()
        refresh_token = creds.get("refresh_token", "")

        if not refresh_token:
            raise NoRefreshTokenError(f"No refresh_token in credentials file: {self._path}")

        logger.info("[OAuth] Refreshing token (refresh_token=%s…)", refresh_token[:8])

        try:
            resp = requests.post(
                CTRADER_OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise OAuthRefreshError(
                f"Network error during token refresh: {exc}",
                retry_hint=True,
            ) from exc

        if resp.status_code == 400:
            body = resp.text[:200]
            raise OAuthHttpError(
                f"OAuth 400 Bad Request: {body}",
                status_code=400,
            )

        if resp.status_code >= 500:
            body = resp.text[:200]
            raise OAuthHttpError(
                f"OAuth {resp.status_code} server error: {body}",
                status_code=resp.status_code,
            )

        if resp.status_code != 200:
            body = resp.text[:200]
            raise OAuthHttpError(
                f"OAuth unexpected status {resp.status_code}: {body}",
                status_code=resp.status_code,
            )

        data = resp.json()

        # cTrader returns accessToken / refreshToken (camelCase)
        new_access = data.get("accessToken") or data.get("access_token", "")
        new_refresh = data.get("refreshToken") or data.get("refresh_token", "")
        expires_in = data.get("expiresIn") or data.get("expires_in", 3600)

        if not new_access:
            raise OAuthRefreshError("OAuth response missing access token")

        if not new_refresh:
            # Some providers rotate refresh tokens; keep old one if not returned
            new_refresh = refresh_token

        token = OAuthToken(
            access_token=new_access,
            refresh_token=new_refresh,
            expires_at=time.time() + expires_in,
        )

        # Persist atomically
        self._write_credentials(creds, new_access, new_refresh)
        self._cached_token = token

        logger.info(
            "[OAuth] Token refreshed — access=%s… expires_at=%s",
            new_access[:8],
            datetime.fromtimestamp(token.expires_at, tz=timezone.utc).isoformat(),
        )
        return token

    def _read_credentials(self) -> dict:
        """Read and parse the credentials JSON file."""
        if not self._path.exists():
            raise OAuthRefreshError(f"Credentials file not found: {self._path}")
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise OAuthRefreshError(f"Failed to read credentials: {exc}") from exc

    def _write_credentials(
        self,
        current_creds: dict,
        new_access: str,
        new_refresh: str,
    ) -> None:
        """Atomically write updated tokens back to the credentials file.

        Writes to a .tmp file first, then renames for atomicity.
        """
        updated = dict(current_creds)
        updated["access_token"] = new_access
        updated["refresh_token"] = new_refresh
        updated["last_refreshed"] = datetime.now(timezone.utc).isoformat()

        # Write to .tmp then rename
        tmp_path = str(self._path) + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(updated, f, indent=2)
            os.rename(tmp_path, str(self._path))
        except OSError as exc:
            # Clean up tmp file if rename failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise OAuthRefreshError(f"Failed to write credentials atomically: {exc}") from exc
