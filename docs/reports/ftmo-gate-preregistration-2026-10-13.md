# FTMO Gate Pre-Registration — Sprint E Matrix (Card 37078a83)

**Card:** 37078a83-2af3-4e5c-9b6e-0ad419192c72
**Written:** 2026-10-13, **before** any matrix cell has been run and **before** any grid
result has been observed.
**Frozen window:** 2022-01-15 → 2026-07-01 (inclusive), symbol XAUUSD.
**Grid:** 17 strategies × 5 timeframes (M3, M5, M15, M30, H1) = 85 cells.

---

## Why this document exists

The card requires the FTMO thresholds to be registered *before* results are seen, so
that the verdict cannot be reverse-engineered to fit whatever the matrix happened to
produce. This file is that registration.

**Integrity condition:** this file must be **committed to git before the G4 matrix is
launched.** An unregistered-after-the-fact threshold is not a pre-registration. As of
writing it is present on disk only — see "Registration status" at the bottom.

---

## 1. Thresholds

| # | Gate criterion | Threshold | Source |
|---|---|---|---|
| T1 | Total return over the frozen window | **≥ +10.0%** | Card-specified |
| T2 | Maximum daily drawdown | **≤ 3.0%** | Card-specified |
| T3 | Daily-DD breaches over the window | **0** | Derived from T2 (FTMO terminates at the limit; a breach is disqualifying, not a margin) |
| T4 | Timeframe breadth | Pass on **≥ 2 distinct timeframes** of the 5 | Card-specified |
| T5 | Trade count | **≥ 30 closed trades per passing cell** | **Default chosen here** — see §1.1 |
| T6 | Statistical survival | Passes the gate-6 DSR/BH-FDR selection-set pass at α = 0.05 | Card-specified (gate 6, merged `8bd7676`) |

A strategy is **FTMO-viable** iff it satisfies T1–T6 simultaneously.

### 1.1 Justification for the T5 default (30 trades)

The card names "min trade-count floor" as a parameter but does not fix a number; I am
setting it and recording that I set it. **30 is a default, not a card requirement, and is
open to amendment by a reviewer before the matrix launches — after launch it is frozen.**

Rationale: at 30 trades the standard error of an estimated Sharpe is still wide enough
that a 10% return over the frozen window is distinguishable from zero under the
non-normality correction in the gate-6 pass. Below ~30, the per-cell Sharpe is
dominated by sampling noise and the DSR deflation denominator (N across the whole
selection set, not per-cell) becomes the only thing standing between a lucky cell and a
discovery claim. Setting the floor low would manufacture candidates; setting it very high
would discard real signal. 30 is the conventional minimum for a per-cell Sharpe to carry
any weight and is deliberately conservative for a claim this consequential.

### 1.2 Deliberately NOT made a threshold

- **Sharpe ratio.** Not a gate criterion. It is reported, and it is the DSR input, but a
  raw Sharpe cutoff on top of T1/T2/T6 would double-count the same evidence.
- **Win rate, profit factor, R-multiple.** Reported for diagnosis only. Same reason.
- **Cross-symbol robustness.** Out of scope — this grid is XAUUSD only.

---

## 2. Selection-set and multiple-testing handling (T6)

The 85-cell grid is a **selection set**: a winner is chosen by comparing cells, so the
winner's statistics are selection-on-the-maximum and are biased upward.

- The deflation denominator is the trial count across the **whole selection set (N=85)**
  for the full grid, or **N = (cells actually run)** if G4 stops at a staged subset. The
  staged-subset N must be reported explicitly, per gate-6 requirement.
- DSR consumes the **per-bar** Sharpe (`expected_max_sharpe` and `SE(SR)=sqrt(V/(n-1))`
  are per-observation quantities). An annualised Sharpe would inflate every DSR.
- A cell is a **discovery** only if it clears the Benjamini-Hochberg step **and**
  `DEFAULT_DSR_ALPHA` (0.05). The earlier `dsr_pvalue < 1.0` formulation was vacuous and
  was removed in `9ffea2b5` — it must not be reintroduced.
- **N is reported in the verdict memo.** AC3 requires it.

---

## 3. Tie-break rule (frozen)

If more than one strategy is FTMO-viable, the tie-break order below is applied in
sequence; the first criterion that separates the candidates decides.

1. **Higher DSR-adjusted discovery rank** from the gate-6 selection-set pass (the same
   ranking that clears T6). This is the primary ordering because it is the only one that
   accounts for the 85-way selection.
2. **Lower maximum daily drawdown.**
3. **Higher total return.**
4. **Higher trade count.**
5. **Lexicographic strategy_id** — a deterministic final tiebreak so the outcome never
   depends on filesystem or dictionary ordering.

Criterion 5 exists only so that the selection is reproducible bit-for-bit. It is not a
substantive criterion and must never be the reason a strategy is named.

---

## 4. Verdict rule (frozen)

- **GO** — at least one strategy satisfies T1–T6. The memo names that strategy, the
  specific cells that cleared each threshold, and the selection-set N.
- **NO-GO** — no strategy satisfies T1–T6.

**NO-GO is a valid, complete deliverable and is to be stated plainly, without
softening language, hedging, or "promising to revisit".** The current smoke evidence
(XAUUSD H1 2024-01-01..2024-03-01, `srmr_plus` −0.2514%, `bb_rsi_reversion` −0.505%) is
from a 2-month smoke slice and is **not** the frozen window; it carries no predictive
weight for the verdict and must not be cited in either direction.

---

## 5. Zero-signal cells

A cell that produces zero signals is reported with its full triage (bars_processed,
first/last bar, skip reason, written diagnosis) and counts as **failing T5**, not as
"no data". The full grid is published **including losers**. A zero-signal cell on the
frozen window is a harness or window bug until proven otherwise — the 2026-09-21 runbook
finding is explicit that zero signals on a *full* window means investigate, never
"no edge".

---

## 6. Registration status

| Item | State |
|---|---|
| Thresholds T1–T6 written | ✅ |
| Tie-break frozen | ✅ |
| Verdict rule frozen | ✅ |
| Any matrix cell run | ❌ none |
| Any grid result observed | ❌ none |
| **Committed to git** | ❌ **not yet — executor exec budget exhausted before commit** |

**Action required before G4 launches:** commit this file, and resolve the T5 trade-count
default (accept 30, or amend it) at the same time. Once the matrix runs, these numbers
are frozen — a later amendment is a post-hoc threshold change and the verdict loses its
pre-registration standing.

---

## 7. Resolution (2026-10-13, pre-G4)

- **T5 accepted at 30** by Ava (orchestrator) — rationale in §1.1 endorsed; frozen as of
  the commit below. Any later change is post-hoc and voids the registration.
- This file committed to git by Ava before G4 launch (commit recorded in the commit
  message referencing this card). `data/tournament/equivalence/` artifacts remain on-disk
  artifacts (dir is gitignored by repo policy); the card's AC1 verification reads them
  from disk.