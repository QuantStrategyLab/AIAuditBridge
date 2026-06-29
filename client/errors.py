"""Unified error types for AiGateway client."""

from __future__ import annotations

import time
import threading


class AiGatewayError(RuntimeError):
    """Base error for all AiGateway client failures."""


class AuthenticationError(AiGatewayError):
    """OIDC token fetch or validation failed."""


class ServiceUnavailableError(AiGatewayError):
    """AiGateway service returned an error or was unreachable."""


class TimeoutError(AiGatewayError):
    """Request or job polling timed out."""


class CircuitBreakerOpenError(AiGatewayError):
    """Circuit breaker is open — service is considered unhealthy."""


class CircuitBreaker:
    """Simple in-process circuit breaker.

    After ``failure_threshold`` consecutive failures, the circuit opens.
    After ``recovery_timeout`` seconds, a single trial request is allowed
    (half-open). If it succeeds, the circuit closes; if it fails, it
    re-opens and the timer resets.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self._threshold = failure_threshold
        self._timeout = recovery_timeout
        self._failures = 0
        self._last_failure_time = 0.0
        self._state = "closed"  # closed | open | half_open
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def before_call(self) -> None:
        """Check if a call is allowed. Raises CircuitBreakerOpenError if open."""
        with self._lock:
            if self._state == "closed":
                return
            if self._state == "open":
                if time.time() - self._last_failure_time >= self._timeout:
                    self._state = "half_open"
                    return
                raise CircuitBreakerOpenError(
                    f"Circuit open: {self._failures} failures, "
                    f"retry in {self._timeout - (time.time() - self._last_failure_time):.0f}s"
                )
            # half_open — allow single trial

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = "closed"

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure_time = time.time()
            if self._failures >= self._threshold:
                self._state = "open"
