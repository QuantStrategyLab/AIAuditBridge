terraform {
  required_version = ">= 1.5, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
}

# No cloud data reads, API enablement, identities, IAM bindings or secret values.
variable "deployment" {
  description = "Null until owner approves projects, existing identities, image and cutover."
  type = object({
    monitor_project       = string
    monitored_projects    = set(string)
    region                = string
    scheduler_region      = string
    image                 = string
    scheduler_sa          = string
    notify_sa             = string
    monitor_sa            = string
    observer_sa           = string
    config_uris           = map(string)
    telegram_secret       = string
    telegram_version      = string
    notification_channels = list(string)
  })
  default = null
  validation {
    condition = var.deployment == null ? true : (
      !contains(var.deployment.monitored_projects, var.deployment.monitor_project) &&
      can(regex("@sha256:[0-9a-f]{64}$", var.deployment.image)) &&
      can(regex("^[0-9]+$", var.deployment.telegram_version)) &&
      length(toset([var.deployment.notify_sa, var.deployment.monitor_sa, var.deployment.observer_sa, var.deployment.scheduler_sa])) == 4 &&
      alltrue([for mode in ["notify", "monitor", "observer"] : can(regex("^gs://", var.deployment.config_uris[mode]))]) &&
      length(var.deployment.notification_channels) > 0
    )
    error_message = "Use independent monitoring project, distinct existing identities, pinned image/secret version, all config objects and existing external alert channels."
  }
}
variable "send_accepted" {
  description = "Owner accepted migrated ledger/route and real receipt; no automatic sending."
  type        = bool
  default     = false
}
locals {
  modes = var.deployment == null ? toset([]) : toset(["notify", "monitor", "observer"])
  identities = var.deployment == null ? {} : {
    notify = var.deployment.notify_sa, monitor = var.deployment.monitor_sa, observer = var.deployment.observer_sa
  }
  schedules = { notify = "30 22 * * 1-5", monitor = "*/30 * * * *", observer = "*/15 * * * *" }
}
resource "google_cloud_run_v2_job" "job" {
  for_each            = local.modes
  project             = var.deployment.monitor_project
  location            = var.deployment.region
  name                = "ai-${each.key}"
  deletion_protection = true
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = local.identities[each.key]
      timeout         = "300s"
      max_retries     = 0
      containers {
        image = var.deployment.image
        args  = [each.key, "--config-uri", var.deployment.config_uris[each.key]]
        resources { limits = { cpu = "1", memory = "512Mi" } }
        env {
          name  = "NOTIFY_SEND_ACCEPTED"
          value = each.key == "notify" && var.send_accepted ? "true" : "false"
        }
        dynamic "env" {
          for_each = each.key == "notify" ? [1] : []
          content {
            name = "TELEGRAM_TOKEN"
            value_source {
              secret_key_ref {
                secret  = var.deployment.telegram_secret
                version = var.deployment.telegram_version
              }
            }
          }
        }
      }
    }
  }
}
resource "google_cloud_scheduler_job" "schedule" {
  for_each         = local.modes
  project          = var.deployment.monitor_project
  region           = var.deployment.scheduler_region
  name             = "ai-${each.key}"
  schedule         = local.schedules[each.key]
  time_zone        = "UTC"
  paused           = true
  attempt_deadline = "30s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.deployment.monitor_project}/locations/${var.deployment.region}/jobs/${google_cloud_run_v2_job.job[each.key].name}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token { service_account_email = var.deployment.scheduler_sa }
  }
}
# Google's managed alert runs independently of all three jobs and their hosts.
resource "google_monitoring_alert_policy" "observer_absent" {
  count                 = var.deployment == null ? 0 : 1
  project               = var.deployment.monitor_project
  display_name          = "AI observer has no successful execution"
  combiner              = "OR"
  enabled               = false
  notification_channels = var.deployment.notification_channels
  conditions {
    display_name = "Observer success absent for one hour"
    condition_absent {
      filter   = "resource.type=\"cloud_run_job\" AND metric.type=\"run.googleapis.com/job/completed_execution_count\" AND resource.labels.job_name=\"ai-observer\" AND metric.labels.result=\"succeeded\""
      duration = "3600s"
      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
}
