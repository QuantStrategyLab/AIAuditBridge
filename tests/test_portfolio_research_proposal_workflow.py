from __future__ import annotations

from pathlib import Path
import unittest


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github/workflows/portfolio_research_proposal_diagnosis.yml"


class PortfolioResearchProposalWorkflowTest(unittest.TestCase):
    def test_workflow_only_consumes_sanitized_readiness_and_comments_existing_issue(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("name: Portfolio Research Proposal Diagnosis", text)
        self.assertIn("cron: '10 12 * * 2-6'", text)
        self.assertNotIn("workflow_dispatch:", text)
        self.assertIn("QuantStrategyLab/UsEquitySnapshotPipelines", text)
        self.assertIn("portfolio-candidate-readiness.yml", text)
        self.assertIn("portfolio-candidate-readiness-*", text)
        self.assertIn("run_portfolio_research_proposal_diagnosis.py", text)
        self.assertIn("actions/create-github-app-token", text)
        self.assertIn("permission-actions: read", text)
        self.assertIn("permission-issues: write", text)
        self.assertIn("id-token: write", text)
        self.assertIn("portfolio-research-proposal-diagnosis-${{ github.run_id }}-${{ github.run_attempt }}", text)
        self.assertNotIn("workflow_dispatch", text)
        self.assertNotIn("gcloud", text.lower())
        self.assertNotIn("broker", text.lower())
        self.assertNotIn("placeorder", text.lower())
        self.assertNotIn("paper", text.lower())
        self.assertNotIn("shadow", text.lower())
        self.assertNotIn("live", text.lower())

    def test_oidc_identity_is_predeclared_for_the_next_controlled_service_deploy(self) -> None:
        expected_ref = (
            "QuantStrategyLab/AIAuditBridge/.github/workflows/"
            "portfolio_research_proposal_diagnosis.yml@refs/heads/main"
        )
        for relative_path in (
            ".github/workflows/vps_codex_service_ops.yml",
            "scripts/deploy_codex_audit_service.sh",
        ):
            with self.subTest(relative_path=relative_path):
                text = (WORKFLOW_PATH.parents[2] / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected_ref, text)


if __name__ == "__main__":
    unittest.main()
