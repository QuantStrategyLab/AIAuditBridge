"""Health monitoring — error rates, latency tracking, degradation detection.

Tracks service health metrics in-memory. Exposed via enhanced /healthz
and a dedicated GET /v1/ai/health endpoint.

Degradation states:
    healthy     — all systems nominal
    degraded    — elevated error rate or latency, but still serving
    unhealthy   — critical failure, should trigger alert
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# ── constants ───────────────────────────────────────────────────────────

# Sliding windows for metrics
WINDOW_SECONDS = 300  # 5 minutes
MAX_WINDOW_SAMPLES = 1000

# Degradation thresholds
ERROR_RATE_DEGRADED = 0.10   # >10% errors → degraded
ERROR_RATE_UNHEALTHY = 0.30  # >30% errors → unhealthy
LATENCY_P95_DEGRADED = 30.0  # p95 > 30s → degraded
LATENCY_P95_UNHEALTHY = 120.0  # p95 > 120s → unhealthy
BACKGROUND_JOB_LATENCY_P95_DEGRADED = 600.0  # background jobs can legitimately run for minutes
BACKGROUND_JOB_LATENCY_P95_UNHEALTHY = 1800.0
BACKGROUND_JOB_LATENCY_PATHS = {"/v1/ai/execute/jobs/run"}


def latency_profile(path: str) -> str:
    return "background_job" if path in BACKGROUND_JOB_LATENCY_PATHS else "online"


def latency_thresholds(path: str) -> tuple[float, float]:
    if latency_profile(path) == "background_job":
        return BACKGROUND_JOB_LATENCY_P95_DEGRADED, BACKGROUND_JOB_LATENCY_P95_UNHEALTHY
    return LATENCY_P95_DEGRADED, LATENCY_P95_UNHEALTHY


def latency_affects_status(path: str) -> bool:
    """Whether endpoint latency should affect service health status."""
    return True


@dataclass
class EndpointMetrics:
    """Per-endpoint health metrics."""

    path: str
    total_requests: int = 0
    success_count: int = 0
    error_count: int = 0
    latency_samples: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_WINDOW_SAMPLES))
    error_samples: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=MAX_WINDOW_SAMPLES))

    def record(self, latency: float, success: bool, error_type: str = "") -> None:
        now = time.time()
        self.total_requests += 1
        if success:
            self.success_count += 1
        else:
            self.error_count += 1
            self.error_samples.append((now, error_type))
        self.latency_samples.append(latency)

    @property
    def error_rate(self) -> float:
        total = self.success_count + self.error_count
        return self.error_count / total if total > 0 else 0.0

    @property
    def recent_errors(self) -> int:
        """Errors in the current window."""
        cutoff = time.time() - WINDOW_SECONDS
        return sum(1 for ts, _ in self.error_samples if ts >= cutoff)

    @property
    def recent_total(self) -> int:
        """Total requests in the recent window.

        Uses the count of latency samples (bounded by MAX_WINDOW_SAMPLES)
        and the count of recent errors as a conservative estimate.
        """
        lat = len(self.latency_samples)
        err = self.recent_errors
        return max(lat, err)  # conservative

    @property
    def recent_error_rate(self) -> float:
        r = self.recent_total
        return self.recent_errors / r if r > 0 else 0.0

    @property
    def p50_latency(self) -> float:
        return _percentile(list(self.latency_samples), 50)

    @property
    def p95_latency(self) -> float:
        return _percentile(list(self.latency_samples), 95)

    @property
    def p99_latency(self) -> float:
        return _percentile(list(self.latency_samples), 99)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "total": self.total_requests,
            "success": self.success_count,
            "errors": self.error_count,
            "error_rate": round(self.recent_error_rate, 4),
            "p50_ms": round(self.p50_latency * 1000, 1),
            "p95_ms": round(self.p95_latency * 1000, 1),
            "p99_ms": round(self.p99_latency * 1000, 1),
            "latency_affects_status": latency_affects_status(self.path),
            "latency_profile": latency_profile(self.path),
        }


def _percentile(sorted_data: list[float], p: float) -> float:
    if not sorted_data:
        return 0.0
    sorted_data.sort()
    k = (len(sorted_data) - 1) * p / 100.0
    f = int(k)
    c = k - f
    if f + 1 < len(sorted_data):
        return sorted_data[f] + c * (sorted_data[f + 1] - sorted_data[f])
    return sorted_data[f]


class HealthMonitor:
    """Thread-safe service health tracker."""

    def __init__(self):
        self._metrics: dict[str, EndpointMetrics] = {}
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._last_error_at: float | None = None
        self._last_error_msg: str = ""

    def get_or_create(self, path: str) -> EndpointMetrics:
        with self._lock:
            if path not in self._metrics:
                self._metrics[path] = EndpointMetrics(path=path)
            return self._metrics[path]

    def record(self, path: str, latency: float, success: bool, error_type: str = "") -> None:
        m = self.get_or_create(path)
        m.record(latency, success, error_type)
        if not success:
            self._last_error_at = time.time()
            self._last_error_msg = error_type

    @property
    def status(self) -> str:
        """Overall service health status."""
        all_metrics = list(self._metrics.values())
        if not all_metrics:
            return "healthy"
        # Check error rates
        for m in all_metrics:
            degraded_threshold, unhealthy_threshold = latency_thresholds(m.path)
            if m.recent_error_rate >= ERROR_RATE_UNHEALTHY:
                return "unhealthy"
            if m.p95_latency >= unhealthy_threshold:
                return "unhealthy"
        for m in all_metrics:
            degraded_threshold, _ = latency_thresholds(m.path)
            if m.recent_error_rate >= ERROR_RATE_DEGRADED:
                return "degraded"
            if m.p95_latency >= degraded_threshold:
                return "degraded"
        return "healthy"

    @property
    def degradation_reasons(self) -> list[dict[str, Any]]:
        """Structured reasons explaining degraded or unhealthy status."""
        reasons: list[dict[str, Any]] = []
        for m in list(self._metrics.values()):
            if m.recent_error_rate >= ERROR_RATE_UNHEALTHY:
                reasons.append({
                    "path": m.path,
                    "reason": "error_rate",
                    "severity": "unhealthy",
                    "value": round(m.recent_error_rate, 4),
                    "threshold": ERROR_RATE_UNHEALTHY,
                })
            elif m.recent_error_rate >= ERROR_RATE_DEGRADED:
                reasons.append({
                    "path": m.path,
                    "reason": "error_rate",
                    "severity": "degraded",
                    "value": round(m.recent_error_rate, 4),
                    "threshold": ERROR_RATE_DEGRADED,
                })
            degraded_threshold, unhealthy_threshold = latency_thresholds(m.path)
            if m.p95_latency >= unhealthy_threshold:
                reasons.append({
                    "path": m.path,
                    "reason": "p95_latency_ms",
                    "severity": "unhealthy",
                    "value": round(m.p95_latency * 1000, 1),
                    "threshold": round(unhealthy_threshold * 1000, 1),
                })
            elif m.p95_latency >= degraded_threshold:
                reasons.append({
                    "path": m.path,
                    "reason": "p95_latency_ms",
                    "severity": "degraded",
                    "value": round(m.p95_latency * 1000, 1),
                    "threshold": round(degraded_threshold * 1000, 1),
                })
        severity_rank = {"unhealthy": 0, "degraded": 1}
        return sorted(reasons, key=lambda item: (severity_rank.get(str(item.get("severity")), 9), str(item.get("path", ""))))

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self._started_at

    @property
    def last_error(self) -> dict[str, Any] | None:
        if self._last_error_at is None:
            return None
        return {
            "at": self._last_error_at,
            "seconds_ago": time.time() - self._last_error_at,
            "message": self._last_error_msg,
        }

    def snapshot(self) -> dict[str, Any]:
        """Full health snapshot for GET /v1/ai/health."""
        with self._lock:
            endpoints = [m.to_dict() for m in self._metrics.values()]
        return {
            "status": self.status,
            "uptime_seconds": self.uptime_seconds,
            "endpoints": endpoints,
            "degradation_reasons": self.degradation_reasons,
            "last_error": self.last_error,
        }


# Singleton
_health_monitor = HealthMonitor()


def get_health_monitor() -> HealthMonitor:
    return _health_monitor
