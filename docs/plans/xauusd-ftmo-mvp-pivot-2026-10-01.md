# XAUUSD-Only Pivot — FTMO MVP Directive

**Date:** 2026-10-01 · **Author:** Ava (grilling relay with Craig, tree: `xau-ftmo-mvp-20261001`)
**Status:** ACTIVE — supersedes multi-symbol tournament planning until FTMO MVP clears.

## Directive (Craig, 2026-10-01 15:47 EDT)

Ayumi focuses **strictly on gold (XAUUSD)** to reach FTMO MVP. All strategies, testing, and tournaments are XAUUSD-only. Forward test is **frozen** — no changes until strategy integration.

## Resolved decisions (all Craig, via grilling 2026-10-01)

| # | Decision | Ruling |
|---|----------|--------|
| Q1 | Tick gap (XAUUSD ticks end 2026-07-13, 80d stale) | **Backfill from cTrader to present** (bars via existing client scripts, needs parameterization); **tournaments frozen on fixed 2022-01→2026-07 window** for comparability |
| Q2 | Timeframe roster | **Intraday focus: M3, M5, M15, M30, H1** (H4/D1 excluded from matrix) |
| Q3 | Matrix sequencing | **Staged: 3–4 lead strategies × 5 TFs first (~5h), then full 17-strategy roster** |
| Q4 | Bar build | **Build only M3 + M30** from existing ticks (`aggregate_ticks_to_bars.py --symbol XAUUSD --timeframes M3,M30`); reuse existing M5/M15/H1 |
| Q5 | Other symbols (EURUSD/GBPUSD/USDJPY) | **Freeze in place.** No new runs, no purge |
| Q6 | Forward-test integration gate | A strategy **clears FTMO 10% total / 3% daily DD on XAUUSD across ≥2 tournament timeframes** |

## Data state (verified 2026-10-01, live duckdb read)

- XAUUSD ticks: 264,989,167 rows, 2022-01-03 → **2026-07-13** (80-day gap → backfill)
- XAUUSD bars: D1/H1/H4/M15/M5 exist; **M3 + M30 did not exist** — being built 2026-10-01 from ticks (frozen window)
- ⚠️ `bars` table has **mixed timestamp units** (some rows epoch-seconds, some epoch-ms). Any new bar writes must normalize to seconds; fix rides along with the backfill build.
- Tick pipeline health check stale (`tick_stall_state.json` last ran 2026-09-17) — pipeline revival folded into backfill work.

## Strategy Admission Gate (Craig, 2026-10-01 19:08 EDT)

Going forward the tournament only admits strategies that have **cleared backtesting and ML parameter validation** — i.e., already sound in theory. The current 17-strategy roster is mostly textbook indicator strategies (no parameter optimization, no SL/TP tuning, no confluence logic, no trend alignment, no higher-timeframe context), so poor tournament results are expected and not disqualifying for the harness itself. Harness reliability and repeatability matter more than current rankings.

Hardening program implied before re-entry to tournaments:
1. Parameter optimization + ML-validated params (walk-forward, out-of-sample discipline)
2. SL/TP engineering (ATR-based, regime-aware) rather than fixed book defaults
3. Confluence requirements (multiple conditions/areas agreeing before entry)
4. Trend alignment filters (with-trend vs counter-trend posture explicit)
5. Higher-TF metrics folded in (MTF context: HTF trend/volatility gating entries)

Existing validated pipelines: SRF (strategy research framework) top-K re-evaluation + matrix runs exist on the gateway — qualification should flow through that, not ad-hoc.

## Ops rules going forward

1. Tournament runs: XAUUSD only, window `2022-01-15 : 2026-07-01`, TFs M3/M5/M15/M30/H1.
2. Runs execute on node `ava-worker-local` per `ayumi-tournament-run` skill (smoke first, scorecard-verified completion).
3. Non-gold symbols: no new tournaments, no tests; data left untouched.
4. Forward test (`launch_blend_forward_test.py --symbols XAUUSD --only SRMR+ --live`): untouched until Q6 gate is met by a strategy.
5. Grilling artifact: `data/sprint-plans/xau-ftmo-mvp-20261001-grilling.json` (workspace).

## Open build work

- [BUILD] card: parameterize `download_ctrader_data.py` (symbols=XAUUSD, TFs incl. M3/M5/M15/M30/H1, date range CLI) + import bars into duckdb with unit normalization.
