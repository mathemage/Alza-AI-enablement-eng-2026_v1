from time import monotonic

import httpx
import pytest

from tests.live.support import (
    LiveConfig,
    LiveFailure,
    gcloud,
    gcloud_json,
    mapping,
    require,
    sequence,
)


def test_live_13_authenticated_preflight(live_config: LiveConfig) -> None:
    started = monotonic()
    try:
        accounts = sequence(
            gcloud_json(
                ["auth", "list", "--filter=status:ACTIVE"],
                "gcloud_identity_unavailable",
            )
        )
        require(len(accounts) == 1, "gcloud_identity_ambiguous")
        require(
            mapping(accounts[0]).get("account") == live_config.account,
            "gcloud_identity_mismatch",
        )

        token = gcloud(["auth", "application-default", "print-access-token"])
        require(token.returncode == 0 and bool(token.stdout.strip()), "adc_unavailable")
        try:
            response = httpx.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {token.stdout.strip()}"},
                timeout=20,
            )
            adc_profile = response.json() if response.status_code == 200 else None
        except httpx.HTTPError, ValueError:
            raise LiveFailure("adc_identity_unavailable") from None
        require(
            isinstance(adc_profile, dict)
            and adc_profile.get("email") == live_config.account,
            "adc_identity_mismatch",
        )

        active_project = gcloud(["config", "get-value", "project"])
        require(
            active_project.returncode == 0
            and active_project.stdout.strip() == live_config.project_id,
            "active_project_mismatch",
        )
        active_region = gcloud(["config", "get-value", "compute/region"])
        require(
            active_region.returncode == 0
            and active_region.stdout.strip() == live_config.region == "europe-west3",
            "active_region_mismatch",
        )

        project = mapping(
            gcloud_json(
                ["projects", "describe", live_config.project_id],
                "project_unavailable",
            )
        )
        require(project.get("projectId") == live_config.project_id, "project_mismatch")
        require(
            str(project.get("projectNumber")) == live_config.project_number,
            "project_number_mismatch",
        )
        require(project.get("lifecycleState") == "ACTIVE", "project_inactive")

        project_billing = mapping(
            gcloud_json(
                ["billing", "projects", "describe", live_config.project_id],
                "project_billing_unavailable",
            )
        )
        require(
            project_billing.get("billingEnabled") is True, "project_billing_disabled"
        )
        require(
            project_billing.get("billingAccountName")
            == f"billingAccounts/{live_config.billing_account_id}",
            "project_billing_mismatch",
        )
        billing = mapping(
            gcloud_json(
                ["billing", "accounts", "describe", live_config.billing_account_id],
                "billing_account_unavailable",
            )
        )
        require(billing.get("open") is True, "billing_account_closed")
        require(live_config.cost_approved, "cost_approval_missing")
    except LiveFailure as error:
        pytest.fail(error.code, pytrace=False)
    elapsed_ms = int((monotonic() - started) * 1000)
    print(
        "PREFLIGHT pass=true identity_match=true adc_match=true project_match=true "
        f"billing_match=true region_match=true mailbox_confirmed=true cost_approved=true elapsed_ms={elapsed_ms}"
    )


def test_live_13_authenticated_smoke(live_config: LiveConfig) -> None:
    started = monotonic()
    try:
        service = mapping(
            gcloud_json(
                [
                    "run",
                    "services",
                    "describe",
                    live_config.service_name,
                    f"--project={live_config.project_id}",
                    f"--region={live_config.region}",
                ],
                "cloud_run_service_absent",
            )
        )
        metadata = mapping(service.get("metadata"))
        annotations = mapping(metadata.get("annotations"))
        require(
            annotations.get("run.googleapis.com/ingress") == "internal",
            "cloud_run_ingress_invalid",
        )
        template = mapping(mapping(service.get("spec")).get("template"))
        template_metadata = mapping(template.get("metadata"))
        template_annotations = mapping(template_metadata.get("annotations"))
        require(
            template_annotations.get("autoscaling.knative.dev/minScale", "0") == "0",
            "cloud_run_min_scale_invalid",
        )
        require(
            annotations.get("run.googleapis.com/maxScale") == "1",
            "cloud_run_max_scale_invalid",
        )
        template_spec = mapping(template.get("spec"))
        require(
            template_spec.get("containerConcurrency") == 1,
            "cloud_run_concurrency_invalid",
        )
        require(template_spec.get("timeoutSeconds") == 115, "cloud_run_timeout_invalid")
        require(
            template_spec.get("serviceAccountName")
            == f"alza-ai-runtime@{live_config.project_id}.iam.gserviceaccount.com",
            "cloud_run_runtime_identity_invalid",
        )
        containers = sequence(template_spec.get("containers"))
        require(len(containers) == 1, "cloud_run_container_invalid")
        container = mapping(containers[0])
        image = container.get("image")
        require(
            isinstance(image, str) and "@sha256:" in image,
            "cloud_run_image_not_immutable",
        )
        startup_probe = mapping(
            container.get("startupProbe"), "cloud_run_startup_probe_invalid"
        )
        startup_http_get = mapping(
            startup_probe.get("httpGet"), "cloud_run_startup_probe_invalid"
        )
        require(
            startup_probe.get("failureThreshold") == 5
            and startup_probe.get("initialDelaySeconds") == 10
            and startup_probe.get("timeoutSeconds") == 3
            and startup_probe.get("periodSeconds") == 3
            and startup_http_get.get("path") == "/health"
            and startup_http_get.get("port") == 8080,
            "cloud_run_startup_probe_invalid",
        )
        environment = {
            item.get("name"): item
            for value in sequence(container.get("env"))
            if isinstance(value, dict)
            for item in [mapping(value)]
        }
        for name, secret in {
            "GMAIL_OAUTH_CLIENT_JSON": "gmail-oauth-client",
            "GMAIL_REFRESH_TOKEN_JSON": "gmail-refresh-token",
        }.items():
            reference = mapping(
                mapping(environment.get(name)).get("valueFrom"),
                "cloud_run_secret_reference_invalid",
            )
            secret_reference = mapping(
                reference.get("secretKeyRef"), "cloud_run_secret_reference_invalid"
            )
            require(
                secret_reference.get("name") == secret
                and secret_reference.get("key") == "latest",
                "cloud_run_secret_reference_invalid",
            )
        status = mapping(service.get("status"))
        conditions = sequence(status.get("conditions"))
        require(
            any(
                mapping(condition).get("type") == "Ready"
                and mapping(condition).get("status") == "True"
                for condition in conditions
            ),
            "cloud_run_revision_not_ready",
        )
        service_url = status.get("url")
        require(isinstance(service_url, str), "cloud_run_url_invalid")
        traffic = sequence(status.get("traffic"))
        require(
            len(traffic) == 1
            and mapping(traffic[0]).get("percent") == 100
            and mapping(traffic[0]).get("revisionName")
            == status.get("latestReadyRevisionName"),
            "cloud_run_traffic_invalid",
        )
        expected_environment = {
            "ALZA_ENV": "production",
            "MAX_ATTACHMENT_ANALYSIS_CALLS": "5",
            "MAX_REPLY_GENERATION_CALLS": "1",
            "MAX_REPLY_OUTPUT_TOKENS": "2048",
            "MAX_SEARCH_CALLS": "1",
        }
        require(
            all(
                mapping(environment.get(name)).get("value") == value
                for name, value in expected_environment.items()
            ),
            "cloud_run_quota_environment_invalid",
        )

        policy = mapping(
            gcloud_json(
                [
                    "run",
                    "services",
                    "get-iam-policy",
                    live_config.service_name,
                    f"--project={live_config.project_id}",
                    f"--region={live_config.region}",
                ],
                "cloud_run_iam_unavailable",
            )
        )
        bindings = sequence(policy.get("bindings", []))
        members = {
            member
            for binding in bindings
            for member in sequence(mapping(binding).get("members", []))
            if isinstance(member, str)
        }
        require(
            not {"allUsers", "allAuthenticatedUsers"}.intersection(members),
            "cloud_run_public_invoker",
        )

        scheduler_jobs = sequence(
            gcloud_json(
                [
                    "scheduler",
                    "jobs",
                    "list",
                    f"--project={live_config.project_id}",
                    f"--location={live_config.region}",
                ],
                "scheduler_jobs_unavailable",
            )
        )
        jobs = {
            str(mapping(job).get("name", "")).rsplit("/", 1)[-1]: mapping(job)
            for job in scheduler_jobs
        }
        expected_jobs = {
            "renew-watch": ("0 3 * * *", "/jobs/renew-watch"),
            "reconcile-unread": ("*/5 * * * *", "/jobs/reconcile-unread"),
        }
        require(set(jobs) == set(expected_jobs), "scheduler_job_inventory_invalid")
        for name, (schedule, route) in expected_jobs.items():
            job = jobs[name]
            target = mapping(job.get("httpTarget"))
            oidc = mapping(target.get("oidcToken"))
            require(
                job.get("state") == "ENABLED"
                and job.get("schedule") == schedule
                and job.get("timeZone") == "Etc/UTC"
                and target.get("httpMethod") == "POST"
                and target.get("uri") == f"{service_url}{route}"
                and oidc.get("audience") == service_url
                and oidc.get("serviceAccountEmail")
                == f"scheduler-invoker@{live_config.project_id}.iam.gserviceaccount.com",
                "scheduler_job_configuration_invalid",
            )

        expected_subscriptions = {
            "gmail-notifications-push": (
                "gmail-notifications",
                "/events/gmail",
                "gmail-notifications-push",
            ),
            "email-work-push": (
                "email-work",
                "/jobs/process-message",
                "email-work-push",
            ),
        }
        for subscription_name, (
            topic_name,
            route,
            identity,
        ) in expected_subscriptions.items():
            subscription = mapping(
                gcloud_json(
                    [
                        "pubsub",
                        "subscriptions",
                        "describe",
                        subscription_name,
                        f"--project={live_config.project_id}",
                    ],
                    "pubsub_subscription_unavailable",
                )
            )
            push = mapping(subscription.get("pushConfig"))
            oidc = mapping(push.get("oidcToken"))
            dead_letter = mapping(subscription.get("deadLetterPolicy"))
            retry = mapping(subscription.get("retryPolicy"))
            require(
                subscription.get("topic")
                == f"projects/{live_config.project_id}/topics/{topic_name}"
                and subscription.get("ackDeadlineSeconds") == 120
                and subscription.get("messageRetentionDuration") == "604800s"
                and push.get("pushEndpoint") == f"{service_url}{route}"
                and oidc.get("audience") == service_url
                and oidc.get("serviceAccountEmail")
                == f"{identity}@{live_config.project_id}.iam.gserviceaccount.com"
                and dead_letter.get("deadLetterTopic")
                == f"projects/{live_config.project_id}/topics/dead-letter"
                and dead_letter.get("maxDeliveryAttempts") == 5
                and retry.get("minimumBackoff") == "10s"
                and retry.get("maximumBackoff") == "600s",
                "pubsub_subscription_configuration_invalid",
            )

        budgets = sequence(
            gcloud_json(
                [
                    "billing",
                    "budgets",
                    "list",
                    f"--billing-account={live_config.billing_account_id}",
                    '--filter=displayName="Alza AI monthly exposure alert"',
                ],
                "billing_budget_unavailable",
            )
        )
        require(len(budgets) == 1, "billing_budget_inventory_invalid")
        amount = mapping(mapping(budgets[0]).get("amount"))
        specified_amount = mapping(amount.get("specifiedAmount"))
        budget_filter = mapping(mapping(budgets[0]).get("budgetFilter"))
        require(
            specified_amount.get("currencyCode") == live_config.budget_currency
            and specified_amount.get("units") == str(live_config.monthly_budget_amount)
            and budget_filter.get("projects")
            == [f"projects/{live_config.project_number}"],
            "billing_budget_configuration_invalid",
        )
    except LiveFailure as error:
        elapsed_ms = int((monotonic() - started) * 1000)
        print(f"AUTH-SMOKE pass=false code={error.code} elapsed_ms={elapsed_ms}")
        pytest.fail(error.code, pytrace=False)
    elapsed_ms = int((monotonic() - started) * 1000)
    print(
        "AUTH-SMOKE pass=true private=true immutable=true ready=true traffic=true "
        "scaling=true timeout=true health_probe=true quotas=true scheduler=true "
        f"subscriptions=true budget=true public_invoker=false elapsed_ms={elapsed_ms}"
    )
