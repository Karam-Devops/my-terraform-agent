# Rule file schema (reference)

Every YAML rule file in this directory MUST validate against the
schema below. The loader (`migrator.translate.rules_engine`) rejects
invalid rules at engine-startup time so misshapen rules fail loudly,
not silently at translate-time.

## Top-level fields

| Field | Required | Type | Notes |
|---|---|---|---|
| `source_type` | ✅ | string | Must equal the GCP `tf_type` (e.g., `google_storage_bucket`). Must match the YAML filename (`<source_type>.yaml`). |
| `target_type` | ✅ | string | Primary AWS resource type emitted (e.g., `aws_s3_bucket`). For multi-resource modules, the conceptual "main" type. |
| `service_name` | ✅ | string | Maps to `Translation.service_name` and the `target/modules/<service_name>/` subdir. Must match an existing AWS module spec (registered in `migrator/translate/__init__.py::all_aws_module_specs()`). |
| `confidence` | optional | string | One of `HIGH`, `MEDIUM`, `LOW`. Informational only — actual confidence comes from `migrator.plan.coverage`. Default: `HIGH`. |
| `description` | optional | string | One-liner for docs. |

## `inputs`

A map of `aws_input_name → source-extraction spec`. Each entry defines
how one input variable to the AWS module body gets its value from
source GCP args.

Shorthand forms:

```yaml
inputs:
  # Copy verbatim from source key "name" into output key "bucket_name"
  bucket_name: name

  # Same key on both sides (just lists what to include)
  region: region
```

Full form (dict):

```yaml
inputs:
  region:
    from: location                  # source key (default: same as target)
    enum_map:                       # map source values to target values
      US: us-east-1
      EU: eu-west-1
    transform: lowercase            # built-in transforms (see below)
    default: us-east-1              # used when source key is missing or empty
```

### Built-in transforms

- `lowercase`, `uppercase` — string case
- `strip_prefix:foo`, `strip_suffix:bar` — substring removal
- `int`, `bool`, `string` — type coercion
- `dotted_to_underscored` — replace dots with underscores (HCL-safe identifiers)
- `quote_string` — wrap in double quotes (HCL literal)

## `compliance_defaults`

Profile-keyed dict of attribute overrides applied when the operator
selects a compliance profile (none/hipaa/soc2/pci):

```yaml
compliance_defaults:
  hipaa:
    block_public_access: true
    kms_encryption: true
  pci:
    block_public_access: true
    kms_encryption: true
  soc2:
    versioning: true
```

Keys merge OVER per-resource source values? **No** — source values
always win. Profile defaults fill GAPS (when source didn't set an
attribute). Same semantics as today's `compliance_profiles.py` defaults.

## `python_override` (escape hatch)

When a rule needs imperative logic (topology shift, cross-resource
lookup, complex conditional), declare a Python override:

```yaml
python_override: migrator.translate.overrides.pubsub_fanout
```

The named module must export `translate(resource, *, compliance_profile, rule_dict) → Translation`.
The engine passes the parsed rule dict for reference. The Python
function takes over completely; rule-driven inputs/compliance_defaults
are NOT auto-applied (the Python code is in full control).

If `python_override` is present, the engine prefers it over the
declarative path. The rule file's other fields become reference
documentation.

## Validation

Loader rejects rule files that:
- Don't parse as YAML
- Are missing `source_type`, `target_type`, or `service_name`
- Have `source_type` that doesn't match the filename
- Reference a `service_name` that isn't in `all_aws_module_specs()`
- Reference a `python_override` module that can't be imported
- Have `inputs.X.enum_map` with non-string source/target values
- Have unknown `transform` names

Failures are logged at engine startup with the offending file path.
The engine continues with other valid rules.

## Example: full rule file

```yaml
# migrator/translate/rules/google_artifact_registry_repository.yaml
source_type:  google_artifact_registry_repository
target_type:  aws_ecr_repository
service_name: ecr-repository
confidence:   HIGH
description: "GCP Artifact Registry → AWS ECR. Container/Helm registries."

inputs:
  repository_name:
    from: repository_id          # GCP uses repository_id; AWS calls it name
  format:
    from: format
    enum_map:
      DOCKER: docker             # AWS uses lowercase
      MAVEN:  maven
      NPM:    npm
      PYTHON: python
    default: docker

compliance_defaults:
  hipaa:
    image_scanning_enabled: true
    image_tag_mutability: IMMUTABLE
    kms_encryption: true
  pci:
    image_scanning_enabled: true
    image_tag_mutability: IMMUTABLE
    kms_encryption: true
```
