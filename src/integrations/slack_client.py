"""Post patch proposals to a Slack incoming webhook using Block Kit."""

import logging

import httpx

from src.agent.alert_handler import ParsedAlert
from src.agent.patch_generator import PatchProposal
from src.integrations.proposal_sections import (
    extract_section as _extract_section,
    is_empty_patch,
    parse_patch as _parse_patch,
    truncate as _truncate,
)

log = logging.getLogger("llopster.slack")

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
_CONFIDENCE_EMOJI = {5: "🟢", 4: "🟢", 3: "🟡", 2: "🔴", 1: "🔴", 0: "🔴"}

# Identifies this client to the notifier seam / status surfaces.
PROVIDER = "slack"


# ---------------------------------------------------------------------------
# Block Kit builder
# ---------------------------------------------------------------------------

def build_blocks(alert: ParsedAlert, proposal: PatchProposal, pr_url: str | None = None) -> list[dict]:
    """Return a Slack Block Kit payload for a patch proposal."""
    emoji = _SEVERITY_EMOJI.get(alert.severity.lower(), "⚪")
    root_cause = _extract_section(proposal.text, "Root Cause")
    patch = _parse_patch(proposal.text)
    reasoning = _extract_section(proposal.text, "Reasoning")

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {alert.alertname} — patch proposed",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if alert.summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{alert.summary}*"},
        })

    conf_emoji = _CONFIDENCE_EMOJI.get(proposal.confidence, "🔴")
    conf_label = f"{proposal.confidence}/5" if proposal.confidence else "N/A"
    blocks.append({
        "type": "section",
        "fields": [
            {
                "type": "mrkdwn",
                "text": f"*Confidence*\n{conf_emoji} {conf_label}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Service*\n{alert.service}",
            },
        ],
    })

    if proposal.confidence_reason:
        blocks.append({
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"_{proposal.confidence_reason}_",
            }],
        })

    if root_cause:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Root Cause*\n{_truncate(root_cause)}",
            },
        })

    if is_empty_patch(patch):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_No patch needed for this alert._"},
        })
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Proposed Patch*\n```{_truncate(patch)}```",
            },
        })

    if reasoning:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Reasoning*\n{_truncate(reasoning)}",
            },
        })

    if pr_url:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "View Pull Request", "emoji": True},
                "url": pr_url,
                "style": "primary",
            }],
        })

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                f"model: `{proposal.model}` · "
                f"in: {proposal.input_tokens:,} tok · "
                f"out: {proposal.output_tokens:,} tok · "
                f"cache hit: {proposal.cache_read_tokens:,} tok"
            ),
        }],
    })

    return blocks


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SlackClient:
    #: Provider id used by the notifier seam + dashboard status surfaces.
    provider = PROVIDER

    def __init__(self, webhook_url: str, client: httpx.AsyncClient):
        self.webhook_url = webhook_url
        self._client = client

    async def post_patch(self, alert: ParsedAlert, proposal: PatchProposal, pr_url: str | None = None) -> None:
        blocks = build_blocks(alert, proposal, pr_url=pr_url)
        resp = await self._client.post(self.webhook_url, json={"blocks": blocks})
        resp.raise_for_status()
        log.info("slack notification sent for %s", alert.alertname)
