locals {
  invoker_identities = {
    gmail_push = google_service_account.identities["gmail_push"].email
    work_push  = google_service_account.identities["work_push"].email
    scheduler  = google_service_account.identities["scheduler"].email
    smoke      = google_service_account.identities["smoke"].email
  }

  runtime_project_roles = toset([
    "roles/aiplatform.user",
    "roles/datastore.user",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])

  pubsub_service_agent_member = "serviceAccount:service-${var.project_number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = local.invoker_identities

  project  = var.project_id
  location = google_cloud_run_v2_service.app.location
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

resource "google_project_iam_member" "runtime" {
  for_each = local.runtime_project_roles

  project = var.project_id
  role    = each.key
  member  = google_service_account.identities["runtime"].member
}

resource "google_storage_bucket_iam_member" "runtime" {
  bucket = google_storage_bucket.scratch.name
  role   = "roles/storage.objectUser"
  member = google_service_account.identities["runtime"].member
}

resource "google_pubsub_topic_iam_member" "runtime_work_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.messaging["email-work"].name
  role    = "roles/pubsub.publisher"
  member  = google_service_account.identities["runtime"].member
}

resource "google_secret_manager_secret_iam_member" "runtime" {
  for_each = google_secret_manager_secret.runtime

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.identities["runtime"].member
}

resource "google_pubsub_topic_iam_member" "gmail_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.messaging["gmail-notifications"].name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

resource "google_service_account_iam_member" "pubsub_token_creator" {
  for_each = toset(["gmail_push", "work_push"])

  service_account_id = google_service_account.identities[each.key].name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = local.pubsub_service_agent_member
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.messaging["dead-letter"].name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_service_agent_member
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  for_each = google_pubsub_subscription.primary

  project      = var.project_id
  subscription = each.value.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_service_agent_member
}
