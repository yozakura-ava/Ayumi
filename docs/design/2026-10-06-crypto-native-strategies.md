# Crypto-Native Strategy Templates — Design Note

**Card:** fe773687-4c5c-48ae-bd9b-03be61d98ba7 · **Date:** 2026-10-06 · **Branch:** `reina/fe773687-crypto-native-strategies`

## Why

The 2026-10-06 real-data sweep (`docs/reports/2026-10-06-crypto-real-data-sweep.md`)
ran 18 FX-paired registry candidates on 871 real Binance.US H1 bars per pair and got
**18 × INSUFFICIENT_DATA — zero trades**. Root causes: (1) pair configs list only FX
symbols, so `get_for_symbol("BTCUSDT")` never binds; (2) thresholds are FX-pip
calibrated (`srmr_plus._resolve_pip_size` raises without a symbol; session gates,
15-80 pip range filters never fire on crypto bars); (3) confidence/threshold sizing
is tuned to FX ATR (~0.05% of price), not crypto H1 ATR (~0.5-1%).

## Candidate set

Three first-principles templates in `src/forex_bot/strategies/crypto_native.py`,
chosen to span the three canonical regime families so the BH-FDR gate sees
genuinely different hypotheses, not three flavors of one:

| strategy_id | family | core rule (all thresholds ATR/σ-relative) |
|---|---|---|
| `crypto_ema_cross_trend` | trend | EMA fast/slow cross, gated by slow-EMA slope ≥ 0.15 × ATR over the fast window |
| `crypto_donchian_breakout` | breakout | close breaks 96-bar high/low ± 0.5 × ATR buffer (anti stop-run) |
| `crypto_zscore_mean_reversion` | mean reversion | fade closes with \|z\| ≥ 2.5 vs 96-bar rolling mean/σ |

All three: 24/7 (no session filters), no pip units anywhere (test-enforced),
ATR-scaled SL (2.5-3.0×) with a 1.5R/2.5R/4R TP ladder, bar-count cooldowns to
keep per-trade cost drag (Sprint C funding/venue overlays price each turn)
proportional to edge, not to churn.

## Declared parameter grids (UP FRONT — no post-hoc tuning)

Frozen in `CRYPTO_PARAM_GRIDS`; `build_crypto_native_strategy` **rejects both
unknown keys and off-grid values** (`ValueError`), so the no-cherry-picking
contract is enforced at construction time:

- `crypto_ema_cross_trend`: fast ∈ {16, 24, 32} × slow ∈ {72, 96, 120} × slope_gate_atr ∈ {0.10, 0.15}, SL 2.5×ATR
- `crypto_donchian_breakout`: entry_lookback ∈ {72, 96, 120} × exit_lookback ∈ {36, 48}, buffer 0.5×ATR, SL 3.0×ATR
- `crypto_zscore_mean_reversion`: lookback ∈ {72, 96, 120} × z_entry ∈ {2.0, 2.5, 3.0}, SL 2.5×ATR

Defaults (grid centers): EMA 24/96, Donchian 96/48, z 96/2.5. The sweep wires
these via identity build (empty params) — the grid exists so any future Optuna
pass samples only these declared cells; the BH-FDR gate on real bars is the judge.

## Production wiring (unchanged sweep path)

- `strategies/registry.py`: three new `StrategyConfig` entries, symbols
  `[BTCUSDT, ETHUSDT, SOLUSDT]`, timeframe H1, active.
- `factory/bridge.py`: three `REGISTRY_STRATEGY_BUILDERS` +
  `_LAZY_STRATEGY_CLASSES` entries (zero-arg constructors, standard lazy path).
- The sweep's `_build_template_for_strategy` / `_build_strategy_template` /
  `build_strategy_from_template` path picks them up unchanged — no sweep edits.

## Verification

`tests/strategies/test_crypto_native.py` (29 tests): signal emission on synthetic
crypto-shaped bars at BTC and SOL price scales, flat-bar silence, pair-config
binding (BTC/ETH/SOL yes, EURUSD no), registry round-trip through
`RegistryStrategyTemplate` + `build_strategy_from_template` for all three pairs,
grid guard (keys and values), ATR-ladder monotonicity, pip-machinery-free source
guard, and **nonzero signal emission on the persisted real Binance.US bars**
(`data/sweep_real_data_bars/real_crypto_bars_h1.csv`) for all three templates with
churn bound (< 1 signal / 10 bars). Adjacent factory suites
(`test_bridge.py`, `test_registry_template.py`) re-run green: 43/43.
