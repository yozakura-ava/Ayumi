# Tournament & Strategy Testing Runbook

> **Last updated:** 2026-09-21
> **Owner:** Ava (orchestration) + Tsubaki (harness builds)
> **Scope:** Tournament harness operation, strategy registration/testing lifecycle, and the operational lessons landed 2026-09-21.
> **Companion skills:** `ayumi-tournament-run` (workspace skill — canonical run procedure, read it first), `pre-build-checklist` for harness changes.

---

## Quick Reference

| Action | Command (on node `ava-worker-local`, repo `/opt/ayumi-tournament/ayumi`) |
|--------|--------|
| Smoke test | `.venv/bin/python scripts/run_tournament.py --smoke --verbose` (pass marker `TOURNAMENT_OK`; USDJPY fail-loud guard is a known-unfixed conflict — see Findings) |
| Check bar coverage | DuckDB query: `select symbol, timeframe, count(*), min(timestamp_utc), max(timestamp_utc) from bars group by 1,2` |
| Full run (detached) | `setsid nohup .venv/bin/python scripts/run_tournament.py --symbol EURUSD --timeframe H1 --window 2020-01-15:2026-07-01 --output data/tournament/scorecard_<SYM>_H1_<tag>.json > data/tournament/run_<tag>.log 2>&1 < /dev/null &` |
| Verify completion | Scorecard JSON exists **and parses** — never judge by exit code or log tail (stdout is buffered) |
| Check a live run | `ps aux \| grep run_tournament` + `tail` the progress lines (`progress strategy=... bars_processed=...`) |

**All runs execute on the node (`host=node`, `node=ava-worker-local`), never the gateway host.**

## Strategy Lifecycle

1. **Write** the strategy under `src/forex-bot/strategies/` — constructor may take `config=` or not; `initialize()` is optional.
2. **Register** in `STRATEGY_CLASS_MAP` (`src/tournament/harness.py`). The adapter (`_build_strategy_instance`) introspects constructors via `inspect.signature` and guards `initialize`/`shutdown` with `hasattr` — so interface mismatches can no longer silently skip strategies (see Findings #1).
3. **Smoke** on a short EURUSD window; then full-window tournament run. Zero signals on a *full* window = harness/window bug, not "no edge" — investigate before believing rankings.
4. **Sweep** (planned, card `48243dbc`): parameter grids ranked by robustness (median/worst-quartile return, % FTMO-passing cells) with sweep/holdout window split — never crown a single default-config winner.
5. **Promote** only strategies that survive cost-realistic evaluation (FTMO costs are a precondition: spread + 2× slippage + commission, position sized off SL distance).

## Findings (2026-09-21 — all verified)

1. **Silent strategy skips (fixed, `9e9aaf30`).** 12 of 17 registered strategies were silently skipped with interface errors (`TypeError: config kwarg`, `AttributeError: initialize`). Fix: adapter-layer signature introspection. Lesson: **every registry entry must complete a signal pass at registration time**, not be discovered skipped three weeks later.
2. **O(N²) harness hot path (fixed, `b1bb93e8`).** The bottleneck was *harness-side*: `MarketState(bars=list(bars_window))` copied the full bar history every bar (~312M element-copies on 25k bars). Fix is one line (no copy); strategies never mutate `state.bars` (grep-verified, Rin independently confirmed). 17-strategy full window: 4–10h → **~40 min**. An incremental indicator-cache module exists (`src/tournament/indicator_cache.py`) but is deliberately **unwired** — it failed the equivalence gate (strategies use subtly different RSI variants; caching would silently drift results). Do not wire it without a per-strategy equivalence proof.
3. **Result-equivalence gate (now standard).** Any performance refactor must prove SHA-256 signal fingerprints match bar-by-bar on a fixed window before/after. Applied and enforced on both fixes above.
4. **GPU is not a tournament accelerator (card `751e8f3f`, memo `docs/infra/gpu-trial-2026-09-21.md`).** The workload is tiny per-bar Python ops; kernel-launch + transfer overhead dominate (~110× slower RSI walk on GPU). GPU venv `/opt/ayumi-tournament/venv-gpu` (cupy-cuda13x 14.2.0) is retained for bootstrap CIs ≥10⁵ resamples and bulk elementwise ≥10⁷ — i.e., the parameter sweep, not the tournament loop.
5. **Duplicate-run clobber hazard.** Two processes racing the same `--output` silently clobber. Always `ps`-check before relaunching; kill the old PID first.
6. **USDJPY H1 data is smoke-sized** (~120 bars) — never use for real runs. The node smoke also trips the fail-loud `TournamentNoSignals` guard on USDJPY; known and unfixed — don't chase it.
7. **Costs are a precondition.** Cost-free scorecards are comparison columns only; no strategy counts as "surviving" without FTMO-realistic costs.

## Current baseline (for comparisons)

- **EURUSD H1 2020-01-15→2026-07-01, pre-adapter-fix (5 strategies, 2026-09-16/21):** dual_tf_squeeze_pro +7.9% (15.6% DD, fails), rsi_threshold +4.5% (4.9% DD, 0 breaches — only FTMO-shaped result), bb_rsi_reversion +1.8%, donchian_atr_trend_v2 −13.5%, srmr_plus −23.9%. GBPUSD (Sep 16): both original strategies negative.
- **Full-roster scorecard:** `data/tournament/scorecard_EURUSD_H1_full_all17.json` (post-precompute relaunch; verify on node).

## Ops notes

- Node repo tracks `origin/main` (yozakura-ava fork) — pull before any run after a merge lands.
- Push from the gateway checkout uses the `id_ed25519_github` deploy key (`GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_github -o IdentitiesOnly=yes"`); default `github.com` key lacks rights.
- Tournament scorecards live in `data/tournament/` on the node; logs are write-buffered — trust `ps` + scorecard files, never log tails.
