# Ayumi Audit — 2026-10-05

**Requested by:** Craig ("audit the Ayumi project") · **Auditor:** Ava (orchestrated 10-lane evidence swarm, EBIPP §2/§3/§14)

## Verdict: 🟡 YELLOW

The Strategy Factory build is real, current, and independently verified — Sprint A (SFA-1/2/3) merged 2026-10-04/05 with Rin APPROVE and green scoped tests, and the crypto lane (A1–A3) is complete behind SymbolTypeGate. But the factory is now **fed by weak statistics and starving operationally**: the tournament lane is dead at the root (17/17 strategies failing since 09-21), Sprint B (R&D hopper) triggered 2026-10-05 00:47 and nobody picked it up, the forward test has produced **0 trades (9 signals, all regime-gated out)**, and 3 statistical defects mean current verdicts overstate quality (see R-lane findings).

## True status vs documented status

| Area | Documented | Actual (evidence, this audit) |
|---|---|---|
| Sprint A | "APPROVED AND IN FLIGHT" (pipeline.md status block) | **Done + merged**: SFA-1 `2279b5e0` (108/108+gate 48/48), SFA-2 `222b8809` (140/140+33/33), SFA-3 `5e11645e` (151/151+25/25); parent `bc8439dd` done |
| Sprint B (R&D hopper, `ea42e6be`) | "DESIGNED-GATED, triggers when Sprint A exits" | **Trigger met 2026-10-05 00:47; card stranded in review, 0 comments**; design doc `rd-hopper-engine-design.md` exists but is **untracked in git** |
| Factory code | Phase 1 wiring | `src/forex_bot/factory/` = 2,269 LOC + `tournament/front_door.py` 603 LOC, verdicts persisted to research.duckdb. **No per-archetype templates yet** (registry pass-through only); `scripts/factory/regime_validation.py` (~1,330 LOC) not wired to anything |
| Crypto lane | A1–A3 merged | Verified: `symbol_type_gating.py` (202 LOC), `crypto_extensions.py` (824 LOC: OI/funding/BTC-dom, settlement veto), `crypto_adapter.py` (902 LOC: BTC/ETH/SOL, 1h enforced, EI egress) |
| Forward test | "verified live 2026-10-04" | Service healthy 21h clean, balance $10,180.48 (+1.80%), **but 9 signals → 0 trades** (all rejected by REGIME-GATE `choppy_not_in_allowed`). Mechanically healthy, strategically silent |
| Tournament lane | Harness scaffolded | **Blocked at root since 09-21**: card `c38d59da` pytz missing / strategy-init incompatible, 17/17 strategies failed, empty scorecards; `f24f6d78` GPU runner blocked ×2 failures; `faf8705b` host-side M3/M5/M30 allowlist still ready (node side done) |

## Key findings (EBIPP §3 ledger highlights)

1. **F-high · Statistical defects in the new factory (verified in code):**
   - PBO computed on a synthetic 2-column matrix (gross vs net) — a cost-sensitivity test, not PBO (`validation_runner.py:398-424`)
   - No CPCV/purge anywhere (grep zero hits); embargo exists, purge does not
   - Tournament ranks by raw `return_pct` (`scorecard.py:277-299`) — no risk adjustment, no multiple-testing correction
2. **F-high · Tournament root blocker** `c38d59da` (17/17 fail) — the North-Star pivot's quality gate is inoperative.
3. **F-high · Sprint B stranded** — trigger met, card untouched, design doc untracked.
4. **F-medium · Workboard hygiene:** 3 orphaned duplicate CRYPTO cards (blocked originals `3c316b12`/`73efa3b9`/`8c03d3bd` vs done twins); `updated_at` is not a staleness signal (archive sweeps bump it); 9 stale open cards (13–26d, all tsubaki); 6 unassigned cards.
5. **F-medium · Repo hygiene:** 33 commits unpushed to origin/main; 34 worktrees (31 stale); 5 untracked paths incl. the Sprint B design doc and `scripts/crypto_paper_trading_7day.py`.
6. **F-medium · Ops:** `data/tick_stall_state.json` stale 18 days (flagged Oct 1, backfill work done, file never refreshed); CI lint/bandit/gitleaks all `continue-on-error` and test job only runs `tests/unit` with a 10-entry ignore list.
7. **F-info · Grilling:** No live project-level grilling exists; the 09-11 one was backfilled (`backfilled: true`). Direction is consistent (tournament = quality gate, live = proving ground) but never affirmed in a live sitting.

## Confidence band (EBIPP §14)

Evidence ledger confidence: **HIGH** for code/cards/git claims (all verified live this audit); **MEDIUM** for process drift claims (single-lane sweeps, spot-verified by orchestrator).

## Remediation

Findings route to the sprint proposed 2026-10-05 (crypto-first factory Sprint C): statistical-gate repairs + crypto cost/liquidation core + tournament unblock + hygiene batch. Grilling gate run in-session with Craig 2026-10-05 (artifact: `data/sprint-plans/ayumi-2026-10-05-grilling.json`).

## Research artifacts

- `docs/research/2026-10-05-crypto-first-strategy-testing-recommendations.md` (crypto lane, 354 lines)
- ML-mechanics, hardening/execution, quant-methodology, UI/headless-QC lane reports held in session `agent:main:dashboard:21636b66`; folded into sprint plan.
