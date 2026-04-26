# importer/tests/test_config_coherence.py
"""Cross-dict coherence tests for importer/config.py.

The importer's resource-type configuration is split across three
dicts that must stay in lockstep:

    ASSET_TO_TERRAFORM_MAP    (asset_type    -> tf_type)
    TF_TYPE_TO_GCLOUD_INFO    (tf_type       -> describe info)
    TF_TYPE_TO_GITHUB_DOC_PATH (tf_type      -> docs URL component)

Adding a new resource type touches all three. A common mistake is
adding to one and forgetting another, which surfaces at runtime as
either:
  * KeyError in get_resource_details_json (missing GCLOUD_INFO entry)
  * Silent fallback to no-context HCL gen (missing DOC_PATH entry)
Both fail late and are tedious to triage. These tests fail fast at
unit time -- one assertion per missing entry, with the offending
tf_type printed.

Phase 2 motivation: P2-3 (CMEK), P2-4 (Cloud Run v2), P2-5 (Pub/Sub)
each added 1-2 new types across all three dicts. Pin the contract
so future additions can't silently miss a dict.
"""

from __future__ import annotations

import unittest

from importer.config import (
    ASSET_TO_TERRAFORM_MAP,
    TF_TYPE_TO_GCLOUD_INFO,
    TF_TYPE_TO_GITHUB_DOC_PATH,
)


class ConfigCoherenceTests(unittest.TestCase):

    def test_every_mapped_tf_type_has_gcloud_info(self):
        """Each tf_type in ASSET_TO_TERRAFORM_MAP must have a
        TF_TYPE_TO_GCLOUD_INFO entry, otherwise gcp_client raises
        on the first describe attempt for that type."""
        missing = [
            tf_type for tf_type in ASSET_TO_TERRAFORM_MAP.values()
            if tf_type not in TF_TYPE_TO_GCLOUD_INFO
        ]
        self.assertEqual(
            missing, [],
            f"tf_type(s) in ASSET_TO_TERRAFORM_MAP without gcloud info: {missing}",
        )

    def test_every_mapped_tf_type_has_doc_path(self):
        """Each tf_type also needs a TF_TYPE_TO_GITHUB_DOC_PATH entry,
        otherwise the schema-prompt builder degrades to no-doc context
        for that type without warning."""
        missing = [
            tf_type for tf_type in ASSET_TO_TERRAFORM_MAP.values()
            if tf_type not in TF_TYPE_TO_GITHUB_DOC_PATH
        ]
        self.assertEqual(
            missing, [],
            f"tf_type(s) in ASSET_TO_TERRAFORM_MAP without doc path: {missing}",
        )

    def test_every_gcloud_info_entry_has_describe_command(self):
        """Every TF_TYPE_TO_GCLOUD_INFO entry must define a
        describe_command. Without it, gcp_client emits an empty
        gcloud subcommand and gcloud rejects with usage error."""
        missing = [
            tf_type for tf_type, info in TF_TYPE_TO_GCLOUD_INFO.items()
            if "describe_command" not in info
        ]
        self.assertEqual(
            missing, [],
            f"TF_TYPE_TO_GCLOUD_INFO entries without describe_command: {missing}",
        )

    def test_every_gcloud_info_entry_has_import_id_format(self):
        """Every entry must define import_id_format -- without it,
        run.py falls back to a heuristic that works for some types
        but produces wrong import ids for nested types (clusters,
        crypto keys). Force every entry to be explicit."""
        missing = [
            tf_type for tf_type, info in TF_TYPE_TO_GCLOUD_INFO.items()
            if "import_id_format" not in info
        ]
        self.assertEqual(
            missing, [],
            f"TF_TYPE_TO_GCLOUD_INFO entries without import_id_format: {missing}",
        )

    def test_location_flags_are_well_formed(self):
        """The three location-flag config keys (zone_flag, region_flag,
        location_flag) all must hold a string starting with `--`. A
        bare `zone` or `--region=` (with trailing equals) here would
        produce silently malformed gcloud commands."""
        for tf_type, info in TF_TYPE_TO_GCLOUD_INFO.items():
            for key in ("zone_flag", "region_flag", "location_flag"):
                if key in info:
                    val = info[key]
                    self.assertIsInstance(
                        val, str,
                        f"{tf_type}.{key} must be a string; got {type(val).__name__}",
                    )
                    self.assertTrue(
                        val.startswith("--") and "=" not in val,
                        f"{tf_type}.{key} must be a `--flag` form (no =); got {val!r}",
                    )

    def test_phase_2_types_are_present(self):
        """Pin the P2-3/P2-4/P2-5 additions so deletion surfaces in CI."""
        for tf_type in (
            "google_kms_key_ring",          # P2-3
            "google_kms_crypto_key",        # P2-3
            "google_cloud_run_v2_service",  # P2-4
            "google_pubsub_topic",          # P2-5
            "google_pubsub_subscription",   # P2-5
        ):
            with self.subTest(tf_type=tf_type):
                self.assertIn(
                    tf_type, TF_TYPE_TO_GCLOUD_INFO,
                    f"{tf_type} missing from TF_TYPE_TO_GCLOUD_INFO",
                )
                self.assertIn(
                    tf_type, TF_TYPE_TO_GITHUB_DOC_PATH,
                    f"{tf_type} missing from TF_TYPE_TO_GITHUB_DOC_PATH",
                )
                self.assertIn(
                    tf_type, ASSET_TO_TERRAFORM_MAP.values(),
                    f"{tf_type} missing as a value of ASSET_TO_TERRAFORM_MAP",
                )


if __name__ == "__main__":
    unittest.main()
