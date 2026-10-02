"""Proximity thresholds from spec §1.2. Single source of truth for all distance checks."""

# Default proximity
NEAR_THRESHOLD = 0.005  # 0.5%
AT_THRESHOLD = 0.002  # 0.2%

# M/W symmetry
MW_SYMMETRY_MAX = 0.015  # 1.5%
MW_EQUAL_THRESHOLD = 0.0005  # 0.05%

# Equal swing merge
EQUAL_SWING_THRESHOLD = 0.0005  # 0.05%

# Trap break thresholds
TRAP_BREAK_LONDON_NY = 0.002  # 0.2%
TRAP_BREAK_ASIA = 0.004  # 0.4%
TRAP_BREAK_CRYPTO = 0.003  # 0.3%

# Asia range
ASIA_RANGE_MAX = 0.02  # 2.0%

# Period extreme proximity tiers
PERIOD_EXTREME_T1 = 0.003  # ≤ 0.3% → score 1.0
PERIOD_EXTREME_T2 = 0.01  # ≤ 1.0% → score 0.7
PERIOD_EXTREME_T3 = 0.02  # ≤ 2.0% → score 0.3

# EMA
EMA_TOUCH_THRESHOLD = 0.001  # 0.1%

# Boardroom (Phase 3+)
BOARDROOM_RANGE = 0.005  # 0.5% over ≥ 20 H1 bars

# Level completion
LEVEL_COMPLETION_RATIO = 0.90  # R3/D3 must be ≥ 90% of R2/D2

# Flat EMA no-trade gate
FLAT_EMA_SEPARATION = 0.003  # 0.3% on H1

# MM candle body ratio
MM_CANDLE_BODY_RATIO = 0.70  # body ≥ 70% of range
