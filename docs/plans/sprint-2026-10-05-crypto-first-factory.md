# Sprint C — Crypto-First Factory Wave 0+1

**Date:** 2026-10-05 · **Approved by:** Craig (grilling, live, all 3 recs accepted — `~/.openclaw/workspace/data/sprint-plans/ayumi-2026-10-05-grilling.json`)
**Parent audit:** `docs/audits/ayumi-audit-2026-10-05.md` (YELLOW)
**Research base:** `~/.openclaw/workspace/docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md` + 4 lanes (ML, hardening, quant-method, UI/headless-QC) in session 21636b66

## Status: IN FLIGHT — Wave 0 complete, 1a.1 + 1b.1 + 1b.2 merged (2026-10-05). Hard gate active.

## Goal

Make the strategy factory produce **trustworthy crypto-first verdicts**: a crypto backtest core that models the things that actually kill crypto P&L (funding, mark-price liquidation, real fees, survivorship), and statistical gates that stop the factory from promoting noise.

**Ground rules carry over unchanged** (OOS isolation, frozen regime labels, frozen ML learner, CPU caps, kill criteria — `docs/roadmaps/strategy-factory-pipeline.md`).

## Wave 0 — Unblock the quality gate (small, first)

| # | Card | SP | Lane |
|---|---|---|---|
| 0.1 ✅ merged 6499e0f2 | Tournament root fix: pytz dep + strategy-init compat (card `c38d59da` unblock; 17/17 → strategies score) | 1 | builder (Reina) |
| 0.2 | Hygiene batch: close 3 orphaned duplicate crypto cards, push 33 commits, prune 31 stale worktrees, commit rd-hopper design doc + tick_stall_state refresh | 1 | Ava + Tomoe |

## Wave 1a — Crypto backtest core (blocks all crypto candidates)

| # | Card | SP | Source |
|---|---|---|---|
| 1a.1 ✅ merged eadbb741 | Funding model: adaptive intervals (8h/4h/1h), realized historical funding charged per interval | 2 | crypto R1 |
| 1a.2 | Mark-price liquidation mechanics in engine | 2 | crypto R1 |
| 1a.3 | Venue/tier-aware cost model, maker/taker branching; pin fees from live account | 1.5 | crypto R2 |
| 1a.4 | Fail-loud data-integrity gate (gaps, delistings) — run fails, never silently repairs | 1 | crypto R5 |
| 1a.5 | Point-in-time crypto universe with delisting records (BTC/ETH/SOL + breadth later) | 1.5 | crypto R4 |

**Hard gate: no Tier A/B crypto verdict before 1a.1–1a.4 land.**

## Wave 1b — Statistical repairs (parallel lane)

| # | Card | SP | Source |
|---|---|---|---|
| 1b.1 ✅ merged cf8a7a64 | Real-trial PBO: persist per-trial return series, CSCV matrix [T, N_trials]; rename old metric cost_sensitivity | 1.5 | quant R1 |
| 1b.2 ✅ merged 206da069 | CPCV runner (purge + existing embargo; N=6–8 start; cpu_guard) | 2 | quant R2 |
| 1b.3 | Risk-adjusted tournament ranking (White RC or Benjamini-Hochberg; kill raw-return sort) | 1.5 | quant R3 |
| 1b.4 | Provenance columns (git_commit, data_hash) in factory_verdicts; reuse srf compute_data_hash | 0.5 | UI lane #1 |

## Deferred by design (Sprint D candidates, not dropped)

ML meta-labeling + calibration (biggest ROI but needs 1b harness first) · vol-target overlay + DD ladder · CUSUM decay monitor · UI dashboard (leaderboard, WF matrix, heatmaps) · LightGBM challenger · Sprint B hopper build-out (Satsuki, design now tracked in git via 0.2). RL declined this cycle; LLM = productivity-only + leakage lint.

## Verification

Every card: fresh worktree, targeted tests via run_test_scope.sh, cpu_guard, builder_quality_gate, Rin review, prove-and-release (builder never completes own card). Orchestrator independent verification before completion (HR8).
