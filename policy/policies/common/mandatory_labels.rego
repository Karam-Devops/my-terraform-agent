# mandatory_labels.rego
#
# Every resource MUST carry the `team` and `env` labels. These two are the
# minimum tag set most organizations need for cost attribution and incident
# triage — exactly what enterprise compliance scanners (Firefly, Wiz) flag
# under their "tagging hygiene" rules.
#
# Severity: MED — missing labels don't break security posture but they
# break every downstream FinOps + IR process. MED keeps the report
# unblocked while still surfacing the finding.

package main

deny[msg] {
    not input.labels.team
    msg := sprintf(
        "[MED][mandatory_labels] resource %s is missing required label: team",
        [input.name],
    )
}

deny[msg] {
    not input.labels.env
    msg := sprintf(
        "[MED][mandatory_labels] resource %s is missing required label: env",
        [input.name],
    )
}
