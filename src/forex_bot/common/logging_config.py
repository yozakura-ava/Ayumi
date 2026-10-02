"""Structured logging configuration for Ayumi.

Replaces nohup/shell-redirect logging with a proper Python logging setup.
Logs rotate daily, keeping 7 days of history.

Usage (at the START of main()):
    from common.logging_config import setup_logging
    setup_logging(log_dir="logs", level="INFO")
"""

import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

# PROJECT_ROOT is 3 levels up from src/forex_bot/common/logging_config.py:
#   parents[0] = common/  parents[1] = forex_bot/  parents[2] = src/  parents[3] = <repo root>
PROJECT_ROOT = Path(__file__).resolve().parents[3]


DEFAULT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    log_name: str = "forward_test",
    backup_count: int = 7,
    console: bool = True,
) -> logging.Logger:
    """Configure root logger with file rotation and optional console output.

    Call this at the VERY START of main(). Idempotent — safe to call multiple times.

    Args:
        log_dir: Directory for log files (created if missing)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_name: Base name for log file (e.g., "forward_test" → forward_test.log)
        backup_count: Number of daily backups to keep
        console: Also log to stderr (for interactive use)

    Returns:
        The root logger, configured.
    """
    # During pytest runs, redirect logs to a temp dir ONLY when the caller
    # used the default log_dir (i.e. did not pass an explicit path). This
    # prevents pytest from clobbering test fixtures that rely on tmp_path
    # while still protecting production log files from test contamination.
    if "pytest" in sys.modules and log_dir == "logs":
        log_dir = os.path.join(tempfile.gettempdir(), "ayumi_pytest_logs")

    # Resolve relative paths against PROJECT_ROOT so the service always
    # writes to the same location regardless of CWD (fixes root contamination
    # when launched from a non-project working directory).
    log_path = Path(log_dir)
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    log_path.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

    # File handler — daily rotation
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_path / f"{log_name}.log"),
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Root logger
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers (idempotent)
    for h in list(root.handlers):
        root.removeHandler(h)

    root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(console_handler)

    # Quiet down noisy libraries
    logging.getLogger("twisted").setLevel(logging.WARNING)
    logging.getLogger("ctrader_open_api").setLevel(logging.WARNING)

    logging.info("Logging initialized: %s/%s.log (level=%s)", log_dir, log_name, level)
    return root
