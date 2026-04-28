# mtagent Cloud Run image — PSA-1 (Phase 5A)
#
# Single-stage image baking:
#   - Python 3.14 runtime
#   - Terraform binary (matched to provider_versions/.terraform.lock.hcl)
#   - gcloud SDK (for the importer's asset enumeration + describe calls)
#   - conftest (for the policy engine's Rego evaluation)
#   - Python deps from requirements.txt (frozen on dev machine 2026-04-28)
#
# Designed for Cloud Run (CG-8H spec). Runtime characteristics:
#   - Listens on $PORT (defaults to 8080) — Cloud Run injects this
#   - Writes per-request workdirs to /tmp/imported/<request_uuid>/
#     (matches MTAGENT_IMPORT_BASE convention; see common/workdir.py)
#   - All project IDs / bucket names / region settings env-overridable
#     so a single image runs against any host/target project pair
#     without rebuild
#
# Future optimizations (NOT in PSA-1):
#   - Multi-stage build to slim final image (~600MB → ~200MB)
#   - Pre-baked KB JSON files in /app/importer/knowledge_base/ to skip
#     the bootstrap step on cold start (D-6 fix already handles fresh
#     workdir, but pre-bake saves the ~5s bootstrap on first request)
#   - Distroless base for security hardening

FROM python:3.14-slim

# --- Build args (overridable via --build-arg) ---
# Pin via .terraform.lock.hcl in repo. Bump together when upgrading.
ARG TERRAFORM_VERSION=1.9.8
ARG GCLOUD_VERSION=496.0.0
ARG CONFTEST_VERSION=0.55.0

# --- System dependencies ---
# curl + unzip for tool installation; ca-certificates for HTTPS;
# git not needed at runtime; tini for clean process supervision.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
        tini \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# --- Terraform binary ---
# Pinned to the version in provider_versions/.terraform.lock.hcl for
# reproducibility. /usr/local/bin/terraform is the path
# common/terraform_path.py expects when TERRAFORM_BINARY env var
# is not set.
RUN curl -fsSL "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" -o /tmp/tf.zip \
    && unzip /tmp/tf.zip -d /usr/local/bin \
    && rm /tmp/tf.zip \
    && terraform --version

# --- gcloud SDK ---
# Installed via the official tarball install path (smaller than
# apt-get install google-cloud-cli, ~250MB vs ~400MB). The components
# we need: gcloud core + the asset / iam / kms / pubsub / run / storage
# command groups (all are core; no separate component install needed).
RUN curl -fsSL "https://storage.googleapis.com/cloud-sdk-release/google-cloud-cli-${GCLOUD_VERSION}-linux-x86_64.tar.gz" -o /tmp/gcloud.tar.gz \
    && tar -xf /tmp/gcloud.tar.gz -C /opt \
    && /opt/google-cloud-sdk/install.sh --quiet --usage-reporting=false --path-update=false \
    && rm /tmp/gcloud.tar.gz \
    && /opt/google-cloud-sdk/bin/gcloud --version
ENV PATH="/opt/google-cloud-sdk/bin:${PATH}"

# --- conftest binary (policy engine) ---
RUN curl -fsSL "https://github.com/open-policy-agent/conftest/releases/download/v${CONFTEST_VERSION}/conftest_${CONFTEST_VERSION}_Linux_x86_64.tar.gz" -o /tmp/conftest.tar.gz \
    && tar -xf /tmp/conftest.tar.gz -C /usr/local/bin conftest \
    && rm /tmp/conftest.tar.gz \
    && conftest --version

# --- Python application ---
WORKDIR /app

# Install Python deps first (separate layer so code changes don't
# bust the dep cache).
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /app/requirements.txt

# Copy application code (.dockerignore filters out venv, imported/,
# __pycache__, etc.)
COPY . /app/

# --- Runtime config (all env-overridable) ---
# Defaults are sensible for the Stage-1 internal-dev deployment
# (mtagent-internal-dev hosting + dev-proj-470211 target). Cloud Run
# env vars will override these in production.
ENV PORT=8080 \
    HOST_PROJECT_ID=mtagent-internal-dev \
    GCP_LOCATION=us-central1 \
    MTAGENT_STATE_BUCKET=mtagent-state-dev \
    MTAGENT_IMPORT_BASE=/tmp/imported \
    MTAGENT_PERSIST_BLUEPRINTS=0 \
    IMPORTER_AUTO_QUARANTINE=1 \
    TRANSLATOR_TARGETS_ALLOWED=aws \
    MAX_TRANSLATION_WORKERS=4 \
    MTAGENT_LOG_FORMAT=json \
    MTAGENT_LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

# Per-request workdir base (writable tmpfs on Cloud Run)
RUN mkdir -p /tmp/imported

# --- Process supervision + entrypoint ---
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command: Streamlit UI on $PORT.
# Phase 6 (PUI-1) creates app/main.py; until then, this fails fast
# at container start, which is the right signal for Phase 5A
# integration testing. Override via `docker run ... <cmd>` for CLI
# ad-hoc use:
#   docker run --rm -it mtagent python -m my-terraform-agent.importer.run
CMD ["sh", "-c", "streamlit run app/main.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true"]
