# Forward Test Protocol — FTMO Demo Environment

**Parent:** AYUAA-317 | **Status:** Active

## Purpose

Validate walk-forward passing strategies against live market conditions on cTrader demo before committing to FTMO challenge.

## Environment Configuration

### cTrader Demo Account
- **Server:** `live-uk-eqx-01.p.c-trader.com`
- **SSL Port (QUOTE):** 5211
- **Plain TCP Port (TRADE):** 5202
- **Account:** Configured via `.env` (`CTRADER_ACCOUNT`)
- **Credentials:** All 7 `CTRADER_*` variables in `.env`

### FTMO Challenge Parameters (Hardcoded in RiskGuard)
| Parameter | Value | Source |
|-----------|-------|--------|
| Starting Balance | $10,000 (demo) / $100,000 (live) | FTMOConfig |
| Daily Loss Limit | 5% | `FTMOConfig.daily_loss_limit_pct` |
| Max Drawdown | 10% | `FTMOConfig.total_drawdown_limit_pct` |
| Max Position Size | 2% per trade | `FTMOConfig.max_position_size_pct` |
| Min Risk:Reward | 1.5 | `FTMOConfig.min_risk_reward` |
| Max Trades/Day | 10 | `FTMOConfig.max_trades_per_day` |
| Max Open Positions | 3 | `FTMOConfig.max_positions` |
| Best Day Rule | 50% | `FTMOConfig.best_day_rule_max_pct` |

### Recommended FTMO Challenge Type
**1-Step Challenge** — no minimum trading days, 10% profit target, 90% profit split.

## Forward Test Phases

### Phase 1: Paper Trading Validation (2-4 weeks)
Run strategy on cTrader demo with live market data. No real capital at risk.

**Entry Criteria:**
- At least 1 strategy passes walk-forward QA gate (3/5 windows)
- Backtest WR >55%, PF >1.0, Sharpe >0.5
- RiskGuard configured with FTMO parameters

**Metrics to Track:**
| Metric | Target | Measurement |
|--------|--------|-------------|
| Win Rate | >55% | Closed trades |
| Profit Factor | >1.3 | Gross profit / gross loss |
| Max Daily Loss | <5% | Daily PnL tracking |
| Max Drawdown | <10% | Peak-to-trough |
| Avg Trade Duration | <24h | Open-to-close time |
| Sharpe Ratio | >0.5 | Risk-adjusted returns |

**Pass/Fail Criteria:**
- **PASS:** Net profit >0 AND no FTMO rule violation AND WR>50% over 4-week period
- **FAIL:** Any FTMO rule violation (daily loss >5% or drawdown >10%) OR net loss >2%

### Phase 2: FTMO Demo Challenge (4-8 weeks)
Execute on actual FTMO demo account following official challenge rules.

**Entry Criteria:**
- Phase 1 PASS with consistent results
- Strategy portfolio finalized (1-3 strategies)
- Position sizing validated
- No code changes needed

**FTMO Challenge Rules (1-Step):**
- Profit Target: 10% of starting balance
- Max Daily Loss: 5% (hard stop)
- Max Total Loss: 10% (hard stop)
- No minimum trading days
- 90% profit split on funded account

**Pass/Fail:**
- **PASS:** Reach 10% profit target without breaching loss limits
- **FAIL:** Daily loss >5% OR total loss >10%

### Phase 3: FTMO Funded (Ongoing)
Live trading with FTMO capital.

**Ongoing Rules:**
- Max Daily Loss: 5%
- Max Drawdown: 10%
- Best Day Rule: 50%
- 90% profit split

## Logging Requirements

### Trade Log (CSV format)
```
timestamp, symbol, direction, entry_price, exit_price, stop_loss, take_profit,
volume, pnl, pnl_pct, duration_minutes, strategy_name, signal_rationale
```

### Daily Summary
- Total PnL
- Win/Loss count
- Max drawdown for the day
- Circuit breaker status
- Open positions at EOD

### Weekly Report
- Cumulative PnL
- Win rate (weekly)
- Profit factor (weekly)
- Max drawdown (weekly)
- Strategy performance breakdown

## Risk Management During Forward Test

1. **Start with 50% position sizing** — halve the max 2% to 1% per trade for first week
2. **Circuit breaker** — automatic 5-min pause after daily loss limit hit
3. **Close all before major news** — NFP, FOMC, ECB rate decisions
4. **No weekend holding** — close all positions Friday 21:00 UTC
5. **Daily balance reconciliation** — compare PaperTrader balance with cTrader account balance

## Components

| Component | File | Status |
|-----------|------|--------|
| PaperTrader | `src/forex_bot/adapters/ctrader/paper_trader.py` | Done |
| RiskGuard | `src/forex_bot/adapters/ctrader/risk_guard.py` | Done |
| OrderManager | `src/forex_bot/adapters/ctrader/order_manager.py` | Done |
| cTrader API Client | `src/forex_bot/adapters/ctrader/api_client.py` | Done |
| Market Data Feed | `src/forex_bot/adapters/ctrader/market_data_feed.py` | In Review (AYUAA-409) |
| FTMO Risk Research | `docs/forex/ftmo-challenge-risk-parameters-and-trade-plan.md` | Done |

## Blocking Items

- [ ] Market data feed (AYUAA-409) — needs QUOTE session wiring
- [ ] TRADE port 5202 connectivity verification
- [ ] End-to-end order flow test (signal -> FIX order -> execution report)
- [ ] Passing strategy portfolio from parameter sweeps (AYUAA-490, 491, 495)
