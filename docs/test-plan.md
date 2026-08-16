# TDD Acceptance Plan

## Purpose and authority

This document is the test contract for the backend-only Gmail assistant described in
`plan.md`. It maps the frozen requirements to the test level that must prove them.
Later issues may add exact commands and observed results, but they must update this
plan before changing behavior and must not weaken an existing acceptance gate.

## TDD evidence contract

Every implementation issue follows `Spec -> Red -> Green -> Refactor`:

1. **Spec:** update `docs/design.md` and this plan with the behavior and acceptance
   boundary before implementation.
2. **Red:** add the smallest focused check that demonstrates the missing behavior,
   run it, and retain the exact command and expected failure in the issue PR. A
   failing check is never committed.
3. **Green:** implement only enough to pass the focused check.
4. **Refactor:** remove duplication or ambiguity, then run the focused check and the
   complete existing suite. Record exact commands, counts, and exit status in the PR.

Tests must not invent results, use production credentials in default runs, make paid
calls in CI, or retain raw message content in evidence. Expected Red output must
identify the unmet behavior rather than an unrelated setup failure.

## Issue 02 executable contract

The focused test sends `GET /healthz` through FastAPI's ASGI boundary and requires all
of the following:

- status `200`;
- content type `application/json`;
- exact response bytes `{"status":"ok"}`.

The route performs no downstream, cloud, or credential check. The expected Red run
uses a valid FastAPI application with the route still absent, so the assertion sees
`404` instead of `200`; an import or dependency failure is not acceptable evidence.

Local and CI validation use these exact commands from a clean checkout:

```text
uv sync --locked
uv run pytest tests/test_health.py -q
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
```

The local black-box check starts the service with:

```text
uv run uvicorn alza_ai.main:app --host 0.0.0.0 --port 8080
```

and sends `curl --fail --silent --show-error http://127.0.0.1:8080/healthz`. The
container gate builds the repository Dockerfile, asserts that its configured user is
not root, starts it on port `8080`, and applies the same HTTP assertion. Playwright is
not installed because this backlog item has no browser UI; live HTTP is the equivalent
integration boundary.

## Issue 03 executable contract

The focused suite runs from `infra/` with `mock_provider "google"`; it must neither
load GCP credentials nor call, plan, or apply against GCP. Before resources exist,
the test must reach Terraform's configuration-evaluation boundary and report an
undeclared regional resource. A missing Terraform executable, provider download,
credential, or unrelated syntax error is not acceptable Red evidence.

The mocked tests prove all of the following:

- Cloud Run, Firestore, scratch storage, Artifact Registry, Scheduler, and all secret
  replicas select `europe-west3`; Pub/Sub persistence is restricted to that region;
- Cloud Run has internal ingress, IAM invocation, minimum `0`, maximum `2`,
  concurrency `1`, one vCPU, 1 GiB, and timeout `115s`;
- the scratch lifecycle deletes after one day, Firestore defines only its database,
  and the three secret containers use user-managed replication without a secret
  version or payload resource;
- exactly the two named primary topic/subscription pairs and the one named shared
  dead-letter pair exist, with `120s` acknowledgement, seven-day retention,
  `10s..600s` retry, and `5` delivery attempts on both primary paths;
- both push subscriptions and both Scheduler jobs use their dedicated service
  accounts, OIDC, the exact service URI audience, and their frozen route;
- runtime access is limited to Firestore, Vertex AI, log/metric write, scratch-object,
  `email-work` publish, and the named secrets; Gmail publishes only to
  `gmail-notifications`; no public invoker member exists;
- maximum instances, model/search/output ceilings, budget amount and thresholds, and
  notification channels remain inputs bounded at or below the frozen exposure caps;
- the CI workflow contains each offline Terraform gate and no Terraform plan or apply.

The focused and complete Terraform commands are:

```text
terraform -chdir=infra test -filter=tests/infrastructure.tftest.hcl
terraform fmt -check -recursive
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
terraform -chdir=infra test
```

`terraform init -backend=false` may download the locked provider but configures no
backend and uses no cloud credential. `terraform test` uses only the mocked provider.

## Issue 04 executable contract

The focused suite is `uv run pytest tests/test_gmail_gateway.py tests/test_oauth.py
-q`. Its first Red run occurs after this specification and the focused tests exist,
but before the Gmail module and CLI command exist; the expected failure must identify
the missing `GmailGateway`/OAuth application contract rather than a missing third-party
package, credential, executable, or network connection. The failing state is not
committed.

A shared contract first drives a deterministic fake and then the adapter against an
in-process mocked Gmail service. It proves:

- start/renew watch uses the exact topic, `INBOX`, and `include`, returns history and
  expiration, and stop is observable;
- history and unread pages preserve stable ordering, cursors/page tokens, message and
  thread IDs, and never fetch a live mailbox;
- complete messages round-trip unchanged, external attachments are strict base64url
  bytes, and label additions/removals are idempotently observable;
- thread inspection returns only the two deterministic identity headers and never a
  body, while send preserves the original `threadId` and the exact MIME bytes;
- the reply builder emits a stable `Message-ID`, matching `Subject`, source
  `In-Reply-To`, and ordered deduplicated `References`;
- mocked `408`/`429`/`5xx` and transport failures become typed retryable errors for
  ordinary calls, other `4xx` become typed terminal errors, and uncertain send
  transport/`5xx` outcomes become typed ambiguous-send errors; exception/output text
  contains no response body, email content, MIME data, or token.

OAuth tests inject a fake installed-app flow and mocked Gmail profile lookup. They
assert the only requested scope is
`https://www.googleapis.com/auth/gmail.modify`, browser authorization uses a random
`127.0.0.1` loopback port with offline explicit consent, and the expected mailbox is
verified before a refresh credential is written. The CLI requires explicit
`--client-secrets`, `--expected-account`, and `--token-output`; success creates a new
`0600` file containing only the refresh token and exact scope. Expanded scopes,
missing refresh tokens, account mismatch, and an existing output fail with sanitized
typed errors and no credential output. `uv run alza-ai oauth bootstrap --help` is the
only subprocess CLI integration check and performs no OAuth or Gmail call. Live Gmail
and Secret Manager mutation remain opt-in issue-13 gates.

After Refactor, run the focused command above and the complete existing suite with
`uv run pytest -q`, followed by Ruff formatting/linting and strict mypy. Every normal
Gmail/OAuth test must use the fake or mock; any network attempt is a test defect.

## Issue 05 executable contract

The focused suite is `uv run pytest tests/test_mime.py -q`. Its first Red run occurs
after this specification and the complete synthetic fixture matrix exist, but before
`alza_ai.mime` exists. The expected failure identifies the absent pure parser contract,
not a missing third-party package, credential, executable, file fixture, or network
connection. The failing state is not committed.

All fixtures are generated from owned ASCII labels and short synthetic byte prefixes;
large boundary bodies are generated in memory and are not committed as binaries.
The focused suite proves:

- a plain body maps all reply-relevant Gmail IDs, UTC `internalDate`, RFC 2047 headers,
  optional `Reply-To`, ordered `References`, normalized text, and immutable values;
- HTML-only input is converted locally while a failing network sentinel observes zero
  calls, a plain alternative is preferred, and adversarial nested mixed/alternative
  parts preserve selected body-fragment wire order without duplicate alternatives;
- padded and unpadded base64url are accepted, while invalid alphabet/padding, malformed
  structures or required headers, missing external data, and an unusable body produce
  only the specified sanitized `MimeParseError.code`; ignored-part base64url and MIME
  nesting above 50 levels are equally terminal;
- separate license-safe PDF, MP3 with ID3 and frame signatures, WAV, JPEG, and PNG
  cases map to canonical attachment values; referenced inline media counts,
  decorative inline media yields a bounded warning, and filenames are decoded and
  sanitized; `audio/x-wav` canonicalizes to `audio/wav`, and a disposition-only file
  counts;
- each supported declared type rejects a different supported signature as
  `mime_attachment_type_mismatch`, and a file-bearing unsupported type rejects as
  `mime_unsupported_attachment_type` without returning bytes;
- four and five attachments pass while six fail; `20 MiB - 1` and exactly `20 MiB`
  pass while one byte more fails per-file; `24 MiB - 1` and exactly `24 MiB` pass
  while one byte more fails in total. The size cases use supplied external byte
  mappings so the test itself does not add base64 expansion or network I/O.
- repeated parsing is equal and leaves its inputs unchanged; duplicate singleton
  headers, unknown or undecodable charsets, empty mailbox keys, non-byte external
  values, and attachment parts without IDs produce only the specified sanitized
  malformed outcome. Content-bearing domain fields are absent from representations;
  captured logs remain empty and contain no raw fixture marker.

After Refactor, run the focused command above, the complete existing suite with
`uv run pytest -q`, `uv run ruff format --check .`, `uv run ruff check .`, and
`uv run mypy src tests`. Because issue 05 exposes no HTTP route, its live integration
smoke starts the existing `uvicorn` entry point and verifies `GET /healthz`; parser
behavior remains exhaustively unit-tested at its pure mapping boundary. Any network,
filesystem, remote HTML, Gmail, cloud, or paid-provider access from the parser is a
defect; the parser must emit no log record.

## Issue 06 executable contract

The focused suite is `uv run pytest tests/test_attachments.py -q`. Its first Red run
occurs after this specification and the full fake-adapter contract exists, but before
`alza_ai.attachments` exists. The expected failure is
`ModuleNotFoundError: No module named 'alza_ai.attachments'`; a missing SDK,
credential, executable, network connection, or unrelated test defect is not acceptable
Red evidence. The failing state is not committed.

Deterministic asynchronous fake scratch-storage and Gemini adapters prove:

- PDF, MP3, WAV, JPEG, and PNG bytes are uploaded once with their canonical media
  type under unique 32-character lowercase hexadecimal names, all to a scratch adapter
  fixed to `europe-west3`; original filenames and identifiers never enter object names;
- each successful upload supplies one opaque `gs://` URI and media type to exactly one
  Gemini call with no inline bytes, output order matches attachment order, and five
  attachment jobs never exceed concurrency `2`;
- summaries are limited to 2,000 characters, extracted text/transcripts to 16,000,
  facts to 20 entries of 500, and warnings to 10 entries of 500; fields are trimmed,
  empty list entries are removed, values are frozen/provider-neutral, and content is
  absent from representations and captured logs;
- an ordinary partial model failure allows every sibling job to finish, cleans every
  allocated object, returns no partial tuple, and raises the first sanitized error in
  input order; upload failure skips its Gemini call but still attempts deletion;
- the `30s` upload/model timeout maps to `attachment_analysis_timeout`; caller
  cancellation remains `CancelledError`; both wait for `finally` deletion attempts;
- deletion failure and the separate `5s` cleanup timeout emit only
  `attachment_cleanup_failed`, add that bounded warning to a successful insight, and
  never mask upload/model/timeout/cancellation behavior.

The same suite runs the Cloud Storage and Google Gen AI adapters against in-process
mock clients. It requires an upload with content type and explicit SDK timeout, delete
by the same opaque name, one Gemini `generate_content` call containing a GCS URI part
and the fixed JSON schema, stable `v1`/`global` configuration, and sanitized malformed
response handling. Normal tests make no GCP, Gemini, OpenRouter, filesystem, or paid
call. The already-tested Terraform bucket location and one-day lifecycle remain
unchanged and are rerun as an offline integration gate.

After Refactor, run the focused command, the complete suite with `uv run pytest -q`,
`uv run ruff format --check .`, `uv run ruff check .`, and `uv run mypy src tests`.
Because issue 06 exposes no new HTTP route, its live integration smoke starts the
existing `uvicorn` entry point and verifies `GET /healthz`; the fake adapters are the
executable attachment boundary.

## Test levels and phase gates

| Level | Boundary | Required gate |
| --- | --- | --- |
| Unit | Pure parsing, policy, state transition, rendering, validation, redaction, and deterministic identifier functions | Deterministic, isolated tests pass with boundary and malformed cases. |
| Contract | Each provider-neutral interface against fakes and mocked vendor adapters | One shared behavior contract passes without network or paid calls. |
| Terraform | Regional resources, IAM, authentication, scaling, lifecycle, quotas, and budgets with mocked providers | Formatting, offline initialization, validation, and Terraform tests pass; CI never applies. |
| Integration | Public HTTP endpoints through a running `uvicorn` process with deterministic fakes | Complete success, retry, terminal, redelivery, and recovery flows pass over HTTP. This is the Playwright-equivalent layer because there is no browser UI. |
| Container | The built non-root image and its production entry point | Image builds, starts, serves `GET /healthz`, and stops cleanly. |
| Authenticated smoke | The deployed private Cloud Run revision and configured operational resources | An authorized identity reaches health; anonymous access fails; watch, Scheduler, subscriptions, quotas, and scaling controls are observable. |
| Live Gmail acceptance | The dedicated mailbox, deployed adapters, native search, and real threading | Five opt-in cases each produce exactly one correctly threaded reply within `120s`, expected state/labels, and sanitized evidence. |

The backlog item 05 CI gate runs Ruff formatting and linting, mypy, the complete
currently available pytest suite, the container smoke check, the offline Terraform
checks, and the mocked Gmail/OAuth plus pure MIME contracts. The eventual complete CI
gate will also run broader black-box integration tests and enforce at least
**85% line coverage** as those layers are introduced.
Normal tests mock Gmail, cloud, Gemini, OpenRouter, and search. Authenticated smoke and
live Gmail tests are explicit operator-approved gates outside default CI.

## Planned Red evidence

Red must fail on the behavior named below after the relevant Spec update and before
the implementation exists. A missing dependency, credential, executable, or unrelated
syntax error is not acceptable Red evidence.

| Issue | Expected focused Red |
| --- | --- |
| 01 | Documentation validation reports the absent architecture document or required section. |
| 02 | The health test reaches the service boundary and observes that `GET /healthz` is absent or does not return the frozen payload. |
| 03 | Mocked-provider Terraform evaluation reports an undeclared regional Cloud Run resource before any GCP resource is defined. |
| 04 | Shared fake-gateway operations and OAuth bootstrap assertions fail because the gateway/command contract is absent. |
| 05 | Synthetic plain/HTML/nested MIME and PDF/MP3/WAV/JPEG/PNG boundary fixtures fail because the pure parser is absent. |
| 06 | Fake storage/model tests fail at the absent analyzer, concurrency bound, or unconditional cleanup behavior. |
| 07 | Shared provider-contract tests fail because selected-provider adapters/configuration do not yet exist. |
| 08 | Stable/forced-current, native-tool, malformed-grounding, and citation tests fail because live-search policy is absent. |
| 09 | Redelivery, concurrent claim, expired lease, ambiguous send, and crash-after-send tests reproduce a duplicate/unsafe missing state transition. |
| 10 | Duplicate push, partial publication, stale cursor, dropped notification, and pre-activation cases fail before synchronization/recovery exists. |
| 11 | Retry/terminal status, redaction, unsafe HTML/URL, sender/loop, deadline, and latency assertions fail before the boundary is wired. |
| 12 | A running `uvicorn` black-box flow reaches public HTTP and exposes at least one genuine incomplete integration path. |
| 13 | Credential-free authenticated smoke/live checks fail against the not-yet-deployed revision before any approved cloud mutation. |
| 14 | Documentation/deployment validation identifies specific missing or stale operations/demo material before authoring it. |

## Delivery phase gates

Issues are consecutive. An issue starts only after its listed dependency PR is
merged and `main` is updated. Each issue is complete only when its specification is
current, expected Red evidence is recorded, focused and complete suites are Green,
and its commit and unmerged PR satisfy the repository contract.

| Issue | Delivery gate |
| --- | --- |
| 01 | Architecture and acceptance documents pass focused documentation validation. |
| 02 | Python 3.14 service scaffold, `GET /healthz`, locked toolchain, non-root container, and CI pass. |
| 03 | Mocked-provider Terraform tests and all offline Terraform checks pass without apply. |
| 04 | Gmail gateway contracts and installed-app OAuth bootstrap pass with Gmail mocked. |
| 05 | Pure recursive MIME parsing and all supported fixture/boundary cases pass. |
| 06 | Attachment analysis, concurrency, failure, and unconditional cleanup contracts pass. |
| 07 | Shared Gemini/OpenRouter reply-provider contracts and configuration isolation pass. |
| 08 | Provider-native current-information search and bounded citation tests pass. |
| 09 | Transactional effectively-once processing, threading, and send recovery pass. |
| 10 | Push synchronization, cursor safety, watch renewal, and unread recovery pass. |
| 11 | Retry, terminal, security, redaction, deadline, and observability gates pass. |
| 12 | Black-box HTTP, complete fake flows, coverage, CI, and container smoke pass. |
| 13 | Approved infrastructure deployment, authenticated smoke, and five live cases pass. |
| 14 | Deployed facts, operations, teardown, demo, and final health/watch evidence agree. |

## Acceptance matrix

The eventual test name or evidence identifier must include the matrix ID so that a
requirement cannot silently lose coverage.

| ID | Requirement and representative cases | Planned level | Delivery issue |
| --- | --- | --- | --- |
| DOC-01 | Required architecture/test sections exist, remain aligned, and introduce no application, infrastructure, frontend, or generated evidence | Unit/documentation validation | 01 |
| TOOL-01 | Python 3.14, FastAPI, `uv`, committed lock, pytest/httpx, Ruff, mypy, Docker, Terraform, GitHub Actions, `src/`, and `tests/` form a clean-checkout backend-only toolchain | Unit, container, CI | 02, 12 |
| API-01 | Only `GET /healthz`, `POST /events/gmail`, `POST /jobs/process-message`, `POST /jobs/renew-watch`, and `POST /jobs/reconcile-unread` are exposed | Unit, integration, authenticated smoke | 02, 10, 13 |
| API-02 | Stable health payload; deployed endpoints require Cloud Run IAM and reject anonymous callers | Integration, container, authenticated smoke | 02, 03, 12, 13 |
| DOMAIN-01 | `InboundEmail`, `Attachment`, `AttachmentInsight`, `Citation`, and `GeneratedReply` carry exactly the provider-neutral, bounded fields and never cross persistence boundaries | Unit, contract, integration | 04-12 |
| PORT-01 | `GmailGateway`, `AttachmentAnalyzer`, `ReplyProvider`, `WorkPublisher`, and `ProcessingStore` fakes and adapters satisfy shared contracts | Contract, integration | 04, 06, 07, 09, 10, 12 |
| GCP-01 | Cloud Run, Firestore, scratch storage, Artifact Registry, Scheduler, and user-managed secret replicas use `europe-west3`; Gemini uses `global` | Terraform, authenticated smoke | 03, 13 |
| GCP-02 | Exactly two primary topic/subscription pairs and one shared dead-letter path exist with dedicated authenticated callers and least privilege | Terraform, integration, authenticated smoke | 03, 11, 13 |
| GCP-03 | Zero minimum and maximum `2` Cloud Run instances, `115s` request timeout, one-day scratch lifecycle, bounded quotas, and budget alerts are configured | Terraform, authenticated smoke | 03, 13 |
| GCP-04 | `terraform fmt -check -recursive`, offline init, validate, and mocked tests pass; CI has no apply and secret payloads never enter state | Terraform, CI | 03, 12 |
| OAUTH-01 | Dedicated mailbox bootstrap requests offline consent and only `https://www.googleapis.com/auth/gmail.modify`; token destination is explicit and secure | Unit, contract, live Gmail | 04, 13 |
| OAUTH-02 | Consent status, Testing seven-day risk, Production transition, watch activation/renewal, revocation, and reauthorization are exercised or operator-verified | Contract, authenticated smoke, live Gmail | 04, 10, 13 |
| GMAIL-01 | Mocked watch/history/message/part/label/thread/send operations map typed retryable, terminal, and ambiguous outcomes without content leakage | Contract, integration | 04, 09, 10, 12 |
| MIME-01 | Plain text, HTML-only, multipart alternative, nested MIME, encoded headers, inline parts, base64url, and malformed input normalize deterministically without remote HTML loading | Unit | 05 |
| MIME-02 | PDF, MP3, WAV, JPEG, and PNG fixtures include declared/content mismatch, unsupported type, count, per-file, and decoded-total boundaries | Unit | 05 |
| ATT-01 | At most five attachments, `20 MiB` each, and `24 MiB` decoded total are accepted before analysis | Unit, integration | 05, 12 |
| ATT-02 | Each supported attachment uses one Gemini request, maximum concurrency `2`, bounded insight fields, opaque staging names, and no OpenRouter byte transfer | Contract, integration | 06, 12 |
| ATT-03 | Success, partial model failure, upload failure, deletion failure, timeout, and cancellation always attempt scratch cleanup in `finally` | Contract, integration | 06, 12 |
| PROVIDER-01 | Default Gemini 3.6 Flash starts without OpenRouter credentials; selected OpenRouter uses its configured default model; only selected credentials are validated | Unit, contract | 07 |
| PROVIDER-02 | Both providers normalize the same reply contract, enforce bounded output/usage, expose latency, and never fall back at application level | Contract, integration | 07, 11, 12 |
| SEARCH-01 | Stable, provider-decided, and forced-current questions select only Gemini Google Search grounding or `openrouter:web_search` | Unit, contract, integration, live Gmail | 08, 12, 13 |
| SEARCH-02 | At most one search-enabled response call; no scraper, separate search service, RAG, deprecated mode, or full-thread context | Unit, contract, integration | 08, 12 |
| CITE-01 | At most five HTTP(S) citations are validated, deduplicated, normalized, and rendered in plain text and safe HTML; required Gemini entry-point HTML is retained | Unit, contract, integration, live Gmail | 08, 12, 13 |
| CITE-02 | Failed or malformed grounding produces an explicit unable-to-verify response and no unsupported current claim | Unit, contract, integration | 08, 12 |
| PROC-01 | Sequential redelivery, simultaneous claims, expired leases, bounded attempts, and transaction conflicts never create two active owners | Unit, contract, integration | 09, 12 |
| PROC-02 | Deterministic outbound identity, original `threadId`, matching `Subject`, `Message-ID`, `In-Reply-To`, and `References` produce one threaded reply | Unit, contract, integration, live Gmail | 04, 09, 12, 13 |
| PROC-03 | Ambiguous send and crash-after-send inspect the thread before retry; confirmed send advances labels/state exactly once | Contract, integration | 09, 12 |
| STATE-01 | Only `processing`, `send_pending`, `sent`, `completed`, and `terminal_error` occur, with transactional leases/attempts and legal transitions | Unit, contract, integration | 09, 12 |
| LABEL-01 | Confirmed success applies `AI/Processed` and removes `UNREAD`; terminal handling applies `AI/Error` and leaves unread before acknowledgment | Contract, integration, live Gmail | 09, 11-13 |
| SYNC-01 | Duplicate push envelopes and concurrent synchronization serialize; metadata-only work publishes completely before cursor advancement | Unit, contract, integration | 10, 12 |
| SYNC-02 | Partial publication preserves the cursor; stale cursors trigger bounded reconciliation rather than data loss or unbounded history scanning | Contract, integration | 10, 12 |
| SYNC-03 | Dropped notifications and unread mail at/after activation are recovered every five minutes; daily watch renewal is idempotent | Contract, integration, authenticated smoke | 10, 12, 13 |
| FAIL-01 | Retryable Gmail, provider, storage, and transient infrastructure failures return non-2xx and remain recoverable within lease/attempt bounds | Unit, contract, integration | 09, 11, 12 |
| FAIL-02 | Terminal MIME/policy failures persist terminal state, apply `AI/Error`, leave unread, and acknowledge only after terminal handling succeeds | Unit, contract, integration | 09, 11, 12 |
| FAIL-03 | Exhausted delivery reaches the shared dead-letter path and is observable without content; terminal records are skipped by reconciliation | Terraform, integration, authenticated smoke | 03, 10, 11, 13 |
| SEC-01 | Sender allowlist, self/automated/bulk loop rejection, HTML escaping, citation scheme/host validation, and secret handling resist unsafe inputs | Unit, integration | 04, 08, 11, 12 |
| PRIV-01 | Firestore, Pub/Sub, logs, Terraform state, HTTP responses, and evidence contain no bodies, prompts, replies, attachment bytes, extracted text, transcripts, insights, addresses, tokens, or secrets | Unit, Terraform, integration, authenticated smoke, live Gmail | 03-13 |
| OBS-01 | Sanitized logs contain correlation/opaque message IDs, stage/state, provider/model, retry class, error code, and per-stage/total latency only | Unit, integration, authenticated smoke | 11-13 |
| TIME-01 | Internal processing stops by `105s`, below the `115s` request timeout; every live case finishes within `120s` | Unit, integration, authenticated smoke, live Gmail | 03, 11-13 |
| COST-01 | Scaling, bounded retries, output/search/media quotas, trial-credit exposure, and budget-alert-not-hard-cap semantics are tested and operator-confirmed | Unit, Terraform, authenticated smoke | 03, 11, 13 |
| LIVE-01 | Plain text; PDF; MP3+WAV; JPEG+PNG; and forced-current grounded-citation messages each get exactly one reply, expected labels/state, and sanitized evidence | Live Gmail | 13 |
| OPS-01 | Final docs, one Mermaid flow, rollback/teardown, provider switching, dead-letter handling, and a rehearsable `10-15` minute Markdown demo match deployed facts without PDF tooling | Documentation validation, authenticated smoke | 14 |

## Issue 01 validation evidence

The following results were observed for issue #3 and must be copied into its pull
request:

### Expected Red, observed before `docs/design.md` existed

- Command: `python3 -m unittest discover -s tests -p 'test_documentation.py' -v`
- Exit status: `1`
- Exact result: `Ran 1 test`; `FAILED (failures=1)`
- Expected failure: `AssertionError: False is not true : docs/design.md is missing`

The failure demonstrated the absent required architecture document, not an environment
or test-harness failure. The failing state was not committed.

### Focused validation after Refactor

- Command: `python3 -m unittest discover -s tests -p 'test_documentation.py' -v`
- Exit status: `0`
- Exact result: `Ran 1 test`; `OK`

### Complete existing suite after Refactor

- Command: `python3 -m unittest discover -s tests -v`
- Exit status: `0`
- Exact result: `Ran 1 test`; `OK`
