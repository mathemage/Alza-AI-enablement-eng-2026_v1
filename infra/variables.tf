variable "project_id" {
  description = "GCP project ID selected explicitly by the operator."
  type        = string
}

variable "project_number" {
  description = "Numeric GCP project number used for service-agent identities."
  type        = string
}

variable "billing_account_id" {
  description = "Billing account that owns the project-scoped alert budget."
  type        = string
}

variable "container_image" {
  description = "Immutable container image deployed to Cloud Run."
  type        = string

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "container_image must use an immutable sha256 digest."
  }
}

variable "scratch_bucket_name" {
  description = "Globally unique name for the regional scratch bucket."
  type        = string
}

variable "region" {
  description = "Region for every proximity-oriented resource."
  type        = string
  default     = "europe-west3"

  validation {
    condition     = var.region == "europe-west3"
    error_message = "The MVP region is fixed to europe-west3."
  }
}

variable "cloud_run_max_instances" {
  description = "Cloud Run exposure ceiling; may be lowered but not raised above two."
  type        = number
  default     = 2

  validation {
    condition     = contains([1, 2], var.cloud_run_max_instances)
    error_message = "cloud_run_max_instances must be one or two."
  }
}

variable "max_attachment_analysis_calls" {
  description = "Per-message attachment model-call ceiling."
  type        = number
  default     = 5

  validation {
    condition     = var.max_attachment_analysis_calls >= 1 && var.max_attachment_analysis_calls <= 5
    error_message = "max_attachment_analysis_calls must be between one and five."
  }
}

variable "max_reply_generation_calls" {
  description = "Per-message reply model-call ceiling."
  type        = number
  default     = 1

  validation {
    condition     = contains([0, 1], var.max_reply_generation_calls)
    error_message = "max_reply_generation_calls must be zero or one."
  }
}

variable "max_search_calls" {
  description = "Per-message search-enabled model-call ceiling."
  type        = number
  default     = 1

  validation {
    condition     = contains([0, 1], var.max_search_calls)
    error_message = "max_search_calls must be zero or one."
  }
}

variable "max_reply_output_tokens" {
  description = "Per-message model output-token ceiling."
  type        = number
  default     = 2048

  validation {
    condition     = var.max_reply_output_tokens >= 1 && var.max_reply_output_tokens <= 2048
    error_message = "max_reply_output_tokens must be between one and 2048."
  }
}

variable "monthly_budget_amount" {
  description = "Monthly project budget alert in whole currency units; this is not a spending cap."
  type        = number
  default     = 20

  validation {
    condition     = var.monthly_budget_amount > 0 && floor(var.monthly_budget_amount) == var.monthly_budget_amount
    error_message = "monthly_budget_amount must be a positive whole number."
  }
}

variable "budget_currency" {
  description = "ISO 4217 currency code for the budget amount."
  type        = string
  default     = "EUR"
}

variable "budget_thresholds" {
  description = "Fractions of the monthly budget that trigger alerts."
  type        = set(number)
  default     = [0.5, 0.9, 1.0]

  validation {
    condition     = alltrue([for threshold in var.budget_thresholds : threshold > 0 && threshold <= 1])
    error_message = "budget_thresholds must be greater than zero and no greater than one."
  }
}

variable "budget_notification_channel_ids" {
  description = "Monitoring notification-channel resource IDs for budget alerts."
  type        = list(string)
  default     = []
}
