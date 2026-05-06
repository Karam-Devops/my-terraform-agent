"""Migrator engine — Phase 7 Git-IaC Translator.

End-to-end pipeline: ingest customer's GCP Terraform/Terragrunt repo →
plan dependencies + score confidence → emit AWS Terragrunt skeleton +
migration guide + helper scripts.

Sibling to importer/, translator/, detector/, policy/. Programmatic
surface mirrors the others' A+D contract: PreflightError on bad inputs,
MigrationResult on completion regardless of per-resource outcomes.
"""
