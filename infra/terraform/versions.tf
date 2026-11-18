terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State bucket is created once by hand (README step 2) and passed via backend.hcl.
  backend "gcs" {
    # bucket comes from backend.hcl (git-ignored): terraform init -backend-config=backend.hcl
    prefix = "abap-triage/dev"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
