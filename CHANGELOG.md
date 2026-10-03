# Changelog

All notable changes to LLopster are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Chart version and `appVersion` are released in lockstep: chart `X.Y.Z` always
ships image tag `X.Y.Z`. The release workflow refuses to publish if the pushed
`vX.Y.Z` tag and `helm-chart/Chart.yaml` disagree.

## [Unreleased]

### Fixed

- **Eval replays no longer show the model the scenario's name.** Both LLM
  prompts print the codebase root, and a scenario that ships its own codebase
  was replayed in place — `eval/scenarios/<scenario-id>/codebase`, where the
  id describes the answer (`crashloop-oomkilled-no-logs`). A control run cited
  the path as its evidence. Replays now copy the codebase to
  `<tmp>/<service-name>`, matching what production mounts at
  `/codebases/<name>`. Results recorded before this fix for
  `crashloop-oomkilled-no-logs`, `oci-chart-undeliverable-patch` and
  `invisible-chart-layer-override` were measured with the leak present.

## [1.3.0] - 2026-10-03

Cluster-context release. The agent can now read the live Kubernetes objects an
alert is about, closing the class of failures whose answer exists in no log
line — a container that dies during startup has an exit code and nothing else.
Alongside it, two operator declarations stop the worst failure a PR-opening
agent can have: a correct, merged patch that never reaches the cluster.

Every default is unchanged — cluster access is off, and both declarations are
optional. A 1.2.0 install upgrades with no values edits; see *Notes for
operators* for the one gate that now refuses more than it did.

### Added

- **Read-only Kubernetes cluster-state access** (`agent.clusterContext`, issue
  #23). The agent can now read the objects a firing alert is actually about —
  container states, `lastState` exit codes and `OOMKilled` reasons, restart
  counts, configured requests/limits, the pod's owner chain, recent Events,
  PVC binding failures and node conditions — and feed them to the LLM as a
  `## Cluster state` block beside Loki logs and Prometheus metrics. This closes
  the class of alerts whose answer exists nowhere else: a container that dies
  during startup writes no logs at all, and the exit code is the only signal.
  - **Off by default and independently revocable.** `enabled: false` renders no
    RBAC and constructs no client.
  - `GET` only, no Kubernetes SDK dependency — a thin `httpx` client in the
    same shape as the Loki and Prometheus clients.
  - Scope with `namespaces` (one RoleBinding each) or `allNamespaces` (a
    ClusterRoleBinding). Enabling with neither fails the render rather than
    silently granting nothing.
  - The generated ClusterRole carries `get`/`list`/`watch` and never `secrets`,
    enforced in chart CI by `scripts/check_cluster_rbac.py`.
  - Env-var values, container `command`/`args`, probe and lifecycle
    `httpHeaders` values, `data`/`stringData` maps and the
    `last-applied-configuration` annotation are redacted before anything
    reaches the LLM, the run record or the dashboard.
  - Object names from alert labels are validated as Kubernetes names before
    they reach a request path, so a crafted label cannot step outside the
    permitted namespace (`../`) or inject query parameters.
  - Scrape label collisions are resolved: `exported_namespace` /
    `exported_pod` name the alerted object and win over the target's own
    labels. The Prometheus `job` label is not mistaken for a batch Job.
  - Recorded on the run (`cluster_state_json`, migration `0009`) and rendered
    on the run detail page.
- **Delivery-mode declaration** (`delivery` in `services.yaml`, issue #24,
  #27). `mode: git-manifest | oci-chart | image-build` tells the agent whether
  a merged source patch actually reconciles. Under the two indirect modes a
  source-only patch passes every gate and changes nothing in the cluster; with
  a same-repo `version_ref` the synthesis prompt now requires the version bump
  in the same diff, and with a cross-repo one it asks for a low-confidence
  explanation of both changes instead of a dead PR.
- **Chart-lineage declaration** (`chart_lineage` in `services.yaml`, issue #25,
  #27). Names the chart layers a service is delivered through and marks which
  are not in the codebase the agent can see, so a key overridden by an
  invisible parent chart is reported as such rather than patched in the copy
  that loses.
- Eval scenarios `oci-chart-undeliverable-patch` and
  `invisible-chart-layer-override` for the two declarations.
- Eval scenario `crashloop-oomkilled-no-logs`, and `recorded_context.cluster_state`
  support in the eval corpus, so the benefit can be measured offline rather
  than asserted.

### Fixed

- **Chart templates are detected structurally, not by directory name** (issue
  #26, #28). The protected-path gate matched the literal names `helm-chart` and
  `charts`, so chart templates under any other layout
  (`<tool>/helm/templates/`, `deploy/templates/`) were **not** protected, while
  every file under any directory named `charts` — app values, docs, ordinary
  Python source — was refused. A chart root is now any directory holding a
  `Chart.yaml`; its `templates/**` is protected and its `values*.yaml` and
  `environments/**` are patchable.
- **Eval `recorded_context` fidelity is enforced** (#33). A scenario carried a
  metric sample and a log line no real run could have collected, which gave the
  answer away and made the chart-lineage scenario pass with or without the
  declaration under test. Every recorded metric must now come from the alert's
  own expression, checked by a corpus-wide test.
- The eval harness no longer suppresses its own replays. `replay_scenario` now
  passes `enforce_backoff=False`: the post-firing backoff guards against
  re-*firings* costing tokens, but a corpus replay is not a re-firing. Left on,
  a second run of the corpus inside `patch_backoff_minutes` (default 60)
  skipped every scenario and reported a 0% pass-rate — indistinguishable from a
  catastrophic regression.
- `root_cause_keywords` are now graded on `expect_patch: true` scenarios, where
  they were previously accepted and silently ignored. A scenario whose
  competing hypotheses live in the same file cannot be graded by file targeting
  alone. Scenarios that declare no keywords are unaffected.
- The diagnosis check reads only the `## Root Cause` section, not the whole
  response. Scanning the full body conflated "concluded X" with "mentioned X
  while ruling it out" — an observed control run diagnosed a liveness-probe
  failure and then advised checking `kubectl describe pod` for `OOMKilled`,
  which scored as a match.

### Changed

- The agent pod now sets `automountServiceAccountToken` explicitly, and it is
  **false** unless `agent.clusterContext.enabled=true`. Kubernetes mounts an
  API-server credential by default; a pod with no cluster access has no reason
  to hold one. Bedrock IRSA is unaffected — the EKS webhook injects its own
  projected token independently of automount.
- Both LLM confidence scales now reference "the collected evidence" rather than
  "logs/metrics" alone, so a diagnosis grounded in cluster state is not scored
  down for the absence of logs the container never wrote.
- **Dependencies**: uvicorn 0.52.1 → 0.53.0, python-dotenv 1.2.2 → 1.2.3,
  boto3/botocore 1.43.67 → 1.43.97, PyJWT 2.13.0 → 2.14.0, cryptography
  50.0.0 → 50.0.1, SQLAlchemy 2.0.51 → 2.0.54, psycopg2-binary 2.9.12 →
  2.9.13, pygments 2.20.0 → 2.21.0.
- **Pinned GitHub Actions**: `helm/kind-action` v1.14.0 → v1.15.0,
  `docker/setup-qemu-action` v4.2.0 → v4.4.0, `docker/setup-buildx-action`
  v4.2.0 → v4.4.1 (SHA pins verified against the release tags).

### Notes for operators

- **Upgrading from 1.2.0 needs no values changes.** `agent.clusterContext` is
  off by default and renders no RBAC; `delivery` and `chart_lineage` are
  optional and absent blocks behave exactly as before.
- **The protected-path gate now refuses patches to chart templates in any
  layout.** If an agent PR previously landed on `<tool>/helm/templates/…`
  because the directory was not named `helm-chart`, the same diagnosis now
  fails closed with no PR. That is the documented intent; the old behavior was
  the bug. In the other direction, `charts/<app>/values.yaml` and source under
  a `charts/` directory are now patchable.
- **The agent pod no longer mounts a ServiceAccount token by default.**
  Nothing in LLopster used it, and Bedrock IRSA injects its own token
  independently. Enabling `agent.clusterContext` turns the mount back on.
- A malformed `delivery` block is dropped with a warning that says out loud no
  constraint is being applied — check agent startup logs after adding one.

## [1.2.0] - 2026-08-17

Enterprise-deployment release. Closes the four gaps that stood between
`helm install` and forking the chart on a locked-down cluster: source on
**GitHub Enterprise Server**, credentials delivered by an **external secret
manager**, images pulled only from a **private registry**, and a **managed
database** instead of the bundled Postgres.

Every default is unchanged — a github.com install with bundled Postgres
upgrades with no values edits. See the new
[Enterprise deployment](docs/PRODUCTION.md#enterprise-deployment) section.

### Added

- **GitHub Enterprise Server support.** `GITHUB_API_BASE` (default
  `https://api.github.com`) now backs every GitHub REST call; in the chart it
  is *derived* from a new `agent.git` block (`host`, `apiBase`) by the
  `llopster.github.apiBase` helper — the same pattern as `PROMETHEUS_URL` — so
  the API root can never drift from the host the clone init container rewrites
  credentials for. Setting `agent.git.host` alone resolves the GHES convention
  `https://<host>/api/v3`.
- **`agent.existingSecret`** — point the chart at a Secret created by External
  Secrets Operator, Vault Agent, or the Secrets Store CSI driver and it renders
  no Secret of its own. Required for GitOps, where the values file is committed
  and cannot hold credentials, and it avoids a Helm-vs-controller ownership
  fight over the same Secret name. Mirrored by
  `postgresql.auth.existingSecret`.
- **`externalDatabase.existingSecret` / `.secretKey`** — a real external-database
  path (RDS, Cloud SQL) for `postgresql.enabled=false`.
- **Pod extensibility hooks**: `agent.extraEnv`, `agent.extraVolumes`, and
  `agent.extraVolumeMounts` (mounted into the agent *and* the clone init
  container), plus `agent.codebaseClone.extraEnv`. Together these mount a
  corporate CA bundle for a GHES instance behind a private CA — `SSL_CERT_FILE`
  for the agent's httpx client, `GIT_SSL_CAINFO` for git.
- **`agent.codebases[].repo`** — an `org/name` shorthand resolved against
  `agent.git.host`, so a GHES install names its host once instead of repeating
  it in every entry. An explicit `gitRepo:` URL still wins, for a repo on a
  different host.
- **Configurable init-container images**: `agent.codebaseClone.image` and
  `postgresql.initImage`, completing image parameterization across the chart.

### Changed

- **`DATABASE_URL` is now always injected by `secretKeyRef`, never as a literal.**
  It was interpolated into the agent and dashboard PodSpecs, which exposed the
  database password to `kubectl describe pod`, `helm get values`, and any
  rendered manifest a GitOps controller stores. The chart composes it into the
  Postgres Secret when it owns the database.
- **The clone init container no longer gates auth on a token prefix.** It
  required `ghp_`/`github_pat_`/`gho_`/`ghs_`/`ghu_`; GHES prefixes are
  configurable per instance and older GHES PATs are bare 40-char hex, so a valid
  enterprise token silently fell through to an anonymous clone and failed
  against a private repo with a confusing error. Only an empty or `CHANGEME`
  token now disables auth; the prefix check survives as a log hint.
- **`alpine/git` is pinned to `v2.54.0`** (was `:latest`). Identical digest to
  what `:latest` resolved to at release time, so nothing about the pulled image
  changed — it is simply reproducible now, and survives a digest-pinning policy.
- **The Postgres StatefulSet now honors `imagePullSecret`.** It was the only
  workload in the chart without it, so a private-mirror cluster could not pull
  its images.
- **Postgres probes read `$POSTGRES_USER` / `$POSTGRES_DB` from the container
  env** (via `sh -c`, since exec probes do not substitute) instead of templated
  values, which are unavailable under `postgresql.auth.existingSecret`.
- **The secure-render gate accepts `agent.existingSecret`.** It reads
  `agent.secrets.LLOPSTER_API_TOKEN`, which is empty by design under an external
  secret manager, so every exposed GitOps install would otherwise have refused
  to render. The agent's runtime startup warning remains the check that can
  actually see the value.
- `_classify_github_token` labels bare 40-char hex as `legacy-pat` rather than
  `unknown`, so the Settings page stops implying a misconfiguration on GHES.
  Display only — nothing gates on it.

### Fixed

- **The documented external-database escape hatch now works.** `values.yaml` said
  that with `postgresql.enabled=false` you must point `DATABASE_URL` at an
  external DB, but both `DATABASE_URL` blocks were wrapped in
  `{{- if .Values.postgresql.enabled }}`. The variable was never set, the app
  fell back to SQLite under `/app/data`, and that write fails against the
  chart's own `readOnlyRootFilesystem` — so the documented configuration could
  only ever crash-loop. The chart now refuses to render instead, **per
  component**: the agent and dashboard are separate pods with separate
  connections, so an `agent.env.DATABASE_URL` passthrough alone does not
  satisfy it.
- **Static AWS credentials in an externally-managed Secret are no longer
  dropped.** The `AWS_*` env vars were gated on `agent.secrets.AWS_ACCESS_KEY_ID`
  being non-empty, which is never true under `agent.existingSecret` — the
  variables were silently omitted with no error.

### Notes for operators

- **Upgrading from 1.1.0 needs no values changes.** `agent.git.host` defaults to
  `github.com`, `existingSecret` fields default empty, and the bundled Postgres
  path is unchanged apart from `DATABASE_URL` moving into the Secret the chart
  already owned.
- **If you run `postgresql.enabled=false` today, the chart will now refuse to
  render** until you set `externalDatabase.existingSecret` (or a
  `DATABASE_URL` env passthrough for *both* the agent and the dashboard). This
  is deliberate: that configuration could not have been working — it fell back
  to a SQLite write that the read-only root filesystem rejects.
- Keys expected in an `agent.existingSecret` are all optional; an absent key
  disables its feature with a startup warning, exactly as an unset env var does.
  Because `checksum/secrets` is computed from chart values, it cannot detect a
  rotation *inside* an externally-managed Secret — pair with a reloader if you
  rotate in place.
- Mirroring the chart into a private registry does **not** require
  `helm dependency build`: the published OCI artifact already vendors the
  optional observability subcharts (~270KB), so `helm pull` → `helm push` is
  sufficient.

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

[1.3.0]: https://github.com/synchrony-solutions/LLopster/releases/tag/v1.3.0
[1.2.0]: https://github.com/synchrony-solutions/LLopster/releases/tag/v1.2.0
[1.1.0]: https://github.com/synchrony-solutions/LLopster/releases/tag/v1.1.0
[1.0.0]: https://github.com/synchrony-solutions/LLopster/releases/tag/v1.0.0
