#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# scripts/push-images.sh
#
# Build and push the LLopster Docker image to GitHub Container Registry
# (ghcr.io/synchrony-solutions/...) and (optionally) trigger a Helm-driven
# rollout in the target k8s cluster.
#
# One image is produced:
#   1. ghcr.io/synchrony-solutions/llopster — agent + dashboard (single
#                                             image, SERVICE env var
#                                             picks process)
#
# demo-app no longer ships from this repo's chart — it lives in its own
# repo (~/dev/demo-app) with its own chart and (TODO) its own build
# script. This script intentionally does not touch demo-app.
#
# The image is built for linux/amd64 (k3s nodes) and tagged twice:
#   - <git-sha>   (immutable, used by helm upgrade so the Deployment spec
#                  actually changes and triggers a rolling restart)
#   - latest      (mutable convenience tag)
#
# Usage:
#   GHCR_USERNAME=your-gh-username GHCR_TOKEN=ghp_xxx ./scripts/push-images.sh
#
# Optional flags:
#   --no-deploy       skip the helm upgrade step (push only)
#   --release NAME    helm release name (default: llopster)
#   --namespace NS    k8s namespace      (default: llopster)
#   --kubeconfig PATH path to kubeconfig (default: $KUBECONFIG or ~/.kube/config)
#   --tag TAG         override the image tag (default: short git SHA)
# -----------------------------------------------------------------------------

set -euo pipefail

# ---- defaults ---------------------------------------------------------------
REGISTRY="ghcr.io/synchrony-solutions"
AGENT_IMAGE="${REGISTRY}/llopster"

# Chart appVersion — the tag a by-the-book `helm install` pulls by default
# (the chart's image.tag falls back to .Chart.AppVersion). We publish this tag
# alongside `latest` so a fresh install never ImagePullBackOffs on a tag that
# was never built. Parsed from Chart.yaml so it can't drift from the chart.
_CHART_YAML="$(dirname "$0")/../helm-chart/Chart.yaml"
APP_VERSION="$(sed -n 's/^appVersion:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' "${_CHART_YAML}" | head -1)"

# Use the short git SHA as the immutable tag so each push produces a unique
# Deployment spec — k8s only restarts pods when something in the spec changes.
# Build-tag policy.  We use the short git SHA as the immutable tag so each
# commit produces a unique Deployment spec.  But if the tree is dirty, the
# SHA points at the previous commit's snapshot while we'd actually build with
# the local working-tree files — and since imagePullPolicy=IfNotPresent, k8s
# nodes happily keep using their cached copy of that tag.  Appending -dirty
# (plus a short timestamp so retries roll) keeps every dirty-tree build
# distinguishable so helm rolls the pods and nodes re-pull fresh.
_REPO_ROOT="$(dirname "$0")/.."
DEFAULT_TAG="$(git -C "${_REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d-%H%M%S)"
if git -C "${_REPO_ROOT}" diff --quiet 2>/dev/null && \
   git -C "${_REPO_ROOT}" diff --cached --quiet 2>/dev/null; then
    : # clean tree, keep the bare SHA
else
    DEFAULT_TAG="${DEFAULT_TAG}-dirty-$(date +%H%M%S)"
fi

TAG="${DEFAULT_TAG}"
RELEASE="llopster"
NAMESPACE="llopster"
KUBECONFIG_FLAG=""
DEPLOY=true

# ---- arg parsing ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-deploy)   DEPLOY=false; shift ;;
        --release)     RELEASE="$2"; shift 2 ;;
        --namespace)   NAMESPACE="$2"; shift 2 ;;
        --kubeconfig)  KUBECONFIG_FLAG="--kubeconfig $2"; shift 2 ;;
        --tag)         TAG="$2"; shift 2 ;;
        -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 1 ;;
    esac
done

# ---- pre-flight -------------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "required: $1" >&2; exit 1; }; }
need docker
need git
$DEPLOY && need helm
$DEPLOY && need kubectl

if [[ -z "${GHCR_USERNAME:-}" || -z "${GHCR_TOKEN:-}" ]]; then
    echo "GHCR_USERNAME and GHCR_TOKEN must be set." >&2
    echo "Create a Personal Access Token at https://github.com/settings/tokens" >&2
    echo "with scopes: write:packages, read:packages." >&2
    exit 1
fi

# Move to repo root so docker build paths resolve.
cd "$(dirname "$0")/.."

echo "==> Logging in to ghcr.io as ${GHCR_USERNAME}"
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USERNAME}" --password-stdin

# Buildx is required for cross-platform builds (push linux/amd64 from Apple
# Silicon).  Create a builder once; subsequent runs reuse it.
if ! docker buildx inspect llopster-builder >/dev/null 2>&1; then
    echo "==> Creating buildx builder 'llopster-builder'"
    docker buildx create --name llopster-builder --use
else
    docker buildx use llopster-builder
fi

# ---- build & push -----------------------------------------------------------
# Publish the Chart.appVersion tag only from a CLEAN tree — it's the semver a
# fresh `helm install` pulls, so it must map to committed code, not a dirty
# working tree. On a dirty tree we skip it and warn (the sha/latest tags still
# publish). To cut a new immutable release, bump appVersion in Chart.yaml and
# push from a clean tree.
_clean_tree() {
    git -C "${_REPO_ROOT}" diff --quiet 2>/dev/null && \
    git -C "${_REPO_ROOT}" diff --cached --quiet 2>/dev/null
}

build_and_push() {
    local context="$1"
    local image="$2"
    local -a tag_args=(--tag "${image}:${TAG}" --tag "${image}:latest")
    local tags_desc="${TAG}, latest"
    if [[ -n "${APP_VERSION}" ]] && _clean_tree; then
        tag_args+=(--tag "${image}:${APP_VERSION}")
        tags_desc="${tags_desc}, ${APP_VERSION} (appVersion)"
    elif [[ -n "${APP_VERSION}" ]]; then
        echo "    NOTE: dirty tree — NOT publishing the ${APP_VERSION} appVersion tag" \
             "(commit first to publish an immutable release image)."
    fi
    echo
    echo "==> Building & pushing ${image} from ${context}"
    echo "    tags: ${tags_desc}"
    docker buildx build \
        --platform linux/amd64 \
        "${tag_args[@]}" \
        --push \
        "${context}"
}

build_and_push "." "${AGENT_IMAGE}"

echo
echo "==> Pushed:"
echo "    ${AGENT_IMAGE}:${TAG}   (agent + dashboard)"
echo "    ${AGENT_IMAGE}:latest"
if [[ -n "${APP_VERSION}" ]] && _clean_tree; then
    echo "    ${AGENT_IMAGE}:${APP_VERSION}   (Chart.appVersion — the helm-install default)"
fi

# ---- trigger rollout --------------------------------------------------------
if [[ "${DEPLOY}" == "false" ]]; then
    echo
    echo "==> --no-deploy set; skipping helm upgrade."
    echo "    To roll out manually:"
    echo "      helm upgrade ${RELEASE} ./helm-chart \\"
    echo "        --namespace ${NAMESPACE} \\"
    echo "        --set agent.image.tag=${TAG} \\"
    echo "        --set dashboard.image.tag=${TAG}"
    exit 0
fi

echo
# Detect first-time install vs upgrade.  --reuse-values requires an existing
# release; on a fresh cluster we use --install + -f values.yaml instead.
# shellcheck disable=SC2086
# Pull secret credentials are always injected from the env vars so they're
# never stale (avoids committing secrets to values.yaml).
PULL_SECRET_FLAGS=(
    --set "imagePullSecret.username=${GHCR_USERNAME}"
    --set "imagePullSecret.password=${GHCR_TOKEN}"
)

# values.secret.yaml — gitignored file with real secrets:
#   agent.secrets.{ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL, GITHUB_TOKEN, GITHUB_REPO}
#   postgresql.auth.password
# If missing, the install will fail because postgres/agent need real values.
SECRET_VALUES_FLAGS=()
if [[ -f helm-chart/values.secret.yaml ]]; then
    echo "==> Using helm-chart/values.secret.yaml for runtime secrets"
    SECRET_VALUES_FLAGS=(-f helm-chart/values.secret.yaml)
elif [[ "${ALLOW_PLACEHOLDERS:-false}" != "true" ]]; then
    cat >&2 <<EOF
ERROR: helm-chart/values.secret.yaml not found.

Without it, the install will fall back to CHANGEME placeholders for:
  - agent.secrets.{ANTHROPIC_API_KEY, SLACK_WEBHOOK_URL, GITHUB_TOKEN, GITHUB_REPO}
  - postgresql.auth.password

The agent will then fail to start (init container can't clone, db auth fails, etc.).

Create the file (gitignored — never commit) with content like:

    cat > helm-chart/values.secret.yaml <<'YAML'
    agent:
      secrets:
        ANTHROPIC_API_KEY: "sk-ant-..."
        SLACK_WEBHOOK_URL: "https://hooks.slack.com/services/..."
        GITHUB_TOKEN: "ghp_..."
        GITHUB_REPO: "synchrony-solutions/llmoki-demo-app"
    postgresql:
      auth:
        password: "choose-a-strong-password"
    YAML

    echo "helm-chart/values.secret.yaml" >> .gitignore

To intentionally proceed with placeholders (debugging only), re-run with:
    ALLOW_PLACEHOLDERS=true ./scripts/push-images.sh
EOF
    exit 1
fi

# Detect dev-cluster overrides — values.dev.yaml carries the
# synchrony-solutions home-lab specifics (storage classes, nip.io
# hostnames, demo-app servicesConfig). Layered between values.yaml and
# values.secret.yaml so customer installs that don't ship a values.dev.yaml
# get a generic install.
DEV_VALUES_FLAGS=()
if [[ -f helm-chart/values.dev.yaml ]]; then
    echo "==> Using helm-chart/values.dev.yaml for dev-cluster overrides"
    DEV_VALUES_FLAGS=(-f helm-chart/values.dev.yaml)
fi

if helm ${KUBECONFIG_FLAG} -n "${NAMESPACE}" status "${RELEASE}" >/dev/null 2>&1; then
    echo "==> Helm UPGRADE ${RELEASE} → image tag ${TAG}"
    # Always re-apply values.yaml so additions to it (new env vars, new
    # resource limits, new feature flags) actually reach the cluster on
    # subsequent deploys.  Previously this used --reuse-values, which kept
    # the OLD release's values and silently dropped any new keys — a
    # nasty footgun (AGENT_URL on the dashboard was the trigger).
    # Ordering matters: later -f / --set wins, so secrets and image tags
    # override values.yaml's placeholders.
    # shellcheck disable=SC2086
    helm upgrade "${RELEASE}" ./helm-chart \
        ${KUBECONFIG_FLAG} \
        --namespace "${NAMESPACE}" \
        -f helm-chart/values.yaml \
        "${DEV_VALUES_FLAGS[@]}" \
        "${SECRET_VALUES_FLAGS[@]}" \
        --set agent.image.tag="${TAG}" \
        --set dashboard.image.tag="${TAG}" \
        "${PULL_SECRET_FLAGS[@]}" \
        --wait \
        --timeout 5m
else
    echo "==> No existing release; running helm INSTALL ${RELEASE}"
    echo "    (subcharts must already be fetched — run scripts/bootstrap-helm.sh first)"
    # shellcheck disable=SC2086
    helm install "${RELEASE}" ./helm-chart \
        ${KUBECONFIG_FLAG} \
        --namespace "${NAMESPACE}" \
        --create-namespace \
        -f helm-chart/values.yaml \
        "${DEV_VALUES_FLAGS[@]}" \
        "${SECRET_VALUES_FLAGS[@]}" \
        --set agent.image.tag="${TAG}" \
        --set dashboard.image.tag="${TAG}" \
        "${PULL_SECRET_FLAGS[@]}" \
        --wait \
        --timeout 5m
fi

echo
echo "==> Rollout status"
# shellcheck disable=SC2086
kubectl ${KUBECONFIG_FLAG} -n "${NAMESPACE}" rollout status deployment/llopster-agent     --timeout=3m || true
# shellcheck disable=SC2086
kubectl ${KUBECONFIG_FLAG} -n "${NAMESPACE}" rollout status deployment/llopster-dashboard --timeout=3m || true
# demo-app no longer rolls from this chart — it's a separate helm release
# in its own repo. See ~/dev/demo-app/helm-chart/.

echo
echo "==> Done.  Pods now running tag ${TAG}."
