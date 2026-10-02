# Volatility Squeeze Breakout Strategy

**Issue:** AYUAA-356 | **Status:** spec_complete | **Source:** Paperclip

## Objective

Implement a volatility squeeze breakout strategy using Bollinger Band / Keltner Channel squeeze detection with expected Profit Factor of 1.6-2.5 and win rate >55%.

## Background

The volatility squeeze (Bollinger Band + Keltner Channel) is a well-established mean-reversion/breakout methodology. When Bollinger Bands contract inside Keltner Channels (squeeze), it indicates low volatility that typically precedes a strong breakout. This complements existing momentum and mean reversion strategies in the pipeline.

## Strategy Logic

### Squeeze Detection

A squeeze occurs when Bollinger Band width is less than Keltner Channel ATR:

```
BB_width = BB_upper - BB_lower
KC_width = Keltner_upper - Keltner_lower = 2 * ATR_keltner

Squeeze = BB_width < KC_width
```

Or equivalently, when BB %B < 0.05 and ATR is below its EMA.

### Squeeze Qualification

Before trading a squeeze, qualify it with:
1. **Minimum squeeze duration**: Squeeze must persist for 2-5 bars before breakout (avoid false breakouts)
2. **Trend confirmation**: Use 20 EMA or ADX to confirm trend direction
3. **Session filter**: Prefer London/NY sessions for GBPJPY, EURUSD; flexible for XAUUSD

### Entry Rules

**Long Entry:**
1. Squeeze is active (BB inside KC)
2. Price closes above Keltner upper band
3. Price also closes above 20 EMA (trend alignment)
4. ADX > 20 (minimum trend strength)
5. Confirmation: 1-2 candle close above Keltner band

**Short Entry:**
1. Squeeze is active (BB inside KC)
2. Price closes below Keltner lower band
3. Price also closes below 20 EMA (trend alignment)
4. ADX > 20 (minimum trend strength)
5. Confirmation: 1-2 candle close below Keltner band

### Exit Rules

**Stop Loss:**
- Long: Below Keltner lower band or 1.5x ATR from entry
- Short: Above Keltner upper band or 1.5x ATR from entry

**Take Profit (multi-exit scaling):**
- TP1: 1R (breakeven move)
- TP2: 2R (partial exit 50%)
- TP3: 3R (close remaining)

**Alternative TP:** Use Bollinger Band middle line (SMA) as TP1 target

### Confidence Scoring

Base confidence 0.60, modified by:
- Squeeze duration bonus: +0.05 for each additional squeeze bar (max +0.15)
- ADX strength bonus: +0.10 if ADX > 30
- Volume confirmation (if available): +0.05

## Parameters

| Parameter | Default | Range | Description |
|-----------|--------|-------|-------------|
| `bb_period` | 20 | 10-50 | Bollinger Band period |
| `bb_std_dev` | 2.0 | 1.5-3.0 | Bollinger Band standard deviations |
| `kc_period` | 20 | 10-50 | Keltner Channel period |
| `kc_atr_multiplier` | 2.0 | 1.5-3.0 | Keltner Channel ATR multiplier |
| `squeeze_threshold` | 0.0 | 0.0-1.0 | BB width vs KC width ratio threshold |
| `min_squeeze_bars` | 2 | 1-5 | Minimum bars in squeeze before entry |
| `ema_period` | 20 | 10-50 | Trend EMA period |
| `adx_period` | 14 | 10-20 | ADX period |
| `adx_min` | 20.0 | 15-30 | Minimum ADX for entry |
| `atr_period` | 14 | 10-20 | ATR period for SL calculation |
| `atr_sl_multiplier` | 1.5 | 1.0-2.5 | ATR multiplier for stop loss |
| `tp1_rr` | 1.0 | 0.5-2.0 | TP1 risk reward ratio |
| `tp2_rr` | 2.0 | 1.5-3.0 | TP2 risk reward ratio |
| `tp3_rr` | 3.0 | 2.0-4.0 | TP3 risk reward ratio |
| `session_filter` | True | bool | Enable London/NY session filter |
| `min_confidence` | 0.55 | 0.4-0.7 | Minimum confidence threshold |

## Presets

### GBPJPY H1 (Primary)
```python
VolatilitySqueezeConfig(
    bb_period=20, bb_std_dev=2.0,
    kc_period=20, kc_atr_multiplier=2.0,
    squeeze_threshold=0.0, min_squeeze_bars=3,
    ema_period=20, adx_period=14, adx_min=20,
    atr_period=14, atr_sl_multiplier=1.5,
    tp1_rr=1.0, tp2_rr=2.0, tp3_rr=3.0,
    session_filter=True
)
```

### EURUSD M15 / H1
```python
VolatilitySqueezeConfig(
    bb_period=20, bb_std_dev=2.0,
    kc_period=20, kc_atr_multiplier=2.0,
    squeeze_threshold=0.0, min_squeeze_bars=2,
    ema_period=20, adx_period=14, adx_min=18,
    atr_period=14, atr_sl_multiplier=1.5,
    tp1_rr=1.0, tp2_rr=2.0, tp3_rr=3.0,
    session_filter=True
)
```

### XAUUSD H1
```python
VolatilitySqueezeConfig(
    bb_period=20, bb_std_dev=2.5,  # Wider BB for gold volatility
    kc_period=20, kc_atr_multiplier=2.5,
    squeeze_threshold=0.0, min_squeeze_bars=2,
    ema_period=20, adx_period=14, adx_min=20,
    atr_period=14, atr_sl_multiplier=2.0,  # Wider SL for gold
    tp1_rr=1.0, tp2_rr=2.0, tp3_rr=3.0,
    session_filter=False  # Gold trades longer sessions
)
```

## File Structure

```
src/forex_bot/
  strategies/
    volatility_squeeze.py    # New strategy implementation
  backtest/
    strategies.py            # Register VolatilitySqueezeStrategy
tests/
  strategies/
    test_volatility_squeeze.py  # Unit tests
```

## Implementation Notes

1. **Keltner Channel calculation**: Use EMA of close for middle band, ATR for upper/lower bands
2. **Squeeze state tracking**: Maintain squeeze_start_bar index to measure squeeze duration
3. **Confluence potential**: Design signal interface to accept optional confluence from order blocks, FVGs
4. **Session filtering**: Reuse existing `_PREFERRED_SESSIONS` from momentum.py

## Success Criteria

- **Win rate**: >55%
- **Profit Factor**: >1.6 (target 1.8-2.0)
- **Walk-forward**: Must pass 80/20 walk-forward validation
- **Correlation**: Low correlation (<0.3) with existing momentum strategies
- **Sharpe ratio**: >0.8 in walk-forward

## Dependencies

- [AYUAA-36](/AYUAA/issues/AYUAA-36) (Order Block Detector) — optional confluence
- [AYUAA-37](/AYUAA/issues/AYUAA-37) (FVG Detector) — optional confluence
- Existing ATR, ADX, EMA calculations in engine.py and momentum.py

## TODO

- [ ] Create `volatility_squeeze.py` in `strategies/` directory
- [ ] Implement `VolatilitySqueezeConfig` dataclass
- [ ] Implement `VolatilitySqueezeStrategy` class
- [ ] Add presets for GBPJPY, EURUSD, XAUUSD
- [ ] Write unit tests (min 20 test cases)
- [ ] Register in `backtest/strategies.py`
- [ ] Run backtest validation
- [ ] Walk-forward validation
- [ ] Correlation analysis with existing strategies