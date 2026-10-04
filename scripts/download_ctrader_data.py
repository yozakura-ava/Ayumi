#!/usr/bin/env python3
"""Parameterised download of historical OHLCV bars from cTrader Open API.

Originally a hardcoded daily-driver script; per card 291de428 it now accepts
``--symbols``, ``--timeframes``, and ``--date-range`` flags so the same
script can drive XAUUSD-only backfills (pivot Q1) without code edits.

When ``--apply`` is set the freshly downloaded CSV is also imported into
``data/ayumi_market.duckdb`` via :mod:`scripts.import_ctrader_bars`, which
enforces the council binding guards (G1–G7, Sora gate). The default is
``--dry-run`` so a download never silently writes to the live DB.

Usage examples
--------------
    # XAUUSD backfill 2026-07-13 → 2026-10-04, dry-run only (safe default)
    python scripts/download_ctrader_data.py \\
        --symbols XAUUSD --timeframes M3,M5,M15,M30,H1 \\
        --start 2026-07-13 --end 2026-10-04

    # Same but commit into the live market DB
    python scripts/download_ctrader_data.py \\
        --symbols XAUUSD --timeframes M3,M5,M15,M30,H1 \\
        --start 2026-07-13 --end 2026-10-04 --apply

    # Explicit multi-symbol probe (whitelist enforced)
    python scripts/download_ctrader_data.py \\
        --symbols XAUUSD,EURUSD --timeframes H1 \\
        --start 2026-09-01 --end 2026-09-30 --dry-run

Hard whitelist (G6) is enforced in :func:`validate_request` — anything that
isn't ``XAUUSD x {M3,M5,M15,M30,H1}`` is rejected before a network call.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Add project root to path so we can import forex_bot package + sibling scripts
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# Card 377b2bab auth-follow-up (2026-10-04, Craig binding): the historical
# client MUST consume the live-maintained credential store
# (``adapters/ctrader/credential_store.py``) — the same path the live
# forward test uses, kept fresh by ``token_lifecycle.manage()``. We do
# NOT manage env vars directly here; CredentialStore is the single
# source of truth for credential schema + .env I/O.
from src.forex_bot.adapters.ctrader.credential_store import CredentialStore  # noqa: E402
from src.forex_bot.data.ctrader_client import CTraderHistoricalClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Hard whitelist (G6) — XAUUSD only across the tournament timeframes.
# Any request outside this set is refused before we touch the network.
ALLOWED_SYMBOLS: frozenset[str] = frozenset({"XAUUSD"})
ALLOWED_TIMEFRAMES: frozenset[str] = frozenset({"M3", "M5", "M15", "M30", "H1"})

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "forex" / "historical"


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _csv(value: str) -> list[str]:
    """argparse type: split comma-parseable string into upper-cased tokens."""
    if value is None:
        return []
    return [tok.strip().upper() for tok in value.split(",") if tok.strip()]


def _iso_date(value: str) -> str:
    """argparse type: ISO-8601 date (YYYY-MM-DD)."""
    import datetime as _dt

    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO date '{value}' (expected YYYY-MM-DD)"
        ) from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="download_ctrader_data",
        description=(
            "Download OHLCV bars from cTrader Open API for a parameterised "
            "set of symbols and timeframes, then (optionally) import them "
            "into data/ayumi_market.duckdb."
        ),
    )
    p.add_argument(
        "--symbols",
        type=_csv,
        default=["XAUUSD"],
        help=(
            "Comma-separated symbols. Whitelist enforced: only XAUUSD is "
            "permitted by the XAUUSD-only FTMO MVP pivot (default: XAUUSD)."
        ),
    )
    p.add_argument(
        "--timeframes",
        type=_csv,
        default=["M3", "M5", "M15", "M30", "H1"],
        help=(
            "Comma-separated timeframes. Whitelist enforced: "
            "M3, M5, M15, M30, H1 only (default: all five)."
        ),
    )
    p.add_argument("--start", type=_iso_date, required=True, help="Start date YYYY-MM-DD (inclusive).")
    p.add_argument("--end", type=_iso_date, required=True, help="End date YYYY-MM-DD (inclusive).")
    p.add_argument(
        "--csv-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write downloaded CSV files (default: data/forex/historical).",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=PROJECT_ROOT / "data" / "ayumi_market.duckdb",
        help="Target DuckDB path (default: data/ayumi_market.duckdb).",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="(default) Download CSVs only; do not write to the DB.",
    )
    p.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Actually import the downloaded CSVs into the DB.",
    )
    p.add_argument(
        "--allow-ms",
        action="store_true",
        help="Allow ingestion of epoch-ms timestamps (G1). Off by default.",
    )
    return p


def validate_request(args: argparse.Namespace) -> None:
    """Enforce G6 hard whitelist and a sane date range.

    Raises ``argparse.ArgumentTypeError`` (via ValueError) when the
    request falls outside the XAUUSD-only MVP scope.
    """
    bad_symbols = [s for s in args.symbols if s not in ALLOWED_SYMBOLS]
    if bad_symbols:
        raise ValueError(
            f"symbols {bad_symbols!r} not in hard whitelist {sorted(ALLOWED_SYMBOLS)} "
            f"(pivot Q5 — other symbols are FROZEN)."
        )
    bad_tfs = [t for t in args.timeframes if t not in ALLOWED_TIMEFRAMES]
    if bad_tfs:
        raise ValueError(
            f"timeframes {bad_tfs!r} not in hard whitelist {sorted(ALLOWED_TIMEFRAMES)} "
            f"(XAUUSD tournament roster)."
        )
    if args.start > args.end:
        raise ValueError(f"--start ({args.start}) must be <= --end ({args.end})")


# ---------------------------------------------------------------------------
# Per-(symbol, timeframe) pipeline
# ---------------------------------------------------------------------------


def download_one(
    client: CTraderHistoricalClient,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    output_dir: Path,
) -> Path:
    """Download one (symbol, timeframe) slice and return the CSV path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / f"{symbol}_{timeframe}.csv"
    logger.info(
        "Downloading %s %s (%s → %s) → %s",
        symbol,
        timeframe,
        start,
        end,
        csv_path,
    )
    try:
        client.download_and_save(
            symbol=symbol,
            timeframe=timeframe,
            start_date=start,
            end_date=end,
            output_path=str(csv_path),
            append=False,  # parameterised backfill is a full overwrite per slice
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed %s %s: %s", symbol, timeframe, exc)
        raise
    return csv_path


def import_one(
    csv_path: Path,
    db_path: Path,
    symbol: str,
    timeframe: str,
    *,
    dry_run: bool,
    allow_ms: bool,
) -> dict:
    """Import one CSV into the DB via the guarded importer.

    Returns the importer's stats dict so the caller can summarise across
    (symbol, timeframe) pairs.
    """
    # Lazy import keeps the download path light when --dry-run is on.
    from import_ctrader_bars import import_csv

    return import_csv(
        csv_path=csv_path,
        db_path=db_path,
        symbol=symbol,
        timeframe=timeframe,
        dry_run=dry_run,
        allow_ms=allow_ms,
    )


# ---------------------------------------------------------------------------
# Credential loading — delegates to CredentialStore (Craig binding)
# ---------------------------------------------------------------------------


def _load_credential_store(env_path: Path) -> CredentialStore:
    """Return a ``CredentialStore`` pointed at the project's .env.

    The store is the single source of truth for credential schema and
    ``.env`` I/O (card 377b2bab auth-follow-up, Craig binding 2026-10-04).
    The historical client consumes it; it does NOT mint its own OAuth
    tokens.
    """
    return CredentialStore(env_path=str(env_path))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        validate_request(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable; parser.error exits

    # Card 377b2bab auth-follow-up (2026-10-04, Craig binding): consume
    # the live-maintained credential store. It reads .env (the canonical
    # credential source) and exposes the access token the live path's
    # token_lifecycle maintains. The historical client never calls the
    # OAuth refresh grant itself.
    try:
        credential_store = _load_credential_store(PROJECT_ROOT / ".env")
        # Touch the store to surface missing-credential errors loudly
        # before we open any network connection.
        credential_store.load()
    except RuntimeError as exc:
        logger.error("CredentialStore load failed: %s", exc)
        return 1

    client = CTraderHistoricalClient(credential_store=credential_store)

    total = len(args.symbols) * len(args.timeframes)
    done = 0
    csv_paths: list[tuple[str, str, Path]] = []
    for symbol in args.symbols:
        for tf in args.timeframes:
            done += 1
            logger.info("[%d/%d] %s %s", done, total, symbol, tf)
            csv_path = download_one(
                client=client,
                symbol=symbol,
                timeframe=tf,
                start=args.start,
                end=args.end,
                output_dir=args.csv_dir,
            )
            csv_paths.append((symbol, tf, csv_path))
            time.sleep(0.5)  # courtesy pause between API calls

    logger.info(
        "Download phase done — %d CSV file(s) ready at %s. import phase = %s",
        len(csv_paths),
        args.csv_dir,
        "DRY-RUN (skipped)" if args.dry_run else "APPLY",
    )

    if args.dry_run:
        # Don't touch the DB. Surface a one-line CSV manifest so the
        # operator can see exactly what would be imported.
        for symbol, tf, path in csv_paths:
            print(f"DRY-RUN  {symbol}  {tf}  ->  {path}")
        return 0

    # Import phase — go through the guarded importer.
    summary = []
    for symbol, tf, path in csv_paths:
        result = import_one(
            csv_path=path,
            db_path=args.db,
            symbol=symbol,
            timeframe=tf,
            dry_run=False,
            allow_ms=args.allow_ms,
        )
        summary.append(result)

    # Print summary
    print("=" * 100)
    print(
        f"{'symbol':<8} {'tf':<5} {'mode':<8} {'staged':>8} {'inserted':>9} "
        f"{'updated':>8} {'pre':>5} {'post':>5}  result"
    )
    print("-" * 100)
    for r in summary:
        print(
            f"{r['symbol']:<8} {r['timeframe']:<5} {r['mode']:<8} "
            f"{r.get('staged_rows', 0):>8} {r.get('inserted_rows', 0):>9} "
            f"{r.get('updated_rows', 0):>8} "
            f"{'ok' if r.get('preflight_ok') else 'FAIL':>5} "
            f"{'ok' if r.get('postflight_ok') else 'FAIL':>5}  {r['result']}"
        )
    print("=" * 100)
    return 0 if all(r["result"] == "ok" for r in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
