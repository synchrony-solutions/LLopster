# Recipe: Claude via AWS Bedrock on EKS (IRSA)

How to run LLopster with `LLM_PROVIDER=bedrock` on an EKS cluster, granting
Bedrock access through **IRSA (IAM Roles for Service Accounts)** — the agent
pod assumes an IAM role via a projected OIDC token, so **no AWS keys are
stored in the cluster**.

This recipe assumes the common case: **the EKS cluster and Bedrock are in the
same AWS account**. For the provider overview and the plain-Helm summary, see
[docs/PRODUCTION.md → AWS Bedrock provider](../PRODUCTION.md#aws-bedrock-provider).

> **Where the AWS credentials live:** with IRSA, nowhere in Kubernetes. The
> only thing that crosses into the cluster is the **role ARN**, which is not a
> secret — it goes in a plain Helm value / ServiceAccount annotation, not a
> `Secret`. Static AWS keys are only needed on clusters that can't do IRSA or
> Pod Identity — see [Fallback: static AWS keys](#fallback-static-aws-keys).

## Architecture

```
EKS pod  (ServiceAccount: llopster/llopster-agent)
   │  projected OIDC web-identity token
   ▼
IAM Role  ── trusts ──►  cluster OIDC provider
   │                     (condition: this exact ServiceAccount)
   │  has attached policy
   ▼
bedrock:InvokeModel  on the Claude inference profiles + foundation models
```

Three IAM objects are involved — an **OIDC provider** (usually already exists),
a **policy**, and a **role** that trusts the cluster OIDC provider and carries
the policy. One Helm annotation ties the role to the agent's ServiceAccount.

## What LLopster needs from Bedrock

| Need | Detail |
|------|--------|
| Action | `bedrock:InvokeModel` (LLopster uses the non-streaming `messages.create`; `InvokeModelWithResponseStream` is optional/future-proofing) |
| Models | The three pipeline models — triage (Haiku), investigation (Sonnet), synthesis (Opus) — as **inference-profile IDs**, e.g. `us.anthropic.claude-opus-4-7-v1:0` |
| Regions | The regions the cross-region profile routes across — for the `us.` profiles: **us-east-1, us-east-2, us-west-2** |
| Model access | Enabled for those Claude models in the Bedrock console, per region |

## Step 0 — Prerequisites

**a) Enable Claude model access in Bedrock** (one-time, per region). Bedrock
console → *Model access* → enable the Claude models used by the pipeline, in
**each** region the `us.` inference profile routes across (us-east-1,
us-east-2, us-west-2). Without this, `InvokeModel` returns
`AccessDeniedException` no matter how the IAM is set up.

**b) Ensure the cluster has an IAM OIDC provider.** IRSA needs the cluster's
OIDC issuer registered as an IAM identity provider (idempotent — safe if it
already exists):

```bash
eksctl utils associate-iam-oidc-provider --cluster <cluster-name> --region us-east-1 --approve
```

If you don't use eksctl, this is the *"Create an IAM OIDC provider for your
cluster"* step in the EKS docs.

## Step 1 — Create the IAM policy

The Bedrock nuance to get right: invoking a **cross-region inference profile**
(`us.anthropic.…`) requires IAM permission on **both** the inference-profile
resource **and** the underlying foundation-model in **every** region the
profile can route to. Miss the foundation-model ARNs and you get a confusing
`AccessDeniedException`.

Save as `llopster-bedrock-policy.json` (substitute `<account-id>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeClaudeInferenceProfiles",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ],
      "Resource": [
        "arn:aws:bedrock:*:<account-id>:inference-profile/us.anthropic.claude-*",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-*"
      ]
    }
  ]
}
```

- **Foundation-model ARNs have an empty account field**
  (`:bedrock:<region>::foundation-model/…`) — they're AWS-owned. Not a typo.
- **Inference-profile ARNs carry your account ID** — they're account-scoped.
- `claude-*` scopes to Claude only. For least-privilege, replace the wildcards
  with the exact profile IDs your deployment uses (opus for synthesis, sonnet
  for investigation, haiku for triage — see
  [Step 3](#step-3--wire-it-into-the-helm-chart)).
- Using a **different geography** (`eu.` / `apac.` profiles)? Change the
  inference-profile prefix and the three foundation-model regions to match that
  profile's source regions.

Create it:

```bash
aws iam create-policy --policy-name LLopsterBedrockInvoke --policy-document file://llopster-bedrock-policy.json
```

## Step 2 — Create the IAM role (IRSA trust)

The role must **trust the cluster's OIDC provider**, restricted to *exactly*
LLopster's ServiceAccount so no other pod can assume it. LLopster's default SA
is **`llopster-agent`** in the release namespace (the docs install into
**`llopster`**), so the OIDC `sub` is
`system:serviceaccount:llopster:llopster-agent`.

### Option A — eksctl builds the role + trust for you

```bash
eksctl create iamserviceaccount --cluster <cluster-name> --region us-east-1 --namespace llopster --name llopster-agent --role-name LLopsterBedrockRole --attach-policy-arn arn:aws:iam::<account-id>:policy/LLopsterBedrockInvoke --approve --override-existing-serviceaccounts
```

This creates the role with the correct OIDC trust scoped to that SA and prints
the role ARN.

> **Ownership caveat:** eksctl also creates the ServiceAccount **object**, and
> so does the Helm chart (`agent.serviceAccount.create=true`). Pick one owner:
> - Let **eksctl own the SA** → set `agent.serviceAccount.create=false` and
>   `agent.serviceAccount.name=llopster-agent` in Helm (chart just references it).
> - Let **Helm own the SA** → create only the role/policy (use Option B, which
>   doesn't create an SA) and put the role ARN in the chart annotation.

### Option B — manual / Terraform-friendly

Get the cluster's OIDC issuer host:

```bash
aws eks describe-cluster --name <cluster-name> --region us-east-1 --query "cluster.identity.oidc.issuer" --output text
```

It returns e.g.
`https://oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE`.
Save the trust policy as `trust.json` (substitute `<account-id>`, `<region>`,
`<oidc-id>`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<account-id>:oidc-provider/oidc.eks.<region>.amazonaws.com/id/<oidc-id>"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.<region>.amazonaws.com/id/<oidc-id>:aud": "sts.amazonaws.com",
          "oidc.eks.<region>.amazonaws.com/id/<oidc-id>:sub": "system:serviceaccount:llopster:llopster-agent"
        }
      }
    }
  ]
}
```

Create the role and attach the policy:

```bash
aws iam create-role --role-name LLopsterBedrockRole --assume-role-policy-document file://trust.json
```

```bash
aws iam attach-role-policy --role-name LLopsterBedrockRole --policy-arn arn:aws:iam::<account-id>:policy/LLopsterBedrockInvoke
```

The role ARN is `arn:aws:iam::<account-id>:role/LLopsterBedrockRole`.

## Step 3 — Wire it into the Helm chart

The role ARN is non-sensitive config: it goes in `values.yaml` (or `--set`).
The chart stamps it onto the ServiceAccount as the `eks.amazonaws.com/role-arn`
annotation; EKS then injects a projected token + role env into the pod, and
boto3 (under the Anthropic Bedrock SDK) picks it up automatically. **No AWS
`Secret` is created.**

`values.bedrock.yaml`:

```yaml
agent:
  llm:
    provider: bedrock
    bedrock:
      region: us-east-1
      # Override ONLY if your enabled inference-profile IDs differ from the
      # chart defaults. These must match the IDs allowed by the IAM policy.
      # model: us.anthropic.claude-opus-4-7-v1:0
      # triageModel: us.anthropic.claude-haiku-4-5-v1:0
      # investigationModel: us.anthropic.claude-sonnet-4-6-v1:0
  serviceAccount:
    create: true          # set false if eksctl created the SA (Step 2, Option A)
    name: llopster-agent
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::<account-id>:role/LLopsterBedrockRole
```

Install/upgrade (layer with your BYO-observability + auth values):

```bash
helm upgrade --install llopster oci://ghcr.io/synchrony-solutions/charts/llopster --version 1.2.0 --namespace llopster --create-namespace -f values.bedrock.yaml -f values.byo.yaml
```

Notes:
- Because `provider=bedrock`, the chart's fail-closed guard **requires**
  `agent.llm.bedrock.region` or it refuses to render.
- `agent.secrets.ANTHROPIC_API_KEY` can stay empty in Bedrock mode.
- The 1-hour `extended-cache-ttl` prompt-cache beta is Anthropic-API-only and
  is forced **off** automatically on Bedrock (5-minute ephemeral caching still
  applies).

## Step 4 — Verify

Confirm the annotation landed on the ServiceAccount:

```bash
kubectl -n llopster get sa llopster-agent -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}{"\n"}'
```

Confirm EKS injected the web-identity env into the pod (you want to see
`AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE`, plus the LLopster provider
vars):

```bash
kubectl -n llopster exec deploy/llopster-agent -- env | grep -E 'LLM_PROVIDER|AWS_REGION|AWS_ROLE_ARN|AWS_WEB_IDENTITY|BEDROCK_'
```

Then confirm at runtime:
- The agent logs `LLM provider=bedrock models=(triage=…, investigation=…, synthesis=…)` on startup.
- The **dashboard Settings → connection card** shows *Claude · AWS Bedrock*, the region, and the active synthesis model.
- Fire one test alert and watch a run reach `done` (a real PR if GitHub is wired).

## Alternative: EKS Pod Identity

Pod Identity is the newer alternative to IRSA — no OIDC trust JSON, and the
ServiceAccount needs **no** `role-arn` annotation. Instead:

1. Create the role trusting the Pod Identity principal:

```bash
aws iam create-role --role-name LLopsterBedrockRole --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"pods.eks.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}'
```

2. Attach the same `LLopsterBedrockInvoke` policy (Step 1), then associate it:

```bash
aws eks create-pod-identity-association --cluster <cluster-name> --namespace llopster --service-account llopster-agent --role-arn arn:aws:iam::<account-id>:role/LLopsterBedrockRole
```

3. In Helm, keep the SA but drop the annotation: `agent.serviceAccount.annotations: {}`.

Requires the **EKS Pod Identity Agent** add-on on the cluster.

## Fallback: static AWS keys

Only when a cluster genuinely can't use IRSA or Pod Identity. Here AWS
credentials become real secrets; the chart routes them through the
`llopster-agent` Kubernetes `Secret` and wires them to the pod **only** when
set and **only** under `provider=bedrock`:

```yaml
agent:
  secrets:
    AWS_ACCESS_KEY_ID: AKIA...
    AWS_SECRET_ACCESS_KEY: ...
    # AWS_SESSION_TOKEN: ...   # only for temporary credentials
```

Prefer IRSA whenever possible: no long-lived keys, auto-rotated tokens, nothing
to leak or rotate by hand.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `AccessDeniedException` on invoke, IAM looks correct | Model access not enabled in the Bedrock console for that model **in that region** (Step 0a) |
| `AccessDeniedException` naming a `foundation-model` ARN | Policy allows the inference profile but not the foundation-model ARNs in all routed regions (Step 1) |
| Pod has no `AWS_ROLE_ARN` env | SA annotation missing/typo'd, or the pod predates the annotation — `kubectl -n llopster rollout restart deploy/llopster-agent` |
| `ValidationException: … model identifier is invalid` | `BEDROCK_MODEL` (etc.) isn't a valid inference-profile ID for the region — check the exact ID and that `region` is one of the profile's source regions |
| Chart refuses to render, mentions `region` | `provider=bedrock` without `agent.llm.bedrock.region` set (fail-closed guard) |
| SA conflict on `helm upgrade` | Both eksctl and Helm are trying to own the ServiceAccount — see the ownership caveat in Step 2, Option A |

## A note on model IDs

The chart's default Bedrock model IDs (`us.anthropic.claude-opus-4-7-v1:0`,
etc.) are starting-point conventions. Set
`agent.llm.bedrock.{model,triageModel,investigationModel}` to the actual
inference-profile IDs enabled in the target account/region, and make the IAM
policy's `Resource` list cover those same IDs.
