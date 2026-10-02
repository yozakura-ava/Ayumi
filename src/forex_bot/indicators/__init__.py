from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd


def _to_series(values: Sequence[float], name: str | None = None) -> pd.Series:
    if isinstance(values, pd.Series):
        return values
    return pd.Series(values, dtype=float, name=name)


def sma(values: Sequence[float], period: int) -> pd.Series:
    s = _to_series(values)
    return s.rolling(window=period, min_periods=period).mean()


def ema(values: Sequence[float], period: int) -> pd.Series:
    s = _to_series(values)
    return s.ewm(span=period, adjust=False).mean()


def std(values: Sequence[float], period: int) -> pd.Series:
    s = _to_series(values)
    return s.rolling(window=period, min_periods=period).std(ddof=0)


def rsi(closes: Sequence[float], period: int = 14) -> pd.Series:
    s = _to_series(closes)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> pd.Series:
    h = _to_series(highs)
    l = _to_series(lows)  # noqa: E741
    c = _to_series(closes)
    prev_close = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_close).abs(), (l - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> pd.Series:
    h = _to_series(highs)
    l = _to_series(lows)  # noqa: E741
    c = _to_series(closes)
    n = len(c)

    if n < 2 * period + 1:
        return pd.Series(np.nan, index=c.index, dtype=float)

    tr_list: list[float] = []
    plus_dm_list: list[float] = []
    minus_dm_list: list[float] = []
    for i in range(1, n):
        tr_val = max(
            h.iloc[i] - l.iloc[i],
            abs(h.iloc[i] - h.iloc[i - 1]),
            abs(l.iloc[i] - l.iloc[i - 1]),
        )
        up = h.iloc[i] - h.iloc[i - 1]
        down = l.iloc[i - 1] - l.iloc[i]
        plus_dm = up if up > down and up > 0 else 0.0
        minus_dm = down if down > up and down > 0 else 0.0
        tr_list.append(tr_val)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    smoothed_tr = sum(tr_list[:period])
    smoothed_plus_dm = sum(plus_dm_list[:period])
    smoothed_minus_dm = sum(minus_dm_list[:period])

    dx_values: list[float] = []
    for i in range(period, len(tr_list)):
        smoothed_tr = smoothed_tr - smoothed_tr / period + tr_list[i]
        smoothed_plus_dm = smoothed_plus_dm - smoothed_plus_dm / period + plus_dm_list[i]
        smoothed_minus_dm = smoothed_minus_dm - smoothed_minus_dm / period + minus_dm_list[i]

        if smoothed_tr == 0:
            dx_values.append(0.0)
            continue

        plus_di = 100.0 * smoothed_plus_dm / smoothed_tr
        minus_di = 100.0 * smoothed_minus_dm / smoothed_tr
        di_sum = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum != 0 else 0.0
        dx_values.append(dx)

    if len(dx_values) < period:
        return pd.Series(np.nan, index=c.index, dtype=float)

    adx_val = sum(dx_values[:period]) / period
    result: list[float] = [np.nan] * (2 * period)
    result.append(adx_val)

    for i in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[i]) / period
        result.append(adx_val)

    while len(result) < n:
        result.append(np.nan)

    return pd.Series(result[:n], index=c.index, dtype=float)


def bollinger_bands(
    closes: Sequence[float], period: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    s = _to_series(closes)
    mid = s.rolling(window=period, min_periods=period).mean()
    std_dev = s.rolling(window=period, min_periods=period).std(ddof=0)
    upper = mid + num_std * std_dev
    lower = mid - num_std * std_dev
    return upper, mid, lower


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    s = _to_series(closes)
    fast_ema = s.ewm(span=fast, adjust=False).mean()
    slow_ema = s.ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    h = _to_series(highs)
    l = _to_series(lows)  # noqa: E741
    c = _to_series(closes)
    lowest_low = l.rolling(window=k_period, min_periods=k_period).min()
    highest_high = h.rolling(window=k_period, min_periods=k_period).max()
    pct_k = 100 * (c - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    pct_d = pct_k.rolling(window=d_period, min_periods=d_period).mean()
    return pct_k, pct_d


def roc(closes: Sequence[float], period: int = 12) -> pd.Series:
    s = _to_series(closes)
    prev = s.shift(period)
    return (s - prev) / prev.replace(0, np.nan) * 100


def atr_percentile(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
    lookback: int = 50,
) -> pd.Series:
    atr_series = atr(highs, lows, closes, period)
    rolling_min = atr_series.rolling(window=lookback, min_periods=lookback).min()
    rolling_max = atr_series.rolling(window=lookback, min_periods=lookback).max()
    rng = rolling_max - rolling_min
    return (atr_series - rolling_min) / rng.replace(0, np.nan)


__all__ = [
    "atr",
    "rsi",
    "adx",
    "ema",
    "sma",
    "std",
    "bollinger_bands",
    "macd",
    "stochastic",
    "roc",
    "atr_percentile",
]
