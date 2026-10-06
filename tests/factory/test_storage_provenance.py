"""Tests for provenance columns on :class:`FactoryVerdictStore` (card 32ff09e3).

Coverage matrix (provenance wiring, SFA-2 follow-on 1b.4):

* :class:`FactoryVerdictStore` writes ``git_commit`` / ``data_hash``
  columns on every row when provided.
* Auto-fetched ``git_commit`` matches ``git rev-parse --short HEAD``.
* Computed ``data_hash`` matches :func:`forex_bot.srf.compute_data_hash`
  (single source of truth for the data-hash algorithm).
* Explicit ``git_commit`` / ``data_hash`` kwargs override the auto path.
* Pre-migration ``factory_verdicts`` rows (no provenance values) remain
  readable after the v2 → v3 schema upgrade — readers tolerate
  ``NULL`` in the new columns.

All tests are pure (no Optuna, no market-data fetch, no network) so
they stay in the HR5 targeted-only envelope.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import duckdb

from forex_bot.factory.storage import (
    SCHEMA_VERSION,
    FactoryVerdictStore,
    _get_git_commit,
)
from forex_bot.factory.validation_runner import ValidationVerdict
from forex_bot.srf import compute_data_hash

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_verdict(**overrides: Any) -> ValidationVerdict:
    """Build a minimal :class:`ValidationVerdict` for store tests."""
    base: dict[str, Any] = {
        "candidate_id": "prov_test",
        "archetype_id": "trend",
        "pair": "EURUSD",
        "timeframe": "H1",
        "tier": "A",
        "windows_passed": 4,
        "windows_total": 5,
        "total_trades": 42,
        "mean_sharpe": 1.7,
        "mean_profit_factor": 1.4,
        "mean_win_rate": 0.62,
        "max_drawdown": 0.04,
        "dsr_pvalue": 0.02,
        "n_trials_used": 160,
        "pbo_score": 0.12,
        "pbo_tier_ceiling": "A",
        "cost_sensitivity": 0.073,
        "spread_pips": 1.5,
        "commission_per_lot_usd": 3.5,
        "slippage_pips": 0.2,
        "go_nogo": True,
        "reason": "tier-A pass",
        "ran_at": "2026-10-06T18:55:00+00:00",
    }
    base.update(overrides)
    return ValidationVerdict(**base)


def _write_data_file(tmp_path: Path, content: bytes = b"sample-input-bars\ngbpjpy\n") -> Path:
    """Drop a synthetic input-data file the hash tests can point at."""
    p = tmp_path / "input.csv"
    p.write_bytes(content)
    return p


# ---------------------------------------------------------------------------
# git_commit auto-fetch
# ---------------------------------------------------------------------------


def test_get_git_commit_matches_rev_parse() -> None:
    """``_get_git_commit`` matches the subprocess baseline."""
    expected = (
        subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )
    assert _get_git_commit() == expected


# ---------------------------------------------------------------------------
# write_verdicts — provenance columns populated
# ---------------------------------------------------------------------------


def test_write_verdicts_auto_populates_git_commit(tmp_path) -> None:
    """Writer auto-fetches ``git_commit`` when no override is given."""
    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    expected = (
        subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )
    written = store.write_verdicts([_make_verdict()])
    assert written == 1

    rows = store.fetch_verdicts(candidate_id="prov_test")
    assert len(rows) == 1
    assert rows[0]["git_commit"] == expected


def test_write_verdicts_auto_computes_data_hash_from_data_path(tmp_path) -> None:
    """Writer computes ``data_hash`` via :func:`compute_data_hash` when
    ``data_path`` is supplied (reuses srf, no duplication)."""
    data_file = _write_data_file(tmp_path)
    expected_hash = compute_data_hash(data_file)

    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    written = store.write_verdicts(
        [_make_verdict()],
        data_path=data_file,
    )
    assert written == 1

    rows = store.fetch_verdicts(candidate_id="prov_test")
    assert len(rows) == 1
    assert rows[0]["data_hash"] == expected_hash
    # Sanity: hash is the truncated 16-char hex srf emits.
    assert len(rows[0]["data_hash"]) == 16


def test_write_verdicts_explicit_git_commit_overrides_auto(tmp_path) -> None:
    """Explicit ``git_commit`` kwarg wins over the auto-fetch path."""
    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    written = store.write_verdicts(
        [_make_verdict(candidate_id="explicit_commit")],
        git_commit="deadbeef",
    )
    assert written == 1

    rows = store.fetch_verdicts(candidate_id="explicit_commit")
    assert rows[0]["git_commit"] == "deadbeef"


def test_write_verdicts_explicit_data_hash_overrides_data_path(tmp_path) -> None:
    """Explicit ``data_hash`` kwarg wins over the ``data_path`` path."""
    data_file = _write_data_file(tmp_path)

    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    written = store.write_verdicts(
        [_make_verdict(candidate_id="explicit_hash")],
        data_path=data_file,
        data_hash="0123456789abcdef",
    )
    assert written == 1

    rows = store.fetch_verdicts(candidate_id="explicit_hash")
    assert rows[0]["data_hash"] == "0123456789abcdef"


def test_write_verdicts_omitted_data_path_leaves_null(tmp_path) -> None:
    """When ``data_path`` is omitted the ``data_hash`` column is ``NULL``."""
    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    store.write_verdicts([_make_verdict()])
    rows = store.fetch_verdicts(candidate_id="prov_test")
    assert rows[0]["data_hash"] is None


# ---------------------------------------------------------------------------
# write_verdicts — batch + idempotency interaction
# ---------------------------------------------------------------------------


def test_write_verdicts_all_rows_share_provenance(tmp_path) -> None:
    """A single ``git_commit`` / ``data_hash`` is stamped onto every row
    in the batch (one provenance pair per write call)."""
    data_file = _write_data_file(tmp_path, b"shared-bytes\n")
    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    verdicts = [
        _make_verdict(candidate_id=f"batch_{i}", ran_at=f"2026-10-06T18:55:0{i}+00:00")
        for i in range(3)
    ]
    store.write_verdicts(verdicts, git_commit="abc1234", data_path=data_file)

    rows = store.fetch_verdicts()
    assert len(rows) == 3
    for row in rows:
        assert row["git_commit"] == "abc1234"
        assert row["data_hash"] == compute_data_hash(data_file)


# ---------------------------------------------------------------------------
# Schema version bump + migration
# ---------------------------------------------------------------------------


def test_schema_version_is_three() -> None:
    """Card 32ff09e3 bumps the version (writers must follow the migration)."""
    assert SCHEMA_VERSION == 3


def test_ensure_schema_idempotent(tmp_path) -> None:
    """Re-running ``ensure_schema`` on a v3 DB is a no-op (no errors,
    version stays at 3, columns remain present)."""
    store = FactoryVerdictStore(tmp_path / "research.duckdb")
    store.ensure_schema()
    store.ensure_schema()
    with duckdb.connect(str(store.db_path), read_only=True) as conn:
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'factory_verdicts'"
            ).fetchall()
        }
        version_row = conn.execute(
            "SELECT MAX(version) FROM _factory_verdicts_schema_version"
        ).fetchone()
        assert version_row is not None
        version = version_row[0]
    assert {"git_commit", "data_hash"}.issubset(cols)
    assert version == 3


# ---------------------------------------------------------------------------
# Legacy (pre-v3) records remain readable
# ---------------------------------------------------------------------------


def test_legacy_records_without_provenance_columns_readable(tmp_path) -> None:
    """Records written by a v2 DB (no provenance columns) survive the
    v2 → v3 migration and read back with ``NULL`` in the new columns.

    Simulates the upgrade path by:
      1. Building a DuckDB with the v2 ``factory_verdicts`` shape (no
         ``git_commit`` / ``data_hash``).
      2. Inserting a legacy row.
      3. Opening the same file via :class:`FactoryVerdictStore` so the
         v2 → v3 migration runs.
      4. Verifying the legacy row reads back with the new columns
         present and ``NULL``-valued.
    """
    db = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(db)) as conn:
        # v2 schema (no provenance columns).  Includes ``cost_sensitivity``
        # because v2 already had it.
        conn.execute(
            """
            CREATE TABLE factory_verdicts (
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
        )
        conn.execute(
            """
            INSERT INTO factory_verdicts
                (verdict_id, candidate_id, archetype_id, pair, timeframe,
                 tier, windows_passed, windows_total, total_trades,
                 cost_sensitivity, go_nogo, ran_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "legacy1|EURUSD|H1|2026-09-01T00:00:00+00:00",
                "legacy1",
                "trend",
                "EURUSD",
                "H1",
                "A",
                4,
                5,
                30,
                0.05,
                True,
                "2026-09-01T00:00:00+00:00",
            ],
        )

    # Open via FactoryVerdictStore — triggers v2 → v3 migration.
    store = FactoryVerdictStore(db)
    rows = store.fetch_verdicts(candidate_id="legacy1")
    assert len(rows) == 1
    row = rows[0]
    # Legacy columns preserved.
    assert row["candidate_id"] == "legacy1"
    assert row["tier"] == "A"
    assert row["total_trades"] == 30
    # New columns added by migration surface as NULL.
    assert row["git_commit"] is None
    assert row["data_hash"] is None
    # New write after migration stamps the columns.
    store.write_verdicts(
        [_make_verdict(candidate_id="fresh_after_migration")],
        git_commit="cafe1234",
        data_hash="feedbeefdeadc0de",
    )
    rows = store.fetch_verdicts(candidate_id="fresh_after_migration")
    assert rows[0]["git_commit"] == "cafe1234"
    assert rows[0]["data_hash"] == "feedbeefdeadc0de"
