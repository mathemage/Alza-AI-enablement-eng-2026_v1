# Gmail Assistant Demo Runbook

This is a `12` minute, read-only demonstration of the deployed MVP and its five
accepted cases. It follows the authoritative [presentation](presentation.md), uses
the [test plan](test-plan.md) for evidence, and delegates every operational mutation
to [operations](operations.md).

## Timeline

| Time | Segment | Action and Expected outcome |
| --- | --- | --- |
| `00:00-01:30` | Preflight | From the repository root, show current Ready/traffic state, private IAM, enabled Scheduler jobs, future-dated Gmail watch, and the reconciled health-route boundary. Expected outcome: the sanitized checks pass and `/healthz` is described accurately as an accepted-image contract behind a Cloud Run reserved path; otherwise use the fallback below. |
| `01:30-02:30` | Outcome and scope | State the one-mailbox, current-message problem and the private backend outcome. Expected outcome: the audience knows what the MVP does and does not do. |
| `02:30-04:00` | Deployed flow | Walk the single diagram in the design from Gmail push through both Pub/Sub paths, Cloud Run, Firestore, scratch storage, native search, recovery, secrets, and observability. Expected outcome: the trust and data boundaries are clear. |
| `04:00-04:45` | Plain case | Show the sanitized `LIVE-01-plain` verifier line. Expected outcome: one reply, same thread, `completed`, correct headers and labels, no attachment, under `120s`. |
| `04:45-05:30` | PDF case | Show `LIVE-01-pdf`. Expected outcome: the plain-case assertions plus exactly one analyzed PDF attachment, under `120s`. |
| `05:30-06:15` | MP3 and WAV case | Show `LIVE-01-audio`. Expected outcome: the plain-case assertions plus exactly two analyzed audio attachments, under `120s`. |
| `06:15-07:00` | JPEG and PNG case | Show `LIVE-01-image`. Expected outcome: the plain-case assertions plus exactly two analyzed image attachments, under `120s`. |
| `07:00-08:00` | Forced-current case | Show `LIVE-01-current`. Expected outcome: the plain-case assertions plus at least one validated public citation from provider-native grounding, under `120s`. |
| `08:00-09:15` | Privacy and reliability | Explain metadata-only persistence, scratch cleanup, transactional states, deterministic send recovery, reconciliation, and dead letters. Expected outcome: no raw content or credential is exposed on screen. |
| `09:15-10:15` | Limitations and Costs | Cover one mailbox, current-message-only context, supported formats, global Gemini processing, no provider fallback, bounded scale/quotas, and the budget alert that does not cap spend. Expected outcome: constraints and cost exposure are explicit. |
| `10:15-11:15` | Operations | Point to OAuth/watch renewal, replay, terminal errors, dead-letter handling, provider switching, quotas, alerts, and rollback in the operations guide. Expected outcome: ownership and recovery paths are clear without running a mutation. |
| `11:15-12:00` | Teardown decision | Explain the ordered teardown path but do not execute it after a successful demo. Expected outcome: the private service, Scheduler jobs, subscriptions, and healthy Gmail watch remain running. |

## Preflight commands

Use the ignored, operator-approved configuration and keep output in the terminal:

```text
uv run pytest -q -s tests/live/test_gcp_acceptance.py --live-config=credentials/live-acceptance.json
uv run pytest -q -s tests/live/test_gmail_acceptance.py --live-config=credentials/live-acceptance.json
```

These checks read the accepted deployment, watch, and existing five source/reply
pairs; they do not send new messages, renew or stop the watch, or change cloud state.
Use the read-only authenticated route control and reserved `/healthz` explanation in
[operations](operations.md#routine-read-only-checks); do not present the platform
`404` as an application outage or claim a live `200`. Never display the ignored
configuration, mailbox content, identifiers, URLs, credentials, or raw cloud
responses.

## Sanitized fallback

If a live read fails, stop the live path and state that current health is unverified.
Show only the committed
[Issue 13 observed sanitized acceptance](test-plan.md#issue-13-observed-sanitized-acceptance).
That evidence is historical, dated 2026-08-19, and illustrates the accepted result;
it is never proof that the service or watch is currently healthy. Do not invent a
pass, expose raw output, resend the five cases, or mutate the deployment to rescue the
demo.

## Presenter guardrails

- Expected outcome for each accepted case is exactly one same-thread reply,
  `completed` state, valid headers/labels, and latency below `120s`; attachment and
  citation counts then match the timeline.
- Limitations and Costs are stated before the close, including that alerts do not cap
  spend and selected providers may incur charges.
- Teardown is a decision, not a demo step. On success, leave the healthy private
  service and Gmail watch running; if teardown is later authorized, follow the
  ordered procedure in [operations](operations.md#ordered-teardown).
