"""Jinja2 environment + Markdown / diff rendering filters.

Centralizes the templating setup so `web_routes` can stay thin. Exposes a
single configured `Jinja2Templates` instance with custom filters:

    | markdown   — render Markdown to HTML, with `diff` blocks syntax-highlighted
    | diff       — render a raw unified-diff string to highlighted HTML
    | duration   — humanize a millisecond count
    | dt         — format a datetime for display
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import DiffLexer, get_lexer_by_name
from pygments.util import ClassNotFound


_TEMPLATE_DIR = Path(__file__).parent / "templates"

_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})


def _highlight_block(code: str, language: str) -> str:
    if language == "diff":
        lexer = DiffLexer()
    else:
        try:
            lexer = get_lexer_by_name(language)
        except ClassNotFound:
            return ""  # let markdown-it fall back to default rendering
    formatter = HtmlFormatter(nowrap=True, cssclass="hl")
    return f'<pre class="hl hl-{language}"><code>{highlight(code, lexer, formatter)}</code></pre>'


# Override fence rendering to call into pygments for syntax-highlighted output.
def _fence_renderer(self, tokens, idx, options, env):  # type: ignore[no-untyped-def]
    token = tokens[idx]
    language = (token.info or "").strip().split()[0] if token.info else ""
    rendered = _highlight_block(token.content, language) if language else ""
    if rendered:
        return rendered
    # Fall through to default
    return f'<pre><code class="language-{language}">{self.renderInlineAsText(tokens, idx, options) or token.content}</code></pre>'


_md.add_render_rule("fence", _fence_renderer)


def render_markdown(text: str | None) -> Markup:
    if not text:
        return Markup("")
    return Markup(_md.render(text))


def render_diff(text: str | None) -> Markup:
    if not text:
        return Markup("")
    return Markup(_highlight_block(text, "diff"))


def humanize_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f} s"
    m = s / 60.0
    return f"{m:.1f} min"


def format_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_reasoning(text: str | None) -> str | None:
    """Pull the ## Reasoning section out of the LLM response (not stored as a
    column; extracted at render time)."""
    if not text:
        return None
    m = re.search(r"##\s+Reasoning\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else None


def status_class(status: str) -> str:
    """Map a processing_status to a CSS class for styling pills."""
    return {
        "pending": "pill pill-pending",
        "queued": "pill pill-queued",
        "collecting": "pill pill-active",
        "generating": "pill pill-active",
        "posting": "pill pill-active",
        "done": "pill pill-done",
        "skipped": "pill pill-skipped",
        "failed": "pill pill-failed",
    }.get(status, "pill")


def severity_class(severity: str | None) -> str:
    return {
        "critical": "pill pill-critical",
        "warning": "pill pill-warning",
        "info": "pill pill-info",
    }.get(severity or "", "pill")


def confidence_class(score: int | None) -> str:
    if score is None:
        return "pill"
    if score >= 4:
        return "pill pill-confidence-high"
    if score >= 3:
        return "pill pill-confidence-mid"
    return "pill pill-confidence-low"


# ---------------------------------------------------------------------------
# Jinja2Templates with filters wired in
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.filters["markdown"] = render_markdown
templates.env.filters["diff"] = render_diff
templates.env.filters["duration"] = humanize_duration
templates.env.filters["dt"] = format_dt
templates.env.globals["parse_reasoning"] = parse_reasoning
templates.env.globals["status_class"] = status_class
templates.env.globals["severity_class"] = severity_class
templates.env.globals["confidence_class"] = confidence_class


def is_terminal_status(status: str) -> bool:
    """Returns True when no further status updates are expected. Templates
    use this to decide whether to attach an HTMX poll trigger."""
    return status in {"done", "skipped", "failed"}


templates.env.globals["is_terminal_status"] = is_terminal_status


def get_pygments_css() -> str:
    """Return the CSS for the pygments default theme. Served statically once
    at build time rather than per-request."""
    return HtmlFormatter(cssclass="hl").get_style_defs(".hl")


__all__ = ["templates", "get_pygments_css"]
