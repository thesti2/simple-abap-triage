output "cloud_run_url" {
  description = "Base URL. xMatters webhook = <url>/webhooks/xmatters; Slack = <url>/slack/events and <url>/slack/interactions."
  value       = var.enable_cloud_run ? google_cloud_run_v2_service.app[0].uri : null
}

output "sql_connection_name" {
  description = "For the Cloud SQL Auth Proxy used by the local fix worker."
  value       = google_sql_database_instance.pg.connection_name
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
}

output "run_service_account" {
  value = google_service_account.run.email
}

output "xmatters_webhook_token" {
  value     = random_password.xmatters_token.result
  sensitive = true
}
