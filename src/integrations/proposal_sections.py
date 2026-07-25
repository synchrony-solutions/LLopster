"""Shared parsing of the Opus synthesis response into named sections.

The synthesis prompt emits ``## Root Cause / ## Proposed Patch / ## Reasoning``
markdown. Both notifier clients (Slack Block Kit, Teams Adaptive Card) render
the same three sections, so the extraction lives here once rather than being
duplicated per client — a drift here would silently make one channel show
different content than the other.
"""

from __future__ import annotations

import re

# Slack's hard per-block limit is 3000 chars; Teams caps the whole Adaptive
# Card payload (~28KB). A per-field cap of 2900 leaves headroom on both.
DEFAULT_TEXT_LIMIT = 2900


def extract_section(text: str, heading: str) -> str:
    """Pull the body of a ``## Heading`` section out of the LLM response."""
    m = re.search(
        rf"##\s+{re.escape(heading)}\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL
    )
    return m.group(1).strip() if m else ""


def parse_patch(text: str) -> str:
    """Return the raw diff lines, stripping ```diff … ``` fences if present."""
    raw = extract_section(text, "Proposed Patch")
    fence = re.search(r"```(?:diff)?\s*\n?(.*?)```", raw, re.DOTALL)
    return fence.group(1).strip() if fence else raw


def is_empty_patch(patch: str) -> bool:
    """True when the proposal contains no actionable diff."""
    return not patch or "(no changes)" in patch or "No code patch" in patch


def truncate(text: str, limit: int = DEFAULT_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "…"
