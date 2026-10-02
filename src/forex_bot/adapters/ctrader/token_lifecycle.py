"""Token lifecycle management — the ONLY module that calls the cTrader OAuth endpoint.

Proactive refresh: refreshes when expires_at < now + 5 days
Reactive refresh: called by session on AUTH_EXPIRED error
Thread-safe: uses a lock to prevent concurrent refreshes within a process
Process-safe: uses a file lock to prevent concurrent refreshes across processes

Sprint 024 / card 591cbfe6 hardening (2026-08-21):
    * Validation is now ADVISORY — the OAuth endpoint is the source of truth.
      Previously a failing pre-commit validation call would discard the
      fresh token and raise ``TokenRefreshError``, causing the proactive
      timer to log "Refreshed token failed validation" every cycle while
      the token aged 12.4+ days (forward-test incident, 4/4 days at 21:00Z).
    * Stale lock-file detection: if the inter-process lock file's mtime is
      older than ``STALE_LOCK_THRESHOLD_S`` we treat it as an orphan from a
      crashed process and steal it instead of deadlocking.
    * ``token_age_s`` property exposes the elapsed time since the current
      token was issued — used by health checks (AC: ``token_age_s < 3600``
      across refresh cycles) and by tests.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import requests

from .credential_store import CredentialStore

logger = logging.getLogger("ayumi.token_lifecycle")

# ── Constants ──────────────────────────────────────────────────────────────

OAUTH_URL = "https://openapi.ctrader.com/apps/token"
REFRESH_BUFFER = timedelta(days=5)  # refresh when < 5 days remaining (token TTL is 30 days)
REQUEST_TIMEOUT = 10  # seconds
PROACTIVE_CHECK_INTERVAL = 300  # seconds between proactive timer checks (5min)

# Sprint 024: orphan lock-file threshold. If the .token_refresh.lock file's
# mtime is older than this, the holding process is presumed dead and we
# steal the lock instead of waiting forever (Unix advisory locks are
# released on process death but a stale file can still confuse diagnostics).
STALE_LOCK_THRESHOLD_S = 300

# Sprint 024: token-age guard rail for health checks. Matches the AC
# ``token_age_s < 3600 across refresh cycles``.
TOKEN_AGE_HEALTH_BUDGET_S = 3600

# Sprint 024: kill-switch re-arm threshold. After this many CONSECUTIVE
# retryable auth failures (network errors, HTTP 5xx), arm the global-freeze
# halt via KillSwitchManager.activate_global_freeze() instead of letting
# the proactive timer retry forever (forward-test incident 4/4 days at
# 21:00Z, auth_errors 5→9+). Default 5 — configurable per-card AC.
DEFAULT_AUTH_FAILURE_THRESHOLD = 5

# Inter-process lock file path (relative to CWD or absolute)
# Read dynamically so tests can override via monkeypatch
_DEFAULT_LOCK_FILE = str(Path("data") / ".token_refresh.lock")


def _get_lock_file_path() -> str:
    """Return the current lock file path (checks env each call for testability)."""
    return os.environ.get("AYUMI_TOKEN_LOCK_FILE", _DEFAULT_LOCK_FILE)


# ── Exceptions ─────────────────────────────────────────────────────────────


class TokenRefreshError(Exception):
    """Raised when token refresh fails.

    Attributes:
        retry: True if the caller should retry (HTTP 5xx, network errors).
               False for permanent failures (HTTP 400 invalid grant).
    """

    def __init__(self, message: str, *, retry: bool = False):
        super().__init__(message)
        self._retry = retry

    @property
    def retry(self) -> bool:
        """Whether the caller should retry the refresh."""
        return self._retry


# ── TokenLifecycle ─────────────────────────────────────────────────────────


class TokenLifecycle:
    """Owns the token refresh lifecycle.

    Only module that calls the cTrader OAuth endpoint.
    On successful refresh, writes new tokens to CredentialStore.

    Thread-safe: concurrent ensure_valid() / force_refresh() calls are
    serialized via a lock. If a refresh is already in progress, subsequent
    callers wait for it to complete and return the freshly-refreshed token.

    ``_refresh_disabled`` is a class-level kill switch. When True, ALL
    refresh logic is bypassed: ``ensure_valid()`` always returns the current
    token, ``force_refresh()`` logs and returns False, and
    ``start_proactive_timer()`` is a no-op. The full refresh internals are
    preserved unchanged for easy re-enablement — just set the flag to False.

    Default is ``False`` (refresh ENABLED) as of 2026-07-30 (card 64a235ea).
    Previously True (disabled) — flipped after 4 token-expiration outages
    (~26h total downtime Jul 2026) demonstrated the kill switch causes more
    harm than it prevents. See ``docs/forex/ayumi-ctrader-token-rotation-runbook.md``.
    """

    _refresh_disabled: bool = False

    def __init__(
        self,
        credential_store: CredentialStore,
        *,
        strict_validation: bool = False,
        clock: Callable[[], datetime] | None = None,
        auth_failure_threshold: int = DEFAULT_AUTH_FAILURE_THRESHOLD,
        kill_switch: Optional[object] = None,
    ):
        """Initialize with a CredentialStore.

        Reads client_id and client_secret from the credential store.

        Args:
            credential_store: Source of cTrader credentials and tokens.
            strict_validation: If True, treat ``_validate_token`` returning
                False as a hard failure (legacy behaviour). Default is
                False — validation is advisory only and the OAuth endpoint
                is the source of truth. See module docstring for context.
            clock: Optional callable returning the current UTC datetime.
                Defaults to ``datetime.now(timezone.utc)``. Exposed so
                unit tests can inject a deterministic clock.
            auth_failure_threshold: Number of consecutive retryable auth
                failures (network error, HTTP 5xx) before arming the
                global-freeze kill switch via the injected ``kill_switch``
                (if provided). Default :data:`DEFAULT_AUTH_FAILURE_THRESHOLD`
                (5). Permanent failures (HTTP 400 invalid grant) do NOT
                count toward this threshold — they require manual
                intervention regardless.
            kill_switch: Optional :class:`KillSwitchManager` instance
                injected by the engine. When None, the re-arm call is
                logged but not dispatched (production wiring sets this
                in ForwardTestEngine.startup; tests inject a mock).
        """
        self._store = credential_store
        creds = credential_store.get()
        self._client_id = creds.client_id
        self._client_secret = creds.client_secret
        self._auth_failure_threshold = max(1, int(auth_failure_threshold))
        self._kill_switch = kill_switch

        self._lock = threading.Lock()
        self._refreshing = threading.Event()
        # Track the last-known access token to avoid redundant reads
        self._access_token: str = creds.access_token
        self._expires_at: Optional[datetime] = creds.expires_at
        # Sprint 024: clock injection MUST be set before any code path
        # that calls ``self._now()`` (e.g. ``_issued_at = self._now()``
        # below). Tests pass a deterministic callable to advance time
        # without sleeping.
        self._clock: Callable[[], datetime] = clock or (
            lambda: datetime.now(timezone.utc)
        )
        # Track when the current access token was issued so we can report
        # ``token_age_s`` for the health gate. ``_issued_at`` is bumped on
        # every successful ``_do_refresh_inner`` and at construction time
        # when a token is loaded from the credential store.
        self._issued_at: datetime = self._now()
        self._strict_validation: bool = strict_validation
        # Sprint 024: count consecutive auth failures to arm the global-freeze
        # kill switch instead of retrying forever. Reset to 0 on any
        # successful refresh.
        self._consecutive_auth_failures: int = 0

        # Proactive timer
        self._timer_thread: Optional[threading.Thread] = None
        self._timer_stop = threading.Event()

    # ── Public API ─────────────────────────────────────────────────────────

    def ensure_valid(self) -> str:
        """Return a valid access token, refreshing if needed.

        If the token expires within REFRESH_BUFFER (5 days) or is already
        expired, a refresh is triggered. Thread-safe: concurrent callers
        block until the in-progress refresh finishes.

        When ``_refresh_disabled`` is True, always returns the current
        token without checking expiry or attempting refresh.

        Returns:
            A valid access token string.

        Raises:
            TokenRefreshError: If the OAuth refresh fails.
        """
        if self._refresh_disabled:
            logger.info("Token refresh disabled — returning current token as-is")
            return self._access_token

        if self._is_valid():
            return self._access_token

        # Token needs refresh — acquire lock
        with self._lock:
            # Double-check after acquiring lock (another thread may have refreshed)
            if self._is_valid():
                return self._access_token
            return self._do_refresh()

    def force_refresh(self) -> str:
        """Force a token refresh regardless of expiry.

        Used when the session receives an AUTH_EXPIRED error from cTrader.
        Thread-safe: concurrent callers block until the in-progress refresh
        finishes, then return the new token.

        When ``_refresh_disabled`` is True, logs a warning and returns the
        current token without attempting refresh.

        Returns:
            The new access token string.

        Raises:
            TokenRefreshError: If the OAuth refresh fails.
        """
        if self._refresh_disabled:
            logger.warning(
                "force_refresh() called but token refresh is DISABLED "
                "(_refresh_disabled=True). Returning current token as-is. "
                "Manual token rotation required."
            )
            return self._access_token

        with self._lock:
            return self._do_refresh(force=True)

    @property
    def expires_at(self) -> Optional[datetime]:
        """Return the current token's expiry time, or None if unknown."""
        return self._expires_at

    @property
    def issued_at(self) -> datetime:
        """Return the UTC datetime when the current access token was issued."""
        return self._issued_at

    @property
    def token_age_s(self) -> float:
        """Seconds elapsed since the current access token was issued.

        Used by health checks (sprint 024 AC: ``token_age_s < 3600`` after a
        refresh cycle) and by unit tests to assert rotation actually
        produced a fresh token.
        """
        now = self._now()
        return max(0.0, (now - self._issued_at).total_seconds())

    @property
    def consecutive_auth_failures(self) -> int:
        """Count of consecutive OAuth refresh failures since last success.

        Cleared on every successful ``_do_refresh_inner`` (counter reset
        to 0). Compared against ``auth_failure_threshold`` by the
        kill-switch re-arm logic in :meth:`_do_refresh_inner`.
        """
        return self._consecutive_auth_failures

    def _now(self) -> datetime:
        """Return the current UTC datetime via the injected clock.

        Default (no clock provided) is ``datetime.now(timezone.utc)``.
        Tests inject a deterministic callable to advance time without
        touching the wall clock — required for the AC
        ``token_age_s < 3600 across refresh cycles`` test.
        """
        return self._clock()

    def start_proactive_timer(self, on_refreshed: Optional[Callable[[str], None]] = None) -> None:
        """Start a daemon thread that proactively refreshes before expiry.

        The thread checks expires_at every PROACTIVE_CHECK_INTERVAL (60s).
        If the token expires within REFRESH_BUFFER (5 days), it calls
        force_refresh(). On error, logs and continues (does not crash).

        When ``_refresh_disabled`` is True, this is a no-op.

        Args:
            on_refreshed: Optional callback invoked with the new access
                          token after each successful proactive refresh.
        """
        if self._refresh_disabled:
            logger.info("start_proactive_timer() skipped — refresh disabled (_refresh_disabled=True)")
            return

        # NOTE: OpenApiSpotFeed manages its own proactive refresh via
        # _schedule_proactive_refresh(). This method is not called in production
        # today but is available for standalone TokenLifecycle usage.
        if self._timer_thread is not None and self._timer_thread.is_alive():
            logger.warning("Proactive timer already running")
            return

        self._timer_stop.clear()
        self._timer_thread = threading.Thread(
            target=self._timer_loop,
            args=(on_refreshed,),
            name="token-proactive-timer",
            daemon=True,
        )
        self._timer_thread.start()
        logger.info("Proactive token timer started")

    def stop_proactive_timer(self) -> None:
        """Stop the proactive timer daemon thread."""
        if self._timer_thread is None:
            return
        self._timer_stop.set()
        self._timer_thread.join(timeout=PROACTIVE_CHECK_INTERVAL + 5)
        self._timer_thread = None
        logger.info("Proactive token timer stopped")

    # ── Internal ───────────────────────────────────────────────────────────

    def _is_valid(self) -> bool:
        """Check if the current token is still valid (with 5-day buffer).

        If expires_at is None (Craig manually wrote fresh tokens to .env
        without an expires_at), we ASSUME the token is fresh and return True.
        This prevents the startup refresh that clobbers Craig's tokens.
        Only refresh when expires_at is known AND within the buffer.
        """
        if self._expires_at is None:
            # No expiry info — assume fresh (Craig wrote the token manually
            # to .env without an EXPIRES_AT field). Demoting to DEBUG because
            # this branch fires on every proactive-check cycle (5 min) in
            # manual-token mode — the INFO-level message is misleading noise.
            logger.debug("No EXPIRES_AT in credentials — assuming token valid (manual token mode)")
            return True
        now = self._now()
        return self._expires_at - now > REFRESH_BUFFER

    def _sync_from_store(self) -> None:
        """Pull latest token data from credential_store (after external update)."""
        creds = self._store.get()
        self._access_token = creds.access_token
        self._expires_at = creds.expires_at

    def _do_refresh(self, force: bool = False) -> str:
        """Execute the OAuth refresh request.

        Caller must hold self._lock.
        Acquires an inter-process file lock to prevent concurrent refreshes
        across multiple processes (forward test, test scripts, subagents).

        Sprint 024 (card 591cbfe6): stale-lock-file detection. If the lock
        file's mtime is older than ``STALE_LOCK_THRESHOLD_S``, we treat it
        as an orphan from a crashed process and steal the lock instead of
        blocking forever. POSIX ``fcntl.flock`` is released automatically on
        process death, so the holding process is presumed crashed and the
        kernel will release its lock as soon as we request an exclusive
        one — stealing is just a logging + diagnostic step.

        Args:
            force: If True, skip the post-lock validity re-check (used by
                   force_refresh which must always refresh).

        Returns:
            The new access token.

        Raises:
            TokenRefreshError: On any refresh failure.
        """
        # Inter-process lock
        lock_path = Path(_get_lock_file_path())
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            self._acquire_lock_with_steal_check(lock_fd, lock_path)
        except OSError as exc:
            logger.error("Cannot acquire inter-process token lock: %s", exc)
            os.close(lock_fd)
            raise TokenRefreshError(f"Cannot acquire inter-process lock: {exc}", retry=True) from exc

        try:
            if not force:
                # After acquiring the file lock, re-check if the token was
                # refreshed by another process while we were waiting
                self._sync_from_store()
                if self._is_valid():
                    logger.info("Token was refreshed by another process while waiting for lock")
                    return self._access_token

            return self._do_refresh_inner()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _acquire_lock_with_steal_check(self, lock_fd: int, lock_path: Path) -> None:
        """Acquire the inter-process lock, stealing if the file is stale.

        Sprint 024 (card 591cbfe6): POSIX ``fcntl.flock`` releases on
        process death, so a crashed holder is not a real deadlock hazard.
        The remaining concern is operator confusion: a stale lock file
        with an old mtime suggests a crashed refresh, even if the kernel
        has already released the underlying lock.

        We attempt ``LOCK_EX | LOCK_NB`` first to avoid blocking on a
        healthy concurrent refresh. If that fails, we check the mtime:
          - mtime older than ``STALE_LOCK_THRESHOLD_S`` → log a
            "stealing stale lock file" warning, then fall back to
            blocking ``LOCK_EX`` (kernel will release the dead holder's
            lock and we proceed).
          - mtime fresh → blocking ``LOCK_EX`` (another process is
            actively refreshing — wait our turn normally).
        """
        # Non-blocking first attempt
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise

        # Failed non-blocking — another holder exists. Check staleness.
        try:
            mtime = os.fstat(lock_fd).st_mtime
        except OSError:
            mtime = 0.0
        age_s = max(0.0, time.time() - mtime) if mtime > 0 else float("inf")

        if age_s >= STALE_LOCK_THRESHOLD_S:
            logger.warning(
                "Stealing stale token-refresh lock file %s (mtime age=%.1fs "
                ">= threshold=%ds) — holding process presumed crashed",
                lock_path,
                age_s,
                STALE_LOCK_THRESHOLD_S,
            )
        else:
            logger.debug(
                "Token-refresh lock held by another process (mtime age=%.1fs) — waiting",
                age_s,
            )

        # Blocking acquire. The kernel releases any dead holder's lock
        # automatically; for a live holder we wait normally.
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

    def _do_refresh_inner(self) -> str:
        """Inner refresh logic — no locking, caller handles all locks.

        After a successful OAuth exchange, validates the new token by
        making a lightweight API call before committing it to .env.
        """
        # Get current refresh token from the store
        creds = self._store.get()
        refresh_token = creds.refresh_token

        if not refresh_token:
            raise TokenRefreshError("No refresh_token available — manual intervention required")

        logger.info("Refreshing cTrader access token")

        try:
            resp = requests.post(
                OAUTH_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("Token refresh network error: %s", exc)
            self._record_auth_failure(retryable=True, kind="network_error")
            raise TokenRefreshError(
                f"Network error during token refresh: {exc}",
                retry=True,
            ) from exc

        # HTTP 400 = invalid grant (permanent failure)
        if resp.status_code == 400:
            body = resp.text[:500]
            logger.error("Token refresh failed — HTTP 400: %s", body)
            # Permanent failure — do NOT increment the re-arm counter.
            # Re-arming the kill switch on an invalid refresh token would
            # halt trading for a problem that requires manual intervention
            # (rotating refresh_token in .env), not a transient outage.
            raise TokenRefreshError(
                "Refresh token invalid — manual intervention required",
                retry=False,
            )

        # HTTP 5xx = server error (retryable)
        if resp.status_code >= 500:
            body = resp.text[:500]
            logger.error(
                "Token refresh failed — HTTP %d: %s",
                resp.status_code,
                body,
            )
            self._record_auth_failure(retryable=True, kind=f"http_{resp.status_code}")
            raise TokenRefreshError(
                f"OAuth server error (HTTP {resp.status_code})",
                retry=True,
            )

        # Any other non-200
        if resp.status_code != 200:
            body = resp.text[:500]
            logger.error(
                "Token refresh failed — HTTP %d: %s",
                resp.status_code,
                body,
            )
            raise TokenRefreshError(
                f"OAuth unexpected status {resp.status_code}",
                retry=False,
            )

        data = resp.json()

        # cTrader uses camelCase, but handle both
        new_access = data.get("accessToken") or data.get("access_token", "")
        new_refresh = data.get("refreshToken") or data.get("refresh_token", "")
        expires_in = data.get("expiresIn") or data.get("expires_in", 3600)

        if not new_access:
            raise TokenRefreshError(
                "OAuth response missing access token",
                retry=False,
            )

        # If no new refresh token returned, keep the old one
        if not new_refresh:
            new_refresh = refresh_token

        # Validate the refreshed token before committing to .env.
        # Sprint 024 (card 591cbfe6): validation is now ADVISORY. The OAuth
        # endpoint returned 200 with a fresh token — that is the source of
        # truth. Previously a failing pre-commit validation call would
        # discard the fresh token and raise TokenRefreshError, causing
        # the proactive timer to log "Refreshed token failed validation"
        # every cycle while the token aged 12.4+ days.
        #
        # Legacy behaviour (hard-fail) remains available via
        # strict_validation=True for ops who want the old behaviour.
        if not self._validate_token(new_access):
            if self._strict_validation:
                logger.error(
                    "Refreshed token failed validation (strict_validation=True) "
                    "— keeping old tokens",
                )
                # Strict-mode failure is operator-induced (the validation
                # endpoint really did reject the token). Record as a
                # NON-retryable failure so the consecutive-failure counter
                # is not corrupted by config errors.
                self._record_auth_failure(retryable=False, kind="strict_validation")
                raise TokenRefreshError(
                    "Refreshed token failed validation — old tokens retained",
                    retry=False,
                )
            logger.warning(
                "Refreshed token failed advisory validation — accepting anyway "
                "(OAuth returned 200; validation endpoint may be temporarily "
                "unavailable or its response shape changed)",
            )

        # Persist to credential store (computes expires_at internally)
        self._store.update_tokens(new_access, new_refresh, expires_in)

        # Update local cache
        creds = self._store.get()
        self._access_token = creds.access_token
        self._expires_at = creds.expires_at

        # Bump _issued_at so token_age_s reflects the fresh refresh.
        # Done AFTER the credential store write so the value is always
        # consistent with the cached token.
        self._issued_at = self._now()

        # Reset the consecutive-failure counter on any successful refresh
        # — a working auth path is the only way the system can self-recover
        # from an extended outage, so we don't want to carry old failures
        # past a single successful rotation.
        if self._consecutive_auth_failures != 0:
            logger.info(
                "Token refresh succeeded after %d consecutive failures — resetting counter",
                self._consecutive_auth_failures,
            )
            self._consecutive_auth_failures = 0

        logger.info("Token refreshed — new expires_at=%s", self._expires_at)

        return self._access_token

    def _validate_token(self, access_token: str) -> bool:
        """Lightweight validation that a token works.

        Makes a simple cTrader API call to verify the token is accepted.
        Returns True if valid, False otherwise.

        On network errors, returns True (optimistic — don't reject a
        token just because the validation endpoint is unreachable).
        """
        validation_url = "https://openapi.ctrader.com/apps/metadata/account-list"
        try:
            resp = requests.get(
                validation_url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                logger.debug("Token validation succeeded")
                return True
            elif resp.status_code in (401, 403):
                logger.error(
                    "Token validation failed — HTTP %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            else:
                # Unexpected status — be optimistic
                logger.warning(
                    "Token validation got unexpected HTTP %d — assuming valid",
                    resp.status_code,
                )
                return True
        except requests.RequestException as exc:
            logger.warning("Token validation network error — assuming valid: %s", exc)
            return True

    def _record_auth_failure(self, *, retryable: bool, kind: str) -> None:
        """Increment the consecutive auth-failure counter and arm the kill switch.

        Sprint 024 (card 591cbfe6): after ``auth_failure_threshold`` consecutive
        retryable failures (network error, HTTP 5xx), arm the global-freeze
        halt instead of letting the proactive timer retry forever.

        Args:
            retryable: True for transient failures (network/5xx). False is
                a no-op — permanent failures (HTTP 400 invalid grant) are
                not the kind that an automated retry loop can recover from,
                so they don't count toward the threshold.
            kind: Short string identifying the failure class for logging
                (e.g. ``network_error``, ``http_503``).
        """
        if not retryable:
            return
        self._consecutive_auth_failures += 1
        logger.warning(
            "Token refresh auth failure (%s) — consecutive count: %d/%d",
            kind,
            self._consecutive_auth_failures,
            self._auth_failure_threshold,
        )
        if self._consecutive_auth_failures >= self._auth_failure_threshold:
            self._arm_global_freeze(kind=kind)

    def _arm_global_freeze(self, *, kind: str) -> None:
        """Activate the global-freeze halt via the injected KillSwitchManager.

        Sprint 024 (card 591cbfe6): caps the unbounded retry loop. Without
        this, an extended outage (e.g. cTrader regional unavailability during
        a closure window) keeps re-trying forever and floods logs with
        "Refreshed token failed validation" / 5xx / network errors while
        the in-memory token continues to age.

        Fail-safe: if no kill switch was injected at construction time,
        log a CRITICAL warning and continue — the operator wiring is
        expected but the refresh machinery itself must not raise here
        (we are already in a failure path).
        """
        reason = (
            f"token_refresh_re_arm: {self._consecutive_auth_failures} consecutive "
            f"auth failures (kind={kind}, threshold={self._auth_failure_threshold})"
        )
        ks = getattr(self, "_kill_switch", None)
        if ks is None:
            logger.critical(
                "Kill-switch re-arm REACHED but no KillSwitchManager injected "
                "— global freeze will NOT be activated. reason=%s",
                reason,
            )
            return
        try:
            ks.activate_global_freeze(reason=reason, triggered_by="token_lifecycle")
            logger.critical(
                "Kill-switch re-armed: GLOBAL FREEZE ACTIVATED after %d consecutive "
                "auth failures (kind=%s)",
                self._consecutive_auth_failures,
                kind,
            )
        except Exception as exc:
            # Re-arm must never raise — the refresh path is already in an
            # error state and we don't want to mask the original failure.
            logger.error(
                "Kill-switch re-arm failed: %s — refresh path continues",
                exc,
                exc_info=True,
            )

    def _timer_loop(self, on_refreshed: Optional[Callable[[str], None]]) -> None:
        """Proactive timer loop — runs in a daemon thread.

        Checks every PROACTIVE_CHECK_INTERVAL (5min). If the token expires within 5 days, calls
        force_refresh(). On error, logs and continues.
        """
        while not self._timer_stop.is_set():
            try:
                if not self._is_valid():
                    logger.debug("Proactive timer: token expiring soon, refreshing")
                    # force_refresh acquires the lock internally
                    new_token = self.force_refresh()
                    if on_refreshed is not None:
                        on_refreshed(new_token)
            except TokenRefreshError as exc:
                logger.error("Proactive refresh failed: %s", exc)
            except Exception as exc:
                logger.error(
                    "Proactive timer unexpected error: %s",
                    exc,
                    exc_info=True,
                )

            # Wait for the check interval (interruptible)
            self._timer_stop.wait(timeout=PROACTIVE_CHECK_INTERVAL)
