# Sweep #3 — crypto-native candidates on real Binance.US bars

**Card:** 68fb28f5-9135-4157-92d1-82c8380e03bd
**Started:** 2026-10-06T20:19:33.622382+00:00
**Finished:** 2026-10-06T20:30:45.398914+00:00
**Elapsed:** 671.78s
**Build commit:** `eb6ecacb` (long: `eb6ecacbe38842cd66125307c252b1ddd35dd747`)
**Parent commit (main):** `eb6ecacb` (spine baseline: `eb6ecacb`)

## TL;DR

- **Candidates evaluated:** 99 / 99
- **Persisted to factory_verdicts:** 99
- **Survivors (Tier A/B/C):** 0 | **REJECT:** 0 | **INSUFFICIENT_DATA:** 99
- **BH-FDR discoveries (q<0.05):** 0 (bh_empty=True)

## Per-strategy summary

| Strategy | Variants | Tier A | Tier C | REJECT | INSUFFICIENT_DATA | Total trades | Mean Sharpe (avg/max) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `crypto_ema_cross_trend` | 54 | 0 | 0 | 0 | 54 | 0 | 0.000 / 0.000 |
| `crypto_donchian_breakout` | 18 | 0 | 0 | 0 | 18 | 0 | 0.000 / 0.000 |
| `crypto_zscore_mean_reversion` | 27 | 0 | 0 | 0 | 27 | 0 | 0.000 / 0.000 |

## Verdict table — survivors + casualties

| Pair | Strategy | variant hash | Tier | Trades | Mean Sharpe | Mean PF | Max DD | q-value | bh_rejected | reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSDT | `crypto_ema_cross_trend` | `7332fdbaa5` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `6c7e31d62a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `6ec7791992` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `e9cf143e7a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `d8a55a8105` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `e87d90ec72` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `cc601cc253` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `f7de08428b` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `ba1ba98488` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `6a4f822d48` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `2cee49009a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `e10871a0d6` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `e49c89f2b9` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `0a971ffc63` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `aaa0c36615` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `69537f6bab` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `7403d593d5` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_ema_cross_trend` | `36ddcb83f7` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_donchian_breakout` | `166d8fba13` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_donchian_breakout` | `4d7e45a91e` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_donchian_breakout` | `4509f71065` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_donchian_breakout` | `4037c6714b` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_donchian_breakout` | `600640c785` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_donchian_breakout` | `03ba84fbab` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `defd534fc1` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `cebe66ed78` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `4b8309d265` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `bba5d9fb9f` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `77ec06b7df` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `ad7d453edc` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `d7b20559f2` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `751b07bbc4` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| BTCUSDT | `crypto_zscore_mean_reversion` | `a88c0e799f` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `7332fdbaa5` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `6c7e31d62a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `6ec7791992` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `e9cf143e7a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `d8a55a8105` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `e87d90ec72` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `cc601cc253` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `f7de08428b` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `ba1ba98488` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `6a4f822d48` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `2cee49009a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `e10871a0d6` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `e49c89f2b9` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `0a971ffc63` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `aaa0c36615` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `69537f6bab` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `7403d593d5` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_ema_cross_trend` | `36ddcb83f7` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_donchian_breakout` | `166d8fba13` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_donchian_breakout` | `4d7e45a91e` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_donchian_breakout` | `4509f71065` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_donchian_breakout` | `4037c6714b` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_donchian_breakout` | `600640c785` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_donchian_breakout` | `03ba84fbab` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `defd534fc1` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `cebe66ed78` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `4b8309d265` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `bba5d9fb9f` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `77ec06b7df` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `ad7d453edc` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `d7b20559f2` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `751b07bbc4` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| ETHUSDT | `crypto_zscore_mean_reversion` | `a88c0e799f` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `7332fdbaa5` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `6c7e31d62a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `6ec7791992` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `e9cf143e7a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `d8a55a8105` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `e87d90ec72` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `cc601cc253` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `f7de08428b` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `ba1ba98488` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `6a4f822d48` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `2cee49009a` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `e10871a0d6` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `e49c89f2b9` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `0a971ffc63` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `aaa0c36615` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `69537f6bab` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `7403d593d5` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_ema_cross_trend` | `36ddcb83f7` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_donchian_breakout` | `166d8fba13` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_donchian_breakout` | `4d7e45a91e` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_donchian_breakout` | `4509f71065` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_donchian_breakout` | `4037c6714b` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_donchian_breakout` | `600640c785` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_donchian_breakout` | `03ba84fbab` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `defd534fc1` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `cebe66ed78` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `4b8309d265` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `bba5d9fb9f` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `77ec06b7df` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `ad7d453edc` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `d7b20559f2` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `751b07bbc4` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |
| SOLUSDT | `crypto_zscore_mean_reversion` | `a88c0e799f` | **INSUFFICIENT_DATA** | 0 | 0.000 | 0.000 | 0.000 | null | False | only 0 trades (< 10 Liora threshold) |

## Integrity gate

| Pair | Status | n_violations | Error |
| --- | --- | --- | --- |
| BTCUSDT | PASS | 0 | - |
| ETHUSDT | PASS | 0 | - |
| SOLUSDT | PASS | 0 | - |

## Provenance

| Pair | n_bars | earliest | latest | data_hash (sha256, prefix) |
| --- | --- | --- | --- | --- |
| BTCUSDT | 871 | 2026-08-31T13:00:00+00:00 | 2026-10-06T19:00:00+00:00 | `a3bafbe8b87d482b…` |
| ETHUSDT | 871 | 2026-08-31T13:00:00+00:00 | 2026-10-06T19:00:00+00:00 | `629621f1fbf4dd63…` |
| SOLUSDT | 871 | 2026-08-31T13:00:00+00:00 | 2026-10-06T19:00:00+00:00 | `df723b2f3707ccc3…` |

## git_commit convention (review note #2)

BUILD worktree short SHA — the commit the sweep RAN UNDER. Rationale: the crypto-native template registry (forex_bot/strategies/crypto_native.py) and this sweep driver land on this branch; verdicts are not reproducible from the parent commit alone. The parent_commit_short field below records the prior main SHA (eb6ecacb at the time of dispatch) for traceability, but factory_verdicts.git_commit holds the BUILD SHA only.

## Honesty notes

- Bars are LIVE (Binance.US /api/v3/klines direct egress; persisted by the prior card 0ab49707 sweep). Source + window + retrieval timestamp + SHA-256 captured per pair in provenance_by_pair.
- Sample size is small: 871 bars / pair (~37 days H1 post-trim). A 33-variant × 3-pair candidate matrix on this short window is more a smoke test of the spine than a statistical seal. If BH-FDR finds 0 discoveries that IS the result; we do NOT loosen the gate.
- Validation runner uses a placeholder trial-return derivation (deterministic from bars+params); the per-strategy strategy classes are bridge-verified (one-shot build) but the runner does not exercise StrategySignal.evaluate on each bar. Walk-forward metrics are the runner's honest outputs, not live P&L.
- The git_commit recorded in factory_verdicts.git_commit is the BUILD short SHA (this commit), per the documented convention. The parent commit (main @ eb6ecacb) is recorded in metadata.parent_commit_short for traceability only.
- factory_verdicts.data_hash is now populated PER ROW from data_hash_by_pair (one SHA-256 per symbol). The earlier real-data sweep left this column NULL.
