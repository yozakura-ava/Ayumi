from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from statsmodels.tsa.stattools import adfuller


@dataclass
class CointegrationResult:
    is_cointegrated: bool
    p_value: float
    hedge_ratio: float
    constant: float
    adf_statistic: float
    adf_p_value: float


@dataclass
class SpreadStats:
    mean: float
    std: float
    z_score: float
    spread: float


class CointegrationEngine:
    """Engine for cointegration analysis between two price series.

    Uses OLS for hedge ratio estimation and statsmodels adfuller for
    proper Augmented Dickey-Fuller testing with MacKinnon critical values.
    """

    def __init__(self, lookback: int = 60):
        self.lookback = lookback

    def compute_hedge_ratio(self, prices_a: np.ndarray, prices_b: np.ndarray) -> tuple[float, float]:
        """Compute hedge ratio via OLS: prices_a = constant + hedge_ratio * prices_b."""
        if len(prices_a) < 2 or len(prices_b) < 2:
            return 1.0, 0.0

        x = np.column_stack([np.ones(len(prices_b)), prices_b])
        coeffs, _, _, _ = np.linalg.lstsq(x, prices_a, rcond=None)
        return float(coeffs[1]), float(coeffs[0])

    def compute_spread(
        self,
        prices_a: np.ndarray,
        prices_b: np.ndarray,
        hedge_ratio: float | None = None,
        constant: float | None = None,
    ) -> np.ndarray:
        """Compute spread: spread = prices_a - hedge_ratio * prices_b - constant."""
        if hedge_ratio is None or constant is None:
            hr, const = self.compute_hedge_ratio(prices_a, prices_b)
            hedge_ratio = hr
            constant = const

        return prices_a - hedge_ratio * prices_b - constant

    def engle_granger_test(
        self, prices_a: np.ndarray, prices_b: np.ndarray, significance: float = 0.05
    ) -> CointegrationResult:
        """Run Engle-Granger two-step cointegration test.

        1. OLS regression to get hedge ratio
        2. Compute spread
        3. ADF test on spread (null: unit root = not cointegrated)
        """
        if len(prices_a) < self.lookback or len(prices_b) < self.lookback:
            return CointegrationResult(
                is_cointegrated=False,
                p_value=1.0,
                hedge_ratio=1.0,
                constant=0.0,
                adf_statistic=0.0,
                adf_p_value=1.0,
            )

        n = min(len(prices_a), len(prices_b), self.lookback)
        pa = prices_a[-n:]
        pb = prices_b[-n:]

        hedge_ratio, constant = self.compute_hedge_ratio(pa, pb)
        spread = self.compute_spread(pa, pb, hedge_ratio, constant)

        adf_result = adfuller(spread, maxlag=1)
        adf_stat = float(adf_result[0])
        adf_p = float(adf_result[1])

        return CointegrationResult(
            is_cointegrated=bool(adf_p < significance),
            p_value=adf_p,
            hedge_ratio=hedge_ratio,
            constant=constant,
            adf_statistic=float(adf_stat),
            adf_p_value=float(adf_p),
        )

    def compute_z_score(
        self,
        prices_a: np.ndarray,
        prices_b: np.ndarray,
        hedge_ratio: float | None = None,
        constant: float | None = None,
        lookback: int | None = None,
    ) -> SpreadStats:
        """Compute z-score of the spread with no look-ahead bias.

        Statistics (mean, std) are computed over [0:-1] and the z-score
        is computed for the last element [-1] only.
        """
        lb = lookback if lookback is not None else self.lookback
        n = min(len(prices_a), len(prices_b), lb + 1)

        if n < 3:
            return SpreadStats(mean=0.0, std=1.0, z_score=0.0, spread=0.0)

        pa = prices_a[-n:]
        pb = prices_b[-n:]

        if hedge_ratio is None or constant is None:
            hr, const = self.compute_hedge_ratio(pa[:-1], pb[:-1])
            hedge_ratio = hr
            constant = const

        spread = self.compute_spread(pa, pb, hedge_ratio, constant)

        hist_spread = spread[:-1]
        current_spread = spread[-1]

        spread_mean = float(np.mean(hist_spread))
        spread_std = float(np.std(hist_spread, ddof=1))

        if spread_std < 1e-10:
            z_score = 0.0
        else:
            z_score = (current_spread - spread_mean) / spread_std

        return SpreadStats(
            mean=spread_mean,
            std=spread_std,
            z_score=z_score,
            spread=current_spread,
        )

    def rolling_cointegration(
        self,
        prices_a: np.ndarray,
        prices_b: np.ndarray,
        window: int,
        step: int = 1,
    ) -> list[dict]:
        """Sliding-window Engle-Granger cointegration analysis."""
        results = []
        for i in range(0, len(prices_a) - window, step):
            pa_window = prices_a[i : i + window]
            pb_window = prices_b[i : i + window]
            result = self.engle_granger_test(pa_window, pb_window)
            spread_stats = self.compute_z_score(pa_window, pb_window, result.hedge_ratio, result.constant, window)
            results.append(
                {
                    "index": i,
                    "window_start": i,
                    "window_end": i + window,
                    "is_cointegrated": result.is_cointegrated,
                    "p_value": result.p_value,
                    "hedge_ratio": result.hedge_ratio,
                    "z_score": spread_stats.z_score,
                    "spread_mean": spread_stats.mean,
                    "spread_std": spread_stats.std,
                }
            )
        return results


class PairsSignalGenerator:
    """Generates entry/exit/stop signals for pairs trading based on z-score."""

    def __init__(
        self,
        entry_threshold: float = 2.0,
        exit_threshold: float = 0.0,
        stop_loss_threshold: float = 3.0,
        lookback: int = 60,
    ):
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.stop_loss_threshold = stop_loss_threshold
        self.cointegration_engine = CointegrationEngine(lookback=lookback)
        self.lookback = lookback
        self._hedge_ratio: float | None = None
        self._constant: float | None = None
        self._in_position: bool = False
        self._position_side: str | None = None

    def reset(self):
        """Clear all internal state."""
        self._hedge_ratio = None
        self._constant = None
        self._in_position = False
        self._position_side = None

    @property
    def hedge_ratio(self) -> float | None:
        return self._hedge_ratio

    @property
    def constant(self) -> float | None:
        return self._constant

    @property
    def in_position(self) -> bool:
        return self._in_position

    def update_cointegration(self, prices_a: np.ndarray, prices_b: np.ndarray) -> bool:
        """Run cointegration test and update hedge ratio if cointegrated."""
        result = self.cointegration_engine.engle_granger_test(prices_a, prices_b)
        if result.is_cointegrated:
            self._hedge_ratio = result.hedge_ratio
            self._constant = result.constant
            return True
        return False

    def compute_spread(self, prices_a: np.ndarray, prices_b: np.ndarray) -> float | None:
        """Compute current spread using stored hedge ratio."""
        if self._hedge_ratio is None:
            return None
        return float(prices_a[-1] - self._hedge_ratio * prices_b[-1] - self._constant)

    def compute_z_score(self, prices_a: np.ndarray, prices_b: np.ndarray) -> float | None:
        """Compute z-score using stored hedge ratio (no look-ahead bias)."""
        if self._hedge_ratio is None:
            return None

        spread_stats = self.cointegration_engine.compute_z_score(
            prices_a,
            prices_b,
            hedge_ratio=self._hedge_ratio,
            constant=self._constant,
            lookback=self.lookback,
        )
        return spread_stats.z_score

    def generate_signal(self, prices_a: np.ndarray, prices_b: np.ndarray) -> tuple[str | None, str | None]:
        """Generate trading signal based on z-score thresholds.

        Returns:
            (signal_type, reason) where signal_type is one of:
            entry_long, entry_short, exit, stop_loss, hold_long, hold_short, or None
        """
        z_score = self.compute_z_score(prices_a, prices_b)

        if z_score is None:
            return None, None

        if not self._in_position:
            if z_score > self.entry_threshold:
                self._in_position = True
                self._position_side = "short"
                return "entry_short", "z_score_overbought"
            elif z_score < -self.entry_threshold:
                self._in_position = True
                self._position_side = "long"
                return "entry_long", "z_score_oversold"
            else:
                return None, None
        else:
            if self._position_side == "short":
                if z_score <= self.exit_threshold:
                    self._in_position = False
                    self._position_side = None
                    return "exit", "z_score_reverted"
                elif z_score > self.stop_loss_threshold:
                    self._in_position = False
                    self._position_side = None
                    return "stop_loss", "z_score_stopped"
                else:
                    return "hold_short", "maintaining_position"
            else:
                if z_score >= self.exit_threshold:
                    self._in_position = False
                    self._position_side = None
                    return "exit", "z_score_reverted"
                elif z_score < -self.stop_loss_threshold:
                    self._in_position = False
                    self._position_side = None
                    return "stop_loss", "z_score_stopped"
                else:
                    return "hold_long", "maintaining_position"


def parameter_sweep(
    prices_a: np.ndarray,
    prices_b: np.ndarray,
    lookbacks: list[int],
    entry_thresholds: list[float],
    exit_thresholds: list[float],
    stop_thresholds: list[float],
) -> list[dict]:
    """Grid search over cointegration parameters.

    Returns list of dicts with parameter combos and signal counts.
    Precomputes cointegration per lookback to avoid redundant ADF tests.
    """
    if not lookbacks:
        return []

    min_len = max(lookbacks) + 1
    if len(prices_a) < min_len or len(prices_b) < min_len:
        return []

    coint_cache: dict[tuple[int, int], tuple[float, float] | None] = {}
    engines: dict[int, CointegrationEngine] = {lb: CointegrationEngine(lookback=lb) for lb in lookbacks}

    for lb in lookbacks:
        engine = engines[lb]
        for i in range(lb, len(prices_a)):
            key = (lb, i)
            pa = prices_a[: i + 1]
            pb = prices_b[: i + 1]
            result = engine.engle_granger_test(pa, pb)
            if result.is_cointegrated:
                coint_cache[key] = (result.hedge_ratio, result.constant)
            else:
                coint_cache[key] = None

    results = []

    for lb in lookbacks:
        for entry in entry_thresholds:
            for exit_t in exit_thresholds:
                for stop in stop_thresholds:
                    if stop <= entry:
                        continue

                    generator = PairsSignalGenerator(
                        entry_threshold=entry,
                        exit_threshold=exit_t,
                        stop_loss_threshold=stop,
                        lookback=lb,
                    )

                    signal_count = 0
                    for i in range(lb, len(prices_a)):
                        cached = coint_cache.get((lb, i))
                        if cached is None:
                            continue

                        hr, const = cached
                        generator._hedge_ratio = hr
                        generator._constant = const

                        pa = prices_a[: i + 1]
                        pb = prices_b[: i + 1]
                        signal, reason = generator.generate_signal(pa, pb)
                        if signal and signal.startswith("entry"):
                            signal_count += 1

                    results.append(
                        {
                            "lookback": lb,
                            "entry_threshold": entry,
                            "exit_threshold": exit_t,
                            "stop_loss_threshold": stop,
                            "num_signals": signal_count,
                        }
                    )

    return results
