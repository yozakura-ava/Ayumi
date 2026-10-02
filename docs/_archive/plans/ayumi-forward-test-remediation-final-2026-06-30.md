# Ayumi Forward Test Remediation — Final Synthesized Plan

**Date:** 2026-06-30  
**Synthesized by:** Ava (after Council review: Ren, Kaito, Rei, Liora)  
**Supersedes:** `ayumi-forward-test-remediation-2026-06-30.md` (planner original)  
**Pipeline stage:** Ready for Craig approval → Builder  

---

## Council Synthesis — What Changed

The planner's original plan had 6 tasks / 13 SP. Council review uncovered 4 critical issues that expand and restructure the work:

| Finding | Source | Impact on Plan |
|---|---|---|
| USDJPY fix must be at the source, not at `amend_sl_tp` boundary | Ren, Rei | Task B split: spike first, then fix at correct layer |
| Risk sizer is scalar-based with no position identity — double-cancel and overshoot are structural | Ren, Liora | Task C expanded: refactor to identity-keyed API |
| `SLPositionSizer` has no thread lock — 4 threads mutate it concurrently | Kaito | Task C must add `threading.RLock` |
| `amend_sl_tp` 1s stagger is broken — `time.sleep` after `return True` is unreachable | Kaito | New task: fix amend cooldown + observability |

**Ava's assessment:** All 4 findings are correct and must be addressed. The identity-keyed sizer refactor (Ren's proposal) is the highest-leverage change — it eliminates double-cancel, overshoot, and stranded budget as entire classes of bugs.

---

## Phased Task Breakdown — 7 Phases / 15 SP

### Phase 1: Stop the Bleeding (2 SP)

**Scope:** Two narrow, independent fixes that prevent immediate damage.

**1A — Disable canary at launcher level (1 SP)**
- **Root cause:** `scripts/launch_blend_forward_test.py:~702` constructs `TestCanaryStrategy(tp_sl_pct=0.005)`, overriding the class default of `0.0`. The `enabled` property checks `tp_sl_pct > 0` — so the disable flag in the class was bypassed by the explicit constructor argument.
- **Fix:** Set `tp_sl_pct=0.0` at the launcher call site (or remove canary from the strategy list entirely).
- **Hardening:** Gate behind `AYUMI_ENABLE_CANARY=1` env var. Add a WARNING log when canary is enabled.
- **Files:** `scripts/launch_blend_forward_test.py`, `src/forex_bot/strategies/test_canary.py`
- **AC:**
  - [ ] `grep -n "TestCanaryStrategy" scripts/launch_blend_forward_test.py` shows `tp_sl_pct=0.0` or env-gated
  - [ ] 30-min dry run: `Test Canary: evals=N no_signal=N` (all evals produce no signal)
  - [ ] No `canary_<N>` rationale in order logs

**1B — Fix positionId type error in late-fill callback (1 SP)**
- **Root cause:** `_register_late_fill_callbacks()` in `forward_test_engine.py:1385-1425` falls back to string clientOrderId when no integer `positionId` is available, passing it to `amend_sl_tp()` which expects an int.
- **Fix:** Validate `ctrader_position_id` is a non-zero `int` before calling `amend_sl_tp()`. If missing, log warning and skip amend.
- **Files:** `src/forex_bot/adapters/ctrader/forward_test_engine.py`
- **AC:**
  - [ ] No `Late SL/TP amend error ... 'str' object cannot be interpreted as an integer` in logs
  - [ ] Warning logged when positionId is missing: `Late fill for order %s but no cTrader positionId — SL/TP skipped`

**Dependencies:** None  
**Parallel with:** Phases 2, 3

---

### Phase 2: Amend Pipeline Fixes (2 SP)

**Scope:** Fix the broken amend cooldown and make amend results observable.

**2A — Fix unreachable `time.sleep` in amend_sl_tp (1 SP)**
- **Root cause:** `open_api_spot_feed.py:~208` — the `time.sleep(1.0)` throttle is after `return True` inside the `with self._amend_lock:` block, so it never executes. The 1s stagger claimed by commit `10fc316` does not actually work.
- **Fix:** Move the sleep before the return, or replace with a proper rate-limiter / semaphore pattern.
- **Files:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`
- **AC:**
  - [ ] Unit test: 5 rapid `amend_sl_tp()` calls are spaced ≥1s apart
  - [ ] No more concurrent amend floods under burst fills

**2B — Make amend_sl_tp return actual broker response (1 SP)**
- **Root cause:** `amend_sl_tp()` always returns `True` (fire-and-forget from commit `78dfd7a`), even when the broker rejects with `TRADING_BAD_STOPS`. The engine logs the error but the caller thinks it succeeded.
- **Fix:** Return `False` when broker rejects. Log the rejection with positionId, SL, TP, and errorCode. Handle partial amend success (one of SL/TP applied, other rejected).
- **Files:** `src/forex_bot/adapters/ctrader/open_api_spot_feed.py`, `src/forex_bot/adapters/ctrader/forward_test_engine.py`
- **AC:**
  - [ ] `amend_sl_tp()` returns `False` on broker rejection
  - [ ] Log includes positionId, errorCode, and description
  - [ ] Partial amend (SL applied, TP rejected) is detected and logged

**Dependencies:** None  
**Parallel with:** Phases 1, 3

---

### Phase 3: USDJPY Price Scaling Spike (1 SP)

**Scope:** Pure investigation — trace where the 100× inflation enters the pipeline.

**Investigation targets:**
1. `open_api_spot_feed.py:fetch_trendbars()` (~line 821) — verify the `10 ** digits` divisor is applied to OHLC deltas consistently for USDJPY
2. `forward_test_engine.py` bar construction — how `MarketState.bars` is populated from trendbars and tick events
3. `session_breakout.py:_pip_size()` (line 37) — confirm it's only used for pip-distance math, not price scaling
4. `session_range_mean_reversion.py:_pip_value_for_price()` — same audit
5. Spot check: compare a raw `ProtoOASpotEvent` tick price for USDJPY with the corresponding `MarketState.bars[-1].close`

**Deliverable:** 1-page findings doc at `docs/audits/usdjpy-scaling-source-2026-06-30.md` identifying:
- Exact file:line where the 100× enters
- Why EURUSD/GBPUSD are unaffected
- Recommended fix layer (with rationale)

**AC:**
- [ ] Findings doc written with concrete file:line evidence
- [ ] Root cause confirmed (not just "somewhere in bar construction")

**Dependencies:** None  
**Parallel with:** Phases 1, 2

---

### Phase 4: USDJPY Price Scaling Fix (2 SP)

**Scope:** Fix the 100× inflation at the source identified in Phase 3.

**Implementation:**
- Fix at the architectural layer identified by the spike (likely bar preloading or MarketState construction)
- Do NOT patch only at `amend_sl_tp` boundary — strategy sizing also uses these prices
- Add symbol-aware normalization that handles any JPY pair automatically
- If the fix is in bar construction, verify spot ticks and historical bars use the same scale

**Files (likely — confirmed by Phase 3):**
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (fetch_trendbars, tick decode)
- Possibly `src/forex_bot/adapters/ctrader/forward_test_engine.py` (MarketState assembly)

**AC:**
- [ ] Unit test: USDJPY signal price `16232.45` normalizes to `162.3245` at broker boundary
- [ ] Unit test: EURUSD `1.138445` passes through unchanged
- [ ] Unit test: GBPUSD `1.322935` passes through unchanged
- [ ] Dry-run forward test: no `TRADING_BAD_STOPS` for USDJPY; SL/TP prices in logs are ~162.x
- [ ] Regression assertion: USDJPY SL/TP values are within `[150, 200]` before sending

**Dependencies:** Phase 3 (must know the source before fixing)

---

### Phase 5: Risk Sizer Refactor — Identity-Keyed API + Thread Safety (3 SP)

**Scope:** This is the highest-leverage change in the plan. It eliminates double-cancel, overshoot, and stranded budget as structural impossibilities.

**What's wrong (council synthesis):**
- `SLPositionSizer` uses scalar accounting: `register_open_position(risk_amount)` adds to `_open_risk`, `cancel_position(risk_amount)` subtracts. No position identity → no way to detect double-cancel, missing-cancel, or amount mismatch.
- `_open_risk` is mutated from 4 threads (tick, callback, reconcile, daily reset) with no lock.
- `max(0.0, _open_risk)` silently clamps negative values, destroying evidence of bugs.
- `cancel_risk()` in `blend_runner.py:133` passes `starting_balance * 0.01` (hardcoded $100) instead of actual reserved risk.

**Implementation:**

1. **Identity-keyed API (Ren's proposal):**
   ```python
   def register(self, signal_id: str, risk_amount: float) -> None
   def cancel(self, signal_id: str) -> None  # idempotent — no-op if already cancelled
   def close(self, signal_id: str, pnl: float) -> None  # record win/loss, release risk
   ```
   - Track per-position state: `{signal_id: {risk_amount, status: "open"|"cancelled"|"closed", pnl}}`
   - `cancel()` is idempotent — calling it twice for the same signal_id is a no-op (not an error)
   - `close()` requires the position to be in "open" status — raises if already cancelled/closed

2. **Thread safety (Kaito's finding):**
   - Add `threading.RLock` to `SLPositionSizer`
   - Wrap every mutation: `register`, `cancel`, `close`, `reset_daily`, `update_balance`
   - `reset_daily()` warns if `_open_risk > 0` and refuses to reset (or logs critical)

3. **Remove silent clamp (Liora's finding):**
   - Remove `max(0.0, _open_risk)` — if `_open_risk` would go negative, raise or hard-log above tolerance (1e-6)
   - The clamp was hiding the exact bugs we're trying to fix

4. **Fix hardcoded $100 (Liora's finding):**
   - `blend_runner.py:133` must pass actual `risk_amount` from the signal, not `starting_balance * 0.01`
   - `forward_test_engine.py` late-fill callback must pass actual `risk_amount` to `cancel_risk()`

**Files:**
- `src/forex_bot/risk/sl_position_sizer.py` (major refactor)
- `src/forex_bot/forward_test/blend_runner.py` (update callsites)
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` (update callsites)

**AC:**
- [ ] Unit test: `register("sig1", 100); cancel("sig1"); cancel("sig1")` — second cancel is no-op, `_open_risk == 0`, no warning
- [ ] Unit test: `register("sig1", 100); close("sig1", 50)` — `_open_risk == 0`, win recorded
- [ ] Unit test: `close("sig1", 50)` without prior `register` — raises `KeyError` or returns error
- [ ] Unit test: 2 threads concurrently call `register` and `cancel` — `_open_risk` ends correct, no overshoot
- [ ] Unit test: `_open_risk` going negative raises/hard-logs instead of silent clamp
- [ ] `blend_runner.py` passes actual `risk_amount`, not hardcoded $100
- [ ] `risk_state_blend.json` shows `open_risk > 0` while positions are open

**Dependencies:** None strictly, but should land after Phase 4 (correct prices needed for sizing)

---

### Phase 6: Risk Lifecycle + Late-Fill Callback (3 SP)

**Scope:** Fix when risk is cancelled and how late-fill callbacks process events.

**What's wrong (council synthesis):**
- `cancel_risk()` fires on `TIMEOUT` (transient state) — but the order may still fill later. When it does, risk was already freed, so the fill's position exists on broker but local `open_risk` stays at 0.
- Late-fill callback has no idempotency — duplicate execution events can trigger double-processing.
- Late-fill callback ordering: amend SL/TP happens before re-registering risk (wrong order).

**Implementation:**

1. **Don't cancel on TIMEOUT (original Task C):**
   - In `_register_late_fill_callbacks()`, change risk-release branch: only call `cancel()` for `REJECTED`, `CANCELLED`, or `NOT_CONNECTED` — NOT for `TIMEOUT` or `SENT`
   - Risk stays reserved until permanent resolution

2. **Stale-risk TTL + reaper (30s configurable):**
   - Add a reaper that checks for positions in `TIMEOUT` state older than 30s
   - If no fill or rejection arrived in 30s, auto-cancel the reserved risk
   - Track timeout timestamp per signal_id
   - Configurable via `AYUMI_STALE_RISK_TTL` env var (default 30)

3. **Late-fill callback idempotency (Kaito):**
   - Add per-order `processed_events: set[str]` to dedupe by `clientOrderId + payloadType + execType`
   - If event already processed, skip

4. **Late-fill callback ordering:**
   - When late fill arrives after `TIMEOUT`:
     a. Re-register risk via `register(signal_id, risk_amount)` if previously cancelled by reaper
     b. Attach SL/TP via `amend_sl_tp()`
     c. Log: `Late fill recovered for signal %s — risk re-registered`

5. **Handle partial amend success (Rei):**
   - If `amend_sl_tp()` applies SL but TP is rejected (or vice versa), log the partial state
   - Do not treat as full success — the position has partial protection

**Files:**
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` (callback logic, risk-release branches)
- `src/forex_bot/forward_test/blend_runner.py` (cancel_risk callsite)

**AC:**
- [ ] Dry-run: `TIMEOUT` that resolves to late fill does NOT emit `Risk cancelled` before the fill
- [ ] Dry-run: 30s after `TIMEOUT` with no resolution, risk is auto-cancelled by reaper
- [ ] Dry-run: late fill arriving after reaper cancellation re-registers risk correctly
- [ ] Unit test: duplicate execution event for same order is processed once
- [ ] `daily_risk_used` increases monotonically as trades are accepted

**Dependencies:** Phase 5 (identity-keyed API is prerequisite)

---

### Phase 7: Broker Startup Preflight — Seed Existing Positions (2 SP)

**Scope:** Reconcile with broker at startup. **Seed risk from existing positions** (Craig decision). Don't reconcile periodically yet — that's deferred.

**What's wrong (council synthesis):**
- `reconcile()` exists in `OpenApiSpotFeed` but is never called for risk purposes
- Launcher has no concept of "dirty account" — starts trading on top of pre-existing positions
- Ren: split into D1 (startup preflight) and D2 (periodic monitor). This phase is D1 only.

**Implementation:**

1. **Startup preflight + seed (Craig's choice: Option A):**
   - Before first strategy evaluation, call `reconcile()` to fetch live cTrader positions
   - If positions exist:
     - For each position, compute risk from `(entry_price - sl_price) × lots × pip_value` if SL is set
     - If SL is not set, use conservative estimate: assume SL at entry ± ATR-based distance, or use entry-only fallback with WARNING log
     - Seed `SLPositionSizer` via the identity-keyed API (`register(positionId, risk_amount)`)
     - Log: `Preflight: seeded N open cTrader positions totaling $X risk`
   - Continue with normal operation. Daily cap still applies to new signals.

2. **Live-mode hard block (Craig's Q4 answer):**
   - Add a validation marker check in the live-mode launcher: `data/ayumi/remediation_validated.flag`
   - If file doesn't exist and mode is live: `Refusing to start in live mode: remediation not validated`
   - Demo mode ignores this check

3. **Reconcile data enrichment (Liora's finding):**
   - `reconcile()` must return enough data to seed the sizer: `positionId`, `symbol`, `volume`, `entry_price`, `sl_price` (nullable)
   - If SL is not set on a broker position, conservative risk estimate from volume × symbol pip value (with WARNING log)

**Files:**
- `src/forex_bot/adapters/ctrader/open_api_spot_feed.py` (enrich reconcile return)
- `src/forex_bot/adapters/ctrader/forward_test_engine.py` (call reconcile at startup)
- `src/forex_bot/forward_test/blend_runner.py` (seed sizer from reconcile data)

**AC:**
- [ ] Startup log shows preflight result with seeded position count and total risk
- [ ] If cTrader has pre-existing positions, `SLPositionSizer` is seeded with each position's risk
- [ ] If a broker position has no SL, conservative risk estimate is used and a WARNING is logged
- [ ] Live-mode launcher blocks without `remediation_validated.flag`
- [ ] `risk_state_blend.json` `open_risk` reflects seeded positions within 10% of broker exposure
- [ ] After seeding, daily cap rejects new signals that would exceed threshold

**Dependencies:** Phase 5 (identity-keyed sizer needed to seed positions correctly)

---

## Dependency Graph

```
Phase 1 (stop bleeding)  ──┐
Phase 2 (amend fixes)    ──┼──> Phase 4 (USDJPY fix) ──> Phase 5 (sizer refactor) ──> Phase 6 (lifecycle) ──> Phase 7 (preflight)
Phase 3 (USDJPY spike)   ──┘                                                    │
                                                                                │
                                                                    (Phase 5 needs correct prices
                                                                     for sizing validation)
```

**Parallel wave 1:** Phases 1 + 2 + 3 (3 builders, within concurrent limit)  
**Wave 2:** Phase 4 (after Phase 3 lands)  
**Wave 3:** Phase 5 (after Phase 4, or parallel if Phase 4 is quick)  
**Wave 4:** Phase 6 (after Phase 5)  
**Wave 5:** Phase 7 (after Phase 5)

---

## Deferred to Future Sprint

These items were raised by council but are NOT in the critical path. They should be carded as `[DEBT]` for a follow-up sprint:

| Item | Source | Why deferred |
|---|---|---|
| Periodic drift monitor (60s reconcile) | Ren (D2 split) | Startup preflight covers the immediate risk; periodic monitor is optimization |
| `amend_sl_tp` kill-switch policy awareness | Original Task E | Defensive future-proofing, not blocking correct operation |
| `_pending_orders` thread lock | Kaito | Important but not blocking — current code processes events serially enough to work |
| `StatePersistence.save()` synchronization | Kaito | JSON corruption risk is low for demo; fix with broader persistence refactor |
| Append-only risk event log | Liora | Observability improvement, not correctness fix |
| Correlation gate + risk gate atomicity | Kaito | Edge case, low probability in current strategy set |
| Static symbol registry fallback risk | Rei | Only triggers on API failure, which is rare |

---

## Verification Approach

### Per-Phase Tests (builder responsibility)
Each phase includes specific acceptance criteria. Builder must demonstrate pass with:
- Unit test output
- `ruff check` and `python3 -m py_compile` on modified files
- Integration verification (grep evidence that new code is called)

### Forward-Test Dry Runs (Ava validation, after all phases)
1. **Canary disabled:** 30-min run, zero canary signals
2. **USDJPY only:** Run `session_range_mr` on USDJPY, confirm no `TRADING_BAD_STOPS`, SL/TP attached with yen-scale prices
3. **Risk state accuracy:** Start with manually placed cTrader positions, verify preflight detects them and `open_risk` matches
4. **Full blend overnight:** Run all production strategies overnight, confirm `daily_risk_used` increases, `open_risk` tracks broker positions, no naked positions

---

## Craig's Q&A Answers (incorporated into plan)

| Q | Answer | Where in plan |
|---|---|---|
| Q1: Canary fate | Env-gate (`AYUMI_ENABLE_CANARY=1`) | Phase 1A |
| Q2: Reconcile cadence | Event-driven at startup (Phase 7). Periodic deferred. | Phase 7 + Deferred |
| Q3: Stale-risk TTL | 30s configurable via `AYUMI_STALE_RISK_TTL` | Phase 6 |
| Q4: Block live-mode | Yes, hard block via `remediation_validated.flag` | Phase 7 |
| Q5: USDJPY source | Separate spike (Phase 3) → fix (Phase 4) | Phases 3+4 |

---

## Summary for Craig

**7 phases, 15 SP total:**

| Phase | SP | What | Dependencies |
|---|---|---|---|
| 1 | 2 | Canary disable + positionId fix | None |
| 2 | 2 | Amend cooldown fix + observability | None |
| 3 | 1 | USDJPY scaling spike (investigation) | None |
| 4 | 2 | USDJPY scaling fix (implement) | Phase 3 |
| 5 | 3 | Risk sizer refactor (identity-keyed + thread-safe) | Phase 4 |
| 6 | 3 | Risk lifecycle + late-fill callback | Phase 5 |
| 7 | 2 | Broker startup preflight + live-mode block | Phase 5 |

**Wave 1 (parallel):** Phases 1, 2, 3 — can start immediately  
**Then:** 4 → 5 → 6 → 7 sequentially  

**Biggest risks:** Phase 3 spike might reveal something unexpected (changes Phase 4 scope). Phase 5 is the largest refactor (3 SP) — if builder struggles, split into 5a (thread lock + hardcoded fix, 1 SP) and 5b (identity-keyed refactor, 2 SP).
