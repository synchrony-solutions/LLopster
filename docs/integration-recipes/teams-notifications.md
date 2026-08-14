# Recipe: Microsoft Teams notifications

How to send LLopster's patch-proposal notifications to a **Microsoft Teams**
channel instead of Slack (or turn notifications off entirely).

LLopster posts an **Adaptive Card** — root cause, the diff, confidence, and a
"View Pull Request" button — to a Teams **Power Automate "Workflows"** incoming
webhook. This is Microsoft's supported path; the classic Office 365
"Incoming Webhook" *connector* is being retired.

## Choosing the channel

Set `NOTIFIER_PROVIDER` (env) / `agent.notifications.provider` (Helm):

| Value | Behavior | Webhook var |
|-------|----------|-------------|
| `slack` (default) | Slack Block Kit message | `SLACK_WEBHOOK_URL` |
| `teams` | Teams Adaptive Card | `TEAMS_WEBHOOK_URL` |
| `none` | Notifications disabled (pipeline still opens PRs) | — |

Back-compat: the default is `slack`, so existing installs are unchanged.

## Step 1 — Create the Teams Workflow (get the webhook URL)

In Teams, create a workflow from the built-in template that posts to a channel
when it receives a webhook request:

1. In Teams, open **Workflows** (⋯ *More apps* → *Workflows*), or go to the
   target channel → **⋯** → **Workflows**.
2. Choose the template **"Post to a channel when a webhook request is
   received"** (also listed under *Notifications*).
3. Pick the **Team** and **Channel** the notifications should land in, and
   **Create / Add workflow**.
4. Teams generates an **HTTP POST URL** — copy it. This is your
   `TEAMS_WEBHOOK_URL`. It looks like:
   `https://prod-NN.<region>.logic.azure.com:443/workflows/<id>/triggers/manual/paths/invoke?...&sig=...`

> The URL embeds a signature (`sig=`) — treat it as a secret. In the Helm
> chart it's stored in the `llopster-agent` Kubernetes Secret, not in plain
> values output.

The workflow template already knows how to render the Adaptive Card LLopster
sends — no card mapping to configure. LLopster posts the
`{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content": …}]}`
envelope the Workflows trigger expects.

## Step 2 — Configure LLopster

### Local / docker-compose (`.env`)

```env
NOTIFIER_PROVIDER=teams
TEAMS_WEBHOOK_URL=https://prod-1.westus.logic.azure.com/workflows/...
```

### Helm

```bash
helm upgrade --install llopster oci://ghcr.io/synchrony-solutions/charts/llopster \
  --version 1.0.0 --namespace llopster --create-namespace \
  # ...your prometheus/loki/anthropic values... \
  --set agent.notifications.provider=teams \
  --set agent.secrets.TEAMS_WEBHOOK_URL='https://prod-1.westus.logic.azure.com/workflows/...'
```

Or in a values file:

```yaml
agent:
  notifications:
    provider: teams
  secrets:
    TEAMS_WEBHOOK_URL: "https://prod-1.westus.logic.azure.com/workflows/..."
```

The chart **requires** `TEAMS_WEBHOOK_URL` when `provider=teams` — it fails to
render without it, so a missing URL is caught at `helm install` rather than
silently dropping notifications.

## Step 3 — Verify

- **Dashboard:** Settings → Notifications shows *Notifications · Microsoft
  Teams* and the webhook host. Click **Test** — a "llopster connection test ✓"
  card should appear in the channel and the button flips to green.
- **End to end:** fire a test alert; when a run reaches `done` with a proposed
  patch, the Adaptive Card (root cause + diff + "View Pull Request") posts to
  the channel.

## Turning notifications off

For a team with no chat-based alerting, set the provider to `none`:

```bash
--set agent.notifications.provider=none
```

The pipeline still runs, opens PRs, and records every run in the dashboard — it
just doesn't post a message. (Leaving all webhook URLs empty has the same
effect.)

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Chart refuses to render, mentions `TEAMS_WEBHOOK_URL` | `provider=teams` without `agent.secrets.TEAMS_WEBHOOK_URL` set |
| Test button returns an HTTP 4xx | Wrong/expired workflow URL, or the workflow was deleted/disabled in Teams |
| Test succeeds but no card appears | The workflow posts to a different channel than expected — re-check the channel chosen in Step 1 |
| Notifications silently absent, no error | `NOTIFIER_PROVIDER` isn't `teams` (a Teams URL alone doesn't switch the provider) — set the provider explicitly |
