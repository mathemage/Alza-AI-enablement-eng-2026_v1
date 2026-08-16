locals {
  required_services = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])

  service_accounts = {
    runtime    = "alza-ai-runtime"
    gmail_push = "gmail-notifications-push"
    work_push  = "email-work-push"
    scheduler  = "scheduler-invoker"
    smoke      = "authenticated-smoke"
  }

  secret_ids = toset([
    "gmail-oauth-client",
    "gmail-refresh-token",
    "openrouter-api-key",
  ])

  quota_environment = {
    MAX_ATTACHMENT_ANALYSIS_CALLS = var.max_attachment_analysis_calls
    MAX_REPLY_GENERATION_CALLS    = var.max_reply_generation_calls
    MAX_REPLY_OUTPUT_TOKENS       = var.max_reply_output_tokens
    MAX_SEARCH_CALLS              = var.max_search_calls
  }
}

resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_service_account" "identities" {
  for_each = local.service_accounts

  project      = var.project_id
  account_id   = each.value
  display_name = each.value

  depends_on = [google_project_service.required]
}

resource "google_artifact_registry_repository" "app" {
  project       = var.project_id
  location      = var.region
  repository_id = "alza-ai"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_firestore_database" "app" {
  project                     = var.project_id
  name                        = "(default)"
  location_id                 = var.region
  type                        = "FIRESTORE_NATIVE"
  concurrency_mode            = "PESSIMISTIC"
  app_engine_integration_mode = "DISABLED"
  deletion_policy             = "DELETE"

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket" "scratch" {
  project                     = var.project_id
  name                        = var.scratch_bucket_name
  location                    = upper(var.region)
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 1
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "runtime" {
  for_each = local.secret_ids

  project   = var.project_id
  secret_id = each.key

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "app" {
  project              = var.project_id
  name                 = "alza-ai"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  invoker_iam_disabled = false
  deletion_protection  = false

  scaling {
    min_instance_count = 0
    max_instance_count = var.cloud_run_max_instances
  }

  template {
    service_account                  = google_service_account.identities["runtime"].email
    timeout                          = "115s"
    max_instance_request_concurrency = 1

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = local.quota_environment
        content {
          name  = env.key
          value = tostring(env.value)
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}
