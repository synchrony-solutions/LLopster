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
            codebase_blob, file_count = _load_codebase(
                root, only_files=investigation.affected_files,
            )
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
) -> str:
    a = ctx.alert
    lines: list[str] = []
    if previous_attempt is not None:
        lines += _format_previous_attempt(previous_attempt)
        lines += [""]
    if investigation is not None:
        lines += _format_investigation_handoff(investigation)
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
