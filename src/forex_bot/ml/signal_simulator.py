import logging
from collections.abc import Callable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

from .features import (
    atr,
    bollinger_bands,
    engulfing_bearish,
    engulfing_bullish,
    roc,
    rsi,
    sma,
)

_SignalFn = Callable[[pd.DataFrame], pd.DataFrame]


def ma_crossover_signals(
    df: pd.DataFrame,
    fast: int = 9,
    slow: int = 21,
    atr_mult: float = 2.0,
    rr: float = 1.5,
) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]

    fast_sma = sma(close, fast)
    slow_sma = sma(close, slow)
    atr_val = atr(high, low, close, 14)

    prev_fast = fast_sma.shift(1)
    prev_slow = slow_sma.shift(1)

    long_signal = (prev_fast <= prev_slow) & (fast_sma > slow_sma)
    short_signal = (prev_fast >= prev_slow) & (fast_sma < slow_sma)

    entries = pd.DataFrame(index=df.index)
    entries["direction"] = 0.0
    entries.loc[long_signal, "direction"] = 1.0
    entries.loc[short_signal, "direction"] = -1.0

    entries["entry_price"] = close
    entries["atr"] = atr_val
    entries["stop_loss"] = np.where(
        entries["direction"] == 1,
        close - atr_val * atr_mult,
        np.where(entries["direction"] == -1, close + atr_val * atr_mult, np.nan),
    )
    entries["take_profit"] = np.where(
        entries["direction"] == 1,
        close + atr_val * atr_mult * rr,
        np.where(entries["direction"] == -1, close - atr_val * atr_mult * rr, np.nan),
    )
    entries["strategy"] = "ma_crossover"

    return entries[entries["direction"] != 0].copy()


def rsi_divergence_signals(
    df: pd.DataFrame,
    period: int = 14,
    oversold: float = 30,
    overbought: float = 70,
    atr_mult: float = 2.0,
    rr: float = 1.5,
) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]

    rsi_val = rsi(close, period)
    atr_val = atr(high, low, close, 14)

    rsi_low = rsi_val.rolling(window=20, min_periods=20).min()
    rsi_high = rsi_val.rolling(window=20, min_periods=20).max()

    bullish_div = (
        (close.rolling(20).min().shift(1) > close.rolling(20).min())
        & (rsi_low.shift(1) > rsi_low)
        & (rsi_val < oversold)
    )
    bearish_div = (
        (close.rolling(20).max().shift(1) < close.rolling(20).max())
        & (rsi_high.shift(1) < rsi_high)
        & (rsi_val > overbought)
    )

    entries = pd.DataFrame(index=df.index)
    entries["direction"] = 0.0
    entries.loc[bullish_div, "direction"] = 1.0
    entries.loc[bearish_div, "direction"] = -1.0

    entries["entry_price"] = close
    entries["atr"] = atr_val
    entries["stop_loss"] = np.where(
        entries["direction"] == 1,
        close - atr_val * atr_mult,
        np.where(entries["direction"] == -1, close + atr_val * atr_mult, np.nan),
    )
    entries["take_profit"] = np.where(
        entries["direction"] == 1,
        close + atr_val * atr_mult * rr,
        np.where(entries["direction"] == -1, close - atr_val * atr_mult * rr, np.nan),
    )
    entries["strategy"] = "rsi_divergence"

    return entries[entries["direction"] != 0].copy()


def bb_mean_reversion_signals(
    df: pd.DataFrame,
    period: int = 20,
    num_std: float = 2.0,
    atr_mult: float = 2.0,
    rr: float = 1.5,
) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_ = df["open"]

    bb_upper, bb_mid, bb_lower = bollinger_bands(close, period, num_std)
    atr_val = atr(high, low, close, 14)

    prev_close = close.shift(1)
    prev_open = open_.shift(1)

    long_signal = (close < bb_lower) & engulfing_bullish(open_, close, prev_open, prev_close).astype(bool)
    short_signal = (close > bb_upper) & engulfing_bearish(open_, close, prev_open, prev_close).astype(bool)

    entries = pd.DataFrame(index=df.index)
    entries["direction"] = 0.0
    entries.loc[long_signal, "direction"] = 1.0
    entries.loc[short_signal, "direction"] = -1.0

    entries["entry_price"] = close
    entries["atr"] = atr_val
    entries["stop_loss"] = np.where(
        entries["direction"] == 1,
        close - atr_val * atr_mult,
        np.where(entries["direction"] == -1, close + atr_val * atr_mult, np.nan),
    )
    entries["take_profit"] = np.where(
        entries["direction"] == 1,
        bb_mid,
        np.where(entries["direction"] == -1, bb_mid, np.nan),
    )
    entries["strategy"] = "bb_mean_reversion"

    return entries[entries["direction"] != 0].copy()


def momentum_signals(df: pd.DataFrame, period: int = 12, atr_mult: float = 2.0, rr: float = 1.5) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]

    roc_val = roc(close, period)
    atr_val = atr(high, low, close, 14)

    prev_roc = roc_val.shift(1)

    long_signal = (prev_roc < 0) & (roc_val > 0) & (roc_val > 0.5)
    short_signal = (prev_roc > 0) & (roc_val < 0) & (roc_val < -0.5)

    entries = pd.DataFrame(index=df.index)
    entries["direction"] = 0.0
    entries.loc[long_signal, "direction"] = 1.0
    entries.loc[short_signal, "direction"] = -1.0

    entries["entry_price"] = close
    entries["atr"] = atr_val
    entries["stop_loss"] = np.where(
        entries["direction"] == 1,
        close - atr_val * atr_mult,
        np.where(entries["direction"] == -1, close + atr_val * atr_mult, np.nan),
    )
    entries["take_profit"] = np.where(
        entries["direction"] == 1,
        close + atr_val * atr_mult * rr,
        np.where(entries["direction"] == -1, close - atr_val * atr_mult * rr, np.nan),
    )
    entries["strategy"] = "momentum"

    return entries[entries["direction"] != 0].copy()


def label_trades(signals: pd.DataFrame, df: pd.DataFrame, max_holding_bars: int = 50) -> pd.DataFrame:
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    results = []
    for idx, row in signals.iterrows():
        entry_idx = df.index.get_loc(idx)
        if entry_idx + max_holding_bars >= len(df):
            continue

        direction = row["direction"]
        sl = row["stop_loss"]
        tp = row["take_profit"]
        entry_price = row["entry_price"]

        if np.isnan(sl) or np.isnan(tp) or np.isnan(entry_price):
            continue

        risk = abs(entry_price - sl)
        if risk == 0:
            continue

        exit_price = entry_price
        outcome = 0
        exit_bar = entry_idx
        exit_reason = "time"

        for i in range(entry_idx + 1, min(entry_idx + max_holding_bars, len(df))):
            if direction == 1:
                if low[i] <= sl:
                    exit_price = sl
                    outcome = 0
                    exit_reason = "stop_loss"
                    exit_bar = i
                    break
                if high[i] >= tp:
                    exit_price = tp
                    outcome = 1
                    exit_reason = "take_profit"
                    exit_bar = i
                    break
            else:
                if high[i] >= sl:
                    exit_price = sl
                    outcome = 0
                    exit_reason = "stop_loss"
                    exit_bar = i
                    break
                if low[i] <= tp:
                    exit_price = tp
                    outcome = 1
                    exit_reason = "take_profit"
                    exit_bar = i
                    break
        else:
            exit_bar = min(entry_idx + max_holding_bars - 1, len(df) - 1)
            exit_price = close[exit_bar]
            pnl = (exit_price - entry_price) * direction
            outcome = 1 if pnl > 0 else 0
            exit_reason = "time"

        holding_bars = exit_bar - entry_idx
        pnl = (exit_price - entry_price) * direction
        rr_actual = pnl / risk if risk > 0 else 0

        results.append(
            {
                "entry_idx": entry_idx,
                "entry_time": idx,
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": sl,
                "take_profit": tp,
                "exit_price": exit_price,
                "outcome": outcome,
                "pnl": pnl,
                "rr_actual": rr_actual,
                "holding_bars": holding_bars,
                "exit_reason": exit_reason,
                "strategy": row["strategy"],
            }
        )

    return pd.DataFrame(results)


def generate_all_signals(df: pd.DataFrame) -> pd.DataFrame:
    signal_fns: list[_SignalFn] = [
        ma_crossover_signals,
        rsi_divergence_signals,
        bb_mean_reversion_signals,
        momentum_signals,
    ]

    all_signals = []
    for fn in signal_fns:
        try:
            sigs = fn(df)
            if len(sigs) > 0:
                all_signals.append(sigs)
        except Exception:
            logger.warning("Signal function %s failed, skipping", getattr(fn, "__name__", fn))
            continue

    if not all_signals:
        return pd.DataFrame()

    combined = pd.concat(all_signals)
    combined = combined.sort_index()

    combined = combined[~combined.index.duplicated(keep="first")]

    return combined


def build_labeled_dataset(df: pd.DataFrame, features: pd.DataFrame, max_holding_bars: int = 50) -> pd.DataFrame:
    signals = generate_all_signals(df)
    if signals.empty:
        return pd.DataFrame()

    labeled = label_trades(signals, df, max_holding_bars)
    if labeled.empty:
        return pd.DataFrame()

    entry_indices = features.index[labeled["entry_idx"].values]
    feat_subset = features.loc[entry_indices].copy()
    feat_subset = feat_subset.reset_index(drop=True)
    labeled = labeled.reset_index(drop=True)

    dataset = pd.concat(
        [
            feat_subset,
            labeled[
                [
                    "direction",
                    "entry_price",
                    "stop_loss",
                    "take_profit",
                    "exit_price",
                    "outcome",
                    "pnl",
                    "rr_actual",
                    "holding_bars",
                    "exit_reason",
                    "strategy",
                ]
            ],
        ],
        axis=1,
    )

    dataset = dataset.dropna(subset=["outcome"])
    return dataset
