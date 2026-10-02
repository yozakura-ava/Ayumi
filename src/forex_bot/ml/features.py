import numpy as np
import pandas as pd
from backtest.engine import TradeDirection
from backtest.ict_smc.models import ConfluenceSignal

BIAS_ENCODING = {
    TradeDirection.LONG: 1,
    TradeDirection.NEUTRAL: 0,
    TradeDirection.SHORT: -1,
}

ICT_FEATURE_NAMES = [
    "ict_confluence_score",
    "ict_structure_score",
    "ict_ob_score",
    "ict_fvg_score",
    "ict_liq_sweep_score",
    "ict_pd_zone_score",
    "ict_session_score",
    "ict_bias_encoded",
    "ict_confluence_count",
    "ict_risk_reward",
]


def build_ict_features(signals: list[ConfluenceSignal], target_index: pd.DatetimeIndex) -> pd.DataFrame:
    if not signals:
        return pd.DataFrame(
            {name: np.nan for name in ICT_FEATURE_NAMES},
            index=target_index,
        )

    signal_map = {s.signal_time: s for s in signals}
    rows = []
    for ts in target_index:
        signal = signal_map.get(ts)
        if signal is not None:
            rows.append(
                {
                    "ict_confluence_score": signal.confidence_score,
                    "ict_structure_score": signal.structure_score,
                    "ict_ob_score": signal.ob_score,
                    "ict_fvg_score": signal.fvg_score,
                    "ict_liq_sweep_score": signal.liq_sweep_score,
                    "ict_pd_zone_score": signal.pd_zone_score,
                    "ict_session_score": signal.session_score,
                    "ict_bias_encoded": BIAS_ENCODING.get(signal.direction, 0),
                    "ict_confluence_count": signal.confluence_count,
                    "ict_risk_reward": signal.risk_reward_ratio,
                }
            )
        else:
            rows.append({name: np.nan for name in ICT_FEATURE_NAMES})

    return pd.DataFrame(rows, index=target_index)


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["Date"])
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.sort_values("date").reset_index(drop=True)
    return df


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = sma(close, period)
    std = close.rolling(window=period, min_periods=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def roc(close: pd.Series, period: int = 12) -> pd.Series:
    return (close - close.shift(period)) / close.shift(period) * 100


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[pd.Series, pd.Series]:
    lowest_low = low.rolling(window=k_period, min_periods=k_period).min()
    highest_high = high.rolling(window=k_period, min_periods=k_period).max()
    pct_k = 100 * (close - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    pct_d = pct_k.rolling(window=d_period, min_periods=d_period).mean()
    return pct_k, pct_d


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal_period: int = 9):
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal_period)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def session_features(dates: pd.Series) -> pd.DataFrame:
    hours = dates.dt.hour
    day_of_week = dates.dt.dayofweek

    killzone_london = ((hours >= 7) & (hours < 12)).astype(int)
    killzone_ny = ((hours >= 12) & (hours < 17)).astype(int)
    killzone_asia = ((hours >= 0) & (hours < 4)).astype(int)
    outside_session = ((hours >= 20) | (hours < 0)).astype(int)

    return pd.DataFrame(
        {
            "hour": hours,
            "day_of_week": day_of_week,
            "killzone_london": killzone_london,
            "killzone_ny": killzone_ny,
            "killzone_asia": killzone_asia,
            "outside_session": outside_session,
        }
    )


def keltner_channels(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    ema_period: int = 20,
    atr_period: int = 10,
    atr_mult: float = 1.5,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = ema(close, ema_period)
    atr_val = atr(high, low, close, atr_period)
    upper = mid + atr_mult * atr_val
    lower = mid - atr_mult * atr_val
    return upper, mid, lower


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    typical = (high + low + close) / 3
    prev_typical = typical.shift(1)
    raw_money_flow = typical * volume
    positive_flow = raw_money_flow.where(typical > prev_typical, 0.0)
    negative_flow = raw_money_flow.where(typical < prev_typical, 0.0)
    pos_sum = positive_flow.rolling(window=period, min_periods=period).sum()
    neg_sum = negative_flow.rolling(window=period, min_periods=period).sum()
    mfr = pos_sum / neg_sum.replace(0, np.nan)
    return 100 - (100 / (1 + mfr))


def vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    dates: pd.Series,
) -> pd.Series:
    typical = (high + low + close) / 3
    cum_tp_vol = (typical * volume).groupby(dates.dt.date).cumsum()
    cum_vol = volume.groupby(dates.dt.date).cumsum()
    return cum_tp_vol / cum_vol.replace(0, np.nan)


def volatility_percentile(atr_series: pd.Series, lookback: int = 50) -> pd.Series:
    rolling_min = atr_series.rolling(window=lookback, min_periods=lookback).min()
    rolling_max = atr_series.rolling(window=lookback, min_periods=lookback).max()
    rng = rolling_max - rolling_min
    return (atr_series - rolling_min) / rng.replace(0, np.nan)


def trend_alignment(close: pd.Series, fast_period: int = 9, slow_period: int = 21) -> pd.Series:
    fast_sma = sma(close, fast_period)
    slow_sma = sma(close, slow_period)
    direction = np.where(close > fast_sma, 1, -1) * np.where(fast_sma > slow_sma, 1, -1)
    return pd.Series(direction, index=close.index)


def higher_highs(high: pd.Series, lookback: int = 5) -> pd.Series:
    rolling_max = high.rolling(window=lookback, min_periods=lookback).max()
    prev_max = rolling_max.shift(1)
    return (rolling_max > prev_max).astype(int)


def lower_lows(low: pd.Series, lookback: int = 5) -> pd.Series:
    rolling_min = low.rolling(window=lookback, min_periods=lookback).min()
    prev_min = rolling_min.shift(1)
    return (rolling_min < prev_min).astype(int)


def engulfing_bullish(open_: pd.Series, close: pd.Series, prev_open: pd.Series, prev_close: pd.Series) -> pd.Series:
    prev_bearish = prev_close < prev_open
    current_bullish = close > open_
    body_engulfs = (close > prev_open) & (open_ < prev_close)
    return (prev_bearish & current_bullish & body_engulfs).astype(int)


def engulfing_bearish(open_: pd.Series, close: pd.Series, prev_open: pd.Series, prev_close: pd.Series) -> pd.Series:
    prev_bullish = prev_close > prev_open
    current_bearish = close < open_
    body_engulfs = (close < prev_open) & (open_ > prev_close)
    return (prev_bullish & current_bearish & body_engulfs).astype(int)


def pin_bar_bullish(high: pd.Series, low: pd.Series, close: pd.Series, open_: pd.Series) -> pd.Series:
    total_range = high - low
    bullish = close > open_
    lower_wick = np.where(bullish, close - low, open_ - low)
    upper_wick = np.where(bullish, high - close, high - open_)
    body = (close - open_).abs()
    long_lower = lower_wick > total_range * 0.6
    small_upper = upper_wick < total_range * 0.1
    small_body = body < total_range * 0.3
    return (long_lower & small_upper & small_body).astype(int)


def pin_bar_bearish(high: pd.Series, low: pd.Series, close: pd.Series, open_: pd.Series) -> pd.Series:
    total_range = high - low
    bullish = close > open_
    upper_wick = np.where(bullish, high - close, high - open_)
    lower_wick = np.where(bullish, close - low, open_ - low)
    body = (close - open_).abs()
    long_upper = upper_wick > total_range * 0.6
    small_lower = lower_wick < total_range * 0.1
    small_body = body < total_range * 0.3
    return (long_upper & small_lower & small_body).astype(int)


def build_feature_matrix(df: pd.DataFrame, signals: list[ConfluenceSignal] | None = None) -> pd.DataFrame:
    close = df["close"]
    high = df["high"]
    low = df["low"]
    open_ = df["open"]

    atr_14 = atr(high, low, close, 14)
    atr_50 = atr(high, low, close, 50)
    rsi_14 = rsi(close, 14)
    sma_9 = sma(close, 9)
    sma_21 = sma(close, 21)
    sma_50 = sma(close, 50)
    ema_200 = ema(close, 200)
    bb_upper, bb_mid, bb_lower = bollinger_bands(close, 20, 2.0)
    roc_12 = roc(close, 12)
    stoch_k, stoch_d = stochastic(high, low, close)
    macd_line, macd_signal, macd_hist = macd(close)
    vol_pct = volatility_percentile(atr_14, 50)
    trend_dir = trend_alignment(close, 9, 21)
    hh = higher_highs(high, 5)
    ll = lower_lows(low, 5)

    bb_pct_b = (close - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)
    bb_width = (bb_upper - bb_lower) / bb_mid.replace(0, np.nan)

    price_vs_sma9 = (close - sma_9) / sma_9.replace(0, np.nan)
    price_vs_sma21 = (close - sma_21) / sma_21.replace(0, np.nan)
    price_vs_sma50 = (close - sma_50) / sma_50.replace(0, np.nan)
    price_vs_ema200 = (close - ema_200) / ema_200.replace(0, np.nan)

    atr_ratio = atr_14 / atr_50.replace(0, np.nan)

    prev_open = open_.shift(1)
    prev_close = close.shift(1)

    features = pd.DataFrame(
        {
            "atr_14": atr_14,
            "atr_50": atr_50,
            "atr_ratio": atr_ratio,
            "vol_pct": vol_pct,
            "rsi": rsi_14,
            "roc": roc_12,
            "stoch_k": stoch_k,
            "stoch_d": stoch_d,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "bb_pct_b": bb_pct_b,
            "bb_width": bb_width,
            "price_vs_sma9": price_vs_sma9,
            "price_vs_sma21": price_vs_sma21,
            "price_vs_sma50": price_vs_sma50,
            "price_vs_ema200": price_vs_ema200,
            "trend_direction": trend_dir,
            "higher_highs": hh,
            "lower_lows": ll,
            "engulfing_bullish": engulfing_bullish(open_, close, prev_open, prev_close),
            "engulfing_bearish": engulfing_bearish(open_, close, prev_open, prev_close),
            "pin_bullish": pin_bar_bullish(high, low, close, open_),
            "pin_bearish": pin_bar_bearish(high, low, close, open_),
        },
        index=df.index,
    )

    sess = session_features(df["date"])
    features = pd.concat([features, sess], axis=1)

    if signals is not None and len(signals) > 0:
        date_series = df["date"]
        signal_map = {s.signal_time: s for s in signals}
        rows = []
        for i, ts in enumerate(date_series):  # noqa: B007
            signal = signal_map.get(ts)
            if signal is not None:
                rows.append(
                    {
                        "ict_confluence_score": signal.confidence_score,
                        "ict_structure_score": signal.structure_score,
                        "ict_ob_score": signal.ob_score,
                        "ict_fvg_score": signal.fvg_score,
                        "ict_liq_sweep_score": signal.liq_sweep_score,
                        "ict_pd_zone_score": signal.pd_zone_score,
                        "ict_session_score": signal.session_score,
                        "ict_bias_encoded": BIAS_ENCODING.get(signal.direction, 0),
                        "ict_confluence_count": signal.confluence_count,
                        "ict_risk_reward": signal.risk_reward_ratio,
                    }
                )
            else:
                rows.append({name: np.nan for name in ICT_FEATURE_NAMES})
        ict_features = pd.DataFrame(rows, index=df.index)
        features = pd.concat([features, ict_features], axis=1)

    return features


def add_multi_timeframe_features(features: pd.DataFrame, h4_df: pd.DataFrame, d1_df: pd.DataFrame) -> pd.DataFrame:
    h4_close = h4_df["close"]
    d1_close = d1_df["close"]

    h4_sma = sma(h4_close, 21)
    h4_features = pd.DataFrame(
        {
            "h4_trend": np.where(h4_close > h4_sma, 1, -1),
            "h4_sma21_dist": (h4_close - h4_sma) / h4_sma.replace(0, np.nan),
        },
        index=h4_df.index,
    )

    d1_sma = sma(d1_close, 50)
    d1_ema = ema(d1_close, 200)
    d1_features = pd.DataFrame(
        {
            "d1_trend": np.where(d1_close > d1_sma, 1, -1),
            "d1_ema200_dist": (d1_close - d1_ema) / d1_ema.replace(0, np.nan),
        },
        index=d1_df.index,
    )

    h4_aligned = h4_features.reindex(features.index, method="ffill")
    d1_aligned = d1_features.reindex(features.index, method="ffill")

    features = pd.concat([features, h4_aligned, d1_aligned], axis=1)

    features["tf_alignment"] = (features["trend_direction"] == features["h4_trend"]).astype(int) + (
        features["trend_direction"] == features["d1_trend"]
    ).astype(int)

    return features
