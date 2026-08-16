locals {
  primary_paths = {
    gmail-notifications = {
      identity = "gmail_push"
      route    = "/events/gmail"
    }
    email-work = {
      identity = "work_push"
      route    = "/jobs/process-message"
    }
  }

  scheduler_jobs = {
    renew-watch = {
      route    = "/jobs/renew-watch"
      schedule = "0 3 * * *"
    }
    reconcile-unread = {
      route    = "/jobs/reconcile-unread"
      schedule = "*/5 * * * *"
    }
  }
}

resource "google_pubsub_topic" "messaging" {
  for_each = toset(["gmail-notifications", "email-work", "dead-letter"])

  project = var.project_id
  name    = each.key

  message_storage_policy {
    allowed_persistence_regions = [var.region]
  }

  depends_on = [google_project_service.required]
}

resource "google_pubsub_subscription" "primary" {
  for_each = local.primary_paths

  project                    = var.project_id
  name                       = "${each.key}-push"
  topic                      = google_pubsub_topic.messaging[each.key].id
  ack_deadline_seconds       = 120
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.messaging["dead-letter"].id
    max_delivery_attempts = 5
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.app.uri}${each.value.route}"

    oidc_token {
      service_account_email = google_service_account.identities[each.value.identity].email
      audience              = google_cloud_run_v2_service.app.uri
    }
  }

  depends_on = [google_service_account_iam_member.pubsub_token_creator]
}

resource "google_pubsub_subscription" "dead_letter_monitor" {
  project                    = var.project_id
  name                       = "dead-letter-monitor"
  topic                      = google_pubsub_topic.messaging["dead-letter"].id
  ack_deadline_seconds       = 120
  message_retention_duration = "604800s"

  expiration_policy {
    ttl = ""
  }
}

resource "google_cloud_scheduler_job" "jobs" {
  for_each = local.scheduler_jobs

  project          = var.project_id
  region           = var.region
  name             = each.key
  schedule         = each.value.schedule
  time_zone        = "Etc/UTC"
  attempt_deadline = "110s"

  retry_config {
    retry_count          = 3
    max_retry_duration   = "300s"
    min_backoff_duration = "10s"
    max_backoff_duration = "60s"
    max_doublings        = 3
  }

  http_target {
    uri         = "${google_cloud_run_v2_service.app.uri}${each.value.route}"
    http_method = "POST"

    oidc_token {
      service_account_email = google_service_account.identities["scheduler"].email
      audience              = google_cloud_run_v2_service.app.uri
    }
  }

  depends_on = [google_cloud_run_v2_service_iam_member.invokers]
}
