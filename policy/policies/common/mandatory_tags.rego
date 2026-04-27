# mandatory_tags.rego
#
# AWS-shaped resources MUST carry `team` and `env` tags. AWS sibling of
# the GCP `mandatory_labels` rule -- same intent (FinOps + IR baseline),
# different field shape: AWS uses `Tags = [{Key, Value}, ...]` (list of
# pairs from aws-cli describe output) instead of GCP's `labels = { k: v }`
# map.
#
# Cloud field: `Tags` (list of objects, each with `Key` + `Value`)
# Severity: MED -- mirrors mandatory_labels for consistency.
#
# P3-4: gated by `input.Tags` precondition so GCP resources (which have
# `labels`, not `Tags`) don't false-fire. Together with the analogous
# `input.labels` guard in mandatory_labels.rego, exactly one of the two
# rules fires per resource based on which cloud's snapshot shape is
# present.
#
# Snapshot shape this rule expects:
#     "Tags": [
#         {"Key": "team", "Value": "platform"},
#         {"Key": "env",  "Value": "prod"}
#     ]

package main

# Helper: true iff the input's Tags list contains an entry with the
# given Key. Indexed via wildcard `_` -- existential semantics: ANY
# element matching is enough.
aws_tag_present(key) {
    input.Tags[_].Key == key
}

deny[msg] {
    input.Tags
    not aws_tag_present("team")
    msg := sprintf(
        "[MED][mandatory_tags] resource %s is missing required tag: team",
        [input.name],
    )
}

deny[msg] {
    input.Tags
    not aws_tag_present("env")
    msg := sprintf(
        "[MED][mandatory_tags] resource %s is missing required tag: env",
        [input.name],
    )
}
