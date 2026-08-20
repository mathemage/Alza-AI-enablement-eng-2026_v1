mock_provider "google" {
  override_during = plan
}

override_resource {
  target          = google_cloud_run_v2_service.app
  override_during = plan
  values = {
    uri = "https://alza-ai-example.europe-west3.run.app"
  }
}

override_resource {
  target          = google_pubsub_topic.messaging["gmail-notifications"]
  override_during = plan
  values = {
    id = "projects/example-project/topics/gmail-notifications"
  }
}

override_resource {
  target          = google_pubsub_topic.messaging["email-work"]
  override_during = plan
  values = {
    id = "projects/example-project/topics/email-work"
  }
}

override_resource {
  target          = google_pubsub_topic.messaging["dead-letter"]
  override_during = plan
  values = {
    id = "projects/example-project/topics/dead-letter"
  }
}

override_resource {
  target          = google_service_account.identities["runtime"]
  override_during = plan
  values = {
    email  = "alza-ai-runtime@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:alza-ai-runtime@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/alza-ai-runtime@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target          = google_service_account.identities["gmail_push"]
  override_during = plan
  values = {
    email  = "gmail-notifications-push@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:gmail-notifications-push@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/gmail-notifications-push@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target          = google_service_account.identities["work_push"]
  override_during = plan
  values = {
    email  = "email-work-push@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:email-work-push@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/email-work-push@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target          = google_service_account.identities["scheduler"]
  override_during = plan
  values = {
    email  = "scheduler-invoker@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:scheduler-invoker@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/scheduler-invoker@example-project.iam.gserviceaccount.com"
  }
}

override_resource {
  target          = google_service_account.identities["smoke"]
  override_during = plan
  values = {
    email  = "authenticated-smoke@example-project.iam.gserviceaccount.com"
    member = "serviceAccount:authenticated-smoke@example-project.iam.gserviceaccount.com"
    name   = "projects/example-project/serviceAccounts/authenticated-smoke@example-project.iam.gserviceaccount.com"
  }
}

variables {
  project_id          = "example-project"
  project_number      = "123456789012"
  billing_account_id  = "000000-000000-000000"
  container_image     = "europe-west3-docker.pkg.dev/example-project/alza-ai/service@sha256:0000000000000000000000000000000000000000000000000000000000000000"
  scratch_bucket_name = "example-project-alza-ai-scratch"
  budget_notification_channel_ids = [
    "projects/example-project/notificationChannels/1234567890",
  ]
}

run "GCP_01_builds_the_regional_private_foundation" {
  command = plan

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.app.location == var.region,
      google_firestore_database.app.location_id == var.region,
      lower(google_storage_bucket.scratch.location) == var.region,
      google_artifact_registry_repository.app.location == var.region,
      alltrue([
        for secret in values(google_secret_manager_secret.runtime) :
        secret.replication[0].user_managed[0].replicas[0].location == var.region
      ]),
    ])
    error_message = "Every proximity-oriented resource and secret replica must use europe-west3."
  }

  assert {
    condition = alltrue([
      google_cloud_run_v2_service.app.ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY",
      google_cloud_run_v2_service.app.invoker_iam_disabled == false,
      google_cloud_run_v2_service.app.scaling[0].min_instance_count == 0,
      google_cloud_run_v2_service.app.scaling[0].max_instance_count == var.cloud_run_max_instances,
      google_cloud_run_v2_service.app.template[0].max_instance_request_concurrency == 1,
      google_cloud_run_v2_service.app.template[0].timeout == "115s",
      google_cloud_run_v2_service.app.template[0].containers[0].resources[0].limits["cpu"] == "1",
      google_cloud_run_v2_service.app.template[0].containers[0].resources[0].limits["memory"] == "1Gi",
    ])
    error_message = "Cloud Run must remain private and within its frozen compute and timeout bounds."
  }

  assert {
    condition = try(alltrue([
      length(google_cloud_run_v2_service.app.template[0].containers[0].startup_probe) == 1,
      google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].failure_threshold == 5,
      google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].initial_delay_seconds == 10,
      google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].timeout_seconds == 3,
      google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].period_seconds == 3,
      length(google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].http_get) == 1,
      google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].http_get[0].path == "/health",
      google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].http_get[0].port == 8080,
      length(google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].tcp_socket) == 0,
      length(google_cloud_run_v2_service.app.template[0].containers[0].startup_probe[0].grpc) == 0,
    ]), false)
    error_message = "Cloud Run must gate startup on the HTTP /health readiness contract."
  }

  assert {
    condition = alltrue([
      google_firestore_database.app.name == "(default)",
      google_firestore_database.app.type == "FIRESTORE_NATIVE",
      google_artifact_registry_repository.app.repository_id == "alza-ai",
      google_artifact_registry_repository.app.format == "DOCKER",
      google_storage_bucket.scratch.uniform_bucket_level_access,
      google_storage_bucket.scratch.public_access_prevention == "enforced",
      one(google_storage_bucket.scratch.lifecycle_rule[0].action).type == "Delete",
      one(google_storage_bucket.scratch.lifecycle_rule[0].condition).age == 1,
    ])
    error_message = "The regional data resources must retain their private, minimal configuration."
  }

  assert {
    condition = (
      toset(keys(google_secret_manager_secret.runtime)) ==
      toset(["gmail-oauth-client", "gmail-refresh-token", "openrouter-api-key"])
    )
    error_message = "Terraform must create exactly the three named secret containers."
  }
}

run "GCP_02_builds_bounded_authenticated_message_paths" {
  command = plan

  assert {
    condition = (
      toset(keys(google_pubsub_topic.messaging)) ==
      toset(["gmail-notifications", "email-work", "dead-letter"])
    )
    error_message = "Exactly two primary topics and the shared dead-letter topic must exist."
  }

  assert {
    condition = alltrue([
      for topic in values(google_pubsub_topic.messaging) :
      toset(topic.message_storage_policy[0].allowed_persistence_regions) == toset([var.region])
    ])
    error_message = "Every Pub/Sub topic must restrict persistence to europe-west3."
  }

  assert {
    condition = (
      toset(keys(google_pubsub_subscription.primary)) ==
      toset(["gmail-notifications", "email-work"])
    )
    error_message = "Exactly the two primary push subscriptions must exist."
  }

  assert {
    condition = alltrue([
      for subscription in values(google_pubsub_subscription.primary) : alltrue([
        subscription.ack_deadline_seconds == 120,
        subscription.message_retention_duration == "604800s",
        subscription.retry_policy[0].minimum_backoff == "10s",
        subscription.retry_policy[0].maximum_backoff == "600s",
        subscription.dead_letter_policy[0].max_delivery_attempts == 5,
        subscription.dead_letter_policy[0].dead_letter_topic == google_pubsub_topic.messaging["dead-letter"].id,
        subscription.push_config[0].oidc_token[0].audience == google_cloud_run_v2_service.app.uri,
      ])
    ])
    error_message = "Both primary subscriptions must have the frozen retry, retention, dead-letter, and OIDC policy."
  }

  assert {
    condition = alltrue([
      google_pubsub_subscription.primary["gmail-notifications"].name == "gmail-notifications-push",
      endswith(
        google_pubsub_subscription.primary["gmail-notifications"].push_config[0].push_endpoint,
        "/events/gmail",
      ),
      google_pubsub_subscription.primary["gmail-notifications"].push_config[0].oidc_token[0].service_account_email == google_service_account.identities["gmail_push"].email,
      google_pubsub_subscription.primary["email-work"].name == "email-work-push",
      endswith(
        google_pubsub_subscription.primary["email-work"].push_config[0].push_endpoint,
        "/jobs/process-message",
      ),
      google_pubsub_subscription.primary["email-work"].push_config[0].oidc_token[0].service_account_email == google_service_account.identities["work_push"].email,
    ])
    error_message = "Each push path must use its frozen route and dedicated OIDC identity."
  }

  assert {
    condition = alltrue([
      google_pubsub_subscription.dead_letter_monitor.name == "dead-letter-monitor",
      google_pubsub_subscription.dead_letter_monitor.topic == google_pubsub_topic.messaging["dead-letter"].id,
      google_pubsub_subscription.dead_letter_monitor.message_retention_duration == "604800s",
      length(google_pubsub_subscription.dead_letter_monitor.push_config) == 0,
    ])
    error_message = "The shared dead-letter path must terminate in one seven-day pull subscription."
  }
}

run "FAIL_03_forwards_exhausted_deliveries_to_the_shared_monitor" {
  command = plan

  assert {
    condition = alltrue([
      for subscription in values(google_pubsub_subscription.primary) : alltrue([
        subscription.dead_letter_policy[0].max_delivery_attempts == 5,
        subscription.dead_letter_policy[0].dead_letter_topic == google_pubsub_topic.messaging["dead-letter"].id,
      ])
    ])
    error_message = "Both unacknowledged primary paths must forward after five attempts."
  }

  assert {
    condition = alltrue([
      google_pubsub_subscription.dead_letter_monitor.topic == google_pubsub_topic.messaging["dead-letter"].id,
      google_pubsub_subscription.dead_letter_monitor.message_retention_duration == "604800s",
      length(google_pubsub_subscription.dead_letter_monitor.push_config) == 0,
    ])
    error_message = "Exhausted deliveries must remain observable in the seven-day pull monitor."
  }
}

run "GCP_02_authenticates_scheduler_paths" {
  command = plan

  assert {
    condition = (
      toset(keys(google_cloud_scheduler_job.jobs)) ==
      toset(["renew-watch", "reconcile-unread"])
    )
    error_message = "Exactly the two frozen Scheduler jobs must exist."
  }

  assert {
    condition = alltrue([
      for job in values(google_cloud_scheduler_job.jobs) : alltrue([
        job.region == var.region,
        job.time_zone == "Etc/UTC",
        job.http_target[0].http_method == "POST",
        job.http_target[0].oidc_token[0].service_account_email == google_service_account.identities["scheduler"].email,
        job.http_target[0].oidc_token[0].audience == google_cloud_run_v2_service.app.uri,
      ])
    ])
    error_message = "Both regional Scheduler jobs must use the dedicated OIDC identity and exact audience."
  }

  assert {
    condition = alltrue([
      google_cloud_scheduler_job.jobs["renew-watch"].schedule == "0 3 * * *",
      endswith(google_cloud_scheduler_job.jobs["renew-watch"].http_target[0].uri, "/jobs/renew-watch"),
      google_cloud_scheduler_job.jobs["reconcile-unread"].schedule == "*/5 * * * *",
      endswith(google_cloud_scheduler_job.jobs["reconcile-unread"].http_target[0].uri, "/jobs/reconcile-unread"),
    ])
    error_message = "Scheduler cadence and routes must match the frozen recovery paths."
  }
}

run "GCP_02_limits_each_identity" {
  command = plan

  assert {
    condition = (
      toset(keys(google_service_account.identities)) ==
      toset(["runtime", "gmail_push", "work_push", "scheduler", "smoke"])
    )
    error_message = "Runtime and each logical invoker must have a distinct service account."
  }

  assert {
    condition = alltrue([
      length(google_cloud_run_v2_service_iam_member.invokers) == 4,
      alltrue([
        for grant in values(google_cloud_run_v2_service_iam_member.invokers) : alltrue([
          grant.role == "roles/run.invoker",
          startswith(grant.member, "serviceAccount:"),
          !contains(["allUsers", "allAuthenticatedUsers"], grant.member),
        ])
      ]),
    ])
    error_message = "Cloud Run invocation must be service-scoped and never public."
  }

  assert {
    condition = (
      toset(keys(google_project_iam_member.runtime)) == toset([
        "roles/aiplatform.user",
        "roles/datastore.user",
        "roles/logging.logWriter",
        "roles/monitoring.metricWriter",
      ])
    )
    error_message = "The runtime project roles must be exactly the frozen minimal set."
  }

  assert {
    condition = alltrue([
      google_storage_bucket_iam_member.runtime.role == "roles/storage.objectUser",
      google_storage_bucket_iam_member.runtime.bucket == google_storage_bucket.scratch.name,
      google_pubsub_topic_iam_member.runtime_work_publisher.role == "roles/pubsub.publisher",
      google_pubsub_topic_iam_member.runtime_work_publisher.topic == google_pubsub_topic.messaging["email-work"].name,
      length(google_secret_manager_secret_iam_member.runtime) == 3,
      alltrue([
        for grant in values(google_secret_manager_secret_iam_member.runtime) :
        grant.role == "roles/secretmanager.secretAccessor"
      ]),
    ])
    error_message = "Runtime object, publication, and secret access must stay resource-scoped."
  }

  assert {
    condition = alltrue([
      google_pubsub_topic_iam_member.gmail_publisher.role == "roles/pubsub.publisher",
      google_pubsub_topic_iam_member.gmail_publisher.topic == google_pubsub_topic.messaging["gmail-notifications"].name,
      google_pubsub_topic_iam_member.gmail_publisher.member == "serviceAccount:gmail-api-push@system.gserviceaccount.com",
    ])
    error_message = "Gmail may publish only to gmail-notifications."
  }

  assert {
    condition = alltrue([
      length(google_service_account_iam_member.pubsub_token_creator) == 2,
      alltrue([
        for grant in values(google_service_account_iam_member.pubsub_token_creator) :
        grant.role == "roles/iam.serviceAccountTokenCreator"
      ]),
      google_pubsub_topic_iam_member.dead_letter_publisher.role == "roles/pubsub.publisher",
      google_pubsub_topic_iam_member.dead_letter_publisher.topic == google_pubsub_topic.messaging["dead-letter"].name,
      length(google_pubsub_subscription_iam_member.dead_letter_subscriber) == 2,
      alltrue([
        for grant in values(google_pubsub_subscription_iam_member.dead_letter_subscriber) :
        grant.role == "roles/pubsub.subscriber"
      ]),
    ])
    error_message = "The Pub/Sub service agent must have only path-specific token and dead-letter grants."
  }
}

run "GCP_03_bounds_cost_and_keeps_ci_safe" {
  command = plan

  assert {
    condition = {
      for setting in google_cloud_run_v2_service.app.template[0].containers[0].env :
      setting.name => setting.value if length(setting.value_source) == 0
      } == {
      MAX_ATTACHMENT_ANALYSIS_CALLS = tostring(var.max_attachment_analysis_calls)
      MAX_REPLY_GENERATION_CALLS    = tostring(var.max_reply_generation_calls)
      MAX_REPLY_OUTPUT_TOKENS       = tostring(var.max_reply_output_tokens)
      MAX_SEARCH_CALLS              = tostring(var.max_search_calls)
      ALZA_ENV                      = "production"
      GEMINI_MODEL                  = "gemini-3.6-flash"
      GOOGLE_CLOUD_PROJECT          = var.project_id
      RESPONSE_PROVIDER             = "gemini"
      SCRATCH_BUCKET                = var.scratch_bucket_name
    }
    error_message = "Cloud Run must receive only the bounded production configuration."
  }

  assert {
    condition = {
      for setting in google_cloud_run_v2_service.app.template[0].containers[0].env :
      setting.name => {
        secret  = one(setting.value_source).secret_key_ref[0].secret
        version = one(setting.value_source).secret_key_ref[0].version
      } if length(setting.value_source) == 1
      } == {
      GMAIL_OAUTH_CLIENT_JSON = {
        secret  = google_secret_manager_secret.runtime["gmail-oauth-client"].secret_id
        version = "latest"
      }
      GMAIL_REFRESH_TOKEN_JSON = {
        secret  = google_secret_manager_secret.runtime["gmail-refresh-token"].secret_id
        version = "latest"
      }
    }
    error_message = "Cloud Run must read only the two required Gmail secrets at runtime."
  }

  assert {
    condition = alltrue([
      google_billing_budget.app.billing_account == var.billing_account_id,
      toset(google_billing_budget.app.budget_filter[0].projects) == toset(["projects/${var.project_number}"]),
      google_billing_budget.app.amount[0].specified_amount[0].currency_code == var.budget_currency,
      google_billing_budget.app.amount[0].specified_amount[0].units == tostring(var.monthly_budget_amount),
      toset([
        for rule in google_billing_budget.app.threshold_rules : rule.threshold_percent
      ]) == var.budget_thresholds,
      toset(google_billing_budget.app.all_updates_rule[0].monitoring_notification_channels) == toset(var.budget_notification_channel_ids),
    ])
    error_message = "The project budget must use the operator's amount, thresholds, and notification channels."
  }

  assert {
    condition = toset(keys(google_project_service.required)) == toset([
      "aiplatform.googleapis.com",
      "artifactregistry.googleapis.com",
      "billingbudgets.googleapis.com",
      "cloudresourcemanager.googleapis.com",
      "cloudscheduler.googleapis.com",
      "firestore.googleapis.com",
      "gmail.googleapis.com",
      "iam.googleapis.com",
      "iamcredentials.googleapis.com",
      "logging.googleapis.com",
      "monitoring.googleapis.com",
      "pubsub.googleapis.com",
      "run.googleapis.com",
      "secretmanager.googleapis.com",
      "storage.googleapis.com",
    ])
    error_message = "Terraform must enable only the explicitly required GCP APIs."
  }

  assert {
    condition = alltrue([
      for filename in fileset(path.module, "*.tf") : alltrue([
        !strcontains(file("${path.module}/${filename}"), "google_secret_manager_secret_version"),
        !strcontains(file("${path.module}/${filename}"), "secret_data"),
        !strcontains(file("${path.module}/${filename}"), "google_firestore_document"),
      ])
    ])
    error_message = "Terraform configuration must contain no secret payload/version or Firestore content resource."
  }

  assert {
    condition = alltrue([
      strcontains(file("${path.module}/../.github/workflows/ci.yml"), "terraform fmt -check -recursive"),
      strcontains(file("${path.module}/../.github/workflows/ci.yml"), "terraform init -backend=false"),
      strcontains(file("${path.module}/../.github/workflows/ci.yml"), "terraform validate"),
      strcontains(file("${path.module}/../.github/workflows/ci.yml"), "terraform test"),
      !strcontains(lower(file("${path.module}/../.github/workflows/ci.yml")), "terraform apply"),
      !strcontains(lower(file("${path.module}/../.github/workflows/ci.yml")), "terraform plan"),
    ])
    error_message = "CI must run every credential-free Terraform gate and must never plan or apply."
  }
}

run "GCP_03_rejects_higher_exposure_caps" {
  command = plan

  variables {
    cloud_run_max_instances       = 3
    max_attachment_analysis_calls = 6
    max_reply_generation_calls    = 2
    max_search_calls              = 2
    max_reply_output_tokens       = 2049
  }

  expect_failures = [
    var.cloud_run_max_instances,
    var.max_attachment_analysis_calls,
    var.max_reply_generation_calls,
    var.max_search_calls,
    var.max_reply_output_tokens,
  ]
}

run "OPS_02_keeps_the_live_sender_allowlist_out_of_the_deployment" {
  command = plan

  assert {
    condition = alltrue([
      contains(local.runtime_project_roles, "roles/datastore.user"),
      alltrue([
        for setting in google_cloud_run_v2_service.app.template[0].containers[0].env :
        !strcontains(lower(setting.name), "sender") && !strcontains(lower(setting.name), "allow")
      ]),
    ])
    error_message = "The runtime must read the live sender allowlist from Firestore, never from deployment configuration."
  }
}
