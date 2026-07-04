import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from service import org_health
from service.ai_gateway_service import AiGatewayRequestHandler
from service.org_health import read_org_health


class OrgHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        org_health._CACHE.clear()
        org_health._REFRESH_EVENTS.clear()

    def test_branch_scope_scans_all_branches_unless_scope_is_explicit(self) -> None:
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_ORG_HEALTH_BRANCH": ""}, clear=False):
            self.assertEqual(org_health._branch_scope("main"), "")
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_ORG_HEALTH_BRANCH": "all"}, clear=False):
            self.assertEqual(org_health._branch_scope("main"), "")
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_ORG_HEALTH_BRANCH": "default"}, clear=False):
            self.assertEqual(org_health._branch_scope("main"), "main")
        with patch.dict(os.environ, {"CODEX_AUDIT_SERVICE_ORG_HEALTH_BRANCH": "release"}, clear=False):
            self.assertEqual(org_health._branch_scope("main"), "release")

    def test_read_org_health_without_token_is_available_but_empty(self) -> None:
        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "",
            "GITHUB_TOKEN": "",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "repo-a, QuantStrategyLab/repo-b",
        }, clear=False):
            result = read_org_health()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["provider"]["status"], "unavailable")
        self.assertEqual(result["provider"]["reason"], "needs_token")
        self.assertEqual(result["summary"]["total_repositories"], 2)
        self.assertEqual(result["repositories"], [])

    def test_read_org_health_does_not_use_default_github_token_for_org_scope(self) -> None:
        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "",
            "GITHUB_TOKEN": "workflow-token",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "repo-a",
        }, clear=False):
            result = read_org_health()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["provider"]["reason"], "needs_token")

    def test_read_org_health_counts_failed_and_running_repos(self) -> None:
        responses = {
            "/repos/QuantStrategyLab/good": {"default_branch": "trunk"},
            "/repos/QuantStrategyLab/good/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 1, "state": "active"}]},
            "/repos/QuantStrategyLab/good/actions/workflows/1/runs?per_page=5": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/good"}]
            },
            "/repos/QuantStrategyLab/bad": {"default_branch": "main"},
            "/repos/QuantStrategyLab/bad/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 2, "state": "active"}]},
            "/repos/QuantStrategyLab/bad/actions/workflows/2/runs?per_page=5": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "failure", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/bad"}]
            },
            "/repos/QuantStrategyLab/busy": {"default_branch": "main"},
            "/repos/QuantStrategyLab/busy/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 3, "state": "active"}]},
            "/repos/QuantStrategyLab/busy/actions/workflows/3/runs?per_page=5": {
                "workflow_runs": [{"name": "Deploy", "status": "in_progress", "conclusion": None, "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/busy"}]
            },
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "good,bad,busy",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["provider"]["status"], "available")
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["summary"]["total_repositories"], 3)
        self.assertEqual(result["summary"]["unhealthy_repositories"], 1)
        self.assertEqual(result["summary"]["degraded_repositories"], 0)
        self.assertEqual(result["summary"]["failed_workflow_runs"], 1)
        self.assertEqual(result["summary"]["in_progress_workflow_runs"], 1)
        self.assertEqual(result["repositories"][0]["latest_run"]["branch"], "trunk")
        self.assertEqual(result["repositories"][1]["latest_run"]["name"], "CI")

    def test_read_org_health_ignores_optional_skipped_workflow_conclusions(self) -> None:
        responses = {
            "/repos/QuantStrategyLab/mixed": {"default_branch": "main"},
            "/repos/QuantStrategyLab/mixed/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 61, "name": "CI", "state": "active"}, {"id": 62, "name": "Optional", "state": "active"}]},
            "/repos/QuantStrategyLab/mixed/actions/workflows/61/runs?per_page=5": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "created_at": "2026-07-04T00:05:00Z", "html_url": "https://example.test/mixed-ci"}]
            },
            "/repos/QuantStrategyLab/mixed/actions/workflows/62/runs?per_page=5": {
                "workflow_runs": [{"name": "Optional", "status": "completed", "conclusion": "skipped", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/mixed-optional"}]
            },
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "mixed",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["degraded_repositories"], 0)
        self.assertEqual(result["repositories"][0]["signals"]["current_degraded_workflow_runs"], 0)

    def test_read_org_health_ignores_workflows_without_runs(self) -> None:
        responses = {
            "/repos/QuantStrategyLab/mixed": {"default_branch": "main"},
            "/repos/QuantStrategyLab/mixed/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 41, "name": "CI", "state": "active"}, {"id": 42, "name": "Manual", "state": "active"}]},
            "/repos/QuantStrategyLab/mixed/actions/workflows/41/runs?per_page=5": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "created_at": "2026-07-04T00:05:00Z", "html_url": "https://example.test/mixed-ci"}]
            },
            "/repos/QuantStrategyLab/mixed/actions/workflows/42/runs?per_page=5": {"workflow_runs": []},
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "mixed",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary"]["degraded_repositories"], 0)
        self.assertEqual(result["repositories"][0]["status"], "healthy")
        self.assertEqual(result["repositories"][0]["signals"]["current_degraded_workflow_runs"], 0)

    def test_read_org_health_degrades_when_any_monitored_workflow_is_degraded(self) -> None:
        responses = {
            "/repos/QuantStrategyLab/mixed": {"default_branch": "main"},
            "/repos/QuantStrategyLab/mixed/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 31, "name": "CI", "state": "active"}, {"id": 32, "name": "Docs", "state": "active"}]},
            "/repos/QuantStrategyLab/mixed/actions/workflows/31/runs?per_page=5": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "created_at": "2026-07-04T00:05:00Z", "html_url": "https://example.test/mixed-ci"}]
            },
            "/repos/QuantStrategyLab/mixed/actions/workflows/32/runs?per_page=5": {
                "workflow_runs": [{"name": "Docs", "status": "completed", "conclusion": "stale", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/mixed-docs"}]
            },
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "mixed",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_WORKFLOWS": "all",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["summary"]["degraded_repositories"], 1)
        self.assertEqual(result["repositories"][0]["status"], "degraded")
        self.assertEqual(result["repositories"][0]["latest_run"]["name"], "CI")
        self.assertEqual(result["repositories"][0]["problem_run"]["name"], "Docs")
        self.assertEqual(result["repositories"][0]["signals"]["current_degraded_workflow_runs"], 1)

    def test_read_org_health_marks_unknown_when_completed_run_lookback_is_exhausted(self) -> None:
        running = [
            {"name": "CI", "status": "in_progress", "conclusion": None, "created_at": f"2026-07-04T00:0{i}:00Z", "html_url": f"https://example.test/running-{i}"}
            for i in range(5, 0, -1)
        ]
        responses = {
            "/repos/QuantStrategyLab/deep": {"default_branch": "main"},
            "/repos/QuantStrategyLab/deep/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 71, "name": "CI", "state": "active"}]},
            "/repos/QuantStrategyLab/deep/actions/workflows/71/runs?per_page=5": {"workflow_runs": running},
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "deep",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_RUN_LOOKBACK_PAGES": "1",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["summary"]["unknown_repositories"], 1)
        self.assertEqual(result["summary"]["degraded_repositories"], 0)
        self.assertEqual(result["summary"]["in_progress_workflow_runs"], 1)
        self.assertEqual(result["repositories"][0]["status"], "unknown")
        self.assertEqual(result["repositories"][0]["problem_run"]["lookback_exhausted"], True)
        self.assertEqual(result["repositories"][0]["signals"]["current_unknown_workflow_runs"], 1)

    def test_read_org_health_pages_until_latest_completed_run(self) -> None:
        running = [
            {"name": "CI", "status": "in_progress", "conclusion": None, "created_at": f"2026-07-04T00:0{i}:00Z", "html_url": f"https://example.test/running-{i}"}
            for i in range(5, 0, -1)
        ]
        responses = {
            "/repos/QuantStrategyLab/deep": {"default_branch": "main"},
            "/repos/QuantStrategyLab/deep/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 51, "name": "CI", "state": "active"}]},
            "/repos/QuantStrategyLab/deep/actions/workflows/51/runs?per_page=5": {"workflow_runs": running},
            "/repos/QuantStrategyLab/deep/actions/workflows/51/runs?per_page=5&page=2": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "failure", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/failed"}]
            },
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "deep",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["summary"]["failed_workflow_runs"], 1)
        self.assertEqual(result["summary"]["in_progress_workflow_runs"], 1)
        self.assertEqual(result["repositories"][0]["latest_run"]["url"], "https://example.test/running-5")
        self.assertEqual(result["repositories"][0]["problem_run"]["url"], "https://example.test/failed")

    def test_read_org_health_keeps_latest_run_after_recent_failure(self) -> None:
        responses = {
            "/repos/QuantStrategyLab/recovered": {"default_branch": "main"},
            "/repos/QuantStrategyLab/recovered/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 11, "state": "active"}, {"id": 12, "state": "active"}]},
            "/repos/QuantStrategyLab/recovered/actions/workflows/11/runs?per_page=5": {
                "workflow_runs": [{"name": "Docs", "status": "completed", "conclusion": "success", "created_at": "2026-07-04T00:03:00Z", "html_url": "https://example.test/recovered-docs"}]
            },
            "/repos/QuantStrategyLab/recovered/actions/workflows/12/runs?per_page=5": {
                "workflow_runs": [
                    {"name": "CI", "status": "in_progress", "conclusion": None, "created_at": "2026-07-04T00:02:00Z", "html_url": "https://example.test/recovered-ci-rerun"},
                    {"name": "CI", "status": "completed", "conclusion": "failure", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/recovered-ci"},
                ]
            },
        }

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            return FakeResponse(responses[request.full_url.removeprefix("https://api.github.com")])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "recovered",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertEqual(result["summary"]["failed_workflow_runs"], 1)
        self.assertEqual(result["summary"]["in_progress_workflow_runs"], 1)
        self.assertEqual(result["repositories"][0]["status"], "unhealthy")
        self.assertEqual(result["repositories"][0]["latest_run"]["name"], "Docs")
        self.assertEqual(result["repositories"][0]["problem_run"]["name"], "CI")
        self.assertEqual(result["repositories"][0]["signals"]["current_failed_workflow_runs"], 1)

    def test_read_org_health_reuses_cached_github_response(self) -> None:
        responses = {
            "/repos/QuantStrategyLab/cached": {"default_branch": "main"},
            "/repos/QuantStrategyLab/cached/actions/workflows?per_page=100&page=1": {"workflows": [{"id": 21, "state": "active"}]},
            "/repos/QuantStrategyLab/cached/actions/workflows/21/runs?per_page=5": {
                "workflow_runs": [{"name": "CI", "status": "completed", "conclusion": "success", "created_at": "2026-07-04T00:00:00Z", "html_url": "https://example.test/cached"}]
            },
        }
        call_paths: list[str] = []

        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self) -> bytes:
                return json.dumps(self.payload).encode("utf-8")

        def fake_urlopen(request, timeout=0):
            path = request.full_url.removeprefix("https://api.github.com")
            call_paths.append(path)
            return FakeResponse(responses[path])

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "cached",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_CACHE_SECONDS": "60",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            first = read_org_health()
            second = read_org_health()

        self.assertEqual(call_paths, [
            "/repos/QuantStrategyLab/cached",
            "/repos/QuantStrategyLab/cached/actions/workflows?per_page=100&page=1",
            "/repos/QuantStrategyLab/cached/actions/workflows/21/runs?per_page=5",
        ])
        self.assertEqual(first, second)

    def test_read_org_health_serves_stale_cache_during_refresh(self) -> None:
        cache_key = (("QuantStrategyLab/cached",), "CODEX_AUDIT_SERVICE_GITHUB_TOKEN", "token", "all")
        cached_result = {
            "status": "ok",
            "provider": {"status": "available", "source": "github_rest", "token_source": "CODEX_AUDIT_SERVICE_GITHUB_TOKEN"},
            "summary": {"total_repositories": 1, "unhealthy_repositories": 0, "degraded_repositories": 0, "failed_workflow_runs": 0, "in_progress_workflow_runs": 0},
            "repositories": [],
        }
        org_health._CACHE[cache_key] = (0, cached_result)
        org_health._REFRESH_EVENTS[cache_key] = threading.Event()

        def fake_urlopen(request, timeout=0):
            raise AssertionError("stale cache hit must not call GitHub")

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "cached",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_CACHE_SECONDS": "60",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            result = read_org_health()

        self.assertIs(result, cached_result)

    def test_read_org_health_waits_for_cold_cache_refresh(self) -> None:
        cache_key = (("QuantStrategyLab/cached",), "CODEX_AUDIT_SERVICE_GITHUB_TOKEN", "token", "all")
        cached_result = {
            "status": "ok",
            "provider": {"status": "available", "source": "github_rest", "token_source": "CODEX_AUDIT_SERVICE_GITHUB_TOKEN"},
            "summary": {"total_repositories": 1, "unhealthy_repositories": 0, "degraded_repositories": 0, "failed_workflow_runs": 0, "in_progress_workflow_runs": 0},
            "repositories": [],
        }
        org_health._REFRESH_EVENTS[cache_key] = threading.Event()

        def finish_refresh() -> None:
            org_health._store_cache(cache_key, cached_result, 60)

        def fake_urlopen(request, timeout=0):
            raise AssertionError("cold single-flight waiter must not call GitHub")

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "cached",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_CACHE_SECONDS": "60",
        }, clear=False), patch("service.org_health.urlopen", fake_urlopen):
            thread = threading.Thread(target=finish_refresh)
            thread.start()
            result = read_org_health(timeout_seconds=0.1)
            thread.join(timeout=1)

        self.assertIs(result, cached_result)

    def test_org_health_endpoint_requires_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_AUTH": "github_oidc",
            "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
        }, clear=False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/ai/org-health", timeout=5)
                self.assertEqual(ctx.exception.code, 401)
            finally:
                server.shutdown()
                server.server_close()

    def test_read_org_health_limits_default_monitored_workflows(self) -> None:
        workflows = [
            {"id": 1, "name": "CI", "state": "active"},
            {"id": 2, "name": "Docs", "state": "active"},
            {"id": 3, "name": "Codex PR Review", "state": "active"},
        ]
        selected = org_health._monitored_workflows(workflows)
        self.assertEqual([item["name"] for item in selected], ["CI", "Codex PR Review"])

    def test_read_org_health_serves_expired_stale_cache_and_refreshes_in_background(self) -> None:
        cache_key = (("QuantStrategyLab/cached",), "CODEX_AUDIT_SERVICE_GITHUB_TOKEN", "token", "all")
        cached_result = {
            "status": "ok",
            "provider": {"status": "available", "source": "github_rest", "token_source": "CODEX_AUDIT_SERVICE_GITHUB_TOKEN"},
            "summary": {"total_repositories": 1, "unhealthy_repositories": 0, "degraded_repositories": 0, "failed_workflow_runs": 0, "in_progress_workflow_runs": 0},
            "repositories": [],
        }
        refreshed_result = {**cached_result, "status": "degraded"}
        org_health._CACHE[cache_key] = (0, cached_result)
        started = threading.Event()

        def fake_build(*args, **kwargs):
            started.set()
            return refreshed_result

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "cached",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_CACHE_SECONDS": "60",
        }, clear=False), patch("service.org_health._build_org_health_snapshot", fake_build):
            result = read_org_health()
            self.assertIs(result, cached_result)
            self.assertTrue(started.wait(timeout=1))

        deadline = time.time() + 1
        while time.time() < deadline:
            cached = org_health._CACHE.get(cache_key)
            if cached and cached[1] is refreshed_result:
                break
            time.sleep(0.01)
        self.assertIs(org_health._CACHE[cache_key][1], refreshed_result)


    def test_read_org_health_returns_placeholder_for_large_cold_cache(self) -> None:
        cache_key = (("QuantStrategyLab/one", "QuantStrategyLab/two"), "CODEX_AUDIT_SERVICE_GITHUB_TOKEN", "token", "all")
        refreshed_result = {
            "status": "ok",
            "provider": {"status": "available", "source": "github_rest", "token_source": "CODEX_AUDIT_SERVICE_GITHUB_TOKEN"},
            "summary": {"total_repositories": 2, "unhealthy_repositories": 0, "unknown_repositories": 0, "degraded_repositories": 0, "failed_workflow_runs": 0, "in_progress_workflow_runs": 0},
            "repositories": [],
        }
        started = threading.Event()

        def fake_build(*args, **kwargs):
            started.set()
            return refreshed_result

        with patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "ghs_test",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "one,two",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_CACHE_SECONDS": "60",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_COLD_ASYNC_REPOSITORIES": "1",
        }, clear=False), patch("service.org_health._build_org_health_snapshot", fake_build):
            result = read_org_health()
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["provider"]["status"], "refreshing")
            self.assertEqual(result["provider"]["reason"], "cold_cache_refreshing")
            self.assertTrue(started.wait(timeout=1))

        deadline = time.time() + 1
        while time.time() < deadline:
            cached = org_health._CACHE.get(cache_key)
            if cached and cached[1] is refreshed_result:
                break
            time.sleep(0.01)
        self.assertIs(org_health._CACHE[cache_key][1], refreshed_result)

    def test_org_health_endpoint_returns_unavailable_without_github_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "CODEX_AUDIT_SERVICE_AUTH": "none",
            "CODEX_AUDIT_SERVICE_ALLOW_NO_AUTH_FOR_LOCAL_TESTS": "true",
            "CODEX_AUDIT_SERVICE_JOB_DIR": tmp,
            "CODEX_AUDIT_SERVICE_GITHUB_TOKEN": "",
            "GITHUB_TOKEN": "",
            "CODEX_AUDIT_SERVICE_ORG_HEALTH_REPOSITORIES": "AIAuditBridge",
        }, clear=False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), AiGatewayRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                response = urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/ai/org-health", timeout=5)
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["status"], "unavailable")
                self.assertEqual(payload["provider"]["reason"], "needs_token")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
