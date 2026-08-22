"""Sonnet diagnosis stage: read alert + logs + metrics + codebase
*outline* and produce a root-cause hypothesis plus a short list of
affected files.

This is Phase B of the three-stage model-tier routing. It runs after
context collection and before the Opus synthesis call. In Phase B
the investigation is recorded on the Run row but Opus still sees the
full codebase blob — purely additive. In Phase C, Opus will consume
`Investigation.affected_files` to dramatically narrow its prompt.

Design notes:

  * **Outline, not contents.** Sonnet only sees file paths grouped by
    top-level directory plus line counts. That keeps the Sonnet call
    cheap (~10 KB outline vs the multi-hundred-KB codebase blob Opus
    sees today) and forces the model to identify likely-affected files
    from path names + log/metric correlation rather than from full
    code reading. The trade-off is real: for cryptic alerts where the
    relevant code lives in a file whose path doesn't hint at the
    symptom, Sonnet may miss. Phase C's fallback is "if affected_files
    is empty or all invalid, fall back to the full codebase blob" so
    Sonnet missing never makes synthesis worse than today.

  * **Triage reasoning in the prompt.** Haiku already produced a one-
    line framing of why the alert is actionable. Including it gives
    Sonnet a head-start rather than starting cold — ~50 extra tokens
    for a meaningful accuracy bump.

  * **Adaptive thinking with fail-open.** Investigation IS a reasoning
    task, so we ask for adaptive thinking. If Sonnet 4.6 ever rejects
    that arg (we got bitten by Haiku 4.5 doing exactly this in Phase
    A), we retry once without thinking. The processor wraps the whole
    call in a fail-open try/except so an outage never blocks Opus.

  * **Affected-files cap.** Anti-abuse: 20 files. The whole point of
    Phase C is a narrower prompt; a 200-file "affected" list would
    defeat the purpose.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from anthropic import AsyncAnthropic, BadRequestError

from src.agent.context_collector import AlertContext
from src.agent.prompts import STAGE_INVESTIGATION, PromptResolver
from src.services_registry import ChartLayer

log = logging.getLogger("llopster.investigator")


_MAX_AFFECTED_FILES = 20

# Mirror PatchGenerator's skip/include logic so the outline matches the
# blob Opus will see in Phase C. Kept duplicated rather than imported
# to avoid a circular import via patch_generator → context_collector.
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", "dist", "build"}
_TEXT_SUFFIXES = {
    ".py", ".yaml", ".yml", ".json", ".toml", ".md", ".txt",
    ".sh", ".dockerfile", "Dockerfile", ".js", ".ts", ".go", ".rs",
}


SYSTEM_PROMPT = """You are an SRE diagnostician. You receive a Prometheus
alert, the surrounding Loki log lines, the alert's metric samples, and
an *outline* of the affected service's codebase (file paths + line
counts, grouped by directory). You do NOT see file contents.

Your job has exactly two outputs:

1. A one-paragraph root-cause hypothesis grounded in the logs/metrics.
2. A short list (≤ 20) of file paths from the outline that are most
   likely to need editing to fix the root cause. Use ONLY paths that
   appear verbatim in the codebase outline — never invent paths.

A downstream model (Opus) will read the contents of the files you list
and propose a patch. Your accuracy on file selection is what makes
that synthesis cheap; missing a file forces Opus to fall back to
reading the whole codebase, but listing irrelevant files just adds
noise. Be precise.

If you genuinely cannot identify likely-affected files from the
outline alone (e.g. the symptom is infrastructure-level and the
relevant file isn't in this codebase), output an empty list and
explain in your reasoning. Synthesis will fall back to a broader scan.

Format your response exactly as:

## Root Cause Hypothesis
<one paragraph grounded in the specific log lines and metric values>

## Affected Files
- <path/from/outline.py>
- <path/from/outline.yaml>

## Confidence
<N>/5 — <one sentence explaining your certainty>

## Reasoning
<one paragraph explaining how the logs/metrics point to those files>

Confidence scale:
- 5: log lines and metric values unambiguously point to specific files
- 4: strong signal; one file confidently identified, others plausible
- 3: log/metric correlation is suggestive but not conclusive
- 2: thin evidence; best guess from path-name heuristics
- 1: outline alone is insufficient — recommend full-codebase fallback"""


@dataclass
class Investigation:
    root_cause: str
    affected_files: list[str]
    confidence: int          # 1-5; 0 if unparseable
    reasoning: str
    response_text: str       # full raw text for dashboard display
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


# ---------------------------------------------------------------------------
# Outline builder
# ---------------------------------------------------------------------------

def build_codebase_outline(root: Path) -> str:
    """Group every text file under `root` by its top-level directory and
    emit `path (N lines)`.  Stable cache prefix per service — the outline
    only changes when files are added/removed/significantly resized.
    """
    if not root.exists():
        return f"# Codebase outline at {root} — directory not found"

    # Bucket by top-level dir (or "." for files at the root).
    by_dir: dict[str, list[tuple[str, int]]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(p in _SKIP_DIRS for p in path.parts):
            continue
        if path.suffix not in _TEXT_SUFFIXES and path.name not in _TEXT_SUFFIXES:
            continue
        try:
            line_count = sum(1 for _ in path.open("r", errors="ignore"))
        except OSError:
            continue
        rel = path.relative_to(root)
        parts = rel.parts
        top = parts[0] if len(parts) > 1 else "."
        by_dir.setdefault(top, []).append((str(rel), line_count))

    lines = [f"# Codebase outline rooted at {root}", ""]
    for top in sorted(by_dir):
        label = "(root)" if top == "." else f"{top}/"
        lines.append(label)
        for path, count in sorted(by_dir[top]):
            lines.append(f"  {path} ({count} lines)")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_root_cause(text: str) -> str:
    m = re.search(
        r"##\s+Root Cause Hypothesis\s*\n(.*?)(?=\n##\s|\Z)",
        text, re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _parse_affected_files(text: str, *, valid_paths: set[str]) -> list[str]:
    """Pull the `## Affected Files` bullet list.

    Drops paths that don't match a real file under `valid_paths`. Silent
    drop is intentional — Sonnet hallucinating a path shouldn't crash
    the pipeline, and Phase C's empty-list fallback covers it.
    """
    m = re.search(
        r"##\s+Affected Files\s*\n(.*?)(?=\n##\s|\Z)",
        text, re.DOTALL,
    )
    if not m:
        return []
    section = m.group(1)
    files: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Strip bullet markers and inline code fences.
        line = re.sub(r"^[-*]\s*", "", line)
        line = line.strip("`").strip()
        if not line:
            continue
        if line in valid_paths and line not in files:
            files.append(line)
        else:
            log.debug("investigator: dropping unknown path %r", line)
        if len(files) >= _MAX_AFFECTED_FILES:
            break
    return files


def _parse_confidence(text: str) -> tuple[int, str]:
    m = re.search(
        r"##\s+Confidence\s*\n(.*?)(?=\n##\s|\Z)",
        text, re.DOTALL,
    )
    if not m:
        return 0, ""
    section = m.group(1).strip()
    score_match = re.search(r"([1-5])\s*/\s*5\s*[—\-–]\s*(.*)", section, re.DOTALL)
    if score_match:
        return int(score_match.group(1)), score_match.group(2).strip().splitlines()[0].strip()
    bare = re.search(r"\b([1-5])\b", section)
    if bare:
        return int(bare.group(1)), section.splitlines()[0].strip()
    return 0, section.splitlines()[0].strip() if section else ""


def _parse_reasoning(text: str) -> str:
    m = re.search(
        r"##\s+Reasoning\s*\n(.*?)(?=\n##\s|\Z)",
        text, re.DOTALL,
    )
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _format_chart_lineage_outline(lineage: tuple[ChartLayer, ...]) -> list[str]:
    """Tell the file-selection stage which chart layers it cannot see.

    This is the stage the lineage matters most at. The system prompt correctly
    forbids inventing paths, and the outline only covers one tree — so when the
    real cause sits in an invisible layer, the model picks the closest-looking
    file that IS in the outline. That wrong path then carries a confidence
    score into synthesis, which is worse than an abstention.

    Naming the missing layers gives it a third option: report that the cause is
    outside the visible tree and return few or no affected files.
    """
    invisible = [layer.name for layer in lineage if not layer.visible]
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
            "— in the outline below" if layer.visible
            else "— **NOT in the outline below**"
        )
        out.append(" ".join(bits))
    out.append("")
    if invisible:
        out += [
            "The outline you are given covers only the visible layer(s). "
            + ", ".join(f"`{n}`" for n in invisible)
            + " are NOT in it.",
            "",
            "If the most likely cause is a values key set in a layer you "
            "cannot see, do NOT substitute the closest-looking file from the "
            "outline. Say so in your root-cause hypothesis and return few or "
            "no affected files — naming the layer a human should look in is "
            "more useful than a confident wrong path.",
        ]
    else:
        out += [
            "Every layer is covered by the outline below.",
        ]
    return out


def _format_user_blob(
    ctx: AlertContext,
    *,
    triage_reasoning: str | None,
    chart_lineage: tuple[ChartLayer, ...] = (),
) -> str:
    a = ctx.alert
    lines: list[str] = []
    if chart_lineage:
        lines += _format_chart_lineage_outline(chart_lineage)
        lines += [""]
    if triage_reasoning:
        lines += [
            "# Pre-flight triage framing",
            "",
            "A cheaper model already determined this alert is worth "
            "investigating. Build on its framing rather than starting "
            "cold — its reasoning was:",
            "",
            f"> {triage_reasoning}",
            "",
        ]
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


def _valid_paths(root: Path) -> set[str]:
    """Set of relative paths Sonnet's affected_files list will be checked
    against."""
    if not root.exists():
        return set()
    out: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(p in _SKIP_DIRS for p in path.parts):
            continue
        if path.suffix not in _TEXT_SUFFIXES and path.name not in _TEXT_SUFFIXES:
            continue
        out.add(str(path.relative_to(root)))
    return out


# ---------------------------------------------------------------------------
# Investigator
# ---------------------------------------------------------------------------

class Investigator:
    def __init__(
        self,
        api_key: str,
        model: str,
        extended_cache_ttl: bool = True,
        *,
        client: AsyncAnthropic | None = None,
        prompt_resolver: PromptResolver | None = None,
    ):
        # ``client`` lets main.py inject a provider-built client (Anthropic
        # API or Bedrock). When None we build the direct-API client from
        # ``api_key`` — the historical default that keeps the unit tests
        # working unchanged. Note ``extended_cache_ttl`` still governs the
        # ``"ttl": "1h"`` cache marker below; main.py forces it False for
        # Bedrock (which doesn't support the extended-TTL beta).
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
            return self.prompt_resolver.resolve(STAGE_INVESTIGATION, stack=stack)
        return SYSTEM_PROMPT

    async def investigate(
        self,
        ctx: AlertContext,
        *,
        codebase_path: str,
        triage_reasoning: str | None = None,
        stack: str | None = None,
        chart_lineage: tuple[ChartLayer, ...] = (),
    ) -> Investigation:
        root = Path(codebase_path)
        outline = build_codebase_outline(root)
        # Volatile half of the prompt — the outline above it carries the
        # cache_control marker, so per-service framing must not go there.
        user_blob = _format_user_blob(
            ctx, triage_reasoning=triage_reasoning, chart_lineage=chart_lineage,
        )
        valid_paths = _valid_paths(root)
        system_prompt = self._system_prompt(stack)

        cache_control: dict[str, str] = {"type": "ephemeral"}
        if self.extended_cache_ttl:
            cache_control["ttl"] = "1h"

        messages_arg = [
            {
                "role": "user",
                "content": [
                    # Outline first — stable per service, cache it.
                    {
                        "type": "text",
                        "text": outline,
                        "cache_control": cache_control,
                    },
                    # Alert + logs + metrics + triage framing last —
                    # volatile per alert, no cache marker.
                    {"type": "text", "text": user_blob},
                ],
            }
        ]

        # Adaptive thinking on a reasoning task — but fail-open to a
        # no-thinking retry if the model rejects it. Haiku 4.5 did this
        # in Phase A; we'd rather degrade than 400 the whole pipeline.
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=system_prompt,
                thinking={"type": "adaptive"},
                messages=messages_arg,
            )
        except BadRequestError as e:
            msg = str(e).lower()
            if "thinking" in msg:
                log.warning(
                    "investigator: %s rejected adaptive thinking; retrying without",
                    self.model,
                )
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=messages_arg,
                )
            else:
                raise

        text = next((b.text for b in response.content if b.type == "text"), "")
        confidence, _ = _parse_confidence(text)
        u = response.usage
        return Investigation(
            root_cause=_parse_root_cause(text),
            affected_files=_parse_affected_files(text, valid_paths=valid_paths),
            confidence=confidence,
            reasoning=_parse_reasoning(text),
            response_text=text,
            model=response.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
