"""Prompt-resolution seam for the three LLM stages.

Each stage (triage / investigation / synthesis) has a baked-in Community
``SYSTEM_PROMPT`` constant in its own module. Those constants remain the
default and the always-available fallback. This module lets a *premium pack*
(loaded at runtime from outside the repo — see ``src/agent/packs.py``) overlay
a tuned prompt on top of the Community base, but only when the deployment is
entitled to that pack (see ``src/agent/entitlements.py``).

The contract is fail-open, always: no pack, no entitlement, or any lookup miss
→ return the exact Community prompt. A pack can only ever *add* to behavior,
never break the baseline.

Wiring
------
- ``main.py`` (the composition root) builds one ``PromptResolver``, seeding it
  with the three Community defaults and loading any mounted packs, then passes
  it into the stage objects. Stages constructed without a resolver (e.g. in
  unit tests) fall back to their module ``SYSTEM_PROMPT`` unchanged.
- A stage calls ``resolver.resolve(STAGE_X, stack=service_pack)`` to get its
  system prompt. ``stack`` comes from the per-service ``pack`` field in
  ``services.yaml`` (``ServiceConfig.pack``); ``None`` means "no pack selected"
  and resolves to the Community prompt (or a stack-agnostic overlay if one is
  registered).

Cache safety
------------
The synthesis prompt is sent as the Anthropic ``system=`` argument — a separate
block from the cached codebase *user* content block. Overlaying the system
prompt therefore does NOT change the codebase prompt-cache prefix, so enabling
a pack does not invalidate the codebase cache. Verify with
``usage.cache_read_input_tokens`` after enabling a pack.

This module intentionally imports NOTHING from the stage modules, so the stage
modules can import the ``STAGE_*`` constants / ``PromptResolver`` from here
without a circular import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Mapping

from src.agent import entitlements

log = logging.getLogger("llopster.prompts")

# Canonical stage identifiers. Keyed by these everywhere (defaults, overlays,
# pack manifests). Keep these strings stable — pack manifests reference them.
STAGE_TRIAGE = "triage"
STAGE_INVESTIGATION = "investigation"
STAGE_SYNTHESIS = "synthesis"
ALL_STAGES = (STAGE_TRIAGE, STAGE_INVESTIGATION, STAGE_SYNTHESIS)

# How a pack overlay is joined onto the Community base. The overlay comes
# *after* the base so its tuned instructions refine (and, where they conflict,
# take precedence over) the generic baseline.
_OVERLAY_SEPARATOR = (
    "\n\n"
    "# ---- Premium pack overlay ----\n"
    "# The following instructions come from an entitled premium pack and "
    "refine the\n# baseline guidance above for this service's technology "
    "stack.\n\n"
)


@dataclass(frozen=True)
class PackPrompt:
    """One stage's prompt overlay contributed by a pack.

    ``stack`` is the value a service sets in its ``pack:`` field to select this
    overlay; ``None`` means the overlay applies to services with no ``pack``
    selected. Overlays are keyed by ``(stage, stack)``.
    """

    pack_id: str
    version: str
    stage: str
    stack: str | None
    text: str


class PromptResolver:
    """Resolves a stage's system prompt: Community default, overlaid by an
    entitled pack prompt when one is registered for ``(stage, stack)``.
    """

    def __init__(
        self,
        defaults: Mapping[str, str],
        *,
        is_pack_enabled: Callable[[str], bool] = entitlements.is_pack_enabled,
    ) -> None:
        # Copy so later mutation of the caller's mapping can't change us.
        self._defaults: dict[str, str] = dict(defaults)
        self._overlays: dict[tuple[str, str | None], PackPrompt] = {}
        self._is_pack_enabled = is_pack_enabled
        # Pack ids we've already warned about being unentitled, so a busy
        # agent doesn't log the same warning on every alert.
        self._unentitled_warned: set[str] = set()

    def register_overlay(self, overlay: PackPrompt) -> None:
        """Register a pack's prompt overlay. Later registrations for the same
        ``(stage, stack)`` win (last pack loaded overrides earlier ones)."""
        if overlay.stage not in self._defaults:
            log.warning(
                "prompts: pack %r targets unknown stage %r — ignoring overlay",
                overlay.pack_id, overlay.stage,
            )
            return
        key = (overlay.stage, overlay.stack)
        if key in self._overlays:
            log.warning(
                "prompts: overlay for %s/%s from pack %r overrides pack %r",
                overlay.stage, overlay.stack, overlay.pack_id,
                self._overlays[key].pack_id,
            )
        self._overlays[key] = overlay
        log.info(
            "prompts: registered overlay pack=%s v=%s stage=%s stack=%s",
            overlay.pack_id, overlay.version, overlay.stage, overlay.stack,
        )

    def resolve(self, stage: str, stack: str | None = None) -> str:
        """Return the system prompt for ``stage`` (optionally for ``stack``).

        Fail-open: any miss — no overlay, or an overlay whose pack is not
        entitled — returns the exact Community default.
        """
        base = self._defaults[stage]
        overlay = self._overlays.get((stage, stack))
        if overlay is None:
            return base
        if not self._is_pack_enabled(overlay.pack_id):
            if overlay.pack_id not in self._unentitled_warned:
                log.warning(
                    "prompts: pack %r present but not entitled — using "
                    "Community prompt for stage=%s stack=%s",
                    overlay.pack_id, stage, stack,
                )
                self._unentitled_warned.add(overlay.pack_id)
            return base
        log.debug(
            "prompts: applying pack=%s overlay for stage=%s stack=%s",
            overlay.pack_id, stage, stack,
        )
        return base + _OVERLAY_SEPARATOR + overlay.text

    def active_overlay(self, stage: str, stack: str | None = None) -> PackPrompt | None:
        """Return the entitled overlay that ``resolve`` would apply, or None.

        Useful for diagnostics / dashboard ("pack X v1.2 applied") without
        re-deriving the entitlement check.
        """
        overlay = self._overlays.get((stage, stack))
        if overlay is not None and self._is_pack_enabled(overlay.pack_id):
            return overlay
        return None
