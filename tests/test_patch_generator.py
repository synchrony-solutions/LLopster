"""Tests for the patch generator — mocks the Anthropic client so no API calls run."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import AlertContext
from src.agent.investigator import Investigation
from src.agent.patch_generator import (
    PatchGenerator,
    _format_alert_context,
    _format_investigation_handoff,
    _load_codebase,
    _parse_confidence,
)
from src.integrations.loki_client import LogLine
from src.integrations.prometheus_client import MetricSample


def _investigation(
    affected_files: list[str],
    *,
    root_cause: str = "memory unit invalid",
    confidence: int = 4,
) -> Investigation:
    return Investigation(
        root_cause=root_cause,
        affected_files=affected_files,
        confidence=confidence,
        reasoning="logs name the file",
        response_text="## Root Cause Hypothesis\n...",
        model="claude-sonnet-4-6",
        input_tokens=2000, output_tokens=300,
        cache_read_tokens=1500, cache_creation_tokens=0,
    )

SAMPLE_RESPONSE_TEXT = """\
## Root Cause
bug

## Proposed Patch
```diff
--- a/main.py
+++ b/main.py
@@ -1,1 +1,1 @@
-buggy = True
+buggy = False
```

## Confidence
4/5 — Root cause is clear from the logs; fix is minimal and targeted.

## Reasoning
Setting buggy to False resolves the issue.
"""


def _make_ctx() -> AlertContext:
    alert = ParsedAlert(
        fingerprint="abc",
        status="firing",
        alertname="HelmValuesMisconfigured",
        severity="warning",
        service="demo-app",
        summary="bad helm values",
        description="memory unit invalid",
        starts_at=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": "demo-app", "severity": "warning"},
        annotations={},
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


# ---------------------------------------------------------------------------
# Confidence parsing
# ---------------------------------------------------------------------------

def test_parse_confidence_standard_format():
    score, reason = _parse_confidence(SAMPLE_RESPONSE_TEXT)
    assert score == 4
    assert "Root cause is clear" in reason


def test_parse_confidence_em_dash():
    text = "## Confidence\n5/5 — Fix is unambiguous.\n## Reasoning\nok"
    assert _parse_confidence(text) == (5, "Fix is unambiguous.")


def test_parse_confidence_hyphen_separator():
    text = "## Confidence\n3/5 - Plausible but uncertain.\n## Reasoning\nok"
    score, reason = _parse_confidence(text)
    assert score == 3


def test_parse_confidence_missing_section():
    score, reason = _parse_confidence("## Root Cause\nno confidence here")
    assert score == 0
    assert "missing" in reason


def test_parse_confidence_unparseable_section():
    score, reason = _parse_confidence("## Confidence\nhigh confidence!\n## Reasoning\nok")
    assert score == 0


# ---------------------------------------------------------------------------
# Context formatting
# ---------------------------------------------------------------------------

def test_format_alert_context_includes_key_fields():
    text = _format_alert_context(_make_ctx())
    assert "HelmValuesMisconfigured" in text
    assert "demo-app" in text
    assert "invalid memory unit: 512MBz" in text
    assert "demo_app_config_errors_total" in text
    assert "3.0" in text
    assert '{service="demo-app"}' in text


# ---------------------------------------------------------------------------
# Codebase loading
# ---------------------------------------------------------------------------

def test_load_codebase_concatenates_text_files(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    (tmp_path / "values.yaml").write_text("memory: 512Mi\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00")
    skipdir = tmp_path / "__pycache__"
    skipdir.mkdir()
    (skipdir / "junk.py").write_text("noise")

    blob, count = _load_codebase(tmp_path)
    assert "main.py" in blob
    assert "print('hi')" in blob
    assert "values.yaml" in blob
    assert "memory: 512Mi" in blob
    assert "image.png" not in blob
    assert "junk.py" not in blob
    assert count == 2  # main.py + values.yaml


def test_load_codebase_missing_root(tmp_path):
    blob, count = _load_codebase(tmp_path / "nope")
    assert "not found" in blob
    assert count == 0


# ---------------------------------------------------------------------------
# Phase C: narrowing via `only_files`
# ---------------------------------------------------------------------------

def test_load_codebase_narrowed_returns_only_requested_files(tmp_path):
    """Narrowed mode must ship ONLY the listed files. Other files in
    the tree are ignored — that's the whole cost-saving point."""
    (tmp_path / "wanted.yaml").write_text("memory: 512Mi\n")
    (tmp_path / "also_wanted.py").write_text("def f(): pass\n")
    (tmp_path / "ignored.py").write_text("def g(): pass\n")
    (tmp_path / "noise.md").write_text("# README\n")

    blob, count = _load_codebase(
        tmp_path, only_files=["wanted.yaml", "also_wanted.py"],
    )
    assert "wanted.yaml" in blob
    assert "memory: 512Mi" in blob
    assert "also_wanted.py" in blob
    assert "ignored.py" not in blob
    assert "noise.md" not in blob
    assert "narrowed to 2 file(s)" in blob
    assert count == 2


def test_load_codebase_narrowed_silently_skips_missing(tmp_path):
    """Sonnet sometimes flags a file that was deleted between
    investigation and synthesis. The loader skips missing files; the
    caller (PatchGenerator.generate) handles "all missing" via the
    empty-blob safety net."""
    (tmp_path / "exists.yaml").write_text("memory: 512Mi\n")

    blob, count = _load_codebase(
        tmp_path, only_files=["exists.yaml", "ghost.py"],
    )
    assert "exists.yaml" in blob
    assert "ghost.py" not in blob
    assert count == 1


def test_load_codebase_narrowed_all_missing_returns_zero_count(tmp_path):
    (tmp_path / "real.yaml").write_text("a: 1\n")
    blob, count = _load_codebase(
        tmp_path, only_files=["one.py", "two.py", "three.py"],
    )
    assert count == 0  # triggers PatchGenerator's fallback to full tree


def test_load_codebase_narrowed_rejects_out_of_tree_paths(tmp_path):
    """Defence in depth: even if Sonnet hallucinates `../etc/passwd`,
    the loader must refuse to read outside the codebase root. Phase B's
    parser already filters against the real tree, but a regression in
    that filter shouldn't become a file-disclosure bug."""
    (tmp_path / "ok.yaml").write_text("a: 1\n")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n")
    try:
        blob, count = _load_codebase(
            tmp_path, only_files=["ok.yaml", "../outside.txt"],
        )
        assert "secret" not in blob
        assert count == 1
    finally:
        outside.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_builds_cached_prompt_and_returns_proposal(tmp_path):
    (tmp_path / "main.py").write_text("buggy = True\n")

    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")

    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE_TEXT)],
        model="claude-opus-4-7",
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=800,
            cache_creation_input_tokens=0,
        ),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(_make_ctx(), codebase_path=str(tmp_path))

    assert proposal.text.startswith("## Root Cause")
    assert proposal.model == "claude-opus-4-7"
    assert proposal.input_tokens == 1000
    assert proposal.output_tokens == 200
    assert proposal.cache_read_tokens == 800
    assert proposal.cache_creation_tokens == 0
    assert proposal.confidence == 4
    assert "Root cause is clear" in proposal.confidence_reason

    call = gen.client.messages.create.await_args
    kwargs = call.kwargs
    assert kwargs["model"] == "claude-opus-4-7"
    assert kwargs["thinking"] == {"type": "adaptive"}
    blocks = kwargs["messages"][0]["content"]
    assert len(blocks) == 2
    # Default has extended_cache_ttl=True → cache_control includes ttl=1h
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "main.py" in blocks[0]["text"]
    assert "cache_control" not in blocks[1]
    assert "HelmValuesMisconfigured" in blocks[1]["text"]


@pytest.mark.asyncio
async def test_generate_extended_cache_ttl_disabled(tmp_path):
    """When extended_cache_ttl=False, cache_control omits the ttl field and
    the beta header isn't set on the client (so accounts without the beta
    flag still work)."""
    (tmp_path / "main.py").write_text("buggy = True\n")
    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7", extended_cache_ttl=False)

    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE_TEXT)],
        model="claude-opus-4-7",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)
    await gen.generate(_make_ctx(), codebase_path=str(tmp_path))

    blocks = gen.client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in blocks[0]["cache_control"]


def test_extended_cache_ttl_sets_beta_header():
    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7", extended_cache_ttl=True)
    # The default_headers we passed should be reflected on the client.
    headers = gen.client._custom_headers if hasattr(gen.client, "_custom_headers") else {}
    # Anthropic SDK stores default_headers on the underlying HTTP client.
    # Inspect both possible storage locations to stay compatible with SDK
    # internal changes.
    raw = dict(getattr(gen.client, "default_headers", {}) or {})
    raw.update(headers or {})
    # Headers may be lowercased/title-cased depending on SDK version.
    lowered = {k.lower(): v for k, v in raw.items()}
    assert "anthropic-beta" in lowered
    assert "extended-cache-ttl-2025-04-11" in lowered["anthropic-beta"]


def test_extended_cache_ttl_disabled_omits_beta_header():
    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7", extended_cache_ttl=False)
    raw = dict(getattr(gen.client, "default_headers", {}) or {})
    lowered = {k.lower(): v for k, v in raw.items()}
    # The agent's own beta header should not be present (the SDK may set
    # other anthropic-beta values internally; we only check ours is absent).
    if "anthropic-beta" in lowered:
        assert "extended-cache-ttl-2025-04-11" not in lowered["anthropic-beta"]


def test_system_prompt_includes_diff_format_discipline():
    """Patch-format rules must be in the system prompt — without them the
    model can balloon output 10-50× with full-file rewrites."""
    from src.agent.patch_generator import SYSTEM_PROMPT
    assert "Diff format rules" in SYSTEM_PROMPT
    assert "NEVER emit a full-file rewrite" in SYSTEM_PROMPT
    assert "3 lines of leading/trailing context" in SYSTEM_PROMPT
    assert "Pick one" in SYSTEM_PROMPT  # no multiple-candidate diffs
    assert "No code patch is appropriate for this alert." in SYSTEM_PROMPT  # explicit no-patch sentinel


@pytest.mark.asyncio
async def test_generate_handles_missing_usage_cache_fields(tmp_path):
    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="## Confidence\n2/5 — Limited context.\n")],
        model="claude-opus-4-7",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(_make_ctx(), codebase_path=str(tmp_path))
    assert proposal.cache_read_tokens == 0
    assert proposal.cache_creation_tokens == 0
    assert proposal.confidence == 2


# ---------------------------------------------------------------------------
# Phase C: investigation handoff + narrow-vs-full orchestration
# ---------------------------------------------------------------------------

def test_format_investigation_handoff_renders_files_and_confidence():
    inv = _investigation(["helm-values.yaml", "check_helm.py"], confidence=3)
    blob = "\n".join(_format_investigation_handoff(inv))
    assert "claude-sonnet-4-6" in blob
    assert "helm-values.yaml" in blob
    assert "check_helm.py" in blob
    assert "memory unit invalid" in blob
    assert "3/5" in blob
    # The escape hatch instruction is what keeps Opus from guessing when
    # Sonnet narrowed it to the wrong files.
    assert "No code patch is appropriate for this alert." in blob


@pytest.mark.asyncio
async def test_generate_narrowed_when_investigation_lists_real_files(tmp_path):
    """Investigation with valid affected_files → narrowed blob, used flag
    True, handoff appears in the volatile half of the prompt, file_count
    reflects what was actually loaded."""
    (tmp_path / "helm-values.yaml").write_text("memory: 512MBz\n")
    (tmp_path / "check_helm.py").write_text("def check(): pass\n")
    (tmp_path / "unrelated.py").write_text("def other(): pass\n")

    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE_TEXT)],
        model="claude-opus-4-7",
        usage=SimpleNamespace(
            input_tokens=500, output_tokens=100,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(
        _make_ctx(),
        codebase_path=str(tmp_path),
        investigation=_investigation(["helm-values.yaml", "check_helm.py"]),
    )

    assert proposal.used_narrowed_context is True
    assert proposal.file_count == 2

    blocks = gen.client.messages.create.await_args.kwargs["messages"][0]["content"]
    cached_blob = blocks[0]["text"]
    volatile_blob = blocks[1]["text"]
    # Narrowed blob has the wanted files but not the unrelated one.
    assert "helm-values.yaml" in cached_blob
    assert "check_helm.py" in cached_blob
    assert "unrelated.py" not in cached_blob
    assert "narrowed to 2 file(s)" in cached_blob
    # Investigation handoff is volatile (no cache marker on block 1).
    assert "Pre-flight investigation" in volatile_blob


@pytest.mark.asyncio
async def test_generate_falls_back_to_full_when_no_investigation(tmp_path):
    """No investigation = today's full-codebase behaviour. used_narrowed
    must be False and the handoff header must NOT appear."""
    (tmp_path / "main.py").write_text("a = 1\n")
    (tmp_path / "other.py").write_text("b = 2\n")

    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE_TEXT)],
        model="claude-opus-4-7",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(_make_ctx(), codebase_path=str(tmp_path))
    assert proposal.used_narrowed_context is False
    assert proposal.file_count == 2

    blocks = gen.client.messages.create.await_args.kwargs["messages"][0]["content"]
    assert "main.py" in blocks[0]["text"]
    assert "other.py" in blocks[0]["text"]
    assert "Pre-flight investigation" not in blocks[1]["text"]


@pytest.mark.asyncio
async def test_generate_falls_back_to_full_on_empty_affected_files(tmp_path):
    """Investigation present but `affected_files=[]` → fall through to
    the existing full-tree behaviour. Sonnet may say "I cannot identify
    likely files from the outline alone" — that's the contract for this
    fallback."""
    (tmp_path / "main.py").write_text("a = 1\n")

    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE_TEXT)],
        model="claude-opus-4-7",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(
        _make_ctx(), codebase_path=str(tmp_path),
        investigation=_investigation(affected_files=[]),
    )
    assert proposal.used_narrowed_context is False
    assert proposal.file_count == 1


@pytest.mark.asyncio
async def test_generate_empty_blob_safety_net(tmp_path):
    """Investigation lists files but NONE exist on disk (e.g. all
    deleted between Sonnet and Opus). The safety net must:
      - log a warning,
      - fall back to the full codebase,
      - set used_narrowed_context=False.

    Without this, Opus would get an effectively empty blob and emit a
    confident wrong patch."""
    (tmp_path / "real.yaml").write_text("a: 1\n")
    (tmp_path / "also_real.py").write_text("x = 2\n")

    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=SAMPLE_RESPONSE_TEXT)],
        model="claude-opus-4-7",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(
        _make_ctx(), codebase_path=str(tmp_path),
        investigation=_investigation(
            affected_files=["ghost.py", "phantom.yaml"],
        ),
    )
    assert proposal.used_narrowed_context is False
    assert proposal.file_count == 2  # both real files loaded via fallback

    blocks = gen.client.messages.create.await_args.kwargs["messages"][0]["content"]
    # No "narrowed to N" header — we fell back to the full tree.
    assert "narrowed to" not in blocks[0]["text"]
    # And no handoff in the volatile half — handoff is gated on
    # used_narrowed since otherwise Opus would be told "files have been
    # filtered" when in fact they haven't.
    assert "Pre-flight investigation" not in blocks[1]["text"]


@pytest.mark.asyncio
async def test_generate_returns_zero_confidence_when_section_missing(tmp_path):
    gen = PatchGenerator(api_key="sk-test", model="claude-opus-4-7")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="## Root Cause\nno confidence section here")],
        model="claude-opus-4-7",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    gen.client.messages.create = AsyncMock(return_value=fake_response)

    proposal = await gen.generate(_make_ctx(), codebase_path=str(tmp_path))
    assert proposal.confidence == 0
