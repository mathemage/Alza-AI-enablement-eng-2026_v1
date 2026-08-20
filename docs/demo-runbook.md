# Gmail Assistant Demo Runbook

This is a `13` minute demonstration of the deployed MVP and its five accepted cases.
Every step is read-only except the sender allowlist segment, whose single reversible
document edit is the only permitted mutation. It follows the authoritative
[presentation](presentation.md), uses the [test plan](test-plan.md) for evidence, and
delegates every other operational mutation to [operations](operations.md).

## Timeline

| Time | Segment | Action and Expected outcome |
| --- | --- | --- |
| `00:00-01:30` | Preflight | From the repository root, show current Ready/traffic state, private IAM, the `/health` HTTP startup probe, exact authenticated health response, enabled Scheduler jobs, and future-dated Gmail watch. Expected outcome: every sanitized check passes; otherwise use the fallback below. |
| `01:30-02:30` | Outcome and scope | State the one-mailbox, current-message problem and the private backend outcome. Expected outcome: the audience knows what the MVP does and does not do. |
| `02:30-04:00` | Deployed flow | Walk the single diagram in the design from Gmail push through both Pub/Sub paths, Cloud Run, Firestore, scratch storage, native search, recovery, secrets, and observability. Expected outcome: the trust and data boundaries are clear. |
| `04:00-04:45` | Plain case | Show the sanitized `LIVE-01-plain` verifier line. Expected outcome: one reply, same thread, `completed`, correct headers and labels, no attachment, under `120s`. |
| `04:45-05:30` | PDF case | Show `LIVE-01-pdf`. Expected outcome: the plain-case assertions plus exactly one analyzed PDF attachment, under `120s`. |
| `05:30-06:15` | MP3 and WAV case | Show `LIVE-01-audio`. Expected outcome: the plain-case assertions plus exactly two analyzed audio attachments, under `120s`. |
| `06:15-07:00` | JPEG and PNG case | Show `LIVE-01-image`. Expected outcome: the plain-case assertions plus exactly two analyzed image attachments, under `120s`. |
| `07:00-08:00` | Forced-current case | Show `LIVE-01-current`. Expected outcome: the plain-case assertions plus at least one validated public citation from provider-native grounding, under `120s`. |
| `08:00-09:15` | Sender allowlist | Show `uv run alza-ai allowlist list --project alza-ai-email-bot-2026`, then admit a requested sender with `uv run alza-ai allowlist add --project alza-ai-email-bot-2026 <entry>`, using an address or a whole domain such as `@alza.cz`. Expected outcome: the printed entries include the new one, the next message from that sender is answered, and no deployment, restart, or secret version was needed. Ask for a new message rather than a resend, because an already-rejected message keeps its terminal record. |
| `09:15-10:30` | Privacy and reliability | Explain metadata-only persistence, scratch cleanup, transactional states, deterministic send recovery, reconciliation, and dead letters. Expected outcome: no raw content or credential is exposed on screen. |
| `10:30-11:30` | Limitations and Costs | Cover one mailbox, current-message-only context, supported formats, global Gemini processing, no provider fallback, bounded scale/quotas, and the budget alert that does not cap spend. Expected outcome: constraints and cost exposure are explicit. |
| `11:30-12:30` | Operations | Point to OAuth/watch renewal, replay, terminal errors, dead-letter handling, provider switching, quotas, alerts, and rollback in the operations guide. Expected outcome: ownership and recovery paths are clear without running a mutation. |
| `12:30-13:15` | Teardown decision | Explain the ordered teardown path but do not execute it after a successful demo. Expected outcome: the private service, Scheduler jobs, subscriptions, and healthy Gmail watch remain running. |

## Preflight commands

Use the ignored, operator-approved configuration and keep output in the terminal:

```text
uv run pytest -q -s tests/live/test_gcp_acceptance.py --live-config=credentials/live-acceptance.json
uv run pytest -q -s tests/live/test_gmail_acceptance.py --live-config=credentials/live-acceptance.json
```

These checks read the accepted deployment, watch, and existing five source/reply
pairs; they do not send new messages, renew or stop the watch, or change cloud state.
The allowlist commands in [operations](operations.md#sender-allowlist) are the only
demo commands that write, and they touch one Firestore document.
Use the read-only authenticated `GET /health` procedure in
[operations](operations.md#routine-read-only-checks) and require exact
`200 {"status":"ok"}`. Never display the ignored configuration, mailbox content,
identifiers, URLs, credentials, or raw cloud responses.

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
- The allowlist segment adds one entry to `runtime-config/sender-policy` and nothing
  else. Never widen it to a public mail domain, never show the document contents beyond
  the printed entries, and undo an entry added only for the demo with
  `uv run alza-ai allowlist remove --project alza-ai-email-bot-2026 <entry>`.
- Limitations and Costs are stated before the close, including that alerts do not cap
  spend and selected providers may incur charges.
- Teardown is a decision, not a demo step. On success, leave the healthy private
  service and Gmail watch running; if teardown is later authorized, follow the
  ordered procedure in [operations](operations.md#ordered-teardown).
