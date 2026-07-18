"""Cheap pre-flight classifier that decides whether an alert is worth
the full diagnostic pipeline.

Runs BEFORE Loki/Prom collection and BEFORE the Opus synthesis call,
so noise alerts cost ~$0.001 (one short Haiku call) instead of a full
Opus invocation with cached codebase + collected context.

The gate is intentionally additive: a skip decision marks the Run row
with reason + confidence, the dashboard surfaces it, and operators can
either flip ``triage_enabled=false`` to disable the gate entirely or
raise ``triage_min_confidence`` so the gate has to be more sure before
it skips anything. Misclassifications stay visible — they never just
vanish.

Output is parsed from the same ``## Section`` header format used by
the patch generator so the SDK surface and parser logic stay symmetric
across stages.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from src.agent.alert_handler import ParsedAlert
from src.agent.prompts import STAGE_TRIAGE, PromptResolver

log = logging.getLogger("llopster.triage")


SYSTEM_PROMPT = """You are an SRE triage gate at the top of an alert response pipeline.

For each alert, decide whether it justifies running the full diagnostic
pipeline (which fetches logs, fetches metrics, sends the codebase to a
larger model, and proposes a code patch). Most alerts are worth running.
Only skip alerts where:

- The alert is purely informational (no action would ever fix it).
- Annotations explicitly say "no action required" / "informational only".
- The alert IS the monitoring/alerting pipeline's own watchdog (e.g.
  AlertManager's "Watchdog" rule, which fires continuously by design to
  prove the alerting pipeline itself is working) — not a signal that
  anything in a monitored application is wrong.
- The alert is a near-duplicate of a recently-seen one whose label-set
  differs only in unstable fields (e.g. pod hash, timestamp); the
  upstream dedup may not catch these.

Do not skip an alert just because its name or summary contains the word
"heartbeat" — an application's own heartbeat/liveness signal going stale
or missing (e.g. a scheduler or worker that stopped checking in) is
itself a real, fixable incident, not a watchdog ping. Only skip
heartbeat-named alerts when they are explicitly the alerting pipeline's
own watchdog (see the bullet above), not an application's internal
liveness check.

When in doubt, decide PROCEED. False-skip is worse than false-proceed:
a false-skip silently drops a real incident, while a false-proceed only
costs Opus tokens (and the synthesis step will recognise non-actionable
alerts on its own and emit the no-patch sentinel).

Format your response exactly as:

## Decision
<proceed | skip>

## Confidence
<N>/5 — <one sentence explaining your certainty>

## Reasoning
<one short paragraph>

Confidence scale:
- 5: unambiguous; the rule for skip/proceed is met cleanly
- 4: high confidence; one minor consideration on the other side
- 3: plausible decision but evidence is thin
- 2: best guess; could go either way
- 1: insufficient context — default to PROCEED if you choose this

If you ever feel forced into confidence 1 or 2 for a SKIP decision,
output PROCEED instead. The downstream operator can disable the gate or
raise the bar if Haiku is being too aggressive."""


@dataclass
class TriageDecision:
    proceed: bool
    confidence: int          # 1-5; 0 if unparseable
    reasoning: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def decision_label(self) -> str:
        return "proceed" if self.proceed else "skip"


def _parse_decision(text: str) -> tuple[bool, int, str]:
    """Pull (proceed, confidence, reasoning) out of the model response.

    Fail-safe: any parse failure returns ``(proceed=True, confidence=0)``
    so a malformed Haiku response can never silently drop a real alert.
    """
    decision_match = re.search(
        r"##\s+Decision\s*\n\s*(proceed|skip)\b",
        text, re.IGNORECASE,
    )
    if decision_match is None:
        return True, 0, "decision section missing from triage response"
    proceed = decision_match.group(1).lower() == "proceed"

    confidence = 0
    conf_match = re.search(
        r"##\s+Confidence\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL,
    )
    if conf_match:
        score_match = re.search(r"([1-5])\s*/\s*5", conf_match.group(1))
        if score_match:
            confidence = int(score_match.group(1))

    reasoning_match = re.search(
        r"##\s+Reasoning\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL,
    )
    reasoning = (
        reasoning_match.group(1).strip() if reasoning_match else ""
    )
    if not reasoning and conf_match:
        # Fall back to the line after "N/5 — " if no separate ## Reasoning.
        tail = re.search(r"[1-5]\s*/\s*5\s*[—\-–]\s*(.*)", conf_match.group(1))
        if tail:
            reasoning = tail.group(1).strip()
    return proceed, confidence, reasoning


def _format_alert(alert: ParsedAlert, *, service_known: bool) -> str:
    parts = [
        "# Alert under triage",
        "",
        f"**Alertname:** {alert.alertname}",
        f"**Severity:** {alert.severity or 'unknown'}",
        f"**Service:** {alert.service or 'unknown'}"
        + (" (registered in services.yaml)" if service_known else " (NOT in services.yaml)"),
        f"**Summary:** {alert.summary or '—'}",
        f"**Description:** {alert.description or '—'}",
        "",
        "## Labels",
    ]
    parts += [f"- {k}: {v}" for k, v in sorted(alert.labels.items())]
    parts += ["", "## Annotations"]
    parts += [f"- {k}: {v}" for k, v in sorted(alert.annotations.items())]
    return "\n".join(parts)


class Triage:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        prompt_resolver: PromptResolver | None = None,
    ):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        # Optional pack-aware prompt seam. None → use the baked-in
        # Community SYSTEM_PROMPT (existing behavior, used by unit tests).
        self.prompt_resolver = prompt_resolver

    def _system_prompt(self, stack: str | None) -> str:
        if self.prompt_resolver is not None:
            return self.prompt_resolver.resolve(STAGE_TRIAGE, stack=stack)
        return SYSTEM_PROMPT

    async def evaluate(
        self,
        alert: ParsedAlert,
        *,
        service_known: bool,
        stack: str | None = None,
    ) -> TriageDecision:
        user_blob = _format_alert(alert, service_known=service_known)
        # NOTE: no `thinking` arg — Haiku 4.5 rejects adaptive thinking with
        # "adaptive thinking is not supported on this model". Triage is a
        # yes/no classification; reasoning is captured in the prose output,
        # not in a thinking block. If a future operator points
        # ANTHROPIC_TRIAGE_MODEL at Sonnet/Opus this still works — those
        # models simply don't get the extended-thinking boost on triage,
        # which is fine for a binary decision.
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=self._system_prompt(stack),
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_blob}],
                }
            ],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        proceed, confidence, reasoning = _parse_decision(text)
        u = response.usage
        return TriageDecision(
            proceed=proceed,
            confidence=confidence,
            reasoning=reasoning or "(no reasoning provided)",
            model=response.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
