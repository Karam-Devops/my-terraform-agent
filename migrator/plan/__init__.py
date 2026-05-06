"""Migrator plan layer.

Two responsibilities:
  * dep_graph — build a directed graph of inter-resource references
  * coverage  — score each resource HIGH/MEDIUM/LOW/MANUAL_REVIEW
                using the GCP→AWS mapping table seeded from Kiro's
                published analysis (see phase7_kiro_repo_scan memory).
"""
