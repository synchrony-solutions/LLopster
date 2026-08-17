{{/*
Common helpers for the k3s-deployment parent chart.
*/}}

{{- define "k3s-deployment.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k3s-deployment.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "k3s-deployment.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
ServiceAccount name for the agent. Defaults to "llopster-agent" (matching
the Secret/ConfigMap naming) but can be overridden, or pointed at an
externally-managed SA by setting agent.serviceAccount.create=false and a
name. Used to attach an IRSA / pod-identity role for the Bedrock provider.
*/}}
{{- define "llopster.agent.serviceAccountName" -}}
{{- default "llopster-agent" .Values.agent.serviceAccount.name -}}
{{- end -}}

{{/*
Name of the Secret holding the agent's credentials.

Defaults to the chart-managed "llopster-agent". When agent.existingSecret is
set the chart renders no Secret at all and every consumer points here instead
— the path for clusters where credentials are delivered by an external secret
manager (External Secrets Operator, Vault Agent, Secrets Store CSI) and
plaintext must never enter a values file committed to a GitOps repo.
*/}}
{{- define "llopster.agent.secretName" -}}
{{- default "llopster-agent" .Values.agent.existingSecret -}}
{{- end -}}

{{/*
A secretKeyRef into the agent's credentials Secret.

Usage: {{ include "llopster.agent.secretKeyRef" (dict "ctx" $ "key" "GITHUB_TOKEN") }}

Keys are marked `optional: true` ONLY when the Secret is externally managed.
The chart-rendered Secret always contains every key, so a missing one there
is a chart bug worth failing on; an external Secret legitimately carries only
the subset the operator uses (a Teams shop has no SLACK_WEBHOOK_URL, an IRSA
cluster has no AWS_* keys). Optional matches the app's own contract — main.py
treats an absent credential as "feature disabled with a warning", never a
crash — so the pod starts and reports the gap instead of CreateContainerConfigError.
*/}}
{{- define "llopster.agent.secretKeyRef" -}}
{{- $ctx := .ctx -}}
secretKeyRef:
  name: {{ include "llopster.agent.secretName" $ctx }}
  key: {{ .key }}
  {{- if $ctx.Values.agent.existingSecret }}
  optional: true
  {{- end }}
{{- end -}}

{{/*
Name of the Secret holding the bundled PostgreSQL credentials (and the
composed DATABASE_URL the agent + dashboard consume). Defaults to the
chart-managed "llopster-postgres-credentials"; postgresql.auth.existingSecret
points it at an externally-managed Secret, which must then carry all four
keys: POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, DATABASE_URL.
*/}}
{{- define "llopster.postgres.secretName" -}}
{{- default "llopster-postgres-credentials" (dig "auth" "existingSecret" "" .Values.postgresql) -}}
{{- end -}}

{{/*
The DATABASE_URL env entry for the agent and the dashboard — always a
secretKeyRef, never a literal.

  postgresql.enabled=true  → the chart-composed URL in the postgres Secret.
  postgresql.enabled=false → externalDatabase.existingSecret (RDS, Cloud SQL,
                             a managed instance delivered by External Secrets).

Emits nothing when neither applies, which is the agent.env.DATABASE_URL
passthrough case — the unconditional gate at the top of llopster-agent.yaml
has already established that one of the three is configured, so this cannot
silently leave the app on its SQLite fallback.
*/}}
{{- define "llopster.databaseUrlEnv" -}}
{{- $ext := .Values.externalDatabase | default dict -}}
{{- if .Values.postgresql.enabled -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "llopster.postgres.secretName" . }}
      key: DATABASE_URL
{{- else if dig "existingSecret" "" $ext -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $ext.existingSecret }}
      key: {{ dig "secretKey" "DATABASE_URL" $ext }}
{{- end -}}
{{- end -}}

{{/*
Clone URL for one agent.codebases entry.

Accepts either an explicit `gitRepo` (full URL — always wins, so existing
values files are untouched) or a `repo: org/name` shorthand, which derives
https://<agent.git.host>/<org>/<name>.git. The shorthand exists so a GitHub
Enterprise Server install names its host ONCE in agent.git.host instead of
repeating it in every codebase entry, where it could drift from the host the
credential rewrite and the API base are built from.

Usage (inside `range .Values.agent.codebases`):
  {{ include "llopster.codebase.url" (dict "ctx" $ "cb" .) }}
*/}}
{{- define "llopster.codebase.url" -}}
{{- $cb := .cb -}}
{{- if $cb.gitRepo -}}
{{- $cb.gitRepo -}}
{{- else if $cb.repo -}}
{{- printf "https://%s/%s.git" (include "llopster.github.host" .ctx) (trimSuffix ".git" $cb.repo) -}}
{{- else -}}
{{- fail (printf "agent.codebases entry %q must set either `repo: org/name` (derived from agent.git.host) or an explicit `gitRepo:` URL" (default "<unnamed>" $cb.name)) -}}
{{- end -}}
{{- end -}}

{{/*
GitHub REST API root for the agent (GITHUB_API_BASE).

Resolution order:
  1. agent.git.apiBase, when set explicitly.
  2. https://api.github.com, when the host is (or defaults to) github.com.
  3. https://<host>/api/v3 — the GitHub Enterprise Server convention.

Derived rather than left to the agent.env passthrough so the API root and
the git host the clone init container rewrites can never drift apart. Same
pattern as PROMETHEUS_URL / LOKI_URL.
*/}}
{{- define "llopster.github.apiBase" -}}
{{- $host := include "llopster.github.host" . -}}
{{- $apiBase := dig "git" "apiBase" "" .Values.agent -}}
{{- if $apiBase -}}
{{- trimSuffix "/" $apiBase -}}
{{- else if eq $host "github.com" -}}
https://api.github.com
{{- else -}}
{{- printf "https://%s/api/v3" $host -}}
{{- end -}}
{{- end -}}

{{/*
Git host the codebase-clone init container rewrites credentials for.
Defaults to github.com.
*/}}
{{- define "llopster.github.host" -}}
{{- default "github.com" (dig "git" "host" "github.com" .Values.agent) -}}
{{- end -}}

{{- define "k3s-deployment.labels" -}}
app.kubernetes.io/name: {{ include "k3s-deployment.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}
