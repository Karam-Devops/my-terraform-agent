"""Migrator output layer.

Two responsibilities for v1:
  * migration_guide — render MIGRATION_GUIDE.md from inventory + dep
                       graph + confidence scores
  * helpers          — emit data-migration helper scripts
                       (gcs_to_s3.sh, secrets_migrate.sh, etc.)

AWS Terragrunt skeleton emission (Design phase) is deferred — see
phase7_migrator_strategy memory.
"""
