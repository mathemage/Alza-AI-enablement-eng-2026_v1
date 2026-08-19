# Operations and teardown

Use the ignored operator configuration created for deployment. Keep every project,
region, service, mailbox, and credential choice explicit; never copy identifiers,
message data, tokens, or raw command output into committed evidence.

## Routine read-only checks

Set only non-secret placeholders in the shell:

```text
PROJECT_ID="<project-id>"
REGION="europe-west3"
SERVICE="alza-ai"
LIVE_CONFIG="credentials/<operator-config>.json"
```

Describe the service, IAM policy, Scheduler jobs, and subscriptions with explicit
scope. Confirm internal ingress, one ready revision with `100%` traffic, scaling
`0/1`, no `allUsers` or `allAuthenticatedUsers`, enabled jobs, and the expected OIDC,
retry, retention, and dead-letter settings.

```text
gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION"
gcloud run services get-iam-policy "$SERVICE" --project="$PROJECT_ID" --region="$REGION"
gcloud scheduler jobs describe renew-watch --project="$PROJECT_ID" --location="$REGION"
gcloud scheduler jobs describe reconcile-unread --project="$PROJECT_ID" --location="$REGION"
gcloud pubsub subscriptions describe gmail-notifications-push --project="$PROJECT_ID"
gcloud pubsub subscriptions describe email-work-push --project="$PROJECT_ID"
gcloud pubsub subscriptions describe dead-letter-monitor --project="$PROJECT_ID"
uv run pytest -q -s tests/live/test_gcp_acceptance.py --live-config="$LIVE_CONFIG"
uv run pytest -q -s tests/live/test_gmail_acceptance.py --live-config="$LIVE_CONFIG"
```

The Gmail check reads the future watch expiry, activation state, labels, and accepted
case records; it sends no message and does not renew the watch. The service's
HTTP startup probe and authenticated health check both use the Cloud Run-compatible
`GET /health` route and require exact `200 {"status":"ok"}`.

Run the non-mutating health check from an approved same-project internal execution
context:

```text
SERVICE_URL="$(gcloud run services describe "$SERVICE" --project="$PROJECT_ID" --region="$REGION" --format='value(status.url)')"
```

Inject only `SERVICE_URL` into the approved internal executor. Inside that executor,
mint the approved executor identity token without displaying it:

```text
METADATA_ROOT='http://metadata.google.internal/computeMetadata/v1'
IDENTITY_TOKEN="$(curl --fail --silent --show-error --header 'Metadata-Flavor: Google' "$METADATA_ROOT/instance/service-accounts/default/identity?audience=$SERVICE_URL&format=full")"
HEALTH_RESULT="$(curl --silent --show-error --write-out '\n%{http_code}' --header "Authorization: Bearer $IDENTITY_TOKEN" "$SERVICE_URL/health")"
HEALTH_STATUS="${HEALTH_RESULT##*$'\n'}"
HEALTH_BODY="${HEALTH_RESULT%$'\n'*}"
test "$HEALTH_STATUS" = '200'
test "$HEALTH_BODY" = '{"status":"ok"}'
unset HEALTH_BODY HEALTH_RESULT HEALTH_STATUS IDENTITY_TOKEN METADATA_ROOT
```

Internal ingress and Cloud Run IAM are independent checks; a valid token from the
public internet is insufficient. If no approved internal executor exists, rely on
Ready/traffic and startup-probe state, and report current authenticated health as
unverified; do not open ingress merely to probe. Routine observation must not call
`renew-watch`, `reconcile-unread`, or `users.stop`. Google documents why the former
`/healthz` route is unusable under its
[Cloud Run reserved-path limitation](https://cloud.google.com/run/docs/known-issues#reserved-url-paths).
Do not restore it as an alias.

Use Cloud Monitoring to read the undelivered-message count for
`dead-letter-monitor`, and Cloud Logging only through sanitized structured fields.
Do not paste log entries into evidence without checking their field allowlist.

## OAuth and watch lifecycle

OAuth bootstrap requests offline access to only `gmail.modify`, verifies the exact
dedicated mailbox, and writes its refresh-token document to an explicit ignored
path. Testing-mode grants can expire after seven days; Production consent is required
for unattended operation unless that risk is deliberately accepted.

```text
uv run alza-ai oauth bootstrap --client-secrets "<ignored-client-json>" --expected-account "<mailbox>" --token-output "<ignored-token-json>"
```

Add rotated OAuth documents as new Secret Manager secret versions outside Terraform
state. Deploy or restart through a reviewed immutable revision, verify the Gmail
profile and labels, then deliberately run the `renew-watch` Scheduler job. The daily
`renew-watch` job runs at `03:00 UTC`; `reconcile-unread` runs every five minutes and
recovers eligible unread mail without advancing the history cursor. A manual run is a
mutation and is for activation or recovery, not health checking.

For reauthorization: pause both jobs, create and verify the replacement OAuth grant,
add the replacement secret version, roll out and verify the revision, renew the watch,
then re-enable the jobs. Do not revoke the old grant until the replacement watch is
healthy.

## Failures, replay, and dead letters

A retryable failure returns `503` and retains recoverable Firestore state. Pub/Sub or
Scheduler performs bounded redelivery; do not publish a duplicate while that delivery
is active. Ambiguous Gmail sends are recovered by thread inspection rather than a
blind resend.

A `terminal_error` is different: the source has `AI/Error`, remains unread, is
acknowledged, and is skipped by reconciliation. It is not a replay queue. Diagnose
the sanitized error code and, after remediation, submit a new source message. There
is no supported ad-hoc Firestore reset.

The two primary subscriptions forward exhausted unacknowledged deliveries to the
shared `dead-letter` topic, observed through `dead-letter-monitor`. Pulling a message
leases it, so inspect only a small batch without automatic acknowledgement. Payloads
contain no body or attachment, but a Gmail notification can contain the mailbox
address; sanitize all output. Determine the original path, fix the cause, and
republish the unchanged envelope to exactly one of `gmail-notifications` or
`email-work`. Acknowledge the dead letter only after the replacement publication
succeeds and its durable result is verified. Never replay a terminal record,
generated reply, MIME body, or attachment.

## Provider, quota, and cost changes

The accepted deployment has `RESPONSE_PROVIDER=gemini` and
`GEMINI_MODEL=gemini-3.6-flash`. OpenRouter support is dormant: switching requires a
reviewed configuration change, an `openrouter-api-key` secret version added outside
Terraform, an `OPENROUTER_API_KEY` Secret Manager reference, the selected model, and a
new digest-pinned immutable revision. Verify private IAM, Ready/traffic state, the
`/health` startup probe and authenticated exact response, and a controlled live case
before moving all traffic.
There is no application-level provider fallback, and attachment analysis still uses
Gemini at its global endpoint after reply generation switches to OpenRouter.

The accepted service uses maximum one instance, concurrency one, a `115s` request
timeout, and per-message quota ceilings of five attachment calls, one reply call, one
search-enabled call, and 2,048 output tokens. Lowering a ceiling is a reviewed deploy;
raising it requires a design change. The `480 CZK` monthly budget alert is notification
only, not a hard cap. Scaling, quotas, provider limits, and the stop procedure are the
actual cost controls; Gemini, search, Cloud Run, storage, and OpenRouter can incur
charges.

## Rollback

Rollback never changes internal ingress or grants a public invoker. Pause both
Scheduler jobs, stop new Gmail notifications, and let or deliberately quiesce pending
Pub/Sub work. Move `100%` traffic to a previously accepted immutable digest, verify
Ready/traffic state, its configured startup probe, authenticated exact `GET /health`,
and the Gmail profile, then restore push delivery, renew the watch if required, and
resume the jobs. If no healthy revision exists, keep processing stopped and proceed
to teardown. Disable superseded secret versions only after the restored revision is
proven healthy.

## Ordered teardown

Teardown is destructive and is never part of a routine check or live demo.

1. Preserve the ignored deployment variables and local Terraform state, select the
   exact project, and obtain approval for the destroy plan.
2. Pause `renew-watch` and `reconcile-unread` so neither can recreate work.
3. Call the Gmail API `users.stop(userId="me")` operation (`users.stop`) with the
   verified mailbox credential. Confirm the watch is stopped before revoking OAuth.
4. Quiesce both push paths, resolve or explicitly discard remaining primary and
   dead-letter deliveries, and record only sanitized counts.
5. Make no further service invocation. List the scratch bucket and remove approved
   remaining objects; `force_destroy=false` otherwise blocks bucket deletion.
6. Disable or destroy out-of-band secret versions and revoke the OAuth grant now that
   the watch and processing paths are stopped.
7. Review `terraform -chdir=infra plan -destroy` using the same ignored inputs, then
   run `terraform destroy` from `infra/` with that same local state. Confirm deletion
   of Cloud Run, Scheduler, Pub/Sub resources, Firestore, scratch storage, Artifact
   Registry, Secret Manager containers, service accounts, IAM grants, and the budget.
8. Remove ignored local OAuth/provider files. Delete local Terraform state and plans
   only after the cloud inventory is confirmed absent.

## Residual data

Terraform deletion does not mean that every related datum disappears immediately:

| Owner | Residual data and required decision |
| --- | --- |
| Gmail | Source messages, sent replies, `AI/Processed`/`AI/Error` labels, and mailbox history remain until the mailbox operator deletes them. |
| Firestore | Operational metadata is deleted with the database; confirm deletion and account for provider recovery/retention behavior. |
| Pub/Sub | Topics, subscriptions, and retained deliveries are deleted; export nothing containing mailbox or message metadata. |
| Scratch Cloud Storage | Normal cleanup and the one-day lifecycle should leave no objects, but the bucket must be empty because `force_destroy=false`. |
| Artifact Registry | The deployed image and digest are removed with the repository; separately retained copies remain their owner's responsibility. |
| Secret Manager | Secret containers are Terraform-managed, while secret versions were added out of band and must be disabled or destroyed explicitly. |
| Telemetry | Cloud Logging logs and Cloud Monitoring metrics follow project retention and are not erased by service deletion. |
| Billing and APIs | The budget alert is removed, but charges already incurred remain; enabled project APIs remain enabled because Terraform uses `disable_on_destroy=false`. |
| OAuth and providers | The OAuth grant persists until revoked. Gmail, Gemini, Google Search, or OpenRouter may retain provider-managed data under their terms. |
| Operator workstation | Ignored credentials, provider keys, acceptance configuration, cached tools, destroy plans, and local Terraform state require explicit secure removal. |
