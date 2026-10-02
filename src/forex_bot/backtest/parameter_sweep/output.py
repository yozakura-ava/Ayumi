from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .result import SweepResult

import pandas as pd


def _to_dataframe(result: SweepResult) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in result.rows:
        record = dict(row.params)
        record["win_rate"] = row.win_rate
        record["max_dd"] = row.max_dd
        record["total_return"] = row.total_return
        record["sharpe_ratio"] = row.sharpe_ratio
        record["trade_count"] = row.trade_count
        record["profit_factor"] = row.profit_factor
        rows.append(record)
    return pd.DataFrame(rows)


def to_csv(result: SweepResult, path: str) -> None:
    df = _to_dataframe(result)
    df.to_csv(path, index=False)


def to_json(result: SweepResult, path: str) -> None:
    df = _to_dataframe(result)
    df.to_json(path, orient="records", indent=2)
