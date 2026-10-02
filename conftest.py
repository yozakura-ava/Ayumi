"""Root conftest.py — applies resource limits to all test runs.

Prevents unit tests from consuming more than 20% CPU / 8GB memory on the shared server.
Individual tests or modules can override by calling resource_limits directly, or by
exporting MEMRAY_MAX_MEMORY=<mb> before invoking pytest.
"""

# ── BLAS thread guard (card 53505568) ─────────────────────────────────────
# Must precede any import that could pull BLAS in (numpy, scipy, pandas, ...).
# Without this, OpenBLAS/MKL/OpenMP spawn worker threads that share memory and
# corrupt state under pytest's suite ordering, manifesting as exit-139 segfaults
# (see diagnosis card 92243d36). setdefault preserves any caller-supplied value.
import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import sys

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src", "forex_bot"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from common.resource_limits import configure_pytest_defaults

# Apply memory limit at collection time. Default raised to 8GB (was 2GB) so the
# full pytest suite can complete without an undocumented MEMRAY_MAX_MEMORY override;
# the env var remains honored for callers who want a tighter or looser cap (card 75e04b13).
configure_pytest_defaults(max_memory_mb=int(os.environ.get("MEMRAY_MAX_MEMORY", "8192")))


def pytest_collection_modifyitems(config, items):
    """Optional hook: log collection size for diagnostics."""
    import logging

    logging.getLogger("ayumi.resource_limits").debug("Collected %d tests, memory limit active", len(items))
