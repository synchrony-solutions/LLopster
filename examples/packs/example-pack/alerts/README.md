# `alerts/` — operator-applied PrometheusRule YAML

A real premium pack ships curated `PrometheusRule` CRDs here, paired with the
tuned prompts in `../prompts/`. The LLopster engine does **not** read this
directory — alert rules are content distributed *with* the pack but applied by
the operator (`kubectl apply` / `helm`) to the cluster's Prometheus, exactly
like the in-repo starter pack at `helm-chart/templates/prometheus-rules/`.

Premium alert rules must use valid Prometheus identifier annotation keys
(`llopster_likely_files`, `llopster_runbook`) — never dotted/slashed keys, or
the operator's validating webhook rejects them.

This example pack ships no alert rules (no premium content in the open repo).
