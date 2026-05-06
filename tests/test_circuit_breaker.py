"""Unit tests for feed/circuit_breaker.py."""

import time
import pytest
from feed.circuit_breaker import CircuitBreaker


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker()
        assert cb.is_open is False

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False
        cb.record_failure()
        assert cb.is_open is True

    def test_record_success_resets_count(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False

    def test_stays_open_during_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown=10.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is True

    def test_resets_after_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown=-0.1)  # already expired
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open is False  # cooldown already passed

    def test_custom_threshold(self):
        cb = CircuitBreaker(threshold=7)
        for _ in range(6):
            cb.record_failure()
        assert cb.is_open is False
        cb.record_failure()
        assert cb.is_open is True

    def test_custom_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown=0.01)
        cb.record_failure()
        assert cb.is_open is True
        time.sleep(0.02)
        assert cb.is_open is False
