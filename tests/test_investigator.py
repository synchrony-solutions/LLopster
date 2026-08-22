"""Unit tests for the Sonnet investigation stage.

Mocks the Anthropic client; no live API calls. Focus areas:
  - Outline builder shape (paths + line counts, grouped by top-level
    dir, skips the same dirs as PatchGenerator)
  - Affected-files parser ignores hallucinated paths silently and
    caps at the anti-abuse limit
  - Triage reasoning makes it into the user blob
  - Adaptive-thinking 400 fail-over to a no-thinking retry
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import BadRequestError

from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import AlertContext
from src.agent.investigator import (
    Investigation,
    Investigator,
    SYSTEM_PROMPT,
    _MAX_AFFECTED_FILES,
    _format_user_blob,
    _parse_affected_files,
    _parse_confidence,
    _parse_reasoning,
    _parse_root_cause,
    build_codebase_outline,
)
from src.integrations.loki_client import LogLine
from src.integrations.prometheus_client import MetricSample
from src.services_registry import ChartLayer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_ctx(alertname: str = "HelmValuesMisconfigured") -> AlertContext:
    alert = ParsedAlert(
        fingerprint="abc", status="firing",
        alertname=alertname, severity="warning", service="demo-app",
        summary="bad helm values", description="memory unit invalid",
        starts_at=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": "demo-app", "severity": "warning"},
        annotations={"runbook": "https://example.com/rb"},
        generator_url="",
    )
    return AlertContext(
        alert=alert,
        log_lines=[
            LogLine(
                timestamp=datetime(2026, 4, 18, 11, 59, tzinfo=timezone.utc),
                line="invalid memory unit: 512MBz",
                labels={"service": "demo-app"},
            )
        ],
        metric_samples=[
            MetricSample(
                metric={"__name__": "demo_app_config_errors_total"},
                value=3.0,
                timestamp=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
            )
        ],
        queries_used={"logql": '{service="demo-app"}', "promql": "vector(1)"},
        errors=[],
    )


SAMPLE_RESPONSE = """\
## Root Cause Hypothesis
The helm values memory unit is invalid: "512MBz".

## Affected Files
- helm-values.yaml
- check_helm_values.py

## Confidence
4/5 — Log line names the file and the symptom.

## Reasoning
The error message is emitted by the helm values parser.
"""


# ---------------------------------------------------------------------------
# Outline builder
# ---------------------------------------------------------------------------

def test_outline_groups_by_top_level_dir(tmp_path):
    (tmp_path / "main.py").write_text("a = 1\nb = 2\nc = 3\n")
    (tmp_path / "demo-app").mkdir()
    (tmp_path / "demo-app" / "check_helm.py").write_text("x = 1\n")
    (tmp_path / "demo-app" / "values.yaml").write_text("memory: 512Mi\nreplicas: 3\n")
    # Skip dirs must be filtered out the same way patch_generator does it.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.pyc").write_bytes(b"\x00\x00")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")

    outline = build_codebase_outline(tmp_path)
    assert "demo-app/" in outline
    assert "(root)" in outline  # main.py lives at the root
    assert "main.py (3 lines)" in outline
    assert "demo-app/check_helm.py (1 lines)" in outline
    assert "demo-app/values.yaml (2 lines)" in outline
    # Skip dirs + binaries must not appear.
    assert "__pycache__" not in outline
    assert "image.png" not in outline


def test_outline_missing_root(tmp_path):
    outline = build_codebase_outline(tmp_path / "nope")
    assert "not found" in outline


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_root_cause_extracts_paragraph():
    assert "invalid" in _parse_root_cause(SAMPLE_RESPONSE)


def test_parse_confidence_standard():
    score, reason = _parse_confidence(SAMPLE_RESPONSE)
    assert score == 4
    assert "Log line" in reason


def test_parse_confidence_missing_section():
    assert _parse_confidence("## Root Cause Hypothesis\nfoo") == (0, "")


def test_parse_reasoning_extracts_paragraph():
    assert "parser" in _parse_reasoning(SAMPLE_RESPONSE)


def test_parse_affected_files_filters_unknown_paths():
    """The whole point of validating against the real codebase: Sonnet
    sometimes invents plausible-looking paths. Those must be dropped
    silently so Phase C's narrowing doesn't try to read nonexistent
    files."""
    valid = {"helm-values.yaml", "check_helm_values.py"}
    files = _parse_affected_files(SAMPLE_RESPONSE, valid_paths=valid)
    assert files == ["helm-values.yaml", "check_helm_values.py"]


def test_parse_affected_files_drops_hallucinated_paths():
    text = (
        "## Affected Files\n"
        "- helm-values.yaml\n"
        "- this/path/does/not/exist.py\n"
        "- check_helm_values.py\n"
    )
    files = _parse_affected_files(text, valid_paths={
        "helm-values.yaml", "check_helm_values.py",
    })
    assert files == ["helm-values.yaml", "check_helm_values.py"]


def test_parse_affected_files_caps_at_anti_abuse_limit():
    """Sonnet listing 100 'affected' files would defeat Phase C's whole
    purpose. We cap the list."""
    valid = {f"file{i}.py" for i in range(50)}
    lines = ["## Affected Files"] + [f"- file{i}.py" for i in range(50)]
    files = _parse_affected_files("\n".join(lines), valid_paths=valid)
    assert len(files) == _MAX_AFFECTED_FILES


def test_parse_affected_files_handles_backticks_and_dedup():
    text = (
        "## Affected Files\n"
        "- `main.py`\n"
        "- main.py\n"
        "* values.yaml\n"
    )
    files = _parse_affected_files(text, valid_paths={"main.py", "values.yaml"})
    assert files == ["main.py", "values.yaml"]


def test_parse_affected_files_empty_section():
    text = "## Affected Files\n\n## Confidence\n3/5 — meh.\n"
    assert _parse_affected_files(text, valid_paths={"main.py"}) == []


# ---------------------------------------------------------------------------
# System prompt invariants the parser depends on
# ---------------------------------------------------------------------------

def test_system_prompt_demands_outline_only_paths():
    assert "verbatim" in SYSTEM_PROMPT
    assert "## Root Cause Hypothesis" in SYSTEM_PROMPT
    assert "## Affected Files" in SYSTEM_PROMPT
    assert "## Confidence" in SYSTEM_PROMPT
    # The cap is mentioned in prose so Sonnet doesn't try to be exhaustive
    assert "20" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Investigator.investigate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_investigate_builds_cached_prompt_and_returns_investigation(tmp_path):
    (tmp_path / "helm-values.yaml").write_text("memory: 512MBz\n")
    (tmp_path / "check_helm_values.py").write_text("def check(): pass\n")

    inv = Investigator(api_key="sk-test", model="claude-sonnet-4-6")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE)],
        model="claude-sonnet-4-6",
        usage=SimpleNamespace(
            input_tokens=2000, output_tokens=300,
            cache_read_input_tokens=1500, cache_creation_input_tokens=0,
        ),
    )
    inv.client.messages.create = AsyncMock(return_value=fake_response)

    result = await inv.investigate(
        _make_ctx(),
        codebase_path=str(tmp_path),
        triage_reasoning="actionable: helm values misconfigured",
    )

    assert isinstance(result, Investigation)
    assert "invalid" in result.root_cause
    assert result.affected_files == ["helm-values.yaml", "check_helm_values.py"]
    assert result.confidence == 4
    assert "parser" in result.reasoning
    assert result.input_tokens == 2000
    assert result.cache_read_tokens == 1500

    kwargs = inv.client.messages.create.await_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["thinking"] == {"type": "adaptive"}
    blocks = kwargs["messages"][0]["content"]
    assert len(blocks) == 2
    # Outline is the cache prefix
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "helm-values.yaml" in blocks[0]["text"]
    # Volatile half: alert + triage reasoning, no cache marker
    assert "cache_control" not in blocks[1]
    assert "actionable: helm values misconfigured" in blocks[1]["text"]
    assert "HelmValuesMisconfigured" in blocks[1]["text"]
    assert "512MBz" in blocks[1]["text"]


@pytest.mark.asyncio
async def test_investigate_falls_over_to_no_thinking_on_400(tmp_path):
    """The Haiku 4.5 surprise taught us: never let a `thinking` arg 400
    silently kill a stage. The investigator must retry without thinking
    if the model rejects it."""
    (tmp_path / "main.py").write_text("a = 1\n")
    inv = Investigator(api_key="sk-test", model="claude-sonnet-4-6")

    # First call raises a BadRequestError about thinking; second call
    # (without thinking) succeeds.
    error = BadRequestError(
        message="adaptive thinking is not supported on this model",
        response=MagicMock(),
        body={"error": {"message": "adaptive thinking is not supported on this model"}},
    )
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE)],
        model="claude-sonnet-4-6",
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=5,
        ),
    )
    inv.client.messages.create = AsyncMock(side_effect=[error, fake_response])

    result = await inv.investigate(
        _make_ctx(), codebase_path=str(tmp_path),
    )
    assert result.confidence == 4
    # Retry happened
    assert inv.client.messages.create.await_count == 2
    second_kwargs = inv.client.messages.create.await_args_list[1].kwargs
    assert "thinking" not in second_kwargs


@pytest.mark.asyncio
async def test_investigate_propagates_non_thinking_400(tmp_path):
    """We only swallow 400s that mention thinking. Anything else (e.g.
    auth, malformed payload) must propagate so the processor's fail-open
    wrapper logs and skips the stage cleanly."""
    inv = Investigator(api_key="sk-test", model="claude-sonnet-4-6")
    error = BadRequestError(
        message="invalid api key",
        response=MagicMock(),
        body={"error": {"message": "invalid api key"}},
    )
    inv.client.messages.create = AsyncMock(side_effect=error)

    with pytest.raises(BadRequestError):
        await inv.investigate(_make_ctx(), codebase_path=str(tmp_path))


@pytest.mark.asyncio
async def test_investigate_drops_hallucinated_paths_against_real_root(tmp_path):
    """End-to-end: response references a file that doesn't exist on disk
    → it must be filtered out before reaching the affected_files list."""
    (tmp_path / "real.yaml").write_text("a: 1\n")

    inv = Investigator(api_key="sk-test", model="claude-sonnet-4-6")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(
            type="text",
            text=(
                "## Root Cause Hypothesis\nx\n"
                "## Affected Files\n- real.yaml\n- imaginary.py\n"
                "## Confidence\n3/5 — meh.\n"
                "## Reasoning\nbecause.\n"
            ),
        )],
        model="claude-sonnet-4-6",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    inv.client.messages.create = AsyncMock(return_value=fake_response)

    result = await inv.investigate(_make_ctx(), codebase_path=str(tmp_path))
    assert result.affected_files == ["real.yaml"]


# ---------------------------------------------------------------------------
# Chart lineage at the file-selection stage (#25 option C).
#
# This is where wrong-file selection happens: the system prompt forbids
# inventing paths, so a model whose outline is missing the causal layer names
# the closest-looking visible file instead — and that wrong path carries a
# confidence score into synthesis.
# ---------------------------------------------------------------------------

LINEAGE = (
    ChartLayer(name="resrv", version="2.5.0", repo="acme/charts"),
    ChartLayer(name="airflow-tool", version="2.1.0", repo="acme/charts", visible=True),
    ChartLayer(name="airflow", version="1.19.0", repo="apache/airflow"),
)


def test_no_lineage_leaves_the_prompt_untouched():
    baseline = _format_user_blob(_make_ctx(), triage_reasoning=None)
    assert baseline == _format_user_blob(
        _make_ctx(), triage_reasoning=None, chart_lineage=(),
    )
    assert "Chart lineage" not in baseline


def test_lineage_names_invisible_layers_and_offers_the_third_option():
    text = _format_user_blob(_make_ctx(), triage_reasoning=None,
                             chart_lineage=LINEAGE)
    assert "Chart lineage" in text
    assert "NOT in the outline below" in text
    assert "`resrv`, `airflow` are NOT in it" in text
    # The escape hatch that converts confident-wrong into honest abstention.
    assert "return few or no affected files" in text


def test_lineage_all_visible_skips_the_warning():
    text = _format_user_blob(
        _make_ctx(), triage_reasoning=None,
        chart_lineage=(ChartLayer(name="solo", visible=True),),
    )
    assert "Every layer is covered by the outline" in text
    assert "NOT in the outline below" not in text


def test_lineage_precedes_triage_framing_and_incident_context():
    text = _format_user_blob(_make_ctx(), triage_reasoning="worth a look",
                             chart_lineage=LINEAGE)
    assert text.index("Chart lineage") < text.index("Pre-flight triage framing")
    assert text.index("Pre-flight triage framing") < text.index("# Incident context")
