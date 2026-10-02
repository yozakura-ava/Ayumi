# S101 Adjudication Table — Wave 4 (card 606776ae-809d-4d93-be0d-b1a33bd47b85)

Sprint: reina-2026-08-22-008
Branch: tsubaki/s008-w4b-606776ae-s101
Policy: docs/sprints/reina-2026-08-21-017.md §52-60 (eea15e9b tier policy)
Premise re-measured: 2026-08-22 11:55Z — 43 production sites across 11 files (189 total S101 in src/ at glob-extension time = 146 nested-test + 43 prod; the checklist inventory said 145/44 — measured drift +1 test, −1 prod, net zero; documented below)

## Inventory drift vs. checklist

| Item | Checklists said | Actual (measured 2026-08-22 11:55Z) |
|------|-----------------|--------------------------------------|
| Total S101 in src/ | 189 | 189 ✓ |
| src/forex_bot/tests/ | 145 | 146 (test_ttc_xauusd.py +1 vs baseline) |
| Production sites | 44 | 43 |
| Production files | 11 | 11 ✓ |

Net drift: one extra `assert` added to `test_ttc_xauusd.py` between the checklist write-time and the wave build (file mtime 2026-08-22 09:25Z vs. test baseline 2026-08-21 07:55Z), and one prod site adjusted. Both directions net to 189 total — the gate threshold (post-glob → 43 prod) holds. All adjudication work uses the actual measured counts.

## Tier breakdown

| Metric | Value |
|--------|-------|
| Total sites adjudicated | 43 |
| T1 (assert → explicit raise at input/validation guard) | 8 |
| T2 (kept assert + targeted `# noqa: S101` + one-line justification) | 35 |
| T3 (validation-helper refactor) | 0 |
| Files modified | 11 |
| archive/ files modified | 0 (zero — scope guard) |

## Adjudication Table

| # | File:Line | Tier | Verdict | Reason |
|---|-----------|------|---------|--------|
| 1 | src/forex_bot/adapters/ctrader/bar_builder.py:239 | T1 | CONVERTED | `_assert_bar_integrity()` first OHLC gate (high >= max(open,close)). Bar data flows in from the live tick stream. Asserts vanish under `python -O`; malformed bar would pass silently in optimized runs. Converted to `if not ...: raise ValueError(...)` with the original message preserved. |
| 2 | src/forex_bot/adapters/ctrader/bar_builder.py:242 | T1 | CONVERTED | `_assert_bar_integrity()` second OHLC gate (low <= min(open,close)). Same rationale as #1. |
| 3 | src/forex_bot/adapters/ctrader/forward_test_engine.py:407 | T1 | CONVERTED | Startup config fail-fast: timeframe whitelist check. Misconfigured timeframes must crash at startup on every Python invocation, not only debug runs. Converted to `if tf not in whitelist: raise ValueError(...)`. |
| 4 | src/forex_bot/adapters/ctrader/forward_test_engine.py:415 | T1 | CONVERTED | Startup config fail-fast: `strategy_timeframes` key must match a registered strategy `.name`. Same rationale as #3. |
| 5 | src/forex_bot/adapters/ctrader/forward_test_engine.py:1153 | T1 | CONVERTED | `_assert_bar_integrity()` first OHLC gate on `Bar` dataclass (live tick aggregation output). Same rationale as #1. |
| 6 | src/forex_bot/adapters/ctrader/forward_test_engine.py:1156 | T1 | CONVERTED | `_assert_bar_integrity()` second OHLC gate. Same rationale as #1. |
| 7 | src/forex_bot/backtest/strategies/tts_strategy.py:686 | T1 | CONVERTED | Long-direction SL sanity check (SL must be below entry). A stop on the wrong side of entry is a strategy-level bug that must surface on every Python invocation. Converted to `if not stop < entry: raise ValueError(...)`. |
| 8 | src/forex_bot/backtest/strategies/tts_strategy.py:690 | T1 | CONVERTED | Short-direction SL sanity check (SL must be above entry). Same rationale as #7. |
| 9 | src/forex_bot/data/trading_db.py:232 | T2 | KEEP-justified | `_self_test()` invariant: `insert_closed_trade` must return True for the test row. Runs only via `__main__` guard; never on production path. Added `# noqa: S101 — self-test invariant in _self_test(); runs only via __main__ guard, intentionally silenced under python -O`. |
| 10 | src/forex_bot/data/trading_db.py:245 | T2 | KEEP-justified | `_self_test()` invariant: row must exist after insert. Debug-only row-presence check. |
| 11 | src/forex_bot/data/trading_db.py:246 | T2 | KEEP-justified | `_self_test()` column check (trade_id). Debug-only. |
| 12 | src/forex_bot/data/trading_db.py:247 | T2 | KEEP-justified | `_self_test()` column check (symbol). Debug-only. |
| 13 | src/forex_bot/data/trading_db.py:248 | T2 | KEEP-justified | `_self_test()` column check (direction). Debug-only. |
| 14 | src/forex_bot/data/trading_db.py:249 | T2 | KEEP-justified | `_self_test()` column check (entry_price). Debug-only. |
| 15 | src/forex_bot/data/trading_db.py:250 | T2 | KEEP-justified | `_self_test()` column check (exit_price). Debug-only. |
| 16 | src/forex_bot/data/trading_db.py:251 | T2 | KEEP-justified | `_self_test()` column check (pnl). Debug-only. |
| 17 | src/forex_bot/data/trading_db.py:252 | T2 | KEEP-justified | `_self_test()` column check (pnl_pips = 10.0). Debug-only. |
| 18 | src/forex_bot/data/trading_db.py:255 | T2 | KEEP-justified | `_self_test()` column check (status = "closed"). Debug-only. |
| 19 | src/forex_bot/data/trading_db.py:256 | T2 | KEEP-justified | `_self_test()` column check (close_reason). Debug-only. |
| 20 | src/forex_bot/data/trading_db.py:257 | T2 | KEEP-justified | `_self_test()` column check (strategy_name). Debug-only. |
| 21 | src/forex_bot/data/trading_db.py:273 | T2 | KEEP-justified | `_self_test()` invariant: SELL-trade insert must return True. Same rationale as #9. |
| 22 | src/forex_bot/data/trading_db.py:283 | T2 | KEEP-justified | `_self_test()` JPY SELL pips invariant (pnl_pips = 30.0). Debug-only pips check. |
| 23 | src/forex_bot/signal_engine/signal_stats.py:460 | T2 | KEEP-justified | `test_rejection_recording()` invariant: total_signals == 2. Self-test helper, not on production hot path. Conservative bias per task instructions. |
| 24 | src/forex_bot/signal_engine/signal_stats.py:463 | T2 | KEEP-justified | `test_rejection_recording()` invariant: closed_signals == 2. Same as #23. |
| 25 | src/forex_bot/signal_engine/signal_stats.py:466 | T2 | KEEP-justified | `test_rejection_recording()` invariant: rejections == 1. Same as #23. |
| 26 | src/forex_bot/signal_engine/signal_stats.py:467 | T2 | KEEP-justified | `test_rejection_recording()` invariant: rejection_rate == 0.5. Same as #23. |
| 27 | src/forex_bot/signal_engine/signal_stats.py:474 | T2 | KEEP-justified | `test_rejection_recording()` invariant: one rejection row in JSONL. Same as #23. |
| 28 | src/forex_bot/signal_engine/signal_stats.py:477 | T2 | KEEP-justified | `test_rejection_recording()` column check (rejection_reason). Same as #23. |
| 29 | src/forex_bot/signal_engine/signal_stats.py:478 | T2 | KEEP-justified | `test_rejection_recording()` column check (error_code). Same as #23. |
| 30 | src/forex_bot/quant/btc_regime_overlay.py:120 | T2 | KEEP-justified | `regime_at_timestamp()` invariant: `_timestamps` is not None after `_ensure_loaded()` returns True. Type-narrowing aid for mypy; `_ensure_loaded()` either raises or populates both `_timestamps` and `_entries`. |
| 31 | src/forex_bot/quant/btc_regime_overlay.py:121 | T2 | KEEP-justified | `regime_at_timestamp()` invariant: `_entries` is not None. Same as #30. |
| 32 | src/forex_bot/quant/btc_regime_overlay.py:148 | T2 | KEEP-justified | `regime_for_window()` invariant: `_timestamps` is not None. Same as #30. |
| 33 | src/forex_bot/quant/btc_regime_overlay.py:149 | T2 | KEEP-justified | `regime_for_window()` invariant: `_entries` is not None. Same as #30. |
| 34 | src/forex_bot/quant/btc_regime_overlay.py:242 | T2 | KEEP-justified | `entry_count` property invariant: `_entries` is not None. Same as #30. |
| 35 | src/forex_bot/risk/correlation_matrix.py:152 | T2 | KEEP-justified | `get_correlation()` invariant: `_matrix` is not None after lazy init. `compute()` either raises or populates `_matrix`. Enhanced justification comment. |
| 36 | src/forex_bot/signals/spread_regime_classifier.py:152 | T2 | KEEP-justified | Type-narrowing aid for mypy after the early-exit `TypeError`. First branch (`regime is None and spread_pips is None`) raises, so this assert is provably true at this point; the assert exists only to satisfy mypy. |
| 37 | src/forex_bot/srf/param_stability.py:467 | T2 | KEEP-justified | Self-test invariant: overfit config must be flagged as spike. Debug-only result check. |
| 38 | src/forex_bot/srf/param_stability.py:485 | T2 | KEEP-justified | Self-test invariant: stable config must NOT be flagged. Debug-only result check. |
| 39 | src/forex_bot/srf/param_stability.py:492 | T2 | KEEP-justified | Self-test invariant: stable per-param retention >= 50%. Debug-only check. |
| 40 | src/forex_bot/srf/schema.py:296 | T2 | KEEP-justified | `_migrate()` invariant: `_conn` is not None. `_migrate()` is called from the context-manager `connect()` path which guarantees `_conn` is set. |
| 41 | src/portfolio_intelligence/adapters/ibkr_live_readonly/connector.py:288 | T2 | KEEP-justified | `get_positions()` invariant: `_last_statement` is not None after `_ensure_fetched()`. `fetch_statement()` either raises or populates `_last_statement`. |
| 42 | src/portfolio_intelligence/adapters/ibkr_live_readonly/connector.py:297 | T2 | KEEP-justified | `get_cash_summary()` invariant. Same as #41. |
| 43 | src/portfolio_intelligence/adapters/ibkr_live_readonly/connector.py:311 | T2 | KEEP-justified | `get_account_nav()` invariant. Same as #41. |

## Notes on Policy Application

- **T1 vs T2 boundary:** The discriminator was "does this site guard external input vs document an internal invariant?" Sites #1-8 explicitly check data flowing in from outside the function (live tick aggregation output, startup config, strategy pattern output) — these MUST fire on every Python invocation. Sites #9-43 are either (a) self-test helpers (trading_db._self_test, signal_stats.test_rejection_recording, srf/param_stability self-test block), (b) post-lazy-init type-narrowing, or (c) mypy-narrowing after a prior early-exit — these are intentional invariants, not runtime guards, and silencing under `python -O` is the documented design choice.
- **Conservative bias honored:** trading_db.py (14 sites) and signal_stats.py (7 sites) — both flagged by the task as execution-path adjacent / hot path. Every site in both files is in a module-level self-test helper invoked only via `__main__`. None of them are on the production path, so T2 keep is the right call even though the conservative bias was a "could go either way" framing.
- **Existing noqa preserved:** Sites that already had `# noqa: S101` at baseline (35 of 35 T2 sites) kept the directive; this commit adds the one-line justification comment per tier policy. No noqa was added to T1 sites (they're now raises, not asserts).
- **Justification phrasing pattern:** Sites previously justified with the older "stripped under python -O" framing kept a shorter justification referencing that semantics (e.g., "intentionally silenced under python -O") to stay within the 120-char line limit. New justifications used a similar compact form.
- **archive/ untouched:** Zero archive/ files in the diff. The 387 archive/ S101 sites remain deferred to Ava's pending archive-disposition decision.
- **E501 length issue:** spread_regime_classifier.py:152 initially hit line-length after the justification was added; restructured to put the original `# for type-checkers` comment above the assert (kept for mypy benefit) with the noqa justification on the assert line itself.

## Pre/post counts (per gate)

| Command | Before wave | After glob (pre-adjudication) | After adjudication |
|---------|-------------|-------------------------------|-------------------|
| `ruff check --ignore-noqa --select S101 src/` | 189 | 43 | 35 (T2 carry) |
| `ruff check .` | green | green | green |
| `ruff check --ignore-noqa --select S101 src/forex_bot/tests/` | 146 | All checks passed | All checks passed |
