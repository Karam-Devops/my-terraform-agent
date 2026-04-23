# policy/integration.py
"""
Integration glue between the detector and the policy enforcer.

The detector calls `classify_drift(...)` after building each `ResourceDrift`,
gets back a `PolicyImpact` describing whether the post-drift cloud state
violates any policy, and uses `summary_tag` to decorate its report:

    🛑 google_storage_bucket.foo  [⚠️  drift introduces 1 HIGH violation(s)]

The day-1 classification is "post-drift cloud has N violations". A precise
"drift INTRODUCED a regression" classification needs the pre-drift state
too — that requires snapshot history we don't have yet, so this is the
honest first cut. When we add a snapshot table the only change is the
classifier; the field/tag plumbing stays the same.

Fail-open by design: if conftest isn't installed or the engine errors,
classify_drift returns an empty PolicyImpact rather than raising. The
detector then renders no policy tag and continues normally. We never
want a missing dependency in the policy module to crash drift detection.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config, engine


@dataclass
class PolicyImpact:
    violations: List[engine.Violation] = field(default_factory=list)

    @property
    def is_violating(self) -> bool:
        return len(self.violations) > 0

    @property
    def high_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "HIGH")

    @property
    def med_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "MED")

    @property
    def low_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "LOW")

    @property
    def summary_tag(self) -> Optional[str]:
        """Short bracketed tag for inline display in the detector report.
        Returns None when nothing to surface (so detector renders nothing
        rather than an empty `[]`)."""
        if not self.is_violating:
            return None
        parts = []
        if self.high_count:
            parts.append(f"{self.high_count} HIGH")
        if self.med_count:
            parts.append(f"{self.med_count} MED")
        if self.low_count and not parts:
            # Only surface LOW alone if nothing more severe — otherwise
            # the tag gets noisy.
            parts.append(f"{self.low_count} LOW")
        return f"⚠️  drift introduces {', '.join(parts)} violation(s)"


def classify_drift(tf_address: str, tf_type: str,
                   cloud_snapshot: Optional[Dict[str, Any]]) -> PolicyImpact:
    """Evaluate the post-drift cloud state against policy and return impact.

    Returns an empty PolicyImpact (no violations) on every fail-open path:
      - resource type isn't in the policy enforcer's scope
      - cloud snapshot is missing (resource deleted out-of-band)
      - conftest isn't installed
      - engine errored mid-evaluation
    """
    if tf_type not in config.IN_SCOPE_TF_TYPES:
        return PolicyImpact()
    if cloud_snapshot is None:
        return PolicyImpact()
    try:
        engine.ensure_conftest_available()
    except RuntimeError:
        return PolicyImpact()

    dirs_to_check = [
        config.COMMON_POLICY_DIR,
        config.policies_dir_for(tf_type),
    ]
    violations = engine.evaluate(
        document=cloud_snapshot,
        policy_dirs=dirs_to_check,
        resource_address=tf_address,
    )
    return PolicyImpact(violations=violations)
