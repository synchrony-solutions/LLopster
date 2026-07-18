"""Tests for the premium-pack prompt-resolution seam, loader, and entitlements.

No live API calls — these exercise prompt resolution / pack loading only.
The example pack under examples/packs/ ships placeholder (non-premium) content
purely to drive the load path.
"""

import textwrap
from pathlib import Path

import pytest

from src.agent import entitlements
from src.agent.packs import (
    DEFAULT_PACKS_DIR,
    compute_stub_signature,
    load_packs_into,
)
from src.agent.investigator import SYSTEM_PROMPT as INVESTIGATION_PROMPT
from src.agent.patch_generator import SYSTEM_PROMPT as SYNTHESIS_PROMPT
from src.agent.prompts import (
    STAGE_INVESTIGATION,
    STAGE_SYNTHESIS,
    STAGE_TRIAGE,
    PackPrompt,
    PromptResolver,
)
from src.agent.triage import SYSTEM_PROMPT as TRIAGE_PROMPT

# Repo-root examples/packs dir holding the shipped example pack.
EXAMPLE_PACKS_DIR = Path(__file__).parent.parent / "examples" / "packs"

DEFAULTS = {
    STAGE_TRIAGE: TRIAGE_PROMPT,
    STAGE_INVESTIGATION: INVESTIGATION_PROMPT,
    STAGE_SYNTHESIS: SYNTHESIS_PROMPT,
}


def _resolver(*, entitled: bool) -> PromptResolver:
    return PromptResolver(DEFAULTS, is_pack_enabled=lambda _pid: entitled)


# ---------------------------------------------------------------------------
# resolve() with no overlays → exact Community defaults
# ---------------------------------------------------------------------------

def test_resolve_returns_exact_community_default_for_each_stage():
    r = PromptResolver(DEFAULTS)
    assert r.resolve(STAGE_TRIAGE) == TRIAGE_PROMPT
    assert r.resolve(STAGE_INVESTIGATION) == INVESTIGATION_PROMPT
    assert r.resolve(STAGE_SYNTHESIS) == SYNTHESIS_PROMPT
    # A stack value with no registered overlay also falls back exactly.
    assert r.resolve(STAGE_SYNTHESIS, stack="jvm") == SYNTHESIS_PROMPT


def test_no_packs_dir_loads_nothing_and_resolves_to_defaults(tmp_path):
    r = PromptResolver(DEFAULTS)
    loaded = load_packs_into(r, str(tmp_path / "does-not-exist"))
    assert loaded == 0
    assert r.resolve(STAGE_SYNTHESIS, stack="example") == SYNTHESIS_PROMPT


def test_empty_packs_dir_value_loads_nothing():
    r = PromptResolver(DEFAULTS)
    assert load_packs_into(r, "") == 0
    assert load_packs_into(r, None) == 0


# ---------------------------------------------------------------------------
# Example pack: present but unentitled → Community used, warning logged
# ---------------------------------------------------------------------------

def test_example_pack_unentitled_falls_back_to_community(caplog):
    r = _resolver(entitled=False)
    loaded = load_packs_into(r, str(EXAMPLE_PACKS_DIR))
    assert loaded == 1  # pack registered...

    with caplog.at_level("WARNING", logger="llopster.prompts"):
        resolved = r.resolve(STAGE_SYNTHESIS, stack="example")
    # ...but unentitled → exact Community prompt, plus a warning.
    assert resolved == SYNTHESIS_PROMPT
    assert "EXAMPLE-PACK-OVERLAY" not in resolved
    assert any("not entitled" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Example pack: present AND entitled → overlay applied for targeted stage/stack
# ---------------------------------------------------------------------------

def test_example_pack_entitled_overlays_each_stage():
    r = _resolver(entitled=True)
    assert load_packs_into(r, str(EXAMPLE_PACKS_DIR)) == 1

    for stage in (STAGE_TRIAGE, STAGE_INVESTIGATION, STAGE_SYNTHESIS):
        resolved = r.resolve(stage, stack="example")
        # Community base is preserved AND the overlay text is appended.
        assert DEFAULTS[stage] in resolved
        assert "EXAMPLE-PACK-OVERLAY" in resolved
        assert len(resolved) > len(DEFAULTS[stage])

    # Wrong stack (no overlay registered for it) → Community only.
    assert r.resolve(STAGE_SYNTHESIS, stack="other") == SYNTHESIS_PROMPT
    # No stack at all → Community only (the pack is stack-scoped to "example").
    assert r.resolve(STAGE_SYNTHESIS) == SYNTHESIS_PROMPT


def test_active_overlay_reports_entitled_pack_only():
    entitled = _resolver(entitled=True)
    load_packs_into(entitled, str(EXAMPLE_PACKS_DIR))
    overlay = entitled.active_overlay(STAGE_SYNTHESIS, stack="example")
    assert overlay is not None
    assert overlay.pack_id == "example-pack"
    assert overlay.version == "0.1.0"

    unentitled = _resolver(entitled=False)
    load_packs_into(unentitled, str(EXAMPLE_PACKS_DIR))
    assert unentitled.active_overlay(STAGE_SYNTHESIS, stack="example") is None


# ---------------------------------------------------------------------------
# Malformed / unverifiable packs → skipped, no raise, Community used
# ---------------------------------------------------------------------------

def _write_pack(base: Path, name: str, manifest: str, prompts: dict[str, str]):
    pack = base / name
    (pack / "prompts").mkdir(parents=True)
    (pack / "manifest.yaml").write_text(manifest)
    for fname, text in prompts.items():
        (pack / "prompts" / fname).write_text(text)
    return pack


def test_bad_signature_pack_is_skipped(tmp_path, caplog):
    _write_pack(
        tmp_path, "badsig",
        manifest=textwrap.dedent("""
            id: badsig
            version: 1.0.0
            stack: example
            signature: "sha256:deadbeef"
            prompts:
              synthesis: synthesis.md
        """),
        prompts={"synthesis.md": "OVERLAY-SHOULD-NOT-APPLY"},
    )
    r = _resolver(entitled=True)
    with caplog.at_level("WARNING", logger="llopster.packs"):
        loaded = load_packs_into(r, str(tmp_path))
    assert loaded == 0
    assert r.resolve(STAGE_SYNTHESIS, stack="example") == SYNTHESIS_PROMPT
    assert any("signature" in rec.message for rec in caplog.records)


def test_malformed_manifest_is_skipped(tmp_path):
    pack = tmp_path / "broken"
    (pack / "prompts").mkdir(parents=True)
    (pack / "manifest.yaml").write_text("this is: : not valid: yaml::")
    (pack / "prompts" / "synthesis.md").write_text("OVERLAY")

    r = _resolver(entitled=True)
    loaded = load_packs_into(r, str(tmp_path))  # must not raise
    assert loaded == 0
    assert r.resolve(STAGE_SYNTHESIS, stack="example") == SYNTHESIS_PROMPT


def test_manifest_missing_required_fields_is_skipped(tmp_path):
    _write_pack(
        tmp_path, "noid",
        manifest="version: 1.0.0\nstack: example\nprompts:\n  synthesis: synthesis.md\n",
        prompts={"synthesis.md": "OVERLAY"},
    )
    r = _resolver(entitled=True)
    assert load_packs_into(r, str(tmp_path)) == 0
    assert r.resolve(STAGE_SYNTHESIS, stack="example") == SYNTHESIS_PROMPT


def test_missing_prompt_file_skips_that_overlay(tmp_path):
    name, version = "halfpack", "2.0.0"
    _write_pack(
        tmp_path, name,
        manifest=textwrap.dedent(f"""
            id: {name}
            version: {version}
            stack: example
            signature: "{compute_stub_signature(name, version)}"
            prompts:
              synthesis: synthesis.md
              triage: missing.md
        """),
        prompts={"synthesis.md": "GOOD-OVERLAY"},
    )
    r = _resolver(entitled=True)
    # synthesis registers; triage's missing file is skipped → pack still loads.
    assert load_packs_into(r, str(tmp_path)) == 1
    assert "GOOD-OVERLAY" in r.resolve(STAGE_SYNTHESIS, stack="example")
    # triage had no valid file → Community fallback.
    assert r.resolve(STAGE_TRIAGE, stack="example") == TRIAGE_PROMPT


def test_unknown_stage_in_manifest_is_ignored(tmp_path):
    name, version = "weirdstage", "1.0.0"
    _write_pack(
        tmp_path, name,
        manifest=textwrap.dedent(f"""
            id: {name}
            version: {version}
            stack: example
            signature: "{compute_stub_signature(name, version)}"
            prompts:
              synthesis: synthesis.md
              not_a_stage: bogus.md
        """),
        prompts={"synthesis.md": "GOOD", "bogus.md": "BAD"},
    )
    r = _resolver(entitled=True)
    assert load_packs_into(r, str(tmp_path)) == 1
    assert "GOOD" in r.resolve(STAGE_SYNTHESIS, stack="example")


# ---------------------------------------------------------------------------
# Stack-agnostic overlay (manifest omits stack → applies when stack is None)
# ---------------------------------------------------------------------------

def test_stack_agnostic_overlay_applies_to_no_pack_services(tmp_path):
    name, version = "global", "1.0.0"
    _write_pack(
        tmp_path, name,
        manifest=textwrap.dedent(f"""
            id: {name}
            version: {version}
            signature: "{compute_stub_signature(name, version)}"
            prompts:
              synthesis: synthesis.md
        """),
        prompts={"synthesis.md": "GLOBAL-OVERLAY"},
    )
    r = _resolver(entitled=True)
    assert load_packs_into(r, str(tmp_path)) == 1
    assert "GLOBAL-OVERLAY" in r.resolve(STAGE_SYNTHESIS, stack=None)


# ---------------------------------------------------------------------------
# Direct overlay registration + cache-safety property
# ---------------------------------------------------------------------------

def test_register_overlay_for_unknown_stage_is_ignored():
    r = PromptResolver(DEFAULTS, is_pack_enabled=lambda _p: True)
    r.register_overlay(
        PackPrompt(pack_id="p", version="1", stage="bogus", stack=None, text="X")
    )
    # Nothing registered for a real stage → defaults intact.
    assert r.resolve(STAGE_SYNTHESIS) == SYNTHESIS_PROMPT


def test_overlay_does_not_mutate_community_default():
    # Cache safety: the system prompt is a separate block from the cached
    # codebase user block, and we never mutate the base constant in place —
    # resolve() builds a new string. Enabling a pack can't corrupt the
    # baked-in prompt for other (unentitled / no-stack) calls.
    r = _resolver(entitled=True)
    load_packs_into(r, str(EXAMPLE_PACKS_DIR))
    _ = r.resolve(STAGE_SYNTHESIS, stack="example")
    assert SYNTHESIS_PROMPT == DEFAULTS[STAGE_SYNTHESIS]
    assert r.resolve(STAGE_SYNTHESIS) == SYNTHESIS_PROMPT


# ---------------------------------------------------------------------------
# Entitlement stub
# ---------------------------------------------------------------------------

def test_entitlements_no_token_means_no_packs(monkeypatch):
    monkeypatch.delenv(entitlements.PACK_TOKEN_ENV, raising=False)
    assert entitlements.is_pack_enabled("example-pack") is False


def test_entitlements_token_allow_list(monkeypatch):
    monkeypatch.setenv(entitlements.PACK_TOKEN_ENV, "jvm-pack, example-pack ,postgres")
    assert entitlements.is_pack_enabled("example-pack") is True
    assert entitlements.is_pack_enabled("jvm-pack") is True
    assert entitlements.is_pack_enabled("postgres") is True
    assert entitlements.is_pack_enabled("not-entitled") is False
    assert entitlements.is_pack_enabled("") is False


def test_default_packs_dir_constant():
    # Documented default mount point; the chart wires LLOPSTER_PACKS_DIR here.
    assert DEFAULT_PACKS_DIR == "/packs"
