# Signal Engine — Phase 1 Implementation Notes

## What's Implemented

- **SwingDetector** (§3): N-bar swing detection with edge case handling
  - Equal swing merging (within 0.05%)
  - Inside bar skipping
  - Outside bars evaluated normally

- **LevelCounter** (§4.2-4.3): Rise/drop counting with R3/D3 magnitude validation
  - Tracks R1→R2→R3 and D1→D2→D3 sequentially
  - R3/D3 must be ≥ 90% of R2/D2 magnitude (configurable)
  - Phase 1 uses simplified validation (no EMA/volume checks yet)

- **HTFAnalyzer** (§4.4-4.6): Phase classification and MTF alignment
  - Phase detection: aligned/consolidating/exhaustion/neutral
  - MTF alignment scoring per §4.5 table
  - Dual-mechanism reconciliation with htf_modifier output

- **SessionAnalyzer** (§8): Session timing and scoring
  - Session definitions with kill zones and overlaps
  - Phase scoring (opening/mid/closing)
  - Weekly modifiers per §8.3
  - NY open manipulation detection per §8.5
  - Asia control detection (tight range + consolidating)

- **BacktestBridge**: Wire signal engine into existing backtest
  - `run(df)` → List[Signal] for batch processing
  - `get_signals_for_bar(df, idx)` → Optional[Signal] for per-bar queries
  - Works with the existing Bar-based backtest engine

## Engineering Decisions

1. **Level counting approach**: Levels are counted by tracking sequential new
   highs (rises) and new lows (drops). A rise requires a swing low followed
   by a swing high at a new price extreme. This is a simplification of the
   full spec which requires EMA confirmation — EMA checks will be added in
   Phase 2+ when pattern detection is implemented.

2. **R3/D3 magnitude validation**: The R3/D2 magnitude check compares against
   the most recent R2/D2, not the largest. This handles reset patterns where
   a fresh R1-R2-R3 sequence starts after a trend reversal.

3. **Session overlap priority**: When multiple sessions are active, overlaps
   are reported as a distinct session name (e.g., "LONDON_NY") to give them
   higher priority in scoring.

4. **BacktestBridge direction logic**: At rise levels, expects long on
   pullbacks (price below level). At drop levels, expects short on bounces
   (price above level). R3/D3 reverse this (exhaustion zones).

5. **Thresholds centralized**: All proximity thresholds are in `thresholds.py`
   as the single source of truth per §1.2.

## Stubs (Phase 2+)

- PatternDetector: M/W 11-point, SVC, traps, Asia liquidity grab
- GateValidator: All gate requirements
- ConfluenceScorer: Weight tables and confidence calculation
- StopTargetCalculator: Priority-ordered stop/target rules
