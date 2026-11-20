variable "project_id" {
  description = "GCP project id (same as GCP_PROJECT_ID in .env)."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "asia-southeast1"
}

variable "name_prefix" {
  description = "Prefix for resource names."
  type        = string
  default     = "abap-triage"
}

variable "model" {
  description = "OpenRouter model id (same as MODEL in .env). Not secret."
  type        = string
}

variable "langsmith_project" {
  description = "LangSmith project name."
  type        = string
  default     = "simple-abap-triage"
}

variable "langsmith_endpoint" {
  description = "LangSmith API endpoint of your LangSmith region (US default; EU/APAC have their own)."
  type        = string
  default     = "https://api.smith.langchain.com"
}

variable "slack_channel_id" {
  description = "Slack channel ID (C...) of the triage channel. Not secret, but kept in git-ignored terraform.tfvars."
  type        = string
  default     = ""
}

variable "enable_cloud_run" {
  description = <<-EOT
    Create the Cloud Run service. Keep false on the first apply, add the secret values
    (README step 4), then set true. Cloud Run fails to deploy if a referenced secret has no version.
  EOT
  type        = bool
  default     = false
}

variable "container_image" {
  description = "Initial image. Real deployments update the image outside Terraform (ignored afterwards)."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "paused" {
  description = <<-EOT
    Pause to save cost between work sessions: Cloud SQL is stopped (only storage is billed) and Cloud Run has
    no always-on instance. Data, URL and tokens are kept. While paused, incoming alerts fail (DB not reachable).
  EOT
  type        = bool
  default     = false
}

variable "sql_instance_suffix" {
  description = <<-EOT
    Suffix of the Cloud SQL instance name (abap-triage-pg-<suffix>). A deleted instance name cannot be reused for
    up to one week, so change this (v1 -> v2 ...) when recreating the database right after a destroy.
  EOT
  type        = string
  default     = "v1"
}

variable "db_tier" {
  description = "Cloud SQL machine tier (smallest shared-core for the MVP)."
  type        = string
  default     = "db-f1-micro"
}
