from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(Enum):
    LONG = "long"
    SHORT = "short"


class Session(Enum):
    LONDON = "london"
    NY_AM = "ny_am"
    NY_PM = "ny_pm"
    OUTSIDE = "outside"


class Strength(Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"
    VERY_STRONG = "very_strong"


@dataclass
class Signal:
    direction: Direction
    strength: Strength
    entry_price: float
    stop_loss: float
    take_profit: float
    signal_time: datetime
    candle_age_seconds: float = 0.0
    confluence_count: int = 0
    has_liquidity_sweep: bool = False
    has_order_block: bool = False
    has_fvg: bool = False
    session: Session = Session.OUTSIDE


@dataclass
class ValidationResult:
    passed: bool
    signal: Signal
    reason: str = ""


@dataclass
class ValidatorConfig:
    min_strength: Strength = Strength.MODERATE
    max_candle_age_seconds: float = 300.0
    min_confluence: int = 2
    require_killzone: bool = True
    max_sl_tp_ratio: float = 0.5


_STRENGTH_ORDER = {
    Strength.WEAK: 0,
    Strength.MODERATE: 1,
    Strength.STRONG: 2,
    Strength.VERY_STRONG: 3,
}

_KILLZONE_SESSIONS = {Session.LONDON, Session.NY_AM, Session.NY_PM}


class SignalValidator:
    def __init__(self, config: ValidatorConfig | None = None):
        self.config = config or ValidatorConfig()

    def validate(self, signal: Signal) -> ValidationResult:
        result = self._check_strength(signal)
        if not result.passed:
            return result

        result = self._check_freshness(signal)
        if not result.passed:
            return result

        result = self._check_confluence(signal)
        if not result.passed:
            return result

        result = self._check_session(signal)
        if not result.passed:
            return result

        result = self._check_risk_reward(signal)
        if not result.passed:
            return result

        return ValidationResult(passed=True, signal=signal, reason="All gates passed")

    def validate_batch(self, signals: list[Signal]) -> list[ValidationResult]:
        return [self.validate(s) for s in signals]

    def filter(self, signals: list[Signal]) -> list[Signal]:
        return [s for s in signals if self.validate(s).passed]

    def confluence_score(
        self,
        signal: Signal,
        freshness_penalty: float = 0.1,
        killzone_bonus: float = 0.2,
    ) -> float:
        score = float(signal.confluence_count)

        if signal.candle_age_seconds > self.config.max_candle_age_seconds:
            score -= freshness_penalty

        if signal.session in _KILLZONE_SESSIONS:
            score += killzone_bonus

        return max(0.0, score)

    def count_confluences(self, signal: Signal) -> int:
        count = signal.confluence_count
        if signal.has_liquidity_sweep:
            count += 1
        if signal.has_order_block:
            count += 1
        if signal.has_fvg:
            count += 1
        return count

    def _check_strength(self, signal: Signal) -> ValidationResult:
        if _STRENGTH_ORDER[signal.strength] < _STRENGTH_ORDER[self.config.min_strength]:
            return ValidationResult(
                passed=False,
                signal=signal,
                reason=f"Strength {signal.strength.value} below minimum {self.config.min_strength.value}",
            )
        return ValidationResult(passed=True, signal=signal)

    def _check_freshness(self, signal: Signal) -> ValidationResult:
        if signal.candle_age_seconds > self.config.max_candle_age_seconds:
            return ValidationResult(
                passed=False,
                signal=signal,
                reason=f"Candle age {signal.candle_age_seconds}s exceeds max {self.config.max_candle_age_seconds}s",
            )
        return ValidationResult(passed=True, signal=signal)

    def _check_confluence(self, signal: Signal) -> ValidationResult:
        if signal.confluence_count < self.config.min_confluence:
            return ValidationResult(
                passed=False,
                signal=signal,
                reason=f"Confluence {signal.confluence_count} below minimum {self.config.min_confluence}",
            )
        return ValidationResult(passed=True, signal=signal)

    def _check_session(self, signal: Signal) -> ValidationResult:
        if self.config.require_killzone and signal.session not in _KILLZONE_SESSIONS:
            return ValidationResult(
                passed=False,
                signal=signal,
                reason=f"Session {signal.session.value} not in killzone",
            )
        return ValidationResult(passed=True, signal=signal)

    def _check_risk_reward(self, signal: Signal) -> ValidationResult:
        risk = abs(signal.entry_price - signal.stop_loss)
        reward = abs(signal.take_profit - signal.entry_price)
        if risk == 0:
            return ValidationResult(
                passed=False,
                signal=signal,
                reason="Stop loss equals entry price (zero risk)",
            )
        if reward == 0:
            return ValidationResult(
                passed=False,
                signal=signal,
                reason="Take profit equals entry price (zero reward)",
            )
        sl_tp = risk / reward
        if sl_tp > self.config.max_sl_tp_ratio and not math.isclose(sl_tp, self.config.max_sl_tp_ratio):
            return ValidationResult(
                passed=False,
                signal=signal,
                reason=f"SL/TP ratio {sl_tp:.4f} exceeds max {self.config.max_sl_tp_ratio}",
            )
        return ValidationResult(passed=True, signal=signal)
