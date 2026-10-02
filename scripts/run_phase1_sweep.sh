#!/bin/bash
# Phase 1 Strategy Sweep — runs all strategies across all pairs/TFs through SRF
# Usage: bash scripts/run_phase1_sweep.sh
set -euo pipefail

cd $AYUMI_ROOT
source .venv/bin/activate
export PYTHONPATH=src/forex_bot

DB="data/research/research.duckdb"
LOG="/tmp/phase1_sweep_$(date +%Y%m%d_%H%M%S).log"
echo "Phase 1 Sweep — logging to $LOG"
echo "Started: $(date)" | tee "$LOG"

# Strategies to test
STRATEGIES=(
  "strategies.killzone_momentum.KillzoneMomentumStrategy"
  "strategies.volatility_squeeze.VolatilitySqueezeStrategy"
  "strategies.volatility_regime_breakout.VolatilityRegimeBreakoutStrategy"
  "strategies.srmr_plus.SRMRPlusStrategy"
  "strategies.ttc_xauusd.TTCXAUUSDStrategy"
  "strategies.bb_rsi_reversion.BBRSIReversionStrategy"
  "strategies.donchian_atr_trend.DonchianATRTrendStrategy"
  "strategies.london_breakout_retest.LondonBreakoutRetestStrategy"
)

# Pair/TF combinations (using available CSVs)
# Format: "PAIR TF CSV_PATH SHORT_NAME"
RUNS=(
  "EURUSD 5  data/forex/historical/EURUSD_M5.csv  EURUSD_M5"
  "EURUSD 15 data/forex/historical/EURUSD_M15.csv EURUSD_M15"
  "EURUSD 60 data/forex/historical/EURUSD_H1.csv  EURUSD_H1"
  "GBPUSD 5  data/forex/historical/GBPUSD_M5.csv  GBPUSD_M5"
  "GBPUSD 15 data/forex/historical/GBPUSD_M15.csv GBPUSD_M15"
  "GBPUSD 60 data/forex/historical/GBPUSD_H1.csv  GBPUSD_H1"
  "XAUUSD 5  data/forex/historical/XAUUSD_M5.csv  XAUUSD_M5"
  "XAUUSD 15 data/forex/historical/XAUUSD_M15.csv XAUUSD_M15"
  "XAUUSD 60 data/forex/historical/XAUUSD_H1.csv  XAUUSD_H1"
)

PASS=0
FAIL=0
SKIP=0
TOTAL=$(( ${#STRATEGIES[@]} * ${#RUNS[@]} ))
COUNT=0

for strategy_path in "${STRATEGIES[@]}"; do
  short_name=$(echo "$strategy_path" | sed 's/.*\.//' | sed 's/Strategy$//' | tr '[:upper:]' '[:lower:]')
  
  for run in "${RUNS[@]}"; do
    read -r pair tf csv run_name <<< "$run"
    COUNT=$((COUNT + 1))
    
    # TTCXAUUSDStrategy only works on XAUUSD
    if [[ "$short_name" == "ttc_xauusd" && "$pair" != "XAUUSD" ]]; then
      SKIP=$((SKIP + 1))
      echo "[$COUNT/$TOTAL] SKIP: $short_name on $pair M$tf (XAUUSD only)" | tee -a "$LOG"
      continue
    fi
    
    echo "[$COUNT/$TOTAL] Running: $short_name on $pair M$tf..." | tee -a "$LOG"
    
    if python3 -m srf run \
      --strategy "$strategy_path" \
      --pair "$pair" --tf "$tf" \
      --data "$csv" \
      --windows 5 --balance 10000 --confidence 0.30 \
      --db "$DB" --repo . \
      --name "$short_name" >> "$LOG" 2>&1; then
      PASS=$((PASS + 1))
      echo "  ✓ completed" | tee -a "$LOG"
    else
      FAIL=$((FAIL + 1))
      echo "  ✗ failed (exit $?)" | tee -a "$LOG"
    fi
  done
done

echo "" | tee -a "$LOG"
echo "=== Sweep Complete ===" | tee -a "$LOG"
echo "Passed: $PASS | Failed: $FAIL | Skipped: $SKIP | Total: $TOTAL" | tee -a "$LOG"
echo "Finished: $(date)" | tee -a "$LOG"
echo "Log: $LOG"
