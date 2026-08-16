# Gmail Assistant Architecture

Status: frozen baseline for backlog item 01. Later issues must update this document
and `docs/test-plan.md` in their Spec phase before changing a decision.

## Scope and non-goals

The product is one backend-only assistant for one dedicated consumer Gmail mailbox.
It reads an eligible current message, understands supported attachments, optionally
grounds time-sensitive answers with live search, and sends one concise reply in the
original Gmail thread.

The implementation baseline is Python `3.14`, FastAPI, `uv`, `pytest`, `httpx`, Ruff,
mypy, Docker, Terraform, and GitHub Actions. Application packages will live under
`src/`, tests under `tests/`, and `uv.lock` will be committed. Backlog item 01 adds
only these documents and their documentation-focused validation; it adds no
application, cloud infrastructure, CI, or frontend implementation and does not alter
the existing `.gitignore`.

The MVP has no browser UI or other frontend technology, full-thread conversational
context, RAG, scraper, separate search service, application-level provider fallback,
or background worker outside Cloud Run and Pub/Sub. Only the current source message,
its supported attachments, and the minimum headers needed to reply are model input.

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
| `POST /jobs/process-message` | Dedicated `email-work-push` OIDC identity; process one versioned metadata work item | `204` for success, an in-flight/final duplicate, or a terminal outcome only after terminal handling | `503` while a transient failure remains retryable or terminal bookkeeping cannot complete |
| `POST /jobs/renew-watch` | Dedicated Scheduler OIDC identity; renew the configured mailbox watch idempotently | `204` | `503` for a transient Gmail or persistence failure |
| `POST /jobs/reconcile-unread` | Dedicated Scheduler OIDC identity; run one bounded reconciliation page set | `204` after its bounded checkpoint is durable | `503` when safe progress/checkpointing fails |

OIDC tokens use the exact service URL as audience. Cloud Run performs token and IAM
validation; handlers additionally bind the configured caller identity to the expected
route. Terraform grants `roles/run.invoker` only to the three logical invoker
identities and an explicitly selected smoke identity. The runtime identity is not a
public invoker.

For Pub/Sub, any `2xx` is an acknowledgment and a non-`2xx` requests redelivery.
Poison envelopes that cannot identify a source message are acknowledged after a
sanitized error record; deterministic terminal message failures follow the label and
state protocol below before acknowledgment.

## Domain models

Domain objects are provider-neutral immutable values. Content-bearing objects exist
only in request memory.

| Model | Frozen fields and invariants |
| --- | --- |
| `InboundEmail` | Opaque mailbox key, Gmail message/thread IDs, RFC `Message-ID`, `Subject`, `From`, optional `Reply-To`, ordered `References`, received timestamp, normalized current-message text, tuple of `Attachment`, and warnings. It contains no prior thread bodies. |
| `Attachment` | Gmail part ID, sanitized filename, canonical media family/type, disposition/content ID, decoded byte length, and decoded bytes. A value exists only after all MIME and size checks pass. |
| `AttachmentInsight` | Filename, media type, summary (maximum 2,000 characters), extracted text or transcript (maximum 16,000 characters), at most 20 relevant facts of 500 characters each, and at most 10 warnings of 500 characters each. |
| `Citation` | Canonical HTTP(S) URL of at most 2,048 characters, title of at most 200 characters, and optional provider label. URLs have a valid public host and no credentials; equality uses the canonical URL. |
| `GeneratedReply` | Plain text, application-rendered safe HTML, at most five `Citation` values, selected provider/model, bounded usage metadata held in memory, and provider/total latency. Plain text and rendered HTML are each limited to 8,000 characters. |

The application, not a provider, constructs the final multipart plain-text/safe-HTML
reply and deterministic headers. Model output is always untrusted input to that
renderer.

## Integration interfaces

Vendor SDKs remain behind five narrow interfaces; tests define deterministic fakes
before real adapters.

| Interface | Required operations and contract |
| --- | --- |
| `GmailGateway` | Start/renew/stop a watch; page history; page unread messages; fetch a complete message/part; inspect a thread for deterministic outbound headers; send a MIME reply in a supplied thread; and add/remove labels. It maps vendor errors to typed retryable, terminal, or ambiguous-send outcomes and never logs payloads or credentials. |
| `AttachmentAnalyzer` | Analyze one validated `Attachment` into one bounded `AttachmentInsight`. The coordinator invokes it once per attachment per processing attempt and limits total analysis concurrency to `2`. |
| `ReplyProvider` | Generate one `GeneratedReply` from current normalized text, bounded insights, reply headers, and a search policy. Gemini and OpenRouter obey one shared contract and expose typed retry classification. |
| `WorkPublisher` | Publish one versioned metadata-only work item with a deterministic work key used by the consumer for idempotency, and return only after Pub/Sub accepts it. Pub/Sub delivery itself remains at-least-once; batch success is all-or-cursor-does-not-advance. |
| `ProcessingStore` | Transactionally own mailbox sync/cursors and per-message claims, leases, attempt counts, state transitions, deterministic outbound IDs, reconciliation checkpoints, and sanitized failures. It accepts no content-bearing domain value. |

Application orchestration depends only on these interfaces. Fakes cover normal tests;
vendor calls and paid/live calls are opt-in acceptance concerns.

## Gmail and OAuth lifecycle

1. The operator creates or selects one dedicated consumer Gmail mailbox and an OAuth
   installed-app client in the intended Google Cloud project. The consent screen and
   authorized account are confirmed before any credential is stored.
2. `uv run alza-ai oauth bootstrap` requests `access_type=offline` and explicit
   consent with only `https://www.googleapis.com/auth/gmail.modify`. It rejects an
   unexpected account or expanded scope, never prints credentials, and writes only
   to an explicitly selected `0600` local destination or an explicitly named Secret
   Manager secret. Deployment uses Secret Manager.
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

## MIME and attachment policy

The parser is pure, recursive, deterministic, and performs no network I/O. It decodes
encoded headers and strict base64url bodies, prefers `text/plain` inside
`multipart/alternative`, and otherwise converts HTML locally to text without loading
remote resources. Nested multiparts are traversed in wire order. HTML, filenames,
and headers remain untrusted.

A file-bearing or referenced inline part counts as an attachment. Decorative inline
parts with no filename and no body reference may be ignored with a bounded warning.
The allowed media families are:

| Format | Accepted declared MIME types | Required content family |
| --- | --- | --- |
| PDF | `application/pdf` | PDF signature |
| MP3 | `audio/mpeg` | MPEG audio/ID3 signature |
| WAV | `audio/wav`, `audio/x-wav` | RIFF/WAVE signature |
| JPEG | `image/jpeg` | JPEG signature |
| PNG | `image/png` | PNG signature |

Declared type and detected family must agree. Unsupported file parts, mismatches,
malformed base64url data, an unusable body, or an attachment exceeding a boundary are
typed terminal outcomes; the parser never exposes partial decoded bytes. Ignored
decorative parts and recoverable presentation defects become bounded warnings.

Limits are inclusive: at most five attachments, at most `20 MiB` (`20 * 1024 * 1024`
decoded bytes) per attachment, and at most `24 MiB` decoded across all attachments.
They are enforced before staging or model calls. Each attachment is staged with an
opaque name, sent in exactly one Gemini analysis request per processing attempt, and
processed with concurrency `2`. All upload/model outcomes run deletion in `finally`;
a deletion failure is sanitized and observable but does not replace the primary
success or failure, and lifecycle deletion remains the backstop.

## Provider and live-search policy

Exactly one reply provider is selected at startup:

- `RESPONSE_PROVIDER=gemini` is the default, with
  `GEMINI_MODEL=gemini-3.6-flash`. It uses Google's `global` Gemini endpoint and makes
  no EU-only model-processing guarantee. Google application credentials are checked;
  no OpenRouter key is required.
- `RESPONSE_PROVIDER=openrouter` requires its own API key and uses configurable
  `OPENROUTER_MODEL=anthropic/claude-opus-5` by default. Gemini reply credentials are
  not required for selection, although attachment analysis still uses its separately
  authorized Gemini path.

Only credentials for the selected reply provider are validated. A selected-provider
failure is returned with its retry classification; the application never calls the
other reply provider as fallback. OpenRouter receives normalized current-message text
and bounded insights, never original bytes or scratch URLs.

Advanced Option C is the only search mode. The application classifies explicit
freshness needs (including current/latest/today facts, prices, schedules, current
events, and office holders) as forced-current. Clearly stable questions use a normal
response call; other questions may permit the selected provider to decide whether to
search. A forced-current or search-permitted request makes one response-generation
call with only the provider-native tool: Gemini Google Search grounding or
`openrouter:web_search`. There is at most one search-enabled response call and one
reply-generation call total per processing attempt; no retry changes provider or
adds a second search call.

The application URL-validates and canonicalizes citations, removes duplicates while
preserving provider order, and retains at most five citations. It renders them
consistently in plain text and escaped HTML. Gemini-supplied Search entry-point HTML
is treated as an authenticated provider-owned UI fragment, preserved unmodified in a
separate application-owned grounding container when the provider contract requires
it, and never mixed with model prose. If a forced-current answer lacks valid
grounding, the reply states that the current fact could not be verified and makes no
uncited current claim. No scraper, separate search API, RAG, deprecated OpenRouter
search mode, or full-thread context is permitted.

## Transactional processing state machine

The Firestore record key is a deterministic digest of `mailbox_key` and Gmail
`message_id`. Its allowed fields are the opaque source/thread IDs, state, lease owner
and expiry, attempt count, deterministic outbound identity, timestamps, optional
reconciliation/correlation IDs, and sanitized retry/error codes. It never receives a
content-bearing model.

`PROCESSING_LEASE_SECONDS=120` and `MAX_PROCESSING_ATTEMPTS=5`. One Firestore
transaction creates or reclaims a record and increments the attempt count. An
unexpired lease has one owner; a concurrent delivery is acknowledged as an in-flight
duplicate. An expired lease can be reclaimed. A completed or terminal record is
acknowledged without work. Transaction conflicts are retryable and cannot create two
owners.

The source maps to these headers:

- `Message-ID: <alza-ai-{sha256(mailbox_key + ":" + message_id)}@reply.invalid>`
- `X-Alza-AI-Source-Message-ID: {message_id}`
- original Gmail `threadId`; the same unfolded semantic `Subject` value, encoded
  deterministically without adding or removing `Re:`; original `Message-ID` as
  `In-Reply-To`; and the original ordered `References` with that ID appended once.

| State | Meaning and permitted next state |
| --- | --- |
| `processing` | One lease owner may fetch, validate, analyze, and generate. It moves to `send_pending`, remains recoverable after a retryable pre-send failure, or becomes `terminal_error` only through terminal handling. |
| `send_pending` | The deterministic identity is durable and a send may have happened. Every owner must inspect the original thread before sending. It moves to `sent`, remains `send_pending` after an ambiguous/retryable outcome, or returns to the same inspection path after lease expiry. |
| `sent` | Gmail acceptance was returned or proven by thread inspection. No further send is allowed. Idempotent label work moves it to `completed`. |
| `completed` | Final success. The reply is confirmed, `AI/Processed` is applied, and `UNREAD` is removed. No outgoing transition exists. |
| `terminal_error` | Final deterministic failure. `AI/Error` is confirmed and the source remains unread. No outgoing transition exists. |

Immediately before a send, a transaction persists `send_pending` and the deterministic
outbound identity; reply content remains in memory. The owner inspects the thread for
either deterministic header. If found, it records `sent` without sending. If absent,
it sends once. A successful Gmail response records `sent`; a timeout or lost response
leaves `send_pending`, returns `503`, and forces inspection after redelivery. Thus a
crash after Gmail accepts but before Firestore updates cannot cause a blind resend.

From `sent`, the handler idempotently applies `AI/Processed`, removes `UNREAD`, and
then records `completed`. A failure in this phase returns `503` and retries labels
without resending. For a deterministic terminal error, it idempotently applies
`AI/Error` while leaving `UNREAD`, then records `terminal_error` and acknowledges. If
labeling or the final transaction fails, it returns `503`; it never acknowledges a
terminal outcome that recovery cannot observe.

## Synchronization, retry, and terminal-failure semantics

Mailbox synchronization uses a separate `120s` Firestore lease and processes at most
10 history pages or 500 discovered messages per request. It persists only a sanitized
sync generation and Gmail page token between bounded requests; the committed history
cursor remains unchanged. Each page's eligible work is published before its page
checkpoint is saved, so a crash can duplicate but not lose work. Only completion of
the final page advances the committed cursor in one transaction and clears the
checkpoint. Any partial publication failure leaves both cursor and page checkpoint at
the last wholly published boundary. Duplicate notifications and overlapping
Scheduler runs either hold the lease or acknowledge an already active synchronization.

Initial and periodic reconciliation scans unread messages no older than UTC
`activated_at`, at most 10 pages or 500 messages per invocation. A sanitized page
checkpoint allows the next five-minute invocation to continue; only a completed scan
clears it. Completed and `terminal_error` records are skipped. Missing and retryable
records are republished. If Gmail rejects a history cursor as stale, reconciliation
must complete before a fresh watch history position replaces the cursor.

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

## Regional infrastructure and IAM

All proximity-oriented workload resources are in `europe-west3` (Frankfurt): one
private Cloud Run service, regional Firestore in Native mode, scratch Cloud Storage,
Artifact Registry, both Cloud Scheduler jobs, and user-managed Secret Manager
replicas. This is a Prague-proximity choice, not a measured latency guarantee. Gemini
uses its `global` endpoint and is the explicit regional exception. Pub/Sub topics are
global resources with a message-storage policy restricted to `europe-west3`.

The Cloud Run service uses internal ingress, IAM-only invocation, zero minimum
instances, maximum instances `2`, container concurrency `1`, one vCPU, 1 GiB memory,
and a `115s` request timeout. Authenticated smoke originates from an authorized
same-project/internal execution context because ingress is not opened for testing.

Terraform enables only APIs required by the backlog and defines:

- `gmail-notifications` -> `gmail-notifications-push`;
- `email-work` -> `email-work-push`;
- shared `dead-letter` -> pull `dead-letter-monitor`;
- daily watch-renewal and five-minute reconciliation Scheduler jobs;
- regional secret containers without versions or payloads;
- budget alerts, application quota inputs, and the bounded Cloud Run configuration.

There are distinct service accounts for the Cloud Run runtime,
`gmail-notifications-push`, `email-work-push`, Scheduler invocation, and authenticated
smoke. Each invoker gets only `roles/run.invoker` on this service; Pub/Sub's service
agent gets only the token/dead-letter permissions required for authenticated push.
The runtime gets Firestore access, object access only on the scratch bucket, publish
access only on `email-work`, access only to named secrets, Gemini invocation, and
logging/metrics permissions. Scheduler has no runtime data permissions.
`gmail-api-push@system.gserviceaccount.com` receives publisher access only on
`gmail-notifications`.

Terraform runs in CI only through `terraform fmt -check -recursive`,
`terraform init -backend=false`, `terraform validate`, and mocked-provider
`terraform test`. CI never runs `terraform apply`; project selection, apply, secret
versions, OAuth, and deployment require explicit operator approval in issue 13.

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
- HTTP responses are empty except the stable health payload and contain neither
  identifiers nor exception text. Metrics aggregate counts and latency without
  content labels.

## Cost, quota, and time controls

The service optimizes for trial credit and low idle cost, not a wholly free guarantee.
Cloud Run has zero minimum and maximum `2` instances. Model/search calls, Cloud Run,
storage, Pub/Sub, Firestore, logging, and OpenRouter when selected can consume credit
or incur charges.

One message is bounded to five attachment-analysis calls and one reply-generation
call. Attachment concurrency is `2`; reply output is at most 2,048 model tokens and
8,000 rendered characters; search uses at most one enabled call and five citations.
Reconciliation, retries, attempts, message sizes, and retention are bounded as stated
above. Configured provider/project quotas may lower these ceilings but never raise the
application limits without a Spec change.

The coordinator has a `105s` internal processing deadline, including cleanup, below
the `115s` Cloud Run request timeout. It stops starting new model/send work when the
remaining budget cannot finish safely and returns a retryable outcome. Live
acceptance measures end-to-end delivery against `120s`; that is an acceptance target,
not the per-request timeout.

Budget thresholds and recipients are explicit Terraform inputs. Alerts report
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
- [OpenRouter web search](https://openrouter.ai/docs/guides/features/server-tools/web-search)
  for the non-deprecated `openrouter:web_search` server tool.

## Delivery decisions

Backlog issues 01 through 14 remain strictly consecutive. Each dependent issue starts
only after the prior PR is merged and `main` is updated. Its Spec phase updates these
documents, its expected Red demonstrates the missing behavior, its focused and
complete suites finish Green, and its PR records exact sanitized evidence.

Default tests use deterministic fakes and mocked providers. The final black-box layer
runs against `uvicorn` over public HTTP routes and is the Playwright-equivalent test
for this UI-free service. Terraform apply, OAuth mutation, authenticated cloud smoke,
and five live Gmail cases are explicit opt-in gates. The service is deployed and left
running only in issue 13; issue 01 intentionally has no server to run.
