#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# scripts/bootstrap-helm.sh
#
# One-time (or fresh-checkout) helm setup:
#   1. Register the upstream chart repositories the parent chart depends on.
#   2. Fetch the pinned subchart tarballs into helm-chart/charts/.
#
# Run this:
#   - once per dev machine before the first `helm install`
#   - on every CI runner before installing
#   - any time helm-chart/Chart.yaml's `dependencies:` block changes
#     (in that case, re-run `helm dependency update` first to refresh the lock)
#
# Usage:
#   ./scripts/bootstrap-helm.sh           # use Chart.lock (deterministic)
#   ./scripts/bootstrap-helm.sh --update  # re-resolve & rewrite Chart.lock
# -----------------------------------------------------------------------------

set -euo pipefail

UPDATE=false
[[ "${1:-}" == "--update" ]] && UPDATE=true

cd "$(dirname "$0")/.."

command -v helm >/dev/null 2>&1 || { echo "helm not installed" >&2; exit 1; }

echo "==> Registering helm repositories"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana              https://grafana.github.io/helm-charts             >/dev/null
helm repo add bitnami              https://charts.bitnami.com/bitnami                >/dev/null

echo "==> Refreshing repo index"
helm repo update

if [[ "${UPDATE}" == "true" ]]; then
    echo "==> Resolving & rewriting Chart.lock"
    helm dependency update ./helm-chart
else
    echo "==> Fetching pinned subcharts (from Chart.lock)"
    helm dependency build ./helm-chart
fi

echo
echo "==> Subcharts now in helm-chart/charts/:"
ls -lh ./helm-chart/charts/ 2>/dev/null || echo "    (none — check for errors above)"

echo
echo "Next:"
echo "  helm install llopster ./helm-chart -n llopster --create-namespace -f helm-chart/values.yaml"
