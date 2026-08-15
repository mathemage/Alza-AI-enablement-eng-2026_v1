# GCP Email AI Assistant Issue Backlog

## Product scope

Build a backend-only Gmail assistant that reads eligible messages from one dedicated
consumer Gmail mailbox, understands supported attachments, optionally grounds
time-sensitive answers with live web search, and sends one concise reply in the
original thread. The MVP is implemented entirely in Python; it has no browser UI,
frontend technology, full-thread conversational context, retrieval-augmented
generation (RAG), scraper, or separate search service.

The repository is already initialized on `main`, tracks `origin/main`, and contains an
existing `.gitignore`. It has no application implementation at the start of this
backlog.

## Fixed technical decisions

- Use Python `3.14`, FastAPI, `uv`, `pytest`, `httpx`, Ruff, mypy, Docker,
  Terraform, and GitHub Actions. Commit `uv.lock` and keep the implementation under
  `src/` with tests under `tests/`.
- Deploy one private Cloud Run service in `europe-west3` (Frankfurt). Place regional
  Firestore, the scratch Cloud Storage bucket, Artifact Registry, Cloud Scheduler,
  and user-managed Secret Manager replicas in `europe-west3`. This is a
  proximity-oriented choice for Prague, not a measured latency guarantee. The
  region is listed for [Cloud Run](https://cloud.google.com/run/docs/locations),
  [Firestore](https://docs.cloud.google.com/firestore/docs/locations), and
  [Cloud Storage](https://docs.cloud.google.com/storage/docs/bucket-locations).
- Expose only `GET /healthz`, `POST /events/gmail`,
  `POST /jobs/process-message`, `POST /jobs/renew-watch`, and
  `POST /jobs/reconcile-unread`. Cloud Run IAM authenticates every deployed request;
  push and Scheduler callers use Google OIDC identities, and the service has no
  public invoker or shared webhook secret.
- Use `InboundEmail`, `Attachment`, `AttachmentInsight`, `Citation`, and
  `GeneratedReply` as provider-neutral domain models. Keep integrations behind
  `GmailGateway`, `AttachmentAnalyzer`, `ReplyProvider`, `WorkPublisher`, and
  `ProcessingStore` interfaces.
- Create two primary Pub/Sub topic/subscription pairs: `gmail-notifications` with
  `gmail-notifications-push`, and `email-work` with `email-work-push`. Both use one
  shared `dead-letter` topic and `dead-letter-monitor` subscription. Grant
  `gmail-api-push@system.gserviceaccount.com` publisher access only to
  `gmail-notifications` and use dedicated authenticated push identities.
- Gmail notifications reach `POST /events/gmail`. That handler serializes mailbox
  history synchronization, publishes metadata-only work items, and advances its
  Firestore history cursor only after every publication succeeds. Work items reach
  `POST /jobs/process-message`. Cloud Scheduler calls
  `POST /jobs/renew-watch` daily and `POST /jobs/reconcile-unread` every five
  minutes.
- Firestore stores mailbox cursors, leases, attempts, processing states, deterministic
  outbound identifiers, and sanitized operational metadata only. It never stores
  message bodies, prompts, generated reply bodies, attachment bytes, extracted text,
  or transcripts. The scratch bucket has a one-day lifecycle backstop, and normal
  processing deletes every staged object in `finally`.
- Per-message states are `processing`, `send_pending`, `sent`, `completed`, and
  `terminal_error`. A retryable failure remains recoverable through a bounded lease;
  `send_pending` requires thread inspection before another send, and
  `terminal_error` is final.
- Authorize the dedicated consumer mailbox with installed-app OAuth and only the
  `https://www.googleapis.com/auth/gmail.modify` scope. Store the refresh credential
  in Secret Manager, never in Git or Terraform state. Preserve Gmail `threadId`, a
  matching `Subject`, and the `Message-ID`, `In-Reply-To`, and `References` headers
  needed for correct threading and recovery.
- Accept `PDF`, `MP3`, `WAV`, `JPEG`, and `PNG`. Allow at most five attachments,
  `20 MiB` per attachment, and `24 MiB` decoded in total. Analyze one attachment per
  Gemini request with concurrency `2`.
- Default to `RESPONSE_PROVIDER=gemini` and
  `GEMINI_MODEL=gemini-3.6-flash`. Allow explicit selection of
  `RESPONSE_PROVIDER=openrouter` with configurable
  `OPENROUTER_MODEL=anthropic/claude-opus-5`. Validate credentials only for the
  selected provider. Never perform application-level cross-provider fallback.
- Gemini uses its `global` endpoint and therefore makes no EU-only model-processing
  guarantee. The selected model and required inputs are described by the
  [Gemini 3.6 Flash model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-6-flash).
  When OpenRouter is selected, it may receive normalized email text,
  `AttachmentInsight` values, and search queries, but never original attachment
  bytes.
- Implement advanced Option C only: live web search with citations. Gemini uses
  provider-native Google Search grounding and OpenRouter uses
  `openrouter:web_search`. Permit at most one search-enabled response call and retain
  at most five deduplicated, validated citations. Do not implement full-thread
  context, RAG, a scraper, a separate search service, or deprecated OpenRouter search
  modes.
- Optimize for trial credit and minimal cost, not for a wholly free deployment. Use
  zero minimum Cloud Run instances, a default maximum of `2` instances, bounded
  model/output quotas, a `115s` request timeout, and a `105s` internal processing
  deadline. Budget alerts report spending but do not hard-cap or prevent it.
- Every implementation issue after Issue 01 follows
  `Spec → Red → Green → Refactor`, records the expected failing test before the
  implementation, and leaves the focused tests and complete test suite green. Issue
  01 is documentation and test planning only.

## Consecutive implementation issues

## Issue 01 — Freeze the architecture and TDD acceptance plan

**Depends on:** None.

**Description:**

Create `docs/design.md` and `docs/test-plan.md` before any application code. Freeze the
decisions needed by later issues: system flow, endpoints, interfaces, Firestore state
transitions, attachment limits, OAuth lifecycle, provider privacy boundaries, retry
and terminal-failure behavior, cost controls, delivery phase gates, and an acceptance
matrix that maps every requirement to a planned test level.

**Success criteria:**

- [ ] `docs/design.md` documents the primary push path, work queue, dead-letter path,
  daily watch renewal, five-minute unread reconciliation, authenticated callers,
  and data ownership at every boundary.
- [ ] `docs/design.md` fixes all endpoints, domain models, interfaces, provider
  selection rules, native search tools, citation limits, MIME limits, regional
  resources, and the prohibition on persisted raw content.
- [ ] The design defines a transactional processing state machine, lease and attempt
  semantics, deterministic outbound identity, ambiguous-send recovery, history
  cursor advancement, retryable failures, and terminal failures.
- [ ] The OAuth section covers dedicated-mailbox setup, offline consent, the
  `https://www.googleapis.com/auth/gmail.modify` scope, secure refresh-token
  storage, watch activation/renewal, revocation, and reauthorization.
- [ ] `docs/test-plan.md` defines unit, contract, Terraform, integration, container,
  authenticated smoke, and live Gmail acceptance gates, including expected Red
  evidence and an `85%` coverage threshold.
- [ ] The acceptance matrix covers plain text, HTML, nested MIME, all five attachment
  formats, live search and citations, threading, redelivery, concurrency,
  ambiguous sends, stale cursors, dropped notifications, cleanup, redaction,
  latency, and cost/security controls.
- [ ] Only documentation and documentation-focused validation are added; no
  application or infrastructure implementation is introduced.

**Prompt for an AI coding agent:**

```text
Implement only Issue 01, “Freeze the architecture and TDD acceptance plan.” This is a documentation-only issue: create `docs/design.md` and `docs/test-plan.md`, make every architecture decision needed by the later backlog, and do not add application or infrastructure implementation.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that every dependency listed above is merged before continuing; this issue has no dependency.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: write the acceptance outline in `docs/test-plan.md` before completing `docs/design.md`; keep both documents aligned with `plan.md` and change no application code.
4. Red: add and run a focused documentation validation check that initially fails because the required documents or sections are absent, and record the expected Red result. Do not commit a failing check.
5. Green: add the smallest complete documentation set that makes the focused validation pass and satisfies every success criterion above.
6. Refactor: remove duplication and ambiguity without expanding scope, then run the focused validation and the complete existing test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, or make unrelated changes.
```

## Issue 02 — Scaffold the Python service and CI

**Depends on:** Issue 01.

**Description:**

Create the smallest typed FastAPI service and deterministic local/CI toolchain. Begin
with a failing test for `GET /healthz`, then add `pyproject.toml`, committed `uv.lock`,
the `src/` package, `tests/`, `.env.example`, a minimal non-root Dockerfile, and a
GitHub Actions workflow. Audit the existing `.gitignore` instead of replacing it.

**Success criteria:**

- [ ] The committed project targets Python `3.14` and uses current compatible
  releases of FastAPI, `uv`, `pytest`, `httpx`, Ruff, and mypy with no frontend
  dependencies.
- [ ] A focused test proves `GET /healthz` returns `200` and a minimal stable JSON
  health payload; the test is observed failing before the route exists.
- [ ] `pyproject.toml`, `uv.lock`, `src/`, `tests/`, `.env.example`, the Dockerfile,
  and `.github/workflows/ci.yml` are minimal and sufficient for a clean checkout.
- [ ] The container runs as a non-root user and starts the same FastAPI application
  exercised by tests.
- [ ] `.gitignore` covers Python caches, virtual environments, Terraform state,
  `.terraform/`, credentials, tokens, `.env*`, and generated evidence while
  explicitly retaining `.env.example`.
- [ ] CI installs from the lock file and runs Ruff formatting/checks, mypy, and the
  complete `pytest` suite without cloud credentials or paid calls.

**Prompt for an AI coding agent:**

```text
Implement only Issue 02, “Scaffold the Python service and CI.” Build the minimal Python `3.14` FastAPI foundation, begin with `GET /healthz`, commit `uv.lock`, audit the existing `.gitignore`, and add no cloud integration or frontend code.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 01 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update the relevant sections of `docs/design.md` and `docs/test-plan.md` before implementation, including exact health behavior and local/CI commands.
4. Red: add and run a focused failing `GET /healthz` test before creating the route, and record the expected Red result. Do not commit a failing test.
5. Green: add the smallest scaffold and FastAPI implementation that makes the focused test pass.
6. Refactor: simplify without expanding scope, then run the focused test and the complete test suite, including Ruff and mypy, and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, or make unrelated changes.
```

## Issue 03 — Define and test GCP infrastructure

**Depends on:** Issue 02.

**Description:**

Add Terraform tests with mocked providers before defining the regional GCP
foundation. Provision APIs, Artifact Registry, a private Cloud Run service, regional
Firestore, scratch Cloud Storage, user-managed Secret Manager replicas, two primary
Pub/Sub paths and the shared dead-letter path, Scheduler jobs, IAM, quotas, budget
alerts, and bounded scaling. CI validates but never applies infrastructure.

**Success criteria:**

- [ ] Failing Terraform tests first assert `europe-west3`, private ingress/invocation,
  zero minimum instances, maximum instances `2`, a `115s` timeout, the one-day
  scratch lifecycle, and user-managed regional secret replication.
- [ ] Terraform defines exactly the two primary topic/subscription pairs and one
  shared dead-letter topic/subscription, with bounded retry/dead-letter settings.
- [ ] Dedicated runtime and invoker service accounts have least-privilege roles;
  `gmail-api-push@system.gserviceaccount.com` can publish only to
  `gmail-notifications`, and push/Scheduler calls use authenticated OIDC.
- [ ] Secret containers are created without secret payloads in configuration or
  Terraform state, and Firestore contains no schema that stores raw content.
- [ ] Model/output quotas, Cloud Run maximum instances, and budget alerts are
  configurable and documented as exposure controls rather than spending hard
  caps.
- [ ] `terraform fmt -check -recursive`, `terraform init -backend=false`,
  `terraform validate`, and `terraform test` pass in a clean environment; no CI
  workflow runs `terraform apply`.

**Prompt for an AI coding agent:**

```text
Implement only Issue 03, “Define and test GCP infrastructure.” Use Terraform mocked-provider tests first, keep regional resources in `europe-west3`, implement least-privilege authenticated paths, and never apply infrastructure in CI.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 02 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update the infrastructure, IAM, cost-control, and Terraform-test sections of `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused Terraform tests with mocked providers before defining the resources, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest Terraform implementation that makes those tests pass without applying infrastructure or placing secrets in state.
6. Refactor: simplify modules and permissions without expanding scope, then run the focused tests and the complete test suite, including `terraform fmt -check -recursive`, `terraform init -backend=false`, `terraform validate`, and `terraform test`, and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, apply infrastructure in CI, or make unrelated changes.
```

## Issue 04 — Implement the Gmail gateway and OAuth bootstrap

**Depends on:** Issue 03.

**Description:**

Define and test a fake `GmailGateway` before adding the Gmail API adapter and the
`uv run alza-ai oauth bootstrap` installed-app OAuth command. Implement watch setup,
history retrieval, complete message and attachment retrieval, label mutation, thread
inspection, and threaded sending. Mock every Gmail API call in normal tests.

**Success criteria:**

- [ ] Contract tests drive a deterministic fake `GmailGateway` before the real
  adapter and cover watch, history, retrieval, labels, thread inspection, and
  send behavior.
- [ ] `uv run alza-ai oauth bootstrap` requests offline consent with only
  `https://www.googleapis.com/auth/gmail.modify`, never logs credentials, and
  writes or uploads them only to an explicitly selected secure destination.
- [ ] Documentation covers consent-screen status, the seven-day refresh-token risk
  for an external app left in Testing, Production transition, revocation, and
  reauthorization.
- [ ] Threaded send preserves Gmail `threadId`, matching `Subject`, deterministic
  `Message-ID`, `In-Reply-To`, and `References` values.
- [ ] The adapter maps retryable and terminal Gmail failures to typed errors without
  leaking message content or credentials.
- [ ] All normal unit and contract tests mock Gmail; live mailbox tests remain
  explicit and opt-in.

**Prompt for an AI coding agent:**

```text
Implement only Issue 04, “Implement the Gmail gateway and OAuth bootstrap.” Drive a fake `GmailGateway` with contract tests, then add mocked Gmail API operations and `uv run alza-ai oauth bootstrap` using only `https://www.googleapis.com/auth/gmail.modify`.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 03 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update OAuth lifecycle, Gmail operations, threading headers, privacy, and gateway contract cases in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing fake-gateway and OAuth tests before the adapter and command exist, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest gateway, mocked adapter, typed errors, and OAuth bootstrap implementation that makes the focused tests pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite with every normal Gmail call mocked, and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, expose credentials, make live Gmail calls in default tests, or make unrelated changes.
```

## Issue 05 — Parse messages and supported MIME attachments

**Depends on:** Issue 04.

**Description:**

Build synthetic fixtures and a pure parser for Gmail messages before implementing
adapter integration. Recursively handle plain text, HTML-only, nested multipart,
base64url content, encoded headers, inline media, and supported attachments. Normalize
valid inputs into `InboundEmail` and reject malformed or oversized inputs with stable,
typed outcomes.

**Success criteria:**

- [ ] Red-first fixtures cover plain text, HTML-only, alternative bodies, adversarial
  nested multiparts, encoded headers, inline parts, and malformed base64url data.
- [ ] Separate license-safe fixtures cover `PDF`, `MP3`, `WAV`, `JPEG`, and `PNG`,
  including MIME declaration/content mismatches.
- [ ] Parsing produces `InboundEmail` with reply-relevant identifiers and
  `Attachment` values; plain text is preferred and HTML conversion performs no
  remote loading.
- [ ] The parser enforces at most five attachments, `20 MiB` per attachment, and
  `24 MiB` decoded total before model processing.
- [ ] Malformed, unsupported, or oversized content yields predictable warnings or
  typed terminal errors according to `docs/design.md`, never partial unsafe
  bytes.
- [ ] The parser is deterministic, performs no network I/O, and logs no raw content.

**Prompt for an AI coding agent:**

```text
Implement only Issue 05, “Parse messages and supported MIME attachments.” Build synthetic fixtures and a pure deterministic parser that produces `InboundEmail`, supports `PDF`, `MP3`, `WAV`, `JPEG`, and `PNG`, and enforces five attachments, `20 MiB` each, and `24 MiB` total.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 04 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update MIME parsing rules, attachment bounds, malformed-input behavior, and fixture coverage in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing fixture tests for plain text, HTML, nested multipart, every supported attachment type, malformed data, and every size/count boundary, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest pure parser and domain mapping that makes the focused tests pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, perform remote HTML loading, log raw content, or make unrelated changes.
```

## Issue 06 — Analyze attachments with Gemini multimodal input

**Depends on:** Issue 05.

**Description:**

Test fake storage and model adapters, including cleanup failures, before implementing
`AttachmentAnalyzer`. Stage each supported attachment in the regional scratch bucket,
invoke exactly one Gemini request per attachment with concurrency `2`, normalize the
result into `AttachmentInsight`, and delete temporary objects in `finally`. Retain the
one-day bucket lifecycle only as a cleanup safety net.

**Success criteria:**

- [ ] Contract tests cover fake scratch storage, fake Gemini calls, per-attachment
  outputs, partial model failure, upload failure, deletion failure, timeout, and
  cancellation.
- [ ] Every attachment is staged in the `europe-west3` scratch bucket with an opaque
  object name and is submitted in exactly one model request; processing
  concurrency never exceeds `2`.
- [ ] `AttachmentInsight` contains provider-neutral filename, media type, summary,
  extracted text or transcript, relevant facts, and warnings with bounded sizes.
- [ ] Temporary objects are deleted in `finally` after success or failure; deletion
  failures are sanitized and observable without masking the primary result.
- [ ] The one-day Cloud Storage lifecycle remains configured as a safety net rather
  than the normal cleanup mechanism.
- [ ] Attachment bytes and extracted content are never placed in Firestore, logs, or
  OpenRouter requests.

**Prompt for an AI coding agent:**

```text
Implement only Issue 06, “Analyze attachments with Gemini multimodal input.” Test fake storage and model adapters first, stage every attachment in regional Cloud Storage, issue one Gemini request per attachment with concurrency `2`, and always clean up in `finally`.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 05 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update attachment staging, Gemini input/output, concurrency, timeout, privacy, and cleanup cases in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing contract tests for fake storage/model adapters, every supported type, concurrency, partial failure, and cleanup failure, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest `AttachmentAnalyzer` implementation that makes the focused tests pass and returns provider-neutral `AttachmentInsight` values.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, persist raw content, send original bytes to OpenRouter, or make unrelated changes.
```

## Issue 07 — Add Gemini and OpenRouter reply providers

**Depends on:** Issue 06.

**Description:**

Write shared `ReplyProvider` contract tests before implementing the Gemini and
OpenRouter adapters. Select exactly one provider from configuration, validate only
that provider's credentials, and construct a provider-neutral `GeneratedReply`
client-side. Keep live search disabled until Issue 08 and never fall back between
providers at application level.

**Success criteria:**

- [ ] Shared contract tests run against deterministic Gemini and OpenRouter fakes and
  verify normalized reply text, safe HTML, provider/model metadata, bounded
  usage, and latency fields.
- [ ] `RESPONSE_PROVIDER=gemini` with
  `GEMINI_MODEL=gemini-3.6-flash` starts without an OpenRouter API key.
- [ ] `RESPONSE_PROVIDER=openrouter` requires its own API key and accepts configurable
  `OPENROUTER_MODEL`, defaulting to `anthropic/claude-opus-5`.
- [ ] Both adapters consume current email text and `AttachmentInsight` values and
  return the same `GeneratedReply` contract; the application constructs final
  reply alternatives and metadata client-side.
- [ ] OpenRouter receives no original attachment bytes, object URLs, credentials, or
  unneeded Gmail metadata.
- [ ] A selected-provider failure is surfaced with its typed retry classification;
  no code path invokes the other provider automatically.

**Prompt for an AI coding agent:**

```text
Implement only Issue 07, “Add Gemini and OpenRouter reply providers.” Drive both adapters from shared `ReplyProvider` contract tests, validate credentials only for the selected provider, build `GeneratedReply` client-side, and implement no application-level fallback.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 06 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update provider contracts, selected-provider configuration, privacy boundaries, reply construction, and failure behavior in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing shared provider contract tests before implementing either adapter, including proof that default Gemini startup needs no OpenRouter key, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest Gemini and OpenRouter adapters and selection wiring that make the focused tests pass.
6. Refactor: remove duplication without expanding scope, then run the focused tests and the complete test suite with no live or paid calls and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, add provider fallback, send original attachment bytes to OpenRouter, or make unrelated changes.
```

## Issue 08 — Implement Option C live web search with citations

**Depends on:** Issue 07.

**Description:**

Implement advanced Option C with the selected provider's native search capability.
Test ordinary and forced-current queries before enabling Google Search grounding for
Gemini or `openrouter:web_search` for OpenRouter. Bound the search-enabled response
to one call, normalize at most five safe citations, and explicitly decline to claim
unverified current facts.

**Success criteria:**

- [ ] Red-first tests distinguish stable questions, provider-decided searches, and
  forced-current questions such as prices, schedules, current events, and office
  holders.
- [ ] Gemini enables only provider-native Google Search grounding and OpenRouter
  enables only `openrouter:web_search`; no scraper, separate search API, RAG, or
  deprecated OpenRouter search mode is added.
- [ ] Each reply makes at most one search-enabled response call, even when grounding
  fails or returns malformed metadata.
- [ ] At most five citations are URL-validated, deduplicated, normalized as
  `Citation`, and rendered consistently in plain-text and safe HTML replies.
- [ ] Gemini's supplied Search entry-point HTML is preserved when required by the
  grounding response contract.
- [ ] If current information cannot be grounded, the reply states that it could not
  be verified and does not fabricate an uncited current claim.

**Prompt for an AI coding agent:**

```text
Implement only Issue 08, “Implement Option C live web search with citations.” Use only Google Search grounding for Gemini or `openrouter:web_search` for OpenRouter, permit one search-enabled response call, retain at most five safe citations, and make no unsupported freshness claim.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 07 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update native-search decisions, forced-current policy, citation normalization/rendering, URL validation, and grounding-failure behavior in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing tests for ordinary and forced-current queries, both provider-native tools, duplicate/unsafe citations, missing grounding metadata, and failed grounding, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest selected-provider native-search and citation implementation that makes the focused tests pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite with provider calls mocked and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, add a scraper, separate search service, full-thread context, RAG, provider fallback, or unrelated changes.
```

## Issue 09 — Provide effectively-once processing and threaded replies

**Depends on:** Issue 08.

**Description:**

Test redelivery, concurrent claims, ambiguous Gmail sends, and crash-after-send
recovery before implementing the one-message coordinator and transactional Firestore
state machine. Combine leases with deterministic outbound identity and thread
inspection to deliver effectively-once threaded replies without persisting raw
content.

**Success criteria:**

- [ ] Focused tests first reproduce sequential redelivery, simultaneous claims,
  expired leases, ambiguous send outcomes, and a crash after Gmail accepts the
  reply but before Firestore completion.
- [ ] `ProcessingStore` uses Firestore transactions for deterministic claims, lease
  expiry, attempt counts, state transitions, and sanitized failure metadata.
- [ ] Each source message maps to a deterministic outbound `Message-ID` and
  `X-Alza-AI-Source-Message-ID`; ambiguous sends inspect the original thread before
  deciding whether to retry.
- [ ] Confirmed replies preserve Gmail `threadId`, matching `Subject`, `In-Reply-To`,
  and `References`, apply `AI/Processed`, and remove `UNREAD` only after confirmed
  send.
- [ ] Terminal processing failures apply `AI/Error` and leave the source message
  unread; retryable failures leave it eligible for retry.
- [ ] Firestore and logs contain no raw email, generated reply, attachment,
  `AttachmentInsight`, prompt, extracted text, or transcript content.

**Prompt for an AI coding agent:**

```text
Implement only Issue 09, “Provide effectively-once processing and threaded replies.” Test redelivery, concurrency, ambiguous sends, and crash recovery first, then compose the adapters behind `POST /jobs/process-message` with a transactional Firestore state machine and deterministic outbound identity.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 08 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update processing states, leases, retry transitions, deterministic headers, label behavior, privacy, and ambiguous-send recovery in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing tests for redelivery, concurrent claims, expired leases, ambiguous sends, and crash-after-send recovery, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest coordinator, `ProcessingStore`, deterministic send, thread inspection, and label implementation that makes the focused tests pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, weaken effectively-once behavior, persist raw content, or make unrelated changes.
```

## Issue 10 — Process Gmail push events and recover dropped notifications

**Depends on:** Issue 09.

**Description:**

Implement mailbox synchronization and its recovery paths. `POST /events/gmail` reads
Gmail history and publishes one metadata-only work item per eligible message before
advancing the cursor. `POST /jobs/renew-watch` renews the watch daily, and
`POST /jobs/reconcile-unread` scans eligible unread messages every five minutes to
recover dropped notifications and pre-existing mail.

**Success criteria:**

- [ ] Red-first tests cover duplicate push envelopes, invalid mailboxes, serialized
  concurrent synchronization, stale history cursors, partial publication,
  dropped notifications, and unread mail that predates activation.
- [ ] Mailbox synchronization is serialized; each versioned work item contains only
  mailbox/message identifiers and correlation metadata, never message content.
- [ ] Every discovered eligible work item is published before the Firestore history
  cursor advances, and any partial publication failure leaves the cursor
  unchanged for safe replay.
- [ ] Stale-cursor recovery performs a bounded unread reconciliation instead of
  silently skipping mail or unboundedly scanning mailbox history.
- [ ] Watch activation records `activated_at`; daily renewal and five-minute
  reconciliation are idempotent and exclude mail older than the documented
  activation boundary unless explicitly eligible.
- [ ] Reconciliation publishes missing eligible work, skips completed and terminal
  records, and lets the effectively-once processor suppress duplicates.

**Prompt for an AI coding agent:**

```text
Implement only Issue 10, “Process Gmail push events and recover dropped notifications.” Serialize mailbox history synchronization, publish all metadata-only work before cursor advancement, preserve the cursor on partial failure, record `activated_at`, renew daily, and reconcile unread mail every five minutes.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 09 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update push-envelope handling, synchronization locking, cursor atomicity, work schema, stale-cursor recovery, `activated_at`, watch renewal, and reconciliation rules in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing tests for duplicate pushes, stale cursors, partial publication, dropped notifications, concurrent sync, and pre-existing unread mail, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest handlers, publisher, cursor transaction, watch renewal, and bounded reconciliation implementation that makes the focused tests pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, advance a cursor after partial publication, publish raw content, or make unrelated changes.
```

## Issue 11 — Add bounded failures, security, and observability

**Depends on:** Issue 10.

**Description:**

Drive retry classification, terminal handling, content redaction, citation safety,
HTML escaping, and latency fields from tests. Bound execution below the Cloud Run
timeout, protect against loops and unauthorized senders, return retryable HTTP
statuses correctly, acknowledge terminal failures only after applying `AI/Error`,
and emit useful structured metadata without content.

**Success criteria:**

- [ ] Red-first tests cover retryable provider/Gmail/storage failures, terminal MIME
  or policy failures, dead-letter behavior, redaction, unsafe citation schemes,
  HTML injection, sender filtering, self-mail loops, and per-stage latency.
- [ ] Transient Pub/Sub processing failures return non-`2xx` so delivery retries;
  terminal failures apply `AI/Error`, persist terminal state, and then return a
  successful acknowledgment.
- [ ] Reconciliation skips terminal records, while retryable records remain eligible
  within bounded attempt and lease policies.
- [ ] The application enforces a `105s` internal deadline beneath the `115s` Cloud Run
  timeout, bounded retries with jitter, output/search/media limits, and the
  configured sender allowlist.
- [ ] Untrusted email/model content is escaped in HTML, citation URLs allow only safe
  schemes and valid hosts, and automated/bulk/self-generated messages cannot
  create reply loops.
- [ ] Structured logs include correlation identifiers, opaque message identifiers,
  state/stage, provider/model, retry class, sanitized error code, and stage/total
  latency, but no addresses, bodies, prompts, replies, insights, media, tokens, or
  secrets.

**Prompt for an AI coding agent:**

```text
Implement only Issue 11, “Add bounded failures, security, and observability.” Test retry decisions, terminal acknowledgments, redaction, citation URLs, HTML escaping, loop/sender policies, and latency first; keep processing below a `105s` internal deadline.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 10 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update retry/terminal classification, acknowledgment rules, deadlines, redaction, safe rendering, sender/loop policy, and structured telemetry in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run focused failing tests for every retry class, terminal label failure, unsafe URL, HTML injection, redaction field, loop/sender rejection, and latency record, and record the expected Red result. Do not commit failing tests.
5. Green: add the smallest failure boundary, security checks, deadline enforcement, and structured telemetry that make the focused tests pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, log raw content or secrets, acknowledge retryable failures as success, or make unrelated changes.
```

## Issue 12 — Complete black-box integration testing and CI

**Depends on:** Issue 11.

**Description:**

Add black-box `pytest` and `httpx` tests against a running `uvicorn` server before
repairing integration gaps. Exercise complete flows through HTTP with deterministic
fake adapters, enforce at least `85%` coverage, and smoke-test the built container.
This is the Playwright-equivalent end-to-end approach for a service with no browser
UI.

**Success criteria:**

- [ ] A red-first black-box harness starts `uvicorn`, calls only public HTTP
  endpoints with realistic authenticated request context, and fails on at least
  one not-yet-integrated complete flow before repairs.
- [ ] Full fake-adapter flows cover plain email, `PDF`, `MP3`, `WAV`, `JPEG`, `PNG`,
  grounded search, citations, safe HTML, Gmail threading, label transitions, and
  scratch cleanup.
- [ ] Recovery flows cover duplicate push/work delivery, concurrent processing,
  ambiguous send recovery, crash-after-send, stale cursor, partial history
  publication, dropped notification reconciliation, and terminal-record skips.
- [ ] Tests assert that HTTP responses, Firestore fakes, published work, and captured
  logs contain no prohibited raw content and that retryable versus terminal
  statuses are correct.
- [ ] CI runs Ruff, mypy, unit/contract/Terraform/integration tests, and coverage with
  no cloud credentials or paid calls and enforces at least `85%` line coverage.
- [ ] CI builds the Docker image, starts it, verifies `GET /healthz`, and shuts it
  down cleanly; the same commands pass locally from a clean checkout.

**Prompt for an AI coding agent:**

```text
Implement only Issue 12, “Complete black-box integration testing and CI.” Add `pytest` and `httpx` tests against a running `uvicorn` server, repair only demonstrated integration gaps, enforce `85%` coverage, and smoke-test the built container; no browser UI means this is the Playwright-equivalent test layer.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 11 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update black-box flow coverage, fake-adapter boundaries, container smoke behavior, redaction assertions, and CI/coverage gates in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run a focused failing black-box test against a running `uvicorn` service before repairing the integration gap, and record the expected Red result. Do not commit failing tests.
5. Green: make the smallest application or wiring repair that completes the specified fake-adapter flows and makes the focused test pass.
6. Refactor: simplify without expanding scope, then run the focused tests and the complete test suite, including Ruff, mypy, Terraform validation/tests, `85%` coverage, and the built-container smoke test, and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results, add frontend technology, call paid/live services in default CI, lower coverage, or make unrelated changes.
```

## Issue 13 — Deploy to GCP and run live acceptance

**Depends on:** Issue 12.

**Description:**

Write authenticated smoke and Gmail acceptance checks before deployment, then apply
Terraform to the explicitly selected project, complete OAuth, deploy the private
Cloud Run revision, activate the Gmail watch, and execute five sanitized live cases.
Leave the healthy service and active watch running for the user.

**Success criteria:**

- [ ] Before mutation, the operator explicitly confirms the active Google identity,
  project, billing account, `europe-west3` region, dedicated Gmail mailbox, OAuth
  consent status, and expected trial-credit/minimal-cost exposure.
- [ ] Automated smoke/acceptance checks exist first and are observed failing against
  the not-yet-deployed revision without fabricating or committing credentials.
- [ ] Terraform is applied outside CI, secret versions are added outside Git and
  Terraform state, an immutable image is deployed, and Cloud Run has no public
  invoker.
- [ ] Authenticated `GET /healthz` passes, the Gmail watch is active, Scheduler jobs
  are enabled, subscriptions are healthy, and the configured maximum instance and
  quota controls are visible.
- [ ] Five live messages cover: plain text; `PDF`; one message with `MP3` and `WAV`;
  one message with `JPEG` and `PNG`; and a forced-current question with grounded,
  valid citations.
- [ ] Every live case sends exactly one reply in the original thread within `120`
  seconds, applies the expected label/state, and leaves only sanitized evidence.
- [ ] The private service and Gmail watch remain healthy and running after acceptance;
  genuine environmental blockers are reported rather than represented as passes.

**Prompt for an AI coding agent:**

```text
Implement only Issue 13, “Deploy to GCP and run live acceptance.” Write authenticated checks first, verify the exact identity/project/billing/region/mailbox before mutation, deploy the private service, activate the watch, run the five specified live cases, and leave the healthy service running.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 12 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update deployment preconditions, authenticated smoke checks, live case definitions, sanitized evidence rules, rollback, and operational acceptance in `docs/design.md` and `docs/test-plan.md` before implementation.
4. Red: add and run the focused authenticated smoke and Gmail acceptance checks before deployment, record the expected Red result, and keep all credentials/evidence out of Git. Do not commit a failing test.
5. Green: apply the approved Terraform outside CI, complete OAuth and secrets, deploy the smallest immutable revision, activate the watch, and make every focused live check pass.
6. Refactor: make only safe configuration/documentation cleanup without expanding scope, then run the focused checks and the complete test suite plus all five live cases, and record exact sanitized results and latencies.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, complete-suite, deployment, and live-acceptance results.
8. Do not merge the PR, commit failing tests, credentials, or generated evidence, invent test results, add frontend technology, deploy to an inferred project, make Cloud Run public, or make unrelated changes.
```

## Issue 14 — Finalize concise documentation and the demo

**Depends on:** Issue 13.

**Description:**

Reconcile documentation with deployed behavior and prepare the authoritative demo
materials. Keep `README.md` minimal, add one Mermaid system-flow diagram and concise
operations/teardown guidance, and create a Markdown presentation plus a rehearsable
`10–15` minute demo runbook. Add no PDF export tooling. Recheck the private service
and Gmail watch before closing the work.

**Success criteria:**

- [ ] `README.md` contains only purpose, prerequisites, local verification, deployment
  entry points, and links to the authoritative design, test plan, operations, and
  demo documents.
- [ ] `docs/design.md` and `docs/test-plan.md` match the deployed endpoints, resources,
  state machine, provider/search behavior, privacy boundary, test evidence, and
  measured acceptance results without stale claims.
- [ ] One readable Mermaid diagram shows Gmail push, both primary Pub/Sub paths, the
  shared dead-letter path, Cloud Run endpoints, Firestore, scratch storage,
  Gemini/OpenRouter native search paths, Scheduler recovery, secrets, and
  observability.
- [ ] Concise operations and teardown instructions cover OAuth/watch renewal, replay,
  terminal errors, dead letters, provider switching, quotas, budget alerts,
  rollback, disabling the watch, deleting regional resources, and residual data.
- [ ] An authoritative Markdown presentation and demo runbook fit `10–15` minutes and
  include preflight, five-case sequence, timings, expected outcomes, sanitized
  fallback evidence, limitations, costs, and teardown; no PDF exporter is added.
- [ ] The final check proves authenticated `GET /healthz`, active Gmail watch, enabled
  Scheduler jobs, and a green complete suite while leaving the healthy service
  running.

**Prompt for an AI coding agent:**

```text
Implement only Issue 14, “Finalize concise documentation and the demo.” Keep `README.md` minimal, reconcile design/test facts with the deployment, add one Mermaid system diagram, concise operations/teardown guidance, and an authoritative Markdown presentation plus a `10–15` minute demo runbook without PDF tooling.

Follow `Spec → Red → Green → Refactor` with this self-contained execution contract:
1. Read `AGENTS.md`. Use or create exactly one GitHub issue for this backlog item, and confirm that Issue 13 is merged before continuing.
2. Update `main`, then create `issue-<number>-<slug>`, replacing `<number>` with the GitHub issue number and `<slug>` with a concise issue slug.
3. Spec: update the documentation acceptance matrix and outline the minimal README, deployed design reconciliation, operations/teardown content, presentation, and timed demo before authoring them.
4. Red: add and run a focused documentation/deployment validation check that initially fails on missing or stale required material, and record the expected Red result. Do not commit a failing check.
5. Green: add the smallest accurate documentation and Markdown demo materials that make the focused validation pass.
6. Refactor: remove repetition and stale detail without expanding scope, then run the focused validation, authenticated health/watch checks, and the complete test suite and record the exact results.
7. Commit with `type(scope): Capitalized summary #<issue-number>`, push the branch, and open a PR containing `Closes #<issue-number>` plus the exact Red, focused, health/watch, and complete-suite results.
8. Do not merge the PR, commit failing tests, invent test results or deployment results, add frontend or PDF export technology, stop the healthy service/watch, or make unrelated changes.
```

## Delivery sequence and completion gates

Issues are strictly consecutive. An issue may start only after its dependency PR is
merged and `main` is updated. A phase is complete only when its documentation is
current, expected Red evidence is recorded, the focused test is Green, the complete
suite is Green, the commit and PR follow the repository contract, and no prohibited
content is present in Git, logs, Firestore, Pub/Sub, or generated evidence.

The MVP is complete only after Issue 14: the private service is healthy in
`europe-west3`, the Gmail watch and recovery jobs are active, all five live acceptance
messages have exactly one correctly threaded reply within `120` seconds, the complete
suite is green with at least `85%` coverage, and the concise operational and demo
documentation matches deployed behavior.

Cost acceptance means trial-credit/minimal-cost controls are configured and verified;
it does not mean the deployment is wholly free. Gemini model/search use and Cloud
resources may consume trial credit or incur charges, OpenRouter is billed separately
when selected, and budget alerts do not hard-cap spending.
