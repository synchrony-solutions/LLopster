"""Jinja2 environment + Markdown / diff rendering filters for the dashboard.

Identical logic to src/api/templating.py but points at the dashboard's own
template directory (src/dashboard/templates/) so the two services are
completely independent deployments — no shared filesystem paths.
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
            return ""
    formatter = HtmlFormatter(nowrap=True, cssclass="hl")
    return f'<pre class="hl hl-{language}"><code>{highlight(code, lexer, formatter)}</code></pre>'


def _fence_renderer(self, tokens, idx, options, env):  # type: ignore[no-untyped-def]
    token = tokens[idx]
    language = (token.info or "").strip().split()[0] if token.info else ""
    rendered = _highlight_block(token.content, language) if language else ""
    if rendered:
        return rendered
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
    if not text:
        return None
    m = re.search(r"##\s+Reasoning\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else None


def status_class(status: str) -> str:
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


def is_terminal_status(status: str) -> bool:
    return status in {"done", "skipped", "failed"}


# Operator ground-truth label display metadata: value → (display text, pill class).
# Keyed by the canonical values in models.OPERATOR_LABELS. Reuses the confidence
# pill palette so "correct" reads green, "wrong" red, "partial" amber.
_OPERATOR_LABEL_META = {
    "correct": ("Correct", "pill-confidence-high"),
    "wrong": ("Wrong", "pill-confidence-low"),
    "partial": ("Partial", "pill-confidence-mid"),
    "na": ("N/A", "pill-skipped"),
}


def operator_label_text(label: str | None) -> str:
    if not label:
        return ""
    return _OPERATOR_LABEL_META.get(label, (label, ""))[0]


def operator_label_class(label: str | None) -> str:
    return "pill " + _OPERATOR_LABEL_META.get(label or "", ("", ""))[1]


def operator_label_choices() -> list[tuple[str, str]]:
    """(value, display-text) pairs in canonical order for the label buttons."""
    from src.db.models import OPERATOR_LABELS

    return [(v, _OPERATOR_LABEL_META[v][0]) for v in OPERATOR_LABELS]


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
templates.env.globals["is_terminal_status"] = is_terminal_status
templates.env.globals["operator_label_text"] = operator_label_text
templates.env.globals["operator_label_class"] = operator_label_class
templates.env.globals["operator_label_choices"] = operator_label_choices


def get_pygments_css() -> str:
    return HtmlFormatter(cssclass="hl").get_style_defs(".hl")


__all__ = ["templates", "get_pygments_css"]
