mock_provider "google" {}
run "unconfigured_migration_has_no_jobs_schedules_or_alerts" {
  command = plan
  assert {
    condition     = length(google_cloud_run_v2_job.job) == 0 && length(google_cloud_scheduler_job.schedule) == 0 && length(google_monitoring_alert_policy.observer_absent) == 0
    error_message = "The migration default must create no cloud jobs/schedules/alerts."
  }
}
