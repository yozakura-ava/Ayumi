"""IBKR Flex Web Service connector — read-only portfolio snapshot.

========================================================================
SECURITY: READ-ONLY BY CONSTRUCTION
========================================================================
This connector is INTENTIONALLY read-only. It exposes ONLY data-retrieval
operations against the IBKR Flex Web Service. It MUST NEVER gain the
ability to place orders, modify positions, or change account state.

The Flex Web Service endpoints used here are GET-only HTTPS requests
that return statement XML. There are no POST/PUT/DELETE operations,
no session tokens capable of order routing, and no trade execution
methods on this class. Adding order placement here would require
deliberate, reviewable code — it is not a refactoring accident away.

If you need order routing, use the cTrader adapter. This module exists
to feed Portfolio Intelligence with account snapshots, nothing more.
========================================================================

Two-step fetch flow:
    1. SendRequest → returns ReferenceCode (or error)
    2. wait 3 s → GetStatement?q={ref_code} → returns statement XML

Errors:
    1014 — Query invalid / not found            → IBKRFlexQueryInvalidError
    1015 — Statement still being prepared       → handled via polling
    1019 — Token expired / invalid              → IBKRFlexTokenExpiredError

Credentials come from `.env` via `dotenv_values`:
    IBKR_FLEX            — Flex Web Service token
    IBKR_FLEX_QUERY_ID   — Query ID (e.g. "1600392")
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

from dotenv import dotenv_values

logger = logging.getLogger("ayumi.portfolio_intelligence.ibkr")


# ── Constants ────────────────────────────────────────────────────────────────

# IBKR's ndcdyn host is the documented Flex Web Service entry point.
FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"

SEND_REQUEST_URL = f"{FLEX_BASE_URL}/SendRequest"
GET_STATEMENT_URL = f"{FLEX_BASE_URL}/GetStatement"

# Wait between SendRequest and GetStatement (IBKR's documented minimum).
STATEMENT_PREP_DELAY_SECONDS = 3.0

# How long to keep polling when the statement is still being prepared.
POLL_MAX_WAIT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 5.0

# IBKR Flex error codes (see Flex Web Service docs).
ERR_QUERY_NOT_FOUND = 1014
ERR_STATEMENT_NOT_READY = 1015
ERR_TOKEN_EXPIRED = 1019

# HTTP / network knobs.
HTTP_TIMEOUT_SECONDS = 30.0
USER_AGENT = "ayumi-portfolio_intelligence/1.0 (+read-only)"

# Default env keys.
ENV_TOKEN = "IBKR_FLEX"  # noqa: S105
ENV_QUERY_ID = "IBKR_FLEX_QUERY_ID"
ENV_PATH_DEFAULT = ".env"


# ── Errors ───────────────────────────────────────────────────────────────────


class IBKRFlexError(RuntimeError):
    """Base class for IBKR Flex Web Service errors."""


class IBKRFlexAuthError(IBKRFlexError):
    """Token missing, expired, or rejected (Flex error 1019)."""


class IBKRFlexQueryInvalidError(IBKRFlexError):
    """Query ID not found or rejected (Flex error 1014)."""


class IBKRFlexStatementNotReady(IBKRFlexError):
    """Statement still being prepared (Flex error 1015) past poll budget."""


class IBKRFlexConfigError(IBKRFlexError):
    """Required credential missing from .env."""


# ── Result dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountInfo:
    """Top-level account metadata from a Flex statement."""

    account_id: str
    period: str
    ending_cash: float
    from_date: str = ""
    to_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "period": self.period,
            "endingCash": self.ending_cash,
            "fromDate": self.from_date,
            "toDate": self.to_date,
        }


@dataclass(frozen=True)
class Position:
    """A single open position from a Flex statement."""

    symbol: str
    quantity: float
    position_value: float
    asset_category: str
    currency: str
    cost_basis: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "positionValue": self.position_value,
            "assetCategory": self.asset_category,
            "currency": self.currency,
            "costBasis": self.cost_basis,
            "markPrice": self.mark_price,
            "unrealizedPnl": self.unrealized_pnl,
        }


@dataclass(frozen=True)
class CashRow:
    """A single cash row, denominated in `currency`."""

    currency: str
    ending_cash: float
    ending_settled_cash: float = 0.0
    ending_trade_cash: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "endingCash": self.ending_cash,
            "endingSettledCash": self.ending_settled_cash,
            "endingTradeCash": self.ending_trade_cash,
        }


@dataclass(frozen=True)
class FlexStatement:
    """Structured result of a full Flex statement fetch."""

    account_info: AccountInfo
    positions: list[Position] = field(default_factory=list)
    cash_report: list[CashRow] = field(default_factory=list)
    reference_code: str = ""
    fetched_at_epoch: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_info": self.account_info.to_dict(),
            "positions": [p.to_dict() for p in self.positions],
            "cash_report": [c.to_dict() for c in self.cash_report],
            "reference_code": self.reference_code,
            "fetched_at_epoch": self.fetched_at_epoch,
        }


# ── Connector ────────────────────────────────────────────────────────────────


class IBKRFlexConnector:
    """Read-only IBKR Flex Web Service connector.

    Authentication is via a Flex Token + Query ID loaded from `.env`
    via `dotenv_values`. Network access is GET-only against the Flex
    Web Service. There are no methods to place, modify, or cancel orders.

    Typical usage:
        conn = IBKRFlexConnector()           # reads .env in cwd
        stmt = conn.fetch_statement()
        for pos in conn.get_positions():
            print(pos["symbol"], pos["positionValue"])

    The connector is stateless across calls except for an in-memory
    cache of the last fetched statement (used by the convenience
    `get_positions` / `get_cash_summary` / `get_account_nav` accessors).
    """

    def __init__(
        self,
        env_path: str | Path = ENV_PATH_DEFAULT,
        token: str | None = None,
        query_id: str | None = None,
        *,
        poll_max_wait_seconds: float = POLL_MAX_WAIT_SECONDS,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        prep_delay_seconds: float = STATEMENT_PREP_DELAY_SECONDS,
        timeout_seconds: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        """Initialize the connector.

        Args:
            env_path: Path to .env file. Defaults to ".env" in cwd.
            token: Optional override for IBKR_FLEX (testing only).
            query_id: Optional override for IBKR_FLEX_QUERY_ID (testing only).
            poll_max_wait_seconds: Max seconds to poll for statement readiness.
            poll_interval_seconds: Seconds between polls.
            prep_delay_seconds: Sleep between SendRequest and first GetStatement.
            timeout_seconds: urllib request timeout.
        """
        self._env_path = Path(env_path)
        self._poll_max_wait = poll_max_wait_seconds
        self._poll_interval = poll_interval_seconds
        self._prep_delay = prep_delay_seconds
        self._timeout = timeout_seconds

        env_data = self._load_env()
        self._token: str = token or env_data.get(ENV_TOKEN, "")
        self._query_id: str = query_id or env_data.get(ENV_QUERY_ID, "")

        if not self._token:
            raise IBKRFlexConfigError(f"Missing {ENV_TOKEN} in {self._env_path} (or pass token= explicitly)")
        if not self._query_id:
            raise IBKRFlexConfigError(f"Missing {ENV_QUERY_ID} in {self._env_path} (or pass query_id= explicitly)")

        # Last successful fetch (populated by fetch_statement()).
        self._last_statement: FlexStatement | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def fetch_statement(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Fetch a fresh Flex statement and return a structured dict.

        Returns a dict with keys: account_info, positions, cash_report,
        reference_code, fetched_at_epoch. The structured dataclasses are
        also cached on the connector instance for the convenience accessors.

        Args:
            force_refresh: If True, always re-fetch even if a cached
                statement exists.

        Raises:
            IBKRFlexAuthError: Token expired/invalid (1019).
            IBKRFlexQueryInvalidError: Query ID invalid (1014).
            IBKRFlexStatementNotReady: Statement still preparing past budget.
            urllib.error.URLError: Network failure / timeout.
        """
        if self._last_statement is not None and not force_refresh:
            return self._last_statement.to_dict()

        ref_code = self._send_request()
        time.sleep(self._prep_delay)
        statement_xml = self._poll_get_statement(ref_code)
        statement = self._parse_statement(statement_xml, ref_code)
        self._last_statement = statement
        return statement.to_dict()

    def get_positions(self) -> list[dict[str, Any]]:
        """Return positions from the most recent fetch as a list of dicts.

        Calls `fetch_statement()` if no statement has been fetched yet.
        """
        self._ensure_fetched()
        assert self._last_statement is not None  # noqa: S101 — invariant after `_ensure_fetched()`; `fetch_statement()` either raises or populates `_last_statement`, intentionally silenced under `python -O`
        return [p.to_dict() for p in self._last_statement.positions]

    def get_cash_summary(self) -> dict[str, float]:
        """Return cash balances keyed by currency.

        Calls `fetch_statement()` if no statement has been fetched yet.
        """
        self._ensure_fetched()
        assert self._last_statement is not None  # noqa: S101 — invariant after `_ensure_fetched()`; `fetch_statement()` either raises or populates `_last_statement`, intentionally silenced under `python -O`
        return {c.currency: c.ending_cash for c in self._last_statement.cash_report}

    def get_account_nav(self) -> float:
        """Return the sum of positionValue + endingCash across the statement.

        WARNING: This is a mixed-currency total. IBKR Flex reports each
        position and cash row in its native currency; we do not FX-convert
        here. Callers needing a single-currency NAV should post-process
        with an FX table.

        Calls `fetch_statement()` if no statement has been fetched yet.
        """
        self._ensure_fetched()
        assert self._last_statement is not None  # noqa: S101 — invariant after `_ensure_fetched()`; `fetch_statement()` either raises or populates `_last_statement`, intentionally silenced under `python -O`
        positions_total = sum(p.position_value for p in self._last_statement.positions)
        cash_total = sum(c.ending_cash for c in self._last_statement.cash_report)
        return positions_total + cash_total

    # ── Internals: HTTP ────────────────────────────────────────────────────

    def _http_get(self, url: str) -> str:
        """GET `url` and return the response body as text.

        Raises urllib.error.URLError on network errors.
        """
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310  # noqa: S310
            # Flex Web Service returns XML; charset is documented as UTF-8.
            data = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return data.decode(charset, errors="replace")

    def _send_request(self) -> str:
        """POST-free SendRequest call (GET). Returns ReferenceCode."""
        url = (
            f"{SEND_REQUEST_URL}"
            f"?t={urllib.parse.quote(self._token, safe='')}"
            f"&q={urllib.parse.quote(self._query_id, safe='')}"
            f"&v=3"
        )
        logger.debug("SendRequest → %s", _redact_url(url, self._token))
        body = self._http_get(url)
        return self._parse_send_request_response(body)

    def _get_statement(self, ref_code: str) -> str:
        """Fetch the statement body for `ref_code` (GET)."""
        url = (
            f"{GET_STATEMENT_URL}"
            f"?q={urllib.parse.quote(ref_code, safe='')}"
            f"&t={urllib.parse.quote(self._token, safe='')}"
            f"&v=3"
        )
        logger.debug("GetStatement → %s", _redact_url(url, self._token))
        return self._http_get(url)

    def _poll_get_statement(self, ref_code: str) -> str:
        """Poll GetStatement until the statement is ready or we time out."""
        deadline = time.monotonic() + self._poll_max_wait
        while True:
            try:
                body = self._get_statement(ref_code)
                self._raise_on_statement_error(body, ref_code=ref_code)
                return body
            except IBKRFlexStatementNotReady as exc:
                if time.monotonic() >= deadline:
                    raise IBKRFlexStatementNotReady(
                        f"Statement {ref_code!r} not ready after {self._poll_max_wait:.0f}s: {exc}"
                    ) from exc
                logger.debug(
                    "Statement %s not ready, sleeping %.1fs",
                    ref_code,
                    self._poll_interval,
                )
                time.sleep(self._poll_interval)

    # ── Internals: XML parsing ─────────────────────────────────────────────

    def _parse_send_request_response(self, body: str) -> str:
        """Parse SendRequest response; return ReferenceCode or raise."""
        root = self._safe_parse(body)
        err_code = _text_of(root, "ErrorCode")
        if err_code:
            err_msg = _text_of(root, "ErrorMessage") or "unknown error"
            code_int = _safe_int(err_code)
            raise _classify_flex_error(code_int, err_msg, context="SendRequest")

        ref_code = _text_of(root, "ReferenceCode")
        if not ref_code:
            raise IBKRFlexError(f"SendRequest returned no ReferenceCode and no ErrorCode: {body[:200]!r}")
        logger.info("SendRequest OK, ReferenceCode=%s", ref_code)
        return ref_code

    def _raise_on_statement_error(self, body: str, *, ref_code: str) -> None:
        """If `body` is an error response, raise the matching exception."""
        root = self._safe_parse(body)
        err_code = _text_of(root, "ErrorCode")
        if not err_code:
            return
        err_msg = _text_of(root, "ErrorMessage") or "unknown error"
        code_int = _safe_int(err_code)
        try:
            raise _classify_flex_error(code_int, err_msg, context=f"GetStatement({ref_code})")
        except IBKRFlexStatementNotReady:
            # Re-raise so the polling loop can catch and retry.
            raise
        except IBKRFlexError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise IBKRFlexError(f"Unexpected Flex error {code_int}: {err_msg}") from exc

    def _parse_statement(self, body: str, ref_code: str) -> FlexStatement:
        """Parse the full Flex statement XML into a FlexStatement."""
        root = self._safe_parse(body)
        # Error responses should already have been caught by _poll_get_statement.
        err_code = _text_of(root, "ErrorCode")
        if err_code:
            err_msg = _text_of(root, "ErrorMessage") or "unknown error"
            raise _classify_flex_error(_safe_int(err_code), err_msg, context=f"GetStatement({ref_code})")

        # The statement root is <FlexStatement>; child may be <Statement>
        # in some versions or <Account> directly in others. We handle both.
        statement_nodes = root.findall(".//Statement") or [root]

        # Use the first account for the convenience accessors. Multi-account
        # statements are not currently merged; callers can extend if needed.
        primary = statement_nodes[0]
        account = primary.find("Account") if primary.tag == "Statement" else primary
        if account is None:
            raise IBKRFlexError("Flex statement contains no <Account> element")

        account_info = self._parse_account_info(account)
        positions = self._parse_positions(account)
        cash_report = self._parse_cash_report(account)

        return FlexStatement(
            account_info=account_info,
            positions=positions,
            cash_report=cash_report,
            reference_code=ref_code,
            fetched_at_epoch=time.time(),
        )

    def _parse_account_info(self, account: Element) -> AccountInfo:
        return AccountInfo(
            account_id=_text_of(account, "accountId"),
            period=_text_of(account, "period"),
            ending_cash=_safe_float(_text_of(account, "endingCash")),
            from_date=_text_of(account, "fromDate"),
            to_date=_text_of(account, "toDate"),
        )

    def _parse_positions(self, account: Element) -> list[Position]:
        positions: list[Position] = []
        # Positions may live under <OpenPositions>/<OpenPosition> or directly
        # under <Account>/<position> depending on Flex report version.
        containers = account.findall("OpenPositions")
        if containers:
            for container in containers:
                for pos_el in container.findall("OpenPosition"):
                    positions.append(self._parse_one_position(pos_el))
        else:
            for pos_el in account.findall("position"):
                positions.append(self._parse_one_position(pos_el))
        return positions

    def _parse_one_position(self, pos_el: Element) -> Position:
        return Position(
            symbol=_text_of(pos_el, "symbol"),
            quantity=_safe_float(_text_of(pos_el, "position")),
            position_value=_safe_float(_text_of(pos_el, "positionValue")),
            asset_category=_text_of(pos_el, "assetCategory"),
            currency=_text_of(pos_el, "currency"),
            cost_basis=_safe_float(_text_of(pos_el, "costBasisMoney") or _text_of(pos_el, "costBasis")),
            mark_price=_safe_float(_text_of(pos_el, "markPrice")),
            unrealized_pnl=_safe_float(_text_of(pos_el, "fifoPnlUnrealized") or _text_of(pos_el, "unrealizedPnl")),
        )

    def _parse_cash_report(self, account: Element) -> list[CashRow]:
        rows: list[CashRow] = []
        for cash_el in account.findall(".//CashReportCurrency"):
            rows.append(
                CashRow(
                    currency=_text_of(cash_el, "currency"),
                    ending_cash=_safe_float(_text_of(cash_el, "endingCash")),
                    ending_settled_cash=_safe_float(_text_of(cash_el, "endingSettledCash")),
                    ending_trade_cash=_safe_float(_text_of(cash_el, "endingTradeCash")),
                )
            )
        return rows

    # ── Internals: misc ────────────────────────────────────────────────────

    def _ensure_fetched(self) -> None:
        if self._last_statement is None:
            self.fetch_statement()

    def _load_env(self) -> dict[str, str]:
        """Load credentials from .env via dotenv_values (NOT os.environ).

        dotenv_values() reads the .env file directly and returns a dict,
        keeping the credentials scoped to this connector without polluting
        the process environment.
        """
        if not self._env_path.exists():
            # Empty dict — __init__ will raise IBKRFlexConfigError for missing keys.
            return {}
        data = dotenv_values(self._env_path)
        # dotenv_values can return None for non-string values; coerce defensively.
        return {k: ("" if v is None else str(v)) for k, v in data.items()}

    @staticmethod
    def _safe_parse(body: str) -> Element:
        try:
            return ET.fromstring(body)  # noqa: S314
        except ET.ParseError as exc:
            raise IBKRFlexError(f"Failed to parse Flex XML response: {exc} (body starts: {body[:200]!r})") from exc


# ── Module-level helpers ─────────────────────────────────────────────────────


def _text_of(el: Element | None, tag: str) -> str:
    """Return text of first child <tag> in `el`, or empty string."""
    if el is None:
        return ""
    child = el.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _safe_float(s: str) -> float:
    """Parse `s` as float; return 0.0 on empty/invalid input."""
    if not s:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(s: str) -> int:
    if not s:
        return 0
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _classify_flex_error(code: int, message: str, *, context: str) -> IBKRFlexError:
    """Map a Flex error code to the right exception subclass."""
    if code == ERR_TOKEN_EXPIRED:
        return IBKRFlexAuthError(f"{context}: token expired/invalid (1019): {message}")
    if code == ERR_QUERY_NOT_FOUND:
        return IBKRFlexQueryInvalidError(f"{context}: query invalid (1014): {message}")
    if code == ERR_STATEMENT_NOT_READY:
        return IBKRFlexStatementNotReady(f"{context}: statement not ready (1015): {message}")
    return IBKRFlexError(f"{context}: Flex error {code}: {message}")


def _redact_url(url: str, token: str) -> str:
    """Replace the token in `url` with `<redacted>` for safe logging."""
    if not token:
        return url
    return url.replace(urllib.parse.quote(token, safe=""), "<redacted>")
