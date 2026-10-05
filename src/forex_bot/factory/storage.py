"""Factory verdict persistence (SFA-2 — research.duckdb schema + writer).

Persists :class:`ValidationVerdict` rows from the validation runner to the
canonical research database (``data/research/research.duckdb`` by default).

Design
------
* A new ``factory_verdicts`` table is added to the SRF database.  The
  schema is intentionally narrow — the runner owns the in-memory shape;
  storage only mirrors it.
* :class:`FactoryVerdictStore` is a thin DuckDB wrapper that exposes a
  single :meth:`write_verdicts` call.  It does NOT take an exclusive file
  lock — the SRF database already has its own single-writer lock and
  concurrent factory writes are coordinated by the caller.
* Migration is idempotent (``CREATE TABLE IF NOT EXISTS``) so the store
  can be instantiated by tests using ``tmp_path`` without polluting the
  canonical DB.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import duckdb

from forex_bot.factory.validation_runner import ValidationVerdict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2

_FACTORY_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS factory_verdicts (
    verdict_id            VARCHAR PRIMARY KEY,
    candidate_id          VARCHAR NOT NULL,
    archetype_id          VARCHAR NOT NULL,
    pair                  VARCHAR NOT NULL,
    timeframe             VARCHAR NOT NULL,
    tier                  VARCHAR NOT NULL,
    windows_passed        INTEGER NOT NULL DEFAULT 0,
    windows_total         INTEGER NOT NULL DEFAULT 0,
    total_trades          INTEGER NOT NULL DEFAULT 0,
    mean_sharpe           DOUBLE,
    mean_profit_factor    DOUBLE,
    mean_win_rate         DOUBLE,
    max_drawdown          DOUBLE,
    dsr_pvalue            DOUBLE,
    n_trials_used         INTEGER NOT NULL DEFAULT 0,
    pbo_score             DOUBLE,
    pbo_tier_ceiling      VARCHAR,
    cost_sensitivity      DOUBLE,
    spread_pips           DOUBLE,
    commission_per_lot_usd DOUBLE,
    slippage_pips         DOUBLE,
    go_nogo               BOOLEAN,
    reason                VARCHAR,
    ran_at                VARCHAR,
    bridge_error          VARCHAR,
    created_at            TIMESTAMPTZ DEFAULT now()
)
"""

_FACTORY_VERDICTS_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS _factory_verdicts_schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ DEFAULT now()
)
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class FactoryVerdictStore:
    """Persist :class:`ValidationVerdict` rows to research.duckdb.

    Parameters
    ----------
    db_path
        Path to the DuckDB file.  Pass ``tmp_path`` from pytest to keep
        tests isolated from the canonical research DB.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── schema management ───────────────────────────────────────────────

    def ensure_schema(self) -> None:
        """Create / migrate the ``factory_verdicts`` table (idempotent).

        v1 → v2 adds ``cost_sensitivity DOUBLE`` (card 4309d26b —
        renamed from the old synthetic 2-column "PBO" which was
        actually cost sensitivity in disguise).  The ``ADD COLUMN IF
        NOT EXISTS`` migration runs before the version bump so an
        existing v1 database upgrades cleanly.
        """
        with duckdb.connect(str(self.db_path)) as conn:
            conn.execute(_FACTORY_VERDICTS_DDL)
            conn.execute(_FACTORY_VERDICTS_VERSION_DDL)
            # Idempotent column-level migration: cost_sensitivity was
            # added in v2.  Safe to re-run on a fresh or upgraded DB.
            conn.execute(
                "ALTER TABLE factory_verdicts ADD COLUMN IF NOT EXISTS cost_sensitivity DOUBLE"
            )
            cur = conn.execute("SELECT MAX(version) FROM _factory_verdicts_schema_version").fetchone()
            current = cur[0] if cur and cur[0] is not None else 0
            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO _factory_verdicts_schema_version (version) VALUES (?)",
                    [SCHEMA_VERSION],
                )

    # ── write ───────────────────────────────────────────────────────────

    def write_verdicts(self, verdicts: Iterable[ValidationVerdict]) -> int:
        """Insert one row per verdict.  Returns the row count written.

        A deterministic :attr:`ValidationVerdict.verdict_id` is
        synthesised from the verdict's natural-key fields
        (``candidate_id`` + ``pair`` + ``timeframe`` + ``ran_at``) so
        reruns of the same batch are idempotent.
        """
        rows: list[tuple] = []
        for v in verdicts:
            rows.append(self._row_for(v))
        if not rows:
            return 0
        self.ensure_schema()
        with duckdb.connect(str(self.db_path)) as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO factory_verdicts (
                    verdict_id, candidate_id, archetype_id, pair, timeframe,
                    tier, windows_passed, windows_total, total_trades,
                    mean_sharpe, mean_profit_factor, mean_win_rate, max_drawdown,
                    dsr_pvalue, n_trials_used, pbo_score, pbo_tier_ceiling,
                    cost_sensitivity,
                    spread_pips, commission_per_lot_usd, slippage_pips,
                    go_nogo, reason, ran_at, bridge_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    # ── read (useful for tests + CLI) ───────────────────────────────────

    def fetch_verdicts(self, candidate_id: str | None = None) -> list[dict]:
        """Return verdicts as dicts; optional filter by ``candidate_id``."""
        self.ensure_schema()
        with duckdb.connect(str(self.db_path), read_only=True) as conn:
            if candidate_id is None:
                rows = conn.execute("SELECT * FROM factory_verdicts ORDER BY created_at").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM factory_verdicts WHERE candidate_id = ? ORDER BY created_at",
                    [candidate_id],
                ).fetchall()
            col_names = [d[0] for d in conn.description]
        return [dict(zip(col_names, r, strict=True)) for r in rows]

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _row_for(v: ValidationVerdict) -> tuple:
        verdict_id = f"{v.candidate_id}|{v.pair}|{v.timeframe}|{v.ran_at}"
        return (
            verdict_id,
            v.candidate_id,
            v.archetype_id,
            v.pair,
            v.timeframe,
            v.tier,
            int(v.windows_passed),
            int(v.windows_total),
            int(v.total_trades),
            v.mean_sharpe,
            v.mean_profit_factor,
            v.mean_win_rate,
            v.max_drawdown,
            v.dsr_pvalue,
            int(v.n_trials_used),
            v.pbo_score,
            v.pbo_tier_ceiling,
            v.cost_sensitivity,
            v.spread_pips,
            v.commission_per_lot_usd,
            v.slippage_pips,
            bool(v.go_nogo),
            v.reason,
            v.ran_at,
            v.bridge_error,
        )


__all__ = ["FactoryVerdictStore", "SCHEMA_VERSION"]
