# Gmail Assistant Architecture

Status: frozen baseline updated through backlog item 11. Later issues must update
this document and `docs/test-plan.md` in their Spec phase before changing a decision.

## Scope and non-goals

The product is one backend-only assistant for one dedicated consumer Gmail mailbox.
It reads an eligible current message, understands supported attachments, optionally
grounds time-sensitive answers with live search, and sends one concise reply in the
original Gmail thread.

The implementation baseline is Python `3.14`, FastAPI, `uv`, `pytest`, `httpx`, Ruff,
mypy, Docker, Terraform, and GitHub Actions. Application packages live under `src/`,
tests under `tests/`, and `uv.lock` is committed. Backlog item 04 adds only the Gmail
gateway boundary, its deterministic fake and mocked adapter tests, and the interactive
operator OAuth bootstrap command. Backlog item 05 adds only immutable email domain
values, synthetic fixtures, and a pure Gmail `format=full` MIME parser. Backlog item
06 adds only bounded attachment insights, regional scratch-storage and Gemini
multimodal adapters, and the asynchronous attachment analyzer. Backlog item 07 adds
only provider-neutral generated replies, one shared reply contract, Gemini and
OpenRouter reply adapters, and selected-provider configuration. Backlog item 08 adds
only deterministic live-search policy, the selected provider's native search tool,
and application-owned citation validation and rendering. Backlog item 09 adds only
the one-message coordinator and route, transactional Firestore processing records,
deterministic threaded sending, metadata-only send recovery, and success/error label
transitions. It does not activate a watch, synchronize Gmail history, deploy, or add
frontend implementation.

Backlog item 10 adds only Gmail push-envelope validation, serialized mailbox-history
synchronization, metadata-only work publication, transactional cursor/checkpoint
state, daily watch renewal, and bounded unread reconciliation. It does not construct
deployment adapters from environment variables, add retry/observability policy beyond
this synchronization boundary, deploy, or add frontend implementation.

Backlog item 11 adds only the processing failure boundary, sender/loop policy,
`105s` internal deadline, safe-rendering regression contract, and content-free
structured processing events. It reuses the existing effectively-once state machine,
provider-owned citation normalization, and application-owned HTML rendering. It does
not construct deployment adapters, change infrastructure, deploy, add a frontend, or
add another retry queue.

The MVP has no browser UI or other frontend technology, full-thread conversational
context, RAG, scraper, separate search service, application-level provider fallback,
or background worker outside Cloud Run and Pub/Sub. Only the current source message,
its supported attachments, and the minimum headers needed to reply are model input.

### Service scaffold and commands

The project requires Python `>=3.14,<3.15` and uses the committed `uv.lock` as the
only dependency resolution input in local verification and CI. The importable
application is `alza_ai.main:app`. FastAPI's OpenAPI, Swagger UI, and ReDoc routes are
disabled so the scaffold does not expose endpoints outside the frozen HTTP surface.

The exact local setup and verification commands for backlog item 02 are:

```text
uv sync --locked
uv run pytest tests/test_health.py -q
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests
```

The local server command is:

```text
uv run uvicorn alza_ai.main:app --host 0.0.0.0 --port 8080
```

CI installs Python `3.14`, synchronizes with `uv sync --locked`, then runs the same
Ruff format/check, mypy, and complete pytest commands. It builds the Dockerfile and
smoke-checks the same application at `GET /healthz`. The image runs as an explicit
non-root user, listens on port `8080`, and includes no development dependency or
credential.

## System flow and data ownership

### Primary push and work path

1. Gmail publishes a notification containing the mailbox address and a new history
   ID to `gmail-notifications`. The only publisher granted on that topic is
   `gmail-api-push@system.gserviceaccount.com`.
2. `gmail-notifications-push` sends an authenticated OIDC push to
   `POST /events/gmail`. The handler validates the envelope and configured mailbox,
   acquires the mailbox synchronization lease, and reads Gmail history from the
   committed Firestore cursor.
3. The handler filters discovered messages and publishes one version-`1`,
   metadata-only item per eligible message to `email-work`. A work item contains only
   `schema_version`, opaque `mailbox_key`, Gmail `message_id`, `history_id`, and an
   opaque deterministic `correlation_id`; it contains no body, subject, address,
   thread content, attachment, or model data.
4. Only after every required publication succeeds does a Firestore transaction move
   the history cursor to the fully enumerated history position. Duplicate publication
   is safe because message processing uses a deterministic record key.
5. `email-work-push` sends an authenticated OIDC push to
   `POST /jobs/process-message`. The handler transactionally claims the message,
   retrieves it from Gmail, parses it in memory, analyzes supported attachments,
   creates a reply with the selected provider and native search policy, sends the
   deterministic reply, updates Gmail labels, and completes the Firestore record.
6. Every scratch object is deleted in `finally`. A one-day bucket lifecycle is only
   a backstop when normal deletion fails.

The Gmail watch includes `INBOX` using `labelFilterBehavior=INCLUDE`. A source is
eligible only when it is in `INBOX` and `UNREAD`, is not already represented by a
completed or terminal record, passes sender/loop policy, and was received at or after
`activated_at`. A pre-activation message can qualify only when post-activation Gmail
history explicitly reports it newly entering the watched inbox; periodic
reconciliation does not backfill older mail by default.

Both primary subscriptions forward exhausted deliveries to the shared `dead-letter`
topic. The pull subscription `dead-letter-monitor` is for sanitized operational
inspection and replay decisions; no dead-letter payload contains raw content.

### Scheduled recovery paths

- Cloud Scheduler invokes `POST /jobs/renew-watch` once daily at `03:00 UTC`. Renewal
  updates watch expiry but never skips or blindly replaces an unprocessed cursor.
- Cloud Scheduler invokes `POST /jobs/reconcile-unread` every five minutes with
  `*/5 * * * *` in UTC. It republishes missing eligible unread messages at or after
  the recorded `activated_at` boundary and skips completed or terminal records.
- Watch activation performs an immediate bounded unread reconciliation, closing the
  gap between consent, cursor creation, and notification delivery.
- A stale history cursor triggers the same bounded unread reconciliation and cursor
  recovery protocol; it never advances directly to the notification's history ID.

### Boundary ownership

| Boundary | May receive or own | May persist | Must never contain |
| --- | --- | --- | --- |
| Dedicated Gmail mailbox | Original messages, attachments, threads, sent replies, and labels | Gmail remains the source and reply system of record | OAuth client secrets or provider credentials |
| Gmail notification topic/subscription | Gmail-required mailbox address and history ID in the inbound envelope | Pub/Sub retention only | Message body, subject, attachment, prompt, insight, or reply |
| Email work topic/subscription | Versioned opaque mailbox/message IDs, history ID, and correlation ID | Pub/Sub retention only | Addresses, thread/body content, attachment bytes, extracted text, prompt, or reply |
| Cloud Run process memory | Current message text/headers, supported bytes, insights, prompt, generated reply, and credentials required for that request | Nothing after request completion | Content in local files, crash dumps, or logs |
| Firestore | Cursor/watch metadata, leases, attempts, processing states, opaque Gmail IDs, deterministic outbound identity, timestamps, and sanitized error codes | Operational metadata only | Addresses, subjects, bodies, prompts, replies, attachment bytes, extracted text/transcripts, or `AttachmentInsight` content |
| Scratch Cloud Storage | One attachment at a time per analysis task under an opaque random object name | Temporary bytes until `finally`; one-day lifecycle backstop | Mailbox/message IDs, filenames in object names, extracted content, prompts, or replies |
| Secret Manager | OAuth client/refresh credential and selected provider secrets | User-managed regional secret versions added outside Terraform | Email/model content; secret payloads in Terraform state |
| Gemini | Current normalized email text and insights for reply generation; one original supported attachment per analysis request; a search query when needed | Provider-managed processing subject to its terms | OAuth refresh credentials or unrelated Gmail metadata |
| OpenRouter | Current normalized email text, bounded `AttachmentInsight` values, and a search query when needed | Provider-managed processing subject to its terms | Original attachment bytes, scratch object URLs, OAuth/provider credentials, or unrelated Gmail metadata |
| Structured logs/traces | Opaque identifiers, stage/state, provider/model, retry class, sanitized code, and timings | Configured logging retention | Addresses, subjects, bodies, prompts, replies, insights, media, token values/counts, or secrets |
| Artifact Registry/Terraform/CI evidence | Immutable image and configuration metadata | Build/configuration artifacts | Credentials, secret versions, raw content, or live-message evidence |

The application never durably persists raw email or generated content. Gmail owns the
source/reply copies; the sole non-Gmail byte-storage exception is the explicitly
temporary scratch object described above.

## HTTP surface and authenticated callers

The service exposes exactly five routes. Cloud Run IAM authenticates every deployed
request. There is no `allUsers` invoker, public route, shared webhook secret, or
alternate application endpoint.

| Route | Authorized caller and behavior | Successful/terminal result | Retryable result |
| --- | --- | --- | --- |
| `GET /healthz` | Approved smoke/operator identity; liveness only, with no dependency or secret details | `200` with exactly `{"status":"ok"}` | `503` only when the process cannot serve work |
| `POST /events/gmail` | Dedicated `gmail-notifications-push` OIDC identity; validate Pub/Sub envelope and configured mailbox, then synchronize history | `204` after complete publish/cursor commit; duplicate, malformed, or wrong-mailbox envelopes are sanitized terminal acknowledgments | `503` for Gmail, transaction, lease, or publication failures |
| `POST /jobs/process-message` | Dedicated `email-work-push` OIDC identity; process one versioned metadata work item | `204` for success, a final duplicate, or a terminal outcome only after terminal handling | `503` for an in-flight duplicate, while a transient failure remains retryable, or when terminal bookkeeping cannot complete |
| `POST /jobs/renew-watch` | Dedicated Scheduler OIDC identity; renew the configured mailbox watch idempotently | `204` | `503` for a transient Gmail or persistence failure |
| `POST /jobs/reconcile-unread` | Dedicated Scheduler OIDC identity; run one bounded reconciliation page set | `204` after its bounded checkpoint is durable | `503` when safe progress/checkpointing fails |

OIDC tokens use the exact service URL as audience. Cloud Run performs token and IAM
validation; handlers additionally bind the configured caller identity to the expected
route. Terraform grants `roles/run.invoker` only to the three logical invoker
identities and an explicitly selected smoke identity. The runtime identity is not a
public invoker.

At the application boundary, `GET /healthz` performs no cloud, credential, or
downstream dependency check. A successful response has status `200`, content type
`application/json`, and the exact UTF-8 body `{"status":"ok"}`. Deployment-level IAM
remains responsible for authenticating the caller; the local ASGI route itself adds
no alternate authentication behavior.

For Pub/Sub, any `2xx` is an acknowledgment and a non-`2xx` requests redelivery.
Poison envelopes that cannot identify a source message are acknowledged after a
sanitized error record; deterministic terminal message failures follow the label and
state protocol below before acknowledgment.

## Domain models

Domain objects are provider-neutral immutable values. Content-bearing objects exist
only in request memory.

| Model | Frozen fields and invariants |
| --- | --- |
| `InboundEmail` | Opaque mailbox key, Gmail message/thread IDs, decoded RFC `Message-ID`, `Subject`, and `From`, optional decoded `Reply-To`, ordered `References`, UTC `internalDate` timestamp, normalized current-message text, tuple of `Attachment`, and bounded warning codes. It contains no prior thread bodies, and content-bearing fields are excluded from its representation. |
| `Attachment` | Gmail part ID, sanitized filename, canonical document/audio/image family and MIME type, `attachment` or `inline` disposition, optional normalized content ID, decoded byte length, and immutable decoded bytes. A value exists only after all MIME and size checks pass; filename, content ID, and bytes are excluded from its representation. |
| `AttachmentInsight` | Original sanitized filename and canonical media type copied from `Attachment`, a trimmed summary (maximum 2,000 characters), trimmed extracted text or transcript (maximum 16,000 characters), at most 20 trimmed relevant facts of 500 characters each, and at most 10 trimmed warnings of 500 characters each. Empty fact/warning entries are discarded. Content-bearing fields are excluded from its representation. |
| `Citation` | Canonical HTTP(S) URL of at most 2,048 characters, normalized title of at most 200 characters, and selected-provider label. URLs have a syntactically valid public host and no credentials, control characters, fragments, unsafe IP literals, or invalid explicit ports; equality and deduplication use the canonical URL. |
| `GeneratedReply` | Normalized plain text, application-rendered safe HTML, an ordered tuple of at most five `Citation` values, optional unmodified Gemini Search entry-point HTML kept separate from reply HTML, selected provider/model, non-negative input/output/total token counts each capped at 1,000,000, and non-negative provider/total latency in integer milliseconds capped at 3,600,000. Provider latency never exceeds total latency. Plain text and rendered HTML are each limited to 8,000 characters, and content-bearing fields are excluded from its representation. |

The application client, not either provider, trims the model's prose, normalizes CRLF
and CR line endings to LF, bounds it, appends the same numbered Sources list to plain
text and escaped HTML, and constructs trusted provider/model, usage, and timing
metadata. Provider HTML is never accepted as reply HTML. The sole exception is
Gemini's authenticated Search entry-point fragment: when supplied it is preserved
byte-for-byte in `search_entry_point_html`, not concatenated with prose or citations.
The later coordinator owns deterministic message headers.

## Integration interfaces

Vendor SDKs remain behind six narrow interfaces; tests define deterministic fakes
before real adapters.

| Interface | Required operations and contract |
| --- | --- |
| `GmailGateway` | Start or renew and stop the `INBOX` watch; page history after a cursor; page `INBOX`+`UNREAD` message references; fetch ID/labels/`internalDate`-only metadata or one complete `full` message; fetch and base64url-decode one external attachment; add/remove message labels; inspect only `Message-ID` and `X-Alza-AI-Source-Message-ID` metadata in a thread; and send one base64url MIME message with a supplied `threadId`. Results use immutable watch/page/reference/thread/sent-message values. |
| `AttachmentAnalyzer` | Analyze an ordered sequence of validated `Attachment` values into ordered bounded `AttachmentInsight` values. It owns staging, one Gemini call per successfully staged attachment, concurrency `2`, timeout normalization, and unconditional scratch deletion. An ordinary partial failure completes every attachment job and then raises the first sanitized error in input order; it never returns partial insights. |
| `ReplyProvider` | Asynchronously generate one `GeneratedReply` from only current normalized text and an ordered sequence of bounded `AttachmentInsight` values. Gemini and OpenRouter obey this same signature and output contract, classify search before the call, make exactly one generation call with zero or one selected-provider native search tool, normalize citations client-side, and expose typed retry classification without fallback. |
| `WorkPublisher` | Publish one versioned metadata-only work item with a deterministic work key used by the consumer for idempotency, and return only after Pub/Sub accepts it. Pub/Sub delivery itself remains at-least-once; batch success is all-or-cursor-does-not-advance. |
| `SynchronizationStore` | Transactionally own mailbox activation/watch metadata, the shared synchronization lease, history cursor, and bounded page-token/item-offset checkpoints. It reads only final per-message state and accepts no content-bearing domain value. |
| `ProcessingStore` | Transactionally own per-message claims, leases, attempt counts, state transitions, deterministic outbound IDs, and sanitized failures. It accepts no content-bearing domain value. |

Application orchestration depends only on these interfaces. Fakes cover normal tests;
vendor calls and paid/live calls are opt-in acceptance concerns.

The issue-04 Gmail adapter always uses `userId="me"`. Watch requests send the exact
fully qualified topic, `labelIds=["INBOX"]`, and
`labelFilterBehavior="include"`, returning the new history ID and integer epoch-
millisecond expiration. History requests require a starting history ID, use a maximum
page size of `500`, preserve the next-page token and final history ID, and surface a
stale-cursor `404` as a terminal typed error for the later synchronization policy to
handle. Unread discovery uses both `INBOX` and `UNREAD`; complete message retrieval
uses `format="full"`. Label modification sends only caller-supplied add/remove IDs.

Thread inspection uses `format="metadata"` and requests only `Message-ID` and
`X-Alza-AI-Source-Message-ID`; it must not retrieve prior bodies. Sending supplies
both the original Gmail `threadId` and unpadded base64url MIME bytes. Retryable reads
and mutations map transport failure, `408`, `429`, and Google `5xx` to
`GmailRetryableError`; all other Gmail `4xx` and malformed successful responses map
to `GmailTerminalError`. A transport failure or `5xx` after a send begins maps to
`GmailAmbiguousSendError`, because Gmail may have accepted the message. Public error
text contains only a stable code and optional HTTP status, never a vendor response,
message data, MIME bytes, email address, or credential.

## Gmail and OAuth lifecycle

1. The operator creates or selects one dedicated consumer Gmail mailbox, enables the
   Gmail API, and creates a Desktop installed-app OAuth client in the intended Google
   Cloud project. The consent-screen user type, publishing status, and authorized
   account are confirmed before any credential is stored. Desktop authorization uses
   the system browser and a random-port `127.0.0.1` loopback listener; deprecated
   out-of-band copy/paste authorization is not used.
2. `uv run alza-ai oauth bootstrap --client-secrets PATH --expected-account ADDRESS
   --token-output PATH` has no implicit credential destination. It requests
   `access_type=offline` and `prompt=consent` with the single-item scope tuple
   `https://www.googleapis.com/auth/gmail.modify`. After consent it requires a refresh
   token, rejects any granted-scope expansion, calls the Gmail profile endpoint to
   match the dedicated mailbox case-insensitively, and only then creates the explicit
   local output with mode `0600` and without overwriting an existing path. The output
   contains only the refresh token and exact scope; the access token and OAuth client
   secret remain excluded. The command prints only a generic completion or sanitized
   error, never a credential. Secret Manager upload remains an explicit deployment
   operation in backlog item 13.
3. An external OAuth app left in Testing can issue refresh tokens that expire after
   seven days. Before unattended use, the operator moves the consent configuration
   to Production as appropriate and verifies the dedicated account remains granted.
4. Terraform creates only regional secret containers with user-managed replication;
   the operator adds OAuth client and refresh secret versions outside Git, CI, and
   Terraform state. Runtime access tokens exist only in memory.
5. Initial activation records UTC `activated_at`, starts a Gmail watch on
   `gmail-notifications` with `INBOX` and `labelFilterBehavior=INCLUDE`, stores the
   returned history cursor and expiry, and performs immediate unread reconciliation.
   Renewal runs daily, well inside Gmail's watch expiry, and only updates expiry/watch
   metadata without jumping an unprocessed cursor.
6. Revocation stops the Gmail watch, disables renewal/reconciliation, revokes the
   OAuth grant, disables or destroys the refresh-token secret version, and leaves
   sanitized state for audit. Reauthorization repeats explicit offline consent,
   creates a new secret version, verifies the mailbox/scope, restarts the watch, and
   reconciles from the retained `activated_at` policy boundary.

Labels `AI/Processed` and `AI/Error` are created idempotently for the dedicated
mailbox. OAuth material is never committed, embedded in an image, logged, or returned
by an endpoint.

The deterministic reply builder accepts the original Gmail message ID, opaque mailbox
key, original RFC `Message-ID`, unfolded semantic `Subject`, prior ordered
`References`, recipient, and generated text. It emits the frozen deterministic
`Message-ID`, preserves the exact semantic `Subject`, sets `In-Reply-To` to the source
RFC ID, and appends that ID to `References` exactly once. The gateway transports those
MIME bytes with the original Gmail `threadId`; it does not regenerate or rewrite the
threading headers.

## MIME and attachment policy

`parse_inbound_email(mailbox_key, message, external_attachments=None)` accepts one
Gmail `format=full` mapping plus an optional mapping of Gmail attachment IDs to bytes
that were already retrieved by the caller. It returns one `InboundEmail`; it accepts
no gateway, callback, file path, URL, or logger and performs no I/O. Inputs are never
mutated. Repeated parsing of equal values produces equal frozen domain values. The
mailbox key must be a non-empty string, every supplied external value must be `bytes`,
and every classified attachment must have a non-empty Gmail part ID; violations are
malformed input.

The top-level `id`, `threadId`, decimal epoch-millisecond `internalDate`, and `payload`
are required. Payload headers are matched case-insensitively. Exactly one non-empty
`Message-ID` and `From` is required. `Subject` is optional and defaults to an empty
string, `Reply-To` is optional, and repeated `References` values are unfolded into
message-ID tokens in wire order. Singleton headers may not be duplicated. RFC 2047
encoded words are decoded before mapping. The timestamp is converted to timezone-aware
UTC. A missing, duplicate, wrongly typed, or undecodable required value is malformed
input.

The parser recursively walks at most 50 Gmail `parts` levels in wire order; deeper
input is malformed. `multipart/alternative`
contributes the first usable `text/plain` descendant, or the first usable `text/html`
descendant when no plain alternative exists; it never contributes both. Other nested
multiparts contribute usable body fragments in wire order, joined by one newline.
Text is decoded strictly with the declared charset (UTF-8 by default); an unknown
charset or undecodable bytes are malformed. Line endings are normalized to `\n`,
trailing horizontal whitespace is removed, and outer blank space is stripped. HTML
is converted locally with the standard-library parser: tags,
scripts, styles, comments, and remote-resource attributes are discarded, block
boundaries become whitespace, and character references become text. No HTML URL is
opened. URL-bearing attributes are discarded rather than injected into normalized
text; malformed hidden-element nesting stays hidden, and a URL that is itself visible
text remains message content. A message with no non-empty usable body is terminal.

Inline Gmail `body.data` is decoded with the strict URL-safe base64 alphabet and
canonical optional padding. An external `attachmentId` is resolved only from the
supplied byte mapping; absent data is terminal. A body containing both representations
is malformed, and the untrusted Gmail `body.size` never overrides the actual decoded
length. Canonical base64url and external-data presence are validated even for an
ignored part; candidate decoded length is computed before allocating inline decoded
bytes. A part is an attachment when it has a non-empty filename, an `attachment`
disposition, or a normalized content ID referenced by a case-insensitive `cid:` URI
in any decoded HTML leaf. Such a part never also contributes body text. An inline
non-text part with no filename or referenced content ID is ignored with the single
warning code `mime_ignored_decorative_inline`. Warning codes are ordered,
deduplicated, and limited to ten.

Attachment filenames decode RFC 2047 words, treat `/` and `\\` as path separators,
remove control characters, trim surrounding whitespace and dots, and are limited to
255 characters. An empty result becomes `attachment`. Disposition and content ID come
from each part's case-insensitive `Content-Disposition` and `Content-ID` headers.
Content IDs have surrounding angle brackets and whitespace removed. Attachment
disposition is canonicalized to `attachment` or `inline`; a CID-referenced part uses
`inline`, and any other file-bearing part without an explicit disposition uses
`attachment`.

The allowed media families are:

| Format | Accepted declared MIME types | Required content family |
| --- | --- | --- |
| PDF | `application/pdf` | PDF signature |
| MP3 | `audio/mpeg` | MPEG audio/ID3 signature |
| WAV | `audio/wav`, `audio/x-wav` | RIFF/WAVE signature |
| JPEG | `image/jpeg` | JPEG signature |
| PNG | `image/png` | PNG signature |

`audio/x-wav` is normalized to `audio/wav`; every other output type is the declared
type above. Declared MIME tokens are compared case-insensitively and normalized to
lowercase. Families normalize to `document`, `audio`, or `image`. Signature predicates
are exact: PDF starts with `%PDF-`; MP3 starts with `ID3` or has an MPEG frame sync
whose first byte is `0xff` and whose second byte has its upper three bits set; WAV
starts with `RIFF` and has `WAVE` at bytes 8 through 11; JPEG starts with
`0xff 0xd8 0xff`; and PNG starts with `89 50 4e 47 0d 0a 1a 0a`. Declared type and
detected family must agree. `MimeParseError` is the single typed terminal exception
and exposes one stable sanitized `code`; its string contains only that code.
Structural/header/timestamp/charset failures use `mime_malformed_message`, invalid
inline base64url uses `mime_malformed_base64url`, absent external bytes uses
`mime_missing_attachment_data`, no usable body uses `mime_missing_body`, an
unsupported file part uses `mime_unsupported_attachment_type`, and a signature
disagreement uses `mime_attachment_type_mismatch`. The parser never returns an
`InboundEmail` or partial decoded attachment bytes after an error.

Limits are inclusive: at most five attachments, at most `20 MiB` (`20 * 1024 * 1024`
decoded bytes) per attachment, and at most `24 MiB` decoded across all attachments.
The sixth discovered attachment fails with `mime_too_many_attachments`; a decoded
attachment above the per-file limit fails with `mime_attachment_too_large`; otherwise
the first addition above the total fails with `mime_attachments_too_large`. Count,
per-file, then running-total checks are applied in that order before content signatures
and before any domain value is returned. They are enforced before staging or model
calls.

For compound-invalid input the first violation is deterministic: validate the message
shape, headers, part metadata, mailbox key, and external mapping types; decode
non-file-bearing text needed for body selection and CID discovery; classify and count
attachments; then, in attachment wire order, validate declared support, decode or
resolve bytes, apply per-file and running-total limits, and check the signature;
finally select the normalized body and require it to be non-empty.

### Attachment analysis and scratch cleanup

`AttachmentAnalyzer.analyze(attachments)` preserves input order and runs at most two
complete attachment jobs concurrently. Each job generates a fresh 32-character
lowercase hexadecimal object name with no filename, message identifier, extension, or
other source metadata. The injected scratch adapter must identify its region as
exactly `europe-west3`; the Cloud Storage adapter writes the immutable attachment bytes
with their canonical media type to the configured Terraform-managed scratch bucket
and returns only `gs://<bucket>/<opaque-name>`.

After a successful upload, the Gemini adapter makes exactly one `generate_content`
request using the configured Gemini model at the `global` Vertex AI endpoint. The
request contains one GCS `file_data` part with the canonical media type and one
application-owned analysis instruction. It never contains inline attachment bytes,
the original filename, mailbox/message identifiers, prior thread content, or an
OpenRouter request. Structured JSON output contains only `summary`, `extracted_text`,
`relevant_facts`, and `warnings`; the analyzer copies filename/media type from the
validated attachment, trims strings, removes empty list entries, and enforces the
`AttachmentInsight` bounds even when provider output exceeds them.

Upload plus Gemini analysis has a `30s` per-attachment timeout. Timeout, upload, model,
and malformed-model outcomes surface respectively as sanitized
`attachment_analysis_timeout`, `attachment_upload_failed`,
`attachment_model_failed`, and `attachment_model_invalid_response` codes with no
vendor message or content. Ordinary failures do not cancel sibling jobs: every
successfully staged sibling still receives its one Gemini request, all jobs execute
cleanup, and the first error in input order is raised after completion. Caller
cancellation cancels unfinished jobs, waits for their cleanup attempts, and preserves
`CancelledError` rather than translating it.

Every job calls deletion in `finally` using the already allocated opaque name,
including when upload partially fails, Gemini fails, times out, or is cancelled.
Cleanup has its own `5s` bound outside the analysis timeout. A delete exception or
cleanup timeout emits only the sanitized `attachment_cleanup_failed` warning event;
on success it is also prepended to that insight's bounded warnings. Cleanup failure
never replaces a successful insight, an analysis error, timeout, or cancellation.
The bucket's one-day lifecycle remains only a safety net for objects that normal
deletion could not remove.

## Provider and live-search policy

Exactly one reply provider is selected at startup:

- `RESPONSE_PROVIDER=gemini` is the default, with
  `GEMINI_MODEL=gemini-3.6-flash`. It uses Google's `global` Gemini endpoint and makes
  no EU-only model-processing guarantee. Its Vertex AI client uses Google application
  credentials and the selected project; no OpenRouter setting or key is read or
  required.
- `RESPONSE_PROVIDER=openrouter` requires its own API key and uses configurable
  `OPENROUTER_MODEL=anthropic/claude-opus-5` by default. Gemini reply credentials are
  not required for selection, although attachment analysis still uses its separately
  authorized Gemini path.

`RESPONSE_PROVIDER` accepts only `gemini` or `openrouter`; an empty or other value is a
sanitized startup configuration error. Selection branches before credential access or
adapter construction, so only the selected provider's credentials are validated and
only its client exists. `OPENROUTER_API_KEY` must be non-empty only for OpenRouter.
An absent model override uses the selected default above; a present blank override is
a sanitized configuration error.

Both adapters serialize one application-owned instruction plus an object containing
only `current_email_text` and ordered `attachment_insights`. Each insight contains its
bounded filename, media type, summary, extracted text/transcript, relevant facts, and
warnings. The method accepts no `Attachment`, byte buffer, scratch URI, OAuth value,
API credential, sender, recipient, subject, message/thread identifier, prior-thread
body, or other Gmail metadata. OpenRouter sends its key only in the Authorization
header and never in the JSON body. Gemini sends one text-only `generate_content`
request; OpenRouter sends one non-streaming `/api/v1/chat/completions` request. Both
set a 2,048 output-token ceiling. A stable request sends no tool. A search-permitted or
forced-current request sends exactly the native tool selected below and never a
provider fallback list.

The adapters treat provider prose and usage as untrusted. Empty or malformed success
responses raise `reply_provider_invalid_response` with terminal classification.
Timeouts, connection failures, HTTP `408`/`429`, and provider `5xx` failures raise
`reply_provider_unavailable` with retryable classification; other provider `4xx`
failures are terminal. Public exception text contains only the stable code and retry
classification. A selected-provider failure is returned unchanged to the caller; no
code path constructs, calls, or retries with the other reply provider.

Advanced Option C is the only search mode. A pure, case-insensitive policy evaluates
only `current_email_text`, with forced-current taking precedence over stable-task
language. Explicit freshness terms such as current, latest, today, tomorrow, or
"as of"; price, schedule, news, weather, score, availability, and exchange-rate
questions; and questions naming a current office holder are forced-current. Explicit
transformations of supplied content such as summarize, rewrite, proofread, translate,
extract, or draft are stable unless they also match forced-current language. Every
other ordinary question is search-permitted so the selected provider may decide
whether search is useful.

A stable request makes one response call without tools. A forced-current or
search-permitted request makes one response call with only the selected provider's
native capability: `types.Tool(google_search=types.GoogleSearch())` for Gemini or
`{"type":"openrouter:web_search"}` for OpenRouter. The OpenRouter request uses neither
the deprecated `web` plugin nor an `:online` model suffix. There is at most one
search-enabled response call and one reply-generation call total per generation
attempt, including missing metadata, malformed metadata, or provider failure; no
application retry, alternate search path, or provider fallback adds another call.
Gemini's tool is Google Search grounding; it is not a custom retrieval integration.

Gemini citations come only from the first candidate's Google Search
`grounding_metadata.grounding_chunks[*].web`; OpenRouter citations come only from the
first choice message's `url_citation` annotations. The application strips titles,
bounds them to 200 characters, and canonicalizes URLs by lowercasing scheme and host,
IDNA-normalizing DNS names, removing default ports and fragments, and using `/` for an
empty path. It rejects values over 2,048 characters, non-HTTP(S) schemes, credentials,
control characters or whitespace, invalid or non-public DNS hosts, invalid ports, and
non-global IP literals. It then removes canonical-URL duplicates in provider order and
retains at most five citations that fit the reply bounds. Provider snippets and index
offsets are never rendered or fetched.

Normalized citations are appended as a numbered `Sources:` section in both plain text
and application-escaped HTML; the `Citation` tuple contains exactly the rendered
order. Gemini's supplied Search entry-point `rendered_content`, when it is a non-empty
string, is preserved unmodified in the separate `search_entry_point_html` field as
required by the grounding contract and is never treated as ordinary safe reply HTML.
For a forced-current request, or a search-permitted response that reports a grounding
attempt, zero valid citations causes all provider prose to be discarded and replaced
with the fixed statement `I couldn't verify the requested current information with
live web search.` A selected-provider request failure remains the existing sanitized
typed error, makes no second call, and emits no reply or unsupported freshness claim.
No scraper, separate search API, RAG, deprecated OpenRouter search mode, or full-thread
context is permitted.

## Transactional processing state machine

The Firestore document ID is
`sha256(mailbox_key + ":" + message_id).hexdigest()`. Its complete allowed field set
for this issue is `mailbox_key`, `message_id`, `thread_id`, `state`, `lease_owner`,
`lease_expires_at`, `attempt_count`, `outbound_message_id`, `sent_message_id`,
`created_at`, `updated_at`, and optional `retry_code` or `error_code`. Values are
opaque identifiers, UTC timestamps, counters, state names, or sanitized stable codes;
the store API cannot accept `InboundEmail`, `Attachment`, `AttachmentInsight`,
`GeneratedReply`, address, subject, body, prompt, extracted text, transcript, MIME
bytes, or generated reply content.

`PROCESSING_LEASE_SECONDS=120` and `MAX_PROCESSING_ATTEMPTS=5`. One Firestore
transaction creates or reclaims a record, assigns a caller-supplied opaque lease
owner, and increments the attempt count once. An unexpired lease has one owner; a
concurrent delivery does no work and returns `503` so one duplicate acknowledgment
cannot suppress recovery if the owner later dies. An expired lease can be reclaimed
from `processing`, `send_pending`, or `sent`. A completed or terminal record is
acknowledged without work. Every mutating store operation checks the lease owner and
legal source state in a Firestore transaction. Transaction conflicts and
unavailability are retryable and cannot create two owners.

The source maps to these headers:

- `Message-ID: <alza-ai-{sha256(mailbox_key + ":" + message_id)}@reply.invalid>`
- `X-Alza-AI-Source-Message-ID: {message_id}`
- original Gmail `threadId`; the same unfolded semantic `Subject` value, encoded
  deterministically without adding or removing `Re:`; original `Message-ID` as
  `In-Reply-To`; and the original ordered `References` with that ID appended once.

| State | Meaning and permitted next state |
| --- | --- |
| `processing` | One lease owner may fetch, validate, analyze, and generate. It moves to `send_pending`, releases its lease but stays `processing` after a retryable pre-send failure, or becomes `terminal_error` only after terminal labeling succeeds. |
| `send_pending` | The deterministic identity is durable and a send may have happened. Every owner must inspect the original thread before sending. It moves to `sent`, or releases its lease but stays `send_pending` after an ambiguous outcome so redelivery repeats inspection. |
| `sent` | Gmail acceptance was returned or proven by thread inspection. No further send is allowed. Idempotent label work moves it to `completed`. |
| `completed` | Final success. The reply is confirmed, `AI/Processed` is applied, and `UNREAD` is removed. No outgoing transition exists. |
| `terminal_error` | Final deterministic failure. `AI/Error` is confirmed and the source remains unread. No outgoing transition exists. |

Immediately before a send, a transaction persists `send_pending` and the deterministic
outbound identity; reply content remains in memory. The owner inspects the thread for
an exact deterministic RFC `Message-ID` or exact source-message header on a message in
the original thread. If found, it records that Gmail message as `sent` without
sending. If absent, it sends once. A successful Gmail response with the original
thread ID records `sent`; a timeout, server failure, malformed send response, or lost
response transactionally releases the lease while leaving `send_pending`, returns
`503`, and forces inspection after redelivery. Thus a crash after Gmail accepts but
before Firestore updates cannot cause a blind resend. Redelivery from `send_pending`
may reconstruct content in memory only after inspection proves the deterministic
reply absent; no generated content is stored.

From `sent`, the handler idempotently applies `AI/Processed`, removes `UNREAD`, and
then records `completed`. A failure in this phase returns `503` and retries labels
without resending. For a deterministic terminal error, it idempotently applies
`AI/Error` while leaving `UNREAD`, then records `terminal_error` and acknowledges. If
labeling or the final transaction fails, it returns `503`; it never acknowledges a
terminal outcome that recovery cannot observe.

The endpoint accepts only a version-`1` Pub/Sub push envelope whose decoded JSON work
item contains non-empty `mailbox_key` and `message_id`; it returns an empty `204` for
completed or terminal claims and an empty `503` for an in-flight claim. The
coordinator fetches and parses only the claimed current message, requires the fetched
opaque IDs to match the work item, analyzes its supported attachments, generates one
reply, and keeps all content in memory. A confirmed reply applies exactly
`AI/Processed` and removes exactly `UNREAD` before completion. A terminal processing
error applies exactly `AI/Error` and removes no label before terminal completion.
Retryable pre-send, ambiguous-send, label, and store failures return an empty `503`;
responses and logs contain no exception or message content.

## Synchronization, retry, and terminal-failure semantics

`POST /events/gmail` accepts only a Pub/Sub push envelope whose base64-decoded JSON is
an object with non-empty string `emailAddress` and decimal-string `historyId` values.
The address must case-insensitively equal the one configured mailbox, but neither the
address nor malformed payload data is logged, persisted, reflected, or copied to work.
Malformed, wrong-mailbox, and already-committed duplicate notifications receive an
empty `204`. A valid newer notification is only a synchronization trigger; its
`historyId` is never assigned directly to the committed cursor.

Mailbox history synchronization and unread reconciliation share one separate `120s`
Firestore lease per opaque `mailbox_key`. Lease acquisition is transactional, so at
most one request calls Gmail or publishes work; an overlapping push or Scheduler run
observes the active owner and returns empty `204`, relying on that owner's invocation
or retry to finish. An expired lease is reclaimable. A history invocation reads only
from the committed `history_cursor`, processes at most 10 pages or 500 discovered
messages, and deduplicates a message within the invocation while preserving Gmail
order. Eligible history is a message added with both `INBOX` and `UNREAD`, or a
message on which post-activation history explicitly added `INBOX` while it is unread.

Each work publication is canonical compact JSON containing exactly
`schema_version=1`, opaque `mailbox_key`, Gmail `message_id`, the record's Gmail
`history_id`, and a deterministic opaque `correlation_id`. The correlation is derived
only from those opaque values. Work never contains the mailbox address, thread ID,
labels, timestamps, subject, sender, body, headers, attachment/model data, or other
raw content. The publisher waits for Pub/Sub acceptance of every item. Only after an
entire history page publishes may the transaction save its sanitized page-token and
integer item-offset checkpoint. A partial page failure retains the previous checkpoint
and committed cursor, so replay may duplicate accepted items but cannot lose an item.
Only the final fully published page transactionally advances `history_cursor` to
Gmail's final history position, clears the checkpoint, and releases the lease.

`POST /jobs/renew-watch` calls the existing exact-topic `INBOX` watch operation. The
first successful activation transaction sets immutable UTC `activated_at`, initializes
`history_cursor` from the returned watch position, and records the expiration. Later
daily calls update the watch position/expiration metadata but never change
`activated_at`, replace the committed cursor, or clear synchronization checkpoints.
Repeating a successful renewal is therefore safe. Initial activation immediately runs
the same unread reconciliation contract; the deployed Scheduler also invokes renewal
at `0 3 * * *` UTC.

Initial and periodic reconciliation lists `INBOX`+`UNREAD`, reads only per-message ID,
labels, and Gmail `internalDate` metadata, and processes at most 10 pages or 500 listed
messages per invocation. Mail older than UTC `activated_at` is excluded unless the
post-activation history rule above explicitly observed it entering `INBOX`.
`completed` and `terminal_error` processing records are skipped; absent, retryable,
or in-flight records are safely republished for the effectively-once processor. A
page-token/item-offset checkpoint lets the next `*/5 * * * *` UTC invocation continue,
while a partial publication failure retains the prior checkpoint. Reconciliation
never moves the history cursor.

If Gmail returns `404` for the committed history cursor, synchronization first runs a
complete bounded unread reconciliation under the same lease. When the scan reaches
its final page, it starts/renews the watch and transactionally replaces the stale
cursor with the returned fresh history position while clearing stale checkpoints.
If any reconciliation, publication, watch, or transaction step fails, the stale
cursor remains committed and the handler returns empty `503`; it never jumps to the
push notification's position or performs an unbounded history/unread scan.

Retryable classes are timeouts, connection failures, rate limits, provider or Google
`5xx`, Firestore conflicts/unavailability, transient storage failures, incomplete
publication, and an ambiguous Gmail send. They preserve a recoverable state and
return `503`; Pub/Sub/Scheduler backoff supplies cross-request retries. In-request
retry is limited to two total attempts with full jitter for idempotent metadata reads
and storage operations. Model generation/analysis is called once per processing
attempt, and Gmail send is never blindly retried.

Terminal classes are malformed or unsupported MIME, type/signature mismatch, size or
count violation, unusable body, sender/loop policy rejection, unsafe irreparable
provider output, and retry-budget exhaustion. They use sanitized stable codes only.
On the fifth claimed processing attempt, another retryable processing failure is
converted to terminal exhaustion if `AI/Error` and Firestore can be finalized;
otherwise the endpoint continues returning `503` and transport delivery can reach
the dead-letter path.

Primary subscriptions use a `120s` acknowledgment deadline, exponential retry with
minimum `10s` and maximum `600s` backoff, maximum delivery attempts `5`, and seven-day
message retention. Dead-letter monitoring also retains messages for seven days.
Dead letters cover repeated endpoint unavailability and failures that prevent safe
terminal bookkeeping; application-final terminal records are acknowledged and do not
need dead-letter delivery.

For message processing, the classification boundary is exhaustive and ordered:

| Failure source | Classification and acknowledgment |
| --- | --- |
| `GmailRetryableError` or `GmailAmbiguousSendError` | Retryable; release the recoverable lease when possible and return `503`. An ambiguous send always remains recoverable through thread inspection. |
| `AttachmentAnalysisError` | Retryable storage/media-provider boundary failure; release and return `503`. |
| `ReplyProviderError(retryable)` | Retryable provider failure; release and return `503`. |
| `ProcessingStoreError` or internal-deadline exhaustion | Retryable; never report success when the durable state is unknown. |
| `MimeParseError`, source mismatch, sender/loop rejection, or `ReplyProviderError(terminal)` before send | Terminal candidate; add exactly `AI/Error`, remove no labels, persist `terminal_error`, then and only then return `204`. |
| Any Gmail or store failure while applying/persisting a terminal candidate | Retryable bookkeeping failure; return `503` and do not persist `terminal_error` before `AI/Error` is confirmed. |

The fifth claimed attempt remains the bounded processing budget. Exhaustion follows
the same terminal-label ordering; if the label or terminal state write fails, Pub/Sub
continues redelivery and may forward the delivery to the existing dead-letter topic.
No retryable class is acknowledged as success.

The coordinator receives a monotonic clock and establishes one absolute deadline
`105.0s` after entry. It checks the remaining budget before fetch, attachment
analysis, generation, thread inspection, send, label, and state transitions. Async
analysis and generation are bounded by the remaining budget. At or beyond the
deadline it starts no new provider or Gmail send operation, records the sanitized
retry code `processing_deadline_exceeded` when possible, and returns `503`. The
existing five-attachment, concurrency-two, 30-second media-job, one-generation,
one-search, 2,048-output-token, 8,000-character, and five-citation ceilings remain
unchanged.

## Regional infrastructure and IAM

The single root module is `infra/`. It requires Terraform `1.15.8`, pins the Google
provider and dependency lock to `7.44.0`, and intentionally declares no remote
backend. All proximity-oriented workload resources use `europe-west3` (Frankfurt):
one private Cloud Run service, Firestore in Native mode, one scratch Cloud Storage
bucket, one Docker Artifact Registry repository, both Cloud Scheduler jobs, and every
user-managed Secret Manager replica. This is a Prague-proximity choice, not a
measured latency guarantee. Gemini uses its `global` endpoint and is the explicit
regional exception. Pub/Sub topics are global resources whose message-storage policy
allows persistence only in `europe-west3`.

Terraform enables only `run`, `artifactregistry`, `firestore`, `storage`,
`secretmanager`, `pubsub`, `cloudscheduler`, `billingbudgets`, `aiplatform`,
`iam`, `iamcredentials`, `logging`, and `monitoring` APIs. The module defines this
exact inventory:

- one `alza-ai` Cloud Run service using an operator-supplied immutable image;
- one `alza-ai` Docker repository, one `(default)` Native Firestore database, and one
  operator-named scratch bucket;
- secret containers `gmail-oauth-client`, `gmail-refresh-token`, and
  `openrouter-api-key`, each with one user-managed `europe-west3` replica and no
  Terraform-managed version or payload;
- `gmail-notifications` -> push subscription `gmail-notifications-push` and
  `email-work` -> push subscription `email-work-push`;
- shared topic `dead-letter` -> pull subscription `dead-letter-monitor`;
- Scheduler jobs `renew-watch` at `0 3 * * *` UTC and `reconcile-unread` at
  `*/5 * * * *` UTC;
- one project-scoped monthly billing budget and bounded application quota inputs.

The Cloud Run service uses `INGRESS_TRAFFIC_INTERNAL_ONLY`, IAM invocation, zero
minimum instances, configurable maximum instances no greater than `2`, container
concurrency `1`, one vCPU, 1 GiB memory, and a `115s` request timeout. Authenticated
smoke originates from an authorized same-project/internal execution context because
ingress is not opened for testing. Both primary subscriptions retain messages for
seven days, acknowledge within `120s`, retry from `10s` to `600s`, and forward after
`5` delivery attempts to the shared dead-letter topic. The dead-letter monitor also
retains messages for seven days.

IAM grants use additive member resources and the narrowest available scope:

| Identity | Grants |
| --- | --- |
| Cloud Run runtime | Project `roles/datastore.user`, `roles/aiplatform.user`, `roles/logging.logWriter`, and `roles/monitoring.metricWriter`; bucket-only `roles/storage.objectUser`; topic-only `roles/pubsub.publisher` on `email-work`; secret-only `roles/secretmanager.secretAccessor` on the three named containers. |
| `gmail-notifications-push`, `email-work-push`, Scheduler invoker, authenticated smoke | Service-only `roles/run.invoker`; no runtime data roles. |
| Pub/Sub service agent | Token creation only on the two push identities, publisher only on `dead-letter`, and subscriber only on the two primary subscriptions. |
| `gmail-api-push@system.gserviceaccount.com` | Topic-only `roles/pubsub.publisher` on `gmail-notifications`. |

The push subscriptions use their dedicated identities to mint OIDC tokens. Both
Scheduler jobs use the Scheduler identity. Every token audience is the exact Cloud
Run service URI, and each target is that URI plus its frozen route. There is no
`allUsers` or `allAuthenticatedUsers` binding. The deployment principal's temporary
`actAs` authority is an operator prerequisite, not a runtime grant managed here.

Terraform runs locally and in CI only through:

```text
terraform fmt -check -recursive
terraform -chdir=infra init -backend=false
terraform -chdir=infra validate
terraform -chdir=infra test
```

Tests use `mock_provider "google"` and require no GCP credentials or billable calls.
CI never runs plan or apply. Project/billing selection, apply, secret versions, OAuth,
and deployment require explicit operator approval in issue 13.

## Security, privacy, and observability

- A required sender allowlist is compared after address normalization. Mail from the
  dedicated mailbox itself, `Auto-Submitted` messages other than `no`, bulk/list/junk
  precedence, list mail, and auto-response-suppressed mail is terminally rejected to
  prevent loops. Reconciliation then skips the terminal record.
- All email and model strings are untrusted. Plain text is bounded; HTML is generated
  by escaping text and inserting only application-owned markup. The parser never
  fetches remote HTML resources.
- Citation URLs allow only `http` or `https`, a syntactically valid public hostname,
  no embedded credentials, no control characters, and no loopback/private/link-local
  IP literal. Citations are rendered as escaped links and are never fetched by the
  service.
- Secrets are read into memory only, redacted by field allowlisting, and excluded
  from Git, images, Terraform state, tests, logs, and evidence. Secret values are
  never used as correlation data.
- Structured events use an allowlist: deterministic correlation ID, opaque mailbox,
  message/thread/work IDs, attempt, stage/state, selected provider/model, retry class,
  sanitized error code, and per-stage/total milliseconds. Addresses, subjects,
  bodies, prompts, replies, `AttachmentInsight` values, filenames, media bytes,
  OAuth/access/API tokens, secret values, and token counts are forbidden.
- Attachment analysis keeps bytes and provider output only in request memory. It
  writes bytes only to the regional scratch object and sends only its opaque GCS URI
  to Gemini; it has no Firestore, Pub/Sub, local-file, or OpenRouter write path.
  Cleanup observability contains only `attachment_cleanup_failed`, never an object
  name, filename, URI, media content, extracted text, transcript, fact, or warning.
- Reply generation keeps current text, bounded insights, provider prose, and rendered
  alternatives in request memory only. The OpenRouter request is constructed by an
  allowlist and contains no original attachment bytes, scratch object URI, credential,
  or unrelated Gmail metadata. Provider errors are sanitized before they cross the
  adapter boundary, and neither request nor response content is logged.
- HTTP responses are empty except the stable health payload and contain neither
  identifiers nor exception text. Metrics aggregate counts and latency without
  content labels.

Sender policy is evaluated after MIME normalization and before attachment analysis or
generation. `From` must contain exactly one valid normalized address present in the
configured allowlist. The normalized sender must differ from the dedicated mailbox.
`Auto-Submitted` is accepted only when absent or `no`; `Precedence` rejects
`bulk`, `list`, and `junk`; any non-empty `List-Id` rejects list mail; and any
non-empty `X-Auto-Response-Suppress` other than `none` rejects automated mail. Policy
failures use only `policy_sender_not_allowed` or `policy_reply_loop` and follow the
terminal label/state protocol. Addresses never enter persistence or telemetry.

Application-rendered reply HTML is the escaped normalized plain text plus only
application-owned `<br>` and citation-link markup. Citation URLs are canonicalized
before rendering and admit only `http` or `https` with a valid public host, no
credentials, whitespace/control characters, fragment, invalid port, or non-global IP
literal. Rejected citations are not rendered or fetched. Provider prose, titles, and
URLs are escaped in their HTML contexts.

Each processing invocation emits structured stage records through an allowlisting
telemetry sink. Every record contains exactly the applicable subset of
`event`, `correlation_id`, opaque `mailbox_key`, opaque `message_id`, `state`,
`stage`, `attempt`, `provider`, `model`, `retry_class`, sanitized `error_code`,
`stage_latency_ms`, and `total_latency_ms`. Required identifiers are derived only
from work metadata; arbitrary mappings and exception text are never merged. Latency
is non-negative monotonic integer milliseconds. At least one final record contains
total latency, and completed provider work records the provider/model plus its stage
latency. The sink receives no address, subject, body, prompt, reply, insight,
filename, media, token value/count, credential, secret, or raw exception field.

## Cost, quota, and time controls

The service optimizes for trial credit and low idle cost, not a wholly free guarantee.
Cloud Run has zero minimum and maximum `2` instances. Model/search calls, Cloud Run,
storage, Pub/Sub, Firestore, logging, and OpenRouter when selected can consume credit
or incur charges.

One message is bounded to five attachment-analysis calls and one reply-generation
call. Attachment concurrency is `2`; each attachment upload/model job has a `30s`
timeout followed by a separately bounded `5s` cleanup attempt; reply output is at
most 2,048 model tokens and 8,000 rendered characters; search uses at most one enabled
call and five citations.
Reconciliation, retries, attempts, message sizes, and retention are bounded as stated
above. Configured provider/project quotas may lower these ceilings but never raise the
application limits without a Spec change.

The coordinator has a `105s` internal processing deadline, including cleanup, below
the `115s` Cloud Run request timeout. It stops starting new model/send work when the
remaining budget cannot finish safely and returns a retryable outcome. Live
acceptance measures end-to-end delivery against `120s`; that is an acceptance target,
not the per-request timeout.

The Terraform inputs permit only lower or equal application ceilings: at most five
attachment-analysis calls, one reply-generation call, one search-enabled call, and
2,048 output tokens per message. They are injected as non-secret Cloud Run settings.
The maximum-instance input is similarly limited to `1..2`; the default is `2`.

The billing account, project number, monthly amount, currency, alert thresholds, and
Monitoring notification-channel IDs are explicit Terraform inputs. Alerts report
spending at configured thresholds but do not hard-cap or prevent charges. Maximum
instances, application call/output limits, provider quotas, and an operator stop
procedure are the actual exposure controls.

## Primary decision references

The implementation must recheck these provider contracts during the relevant issue's
Spec phase:

- [Cloud Run ingress](https://cloud.google.com/run/docs/securing/ingress) for
  same-project Pub/Sub/Scheduler access to internal services;
- [authenticated Pub/Sub push](https://cloud.google.com/pubsub/docs/authenticate-push-subscriptions)
  for OIDC identity, audience, and service-agent permissions;
- [Gmail push notifications](https://developers.google.com/workspace/gmail/api/guides/push)
  for history cursors and daily watch renewal;
- [Google OAuth 2.0](https://developers.google.com/identity/protocols/oauth2) for
  offline refresh tokens, expiry, and revocation;
- [Gmail threading](https://developers.google.com/workspace/gmail/api/guides/threads)
  for `threadId`, matching `Subject`, `In-Reply-To`, and `References`;
- [Gemini 3.6 Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-6-flash)
  for model modalities and `global` availability; and
- [Google Gen AI SDK](https://googleapis.github.io/python-genai/) for asynchronous
  generation, stable API selection, response text, usage metadata, and client
  lifecycle;
- [Vertex AI Grounding with Google Search](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/grounding/grounding-with-google-search)
  for the native Google Search tool, grounding chunks, citations, and required Search
  entry-point rendering;
- [OpenRouter chat completions](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)
  for bearer authentication, request messages, response text, and usage fields; and
- [OpenRouter web search](https://openrouter.ai/docs/guides/features/server-tools/web-search)
  for the non-deprecated `openrouter:web_search` server tool.

## Delivery decisions

Backlog issues 01 through 14 remain strictly consecutive. Each dependent issue starts
only after the prior PR is merged and `main` is updated. Its Spec phase updates these
documents, its expected Red demonstrates the missing behavior, its focused and
complete suites finish Green, and its PR records exact sanitized evidence.

Default tests use deterministic fakes and mocked providers. The final black-box layer
runs against `uvicorn` over public HTTP routes and is the Playwright-equivalent test
for this UI-free service. Backlog item 02 verifies liveness both through the ASGI test
boundary and a running local/container process. Terraform apply, OAuth mutation,
authenticated cloud smoke, and five live Gmail cases are explicit opt-in gates. The
service is deployed and left running only in issue 13.
