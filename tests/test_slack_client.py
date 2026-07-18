"""Tests for the Slack client — no live HTTP calls."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.patch_generator import PatchProposal
from src.integrations.slack_client import SlackClient, _parse_patch, _extract_section, build_blocks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
5/5 — Root cause is unambiguous from the logs and the fix is a one-character correction.

## Reasoning
Replacing `512MBz` with `512Mi` matches the validator regex and stops the counter.
"""

NO_PATCH_TEXT = """\
## Root Cause
This is a synthetic always-firing alert with no real fault.

## Proposed Patch
No code patch is appropriate for this alert.

```diff
(no changes)
```

## Confidence
1/5 — Synthetic alert with no actionable root cause in the codebase.

## Reasoning
The alert is intentional infrastructure plumbing to verify the pipeline.
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


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def test_extract_section_root_cause():
    text = _extract_section(SAMPLE_PROPOSAL_TEXT, "Root Cause")
    assert "512MBz" in text
    assert "## Proposed Patch" not in text


def test_extract_section_missing():
    assert _extract_section("no sections here", "Root Cause") == ""


def test_parse_patch_strips_fences():
    patch = _parse_patch(SAMPLE_PROPOSAL_TEXT)
    assert patch.startswith("---")
    assert "```" not in patch


def test_parse_patch_no_changes():
    patch = _parse_patch(NO_PATCH_TEXT)
    assert "(no changes)" in patch


# ---------------------------------------------------------------------------
# Block Kit builder
# ---------------------------------------------------------------------------

def test_build_blocks_contains_patch():
    blocks = build_blocks(_make_alert(), _make_proposal())
    texts = [
        b.get("text", {}).get("text", "")
        for b in blocks
        if b["type"] == "section"
    ]
    combined = "\n".join(texts)
    assert "Root Cause" in combined
    assert "512MBz" in combined
    assert "512Mi" in combined
    assert "Reasoning" in combined


def test_build_blocks_no_patch_message():
    blocks = build_blocks(_make_alert(), _make_proposal(NO_PATCH_TEXT))
    texts = [
        b.get("text", {}).get("text", "")
        for b in blocks
        if b["type"] == "section"
    ]
    assert any("No patch needed" in t for t in texts)


def test_build_blocks_severity_emoji_warning():
    blocks = build_blocks(_make_alert("warning"), _make_proposal())
    header = next(b for b in blocks if b["type"] == "header")
    assert "🟡" in header["text"]["text"]


def test_build_blocks_severity_emoji_critical():
    blocks = build_blocks(_make_alert("critical"), _make_proposal())
    header = next(b for b in blocks if b["type"] == "header")
    assert "🔴" in header["text"]["text"]


def test_build_blocks_context_footer():
    blocks = build_blocks(_make_alert(), _make_proposal())
    # The token-usage footer is the last context block
    ctx_blocks = [b for b in blocks if b["type"] == "context"]
    footer_text = ctx_blocks[-1]["elements"][0]["text"]
    assert "claude-opus-4-7" in footer_text
    assert "11,006" in footer_text


def test_build_blocks_confidence_shown():
    blocks = build_blocks(_make_alert(), _make_proposal(confidence=5))
    section_texts = [
        b.get("fields", []) for b in blocks if b["type"] == "section"
    ]
    fields = [f["text"] for fields in section_texts for f in fields]
    assert any("5/5" in f for f in fields)
    assert any("🟢" in f for f in fields)


def test_build_blocks_low_confidence_shown():
    blocks = build_blocks(_make_alert(), _make_proposal(confidence=2))
    section_texts = [b.get("fields", []) for b in blocks if b["type"] == "section"]
    fields = [f["text"] for fields in section_texts for f in fields]
    assert any("2/5" in f for f in fields)
    assert any("🔴" in f for f in fields)


def test_build_blocks_pr_button_present_when_url_given():
    blocks = build_blocks(_make_alert(), _make_proposal(), pr_url="https://github.com/owner/repo/pull/42")
    actions = next((b for b in blocks if b["type"] == "actions"), None)
    assert actions is not None
    assert actions["elements"][0]["url"] == "https://github.com/owner/repo/pull/42"
    assert actions["elements"][0]["style"] == "primary"


def test_build_blocks_no_pr_button_when_url_absent():
    blocks = build_blocks(_make_alert(), _make_proposal(), pr_url=None)
    assert not any(b["type"] == "actions" for b in blocks)


# ---------------------------------------------------------------------------
# SlackClient HTTP behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_patch_sends_blocks():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    client = SlackClient(webhook_url="https://hooks.slack.com/test", client=mock_http)
    await client.post_patch(_make_alert(), _make_proposal())

    mock_http.post.assert_awaited_once()
    call_kwargs = mock_http.post.call_args
    url = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs["url"]
    assert url == "https://hooks.slack.com/test"
    payload = call_kwargs.kwargs["json"]
    assert "blocks" in payload
    assert isinstance(payload["blocks"], list)
    mock_response.raise_for_status.assert_called_once()


@pytest.mark.asyncio
async def test_post_patch_raises_on_http_error():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "400", request=MagicMock(), response=MagicMock()
        )
    )
    mock_http = AsyncMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    client = SlackClient(webhook_url="https://hooks.slack.com/test", client=mock_http)
    with pytest.raises(httpx.HTTPStatusError):
        await client.post_patch(_make_alert(), _make_proposal())
