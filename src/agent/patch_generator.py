"""Send alert context + codebase to Claude and ask for a proposed fix."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from anthropic import AsyncAnthropic

from src.agent.context_collector import AlertContext
from src.agent.dedup import PreviousAttempt
from src.agent.investigator import Investigation
from src.agent.prompts import STAGE_SYNTHESIS, PromptResolver
from src.services_registry import ChartLayer, Delivery

log = logging.getLogger("llopster.patch")


SYSTEM_PROMPT = """You are an SRE assistant embedded in an incident response pipeline.

You receive a Prometheus alert, the surrounding Loki log lines, the alert's
underlying metric values, and the source code of the affected service. Your job:

1. Identify the root cause of the alert from the logs and metrics.
2. Locate the specific file(s) and line(s) in the codebase that caused it.
3. Propose a minimal patch as a unified diff that fixes the root cause.
4. Rate your confidence in the diagnosis and fix.

Format your response as:

## Root Cause
<one paragraph>

## Proposed Patch
```diff
<unified diff>
```

## Confidence
<N>/5 — <one sentence explaining your certainty>

Use this scale:
- 5: Root cause is unambiguous from logs/metrics; fix is clear and isolated
- 4: High confidence in root cause and fix; minor uncertainty remains
- 3: Plausible root cause and fix, but limited evidence or multiple possible causes
- 2: Uncertain root cause; fix is a best guess based on limited context
- 1: Insufficient context to diagnose; patch is speculative

## Reasoning
<one paragraph explaining why this patch fixes the issue>

If the codebase context is insufficient to propose a confident fix, say so
explicitly and assign a confidence of 1 or 2 rather than guessing.

# Diff format rules — strict

Cost and patch-application correctness both depend on tight diffs. Follow
these rules in the `## Proposed Patch` block:

- Output a unified diff and nothing else inside the ```diff fence. No prose,
  no explanations, no alternative versions. Save commentary for the
  `## Reasoning` section.
- NEVER emit a full-file rewrite. If only one line changes, the diff has
  exactly one `-` line and one `+` line plus the standard 3 lines of
  surrounding context. Do not pad with unchanged content beyond what the
  unified-diff format requires.
- Use exactly 3 lines of leading/trailing context per hunk (the `git diff`
  default). More context inflates output tokens and makes the patch fragile
  if the surrounding file changes.
- Use accurate `@@ -<old_start>,<old_count> +<new_start>,<new_count> @@`
  hunk headers. The patch applier uses these line numbers verbatim — wrong
  numbers apply the patch in the wrong place.
- Never output multiple candidate diffs. Pick one. If you have two ideas,
  state the alternative in `## Reasoning` and put the higher-confidence one
  in `## Proposed Patch`.
- If the fix spans multiple files, emit one combined unified diff with
  multiple `--- a/<path>` / `+++ b/<path>` headers. Don't emit multiple
  separate fenced diff blocks.
- If no code patch is appropriate (alert is informational, requires manual
  intervention, or the codebase context is insufficient), put exactly the
  string `No code patch is appropriate for this alert.` inside the diff
  fence and explain why in `## Reasoning`."""


# Ship every text file in the codebase. Skip binaries and bulky directories.
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "dist", "build"}
_TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".json", ".toml", ".md", ".txt",
    ".sh", ".dockerfile", "Dockerfile", ".js", ".ts", ".go", ".rs",
}


@dataclass
class PatchProposal:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    confidence: int        # 1–5; 0 if unparseable
    confidence_reason: str # sentence after "N/5 — "
    # Phase C bookkeeping: True when Opus saw a narrowed codebase blob
    # built from Sonnet's affected_files; False when we fell back to the
    # full codebase (no investigation, empty affected_files, or every
    # file in the list was missing on disk at load time). The dashboard
    # badge + future per-mode spend stats read this.
    used_narrowed_context: bool = False
    # Count of files actually included in the blob Opus saw. Lets the
    # dashboard show "claude-opus-4-7 · 3 files" without re-walking the
    # codebase. 0 means the load failed entirely (very rare; should not
    # happen because of the empty-blob safety net).
    file_count: int = 0


def _parse_confidence(text: str) -> tuple[int, str]:
    """Extract the N/5 score and reason from the ## Confidence section.

    Returns (0, "...") if the section is missing or malformed — callers
    should treat 0 as below any threshold (fail-safe: skip the PR).
    """
    m = re.search(r"##\s+Confidence\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL)
    if not m:
        return 0, "confidence section missing from response"
    section = m.group(1).strip()
    score_match = re.search(r"([1-5])\s*/\s*5\s*[—\-–]\s*(.*)", section, re.DOTALL)
    if score_match:
        score = int(score_match.group(1))
        reason = score_match.group(2).strip().splitlines()[0].strip()
        return score, reason
    # Fallback: bare digit
    bare = re.search(r"\b([1-5])\b", section)
    if bare:
        return int(bare.group(1)), section.splitlines()[0].strip()
    return 0, "confidence score not parseable"


class PatchGenerator:
    def __init__(
        self,
        api_key: str,
        model: str,
        extended_cache_ttl: bool = True,
        *,
        client: AsyncAnthropic | None = None,
        prompt_resolver: PromptResolver | None = None,
    ):
        # The extended-TTL cache beta lets us pin the codebase blob in cache
        # for 1 hour instead of 5 minutes. The codebase only changes on
        # deploy, so 5-min TTL forces low-traffic agents to rebuild the
        # cache constantly and burn cost. Opt out for accounts that don't
        # have the beta enabled.
        #
        # ``client`` lets main.py inject a provider-built client (Anthropic
        # API or Bedrock). When None we build the direct-API client from
        # ``api_key`` — the historical default that keeps the unit tests
        # working unchanged. ``extended_cache_ttl`` still governs the
        # ``"ttl": "1h"`` cache marker in generate(); main.py forces it
        # False for Bedrock (no extended-TTL beta there).
        if client is not None:
            self.client = client
        else:
            default_headers = (
                {"anthropic-beta": "extended-cache-ttl-2025-04-11"}
                if extended_cache_ttl else {}
            )
            self.client = AsyncAnthropic(api_key=api_key, default_headers=default_headers)
        self.model = model
        self.extended_cache_ttl = extended_cache_ttl
        # Optional pack-aware prompt seam. None → baked-in Community
        # SYSTEM_PROMPT (existing behavior, used by unit tests).
        self.prompt_resolver = prompt_resolver

    def _system_prompt(self, stack: str | None) -> str:
        if self.prompt_resolver is not None:
            return self.prompt_resolver.resolve(STAGE_SYNTHESIS, stack=stack)
        return SYSTEM_PROMPT

    async def generate(
        self,
        ctx: AlertContext,
        codebase_path: str,
        *,
        previous_attempt: PreviousAttempt | None = None,
        investigation: Investigation | None = None,
        stack: str | None = None,
        delivery: Delivery | None = None,
        chart_lineage: tuple[ChartLayer, ...] = (),
        github_repo: str | None = None,
    ) -> PatchProposal:
        root = Path(codebase_path)

        # ---- Fallback ladder for the codebase blob ----------------------
        # Phase C narrows Opus's view to only the files Sonnet flagged.
        # Three escape hatches guarantee we never make synthesis worse
        # than today:
        #   1. No investigation (Sonnet errored / disabled) → full tree
        #   2. Investigation present but affected_files empty → full tree
        #   3. affected_files present but EVERY path is missing on disk
        #      at load time → fall back to full tree + log warning
        # In every other case the blob is the narrowed slice.
        used_narrowed = False
        file_count = 0
        if investigation is not None and investigation.affected_files:
            # The narrowed blob is what synthesis can actually read. When the
            # delivery constraint tells the model to bump a version reference
            # in this repo, that file has to be IN the blob — the investigator
            # never flags it (it is deploy-side config, not a code path), and
            # asking for a hunk against a file the model cannot see produces a
            # fabricated diff that fails closed at `_locate_hunk`.
            only_files = list(investigation.affected_files)
            ref_path = _same_repo_version_ref(delivery, github_repo)
            if ref_path and ref_path not in only_files:
                only_files.append(ref_path)
            codebase_blob, file_count = _load_codebase(root, only_files=only_files)
            if file_count == 0:
                log.warning(
                    "patch_generator: every affected file was missing on disk "
                    "(%s); falling back to full codebase",
                    investigation.affected_files,
                )
                codebase_blob, file_count = _load_codebase(root)
            else:
                used_narrowed = True
        else:
            codebase_blob, file_count = _load_codebase(root)

        alert_blob = _format_alert_context(
            ctx,
            previous_attempt=previous_attempt,
            investigation=investigation if used_narrowed else None,
            delivery=delivery,
            chart_lineage=chart_lineage,
            github_repo=github_repo,
        )

        cache_control: dict[str, str] = {"type": "ephemeral"}
        if self.extended_cache_ttl:
            cache_control["ttl"] = "1h"

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=self._system_prompt(stack),
            thinking={"type": "adaptive"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        # Codebase first — stable across alerts (or stable
                        # within a service-incident-type when narrowed),
                        # cache it.
                        {
                            "type": "text",
                            "text": codebase_blob,
                            "cache_control": cache_control,
                        },
                        # Alert context + investigation handoff last —
                        # volatile, no cache marker.
                        {"type": "text", "text": alert_blob},
                    ],
                }
            ],
        )

        text = next((b.text for b in response.content if b.type == "text"), "")
        confidence, confidence_reason = _parse_confidence(text)
        u = response.usage
        return PatchProposal(
            text=text,
            model=response.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            confidence=confidence,
            confidence_reason=confidence_reason,
            used_narrowed_context=used_narrowed,
            file_count=file_count,
        )


def _load_codebase(
    root: Path,
    *,
    only_files: list[str] | None = None,
) -> tuple[str, int]:
    """Concatenate text files under `root` into a labeled blob.

    When `only_files` is provided, restrict the blob to that explicit
    list (Phase C narrowing). Paths are interpreted relative to `root`.
    Missing paths are silently skipped — the caller decides what to do
    when nothing was loaded (PatchGenerator.generate falls back to the
    full tree on empty narrowed blob).

    Returns (blob, file_count) so the caller can record how many files
    Opus actually saw for the dashboard badge / cost stats.
    """
    if not root.exists():
        return f"<codebase at {root} not found>", 0

    if only_files is not None:
        # Narrowed mode: load exactly the requested files in the listed
        # order so the cache prefix is deterministic per affected-files
        # selection (alphabetical order would cache-thrash when Sonnet
        # reorders its picks across runs).
        header = (
            f"# Codebase rooted at {root} "
            f"(narrowed to {len(only_files)} file(s) selected by investigator)\n"
        )
        parts = [header]
        loaded = 0
        for rel in only_files:
            path = root / rel
            # Defence against absolute-path or `..` injection: ensure
            # the resolved path stays under root.
            try:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError):
                log.warning("patch_generator: dropping out-of-tree path %r", rel)
                continue
            if not path.is_file():
                continue
            try:
                content = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            parts.append(f"\n--- FILE: {rel} ---\n{content}")
            loaded += 1
        return "".join(parts), loaded

    # Full-tree mode (today's behaviour).
    parts = [f"# Codebase rooted at {root}\n"]
    loaded = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(p in _SKIP_DIRS for p in path.parts):
            continue
        if path.suffix not in _TEXT_SUFFIXES and path.name not in _TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root)
        parts.append(f"\n--- FILE: {rel} ---\n{content}")
        loaded += 1
    return "".join(parts), loaded


def _format_alert_context(
    ctx: AlertContext,
    *,
    previous_attempt: PreviousAttempt | None = None,
    investigation: Investigation | None = None,
    delivery: Delivery | None = None,
    chart_lineage: tuple[ChartLayer, ...] = (),
    github_repo: str | None = None,
) -> str:
    a = ctx.alert
    lines: list[str] = []
    if previous_attempt is not None:
        lines += _format_previous_attempt(previous_attempt)
        lines += [""]
    if investigation is not None:
        lines += _format_investigation_handoff(investigation)
        lines += [""]
    # Operator-declared framing, grouped with the other handoff headers. Both
    # live in the volatile block (after the codebase blob's cache_control
    # marker), so adding them cannot invalidate the cached prefix.
    if delivery is not None and delivery.is_indirect:
        lines += _format_delivery_constraint(delivery, github_repo=github_repo)
        lines += [""]
    if chart_lineage:
        lines += _format_chart_lineage(chart_lineage)
        lines += [""]
    lines += [
        "# Incident context",
        "",
        f"**Alert:** {a.alertname}",
        f"**Severity:** {a.severity}",
        f"**Service:** {a.service}",
        f"**Summary:** {a.summary}",
        f"**Description:** {a.description}",
        f"**Started at:** {a.starts_at.isoformat() if a.starts_at else 'unknown'}",
        "",
        "## Labels",
        *(f"- {k}: {v}" for k, v in sorted(a.labels.items())),
        "",
        "## Queries used to gather context",
        *(f"- {k}: `{v}`" for k, v in ctx.queries_used.items()),
        "",
        f"## Loki log lines ({len(ctx.log_lines)} returned, newest first)",
    ]
    for line in ctx.log_lines:
        lines.append(f"- {line.timestamp.isoformat()} {line.line}")
    lines += ["", f"## Prometheus samples ({len(ctx.metric_samples)})"]
    for s in ctx.metric_samples:
        lines.append(f"- {s.metric} = {s.value}")
    if ctx.errors:
        lines += ["", "## Errors collecting context"]
        lines += [f"- {e}" for e in ctx.errors]
    return "\n".join(lines)


def _format_delivery_constraint(
    delivery: Delivery, *, github_repo: str | None,
) -> list[str]:
    """State how a merged patch actually reaches the cluster.

    Only emitted for the indirect modes. `git-manifest` is what the pipeline
    has always assumed, so declaring it changes nothing and costs no tokens.

    The failure this exists to prevent: under `oci-chart`/`image-build` a patch
    to the source can be correct, apply cleanly, pass validation, merge — and
    never reach the cluster, because what the cluster consumes is a packaged
    artifact at a pinned version. Every gate in the pipeline passes it. Only
    the operator's declaration can catch it, so it is stated as a hard
    constraint on the answer rather than as background.

    Whether the model is told to bump the version reference *in this diff*
    depends on where that reference lives. A PR targets exactly one repo, so
    asking for a cross-repo bump would request a patch the pipeline cannot open
    — it would be refused at the path gate, or split across repos it cannot
    span. Inclusion is therefore requested only when the reference is known to
    sit in the repo this PR will target.
    """
    ref = delivery.version_ref
    same_repo = bool(
        ref is not None and ref.repo and github_repo and ref.repo == github_repo
    )

    if delivery.mode == "oci-chart":
        what = (
            "This service's chart is PACKAGED and pushed to a registry. Editing "
            "the chart source in this repo does NOT change anything in the "
            "cluster until the chart is repackaged and the version reference "
            "that the cluster consumes is bumped."
        )
    else:  # image-build
        what = (
            "This service's image is BUILT by a CI pipeline. Editing the source "
            "in this repo does NOT change anything in the cluster until a new "
            "image is built and the image tag that the cluster consumes is "
            "bumped."
        )

    out = [
        "# Delivery constraint — read before proposing a patch",
        "",
        f"**Delivery mode:** `{delivery.mode}`",
        "",
        what,
        "",
        "A source-only patch here will look correct, apply cleanly, pass "
        "validation, and merge — and the alert will keep firing. Do not treat "
        "a clean diff as a fix.",
        "",
    ]

    if ref is not None and any((ref.repo, ref.path, ref.key)):
        out += ["The version the cluster actually consumes is declared at:"]
        if ref.repo:
            out.append(f"- repo: `{ref.repo}`")
        if ref.path:
            out.append(f"- file: `{ref.path}`")
        if ref.key:
            out.append(f"- key: `{ref.key}`")
        out.append("")

    if same_repo:
        out += [
            "That reference is in the same repository this PR targets, so you "
            "MUST include the version bump in the same diff as any source "
            "change. A source change on its own is not an acceptable answer.",
            "",
            "If you cannot determine the correct new version, do not invent "
            "one: omit the patch and return a confidence of 1 or 2 explaining "
            "what needs to be released.",
        ]
    else:
        out += [
            "That reference is NOT in the repository this PR targets, and a "
            "pull request can only span one repository. You therefore cannot "
            "deliver a working fix in this diff.",
            "",
            "Return a confidence of 1 or 2. In `## Root Cause` and "
            "`## Reasoning`, state the source change that is needed AND the "
            "separate repackage/bump required to make it take effect, so a "
            "human can carry out both. A confident-looking patch here would be "
            "worse than no patch.",
        ]
    return out


def _format_chart_lineage(lineage: tuple[ChartLayer, ...]) -> list[str]:
    """Name the chart layers, including the ones outside the codebase blob.

    The model is correctly forbidden from inventing paths, so when the causal
    surface is split across charts it cannot see, it names the closest-looking
    file in the tree it *can* see — a confident answer about the wrong file.
    Telling it which layers exist but are invisible converts a share of those
    into honest low-confidence results.

    Listed outermost first. In Helm a parent chart's values override a
    subchart's, so an outer layer silently wins over an inner one — which is
    how a clean patch to a visible inner layer produces a merged PR and an
    alert that never clears.
    """
    out = [
        "# Chart lineage",
        "",
        "This service is delivered by a chart-of-charts. Layers are listed "
        "outermost first — an outer layer's values OVERRIDE an inner layer's:",
        "",
    ]
    for i, layer in enumerate(lineage, start=1):
        bits = [f"{i}. **{layer.name}**"]
        if layer.version:
            bits.append(f"v{layer.version}")
        if layer.repo:
            bits.append(f"(`{layer.repo}`)")
        bits.append(
            "— visible in the codebase below" if layer.visible
            else "— **NOT VISIBLE to you**"
        )
        out.append(" ".join(bits))

    invisible = [layer.name for layer in lineage if not layer.visible]
    out += [""]
    if invisible:
        out += [
            "You have not been shown: " + ", ".join(f"`{n}`" for n in invisible) + ".",
            "",
            "If a values key is set in one of those layers, it overrides the "
            "same key wherever you can see it, and a patch to the visible copy "
            "has NO EFFECT. When the most likely cause sits in a layer you "
            "cannot see, say so in `## Root Cause` and return a confidence of "
            "1 or 2. Do not patch the nearest visible file to have something "
            "to propose — naming the layer a human should look in is the more "
            "useful answer.",
        ]
    else:
        out += [
            "Every layer is visible in the codebase below, so the full values "
            "precedence chain is available to you.",
        ]
    return out


def _format_investigation_handoff(inv: Investigation) -> list[str]:
    """Render the Sonnet investigation as a handoff header for Opus.

    Why this matters: Opus is seeing a narrowed codebase view that
    Sonnet selected. If Sonnet picked the wrong files, Opus needs to
    know it's NOT looking at the full code and must use the no-patch
    sentinel rather than guessing from incomplete context.

    Treat Sonnet's hypothesis as a starting point, not a fact — symmetric
    with how `_format_previous_attempt` frames a prior merged-but-failed
    fix as a hypothesis to disprove rather than a template to repeat.
    """
    files_block = "\n".join(f"- {f}" for f in inv.affected_files)
    out = [
        f"# Pre-flight investigation ({inv.model})",
        "",
        "A diagnosis model reviewed the alert + logs + metrics + codebase "
        "outline and identified the likely-affected files BEFORE you saw "
        "the code. Your codebase view below has been filtered to ONLY "
        "those files. Treat the hypothesis as a starting point to verify, "
        "not a fact.",
        "",
        "## Hypothesised root cause",
        inv.root_cause or "(none recorded)",
        "",
        "## Files included in your view",
        files_block or "(none)",
        "",
        f"## Investigator confidence",
        f"{inv.confidence}/5"
        + (f" — {inv.reasoning}" if inv.reasoning else ""),
        "",
        "If the included files don't contain the actual root cause, say so "
        "in `## Reasoning` and emit `No code patch is appropriate for this "
        "alert.` inside the diff fence. The synthesis prompt has already "
        "filtered the codebase based on the above list, so guessing from "
        "incomplete context is worse than escalating.",
    ]
    return out


def _format_previous_attempt(p: PreviousAttempt) -> list[str]:
    """Render a prior merged-but-ineffective fix attempt as a prompt header.

    Goal: stop the model from re-proposing the same losing diff. The merged
    PR is live; the alert is still firing; therefore the prior diagnosis or
    fix was wrong. We surface both so the model can build on them rather
    than rediscover them.
    """
    merged_at = p.pr_merged_at.isoformat() if p.pr_merged_at else "unknown"
    out = [
        "# ⚠️ Previous fix attempt did not resolve this alert",
        "",
        f"A prior patch for this alert was merged at **{merged_at}** "
        f"(PR: {p.pr_url}, run id: {p.run_id}).",
        "The alert has fired again past the post-merge deploy grace window, "
        "which means the previous fix is live and did NOT resolve the issue.",
        "",
        "**Do not re-propose the same diff.** Either the previous diagnosis "
        "was incorrect, or the fix was incomplete. Use the prior attempt "
        "below as a hypothesis to disprove, not a template to repeat.",
        "",
    ]
    if p.parsed_root_cause:
        out += [
            "## Previous root cause hypothesis",
            p.parsed_root_cause,
            "",
        ]
    if p.parsed_diff:
        out += [
            "## Previously merged patch",
            "```diff",
            p.parsed_diff.strip(),
            "```",
            "",
        ]
    return out


def _same_repo_version_ref(
    delivery: Delivery | None, github_repo: str | None,
) -> str | None:
    """The declared version-reference path when it is in the repo this PR
    targets, and the delivery mode makes bumping it part of the answer.

    Mirrors `processor._same_repo_version_ref_path`, which widens the PR path
    allowlist by the same path. Both have to agree: the prompt asks for the
    bump, the blob has to show the file, and the gate has to let it through.
    """
    if delivery is None or not delivery.is_indirect:
        return None
    ref = delivery.version_ref
    if ref is None or not ref.path or not ref.repo:
        return None
    if github_repo is None or ref.repo != github_repo:
        return None
    return ref.path
