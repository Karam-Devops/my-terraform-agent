# Customer translation profiles

This directory holds YAML profile files that externalize customer-specific
GCP→AWS local-ref substitutions. The Migrator engine loads
`_default.yaml` + (optionally) a customer-named profile, merges them
(customer overrides defaults), and applies the resulting substitutions
to translator output before the unresolved-local sanitizer runs.

## Why externalize?

Before this refactor, customer-specific substitution patterns were
hardcoded inline in two emitter files:
- `migrator/output/terraform_emitter.py` (`_SOURCE_REF_SUBSTITUTIONS` list)
- `migrator/output/terragrunt_emitter.py` (`_GCP_TO_AWS_LOCAL_REFS` list)

Onboarding a new customer required editing Python code and authoring a
test. That works for a handful of customers; it doesn't scale to dozens.

With profile YAMLs:
- Non-developers can extend coverage (just edit YAML)
- Audit trail per customer (one file per customer)
- Same engine binary works for any customer
- Selection happens at run_migration() call-time

## Adding a new customer

1. Run the engine against the customer's repo with `customer_profile = "default"`.
2. Inspect the emitted output for `${"TODO-local-X-Y-Z"}` placeholders. Each
   is a local-ref the engine couldn't auto-resolve.
3. For each TODO that has an AWS analog (e.g., `local._customer_project.locals.gcp_project`
   maps to `local.environment`), add a key to `<customer_name>.yaml`:

```yaml
local_substitutions:
  "${local._customer_project.locals.gcp_project}": "${local.environment}"
```

4. Re-run with `customer_profile = "<customer_name>"`. The TODO is now a clean
   substitution.

## File format

```yaml
local_substitutions:
  "<source-ref>":   "<aws-target-ref>"

metadata:                    # optional — diagnostic only
  name:        "..."
  description: "..."
  applies_to:  "..."
```

### Source-ref forms

Both forms should be listed when applicable:
- **Interpolation:** `"${var.environment}"` or `"${local._project.locals.X}"`
- **Bare:** `"var.environment"` (when the ref appears outside `${...}`)
- **Mangled:** `"${var_environment}"` (python-hcl2 mangles `${var.X}` to `${var_X}` in dict-key positions; list this form if your source uses refs in dict keys)

### Target-ref values

Use AWS-target locals available in our env-root scope:
- `local.environment` (the env name, e.g., "dev", "prod")
- `local.region` (the AWS region)
- `local.account_id` (the AWS account ID)
- `local.common_tags` (the env's tag map)

Or, for cases where the source value should become a literal:
- Quoted strings: `'"us-east-1"'` (note the outer quotes — YAML string containing HCL quotes)

## Merge semantics

When `run_migration(customer_profile="X")`:
1. Load `_default.yaml`
2. Load `X.yaml` (if exists)
3. Merge `X.yaml.local_substitutions` over `_default.yaml.local_substitutions`
   (customer keys win on conflict)
4. Sort by key length, descending — longer keys checked first to prevent
   prefix matches (e.g., `local._project.locals.project_id` before `local.env`)
5. Apply substitutions to translator output

## Profile selection

```python
# Default behaviour: only _default.yaml applied
run_migration(repo_path, target_cloud="aws")

# Customer-specific profile + defaults
run_migration(repo_path, target_cloud="aws", customer_profile="dh")
```

Selection is also surfaced as a UI dropdown on the Migrate Repo page
(combo picker), reading the directory listing here for available profile
names (any `*.yaml` file other than `_default.yaml`).

## Profiles shipped today

| Profile      | Customer / Pattern                                           |
|--------------|--------------------------------------------------------------|
| `_default`   | Generic — var.environment, var.region, var.labels, local.env |
| `dh`         | DeepHealth — `_project.hcl` / `_env_configs.hcl` four-file include pattern with leading-underscore include locals |

Future: `acme_health`, `regional_bank`, etc. — one per onboarded customer.
