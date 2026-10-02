# CONTEXT.md — Ayumi Project Glossary

*Project-specific terms for the Ayumi trading bot.*
*Master glossary: `~/.openclaw/workspace/CONTEXT.md`*
*Maintenance: Propose additions via `data/glossary-staging.jsonl`*

---

## Architecture Quick Reference

- **Language:** Python 3.12
- **Source:** `src/forex_bot/` (318 files, ~96k LOC)
- **Tests:** `tests/` (339 files, ~104k LOC)
- **Package quirk:** `forex_bot` has a hyphen — not a valid Python package name. All imports use bare module names via `sys.path` (e.g. `from confidence.engine import …`), NOT `from forex_bot.confidence…`.
- **Target:** FTMO 1-Step Standard ($10K, 3% daily DD, 10% total DD)

## Key Directories

| Path | Purpose |
|------|---------|
| `adapters/ctrader/` | Broker integration (40 modules: connection, auth, orders, risk, forward-test engine) |
| `backtest/` | Backtesting: engines, strategies, ICT/SMC primitives, parameter sweeps, walk-forward, FTMO sim |
| `confidence/` | Multi-layer confidence engine: gates, providers, confluence, tuning |
| `core/` | Canonical types: Bar, BarPeriod, StrategySignal, IStrategy Protocol, pip math |
| `engine/` | New canonical engine split: base, mixins, signal_router, strategy_executor, health_monitor |
| `ml/` | ML pipeline: train_model, blend_optimizer, Optuna, signal_provider, confidence_learner |
| `orchestrator/` | Signal orchestrator + strategy adapter: confidence → routing → sizing → execution |
| `policy/` | Behavioral (streak/drawdown dampener), kill_criteria (per-strategy + global thresholds) |
| `quant/` | Walk-forward, OOS gate (Deflated Sharpe), Go/No-Go, Markov regime, portfolio |
| `risk/` | FTMOGuard, SLPositionSizer, profile_router (Sniper/Swarm) |
| `signal_engine/` | TTC signal engine: patterns, levels, gates, confluence, sessions, stops, output |
| `srf/` | Strategy Research Framework: DuckDB-backed walk-forward research pipeline |

## Critical Constants

| Constant | Value | Location |
|----------|-------|----------|
| `FTMO_DAILY_DD_LIMIT_PCT` | 0.03 (3%) | `risk/ftmo_params.py` |
| `FTMO_TOTAL_DD_LIMIT_PCT` | 0.10 (10%) | `risk/ftmo_params.py` |
| `FTMO_RISK_PER_TRADE_PCT` | 0.005 (0.5%) | `risk/ftmo_params.py` |
| `FTMO_MAX_CONCURRENT_POSITIONS` | 3 | `risk/ftmo_params.py` |
| `FTMO_MAX_TRADES_PER_DAY` | 10 | `risk/ftmo_params.py` |
| `FTMO_MIN_RISK_REWARD` | 1.5 | `risk/ftmo_params.py` |
| Trading day reset | 00:00 America/Toronto | `risk/ftmo_guard.py` |
| `MW_SYMMETRY_MAX` | 0.015 (1.5%) | `signal_engine/thresholds.py` |
| `MM_CANDLE_BODY_RATIO` | 0.70 (70%) | `signal_engine/thresholds.py` |
| `LEVEL_COMPLETION_RATIO` | 0.90 (90%) | `signal_engine/thresholds.py` |
| `FLAT_EMA_SEPARATION` | 0.003 (0.3%) | `signal_engine/thresholds.py` |

## Common Pitfalls

- **XAUUSD pip size is 0.1** — price-based heuristics mis-classified Gold as JPY pairs before `utils/pip_value.py` existed.
- **`forex_bot` is not `forex_bot`** — the hyphen makes it invalid as a Python package name. All imports are bare-module via `sys.path`.
- **ForwardTestEngine is the ONLY canonical engine** — deprecated engines moved to `_deprecated/` on 2026-07-08.
- **TTC vs TTS** — TTC = Turn-The-Candle (signal engine). TTS = Turn-The-Strategy (adapter). The acronym drifted in the codebase.
