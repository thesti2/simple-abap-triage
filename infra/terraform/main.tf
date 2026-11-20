locals {
  apis = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
  ]

  # Secrets whose values are added by hand (kept out of Terraform state).
  manual_secrets = {
    OPENROUTER_API_KEY   = "openrouter-api-key"
    LANGSMITH_API_KEY    = "langsmith-api-key"
    SLACK_BOT_TOKEN      = "slack-bot-token"
    SLACK_SIGNING_SECRET = "slack-signing-secret"
  }
}

# ---------------------------------------------------------------- APIs
resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- Service account (Cloud Run runtime)
resource "google_service_account" "run" {
  account_id   = "${var.name_prefix}-run"
  display_name = "ABAP triage – Cloud Run runtime"
  depends_on   = [google_project_service.apis]
}

resource "google_project_iam_member" "run_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.run.email}"
}

# ---------------------------------------------------------------- Artifact Registry
resource "google_artifact_registry_repository" "app" {
  location      = var.region
  repository_id = var.name_prefix
  format        = "DOCKER"
  description   = "ABAP triage backend images"
  depends_on    = [google_project_service.apis]
}

# ---------------------------------------------------------------- Cloud SQL (Postgres)
resource "google_sql_database_instance" "pg" {
  name             = "${var.name_prefix}-pg-${var.sql_instance_suffix}"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    activation_policy = var.paused ? "NEVER" : "ALWAYS" # NEVER = instance stopped, storage kept
    availability_type = "ZONAL"
    disk_size         = 10
    disk_type         = "PD_SSD"

    backup_configuration {
      enabled = true
    }

    # Public IP but no authorized networks: only reachable through the Cloud SQL
    # connector (Cloud Run built-in, or Cloud SQL Auth Proxy for the local fix worker).
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
  }

  deletion_protection = true
  depends_on          = [google_project_service.apis]
}

resource "google_sql_database" "triage" {
  name     = "triage"
  instance = google_sql_database_instance.pg.name
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "triage" {
  name     = "triage"
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# ---------------------------------------------------------------- Secrets
resource "google_secret_manager_secret" "manual" {
  for_each  = local.manual_secrets
  secret_id = "${var.name_prefix}-${each.value}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "${var.name_prefix}-db-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db.result
}

# Token xMatters sends in the X-Triage-Token header. Generated here; read it with
# `terraform output -raw xmatters_webhook_token` and put it in the xMatters flow.
resource "random_password" "xmatters_token" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "xmatters_token" {
  secret_id = "${var.name_prefix}-xmatters-webhook-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "xmatters_token" {
  secret      = google_secret_manager_secret.xmatters_token.id
  secret_data = random_password.xmatters_token.result
}

locals {
  all_secret_ids = merge(
    { for k, s in google_secret_manager_secret.manual : k => s.secret_id },
    {
      DB_PASSWORD            = google_secret_manager_secret.db_password.secret_id
      XMATTERS_WEBHOOK_TOKEN = google_secret_manager_secret.xmatters_token.secret_id
    }
  )
}

resource "google_secret_manager_secret_iam_member" "run_access" {
  for_each  = local.all_secret_ids
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# ---------------------------------------------------------------- Cloud Run (FastAPI)
resource "google_cloud_run_v2_service" "app" {
  count               = var.enable_cloud_run ? 1 : 0
  name                = var.name_prefix
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.run.email

    scaling {
      min_instance_count = var.paused ? 0 : 1 # 1 = always-on (Slack/background work); 0 while paused
      max_instance_count = 2
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.pg.connection_name]
      }
    }

    containers {
      image = var.container_image

      resources {
        cpu_idle = false # CPU always allocated (background tasks after 202 response)
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "MODEL"
        value = var.model
      }
      env {
        name  = "LANGSMITH_TRACING"
        value = "true"
      }
      env {
        name  = "LANGSMITH_PROJECT"
        value = var.langsmith_project
      }
      env {
        name  = "LANGSMITH_ENDPOINT"
        value = var.langsmith_endpoint
      }
      env {
        name  = "SLACK_MODE"
        value = "http" # Slack Request URLs -> /slack/events, /slack/interactions (no Socket Mode on Cloud Run)
      }
      env {
        name  = "SLACK_CHANNEL_ID"
        value = var.slack_channel_id
      }
      env {
        name  = "DB_NAME"
        value = google_sql_database.triage.name
      }
      env {
        name  = "DB_USER"
        value = google_sql_user.triage.name
      }
      env {
        name  = "DB_SOCKET"
        value = "/cloudsql/${google_sql_database_instance.pg.connection_name}"
      }

      dynamic "env" {
        for_each = local.all_secret_ids
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # Image and client info are updated by deployments, not Terraform.
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_secret_manager_secret_iam_member.run_access]
}

# Public endpoint: xMatters and Slack must reach it. The app itself checks
# X-Triage-Token (xMatters) and the Slack request signature (signing secret).
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.enable_cloud_run ? 1 : 0
  name     = google_cloud_run_v2_service.app[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}
