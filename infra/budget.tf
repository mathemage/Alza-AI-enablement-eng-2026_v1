resource "google_billing_budget" "app" {
  billing_account = var.billing_account_id
  display_name    = "Alza AI monthly exposure alert"

  budget_filter {
    projects = ["projects/${var.project_number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.monthly_budget_amount)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.budget_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  dynamic "all_updates_rule" {
    for_each = length(var.budget_notification_channel_ids) > 0 ? [true] : []
    content {
      monitoring_notification_channels = var.budget_notification_channel_ids
      disable_default_iam_recipients   = true
    }
  }

  depends_on = [google_project_service.required]
}
