# LLopster

An AI-augmented SRE agent: when a Prometheus alert fires, LLopster investigates the logs and metrics, proposes a code fix, and opens a pull request — with a human reviewing the diff before it merges.

**Is this for you?** You're a self-hosted, single-cluster shop running **Kubernetes + Prometheus + Loki**, with an [Anthropic API key](https://console.anthropic.com/) and services on GitHub. LLopster attaches to your *existing* observability stack (it doesn't replace it) and opens PRs against your *own* service repos.

**At a glance:**

| | |
|---|---|
| **What it needs** | Kubernetes, Prometheus, Loki, an Anthropic API key, GitHub (optional, for PRs), Slack (optional, for notifications) |
| **Try it locally** | `docker compose up -d --build` — see [Quickstart](#quickstart-local-evaluation) below (~5 min, not production) |
| **Run it for real** | `helm install llopster oci://ghcr.io/synchrony-solutions/charts/llopster` — see [docs/PRODUCTION.md](docs/PRODUCTION.md) |
| **License** | Source-available ([FSL-1.1-ALv2](#license)), free to self-host, paid tiers unlock at runtime |

## How it works

When Prometheus alerts fire, the agent runs a tiered LLM pipeline — a cheap Haiku **triage** gate, a Sonnet **investigation** that picks the likely-affected files, then an Opus **synthesis** that sees only those files plus collected Loki logs and Prometheus metrics — and gets back a unified-diff patch. High-confidence patches are committed to a new branch on the right repo and opened as a **draft** pull request for human review (announced in Slack with a PR-link button).

```
AlertManager fires
    → POST /webhook
        → insert a Run row (pending) and return run_id immediately
        → background task picks it up:
            → parse alert + check pre-pipeline filter
                ↘ skip if non-firing, severity=info, alertname in ignore-list,
                  or service not in services.yaml — zero outbound HTTP, zero tokens
            → duplicate-PR dedup: skip `duplicate-pending-pr` if a recent Run
              with the same alert fingerprint already has an open PR
            → processing_mode check: in `manual` mode, park the Run at `queued`
              (no LLM calls) until an operator dispatches it from the dashboard
            → triage (Haiku): proceed | skip — non-actionable + high-confidence
              short-circuits here; severity=critical bypasses the gate
            → context_collector pulls Loki logs + PromQL metrics  (status: collecting)
            → investigation (Sonnet): reads the alert + logs + metrics + a codebase
              *outline* and names the ≤20 likely-affected files
            → synthesis (Opus): sees only the narrowed files + alert context  (status: generating)
              → returns: ## Root Cause / ## Proposed Patch / ## Confidence / ## Reasoning
            → if confidence ≥ PATCH_CONFIDENCE_THRESHOLD and a real diff is present:
                → github_client commits the diff to a new branch and opens a draft PR  (status: posting)
            → slack_client posts a Block Kit message with the diff and a "View PR" button
            → row updated to status: done

Background tasks running alongside:
    pr_poller   — polls GitHub every 60s for PR open/closed/merged status
    run_pruner  — every hour, deletes runs older than RUN_RETENTION_DAYS (default 90)
```

Each stage is independently kill-switchable from the settings table (`triage_enabled`, `investigation_enabled`, `processing_mode`) and degrades safely: a triage API failure fails-open to the full pipeline, and synthesis falls back to the full codebase blob if investigation didn't run or narrowed to files that don't exist on disk.

The webhook is non-blocking — it returns in <100ms with a `run_id` regardless of how long the pipeline takes. Clients (AlertManager, the dashboard, anything else) poll `GET /api/runs/{id}` (or subscribe to the SSE `/runs/{id}/stream`) to track progress. The full per-alert lifecycle (alert payload, queries used, log lines collected, metric samples, full LLM response, PR + Slack outcomes) is persisted to a database — see [Run history & API](#run-history--api).

Per-alert flow lives in [src/agent/processor.py](src/agent/processor.py); the pre-pipeline filter is [src/agent/alert_filter.py](src/agent/alert_filter.py); the webhook entry point is [src/api/main.py](src/api/main.py). Each integration is independently disable-able by leaving its env var unset (warning logged, no crash).

**Two processes.** LLopster runs as two containers sharing one database: the **agent** (`:8000`) receives webhooks, runs the pipeline, and runs the background tasks; the **dashboard** (`:3001`) is a read-only UI + JSON API over the same data. They're split so the dashboard keeps serving run history even if the agent is stuck in a crash-loop.

## Quickstart (local evaluation)

> **This is local evaluation, not production.** `docker compose` brings up everything in one Docker network with bundled Prometheus/Loki/Grafana, a SQLite file, and no inbound auth by default. It's the fastest way to see the loop work end to end. For a real deployment, see [docs/PRODUCTION.md](docs/PRODUCTION.md) — production runs the Helm chart against your cluster's existing **BYO** ("bring your own") Prometheus + Loki, not a bundled stack.

### 1. Clone and configure

```bash
git clone git@github.com:synchrony-solutions/LLopster.git
cd LLopster
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...

# Standard Anthropic accounts must set this false — the 1-hour prompt-cache TTL
# is a beta and every Sonnet/Opus call 400s without it. Defaults to true.
EXTENDED_CACHE_TTL=false  # set true only if your account has the extended-cache-ttl beta

# Optional integrations — leave blank to disable that feature
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
GITHUB_TOKEN=ghp_...

# Local stack (defaults are fine)
LOKI_URL=http://localhost:3100
PROMETHEUS_URL=http://localhost:9090
```

> Per-service codebase path + GitHub repo live in [config/services.yaml](config/services.yaml), not in `.env` — see [Multi-service configuration](#multi-service-configuration) below.

### 2. Start the full stack

```bash
docker compose up -d --build
```

This brings up everything, including the agent itself:

- **agent** at `http://localhost:8000` — FastAPI webhook receiver + pipeline (no UI; serves `/webhook`, `/health`, `/trigger`)
- **dashboard** at `http://localhost:3001` — the run history UI (visit `/runs` in a browser)
- **Prometheus** at `http://localhost:9090` — scraping the demo-app
- **Loki** at `http://localhost:3100` — receiving logs from the demo-app
- **Grafana** at `http://localhost:3000` (admin / admin)
- **AlertManager** at `http://localhost:9093` — wired to POST to `http://agent:8000/webhook` over the compose network
- **demo-app** at `http://localhost:8001/metrics` — running 5 buggy checks in parallel

`--build` is required after changes to `src/`, `requirements.txt`, or `demo-app/` source.

The agent reads its config from `.env` automatically (compose passes the variables through). `services.yaml` and `./demo-app` are bind-mounted into the container, so you can edit either without rebuilding the image. You should see startup warnings in `docker compose logs agent` for any optional integration whose env var is unset (Slack, GitHub) — that's expected, not a failure.

### 3. Install Python dependencies (for tests + local dev only)

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The agent runs in docker; this venv is only needed for `pytest` and the local-dev workflow described in [Local development workflow](#local-development-workflow) below.

### 4. Trigger a test alert

The demo-app is generating real failure signal continuously, so within ~2 minutes of bringing up the stack the alert rules in [config/prometheus/rules/demo-alerts.yml](config/prometheus/rules/demo-alerts.yml) will start firing on their own and AlertManager will route them to the agent. You can also POST a fixture directly without waiting — see [Firing test alerts manually](#firing-test-alerts-manually).

## Multi-service configuration

The agent supports multiple services, each with its own source directory and target GitHub repo. The mapping lives in [config/services.yaml](config/services.yaml):

```yaml
demo-app:
  codebase_path: ./demo-app
  github_repo: synchrony-solutions/llopster-demo

payments-api:
  codebase_path: /repos/payments-api
  github_repo: my-org/payments-api

checkout-frontend:
  codebase_path: /repos/checkout-frontend
  github_repo: my-org/checkout-frontend
```

### How alert routing works

- Each top-level YAML key is a service name.
- The agent looks up `alert.labels.service` against these keys when an alert arrives.
- `codebase_path` is the local directory the agent reads as the codebase blob it sends to Claude. Can be relative (resolved from the project root) or absolute.
- `github_repo` is `owner/repo` and is what the PR gets opened against.
- `GITHUB_TOKEN` is global (one token, env var) — the same token must have `repo` scope on every repo listed here.

### Adding a new service — checklist

1. **Clone the service's source** somewhere on your host (e.g. `~/repos/payments-api`).
2. **Mount it into the agent container** by adding a volume to the `agent` service in `docker-compose.yml`:
   ```yaml
   agent:
     # ...existing config...
     volumes:
       - ./config/services.yaml:/app/config/services.yaml:ro
       - ./demo-app:/app/demo-app:ro
       - ~/repos/payments-api:/app/repos/payments-api:ro    # <-- new
   ```
3. **Add an entry to `config/services.yaml`** using the path *as it appears inside the container*:
   ```yaml
   payments-api:
     codebase_path: /app/repos/payments-api
     github_repo: my-org/payments-api
   ```
4. **Make sure your alert rules emit a `service` label** that matches the YAML key. For example:
   ```yaml
   - alert: PaymentsHighErrorRate
     expr: rate(payments_5xx_total[5m]) > 0.01
     for: 5m
     labels:
       severity: critical
       service: payments-api    # must match the key in services.yaml
     annotations:
       summary: "payments-api 5xx rate elevated"
   ```
5. **Restart the agent** to pick up the new volume mount and registry entry: `docker compose up -d agent` (no rebuild needed — `services.yaml` and source paths are bind-mounted).
6. **Verify** by firing an alert with `labels.service: payments-api` and checking the agent log — you should see a context-collection line for the new service rather than a `no service config for 'payments-api' — skipping` warning.

### Failure modes

- **Alert with an unmapped `service` label** → agent logs a warning listing known services and skips patch generation. No crash, no PR.
- **Bad YAML or missing fields** → that entry is skipped with a warning at startup; other entries still load.
- **`config/services.yaml` missing entirely** → registry is empty; every alert is skipped with a warning. Useful for getting the rest of the stack up before wiring services.

## Firing test alerts manually

Five fixtures live in [tests/fixtures/](tests/fixtures/), one per demo scenario:

| Fixture | Alert | Bug it surfaces |
|---|---|---|
| `sample-alert.json` | `HelmValuesMisconfigured` | invalid memory unit `"512MBz"` |
| `db-pool-exhausted-alert.json` | `DatabasePoolExhausted` | pool sized 2 for 5 concurrent queries |
| `cache-hit-rate-low-alert.json` | `CacheHitRateLow` | `TTL_SECONDS / 1000` (s vs ms) |
| `upstream-timeout-spike-alert.json` | `UpstreamTimeoutSpike` | `TIMEOUT_SECONDS = 0.001` (s vs ms) |
| `heartbeat-stale-alert.json` | `HeartbeatStale` | heartbeat interval = 86400 (day vs minute) |

### Why you need the timestamp trick

The fixtures have a fixed `startsAt` timestamp baked in. The context collector queries Loki for logs in a 30-minute window around `startsAt` (`LOG_LOOKBACK_MINUTES`), so a stale timestamp means zero log lines come back. Inject the current time before posting:

```bash
# Helm values misconfiguration
curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d "$(jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
       '.alerts[0].startsAt = $ts' \
       tests/fixtures/sample-alert.json)" | jq

# Database pool exhaustion
curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d "$(jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
       '.alerts[0].startsAt = $ts' \
       tests/fixtures/db-pool-exhausted-alert.json)" | jq

# Cache hit rate collapse
curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d "$(jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
       '.alerts[0].startsAt = $ts' \
       tests/fixtures/cache-hit-rate-low-alert.json)" | jq

# Upstream timeout spike
curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d "$(jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
       '.alerts[0].startsAt = $ts' \
       tests/fixtures/upstream-timeout-spike-alert.json)" | jq

# Heartbeat stale
curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d "$(jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" \
       '.alerts[0].startsAt = $ts' \
       tests/fixtures/heartbeat-stale-alert.json)" | jq
```

### What success looks like

The webhook is non-blocking, so the POST returns **immediately** — before the pipeline has run — with a `run_id` and `status: pending`:

```json
{
  "received": 1,
  "alerts": [
    {
      "run_id": "abc-123",
      "alertname": "HelmValuesMisconfigured",
      "status": "pending"
    }
  ]
}
```

`status: pending` here is expected, **not** a failure — it just means the run was accepted and the pipeline is running in the background. Watch the outcome on the dashboard run detail at `http://localhost:3001/runs/abc-123`, or poll the JSON:

```bash
curl -s http://localhost:3001/api/runs/abc-123 | jq
```

Once the run reaches `done`, that payload is where success shows up. Expect:
- `log_lines > 0` — Loki query returned something (timestamp is current, demo-app is pushing logs)
- `confidence` ≥ `PATCH_CONFIDENCE_THRESHOLD` (default 4) — Claude's self-rating cleared the gate
- `slack_notified: true` — Slack message was delivered (if `SLACK_WEBHOOK_URL` is set)
- `pr_url` is non-null — a draft PR was opened on the service's GitHub repo (if `GITHUB_TOKEN` is set and the proposal contained a real diff)

### Adding your own fixture

Copy one of the existing fixtures and edit:

- `alerts[0].labels.alertname` — the alert name
- `alerts[0].labels.service` — must match a key in `services.yaml`
- `alerts[0].labels.severity` — `critical` / `warning` / `info`
- `alerts[0].annotations.summary` and `description` — what the on-call sees
- `alerts[0].generatorURL` — must contain a URL-encoded valid PromQL expression as `g0.expr=...`. The context collector parses this to build its Prometheus query. Use `python -c 'import urllib.parse; print(urllib.parse.quote("up == 0"))'` to encode.

## Dashboard

Open **`http://localhost:3001/`** once the stack is up. Server-rendered Jinja2 + HTMX (no JS build chain). Four pages, accessed from the top nav:

**Runs list (`/runs`)** — every alert the agent has processed, newest first. Columns: alertname, service, severity pill, processing-status pill (`pending` / `collecting` / `generating` / `posting` / `done` / `skipped` / `failed`), confidence badge, PR link with live status (open/merged/closed via the background `pr_poller`), Slack delivery state. Click any row for the detail view. Filter by service, alertname, or free-text search across alertname/service/LLM-response-text via the `q` field. Paginate with prev/next. While any run is in progress the table auto-refreshes every 2 seconds via HTMX polling and stops on its own once everything is terminal. A footer shows current retention status — `Retention: runs older than 90 days are pruned automatically — last sweep 2026-05-05 08:25:14 UTC`.

**Run detail (`/runs/{id}`)** — full lifecycle for one alert:

- Alert metadata: severity, service, run ID, received-at, trigger source (`alertmanager` or `manual`)
- Context collected: the LogQL + PromQL queries used, lookback window, expandable log-lines table, expandable metric-samples table, any collection warnings
- Claude's response: rendered Markdown root cause, syntax-highlighted unified diff (Pygments `DiffLexer`), confidence pill + reason, parsed reasoning paragraph, plus a "raw response" expandable
- Token usage: input / output / cache-read / cache-creation / latency
- Outcomes: GitHub PR link with branch name and live merge status, or the skip reason; Slack notification state
- Raw alert payload (collapsible JSON)

Like the list, the detail page polls every 2 seconds while the run is non-terminal so the status pill, log-line count, and outcomes update live as each pipeline phase completes. There's also an SSE endpoint at `/runs/{id}/stream` for clients that prefer push over poll.

**Trigger (`/trigger`)** — manually fire an alert through the pipeline without waiting for AlertManager. Two modes:

- **Replay** — pick an existing run from the dropdown; the agent re-runs the pipeline against it with current settings (useful after tweaking the prompt or settings).
- **Synthesize** — fill in alertname, service, severity, summary, description, and a custom log-lookback window. The agent fabricates a `ParsedAlert` and runs the full pipeline. Useful for proactive investigation: "scan the last 4 hours for checkout-frontend, tell me if anything looks off."

After submit, you're redirected to the run detail page where polling shows the pipeline executing live.

**Settings (`/settings`)** — runtime overrides stored in the `Setting` table; the processor reads these on every alert (DB wins over env var). Currently exposes:

- `patch_confidence_threshold` — minimum confidence to open a PR (default 4)
- `log_lookback_minutes` — how far back to query Loki around an alert
- HTMX-powered "Test connection" buttons for Slack and GitHub credentials (no-op probes that report success/failure inline)
- An **API access** card for setting/clearing the inbound shared-secret token (see [Securing the inbound surfaces](#securing-the-inbound-surfaces))
- A **License** card showing the active tier, entitled features, and expiry, with a field to paste or clear a license key. Unlocks paid tiers without a redeploy and never displays the raw key. See [Tiers & license key](#tiers--license-key).

Changes take effect on the next webhook — no restart required.

**Stats (`/stats`)** — daily aggregates of `done` / `failed` / `skipped` / other counts over the last N days (default 14). Powered by `/api/stats` for programmatic access.

## Run history & API

Every alert the agent processes is persisted to a database (SQLite for local dev, PostgreSQL in the cluster). The dashboard above renders this; for programmatic access it's also exposed as JSON.

### Endpoints

JSON API:

- **`GET /health`** — liveness probe.
- **`POST /webhook`** — AlertManager entry point. Returns immediately with `{"received": N, "alerts": [{"run_id": "...", "alertname": "...", "status": "pending"}, ...]}`. The actual pipeline runs in the background; poll the run endpoints below to track progress.
- **`GET /api/runs`** — list view. Returns paginated `RunSummary` objects (compact: alertname, service, severity, processing_status, confidence, PR/Slack outcomes, error). Query params: `limit` (1-200, default 50), `offset`, `service`, `alertname`, `q` (free-text search). Newest first.
- **`GET /api/runs/{run_id}`** — full detail for one run, including the original alert payload, the LogQL/PromQL queries used, every collected log line and metric sample, the full LLM response text, parsed root cause + diff + confidence, and PR/Slack outcomes. 404 if not found.
- **`GET /api/stats`** — daily aggregates by `processing_status` over the last `days` (default 14, max 90).

HTML pages (browser):

- **`GET /`** → 302 to `/runs`
- **`GET /runs`** + **`GET /runs/{id}`** — list and detail views
- **`GET /trigger`** + **`POST /trigger`** — manual alert form and submission
- **`GET /settings`** + **`POST /settings`** — runtime config form
- **`GET /stats`** — daily aggregate view
- **`GET /runs/{id}/stream`** — SSE stream of `processing_status` updates until terminal
- HTMX partials at `/runs/partial` and `/runs/{id}/partial` — used by the live-poll triggers; not normally hit directly

### Run lifecycle

Each `Run` row carries a `processing_status` that advances through:

```
pending → collecting → generating → posting → done
       ↘ queued    (processing_mode=manual: parked, awaiting operator dispatch)
       ↘ skipped   (pre-filter rejected: non-firing, severity=info, alertname in
                    ignore-list, service not in services.yaml, no Anthropic key,
                    duplicate-pending-pr, or the Haiku triage gate returned skip)
       ↘ failed    (unhandled exception; error_message populated)
```

The pre-pipeline filter (`src/agent/alert_filter.py`) runs at the very top of `process_alert`, BEFORE Loki/Prometheus collection. Skipped alerts cost zero outbound HTTP and zero LLM tokens. The Run row is still created so the dashboard can show what came in and why it was rejected (in `error_message`).

Status updates land on the row at every phase boundary, so polling clients see live progress. A crash mid-pipeline leaves the row at the last successful phase rather than disappearing.

The default ignore-list is intentionally tiny: `AlwaysFiringDemoAlert`, `PrometheusTargetDown`, `Watchdog`, `InfoInhibitor`. Operators extend it via the `ignore_alertnames` setting (comma-separated) on the settings page.

### Quick examples

```bash
# Fire a fixture (returns immediately with a run_id)
curl -s -X POST http://localhost:8000/webhook \
  -H "Content-Type: application/json" \
  -d "$(jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)" '.alerts[0].startsAt = $ts' \
       tests/fixtures/sample-alert.json)"
# → {"received": 1, "alerts": [{"run_id": "abc-123", "alertname": "...", "status": "pending"}]}

# List recent runs
curl -s http://localhost:3001/api/runs | jq '.items[] | {alertname, processing_status, confidence, pr_url}'

# Get full detail for one run
curl -s http://localhost:3001/api/runs/abc-123 | jq

# Filter
curl -s 'http://localhost:3001/api/runs?service=demo-app&limit=10' | jq
```

### Database location and inspection

In the compose quickstart, the SQLite file lives at `/app/data/llopster.db` inside the container, backed by the `agent-data` named volume. The slim base image doesn't ship the `sqlite3` CLI; the easiest options to inspect:

```bash
# Easiest: use the JSON API (works from any host)
curl -s http://localhost:3001/api/runs | jq

# Or copy the DB file out and open with whatever sqlite tool you like
docker compose cp agent:/app/data/llopster.db /tmp/llopster.db
sqlite3 /tmp/llopster.db "SELECT alertname, processing_status, parsed_confidence, pr_url FROM runs ORDER BY created_at DESC LIMIT 5;"
```

For local dev without docker, the default `DATABASE_URL` writes to `./data/llopster.db` in the project root (gitignored). In production (Helm), `DATABASE_URL` points at the chart's PostgreSQL instance instead.

## Local development workflow

For most usage, `docker compose up -d --build` is all you need — the agent runs inside the compose network with everything else.

When you're actively iterating on the agent code itself, host-bound `uvicorn --reload` is faster than rebuilding the image on every edit:

```bash
docker compose stop agent                                          # free up :8000
.venv/bin/uvicorn src.api.main:app --reload --port 8000            # host-bound, hot-reloads on edits
```

Caveats:

- **AlertManager won't reach you.** AlertManager is wired to `http://agent:8000` over the compose network, which doesn't resolve to the host. AlertManager-fired alerts will fail; only manually-`curl`'d fixtures hit your local uvicorn (which is the [Firing test alerts manually](#firing-test-alerts-manually) workflow anyway).
- **Override URLs in `.env`.** Your local agent talks to Loki/Prometheus on the host network, not the compose network. Either set `LOKI_URL=http://localhost:3100` and `PROMETHEUS_URL=http://localhost:9090` in `.env`, or unset them and let the defaults apply (they already point at localhost).
- **Switch back when done.** `docker compose start agent` puts the dockerized agent back in front of AlertManager.

## Production deployment

`docker compose` above is local evaluation only. Production runs LLopster as its own Helm release against your cluster's existing Prometheus + Loki — see **[docs/PRODUCTION.md](docs/PRODUCTION.md)** for the full guide: prerequisites, install modes (BYO vs bundled), the `helm install` walkthrough, and how monitored services register as separate releases. For wiring your existing AlertManager + Loki specifically, see [helm-chart/docs/integration-recipes/](helm-chart/docs/integration-recipes/).

## Configuration reference

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `ANTHROPIC_MODEL` | Model ID | `claude-opus-4-7` |
| `EXTENDED_CACHE_TTL` | Use the 1-hour prompt-cache TTL beta (`extended-cache-ttl-2025-04-11`). Set to `false` if your account doesn't have the beta. | `true` |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook | optional (disables Slack if unset) |
| `GITHUB_TOKEN` | GitHub PAT with `repo` scope on every service repo | optional (disables PR creation if unset) |
| `LLOPSTER_API_TOKEN` | Shared secret guarding the inbound write surfaces (`/webhook`, trigger, settings/license). **Empty = auth disabled** (loud startup warning). See [Securing the inbound surfaces](#securing-the-inbound-surfaces). Runtime-overridable via the `api_auth_token` setting (Settings → API access). | optional (auth disabled if unset) |
| `OPEN_PRS_AS_DRAFT` | Open LLM-authored PRs as drafts for human review (least-privilege). Set `false` to open ready-for-review PRs. Runtime-overridable via `open_prs_as_draft` setting. | `true` |
| `MAX_RUNS_PER_HOUR` | Cost circuit breaker: if this many runs are created within an hour, the agent trips to manual mode and stops spending. Ships **non-zero** so there's an out-of-the-box spend cap; `0` disables. Runtime-overridable via `/settings`. | `50` |
| `MAX_USD_PER_DAY` | Cost circuit breaker: if estimated synthesis spend over the trailing day reaches this dollar amount, the agent trips to manual mode. Ships **non-zero** as a safety net (tune to your budget); `0` disables. Runtime-overridable via `/settings`. | `25` |
| `PATCH_BACKOFF_MINUTES` | After an alert's pipeline finishes **without opening a PR** (below the confidence threshold, or no actionable patch), suppress repeated firings of that same alert for this many minutes — one re-investigation per window — so a flapping unfixable alert doesn't re-run the full pipeline every time. `0` disables. Runtime-overridable via `/settings`. | `60` |
| `PATCH_CONFIDENCE_THRESHOLD` | Min Claude self-confidence (1-5) to open a PR. Runtime-overridable via `/settings`. | `4` |
| `LLOPSTER_LICENSE_KEY` | Signed license JWT that unlocks paid (Business/Enterprise) features. Empty = full **Community** tier (gates nothing). Verified locally/offline; a key pasted in the dashboard (Settings → License) overrides this without a redeploy. See [Tiers & license key](#tiers--license-key). | optional (Community if unset) |
| `SERVICES_CONFIG` | Path to services registry YAML | `config/services.yaml` |
| `LOKI_URL` | Loki base URL | `http://localhost:3100` |
| `PROMETHEUS_URL` | Prometheus base URL | `http://localhost:9090` |
| `LOG_LOOKBACK_MINUTES` | How far back to pull Loki logs around an alert. Runtime-overridable via `/settings`. | `30` |
| `MAX_LOG_LINES` | Cap on log lines sent to the LLM | `200` |
| `RUN_RETENTION_DAYS` | Background pruner deletes runs older than this many days. `0` disables pruning. Runtime-overridable via `run_retention_days` setting. | `90` |
| `RUN_PRUNE_INTERVAL_SECONDS` | How often the pruner wakes up | `3600` |
| `DATABASE_URL` | SQLAlchemy async URL for run history | `sqlite+aiosqlite:///./data/llopster.db` (local), `sqlite+aiosqlite:////app/data/llopster.db` (compose), PostgreSQL (Helm) |

## Securing the inbound surfaces

LLopster's network surfaces — the AlertManager `/webhook`, the manual-trigger routes, and the dashboard's settings/license mutations — spend LLM money and open PRs with a write-scoped GitHub token. They are guarded by a single **shared-secret** check.

**Default is OFF.** If `LLOPSTER_API_TOKEN` is unset (and no `api_auth_token` runtime override exists), the check is **disabled** and a loud warning is logged at startup. This keeps local eval frictionless — but **set a token before exposing the agent or dashboard on any untrusted network**, and always set one in production.

When a token *is* configured, every protected route requires it (constant-time compared) and returns `401` otherwise. Clients present it two ways:

- `Authorization: Bearer <token>` — for AlertManager, `curl`, and the dashboard's server-side proxy calls.
- HTTP Basic (the **password** component; username ignored) — so a browser hitting the dashboard gets a native login prompt.

### ⚠️ The set-once bootstrap (read this before enabling)

The route that *sets* the token (`POST /settings/api-token`, i.e. **Settings → API access**) is **itself protected by the token**. That creates a deliberate one-time ordering you need to know about:

1. **Set the token once while auth is still disabled** — either paste it in **Settings → API access** on a trusted network, or ship it at deploy time via `LLOPSTER_API_TOKEN` (recommended for production).
2. From that moment it **enforces** — the webhook, trigger, and settings routes all require it.
3. **Every later change to the token requires the current token.** You can't silently rotate it from an unauthenticated session.

**Recovery if you lose the token after setting it:** the env var is the escape hatch. Set or replace `LLOPSTER_API_TOKEN` in the deploy-time Secret and clear the dashboard override — the runtime override (`api_auth_token` setting) only takes precedence *when present*, so a fresh env value wins once the override is blank.

### Both pods must agree on the value

The agent verifies the token; the dashboard mounts the **same** secret (the only secret it carries) so it can (a) enforce auth on its own settings/license routes and (b) forward the bearer on its server-side proxy calls to the agent. In the Helm chart this is `agent.secrets.LLOPSTER_API_TOKEN` → the `llopster-agent` Secret → `secretKeyRef` on **both** the agent and the dashboard (the dashboard's reference is `optional: true`).

### Wiring AlertManager

Once a token is set, AlertManager must send it as a bearer token on the webhook receiver:

```yaml
receivers:
  - name: llopster
    webhook_configs:
      - url: http://llopster-agent.llopster:8000/webhook
        http_config:
          authorization:
            type: Bearer
            credentials: <the same LLOPSTER_API_TOKEN value>
```

(The testbed's AlertManager wiring lives in the `testbed-infra` sibling repo.)

### Related safety defaults

Two more launch-safety behaviors ship on/near these surfaces:

- **PRs open as drafts by default** (`OPEN_PRS_AS_DRAFT=true`) — an LLM-authored diff lands as a draft for human review, not a ready-to-merge PR. Opt out per-deploy or via the `open_prs_as_draft` setting.
- **Automatic cost circuit breaker** (`MAX_RUNS_PER_HOUR` / `MAX_USD_PER_DAY`, **non-zero by default** — 50/hr and $25/day, a safety net you should tune to your budget; `0` disables a ceiling) — when a ceiling is reached the agent trips to **manual** mode (new alerts park as `queued`) and stops spending until an operator intervenes. Operator-initiated runs (manual trigger/dispatch) bypass the breaker so you can still drain the queue by hand. Tunable from **Settings**.
- **Post-firing backoff** (`PATCH_BACKOFF_MINUTES`, default 60) — an alert that finishes the pipeline without opening a PR (below the confidence threshold, or no actionable patch) never matches the open-PR dedup, so without a cap every re-firing would re-run the full Haiku→Sonnet→Opus pipeline. The backoff suppresses those re-firings (one re-investigation per window). Tunable from **Settings**; `0` disables.
- **Independent validation gate** — before a PR is opened, the *patched* content of every touched file is parse/compile-checked (`py_compile` for Python, YAML/JSON parse for config), independent of Claude's self-scored confidence; a file that no longer parses fails the run closed with no PR. LLM-authored diffs are also **path-gated**: they can only touch files the investigation flagged, and never `.github/`/CI/`Dockerfile`/Helm-chart-template paths.
- **Self-observability** — the agent exposes a Prometheus `GET /metrics` endpoint (runs by status, backlog depth, trailing-day spend, cost-breaker state) with an opt-in `ServiceMonitor` (`agent.serviceMonitor.enabled`) so you can alert on the agent itself.

## Tiers & license key

LLopster ships **one image and one Helm chart for every tier** — Community, Business, Enterprise. Paid features live (dormant) in the same open image and switch on at runtime when a signed **license key** grants them; there is no separate "enterprise build." With no key you get the full **Community** tier, free and unrestricted for self-hosting. (This runtime key is distinct from the [FSL source license](#license) below — one governs *features at runtime*, the other governs *what you may legally do with the source*.)

**How it works**

- The license is an **Ed25519-signed JWT** listing the entitled `tier`, `features`, `expiry`, and cluster count. The agent verifies it **locally and offline** against a public key baked into the image — no phone-home, works air-gapped, and never blocks the agent if a licensing server is unreachable.
- **Fail-safe:** a missing, expired, malformed, or wrong-signature key all degrade to Community (which gates nothing). A bad key disables a feature; it never crashes the agent.
- The whole codebase asks one question — `is_feature_enabled("multi_cluster")` — so every paid feature gates through the same check. Premium-pack entitlement routes through it too, as a `pack:<id>` feature.

**Two ways to supply a key** (the dashboard override wins, so you can rotate without a redeploy):

1. **Deploy-time Secret (source of truth):** set `LLOPSTER_LICENSE_KEY` — `agent.secrets.LLOPSTER_LICENSE_KEY` in the Helm values (mounted as a Secret), or the env var for docker/compose.
2. **Paste in the dashboard:** **Settings → License**, paste the key, **Apply**. This writes a `license_key` runtime override that takes precedence over the env var; clear it (blank + Apply) to fall back. The card shows the active tier, features, and expiry — never the raw key.

> Community users never need a key. Today the only consumer of the gate is premium-pack entitlement; the Business/Enterprise feature flags it will gate (multi-cluster, SSO, …) are planned. Operators issue keys with `scripts/sign_license.py`, which requires the private signing key held outside the repo.

## Running tests

```bash
.venv/bin/python -m pytest tests/
```

<!--TEST_COUNT-->471<!--/TEST_COUNT--> tests, all passing. Tests use `httpx.MockTransport` for HTTP clients, `unittest.mock.AsyncMock` for the Anthropic client, and `sqlite+aiosqlite:///:memory:` for the database — no live API calls and no on-disk DB run in the suite. HTML route tests render the templates against an in-memory DB and assert on key substrings + the presence/absence of the HTMX poll trigger. Background-task tests (pruner, pr_poller) run with sub-second intervals so the loop is exercised in <0.5s.

## Related repositories

LLopster is the agent. The cluster it runs in is modeled by a small constellation of sibling repos — a shared observability stack and the example services LLopster monitors. Each monitored service is its own repo, Helm release, namespace, and GitHub PR target (which is the whole point — LLopster opens PRs against the *service's* repo, not its own).

| Repo | Role |
|---|---|
| **[synchrony-solutions/LLopster](https://github.com/synchrony-solutions/LLopster)** | **This repo** — LLopster agent + dashboard + Helm chart. |
| [synchrony-solutions/testbed-infra](https://github.com/synchrony-solutions/testbed-infra) | Shared observability stack (kube-prometheus-stack + Loki + Grafana Alloy) in the `monitoring` namespace. Stands in for a customer's existing monitoring; LLopster connects to it in BYO mode. |
| [synchrony-solutions/llopster-demo](https://github.com/synchrony-solutions/llopster-demo) | Example monitored service #1 — 5 intentional bugs, metrics on `:8001`. PRs from `demo-app` alerts land here. |
| [synchrony-solutions/order-service](https://github.com/synchrony-solutions/order-service) | Example monitored service #2 — 3 intentional bugs, metrics on `:8002`. PRs from `order-service` alerts land here. |

Locally these are checked out as siblings: `~/dev/LLopster`, `~/dev/testbed-infra`, `~/dev/demo-app`, `~/dev/order-service`.

## Contributing

Contributions are welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for the dev setup, coding conventions, and the DCO sign-off requirement. Because the agent holds real credentials and writes to real repos, `main` is protected and every change lands through review:

- **No direct pushes to `main`.** All changes go through a pull request off a branch; history is kept linear (no merge commits, no force-pushes).
- **CI must pass.** The `pytest` suite is a required status check and blocks the merge until green (and your branch must be up to date with `main`).
- **A code-owner review is required.** At least one approving review from the relevant [CODEOWNERS](.github/CODEOWNERS) is mandatory; pushing new commits dismisses stale approvals, and review threads must be resolved before merge.
- **Security issues don't go through public PRs** — follow [SECURITY.md](SECURITY.md) instead.

Releases are cut by tagging `v*`, which is restricted to the release team.

## License

LLopster is **source-available** under the **[Functional Source License v1.1 (FSL-1.1-ALv2)](LICENSE.md)** — the same license Sentry created for exactly this kind of product. In plain English:

- ✅ **You can** read the source, self-host it, modify it, and run it for any internal or production purpose — free, no key required, single or many clusters.
- ✅ **You can** use it for non-commercial education/research, and provide professional services around it to a licensee.
- ❌ **You cannot** make it available to others as a commercial product or service that competes with LLopster (i.e. a hosted/managed "LLopster-as-a-service" or a substantially similar substitute). That commercial right is reserved to us.
- ⏳ **It converts to open source automatically:** each released version becomes available under the permissive **Apache License 2.0** on the **second anniversary** of its release. So the restriction is time-boxed — yesterday's LLopster is tomorrow's true open source.

This is *source-available*, not OSI "open source," and that's deliberate: it keeps the code transparent and self-hostable (the trust property our buyers need) while reserving the right to commercialize the product to us during the window that matters. The **LLopster** name and logo are trademarks and are not licensed for others' commercial use.

> Not legal advice — see [LICENSE.md](LICENSE.md) for the binding terms.

## Where to read next

- [docs/PRODUCTION.md](docs/PRODUCTION.md) — the Helm/BYO production deployment guide.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to set up a dev environment, run the tests, and open a pull request.
- [SECURITY.md](SECURITY.md) — the security model, operator hardening checklist, and how to report a vulnerability.
