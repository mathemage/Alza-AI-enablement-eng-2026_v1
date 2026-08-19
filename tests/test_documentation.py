import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DESIGN = ROOT / "docs" / "design.md"
TEST_PLAN = ROOT / "docs" / "test-plan.md"
OPERATIONS = ROOT / "docs" / "operations.md"
PRESENTATION = ROOT / "docs" / "presentation.md"
RUNBOOK = ROOT / "docs" / "demo-runbook.md"


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
            "GET /health",
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
            "DOC-02",
            "DESIGN-01",
            "OPS-01",
            "DEMO-01",
            "FINAL-01",
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

    def test_issue_14_doc_02_design_01_ops_01_demo_01_final_01(self) -> None:
        problems: list[str] = []
        documents: dict[Path, str] = {}
        for path in (README, DESIGN, TEST_PLAN, OPERATIONS, PRESENTATION, RUNBOOK):
            if path.is_file():
                documents[path] = path.read_text(encoding="utf-8")
            else:
                problems.append(f"DOC-02 missing:{path.relative_to(ROOT)}")

        design = documents.get(DESIGN, "")
        if (
            "Status: deployed MVP baseline finalized through backlog item 14."
            not in design
        ):
            problems.append("DESIGN-01 stale:docs/design.md status")

        markdown = tuple(ROOT.glob("*.md")) + tuple((ROOT / "docs").glob("*.md"))
        mermaid_owners = [
            path.relative_to(ROOT)
            for path in markdown
            if "```mermaid" in path.read_text(encoding="utf-8")
        ]
        mermaid_count = sum(
            path.read_text(encoding="utf-8").count("```mermaid") for path in markdown
        )
        if mermaid_owners != [Path("docs/design.md")] or mermaid_count != 1:
            problems.append(
                f"DESIGN-01 mermaid:expected docs/design.md only, got {mermaid_owners}"
            )
        diagram_match = re.search(r"```mermaid\n(.*?)```", design, flags=re.DOTALL)
        diagram = diagram_match.group(1) if diagram_match else ""
        if "/healthz" in diagram:
            problems.append("DESIGN-01 diagram:legacy /healthz route")

        readme = documents.get(README, "")
        readme_headings = re.findall(r"^## .+$", readme, flags=re.MULTILINE)
        if readme_headings != [
            "## Prerequisites",
            "## Verify locally",
            "## Deploy and operate",
            "## Authoritative documents",
        ]:
            problems.append("DOC-02 README.md:non-minimal or missing sections")
        for target in (
            "docs/design.md",
            "docs/test-plan.md",
            "docs/operations.md",
            "docs/presentation.md",
            "docs/demo-runbook.md",
        ):
            if target not in readme:
                problems.append(f"DOC-02 README.md:missing link:{target}")

        for term in (
            "gmail-notifications",
            "gmail-notifications-push",
            "/health",
            "/events/gmail",
            "email-work",
            "email-work-push",
            "/jobs/process-message",
            "dead-letter-monitor",
            "/jobs/renew-watch",
            "/jobs/reconcile-unread",
            "Firestore",
            "scratch",
            "Secret Manager",
            "Gemini",
            "Google Search",
            "OpenRouter",
            "openrouter:web_search",
            "Cloud Logging",
            "Cloud Monitoring",
        ):
            if term not in diagram:
                problems.append(f"DESIGN-01 diagram:missing:{term}")
        for term in (
            "alza-ai-00005-cfq",
            "sha256:cf2013a13a82847e48812282a4217bd624e8e3ff6f45c313ad8ed2ced938957f",
            "HTTP startup probe",
            "https://cloud.google.com/run/docs/configuring/healthchecks",
        ):
            if term not in design:
                problems.append(
                    f"DESIGN-01 docs/design.md:missing deployed fact:{term}"
                )

        operations = documents.get(OPERATIONS, "")
        for term in (
            "OAuth",
            "renew-watch",
            "reconcile-unread",
            "replay",
            "terminal_error",
            "dead-letter-monitor",
            "RESPONSE_PROVIDER",
            "quota",
            "budget alert",
            "Rollback",
            "users.stop",
            "terraform destroy",
            "Residual data",
            "Firestore",
            "Pub/Sub",
            "scratch",
            "Artifact Registry",
            "secret versions",
            "logs",
            "metrics",
            "APIs",
            "OAuth grant",
            "local Terraform state",
            "IDENTITY_TOKEN",
            "GET /health",
            '{"status":"ok"}',
            "HTTP startup probe",
            "same-project internal",
        ):
            if term.casefold() not in operations.casefold():
                problems.append(f"OPS-01 docs/operations.md:missing:{term}")

        presentation = documents.get(PRESENTATION, "")
        for term in (
            "# Gmail Assistant MVP",
            "Five-case proof",
            "Privacy",
            "Reliability",
            "Limitations",
            "Costs",
            "Operations and teardown",
            "HTTP startup probe",
        ):
            if term not in presentation:
                problems.append(f"DEMO-01 docs/presentation.md:missing:{term}")

        runbook = documents.get(RUNBOOK, "")
        ranges = re.findall(r"\| `(\d{2}):(\d{2})-(\d{2}):(\d{2})` \|", runbook)
        if ranges:
            starts = [int(a) * 60 + int(b) for a, b, _, _ in ranges]
            ends = [int(c) * 60 + int(d) for _, _, c, d in ranges]
            contiguous = starts[0] == 0 and all(
                start == previous for start, previous in zip(starts[1:], ends)
            )
            duration = ends[-1] / 60
            if not contiguous or not 10 <= duration <= 15:
                problems.append(
                    "DEMO-01 docs/demo-runbook.md:timeline must be contiguous 10-15m"
                )
        else:
            problems.append("DEMO-01 docs/demo-runbook.md:missing parseable timeline")
        for term in (
            "Preflight",
            "plain",
            "PDF",
            "MP3",
            "WAV",
            "JPEG",
            "PNG",
            "forced-current",
            "Expected outcome",
            "Sanitized fallback",
            "read-only",
            "historical",
            "operations.md#routine-read-only-checks",
            "operations.md#ordered-teardown",
            "Limitations",
            "Costs",
            "Teardown",
            "GET /health",
            "startup probe",
            '`200 {"status":"ok"}`',
        ):
            if term.casefold() not in runbook.casefold():
                problems.append(f"DEMO-01 docs/demo-runbook.md:missing:{term}")

        project_config = (
            (ROOT / "pyproject.toml").read_text(encoding="utf-8").casefold()
        )
        for forbidden in ("marp", "pandoc", "weasyprint", "reportlab", "reveal.js"):
            if forbidden in project_config:
                problems.append(
                    f"DEMO-01 pyproject.toml:PDF/presentation tooling:{forbidden}"
                )
        if (ROOT / "package.json").exists():
            problems.append("DEMO-01 package.json:frontend tooling is out of scope")

        source = (ROOT / "src" / "alza_ai" / "main.py").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        infrastructure = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")
        if (
            '@application.get("/health")' not in source
            or '@application.get("/healthz")' in source
        ):
            problems.append("FINAL-01 src/alza_ai/main.py:health route is stale")
        if "http://127.0.0.1:8080/health" not in workflow or "/healthz" in workflow:
            problems.append("FINAL-01 .github/workflows/ci.yml:health smoke is stale")
        for term in ("startup_probe {", 'path = "/health"', "port = 8080"):
            if term not in infrastructure:
                problems.append(f"FINAL-01 infra/main.tf:missing:{term}")

        self.assertFalse(problems, "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
