# mandatory_labels.rego
#
# GCP-shaped resources MUST carry the `team` and `env` labels. These two are
# the minimum tag set most organizations need for cost attribution and incident
# triage — exactly what enterprise compliance scanners (Firefly, Wiz) flag
# under their "tagging hygiene" rules.
#
# Cloud field: `labels` (a map: { "team": "...", "env": "..." })
# Severity: MED — missing labels don't break security posture but they
# break every downstream FinOps + IR process. MED keeps the report
# unblocked while still surfacing the finding.
#
# P3-4: GCP-shape gated by `input.labels` precondition. Without the guard,
# AWS resources (which have `Tags`, not `labels`) would false-fire this
# rule because Rego's `not input.labels.team` is true when input.labels
# itself is undefined. The guard ensures the rule only evaluates when
# the GCP-style labels map is present in the snapshot. The AWS sibling
# rule lives in mandatory_tags.rego and uses the AWS `Tags = [{Key,Value}]`
# list shape.

package main

deny[msg] {
    input.labels
    not input.labels.team
    msg := sprintf(
        "[MED][mandatory_labels] resource %s is missing required label: team",
        [input.name],
    )
}

deny[msg] {
    input.labels
    not input.labels.env
    msg := sprintf(
        "[MED][mandatory_labels] resource %s is missing required label: env",
        [input.name],
    )
}
