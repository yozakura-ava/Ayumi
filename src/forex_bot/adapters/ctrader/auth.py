"""CTraderAuth — unified authentication facade for cTrader OpenAPI.

Composes CredentialManager + TokenManager into a single interface.
Does NOT duplicate TokenManager's OAuth refresh or validation logic;
delegates to it instead.

Usage::

    auth = CTraderAuth.create()  # auto-migrates from .env if needed
    feed = OpenApiSpotFeed(
        ctid_account_id=auth.account_id,
        client_id=auth.client_id,
        client_secret=auth.client_secret,
        access_token=auth.access_token,
        refresh_token=auth.refresh_token,
    )
"""

from __future__ import annotations

import logging
from pathlib import Path

from .credentials import (
    CredentialError,
    CredentialManager,
)
from .token_manager import TokenManager


class DualSourceError(Exception):
    """Raised when cTrader credentials exist in both .env and data/.credentials."""


logger = logging.getLogger("ayumi.ctrader_auth")

# ── Project root detection ─────────────────────────────────────────────────


def _project_root() -> Path:
    """Return the project root (4 levels up from this file).

    This module lives at src/forex_bot/adapters/ctrader/auth.py, so the
    project root is parents[4].
    """
    return Path(__file__).resolve().parents[4]


# ── CTraderAuth ────────────────────────────────────────────────────────────


class CTraderAuth:
    """Unified cTrader authentication facade.

    Wraps existing TokenManager (OAuth refresh, validation, tracking)
    and CredentialManager (secure file-based storage, migration).

    Do NOT construct directly — use ``CTraderAuth.create()`` factory method.
    """

    def __init__(
        self,
        credential_mgr: CredentialManager,
        token_mgr: TokenManager,
        credentials: dict,
    ):
        self._cred_mgr = credential_mgr
        self._token_mgr = token_mgr
        self._credentials = credentials

    # ── Factory ────────────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        credentials_path: str | Path | None = None,
        env_path: str | Path | None = None,
        token_state_path: str | Path | None = None,
        auto_migrate: bool = True,
    ) -> CTraderAuth:
        """Create a CTraderAuth instance, auto-migrating from .env if needed.

        Args:
            credentials_path: Path to credentials file. Default: data/.credentials
            env_path: Path to .env file. Default: .env
            token_state_path: Path to token state file. Default: data/token_state.json
            auto_migrate: If True, migrate from .env when credentials file is missing.

        Returns:
            Configured CTraderAuth instance.

        Raises:
            CredentialError: If credentials cannot be loaded or migrated.
        """
        root = _project_root()

        cred_path = Path(credentials_path) if credentials_path else root / "data" / ".credentials"
        env_p = Path(env_path) if env_path else root / ".env"
        token_path = Path(token_state_path) if token_state_path else root / "data" / "token_state.json"

        cred_mgr = CredentialManager(
            credentials_path=cred_path,
            env_path=env_p,
        )
        token_mgr = TokenManager(
            token_path=str(token_path),
            env_path=str(env_p),
        )

        # Try loading credentials file
        credentials: dict | None = None
        try:
            credentials = cred_mgr.load()
            logger.debug("Loaded credentials from %s", cred_path)
        except CredentialError:
            if not auto_migrate:
                raise CredentialError("Credentials file not found and auto_migrate=False") from None

        # Auto-migrate from .env if needed
        if credentials is None and auto_migrate:
            if cred_mgr.needs_migration():
                logger.info("Auto-migrating credentials from .env to %s", cred_path)
                try:
                    credentials = cred_mgr.migrate_from_env()
                except CredentialError as exc:
                    raise CredentialError(f"Auto-migration failed: {exc}") from exc
            else:
                raise CredentialError(
                    "No credentials found in .env or credentials file. Configure cTrader credentials first."
                )

        # Startup guard: check for dual source
        guard_result = cred_mgr.startup_check()
        if guard_result:
            logger.error("%s", guard_result["error"])

        return cls(
            credential_mgr=cred_mgr,
            token_mgr=token_mgr,
            credentials=credentials,
        )

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def access_token(self) -> str:
        return self._credentials.get("access_token", "")

    @property
    def refresh_token(self) -> str:
        return self._credentials.get("refresh_token", "")

    @property
    def client_id(self) -> str:
        return self._credentials.get("client_id", "")

    @property
    def client_secret(self) -> str:
        return self._credentials.get("client_secret", "")

    @property
    def account_id(self) -> int:
        val = self._credentials.get("account_id", "0")
        return int(val) if val else 0

    @property
    def trader_login(self) -> str:
        return self._credentials.get("trader_login", "")

    @property
    def token_manager(self) -> TokenManager:
        """Direct access to the underlying TokenManager for refresh/validation."""
        return self._token_mgr

    @property
    def credential_manager(self) -> CredentialManager:
        """Direct access to the underlying CredentialManager."""
        return self._cred_mgr

    # ── Authentication methods ─────────────────────────────────────────────

    def app_authenticate(self, client) -> None:
        """Perform app-level authentication with the cTrader client.

        Args:
            client: A ``ctrader_open_api.Client`` instance.

        Delegates to the existing protocol: ProtoOAAppAuthReq.
        """
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAApplicationAuthReq,
        )

        req = ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        client.send(req)
        logger.debug("App auth sent (client_id=%s…)", self.client_id[:8])

    def account_authenticate(self, client, account_id: int | None = None) -> None:
        """Perform account-level authentication with the cTrader client.

        Args:
            client: A ``ctrader_open_api.Client`` instance.
            account_id: Override account ID. Defaults to self.account_id.

        Delegates to the existing protocol: ProtoOAAccountAuthReq.
        """
        from ctrader_open_api.messages.OpenApiMessages_pb2 import (
            ProtoOAAccountAuthReq,
        )

        acct_id = account_id or self.account_id
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = acct_id
        req.accessToken = self.access_token
        client.send(req)
        logger.debug("Account auth sent (account=%d)", acct_id)

    def load_credentials(self) -> dict:
        """Re-load credentials from disk and validate.

        Returns:
            Fresh credentials dict.

        Raises:
            CredentialError: On load or validation failure.
            DualSourceError: If dual source detected.
        """
        credentials = self._cred_mgr.load()

        # Check for dual source
        if self._cred_mgr.has_dual_source():
            raise DualSourceError("Credentials found in both .env and credentials file")

        self._credentials = credentials
        return credentials

    def update_tokens(self, access_token: str, refresh_token: str) -> None:
        """Update stored tokens after a successful refresh.

        Updates both the credentials file and the in-memory state.

        Args:
            access_token: New access token.
            refresh_token: New refresh token.
        """
        self._cred_mgr.update_tokens(access_token, refresh_token)
        self._credentials["access_token"] = access_token
        self._credentials["refresh_token"] = refresh_token
        logger.info("Tokens updated in credentials store")

    def validate_startup(self) -> dict:
        """Run TokenManager's startup validation on the current access token.

        Returns:
            TokenManager's status dict (status, message, days_remaining, etc.).
        """
        return self._token_mgr.validate_on_startup(self.access_token)

    def refresh_if_needed(self, warning_days: int = 7) -> str | None:
        """Proactively refresh the token if it's within warning_days of expiry.

        Delegates to TokenManager.refresh_if_needed().

        Args:
            warning_days: Days before expiry to trigger refresh.

        Returns:
            New access token, or None if refresh wasn't needed / failed.
        """
        new_token = self._token_mgr.refresh_if_needed(
            client_id=self.client_id,
            client_secret=self.client_secret,
            refresh_token=self.refresh_token,
            warning_days=warning_days,
        )
        if new_token:
            # TokenManager already updated .env via _atomic_env_write
            # Update our credentials file too
            self._credentials["access_token"] = new_token
            # Note: refresh_token may have changed too, but TokenManager
            # handles that via _atomic_env_write. We'll reload from file
            # on next load_credentials() call.
        return new_token
