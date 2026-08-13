"""LLM provider seam — one place that knows *how* to build the Anthropic
message client, so the rest of the pipeline stays provider-agnostic.

Today two providers are supported:

* ``anthropic`` (default) — the direct Anthropic API, keyed by
  ``ANTHROPIC_API_KEY``. Identical to the pre-Bedrock behavior.
* ``bedrock`` — Claude via AWS Bedrock. Credentials resolve through the
  standard boto3 chain (IRSA / pod-identity / instance profile on EKS is
  the recommended path — no static keys), with optional static
  ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``
  as a fallback for clusters without IRSA.

Both clients expose the *identical* ``.messages.create(...)`` surface and
raise the same ``anthropic`` exception types, so the Triage / Investigator
/ PatchGenerator call sites don't branch on provider — they just receive a
ready-built client.

Two provider quirks are handled here rather than at the call sites:

* **Model IDs differ.** Bedrock addresses models by inference-profile ID
  (e.g. ``us.anthropic.claude-opus-4-7-v1:0``), not the bare
  ``claude-opus-4-7`` the direct API uses. ``resolve_models`` returns the
  right trio for the active provider.
* **The extended-cache-ttl beta is Anthropic-API-only.** The
  ``anthropic-beta: extended-cache-ttl-2025-04-11`` header (and the
  ``"ttl": "1h"`` cache_control marker it unlocks) is not a Bedrock
  feature; sending it there risks a 400. ``effective_extended_cache_ttl``
  forces it off for Bedrock — 5-minute ephemeral prompt caching still
  works on Bedrock, it's only the 1-hour TTL that's gated.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from anthropic import AsyncAnthropic

if TYPE_CHECKING:
    from src.config import Config

log = logging.getLogger("llopster.llm_provider")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_BEDROCK = "bedrock"
VALID_PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_BEDROCK)

# The beta header + 1h TTL marker that only the direct Anthropic API honors.
_EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"


class ResolvedModels(NamedTuple):
    """The three per-stage model IDs for the active provider."""

    triage: str
    investigation: str
    synthesis: str


def normalize_provider(raw: str | None) -> str:
    """Coerce the ``LLM_PROVIDER`` env value to a known provider.

    Unknown / empty values fall back to the direct Anthropic API (the
    historical default) with a loud warning — never a crash, matching the
    fail-safe posture of the rest of the config surface.
    """
    provider = (raw or PROVIDER_ANTHROPIC).strip().lower()
    if provider not in VALID_PROVIDERS:
        log.warning(
            "LLM_PROVIDER=%r is not one of %s — falling back to %r",
            raw, VALID_PROVIDERS, PROVIDER_ANTHROPIC,
        )
        return PROVIDER_ANTHROPIC
    return provider


def effective_extended_cache_ttl(config: "Config") -> bool:
    """Whether the extended (1h) prompt-cache TTL should actually be used.

    Only the direct Anthropic API supports it; on Bedrock it's forced off
    regardless of ``EXTENDED_CACHE_TTL`` so we never send an unsupported
    beta header / TTL marker.
    """
    return config.extended_cache_ttl and config.llm_provider == PROVIDER_ANTHROPIC


def resolve_models(config: "Config") -> ResolvedModels:
    """Return the (triage, investigation, synthesis) model IDs for the
    active provider. On Bedrock, an empty per-stage override falls back to
    the direct-API model string so *something* is always passed (the
    operator is expected to set the Bedrock inference-profile IDs)."""
    if config.llm_provider == PROVIDER_BEDROCK:
        return ResolvedModels(
            triage=config.bedrock_triage_model or config.anthropic_triage_model,
            investigation=(
                config.bedrock_investigation_model
                or config.anthropic_investigation_model
            ),
            synthesis=config.bedrock_model or config.anthropic_model,
        )
    return ResolvedModels(
        triage=config.anthropic_triage_model,
        investigation=config.anthropic_investigation_model,
        synthesis=config.anthropic_model,
    )


def build_message_client(config: "Config", *, extended_cache_ttl: bool):
    """Build the async message client for the active provider.

    ``extended_cache_ttl`` controls the direct-API cache beta header only;
    it is ignored for Bedrock (which never gets the header). Returns an
    object exposing ``.messages.create(...)`` — either ``AsyncAnthropic``
    or ``AsyncAnthropicBedrock``.
    """
    if config.llm_provider == PROVIDER_BEDROCK:
        return _build_bedrock_client(config)
    return _build_anthropic_client(
        api_key=config.anthropic_api_key,
        extended_cache_ttl=extended_cache_ttl,
    )


def _build_anthropic_client(*, api_key: str, extended_cache_ttl: bool) -> AsyncAnthropic:
    default_headers = (
        {"anthropic-beta": _EXTENDED_CACHE_TTL_BETA} if extended_cache_ttl else {}
    )
    return AsyncAnthropic(api_key=api_key, default_headers=default_headers)


def _build_bedrock_client(config: "Config"):
    # Imported lazily so a Community/direct-API deployment doesn't need the
    # boto3 dependency chain installed to import this module.
    from anthropic import AsyncAnthropicBedrock

    if not config.aws_region:
        log.warning(
            "LLM_PROVIDER=bedrock but AWS_REGION is unset — relying on the "
            "ambient boto3 region resolution; set AWS_REGION if calls fail."
        )

    # Static keys are the fallback path. When they're absent we pass None,
    # which lets the underlying boto3 credential chain (IRSA / pod-identity
    # / instance profile) resolve credentials — the recommended EKS setup.
    return AsyncAnthropicBedrock(
        aws_region=config.aws_region or None,
        aws_access_key=config.aws_access_key_id or None,
        aws_secret_key=config.aws_secret_access_key or None,
        aws_session_token=config.aws_session_token or None,
    )
