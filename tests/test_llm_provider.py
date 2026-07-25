"""Tests for the LLM provider seam (src/agent/llm_provider.py) and the
provider-aware bits of Config / cost pricing it drives."""

from dataclasses import replace

import pytest
from anthropic import AsyncAnthropic, AsyncAnthropicBedrock

from src.agent import cost
from src.agent.llm_provider import (
    PROVIDER_ANTHROPIC,
    PROVIDER_BEDROCK,
    build_message_client,
    effective_extended_cache_ttl,
    normalize_provider,
    resolve_models,
)
from src.config import Config

_BETA_HEADER = "anthropic-beta"
_BETA_VALUE = "extended-cache-ttl-2025-04-11"


def _cfg(**overrides) -> Config:
    """A Config with known-good defaults, overridable per test. Frozen
    dataclass → use replace() so we don't depend on ambient env."""
    base = Config(
        llm_provider=PROVIDER_ANTHROPIC,
        anthropic_api_key="sk-test",
        extended_cache_ttl=True,
    )
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# normalize_provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("anthropic", PROVIDER_ANTHROPIC),
    ("bedrock", PROVIDER_BEDROCK),
    ("BEDROCK", PROVIDER_BEDROCK),
    ("  Bedrock  ", PROVIDER_BEDROCK),
    ("", PROVIDER_ANTHROPIC),
    (None, PROVIDER_ANTHROPIC),
    ("vertex", PROVIDER_ANTHROPIC),  # unknown → safe fallback
    ("garbage", PROVIDER_ANTHROPIC),
])
def test_normalize_provider(raw, expected):
    assert normalize_provider(raw) == expected


# ---------------------------------------------------------------------------
# effective_extended_cache_ttl — Bedrock forces it off
# ---------------------------------------------------------------------------

def test_extended_ttl_anthropic_respects_flag():
    assert effective_extended_cache_ttl(_cfg(extended_cache_ttl=True)) is True
    assert effective_extended_cache_ttl(_cfg(extended_cache_ttl=False)) is False


def test_extended_ttl_forced_off_for_bedrock():
    cfg = _cfg(llm_provider=PROVIDER_BEDROCK, extended_cache_ttl=True)
    assert effective_extended_cache_ttl(cfg) is False


# ---------------------------------------------------------------------------
# resolve_models
# ---------------------------------------------------------------------------

def test_resolve_models_anthropic():
    cfg = _cfg(
        anthropic_model="claude-opus-4-7",
        anthropic_triage_model="claude-haiku-4-5",
        anthropic_investigation_model="claude-sonnet-4-6",
    )
    m = resolve_models(cfg)
    assert m.synthesis == "claude-opus-4-7"
    assert m.triage == "claude-haiku-4-5"
    assert m.investigation == "claude-sonnet-4-6"


def test_resolve_models_bedrock_uses_bedrock_ids():
    cfg = _cfg(
        llm_provider=PROVIDER_BEDROCK,
        bedrock_model="us.anthropic.claude-opus-4-7-v1:0",
        bedrock_triage_model="us.anthropic.claude-haiku-4-5-v1:0",
        bedrock_investigation_model="us.anthropic.claude-sonnet-4-6-v1:0",
    )
    m = resolve_models(cfg)
    assert m.synthesis == "us.anthropic.claude-opus-4-7-v1:0"
    assert m.triage == "us.anthropic.claude-haiku-4-5-v1:0"
    assert m.investigation == "us.anthropic.claude-sonnet-4-6-v1:0"


def test_resolve_models_bedrock_empty_override_falls_back_to_api_name():
    cfg = _cfg(
        llm_provider=PROVIDER_BEDROCK,
        anthropic_model="claude-opus-4-7",
        bedrock_model="",  # unset override
    )
    assert resolve_models(cfg).synthesis == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# build_message_client
# ---------------------------------------------------------------------------

def test_build_anthropic_client_with_beta_header():
    client = build_message_client(_cfg(), extended_cache_ttl=True)
    assert isinstance(client, AsyncAnthropic)
    assert client.default_headers.get(_BETA_HEADER) == _BETA_VALUE


def test_build_anthropic_client_without_beta_header():
    client = build_message_client(_cfg(), extended_cache_ttl=False)
    assert isinstance(client, AsyncAnthropic)
    assert _BETA_HEADER not in client.default_headers


def test_build_bedrock_client():
    cfg = _cfg(llm_provider=PROVIDER_BEDROCK, aws_region="us-east-1")
    # extended_cache_ttl is ignored for Bedrock — never a beta header there.
    client = build_message_client(cfg, extended_cache_ttl=True)
    assert isinstance(client, AsyncAnthropicBedrock)
    assert client.aws_region == "us-east-1"


def test_build_bedrock_client_without_static_keys_uses_ambient_chain():
    # No AWS_* keys set → constructor must still succeed (boto3 resolves
    # credentials lazily at call time, i.e. IRSA / instance profile).
    cfg = _cfg(
        llm_provider=PROVIDER_BEDROCK,
        aws_region="us-west-2",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_session_token="",
    )
    client = build_message_client(cfg, extended_cache_ttl=False)
    assert isinstance(client, AsyncAnthropicBedrock)


# ---------------------------------------------------------------------------
# Config.llm_configured
# ---------------------------------------------------------------------------

def test_llm_configured_anthropic_requires_key():
    assert _cfg(anthropic_api_key="sk-test").llm_configured is True
    assert _cfg(anthropic_api_key="").llm_configured is False


def test_llm_configured_bedrock_always_true():
    # Bedrock resolves credentials at call time — configured regardless of
    # whether an Anthropic key or static AWS keys are present.
    cfg = _cfg(llm_provider=PROVIDER_BEDROCK, anthropic_api_key="")
    assert cfg.llm_configured is True


# ---------------------------------------------------------------------------
# cost.pricing_for — Bedrock inference-profile IDs price correctly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,canonical", [
    ("us.anthropic.claude-opus-4-7-v1:0", "claude-opus-4-7"),
    ("us.anthropic.claude-sonnet-4-6-v1:0", "claude-sonnet-4-6"),
    ("eu.anthropic.claude-haiku-4-5-v1:0", "claude-haiku-4-5"),
    ("anthropic.claude-sonnet-4-6-v1:0", "claude-sonnet-4-6"),
])
def test_pricing_for_bedrock_ids(model, canonical):
    assert cost.pricing_for(model) is cost.PRICING[canonical]


def test_pricing_for_exact_match_still_works():
    assert cost.pricing_for("claude-sonnet-4-6") is cost.PRICING["claude-sonnet-4-6"]


def test_pricing_for_unknown_falls_back_to_default():
    assert cost.pricing_for("some-unknown-model") is cost.DEFAULT_PRICING
    assert cost.pricing_for(None) is cost.DEFAULT_PRICING
