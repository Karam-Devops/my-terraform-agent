# common/tests/test_errors.py
"""Unit tests for common.errors.

Coverage focus: the UpstreamTimeout contract -- structured fields,
user_hint, and base-class hierarchy. These pin what engines emit into
logs and what the Streamlit UI (Phase 6) renders to customers.

Why a dedicated test: the exception's fields are the log schema
operators will filter on. If someone later renames `binary` to
`tool`, every Cloud Logging dashboard breaks silently. The tests
below pin the exact field names.
"""

from __future__ import annotations

import unittest

from common.errors import EngineError, PreflightError, UpstreamTimeout


class UpstreamTimeoutTests(unittest.TestCase):

    def test_is_engine_error_subclass(self):
        """UI can ``except EngineError`` and catch every typed failure."""
        self.assertTrue(issubclass(UpstreamTimeout, EngineError))
        self.assertTrue(issubclass(UpstreamTimeout, Exception))

    def test_carries_required_structured_fields(self):
        """The fields operators filter logs on must be attributes AND in .fields.

        If any of these names change, update the commit message + the
        CC-2 punchlist entry, because downstream log dashboards key
        off them.
        """
        exc = UpstreamTimeout(
            "terraform plan timed out after 300s (elapsed 302.4s)",
            binary="terraform",
            stage="plan",
            elapsed_s=302.4,
            timeout_s=300.0,
            cmd="terraform",
        )
        # Attribute access (used in engine code that handles the exception)
        self.assertEqual(exc.binary, "terraform")
        self.assertEqual(exc.stage, "plan")
        self.assertEqual(exc.elapsed_s, 302.4)
        self.assertEqual(exc.timeout_s, 300.0)
        self.assertEqual(exc.cmd, "terraform")

        # .fields dict (used by structured logger to emit fields)
        self.assertEqual(exc.fields["binary"], "terraform")
        self.assertEqual(exc.fields["stage"], "plan")
        self.assertEqual(exc.fields["elapsed_s"], 302.4)
        self.assertEqual(exc.fields["timeout_s"], 300.0)

    def test_cmd_defaults_to_binary(self):
        """When cmd isn't passed, it falls back to binary.

        Lets callers omit `cmd` in the common case (gcloud/terraform)
        without losing the log field.
        """
        exc = UpstreamTimeout(
            "gcloud describe timed out after 60s",
            binary="gcloud",
            stage="describe",
            elapsed_s=60.5,
            timeout_s=60.0,
        )
        self.assertEqual(exc.cmd, "gcloud")

    def test_user_hint_is_ui_safe(self):
        """user_hint is what customers see; must not leak paths or internals.

        Pinning the prefix (not full equality) so we can reword
        without breaking the test on every copy-edit.
        """
        exc = UpstreamTimeout(
            "terraform plan timed out",
            binary="terraform", stage="plan",
            elapsed_s=302.4, timeout_s=300.0,
        )
        self.assertIn("upstream", exc.user_hint.lower())
        # Must NOT leak internal paths or config
        self.assertNotIn("/", exc.user_hint)
        self.assertNotIn("terraform", exc.user_hint.lower(),
                         "binary name leaks technical detail; keep hint generic")

    def test_preserves_cause_chain(self):
        """Original TimeoutExpired must be in __cause__ for debugging."""
        import subprocess

        original = subprocess.TimeoutExpired(cmd="terraform", timeout=300)
        try:
            try:
                raise original
            except subprocess.TimeoutExpired as e:
                raise UpstreamTimeout(
                    "terraform plan timed out",
                    binary="terraform", stage="plan",
                    elapsed_s=302.0, timeout_s=300.0,
                ) from e
        except UpstreamTimeout as exc:
            self.assertIs(exc.__cause__, original,
                          "__cause__ must link back to subprocess.TimeoutExpired")


class PreflightErrorTests(unittest.TestCase):
    """Pins the PreflightError contract used by run_workflow A+D pattern.

    run_workflow raises PreflightError when it can't START (bad project
    ID, unresolvable workdir, terraform init failure). The UI catches
    EngineError and renders .user_hint; dashboards filter on .stage.
    Changing these field names breaks both — tests pin them.
    """

    def test_is_engine_error_subclass(self):
        """UI's `except EngineError` must also catch PreflightError."""
        self.assertTrue(issubclass(PreflightError, EngineError))
        self.assertTrue(issubclass(PreflightError, Exception))

    def test_carries_stage_and_reason(self):
        """stage + reason are the two fields dashboards filter on."""
        exc = PreflightError(
            "project ID 'prod-foo' not permitted under DEMO_PROJECT_ID lock",
            stage="validate_project_id",
            reason="demo lock active; only 'demo-bar' allowed",
        )
        self.assertEqual(exc.stage, "validate_project_id")
        self.assertEqual(exc.reason, "demo lock active; only 'demo-bar' allowed")
        self.assertEqual(exc.fields["stage"], "validate_project_id")
        self.assertEqual(exc.fields["reason"],
                         "demo lock active; only 'demo-bar' allowed")

    def test_reason_defaults_to_message(self):
        """When reason is omitted, message is reused — common case."""
        exc = PreflightError(
            "terraform init failed",
            stage="terraform_init",
        )
        self.assertEqual(exc.reason, "terraform init failed")

    def test_user_hint_is_ui_safe(self):
        """Hint shown to customers must not leak internal terminology."""
        exc = PreflightError("xx", stage="terraform_init")
        # No paths, no binary names, no internal stage IDs
        self.assertNotIn("/", exc.user_hint)
        self.assertNotIn("terraform", exc.user_hint.lower())
        self.assertNotIn("workdir", exc.user_hint.lower())

    def test_preserves_cause_chain(self):
        """Original ValueError from project-ID validation stays in __cause__."""
        original = ValueError("project ID cannot be empty")
        try:
            try:
                raise original
            except ValueError as e:
                raise PreflightError(
                    "project ID validation failed",
                    stage="validate_project_id",
                    reason=str(e),
                ) from e
        except PreflightError as exc:
            self.assertIs(exc.__cause__, original)


if __name__ == "__main__":
    unittest.main()
