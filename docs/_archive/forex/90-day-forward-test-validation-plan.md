# 90-Day Forward Test Validation Plan

**Issue:** AYU-131 | **Status:** in_progress | **Type:** Research Task
**Agent:** Sage (QA + Research) | **Date:** 2026-04-23

## Executive Summary

This document defines the 90-day forward test validation plan for the Ayumi forex trading bot. The plan establishes phased validation gates that balance rapid iteration against rigorous FTMO compliance requirements. Based on prior walk-forward results (AYUAA-221 NO-GO), this plan focuses on strategies that passed walk-forward QA while ensuring the statistical significance thresholds are met before live deployment.

**Key Finding:** The hybrid ICT/SMC + quantitative overlay strategy (AYUAA-221) failed walk-forward with only 1/5 windows passing per pair. Any forward test must address the core deficiencies: WR ~42% vs 55% required, and only 1/5 profitable windows.

---

## Phase 0: Pre-Launch (Days -7 to 0)

### Gate: Before Forward Test Begins

| Criterion | Target | Current Status | Action Required |
|-----------|--------|----------------|-----------------|
| Walk-Forward Pass | ≥3/5 windows | Hybrid: 1/5 ❌ | Must identify passing strategy |
| Backtest WR | >55% | Hybrid: 42% ❌ | Strategy parameter adjustment needed |
| Backtest PF | >1.5 | Hybrid: 0.97 ❌ | See above |
| Sharpe Ratio | >0.5 | Hybrid: -1.05 ❌ | See above |
| RiskGuard Configured | FTMO params | Implemented ✅ | Verify FTMO 1-Step config |
| PaperTrader Status | Connected | Implemented ✅ | E2E test pending |
| cTrader Demo Account | Active | Need verification | Confirm connectivity |

### Pre-Test Requirements

1. **Strategy Selection:** Identify 1-2 strategies passing walk-forward QA (≥3/5 windows)
2. **Parameter Freeze:** No code changes during 90-day forward test
3. **Position Sizing:** Start at 50% recommended size for first 2 weeks
4. **Circuit Breaker Configured:** 5-min auto-pause after daily loss limit

### Entry Criteria for Phase 1

- [ ] At least 1 strategy with 3+/5 walk-forward windows passing
- [ ] Backtest WR >55% confirmed
- [ ] cTrader demo account verified and connected
- [ ] PaperTrader balance matches account balance
- [ ] RiskGuard FTMO parameters verified

---

## Phase 1: Foundation & Signal Validation (Days 1-30)

**Objective:** Establish baseline signal quality and paper trading discipline

### Week 1-2: Signal Generation Review

| Day | Activity | Success Criteria |
|-----|----------|------------------|
| 1-3 | cTrader demo connection verification | QUOTE and TRADE ports connected |
| 4-7 | Paper trading with 50% size | No execution errors, logs flowing |
| 8-14 | Signal monitoring | Signals generating on expected pairs |

**Metrics to Track:**
- Signal count per session (London/NY)
- Signal rejection rate (from filters)
- Paper trade execution latency

### Week 3-4: Trade Journal Establishment

| Day | Activity | Deliverable |
|-----|----------|-------------|
| 15-20 | Full paper trading | All trades logged in CSV format |
| 21-25 | Daily review | Reconciliation with cTrader balance |
| 26-30 | Weekly summary | P&L, WR, PF, MaxDD per week |

**Phase 1 Gate (Day 30) — GO/NO-GO:**

| Metric | Threshold | Measurement |
|--------|-----------|-------------|
| Net P&L | ≥0 (no losses) | Closed trades only |
| FTMO Rule Compliance | Zero violations | Daily loss <5%, total DD <10% |
| Win Rate | >50% | 4-week sample (minimum 20 trades) |
| Max Daily Loss | <3% | Any single day |

**Phase 1 GO Decision:**
- Net P&L positive OR neutral
- No FTMO rule violations
- WR >50%
- Minimum 20 paper trades

**Phase 1 NO-GO Triggers:**
- Daily loss >5% any day
- Total drawdown >10%
- Net loss >2%
- 3+ consecutive losing days

---

## Phase 2: Validation & Consistency (Days 31-60)

**Objective:** Demonstrate consistent performance across varying market conditions

### Week 5-6: Size Increase to 75%

| Day | Activity | Size |
|-----|----------|------|
| 29 | Size increase decision | 75% of recommended |
| 30-35 | Full trading | 75% position size |
| 36-42 | Mid-phase review | Metrics calculation |

**Week 5-6 Focus:**
- London killzone performance (08:00-10:00 CET)
- NY overlap session (13:30-16:00 CET)
- News event avoidance compliance

### Week 7-8: Size Increase to 100%

| Day | Activity | Size |
|-----|----------|------|
| 43 | Size increase decision | 100% recommended |
| 44-49 | Full trading | 100% position size |
| 50-56 | End-of-phase review | Full metrics calculation |

**Phase 2 Gate (Day 60) — GO/NO-GO:**

| Metric | Threshold | Measurement |
|--------|-----------|-------------|
| Cumulative P&L | >0 | All closed trades |
| Win Rate | >55% | 8-week aggregate |
| Profit Factor | >1.3 | Gross profit / gross loss |
| Max Drawdown | <5% | Peak-to-trough (FTMO 1-Step) |
| Sharpe Ratio | >0.5 | Risk-adjusted returns |
| Trade Count | >100 OOS | Minimum statistical significance |
| Best Day Rule | <50% of total profit | Per FTMO 1-Step rules |

**Phase 2 GO Decision:**
- All metrics meet thresholds
- Consistent performance Week 5-8 vs Week 1-4
- No FTMO rule violations
- Strategy behavior matches backtest expectations

**Phase 2 NO-GO Triggers:**
- WR drops below 50% for 2 consecutive weeks
- PF <1.0 for any 2-week window
- MaxDD >7.5% (75% of FTMO limit)
- Net loss in any week >1.5%
- Statistical significance not achieved (p-value >0.10)

---

## Phase 3: Decision & Preparation (Days 61-90)

**Objective:** Final validation before FTMO challenge commitment

### Week 9-10: Final Confirmation

| Day | Activity | Purpose |
|-----|----------|---------|
| 57-63 | Full trading | Final performance data |
| 64-70 | Strategy behavior analysis | Confirm no regime changes |
| 71-75 | FTMO documentation prep | Account application ready |

### Week 11-12: FTMO Application

| Day | Activity | Deliverable |
|-----|----------|-------------|
| 76-80 | FTMO account application | Submitted 1-Step challenge |
| 81-85 | Initial challenge trading | First 2 weeks on FTMO demo |
| 86-90 | End-of-phase review | 90-day validation report |

**Phase 3 Gate (Day 90) — FTMO Entry Decision:**

| Metric | Threshold | Source |
|--------|-----------|--------|
| 90-Day P&L | >10% (if pursuing FTMO) | Actual results |
| Daily Loss Compliance | Zero >5% days | FTMO log |
| Total Drawdown | <10% throughout | FTMO log |
| Best Day Rule | Compliant | FTMO log |
| Strategy Consistency | No regime drift | Signal analysis |

---

## Success Metrics Framework

### GO/NO-GO Criteria Summary

| Phase | Gate Day | Primary GO Criteria | Primary NO-GO Triggers |
|-------|----------|---------------------|------------------------|
| Pre-Launch | 0 | 3+/5 WF windows, WR>55%, PF>1.5 | Strategy fails walk-forward |
| Phase 1 | 30 | P&L≥0, WR>50%, no FTMO violations | Daily loss >5%, DD >10% |
| Phase 2 | 60 | WR>55%, PF>1.3, MaxDD<5%, Sharpe>0.5 | WR<50%×2wks, DD>7.5% |
| Phase 3 | 90 | FTMO entry metrics met | Regime change, violations |

### Statistical Validation Requirements

Per `quant/statistical_validation.py`:
- **Minimum OOS Trades:** 50 (alpha=0.10, t-test for significance)
- **Multi-Pair Validation:** At least 2 pairs with PF>1.0 required for GO
- **P-value Threshold:** <0.10 for one-tailed t-test

---

## Risk Management Protocol

### Position Sizing Progression

| Phase | Size | Notes |
|-------|------|-------|
| Week 1-2 | 50% | Conservative start |
| Week 3-4 | 75% | Phase 1 confirmation |
| Week 5+ | 100% | Full risk parameters |

### Circuit Breaker Rules

1. **Daily Loss Limit (1.5% conservative):** Stop all trading until next day
2. **Consecutive Loss Rule (2 days):** Mandatory day off
3. **Weekly Limit (3%):** Pause until Monday
4. **Max Drawdown (6%):** Stop trading, submit for review

### News Avoidance Protocol

| Event | Avoidance Window |
|-------|------------------|
| NFP | 30 min before/after |
| FOMC | 2 hrs before/after |
| ECB/BOE | 1 hr before/after |
| CPI | Full day |

### Weekend Close Rule

All positions closed Friday 21:00 UTC. No weekend holds during forward test.

---

## cTrader Environment Configuration

### Connection Verification (Pre-Launch)

```
QUOTE Port: 5211 (SSL)
TRADE Port: 5202 (Plain TCP)
Server: live-uk-eqx-01.p.c-trader.com
Account: CTRADER_ACCOUNT in .env
```

### Required Environment Variables

```
CTRADER_CLIENT_ID
CTRADER_CLIENT_SECRET
CTRADER_PASSWORD
CTRADER_USERNAME
CTRADER_ACCOUNT
CTRADER_HOST
```

### PaperTrader Reconciliation

Daily comparison of:
- PaperTrader internal balance
- cTrader account balance
- Open positions count
- Pending orders count

Discrepancies >0.1% trigger investigation before continuing.

---

## Trade Log Format

```csv
timestamp,symbol,direction,entry_price,exit_price,stop_loss,take_profit,volume,pnl,pnl_pct,duration_minutes,strategy_name,signal_rationale
2026-04-15 08:30:00,EURUSD,long,1.0850,1.0870,1.0830,1.0890,0.50,100.00,0.92,240,killzone_momentum,London killzone FVG long
```

### Daily Summary Fields

- Total PnL
- Win/Loss count
- Max drawdown for day
- Circuit breaker triggers
- Open positions at EOD

### Weekly Report Fields

- Cumulative PnL
- Win rate (weekly)
- Profit factor (weekly)
- Max drawdown (weekly)
- Strategy breakdown
- Signal count by type

---

## Deliverables Checklist

### Phase 1 (Day 30)
- [ ] 30-day trade log (CSV)
- [ ] Daily summaries (22 entries)
- [ ] Weekly reports (4 reports)
- [ ] Phase 1 gate metrics report
- [ ] Signal quality analysis

### Phase 2 (Day 60)
- [ ] 60-day trade log (CSV)
- [ ] Phase 2 gate metrics report
- [ ] Consistency analysis (Week 1-4 vs 5-8)
- [ ] FTMO rule compliance report
- [ ] Statistical significance confirmation

### Phase 3 (Day 90)
- [ ] 90-day trade log (CSV)
- [ ] Final performance report
- [ ] FTMO readiness assessment
- [ ] Strategy behavior drift analysis
- [ ] 90-day forward test summary

---

## Blocking Items for Forward Test

Based on `forward-test-protocol.md`:

| Item | Status | Owner | Blocking? |
|------|--------|-------|------------|
| Market data feed (AYUAA-409) | In Review | Kai | Yes - pending QUOTE session wiring |
| TRADE port 5202 connectivity | Not verified | Kai | Yes - E2E order flow test needed |
| End-to-end order flow test | Not completed | Kai | Yes - signal→FIX→execution |
| Passing strategy portfolio | FAILED (1/5 windows) | Research | Yes - must find passing strategy |

**Immediate Action Required:** The AYUAA-221 NO-GO result means no strategy currently meets the minimum walk-forward requirement (3+/5 windows). Forward test cannot proceed until either:
1. A strategy is found/modified that passes ≥3/5 windows, OR
2. Walk-forward criteria are adjusted with proper justification

---

## Recommended Next Steps

1. **Immediate (Day 0-7):**
   - Review all existing walk-forward results for strategies with 3+/5 windows
   - If none found: Research task to identify parameter changes needed for AYUAA-221 NO-GO strategies

2. **Short-term (Day 8-30):**
   - Verify cTrader connectivity
   - Run paper trading with passing strategy
   - Establish trade journal discipline

3. **Medium-term (Day 31-60):**
   - Progress through phases if metrics meet gates
   - Monitor for regime changes in market conditions

4. **Long-term (Day 61-90):**
   - FTMO demo challenge application
   - Monitor against 10% profit target

---

## Appendix: Key Files Reference

| Document | Path | Status |
|----------|------|--------|
| Forward Test Protocol | `docs/forex/forward-test-protocol.md` | Active |
| Walk-Forward Evaluation | `docs/forex/hybrid-strategy-walk-forward-evaluation.md` | NO-GO (AYUAA-221) |
| FTMO Risk Parameters | `docs/forex/ftmo-challenge-risk-parameters-and-trade-plan.md` | Done (AYUAA-54) |
| Go/No-Go Criteria | `src/forex_bot/quant/go_nogo_criteria.py` | Implemented |
| Statistical Validation | `src/forex_bot/quant/statistical_validation.py` | Implemented |
| Walk Forward Engine | `src/forex_bot/quant/walk_forward.py` | Implemented |
| QA Gate Analysis | `docs/forex/qa-gate-evaluation-analysis-april2026.md` | Completed |

---

**Research by:** Sage (QA + Research)
**Confidence:** High in framework accuracy, Medium in strategy selection (pending walk-forward results review)
**Limitations:** This plan assumes at least one strategy passes walk-forward QA. Current evidence suggests this may require parameter tuning or strategy redesign before forward test can begin.