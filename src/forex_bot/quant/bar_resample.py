from __future__ import annotations

from datetime import timedelta

from backtest.engine import Bar


def resample_bars(bars: list[Bar], target_minutes: int) -> list[Bar]:
    if not bars:
        return []

    interval = timedelta(minutes=target_minutes)
    resampled: list[Bar] = []
    current_group: list[Bar] = []
    group_start = None

    for bar in bars:
        if group_start is None:
            group_start = bar.time.replace(second=0, microsecond=0)
            current_group.append(bar)
            continue

        bar_floor = bar.time.replace(second=0, microsecond=0)
        elapsed = bar_floor - group_start

        if elapsed < interval:
            current_group.append(bar)
        else:
            if current_group:
                resampled.append(_merge_group(current_group))
            current_group = [bar]
            group_start = bar_floor

    if current_group:
        resampled.append(_merge_group(current_group))

    return resampled


def _merge_group(group: list[Bar]) -> Bar:
    return Bar(
        time=group[0].time,
        open=group[0].open,
        high=max(b.high for b in group),
        low=min(b.low for b in group),
        close=group[-1].close,
        volume=sum(b.volume for b in group),
    )
