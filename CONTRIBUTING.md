# Contributing to LLopster

Thanks for your interest in improving LLopster. It's an AI-augmented SRE agent
that reads alerts, logs, and metrics, proposes a fix, and opens a pull request —
so it holds real credentials and can write to real repositories. That makes
correctness and fail-safe behaviour matter more than in a typical web app, and
it shapes how we review changes. Please read this before opening a PR.

> Found a security issue? **Do not open a public issue or PR.** Follow
> [SECURITY.md](SECURITY.md) instead.

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md) — by participating,
you agree to uphold it.

## Ways to contribute

- **Bug reports** — open an issue with a minimal repro, what you expected, what
  happened, and your environment (LLopster version, Python version, deploy mode:
  `docker compose` vs. Helm).
- **Bug fixes and features** — see the workflow below. Small, focused PRs merge
  fastest.
- **Documentation** — corrections and clarifications to `README.md`,
  `docs/`, or the Helm docs are very welcome and a great first contribution.

If you're planning a large or architectural change, open an issue to discuss it
first so you don't invest in a direction we'd ask you to rework.

## Development setup

LLopster targets **Python 3.12**. The app itself runs in Docker, but the test
suite runs locally against a virtualenv.

```bash
# Clone your fork
git clone https://github.com/<your-fork>/LLopster.git
cd LLopster

# Create and populate a virtualenv (note: this repo uses .venv/bin/python
# explicitly — plain `python`/`python3` may not have the deps)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

To run the full stack locally (agent + dashboard + demo-app + observability),
follow the **Quickstart** in [README.md](README.md). You'll need an
`ANTHROPIC_API_KEY` for anything that actually calls the model; most
contributions can be validated with the test suite alone.

## Running the tests

```bash
.venv/bin/python -m pytest tests/
```

The suite is fast and hermetic — **no live API calls, no external services**. HTTP
clients are exercised with `httpx.MockTransport` and the Anthropic client with
`unittest.mock.AsyncMock`; database tests use in-memory `sqlite+aiosqlite` with a
fresh schema per test. If a test needs the network or a real key, that's a bug in
the test.

**Every PR must keep the suite green, and behavioural changes must add tests.**
CI runs the same `pytest` on every pull request (see
`.github/workflows/tests.yml`), plus a Helm chart lint/template check
(`helm-ci.yml`) for changes under `helm-chart/`.

## Coding conventions

These are the house rules the codebase already follows — match them:

- **Fail safe, never crash on the sad path.** Missing env vars disable a feature
  with a warning, they don't raise. Every LLM stage is kill-switchable via the
  settings table and must degrade gracefully when disabled or when the API
  errors. The per-alert pipeline is fire-and-forget and must never raise out of
  `process_alert`.
- **Security controls fail closed.** Anything that gates a patch, a PR, an auth
  check, or a license/entitlement decision must fail toward *less* access, not
  more. A bug in `github_client.py` diff application, `auth.py`, or `license.py`
  should drop the change, not ship it.
- **Write code that reads like the code around it.** Match the surrounding
  naming, structure, and comment density. Don't refactor speculatively or add
  config knobs nothing uses.
- **Keep changes incremental and single-purpose.** One logical change per PR.
- **Don't hardcode counts or figures that drift** (e.g. a test count) into docs;
  let CI or the reader derive them.

## Pull request process

`main` is protected: there are no direct pushes, and the merge is gated on CI
and review. Concretely:

1. Fork the repo and create a branch off `main`
   (`git checkout -b fix/short-description`).
2. Make your change; add or update tests.
3. Run `.venv/bin/python -m pytest tests/` and make sure it's green.
4. Open a PR against `main`. Describe **what** changed and **why**; link any
   related issue. Draft PRs are welcome for early feedback.
5. **CI must pass.** `pytest` runs on every PR as a *required* status check and
   blocks the merge until green; keep your branch up to date with `main`.
6. **A code-owner review is required.** At least one approving review from the
   relevant [CODEOWNERS](.github/CODEOWNERS) is mandatory before merge. Pushing
   new commits dismisses stale approvals, and open review threads must be
   resolved. Security-sensitive areas (diff application, the inbound-auth
   surface, the GitHub/token path, license verification) get extra scrutiny —
   expect questions.
7. History on `main` is kept **linear** — no merge commits or force-pushes — so
   expect to rebase (or squash) rather than merge `main` into your branch.

Releases are cut by pushing a `v*` tag, which is restricted to the release team;
regular contributions never need to tag.

### Sign-offs (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/).
Certify that you wrote (or have the right to submit) your contribution by adding
a sign-off line to each commit:

```
Signed-off-by: Your Name <you@example.com>
```

`git commit -s` adds this automatically.

## Licensing of contributions

LLopster is released under the **Functional Source License (FSL-1.1-ALv2)** — see
[LICENSE.md](LICENSE.md). By submitting a contribution you agree it is licensed
under those same terms. FSL is source-available (not OSI open source) and each
version converts to Apache-2.0 two years after release; contributions inherit
that model.

**Never** include in a PR: real secrets or credentials, the license signing key,
any customer data, or proprietary premium-pack content. Example/placeholder
values only (see `helm-chart/example.values.secret.yaml` for the expected
shape).

## Questions

Open a [discussion or issue](https://github.com/synchrony-solutions/LLopster/issues). We're
happy to help you land your first change.
