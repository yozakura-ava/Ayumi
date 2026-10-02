# Pair Selection Policy — 4-Cluster Correlation Framework

> **Source:** SRB-AYUMI-001 (Satoshi Research Brief, Tier 1, 2026-07-01)
> **Status:** Implemented
> **Date:** 2026-07-06

## Problem

The FTMO 10-pair portfolio collapses to 4 correlation clusters. Trading more than 2 pairs from any single cluster is functionally equivalent to holding a single oversized position — the pairs tend to win and lose together, amplifying drawdown risk beyond FTMO's 3% daily / 10% total limits.

## The 4 Clusters

| Cluster | Pairs | Driver |
|---------|-------|--------|
| **USD-weak majors** | EURUSD, GBPUSD | EUR/GBP strength = USD weakness |
| **USD-strong** | USDJPY, USDCHF, USDCAD | Safe-haven USD demand |
| **Commodity-linked** | AUDUSD, XAUUSD, USDCAD | Commodity flow (AUD, gold, oil-linked CAD) |
| **JPY crosses** | GBPJPY, EURJPY, EURGBP | JPY safe-haven flows + EUR/GBP cross |

**Note:** USDCAD appears in two clusters (USD-strong and commodity-linked). This is intentional — CAD is influenced by both USD regime and commodity flows. A pair belonging to multiple clusters means trading it counts against both cluster limits.

## Policy

**Maximum 2 active pairs per cluster.**

A portfolio with 3+ pairs from one cluster violates the policy regardless of direction (long or short). Correlation is direction-agnostic for risk purposes — correlated pairs move together regardless of trade direction bias.

## Implementation

### Modules

- `src/forex_bot/risk/correlation_matrix.py` — Rolling Pearson correlation matrix with dual-window (30d + 90d) support
- `src/forex_bot/risk/pair_selection.py` — Cluster definitions, policy enforcement, validation

### Usage

```python
from risk.pair_selection import PairSelectionPolicy

policy = PairSelectionPolicy()

# Validate a portfolio
result = policy.validate_portfolio(["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])
print(result.summary)
# "Portfolio valid: 4 pairs across 4 clusters."

# Flag over-clustered portfolios
flagged = policy.flag_over_clustered(["USDJPY", "USDCHF", "USDCAD"])
# ["usd_strong"]

# Check which clusters a pair belongs to
clusters = policy.find_cluster("USDCAD")
# ["commodity_linked", "usd_strong"]
```

### Correlation Matrix

```python
from risk.correlation_matrix import CorrelationMatrix

cm = CorrelationMatrix()
cm.add_returns("EURUSD", [...])
cm.add_returns("GBPUSD", [...])

# Get both 30d and 90d correlation matrices
matrices = cm.compute_multi_window([30, 90])
corr_30d = matrices[30]["EURUSD"]["GBPUSD"]
corr_90d = matrices[90]["EURUSD"]["GBPUSD"]
```

The 30-day window captures short-term correlation shifts (useful for tactical risk management). The 90-day window captures structural correlation (useful for portfolio construction).

## Out of Scope

- **Selecting which 2 pairs per cluster:** Craig's decision based on liquidity, spread, and strategy preference
- **New pair discovery:** Beyond the 10 listed pairs
- **Live trade execution integration:** Separate card for wiring into the execution layer
- **Dynamic cluster reassignment:** Clusters are static per the research; dynamic re-clustering is a future enhancement

## Rollback

```bash
git checkout HEAD -- src/forex_bot/risk/correlation_matrix.py \
    src/forex_bot/risk/pair_selection.py \
    tests/unit/risk/test_pair_selection.py
rm -f docs/ayumi/pair-selection-policy.md
```
