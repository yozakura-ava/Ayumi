"""Discover available symbols and backfill historical data.

Usage:
    PYTHONPATH=src/forex_bot:src python scripts/discover_symbols.py --discover
    PYTHONPATH=src/forex_bot:src python scripts/discover_symbols.py --backfill --timeframe H1
    PYTHONPATH=src/forex_bot:src python scripts/discover_symbols.py --backfill --symbols EUR/GBP,XAG/USD --timeframe H1
    PYTHONPATH=src/forex_bot:src python scripts/discover_symbols.py --status
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from adapters.ctrader.models import cTraderCredentials  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def get_credentials() -> cTraderCredentials:
    """Load credentials from environment or config."""
    import os

    return cTraderCredentials(
        host=os.environ.get("CTRADER_HOST", "h1.p.ctrader.com"),
        port=int(os.environ.get("CTRADER_PORT", "5211")),
        use_ssl=os.environ.get("CTRADER_SSL", "true").lower() == "true",
        sender_comp_id=os.environ.get("CTRADER_SENDER_ID", os.environ.get("CTRADER_ACCOUNT", "")),
        target_comp_id=os.environ.get("CTRADER_TARGET_ID", "cServer"),
        sender_sub_id=os.environ.get("CTRADER_SENDER_SUB", "QUOTE"),
        target_sub_id=os.environ.get("CTRADER_TARGET_SUB", "QUOTE"),
        username=os.environ.get("CTRADER_USERNAME", os.environ.get("CTRADER_ACCOUNT", "")),
        password=os.environ.get("CTRADER_PASSWORD", ""),
    )


def cmd_discover(args):
    """Discover symbols via FIX protocol."""
    from adapters.ctrader.symbol_discovery import SymbolDiscovery

    creds = get_credentials()
    discovery = SymbolDiscovery(creds)

    # Try SecurityListRequest first
    logger.info("Attempting SecurityListRequest...")
    found = discovery.discover_via_security_list(timeout=args.timeout)

    if not found:
        logger.info("SecurityListRequest failed, trying subscription probe...")
        found = discovery.discover_via_subscription_probe()

    all_symbols = discovery.get_all()
    logger.info(f"Total known symbols: {len(all_symbols)}")

    # Print by category
    categories = {}
    for info in all_symbols.values():
        categories.setdefault(info.category, []).append(info)

    for cat, infos in sorted(categories.items()):
        names = ", ".join(info.name for info in infos)
        logger.info(f"  {cat}: {names}")


def cmd_backfill(args):
    """Backfill historical data."""
    from adapters.ctrader.symbol_discovery import SymbolDiscovery
    from data.backfill import HistoricalDataBackfill

    creds = get_credentials()
    discovery = SymbolDiscovery(creds)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        all_syms = discovery.get_all()
        symbols = [info.name for info in all_syms.values()]

    if not symbols:
        logger.warning("No symbols to backfill. Run --discover first.")
        return

    backfill = HistoricalDataBackfill(creds)
    results = backfill.backfill_multiple(symbols, timeframe=args.timeframe)

    for symbol, path in results.items():
        logger.info(f"  {symbol} -> {path}")


def cmd_status(args):
    """Show current discovery status."""
    cache_path = Path("data/ctrader_symbols.json")
    data_dir = Path("data/forex/historical")

    if cache_path.exists():
        data = json.loads(cache_path.read_text())
        logger.info(f"Cache: {len(data)} symbols in {cache_path}")

        categories = {}
        for sid, info in data.items():  # noqa: B007
            cat = info.get("category", "other")
            categories.setdefault(cat, []).append(info["name"])

        for cat, names in sorted(categories.items()):
            logger.info(f"  {cat}: {len(names)} symbols")
    else:
        logger.info("No symbol cache found. Run --discover first.")

    if data_dir.exists():
        csvs = sorted(data_dir.glob("*.csv"))
        logger.info(f"Historical data: {len(csvs)} CSV files in {data_dir}")
        for csv in csvs[:20]:
            import pandas as pd

            df = pd.read_csv(csv)
            logger.info(f"  {csv.name}: {len(df)} rows")
        if len(csvs) > 20:
            logger.info(f"  ... and {len(csvs) - 20} more")
    else:
        logger.info("No historical data directory found.")


def main():
    parser = argparse.ArgumentParser(description="cTrader symbol discovery and data backfill")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout for FIX requests")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("discover", help="Discover available symbols")

    bp = sub.add_parser("backfill", help="Backfill historical data")
    bp.add_argument("--timeframe", default="H1", help="Timeframe (M1, M5, M15, M30, H1, H4, D1)")
    bp.add_argument("--symbols", default=None, help="Comma-separated symbol list")

    sub.add_parser("status", help="Show current discovery and data status")

    args = parser.parse_args()

    if args.command == "discover":
        cmd_discover(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
