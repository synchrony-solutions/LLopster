"""Token-to-dollar conversion for the dashboard's spend telemetry.

We surface per-day spend so operators can answer two questions:
  1. "Is the agent burning money?" (the symptom that caused us to ship
     dedup + manual-mode in the first place — a runaway loop cost the
     first cluster install the entire month's API budget in 24 hours)
  2. "How much is prompt caching actually saving us?" (the cache write
     premium only pays back if read-hits are high)

Prices below are list per anthropic.com/pricing as of 2026-05 — keep
them centralised so the next price change is a one-line edit. We model
the 1M-token tier; the larger Sonnet/Opus tiers use the same prefix
caching ratios so this stays linear.

`compute_run_cost` accepts an "anything that has the four token
attributes" so the dashboard's stats page, the run-detail tooltip, and
the per-run SSE event can all reuse the same math.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens for one model family.

    Cache-read is 0.1× input, cache-write is 1.25× input — that ratio
    is industry-standard for prefix caches, but encoded per-model so
    the next Claude release (which may shift the multipliers) is a
    one-line edit rather than a logic change.
    """

    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float


# Anthropic list prices, May 2026. Keep models we actively use; falling
# back to opus-4-7 pricing for unknown models is conservative (it's the
# highest tier — we'd rather overestimate spend than under-report).
PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-7": ModelPricing(
        input_per_mtok=15.0,
        output_per_mtok=75.0,
        cache_read_per_mtok=1.50,
        cache_write_per_mtok=18.75,
    ),
    "claude-sonnet-4-6": ModelPricing(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_read_per_mtok=0.30,
        cache_write_per_mtok=3.75,
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_read_per_mtok=0.10,
        cache_write_per_mtok=1.25,
    ),
}

DEFAULT_PRICING = PRICING["claude-opus-4-7"]


def pricing_for(model: str | None) -> ModelPricing:
    """Best-effort lookup. Unknown / None falls back to Opus (the most
    expensive option) so we never silently under-report spend."""
    if not model:
        return DEFAULT_PRICING
    return PRICING.get(model.strip().lower(), DEFAULT_PRICING)


def compute_cost_usd(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read_tokens: int | None,
    cache_creation_tokens: int | None,
    model: str | None = None,
) -> float:
    """USD spend for a single Anthropic API call.

    None / zero token counts are treated as zero — common when a run
    short-circuited before reaching the LLM (filtered alert, dedup
    skip, manual-mode park). Returning 0.0 in those cases means the
    dashboard's "skipped runs cost nothing" intuition holds.
    """
    p = pricing_for(model)
    return (
        ((input_tokens or 0) / 1_000_000) * p.input_per_mtok
        + ((output_tokens or 0) / 1_000_000) * p.output_per_mtok
        + ((cache_read_tokens or 0) / 1_000_000) * p.cache_read_per_mtok
        + ((cache_creation_tokens or 0) / 1_000_000) * p.cache_write_per_mtok
    )


def cache_savings_usd(
    *,
    cache_read_tokens: int | None,
    model: str | None = None,
) -> float:
    """What we would have paid if those cache-hits had been fresh input.

    Used on the stats page to make the value of prompt caching visible
    in dollars rather than as an abstract hit-rate percentage.
    """
    p = pricing_for(model)
    reads_m = (cache_read_tokens or 0) / 1_000_000
    return reads_m * (p.input_per_mtok - p.cache_read_per_mtok)
