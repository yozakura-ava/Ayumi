from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
from scipy import stats


class GoNogoDecision(Enum):
    GO = "GO"
    NO_GO = "NO_GO"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GoNogoResult:
    decision: GoNogoDecision
    p_value: float | None = None
    total_oos_trades: int = 0
    min_trades_met: bool = False
    significance_met: bool = False
    full_bt_consistent: bool | None = None
    multi_pair_status: str | None = None
    checks: list[CheckResult] = field(default_factory=list)


def _one_tailed_p(t_stat: float, p_value: float) -> float:
    return p_value / 2.0 if t_stat > 0 else 1.0 - p_value / 2.0


def check_statistical_significance(
    pnls: list[float],
    alpha: float = 0.10,
) -> CheckResult:
    if len(pnls) < 2:
        return CheckResult(
            name="statistical_significance",
            passed=False,
            detail="Not enough trades for t-test (need >= 2)",
        )

    arr = np.array(pnls, dtype=np.float64)
    t_stat, p_value = stats.ttest_1samp(arr, 0.0)
    t_stat = float(t_stat)
    p_value = float(p_value)
    one_tailed_p = _one_tailed_p(t_stat, p_value)

    passed = one_tailed_p < alpha
    detail = f"one-tailed p={one_tailed_p:.4f}, alpha={alpha}, t={t_stat:.4f}, n={len(pnls)}"
    return CheckResult(
        name="statistical_significance",
        passed=passed,
        detail=detail,
        extra={"one_tailed_p": one_tailed_p},
    )


def check_min_trade_count(
    total_trades: int,
    minimum: int = 50,
) -> CheckResult:
    passed = total_trades >= minimum
    detail = f"{total_trades} trades (minimum {minimum})"
    return CheckResult(
        name="min_trade_count",
        passed=passed,
        detail=detail,
    )


def check_full_bt_consistency(
    full_bt_pnl: float,
    wf_positive: bool,
) -> CheckResult:
    if full_bt_pnl > 0 and wf_positive:
        return CheckResult(
            name="full_bt_consistency",
            passed=True,
            detail=f"Full BT PnL={full_bt_pnl:.2f}, WF positive=True",
        )
    if full_bt_pnl <= 0 and not wf_positive:
        return CheckResult(
            name="full_bt_consistency",
            passed=True,
            detail=f"Full BT PnL={full_bt_pnl:.2f}, WF positive=False",
        )
    return CheckResult(
        name="full_bt_consistency",
        passed=False,
        detail=(
            f"Full BT PnL={full_bt_pnl:.2f}, WF positive={wf_positive} "
            f"(negative BT with positive WF = inconclusive signal)"
        ),
    )


def check_multi_pair_validation(
    pair_results: dict[str, float],
) -> CheckResult:
    pairs_with_positive_pf = {pair: pf for pair, pf in pair_results.items() if pf > 1.0}
    count = len(pairs_with_positive_pf)

    if count >= 2:
        status = "confirmed"
        passed = True
    elif count == 1:
        status = "weak"
        passed = False
    else:
        status = "failed"
        passed = False

    detail = f"{count}/{len(pair_results)} pairs with PF>1.0 (status={status}): {pair_results}"
    return CheckResult(
        name="multi_pair_validation",
        passed=passed,
        detail=detail,
        extra={"status": status},
    )


def evaluate_statistical_checks(
    oos_pnls: list[float],
    full_bt_pnl: float | None = None,
    pair_results: dict[str, float] | None = None,
    alpha: float = 0.10,
    min_trades: int = 50,
) -> GoNogoResult:
    checks: list[CheckResult] = []

    sig_check = check_statistical_significance(oos_pnls, alpha)
    checks.append(sig_check)

    trade_count_check = check_min_trade_count(len(oos_pnls), min_trades)
    checks.append(trade_count_check)

    full_bt_consistent: bool | None = None
    if full_bt_pnl is not None:
        wf_positive = sum(oos_pnls) > 0 if oos_pnls else False
        bt_check = check_full_bt_consistency(full_bt_pnl, wf_positive)
        checks.append(bt_check)
        full_bt_consistent = bt_check.passed

    multi_pair_status: str | None = None
    if pair_results is not None:
        mp_check = check_multi_pair_validation(pair_results)
        checks.append(mp_check)
        multi_pair_status = mp_check.extra.get("status", "unknown")

    sig_passed = sig_check.passed
    trades_passed = trade_count_check.passed

    if not trades_passed:
        decision = GoNogoDecision.INCONCLUSIVE
    elif full_bt_consistent is False:
        decision = GoNogoDecision.INCONCLUSIVE
    elif sig_passed and trades_passed:
        if multi_pair_status == "confirmed":
            decision = GoNogoDecision.GO
        elif multi_pair_status == "weak":
            decision = GoNogoDecision.INCONCLUSIVE
        else:
            decision = GoNogoDecision.INCONCLUSIVE
    elif trades_passed and not sig_passed:
        decision = GoNogoDecision.NO_GO
    else:
        decision = GoNogoDecision.NO_GO

    p_value = sig_check.extra.get("one_tailed_p") if len(oos_pnls) >= 2 else None

    return GoNogoResult(
        decision=decision,
        p_value=p_value,
        total_oos_trades=len(oos_pnls),
        min_trades_met=trades_passed,
        significance_met=sig_passed,
        full_bt_consistent=full_bt_consistent,
        multi_pair_status=multi_pair_status,
        checks=checks,
    )
