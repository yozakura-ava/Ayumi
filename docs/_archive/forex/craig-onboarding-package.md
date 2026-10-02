# Craig Onboarding Package — AYU-125

**Parent:** [AYU-122](/AYU/issues/AYU-122) | **Status:** Complete

---

## What's in This Package

This document gives Craig everything needed to start using the hybrid trading system. It combines the trading rules, interface guide, risk parameters, and quick-reference card into one place.

---

## System Architecture: What Algo Does vs What Craig Does

### Craig's Role (Human Discretionary)
- **Entry decisions**: Identify and signal trade setups based on structured discretionary rules
- **Direction**: Choose BUY or SELL based on technical/fundamental analysis  
- **Pattern confirmation**: Confirm ICT concepts (order blocks, FVGs, liquidity sweeps) are present
- **Session selection**: Trade only during specified killzones

### Algo's Role (Automated Risk Management)
- **Position sizing**: Calculated from account balance + risk parameters (not Craig's decision)
- **Stop loss placement**: Algorithm places SL based on structure, not Craig's preference
- **Take profit management**: 3-tier TP with progressive SL trailing
- **Drawdown circuit breakers**: Daily loss limit, weekly drawdown limit, max positions
- **FTMO compliance**: Every trade checked against FTMO evaluation rules
- **Risk validation**: Rejects signals that violate R:R minimum, DD limits, position size

### Division of Labor
```
Craig decides: WHAT to trade (pattern, direction, pair)
Algo decides: How much to risk, where stops go, when to take profit, when to stop
```

---

## Trading Rules Reference (Quick Card)

### Valid Entry Criteria — ALL must be present
1. **Active session**: London (02-05 UTC), NY AM (13-16 UTC), or NY PM (18-20 UTC)
2. **H4 trend confirmation**: Price above/below H4 SMA 21 for long/short
3. **Pattern present**: Order block, FVG, or liquidity sweep at entry zone
4. **Risk:Reward >= 1.5**: SL distance vs TP distance

### Entry Signal Format (CLI)
```
python -m hybrid signal BUY EURUSD entry=1.2345 sl=1.2330 tp1=1.2355 confidence=0.75 rationale="London killzone + H4 uptrend"
```

### Session Rules
| Session | Hours (UTC) | Bias | Notes |
|---------|-------------|------|-------|
| London | 02:00-05:00 | Bullish | Best for long entries |
| NY AM | 13:00-16:00 | Bearish | Best for short entries |
| NY PM | 18:00-20:00 | Range-bound | Mean reversion only |
| Asian | 00:00-02:00 | Range-bound | No trend entries |
| Outside | — | No entries | Skip |

### Circuit Breakers (Automatic)
- **Daily loss > 4%**: Stop new entries until next day
- **Total drawdown > 8%**: Full stop + alert operator
- **Max 3 concurrent positions**: No new entries if at limit
- **Max 10 trades/day**: RiskGuard enforces FTMO limit

### Recovery Protocol (After Circuit Breaker)
1. Paper trade only for 3 consecutive winning days
2. If 3 winners: increase lot size 50%
3. If 1 loser: reduce to minimum lot
4. Resume normal sizing only after 5 consecutive winning days

---

## Risk Parameters (Current Defaults)

| Parameter | Default | Craig Adjustable? | FTMO Hard Limit |
|-----------|---------|-------------------|-----------------|
| Risk per trade | 0.5% | Yes (0.5%-2%) | 2% max |
| Daily loss limit | 1.5% | Yes (1%-5%) | 5% |
| Daily DD limit | 4.5% | Yes (3%-5%) | 10% |
| Weekly DD limit | 9% | No | 10% |
| Max positions | 3 | No | 3 |
| Max trades/day | 10 | No | 10 |
| Min R:R | 1.5 | No | 1.5 |
| Max position size | 2% | No | 2% |

**FTMO Safety Margins**: Our limits are tighter than FTMO's to provide buffer.
- FTMO daily loss: 5% → We use 1.5% default (3x buffer)
- FTMO total DD: 10% → We use 4.5% default (2x buffer)

---

## Getting Started Guide

### Step 1: Install and Configure
1. Clone repo: `git clone https://github.com/ayumi/forex_bot`
2. Install: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and configure cTrader credentials
4. Test connection: `python -m hybrid status`

### Step 2: Paper Trade for 1 Week
1. Run on cTrader demo account only
2. Start with 0.5% risk per trade
3. Maximum 3 trades per day
4. Log every signal (rationale, outcome)
5. Review daily PnL at end of each day

### Step 3: Review Performance Report
1. Check WR%, PF, max DD after 1 week
2. Target: WR >50%, PF >1.0, no circuit breaker triggers
3. If passing: proceed to Step 4
4. If failing: identify which rule was violated, adjust

### Step 4: Go Live with Minimum Lot Size
1. Switch to live FTMO demo account
2. Use minimum lot size (0.01 lots)
3. Continue 1:1 with paper trade rules
4. After 2 weeks consistent performance: consider increasing lot size

---

## CLI Reference

### Signal Command
```bash
python -m hybrid signal BUY EURUSD entry=1.2345 sl=1.2330 tp1=1.2355 confidence=0.75 rationale="London killzone"
```

**Required fields**: direction, pair, entry, sl, tp1
**Optional fields**: confidence (0.0-1.0), rationale

### Status Command
```bash
python -m hybrid status
```
Shows: daily PnL, drawdown %, open positions, session info, circuit breaker status

### Positions Command
```bash
python -m hybrid positions
```
Shows: all open positions with entry price, current PnL, SL/TP levels

### Close Command
```bash
python -m hybrid close <position_id>
```
Manually close a specific position

---

## Signal Validation (What Algo Rejects)

| Code | Reason | Craig Action |
|------|--------|--------------|
| R:R_LOW | R:R < 1.5 | Widen TP or tighten SL |
| DD_HIGH | Daily DD > 4.5% | Wait until tomorrow |
| MAX_POS | 3 positions open | Close one first |
| SESSION_BAD | Outside killzone | Wait for session |
| SL_RANGE | SL < 5 pips or > 50 pips | Adjust SL distance |
| PAIR_NA | Pair not in allowed list | Use EURUSD/GBPUSD only |

---

## Files Referenced
- Hybrid engine: `src/forex_bot/hybrid/engine.py`
- Risk guard: `src/forex_bot/adapters/ctrader/risk_guard.py`
- Forward test protocol: `docs/forex/forward-test-protocol.md`
- FTMO rules: `docs/forex/ftmo-challenge-risk-parameters-and-trade-plan.md`
