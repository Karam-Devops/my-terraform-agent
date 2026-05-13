# SRE Agent — Demo Machine Runbook

Step-by-step setup to run the Phase 8 SRE / Incident Response Agent
on a fresh demo machine against the `dev-proj-470211` GCP project.

**Assumptions**: gcloud SDK installed, Python 3.11/3.12 available,
git installed, browser available.

**IMPORTANT**: Use **Git Bash** (not PowerShell or CMD) for all
commands. PowerShell's execution policy blocks gcloud.ps1; the
setup script is bash-only.

---

## 1. Pull the branch (30 sec)

```bash
cd <your repo root>
git fetch origin phase8-sre-agent
git checkout phase8-sre-agent
git pull origin phase8-sre-agent
```

Verify you're on the right commit (`4ac7b83` or later):

```bash
git log --oneline -1
```

---

## 2. Python dependencies (~5 min, one-time)

```bash
pip install -r requirements.txt
```

If Python 3.14 or another bleeding-edge version causes wheel-build
failures, create a 3.11 venv:

```bash
py -3.11 -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
```

---

## 3. GCP authentication (~30 sec)

```bash
gcloud auth application-default login    # opens browser flow
gcloud config set project dev-proj-470211
gcloud config get-value account          # confirm identity
```

---

## 4. One-shot GCP setup (~1 min)

Creates Pub/Sub topic + subscription, enables 5 APIs, grants 5
read-only IAM roles to your identity. Idempotent — safe to re-run.

```bash
PROJECT_ID=dev-proj-470211 \
AGENT_SA="$(gcloud config get-value account)" \
SKIP_NOTIFICATION_CHANNEL=1 \
  bash scripts/sre_setup_gcp.sh
```

`SKIP_NOTIFICATION_CHANNEL=1` skips the Cloud Monitoring notification
channel step — irrelevant for demos that seed alerts directly, and
avoids the gcloud alpha/beta component prompt that hangs interactively
on some installs.

Expected output: 5 APIs enabled, topic + subscription confirmed,
5 IAM roles granted.

---

## 5. Clear stale Pub/Sub backlog (~5 sec)

Only needed if the subscription has been used before — flushes any
messages from prior demo runs so you start with a clean slate.

```bash
gcloud pubsub subscriptions seek sre-agent-pull-subscription \
    --time=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
    --project=dev-proj-470211
```

---

## 6. Seed 3 demo alerts (~5 sec)

Publishes 3 canned scenarios (SEV2 ALB 5xx, SEV1 Cloud SQL
connections, SEV2 GKE ImagePullBackOff) directly to the Pub/Sub
topic.

```bash
python scripts/sre_seed_demo_alerts.py --project=dev-proj-470211 --all
```

---

## 7. Generate test evidence (~3 sec)

So the agent has something to find when you Run triage. Creates a
throwaway firewall rule that produces a `v1.compute.firewalls.insert`
audit log entry.

```bash
NAME="sre-demo-$(date +%s)"
gcloud compute firewall-rules create "$NAME" \
    --network=default --action=deny --rules=tcp:1 \
    --source-ranges=10.0.0.0/32 \
    --project=dev-proj-470211 --quiet
```

The rule denies traffic on `tcp:1` from a `/32` of private IP space —
no real traffic ever hits it, harmless. Optional cleanup later:

```bash
gcloud compute firewall-rules delete "$NAME" \
    --project=dev-proj-470211 --quiet
```

---

## 8. Launch Streamlit (~10 sec to first paint)

```bash
python -m streamlit run "app/🏠_Home.py"
```

Browser opens to `http://localhost:8501`. If Streamlit auto-picks
a different port (e.g., 8502), use whatever the terminal prints
in its `Local URL:` line.

---

## 9. Demo flow in browser (~3 min)

1. Left nav → **🚨 SRE Agent**
2. Click **🔄 Pull now** → 3 alerts appear in the queue
3. Click **Triage →** on the **SEV2 ALB 5xx** card
4. Click **▶ Run triage** → wait ~5-10 sec → LLM-written hypothesis
   cards appear with confidence bars and evidence timeline
5. Expand **🤖 Refine with operator notes** → type something like
   *"That firewall rule was a test command, ignore it"* → click
   **Re-rank** → see confidence delta chips appear (▲ green for
   promoted, ▼ red for demoted)
6. Click **💾 Save snapshot** → hard-refresh browser (Ctrl-Shift-R)
   → **🔁 Restore** banner appears at top → click Restore → entire
   triage view re-hydrates without a re-run
7. Pick a different alert from the queue (e.g., SEV1 Cloud SQL),
   run triage → notice **📚 Past triages (2 recent)** expander
   appears above the context bar; use it to switch back to any
   prior triage

---

## Common gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `streamlit: command not found` | Bare CLI not on PATH | Use `python -m streamlit run ...` |
| `gcloud.ps1 cannot be loaded` | PowerShell execution policy | Switch to Git Bash |
| Source chips all `FAILED` with WinError 2 | gcloud not findable for subprocess | Verify `which gcloud` in same Git Bash terminal; restart Streamlit so the patched module reloads |
| `Cloud Pub/Sub is not reachable` after Pull on empty queue | Stale message from earlier — empty queue used to misreport | Resolved in commit `4ac7b83`; if still seeing it, restart Streamlit |
| Queue shows only 1 alert after seeding 3 | First Pub/Sub pull returns partial batch | Resolved in commit `4ac7b83` (client-side drain loop); if still seeing it, click Pull now again |
| 0 evidence in triage | Project quiet in last 60 min | Re-run Step 7 (firewall rule) within the lookback window, or widen **Lookback window** to 240 |
| Triage button does nothing | `sre/` module change not reloaded | Streamlit only hot-reloads page files. Ctrl-C and restart `python -m streamlit run ...` |
| `Hypotheses will appear here once Day 2/3 lands` message | Stale Day-1 placeholder text | Resolved in commit `4ac7b83` — context-aware empty-state messages now |

---

## How to verify the Pub/Sub plumbing without the UI

Anywhere in the runbook, you can confirm messages are flowing:

```bash
# Count unacked messages in backlog (no consumption)
gcloud pubsub subscriptions describe sre-agent-pull-subscription \
    --project=dev-proj-470211 --format=yaml | grep numUndelivered

# Peek (10-second lease, returns to backlog after)
gcloud pubsub subscriptions pull sre-agent-pull-subscription \
    --project=dev-proj-470211 --limit=5 --format=json
```

In the Console:
- **Pub/Sub → Subscriptions → `sre-agent-pull-subscription` → Metrics**
  → "Unacked message count" chart
- **Pub/Sub → Subscriptions → `sre-agent-pull-subscription` → Messages**
  → "Pull" button (leave **Enable ack messages** UNCHECKED so the
  Streamlit page can still consume them)

---

## Where things live

| Component | Path | Purpose |
|---|---|---|
| Engine entrypoint | `sre/run.py` | `run_incident_triage()` orchestrator |
| Result dataclasses | `sre/results.py` | AlertEnvelope / EvidenceItem / Hypothesis / IncidentResult |
| Pub/Sub puller | `sre/triggers/gcp_pubsub.py` | `list_pending_alerts`, `ack`, `nack` |
| Alert parser | `sre/triggers/alert_parser.py` | Cloud Monitoring + demo-seeder payload normalize |
| GCP sources | `sre/sources/{gcp_asset_changes, gcp_iam_changes, gcp_deploys}.py` | Per-source evidence collectors |
| Heuristic ranker | `sre/correlator.py` | Score + cluster + rank Hypothesis list |
| LLM writer | `sre/llm/hypothesis_writer.py` | Operator-grade prose for top-N hypotheses |
| LLM refine | `sre/llm/refine.py` | Re-rank with operator notes |
| Persistence | `sre/output/result_persistence.py` | gs:// / file:// snapshot + per-user registry |
| Streamlit page | `app/pages/7_🚨_SRE_Agent.py` | Operator UI |
| GCP setup | `scripts/sre_setup_gcp.sh` | Idempotent topic + sub + IAM bootstrap |
| Demo seeder | `scripts/sre_seed_demo_alerts.py` | Publishes 3 canned scenarios |

---

## Cleanup after the demo

```bash
# Drop the throwaway firewall rule(s)
gcloud compute firewall-rules list --project=dev-proj-470211 \
    --filter="name~^sre-demo-" --format="value(name)" | \
    xargs -I{} gcloud compute firewall-rules delete {} \
        --project=dev-proj-470211 --quiet

# (Optional) Tear down the Pub/Sub plumbing entirely
gcloud pubsub subscriptions delete sre-agent-pull-subscription \
    --project=dev-proj-470211 --quiet
gcloud pubsub topics delete sre-incident-alerts \
    --project=dev-proj-470211 --quiet
```

IAM grants on your user identity persist until manually removed — they
don't affect anything outside the SRE Agent's read paths and are safe
to leave for repeat demos.
