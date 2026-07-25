# Production deployment (Helm)

This is the production path. **`docker compose` (the [README quickstart](../README.md#quickstart-local-evaluation)) is local evaluation only — not production.** It runs everything in one Docker network with no auth by default, bundled Prometheus/Loki, and a SQLite file on a named volume. Production runs LLopster as its own Helm release against your cluster's existing observability stack.

## Prerequisites (Helm path)

- A running Kubernetes (or k3s) cluster, with a `kubectl` context pointed at it
- [Helm 3](https://helm.sh/docs/intro/install/)
- Your cluster's **existing Prometheus + Loki** query APIs reachable from the cluster (BYO mode — see [Install modes](#install-modes) below). LLopster does not need to be the thing that scrapes your apps; it only queries.
- The **Prometheus Operator** (e.g. kube-prometheus-stack), *only if* you plan to set `alertRules.enabled=true` to install LLopster's curated `PrometheusRule` starter pack as a CRD
- An [Anthropic API key](https://console.anthropic.com/); optionally a GitHub token (PR creation) and a Slack incoming webhook (notifications)

`./scripts/bootstrap-helm.sh` only fetches the chart's optional subchart tarballs (Helm needs declared dependencies present on disk to render, even when you're not installing them) — it does not provision a cluster, install `kubectl`/Helm, or stand up Prometheus/Loki for you.

## Install modes

LLopster installs as **its own release** — the agent, the read-only dashboard, and a PostgreSQL backing store. The observability stack is **decoupled**: point LLopster at your cluster's existing Prometheus + Loki (BYO, recommended for production), or let the chart bundle a self-contained one for evaluation.

Three independent toggles in [helm-chart/values.yaml](../helm-chart/values.yaml):

- `prometheus.bundled` / `loki.bundled` — `true` installs the community subchart; `false` skips it and uses `prometheus.url` / `loki.url` pointing at your existing query API.
- `alertRules.enabled` — opt in to LLopster's curated `PrometheusRule` starter pack (cross-cutting infra alerts annotated with hints the prompt uses). Requires the Prometheus Operator; for raw Prometheus, see [helm-chart/docs/integration-recipes/](../helm-chart/docs/integration-recipes/).

The chart is published to ghcr as an OCI artifact on every tagged release —
no repo clone or `helm repo add` needed:

```bash
# BYO observability (production): point at your existing Prometheus + Loki and
# wire your existing AlertManager to LLopster's webhook (integration-recipes).
helm install llopster oci://ghcr.io/synchrony-solutions/charts/llopster \
  --version 0.2.0 \
  --namespace llopster --create-namespace \
  --set prometheus.bundled=false \
  --set prometheus.url=http://<your-prometheus>.<ns>.svc:9090 \
  --set loki.bundled=false \
  --set loki.url=http://<your-loki>.<ns>.svc:3100 \
  --set agent.secrets.ANTHROPIC_API_KEY=sk-ant-... \
  --set agent.secrets.GITHUB_TOKEN=ghp_... \
  --set agent.secrets.SLACK_WEBHOOK_URL=https://hooks.slack.com/...
```

Installing from a clone instead (development, or unreleased chart changes):

```bash
# From the repo root
# bootstrap-helm.sh fetches the optional subchart tarballs (required even in
# BYO mode — Helm needs declared dependencies present on disk to render).
./scripts/bootstrap-helm.sh --update

# Same flags as the OCI install above:
helm install llopster helm-chart/ \
  --namespace llopster --create-namespace \
  --set prometheus.bundled=false \
  --set prometheus.url=http://<your-prometheus>.<ns>.svc:9090 \
  --set loki.bundled=false \
  --set loki.url=http://<your-loki>.<ns>.svc:3100 \
  --set agent.secrets.ANTHROPIC_API_KEY=sk-ant-... \
  --set agent.secrets.GITHUB_TOKEN=ghp_... \
  --set agent.secrets.SLACK_WEBHOOK_URL=https://hooks.slack.com/...

# Or for the full build → push → deploy loop (auto-loads values.dev.yaml +
# values.secret.yaml), use:
#   ./scripts/push-images.sh
```

> **Schema migrations are automatic.** Alembic runs on every agent startup via `init_schema` in [src/db/engine.py](../src/db/engine.py) with three-way detection: fresh DB → `create_all` + stamp head; legacy pre-alembic DB → upgrade head; alembic-managed DB → upgrade head. You don't need a separate migrate-job in your chart.

## Wiring your existing AlertManager + Loki

[helm-chart/docs/integration-recipes/](../helm-chart/docs/integration-recipes/) is the BYO contract — what LLopster needs from your stack, the Loki label-matching rules, and copy-paste snippets for the AlertManager receiver and raw-Prometheus rule annotations. Read it before going BYO; most "zero log lines" or "alert never reaches LLopster" issues trace back to a label or receiver mismatch covered there.

The webhook itself should be authenticated in production — see [Securing the inbound surfaces](../README.md#securing-the-inbound-surfaces) in the README for the shared-secret setup and the exact AlertManager receiver config to send the bearer token. The chart is **secure-by-default**: it refuses to render when the agent or dashboard is exposed (an Ingress is enabled, or its Service is `LoadBalancer`/`NodePort`) without `agent.secrets.LLOPSTER_API_TOKEN` set — override for a trusted/local-only deploy with `--set agent.allowUnauthenticated=true`.

## Monitoring LLopster itself

The agent exposes a Prometheus scrape target at `GET /metrics` (runs by processing status, backlog/queue depth, runs created in the trailing hour, estimated trailing-day synthesis spend, and cost-breaker/manual-mode state — all computed from the database at scrape time, so they survive pod restarts). Turn on the bundled ServiceMonitor to have the Prometheus Operator scrape it:

```
--set agent.serviceMonitor.enabled=true \
--set agent.serviceMonitor.labels.release=<your-kube-prometheus-stack-release>
```

It's opt-in because it requires the Prometheus Operator CRDs (`monitoring.coreos.com/v1`). With it on, you can alert on the monitor itself — e.g. `llopster_backlog` climbing (pipeline stalled), `llopster_processing_mode_manual == 1` (cost breaker tripped), or `llopster_estimated_spend_usd_last_day` over budget.

## The services LLopster monitors are separate releases

LLopster's chart does **not** deploy the apps it watches. Each monitored service is its own repo, Helm chart, namespace, and GitHub PR target. The reference testbed (`~/dev/testbed-infra` for the shared kube-prometheus-stack + Loki + Grafana Alloy, plus `~/dev/demo-app` and `~/dev/order-service` as two independent app releases) demonstrated this end-to-end: two services, eight seeded bugs, with zero crossed routing. An earlier run produced 6 of 8 correct PRs on the right repo (the other two include a [known triage false-negative](../src/agent/triage.py) on application-level heartbeat alerts), but that number predates the triage fix and the removal of fix-locating hints from the seeded bugs, so it should be treated as illustrative, not a measured accuracy figure.

To register a service with LLopster, add it to `agent.servicesConfig` (the `service` alert label → `github_repo` mapping) and `agent.codebases` (the init-container clone source) — see [values.dev.yaml](../helm-chart/values.dev.yaml) for a two-service example.

See [helm-chart/values.yaml](../helm-chart/values.yaml) for the full set of tunables, and the [Configuration reference](../README.md#configuration-reference) in the README for the agent's own env vars. The chart targets k3s with Traefik ingress today; broader Kubernetes portability is planned.
