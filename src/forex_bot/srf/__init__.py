"""Strategy Research Framework (SRF).

Database-backed strategy research pipeline with walk-forward validation,
DuckDB result tracking, and statistical robustness gates.

Public API:
    SRFDatabase         — connection wrapper with single-writer lock + migrations
    compute_data_hash   — SHA256 of input data for provenance
    generate_run_id     — deterministic-ish run ID generator
"""

from .drift_monitor import (
    DriftMonitor,
    DriftReport,
    DriftResult,
    compute_ks_test,
    compute_psi,
)
from .schema import SRFDatabase, compute_data_hash, generate_run_id

__version__ = "0.1.0"

__all__ = [
    "SRFDatabase",
    "compute_data_hash",
    "generate_run_id",
    # Drift monitoring
    "DriftMonitor",
    "DriftReport",
    "DriftResult",
    "compute_psi",
    "compute_ks_test",
]
