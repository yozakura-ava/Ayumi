#!/usr/bin/env python3
"""Update sweep #3 provenance to the current build SHA.

The first sweep ran with HEAD = eb6ecacb (the parent commit on this
branch, i.e. main @ eb6ecacb), so all 99 factory_verdicts rows
captured that SHA as their git_commit. The convention (documented in
the report metadata) is that factory_verdicts.git_commit holds the
BUILD short SHA — the commit the sweep RAN UNDER. After the build is
committed (now), the BUILD SHA is 8911c8ba, so we need to:

1. UPDATE factory_verdicts.git_commit on all 99 rows from eb6ecacb
   to the current BUILD short SHA.
2. Update the JSON report metadata + the verdict_table rows so the
   values written to disk match the duckdb state.
3. Re-render the markdown summary with the new SHA.
4. Leave the per-pair ``data_hash`` columns untouched (those are
   the bar-bytes hashes and don't change).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb

WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE))  # so scripts.<x> resolves
sys.path.insert(0, str(WORKTREE / "src"))
sys.path.insert(0, str(WORKTREE / "src" / "forex_bot"))

DB_PATH = WORKTREE / "data" / "sweep_crypto_native_real_data.duckdb"
JSON_PATH = WORKTREE / "docs" / "reports" / "2026-10-06-crypto-native-sweep3.json"
MD_PATH = WORKTREE / "docs" / "reports" / "2026-10-06-crypto-native-sweep3.md"


def get_short_sha() -> str:
    """Return ``git rev-parse --short HEAD``."""
    return (
        subprocess.check_output(  # noqa: S607
            ["/usr/bin/git", "rev-parse", "--short", "HEAD"],
            cwd=str(WORKTREE),
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


def get_long_sha() -> str:
    """Return full ``git rev-parse HEAD``."""
    return (
        subprocess.check_output(  # noqa: S607
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=str(WORKTREE),
            stderr=subprocess.DEVNULL,
        )
        .decode()
        .strip()
    )


def update_duckdb_rows(new_sha: str) -> int:
    """UPDATE factory_verdicts.git_commit to ``new_sha`` for all rows."""
    with duckdb.connect(str(DB_PATH)) as conn:
        # Show old values for the changelog.
        old = conn.execute(
            "SELECT git_commit, COUNT(*) FROM factory_verdicts GROUP BY git_commit"
        ).fetchall()
        print(f"  before update: {old}")
        conn.execute(
            "UPDATE factory_verdicts SET git_commit = ? WHERE git_commit IS NOT NULL",
            [new_sha],
        )
        new = conn.execute(
            "SELECT git_commit, COUNT(*) FROM factory_verdicts GROUP BY git_commit"
        ).fetchall()
        print(f"  after  update: {new}")
        n_rows_result = conn.execute("SELECT COUNT(*) FROM factory_verdicts").fetchone()
        n_rows = n_rows_result[0] if n_rows_result is not None else 0  # type: ignore[index]
    return int(n_rows)


def update_report_files(new_sha_short: str, new_sha_long: str) -> None:
    """Rewrite the JSON report + MD summary to reflect the new BUILD SHA."""
    payload = json.loads(JSON_PATH.read_text())
    old_short = payload["metadata"].get("git_commit")
    payload["metadata"]["git_commit"] = new_sha_short
    payload["metadata"]["git_commit_long"] = new_sha_long
    # Also rewrite the per-row git_commit so the JSON is consistent with
    # the duckdb state.
    for row in payload.get("verdict_table", []):
        if row.get("git_commit") == old_short:
            row["git_commit"] = new_sha_short
    JSON_PATH.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  JSON report rewritten with git_commit={new_sha_short}")
    # Re-render the markdown by re-running the sweep driver's renderer.
    from scripts.sweep_crypto_native_real_data import render_markdown_summary
    MD_PATH.write_text(render_markdown_summary(payload))
    print(f"  MD summary re-rendered with git_commit={new_sha_short}")


def main() -> int:
    short_sha = get_short_sha()
    long_sha = get_long_sha()
    print(f"BUILD short SHA: {short_sha}")
    print(f"BUILD long  SHA: {long_sha}")
    print("Updating duckdb factory_verdicts rows:")
    n = update_duckdb_rows(short_sha)
    print(f"  total rows: {n}")
    print("Updating JSON + MD reports:")
    update_report_files(short_sha, long_sha)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
