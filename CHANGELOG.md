# Changelog

All notable changes to LLopster are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Chart version and `appVersion` are released in lockstep: chart `X.Y.Z` always
ships image tag `X.Y.Z`. The release workflow refuses to publish if the pushed
`vX.Y.Z` tag and `helm-chart/Chart.yaml` disagree.

## [1.1.0] - 2026-08-14

Dependency-maintenance release: clears the open Dependabot backlog (nine PRs —
Python packages, pinned GitHub Actions, and the container base image). No
feature or API changes; no security advisories were outstanding against the
tree.

### Changed

- **Runtime moved to Python 3.14** (`python:3.12-slim` → `python:3.14-slim`).
  CI's `python-version` moved in lockstep so the tested interpreter matches the
  shipped one. Every compiled dependency resolves a cp314 wheel — the image
  builds with no source compiles.
- **Anthropic SDK 0.69.0 → 0.121.0.** The `AsyncAnthropic` /
  `AsyncAnthropicBedrock` seam in [llm_provider.py](src/agent/llm_provider.py)
  is unchanged; both clients still construct and Bedrock still forces the
  `extended-cache-ttl` beta off.
- **Web stack**: FastAPI 0.115.0 → 0.141.1 (pulls Starlette 1.6), uvicorn
  0.30.6 → 0.52.1, python-multipart 0.0.12 → 0.0.32, jinja2 3.1.4 → 3.1.6.
- **Data layer**: SQLAlchemy 2.0.36 → 2.0.51, alembic >=1.14.0 → >=1.19.1,
  asyncpg 0.29.0 → 0.31.0, aiosqlite 0.20.0 → 0.22.1, psycopg2-binary 2.9.9 →
  2.9.12.
- **Crypto / licensing**: cryptography 43.0.1 → 50.0.0, PyJWT 2.9.0 → 2.13.0.
- **Dashboard rendering**: markdown-it-py 3.0.0 → 4.2.0, pygments 2.18.0 →
  2.20.0.
- **Other**: httpx 0.27.2 → 0.28.1, boto3/botocore 1.43.56 → 1.43.67, pyyaml
  6.0.2 → 6.0.3, python-dotenv 1.0.1 → 1.2.2. The `demo-app/` fixture's own
  pins were refreshed alongside.
- **Test tooling**: pytest-asyncio 0.24.0 → 1.4.0, which requires
  `pytest>=8.4` — pytest moved 8.3.3 → 9.1.1 with it. (Dependabot proposed
  these in two separate PRs that conflicted with each other; they only resolve
  as a pair.)
- **Pinned GitHub Actions**: `actions/checkout` v4.3.1 → v7.0.1,
  `actions/setup-python` v5.6.0 → v7.0.0, `azure/setup-helm` v4.3.1 → v5.0.1
  (SHA pins updated with the version comments).

### Fixed

- **Route-auth coverage guard no longer passes vacuously.** FastAPI ≥0.140 stops
  flattening `include_router()` routes into `app.routes`, wrapping them in an
  `_IncludedRouter` whose effective routes carry the include-time
  `dependencies=[...]`. `tests/test_route_auth_coverage.py` walked only
  top-level `APIRoute`s, so after the bump it enumerated *zero* routes — two
  assertions failed outright and, worse, the "every write route is guarded"
  check passed against an empty set. Enumeration now descends into included
  routers, and a new non-vacuity test anchors it to known routes so a future
  internals change fails loudly instead of silently. Runtime enforcement was
  never affected — the dashboard read/write surfaces were verified to still
  return 401 unauthenticated and 200 with a valid bearer.

## [1.0.0] - 2026-08-13

First public release. LLopster is source-available under the Functional Source
License (FSL-1.1-ALv2); the Community tier self-hosts with no license key.

### Added

- **Tiered LLM pipeline.** AlertManager fires → Haiku triage → context
  collection (Loki logs + Prometheus metrics) → Sonnet investigation narrows to
  likely files → Opus synthesis emits a unified-diff patch. Each stage is
  independently kill-switchable from the settings table and fails safe.
- **Pluggable LLM providers.** `LLM_PROVIDER` selects the Anthropic API or
  Amazon Bedrock; per-stage model overrides for triage, investigation, and
  synthesis. See
  [docs/integration-recipes/bedrock-irsa.md](docs/integration-recipes/bedrock-irsa.md).
- **Pluggable notifiers.** `NOTIFIER_PROVIDER` selects Slack or Microsoft
  Teams. See
  [docs/integration-recipes/teams-notifications.md](docs/integration-recipes/teams-notifications.md).
- **Guarded pull requests.** Patches are applied in memory and gated three ways
  before any branch, commit, or PR exists: hard-denied paths (`.github/`, CI
  configs, `Dockerfile`, chart templates), an `affected_files` allowlist, and an
  independent validation pass that `py_compile`s Python and parses YAML/JSON of
  the *patched* content. Any failure aborts with no side effects. PRs open as
  drafts by default.
- **Cost controls.** A circuit breaker trips on runs-per-hour or estimated
  spend-per-day (defaults 50/hr, $25/day) before any LLM call and flips the
  agent to manual mode. A post-firing backoff suppresses re-firings whose last
  run produced no PR.
- **Dashboard.** Run list and detail views with live HTMX polling, operator
  correct/wrong/partial labelling, settings, and diagnostics. No JS framework,
  no build step.
- **Inbound authentication.** A shared secret guards every surface that spends
  money, holds the GitHub PAT, or exposes raw production logs and LLM output.
  Disabled by default with a loud startup warning so local evaluation works;
  see the set-once bootstrap in [SECURITY.md](SECURITY.md).
- **Helm chart**, published to `oci://ghcr.io/synchrony-solutions/charts`.
  Bring-your-own Prometheus and Loki by default, with optional bundled
  subcharts for evaluation. A secure-render gate refuses to install an exposed
  agent or dashboard (Ingress, or a `LoadBalancer`/`NodePort` Service) without
  an API token, unless `agent.allowUnauthenticated=true` is set explicitly.
- **Self-observability.** A DB-backed `/metrics` endpoint (runs by status,
  backlog depth, trailing-day spend, breaker state) computed at scrape time so
  it survives restarts, with an opt-in `ServiceMonitor`.
- **Offline license framework.** Ed25519-signed JWT verified locally against an
  embedded public key. Missing, expired, and malformed keys all degrade to the
  Community tier — never a crash.
- **Schema migrations** via Alembic, with three-way detection on startup that
  upgrades pre-Alembic volumes in place.

### Notes for operators

- Container images are published for `linux/amd64` and `linux/arm64`.
- Minimum Kubernetes version is 1.19 (`networking.k8s.io/v1` Ingress).
- The chart does **not** deploy the services LLopster monitors. Each monitored
  service is its own repo, chart, release, and PR target — see
  [docs/PRODUCTION.md](docs/PRODUCTION.md).

[1.0.0]: https://github.com/synchrony-solutions/LLopster/releases/tag/v1.0.0
