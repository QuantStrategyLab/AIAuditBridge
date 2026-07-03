"""Tests for service/health.py — health monitoring and endpoint metrics."""

from __future__ import annotations

import time
import unittest

from service.health import (
    ERROR_RATE_DEGRADED,
    ERROR_RATE_UNHEALTHY,
    EndpointMetrics,
    HealthMonitor,
    _percentile,
    get_health_monitor,
)


class TestPercentile(unittest.TestCase):
    """Percentile calculation utility."""

    def test_returns_zero_for_empty_data(self) -> None:
        self.assertEqual(_percentile([], 50), 0.0)

    def test_p50_of_single_value(self) -> None:
        self.assertEqual(_percentile([10.0], 50), 10.0)

    def test_p50_of_sorted_values(self) -> None:
        self.assertAlmostEqual(_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50), 3.0)

    def test_p95_of_values(self) -> None:
        data = list(range(1, 101))  # 1..100
        # p95 ≈ 95.05
        p95 = _percentile(data, 95)
        self.assertGreater(p95, 94)
        self.assertLess(p95, 96)


class TestEndpointMetrics(unittest.TestCase):
    """Per-endpoint metric tracking."""

    def setUp(self) -> None:
        self.metrics = EndpointMetrics(path="/v1/ai/analyze")

    def test_initial_state(self) -> None:
        self.assertEqual(self.metrics.total_requests, 0)
        self.assertEqual(self.metrics.error_rate, 0.0)
        self.assertEqual(self.metrics.p50_latency, 0.0)
        self.assertEqual(self.metrics.p95_latency, 0.0)

    def test_record_success(self) -> None:
        self.metrics.record(latency=0.5, success=True)
        self.assertEqual(self.metrics.total_requests, 1)
        self.assertEqual(self.metrics.success_count, 1)
        self.assertEqual(self.metrics.error_count, 0)

    def test_record_error(self) -> None:
        self.metrics.record(latency=1.0, success=False, error_type="timeout")
        self.assertEqual(self.metrics.total_requests, 1)
        self.assertEqual(self.metrics.success_count, 0)
        self.assertEqual(self.metrics.error_count, 1)

    def test_error_rate_calculation(self) -> None:
        self.metrics.record(latency=0.1, success=True)
        self.metrics.record(latency=0.2, success=False)
        self.metrics.record(latency=0.3, success=False)
        self.assertAlmostEqual(self.metrics.error_rate, 2 / 3)

    def test_latency_percentiles(self) -> None:
        for i in range(1, 101):
            self.metrics.record(latency=float(i), success=True)
        self.assertAlmostEqual(self.metrics.p50_latency, 50.0, delta=1.0)
        self.assertAlmostEqual(self.metrics.p95_latency, 95.0, delta=2.0)

    def test_to_dict_includes_all_fields(self) -> None:
        self.metrics.record(latency=0.5, success=True)
        d = self.metrics.to_dict()
        self.assertEqual(d["path"], "/v1/ai/analyze")
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["success"], 1)
        self.assertEqual(d["errors"], 0)
        self.assertIn("error_rate", d)
        self.assertIn("p50_ms", d)
        self.assertIn("p95_ms", d)
        self.assertIn("p99_ms", d)


class TestHealthMonitor(unittest.TestCase):
    """HealthMonitor state management."""

    def setUp(self) -> None:
        self.monitor = HealthMonitor()

    def test_initial_status_healthy(self) -> None:
        self.assertEqual(self.monitor.status, "healthy")

    def test_status_healthy_with_successful_requests(self) -> None:
        self.monitor.record("/v1/ai/analyze", latency=0.5, success=True)
        self.assertEqual(self.monitor.status, "healthy")

    def test_status_degraded_on_high_error_rate(self) -> None:
        for _ in range(int(ERROR_RATE_DEGRADED * 100)):
            self.monitor.record("/v1/ai/analyze", latency=0.1, success=False)
        for _ in range(int((1 - ERROR_RATE_DEGRADED) * 100) - 1):
            self.monitor.record("/v1/ai/analyze", latency=0.1, success=True)
        self.assertEqual(self.monitor.status, "degraded")
        self.assertEqual(self.monitor.degradation_reasons[0]["reason"], "error_rate")

    def test_status_unhealthy_on_very_high_error_rate(self) -> None:
        for _ in range(int(ERROR_RATE_UNHEALTHY * 100)):
            self.monitor.record("/v1/ai/analyze", latency=0.1, success=False)
        for _ in range(int((1 - ERROR_RATE_UNHEALTHY) * 100)):
            self.monitor.record("/v1/ai/analyze", latency=0.1, success=True)
        self.assertEqual(self.monitor.status, "unhealthy")

    def test_uptime_increases(self) -> None:
        uptime = self.monitor.uptime_seconds
        time.sleep(0.01)
        self.assertGreaterEqual(self.monitor.uptime_seconds, uptime)

    def test_last_error_returns_none_initially(self) -> None:
        self.assertIsNone(self.monitor.last_error)

    def test_last_error_after_failure(self) -> None:
        self.monitor.record("/v1/ai/analyze", latency=0.5, success=False, error_type="timeout")
        self.assertIsNotNone(self.monitor.last_error)
        self.assertIn("message", self.monitor.last_error)
        self.assertEqual(self.monitor.last_error["message"], "timeout")

    def test_snapshot_returns_all_fields(self) -> None:
        self.monitor.record("/v1/ai/analyze", latency=0.5, success=True)
        snap = self.monitor.snapshot()
        self.assertIn("status", snap)
        self.assertIn("uptime_seconds", snap)
        self.assertIn("endpoints", snap)
        self.assertIn("degradation_reasons", snap)
        self.assertIn("last_error", snap)

    def test_snapshot_explains_latency_degradation(self) -> None:
        self.monitor.record("/v1/ai/execute/jobs", latency=31.0, success=True)
        snap = self.monitor.snapshot()
        self.assertEqual(snap["status"], "degraded")
        self.assertEqual(snap["degradation_reasons"][0]["reason"], "p95_latency_ms")

    def test_degradation_reasons_prioritize_unhealthy(self) -> None:
        self.monitor.record("/v1/ai/degraded", latency=31.0, success=True)
        self.monitor.record("/v1/ai/unhealthy", latency=121.0, success=True)
        reasons = self.monitor.degradation_reasons
        self.assertEqual(reasons[0]["severity"], "unhealthy")

    def test_multiple_endpoints_tracked_independently(self) -> None:
        self.monitor.record("/v1/ai/analyze", latency=0.5, success=True)
        self.monitor.record("/v1/ai/execute/jobs", latency=2.0, success=False)
        snap = self.monitor.snapshot()
        self.assertEqual(len(snap["endpoints"]), 2)

    def test_get_or_create_returns_same_instance(self) -> None:
        m1 = self.monitor.get_or_create("/v1/ai/analyze")
        m2 = self.monitor.get_or_create("/v1/ai/analyze")
        self.assertIs(m1, m2)


class TestHealthMonitorSingleton(unittest.TestCase):
    """Global health monitor instance."""

    def test_get_health_monitor_returns_singleton(self) -> None:
        m1 = get_health_monitor()
        m2 = get_health_monitor()
        self.assertIs(m1, m2)
