import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "docs" / "design.md"
TEST_PLAN = ROOT / "docs" / "test-plan.md"


class DocumentationContractTests(unittest.TestCase):
    def test_frozen_architecture_and_acceptance_plan(self) -> None:
        self.assertTrue(TEST_PLAN.is_file(), "docs/test-plan.md is missing")
        self.assertTrue(DESIGN.is_file(), "docs/design.md is missing")

        design = DESIGN.read_text(encoding="utf-8")
        test_plan = TEST_PLAN.read_text(encoding="utf-8")

        design_headings = (
            "# Gmail Assistant Architecture",
            "## Scope and non-goals",
            "## System flow and data ownership",
            "## HTTP surface and authenticated callers",
            "## Domain models",
            "## Integration interfaces",
            "## Gmail and OAuth lifecycle",
            "## MIME and attachment policy",
            "## Provider and live-search policy",
            "## Transactional processing state machine",
            "## Synchronization, retry, and terminal-failure semantics",
            "## Regional infrastructure and IAM",
            "## Security, privacy, and observability",
            "## Cost, quota, and time controls",
            "## Delivery decisions",
        )
        test_plan_headings = (
            "# TDD Acceptance Plan",
            "## Purpose and authority",
            "## TDD evidence contract",
            "## Test levels and phase gates",
            "## Planned Red evidence",
            "## Delivery phase gates",
            "## Acceptance matrix",
            "## Issue 01 validation evidence",
        )

        for heading in design_headings:
            with self.subTest(document="design", heading=heading):
                self.assertIn(heading, design)
        for heading in test_plan_headings:
            with self.subTest(document="test-plan", heading=heading):
                self.assertIn(heading, test_plan)

        frozen_design_terms = (
            "GET /healthz",
            "POST /events/gmail",
            "POST /jobs/process-message",
            "POST /jobs/renew-watch",
            "POST /jobs/reconcile-unread",
            "gmail-notifications",
            "gmail-notifications-push",
            "email-work",
            "email-work-push",
            "dead-letter",
            "dead-letter-monitor",
            "InboundEmail",
            "Attachment",
            "AttachmentInsight",
            "Citation",
            "GeneratedReply",
            "GmailGateway",
            "AttachmentAnalyzer",
            "ReplyProvider",
            "WorkPublisher",
            "ProcessingStore",
            "processing",
            "send_pending",
            "sent",
            "completed",
            "terminal_error",
            "europe-west3",
            "https://www.googleapis.com/auth/gmail.modify",
            "RESPONSE_PROVIDER=gemini",
            "GEMINI_MODEL=gemini-3.6-flash",
            "RESPONSE_PROVIDER=openrouter",
            "OPENROUTER_MODEL=anthropic/claude-opus-5",
            "Google Search grounding",
            "openrouter:web_search",
            "five citations",
            "five attachments",
            "20 MiB",
            "24 MiB",
            "concurrency `2`",
            "Message-ID",
            "X-Alza-AI-Source-Message-ID",
            "In-Reply-To",
            "References",
            "AI/Processed",
            "AI/Error",
            "activated_at",
            "105s",
            "115s",
        )
        for term in frozen_design_terms:
            with self.subTest(document="design", term=term):
                self.assertIn(term, design)

        required_matrix_ids = (
            "MIME-01",
            "MIME-02",
            "ATT-03",
            "SEARCH-01",
            "CITE-01",
            "PROC-01",
            "PROC-03",
            "SYNC-02",
            "SYNC-03",
            "SEC-01",
            "PRIV-01",
            "OBS-01",
            "TIME-01",
            "COST-01",
            "LIVE-01",
        )
        for matrix_id in required_matrix_ids:
            with self.subTest(document="test-plan", matrix_id=matrix_id):
                self.assertIn(f"| {matrix_id} |", test_plan)

        for level in (
            "Unit",
            "Contract",
            "Terraform",
            "Integration",
            "Container",
            "Authenticated smoke",
            "Live Gmail acceptance",
            "85% line coverage",
            "Expected Red",
        ):
            with self.subTest(document="test-plan", level=level):
                self.assertIn(level, test_plan)

        evidence_terms = (
            "Expected Red, observed before `docs/design.md` existed",
            "Exit status: `1`",
            "FAILED (failures=1)",
            "Focused validation after Refactor",
            "Complete existing suite after Refactor",
            "Exit status: `0`",
            "Ran 1 test",
        )
        for term in evidence_terms:
            with self.subTest(document="test-plan", evidence=term):
                self.assertIn(term, test_plan)


if __name__ == "__main__":
    unittest.main()
