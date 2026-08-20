# TDD Acceptance Plan

## Purpose and authority

This document is the test contract for the backend-only Gmail assistant described in
`plan.md`. It maps the finalized requirements through backlog item 14 to the test
level and observed evidence that prove them. Any later behavior change must update
this plan first and must not weaken an existing acceptance gate.

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

The focused test sends `GET /health` through FastAPI's ASGI boundary and requires all
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

and sends `curl --fail --silent --show-error http://127.0.0.1:8080/health`. The
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
  sanitized; `audio/x-wav` and Gmail-observed `audio/vnd.wave` canonicalize to
  `audio/wav`, and a disposition-only file counts;
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
smoke starts the existing `uvicorn` entry point and verifies `GET /health`; parser
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
existing `uvicorn` entry point and verifies `GET /health`; the fake adapters are the
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
existing `uvicorn` entry point and verifies `GET /health`; the shared fake/client
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
no HTTP route, start the existing `uvicorn` entry point and verify `GET /health` as
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
documented empty status/body plus `GET /health`. This running HTTP check is the
Playwright-equivalent integration layer because the product has no browser UI.

## Issue 10 executable contract

The focused suite is `uv run pytest tests/test_synchronization.py -q`. Its Red run
occurs after this specification and the focused tests exist but before the
push-envelope parser, synchronization store/coordinator, work publisher, watch
renewal, reconciliation, and three HTTP routes exist. Acceptable Red evidence is a
collection failure naming the absent `alza_ai.synchronization` contract. A credential,
emulator, network, Pub/Sub/Gmail service, missing third-party package, or unrelated
syntax failure is not acceptable Red evidence. The failing state is not committed.

Push parsing cases decode padded and unpadded base64url Gmail notifications and accept
only the configured mailbox plus a positive integer or decimal-string history ID,
normalizing Gmail's integer form to a decimal string. Malformed base64/JSON, absent
values, and wrong mailboxes return empty `204` without coordinator calls or reflecting
address or payload data. Replaying one valid push after its cursor commits performs no
additional history call or work publication. Two simultaneous pushes for one mailbox use a
barrier-controlled Gmail fake and transactional store: exactly one owns the mailbox
lease and publishes; the overlapping request acknowledges without entering Gmail.

The history contract uses ordered synthetic `messagesAdded` and `labelsAdded` records
across pages. It proves each message is deduplicated per invocation and that published
JSON has exactly `schema_version`, `mailbox_key`, `message_id`, `history_id`, and
deterministic `correlation_id`, with no owned raw marker. A publisher that accepts one
item then fails forces empty `503`; the cursor and page checkpoint retain their prior
values, and redelivery safely republishes before advancing the cursor. A fully
published final page advances once to Gmail's returned final cursor and runs bounded
unread reconciliation before acknowledging, covering Gmail's observed
notification/history visibility gap.

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
src tests`. Start current-branch `uvicorn`, verify `GET /health`, malformed push
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

## Issue 12 executable contract

The focused command is `uv run pytest tests/integration/test_black_box.py -q`.
Its first Red run starts a real `uvicorn.Server` on loopback, sends the complete work
item published by synchronization to `POST /jobs/process-message` with `httpx`, and
expects processing telemetry to retain the publisher's deterministic
`correlation_id`. Before repair, the endpoint completes with `204` but the assertion
fails because the work parser discards `history_id` and `correlation_id` and processing
derives a different value. A missing executable, dependency, credential, port,
network service, or unrelated setup failure is not acceptable Red evidence. The
failing state is not committed.

All black-box requests use live loopback TCP, the five public routes, and realistic
synthetic authorization plus Pub/Sub/Scheduler request headers. The app is composed
with deterministic in-memory fakes; tests add no control endpoint and make no Gmail,
Firestore, Storage, Pub/Sub, Gemini, OpenRouter, search, cloud, or paid call.

The complete success matrix starts from HTTP notification and work envelopes and
proves:

- plain email plus `PDF`, `MP3`, `WAV`, `JPEG`, and `PNG` attachments traverse the
  real parser/coordinator boundaries; analysis sees the canonical media types,
  scratch objects are uniquely allocated and always deleted, and generated output
  contains grounded, validated citations rendered as escaped application HTML;
- Gmail receives one deterministic reply in the original thread with exact
  `Message-ID`, `In-Reply-To`, ordered `References`, and semantic subject; success
  applies `AI/Processed`, removes `UNREAD`, and persists only final metadata;
- duplicate push/work delivery and concurrent processing create one owner/send;
  ambiguous-send and crash-after-send redelivery recover through thread metadata
  without a second send; and retryable outcomes are empty `503` while terminal and
  final outcomes are empty `204`;
- stale cursor recovery, partial history publication, and dropped-notification
  reconciliation publish every eligible item without cursor loss, while completed
  and terminal records are skipped;
- published work uses only the four specified metadata fields plus schema version,
  and its validated correlation ID is unchanged in processing telemetry.

Privacy assertions recursively inspect every HTTP body, fake Firestore value,
published work item, and captured structured event. Owned markers for address,
subject, body, prompt/reply, insight, filename, media bytes, token, credential,
secret, and raw exception must be absent. Malformed metadata is acknowledged without
reflection or telemetry. Logs remain allowlisted and retryable versus terminal
statuses retain the issue-11 contract.

After Refactor, run the focused command, then:

```text
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
uv run pytest --cov=alza_ai --cov-report=term-missing --cov-fail-under=85 -q
terraform fmt -check -recursive
terraform -chdir=infra init -backend=false -input=false
terraform -chdir=infra validate
terraform -chdir=infra test
git diff --check
```

CI runs the same offline gates. It separately builds `alza-ai:test`, verifies a
non-root configured user, starts the container on loopback port `8080`, requires exact
`200 {"status":"ok"}` from `GET /health`, and stops it through an exit trap. The
local container smoke uses the same lifecycle. At handoff, the local Uvicorn service
is left running for inspection. Playwright is not installed because this service has
no browser UI; live HTTP is its equivalent end-to-end layer.

## Issue 13 executable contract

Issue 13 uses opt-in checks that default discovery skips when `--live-config` is
absent, so CI makes no live call. Their only configuration is an ignored file below
`credentials/`; it contains exact operator-selected resource identifiers and paths
to local secret material, never secret values in command arguments. The checks reject
unexpected identities, projects, billing links, regions, mailboxes, consent state,
image tags, public IAM, or generated evidence paths before any deployment mutation.

The preflight command authenticates without mutation and requires these facts to
match the ignored configuration: active CLI and ADC account, active project and
numeric project number, enabled link to the exact open billing account,
`europe-west3`, required operator identifiers, and explicit minimal-cost approval.
Its sanitized `mailbox_confirmed=true` field means that the required mailbox
identifier is configured; it does not call Gmail. The separate Gmail verifier checks
the exact profile, OAuth consent status, modify-only scope, labels, watch, sender,
and accepted cases, and prints only sanitized pass fields. The authenticated smoke
checks the configured billing currency and monthly alert against Cloud Billing.

Red adds the complete read-only authenticated smoke and Gmail acceptance verifier
before infrastructure exists, then runs it with authenticated operator configuration.
The accepted Red reaches GCP and reports that the exact `alza-ai` Cloud Run service
and watch state are absent. A missing executable, GCP login, ADC login, operator
selection, Gmail OAuth credential, or unrelated exception is not accepted as Red.
The exact sanitized command, exit status, stable failure codes, and elapsed time are
copied to the PR; the failing state and generated output are not committed.

Green uses reviewed Terraform commands outside CI. It first targets APIs, the
regional Artifact Registry repository, and empty secret containers; builds and pushes
one image; resolves its registry digest; adds OAuth/API secret versions outside
Terraform; and applies the complete module with the immutable digest, maximum
instances `1`, `480 CZK` monthly alert, and the frozen quota ceilings. The focused
post-deployment command then proves:

- one private internal-only Cloud Run revision receives `100%` traffic and its image
  contains `@sha256:`; no public invoker member exists;
- the immutable image runs locally as its non-root user and receives exact
  `200 {"status":"ok"}` from `/health`; the deployed HTTP startup probe has the
  frozen path/port/timing settings, and an authenticated same-project internal GET
  returns the exact response while no public invoker exists;
- minimum/maximum instances are `0/1`, concurrency is `1`, timeout is `115s`, and
  attachment/generation/search/output ceilings are `5/1/1/2048`;
- `renew-watch` and `reconcile-unread` are enabled with their exact schedules,
  routes, identities, and audience; primary subscriptions retain the expected push,
  retry, acknowledgment, and dead-letter policy;
- Gmail profile and single modify scope match, both application labels exist, the
  watch expiration is in the future, and activation has a committed cursor.

The live verifier observes, but never persists, five source/reply pairs:

| Case ID | Stimulus | Required assertions |
| --- | --- | --- |
| `LIVE-01-plain` | Plain text | One reply, same thread, completed state, processed/read labels, `<120s`. |
| `LIVE-01-pdf` | One valid PDF | Plain assertions plus exactly one supported attachment analyzed. |
| `LIVE-01-audio` | MP3 and WAV together | Plain assertions plus exactly two supported attachments analyzed. |
| `LIVE-01-image` | JPEG and PNG together | Plain assertions plus exactly two supported attachments analyzed. |
| `LIVE-01-current` | Forced-current question | Plain assertions plus at least one valid public HTTP(S) citation and native grounding. |

Output is one sanitized line per case containing only its fixed case ID, pass/fail,
integer end-to-end latency milliseconds, reply/attachment/citation counts, final
state, and boolean thread/header/label checks. It must not contain an address,
subject, body, prompt, reply, citation URL/title, filename, media bytes, model output,
Gmail ID, Firestore path, project/billing identifier, access/refresh token, OAuth
client value, API key, exception text, or raw cloud response. Output remains terminal
text only; `/evidence/`, `/test-results/`, Terraform plans/state, and credentials stay
ignored and uncommitted.

Refactor permits only removal of duplicated configuration or documentation ambiguity.
It reruns the preflight, focused smoke, all five live cases, Ruff, mypy, the full
Python suite at `85%` coverage, offline Terraform formatting/init/validation/tests,
the built-container smoke, and `git diff --check`. A final focused pass rechecks
private IAM, Ready/traffic state, the `/health` startup and authenticated response,
future watch expiration, enabled Scheduler jobs,
healthy subscriptions, empty dead-letter backlog, and `100%` traffic to the accepted
digest. The service and Gmail watch remain running. If any Green/live gate fails,
rollback pauses Scheduler, stops the watch, disables push delivery, restores the
previous digest when present, and never changes ingress or grants a public invoker.

## Issue 14 executable contract

Issue 14 closes the MVP with documentation and documentation-focused validation
only. Before authoring, the required documents and their authority are frozen as
follows:

- `README.md` is a minimal entry point containing only purpose, prerequisites, local
  verification, deployment entry points, and links to the authoritative documents.
- `docs/design.md` owns the deployed system design. It must remove pre-deployment
  qualifiers; match the five image routes, the Cloud Run-compatible health path,
  two primary Pub/Sub paths, shared dead-letter path, processing state machine,
  selected-provider native search, privacy boundary, and accepted deployment facts.
  It owns the repository's one Mermaid diagram, showing Gmail push/API, both primary
  topic/subscription paths, their shared dead-letter path, all five image routes,
  Firestore, scratch storage, Gemini/Google Search, the OpenRouter alternative, both
  Scheduler jobs, secrets, logging, and monitoring.
- `docs/operations.md` owns routine operation and teardown: OAuth and watch renewal,
  replay, terminal errors, dead letters, provider switching, quotas and budget
  alerts, rollback, watch disablement, regional resource deletion, and residual
  Gmail, Firestore, Pub/Sub, scratch, registry, secret, telemetry, budget/API, OAuth,
  provider, and ignored local-state residuals.
- `docs/presentation.md` is the authoritative Markdown presentation. It covers the
  problem, deployed flow, five-case proof, privacy/reliability, limitations, cost,
  operations, and teardown without embedding runbook instructions.
- `docs/demo-runbook.md` is a rehearsable `10-15` minute sequence with preflight,
  read-only verification of all five accepted live cases, explicit timings and
  expected outcomes, clearly historical sanitized fallback evidence, limitations,
  costs, and teardown. It links to rather than duplicates the presentation and
  operations details.

The final health correction is the sole narrow exception to that documentation-only
scope. Google Cloud reserves some paths ending in `z`, so the current contract
replaces `/healthz` rather than retaining an unreachable alias:

- `GET /health` is the image's only GET route and keeps the exact
  `200 {"status":"ok"}` response with no downstream dependency check;
- the image still exposes exactly five routes, and `/healthz` returns `404` locally;
- Cloud Run uses one HTTP startup probe for `/health` on port `8080`, with the
  Google sample's failure threshold `5`, initial delay `10s`, timeout `3s`, and
  period `3s`;
- the focused Red commands are `uv run pytest tests/test_health.py -q` before the
  application route changes and `terraform -chdir=infra test` before the probe is
  configured; neither failing state is committed; and
- Green requires an immutable revision whose authenticated same-project internal
  `GET /health` returns exact status/body, followed by read-only watch, Scheduler,
  traffic, IAM, and complete-suite checks without stopping the service or watch.

The focused validator is `uv run pytest tests/test_documentation.py -q`. Red must
reach that validator and report the absent `README.md`, operations, presentation,
runbook, Mermaid flow, or a specific stale pre-deployment marker. A dependency,
credential, network, or unrelated test failure is not acceptable Red, and the
failing state is not committed. Green requires the smallest text that satisfies the
contracts, exactly one Mermaid block in the repository documentation, a parsed demo
duration within `10-15` minutes, and no frontend or PDF export dependency.

After Refactor, rerun the focused validator, the current CI-equivalent Python,
coverage, Ruff, mypy, Terraform, integration, and container gates. The accepted
digest must return exact `200 {"status":"ok"}` locally, the deployed revision must
expose the matching `/health` HTTP startup probe, and an authenticated same-project
internal request must return that exact status/body. Then read the future-dated Gmail
watch, enabled Scheduler jobs, private IAM, and accepted traffic. Record exact
sanitized results here and in the PR. Do not renew or stop a healthy watch during the
check; leave the accepted private service and watch running.

### Health correction Red

The application and infrastructure checks failed on the intended missing contracts
before either implementation change:

```text
uv run pytest tests/test_health.py -q
exit 1
1 failed, 1 warning in 0.76s
reason: GET /health returned 404 instead of 200

terraform -chdir=infra test
exit 1
6 passed, 1 failed
reason: Cloud Run must gate startup on the HTTP /health readiness contract

uv run pytest -q -s tests/live/test_gcp_acceptance.py::test_live_13_authenticated_smoke --live-config=credentials/live-acceptance.json
exit 1
AUTH-SMOKE pass=false code=cloud_run_startup_probe_invalid elapsed_ms=1479
1 failed in 1.53s
```

No failing check was committed. The live failure was read-only and occurred before
deployment mutation.

### Health correction Green and live result

The committed candidate passed the exact local image contract before deployment and
was pushed once by immutable digest:

```text
IMMUTABLE-CANDIDATE pass=true commit=00d9f37 nonroot=true status=200 body_exact=true legacy_404=true
IMAGE-PUSH pass=true digest=sha256:f2f474bc0005dd6a4b5876b52e3d90e0cff08170264d18b6d23f59fa185b8903
```

The saved Terraform plan allowed only the Cloud Run image and HTTP startup-probe
change. Applying that plan created no resource and destroyed no resource:

```text
TF-PLAN pass=true add=0 change=1 destroy=0 replace=0 only=cloud_run_image_and_startup_probe
TF-APPLY pass=true add=0 change=1 destroy=0
DEPLOYMENT pass=true revision=alza-ai-00006-b4t traffic=100 digest=sha256:f2f474bc0005dd6a4b5876b52e3d90e0cff08170264d18b6d23f59fa185b8903 health_probe=true
```

An approved same-project executor identity made the non-mutating request from the
project VPC. Both short-lived probe VMs, their boot disks, and their ephemeral
addresses were deleted immediately afterward; the second run normalized serial
console line endings so the successful health result also had an unambiguous zero
exit status:

```text
AUTH-HEALTH pass=true authenticated=true internal=true status=200 body_exact=true
PROBE-CLEANUP pass=true vm_absent=true disks_deleted=true ephemeral_ip_released=true
TF-DRIFT pass=true add=0 change=0 destroy=0
POST-DEPLOY pass=true probe_vms=0 probe_disks=0 scheduler_count=2 enabled=true service_watch_untouched=true
```

The current accepted private revision is `alza-ai-00006-b4t`, serving `100%` of
traffic from immutable digest
`sha256:f2f474bc0005dd6a4b5876b52e3d90e0cff08170264d18b6d23f59fa185b8903`.
The previous issue 13 revision and digest remain available as rollback evidence.

### Issue 14 observed validation

Expected Red, before the required material was authored:

```text
uv run pytest tests/test_documentation.py -q
exit 1
1 failed, 1 passed, 107 subtests passed in 0.06s
```

The failure named the absent `README.md`, operations guide, presentation, demo
runbook, Mermaid flow, and stale deployment text. No failing check was committed.

Focused Refactor result:

```text
uv run pytest tests/test_documentation.py -q
2 passed, 107 subtests passed in 0.02s
```

Initial Issue 14 verification exposed the former Cloud Run reserved-path mismatch.
This evidence is historical and superseded by the health correction specified above:

```text
DEPLOYED-DIGEST-LOCAL pass=true status=200 body_exact=true
AUTH-HEALTH pass=false authenticated=true internal=true status=404 body_exact=false
AUTH-ROUTE-CONTROL get_post_only=405 post_health=404
```

The authenticated `404` is the expected Cloud Run interception of `/healthz`, not an
application-health pass. The `405` control proves the authenticated GET reached the
FastAPI revision without invoking the POST-only reconciliation operation. All
short-lived probe resources were deleted, `renew-watch` was restored to its exact
enabled `POST /jobs/renew-watch` configuration, and the service and Gmail watch were
never stopped.

Final read-only deployment and watch verification after the health correction and
cleanup:

```text
PREFLIGHT pass=true identity_match=true adc_match=true project_match=true billing_match=true region_match=true mailbox_confirmed=true cost_approved=true elapsed_ms=7207
AUTH-SMOKE pass=true private=true immutable=true ready=true traffic=true scaling=true timeout=true health_probe=true quotas=true scheduler=true subscriptions=true budget=true public_invoker=false elapsed_ms=8562
GCP acceptance: 2 passed in 15.85s
LIVE-01-plain pass=true latency_ms=69000 reply_count=1 attachment_count=0 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-pdf pass=true latency_ms=90000 reply_count=1 attachment_count=1 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-audio pass=true latency_ms=69000 reply_count=1 attachment_count=2 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-image pass=true latency_ms=51000 reply_count=1 attachment_count=2 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-current pass=true latency_ms=62000 reply_count=1 attachment_count=0 citation_count=1 state=completed thread=true headers=true labels=true
Gmail acceptance: 1 passed, 1 warning in 7.23s
```

The Gmail command only reread the future watch, labels, and five existing accepted
source/reply pairs; it sent no message and did not renew or stop the watch.

Final complete verification:

```text
uv sync --locked: Resolved 69 packages in 2ms; Audited 68 packages in 0.72ms
Health/documentation focused: 3 passed, 1 warning, 107 subtests passed in 0.76s
Ruff format: 42 files already formatted
Ruff check: All checks passed!
mypy: Success: no issues found in 32 source files
Integration: 8 passed, 1 warning in 2.63s
Python/coverage: 267 passed, 3 skipped, 1 warning, 107 subtests passed in 4.29s; 88.72%
Terraform fmt: pass
Terraform init: successfully initialized
Terraform validate: configuration is valid
Terraform test: 7 passed, 0 failed
CONTAINER-SMOKE pass=true nonroot=true status=200 body_exact=true legacy_404=true elapsed_ms=5278
git diff --check: exit 0, no output
```

## Issue 33 executable contract

Issue 33 replaces the startup-snapshot sender allowlist with a live policy the operator
can widen during a presentation. The contract is frozen as follows before
implementation:

- The allowlist owns Firestore document `runtime-config/sender-policy`, field
  `allowed_senders`, a list of strings. `SenderPolicy` holds the dedicated mailbox and
  an entry source, and resolves the source on every message, so an edit takes effect on
  the next processed message without a restart, revision, or secret version.
- `SenderPolicyStore.allowed_senders` reads that document and raises
  `ProcessingStoreError` with `sender_policy_unavailable` when Firestore is unreachable,
  so the coordinator retries instead of accepting or terminally rejecting the sender.
- An entry is one normalized address such as `person@example.test`, or one whole domain
  written as `@example.test`. A domain entry matches only the exact normalized sender
  domain: `@example.test` never admits `sub.example.test`, `notexample.test`, or
  `example.test.attacker.test`. Entries are IDNA and case normalized, unparsable entries
  are ignored, and a missing, empty, or entirely unparsable document rejects every
  sender with `policy_sender_not_allowed`.
- Mailbox-self and automated-mail rejection keeps precedence, so a domain entry that
  covers the dedicated mailbox still yields `policy_reply_loop`.
- `RuntimeSettings` no longer reads `allowed_senders`; the OAuth client secret carries
  only its installed client, `mailbox`, and `mailbox_key`.
- Terraform keeps its frozen rule that the configuration owns no Firestore content, so
  it never seeds or overwrites the document. The runtime identity keeps
  `roles/datastore.user`, the deployed revision carries no allowlist configuration, and
  the documented bootstrap command seeds `@alza.cz` once.
- `alza-ai allowlist list|add|remove` reads and writes that document under an explicit
  `--project`, normalizes and deduplicates entries, rejects a malformed entry with a
  sanitized message and exit status `1`, and prints the resulting entries.

The focused Red commands are
`uv run pytest tests/test_processing.py tests/test_reliability.py tests/test_runtime.py tests/test_cli.py tests/test_documentation.py -q`
and `terraform -chdir=infra test`. Red must fail only on the missing live policy,
operator command, Terraform coverage, and documentation; a dependency, credential, or
network failure is not acceptable Red, and no failing state is committed. Green requires
the smallest code, configuration, and text that satisfy the contract, then the complete
Python, Ruff, mypy, integration, container, and Terraform gates. Delivery requires an
immutable-digest revision, read-only authenticated smoke and five-case Gmail acceptance,
and one live acceptance in which a sender outside the allowlist is admitted by adding a
domain entry with the operator command and no deployment.

## Test levels and phase gates

| Level | Boundary | Required gate |
| --- | --- | --- |
| Unit | Pure parsing, policy, state transition, rendering, validation, redaction, and deterministic identifier functions | Deterministic, isolated tests pass with boundary and malformed cases. |
| Contract | Each provider-neutral interface against fakes and mocked vendor adapters | One shared behavior contract passes without network or paid calls. |
| Terraform | Regional resources, IAM, authentication, scaling, lifecycle, quotas, and budgets with mocked providers | Formatting, offline initialization, validation, and Terraform tests pass; CI never applies. |
| Integration | Public HTTP endpoints through a running `uvicorn` process with deterministic fakes | Complete success, retry, terminal, redelivery, and recovery flows pass over HTTP. This is the Playwright-equivalent layer because there is no browser UI. |
| Container | The built non-root image and its production entry point | Image builds, starts, serves exact `GET /health`, rejects legacy `/healthz`, and stops cleanly. |
| Authenticated smoke | The deployed private Cloud Run revision and configured operational resources | Authenticated same-project internal `GET /health` returns the exact image contract; Ready/traffic, watch, Scheduler, subscriptions, quotas, and scaling controls are observable. |
| Live Gmail acceptance | The dedicated mailbox, deployed adapters, native search, and real threading | Five opt-in cases each produce exactly one correctly threaded reply within `120s`, expected state/labels, and sanitized evidence. |

The current CI gate runs Ruff formatting and linting, strict mypy, the loopback
black-box suite, the complete Python suite with at least **85% line coverage**, the
built-container smoke check, and offline Terraform checks. It uses
only deterministic fakes and mocked providers.
Normal tests mock Gmail, cloud, Gemini, OpenRouter, and search. Authenticated smoke and
live Gmail tests are explicit operator-approved gates outside default CI.

### Issue 13 observed sanitized acceptance

The 2026-08-19 Red runs reached the intended authenticated boundaries before any
deployment mutation. The GCP smoke exited `1` with
`cloud_run_service_absent` (`elapsed_ms=1406`), and the Gmail acceptance exited `1`
with `gmail_watch_absent` (`elapsed_ms=6390`). The failing states and terminal output
were not committed. Focused Red reproductions discovered during live acceptance also
failed before their fixes for empty-history visibility reconciliation, unpadded
base64url notification data, Gmail's integer `historyId`, and Gmail's
`audio/vnd.wave` WAV declaration; each focused test passed after its minimal fix.

Final focused and complete results are:

```text
Ruff format: 38 files already formatted
Ruff check: All checks passed
mypy: Success: no issues found in 32 source files
Synchronization: 14 passed in 0.76s
MIME: 73 passed in 0.16s
Python complete: 266 passed, 3 skipped, 102 subtests passed in 4.01s
Coverage: 88.72% (required 85%)
Terraform: valid; 7 passed, 0 failed
Container: pass=true nonroot=true status=200 body_exact=true elapsed_ms=4048
```

The issue 13 deployment accepted at that time was private revision
`alza-ai-00005-cfq`, serving `100%` traffic from immutable digest
`sha256:cf2013a13a82847e48812282a4217bd624e8e3ff6f45c313ad8ed2ced938957f`.
This block is historical; the health-correction result above is the current accepted
revision. Its issue 13 authenticated and operational results were:

```text
PREFLIGHT pass=true identity_match=true adc_match=true project_match=true billing_match=true region_match=true mailbox_confirmed=true cost_approved=true elapsed_ms=7314
AUTH-SMOKE pass=true private=true immutable=true ready=true traffic=true scaling=true timeout=true quotas=true scheduler=true subscriptions=true budget=true public_invoker=false elapsed_ms=7678
PRODUCTION-GMAIL-PROBE pass=true profile_match=true labels_restored=true latency_ms=1094
TERRAFORM-DRIFT pass=true create=0 update=0 replace=0 destroy=0
DEAD-LETTER pass=true backlog=0
```

The final five-case verifier emitted only the approved sanitized fields:

```text
LIVE-01-plain pass=true latency_ms=69000 reply_count=1 attachment_count=0 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-pdf pass=true latency_ms=90000 reply_count=1 attachment_count=1 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-audio pass=true latency_ms=69000 reply_count=1 attachment_count=2 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-image pass=true latency_ms=51000 reply_count=1 attachment_count=2 citation_count=0 state=completed thread=true headers=true labels=true
LIVE-01-current pass=true latency_ms=62000 reply_count=1 attachment_count=0 citation_count=1 state=completed thread=true headers=true labels=true
```

Rollback was not invoked. The accepted private revision retains `100%` traffic, both
Scheduler jobs and push subscriptions remain active, the Gmail watch is future-dated,
and the service remains running.

## Planned Red evidence

Red must fail on the behavior named below after the relevant Spec update and before
the implementation exists. A missing dependency, credential, executable, or unrelated
syntax error is not acceptable Red evidence.

| Issue | Expected focused Red |
| --- | --- |
| 01 | Documentation validation reports the absent architecture document or required section. |
| 02 | The health test reaches the service boundary and observes that `GET /health` is absent or does not return the frozen payload. |
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
| 02 | Python 3.14 service scaffold, `GET /health`, locked toolchain, non-root container, and CI pass. |
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
| 14 | Deployed facts, operations, teardown, demo, and final health-boundary/watch evidence agree. |

## Acceptance matrix

The eventual test name or evidence identifier must include the matrix ID so that a
requirement cannot silently lose coverage.

| ID | Requirement and representative cases | Planned level | Delivery issue |
| --- | --- | --- | --- |
| DOC-01 | Required architecture/test sections exist, remain aligned, and introduce no application, infrastructure, frontend, or generated evidence | Unit/documentation validation | 01 |
| TOOL-01 | Python 3.14, FastAPI, `uv`, committed lock, pytest/httpx, Ruff, mypy, Docker, Terraform, GitHub Actions, `src/`, and `tests/` form a clean-checkout backend-only toolchain | Unit, container, CI | 02, 12 |
| API-01 | The image declares only `GET /health`, `POST /events/gmail`, `POST /jobs/process-message`, `POST /jobs/renew-watch`, and `POST /jobs/reconcile-unread`; legacy `/healthz` is absent | Unit, integration, authenticated smoke | 02, 10, 13, 14 |
| API-02 | The accepted image has a stable exact health payload; Cloud Run gates startup on the same `/health` route, deployed routes require IAM, and an authenticated same-project internal GET proves the live contract | Integration, container, Terraform, authenticated smoke | 02, 03, 12-14 |
| DOMAIN-01 | `InboundEmail`, `Attachment`, `AttachmentInsight`, `Citation`, and `GeneratedReply` carry exactly the provider-neutral, bounded fields and never cross persistence boundaries | Unit, contract, integration | 04-12 |
| PORT-01 | `GmailGateway`, `AttachmentAnalyzer`, `ReplyProvider`, `WorkPublisher`, and `ProcessingStore` fakes and adapters satisfy shared contracts | Contract, integration | 04, 06, 07, 09, 10, 12 |
| GCP-01 | Cloud Run, Firestore, scratch storage, Artifact Registry, Scheduler, and user-managed secret replicas use `europe-west3`; Gemini uses `global` | Terraform, authenticated smoke | 03, 13 |
| GCP-02 | Exactly two primary topic/subscription pairs and one shared dead-letter path exist with dedicated authenticated callers and least privilege | Terraform, integration, authenticated smoke | 03, 11, 13 |
| GCP-03 | Terraform bounds Cloud Run to zero minimum and maximum `1..2` (default `2`); the accepted deployment uses `0/1`, `115s`, one-day scratch lifecycle, bounded quotas, and budget alerts | Terraform, authenticated smoke | 03, 13, 14 |
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
| SEC-02 | The sender allowlist resolves per message from Firestore, accepts address and exact-domain entries, keeps loop rejection first, fails closed on an empty document, and retries an unavailable read | Unit, contract, integration, live Gmail | 33 |
| OPS-02 | The allowlist document stays outside Terraform and the deployed revision, and `alza-ai allowlist` lists, adds, and removes entries live without a deployment | Unit, Terraform, documentation validation, live Gmail | 33 |
| PRIV-01 | Gmail notifications contain only the Gmail-required mailbox address/history ID; `email-work`, Firestore, logs, Terraform state, HTTP responses, and evidence contain no bodies, prompts, replies, attachment bytes, extracted text, transcripts, insights, addresses, tokens, or secrets | Unit, Terraform, integration, authenticated smoke, live Gmail | 03-14 |
| OBS-01 | Sanitized logs contain correlation/opaque message IDs, stage/state, provider/model, retry class, error code, and per-stage/total latency only | Unit, integration, authenticated smoke | 11-13 |
| TIME-01 | Internal processing stops by `105s`, below the `115s` request timeout; every live case finishes within `120s` | Unit, integration, authenticated smoke, live Gmail | 03, 11-13 |
| COST-01 | Scaling, bounded retries, output/search/media quotas, trial-credit exposure, and budget-alert-not-hard-cap semantics are tested and operator-confirmed | Unit, Terraform, authenticated smoke | 03, 11, 13 |
| LIVE-01 | Plain text; PDF; MP3+WAV; JPEG+PNG; and forced-current grounded-citation messages each get exactly one reply, expected labels/state, and sanitized evidence | Live Gmail | 13 |
| DOC-02 | `README.md` stays limited to purpose, prerequisites, local verification, deployment entry points, and authoritative-document links | Documentation validation | 14 |
| DESIGN-01 | The final design matches deployed routes, resources, state/search/privacy behavior and owns exactly one readable Mermaid system flow | Documentation validation, authenticated smoke | 14 |
| OPS-01 | Operations covers watch/OAuth renewal, replay, terminal/dead-letter recovery, provider switching, quotas, budgets, rollback, ordered teardown, and residual data | Documentation validation | 14 |
| DEMO-01 | The authoritative Markdown presentation and parsed `10-15` minute five-case runbook include preflight, timings, outcomes, sanitized fallback, limitations, costs, and teardown without PDF tooling | Documentation validation | 14 |
| FINAL-01 | Focused and complete suites are green; authenticated `/health`, the startup probe, future watch, enabled Scheduler, private IAM, accepted traffic, and the immutable image agree without stopping the service or watch | Documentation validation, authenticated smoke | 14 |

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
