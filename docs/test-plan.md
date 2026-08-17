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

## Issue 07 executable contract

The focused suite is `uv run pytest tests/test_reply_providers.py -q`. Its first Red
run occurs after this specification and the shared provider contract exists, but
before `alza_ai.reply_providers` exists. The expected failure is
`ModuleNotFoundError: No module named 'alza_ai.reply_providers'`; a missing credential,
network connection, provider SDK response, or unrelated test defect is not acceptable
Red evidence. The failing state is not committed.

One parametrized asynchronous contract runs unchanged against Gemini and OpenRouter
adapters backed by deterministic in-process clients and clocks. It proves that both:

- accept only normalized current-message text and ordered bounded
  `AttachmentInsight` values, make exactly one selected-provider call, and return the
  same frozen `GeneratedReply` shape;
- trim prose, normalize CRLF/CR to LF, cap plain text and application-escaped HTML at
  8,000 characters, never trust provider HTML, and leave citations empty while search
  is disabled;
- report the configured provider and model rather than untrusted response metadata;
  clamp input/output/total token fields to `0..1,000,000`; and expose deterministic
  non-negative provider and total latency in integer milliseconds, capped at
  3,600,000 with provider latency no greater than total latency;
- map empty or malformed success responses to terminal
  `reply_provider_invalid_response`, and timeouts, connections, `408`, `429`, and
  provider `5xx` to retryable `reply_provider_unavailable`, without leaking the fake
  provider's private marker.

Configuration tests construct the default Gemini provider with
`RESPONSE_PROVIDER=gemini` and `GEMINI_MODEL=gemini-3.6-flash` while
`OPENROUTER_API_KEY` is absent, and assert that no OpenRouter credential or client is
read. Separate cases prove that selected OpenRouter rejects an absent/blank key, uses
`OPENROUTER_MODEL=anthropic/claude-opus-5` by default, honors one non-empty override,
and does not construct the Gemini reply client. Invalid provider/model settings fail
with sanitized configuration codes.

Mocked-adapter request assertions require Gemini `v1` at `global` and one text-only
`generate_content` call with no tools. OpenRouter uses `httpx.MockTransport` to require
one non-streaming `POST /api/v1/chat/completions`, its key only in the bearer header,
the configured model and 2,048-token bound, and an allowlisted JSON body containing no
original attachment bytes, scratch URI, credential, prior-thread content, or unrelated
Gmail metadata. Failure tests select one provider, raise its typed error, and prove the
other client has zero constructions and calls. No test opens a network connection or
makes a live, cloud, search, or paid-provider request.

After Refactor, run the focused command, the complete suite with `uv run pytest -q`,
`uv run ruff format --check .`, `uv run ruff check .`, and `uv run mypy src tests`.
Because issue 07 adds no HTTP route, its black-box integration smoke starts the
existing `uvicorn` entry point and verifies `GET /healthz`; the shared fake/client
suite is the executable reply-provider boundary.

## Issue 08 executable contract

The focused suite is `uv run pytest tests/test_live_search.py -q`. Its Red run occurs
after this specification and the focused tests exist but before the search-policy,
native-tool, grounding-metadata, and citation implementation exists. Acceptable Red
evidence is collection or assertion failure naming the missing live-search contract,
followed by failing policy/tool/citation assertions once the tests can collect. A
credential, network, live provider, missing dependency, or unrelated syntax failure
is not acceptable Red evidence. The failing state is not committed.

Pure policy cases require `Summarize the supplied attachment.` to remain stable and
send no tool, an ordinary question without explicit stability or freshness language
to permit provider-decided search, and representative current price, tomorrow's
schedule, today's news, and current office-holder questions to require grounding.
Forced-current terms take precedence when stable-task language appears in the same
message. Classification reads only current message text, not attachment content or
thread history.

One parametrized mocked-adapter contract runs against Gemini and OpenRouter and proves
that a search-permitted or forced-current reply makes exactly one response call.
Gemini's call contains only `types.Tool(google_search=types.GoogleSearch())`;
OpenRouter's call contains only `{"type":"openrouter:web_search"}` and contains no
deprecated plugin or `:online` suffix. Stable cases keep the issue-07 no-tool request.
A transport or provider grounding failure retains the typed retry classification and
makes no second call or provider fallback.

Grounded response cases exercise Gemini's first-candidate `grounding_chunks[*].web`
and Search entry-point `rendered_content`, plus OpenRouter's first-message
`url_citation` annotations. The application must preserve provider order; strip and
bound titles; canonicalize scheme, IDNA host, default port, fragment, and empty path;
deduplicate by canonical URL; reject credentials, controls/whitespace, invalid hosts
or ports, non-HTTP(S) schemes, and non-global IP literals; and retain no more than five
safe `Citation` values. The same numbered `Sources:` list must appear in bounded plain
text and escaped HTML, while a Gemini Search entry-point fragment remains exactly
unchanged in its separate field.

A forced-current successful response with missing metadata, malformed metadata, or
no valid citation discards provider prose and returns exactly `I couldn't verify the
requested current information with live web search.` A search-permitted response may
remain ungrounded only when metadata shows that the provider did not attempt search;
once a grounding attempt is reported, the same safe replacement applies. These cases
assert that no uncited current provider claim survives and that each generation still
made only its original response call.

After Refactor, run `uv run pytest tests/test_live_search.py -q`, then the complete
mocked-provider suite with `uv run pytest -q`, `uv run ruff format --check .`,
`uv run ruff check .`, and `uv run mypy src tests`. No default test opens a network
connection or makes a cloud, live-search, or paid-provider call. Because issue 08 adds
no HTTP route, start the existing `uvicorn` entry point and verify `GET /healthz` as
the applicable running-server smoke.

## Issue 09 executable contract

The focused suite is `uv run pytest tests/test_processing.py -q`. Its Red run occurs
after this specification and the focused fake-boundary tests exist but before the
processing coordinator, `ProcessingStore`, Firestore adapter, or processing route
exists. Acceptable Red evidence is a collection failure naming the absent
`alza_ai.processing` contract, followed by behavioral failures if collection requires
a minimal import shell. A credential, emulator, network, provider, or unrelated
syntax failure is not acceptable Red evidence. The failing state is not committed.

The deterministic in-memory store contract and the Firestore adapter use the same
clock-controlled claim cases. Sequential redelivery after completion is final and
does no Gmail/provider work. Two simultaneous claims for one record yield exactly one
owner and one in-flight duplicate; the transaction increments `attempt_count` once
and the in-flight duplicate remains retryable until the owner reaches a final state.
An unexpired lease cannot be stolen, while an expired `processing`, `send_pending`,
or `sent` lease can be reclaimed by one new owner with one additional attempt. A
retryable pre-send failure releases the `processing` lease, and an ambiguous send
releases the `send_pending` lease, so the next delivery is eligible immediately.
Illegal states or stale owners cannot mutate a record.

The coordinator tests start with a source message already represented by metadata-
only work. They require a deterministic MIME `Message-ID` and
`X-Alza-AI-Source-Message-ID`, original Gmail `threadId`, exact parsed `Subject`,
source `In-Reply-To`, and ordered deduplicated `References`. Before every possible
send the coordinator persists `send_pending` and inspects only thread metadata. A
matching deterministic RFC ID or source ID proves acceptance and moves to `sent`
without another send. An absent identity permits one send. A successful send followed
by a simulated crash before `sent` leaves recoverable `send_pending`; redelivery
finds the accepted thread message, sends nothing, applies `AI/Processed`, removes
`UNREAD`, and completes. An ambiguous send with no accepted message returns `503` and
redelivery may send once only after another negative inspection.

Label tests require confirmed success to call exactly
`add=("AI/Processed",), remove=("UNREAD",)` before `completed`. Terminal processing
calls exactly `add=("AI/Error",), remove=()` before `terminal_error`; a label failure
returns `503` and leaves a recoverable record. Focused privacy assertions recursively
inspect every persisted Firestore value, captured log record, and HTTP response and
prove that owned raw markers for address, subject, body, prompt, generated reply,
attachment bytes, filename, extracted text, transcript, and `AttachmentInsight` do
not occur.

The ASGI integration cases call `POST /jobs/process-message` with deterministic
in-process Gmail, parser, analyzer, provider, and store adapters. They require empty
`204` for a completed delivery or final duplicate and empty `503` for in-flight,
retryable, or ambiguous work. Malformed versioned envelopes are acknowledged empty
without content reflection. No default test uses Firestore credentials, Gmail,
network, live search, or a paid provider; the Firestore transaction surface is
exercised with a mocked client.

After Refactor, run `uv run pytest tests/test_processing.py -q`, then `uv run pytest
-q`, `uv run ruff format --check .`, `uv run ruff check .`, and `uv run mypy src
tests`. Start the current branch with `uvicorn`, send one black-box metadata-only
processing request configured with deterministic local adapters, and verify the
documented empty status/body plus `GET /healthz`. This running HTTP check is the
Playwright-equivalent integration layer because the product has no browser UI.

## Issue 10 executable contract

The focused suite is `uv run pytest tests/test_synchronization.py -q`. Its Red run
occurs after this specification and the focused tests exist but before the
push-envelope parser, synchronization store/coordinator, work publisher, watch
renewal, reconciliation, and three HTTP routes exist. Acceptable Red evidence is a
collection failure naming the absent `alza_ai.synchronization` contract. A credential,
emulator, network, Pub/Sub/Gmail service, missing third-party package, or unrelated
syntax failure is not acceptable Red evidence. The failing state is not committed.

Push parsing cases decode the owned synthetic Gmail notification and accept only the
configured mailbox plus decimal history ID. Malformed base64/JSON, absent values, and
wrong mailboxes return empty `204` without coordinator calls or reflecting address or
payload data. Replaying one valid push after its cursor commits performs no additional
history call or work publication. Two simultaneous pushes for one mailbox use a
barrier-controlled Gmail fake and transactional store: exactly one owns the mailbox
lease and publishes; the overlapping request acknowledges without entering Gmail.

The history contract uses ordered synthetic `messagesAdded` and `labelsAdded` records
across pages. It proves each message is deduplicated per invocation and that published
JSON has exactly `schema_version`, `mailbox_key`, `message_id`, `history_id`, and
deterministic `correlation_id`, with no owned raw marker. A publisher that accepts one
item then fails forces empty `503`; the cursor and page checkpoint retain their prior
values, and redelivery safely republishes before advancing the cursor. A fully
published final page advances once to Gmail's returned final cursor.

A Gmail `404` stale-cursor case proves the pushed history ID is never committed. It
runs bounded unread recovery first and replaces the stale cursor with a fresh watch
position only after the final reconciliation publication succeeds. An injected
reconciliation/publication failure retains the stale cursor. Every Gmail history or
unread scan is capped at 10 pages or 500 listed messages and persists only a sanitized
page-token/item-offset continuation checkpoint when more work remains.

Reconciliation tests create a dropped post-activation unread message, a message whose
`internalDate` predates immutable `activated_at`, completed and `terminal_error`
records, and a retryable processing record. The dropped and retryable messages publish;
the pre-activation and final records do not. Repeating the scan is safe. Initial watch
activation records UTC `activated_at`, its returned cursor, and expiration once;
subsequent daily renewal changes only watch metadata. The ASGI contract maps completed,
duplicate, malformed, and wrong-mailbox work to empty `204`, and safe-progress
failures to empty `503` for `/events/gmail`, `/jobs/renew-watch`, and
`/jobs/reconcile-unread`.

After Refactor, run `uv run pytest tests/test_synchronization.py -q`, then `uv run
pytest -q`, `uv run ruff format --check .`, `uv run ruff check .`, and `uv run mypy
src tests`. Start current-branch `uvicorn`, verify `GET /healthz`, malformed push
acknowledgment, and the empty retry responses produced when deployment adapters are
intentionally absent. This live HTTP/ASGI exercise is the Playwright-equivalent
integration layer because this item adds no browser UI.

## Issue 11 executable contract

The focused Red command is `uv run pytest tests/test_reliability.py -q`. Tests use
the existing in-memory processing store and deterministic Gmail/analyzer/provider
fakes, a step-controlled monotonic clock, and a recording telemetry sink. They make
no network, Gmail, Firestore, storage, Pub/Sub, search, or paid-provider call.

The focused suite must cover these failure decisions individually:

- retryable Gmail read/mutation and ambiguous-send errors, attachment storage/model
  errors, retryable provider errors, processing-store failures, and internal-deadline
  exhaustion all return `RETRY`, which the ASGI route maps to an empty `503`;
- malformed MIME, source mismatch, terminal provider output, sender rejection,
  self-mail, automatic response, bulk/list mail, and bounded-attempt exhaustion are
  terminal candidates, but return `ACK`/empty `204` only after exactly `AI/Error`
  was added without removing `UNREAD` and `terminal_error` was persisted;
- a retryable or terminal Gmail label failure and a processing-store failure during
  terminal bookkeeping return `RETRY`/empty `503`, do not send a reply, and do not
  persist terminal state before the error label succeeds;
- the fifth retry remains bounded, exhausted transport delivery uses the existing
  dead-letter subscription configuration, and reconciliation continues to skip only
  durable `terminal_error`/`completed` records.

The supplementary Red command is
`uv run pytest tests/test_reliability.py tests/test_attachments.py -q`. Before the
shared retry helper exists, its tests must fail while proving that retryable Gmail
reads and scratch stage/delete receive exactly two attempts and one sampled full-
jitter delay, terminal/read-success cases receive no retry, and a delay that cannot
fit the `105s` budget starts no second call. A dedicated mocked-Terraform assertion
must keep both primary subscriptions at five attempts and connected to the shared
seven-day dead-letter monitor. Send, model, label, and state calls remain exactly
once per processing attempt.

Security cases feed unsafe values directly to the pure boundary and through a full
coordinator attempt. `javascript:`, `data:`, credentials, malformed/non-public hosts,
and control-character citation URLs are discarded. Script tags, event attributes,
quotes, ampersands, and malicious citation titles remain escaped in generated HTML.
The sender allowlist accepts one case-normalized mailbox only; unauthorized,
self-generated, `Auto-Submitted`, bulk/list/junk, `List-Id`, and auto-response-
suppressed sources are terminally rejected before analyzer/provider/send calls.

Redaction tests put unique markers in every forbidden field: address, subject, body,
prompt/reply, insight, filename/media bytes, token/credential/secret, and raw exception
text. Persisted records, structured events, captured logs, and empty HTTP responses
must contain none of them. An attempted arbitrary telemetry field is dropped rather
than serialized. Structured records assert the allowlisted identifiers, stage/state,
attempt, provider/model, retry class, sanitized code, per-stage latency, and final
total latency.

Deadline tests advance a fake monotonic clock to the `105.0s` boundary and assert no
new generation or send starts there. Every reported stage and total latency is a
non-negative integer, final total latency is present on success and failure, and the
coordinator returns before its internal deadline under the deterministic test clock.
The existing provider tests remain the contract for the unchanged 2,048-token,
8,000-character, one-search, and five-citation limits.

After Refactor, the focused command, complete `uv run pytest -q` suite, Ruff format
and lint, strict mypy, and `git diff --check` must pass. Backend integration runs
through ASGI and a live local Uvicorn process; Playwright is inapplicable because the
frozen product has no browser surface. The local server must remain running for user
inspection.

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
