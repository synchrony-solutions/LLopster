"""Tests for the Microsoft Teams client — no live HTTP calls."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.patch_generator import PatchProposal
from src.integrations.teams_client import (
    ADAPTIVE_CONTENT_TYPE,
    TeamsClient,
    build_adaptive_card,
    build_text_card,
    message_envelope,
)

SAMPLE_PROPOSAL_TEXT = """\
## Root Cause
The `helm-values.yaml` declares `memory: "512MBz"` which is invalid.

## Proposed Patch
```diff
--- a/helm-values.yaml
+++ b/helm-values.yaml
@@ -5,1 +5,1 @@
-    memory: "512MBz"
+    memory: "512Mi"
```

## Confidence
5/5 — unambiguous.

## Reasoning
Replacing `512MBz` with `512Mi` matches the validator regex.
"""

NO_PATCH_TEXT = """\
## Root Cause
Synthetic always-firing alert with no real fault.

## Proposed Patch
No code patch is appropriate for this alert.

```diff
(no changes)
```

## Confidence
1/5 — synthetic.

## Reasoning
Intentional plumbing to verify the pipeline.
"""


def _make_alert(severity: str = "warning") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="abc",
        status="firing",
        alertname="HelmValuesMisconfigured",
        severity=severity,
        service="demo-app",
        summary="Helm values misconfigured",
        description="bad memory unit",
        starts_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": "demo-app"},
        annotations={},
        generator_url="",
    )


def _make_proposal(text: str = SAMPLE_PROPOSAL_TEXT, confidence: int = 5) -> PatchProposal:
    return PatchProposal(
        text=text,
        model="claude-opus-4-7",
        input_tokens=11006,
        output_tokens=672,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        confidence=confidence,
        confidence_reason="Root cause is unambiguous from the logs.",
    )


def _all_text(card: dict) -> str:
    """Concatenate every TextBlock/FactSet string in a card body for
    substring assertions."""
    parts: list[str] = []
    for block in card["body"]:
        if block.get("type") == "TextBlock":
            parts.append(block["text"])
        elif block.get("type") == "FactSet":
            for f in block["facts"]:
                parts.append(f"{f['title']}:{f['value']}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Adaptive Card builder
# ---------------------------------------------------------------------------

def test_card_is_well_formed_adaptive_card():
    card = build_adaptive_card(_make_alert(), _make_proposal())
    assert card["type"] == "AdaptiveCard"
    assert card["version"]
    assert isinstance(card["body"], list) and card["body"]


def test_card_includes_core_content():
    card = build_adaptive_card(_make_alert(), _make_proposal())
    text = _all_text(card)
    assert "HelmValuesMisconfigured" in text
    assert "512Mi" in text            # the patch diff
    assert "Confidence" in text and "5/5" in text
    assert "demo-app" in text          # service fact


def test_card_pr_url_becomes_openurl_action():
    card = build_adaptive_card(_make_alert(), _make_proposal(), pr_url="https://github.com/o/r/pull/7")
    actions = card.get("actions", [])
    assert any(
        a["type"] == "Action.OpenUrl" and a["url"] == "https://github.com/o/r/pull/7"
        for a in actions
    )


def test_card_without_pr_url_has_no_actions():
    card = build_adaptive_card(_make_alert(), _make_proposal(), pr_url=None)
    assert "actions" not in card


def test_card_severity_color():
    crit = build_adaptive_card(_make_alert("critical"), _make_proposal())
    assert crit["body"][0]["color"] == "Attention"
    warn = build_adaptive_card(_make_alert("warning"), _make_proposal())
    assert warn["body"][0]["color"] == "Warning"


def test_card_no_patch_message():
    card = build_adaptive_card(_make_alert(), _make_proposal(NO_PATCH_TEXT, confidence=1))
    assert "No patch needed" in _all_text(card)


def test_message_envelope_shape():
    card = build_adaptive_card(_make_alert(), _make_proposal())
    env = message_envelope(card)
    assert env["type"] == "message"
    assert env["attachments"][0]["contentType"] == ADAPTIVE_CONTENT_TYPE
    assert env["attachments"][0]["content"] is card


def test_build_text_card():
    card = build_text_card("llopster connection test ✓")
    assert card["type"] == "AdaptiveCard"
    assert "llopster connection test" in _all_text(card)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_patch_sends_adaptive_card_envelope():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    client = TeamsClient(webhook_url="https://prod-1.westus.logic.azure.com/workflows/x", client=mock_http)
    await client.post_patch(_make_alert(), _make_proposal(), pr_url="https://github.com/o/r/pull/7")

    mock_http.post.assert_awaited_once()
    call = mock_http.post.call_args
    url = call.args[0] if call.args else call.kwargs["url"]
    assert url.startswith("https://prod-1.westus.logic.azure.com/")
    payload = call.kwargs["json"]
    assert payload["type"] == "message"
    assert payload["attachments"][0]["contentType"] == ADAPTIVE_CONTENT_TYPE
    mock_response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_post_patch_raises_on_http_error():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("400", request=MagicMock(), response=MagicMock())
    )
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    client = TeamsClient(webhook_url="https://prod-1.westus.logic.azure.com/workflows/x", client=mock_http)
    with pytest.raises(httpx.HTTPStatusError):
        await client.post_patch(_make_alert(), _make_proposal())


def test_provider_id():
    assert TeamsClient.provider == "teams"
