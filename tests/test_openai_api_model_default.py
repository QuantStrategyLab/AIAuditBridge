"""Regression: OpenAI API defaults resolve from live catalog, not retired literals."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from service.adapters.llm_adapter import default_openai_model, resolve_model
from service.automation_decision import (
    EXECUTION_HUMAN_REVIEW,
    EXECUTION_RUN,
    MODE_REVIEW_AND_FIX,
    decide_automation_execution,
    default_low_cost_model,
)
from service.automation_run_ledger import CONTROL_CONTINUE, CONTROL_PAUSE_AUTO_FIX
from service.model_catalog import (
    ModelRecord,
    TierAssignment,
    allow_catalog_parent,
    load_catalog,
    save_catalog_atomic,
)
from service.model_catalog_sync import bootstrap_records, build_catalog
from service.model_resolver import reset_catalog_cache, resolve_openai_api_model
from service.model_router import default_dual_review_model_for_reviewer

_RETIRED_LEGACY = frozenset({"gpt-5.4", "gpt-5.4-mini"})
_CODEX_ROSTER = frozenset({"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"})


def _assert_openai_api_default(selected: str, catalog) -> None:
    assert selected
    assert selected.lower() not in {"auto", "tier:auto"}
    assert selected not in _RETIRED_LEGACY
    record = catalog.models.get(selected)
    assert record is not None
    assert str(record.provider).lower() == "openai"


class OpenAIApiModelDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        repo_catalog = Path(__file__).resolve().parents[1] / "generated" / "model_catalog.json"
        if not repo_catalog.is_file():
            raise unittest.SkipTest("generated/model_catalog.json missing")
        cls._catalog_path = str(repo_catalog)

    def setUp(self) -> None:
        reset_catalog_cache()
        self._env = patch.dict(
            os.environ,
            {"MODEL_CATALOG_PATH": self._catalog_path},
            clear=False,
        )
        self._env.start()
        os.environ.pop("OPENAI_MODEL", None)
        os.environ.pop("DUAL_REVIEW_GPT_MODEL", None)

    def tearDown(self) -> None:
        reset_catalog_cache()
        self._env.stop()

    def test_resolve_openai_api_model_skips_retired_tier_pins(self) -> None:
        catalog = load_catalog(Path(self._catalog_path))
        selected = resolve_openai_api_model(preferred_tier="fast", prefer_low_cost=True)
        _assert_openai_api_default(selected, catalog)
        self.assertNotEqual(selected, catalog.tiers["fast"].model)

    def test_explicit_openai_model_env_is_preserved(self) -> None:
        with patch.dict(os.environ, {"OPENAI_MODEL": "gpt-5.5-pro"}, clear=False):
            self.assertEqual(default_openai_model(), "gpt-5.5-pro")
            self.assertEqual(resolve_model("gpt-5.5-pro"), ("openai", "gpt-5.5-pro"))

    def test_llm_default_openai_uses_catalog_not_retired_literal(self) -> None:
        catalog = load_catalog(Path(self._catalog_path))
        selected = default_openai_model()
        _assert_openai_api_default(selected, catalog)

    def test_dual_review_gpt_fallback_uses_openai_catalog(self) -> None:
        catalog = load_catalog(Path(self._catalog_path))
        with patch("service.model_router.route_model", return_value={"model": "claude-sonnet-4-6"}):
            selected = default_dual_review_model_for_reviewer("gpt")
        _assert_openai_api_default(selected, catalog)

    def test_dual_review_explicit_env_override_wins(self) -> None:
        with patch.dict(os.environ, {"DUAL_REVIEW_GPT_MODEL": "gpt-5.5-pro"}, clear=False), patch(
            "service.model_router.route_model",
            return_value={"model": "claude-sonnet-4-6"},
        ):
            self.assertEqual(default_dual_review_model_for_reviewer("gpt"), "gpt-5.5-pro")

    def test_low_quota_without_policy_model_uses_catalog_openai(self) -> None:
        catalog = load_catalog(Path(self._catalog_path))
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_PAUSE_AUTO_FIX,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"low_cost_provider": "openai"}},
        )
        self.assertEqual(result["action"], EXECUTION_RUN)
        self.assertEqual(result["effective_provider"], "openai")
        _assert_openai_api_default(result["effective_model"], catalog)

    def test_explicit_policy_low_cost_model_is_preserved(self) -> None:
        result = decide_automation_execution(
            repo="QuantStrategyLab/AIAuditBridge",
            requested_mode=MODE_REVIEW_AND_FIX,
            control_action=CONTROL_PAUSE_AUTO_FIX,
            service_health="healthy",
            quota_status="low",
            org_health_status="ok",
            policy={"default": {"low_cost_model": "gpt-5.4-mini"}},
        )
        self.assertEqual(result["effective_model"], "gpt-5.4-mini")

    def test_missing_openai_catalog_models_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            catalog = build_catalog(bootstrap_records())
            catalog.models = {
                model_id: record
                for model_id, record in catalog.models.items()
                if str(record.provider).lower() != "openai"
            }
            catalog.models["claude-sonnet-4-6"] = ModelRecord(
                model_id="claude-sonnet-4-6",
                provider="anthropic",
                capability_score=0.9,
                input_cost_per_1m=3.0,
                output_cost_per_1m=15.0,
            )
            for tier_name, effort in (
                ("nano", "low"),
                ("fast", "low"),
                ("standard", "medium"),
                ("capable", "high"),
                ("flagship", "xhigh"),
            ):
                catalog.tiers[tier_name] = TierAssignment(
                    tier=tier_name,
                    model="claude-sonnet-4-6",
                    provider="anthropic",
                    effort=effort,
                )
            save_catalog_atomic(catalog, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            with self.assertRaisesRegex(RuntimeError, "no usable OpenAI API model"):
                resolve_openai_api_model(prefer_low_cost=True)
            with self.assertRaisesRegex(RuntimeError, "no usable OpenAI API model"):
                default_low_cost_model()

            result = decide_automation_execution(
                repo="QuantStrategyLab/AIAuditBridge",
                requested_mode=MODE_REVIEW_AND_FIX,
                control_action=CONTROL_CONTINUE,
                service_health="healthy",
                quota_status="low",
                org_health_status="ok",
                policy={"default": {"low_cost_provider": "openai"}},
            )
            self.assertEqual(result["action"], EXECUTION_HUMAN_REVIEW)
            self.assertTrue(
                any("low-cost OpenAI model unavailable" in reason for reason in result["reasons"])
            )

    def test_api_default_may_select_openai_api_only_model_not_codex_roster(self) -> None:
        """API defaults must not be constrained to the Codex research roster."""
        with tempfile.TemporaryDirectory() as tmp:
            allow_catalog_parent(Path(tmp))
            path = Path(tmp) / "model_catalog.json"
            catalog = build_catalog(bootstrap_records())
            catalog.models["gpt-4.1-nano"] = ModelRecord(
                model_id="gpt-4.1-nano",
                provider="openai",
                capability_score=0.2,
                input_cost_per_1m=0.05,
                output_cost_per_1m=0.2,
            )
            for tier_name, effort in (
                ("nano", "low"),
                ("fast", "low"),
                ("standard", "medium"),
                ("capable", "high"),
                ("flagship", "xhigh"),
            ):
                catalog.tiers[tier_name] = TierAssignment(
                    tier=tier_name,
                    model="gpt-4.1-nano",
                    provider="openai",
                    effort=effort,
                )
            # Keep roster models present but higher cost so low-cost prefers API-only.
            save_catalog_atomic(catalog, path)
            os.environ["MODEL_CATALOG_PATH"] = str(path)
            reset_catalog_cache()

            selected = resolve_openai_api_model(preferred_tier="nano", prefer_low_cost=True)
            self.assertEqual(selected, "gpt-4.1-nano")
            self.assertNotIn(selected, _CODEX_ROSTER)


if __name__ == "__main__":
    unittest.main()
