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
* Provenance columns (``git_commit``, ``data_hash``) — card 32ff09e3
  — are populated from caller-supplied ``git_commit`` / ``data_path``
  kwargs; the SHA256 hash is computed via
  :func:`forex_bot.srf.compute_data_hash` (single source of truth for
  the project's data-hash algorithm).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterable

import duckdb

from forex_bot.factory.validation_runner import ValidationVerdict
from forex_bot.srf import compute_data_hash

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3

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
    git_commit            VARCHAR,
    data_hash             VARCHAR,
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

        Migration history (additive only — ``ADD COLUMN IF NOT EXISTS``):

        * v1 → v2 adds ``cost_sensitivity DOUBLE`` (card 4309d26b —
          renamed from the old synthetic 2-column "PBO" which was
          actually cost sensitivity in disguise).
        * v2 → v3 adds ``git_commit VARCHAR`` and ``data_hash VARCHAR``
          (card 32ff09e3 — provenance columns).

        The ``ADD COLUMN IF NOT EXISTS`` migrations run before the
        version bump so an existing v1 or v2 database upgrades
        cleanly.  Records written before a column was added carry
        ``NULL`` in that column after migration, which readers tolerate
        (the dict shape just exposes ``None`` for those keys).
        """
        with duckdb.connect(str(self.db_path)) as conn:
            conn.execute(_FACTORY_VERDICTS_DDL)
            conn.execute(_FACTORY_VERDICTS_VERSION_DDL)
            # Idempotent column-level migrations.  Safe to re-run on a
            # fresh or upgraded DB.
            conn.execute(
                "ALTER TABLE factory_verdicts ADD COLUMN IF NOT EXISTS cost_sensitivity DOUBLE"
            )
            conn.execute(
                "ALTER TABLE factory_verdicts ADD COLUMN IF NOT EXISTS git_commit VARCHAR"
            )
            conn.execute(
                "ALTER TABLE factory_verdicts ADD COLUMN IF NOT EXISTS data_hash VARCHAR"
            )
            cur = conn.execute("SELECT MAX(version) FROM _factory_verdicts_schema_version").fetchone()
            current = cur[0] if cur and cur[0] is not None else 0
            if current < SCHEMA_VERSION:
                conn.execute(
                    "INSERT INTO _factory_verdicts_schema_version (version) VALUES (?)",
                    [SCHEMA_VERSION],
                )

    # ── write ───────────────────────────────────────────────────────────

    def write_verdicts(
        self,
        verdicts: Iterable[ValidationVerdict],
        *,
        git_commit: str | None = None,
        data_path: str | Path | None = None,
        data_hash: str | None = None,
        data_hash_by_pair: dict[str, str] | None = None,
    ) -> int:
        """Insert one row per verdict.  Returns the row count written.

        A deterministic :attr:`ValidationVerdict.verdict_id` is
        synthesised from the verdict's natural-key fields
        (``candidate_id`` + ``pair`` + ``timeframe`` + ``ran_at``) so
        reruns of the same batch are idempotent.

        Provenance kwargs (card 32ff09e3, per-pair extension card 68fb28f5)
        -------------------------------------------------------------------
        * ``git_commit`` — short SHA of the commit the verdict was
          produced under.  ``None`` (default) auto-fetches via
          :func:`_get_git_commit`; pass an explicit value to override
          (e.g. in tests).
        * ``data_path`` — filesystem path to the input data file the
          verdicts were derived from.  When provided, ``data_hash`` is
          computed via :func:`forex_bot.srf.compute_data_hash` (single
          source of truth for the project's data-hash algorithm).
          ``None`` leaves the column ``NULL``.
        * ``data_hash`` — pre-computed hash; wins over ``data_path``
          when both are supplied (useful for callers that already
          have the bytes hashed). Used as the fallback when
          ``data_hash_by_pair`` does not contain ``v.pair``.
        * ``data_hash_by_pair`` — per-pair ``{pair: hash}`` mapping
          (card 68fb28f5 fix #1). When provided, each row's
          ``data_hash`` column is set from
          ``data_hash_by_pair[v.pair]``; if the pair is not in the
          map the writer falls back to ``data_hash``. This lets
          multi-pair sweeps (e.g. BTC/ETH/SOL) record per-pair data
          provenance instead of one run-level hash that hides which
          symbol's bytes produced each verdict.
        """
        commit = git_commit if git_commit is not None else _get_git_commit()
        if data_hash is None and data_path is not None:
            data_hash = compute_data_hash(data_path)
        rows: list[tuple] = []
        for v in verdicts:
            # Per-pair row data_hash; fall back to the run-level hash
            # when the per-pair map is absent or lacks this pair.
            row_hash: str | None = None
            if data_hash_by_pair:
                row_hash = data_hash_by_pair.get(v.pair, data_hash)
            else:
                row_hash = data_hash
            rows.append(self._row_for(v, git_commit=commit, data_hash=row_hash))
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
                    go_nogo, reason, ran_at, bridge_error,
                    git_commit, data_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    # ── read (useful for tests + CLI) ───────────────────────────────────

    def fetch_verdicts(self, candidate_id: str | None = None) -> list[dict]:
        """Return verdicts as dicts; optional filter by ``candidate_id``.

        Tolerates legacy records written before the v3 provenance
        columns were added — those rows surface with ``None`` in
        ``git_commit`` / ``data_hash`` (DuckDB ``NULL`` semantics).
        """
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
    def _row_for(
        v: ValidationVerdict,
        *,
        git_commit: str | None,
        data_hash: str | None,
    ) -> tuple:
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
            git_commit,
            data_hash,
        )


def _get_git_commit() -> str | None:
    """Return ``git rev-parse --short HEAD`` of the current tree.

    Tolerant variant — unlike :meth:`SRFRunner._get_git_commit` we do
    NOT require a clean tree, because factory verdicts may be written
    from a worktree mid-build.  Returns ``None`` only when ``git`` is
    unavailable or the call fails (so the column surfaces as ``NULL``
    rather than crashing the writer).
    """
    try:
        commit = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
        return commit or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


__all__ = ["FactoryVerdictStore", "SCHEMA_VERSION"]
