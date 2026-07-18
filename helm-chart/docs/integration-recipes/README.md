# Integration recipes

Snippets for connecting LLopster to a cluster's existing observability
stack — the **BYO** install path (`prometheus.bundled=false` /
`loki.bundled=false`).

## What LLopster needs from your stack

LLopster connects out to two URLs and receives one webhook in:

| Direction | Endpoint | Configured via |
|-----------|----------|----------------|
| Out → Prometheus query API | for metrics referenced by an alert's `generatorURL` | `prometheus.url` in `values.yaml` |
| Out → Loki query API | for logs around the alert timestamp | `loki.url` in `values.yaml` |
| In ← AlertManager webhook | one HTTP POST per alert group | the receiver snippet below |

LLopster does **not** need access to: kube-apiserver, etcd, your apps'
deployment manifests, or kube-state-metrics directly (it reads
kube-state-metrics through Prometheus). It is also **collector-agnostic** for
logs — it reads Loki's query API, so whatever writes to Loki (Grafana Alloy,
Promtail, Vector, Fluent Bit) works unchanged.

## Logs: the label-matching contract

LLopster scopes its LogQL to the alerting workload by probing a list of Loki
**stream labels** in order (config `LOG_SCOPE_LABELS`) and using the first that
is also present on the alert:

```
service, app, app_kubernetes_io_name, app_kubernetes_io_instance,
container, pod, namespace, job
```

The only requirement is that the labels your log collector attaches to streams
**overlap** with the labels your Prometheus alerts carry. If your collector
uses a different scheme, either relabel it to emit one of the above, or set
`agent.env.LOG_SCOPE_LABELS` in `values.yaml` to match. If LLopster reports
"no usable label found to build LogQL selector" or finds zero log lines, this
is almost always the mismatch to fix.

## Files

### `alertmanager-receiver.yaml`

A receiver + route block to add to your existing AlertManager
configuration. Forwards alerts to LLopster's webhook over in-cluster DNS.

For kube-prometheus-stack installs, paste it into your `values.yaml`
under `alertmanager.config:`.

### `alloy-logs.yaml`

A `grafana/alloy` chart values snippet that ships pod logs to Loki with the
stream labels LLopster probes (`namespace` / `pod` / `container` / `app` /
`app_kubernetes_io_name`). Install it alongside your Loki, then point both
Alloy's `loki.write` and LLopster's `loki.url` at the same Loki. This is the
recommended modern log path — Grafana has deprecated Promtail in favour of
Alloy. Existing Promtail installs keep working unchanged (same Loki, same
labels).

### `prometheus-rules-snippet.yaml`

LLopster's starter-pack alerts in raw Prometheus YAML form. Use this when
your cluster runs plain Prometheus (no operator) — paste into your
`rule_files:` target or `serverFiles.alerting_rules.yml`.

If your cluster runs **kube-prometheus-stack** (or any other Prometheus
Operator install), prefer setting `alertRules.enabled=true` in LLopster's
`values.yaml` — the chart ships the same content as a PrometheusRule CRD
the operator picks up automatically.

## How to flag alerts as "for LLopster"

Both snippets above use a `llopster_managed="true"` label to mark alerts
LLopster should act on. The AlertManager route matches on this label so
other receivers stay untouched.

If you want LLopster to handle a *subset* of your existing alerts, add
the `llopster_managed: "true"` label to those rules' `labels:` block —
or drop the matcher from the receiver if you want LLopster to receive
everything.
