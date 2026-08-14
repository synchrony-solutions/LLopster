# Changelog

All notable changes to LLopster are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Chart version and `appVersion` are released in lockstep: chart `X.Y.Z` always
ships image tag `X.Y.Z`. The release workflow refuses to publish if the pushed
`vX.Y.Z` tag and `helm-chart/Chart.yaml` disagree.

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
