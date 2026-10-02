# Phase 8a — DuckDB Spike Benchmark Report

**Date:** 2026-07-08
**Card:** d261d8a1-d8c1-42d5-bfaa-e8313affc6e6
**Builder:** Ava (subagent, depth 1/1)
**Goal:** Validate DuckDB is a viable analytics/migration target for Ayumi's research path before committing to a full migration.

---

## TL;DR

| Question | Answer |
|---|---|
| Does `duckdb` install and import cleanly? | ✅ Yes (1.5.4) |
| Can DuckDB ingest the existing XAUUSD M15 CSV? | ✅ Yes, 74,324 rows in ~85 ms |
| Are analytical queries correct? | ✅ Yes, monthly averages match pandas to 1e-6 |
| Is DuckDB faster than pandas on this workload? | ⚠️ Mixed — wins on GROUP BY, loses on a single boolean mask (data fits in RAM) |
| Does concurrent read on the same file work? | ✅ Yes (2 read-only connections, no errors) |
| Can DuckDB read our existing Parquet files? | ✅ Yes, 76,529 rows across 5 files in ~59 ms |
| Can DuckDB ATTACH our existing SQLite DB? | ✅ Yes, 7 tables visible, cross-DB JOIN works |
| Should we migrate to DuckDB? | ✅ Yes for the **research/analytics path**. No for the hot trading path (pandas is still fast enough at this scale and adding DuckDB as a runtime dependency for the live bot is unjustified). |

**Verdict: PROCEED to Phase 8b (migration design).** The technology works, the ergonomics are excellent, and DuckDB's "one engine, many formats" story (CSV → Parquet → SQLite, all from SQL) is exactly what the Quest/strategy-test pipeline needs.

---

## 1. Setup

```bash
source $AYUMI_ROOT/.venv/bin/activate
pip install duckdb          # 21.5 MB wheel, ~3 s
# → Successfully installed duckdb-1.5.4
```

- **duckdb** not added to `requirements.txt` (per task constraint). Recorded separately in `requirements-duckdb.txt`.
- Python 3.12, pandas 3.0.2, DuckDB 1.5.4.
- No existing source files modified.

## 2. Dataset

| Property | Value |
|---|---|
| File | `data/forex/historical/XAUUSD_M15.csv` |
| Size | 4,342,794 bytes (4.3 MB) |
| Rows | 74,324 (matches `wc -l` − 1) |
| Columns | `Date, Open, High, Low, Close, Volume` |
| Date range | 2023-01-02 18:00 → 2026-04-24 (per `Date`) |
| Schema after `read_csv_auto` | `Date TIMESTAMP, Open DOUBLE, High DOUBLE, Low DOUBLE, Close DOUBLE, Volume BIGINT` |

DuckDB auto-detected `Date` as TIMESTAMP correctly. The task example used `timestamp` and `close` (lowercase) — actual CSV columns are `Date` and `Close`. All queries in this report use the real column names; DuckDB also accepts unquoted case-insensitive references but we quote for clarity.

## 3. Benchmark Methodology

- `time.perf_counter()`-based timing, **best of 3** runs per query, fresh process state.
- DuckDB: `:memory:` connection, query result fetched with `.fetchall()`.
- pandas: `pd.read_csv(...)` once, queries on the resident DataFrame.
- Both pipelines had data resident in RAM (DuckDB via the materialized `bars` table, pandas via the DataFrame) — this is the realistic steady-state for a research/analytics session.
- Same CSV, same semantic operations, same precision (DOUBLE / float64).

The full benchmark script lives at `/tmp/duckdb_spike.py` (spike artifact, not committed).
Raw timings are in `reports/duckdb-spike-results.json`.

## 4. Results

### 4.1 Ingestion

| Operation | DuckDB | pandas | Notes |
|---|---:|---:|---|
| CSV → table/DataFrame | **85.19 ms** | 71.64 ms | pandas slightly faster — both are essentially `mmap + parse` here |

Both pipelines produce **74,324 rows** (matches `wc -l` of 74,325 minus the header). ✅

### 4.2 Analytical Queries (best of 3)

| Query | DuckDB | pandas | Speedup |
|---|---:|---:|---:|
| `SELECT COUNT(*) FROM bars` | 0.50 ms | ~0 ms | n/a (trivial) |
| Monthly AVG(close) — `GROUP BY date_trunc('month', Date)` | **6.93 ms** | 20.36 ms | **2.94×** faster |
| Date-range filter — `WHERE Date BETWEEN '2024-01-01' AND '2024-06-01'` | 7.82 ms | **1.27 ms** | 0.16× (pandas wins — boolean mask on in-memory DataFrame is hard to beat) |
| Hourly AVG(High − Low) — `GROUP BY hour(Date)` | **2.14 ms** | 3.74 ms | **1.75×** faster |

**Correctness check:** Monthly `AVG(close)` values agree with pandas to **max abs diff = 0.0** across all 41 months. ✅

### 4.3 Interpreting the Numbers

The 10× speedup target in the card is **not hit on this dataset**. Two reasons, both expected:

1. **Data fits in memory.** 74K rows × 6 columns is ~3.5 MB of numeric data. Both engines are effectively computing on hot L3 cache. DuckDB's vectorized execution shines when there's data to crunch; here there isn't.
2. **pandas is already very good at small-data single-pass operations.** A boolean mask on an in-memory DataFrame is a pointer shuffle — pandas can do this in microseconds.

Where DuckDB **does** win consistently:
- Aggregations with `GROUP BY` (vectorized hash tables, parallel scan).
- Anything that pushes the predicate into the scan (columnar pushdown, min/max statistics).

Where DuckDB will pull ahead on our actual workload (estimated, based on published benchmarks):
- **Backtests over multi-year, multi-pair data** — millions of rows.
- **Parameter sweeps** — running the same aggregation 1000× with different parameters (DuckDB's prepared statements + parallelism).
- **Memory pressure** — DuckDB's out-of-core processing won't OOM on a 2 GB CSV where pandas will.

### 4.4 Concurrent Reads

```
[4] Concurrent reads — two DuckDB connections on same persistent file
  2 readers concurrent    wall=16.14ms  per-thread=[14.09ms, 15.70ms]
  no errors — both readers returned valid results
```

Two `read_only=True` connections to the same on-disk DuckDB file ran `SELECT COUNT(*), AVG(Close) FROM bars` simultaneously. Both returned identical results (74,324 rows, avg 2811.98). Wall time = ~16 ms vs ~14 ms for a single thread — minimal contention.

**Configuration requirement:** ALL connections to the same file must use the same `read_only` setting. A `read_only=False` writer + `read_only=True` reader on the same file raises:
```
Connection Error: Can't open a connection to same database file
with a different configuration than existing connections
```
This is by design (DuckDB doesn't want mixed read/write semantics on one file). Workaround: route analytics through a separate file from the writer, or use `:memory:` on the reader side and refresh periodically.

### 4.5 Parquet

```
[5] Parquet read via glob
  DuckDB  SELECT * FROM read_parquet(glob)   best=59.14ms
  Parquet total rows : 76,529
  Per-file breakdown:
    AUDUSD_1h.parquet     17,323 rows
    EURUSD_1h.parquet     17,223 rows
    GBPUSD_1h.parquet     17,225 rows
    USDJPY_1d.parquet      7,632 rows
    USDJPY_1h.parquet     17,126 rows
```

- 5 Parquet files, **76,529 total rows**, read in 59 ms — that's **~1.3 M rows/sec** scan rate, **zero code** (just `read_parquet('glob/*.parquet')`).
- DuckDB auto-sniffs the schema; columns are `Open, High, Low, Close, Volume, timestamp`. The `filename` virtual column lets you attribute rows back to source files without an external manifest.
- Parquet is **not** the current source of truth in this codebase (the CSV in `data/forex/historical/` is) — but the read path works, which means Phase 8b can convert CSV → Parquet and benefit from the columnar compression (~10× smaller on disk, scan speedup on selective queries).

### 4.6 SQLite ATTACH

```
[6] SQLite ATTACH  ($AYUMI_ROOT/data/trading.db)
  ATTACH succeeded
  Tables visible: ['_schema_version', 'bars', 'daily_summary',
                   'equity_curve', 'rolling_metrics', 'sqlite_sequence', 'trades']
  equity_curve columns: ['id', 'timestamp', 'balance', 'equity',
                         'unrealized_pnl', 'open_positions', 'daily_pnl', 'metadata']
  equity_curve sample (3 rows):
    (1, '2026-04-23T01:35:22.711108+00:00', 10000.0, 10000.0, 0.0, 0, None, None)
    (2, '2026-04-23T01:36:12.832121+00:00', 10000.0, 10000.0, 0.0, 0, None, None)
    (3, '2026-04-23T01:36:22.713954+00:00', 10000.0, 10000.0, 0.0, 0, None, None)
  Cross-DB query (parquet + sqlite_db.equity_curve): (76529, 24239, 584)
```

- ATTACH syntax works as documented. ⚠️ **Catalog quirk:** tables are exposed under `main.sqlite_master` (not `sqlite_db.sqlite_master`) for table discovery, but data access uses `sqlite_db.<table_name>`. This is documented but easy to miss.
- `equity_curve` has 584 rows (good — non-empty, lets us verify reads).
- Cross-DB query joining Parquet data with SQLite `equity_curve` in a single SQL statement works. This is the killer feature for migration: **no ETL, no exports, one engine queries everything**.

**Observation (not part of spike scope):** `data/trading.db` now has a `bars` table in addition to the original 5 tables (`trades, equity_curve, daily_summary, rolling_metrics, _schema_version`). This table did not exist when the spike was specced. Someone (likely the autobuild pipeline or a previous session) has been writing bar data into SQLite. Worth flagging to the orchestrator — if a CSV → SQLite migration is already in flight, Phase 8b should account for it.

---

## 5. Spikes-vs-Reality Caveats

1. **The dataset is small.** 74K rows won't reveal DuckDB's real advantage. The card explicitly framed this as a "spike — prove the technology." ✅ proven. A follow-up benchmark on the full multi-pair M15 dataset (~5 files × 74K = 370K rows) and on multi-year M5 data would be the natural next step.

2. **Single-process, in-memory only.** We didn't test persistent DuckDB files in a long-running process, MVCC, or backup/restore. Those are migration-phase concerns.

3. **No write path tested.** This spike was read-only. The bar-data writer in `src/forex_bot/` (if any) needs separate validation before Phase 8b.

4. **No type-stress test.** All numeric columns were clean doubles. Real-world Dukascopy CSVs sometimes have malformed rows; DuckDB's CSV sniffer has `ignore_errors=true` and `sample_size` knobs but they weren't exercised.

## 6. Recommendations for Phase 8b

1. **Adopt DuckDB for the analytics/research path** (Quest, strategy sweeps, ICIR computations, walk-forward analysis). This is where the cost-of-migration is lowest and the value is highest.

2. **Convert CSV → Parquet for the historical data lake.** Estimated ~10× compression, faster selective queries, native DuckDB read path. The `data/forex/parquet/` directory already exists with 5 files — extend it.

3. **Keep SQLite as the OLTP store for trades, equity_curve, etc.** DuckDB's ATTACH makes coexistence zero-cost: query SQLite from DuckDB without copying data. No need to migrate the write path.

4. **Do NOT add DuckDB to the live trading bot's runtime deps.** The bot's data needs are small and time-sensitive; pandas + numpy are sufficient. DuckDB belongs in the research/analytics tier.

5. **Standardize on a single SQL dialect** for analytics. DuckDB SQL is close to PostgreSQL but has its own datetime functions (`date_trunc`, `epoch`, `epoch_ms`). Document the dialect in `docs/forex/duckdb-style.md`.

6. **Surface the mystery `bars` table** in `data/trading.db` to the orchestrator before Phase 8b starts. If it's the start of an in-flight migration, the plan needs to absorb it.

## 7. Artifacts

| Path | Purpose |
|---|---|
| `requirements-duckdb.txt` | Pinned dependency, NOT yet merged into `requirements.txt` |
| `reports/duckdb-spike-results.json` | Machine-readable timing data (all raw numbers, per-thread, per-query) |
| `reports/duckdb-spike-benchmark-2026-07-08.md` | This report |
| `/tmp/duckdb_spike.py` | Spike benchmark script (transient, not committed) |

## 8. Acceptance Criteria

| Criterion | Status |
|---|---|
| `duckdb` package installed and importable in `.venv` | ✅ 1.5.4 |
| CSV import produces correct row count (vs `wc -l`) | ✅ 74,324 = 74,325 − 1 |
| Analytical queries work and return correct results | ✅ monthly AVG matches pandas to 1e-6 |
| Timing comparison: DuckDB vs pandas | ✅ Best of 3, see §4.2. 10× target not hit on this size class — see §4.3 |
| Concurrent reads work without error | ✅ 2 read-only connections, 16 ms wall |
| SQLite ATTACH works | ✅ 7 tables visible, cross-DB JOIN executed |
| Benchmark report written | ✅ this file |

---

**Status: COMPLETE** ✅

Builder reports ready for orchestrator review. Recommend Ava promote `requirements-duckdb.txt` to `requirements.txt` after reading §6 recommendations and investigating the new `bars` table in `data/trading.db`.