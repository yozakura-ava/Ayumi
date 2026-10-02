from __future__ import annotations

from typing import TypedDict

import numpy as np
import pandas as pd


class Position(TypedDict):
    symbol: str
    exposure: float


def _log_returns(price_series: pd.Series) -> pd.Series:
    return pd.Series(np.log(price_series / price_series.shift(1)), index=price_series.index)


def rolling_correlation(
    price_series_a: pd.Series,
    price_series_b: pd.Series,
    window: int = 50,
) -> pd.Series:
    returns_a = _log_returns(price_series_a)
    returns_b = _log_returns(price_series_b)
    return returns_a.rolling(window=window).corr(returns_b)


def correlation_matrix(
    price_dict: dict[str, pd.Series],
    window: int = 50,
) -> pd.DataFrame:
    symbols = list(price_dict.keys())
    n = len(symbols)
    corr_matrix = pd.DataFrame(
        np.zeros((n, n)),
        index=symbols,
        columns=symbols,
    )
    for i, sym_a in enumerate(symbols):
        for j, sym_b in enumerate(symbols):
            if i == j:
                corr_matrix.loc[sym_a, sym_b] = 1.0
            else:
                corr = rolling_correlation(price_dict[sym_a], price_dict[sym_b], window)
                corr_matrix.loc[sym_a, sym_b] = corr.iloc[-1] if not corr.empty else np.nan
    return corr_matrix


class CorrelationTracker:
    def __init__(
        self,
        pairs: list[str],
        window: int = 50,
        threshold: float = 0.7,
    ):
        self.pairs = pairs
        self.window = window
        self.threshold = threshold
        self.price_history: dict[str, pd.Series] = {pair: pd.Series([], dtype=float) for pair in pairs}
        self._corr_matrix: pd.DataFrame | None = None
        self._is_initialized = False

    def update(self, prices: dict[str, float]) -> pd.DataFrame:
        for pair, price in prices.items():
            if pair not in self.price_history:
                continue
            current = self.price_history[pair]
            new_prices = list(current.values) + [price]
            self.price_history[pair] = pd.Series(new_prices)

        min_len = len(self.price_history[self.pairs[0]])

        if min_len >= self.window + 1:
            self._corr_matrix = correlation_matrix(self.price_history, self.window)
            self._is_initialized = True

        if min_len > self.window:
            for pair in self.pairs:
                self.price_history[pair] = self.price_history[pair].iloc[-self.window :]

        return self._corr_matrix if self._corr_matrix is not None else pd.DataFrame()

    @property
    def correlation_matrix(self) -> pd.DataFrame | None:
        return self._corr_matrix

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    def check_exposure(self, positions: list[Position]) -> list[str]:
        if self._corr_matrix is None:
            return []
        return check_correlated_exposure(positions, self._corr_matrix, self.threshold)

    def get_portfolio_exposure(self, positions: list[Position]) -> float:
        if self._corr_matrix is None:
            return sum(p["exposure"] for p in positions) if positions else 0.0
        return portfolio_exposure(positions, self._corr_matrix)


def check_correlated_exposure(
    current_positions: list[Position],
    corr_matrix: pd.DataFrame,
    threshold: float = 0.7,
) -> list[str]:
    warnings: list[str] = []
    for i, pos_a in enumerate(current_positions):
        for j, pos_b in enumerate(current_positions):
            if i >= j:
                continue
            sym_a = pos_a["symbol"]
            sym_b = pos_b["symbol"]
            if sym_a not in corr_matrix.index or sym_b not in corr_matrix.columns:
                continue
            corr_value = corr_matrix.loc[sym_a, sym_b]
            if pd.notna(corr_value) and abs(corr_value) > threshold:
                warnings.append(
                    f"High correlation ({corr_value:.3f}) between {sym_a} and {sym_b} "
                    f"— combined exposure may exceed limits"
                )
    return warnings


def portfolio_exposure(
    current_positions: list[Position],
    corr_matrix: pd.DataFrame,
) -> float:
    if not current_positions:
        return 0.0
    total_exposure = sum(p["exposure"] for p in current_positions)
    for i, pos_a in enumerate(current_positions):
        for j, pos_b in enumerate(current_positions):
            if i >= j:
                continue
            sym_a = pos_a["symbol"]
            sym_b = pos_b["symbol"]
            if sym_a not in corr_matrix.index or sym_b not in corr_matrix.columns:
                continue
            corr_value = corr_matrix.loc[sym_a, sym_b]
            if pd.notna(corr_value):
                overlap = abs(corr_value) * (pos_a["exposure"] + pos_b["exposure"]) / 2.0
                total_exposure -= overlap
    return max(0.0, total_exposure)
