# Alza AI Gmail Assistant

A backend-only assistant for one dedicated Gmail mailbox. It processes eligible
messages and supported attachments, optionally uses native live search, and sends one
concise reply in the original thread.

## Prerequisites

Python `3.14`, `uv 0.12.5`, Terraform `1.15.8`, Docker, Git, and the `gcloud` CLI are
required for local and deployed verification. Deployment also requires an explicitly
selected and authenticated GCP project, billing account, region, and dedicated Gmail
mailbox.

## Verify locally

```text
uv sync --locked
uv run pytest tests/integration -q
uv run pytest --cov=alza_ai --cov-report=term-missing --cov-fail-under=85 -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
terraform fmt -check -recursive
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra validate
terraform -chdir=infra test
```

## Deploy and operate

Terraform starts in `infra/`; CI never applies it. Use only reviewed, explicit
operator inputs and an immutable image digest. Follow [operations](docs/operations.md)
for deployment checks, routine work, rollback, and teardown.

## Authoritative documents

- [System design](docs/design.md)
- [Test and acceptance plan](docs/test-plan.md)
- [Operations and teardown](docs/operations.md)
- [Presentation](docs/presentation.md)
- [10-15 minute demo runbook](docs/demo-runbook.md)
