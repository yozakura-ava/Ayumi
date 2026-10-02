"""Profile Router for Ayumi signal routing.

Routes signals to Sniper or Swarm profile based on confidence score.

Thresholds:
    - >= 0.70 → Sniper (full risk allocation)
    - 0.40 - 0.69 → Swarm (half risk allocation)
    - < 0.40 → Rejected
"""

import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class Profile(str, Enum):
    SNIPER = "sniper"
    SWARM = "swarm"


class ProfileRouter:
    """Routes signals to Sniper or Swarm profile based on confidence score."""

    SNIPER_THRESHOLD = 0.70
    SWARM_THRESHOLD = 0.40
    MAX_SNIPER_POSITIONS = 3
    MAX_SWARM_POSITIONS = 5

    def __init__(
        self,
        sniper_threshold: float = 0.70,
        swarm_threshold: float = 0.40,
        max_sniper: int = 3,
        max_swarm: int = 5,
    ):
        self.sniper_threshold = sniper_threshold
        self.swarm_threshold = swarm_threshold
        self.max_sniper = max_sniper
        self.max_swarm = max_swarm
        self._sniper_open = 0
        self._swarm_open = 0
        self._positions: dict[str, Profile] = {}  # position_id -> profile

    def route(self, confidence: float) -> Optional[Profile]:
        """Route a signal based on confidence score.

        Returns Profile or None if rejected (below threshold or at capacity).
        """
        if confidence < 0.0 or confidence > 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {confidence}")

        if confidence < self.swarm_threshold:
            logger.debug(
                "Signal rejected: confidence %.2f below swarm threshold %.2f",
                confidence,
                self.swarm_threshold,
            )
            return None

        if confidence >= self.sniper_threshold:
            profile = Profile.SNIPER
            if self._sniper_open >= self.max_sniper:
                logger.info(
                    "Sniper at capacity (%d/%d), falling back to swarm",
                    self._sniper_open,
                    self.max_sniper,
                )
                profile = Profile.SWARM
                if self._swarm_open >= self.max_swarm:
                    logger.info(
                        "Swarm also at capacity (%d/%d), signal rejected",
                        self._swarm_open,
                        self.max_swarm,
                    )
                    return None
        else:
            profile = Profile.SWARM
            if self._swarm_open >= self.max_swarm:
                logger.info(
                    "Swarm at capacity (%d/%d), signal rejected",
                    self._swarm_open,
                    self.max_swarm,
                )
                return None

        return profile

    def register_open(self, position_id: str, profile: Profile) -> None:
        """Register an open position with its profile."""
        if position_id in self._positions:
            raise ValueError(f"Position {position_id} already registered")
        self._positions[position_id] = profile
        if profile == Profile.SNIPER:
            self._sniper_open += 1
        else:
            self._swarm_open += 1

    def close(self, position_id: str) -> None:
        """Close a position and free its slot."""
        if position_id not in self._positions:
            raise ValueError(f"Unknown position {position_id}")
        profile = self._positions.pop(position_id)
        if profile == Profile.SNIPER:
            self._sniper_open = max(0, self._sniper_open - 1)
        else:
            self._swarm_open = max(0, self._swarm_open - 1)

    @property
    def sniper_count(self) -> int:
        return self._sniper_open

    @property
    def swarm_count(self) -> int:
        return self._swarm_open
