"""Post patch proposals to Microsoft Teams as an Adaptive Card.

Targets the **Power Automate "Workflows"** incoming-webhook path (the
supported successor to the retiring Office 365 "Incoming Webhook" connector).
That endpoint expects a ``message`` envelope wrapping an Adaptive Card:

    {"type": "message",
     "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                      "content": {<adaptive card>}}]}

The card renders the same content as the Slack Block Kit message (header,
summary, confidence/service facts, root cause, patch, reasoning, a "View Pull
Request" action, and a subtle model/token footer) so both channels stay in
sync — the shared section parsing lives in ``proposal_sections``.
"""

import logging

import httpx

from src.agent.alert_handler import ParsedAlert
from src.agent.patch_generator import PatchProposal
from src.integrations.proposal_sections import (
    extract_section,
    is_empty_patch,
    parse_patch,
    truncate,
)

log = logging.getLogger("llopster.teams")

# Identifies this client to the notifier seam / status surfaces.
PROVIDER = "teams"

ADAPTIVE_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
# Adaptive Card schema version. 1.4 is broadly supported in Teams and covers
# FactSet + Monospace fontType + Action.OpenUrl, all of which we use.
_CARD_VERSION = "1.4"

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
# Adaptive Card text colors (a fixed enum): map alert severity to one.
_SEVERITY_COLOR = {"critical": "Attention", "warning": "Warning", "info": "Accent"}
_CONFIDENCE_EMOJI = {5: "🟢", 4: "🟢", 3: "🟡", 2: "🔴", 1: "🔴", 0: "🔴"}


# ---------------------------------------------------------------------------
# Adaptive Card builder
# ---------------------------------------------------------------------------

def _text(text: str, **kwargs) -> dict:
    block = {"type": "TextBlock", "text": text, "wrap": True}
    block.update(kwargs)
    return block


def build_adaptive_card(
    alert: ParsedAlert, proposal: PatchProposal, pr_url: str | None = None
) -> dict:
    """Return the Adaptive Card ``content`` dict for a patch proposal."""
    emoji = _SEVERITY_EMOJI.get(alert.severity.lower(), "⚪")
    color = _SEVERITY_COLOR.get(alert.severity.lower(), "Default")
    root_cause = extract_section(proposal.text, "Root Cause")
    patch = parse_patch(proposal.text)
    reasoning = extract_section(proposal.text, "Reasoning")

    body: list[dict] = [
        _text(
            f"{emoji} {alert.alertname} — patch proposed",
            weight="Bolder", size="Large", color=color,
        ),
    ]

    if alert.summary:
        body.append(_text(alert.summary, weight="Bolder"))

    conf_emoji = _CONFIDENCE_EMOJI.get(proposal.confidence, "🔴")
    conf_label = f"{proposal.confidence}/5" if proposal.confidence else "N/A"
    body.append({
        "type": "FactSet",
        "facts": [
            {"title": "Confidence", "value": f"{conf_emoji} {conf_label}"},
            {"title": "Service", "value": alert.service or "—"},
        ],
    })

    if proposal.confidence_reason:
        body.append(_text(proposal.confidence_reason, isSubtle=True))

    if root_cause:
        body.append(_text("**Root Cause**", weight="Bolder"))
        body.append(_text(truncate(root_cause)))

    if is_empty_patch(patch):
        body.append(_text("_No patch needed for this alert._", isSubtle=True))
    else:
        body.append(_text("**Proposed Patch**", weight="Bolder"))
        # Monospace keeps the diff legible; wrap=True so long lines don't clip.
        body.append(_text(truncate(patch), fontType="Monospace"))

    if reasoning:
        body.append(_text("**Reasoning**", weight="Bolder"))
        body.append(_text(truncate(reasoning)))

    body.append(_text(
        f"model: {proposal.model} · "
        f"in: {proposal.input_tokens:,} tok · "
        f"out: {proposal.output_tokens:,} tok · "
        f"cache hit: {proposal.cache_read_tokens:,} tok",
        isSubtle=True, size="Small",
    ))

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": _CARD_VERSION,
        "body": body,
    }
    if pr_url:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "View Pull Request", "url": pr_url}
        ]
    return card


def build_text_card(message: str) -> dict:
    """A minimal Adaptive Card carrying a single line — used for the
    dashboard's 'Test connection' ping."""
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": _CARD_VERSION,
        "body": [_text(message)],
    }


def message_envelope(card: dict) -> dict:
    """Wrap an Adaptive Card in the Teams Workflows ``message`` envelope."""
    return {
        "type": "message",
        "attachments": [{"contentType": ADAPTIVE_CONTENT_TYPE, "content": card}],
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TeamsClient:
    #: Provider id used by the notifier seam + dashboard status surfaces.
    provider = PROVIDER

    def __init__(self, webhook_url: str, client: httpx.AsyncClient):
        self.webhook_url = webhook_url
        self._client = client

    async def post_patch(
        self, alert: ParsedAlert, proposal: PatchProposal, pr_url: str | None = None
    ) -> None:
        card = build_adaptive_card(alert, proposal, pr_url=pr_url)
        # Workflows returns 202 Accepted on success — a 2xx, so raise_for_status
        # passes; a bad URL / disabled flow surfaces as 4xx/5xx.
        resp = await self._client.post(self.webhook_url, json=message_envelope(card))
        resp.raise_for_status()
        log.info("teams notification sent for %s", alert.alertname)
