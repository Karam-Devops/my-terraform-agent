# Migrator pre-push checklist

This file is for **anyone (human or AI) modifying the Migrator engine**
— translators, sanitizer, wiring, emitter. The checks below codify
the post-mortem of Kiro Power's v6/v7/v8 reviews, where the same
classes of bugs kept slipping past Tier 0/1 because they were
semantic, not syntactic.

## Why this exists

Tier 0 (HCL parse) and Tier 1 (terraform fmt) are necessary but not
sufficient. They confirm the output is **syntactically valid HCL**.
They do NOT confirm:

- The output will pass `terraform plan` / `terraform apply`.
- The literal string values inside variables make sense to AWS.
- The emitted documentation matches the actually-emitted modules.
- The customer's source data has been extracted (not just produced empty maps).

Kiro Power was catching all of those because he was reading the
output as an operator would. This checklist + the
`scripts/preflight_migrator.py` script bring the same discipline
in-house.

---

## Automated checks — run before every push

```bash
python scripts/preflight_migrator.py
```

This:
1. Emits a fresh migration on the customer fixture.
2. Runs Tier 0/1 (always) + Tier 2 on **one canonical env**
   (`environments_dev` by default — fast, ~30s vs the 6-min full sweep).
3. Greps the emitted output for every known antipattern Kiro caught.
4. Verifies the HIPAA-header lists only services actually emitted.

Exit code 0 = clean. Non-zero = critical issue found, see output.

To run faster when you've just emitted and only want re-checks:

```bash
python scripts/preflight_migrator.py --skip-migration
```

To run Tier 2 against a different env (e.g., the cross-env case):

```bash
python scripts/preflight_migrator.py --canonical-env environments_terarecon
```

To do a full Tier 2 sweep instead (before a major release):

```bash
# Run from a Python script — full sweep takes ~6 min on the 941-resource fixture
python -c "
from migrator.run import run_migration
r = run_migration(r'C:\Users\41708\gcp-iac-fixtures\simple-gcp',
                  target_format='terraform', skip_tier2=False,
                  compliance_profile='hipaa', customer_profile='dh')
print('OVERALL:', r.validation.get('overall_passed'))
"
```

---

## Discipline questions — only a human can answer

The script catches the antipatterns I've SEEN. It can't catch new
classes. Before declaring a fix done, ask yourself:

1. **Did I grep the CUSTOMER SOURCE for every variant of the field
   I'm extracting?**
   - DH's source uses `cloudsql_instances` for SQL but vanilla GCP
     modules use `sql_config`. If a translator only checks one, the
     other half of the source emits empty placeholders.
   - Pattern: `grep -rh "<field_name>\s*=" /path/to/customer-source | sort -u`

2. **Did I grep MY EMITTED OUTPUT for the literal antipattern I just
   fixed, across ALL envs?**
   - When fixing `vpc_id = "TODO-vpc-id"`, grep all 23 envs after the
     fix to verify it's gone everywhere, not just in the one env I
     tested.
   - The preflight script automates this for known antipatterns. New
     ones you discover should be added to `_ANTIPATTERNS` in the script.

3. **Did I read 2 actual main.tf files end-to-end as an operator
   would?**
   - One I expect to work cleanly (e.g., `environments_dev`).
   - One edge case (e.g., `environments_terarecon` — cross-env refs,
     no in-env VPC module).
   - Open the file. Read top to bottom. Does it look like something
     you'd hand to a customer?

4. **Walking through `terraform apply` mentally**: would each
   `default = "TODO-..."` value be accepted by AWS?
   - `default = "TODO-supply-vpc_id"` — AWS rejects `vpc-id` as a
     valid VPC ID at apply. The default LOOKS like a placeholder but
     it's a real value terraform will pass through.
   - Right pattern: `nullable = false` with no default. Forces
     operator to supply via tfvars. Fails fast at plan with a clear
     "variable is required" message.

5. **Does each emitted documentation/header string match what's
   actually in this env?**
   - HIPAA header listing `alb, eks, rds, s3, secrets, vpc` for an
     env that only emits VPC modules misleads operators.
   - Intersect global capability lists with per-env actuals before
     emitting any "applied to: X" claim.

---

## When the preflight script finds something new

If you spot a new antipattern Kiro (or a customer) flags:

1. **Add it to `_ANTIPATTERNS`** in `scripts/preflight_migrator.py` with:
   - A clear regex
   - Severity (`critical` = will fail plan/apply; `minor` = degraded
     but works)
   - A hint pointing at the right file to fix
2. **Confirm the regex catches the live occurrence** — run the
   script against the current broken output, see your new pattern in
   the report.
3. **Then fix the underlying bug** — and re-run preflight to verify
   the regex now reports zero hits.

The script is the regression net. Every new bug-class deserves an
entry so we never re-discover it later.

---

## What the script does NOT cover (yet)

- **No `terraform plan` simulation** — Tier 4 territory, needs AWS
  credentials. The discipline question #4 above is the human substitute.
- **No customer-source coverage report** — i.e., "of all the fields
  in this source file, which ones did our translator actually read?"
  Manual today; could automate with a per-translator field map.
- **No semantic-correctness assertions** — e.g., "every aws_lb has
  ssl_certificate_arn referencing a real cert resource somewhere in
  the tree". Doable but invasive.

These are good follow-up additions. File an issue / TODO when one bites.
