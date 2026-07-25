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

{{- define "k3s-deployment.labels" -}}
app.kubernetes.io/name: {{ include "k3s-deployment.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}
