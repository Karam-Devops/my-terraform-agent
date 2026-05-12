# Rule-driven translator system

This directory holds **declarative YAML rule files** that describe
GCP→AWS translations without writing Python. The Migrator engine
loads these at startup and uses them to generate `Translation`
objects at run-time.

The rule-driven path is **opt-in alongside** the existing Python-based
translators (`migrator/translate/<service>.py`). The dispatcher tries
rules first; if no rule file exists for a resource type, it falls
back to the Python translator from `TRANSLATORS` dict. Both can
coexist indefinitely.

## Why hybrid (rules + code)?

| Aspect | Pure Python (status quo) | YAML rules | Hybrid |
|---|---|---|---|
| Author speed | Hours-days per translator | Minutes per type | YAML for simple, Python for complex |
| Non-dev contribution | Hard (review Python code) | Easy (edit YAML data) | Domain experts edit rules |
| Expressive power | Full | Limited to schema | Full via Python override |
| Audit trail | Code git history | Data git history | Same |
| Determinism | Bit-identical | Bit-identical | Bit-identical |

For 80% of resource types (simple attribute renames + enum maps),
YAML is enough. The 20% that need topology shifts (Pub/Sub → SNS+SQS,
IAM bindings, NCC hub) keep their Python translators.

## File layout

```
migrator/translate/rules/
├── README.md                                 ← this file
├── _schema.md                                ← reference schema doc
├── <gcp_resource_type>.yaml                  ← one file per supported type
│   └── e.g. google_artifact_registry_repository.yaml
└── ... more rule files as we migrate
```

## Quick start — adding a new resource type

```yaml
# migrator/translate/rules/google_my_new_resource.yaml
source_type: google_my_new_resource
target_type:  aws_my_equivalent
service_name: my-service
confidence:   HIGH

# Map each AWS module input from a source GCP arg.
# Each entry is either:
#   - a string (literal source key to copy)
#   - a dict with optional rename / transform / default / enum_map
inputs:
  bucket_name:
    from: name
  region:
    from: location
    enum_map:
      US: us-east-1
      EU: eu-west-1
  storage_class:
    from: storage_class
    enum_map:
      STANDARD: STANDARD
      NEARLINE: STANDARD_IA
      COLDLINE: GLACIER_IR
    default: STANDARD

# Profile-driven attrs (forced on under compliance profiles).
# Same shape as compliance_profiles.py defaults.
compliance_defaults:
  hipaa:
    block_public_access: true
    versioning: true
    kms_encryption: true

# AWS module body lives separately (still emitted from Python
# aws_module_spec()). For rule-driven types that don't have a
# corresponding Python file, point at a generic emitter:
module_body_python: migrator.translate.overrides.my_module_body
```

Then re-run the engine. The dispatcher auto-discovers the new rule
file at startup. No Python code change needed.

## When to drop to Python

Use a Python override (in `migrator/translate/overrides/` or the
legacy `migrator/translate/<service>.py`) when the translation needs:

- **Topology shifts** (1 GCP resource → multiple AWS resources, like
  Pub/Sub → SNS + SQS + subscription)
- **Computed values** (CIDR widening, instance-class heuristics, etc.)
- **Cross-resource references** (looking up the VPC ID from a sibling
  module's output)
- **Conditional logic** (different translation based on source attr
  values)

For everything else (attribute renames, enum maps, profile defaults),
YAML is the right place.

## Migration plan

1. **New types land as YAML by default.** Code review takes 10 minutes
   instead of 2 hours.
2. **Existing Python translators stay** until/unless we want to migrate
   them. No deadline pressure.
3. **Simple translators (acm, ecr, log_sink, route53, secrets, subnet)**
   are good migration candidates — they're mostly attribute renames.
4. **Complex translators (sns_sqs, vpc, ec2, eks)** stay Python.

The dispatcher logs which path it took per resource so we know which
types are rule-driven vs code-driven at any point.
