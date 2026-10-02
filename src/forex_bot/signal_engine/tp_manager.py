"""TPManager — 3-level take profit system with progressive SL management.

TTCL3 system:
  TP1 = 1R (risk:reward 1:1) — close 33%
  TP2 = 1.5R — close 33%
  TP3 = 2R — let 34% run

Progressive SL management:
  After TP1 hit → move SL to breakeven + 1 pip
  After TP2 hit → move SL to TP1 price (lock profit)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytz

_ET = pytz.timezone("America/New_York")


class TPManager:
    """Manages 3-level TP system with progressive SL management."""

    # Position sizing per TP level
    TP1_SIZE = 0.33
    TP2_SIZE = 0.33
    TP3_SIZE = 0.34

    # R:R multiples for each TP level
    TP1_RR = 1.0
    TP2_RR = 1.5
    TP3_RR = 2.0

    def __init__(
        self,
        entry_price: float,
        stop_price: float,
        pip_size: float,
        direction: str = "long",
    ):
        self.entry = entry_price
        self.stop = stop_price
        self.original_stop = stop_price
        self.pip_size = pip_size
        self.direction = direction

        self.stop_distance = abs(entry_price - stop_price)

        # TP levels
        if direction == "long":
            self.tp1_price = entry_price + self.stop_distance * self.TP1_RR
            self.tp2_price = entry_price + self.stop_distance * self.TP2_RR
            self.tp3_price = entry_price + self.stop_distance * self.TP3_RR
        else:
            self.tp1_price = entry_price - self.stop_distance * self.TP1_RR
            self.tp2_price = entry_price - self.stop_distance * self.TP2_RR
            self.tp3_price = entry_price - self.stop_distance * self.TP3_RR

        # Track which levels have been hit
        self.tp1_hit = False
        self.tp2_hit = False
        self.tp3_hit = False
        self.sl_moved_to_be = False
        self.sl_moved_to_tp1 = False

    def update(self, high: float, low: float, close: float) -> Optional[str]:
        """Check if any TP level is hit. Returns highest hit level name or None.

        Also updates SL progressively.
        """
        highest_hit = None

        if self.direction == "long":
            if high >= self.tp3_price:
                self.tp3_hit = True
                self.tp2_hit = True
                self.tp1_hit = True
                highest_hit = "tp3"
            elif high >= self.tp2_price:
                self.tp2_hit = True
                self.tp1_hit = True
                highest_hit = "tp2"
            elif high >= self.tp1_price:
                self.tp1_hit = True
                highest_hit = "tp1"
        else:
            if low <= self.tp3_price:
                self.tp3_hit = True
                self.tp2_hit = True
                self.tp1_hit = True
                highest_hit = "tp3"
            elif low <= self.tp2_price:
                self.tp2_hit = True
                self.tp1_hit = True
                highest_hit = "tp2"
            elif low <= self.tp1_price:
                self.tp1_hit = True
                highest_hit = "tp1"

        self._update_sl()
        return highest_hit

    def _update_sl(self) -> None:
        """Progressively move SL based on TP hits."""
        if self.tp2_hit and not self.sl_moved_to_tp1:
            # Lock in TP1 profit — move SL to TP1 price
            if self.direction == "long":
                self.stop = self.tp1_price
            else:
                self.stop = self.tp1_price
            self.sl_moved_to_tp1 = True
        elif self.tp1_hit and not self.sl_moved_to_be:
            # Move to breakeven + 1 pip
            if self.direction == "long":
                self.stop = max(self.stop, self.entry - 1 * self.pip_size)
            else:
                self.stop = min(self.stop, self.entry + 1 * self.pip_size)
            self.sl_moved_to_be = True

    @staticmethod
    def should_mandatory_exit(bar_time: datetime) -> bool:
        """Exit at 8am NY regardless of P&L (mandatory exit per TTC rules)."""
        if bar_time.tzinfo is None:
            bar_time = pytz.utc.localize(bar_time)
        et = bar_time.astimezone(_ET)
        return et.hour >= 8 and et.hour < 12
