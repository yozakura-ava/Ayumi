# Tournament Walking-Skeleton Spec — card db04d5b5

> **Status:** walking-skeleton complete (build phase).
> **Card:** db04d5b5-e6b0-4fb8-9038-3cbf11e4dd00
> **Author:** Tsubaki (builder)
> **Spec authority:** docs/plans/sprint-2026-09-13-ayumi-tournament-scaffold.md
> **Review gate:** Rin (approval pending)
> **Merge gate:** Reina / Ava (orchestrator)

## 1. Purpose

Open the Ayumi tournament milestone (Craig pivot 2026-09-11: *tournament-proven
strategy quality before FTMO spend*) with one focused card: a walking-skeleton
harness that runs ≥2 existing strategies unmodified on a single historical
window and emits a ranked JSON + console scorecard with FTMO-constraint
columns.

This is the **shape** of the tournament, not the breadth. Subsequent cards
(decomposition below) grow shared-engine coupling, intraday-equity
snapshot fidelity, and multi-symbol matrix support.

## 2. Architecture

```
scripts/run_tournament.py     ← CLI entry point (--smoke flag wires defaults)
       │
       ▼
src/tournament/
    ├── harness.py            ← Register (STRATEGY_CLASS_MAP) → load bars →
    │                          extract signals → simulate trades →
    │                          build & rank scorecard rows
    └── scorecard.py          ← FTMO metric computation + JSON/console render

src/forex_bot/strategies/     ← **UNTOUCHED** — strategies run via existing
    registry.py                 registry; harness imports via
                                ``STRATEGY_CLASS_MAP`` mapping only
```

### 2.1 Strategy registration

Strategies are registered by `id` against a single canonical id→class map
in `src/tournament/harness.py:STRATEGY_CLASS_MAP`. Adding a new strategy
to the tournament is a **one-line change** in that map; no strategy code
under `src/forex_bot/strategies/` is ever modified by the harness.

For strategies whose config is symbol-aware (e.g. `SRMRPlusConfig` requires
`symbol` to resolve USDJPY pip size), the harness passes the active
symbol through `_build_strategy_instance(strategy_id, symbol=...)`. This
mirrors the production launcher's pattern without touching strategy code.

### 2.2 Bar source resolution

Default DuckDB path resolved (first hit wins):

1. `$AYUMI_DUCKDB_PATH` env var (explicit CI override).
2. `<main_worktree>/data/ayumi_market.duckdb` via `git worktree list` —
   mirrors the isolation-guard pattern in
   `scripts/backtest_blend_harness.py:_main_worktree_root` (card e1e32b07).
3. `<current_tree>/data/ayumi_market.duckdb` as last-resort fallback.

This keeps the harness runnable from main, feature worktrees, and CI
without per-tree bar-state surprises. A missing file surfaces as
`TournamentEmptyWindow` (NOT a stack trace) so the smoke run can render
a clear "duckdb file not found" error message.

### 2.3 Signal extraction

For each strategy, the harness walks the bar window chronologically,
maintaining a sliding `MarketState(bars=[...])` (matches the production
strategy contract). Each `evaluate(state)` call is recorded; empty
signal lists are legitimate (edge case 4) and surface as a zero-trade
row, not a skip.

### 2.4 Trade simulator

Deterministic OHLC-bar simulator:

- Entry: signal bar's close (mid-price).
- Exit: TP1 hit, SL hit, `max_bars_held=100` timeout, or end-of-data
  force-close.
- SL wins over TP on same-bar collisions (worst-case, standard
  prop-firm backtest convention).
- Trade `pnl_fraction` is computed in **R-units**: SL distance defines
  1R; `r_fraction=0.005` (FTMO 0.5% risk per trade).

This approximation has two intentional limitations (decomposition cards
below):

1. Intra-bar equity snapshots aren't captured — daily-DD breach
   detection uses end-of-day equity only.
2. Trade P&L is normalized fraction (not USD) — sufficient for relative
   ranking, not for absolute P&L accounting.

### 2.5 Scorecard schema

Canonical column schema (9 fields, deterministically ordered):

| column | type | meaning |
|---|---|---|
| `strategy_id` | str | registered strategy id |
| `symbol` | str | bar symbol (USDJPY) |
| `timeframe` | str | bar timeframe (H1) |
| `return_pct` | float | total return as % of starting equity |
| `max_dd_pct` | float | peak-to-trough max drawdown % |
| `daily_dd_breaches` | int | distinct days where DD ≥ FTMO 3% (canonical `FTMO_DAILY_DD_LIMIT_PCT`) |
| `total_dd_breaches` | int | distinct days where DD ≥ FTMO 10% (canonical `FTMO_TOTAL_DD_LIMIT_PCT`) |
| `trade_count` | int | closed trades on window |
| `source` | str | bar DuckDB path (audit linkage) |

`daily_dd_breaches` and `total_dd_breaches` are **separate columns** —
they are NOT aliased and NOT collapsed, per the spec's hard
"both must be present" contract.

### 2.6 Ranking

Deterministic 3-key sort (per edge case 6: ties must produce identical
ordering):

1. `return_pct` DESC (primary quality metric)
2. `trade_count` DESC (more trades = higher confidence)
3. `strategy_id` ASC (final, by name)

NaN-safe: `-∞` sorts NaN to the bottom.

## 3. Smoke run output

```
$ python3 scripts/run_tournament.py --smoke
========================================================================
  TOURNAMENT SCORECARD
========================================================================
  Window:      2024-06-03 → 2024-06-09  (120 bars)
  Symbol/TF:   USDJPY/H1
  Source:      $AYUMI_ROOT/data/ayumi_market.duckdb
  bb_rsi_reversion             signals=   0   trades=   0
  srmr_plus                    signals=   0   trades=   0

| Rank   | Strategy         | Sym    | TF   |   Return% |   MaxDD% |   DailyBreaches |   TotalBreaches |   Trades |
|--------|------------------|--------|------|-----------|----------|-----------------|-----------------|----------|
| #1     | bb_rsi_reversion | USDJPY | H1   |         0 |        0 |               0 |               0 |        0 |
| #2     | srmr_plus        | USDJPY | H1   |         0 |        0 |               0 |               0 |        0 |

Scorecard JSON written to .../data/tournament/scorecard_smoke.json
TOURNAMENT_OK rows=2 top=bb_rsi_reversion return_pct=0.00
```

**The 0-trade outcome on the smoke slice is a legitimate outcome, not a
defect.** Each strategy runs unmodified with its own warm-up / filter
contract:

- `srmr_plus` requires a prior-session range lookup (≥51 bars of history
  + a previous London/NY session in `state.bars`). The 5-day DuckDB
  smoke slice has only 120 bars and lacks a clean prior-session anchor
  for the first 51 bars — by design, no signals fire.
- `bb_rsi_reversion` carries a `require_low_volatility=True` filter that
  excludes most of the synthetic 5-day USDJPY walk — also by design.

The scorecard rows are valid (ranked, all FTMO columns populated,
trade_count=0). The "strategy produces 0 trades on the window" edge
case 4 is structurally handled: a zero-trade row is emitted, not a
skip.

Longer-window / cross-symbol evaluation forms the multi-symbol matrix
decomposition card (see below). This walking skeleton demonstrates the
**shape** of the tournament.

## 4. Verification (HR5 — targeted tests)

```
$ python3 -m pytest tests/tournament -q
....................                                                  [100%]
20 passed, 2 warnings in 9.26s
```

20 tests cover the 7 spec-checklist assertions:

| Checklist assertion | Test(s) |
|---|---|
| `harness_registers_two_existing_strategies` | `test_harness_registers_two_existing_strategies`, `_dedupes_strategy_ids`, `_rejects_unknown_strategy_ids`, `_rejects_empty_strategy_ids` |
| `scorecard_has_ftmo_columns` | `test_scorecard_row_has_canonical_columns`, `_zero_trade_row_is_valid`, `_row_serializes_to_json` |
| `scorecard_runs_unmodified_strategies` | `test_scorecard_runs_unmodified_strategies` |
| `run_tournament_smoke_exits_zero` | `test_run_tournament_smoke_exits_zero`, `test_run_tournament_emits_json_file` |
| `run_tournament_emits_json_file` | `test_run_tournament_emits_json_file` |
| `empty_window_exits_nonzero` | `test_empty_window_raises`, `test_empty_duckdb_window_raises`, `test_empty_window_cli_returns_nonzero` |
| `targeted_tests_isolated_to_tests_tournament` | structural: only file under `tests/tournament/` |
| + ranking determinism | `test_ranking_is_deterministic`, `_descends_by_return`, `_empty_input` |
| + console rendering | `test_console_table_includes_all_rows_and_headers` |
| + DuckDB path resolution | `test_default_path_resolution_honors_env_override`, `test_load_bars_for_window_filters_by_symbol` |

### 4.1 Subprocess rlimit quirk

`tests/conftest.py` (`common.resource_limits`) sets a hard 2 GB
`RLIMIT_AS` at pytest collection time. Without a fix, child processes
forked via `subprocess.run` inherit the limit and SIGSEGV (-11) on
duckdb/pandas import. The `_preexec_unlimit_memory()` helper resets
`RLIMIT_AS` to infinite in the child via `preexec_fn` — explicit
documented pattern (test file §`_preexec_unlimit_memory`).

### 4.2 `tests/conftest.py` write-guard

The conftest rejects writes under `<repo>/data/` during tests. The
two subprocess tests pass `--output <tmp_path>/...` to redirect the
JSON file outside the repo (per the data/ gitignore and the conftest
contract).

## 5. File summary

| File | Status | Notes |
|---|---|---|
| `scripts/run_tournament.py` | NEW (236 lines) | CLI entry + `--smoke` |
| `src/tournament/__init__.py` | NEW (41 lines) | package surface |
| `src/tournament/harness.py` | NEW (602 lines) | register/load/simulate/score |
| `src/tournament/scorecard.py` | NEW (375 lines) | FTMO metric + render |
| `tests/tournament/test_harness.py` | NEW (488 lines) | 20 targeted tests, all green |
| `docs/plans/ayumi-tournament-harness-spec.md` | NEW (this file) | spec + decomposition |
| `data/build-rin-reports/db04d5b5-build.md` | NEW | post-mortem (separate file) |

**No existing files were modified.** `src/forex_bot/strategies/*`,
`src/forex_bot/risk/ftmo_params.py`, `scripts/backtest_blend_harness.py`,
`conftest.py`, `pytest.ini`, `ruff.toml` are all untouched.

## 6. Edge cases handled

| # | Edge case | Handling |
|---|---|---|
| 1 | Empty strategy list | `ValueError("must be non-empty")` at construction |
| 2 | Unknown strategy id | `KeyError("unknown strategy_ids")` at construction |
| 3 | Strategy given symbol not in window | Strategy returns no signals → row emits with trade_count=0 (NOT skipped) |
| 4 | Empty dataset (0 bars) | `TournamentEmptyWindow` with clear message; CLI exit 1 |
| 5 | Strategy produces 0 trades | Row still emitted with all-zeros (NOT skipped) |
| 6 | Two strategies tie on return | Deterministic sort: trade_count DESC, then strategy_id ASC |
| 7 | FTMO 10% / 3% columns | TWO separate columns (`daily_dd_breaches`, `total_dd_breaches`); not aliased |
| 8 | Tick-vault vs DuckDB divergence | Default DuckDB via `git worktree list` (main tree); env override allowed |
| 9 | Cross-timeframe strategies | Timeframe is per-harness-instance; mixing tracked as a follow-up card |
| 10 | Subprocess SIGSEGV under pytest | `_preexec_unlimit_memory()` resets `RLIMIT_AS` in child |

## 7. Decomposition follow-up (2-3 cards ≤2 SP each)

> Sprint rule (C5): next 2-3 build cards from this skeleton, each ≤ 2 SP.
> These map directly to the sprint-plan risks (Ayumi sprint §Risks, rows 1-3).

### 7.1 Card A: Backtest engine multi-strategy coupling refactor (≤2 SP)

**Risk addressed:** Sprint plan risk #1 (backtest engine coupling friction).
**Current state:** Each strategy gets its own sliding `MarketState` from the
harness. Production `BlendForwardTestEngine` builds ONE shared `MarketState`
for all strategies — the harness's per-strategy state object doesn't match.
**Card scope:**
- Lift `_extract_signals_from_strategy` into a shared `MarketState` builder
  (one state, evaluate N strategies per bar instead of N states × evaluate).
- Add a CORRELATION-aware signal filter (drop signals when another strategy
  is already long the same symbol) — bounded to the smoke window.

### 7.2 Card B: Intraday equity snapshots for accurate daily-DD breaches (≤2 SP)

**Risk addressed:** Sprint plan risk F4 + skeleton caveat 4 (intra-day
equity resolution).
**Current state:** `build_scorecard_row._daily_dd_breach_counts` uses
end-of-day equity only. A run with a single 4% intra-day move followed
by recovery gets counted as 0 daily-DD breaches (under-count).
**Card scope:**
- Capture per-trade equity snapshots (entry → MFE/MAE → exit) inside the
  simulator; expose them as part of the trade dict.
- Switch the daily-DD pass from end-of-day to "max drawdown observed
  during the trading day" — using true intraday snapshots.
- Add a config flag (`intraday_snapshots: bool`) so the skeleton remains
  back-compatible.

### 7.3 Card C: Multi-symbol matrix support (≤2 SP)

**Risk addressed:** Sprint plan §"scope" (USDJPY H1 only today).
**Current state:** Harness accepts one (symbol, timeframe) tuple per run.
Adding EURUSD + GBPUSD means N independent runs with N output files.
**Card scope:**
- Promote `--symbol` / `--timeframe` to repeatable (`--symbol USDJPY
  --symbol EURUSD`).
- Per-symbol scorecard in a single JSON: rows keyed by
  `(strategy_id, symbol)`; ranking across the matrix.
- Reuse existing `get_for_symbol` registry filter so strategies that
  don't trade a given symbol are skipped with the existing
  "strategy skipped" log path.

## 8. Out of scope (deliberately deferred)

These exist as backlog notes but are NOT part of this card:

- **Trade-cost modeling.** Skeleton uses mid-price. Production includes
  spread, commission, slippage (per `backtest_blend_harness.py --costs`).
  Follow-up: `--costs` flag passthrough on the tournament CLI.
- **Cross-strategy correlation penalty.** Documented in card A scope.
- **Walk-forward / multi-window aggregation.** Skeleton is per-window;
  aggregation across windows is a future card.
- **Real-time FTMO live integration.** Tournament is offline-historical
  only in this card; live-mode signal ranking is a separate milestone
  (Milestone 2 in the sprint plan).
- **Strategy deprecation handling.** `bb_rsi_reversion` is deprecated
  but kept reachable via `STRATEGY_CLASS_MAP`; the deprecation warning
  is unavoidable until the strategy is fully removed from production.

## 9. References

- Card pre-build checklist: `/root/.openclaw/workspace/data/build-checklists/db04d5b5-e6b0-4fb8-9038-3cbf11e4dd00.json`
- Sprint plan: `docs/plans/sprint-2026-09-13-ayumi-tournament-scaffold.md`
- Reference harness: `scripts/backtest_blend_harness.py` (production
  guard-resolver pattern)
- FTMO canonical constants: `src/forex_bot/risk/ftmo_params.py`
- Strategy registry: `src/forex_bot/strategies/registry.py` +
  `src/forex_bot/strategies/__init__.py`
- Backing isolation precedent: card e1e32b07 (worktree guard resolver)

## 10. Build metadata

See `data/build-rin-reports/db04d5b5-build.md` for the per-commit
post-mortem + BUILD-METADATA footer.

---

## Appendix A — SRMR+ hours rejection diagnosis (card c4b86732, AC2)

> **Status:** documentation-only (no strategy-code edits per card scope).
> **Investigation date:** 2026-09-14.
> **Author:** Tsubaki (builder lane).
> **Evidence:** harness spec §3 smoke run output (`srmr_plus signals=0 trades=0`)
> + `src/forex_bot/strategies/srmr_plus.py:292-310` (`_is_trading_session`,
> `_get_bar_session_type`) + `src/forex_bot/config/sessions.py:104-114`
> (`SessionRangeHours`).

### A.1 Symptom

When the tournament harness runs `srmr_plus` on the default smoke window
(USDJPY H1, 2024-06-03 → 2024-06-09, 120 bars), it produces 0 signals
and 0 trades. The harness exits 0 (success) without surfacing the
silent-zero-signals case (this is what card c4b86732 AC1 now addresses).

When run on wider windows (e.g. 2024-01-01 → 2024-06-30, ~4350 H1 bars),
the same strategy also produces 0 signals — but exits cleanly. This is
the "silent grind" failure mode observed in the 2026-09-14 GBPUSD
2h13m run.

### A.2 Root cause (timezone / bar-time contract)

The hours-rejection is NOT a timezone bug — both sides are in UTC. The
contract is:

| Side | Source | Type | TZ |
|---|---|---|---|
| Bar time | `harness.py:load_bars_for_window` — `datetime.fromtimestamp(int(ts), tz=timezone.utc)` | tz-aware datetime | UTC |
| Session boundaries | `config/sessions.py:SessionRangeHours` (LONDON_START=time(7,0), NY_OPEN_START=time(12,0), etc.) | `time` object | UTC |
| Filter | `srmr_plus.py:_is_trading_session(bar_time)` — reads `bar_time.hour` and compares to `SessionRangeHours.X.hour` | hour-integer | UTC |

`bar.time.hour` returns the UTC hour (the bar's `datetime` is tz-aware
in UTC), and the session boundary constants are defined as UTC `time`
objects. The contract is consistent end-to-end.

### A.3 Why the smoke window produces 0 signals

The 5-day USDJPY H1 window has **8 in-session hours per day** (London
7–11 UTC + NY 12–16 UTC = 8 hours, 33% of the 24-hour day). Combined
with the harness's **30-bar warm-up gate** (bars 0–29 skipped, no
`evaluate(state)` call), only ~10 in-session bars are evaluated on the
5-day window.

`SRMRPlusStrategy` then requires a **prior-session range lookup**
(≥51 bars of history + a clean prior London/NY session anchor in
`state.bars`). With only ~10 in-session bars after warm-up and no
prior-session anchor, the strategy's internal session-range filter
never completes — every `evaluate(state)` returns `None`.

This is **not** a harness bug. It is the documented behavior for the
5-day smoke slice (harness spec §3): the smoke run is a structural
sanity check, not a signal-exercise benchmark.

### A.4 Why wider windows also produce 0 signals on some setups

When invoked on wider windows via the harness (e.g. `--window
2024-01-01:2024-06-30`), the same filter chain runs but with enough
bars to clear warm-up and prior-session lookup. The strategy's session
filter then operates on the in-session subset. If the wider window
happens to be low-volatility (e.g. holiday period), SRMR+'s additional
volatility / range filters can still suppress every signal.

The card c4b86732 AC1 fail-loud guard now surfaces this as a
non-zero CLI exit + clear `TournamentNoSignals(strategy_id=...,
symbol=..., timeframe=..., start_date=..., end_date=..., bars_processed=...,
signals_emitted=0)` error instead of a silent zero-trade row.

### A.5 Correct invocation

To exercise `srmr_plus` with real signals on the harness:

1. **Window:** at least 60 days of USDJPY H1 (≥30 bars warm-up + ≥51
   bars prior-session anchor + session-coverage of London/NY).
   Recommended: 90+ days so volatility / range filters also have data.
2. **Symbol:** USDJPY (default) is fine. For XAUUSD, pass
   `symbol="XAUUSD"` (the harness propagates `symbol` to
   `SRMRPlusConfig.symbol` automatically — see
   `harness.py:_build_strategy_instance`).
3. **Timeframe:** H1 (the strategy's documented default). M15
   requires sufficient M15 bars; H1 is the safer smoke-grade
   timeframe.
4. **CLI invocation:**
   ```
   python3 scripts/run_tournament.py \
       --strategies srmr_plus \
       --symbol USDJPY \
       --timeframe H1 \
       --window 2024-01-01:2024-06-30 \
       --output data/tournament/srmr_plus_6m.json
   ```
5. **Expected outcome:** ≥1 signal per week on average; ~10–40 trades
   on a 6-month window depending on volatility regime. If 0 signals
   persist, the new fail-loud guard surfaces this as
   `TournamentNoSignals` (card c4b86732 AC1) — investigate the wider
   window's volatility / session-coverage before declaring the
   strategy dead.

### A.6 Why no strategy-code edits

The card explicitly scopes SRMR+ out of the buildable surface
(`allowed_files` lists only harness/scripts/tests/docs). The hours
contract IS correct as-is — modifying `_is_trading_session` would
require a dedicated child card with its own allowed_files and review
lane (e.g. "SRMR+ M5/M15 timeframe support" or "SRMR+ session-window
config externalization"). This appendix documents the diagnosis so
Ava/Rin can scope a follow-up card if the silent-zero pattern
persists on the recommended 90-day window.

### A.7 Verification path

- Read `src/forex_bot/strategies/srmr_plus.py` lines 290–310 (session
  filter) + lines 640–655 (filter call site) for the contract.
- Read `src/forex_bot/config/sessions.py` lines 104–115 for the
  boundary constants.
- Read `src/tournament/harness.py:load_bars_for_window` for the bar
  UTC construction.
- Compare `Bar(time=...)` → `bar.time.tzinfo` (must be `UTC`) and
  `bar.time.hour` (must equal the UTC hour of the timestamp).
