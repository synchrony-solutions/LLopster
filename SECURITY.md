# Security Policy

LLopster is a high-privilege component by design: it ingests untrusted alert,
log, and metric content, calls an LLM, **auto-writes code patches**, and holds a
`GITHUB_TOKEN` with write access and an `ANTHROPIC_API_KEY`. We take security
reports seriously and appreciate coordinated disclosure.

## Reporting a vulnerability

**Please do not report security issues through public GitHub issues, pull
requests, or discussions.**

Instead, use **GitHub's private vulnerability reporting**:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** (Privately report a vulnerability).
3. Fill in the details.

This opens a private advisory visible only to you and the maintainers.

If you cannot use GitHub's private reporting, email
**`<security@your-domain>`** _(maintainers: replace with a monitored address
before launch)_ with the details.

Please include:

- A description of the issue and its impact.
- Steps to reproduce, or a proof-of-concept.
- Affected version(s) and deployment mode (`docker compose` or Helm).
- Any suggested remediation, if you have one.

### What to expect

- **Acknowledgement:** we aim to confirm receipt within **3 business days**.
- **Assessment:** an initial severity assessment and next steps within
  **10 business days**.
- **Fix & disclosure:** we'll work with you on a coordinated disclosure timeline
  and credit you in the advisory unless you prefer to remain anonymous. Please
  give us a reasonable window to release a fix before any public disclosure.

This is a young project maintained by a small team — timelines are best-effort,
and we'll keep you updated if something takes longer.

## Supported versions

Security fixes are applied to the latest released line. Older lines are not
backported.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Security model & operator responsibilities

LLopster's blast radius depends heavily on how you deploy it. The following are
**operator responsibilities** — treat this as a hardening checklist, not a
promise that defaults are safe for an exposed deployment:

- **Authenticate every inbound write surface.** Set `LLOPSTER_API_TOKEN` (or the
  dashboard `api_auth_token`) so `/webhook`, the trigger routes, and settings/
  license mutations require a bearer token. With no token set, these surfaces are
  **unauthenticated** — acceptable only on a trusted/local network. See the
  "Securing the inbound surfaces" section of [README.md](README.md).
- **Never expose LLopster directly to the internet without TLS and auth in
  front.** Terminate TLS and require authentication at your ingress/proxy.
- **Scope the GitHub token to least privilege.** Prefer a fine-grained PAT (or a
  GitHub App) limited to the specific repositories LLopster manages, with only
  `Contents: RW` + `Pull requests: RW`. Avoid a broad classic `repo`-scoped
  token.
- **Keep opened PRs as drafts** (`OPEN_PRS_AS_DRAFT=true`, the default) so a human
  reviews and merges. LLopster proposes; it should not merge.
- **Set cost ceilings.** Configure `MAX_RUNS_PER_HOUR` / `MAX_USD_PER_DAY` (and
  keep the dedup/backoff controls on) so a misbehaving alert source cannot drive
  unbounded LLM spend.
- **Protect your secrets.** `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`,
  `SLACK_WEBHOOK_URL`, and the database password belong in Kubernetes Secrets (or
  equivalent), never in values files committed to git. See
  `helm-chart/example.values.secret.yaml`.

### License signing

The license scheme is asymmetric (Ed25519). Only the **public** verification key
ships in this repository; the **private** signing key is never committed and is
not present in the source tree. Any missing, expired, or invalid license fails
safe to the free Community tier.

## Out of scope

The following are **not** vulnerabilities in LLopster:

- **The seeded bugs in `demo-app/`.** These are intentional, documented faults
  used to demonstrate and test the pipeline — not real defects.
- **Behaviour resulting from disabling built-in safety controls** (e.g. running
  with no inbound auth on an exposed network, granting an over-scoped token,
  turning off draft PRs, or setting cost ceilings to unlimited). These are
  operator configuration choices; the hardening checklist above is the guidance.
- **Prompt content that an operator's own trusted alerts/logs inject into a
  proposed patch that a human then reviews and merges.** The draft-PR + human-
  review step is the intended control. Reports showing a bypass of the patch
  path gates or the validation gate, however, **are** in scope.

## Disclosure

We follow coordinated disclosure. Once a fix is available, we'll publish a
GitHub Security Advisory describing the issue, affected versions, and remediation,
and credit the reporter.
