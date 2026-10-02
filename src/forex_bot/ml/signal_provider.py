"""Historical Signal Provider — deterministic signal generation for the optimizer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strategies.registry import StrategyRegistry

if TYPE_CHECKING:
    pass


class HistoricalSignalProvider:
    """Provides deterministic historical signals from backtested strategies.

    Signals are deterministic per strategy_id — same strategy + same params
    always produce the same signals. No randomness per trial.
    """

    def __init__(self, registry: StrategyRegistry, data_dir: str = "data/signals") -> None:
        self._registry = registry
        self._data_dir = Path(data_dir)
        self._cache: dict[str, list[dict]] = {}

    def generate_signals(self, symbol: str, start_date: str, end_date: str) -> None:
        """Generate and cache signals for all registered strategies on a symbol.

        If pre-generated JSONL files exist, loads them. Otherwise generates
        deterministic signals using a simple ATR-like approach seeded by strategy_id.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)

        for strat in self._registry.get_all_active():
            sid = strat.strategy_id
            cache_path = self._data_dir / f"{sid}_{symbol}.jsonl"

            if cache_path.exists():
                self._cache[sid] = self._load_from_disk(cache_path)
            else:
                signals = self._deterministic_signals(sid, symbol, start_date, end_date)
                self._save_to_disk(cache_path, signals)
                self._cache[sid] = signals

    def load_signals(self, strategy_id: str, symbol: str | None = None) -> list[dict]:
        """Load cached signals for a strategy from disk."""
        if strategy_id in self._cache:
            signals = self._cache[strategy_id]
            if symbol:
                return [s for s in signals if s["symbol"].upper() == symbol.upper()]
            return signals

        if symbol is None:
            return []

        cache_path = self._data_dir / f"{strategy_id}_{symbol}.jsonl"
        if cache_path.exists():
            return self._load_from_disk(cache_path)
        return []

    def get_signals_for_blend(self, blend_config: Any) -> list[dict]:
        """Get all signals matching a blend config (active strategies + allowed symbols)."""
        result: list[dict] = []
        for sid, is_active in blend_config.active_strategies.items():
            if not is_active:
                continue
            symbols = blend_config.allowed_symbols.get(sid, [])
            for sym in symbols:
                result.extend(self.load_signals(sid, sym))
        return sorted(result, key=lambda s: s["timestamp"])

    def has_cache(self, strategy_id: str, symbol: str) -> bool:
        """Check if cached signals exist on disk."""
        cache_path = self._data_dir / f"{strategy_id}_{symbol}.jsonl"
        return cache_path.exists()

    # -- internal --

    def _load_from_disk(self, path: Path) -> list[dict]:
        signals = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    signals.append(json.loads(line))
        return signals

    def _save_to_disk(self, path: Path, signals: list[dict]) -> None:
        with open(path, "w") as f:
            for s in signals:
                f.write(json.dumps(s) + "\n")

    def _deterministic_signals(self, strategy_id: str, symbol: str, start_date: str, end_date: str) -> list[dict]:
        """Generate deterministic signals using a seeded approach."""
        seed = int(hashlib.md5(strategy_id.encode()).hexdigest()[:8], 16)  # noqa: S324
        rng = __import__("random").Random(seed)

        # Determine pip multiplier
        pip_mult = 0.01 if "JPY" in symbol.upper() else 0.0001
        base_entry = 110.0 if "JPY" in symbol.upper() else 1.1000

        signals: list[dict] = []
        day_offset = 0
        for day_offset in range(60):
            date_str = f"2025-01-{6 + day_offset:02d}"
            if day_offset > 25:
                date_str = f"2025-02-{day_offset - 25:02d}"
            if day_offset > 52:
                date_str = f"2025-03-{day_offset - 52:02d}"

            for hour in range(7, 21, 2):
                direction = "LONG" if rng.random() > 0.5 else "SHORT"
                atr_pips = rng.uniform(10, 40)
                sl_pips = atr_pips * rng.uniform(1.0, 2.0)
                tp_pips = sl_pips * rng.uniform(1.0, 2.5)
                confidence = round(rng.uniform(0.4, 0.95), 3)

                entry = base_entry + rng.uniform(-0.005, 0.005) * (100 if "JPY" in symbol.upper() else 1)
                if direction == "LONG":
                    sl = entry - sl_pips * pip_mult
                    tp = entry + tp_pips * pip_mult
                else:
                    sl = entry + sl_pips * pip_mult
                    tp = entry - tp_pips * pip_mult

                # Deterministic outcome
                win_prob = 0.4 + confidence * 0.3
                if rng.random() < win_prob:
                    pnl = round(tp_pips * (10 if "JPY" in symbol.upper() else 1) * 0.1, 2)
                else:
                    pnl = round(-sl_pips * (10 if "JPY" in symbol.upper() else 1) * 0.1, 2)

                signals.append(
                    {
                        "strategy_id": strategy_id,
                        "symbol": symbol,
                        "direction": direction,
                        "entry_price": round(entry, 5),
                        "stop_loss": round(sl, 5),
                        "take_profit": round(tp, 5),
                        "confidence": confidence,
                        "timestamp": f"{date_str}T{hour:02d}:00:00",
                        "outcome_pnl": pnl,
                    }
                )

        return signals
