# Backtesting Strategy Runbook

> **Last updated:** 2026-07-12
> **Owner:** Ava (orchestration) + Tsukasa (execution)
> **Scope:** Ayumi SRF (Strategy Research Framework) backtesting workflow

This runbook captures the standard backtesting procedure so future sessions don't reinvent the wheel. Update this file when the process changes — don't keep tribal knowledge in head or session memory.

---

## Quick Reference

| Action | Command |
|--------|---------|
| Aggregate ticks → bars (one-time per symbol/TF) | `python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD` |
| List what's aggregated | `python3 scripts/aggregate_ticks_to_bars.py --list` |
| Re-aggregate (force) | `python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD --force` |
| Run a sweep on one symbol | `python3 scripts/run_srf_sweep.py --pair GBPUSD --timeframes M15,H1,M5 --windows 5` |
| View sweep results | `python3 -c "import duckdb; ..."` (see "Reading Results" below) |
| Run portfolio blend | `python3 scripts/run_portfolio_blend.py --config configs/blend.json` |
| Reset sweep data | `python3 -c "import duckdb; con=duckdb.connect('data/research/research.duckdb'); ..."` |

---

## Core Architecture

```
Raw ticks (ayumi_market.duckdb.ticks)
    ↓ [aggregate_ticks_to_bars.py — one-time per symbol/TF]
OHLCV bars (ayumi_market.duckdb.bars)
    ↓ [backtest/tick_loader.py — load_bars()]
Strategies consume Bar objects
    ↓ [backtest/walk_forward_runner.py]
Walk-forward results → DuckDB (data/research/research.duckdb)
    ↓ [scripts/run_srf_sweep.py]
Metrics summary → DuckDB
    ↓ [portfolio_blend.py]
Portfolio-level metrics → DuckDB
```

**Key principle:** Once bars are aggregated, **never re-aggregate ticks.** Future loads read from the `bars` table directly.

---

## Step 0: Confirm Tick Data Exists

```bash
ls data/forex/dukascopy/      # CSV tick files
python3 -c "
import duckdb
con = duckdb.connect('data/ayumi_market.duckdb', read_only=True)
print(con.execute('SELECT DISTINCT symbol, count(*), min(timestamp_ms), max(timestamp_ms) FROM ticks GROUP BY symbol').fetchdf())
"
```

**If ticks missing:** See "Adding New Symbols" below. **No CSV fallback for new backtests** — all backtests must use real tick data.

---

## Step 1: Aggregate Ticks → Bars (one-time per symbol/TF)

The `bars` table in `ayumi_market.duckdb` is the single source of truth for backtest data. Pre-aggregate and reuse.

```bash
# Default: aggregates all 7 timeframes (M1, M5, M15, M30, H1, H4, D1)
python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD

# Specific timeframes only
python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD --timeframes M5,M15,H1

# Force re-aggregation (deletes + rebuilds)
python3 scripts/aggregate_ticks_to_bars.py --symbol GBPUSD --timeframes M5 --force

# Verify what's there
python3 scripts/aggregate_ticks_to_bars.py --list
```

**Idempotency:** Re-running without `--force` is a no-op if bars exist. Safe to run repeatedly.

**Spread data:** Aggregation includes real `spread_pips` (avg bid-ask spread per bar). Use this for realistic slippage modeling.

**Time cost:** ~18s per timeframe on 117M GBPUSD ticks. Linear with tick count.

---

## Step 2: Run a Sweep

```bash
# One symbol, multiple timeframes, default 5 windows
python3 scripts/run_srf_sweep.py --pair GBPUSD --timeframes M15,H1,M5 --windows 5
```

**What happens:**
1. For each (strategy, timeframe) combo:
   - Loads bars from DuckDB (CSV fallback only if ticks unavailable — see "CSV Fallback Warning" below)
   - Runs 5-window walk-forward with 70/15/15 train/val/test split
   - Computes aggregated metrics + go/no-go decision
   - Writes to `runs`, `windows`, `metrics_summary` tables
2. Prints summary table to stdout
3. Saves raw results to `/tmp/srf_sweep_<PAIR>_<TIMESTAMP>.json`

**CSV Fallback Warning:** If a symbol isn't in the `bars` table, the loader falls back to `data/forex/historical/<PAIR>_<TF>.csv` files. These have NO spread data, NO volume, and NO session info — results from CSV fallback should be considered preliminary. **Always prefer tick-aggregated bars.**

---

## Step 3: Read Sweep Results

```python
import duckdb
con = duckdb.connect('data/research/research.duckdb', read_only=True)

# Top 20 by PF
df = con.execute("""
    SELECT r.pair, r.strategy_name, r.timeframe, 
           round(m.mean_profit_factor, 2) as PF,
           round(m.mean_win_rate*100, 1) as WR_pct,
           m.total_trades, m.windows_passed, m.windows_total, m.go_nogo
    FROM metrics_summary m
    JOIN runs r ON m.run_id = r.run_id
    ORDER BY m.mean_profit_factor DESC
    LIMIT 20
""").fetchdf()
print(df)
```

**Interpret `go_nogo`:**
- `go` — meets all 5 gates
- `watch` — partial pass (e.g., PF met but trade count low)
- `no-go` — fails one or more hard gates

---

## Go/No-Go Gates (srf/gonogo.py)

```python
MIN_PROFIT_FACTOR = 1.3        # hard
MIN_WINDOWS_PASSED = 3         # hard (of 5)
MAX_PARAM_STABILITY_CV = 0.3   # hard
MAX_OOS_SHARPE_DECAY = 0.5     # hard
MIN_TRADES_PER_WINDOW = 15     # hard — too high for monthly windows, see "Sparse Strategies" below
```

**The 15-trade gate is calibrated for daily/weekly test windows.** With 30-day test windows at M15/H1, only 5-10 trades/window is realistic. Two paths forward:
- **Sparse swing trades** (M15/H1): drop per-strategy gate to 5 trades/window, raise WR floor to 65%
- **High-frequency trades** (M3/M5): keep 15 trades/window gate

---

## Portfolio Blend

`src/forex_bot/backtest/portfolio_blend.py` (1140 LOC) handles multi-strategy blending:

**Built-in weight methods:**
- `equal_risk` — equal risk contribution per strategy
- `inverse_variance` — lower vol strategies get more weight
- `profit_factor` — higher PF strategies get more weight
- `sharpe_weighted` — Sharpe-weighted
- `combined_score` — composite

**Built-in FTMO criteria:**
```python
FTMO_CRITERIA = {"win_rate": 55.0, "profit_factor": 1.3, "sharpe_ratio": 0.5}
```

**Correlation handling:** Computes pairwise correlation matrix, filters out strategies with >0.7 correlation to avoid double-exposure.

**Default risk params:**
```python
risk_per_trade_pct = 0.01      # 1% per trade
max_daily_drawdown_pct = 0.02  # 2% daily
max_total_drawdown_pct = 0.05 # 5% total
max_open_trades = 1            # per single backtest — need to adjust for blend
```

---

## Adding New Symbols

**1. Download tick data from Dukascopy:**
- Tool: `scripts/dukascopy_download.py` (see existing tick files in `data/forex/dukascopy/` for format)
- Time range: 2020-01-01 to present (5 years ideal for monthly walk-forward windows)
- Tick format: `XAUUSD_M1_2015-01.csv` per month, ~50MB-1GB per year per symbol

**2. Import ticks into DuckDB:**
```bash
python3 scripts/import_ticks.py --symbol XAUUSD --source data/forex/dukascopy/
```

**3. Aggregate ticks → bars:**
```bash
python3 scripts/aggregate_ticks_to_bars.py --symbol XAUUSD
```

**4. Sweep:**
```bash
python3 scripts/run_srf_sweep.py --pair XAUUSD --timeframes M15,H1,M5 --windows 5
```

---

## Adding New Strategies

**1. Create strategy file** in `src/forex_bot/strategies/<name>.py`:
- Must inherit base strategy interface (see existing strategies for pattern)
- Must implement `evaluate(state: MarketState) -> StrategySignal | None`
- Config dataclass at top with sensible defaults

**2. Register in sweep script:**
Edit `scripts/run_srf_sweep.py`, `get_strategies_for_pair()` function. Add factory lambda.

**3. Verify it fires signals** (sanity check before sweep):
```python
import sys; sys.path.insert(0, 'src/forex_bot'); sys.path.insert(0, 'src')
from strategies.<name> import <Strategy>, <Config>
from backtest.tick_loader import load_bars
from core.types import MarketState, SessionType

bars = load_bars('GBPUSD', 'M15')[:3000]
strat = <Strategy>(<Config>())
strat.reset()
signals = 0
for i in range(100, len(bars)):
    window = bars[max(0,i-100):i+1]
    state = MarketState(bars=window, current_session=SessionType.LONDON)
    sig = strat.evaluate(state)
    if sig: signals += 1
print(f'{signals} signals from {len(bars)-100} bars')
# Should be at least 5-10 from 2900 bars for a viable strategy
```

**4. Sweep and evaluate.**

---

## Sparse Strategies (Swing Layer)

Some strategies will produce <15 trades per 30-day window. This is fine for swing-layer candidates. Criteria for accepting sparse strategies:

| Metric | Threshold |
|--------|-----------|
| Win rate | ≥ 65% |
| Profit factor | ≥ 2.0 |
| Trades per window | ≥ 3 |
| Consistency across windows | Same direction wins in ≥ 4/5 windows |

If a strategy meets these, it's a valid swing-layer candidate even if it doesn't clear the standard 15-trade gate. Tag it explicitly as a swing-layer strategy in the notes.

---

## High-Frequency Strategies (M3/M5 Layer)

For sub-15-minute timeframes, expect:
- 15-50+ trades/window
- Lower WR (35-50% typical)
- Higher absolute trade count
- Tighter stops (5-20 pips)

**Specific to M3:** Not yet supported. Need to add M3 to `aggregate_ticks_to_bars.py` TIMEFRAMES dict and `tick_loader.py` TF_MINUTES. Card: `[DEBT] Add M3 timeframe support`.

---

## Best Day Rule (FTMO Funded Phase)

**Module:** `src/forex_bot/backtest/best_day_rule.py`
**Class:** `BestDayRuleTracker`
**Reference:** `docs/research/ftmo-risk-and-port-sizing-2026-07.md` §A.7

The FTMO Best Day Rule says: during the **funded phase only**, no single trading day may contribute more than 50% of cumulative profits. The rule prevents a single lucky spike from masking strategy inconsistency, and it is active on the path to scaling.

**Caveat:** The 50% threshold appears in multiple sources but is not clearly documented on FTMO's official objectives page. **Verify the exact threshold in the FTMO client area terms before relying on this rule for live trading.** The threshold is configurable via `BestDayRuleTracker(account_phase="funded", threshold=...)`.

### How it works

| Property | Value |
|---|---|
| Active phase | `funded` only (challenge phase → inactive, always allow) |
| Default threshold | 50% of cumulative P/L since funding start |
| Default reset timezone | CET (UTC+1); pass `reset_tz_offset_hours=2` for CEST summer |
| Loss handling | Losses never blocked; rule constrains profit concentration only |
| Cumulative ≤ 0 | Always allow (no profit concentration exists yet) |

### Basic usage

```python
from datetime import datetime, timezone
from backtest.best_day_rule import BestDayRuleTracker
from backtest.portfolio_blend import check_ftmo_best_day_rule

# 1. Construct at the moment of phase promotion (challenge → funded)
tracker = BestDayRuleTracker(account_phase="funded")

# 2. On every trade close, record P/L
tracker.record_trade_close(
    close_time=datetime.now(timezone.utc),
    pnl_dollars=400.0,
)

# 3. Before every entry decision, gate it
allowed, reason = check_ftmo_best_day_rule(
    tracker,
    planned_profit_dollars=200.0,
    now=datetime.now(timezone.utc),
)
if not allowed:
    logger.info("Entry blocked: %s", reason)
    return  # skip this entry

# 4. Inspect state for UI / logging
print(tracker.status(now=datetime.now(timezone.utc)))
# {
#   'is_active': True,
#   'today_pnl': 400.0,
#   'cumulative_pnl': 1000.0,
#   'today_share': 0.4,
#   'remaining_today_headroom_dollars': 100.0,
#   ...
# }
```

### Integration checklist for live trading

- [ ] Construct `BestDayRuleTracker` exactly once per funded account session, at the moment of phase promotion. Persist its state across restarts (`status()` returns the full state).
- [ ] After each closed trade, call `record_trade_close(close_time, pnl_dollars)`. The tracker's internal daily reset fires automatically at CET midnight.
- [ ] Before each entry, call `check_ftmo_best_day_rule(tracker, planned_profit_dollars, now=...)` and skip entries when `allowed` is `False`. Logging the reason is recommended.
- [ ] On phase demotion (back to challenge), replace the tracker with a new `BestDayRuleTracker(account_phase="challenge")` instance. Old P/L state should be archived.
- [ ] For DST correctness, re-construct the tracker each morning with the correct `reset_tz_offset_hours` (1 for CET winter, 2 for CEST summer). The tracker does not currently auto-detect DST — pass it explicitly.
- [ ] Verify the threshold against FTMO's client area terms before relying on it for production sizing decisions. Default 50% is a conservative interpretation per research §A.7.

### Difference from `risk/ftmo_guard.py`

`src/forex_bot/risk/ftmo_guard.py` also implements a Best Day check, but with a different formula: it uses the **sum of positive-day P/L** as the denominator, not cumulative P/L. The two implementations are complementary:

| Use case | Module |
|---|---|
| Live funded-phase entry gating with planned-profit projection | `backtest/best_day_rule.py` (this card) |
| Per-account daily P&L accounting alongside daily-loss and drawdown breakers | `risk/ftmo_guard.py` (existing) |

A future consolidation card may merge these into a single canonical tracker once the FTMO threshold is verified. For now, both are valid interpretations of the rule.

---

## News Blackout Filter (FTMO Compliance)

Per FTMO Standard account rules: **"no trading 2 min before/after high-impact news."**
Our blend plan extends this to a **5-minute** safety margin on each side.

### What it does

The :class:`~data.news_calendar.NewsCalendarFilter` class checks whether
the current time falls within a blackout window around high-impact economic
events (NFP, FOMC, ECB, BOJ, BOE rate decisions, CPI, PPI, GDP, etc.).

### Usage in trading loops

```python
from data.news_calendar import NewsCalendarFilter
from backtest.portfolio_blend import check_news_blackout

nf = NewsCalendarFilter()  # auto-fetches ForexFactory calendar

# Before each entry:
allowed, reason = check_news_blackout(nf, ["EURUSD", "USDJPY"])
if not allowed:
    logger.info("Skipping entry: %s", reason)
    continue
```

### Usage in backtests

For deterministic backtests, disable auto-fetch and inject a fixed calendar:

```python
nf = NewsCalendarFilter(
    cache_path="data/news_calendar_2024.json",
    auto_fetch=False,
)
```

### Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `blackout_minutes` | 5 | Minutes before AND after event |
| `cache_path` | `data/news_calendar_cache.json` | Local calendar cache |
| `cache_ttl_hours` | 24 | Max cache age before re-fetch |
| `auto_fetch` | `True` | Fetch from ForexFactory when stale |
| `permissive_on_failure` | `True` | Allow entries if no data (log warning) |

### High-impact event criteria

An event triggers a blackout if:
1. Its `impact` field is `"high"`, OR
2. Its title matches keywords: NFP, FOMC, ECB rate, BoE rate, BoJ rate, CPI, PPI, GDP, ISM PMI, unemployment rate
3. Central bank rate decision keywords per currency

### Files

| File | Purpose |
|------|---------|
| `src/forex_bot/data/news_calendar.py` | Filter implementation |
| `src/forex_bot/backtest/portfolio_blend.py` | `check_news_blackout()` entry gate |
| `tests/unit/data/test_news_calendar.py` | Unit tests |

---

## Common Pitfalls

**1. "All 5 windows pass" is misleading.** It means PF≥1.3 in all 5 windows, not that the strategy is great. A strategy with PF=1.31 in 5 windows is far weaker than PF=3.0 in 3 windows.

**2. CSV data lies.** No spread, no volume, no real session info. Always check: `python3 -c "from backtest.tick_loader import load_bars; bars = load_bars('GBPUSD','M15'); print(bars[0])"` — if `spread_pips == 0`, you're on CSV.

**3. Profit factor caps at 10.0.** If you see `PF=10.0` exactly, that's `total_loss==0` — all wins. Treat with skepticism (could be a 5-trade lucky streak or a bug). Inspect raw trades.

**4. "0 trades" usually means a config bug, not a bad strategy.** Common causes:
- `min_confidence` too high for the strategy's confidence formula
- Session filter excluding all hours (when current_session is None in test)
- ADX/RSI thresholds impossible to meet simultaneously
- Wrong band/boundary check (e.g., checking `kc_upper` when price is below `bb_upper` during squeeze)

**5. Confidence gate mismatch.** Walk-forward runner's `min_confidence` filter is at 0.30 by default. The strategy's own `config.min_confidence` is a second filter. Both must be low enough for signals to fire.

---

## Decision Log

| Date | Decision | Why |
|------|----------|-----|
| 2026-07-12 | Built tick aggregation layer | CSV had no spread/volume/session info |
| 2026-07-12 | Added M3 timeframe support | (pending) |
| 2026-07-12 | Relaxed `volatility_squeeze` params | Original ADX≥20 in squeeze regime is self-contradictory |
| 2026-07-12 | Built `donchian_atr_trend` and `london_breakout_retest` | Add trend-following + session-based strategies |
| 2026-07-12 | Switched to tick data only (no CSV for production backtests) | Craig directive, CSV is preliminary |

---

## Related Files

- `scripts/aggregate_ticks_to_bars.py` — tick → bars aggregation
- `scripts/run_srf_sweep.py` — single-symbol sweep driver
- `scripts/run_portfolio_blend.py` — multi-strategy blend (built, commit 942e4a4)
- `src/forex_bot/backtest/tick_loader.py` — bar loader (DuckDB + CSV fallback)
- `src/forex_bot/backtest/walk_forward_runner.py` — walk-forward engine
- `src/forex_bot/backtest/portfolio_blend.py` — blend framework
- `src/forex_bot/srf/gonogo.py` — go/no-go gate logic
- `src/forex_bot/srf/schema.py` — DuckDB schema for sweep data
- `data/ayumi_market.duckdb` — bars + ticks source
- `data/research/research.duckdb` — sweep results + portfolio metrics

---

*Update this file when procedures change. Future Ava should be able to run a full backtest cycle from this doc alone.*