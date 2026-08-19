# Gmail Assistant MVP

## Outcome

One backend service watches a dedicated Gmail inbox, understands the current message
and supported attachments, optionally grounds current facts, and sends one concise
reply in the original thread. The MVP is deployed privately in `europe-west3`; it has
no product UI. The [system design](design.md) is the architecture authority.

## Deployed flow

Gmail push reaches `gmail-notifications`, authenticated Cloud Run synchronization
publishes metadata-only work to `email-work`, and processing uses Firestore for
idempotent state. Attachment bytes visit regional scratch storage only temporarily.
Gemini is the deployed reply provider and uses native Google Search grounding when
needed. OpenRouter with `openrouter:web_search` is an application-supported
alternative that requires reviewed secret/configuration wiring and a new revision.
See the single system diagram in the
[design](design.md#system-flow-and-data-ownership).

## Five-case proof

The sanitized acceptance run on 2026-08-19 proved one correctly threaded reply,
`completed` state, and correct labels for every case within the `120s` target.

| Case | Coverage | Accepted latency |
| --- | --- | ---: |
| Plain | Current-message reply | `69s` |
| PDF | One analyzed attachment | `90s` |
| MP3 + WAV | Two analyzed audio attachments | `69s` |
| JPEG + PNG | Two analyzed image attachments | `51s` |
| Forced-current | Native search with one valid public citation | `62s` |

Exact sanitized results live in the
[test plan](test-plan.md#issue-13-observed-sanitized-acceptance).

## Privacy

Gmail remains the source and reply system of record. Its notification topic carries
only the Gmail-required mailbox address and history ID; the work topic carries opaque
metadata. Firestore stores cursors, leases, states, and sanitized codes. Bodies,
prompts, replies, attachment bytes, and insights are not persisted in these paths or
emitted as evidence; addresses never enter work items, Firestore, logs, or evidence.
Scratch objects are deleted in `finally`, with a one-day lifecycle only as a backstop.

## Reliability

At-least-once delivery is made effectively once with transactional leases, bounded
attempts, deterministic outbound identity, and thread inspection after ambiguous
sends. States are `processing`, `send_pending`, `sent`, `completed`, and
`terminal_error`. Daily watch renewal, five-minute unread reconciliation, and the
shared dead-letter path cover dropped or exhausted delivery.

## Limitations

The MVP serves one dedicated mailbox and only the current message. It supports PDF,
MP3, WAV, JPEG, and PNG within documented limits. It has no full-thread context, RAG,
scraper, separate search service, browser UI, or application-level provider fallback.
Gemini uses a global endpoint, so model processing has no EU-only guarantee. The
image and Cloud Run HTTP startup probe share the exact `GET /health` contract;
production acceptance requires that response from an authenticated same-project
internal request.

## Costs

The accepted deployment scales from zero to one instance and bounds attachment,
generation, search, and output work. The `480 CZK` monthly budget alert reports
spend; it is not a hard cap. Cloud resources, Gemini model/search use, and OpenRouter
when selected can consume trial credit or incur charges.

## Operations and teardown

Routine readiness, authenticated health, OAuth/watch renewal, recovery, provider
switching, quotas, rollback, and ordered teardown are defined in
[operations](operations.md). A successful demo does not tear down or stop the healthy
service or Gmail watch. The timed delivery sequence and fallback rules are in the
[demo runbook](demo-runbook.md).
