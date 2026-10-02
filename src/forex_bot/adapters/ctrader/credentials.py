"""CredentialManager — atomic, file-based cTrader credential storage.

Handles:
- Load/save of ``data/.credentials`` JSON file
- Atomic write (.tmp + rename) with chmod 600
- One-time migration from ``.env`` token values → credentials file
- Placeholder detection and rejection
- Startup guard against dual-source credentials

All credential writes MUST go through this module. No other path should
write to ``data/.credentials``.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("ayumi.credentials")

# ── Constants ──────────────────────────────────────────────────────────────

CREDENTIALS_VERSION = 1

_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "new-access",
        "new-refresh",
        "REPLACE",
        "xxx",
        "***",
        "none",
        "null",
        "todo",
        "changeme",
    }
)

# Environment variable keys that hold cTrader credentials
_CTRADER_ENV_KEYS = {
    "client_id": "CTRADER_OPENAPI_CLIENT_ID",
    "client_secret": "CTRADER_OPENAPI_CLIENT_SECRET",
    "access_token": "CTRADER_OPENAPI_ACCESS_TOKEN",
    "refresh_token": "CTRADER_OPENAPI_REFRESH_TOKEN",
    "account_id": "CTRADER_OPENAPI_ACCOUNT_ID",
    "trader_login": "CTRADER_OPENAPI_TRADER_LOGIN",
}

# Keys whose VALUES are considered sensitive (never log)
_SENSITIVE_KEYS = frozenset(
    {
        "client_secret",
        "access_token",
        "refresh_token",
    }
)

# ── Exceptions ─────────────────────────────────────────────────────────────


class CredentialError(Exception):
    """Base exception for credential errors."""


class PlaceholderCredentialError(CredentialError):
    """Raised when a credential value is a known placeholder."""


class DualSourceError(CredentialError):
    """Raised when credentials exist in both .env and credentials file."""


# ── CredentialManager ──────────────────────────────────────────────────────


class CredentialManager:
    """Manage cTrader credentials in a dedicated JSON file.

    Args:
        credentials_path: Path to the credentials JSON file.
            Default: ``data/.credentials`` (relative to project root).
        env_path: Path to the ``.env`` file for migration.
            Default: ``.env`` (relative to project root).
    """

    def __init__(
        self,
        credentials_path: str | Path = "data/.credentials",
        env_path: str | Path = ".env",
    ):
        self._path = Path(credentials_path)
        self._env_path = Path(env_path)

    # ── Public API ─────────────────────────────────────────────────────────

    def load(self) -> dict:
        """Load credentials from the credentials file.

        Returns:
            Dict with credential fields (client_id, access_token, etc.).

        Raises:
            CredentialError: If the file is missing or corrupt.
        """
        if not self._path.exists():
            raise CredentialError(f"Credentials file not found: {self._path}")

        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise CredentialError(f"Failed to load credentials: {exc}") from exc

        if data.get("version") != CREDENTIALS_VERSION:
            raise CredentialError(f"Unsupported credentials version: {data.get('version')}")

        return data

    def save(self, credentials: dict) -> None:
        """Atomically write credentials to disk.

        Args:
            credentials: Dict with credential fields. Must include all keys
                from the credential file format.

        Raises:
            CredentialError: If any value is a placeholder.
        """
        self._validate_no_placeholders(credentials)

        data = {
            "version": CREDENTIALS_VERSION,
            "client_id": credentials.get("client_id", ""),
            "client_secret": credentials.get("client_secret", ""),
            "access_token": credentials.get("access_token", ""),
            "refresh_token": credentials.get("refresh_token", ""),
            "account_id": credentials.get("account_id", ""),
            "trader_login": credentials.get("trader_login", ""),
            "last_refreshed": credentials.get(
                "last_refreshed",
                datetime.now(timezone.utc).isoformat(),
            ),
        }

        self._atomic_write(data)

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Update only the token fields in an existing credentials file.

        Args:
            access_token: New access token.
            refresh_token: New refresh token.
        """
        for label, val in [
            ("access_token", access_token),
            ("refresh_token", refresh_token),
        ]:
            if self._is_placeholder(val):
                raise PlaceholderCredentialError(f"Cannot save placeholder {label}: '{val}'")

        try:
            data = self.load()
        except CredentialError:
            raise CredentialError("Cannot update tokens: credentials file does not exist. Call save() first.") from None

        data["access_token"] = access_token
        data["refresh_token"] = refresh_token
        data["last_refreshed"] = datetime.now(timezone.utc).isoformat()
        self._atomic_write(data)

    def needs_migration(self) -> bool:
        """Return True if .env still has cTrader token values that need migration.

        Checks whether the .env file contains non-placeholder values for
        CTRADER_OPENAPI_ACCESS_TOKEN or CTRADER_OPENAPI_REFRESH_TOKEN.
        """
        if not self._env_path.exists():
            return False

        env_data = self._read_env()
        for key in ("CTRADER_OPENAPI_ACCESS_TOKEN", "CTRADER_OPENAPI_REFRESH_TOKEN"):
            val = env_data.get(key, "").strip()
            if val and not self._is_placeholder(val):
                return True
        return False

    def has_dual_source(self) -> bool:
        """Return True if credentials exist in BOTH .env and credentials file.

        This indicates an incomplete migration or accidental dual-entry.
        """
        credentials_exist = self._path.exists()
        if not credentials_exist:
            return False
        return self.needs_migration()

    def migrate_from_env(self) -> dict:
        """Migrate cTrader credentials from .env to the credentials file.

        Reads all CTRADER_OPENAPI_* values from .env, writes them to the
        credentials file, then empties the token values in .env (replacing
        with a migration comment).

        Returns:
            The migrated credentials dict.

        Raises:
            CredentialError: If .env has no credentials to migrate.
            PlaceholderCredentialError: If .env contains only placeholder values.
        """
        env_data = self._read_env()

        credentials: dict = {}
        for field, env_key in _CTRADER_ENV_KEYS.items():
            credentials[field] = env_data.get(env_key, "").strip()

        # Check that we actually have something meaningful
        has_real_token = any(
            credentials.get(k) and not self._is_placeholder(credentials[k])
            for k in ("access_token", "refresh_token", "client_id")
        )

        if not has_real_token:
            raise CredentialError("No real credentials found in .env — nothing to migrate")

        # Validate non-placeholders for tokens
        for field in ("access_token", "refresh_token"):
            val = credentials.get(field, "")
            if val and self._is_placeholder(val):
                logger.warning(
                    "Skipping placeholder %s during migration: '%s'",
                    field,
                    val[:8] + "…" if val else "(empty)",
                )

        # Save to credentials file
        self.save(credentials)

        # Empty token values from .env
        self._migrate_env_tokens(env_data)

        logger.info(
            "Migrated cTrader credentials from .env to %s",
            self._path,
        )
        return credentials

    def startup_check(self) -> dict | None:
        """Run startup guard checks.

        Returns:
            None if all clear.
            Dict with 'error' key and message if a problem is detected.

        Checks:
        1. If .env still has cTrader tokens post-migration → error
        2. If credentials file is missing and .env has tokens → suggest migration
        """
        env_has_tokens = self.needs_migration()
        file_exists = self._path.exists()

        if file_exists and env_has_tokens:
            return {
                "error": (
                    "Dual source detected: credentials exist in both .env "
                    "and data/.credentials. Complete migration by running "
                    "migrate_from_env() or empty .env token values manually."
                ),
            }

        if not file_exists and env_has_tokens:
            logger.warning(
                "Credentials file missing but .env has cTrader tokens. Run migration to move to data/.credentials."
            )

        return None

    # ── Internal helpers ───────────────────────────────────────────────────

    def _atomic_write(self, data: dict) -> None:
        """Write data to the credentials file atomically (tmp + rename, chmod 600)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".credentials_tmp_",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # Set restrictive permissions before rename
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _validate_no_placeholders(self, credentials: dict) -> None:
        """Raise PlaceholderCredentialError if any sensitive value is a placeholder."""
        for key in _SENSITIVE_KEYS:
            val = credentials.get(key, "")
            if self._is_placeholder(val):
                raise PlaceholderCredentialError(f"Placeholder value detected for '{key}': '{val}'")

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        """Return True if the value looks like a placeholder."""
        if not value:
            return True
        return value in _PLACEHOLDER_VALUES or value.lower() in {p for p in _PLACEHOLDER_VALUES if p.isascii()}

    def _read_env(self) -> dict[str, str]:
        """Read the .env file and return a dict of key=value pairs."""
        if not self._env_path.exists():
            return {}

        result: dict[str, str] = {}
        try:
            with open(self._env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, _, val = line.partition("=")
                        val = val.split("#")[0].strip()  # strip inline comments
                        result[key.strip()] = val
        except OSError:
            pass
        return result

    def _migrate_env_tokens(self, env_data: dict[str, str]) -> None:
        """Mark tokens as migrated in .env WITHOUT removing the values.

        BQ-1036: Previous behaviour emptied token values during migration,
        causing repeated placeholder regressions. Tokens are now left intact
        so .env remains a reliable backup source.
        """
        if not self._env_path.exists():
            return

        logger.info("Credentials migrated to data/.credentials. Token values preserved in .env as backup.")
