# cloudrun_min_instances_documented.rego
# Source: NONE (Cloud Run not covered in GoogleCloudPlatform/policy-library archived library)
# Standard: Industry consensus (cold-start latency / cost discipline) | NIST SP 800-53 SA-3 (System Development)
# Default: Require template.scaling.minInstanceCount be set explicitly (any value -- including 0 -- is acceptable; the operational gap is leaving it implicit)
# See docs/policy_provenance.md for full mining details.

package main

# Helper: defensively read scaling.minInstanceCount. Returns -1 (a value
# Cloud Run never returns) when the field is absent so the inequality
# below trips on the "not explicitly set" case.
min_instance_count := n {
    template := object.get(input, "template", {})
    scaling := object.get(template, "scaling", {})
    n := object.get(scaling, "minInstanceCount", -1)
}

deny[msg] {
    min_instance_count == -1
    msg := sprintf(
        "[LOW][cloudrun_min_instances_documented] Cloud Run service %s has no explicit template.scaling.minInstanceCount -- set it (0 for cost-optimized, >=1 for cold-start sensitive) so the deployment intent is documented in code",
        [input.name],
    )
}
