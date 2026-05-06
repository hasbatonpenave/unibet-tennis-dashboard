"""Circuit breaker for API error resilience."""

import logging
import time

logger = logging.getLogger("unibet_feed")


class CircuitBreaker:
    """Opens after `threshold` consecutive failures, stays open for `cooldown` seconds."""

    def __init__(self, threshold: int = 5, cooldown: float = 300.0):
        self._threshold = threshold
        self._cooldown = cooldown
        self._consecutive_errors = 0
        self._open = False
        self._until = 0.0

    @property
    def is_open(self) -> bool:
        if not self._open:
            return False
        if time.time() >= self._until:
            self._open = False
            self._consecutive_errors = 0
            logger.info("Circuit breaker reset")
            return False
        return True

    def record_failure(self) -> None:
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._threshold:
            self._open = True
            self._until = time.time() + self._cooldown
            logger.warning(f"Circuit breaker opened for {self._cooldown}s")

    def record_success(self) -> None:
        self._consecutive_errors = 0
