# Ayumi Data Sources

> **Scope:** Reference for the historical and live data feeds that feed the
> Ayumi backtest, forward-test, and live-trading pipelines.
>
> **Audience:** Strategy authors, backtest reviewers, and the Tsukasa build
> lane when extending or replacing a data source.
>
> **Status:** Current. See per-source sections for live-vs-backtest
> coverage and known gaps.

---

## At a glance

| Source            | Use case                       | Format             | Coverage           | Module / entry point                                                       |
| ----------------- | ------------------------------ | ------------------ | ------------------ | -------------------------------------------------------------------------- |
| Dukascopy (bi5)   | Historical tick backtests      | LZMA `>IIIff` blobs | 2015–present       | `src/forex_bot/data/dukascopy_importer.py` → `DukascopyImporter`            |
| Dukascopy (CSV)   | Tick staging (from bi5)        | `timestamp,…` CSV   | Same as bi5        | `scripts/import_ticks.py` → `import_csv()`                                 |
| HistData.com M1   | Legacy M1 bar backtests        | M1 OHLCV CSV       | Variable, ≥10 yrs  | Loaded by `ml.data_source.SQLiteCandleLoader`                              |
| cTrader Open API  | Live tick stream + execution   | Protobuf over TCP  | Live only          | `src/forex_bot/ctrader/` + `ctrader_client.py`                             |
| FRED / ECB / MT5  | Carry signals (regime feature) | Mixed              | Daily              | `src/forex_bot/data/carry_signals.py`                                      |

The rest of this document is focused on the **Dukascopy bi5** path, which
is the reference historical source for tick-level backtests. The other
sources are linked but not detailed here — see `docs/ayumi/data-sources/`
for module-specific docs.

---

## Dukascopy bi5 — tick-level historical data

Dukascopy's REST datafeed (`https://datafeed.dukascopy.com/datafeed`)
exposes one file per hour per symbol per day. Each `.bi5` file is
LZMA-compressed and decompresses to a stream of fixed 20-byte records.

### Binary format

Each 20-byte record is laid out as big-endian `>IIIff`:

| Field        | Type     | Meaning                                            |
| ------------ | -------- | -------------------------------------------------- |
| `time_ms`    | `uint32` | Milliseconds since the start of the hour (UTC)     |
| `ask_scaled` | `uint32` | Ask price × 1 000 000                              |
| `bid_scaled` | `uint32` | Bid price × 1 000 000                              |
| `ask_vol`    | `float32`| Ask-side volume for this tick                      |
| `bid_vol`    | `float32`| Bid-side volume for this tick                      |

The 1 000 000 divisor is the same across FX pairs and XAUUSD — verified
empirically against JForex reference data.

### URL pattern

```
{BASE_URL}/{SYMBOL}/{YYYY}/{MM}/{DD}/{HH}h_ticks.bi5
```

For example:

```
https://datafeed.dukascopy.com/datafeed/EURUSD/2024/06/03/12h_ticks.bi5
```

A 404 is a normal "no data" response (weekend, holiday, pre-launch hour)
and is **not** an error. LZMA failures and connection drops are errors
and trigger the retry policy.

### Module API

The clean Python entry point is `DukascopyImporter` in
`src/forex_bot/data/dukascopy_importer.py`:

```python
from datetime import date
from pathlib import Path

from data.dukascopy_importer import DukascopyImporter

importer = DukascopyImporter(output_dir=Path("data/ticks"))
ticks = importer.download_day("EURUSD", date(2024, 6, 3))
importer.write_csv("EURUSD", date(2024, 6, 3), ticks)
```

Key methods:

| Method                                | Purpose                                                                  |
| ------------------------------------- | ------------------------------------------------------------------------ |
| `DukascopyImporter(...)`              | Construct; `output_dir` is created if missing, network is **not** hit.   |
| `.fetch_hour_ticks(symbol, dt, hour)` | Download + parse a single hour; raises `DukascopyFetchError` on failure. |
| `.download_day(symbol, dt)`           | All 24 hours; skips weekends without hitting the network.                |
| `.download_range(symbol, start, end)` | Iterator over daily buckets; skips days whose CSV already exists.        |
| `.write_csv(symbol, dt, ticks)`       | Persist one day's ticks to `output_dir`.                                 |
| `DukascopyImporter.parse_bi5(...)`    | Pure parser — no I/O, no network. Used by tests.                         |

Configuration knobs (all keyword-only on the constructor):

* `rate_limit_rps` (default 4.0) — minimum interval between requests.
* `max_retries` (default 3) — total attempts before giving up.
* `retry_backoff` (default `(5, 15, 30)` seconds) — sleep before each retry.
* `base_url` — overridable for test fixtures and offline mirrors.

### CSV output format

`write_csv` produces one file per day:

```
filename : {SYMBOL}_{YYYY}{MM}{DD}.csv
columns  : timestamp, instrument, bid, ask, bidVol, askVol
example  : 1717416000123,EURUSD,1.085,1.0852,1.5,2.0
```

`timestamp` is a wall-clock UTC epoch in **milliseconds** (matches the
`DuckDB` `ticks` table layout; see `scripts/import_ticks.py`).

### Pipeline (end-to-end)

1. **Harvest** — `scripts/harvest_ticks_overnight.py` walks the date range
   per symbol, downloads hour blobs, decodes them with the same `>IIIff`
   logic, and writes per-day CSVs to
   `tools/dukascopy-harvester/output/`. It is the long-running,
   rate-limited overnight job.
2. **Refactor target** — `scripts/harvest_ticks_overnight.py` is being
   migrated to use `DukascopyImporter.download_range(...)` so the harvest
   logic, the parser, and the CSV layout live in one tested module
   instead of three different script-level copies.
3. **Import** — `scripts/import_ticks.py` reads the CSVs, bulk-inserts
   them into the `ticks` table in `data/ayumi_market.duckdb`, and
   generates the M1 / M5 / … bar aggregations.
4. **Backtest** — Strategies read the `ticks` table via the DuckDB
   connection; bars are pre-aggregated for non-tick strategies.

### Rate limits, retries, and known failure modes

* **Rate limit:** Default 4 req/s. The `harvest_ticks_overnight.py`
  script lowers to ~3 req/s during weekend-free bulk runs to stay well
  below the published 30 req/s soft cap.
* **404 = "no data":** Hour queries for weekend hours return 404; the
  importer treats this as an empty hour and continues.
* **LZMA failures:** A small number of bi5 files (≈ 0.01 % in 2023–2024
  sweeps) fail LZMA decompression. The importer logs and skips the
  hour; harvest continues with the next day.
* **Network drops:** Connection errors and HTTP 5xx trigger the retry
  policy. After `max_retries` failures the importer raises
  `DukascopyFetchError`; `download_range` logs and skips the day, leaving
  the missing CSV for the next pass to fill.

### Coverage

| Symbol  | Start       | End         | Notes                               |
| ------- | ----------- | ----------- | ----------------------------------- |
| GBPUSD  | 2015-04-09  | Present     | Earliest in current harvest.        |
| EURUSD  | 2003-05-04  | Present     | Longest history.                    |
| XAUUSD  | 2004-08-30  | Present     | Raw values are larger (~2 × 10⁹).   |

Other pairs (USDJPY, AUDUSD, …) are available on demand; add a new
entry to the `JOBS` list in `scripts/harvest_ticks_overnight.py` and
the same importer handles them.

### How the live pipeline differs

| Dimension      | Dukascopy bi5 (backtest)  | cTrader Open API (live)               |
| -------------- | ------------------------- | ------------------------------------- |
| Format         | `>IIIff` per tick        | Protobuf `SpotEvent` stream           |
| Latency        | Day-aligned, batch        | Sub-second, push                      |
| Cost model     | Empirical (bid/ask split) | Variable spread + market impact       |
| Gaps           | Weekend / holiday         | Connection drops + provider throttling |

See `docs/forex/tick-quality-comparison-2026-07.md` for a side-by-side
audit and the implications for live-vs-backtest divergence.

---

## Tests and validation

* Unit tests for the bi5 parser, URL builder, CSV writer, and
  weekend-skip logic live in
  `tests/unit/data/test_dukascopy_importer.py` (29 tests, all offline —
  network is mocked). Run with:

  ```bash
  cd $AYUMI_ROOT
  python3 -m pytest tests/unit/data/test_dukascopy_importer.py -v
  ```

* The bi5 parser is exercised against a synthetic, LZMA-compressed blob
  in tests so format regressions are caught without contacting the
  upstream API.

* End-to-end smoke tests live in the overnight harvest log:
  `tools/dukascopy-harvester/overnight_rest_log.txt`.

## See also

* `src/forex_bot/data/dukascopy_importer.py` — module reference.
* `scripts/harvest_ticks_overnight.py` — long-running harvester.
* `scripts/import_ticks.py` — CSV → DuckDB importer.
* `docs/runbooks/data-pipeline.md` — operational runbook.
* `docs/forex/tick-quality-comparison-2026-07.md` — live vs backtest audit.
* `docs/ayumi/data-sources/` — module-level data source docs (carry, COT).
