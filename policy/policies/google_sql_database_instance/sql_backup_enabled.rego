# sql_backup_enabled.rego
# Source: GCP policy-library (archived 2025-08-20) gcp_sql_backup_v1
# Standard: CIS GCP 6.7 | NIST SP 800-53 CP-9 (System Backup)
# Default: Require settings.backupConfiguration.enabled == true (point-in-time recovery)
# See docs/policy_provenance.md for full mining details.

package main

deny[msg] {
    settings := object.get(input, "settings", {})
    backup := object.get(settings, "backupConfiguration", {})
    not object.get(backup, "enabled", false) == true
    msg := sprintf(
        "[HIGH][sql_backup_enabled] Cloud SQL instance %s does not have automated backups enabled (settings.backupConfiguration.enabled must be true) -- no recovery point objective (CIS GCP 6.7)",
        [input.name],
    )
}
